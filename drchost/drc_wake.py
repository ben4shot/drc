#!/usr/bin/env python3
"""Arranca la sesión `drc` cuando el GamePad pulsa su botón de encendido y la Pi no tiene el AP levantado.

El mando, al encenderse, emite una ráfaga (~220-830 copias en 50-200 ms) de una QoS data ToDS protegida de
71 B hacia la MAC de su consola: es el `wowl_pkt` del firmware (CCMP con la clave WoWL, PN=1). No hace falta
descifrarla: tipo, destino, tamaño y PN=1 + la cadencia bastan. Con `wlan1` en monitor se vigila y, al verla,
se devuelve la interfaz a modo gestionado y se arranca `drc` (que levanta el AP; el mando se asocia solo).

Uso (root):  drc_wake.py [--iface wlan1] [--canal 36] [--mando cc:fb:65:bf:44:29]
"""
import argparse
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _p in ("/usr/sbin", "/sbin", "/usr/local/sbin"):
    if _p not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = os.environ.get("PATH", "") + ":" + _p

LARGO_WAKE = 71
MINIMAS = 20
VENTANA = 0.5
ENFRIAMIENTO = 10.0
DROP_DIR_DRC = "/etc/systemd/system/drc.service.d"
AP_CONF_CANDIDATES = (
    "/var/lib/drc/generated_ap_normal.conf",
    "/etc/drc/generated_ap_normal.conf",
)
HOSTAPD_CANDIDATES = (
    "/opt/drc/build/vanilla-drc-hostap/hostapd/hostapd",
    "/opt/drc/build/drc-hostap/hostapd/hostapd",
    "/usr/local/bin/hostapd",
    "/usr/sbin/hostapd",
)


def dot11_de_radiotap(p):
    if len(p) < 4:
        return None
    rl = struct.unpack_from("<H", p, 2)[0]
    return p[rl:] if rl <= len(p) else None


def es_trama_wake(d, pi, mando=None):
    """¿Es una trama del aviso de encendido hacia `pi` (MAC de la Pi)? `mando` fija además el emisor."""
    if len(d) != LARGO_WAKE:
        return False
    if d[0] != 0x88 or (d[1] & 0x43) != 0x41:      # QoS data, ToDS + protegida, sin FromDS
        return False
    if d[4:10] != pi or d[16:22] != pi:            # RA = BSSID = la Pi
        return False
    if mando is not None and d[10:16] != mando:
        return False
    pn = bytes([d[26], d[27], d[30], d[31], d[32], d[33]])
    return pn == b"\x01\x00\x00\x00\x00\x00"       # clave recién instalada: PN=1


def es_probe_del_mando(d, mando):
    """Probe request (gestión, subtipo 4) emitido por el mando: así busca la red cuando no manda el aviso."""
    return mando is not None and len(d) >= 24 and d[0] == 0x40 and d[10:16] == mando


class DetectorWake:
    """Dispara (True) al ver `minimas` tramas de aviso dentro de `ventana` s; luego espera `enfriamiento` s."""

    def __init__(self, pi, minimas=MINIMAS, ventana=VENTANA, enfriamiento=ENFRIAMIENTO, mando=None):
        self.minimas, self.ventana, self.enfriamiento = minimas, ventana, enfriamiento
        self.pi, self.mando = pi, mando
        self._t = []
        self._hasta = None

    def ver(self, t, d):
        if self._hasta is not None and t < self._hasta:
            return False
        if es_probe_del_mando(d, self.mando):
            self._t = []
            self._hasta = t + self.enfriamiento
            return True
        if not es_trama_wake(d, self.pi, self.mando):
            return False
        self._t = [x for x in self._t if t - x <= self.ventana] + [t]
        if len(self._t) >= self.minimas:
            self._t = []
            self._hasta = t + self.enfriamiento
            return True
        return False


def mac_de(ruta):
    return bytes.fromhex(open(ruta).read().strip().replace(":", ""))


def mando_vinculado(ruta="/var/lib/drc/generated_netboot_info.conf"):
    """MAC del GamePad vinculado con la Pi, o None si no hay vinculación legible."""
    try:
        m = bytes.fromhex(open(ruta).read().strip().replace(":", ""))
    except (OSError, ValueError):
        return None
    return m if len(m) == 6 else None


def ordenes_arranque(iface):
    return [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "managed"],
        ["ip", "link", "set", iface, "up"],
        ["systemctl", "start", "--no-block", "drc"],
    ]


def ordenes_monitor(iface, canal):
    return [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
        ["iw", "dev", iface, "set", "channel", str(canal)],
    ]


CANALES_5G = [36, 40, 44, 48, 149, 153, 157, 161, 165]
PLAZO_CANAL = 1.0


def vigilar(iface, pi, mando, ejecutar=subprocess.run, ahora=time.time, canal_socket=None,
            canales=None, dwell=0.3):
    """Escucha hasta ver el encendido del mando. Con `canales`, salta entre ellos cada `dwell` s (el mando
    que no manda aviso busca la red con probes por todos los canales de 5 GHz)."""
    det = DetectorWake(pi, mando=mando)
    i_canal, t_canal = 0, ahora()
    s = canal_socket or socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    if canal_socket is None:
        s.bind((iface, 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)   # no perder el probe mientras salta
        s.settimeout(0.1 if canales else 1)
    while True:
        if canales:
            ahora_t = ahora()
            if ahora_t - t_canal >= dwell:
                i_canal = (i_canal + 1) % len(canales)
                t_canal = ahora_t
                # En monitor, el TP-Link a veces no contesta a un cambio de canal: plazo y se sigue escuchando
                try:
                    ejecutar(["iw", "dev", iface, "set", "channel", str(canales[i_canal])], check=False,
                             timeout=PLAZO_CANAL, capture_output=True)
                except subprocess.TimeoutExpired:
                    pass
                t_canal = ahora()                  # el tiempo en el canal cuenta desde que el cambio acaba
        try:
            p = s.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            return False
        d = dot11_de_radiotap(p)
        if d is not None and det.ver(ahora(), d):
            emisor = d[10:16].hex(":")
            que = "probe" if es_probe_del_mando(d, mando) else "aviso de encendido"
            print(f"[drc_wake] {que} de {emisor} -> arrancando drc", flush=True)
            for o in ordenes_arranque(iface):
                ejecutar(o, check=False)
            return True


def estacion_presente(volcado, mando):
    """¿Aparece el mando en la salida de `iw dev <iface> station dump`?"""
    return f"Station {mando.hex(':')}" in volcado


class ContadorAusencia:
    """True cuando el mando lleva `plazo` s seguidos sin estar asociado (contando desde el primer vistazo)."""

    def __init__(self, plazo=30):
        self.plazo = plazo
        self._desde = None

    def ver(self, t, presente):
        if presente:
            self._desde = None
            return False
        if self._desde is None:
            self._desde = t
        return t - self._desde > self.plazo


def vigiar_ausencia(iface, mando, plazo=30, ejecutar=subprocess.run, leer=None, ahora=time.time,
                    dormir=time.sleep, cada=2.0):
    """Con drc activo: si el mando falta del AP más de `plazo` s, para drc (el drop-in relanza drc-wake)."""
    if leer is None:
        def leer():
            r = subprocess.run(["iw", "dev", iface, "station", "dump"], capture_output=True, text=True,
                               timeout=5)
            return r.stdout
    cont = ContadorAusencia(plazo)
    while True:
        try:
            volcado = leer()
        except (subprocess.TimeoutExpired, OSError):
            volcado = ""
        if cont.ver(ahora(), estacion_presente(volcado, mando)):
            print(f"[drc_wake] {mando.hex(':')} lleva más de {plazo} s fuera -> parando drc", flush=True)
            ejecutar(["systemctl", "stop", "--no-block", "drc"], check=False)
            return True
        dormir(cada)


def _conf_path(conf_path=None):
    return conf_path or os.environ.get("DRC_CONF_FILE") or "/etc/drc/drc.conf"


def _conf_iface(default="wlan1"):
    p = _conf_path()
    if Path(p).is_file():
        try:
            for line in Path(p).read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"^\s*WIFI_IFACE\s*=\s*['\"]?([^\s'\"#]+)['\"]?", line)
                if m:
                    return m.group(1)
        except OSError:
            pass
    return default


def _conf_channel(default=36):
    p = _conf_path()
    if Path(p).is_file():
        try:
            for line in Path(p).read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"^\s*WIFI_CHANNEL\s*=\s*['\"]?(\d+)['\"]?", line)
                if m:
                    return int(m.group(1))
        except OSError:
            pass
    return default


def interfaz_configurada(drop_dir=DROP_DIR_DRC, conf_path=None, default="wlan1"):
    """Lee la interfaz activa de drc: último `Environment=DRC_IFACE=...` en los drop-ins de drc.service,
    si no `WIFI_IFACE` en drc.conf, si no `default`."""
    iface = None
    d = Path(drop_dir)
    if d.is_dir():
        for p in sorted(d.glob("*.conf")):
            try:
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    m = re.match(r"^\s*Environment\s*=\s*['\"]?DRC_IFACE=([^\s'\"]+)['\"]?", line)
                    if m:
                        iface = m.group(1)
            except OSError:
                pass
    if iface:
        return iface
    cp = _conf_path(conf_path)
    if cp and Path(cp).is_file():
        try:
            for line in Path(cp).read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"^\s*WIFI_IFACE\s*=\s*['\"]?([^\s'\"#]+)['\"]?", line)
                if m:
                    return m.group(1)
        except OSError:
            pass
    return default


def _regdom_configurado(conf_path="/etc/drc/drc.conf", default="US"):
    if conf_path and Path(conf_path).is_file():
        try:
            for line in Path(conf_path).read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"^\s*WIFI_REGDOM\s*=\s*['\"]?([^\s'\"#]+)['\"]?", line)
                if m:
                    return m.group(1)
        except OSError:
            pass
    return default


def soporta_monitor(iface, leer_phy=None, ejecutar=None):
    """¿El chip Wi-Fi de `iface` admite modo monitor según `iw phy <phy> info`?"""
    if leer_phy is None:
        phy_path = Path(f"/sys/class/net/{iface}/phy80211/name")
        try:
            phy = phy_path.read_text(encoding="utf-8").strip()
        except OSError:
            phy = ""
    else:
        phy = leer_phy(iface)
    if not phy:
        return False
    if ejecutar is None:
        try:
            r = subprocess.run(["iw", "phy", phy, "info"], capture_output=True, text=True, timeout=5, check=False)
            out = r.stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
    else:
        out = ejecutar(["iw", "phy", phy, "info"])
    en_modos = False
    for line in out.splitlines():
        if "Supported interface modes:" in line:
            en_modos = True
            continue
        if en_modos:
            s = line.strip()
            if s.startswith("*"):
                if s.lstrip("* ").strip() == "monitor":
                    return True
            elif s:
                break
    return False


def buscar_hostapd(candidates=HOSTAPD_CANDIDATES):
    for c in candidates:
        if Path(c).is_file():
            return str(c)
    return shutil.which("hostapd")


def buscar_ap_conf(candidates=AP_CONF_CANDIDATES):
    for c in candidates:
        if Path(c).is_file():
            return str(c)
    return None


def _preparar_iface_ap(iface, target_mac=None, ejecutar=subprocess.run):
    if shutil.which("nmcli"):
        ejecutar(["nmcli", "device", "disconnect", iface], check=False,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ejecutar(["nmcli", "device", "set", iface, "managed", "no"], check=False,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ejecutar(["rfkill", "unblock", "wifi"], check=False,
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ejecutar(["iw", "reg", "set", _regdom_configurado()], check=False,
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sock = Path(f"/var/run/hostapd/{iface}")
    try:
        if sock.exists():
            sock.unlink()
    except OSError:
        pass
    ejecutar(["ip", "link", "set", "dev", iface, "down"], check=False)
    if target_mac:
        ejecutar(["ip", "link", "set", "dev", iface, "address", target_mac], check=False)
    ejecutar(["ip", "link", "set", "dev", iface, "up"], check=True)


def vigilar_ap(iface, mando, ap_conf=None, hostapd_bin=None, ejecutar=subprocess.run,
               lanzar=subprocess.Popen, leer=None, dormir=time.sleep, cada=0.25):
    """Alternativa cuando `iface` no soporta modo monitor (p. ej. wlan0 interno):
    levanta solo hostapd ligero con la configuración del AP normal y, al asociarse el GamePad,
    detiene ese hostapd y arranca `drc`."""
    ap_conf = ap_conf or buscar_ap_conf()
    hostapd_bin = hostapd_bin or buscar_hostapd()
    if not ap_conf:
        print("[drc_wake] no se encontró generated_ap_normal.conf para AP en espera", file=sys.stderr, flush=True)
        return False
    if not hostapd_bin:
        print("[drc_wake] no se encontró binario hostapd para AP en espera", file=sys.stderr, flush=True)
        return False

    conf_text = Path(ap_conf).read_text(encoding="utf-8")
    fixed_conf = re.sub(r"^interface=.*$", f"interface={iface}", conf_text, flags=re.MULTILINE)
    if "interface=" not in fixed_conf:
        fixed_conf = f"interface={iface}\n" + fixed_conf

    m_ssid = re.search(r"^ssid=WiiU([0-9a-fA-F]{12})", fixed_conf, re.MULTILINE)
    target_mac = ":".join(m_ssid.group(1)[i:i + 2] for i in range(0, 12, 2)) if m_ssid else None

    _preparar_iface_ap(iface, target_mac=target_mac, ejecutar=ejecutar)

    if leer is None:
        def leer():
            r = subprocess.run(["iw", "dev", iface, "station", "dump"], capture_output=True, text=True, timeout=5)
            return r.stdout

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="drc_wake_ap_", suffix=".conf")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(fixed_conf)
        proc = lanzar([hostapd_bin, tmp_path])
        detectado = False
        try:
            while True:
                if proc.poll() is not None:
                    print(f"[drc_wake] hostapd terminó inesperadamente (código {proc.returncode})",
                          file=sys.stderr, flush=True)
                    return False
                try:
                    volcado = leer()
                except (subprocess.TimeoutExpired, OSError):
                    volcado = ""
                if (mando is not None and estacion_presente(volcado, mando)) or (
                    mando is None and "Station " in volcado
                ):
                    mac_txt = mando.hex(":") if mando is not None else "GamePad"
                    print(f"[drc_wake] asociación de {mac_txt} en AP en espera ({iface}) -> arrancando drc",
                          flush=True)
                    detectado = True
                    break
                dormir(cada)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            sock = Path(f"/var/run/hostapd/{iface}")
            try:
                if sock.exists():
                    sock.unlink()
            except OSError:
                pass
        if detectado:
            ejecutar(["systemctl", "start", "--no-block", "drc"], check=False)
            return True
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="auto",
                    help="interfaz Wi-Fi; «auto» usa la configurada en drc.service.d / drc.conf")
    ap.add_argument("--canal", type=int, default=36)
    ap.add_argument("--saltar", action="store_true", help="saltar entre los canales de 5 GHz (busca probes)")
    ap.add_argument("--mando", default=None, help="MAC del GamePad; «auto» = el vinculado con la Pi; si se omite, cualquier emisor")
    ap.add_argument("--ausencia", type=int, default=None, metavar="S",
                    help="modo vigía (con drc activo): parar drc si el mando falta S segundos")
    a = ap.parse_args(argv)
    iface = interfaz_configurada() if a.iface == "auto" else a.iface
    if a.ausencia is not None:
        mando = mando_vinculado() if a.mando in (None, "auto") else bytes.fromhex(a.mando.replace(":", ""))
        if mando is None:
            print("[drc_wake] sin GamePad vinculado: nada que vigilar", flush=True)
            return 0
        print(f"[drc_wake] vigilando ausencia en {iface} ({mando.hex(':')}, plazo {a.ausencia} s)", flush=True)
        vigiar_ausencia(iface, mando, plazo=a.ausencia)
        return 0
    if a.mando == "auto":
        mando = mando_vinculado()
        if mando is None:
            print("[drc_wake] sin GamePad vinculado: se acepta cualquier emisor", flush=True)
    else:
        mando = bytes.fromhex(a.mando.replace(":", "")) if a.mando else None
    if not soporta_monitor(iface):
        def _salir(_sig, _frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, _salir)
        signal.signal(signal.SIGINT, _salir)
        print(f"[drc_wake] {iface} no soporta modo monitor -> vigilando con AP en espera", flush=True)
        return 0 if vigilar_ap(iface, mando) else 1
    pi = mac_de(f"/sys/class/net/{iface}/address")
    for o in ordenes_monitor(iface, a.canal):
        subprocess.run(o, check=True)
    print(f"[drc_wake] vigilando {iface} canal {a.canal} (Pi {pi.hex(':')})", flush=True)
    vigilar(iface, pi, mando, canales=CANALES_5G if a.saltar and mando else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
