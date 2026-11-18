#!/usr/bin/env python3
"""App táctil de la cámara del GamePad: imagen en vivo, foto, ajustes del sensor y espejo.

Se lanza desde el menú: `sudo -E env SDL_VIDEODRIVER=wayland python3 /opt/drc/apps/drc_camera_app.py`
(necesita root: escucha los paquetes de la cámara con un socket crudo, igual que drc_cam_v4l2.py).
La cámara del mando emite 640x480 H.264 a ~10 fps; fps y enfoque no son ajustables.
"""
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time

import pygame

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui  # noqa: E402

CTL = "/tmp/drc_ctl"
CAM_W, CAM_H = 640, 480
AP_IP = "192.168.1.10"


def ctl(linea, ruta=CTL):
    """Manda una orden a drc_stream por su FIFO (no se cuelga si drc_stream no está)."""
    try:
        fd = os.open(ruta, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (linea + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def interfaz_ap(salida_ip=None):
    """Interfaz Wi-Fi donde está el AP del GamePad (la que tiene 192.168.1.10), o None."""
    if os.environ.get("DRC_IFACE"):
        return os.environ["DRC_IFACE"]
    if salida_ip is None:
        try:
            salida_ip = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return None
    for linea in salida_ip.splitlines():
        t = linea.split()
        if len(t) >= 4 and t[3].startswith(AP_IP + "/"):
            return t[1]
    return None


class Ajuste:
    """Un parámetro del sensor con botones − / +. `valor=None` significa «automático / por defecto»."""

    def __init__(self, clave, etiqueta, minimo, maximo, paso, inicio, auto=False):
        self.clave, self.etiqueta, self.min, self.max, self.paso, self.inicio, self.auto = clave, etiqueta, minimo, maximo, paso, inicio, auto
        self.valor = None

    def mover(self, sentido):
        """Un paso (+1/−1). Devuelve la orden para drc_stream."""
        if self.valor is None:
            self.valor = self.inicio
        else:
            self.valor = max(self.min, min(self.max, self.valor + sentido * self.paso))
        return self.orden()

    def restablecer(self):
        self.valor = None
        return self.orden()

    def orden(self):
        if self.valor is None:
            return "cam %s auto" % self.clave if self.auto else "cam %s %d" % (self.clave, self.inicio)
        return "cam %s %d" % (self.clave, self.valor)

    def texto(self):
        if self.valor is None:
            return "auto" if self.auto else "normal"
        return str(self.valor)


def ajustes_por_defecto():
    return [
        Ajuste("brightness", "Brillo", -100, 100, 10, 0),
        Ajuste("contrast", "Contraste", 0, 100, 10, 50),
        Ajuste("saturation", "Color", 0, 128, 8, 64),
        Ajuste("exposure", "Exposición", 0, 65535, 2000, 8000, auto=True),
        Ajuste("gain", "Ganancia", 0, 111, 8, 32, auto=True),
    ]


def nombre_foto(ahora=None):
    return time.strftime("gamepad-camara-%Y%m%d-%H%M%S.png", ahora or time.localtime())


def carpeta_fotos():
    usuario = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
    home = os.path.expanduser("~" + usuario)
    return os.path.join(home, "Pictures")


class Camara:
    """Recibe los paquetes de la cámara, reensambla el H.264 y lo decodifica con ffmpeg a RGB 640x480."""

    def __init__(self):
        self.corriendo = True
        self.surf = None
        self.frames = 0
        self.fps = 0.0
        self.estado = "Iniciando cámara…"
        self.cola = queue.Queue(maxsize=30)
        self.t_ultimo = 0.0
        self.paquetes = 0      # paquetes de cámara recibidos
        self.cuadros_h264 = 0  # cuadros H.264 reensamblados

    def iniciar(self):
        for f in (self._sniffer, self._decodificador):
            threading.Thread(target=f, daemon=True).start()

    def parar(self):
        self.corriendo = False

    def _sniffer(self):
        from drc_cam_v4l2 import CamH264Builder
        iface = interfaz_ap()
        if not iface:
            self.estado = "No encuentro la interfaz del GamePad."
            return
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0800))
            try:   # con root se puede superar el límite normal: un bache del bucle de dibujo no pierde paquetes
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUFFORCE, 8 << 20)
            except OSError:
                pass
            s.bind((iface, 0))
            s.settimeout(0.3)
        except OSError as e:
            self.estado = "Sin permiso para capturar (¿root?): %s" % e
            return
        b = CamH264Builder()
        ctl("set camera 1")
        self.estado = "Esperando imagen del sensor…"
        while self.corriendo:
            try:
                pkt = s.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(pkt) < 42 or pkt[23] != 17:
                continue
            sport, dport = struct.unpack("!HH", pkt[34:38])
            if sport == 50120 and dport == 50020:
                self.paquetes += 1
                fr = b.push(pkt[42:])
                if fr:
                    self.cuadros_h264 += 1
                    try:
                        self.cola.put_nowait(fr)
                    except queue.Full:
                        pass

    def _decodificador(self):
        while self.corriendo:
            try:
                p = subprocess.Popen(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0",
                     "-f", "h264", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (CAM_W, CAM_H), "pipe:1"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                self.estado = "Falta ffmpeg."
                return

            def alimentar():
                while self.corriendo and p.poll() is None:
                    try:
                        p.stdin.write(self.cola.get(timeout=0.3))
                        p.stdin.flush()
                    except queue.Empty:
                        continue
                    except OSError:
                        break

            threading.Thread(target=alimentar, daemon=True).start()
            tam = CAM_W * CAM_H * 3
            while self.corriendo and p.poll() is None:
                raw = p.stdout.read(tam)
                if len(raw) != tam:
                    break
                self.surf = pygame.image.frombuffer(raw, (CAM_W, CAM_H), "RGB")
                self.frames += 1
                t = time.monotonic()
                if self.t_ultimo:
                    inst = 1.0 / max(t - self.t_ultimo, 1e-3)
                    self.fps = inst if not self.fps else self.fps * 0.8 + inst * 0.2
                self.t_ultimo = t
                self.estado = "EN VIVO"
            try:
                p.kill()
            except OSError:
                pass
            time.sleep(0.3)


def listar_fotos(carpeta):
    """Fotos de la cámara de `carpeta`, las más nuevas primero (el nombre lleva la fecha)."""
    try:
        nombres = [n for n in os.listdir(carpeta) if n.startswith("gamepad-camara-") and n.lower().endswith(".png")]
    except OSError:
        return []
    return sorted((os.path.join(carpeta, n) for n in nombres), reverse=True)


def ruta_corta(ruta, home=None):
    """Ruta con «~» en lugar de la carpeta personal, para que quepa en pantalla."""
    home = home or os.path.dirname(carpeta_fotos())
    return "~" + ruta[len(home):] if ruta.startswith(home + os.sep) else ruta


def cargar_foto(ruta, ancho, alto):
    """Foto escalada para caber en ancho x alto sin deformarla; None si no se puede leer."""
    try:
        img = pygame.image.load(ruta)
    except (pygame.error, OSError):
        return None
    w, h = img.get_size()
    k = min(ancho / w, alto / h)
    return pygame.transform.smoothscale(img, (max(1, int(w * k)), max(1, int(h * k)))) if k != 1 else img


def guardar_foto(surf, espejo):
    carpeta = carpeta_fotos()
    nueva = not os.path.isdir(carpeta)
    os.makedirs(carpeta, exist_ok=True)
    uid = os.environ.get("SUDO_UID")
    if nueva and uid:   # la app corre como root: la carpeta nueva es del usuario, no de root
        try:
            os.chown(carpeta, int(uid), int(os.environ.get("SUDO_GID", uid)))
        except OSError:
            pass
    img = pygame.transform.flip(surf, True, False) if espejo else surf
    ruta = os.path.join(carpeta, nombre_foto())
    pygame.image.save(img, ruta)
    if uid:
        try:
            os.chown(ruta, int(uid), int(os.environ.get("SUDO_GID", uid)))
        except OSError:
            pass
    return ruta


def main():
    pygame.display.init()   # sin pygame.init(): el mezclador de audio abriría el ALSA del GamePad (underruns)
    pygame.font.init()
    pygame.display.set_mode((ui.W, ui.H), pygame.RESIZABLE)   # el WM da menos alto (barra): se escala el lienzo
    lienzo = ui.Lienzo(rapido=True)   # vídeo a 30 fps: escalado barato
    pantalla = lienzo.canvas
    pygame.display.set_caption("Cámara")
    reloj = pygame.time.Clock()
    cam = Camara()
    cam.iniciar()
    ajustes = ajustes_por_defecto()
    espejo, aviso, t_aviso = False, "", 0.0
    px = 640 + 8
    bw = 62   # tres botones pequeños en una fila
    botones = {
        "foto": ui.Button((px, 6, 198, 46), "FOTO", size=20),
        "espejo": ui.Button((px, 58, bw, 34), "Espejo", size=12),
        "reset": ui.Button((px + bw + 6, 58, bw, 34), "Reiniciar", size=12),
        "fotos": ui.Button((px + 2 * (bw + 6), 58, bw, 34), "Fotos", size=12),
        "salir": ui.Button((px, 440, 198, 34), "Salir", size=16),
        # galería
        "ant": ui.Button((px, 6, 96, 46), "◀", size=22),
        "sig": ui.Button((px + 102, 6, 96, 46), "▶", size=22),
        "borrar": ui.Button((px, 380, 198, 44), "Borrar", size=16),
        "volver": ui.Button((px, 430, 198, 44), "Volver", size=16),
    }
    filas = []
    for i, a in enumerate(ajustes):
        y = 100 + i * 66
        filas.append((a, ui.Button((px, y + 20, 44, 38), "−", size=22), ui.Button((px + 154, y + 20, 44, 38), "+", size=22), y))

    # galería: lista de fotos de la carpeta, la que se ve y la imagen ya escalada
    galeria = {"on": False, "fotos": [], "i": 0, "img": None, "confirmar": 0.0}

    def abrir_foto(i):
        fs = galeria["fotos"]
        galeria["i"] = max(0, min(len(fs) - 1, i)) if fs else 0
        galeria["img"] = cargar_foto(fs[galeria["i"]], 640, 480) if fs else None
        galeria["confirmar"] = 0.0

    def entrar_galeria():
        galeria["on"] = True
        galeria["fotos"] = listar_fotos(carpeta_fotos())
        abrir_foto(0)

    corriendo = True
    while corriendo:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                corriendo = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and galeria["on"]:
                p = lienzo.pos(ev.pos)
                fs = galeria["fotos"]
                if botones["volver"].hit(p):
                    galeria["on"] = False
                elif botones["ant"].hit(p) and fs:
                    abrir_foto((galeria["i"] + 1) % len(fs))      # «anterior» = más antigua (la lista va de nuevas a viejas)
                elif botones["sig"].hit(p) and fs:
                    abrir_foto((galeria["i"] - 1) % len(fs))
                elif botones["borrar"].hit(p) and fs:
                    if time.monotonic() - galeria["confirmar"] < 3:   # segundo toque: borrar de verdad
                        try:
                            os.remove(fs[galeria["i"]])
                            aviso = "Foto borrada"
                        except OSError as e:
                            aviso = "No se pudo borrar: %s" % e
                        t_aviso = time.monotonic()
                        galeria["fotos"] = listar_fotos(carpeta_fotos())
                        abrir_foto(galeria["i"])
                    else:
                        galeria["confirmar"] = time.monotonic()
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                p = lienzo.pos(ev.pos)
                if botones["salir"].hit(p):
                    corriendo = False
                elif botones["foto"].hit(p):
                    if cam.surf is not None:
                        try:
                            aviso = "Guardada en " + ruta_corta(guardar_foto(cam.surf, espejo))
                        except OSError as e:
                            aviso = "No se pudo guardar: %s" % e
                    else:
                        aviso = "Aún no hay imagen."
                    t_aviso = time.monotonic()
                elif botones["fotos"].hit(p):
                    entrar_galeria()
                elif botones["espejo"].hit(p):
                    espejo = not espejo
                elif botones["reset"].hit(p):
                    for a in ajustes:
                        ctl(a.restablecer())
                else:
                    for a, menos, mas, _y in filas:
                        if menos.hit(p):
                            ctl(a.mover(-1))
                        elif mas.hit(p):
                            ctl(a.mover(+1))
        pantalla.fill(ui.BG)
        if galeria["on"]:
            fs = galeria["fotos"]
            if galeria["img"] is not None:
                pantalla.blit(galeria["img"], galeria["img"].get_rect(center=(320, 240)))
            else:
                ui.text(pantalla, "Todavía no hay fotos.\nPulsa FOTO en la vista en vivo." if not fs else "No se pudo abrir la foto.",
                        (320, 210), 20, ui.DIM, center=True, maxw=560)
            pygame.draw.rect(pantalla, ui.PANEL, (640, 0, ui.W - 640, ui.H))
            botones["ant"].draw(pantalla)
            botones["sig"].draw(pantalla)
            if fs:
                ui.text(pantalla, "%d de %d" % (galeria["i"] + 1, len(fs)), (px + 99, 64), 17, ui.TEXT, True, center=True)
                ui.text(pantalla, os.path.basename(fs[galeria["i"]]), (px, 94), 12, ui.DIM, maxw=198, maxlines=3)
            ui.text(pantalla, "Carpeta:\n" + ruta_corta(carpeta_fotos()), (px, 160), 13, ui.DIM, maxw=198, maxlines=3)
            confirmando = time.monotonic() - galeria["confirmar"] < 3
            botones["borrar"].label = "Toca otra vez" if confirmando else "Borrar"
            botones["borrar"].draw(pantalla, color=ui.ERR)
            botones["volver"].draw(pantalla)
        else:
            if cam.surf is not None:
                pantalla.blit(pygame.transform.flip(cam.surf, True, False) if espejo else cam.surf, (0, 0))
            else:
                ui.text(pantalla, cam.estado, (320, 220), 20, ui.DIM, center=True, maxw=560)
            pygame.draw.rect(pantalla, ui.PANEL, (640, 0, ui.W - 640, ui.H))
            botones["foto"].draw(pantalla, color=ui.OK)
            botones["espejo"].draw(pantalla, active=espejo)
            botones["reset"].draw(pantalla)
            botones["fotos"].draw(pantalla)
            botones["salir"].draw(pantalla, color=ui.ERR)
            for a, menos, mas, y in filas:
                ui.text(pantalla, a.etiqueta, (px, y), 14, ui.DIM)
                menos.draw(pantalla)
                mas.draw(pantalla)
                ui.text(pantalla, a.texto(), (px + 99, y + 28), 17, ui.TEXT, True, center=True)
            ui.text(pantalla, "%s  %.0f fps" % (cam.estado, cam.fps) if cam.surf is not None else "", (px, 426), 12, ui.OK)
        if aviso and time.monotonic() - t_aviso < 4:
            ui.panel(pantalla, (8, 440, 620, 32), (0, 0, 0))
            ui.text(pantalla, aviso, (16, 446), 15, ui.TEXT, maxw=600, maxlines=1)
        lienzo.presentar()
        reloj.tick(30)
    cam.parar()
    ctl("set camera 0")
    pygame.quit()


if __name__ == "__main__":
    main()
