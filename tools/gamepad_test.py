#!/usr/bin/env python3
"""Probador visual del Wii U GamePad: comprueba todas las funciones del mando desde su propia pantalla.

Se ejecuta en la sesión del GamePad (por ejemplo `GAMEPAD_AUTOSTART="python3 tools/gamepad_test.py"`) o en una
ventana del PC mientras `drc_stream` transmite. Páginas (se cambian tocando las pestañas):

  Resumen · Botones · Táctil · Movimiento · Audio · Cámara · Pantalla · Estado

Cada función tiene una comprobación que se marca sola (botones pulsados, zonas tocadas, sensores que responden,
fotogramas de la cámara...) o que confirmas tú con ✓/✗ (¿se oyó?, ¿vibró?). «Guardar informe» escribe un JSON.
Los sensores, el micrófono y la cámara salen en paquetes UDP: sin root no se pueden leer y esas comprobaciones
quedan «omitidas» (el resto funciona). Interfaz: --iface o WIFI_IFACE de config/drc.conf.

Para ver cada página sin mando: `python3 tools/gamepad_test.py --shots /tmp/paginas` (usa SDL «dummy»).
"""
import argparse
import math
import os
import queue
import subprocess
import sys
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "apps"))

try:
    import pygame  # noqa: E402
except ImportError:
    sys.exit("Falta pygame (Arch: sudo pacman -S python-pygame · Debian: sudo apt install python3-pygame)")
import ui  # noqa: E402
from drchost import padtest as pt  # noqa: E402

W, H = ui.W, ui.H
TOP = 46                       # alto de la barra de pestañas
TABS = ["Resumen", "Botones", "Táctil", "Sensores", "Audio", "Cámara", "Pantalla", "Estado"]
STATE_COLOR = {pt.PASS: ui.OK, pt.FAIL: ui.ERR, pt.PENDING: ui.DIM, pt.SKIP: ui.WARN}
STATE_MARK = {pt.PASS: "●", pt.FAIL: "●", pt.PENDING: "●", pt.SKIP: "●"}   # el color distingue el estado


class App:
    def __init__(self, iface: str, report_path: str):
        self.st, self.rep = pt.PadState(), pt.Report()
        self.report_path = report_path
        self.page = 0
        self.msg, self.msg_until = "", 0.0
        self.status = {}
        self.rec_chunks, self.recording, self.has_rec = [], False, False
        self.cam_view, self.cam_surf = False, None
        self.cam_q = queue.Queue(maxsize=30)
        self.running = True
        self.backlight = 3
        self.nfc_busy = False
        self.iface = iface
        try:
            from drchost.drc_cam_v4l2 import CamH264Builder
            self.cam_builder = CamH264Builder()
        except Exception:
            self.cam_builder = None
        self.evdev = pt.EvdevReader(self.st)
        self.sniffer = pt.Sniffer(self.st, iface, on_cam_chunk=self._cam_chunk, on_mic_pcm=self._mic_pcm,
                                  pen_cfg=pen_cfg())

    # ------------------------------------------------------------ hilos y ayudas
    def start(self):
        self.evdev.start()
        self.sniffer.start()
        threading.Thread(target=self._cam_decoder, daemon=True).start()
        pt.ctl_send("set mic 1")
        pt.ctl_send("set audio_on 1")

    def stop(self):
        self.running = False
        self.evdev.running = False
        self.sniffer.running = False
        pt.ctl_send("set mic 0")
        pt.ctl_send("set camera 0")

    def say(self, text, secs=3.0):
        self.msg, self.msg_until = text, time.monotonic() + secs

    def bg(self, fn):
        def run():
            try:
                fn()
            except Exception as e:          # un hilo que muere en silencio deja el botón sin respuesta
                self.say(f"Error: {type(e).__name__}: {e}", 6.0)
        threading.Thread(target=run, daemon=True).start()

    def _mic_pcm(self, pcm):
        if self.recording:
            self.rec_chunks.append(pcm)

    def _cam_chunk(self, msg):
        if self.cam_builder is None:
            return
        frame = self.cam_builder.push(msg)
        if frame:
            self.st.cam_frames += 1
            self.st.cam_last_bytes = len(frame)
            if self.cam_view:
                try:
                    self.cam_q.put_nowait(frame)
                except queue.Full:
                    pass

    def _cam_decoder(self):
        fw, fh = 240, 180
        while self.running:
            if not self.cam_view:
                time.sleep(0.1)
                continue
            try:
                p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
                                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{fw}x{fh}", "pipe:1"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                self.say("Falta ffmpeg: no se puede ver la cámara")
                self.cam_view = False
                continue

            def feed():
                while self.running and self.cam_view and p.poll() is None:
                    try:
                        p.stdin.write(self.cam_q.get(timeout=0.2))
                        p.stdin.flush()
                    except queue.Empty:
                        continue
                    except OSError:
                        break
            threading.Thread(target=feed, daemon=True).start()
            while self.running and self.cam_view and p.poll() is None:
                raw = p.stdout.read(fw * fh * 3)
                if len(raw) != fw * fh * 3:
                    break
                self.cam_surf = pygame.image.frombuffer(raw, (fw, fh), "RGB")
            try:
                p.kill()
            except OSError:
                pass

    # ------------------------------------------------------------ acciones
    def auto(self):
        self.say("Comprobando…")
        pt.auto_checks(self.rep)
        self.say("Comprobaciones automáticas hechas")

    def record(self):
        if self.recording:
            return

        def work():
            self.rec_chunks, self.recording = [], True
            time.sleep(4)
            self.recording = False
            raw = b"".join(self.rec_chunks)
            if not raw:
                self.say("Sin audio del micrófono (¿root? ¿mic activado?)")
                return
            open("/tmp/gamepad_test_rec.raw", "wb").write(raw)
            subprocess.run("ffmpeg -y -f s16le -ar 16000 -ac 1 -i /tmp/gamepad_test_rec.raw -af volume=5.0 -ar 48000 -ac 2 "
                           "/tmp/gamepad_test_rec.wav", shell=True, capture_output=True)
            self.has_rec = True
            self.say("Grabado")
        self.bg(work)

    def play(self, path="/tmp/gamepad_test_rec.wav"):
        def work():
            pt.ctl_send("set audio_on 1")
            cmd = pt.play_command(path)
            if cmd is None:
                self.say("Falta un reproductor: aplay (alsa-utils) o paplay/pw-play (PipeWire)", 6.0)
                return
            subprocess.run(cmd, capture_output=True)
        self.bg(work)

    def tone(self):
        path = "/tmp/gamepad_test_tone.wav"
        if not os.path.exists(path):
            with wave.open(path, "wb") as w:
                w.setnchannels(2), w.setsampwidth(2), w.setframerate(48000)
                frames = b"".join(int(9000 * math.sin(2 * math.pi * 440 * i / 48000)).to_bytes(2, "little", signed=True) * 2
                                  for i in range(48000))
                w.writeframes(frames)
        self.play(path)

    def vibrate(self, pattern):
        def work():
            for ms, gap in pattern:
                pt.ctl_send(f"vibrate {ms}")
                time.sleep(ms / 1000 + gap)
        self.bg(work)

    def set_backlight(self, delta):
        self.backlight = max(1, min(5, self.backlight + delta))
        pt.ctl_send(f"set backlight {self.backlight}")

    def lcd_blink(self):
        def work():
            pt.ctl_send("set lcd_on 0")
            time.sleep(1.2)
            pt.ctl_send("set lcd_on 1")
            # lcd_on 1 manda 0xff, el único valor que enciende el LCD (LVC 0x36e82; comprobado en el mando 2026-11-16)
            self.say("Pantalla apagada 1 s y encendida de nuevo", 4.0)
        self.bg(work)

    def detect_nfc(self):
        if self.nfc_busy:
            return
        self.nfc_busy = True

        def work():
            try:
                from drchost.drc_nfc import Nfc, PadLink
                self.say("Acerca una etiqueta NFC al mando (10 s)…", 10)
                d = Nfc(PadLink()).detect(10.0)
                if d.get("presente"):
                    self.rep.set("nfc", pt.PASS, d.get("uid", ""))
                    self.say("NFC: " + d.get("uid", "etiqueta detectada"))
                else:
                    self.rep.set("nfc", pt.FAIL, "ninguna etiqueta en 10 s")
                    self.say("NFC: no se detectó ninguna etiqueta")
            except Exception as e:
                self.rep.set("nfc", pt.FAIL, str(e))
            finally:
                self.nfc_busy = False
        self.bg(work)

    def confirm(self, key, ok):
        self.rep.set(key, pt.PASS if ok else pt.FAIL, "confirmado por la persona" if ok else "la persona indica que no")

    def refresh(self):
        pt.update_input_checks(self.rep, self.st)
        if self.st.sniffer_error:
            for k in ("accel", "gyro", "mic", "camera"):
                if self.rep.by[k].state == pt.PENDING:
                    self.rep.set(k, pt.SKIP, self.st.sniffer_error)


# ---------------------------------------------------------------------- dibujo
def bar(surf, x, y, w, h, frac, color):
    ui.panel(surf, (x, y, w, h), ui.PANEL, 4)
    f = max(0.0, min(1.0, frac))
    if f > 0:
        pygame.draw.rect(surf, color, (x + 2, y + 2, int((w - 4) * f), h - 4), border_radius=3)


def confirm_buttons(app, surf, key, y, label, x=24):
    """✓ / ✗ para una comprobación manual; devuelve los botones para el manejador de toques."""
    c = app.rep.by[key]
    ui.text(surf, f"{label}  [{c.state}]", (x, y), 15, STATE_COLOR[c.state], bold=True)
    b_ok = ui.Button((x, y + 26, 120, 36), "Sí", color=(30, 90, 70))
    b_no = ui.Button((x + 128, y + 26, 120, 36), "No", color=(110, 40, 50))
    b_ok.draw(surf)
    b_no.draw(surf)
    return b_ok, b_no


class Pages:
    def __init__(self, app):
        self.app = app
        self.buttons = {}        # botones de la página actual, rehechos al dibujar

    # --- cabecera
    def tabs(self, surf):
        w = W // len(TABS)
        for i, name in enumerate(TABS):
            r = pygame.Rect(i * w, 0, w, TOP)
            active = i == self.app.page
            pygame.draw.rect(surf, ui.ACCENT if active else ui.PANEL, r)
            pygame.draw.rect(surf, ui.BG, r, 1)
            ui.text(surf, name, (r.centerx, 13), 16, ui.TEXT if active else ui.DIM, bold=active, center=True)

    def draw(self, surf):
        a = self.app
        surf.fill(ui.BG)
        self.tabs(surf)
        self.buttons = {}
        getattr(self, "p_" + ["resumen", "botones", "tactil", "sensores", "audio", "camara", "pantalla", "estado"][a.page])(surf)
        if time.monotonic() < a.msg_until:
            ui.panel(surf, (20, H - 34, W - 40, 28), ui.PANEL2, 8)
            ui.text(surf, a.msg, (W // 2, H - 31), 15, ui.TEXT, center=True)

    def btn(self, key, rect, label, color=ui.PANEL2, size=16):
        b = ui.Button(rect, label, color=color, size=size)
        b.draw(self.app_surf)
        self.buttons[key] = b
        return b

    # --- páginas
    def p_resumen(self, surf):
        a = self.app
        self.app_surf = surf
        grupos = {}
        for c in a.rep.checks:
            grupos.setdefault(c.group, []).append(c)
        cols = [[g for g in list(grupos)[:4]], [g for g in list(grupos)[4:]]]
        for ci, gs in enumerate(cols):
            x, y = 20 + ci * 420, TOP + 10
            for g in gs:
                ui.text(surf, g.upper(), (x, y), 13, ui.ACCENT, bold=True)
                y += 18
                for c in grupos[g]:
                    ui.text(surf, STATE_MARK[c.state], (x, y), 16, STATE_COLOR[c.state], bold=True)
                    ui.text(surf, c.title + ("  (manual)" if c.manual else ""), (x + 22, y), 14, ui.TEXT, maxw=380)
                    y += 20
                y += 6
        n = a.rep.summary()
        ui.text(surf, f"{n[pt.PASS]} ok · {n[pt.FAIL]} fallo · {n[pt.PENDING]} pendientes · {n[pt.SKIP]} omitidas",
                (20, H - 112), 15, ui.DIM)
        self.btn("auto", (20, H - 82, 200, 40), "Comprobar (auto)")
        self.btn("save", (232, H - 82, 200, 40), "Guardar informe")
        self.btn("quit", (644, H - 82, 190, 40), "Salir", (110, 40, 50))

    def p_botones(self, surf):
        a, st = self.app, self.app.st
        self.app_surf = surf
        if not st.evdev_ok:
            ui.text(surf, "Sin dispositivo «Wii U GamePad»: ¿transmite drc_stream?", (20, H - 82), 14, ui.WARN)
        layout = {"L": (30, 70), "R": (730, 70), "ZL": (30, 20), "ZR": (730, 20), "-": (330, 80), "HOME": (400, 80),
                  "+": (470, 80), "X": (700, 130), "Y": (660, 175), "A": (740, 175), "B": (700, 220),
                  "UP": (110, 140), "LEFT": (70, 180), "RIGHT": (150, 180), "DOWN": (110, 220),
                  "L3": (230, 300), "R3": (550, 300), "TV": (335, 130), "POWER": (415, 130)}
        for name, (x, y) in layout.items():
            on = st.buttons.get(name, False)
            ever = name in st.pressed_ever
            col = ui.ACCENT if on else (ui.OK if ever else ui.PANEL2)
            r = pygame.Rect(x, TOP + y, 56 if len(name) < 3 else 64, 38)
            ui.panel(surf, r, col, 8)
            ui.text(surf, name, (r.centerx, r.y + 9), 15, ui.TEXT, bold=True, center=True)
        for key, (cx, cy) in {"l": (290, 380), "r": (570, 380)}.items():
            click = key.upper() + "3"          # clic del stick: L3 / R3
            pulsado, hecho = st.buttons.get(click, False), click in st.pressed_ever
            pygame.draw.circle(surf, (20, 70, 60) if pulsado else ui.PANEL, (cx, cy - 20), 60)
            pygame.draw.circle(surf, ui.OK if hecho else ui.DIM, (cx, cy - 20), 60, 4 if hecho else 2)
            px = cx + int(st.sticks[key + "x"] * 45)
            py = cy - 20 + int(st.sticks[key + "y"] * 45)
            pygame.draw.circle(surf, ui.OK if hecho else ui.ACCENT, (px, py), 14)
        dirs = a.st.stick_dirs
        ui.text(surf, f"Sticks: {len(dirs)}/8 direcciones", (355, TOP + 335), 14, ui.DIM)
        ui.text(surf, a.rep.by["buttons"].detail, (20, H - 60), 13, ui.DIM, maxw=W - 40)
        ui.text(surf, "Verde = ya pulsado · morado = pulsado ahora", (20, H - 40), 13, ui.DIM)

    def p_tactil(self, surf):
        a, st = self.app, self.app.st
        self.app_surf = surf
        area = pygame.Rect(0, TOP, W, H - TOP)
        for r in range(3):
            for c in range(3):
                z = f"{r}{c}"
                zr = pygame.Rect(c * W // 3, TOP + r * (H - TOP) // 3, W // 3, (H - TOP) // 3)
                pygame.draw.rect(surf, (20, 70, 60) if z in st.touch_zones else ui.PANEL, zr.inflate(-4, -4), border_radius=8)
        pts = st.touch_trail[-200:]
        sx, sy = W / pt.TOUCH_W, (H - TOP) / pt.TOUCH_H
        for i in range(1, len(pts)):
            pygame.draw.line(surf, ui.ACCENT, (pts[i - 1][0] * sx, TOP + pts[i - 1][1] * sy),
                             (pts[i][0] * sx, TOP + pts[i][1] * sy), 3)
        pres = pt.pressure_shown(st)
        if st.touch:
            c = (int(st.touch[0] * sx), int(TOP + st.touch[1] * sy))
            if pres is not None:
                # Círculo que crece con la presión (16 px sin presión … 80 px a 1023), relleno translúcido
                r = pt.pressure_radius(pres[0])
                halo = pygame.Surface((2 * r + 2, 2 * r + 2), pygame.SRCALPHA)
                pygame.draw.circle(halo, (*ui.WARN, 70), (r + 1, r + 1), r)
                surf.blit(halo, (c[0] - r - 1, c[1] - r - 1))
                pygame.draw.circle(surf, ui.WARN, c, r, 3)
            pygame.draw.circle(surf, ui.TEXT, c, 4)
        ui.text(surf, f"Zonas tocadas: {len(st.touch_zones)}/9   " + (f"({st.touch[0]}, {st.touch[1]})" if st.touch else ""),
                (W // 2, TOP + 8), 16, ui.TEXT, bold=True, center=True)
        # Presión: la del lápiz (lo que recibe Krita) o la calculada del paquete con el modelo de drc_stream
        if pres is None:
            ui.text(surf, "Presión: sin datos (modo lápiz o sudo para leer el paquete del mando)",
                    (W // 2, H - 22), 13, ui.DIM, center=True)
        else:
            val, origen = pres
            raw = st.touch_raw["pressure"] if st.touch_raw and origen == "calculada" else None
            pygame.draw.rect(surf, ui.BG, (W // 2 - 154, H - 44, 308, 20), border_radius=6)   # fondo visible de la barra
            bar(surf, W // 2 - 150, H - 40, 300, 12, val / 1023, ui.WARN)
            ui.text(surf, f"Presión {val:4d}/1023 ({origen})" + (f" · crudo {raw}" if raw else ""),
                    (W // 2, H - 22), 14, ui.TEXT, center=True)
        if not st.touch_ok:
            ui.text(surf, "Sin dispositivo «Wii U GamePad Touchscreen»", (W // 2, TOP + 34), 14, ui.WARN, center=True)
        elif st.touch_events == 0:
            ui.text(surf, "El táctil llega como puntero (touch2pointer tiene el dispositivo): se usa el ratón", (W // 2, TOP + 34), 13, ui.DIM, center=True)

    def p_sensores(self, surf):
        a, st = self.app, self.app.st
        self.app_surf = surf
        if st.sniffer_error:
            ui.text(surf, st.sniffer_error, (20, TOP + 10), 14, ui.WARN, maxw=W - 40)
            return
        cx, cy, rad = 200, TOP + 190, 110
        pygame.draw.circle(surf, ui.PANEL, (cx, cy), rad)
        pygame.draw.circle(surf, ui.DIM, (cx, cy), rad, 2)
        ax, ay, az = st.acc
        bx = cx + max(-1, min(1, ax / 8500.0)) * (rad - 16)
        by = cy - max(-1, min(1, ay / 8500.0)) * (rad - 16)
        pygame.draw.circle(surf, ui.OK, (int(bx), int(by)), 14)
        ui.text(surf, f"Acelerómetro  x={ax} y={ay} z={az}", (cx, cy + rad + 12), 14, ui.TEXT, center=True)
        ui.text(surf, f"{a.rep.by['accel'].state}: {a.rep.by['accel'].detail or 'mueve/inclina el mando'}", (cx, cy + rad + 34),
                14, STATE_COLOR[a.rep.by["accel"].state], center=True)
        names = ["roll", "yaw", "pitch"]
        for i, (n, v) in enumerate(zip(names, st.gyro)):
            y = TOP + 70 + i * 70
            ui.text(surf, f"{n}: {v:+7.1f} °/s", (450, y - 22), 14, ui.TEXT)
            bar(surf, 450, y, 360, 22, abs(v) / 250.0, ui.WARN)
        ui.text(surf, f"Giroscopio  {a.rep.by['gyro'].state}: {a.rep.by['gyro'].detail or 'gira el mando'}", (450, TOP + 290),
                14, STATE_COLOR[a.rep.by["gyro"].state])

    def p_audio(self, surf):
        a, st = self.app, self.app.st
        self.app_surf = surf
        ui.text(surf, "Micrófono", (24, TOP + 10), 16, ui.ACCENT, bold=True)
        bar(surf, 24, TOP + 36, 400, 26, st.mic_level, ui.OK if st.mic_level < 0.7 else ui.ERR)
        c = a.rep.by["mic"]
        ui.text(surf, f"{c.state}: {c.detail or 'habla cerca del mando'}", (24, TOP + 68), 14, STATE_COLOR[c.state])
        if st.sniffer_error:
            ui.text(surf, st.sniffer_error, (24, TOP + 90), 13, ui.WARN, maxw=400)
        self.btn("rec", (24, TOP + 120, 190, 40), "● Grabar 4 s" if not a.recording else "Grabando…", (150, 40, 40))
        self.btn("playrec", (224, TOP + 120, 200, 40), "Oír grabación", (30, 100, 60) if a.has_rec else ui.PANEL)
        ui.text(surf, "Altavoces", (460, TOP + 10), 16, ui.ACCENT, bold=True)
        self.btn("tone", (460, TOP + 36, 200, 40), "♪ Tono 440 Hz")
        b = confirm_buttons(a, surf, "speaker", TOP + 86, "¿Se oyó el tono?", x=460)
        self.buttons["spk_ok"], self.buttons["spk_no"] = b
        ui.text(surf, "Volumen", (24, TOP + 200), 16, ui.ACCENT, bold=True)
        s = a.status
        ui.text(surf, f"Ganancia {s.get('volume', '?')}%   Deslizador físico {s.get('slider', '?')}", (24, TOP + 226), 15)
        self.btn("vol-", (24, TOP + 256, 90, 40), "− vol")
        self.btn("vol+", (124, TOP + 256, 90, 40), "+ vol")
        self.btn("volchk", (224, TOP + 256, 200, 40), "Marcar volumen")

    def p_camara(self, surf):
        a, st = self.app, self.app.st
        self.app_surf = surf
        box = pygame.Rect(24, TOP + 14, 480, 360)
        pygame.draw.rect(surf, ui.PANEL, box, border_radius=8)
        if a.cam_surf is not None and a.cam_view:
            surf.blit(pygame.transform.smoothscale(a.cam_surf, box.size), box.topleft)
        else:
            ui.text(surf, "Cámara apagada" if not a.cam_view else "Esperando fotogramas…", (box.centerx, box.centery - 10),
                    16, ui.DIM, center=True)
        c = a.rep.by["camera"]
        ui.text(surf, f"{c.state}: {c.detail or '—'}", (530, TOP + 20), 15, STATE_COLOR[c.state])
        ui.text(surf, f"Fotogramas: {st.cam_frames}", (530, TOP + 48), 15)
        ui.text(surf, f"Último: {st.cam_last_bytes // 1024} KB   Paquetes: {st.cam_packets}", (530, TOP + 72), 14, ui.DIM)
        self.btn("cam", (530, TOP + 110, 290, 44), "Apagar cámara" if a.cam_view else "Encender cámara",
                 (20, 110, 90) if a.cam_view else ui.PANEL2)
        if st.sniffer_error:
            ui.text(surf, st.sniffer_error, (530, TOP + 170), 13, ui.WARN, maxw=300)

    def p_pantalla(self, surf):
        a = self.app
        self.app_surf = surf
        ui.text(surf, "Retroiluminación", (24, TOP + 10), 16, ui.ACCENT, bold=True)
        ui.text(surf, f"Nivel {a.backlight}/5", (24, TOP + 36), 15)
        self.btn("bl-", (24, TOP + 62, 100, 40), "− brillo")
        self.btn("bl+", (134, TOP + 62, 100, 40), "+ brillo")
        self.btn("lcd", (244, TOP + 62, 180, 40), "Apagar 1 s")
        b = confirm_buttons(a, surf, "backlight", TOP + 112, "¿Se vio el cambio?")
        self.buttons["bl_ok"], self.buttons["bl_no"] = b
        ui.text(surf, "Vibración", (460, TOP + 10), 16, ui.ACCENT, bold=True)
        self.btn("v1", (460, TOP + 36, 170, 40), "Corta")
        self.btn("v2", (640, TOP + 36, 170, 40), "3 pulsos")
        self.btn("v3", (460, TOP + 84, 170, 40), "Larga 1 s")
        b = confirm_buttons(a, surf, "vibration", TOP + 134, "¿Vibró?", x=460)
        self.buttons["vib_ok"], self.buttons["vib_no"] = b
        ui.text(surf, "Barra sensora IR", (24, TOP + 230), 16, ui.ACCENT, bold=True)
        self.btn("sb1", (24, TOP + 256, 100, 40), "Encender")
        self.btn("sb0", (134, TOP + 256, 100, 40), "Apagar")
        ui.text(surf, "(el LED IR se ve con la cámara de un móvil)", (244, TOP + 266), 13, ui.DIM)

    def p_estado(self, surf):
        a = self.app
        self.app_surf = surf
        s = a.status
        ui.text(surf, "Estado del mando (/tmp/drc_status)", (24, TOP + 10), 16, ui.ACCENT, bold=True)
        if not s.get("valid"):
            ui.text(surf, "Sin estado válido: ¿transmite drc_stream?", (24, TOP + 38), 15, ui.WARN)
        else:
            bat = s.get("battery", 0)
            ui.text(surf, f"Batería {bat}/6   " + ("cargando" if s.get("charging") else "con batería"), (24, TOP + 38), 15)
            bar(surf, 24, TOP + 64, 300, 22, bat / 6.0, ui.OK if bat > 2 else ui.ERR)
            ui.text(surf, f"Corriente alterna: {'sí' if s.get('ac') else 'no'}   Deslizador: {s.get('slider')}   "
                          f"Ganancia: {s.get('volume')}%", (24, TOP + 96), 14, ui.DIM)
        self.btn("q_fw", (24, TOP + 140, 200, 40), "Leer firmware")
        self.btn("q_rssi", (234, TOP + 140, 200, 40), "Leer señal (RSSI)")
        self.btn("nfc", (444, TOP + 140, 200, 40), "Detectar NFC", ui.PANEL if a.nfc_busy else ui.PANEL2)
        y = TOP + 200
        for k in ("link", "fw", "rssi", "battery", "nfc"):
            c = a.rep.by[k]
            ui.text(surf, f"{STATE_MARK[c.state]} {c.title}: {c.detail}", (24, y), 14, STATE_COLOR[c.state], maxw=W - 48)
            y += 22

    # --- toques
    def click(self, pos):
        a = self.app
        if pos[1] < TOP:
            a.page = min(len(TABS) - 1, pos[0] * len(TABS) // W)
            return True
        for key, b in self.buttons.items():
            if not b.hit(pos):
                continue
            if key == "auto":
                a.bg(a.auto)
            elif key == "save":
                a.rep.save(a.report_path)
                a.say("Informe guardado en " + a.report_path, 4)
            elif key == "quit":
                return False
            elif key == "rec":
                a.record()
            elif key == "playrec" and a.has_rec:
                a.play()
            elif key == "tone":
                a.tone()
            elif key in ("spk_ok", "spk_no"):
                a.confirm("speaker", key == "spk_ok")
            elif key in ("bl_ok", "bl_no"):
                a.confirm("backlight", key == "bl_ok")
            elif key in ("vib_ok", "vib_no"):
                a.confirm("vibration", key == "vib_ok")
            elif key == "vol-":
                pt.ctl_send("volume -10")
            elif key == "vol+":
                pt.ctl_send("volume +10")
            elif key == "volchk":
                a.rep.set("volume", pt.PASS, f"ganancia={a.status.get('volume')} deslizador={a.status.get('slider')}")
            elif key == "cam":
                a.cam_view = not a.cam_view
                pt.ctl_send("set camera 1" if a.cam_view else "set camera 0")
                if not a.cam_view:
                    a.cam_surf = None
            elif key == "bl-":
                a.set_backlight(-1)
            elif key == "bl+":
                a.set_backlight(+1)
            elif key == "lcd":
                a.lcd_blink()
            elif key == "v1":
                a.vibrate([(250, 0)])
            elif key == "v2":
                a.vibrate([(250, 0.3)] * 3)
            elif key == "v3":
                a.vibrate([(1000, 0)])
            elif key in ("sb1", "sb0"):
                r = pt.ctl_query("set sensorbar " + key[-1], "sensorbar")
                a.say("Barra sensora: " + (r or "sin respuesta"))
            elif key == "q_fw":
                r = pt.reply_bytes(pt.ctl_query("get fw_version", "fw_version"))
                a.rep.set("fw", pt.PASS if r else pt.FAIL, r.hex() if r else "sin respuesta")
            elif key == "q_rssi":
                r = pt.reply_bytes(pt.ctl_query("get rssi", "rssi"))
                a.rep.set("rssi", pt.PASS if r else pt.FAIL, r.hex() if r else "sin respuesta")
            elif key == "nfc":
                a.detect_nfc()
            break
        return True


def pen_cfg():
    """Ajustes de presión del lápiz como los usa drc_stream: drc.conf y, por encima, el entorno."""
    conf = {}
    try:
        from drchost.drc_ap import load_drc_conf
        conf = dict(load_drc_conf())
    except Exception:
        pass
    conf.update({k: v for k, v in os.environ.items() if k.startswith("DRC_PEN_PRESSURE_")})
    return pt.pen_config(conf)


def default_iface():
    try:
        from drchost.drc_ap import load_drc_conf
        return load_drc_conf().get("WIFI_IFACE", "wlan0")
    except Exception:
        return "wlan0"


def entorno_grafico_de_sudo(env=None, runtime_root="/run/user", en_mando=False, drc_root="/tmp"):
    """`sudo` borra XDG_RUNTIME_DIR y WAYLAND_DISPLAY y la ventana no se podía abrir. Si corremos como root por sudo,
    se recupera la sesión gráfica de quien lo lanzó (nunca la `wayland-drc` del propio GamePad). Devuelve lo que cambió."""
    env = os.environ if env is None else env
    uid = env.get("SUDO_UID")
    cambios = {}
    if not uid or env.get("XDG_RUNTIME_DIR"):
        return cambios
    run = os.path.join(runtime_root, uid)
    if en_mando:
        # la sesión del GamePad (cage) tiene su propio directorio de ejecución (start_drc.sh): ahí está wayland-drc.
        # El probador, como root, abre su ventana allí y lee sensores, micrófono y cámara.
        run = os.path.join(drc_root, f"drc_runtime_{uid}")
        cambios["WAYLAND_DISPLAY"] = "wayland-drc"
    cambios["XDG_RUNTIME_DIR"] = run
    if not en_mando and not env.get("WAYLAND_DISPLAY"):
        socks = sorted(n for n in (os.listdir(run) if os.path.isdir(run) else [])
                       if n.startswith("wayland-") and n != "wayland-drc" and not n.endswith((".lock", ".sock", ".ctl")))
        if socks:
            cambios["WAYLAND_DISPLAY"] = socks[0]
    if not env.get("DISPLAY"):
        cambios["DISPLAY"] = ":0"
    env.update(cambios)
    return cambios


def main(argv=None):
    ap = argparse.ArgumentParser(description="Probador visual del Wii U GamePad")
    ap.add_argument("--iface", default=None, help="interfaz Wi-Fi del GamePad (por defecto WIFI_IFACE de drc.conf)")
    ap.add_argument("--report", default=os.path.join(os.path.expanduser("~" + os.environ.get("SUDO_USER", "")),
                                                     "gamepad_test_report.json"))
    ap.add_argument("--en-mando", action="store_true",
                    help="abre la ventana en la pantalla del GamePad (sesión wayland-drc) en vez de en la de este PC; "
                         "con sudo, así los sensores, el micrófono y la cámara funcionan")
    ap.add_argument("--shots", metavar="DIR", help="guarda una captura de cada página y sale (sin mando, SDL dummy)")
    a = ap.parse_args(argv)
    if a.shots:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
    else:
        cambios = entorno_grafico_de_sudo(en_mando=a.en_mando)
        if a.en_mando:
            run = cambios.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR", "")
            if cambios.get("WAYLAND_DISPLAY") is None:
                os.environ["WAYLAND_DISPLAY"] = "wayland-drc"
            if not os.path.exists(os.path.join(run, "wayland-drc")):
                sys.exit("No hay sesión del GamePad (wayland-drc): arranca antes la transmisión en modo separate "
                         "(python3 drchost/drc_ctl.py start --mode separate)")
            os.environ["SDL_VIDEODRIVER"] = "wayland"
        os.environ.setdefault("SDL_VIDEODRIVER", "wayland,x11")
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_caption("Wii U GamePad - Probador")
    screen = pygame.display.set_mode((W, H), 0 if a.shots else pygame.RESIZABLE)
    lienzo = ui.Lienzo()
    app = App(a.iface or default_iface(), a.report)
    pages = Pages(app)
    if a.shots:
        os.makedirs(a.shots, exist_ok=True)
        app.status = {"valid": 1, "battery": 4, "charging": 0, "ac": 0, "slider": 128, "volume": 80}
        for i, name in enumerate(TABS):
            app.page = i
            pages.draw(lienzo.canvas)
            pygame.image.save(lienzo.canvas, os.path.join(a.shots, f"{i}_{name}.png"))
        print("capturas en", a.shots)
        return 0
    app.start()
    clock, run, last_status = pygame.time.Clock(), True, 0.0
    while run and app.running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q)):
                run = False
            elif ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEMOTION, pygame.MOUSEBUTTONUP):
                cx, cy = lienzo.pos(ev.pos)
                if app.page == 2 and cy >= TOP and app.st.touch_events == 0:      # página Táctil, toques que llegan como puntero
                    down = ev.type != pygame.MOUSEBUTTONUP and (ev.type != pygame.MOUSEMOTION or ev.buttons[0])
                    if ev.type != pygame.MOUSEMOTION or app.st.touch is not None or down:
                        pt.apply_pointer_touch(app.st, int(cx * pt.TOUCH_W / W), int((cy - TOP) * pt.TOUCH_H / (H - TOP)), down)
                if ev.type == pygame.MOUSEBUTTONDOWN and not pages.click((cx, cy)):
                    run = False
        if time.monotonic() - last_status > 1.0:
            app.status, last_status = pt.read_status(), time.monotonic()
        app.refresh()
        pages.draw(lienzo.canvas)
        lienzo.presentar()
        clock.tick(30)
    app.stop()
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
