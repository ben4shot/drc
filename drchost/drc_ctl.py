#!/usr/bin/env python3
"""Control del GamePad desde el PC: emparejar, empezar/parar la transmisión, ver el estado y elegir la interfaz.

Una sola lógica (este módulo, probada en test_drc_ctl.py) con dos caras: el menú/CLI de este mismo fichero y la
aplicación gráfica `drc_gui.py`. Para cada acción se calcula una lista de requisitos *dinámica* (lo que tiene el
equipo y lo que falta, con cómo arreglarlo), y solo se lanza lo que ya se puede lanzar.

    drc_ctl.py                    menú interactivo
    drc_ctl.py status             estado actual
    drc_ctl.py ifaces             interfaces Wi-Fi y si sirven
    drc_ctl.py check pair|start   requisitos de la acción (código de salida 1 si falta alguno bloqueante)
    drc_ctl.py pair [--symbols 0,0,0,0] [--iface wlan1]
    drc_ctl.py start [--mode separate|extended|mirror] [--iface wlan1]
    drc_ctl.py stop
    drc_ctl.py iface <nombre>     guarda la interfaz elegida
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import drc_ap  # noqa: E402  (load_drc_conf, tsf_sysfs_path, PERSIST_DIR, SYMBOLS ...)

SYMBOLS = ("♠", "♥", "♦", "♣")          # 0 = pica, 1 = corazón, 2 = diamante, 3 = trébol
CONF_DIR = Path("/tmp/drc_conf")
STATE_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "drc" / "ctl.json"
GAMEPAD_IP = "192.168.1.11"
CONSOLE_IP = "192.168.1.10"

OK, WARN, FAIL = "ok", "warn", "fail"


# ---------------------------------------------------------------- acceso al sistema (sustituible en pruebas)
class Host:
    """Todo lo que toca el sistema: comandos, binarios, sysfs. Las pruebas pasan una subclase falsa."""

    def __init__(self, sys_net: Path = Path("/sys/class/net")):
        self.sys_net = sys_net

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def run(self, cmd: Sequence[str], timeout: float = 5.0) -> Tuple[int, str]:
        try:
            res = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, check=False)
            return res.returncode, (res.stdout or "") + (res.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""

    def is_root(self) -> bool:
        return os.geteuid() == 0

    def regdb_present(self) -> bool:
        """¿Está regulatory.db (paquete wireless-regdb)? Sin él `iw reg set` no cambia nada y el dominio queda en 00."""
        return any(Path(d, "regulatory.db").exists() for d in ("/lib/firmware", "/usr/lib/firmware"))

    def user(self) -> str:
        return os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"

    def env(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)


@dataclass
class Check:
    key: str
    name: str
    status: str                 # ok | warn | fail
    detail: str = ""
    fix: str = ""               # qué hacer si no está ok

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


def blocking(checks: Sequence[Check]) -> List[Check]:
    return [c for c in checks if c.blocking]


# ---------------------------------------------------------------- símbolos del emparejamiento
def parse_symbols(text: str) -> Tuple[int, int, int, int]:
    """«0,1,2,3», «0123» o «♠♥♦♣» -> (0, 1, 2, 3). ValueError si no son 4 símbolos válidos."""
    s = text.strip()
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
    else:
        parts = list(s.replace(" ", ""))
    out = []
    for p in parts:
        if p in SYMBOLS:
            out.append(SYMBOLS.index(p))
        elif p.isdigit() and 0 <= int(p) <= 3 and len(p) == 1:
            out.append(int(p))
        else:
            raise ValueError(f"símbolo no válido: {p!r} (valen 0-3 o ♠♥♦♣)")
    if len(out) != 4:
        raise ValueError("hacen falta exactamente 4 símbolos")
    return (out[0], out[1], out[2], out[3])


def show_symbols(digits: Sequence[int]) -> str:
    return " ".join(SYMBOLS[d] for d in digits)


# ---------------------------------------------------------------- estado guardado y configuración
def load_state(path: Optional[Path] = None) -> dict:
    try:
        return json.loads((path or STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}


def save_state(update: dict, path: Optional[Path] = None) -> None:
    path = path or STATE_FILE
    data = load_state(path)
    data.update(update)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def current_iface(cfg: Optional[dict] = None, state: Optional[dict] = None) -> str:
    """La interfaz del GamePad: WIFI_IFACE de drc.conf, la misma para todo (transmisión, cámara, drc_wake...).
    `state` ya no se usa: una interfaz vieja guardada en ctl.json no manda."""
    cfg = drc_ap.load_drc_conf() if cfg is None else cfg
    return cfg.get("WIFI_IFACE") or "wlan0"


def current_symbols(state: Optional[dict] = None) -> Tuple[int, int, int, int]:
    state = load_state() if state is None else state
    try:
        return parse_symbols(state.get("symbols", "0,0,0,0"))
    except ValueError:
        return (0, 0, 0, 0)


# ---------------------------------------------------------------- configuración (drc.conf)
@dataclass
class Field:
    key: str
    label: str
    group: str
    kind: str = "text"               # choice | int | text | bool
    choices: Tuple[str, ...] = ()
    lo: int = 0
    hi: int = 0
    help: str = ""
    default: str = ""


CONFIG_FIELDS: List[Field] = [
    Field("DRC_MODE", "Modo de pantalla", "Pantalla", "choice", ("separate", "extended", "mirror"),
          help="separate = sesión propia del GamePad (sin tocar tu escritorio); extended = un monitor más de tu "
               "escritorio (verás tu barra y el foco se comparte); mirror = duplica un monitor", default="separate"),
    Field("DESKTOP_SESSION", "Sesión del GamePad (separate)", "Pantalla", "choice",
          ("cage", "auto", "wiiu-wm", "hyprland", "sway", "labwc"),
          help="cage = mínima, solo la aplicación del mando; auto = el escritorio del equipo", default="auto"),
    Field("GAMEPAD_AUTOSTART", "Aplicación del GamePad", "Pantalla", "text",
          help="Lo que se abre al empezar (p. ej. steam -gamepadui). Vacío = solo el escritorio", default="steam -gamepadui"),
    Field("GAMEPAD_STEAM_HOME", "HOME del Steam del mando", "Pantalla", "text",
          help="Carpeta propia para un segundo Steam (inicio de sesión aparte). Vacío = usar el del usuario"),
    Field("DISPLAY_FPS", "Fotogramas por segundo", "Pantalla", "int", lo=15, hi=60, default="60"),
    Field("WIFI_CHANNEL", "Canal Wi-Fi (5 GHz)", "Red", "int", lo=36, hi=165, help="El 40 va bien con dominio US", default="40"),
    Field("WIFI_REGDOM", "Dominio regulatorio", "Red", "text", help="Código de país (US permite el canal 36–48 sin DFS)", default="US"),
    Field("WIFI_MGMT_TOOL", "Gestor Wi-Fi", "Red", "choice", ("auto", "nmcli", "iw", "none", "custom"), default="auto"),
    Field("FIREWALL_OPEN", "Cortafuegos", "Red", "choice", ("auto", "1", "0"),
          help="auto = abre el tráfico del GamePad solo si el equipo filtra; 1 = siempre; 0 = nunca", default="auto"),
    Field("VIDEO_BITRATE_MODE", "Bitrate: máximo (calidad variable) o fijo", "Imagen", "choice", ("max", "fija"),
          default="max"),
    Field("VIDEO_BITRATE_KBPS", "Bitrate de vídeo (kbps): el tope o el fijo", "Imagen", "int", lo=1000, hi=30000,
          default="9300"),
    Field("DRC_CRF", "Calidad (CRF, menor = mejor; solo con bitrate máximo)", "Imagen", "int", lo=0, hi=51, default="14"),
    Field("DRC_QP_MIN", "QP mínimo", "Imagen", "int", lo=0, hi=51, default="12"),
    Field("DRC_FRAME_MAX", "Tamaño máximo de fotograma (bytes)", "Imagen", "int", lo=2000, hi=60000, default="26000"),
    Field("DRC_SUBME", "Calidad de movimiento (subme)", "Imagen", "int", lo=0, hi=11, default="2"),
    Field("DRC_TOUCH_MODE", "Táctil: ratón, toques nativos o lápiz con presión", "Táctil", "choice",
          ("pointer", "touch", "pen"), default="pointer",
          help="pointer: como ratón (cursor y clic en cualquier app); touch: toques nativos (gestos en apps "
               "Wayland); pen: tableta para Krita, GIMP... (no con cage ni wiiu-wm)"),
    Field("DRC_PEN_PRESSURE_MIN", "Lápiz: presión del toque más suave", "Táctil", "int", lo=-2000, hi=4000,
          default="-350"),
    Field("DRC_PEN_PRESSURE_MAX", "Lápiz: presión del toque más fuerte", "Táctil", "int", lo=-2000, hi=4000,
          default="20"),
    Field("DRC_PEN_PRESSURE_CURVE", "Lápiz: curva (>1 hay que apretar más, <1 más sensible)", "Táctil", "text",
          default="2.0"),
    Field("DRC_PEN_PRESSURE_SMOOTH", "Lápiz: suavizado de la presión (0 … 0.9)", "Táctil", "text", default="0.3"),
    Field("AUDIO_OUTPUT", "Salida de audio", "Audio", "choice", ("gamepad", "aux"), default="gamepad"),
    Field("AUDIO_BACKEND", "Sistema de audio", "Audio", "choice", ("auto", "pipewire", "alsa"), default="auto"),
    Field("GAMEPAD_BINDS", "Atajos con los botones", "Periféricos", "text",
          help="Combinaciones separadas por «;», p. ej. HOME+X=close", default="HOME+X=close"),
]
FIELDS_BY_KEY = {f.key: f for f in CONFIG_FIELDS}


DEFAULT_CONF = REPO / "config" / "drc.conf"
EXAMPLE_CONF = REPO / "config" / "drc.conf.example"


def conf_path() -> Optional[Path]:
    """El drc.conf que lee el proyecto (el mismo orden de búsqueda que drc_ap.load_drc_conf)."""
    cands = []
    if os.environ.get("DRC_CONF_FILE"):
        cands.append(Path(os.environ["DRC_CONF_FILE"]))
    cands += [Path("/etc/drc/drc.conf"), DEFAULT_CONF]
    return next((p for p in cands if p.is_file()), None)


def set_iface(name: str) -> Path:
    """Guarda la interfaz elegida en WIFI_IFACE de drc.conf (si no hay drc.conf, lo crea desde el ejemplo)."""
    path = conf_path()
    if path is None:
        path = DEFAULT_CONF
        path.write_text(EXAMPLE_CONF.read_text() if EXAMPLE_CONF.is_file() else "")
    return set_conf_value("WIFI_IFACE", name, path)


def platform_check(cfg: dict) -> "Check":
    """DRC_PLATFORM tiene que nombrar un perfil que exista: si no, se ignora en silencio y quedan los valores
    genéricos (TS_OFFSET_US de 22000 en vez de los del adaptador...)."""
    plat = (os.environ.get("DRC_PLATFORM") or cfg.get("DRC_PLATFORM", "")).strip()
    auto = plat == "auto"
    if auto:   # el perfil que corresponde al driver de la interfaz elegida
        plat = drc_ap.resolve_platform(plat, os.environ.get("DRC_IFACE") or cfg.get("WIFI_IFACE", "wlan0"))
    if not plat:
        return Check("platform", "Perfil de plataforma", OK, "ninguno (valores de drc.conf o los de por defecto)")
    dirs = [REPO / "config" / "platforms"]
    path = conf_path()
    if path is not None:
        dirs.insert(0, path.parent / "platforms")
    found = next((d / f"{plat}.conf" for d in dirs if (d / f"{plat}.conf").is_file()), None)
    if found is None:
        have = sorted({p.stem for d in dirs if d.is_dir() for p in d.glob("*.conf")})
        return Check("platform", "Perfil de plataforma", FAIL, f"«{plat}» no existe (hay: {', '.join(have) or 'ninguno'})",
                     "Corrige DRC_PLATFORM en drc.conf.")
    return Check("platform", "Perfil de plataforma", OK, f"{plat}{' (automático, por el driver)' if auto else ''} ({found})")


def field_value(cfg: dict, f: Field) -> str:
    return cfg.get(f.key, f.default)


def validate_field(f: Field, raw: str) -> str:
    """Devuelve el valor normalizado o lanza ValueError con el motivo."""
    v = raw.strip()
    if "\n" in v or "\r" in v:
        raise ValueError("no puede tener saltos de línea")
    if f.kind == "choice":
        if v not in f.choices:
            raise ValueError("debe ser uno de: " + ", ".join(f.choices))
    elif f.kind == "int":
        if not re.fullmatch(r"-?\d+", v) or not (f.lo <= int(v) <= f.hi):
            raise ValueError(f"debe ser un número entre {f.lo} y {f.hi}")
        v = str(int(v))
    elif f.kind == "bool":
        if v.lower() in ("1", "si", "sí", "s", "true", "on", "yes", "y"):
            v = "1"
        elif v.lower() in ("0", "no", "n", "false", "off"):
            v = "0"
        else:
            raise ValueError("debe ser 1 (sí) o 0 (no)")
    return v


def _shell_quote(v: str) -> str:
    return '"' + re.sub(r'(["\\$`])', r"\\\1", v) + '"'


def set_conf_value(key: str, value: str, path: Optional[Path] = None) -> Path:
    """Cambia (o añade) KEY="valor" en drc.conf conservando el resto del fichero y el comentario de la línea."""
    path = path or conf_path()
    if path is None:
        raise FileNotFoundError("no hay drc.conf (copia config/drc.conf.example a config/drc.conf)")
    lines = path.read_text().splitlines()
    pat = re.compile(rf"^(\s*{re.escape(key)}=)(\"[^\"]*\"|'[^']*'|[^\s#]*)(\s*#.*)?$")
    done = False
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            lines[i] = f"{m.group(1)}{_shell_quote(value)}{m.group(3) or ''}"
            done = True
            break
    if not done:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={_shell_quote(value)}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def effective_mode(cfg: Optional[dict] = None) -> str:
    cfg = drc_ap.load_drc_conf() if cfg is None else cfg
    return cfg.get("DRC_MODE", "separate") or "separate"


# ---------------------------------------------------------------- interfaces Wi-Fi
@dataclass
class WifiIface:
    name: str
    driver: str = ""
    mac: str = ""
    phy: str = ""
    usb: bool = False
    ap: bool = False                # soporta modo AP
    band5: bool = False             # tiene canales de 5 GHz
    tsf: bool = False               # el driver expone el TSF (hace falta para ver imagen)
    down: bool = False              # interfaz apagada: el driver parcheado crea el atributo tsf solo al levantarla
    default_route: bool = False     # es la salida a Internet del equipo
    connected: str = ""             # SSID si está conectada como cliente
    blocked: bool = False           # rfkill
    channels_ok: List[int] = field(default_factory=list)   # canales de 5 GHz utilizables como AP

    @property
    def suitable(self) -> bool:
        return self.ap and self.band5 and not self.default_route

    def summary(self) -> str:
        bits = [self.driver or "?", "USB" if self.usb else "interna"]
        bits.append("AP" if self.ap else "sin AP")
        bits.append("5 GHz" if self.band5 else "sin 5 GHz")
        bits.append("TSF" if self.tsf else ("TSF sin comprobar (apagada)" if self.down else "sin TSF"))
        if self.default_route:
            bits.append("sale a Internet")
        elif self.connected:
            bits.append(f"conectada a «{self.connected}»")
        return ", ".join(bits)


def parse_iw_phy(text: str) -> dict:
    """Interpreta `iw phy <phy> info`: modos admitidos y canales de 5 GHz que se pueden usar como AP."""
    modes = []
    m = re.search(r"Supported interface modes:\n((?:\s+\* .*\n)+)", text)
    if m:
        modes = [l.strip()[2:].strip() for l in m.group(1).splitlines()]
    chans: List[int] = []
    for freq, chan, rest in re.findall(r"\*\s+(\d+)(?:\.\d+)? MHz \[(\d+)\]\s*(.*)", text):
        if int(freq) >= 5000 and "disabled" not in rest and "no IR" not in rest:
            chans.append(int(chan))
    return {"modes": modes, "ap": "AP" in modes, "channels5": sorted(set(chans)), "band5": bool(chans)}


def list_wifi_ifaces(host: Optional[Host] = None) -> List[WifiIface]:
    host = host or Host()
    out: List[WifiIface] = []
    try:
        names = sorted(p.name for p in host.sys_net.iterdir() if (p / "wireless").exists() or (p / "phy80211").exists())
    except OSError:
        return out
    rc, route = host.run(["ip", "route", "show", "default"])
    default_devs = set(re.findall(r"dev (\S+)", route)) if rc == 0 else set()
    rc, rfk = host.run(["rfkill", "list"])
    for name in names:
        base = host.sys_net / name
        it = WifiIface(name=name)
        try:
            it.driver = Path(os.path.realpath(base / "device" / "driver")).name
        except OSError:
            pass
        try:
            it.usb = "/usb" in os.path.realpath(base / "device")
        except OSError:
            pass
        try:
            it.phy = (base / "phy80211" / "name").read_text().strip()
        except OSError:
            pass
        it.mac = _permanent_mac(base)
        it.tsf = drc_ap.tsf_sysfs_path(name, host.sys_net) is not None
        try:
            it.down = not (int((base / "flags").read_text().strip(), 16) & 1)   # IFF_UP
        except (OSError, ValueError):
            pass
        it.default_route = name in default_devs
        if it.phy:
            rc, info = host.run(["iw", "phy", it.phy, "info"], timeout=8)
            if rc == 0:
                p = parse_iw_phy(info)
                it.ap, it.band5, it.channels_ok = p["ap"], p["band5"], p["channels5"]
            if rc == 0 and rfk:
                it.blocked = _rfkill_blocked(rfk, it.phy)
        rc, link = host.run(["iw", "dev", name, "link"])
        m = re.search(r"SSID: (.*)", link) if rc == 0 else None
        it.connected = m.group(1).strip() if m else ""
        out.append(it)
    return out


def _permanent_mac(base: Path) -> str:
    for cand in (base / "phy80211" / "addresses", base / "address"):
        try:
            line = next(l.strip() for l in cand.read_text().splitlines() if l.strip())
            return line.lower()
        except (OSError, StopIteration):
            continue
    return ""


def _rfkill_blocked(text: str, phy: str) -> bool:
    for block in re.split(r"\n(?=\d+: )", text):
        if re.match(rf"\d+: {re.escape(phy)}:", block):
            return "blocked: yes" in block.lower()
    return False


def recommend_iface(ifaces: Sequence[WifiIface]) -> Optional[WifiIface]:
    """La mejor para el GamePad: AP + 5 GHz, que no sea la salida a Internet, y mejor con TSF y USB."""
    cands = [i for i in ifaces if i.suitable and not i.blocked]
    if not cands:
        return None
    return sorted(cands, key=lambda i: (not i.tsf, not i.usb, i.name))[0]


# ---------------------------------------------------------------- requisitos
def find_hostapd(host: Host) -> Optional[str]:
    cands = [str(REPO / "build_local/prefix/bin/hostapd"), "/usr/local/bin/hostapd", "/usr/sbin/hostapd"]
    for c in cands:
        if Path(c).exists():
            return c
    return host.which("hostapd")


def find_drc_stream(host: Host) -> Optional[str]:
    for c in (str(REPO / "build_local/prefix/bin/drc_stream"), "/usr/local/bin/drc_stream"):
        if Path(c).exists():
            return c
    return host.which("drc_stream")


def find_touch2pointer() -> Optional[str]:
    p = REPO / "build_local/prefix/bin/touch2pointer"
    return str(p) if p.exists() else None


def paired_gamepad(persist: Path = drc_ap.PERSIST_DIR, repo_cfg: Optional[Path] = None) -> Optional[str]:
    """MAC del GamePad emparejado (de la vinculación guardada), o None."""
    repo_cfg = repo_cfg or (REPO / "config")
    for d in (persist, repo_cfg):
        if (d / "generated_ap_normal.conf").exists():
            try:
                return (d / "generated_netboot_info.conf").read_text().strip() or "(MAC desconocida)"
            except OSError:
                return "(MAC desconocida)"
    return None


def _iface_by_name(ifaces: Sequence[WifiIface], name: str) -> Optional[WifiIface]:
    return next((i for i in ifaces if i.name == name), None)


def regdb_check(host: Host) -> Check:
    if host.regdb_present():
        return Check("regdb", "Base de datos regulatoria", OK, "regulatory.db instalada")
    return Check("regdb", "Base de datos regulatoria", WARN,
                 "falta regulatory.db: el dominio regulatorio se queda en «00» (mundo) aunque pidas WIFI_REGDOM, y "
                 "el canal 36 no admite punto de acceso",
                 "Instala wireless-regdb (Arch: sudo pacman -S wireless-regdb; Debian: sudo apt install wireless-regdb)")


def _iface_checks(host: Host, iface: str, ifaces: Sequence[WifiIface], cfg: dict, channel: int,
                  channel_key: str = "WIFI_CHANNEL") -> List[Check]:
    it = _iface_by_name(ifaces, iface)
    if it is None:
        found = ", ".join(i.name for i in ifaces) or "ninguna"
        return [Check("iface", "Interfaz Wi-Fi", FAIL, f"«{iface}» no existe (hay: {found})",
                      "Elige otra interfaz (opción «Elegir interfaz de red»).")]
    checks = [Check("iface", "Interfaz Wi-Fi", OK, f"{it.name}: {it.summary()}")]
    checks.append(Check("ap", "Modo AP", OK if it.ap else FAIL,
                        "la tarjeta puede crear un punto de acceso" if it.ap else "la tarjeta no admite modo AP",
                        "" if it.ap else "Usa otra tarjeta (el TP-Link USB con rtw89 sirve)."))
    if it.band5:
        usable = channel in it.channels_ok
        fix = f"Cambia {channel_key} o WIFI_REGDOM en config/drc.conf (p. ej. WIFI_REGDOM=\"US\")."
        if not usable and not host.regdb_present():
            fix = "Instala wireless-regdb (sin él WIFI_REGDOM no se aplica) o " + fix[0].lower() + fix[1:]
        checks.append(Check("band", f"Canal {channel} (5 GHz)", OK if usable else FAIL,
                            "permitido por el dominio regulatorio" if usable else
                            f"el canal {channel} no se puede usar (permitidos: {', '.join(map(str, it.channels_ok[:8]))}…)",
                            "" if usable else fix))
    else:
        checks.append(Check("band", "5 GHz", FAIL, "la tarjeta no tiene canales de 5 GHz",
                            "El GamePad solo habla en 5 GHz: usa otra tarjeta."))
    if it.default_route:
        checks.append(Check("internet", "No es tu conexión a Internet", FAIL,
                            f"{it.name} es la salida a Internet del equipo: se desconectaría al crear el AP",
                            "Elige otra interfaz (p. ej. la USB) para el GamePad."))
    elif it.connected:
        checks.append(Check("internet", "No es tu conexión a Internet", WARN,
                            f"{it.name} está conectada a «{it.connected}»: se desconectará",
                            "Está bien si no la usas para otra cosa."))
    else:
        checks.append(Check("internet", "No es tu conexión a Internet", OK, f"{it.name} está libre"))
    if it.blocked:
        checks.append(Check("rfkill", "Radio activada", FAIL, "rfkill tiene bloqueada esta radio", "rfkill unblock wifi"))
    return checks


def _privilege_check(host: Host) -> Check:
    if host.is_root():
        return Check("priv", "Permisos de administrador", OK, "ya eres root")
    if host.which("pkexec") or host.which("sudo"):
        return Check("priv", "Permisos de administrador", OK,
                     "se pedirá la contraseña al empezar (" + ("pkexec" if host.which("pkexec") else "sudo") + ")")
    return Check("priv", "Permisos de administrador", FAIL, "no hay sudo ni pkexec", "Instala sudo o polkit.")


def check_pair(host: Optional[Host] = None, iface: Optional[str] = None,
               symbols: Sequence[int] = (0, 0, 0, 0), cfg: Optional[dict] = None,
               ifaces: Optional[Sequence[WifiIface]] = None) -> List[Check]:
    """Qué hace falta para emparejar el GamePad y qué de eso se tiene ya."""
    host = host or Host()
    cfg = drc_ap.load_drc_conf() if cfg is None else cfg
    iface = iface or current_iface(cfg)
    ifaces = list_wifi_ifaces(host) if ifaces is None else ifaces
    channel = drc_ap.pairing_channel(cfg)   # el AP de vinculación va en su canal, no en el de la transmisión
    checks = _iface_checks(host, iface, ifaces, cfg, channel, "DRC_PAIR_CHANNEL") + [regdb_check(host), platform_check(cfg)]
    hostapd = find_hostapd(host)
    checks.append(Check("hostapd", "hostapd con parches DRC", OK if hostapd else FAIL,
                        hostapd or "no está compilado",
                        "" if hostapd else "bash drchost/build_local.sh (compila hostapd y drc_stream en build_local/)"))
    checks.append(_privilege_check(host))
    try:
        parse_symbols(",".join(map(str, symbols)))
        checks.append(Check("symbols", "Símbolos de emparejamiento", OK,
                            f"{show_symbols(symbols)}: tendrás que introducir los mismos en el GamePad"))
    except ValueError as e:
        checks.append(Check("symbols", "Símbolos de emparejamiento", FAIL, str(e), "Elige 4 símbolos."))
    checks.append(Check("conf", "Configuración drc.conf", OK if cfg else WARN,
                        "cargada" if cfg else "no se encontró: se usan los valores por defecto",
                        "" if cfg else "Copia config/drc.conf.example a config/drc.conf."))
    prior = paired_gamepad()
    checks.append(Check("paired", "Emparejamiento anterior", OK if not prior else WARN,
                        "ninguno" if not prior else f"ya hay un GamePad emparejado ({prior}); se reemplazará"))
    return checks


def _session_tool_checks(host: Host, cfg: dict) -> List[Check]:
    session = (host.env("DESKTOP_SESSION_DRC") or cfg.get("DESKTOP_SESSION", "auto")).lower()
    out: List[Check] = []
    need = {"cage": "cage", "kiosk": "cage", "steam": "cage", "wiiu-wm": None, "labwc": "labwc", "sway": "sway"}.get(session, "")
    if need:
        found = host.which(need)
        out.append(Check("compositor", f"Sesión del GamePad ({session})", OK if found else FAIL,
                         found or f"falta {need}", "" if found else f"sudo pacman -S {need}"))
        if need == "cage":
            w, h = cfg.get("DISPLAY_WIDTH", "854") or "854", cfg.get("DISPLAY_HEIGHT", "480") or "480"
            rr = host.which("wlr-randr")
            out.append(Check("resolution", f"Resolución de la sesión ({w}x{h})", OK if rr else WARN,
                             f"{w}x{h}, la del mando" if rr else
                             "falta wlr-randr: queda en 1280x720 y los juegos no ofrecen la del mando",
                             "" if rr else "sudo pacman -S wlr-randr"))
    elif session == "wiiu-wm":
        wm = REPO / "build_local/prefix/bin/wiiu-wm"
        out.append(Check("compositor", "Sesión del GamePad (wiiu-wm)", OK if wm.exists() else FAIL,
                         str(wm) if wm.exists() else "falta wiiu-wm", "bash scripts/build_desktop_local.sh (en el repo wiiu-wm)"))
    else:
        out.append(Check("compositor", "Sesión del GamePad (auto)", OK,
                         "se usa el escritorio del equipo" if host.which("Hyprland") or host.which("sway") else
                         "se usará labwc si está", ""))
    out.append(Check("recorder", "Captura de vídeo (wf-recorder)", OK if host.which("wf-recorder") else FAIL,
                     host.which("wf-recorder") or "falta", "sudo pacman -S wf-recorder"))
    return out


def touch_mode_check(cfg: dict, mode: str) -> Check:
    """DRC_TOUCH_MODE=pen solo sirve si quien lee el táctil admite tabletas (lo mismo decide start_drc.sh)."""
    tm = (os.environ.get("DRC_TOUCH_MODE") or cfg.get("DRC_TOUCH_MODE", "pointer")).strip() or "pointer"
    if tm != "pen":
        return Check("pen", "Táctil", OK, "toques nativos" if tm == "touch" else "como ratón (cursor y clic)")
    if mode != "separate":
        return Check("pen", "Táctil", OK, "lápiz con presión: lo recibe el escritorio del equipo como una tableta")
    session = (cfg.get("DESKTOP_SESSION", "auto") or "auto").lower()
    comp = (cfg.get("DRC_COMPOSITOR", "") or "").lower()
    host_hypr = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) or "hyprland" in os.environ.get(
        "XDG_CURRENT_DESKTOP", "").lower()
    why = ""
    if session in ("cage", "kiosk", "steam"):
        why = "la sesión del mando es cage, que no admite tabletas"
    elif "wiiu-wm" in (session, comp):
        why = "wiiu-wm no admite tabletas"
    elif session in ("auto", "hyprland") and not comp and host_hypr:
        why = "con Hyprland anidado el lápiz iría al escritorio del equipo"
    if why:
        return Check("pen", "Táctil", WARN, f"lápiz pedido, pero {why}: se usará la pantalla táctil (sin presión)",
                     "Usa el modo extended/mirror o una sesión con tabletas (DESKTOP_SESSION=labwc o sway).")
    return Check("pen", "Táctil", OK, f"lápiz con presión en la sesión del mando ({session})")


def mode_check(mode: str) -> Check:
    if mode == "separate":
        return Check("mode", "Modo de pantalla", OK, "separate: el GamePad tiene su propia sesión, sin tocar tu escritorio")
    return Check("mode", "Modo de pantalla", WARN,
                 f"{mode}: el GamePad será un monitor de tu escritorio (verás tu barra y el foco se comparte)",
                 "Para una sesión propia pon el modo en separate (Configuración).")


def check_start(host: Optional[Host] = None, iface: Optional[str] = None, cfg: Optional[dict] = None,
                ifaces: Optional[Sequence[WifiIface]] = None, persist: Path = drc_ap.PERSIST_DIR,
                mode: str = "") -> List[Check]:
    """Qué hace falta para empezar a transmitir (según el modo: separate = sesión propia del GamePad)."""
    host = host or Host()
    cfg = drc_ap.load_drc_conf() if cfg is None else cfg
    mode = mode or cfg.get("DRC_MODE", "separate")
    iface = iface or current_iface(cfg)
    ifaces = list_wifi_ifaces(host) if ifaces is None else ifaces
    channel = int(cfg.get("WIFI_CHANNEL", 40) or 40)
    paired = paired_gamepad(persist)
    checks = [mode_check(mode), platform_check(cfg)]
    if (os.environ.get("DRC_TOUCH_MODE") or cfg.get("DRC_TOUCH_MODE", "")).strip() == "pen":
        checks.append(touch_mode_check(cfg, mode))
    checks.append(Check("paired", "GamePad emparejado", OK if paired else FAIL,
                    f"MAC {paired}" if paired else "no hay ninguna vinculación guardada",
                    "" if paired else "Empareja primero (opción «Emparejar»)."))
    checks += _iface_checks(host, iface, ifaces, cfg, channel) + [regdb_check(host)]
    it = _iface_by_name(ifaces, iface)
    if it is not None:
        allow = host.env("DRC_ALLOW_NO_TSF") == "1"
        if it.tsf:
            checks.append(Check("tsf", "TSF del driver (sin él no hay imagen)", OK, f"/sys/class/net/{iface}/tsf"))
        elif it.down:
            # El driver parcheado crea /sys/class/net/<if>/tsf al levantar el AP: con la interfaz apagada no se puede ver.
            # drc_ap.py lo comprueba otra vez con el AP en marcha y se detiene si falta.
            checks.append(Check("tsf", "TSF del driver (sin él no hay imagen)", WARN,
                                f"{iface} está apagada: el atributo tsf aparece al levantar el AP, así que no se puede comprobar ahora; se verifica al arrancar",
                                "Si al arrancar falla: sudo bash tools/install_rtw89.sh en el repo drc-wifi-drivers (repetir tras cada actualización del kernel)"))
        else:
            checks.append(Check("tsf", "TSF del driver (sin él no hay imagen)", WARN if allow else FAIL,
                                drc_ap.TSF_MISSING_HELP.format(iface=iface),
                                "sudo bash tools/install_rtw89.sh en el repo drc-wifi-drivers (repetir tras cada actualización del kernel)"))
    hostapd = find_hostapd(host)
    checks.append(Check("hostapd", "hostapd con parches DRC", OK if hostapd else FAIL, hostapd or "no está compilado",
                        "" if hostapd else "bash drchost/build_local.sh"))
    stream = find_drc_stream(host)
    checks.append(Check("drc_stream", "drc_stream (codificador y transmisión)", OK if stream else FAIL,
                        stream or "no está compilado", "" if stream else "bash drchost/build_local.sh"))
    checks.append(_privilege_check(host))
    if mode == "separate":
        checks += _session_tool_checks(host, cfg)
    else:
        checks.append(Check("desktop", f"Escritorio del equipo (modo {mode})", OK if host.which("hyprctl") else FAIL,
                            "Hyprland: el GamePad será un monitor de tu sesión" if host.which("hyprctl") else
                            "este modo necesita Hyprland", "" if host.which("hyprctl") else "Usa el modo separate."))
        checks.append(Check("recorder", "Captura de vídeo (wf-recorder)", OK if host.which("wf-recorder") else FAIL,
                            host.which("wf-recorder") or "falta", "sudo pacman -S wf-recorder"))
    running = running_processes(host)
    checks.append(Check("running", "No hay otra transmisión en marcha", OK if not running.get("drc_stream") else FAIL,
                        "libre" if not running.get("drc_stream") else "ya hay un drc_stream corriendo",
                        "" if not running.get("drc_stream") else "Detén la transmisión actual primero."))
    return checks


# ---------------------------------------------------------------- estado
def running_processes(host: Host) -> Dict[str, bool]:
    out = {}
    for name in ("hostapd", "drc_stream", "cage", "wiiu-wm", "touch2pointer"):
        rc, _ = host.run(["pgrep", "-x", name])
        out[name] = rc == 0
    rc, _ = host.run(["pgrep", "-f", "start_drc.sh"])
    out["start_drc"] = rc == 0
    return out


def status(host: Optional[Host] = None, iface: Optional[str] = None, cfg: Optional[dict] = None) -> dict:
    host = host or Host()
    cfg = drc_ap.load_drc_conf() if cfg is None else cfg
    iface = iface or current_iface(cfg)
    proc = running_processes(host)
    rc, addr = host.run(["ip", "-4", "addr", "show", "dev", iface])
    # La IP 192.168.1.10 se queda puesta tras una sesión; el AP solo está activo si hostapd corre y la tiene.
    ap_up = bool(proc.get("hostapd")) and rc == 0 and CONSOLE_IP in addr
    reach = False
    if ap_up:
        rc, _ = host.run(["ping", "-c", "1", "-W", "1", GAMEPAD_IP], timeout=3)
        reach = rc == 0
    streaming = proc.get("drc_stream", False)
    if streaming and reach:
        state = "transmitiendo"
    elif streaming:
        state = "transmitiendo (el GamePad no responde)"
    elif ap_up and reach:
        state = "GamePad conectado (sin transmitir)"
    elif proc.get("hostapd"):
        state = "esperando al GamePad"
    else:
        state = "stop"
    return {
        "state": state,
        "iface": iface,
        "ap_up": ap_up,
        "gamepad_reachable": reach,
        "paired": paired_gamepad(),
        "processes": proc,
        "session": cfg.get("DESKTOP_SESSION", "auto"),
        "mode": cfg.get("DRC_MODE", "separate"),
    }


def format_status(st: dict) -> str:
    p = st["processes"]
    on = lambda k: "sí" if p.get(k) else "no"
    return "\n".join([
        f"Estado:        {st['state']}",
        f"Interfaz:      {st['iface']} (AP {'activo' if st['ap_up'] else 'inactivo'})",
        f"GamePad:       {'responde en ' + GAMEPAD_IP if st['gamepad_reachable'] else 'no responde'}",
        f"Emparejado:    {st['paired'] or 'no'}",
        f"Pantalla:      modo {st['mode']}, sesión {st['session']}",
        f"Procesos:      hostapd {on('hostapd')}, drc_stream {on('drc_stream')}, sesión (cage/wm) "
        f"{'sí' if p.get('cage') or p.get('wiiu-wm') else 'no'}",
    ])


# ---------------------------------------------------------------- lanzar acciones
def privilege_prefix(host: Host, gui: bool = False) -> List[str]:
    """Orden delante del comando que necesita root. En la CLI, sudo (pide la contraseña en la terminal); en la
    interfaz gráfica, pkexec (ventana de polkit)."""
    if host.is_root():
        return []
    if gui and host.which("pkexec"):
        return ["pkexec"]
    if host.which("sudo"):
        return ["sudo"]
    if host.which("pkexec"):
        return ["pkexec"]
    return []


def pair_command(host: Host, iface: str, symbols: Sequence[int], gui: bool = False) -> List[str]:
    return privilege_prefix(host, gui) + [
        "env", f"DRC_DESKTOP_USER={host.user()}",
        sys.executable, str(HERE / "drc_ap.py"), "--pair", "--iface", iface,
        "--symbols", ",".join(map(str, symbols)),
    ]


def start_command(host: Host, iface: str, mode: str = "", gui: bool = False) -> List[str]:
    """Siempre con el modo explícito (el que se ve en pantalla), nunca uno heredado en silencio."""
    mode = mode or effective_mode()
    cmd = privilege_prefix(host, gui) + ["env", f"DRC_DESKTOP_USER={host.user()}"]
    return cmd + ["bash", str(HERE / "start_drc.sh"), "--iface", iface, "--mode", mode]


def stop_command(host: Host, gui: bool = False) -> List[str]:
    return privilege_prefix(host, gui) + ["pkill", "-INT", "-f", "start_drc.sh"]


# ---------------------------------------------------------------- presentación (CLI)
ICON = {OK: "✓", WARN: "!", FAIL: "✗"}
COLOR = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}


def format_checks(checks: Sequence[Check], color: bool = False) -> str:
    lines = []
    for c in checks:
        mark = ICON[c.status]
        if color:
            mark = f"{COLOR[c.status]}{mark}\033[0m"
        lines.append(f" {mark} {c.name}" + (f" — {c.detail}" if c.detail else ""))
        if c.status != OK and c.fix:
            lines.append(f"     → {c.fix}")
    return "\n".join(lines)


def format_ifaces(ifaces: Sequence[WifiIface], current: str = "") -> str:
    if not ifaces:
        return "(no hay interfaces Wi-Fi)"
    best = recommend_iface(ifaces)
    lines = []
    for n, i in enumerate(ifaces, 1):
        tags = []
        if i.name == current:
            tags.append("actual")
        if best is not None and i.name == best.name:
            tags.append("recomendada")
        if not i.suitable:
            tags.append("no sirve" if not (i.ap and i.band5) else "es tu Internet")
        lines.append(f" {n}) {i.name:<14} {i.summary()}" + (f"  [{', '.join(tags)}]" if tags else ""))
    return "\n".join(lines)


def run_action(cmd: List[str]) -> int:
    print("$ " + " ".join(cmd), flush=True)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130


# ---------------------------------------------------------------- menú interactivo
def ask(prompt: str, input_fn: Callable[[str], str] = input) -> str:
    try:
        return input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def choose_iface(host: Host, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> Optional[str]:
    cfg = drc_ap.load_drc_conf()
    ifaces = list_wifi_ifaces(host)
    cur = current_iface(cfg)
    out("Interfaces Wi-Fi:\n" + format_ifaces(ifaces, cur))
    if not ifaces:
        return None
    sel = ask("Número de la interfaz (Enter = dejar la actual): ", input_fn)
    if not sel:
        return None
    if sel.isdigit() and 1 <= int(sel) <= len(ifaces):
        name = ifaces[int(sel) - 1].name
        out(f"Interfaz elegida: {name} (guardada en {set_iface(name)})")
        return name
    out("Opción no válida.")
    return None


def ask_symbols(input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> Tuple[int, int, int, int]:
    cur = current_symbols()
    out("Símbolos: 0=♠ 1=♥ 2=♦ 3=♣")
    while True:
        raw = ask(f"Los 4 símbolos que pondrás en el GamePad [{show_symbols(cur)}] (Enter = esos): ", input_fn)
        if not raw:
            return cur
        try:
            sym = parse_symbols(raw)
            save_state({"symbols": ",".join(map(str, sym))})
            return sym
        except ValueError as e:
            out(str(e))


def problems(checks: Sequence[Check]) -> List[Check]:
    return [c for c in checks if c.status != OK]


def do_pair(host: Host, input_fn=input, out=print, runner=run_action) -> int:
    iface = current_iface()
    sym = ask_symbols(input_fn, out)
    checks = check_pair(host, iface, sym)
    bad = blocking(checks)
    if problems(checks):
        out("\n" + format_checks(problems(checks), color=sys.stdout.isatty()))
    if bad:
        out(f"\nFalta resolver {len(bad)} cosa(s) antes de poder emparejar.")
        return 1
    if problems(checks) and ask("\n¿Seguir? [S/n] ", input_fn).lower() in ("n", "no"):
        return 0
    out(f"\nEn el GamePad: botón SYNC (detrás) y los símbolos {show_symbols(sym)}.")
    return runner(pair_command(host, iface, sym))


def do_start(host: Host, input_fn=input, out=print, runner=run_action) -> int:
    iface = current_iface()
    checks = check_start(host, iface)
    bad = blocking(checks)
    if problems(checks):
        out(format_checks(problems(checks), color=sys.stdout.isatty()))
    if bad:
        out(f"\nFalta resolver {len(bad)} cosa(s) antes de transmitir.")
        return 1
    if problems(checks) and ask("\n¿Seguir? [S/n] ", input_fn).lower() in ("n", "no"):
        return 0
    out("Ctrl+C para detener la transmisión.")
    return runner(start_command(host, iface))


def clear_screen() -> None:
    """Limpia la terminal (y su historial visible) para que las pantallas del menú no se apilen."""
    if sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J\033[3J")
        sys.stdout.flush()


def edit_config(input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
                clear: Callable[[], None] = lambda: None, path: Optional[Path] = None) -> None:
    """Pantalla de configuración: lista los ajustes de drc.conf y deja cambiar el que se elija."""
    msg = ""
    while True:
        clear()
        cfg = drc_ap.load_drc_conf()
        where = path or conf_path()
        out(f"=== Configuración ({where}) ===")
        group = ""
        for n, f in enumerate(CONFIG_FIELDS, 1):
            if f.group != group:
                group = f.group
                out(f"\n {group}")
            val = field_value(cfg, f)
            out(f"  {n:>2}) {f.label}: {val if val != '' else '(vacío)'}")
        if msg:
            out("\n" + msg)
        sel = ask("\nNúmero del ajuste a cambiar (Enter = volver): ", input_fn)
        if not sel:
            return
        if not (sel.isdigit() and 1 <= int(sel) <= len(CONFIG_FIELDS)):
            msg = "Opción no válida."
            continue
        f = CONFIG_FIELDS[int(sel) - 1]
        cur = field_value(cfg, f)
        out(f"\n{f.label} (ahora: {cur if cur != '' else '(vacío)'})" + (f"\n{f.help}" if f.help else ""))
        if f.kind == "choice":
            out("  " + "   ".join(f"{i}) {c}" for i, c in enumerate(f.choices, 1)))
        elif f.kind == "int":
            out(f"  Número entre {f.lo} y {f.hi}")
        elif f.kind == "bool":
            out("  1 = sí, 0 = no")
        elif f.kind == "text":
            out("  Escribe el valor («-» lo deja vacío)")
        raw = ask("Nuevo valor (Enter = no cambiar): ", input_fn)
        if not raw:
            msg = "Sin cambios."
            continue
        if f.kind == "choice" and raw.isdigit() and 1 <= int(raw) <= len(f.choices):
            raw = f.choices[int(raw) - 1]
        if f.kind == "text" and raw == "-":
            raw = ""
        try:
            value = validate_field(f, raw)
            set_conf_value(f.key, value, where)
            msg = f"Guardado: {f.label} = {value if value != '' else '(vacío)'}"
        except (ValueError, OSError) as e:
            msg = f"No se guardó: {e}"


def menu(host: Optional[Host] = None, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
         runner: Callable[[List[str]], int] = run_action, interactive: bool = True) -> int:
    host = host or Host()
    clear = clear_screen if interactive else (lambda: None)
    pause = (lambda: ask("\nEnter para volver al menú… ", input_fn)) if interactive else (lambda: None)
    while True:
        clear()
        cfg = drc_ap.load_drc_conf()
        out(f"=== Wii U GamePad — interfaz: {current_iface(cfg)} · modo: {effective_mode(cfg)} ===")
        out(f"    Estado: {status(host, cfg=cfg)['state']}\n")
        out(" 1) Emparejar el GamePad\n 2) Empezar la transmisión\n 3) Detener la transmisión\n"
            " 4) Ver el estado\n 5) Elegir interfaz de red\n 6) Configuración\n 7) Salir")
        sel = ask("> ", input_fn)
        if sel in ("7", "q", "salir", ""):
            return 0
        if sel in ("1", "2", "3", "4", "5"):
            clear()
        if sel == "1":
            do_pair(host, input_fn, out, runner)
        elif sel == "2":
            do_start(host, input_fn, out, runner)
        elif sel == "3":
            runner(stop_command(host))
        elif sel == "4":
            out(format_status(status(host)))
        elif sel == "5":
            choose_iface(host, input_fn, out)
        elif sel == "6":
            edit_config(input_fn, out, clear)
            continue
        else:
            out("Opción no válida.")
        pause()


# ---------------------------------------------------------------- CLI
def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Control del Wii U GamePad (emparejar, transmitir, estado)")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("ifaces")
    c = sub.add_parser("check"); c.add_argument("what", choices=("pair", "start")); c.add_argument("--iface"); c.add_argument("--symbols"); c.add_argument("--mode", default="")
    pr = sub.add_parser("pair"); pr.add_argument("--iface"); pr.add_argument("--symbols")
    st = sub.add_parser("start"); st.add_argument("--iface"); st.add_argument("--mode", default="")
    sub.add_parser("stop")
    sub.add_parser("config")
    sv = sub.add_parser("set"); sv.add_argument("key"); sv.add_argument("value")
    ifc = sub.add_parser("iface"); ifc.add_argument("name")
    args = p.parse_args(argv)
    host = Host()
    if args.cmd is None:
        return menu(host)
    if args.cmd == "status":
        print(format_status(status(host)))
        return 0
    if args.cmd == "ifaces":
        print(format_ifaces(list_wifi_ifaces(host), current_iface()))
        return 0
    if args.cmd == "config":
        cfg = drc_ap.load_drc_conf()
        for f in CONFIG_FIELDS:
            print(f"{f.key:<20} {field_value(cfg, f)!s:<28} {f.label}")
        return 0
    if args.cmd == "set":
        f = FIELDS_BY_KEY.get(args.key)
        if f is None:
            print(f"Ajuste desconocido: {args.key} (mira `config`)", file=sys.stderr)
            return 2
        try:
            value = validate_field(f, args.value)
            print(f"{f.key} = {value!r} guardado en {set_conf_value(f.key, value)}")
        except (ValueError, OSError) as e:
            print(f"No se guardó: {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "iface":
        try:
            print(f"Interfaz elegida: {args.name} (guardada en {set_iface(args.name)})")
        except OSError as e:
            print(f"No se guardó: {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "stop":
        return run_action(stop_command(host))
    iface = getattr(args, "iface", None) or current_iface()
    if args.cmd == "check" and args.what == "start" or args.cmd == "start":
        checks = check_start(host, iface, mode=getattr(args, "mode", "") or "")
        if args.cmd == "check":
            print(format_checks(checks, color=sys.stdout.isatty()))
            return 1 if blocking(checks) else 0
        if blocking(checks):
            print(format_checks(checks, color=sys.stdout.isatty()))
            return 1
        return run_action(start_command(host, iface, args.mode))
    sym = parse_symbols(args.symbols) if getattr(args, "symbols", None) else current_symbols()
    checks = check_pair(host, iface, sym)
    if args.cmd == "check":
        print(format_checks(checks, color=sys.stdout.isatty()))
        return 1 if blocking(checks) else 0
    if blocking(checks):
        print(format_checks(checks, color=sys.stdout.isatty()))
        return 1
    return run_action(pair_command(host, iface, sym))


if __name__ == "__main__":
    sys.exit(main())
