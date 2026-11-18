#!/usr/bin/env python3
"""Lógica del probador del GamePad (sin interfaz): lecturas, órdenes y lista de comprobaciones.

La app gráfica es tools/gamepad_test.py; `python3 -m drchost.padtest` ejecuta solo las comprobaciones
automáticas y escribe un informe JSON (útil para comprobar un port a otro equipo sin tocar el mando).

Fuentes de datos (todas las crea `drc_stream` al transmitir):
  - dispositivos evdev «Wii U GamePad» (botones, sticks, gatillos, cruceta) y «Wii U GamePad Touchscreen»;
  - /tmp/drc_status   batería, carga, corriente, deslizador de volumen y ganancia;
  - /tmp/drc_ctl      órdenes (get/set/vibrate/cam/raw...), respuestas en /tmp/drc_ctl.out;
  - paquetes UDP del mando (sensores 50122->50022, micrófono 50121->50021, cámara 50120->50020) con un socket
    AF_PACKET: necesita root y la interfaz Wi-Fi del GamePad (WIFI_IFACE de drc.conf).
"""
import glob
import json
import math
import os
import socket
import shutil
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

CTL = "/tmp/drc_ctl"
CTL_OUT = "/tmp/drc_ctl.out"
STATUS = "/tmp/drc_status"
PAD_NAME = "Wii U GamePad"
TOUCH_NAME = "Wii U GamePad Touchscreen"
PEN_NAME = "Wii U GamePad Pen"                # DRC_TOUCH_MODE=pen: tableta con presión

EV_KEY, EV_ABS = 1, 3
EVENT = struct.Struct("llHHi")   # struct input_event en 64 bits
# Códigos de drc_stream (uinput_map.h): A/B/X/Y siguen la posición física Nintendo
BUTTONS = {
    "A": 0x131, "B": 0x130, "X": 0x133, "Y": 0x134, "L": 0x136, "R": 0x137, "ZL": 0x138, "ZR": 0x139,
    "-": 0x13A, "+": 0x13B, "HOME": 0x13C, "L3": 0x13D, "R3": 0x13E, "TV": 0x176,   # TV = KEY_KEYBOARD
}
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y = 0, 1, 2, 3, 4, 5, 16, 17
ABS_PRESSURE = 0x18
BTN_DPAD = {0x220: "UP", 0x221: "DOWN", 0x222: "LEFT", 0x223: "RIGHT"}
TOUCH_W, TOUCH_H = 854, 480


# ------------------------------------------------------------------ estado del mando
@dataclass
class PadState:
    buttons: Dict[str, bool] = field(default_factory=dict)       # estado actual
    pressed_ever: set = field(default_factory=set)               # botones/direcciones pulsados alguna vez
    sticks: Dict[str, float] = field(default_factory=lambda: {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0})
    stick_dirs: set = field(default_factory=set)                 # "L+x", "L-x", "L+y", "L-y", "R..." alcanzadas
    touch: Optional[tuple] = None                                # (x, y) mientras toca
    touch_trail: list = field(default_factory=list)
    touch_zones: set = field(default_factory=set)
    acc: tuple = (0, 0, 0)
    gyro: tuple = (0, 0, 0)                                      # °/s (roll, yaw, pitch)
    acc_range: float = 0.0
    gyro_peak: float = 0.0
    mic_level: float = 0.0
    mic_peak: float = 0.0
    cam_packets: int = 0
    cam_frames: int = 0
    cam_last_bytes: int = 0
    sniffer_error: str = ""
    evdev_ok: bool = False
    touch_ok: bool = False
    packet_buttons: bool = False                                 # el sniffer entrega L3/R3/TV/POWER desde el paquete de entrada
    touch_events: int = 0                                        # eventos recibidos del dispositivo táctil (0 = nadie los lee)
    pen_pressure: Optional[int] = None                           # 0..1023 del dispositivo lápiz (None = no hay lápiz)
    raw_pressure: Optional[int] = None                           # 0..1023 calculada del paquete del mando (root)
    touch_raw: Optional[dict] = None                             # último táctil en crudo del paquete (x, y, pressure)


STICK_DEAD = 0.25


def zone_of(x: float, y: float) -> str:
    """Zona 3x3 de la pantalla táctil (0..2 por eje): 'c' centro, esquinas y bordes con letras N/S/E/O."""
    col = min(2, max(0, int(x * 3 / TOUCH_W)))
    row = min(2, max(0, int(y * 3 / TOUCH_H)))
    return f"{row}{col}"


def apply_event(st: PadState, dev: str, etype: int, code: int, value: int, touch_abs: Dict[str, int]) -> None:
    """Actualiza `st` con un evento evdev de `dev` ('pad', 'touch' o 'pen'). `touch_abs` guarda la X/Y de la
    pulsación. El lápiz es como el táctil más la presión (0..1023, la que recibe Krita)."""
    if dev == "pad":
        if etype == EV_KEY:
            name = next((n for n, c in BUTTONS.items() if c == code), None) or BTN_DPAD.get(code)
            if name:
                st.buttons[name] = bool(value)
                if value:
                    st.pressed_ever.add(name)
        elif etype == EV_ABS:
            if code in (ABS_X, ABS_Y, ABS_RX, ABS_RY):
                key = {ABS_X: "lx", ABS_Y: "ly", ABS_RX: "rx", ABS_RY: "ry"}[code]
                v = max(-1.0, min(1.0, value / 32767.0))
                st.sticks[key] = v
                if abs(v) >= 0.8:
                    st.stick_dirs.add(f"{key[0].upper()}{'+' if v > 0 else '-'}{key[1]}")
            elif code in (ABS_Z, ABS_RZ):
                name = "ZL" if code == ABS_Z else "ZR"
                st.buttons[name] = value > 127
                if value > 127:
                    st.pressed_ever.add(name)
            elif code in (ABS_HAT0X, ABS_HAT0Y):
                names = ("LEFT", "RIGHT") if code == ABS_HAT0X else ("UP", "DOWN")
                st.buttons[names[0]] = value < 0
                st.buttons[names[1]] = value > 0
                if value:
                    st.pressed_ever.add(names[0] if value < 0 else names[1])
    elif dev in ("touch", "pen"):
        st.touch_events += 1
        if dev == "pen":
            if etype == EV_ABS and code == ABS_PRESSURE:
                st.pen_pressure = value
            elif etype == EV_KEY and code == 0x14A and not value:
                st.pen_pressure = 0
            elif st.pen_pressure is None:
                st.pen_pressure = 0
        if etype == EV_ABS and code in (ABS_X, ABS_Y):
            touch_abs["x" if code == ABS_X else "y"] = value
            if st.touch is not None:
                st.touch = (touch_abs.get("x", 0), touch_abs.get("y", 0))
        elif etype == EV_KEY and code == 0x14A:           # BTN_TOUCH
            if value:
                st.touch = (touch_abs.get("x", 0), touch_abs.get("y", 0))
            else:
                st.touch = None
        if st.touch is not None:
            st.touch_trail.append(st.touch)
            del st.touch_trail[:-400]
            st.touch_zones.add(zone_of(*st.touch))


def apply_pointer_touch(st: PadState, x: int, y: int, down: bool) -> None:
    """Táctil visto como puntero (x, y en la escala 854x480 del mando). En la sesión del propio GamePad, touch2pointer
    toma el dispositivo táctil en exclusiva y convierte los toques en ratón: ahí el probador solo ve el puntero."""
    if not down:
        st.touch = None
        return
    st.touch = (x, y)
    st.touch_trail.append(st.touch)
    del st.touch_trail[:-400]
    st.touch_zones.add(zone_of(x, y))


# ------------------------------------------------------------------ presión (como drc_stream/pen_pressure.h)
PEN_DEFAULTS = {"kx": 0.7538, "ky": 0.0071, "kxx": -8.68e-5, "kyy": 8.0e-6, "kxy": 2.51e-5,
                "min": -350.0, "max": 20.0, "curve": 2.0}


def pen_config(conf) -> dict:
    """DRC_PEN_PRESSURE_* de `conf` (drc.conf + entorno) sobre los valores por defecto; lo inválido se ignora."""
    c = dict(PEN_DEFAULTS)
    for k in c:
        v = conf.get(f"DRC_PEN_PRESSURE_{k.upper()}")
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            c[k] = f
    if c["max"] <= c["min"]:
        c["min"], c["max"] = PEN_DEFAULTS["min"], PEN_DEFAULTS["max"]
    if not 0.05 < c["curve"] < 20:
        c["curve"] = PEN_DEFAULTS["curve"]
    return c


def pen_pressure_from_raw(raw: int, x: int, y: int, cfg: dict) -> int:
    """Presión 0..1023 desde el táctil en crudo, sin suavizado: 0 = sin tocar, 1..1023 tocando."""
    if raw <= 0:
        return 0
    level = raw - (cfg["kx"] * x + cfg["ky"] * y + cfg["kxx"] * x * x + cfg["kyy"] * y * y + cfg["kxy"] * x * y)
    t = min(1.0, max(0.0, (level - cfg["min"]) / (cfg["max"] - cfg["min"]))) ** cfg["curve"]
    return 1 + round(t * 1022)


def apply_raw_touch(st: PadState, raw: dict, cfg: dict) -> None:
    st.touch_raw = raw
    st.raw_pressure = pen_pressure_from_raw(raw["pressure"], raw["x"], raw["y"], cfg)


def pressure_shown(st: PadState) -> Optional[tuple]:
    """(presión 0..1023, origen): la del lápiz si existe (lo que recibe el escritorio); si no, la calculada del
    paquete del mando; None si no hay ninguna (sin lápiz y sin root)."""
    if st.pen_pressure is not None:
        return st.pen_pressure, "lápiz"
    if st.raw_pressure is not None:
        return st.raw_pressure, "calculada"
    return None


def pressure_radius(p: Optional[int], base: int = 16, extra: int = 64) -> int:
    """Radio del círculo alrededor del toque: `base` sin presión, `base + extra` a 1023."""
    return base + round(extra * max(0, min(1023, p or 0)) / 1023)


def find_input(name: str, root: str = "/sys/class/input") -> Optional[str]:
    for p in glob.glob(os.path.join(root, "event*", "device", "name")):
        try:
            if open(p).read().strip() == name:
                return "/dev/input/" + p.split(os.sep)[-3]
        except OSError:
            pass
    return None


class EvdevReader(threading.Thread):
    """Lee los dispositivos del mando (sin tomarlos en exclusiva) y alimenta un PadState."""

    def __init__(self, st: PadState):
        super().__init__(daemon=True)
        self.st, self.running, self.touch_abs = st, True, {}
        self.fds: Dict[int, str] = {}

    def _open(self):
        for dev, name in (("pad", PAD_NAME), ("touch", TOUCH_NAME), ("pen", PEN_NAME)):
            if dev in self.fds.values() or (dev != "pad" and {"touch", "pen"} & set(self.fds.values())):
                continue
            path = find_input(name)
            if path:
                try:
                    self.fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = dev
                    if dev == "pad":
                        self.st.evdev_ok = True
                    else:
                        self.st.touch_ok = True
                except OSError:
                    pass

    def run(self):
        import select
        last_scan = 0.0
        while self.running:
            if time.monotonic() - last_scan > 1.0 and len(self.fds) < 2:   # el mando y el táctil o el lápiz
                self._open()
                last_scan = time.monotonic()
            if not self.fds:
                time.sleep(0.2)
                continue
            r, _, _ = select.select(list(self.fds), [], [], 0.2)
            for fd in r:
                try:
                    data = os.read(fd, EVENT.size * 32)
                except OSError:
                    self.fds.pop(fd, None)
                    continue
                for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, et, code, val = EVENT.unpack_from(data, off)
                    apply_event(self.st, self.fds[fd], et, code, val, self.touch_abs)


# ------------------------------------------------------------------ paquetes del mando
GYRO_SCALE = 250.0     # cuentas por °/s


def parse_touch_raw(msg: bytes) -> dict:
    """Táctil en crudo, como libdrc (input-receiver.cpp): media de los 10 puntos de 12 bits desde el byte 36 y la
    presión de 12 bits en los bits 4-6 de los bytes 37, 39, 41 y 43 (0 = sin tocar). Sin calibrar."""
    xs = [((msg[36 + 4 * i + 1] & 0xF) << 8) | msg[36 + 4 * i] for i in range(10)]
    ys = [((msg[36 + 4 * i + 3] & 0xF) << 8) | msg[36 + 4 * i + 2] for i in range(10)]
    pressure = 0
    for k, byte in enumerate((37, 39, 41, 43)):
        pressure |= ((msg[byte] >> 4) & 7) << (3 * k)
    return {"x": sum(xs) // 10, "y": sum(ys) // 10, "pressure": pressure}


def parse_hid(msg: bytes) -> Optional[dict]:
    """UDP 50122->50022: acelerómetro (16..22, 3×s16 BE) y giroscopio (22..31, 3×s24 BE). None si es corto."""
    if len(msg) < 31:
        return None
    pitch, roll, z = struct.unpack(">hhh", msg[16:22])
    g = [int.from_bytes(msg[o:o + 3], "big", signed=True) for o in (22, 25, 28)]
    out = {"acc": (roll, pitch, z), "gyro_raw": tuple(g), "gyro": tuple(v / GYRO_SCALE for v in g)}
    if len(msg) >= 76:
        out["touch_raw"] = parse_touch_raw(msg)
    if len(msg) > 80:
        # Botones que el dispositivo uinput no entrega (o que no se ven por evdev): del propio paquete de entrada.
        # buttons = msg[80]<<16 | msg[2]<<8 | msg[3] (libdrc input-receiver.cpp); msg[4] = estado de energía (bit 1: POWER)
        b = (msg[80] << 16) | (msg[2] << 8) | msg[3]
        out["extra_buttons"] = {"L3": bool(b & 0x800000), "R3": bool(b & 0x400000), "TV": bool(b & 0x200000),
                                "POWER": bool(msg[4] & 0x02)}
    return out


def mic_level(msg: bytes) -> Optional[float]:
    """UDP 50121->50021: cabecera 0xDx de 8 B y PCM 16 kHz mono s16. Devuelve 0..1 (ruido de fondo ≈ 120 RMS)."""
    if len(msg) < 10 or (msg[0] & 0xF0) != 0xD0:
        return None
    pcm = msg[8:]
    n = len(pcm) // 2
    if n == 0:
        return None
    s = struct.unpack(f"<{n}h", pcm[:n * 2])
    rms = math.sqrt(sum(v * v for v in s) / n)
    return max(0.0, min(1.0, (rms - 120.0) / 2500.0))


class Sniffer(threading.Thread):
    """Escucha los UDP del mando en `iface` (root). Sin permisos deja el motivo en st.sniffer_error."""

    def __init__(self, st: PadState, iface: str, on_cam_chunk: Optional[Callable[[bytes], None]] = None,
                 on_mic_pcm: Optional[Callable[[bytes], None]] = None, pen_cfg: Optional[dict] = None):
        super().__init__(daemon=True)
        self.st, self.iface, self.running = st, iface, True
        self.pen_cfg = pen_cfg or dict(PEN_DEFAULTS)
        self.on_cam_chunk, self.on_mic_pcm = on_cam_chunk, on_mic_pcm
        self.baseline_acc: Optional[tuple] = None

    def run(self):
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0800))
            s.bind((self.iface, 0))
            s.settimeout(0.2)
        except Exception as e:
            self.st.sniffer_error = f"{type(e).__name__}: {e} (hace falta root y la interfaz {self.iface!r}; sin sudo: dar cap_net_raw a una copia de python, ver README)"
            return
        while self.running:
            try:
                pkt = s.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(pkt) < 42 or pkt[23] != 17:
                continue
            sport, dport = struct.unpack("!HH", pkt[34:38])
            msg = pkt[42:]
            if (sport, dport) == (50122, 50022):
                h = parse_hid(msg)
                if h:
                    st = self.st
                    st.acc, st.gyro = h["acc"], h["gyro"]
                    if self.baseline_acc is None:
                        self.baseline_acc = h["acc"]
                    st.acc_range = max(st.acc_range, max(abs(a - b) for a, b in zip(h["acc"], self.baseline_acc)))
                    st.gyro_peak = max(st.gyro_peak, max(abs(v) for v in h["gyro"]))
                    for name, on in h.get("extra_buttons", {}).items():
                        st.buttons[name] = on
                        if on:
                            st.pressed_ever.add(name)
                    st.packet_buttons = True
                    if "touch_raw" in h:
                        apply_raw_touch(st, h["touch_raw"], self.pen_cfg)
            elif (sport, dport) == (50121, 50021):
                lv = mic_level(msg)
                if lv is not None:
                    self.st.mic_level = max(lv, self.st.mic_level * 0.85)
                    self.st.mic_peak = max(self.st.mic_peak, lv)
                    if self.on_mic_pcm:
                        self.on_mic_pcm(msg[8:])
            elif (sport, dport) == (50120, 50020):
                self.st.cam_packets += 1
                if self.on_cam_chunk:
                    self.on_cam_chunk(msg)
        s.close()


# ------------------------------------------------------------------ órdenes y estado
def parse_status(text: str) -> dict:
    """/tmp/drc_status: «clave=valor» por línea o separados por espacios."""
    out = {}
    for tok in text.replace("\n", " ").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def read_status(path: str = STATUS) -> dict:
    try:
        return parse_status(open(path).read())
    except OSError:
        return {}


PC_SINK = "drc-gamepad"      # sumidero virtual de PipeWire que crea start_drc.sh en un PC (host_pc.sh)


def como_usuario_de_audio(cmd: list, env: Optional[dict] = None, geteuid=os.geteuid) -> list:
    """Como root (sudo), PipeWire no contesta: el servidor es del usuario. Se lanza `cmd` como ese usuario con su
    /run/user/<uid> (no el XDG_RUNTIME_DIR privado de la sesión del GamePad, que es /tmp/drc_runtime_<uid>)."""
    env = os.environ if env is None else env
    uid, user = env.get("SUDO_UID"), env.get("SUDO_USER")
    if geteuid() != 0 or not uid or not user:
        return cmd
    run = f"/run/user/{uid}"
    return ["runuser", "-u", user, "--", "env", f"XDG_RUNTIME_DIR={run}", f"PULSE_SERVER=unix:{run}/pulse/native"] + cmd


def gamepad_sink(run=None) -> Optional[str]:
    """Nombre del sumidero de PipeWire/Pulse que va al mando (solo existe en un PC con PipeWire)."""
    try:
        out = (run or (lambda: subprocess.run(como_usuario_de_audio(["pactl", "list", "short", "sinks"]),
                                               capture_output=True, text=True, timeout=5).stdout))()
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) > 1 and PC_SINK in cols[1]:
            return cols[1]
    return None


def play_command(path: str, which=shutil.which, sink: Optional[str] = None) -> Optional[list]:
    """Orden para reproducir un WAV hacia el mando: aplay (la Pi: su dispositivo por defecto es el mando) o, en un PC
    con PipeWire, paplay/pw-play dirigido al sumidero del mando. None si no hay ningún reproductor."""
    if which("aplay"):
        return ["aplay", "-q", path]
    sink = sink if sink is not None else gamepad_sink()
    if which("paplay"):
        return como_usuario_de_audio(["paplay"] + ([f"--device={sink}"] if sink else []) + [path])
    if which("pw-play"):
        return como_usuario_de_audio(["pw-play"] + ([f"--target={sink}"] if sink else []) + [path])
    return None


def ctl_send(cmd: str, ctl: str = CTL) -> bool:
    """Escribe una orden en la FIFO de drc_stream. False si no existe (no se transmite)."""
    try:
        fd = os.open(ctl, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (cmd.strip() + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def ctl_query(cmd: str, expect: str, timeout: float = 3.0, ctl: str = CTL, out: str = CTL_OUT) -> Optional[str]:
    """Envía `cmd` y devuelve la línea de respuesta que empieza por `expect` (None si no llega)."""
    def lines():
        try:
            return open(out).read().splitlines()
        except OSError:
            return []
    before = len(lines())
    if not ctl_send(cmd, ctl):
        return None
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(0.05)
        for ln in lines()[before:]:
            if ln.startswith(expect):
                return ln
    return None


def reply_bytes(line: Optional[str]) -> Optional[bytes]:
    """«battery ok 00 05 ...» -> bytes; None si falló o no hubo respuesta."""
    if not line:
        return None
    t = line.split()
    if len(t) < 2 or t[1] != "ok":
        return None
    try:
        return bytes(int(x, 16) for x in t[2:])
    except ValueError:
        return None


# ------------------------------------------------------------------ lista de comprobaciones
PENDING, PASS, FAIL, SKIP = "pendiente", "ok", "fallo", "omitida"


@dataclass
class Check:
    key: str
    title: str
    group: str
    manual: bool = False          # la confirma la persona (¿se oyó? ¿vibró?)
    state: str = PENDING
    detail: str = ""


def new_checks() -> List[Check]:
    c = Check
    return [
        c("link", "Estado de la transmisión (drc_status)", "Enlace"),
        c("fw", "Versión de firmware del mando", "Enlace"),
        c("rssi", "Señal Wi-Fi (RSSI)", "Enlace"),
        c("buttons", "Todos los botones y la cruceta", "Entradas"),
        c("sticks", "Sticks: 4 direcciones cada uno", "Entradas"),
        c("touch", "Pantalla táctil: las 9 zonas", "Entradas"),
        c("accel", "Acelerómetro responde al movimiento", "Sensores"),
        c("gyro", "Giroscopio responde al giro", "Sensores"),
        c("mic", "Micrófono recibe audio", "Audio"),
        c("speaker", "Altavoces (se oye el tono)", "Audio", manual=True),
        c("volume", "Volumen: deslizador y ganancia", "Audio"),
        c("camera", "Cámara entrega fotogramas", "Cámara"),
        c("brightness", "Brillo: lectura y cambio", "Pantalla"),
        c("backlight", "Retroiluminación se ve cambiar", "Pantalla", manual=True),
        c("vibration", "Vibración (se siente)", "Háptica", manual=True),
        c("sensorbar", "Barra sensora IR (orden aceptada)", "Otros"),
        c("battery", "Batería y estado de carga", "Otros"),
        c("nfc", "NFC detecta una etiqueta", "Otros"),
    ]


class Report:
    def __init__(self, checks: Optional[List[Check]] = None):
        self.checks = checks or new_checks()
        self.by = {c.key: c for c in self.checks}

    def set(self, key: str, state: str, detail: str = "") -> None:
        c = self.by[key]
        c.state, c.detail = state, detail

    def summary(self) -> dict:
        n = {s: 0 for s in (PENDING, PASS, FAIL, SKIP)}
        for c in self.checks:
            n[c.state] += 1
        return n

    def to_json(self) -> str:
        return json.dumps({"fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "resumen": self.summary(),
                           "comprobaciones": [vars(c) for c in self.checks]}, ensure_ascii=False, indent=2)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())


def update_input_checks(rep: Report, st: PadState) -> None:
    """Marca las comprobaciones que se cumplen solas al usar el mando."""
    need = set(BUTTONS) | set(BTN_DPAD.values()) | ({"POWER"} if st.packet_buttons else set())
    got = need & st.pressed_ever
    if got == need:
        rep.set("buttons", PASS, "todos pulsados")
    else:
        rep.set("buttons", PENDING, "faltan: " + " ".join(sorted(need - got)))
    dirs = {f"{s}{sg}{ax}" for s in "LR" for sg in "+-" for ax in "xy"}
    if dirs <= st.stick_dirs:
        rep.set("sticks", PASS, "8 direcciones")
    else:
        rep.set("sticks", PENDING, f"{len(dirs & st.stick_dirs)}/8 direcciones")
    zones = {f"{r}{c}" for r in range(3) for c in range(3)}
    if zones <= st.touch_zones:
        rep.set("touch", PASS, "9 zonas")
    else:
        rep.set("touch", PENDING, f"{len(zones & st.touch_zones)}/9 zonas")
    if st.acc_range > 1500:
        rep.set("accel", PASS, f"variación {int(st.acc_range)}")
    if st.gyro_peak > 20:
        rep.set("gyro", PASS, f"pico {st.gyro_peak:.0f} °/s")
    if st.mic_peak > 0.05:
        rep.set("mic", PASS, f"pico {st.mic_peak:.2f}")
    if st.cam_frames > 0:
        rep.set("camera", PASS, f"{st.cam_frames} fotogramas")


def auto_checks(rep: Report, ctl: Callable = ctl_query, status: Callable = read_status) -> None:
    """Comprobaciones que no necesitan a la persona: órdenes al mando y /tmp/drc_status."""
    s = status()
    if s.get("valid", 0):
        rep.set("link", PASS, f"batería={s.get('battery')} volumen={s.get('volume')}")
        rep.set("battery", PASS, f"batería={s.get('battery')}/6 carga={s.get('charging', s.get('power', '?'))}")
        rep.set("volume", PASS, f"deslizador={s.get('slider')} ganancia={s.get('volume')}")
    else:
        rep.set("link", FAIL, "sin /tmp/drc_status válido: ¿está transmitiendo drc_stream?")
    r = reply_bytes(ctl("get fw_version", "fw_version"))
    rep.set("fw", PASS if r else FAIL, r.hex() if r else "sin respuesta")
    r = reply_bytes(ctl("get rssi", "rssi"))
    rep.set("rssi", PASS if r else FAIL, r.hex() if r else "sin respuesta")
    r = reply_bytes(ctl("get brightness", "brightness"))
    if r:
        rep.set("brightness", PASS, f"actual={r.hex()}")
    else:
        rep.set("brightness", FAIL, "sin respuesta")
    r = ctl("set sensorbar 1", "sensorbar")
    ctl("set sensorbar 0", "sensorbar")
    rep.set("sensorbar", PASS if r and " ok" in r else FAIL, r or "sin respuesta")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Comprobaciones automáticas del GamePad (sin interfaz)")
    ap.add_argument("--iface", help="interfaz Wi-Fi del GamePad (por defecto WIFI_IFACE de drc.conf)")
    ap.add_argument("--seconds", type=float, default=5.0, help="tiempo de muestreo de sensores/micro/cámara")
    ap.add_argument("--out", help="guardar el informe JSON en este fichero")
    a = ap.parse_args(argv)
    iface = a.iface
    if not iface:
        try:
            from drchost.drc_ap import load_drc_conf
            iface = load_drc_conf().get("WIFI_IFACE", "wlan0")
        except Exception:
            iface = "wlan0"
    rep, st = Report(), PadState()
    sn = Sniffer(st, iface)
    sn.start()
    ctl_send("set mic 1")
    time.sleep(a.seconds)
    auto_checks(rep)
    update_input_checks(rep, st)
    sn.running = False
    if st.sniffer_error:
        for key in ("accel", "gyro", "mic", "camera"):
            rep.set(key, SKIP, st.sniffer_error)
    print(rep.to_json())
    if a.out:
        rep.save(a.out)
    return 1 if rep.summary()[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
