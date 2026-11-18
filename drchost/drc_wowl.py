#!/usr/bin/env python3
"""La Pi (como consola) enciende un GamePad apagado: emite el aviso Wake-on-WLAN.

El mando, apagado, mantiene un filtro de paquetes (`pkt_filter`, firmware `FUN_00020316`): id 0x80, patrón de
6 B en el offset 15 del cuerpo = la MAC WoWL que tiene guardada (la de la consola/Pi), tras descifrar con la
clave WoWL (16 B, AES-CCM). Esa clave y la MAC salen de la configuración del mando (comando (5,6): clave en
blob[0x6d], MAC en blob[0x7f], cada campo con su CRC16 detrás).

La trama es una QoS data FromDS protegida (TID 7, como la que emite la consola): cuerpo de 25 B con la MAC en
el offset 15, CCMP con PN creciente. Se emite en ráfaga por una interfaz en monitor.

Uso (root, con la interfaz en monitor en el canal del mando):
  drc_wowl.py --blob ~/.wowl_blob.bin --mando aa:bb:cc:dd:ee:01 [--iface wlan1] [--copias 120] [--rondas 20]
"""
import argparse
import socket
import struct
import sys
import time

TID = 7
LARGO_CUERPO = 25
OFFSET_MAC = 15


def crc16(datos):
    """CRC16 del blob de configuración del mando (init 0xffff, polinomio reflejado 0x8408)."""
    c = 0xFFFF
    for b in datos:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c


def _wowl_de(blob):
    if len(blob) < 0x87:
        return None
    clave, mac = blob[0x6D:0x7D], blob[0x7F:0x85]
    if struct.unpack_from("<H", blob, 0x7D)[0] != crc16(clave):
        return None
    if struct.unpack_from("<H", blob, 0x85)[0] != crc16(mac):
        return None
    return clave, mac


def leer_wowl(ruta):
    """(clave 16 B, MAC 6 B) del blob de configuración (0x300 B) del mando, o None si falta o el CRC falla. Acepta
    también la respuesta entera de `raw 5 06` por el FIFO, que lleva 16 B de cabecera delante."""
    try:
        blob = open(ruta, "rb").read()
    except OSError:
        return None
    return _wowl_de(blob) or _wowl_de(blob[16:])


def cuerpo_wake(mac_consola):
    c = bytearray(LARGO_CUERPO)
    c[OFFSET_MAC:OFFSET_MAC + 6] = mac_consola
    return bytes(c)


def _ccmp(clave, a1, a2, a3, fc, sc, qos, pn, cuerpo):
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    pn_bytes = pn.to_bytes(6, "big")
    nonce = bytes([qos[0] & 0xF]) + a2 + pn_bytes
    aad = bytes([fc[0] & 0x8F, fc[1] & 0xC7]) + a1 + a2 + a3 + bytes([sc[0] & 0x0F, 0]) + bytes([qos[0] & 0xF, 0])
    return AESCCM(clave, 8).encrypt(nonce, cuerpo, aad)


def descifrar(trama, clave):
    """Descifra una trama de datos protegida (CCMP) probando las variantes plausibles de la clave WoWL: tal cual o
    invertida, con la prioridad de la trama o 0 en el nonce, MIC de 8 o 16 B. Devuelve (variante, cuerpo) o None.
    Sirve para leer el aviso que emite el propio mando (ToDS) y saber qué clave y cuerpo espera."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    d = trama
    if len(d) < 24 or (d[0] & 0x0C) != 0x08 or not d[1] & 0x40:
        return None
    h = 24 + (6 if (d[1] & 0x03) == 0x03 else 0)
    qos = d[h:h + 2] if d[0] & 0x80 else b""
    h += len(qos)
    if len(d) < h + 8 + 8 + 1:
        return None
    pn = bytes([d[h + 7], d[h + 6], d[h + 5], d[h + 4], d[h + 1], d[h]])
    tid = qos[0] & 0xF if qos else 0
    aad = bytes([d[0] & 0x8F, d[1] & 0xC7]) + d[4:22] + bytes([d[22] & 0x0F, 0]) + d[24:h - len(qos)] + \
        (bytes([tid, 0]) if qos else b"")
    datos = d[h + 8:]
    for nombre, k in (("clave tal cual", clave), ("clave invertida", clave[::-1])):
        for prio in dict.fromkeys((tid, 0)):
            for mic in (8, 16):
                if len(datos) <= mic:
                    continue
                try:
                    plano = AESCCM(k, mic).decrypt(bytes([prio]) + d[10:16] + pn, datos, aad)
                except (InvalidTag, ValueError):
                    continue
                extra = (" (prioridad 0)" if prio != tid else "") + (" (MIC 16)" if mic == 16 else "")
                return nombre + extra, plano
    return None


def trama_wake(clave, mac_consola, mac_mando, pn=1, seq=0):
    """QoS data FromDS protegida de la consola (`mac_consola`) hacia el mando; sin FCS."""
    fc = bytes([0x88, 0x42])
    sc = struct.pack("<H", (seq & 0xFFF) << 4)
    qos = bytes([TID, 0])
    cab = fc + bytes([0x3C, 0x00]) + mac_mando + mac_consola + mac_consola + sc + qos
    ccmp = bytes([pn & 0xFF, (pn >> 8) & 0xFF, 0, 0x20]) + (pn >> 16).to_bytes(4, "little")
    return cab + ccmp + _ccmp(clave, mac_mando, mac_consola, mac_consola, fc, sc, qos, pn, cuerpo_wake(mac_consola))


def rafaga_wake(clave, mac_consola, mac_mando, n=120, pn0=1, seq0=0, pn_fijo=False, por_seq=1):
    """`n` tramas. Por defecto PN y secuencia suben en cada una. Con `pn_fijo` se repite el mismo PN y la
    secuencia sube cada `por_seq` tramas: como la ráfaga real de la consola (~800 tramas en 0,2 s)."""
    return [trama_wake(clave, mac_consola, mac_mando,
                       pn=pn0 if pn_fijo else pn0 + i, seq=seq0 + i // por_seq) for i in range(n)]


def con_radiotap(trama):
    return bytes([0, 0, 8, 0, 0, 0, 0, 0]) + trama


def emitir(iface, tramas, pausa=0.0005, enviar=None):
    s = None
    if enviar is None:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        s.bind((iface, 0))
        enviar = s.send
    n = 0
    for t in tramas:
        enviar(con_radiotap(t))
        n += 1
        time.sleep(pausa)
    if s:
        s.close()
    return n


def _conf_iface() -> str:
    """Interfaz Wi-Fi del GamePad: WIFI_IFACE de drc.conf (o wlan0)."""
    try:
        from drc_ap import load_drc_conf
        return load_drc_conf().get("WIFI_IFACE", "wlan0")
    except Exception:
        return "wlan0"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--blob", required=True, help="configuración (0x300 B) leída del mando con `raw 5 06`")
    ap.add_argument("--mando", required=True, help="MAC del GamePad")
    ap.add_argument("--iface", default=_conf_iface())
    ap.add_argument("--copias", type=int, default=120)
    ap.add_argument("--rondas", type=int, default=20)
    ap.add_argument("--pausa-rondas", type=float, default=0.25)
    ap.add_argument("--como-consola", action="store_true",
                    help="una sola ráfaga densa como la real: 800 tramas, PN=1 repetido, sin pausa entre ellas")
    a = ap.parse_args(argv)
    r = leer_wowl(a.blob)
    if r is None:
        print("[drc_wowl] blob sin clave WoWL válida", file=sys.stderr)
        return 1
    clave, mac_consola = r
    mando = bytes.fromhex(a.mando.replace(":", ""))
    if a.como_consola:
        n = emitir(a.iface, rafaga_wake(clave, mac_consola, mando, n=800, pn_fijo=True, por_seq=7), pausa=0.0)
        print(f"[drc_wowl] ráfaga como la de la consola: {n} tramas", flush=True)
        return 0
    pn = 1
    for i in range(a.rondas):
        n = emitir(a.iface, rafaga_wake(clave, mac_consola, mando, n=a.copias, pn0=pn, seq0=pn))
        pn += n
        print(f"[drc_wowl] ronda {i + 1}/{a.rondas}: {n} tramas (PN hasta {pn - 1})", flush=True)
        time.sleep(a.pausa_rondas)
    return 0


if __name__ == "__main__":
    sys.exit(main())
