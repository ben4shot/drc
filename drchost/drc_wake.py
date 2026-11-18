#!/usr/bin/env python3
"""Arranca la sesión `drc` cuando el GamePad pulsa su botón de encendido y la Pi no tiene el AP levantado.

El mando, al encenderse, emite una ráfaga (~220-830 copias en 50-200 ms) de una QoS data ToDS protegida de
71 B hacia la MAC de su consola: es el `wowl_pkt` del firmware (CCMP con la clave WoWL, PN=1). No hace falta
descifrarla: tipo, destino, tamaño y PN=1 + la cadencia bastan. Con `wlan1` en monitor se vigila y, al verla,
se devuelve la interfaz a modo gestionado y se arranca `drc` (que levanta el AP; el mando se asocia solo).

Uso (root):  drc_wake.py [--iface wlan1] [--canal 36] [--mando aa:bb:cc:dd:ee:01]
"""
import argparse
import socket
import struct
import subprocess
import sys
import time

LARGO_WAKE = 71
MINIMAS = 20
VENTANA = 0.5
ENFRIAMIENTO = 10.0


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


def _conf_iface() -> str:
    """Interfaz Wi-Fi del GamePad: WIFI_IFACE de drc.conf (o wlan0)."""
    try:
        from drc_ap import load_drc_conf
        return load_drc_conf().get("WIFI_IFACE", "wlan0")
    except Exception:
        return "wlan0"


def _conf_channel() -> int:
    """Canal del AP del GamePad: WIFI_CHANNEL de drc.conf (o 36)."""
    try:
        from drc_ap import load_drc_conf
        return int(load_drc_conf().get("WIFI_CHANNEL", "36") or 36)
    except Exception:
        return 36


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default=_conf_iface())
    ap.add_argument("--canal", type=int, default=_conf_channel())
    ap.add_argument("--saltar", action="store_true", help="saltar entre los canales de 5 GHz (busca probes)")
    ap.add_argument("--mando", default=None, help="MAC del GamePad; «auto» = el vinculado con la Pi; si se omite, cualquier emisor")
    ap.add_argument("--ausencia", type=int, default=None, metavar="S",
                    help="modo vigía (con drc activo): parar drc si el mando falta S segundos")
    a = ap.parse_args(argv)
    if a.ausencia is not None:
        mando = mando_vinculado() if a.mando in (None, "auto") else bytes.fromhex(a.mando.replace(":", ""))
        if mando is None:
            print("[drc_wake] sin GamePad vinculado: nada que vigilar", flush=True)
            return 0
        vigiar_ausencia(a.iface, mando, plazo=a.ausencia)
        return 0
    pi = mac_de(f"/sys/class/net/{a.iface}/address")
    if a.mando == "auto":
        mando = mando_vinculado()
        if mando is None:
            print("[drc_wake] sin GamePad vinculado: se acepta cualquier emisor", flush=True)
    else:
        mando = bytes.fromhex(a.mando.replace(":", "")) if a.mando else None
    for o in ordenes_monitor(a.iface, a.canal):
        subprocess.run(o, check=True)
    print(f"[drc_wake] vigilando {a.iface} canal {a.canal} (Pi {pi.hex(':')})", flush=True)
    vigilar(a.iface, pi, mando, canales=CANALES_5G if a.saltar and mando else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
