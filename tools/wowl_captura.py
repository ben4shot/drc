#!/usr/bin/env python3
"""Captura lo que emite el GamePad al encenderse sin su consola: el aviso Wake-on-WLAN (ráfaga de tramas de 71 B,
ToDS, cifradas con la clave WoWL) y sus probes. Si se le da la configuración del mando (`raw 5 06`, la guarda el
probador o `drc_wowl`), intenta descifrar el aviso: así se sabe qué clave y cuerpo usa, y la trama para encenderlo
desde el equipo es la misma en sentido contrario.

Con la sesión de drc PARADA (la interfaz pasa a modo monitor y se devuelve a managed al terminar):
  sudo python3 tools/wowl_captura.py [--segundos 30] [--canal 40] [--blob ~/drc_capturas/wowl_blob.bin]
Durante la captura, enciende el mando con POWER (y si arranca, apágalo y vuelve a encenderlo).
"""
import argparse
import os
import pwd
import socket
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "drchost"))


def del_mando(d, mando):
    return len(d) >= 16 and d[10:16] == mando


def tipo(d):
    if d[0] == 0x88 and len(d) in (67, 71) and (d[1] & 0x03) == 0x01:   # 71 = 67 + FCS si el driver la deja
        return f"aviso WoWL ({len(d)} B, ToDS)"
    if d[0] == 0x40:
        return "probe request"
    return f"trama {d[0]:02x}/{d[1]:02x}, {len(d)} B"


def resumen(tramas):
    r = {}
    for d in tramas:
        r[tipo(d)] = r.get(tipo(d), 0) + 1
    return r


def pcap_bytes(paquetes):
    """[(t, paquete con radiotap)] -> pcap (linktype 127 = radiotap)."""
    out = bytearray(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127))
    for t, p in paquetes:
        sec = int(t)
        out += struct.pack("<IIII", sec, int(round((t - sec) * 1e6)), len(p), len(p)) + p
    return bytes(out)


def sesion_abierta(pgrep=None):
    if pgrep is None:
        def pgrep():
            return subprocess.run(["pgrep", "-af", "hostapd"], capture_output=True, text=True).stdout
    return bool(pgrep().strip())


def _conf():
    try:
        from drc_ap import load_drc_conf
        return load_drc_conf()
    except Exception:
        return {}


def _mac_mando():
    for ruta in (os.path.join(ROOT, "config", "generated_netboot_info.conf"), "/var/lib/drc/generated_netboot_info.conf"):
        try:
            m = bytes.fromhex(open(ruta).read().strip().replace(":", ""))
            if len(m) == 6:
                return m
        except (OSError, ValueError):
            pass
    return None


def _canal():
    try:
        for ln in open(os.path.join(ROOT, "config", "generated_ap_normal.conf")):
            if ln.startswith("channel="):
                return int(ln.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return 40


def _home_usuario():
    u = os.environ.get("SUDO_USER")
    return (pwd.getpwnam(u).pw_dir, pwd.getpwnam(u).pw_uid, pwd.getpwnam(u).pw_gid) if u else (os.path.expanduser("~"), -1, -1)


def main(argv=None):
    home, uid, gid = _home_usuario()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iface", default=_conf().get("WIFI_IFACE", "wlan0"))
    ap.add_argument("--canal", type=int, default=_canal())
    ap.add_argument("--segundos", type=float, default=30)
    ap.add_argument("--blob", default=os.path.join(home, "drc_capturas", "wowl_blob.bin"))
    ap.add_argument("--pcap", default=os.path.join(home, "drc_capturas", "wowl_mando.pcap"))
    a = ap.parse_args(argv)
    if os.geteuid() != 0:
        sys.exit("Hace falta root: sudo python3 tools/wowl_captura.py")
    if sesion_abierta():
        sys.exit("La sesión de drc está abierta (hostapd): párala antes, la interfaz tiene que pasar a monitor.")
    mando = _mac_mando()
    if mando is None:
        sys.exit("No encuentro la MAC del mando vinculado (config/generated_netboot_info.conf).")
    import drc_wake
    import drc_wowl
    wowl = drc_wowl.leer_wowl(a.blob) if os.path.exists(a.blob) else None
    print(f"Interfaz {a.iface} en monitor, canal {a.canal}; mando {mando.hex(':')}; "
          f"clave WoWL: {'sí' if wowl else 'no (no se intentará descifrar)'}")
    for o in drc_wake.ordenes_monitor(a.iface, a.canal):
        subprocess.run(o, check=True)
    paquetes, tramas = [], []
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        s.bind((a.iface, 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        s.settimeout(0.2)
        print(f"\n>>> Enciende el mando con POWER ahora ({a.segundos:.0f} s)\n", flush=True)
        fin, visto = time.time() + a.segundos, {}
        while time.time() < fin:
            try:
                p = s.recv(4096)
            except socket.timeout:
                continue
            d = drc_wake.dot11_de_radiotap(p)
            if d is None or not del_mando(d, mando):
                continue
            paquetes.append((time.time(), p))
            tramas.append(d)
            k = tipo(d)
            if k not in visto:
                print(f"  {time.strftime('%H:%M:%S')} primera «{k}»", flush=True)
            visto[k] = visto.get(k, 0) + 1
        s.close()
    finally:
        for o in (["ip", "link", "set", a.iface, "down"], ["iw", "dev", a.iface, "set", "type", "managed"],
                  ["ip", "link", "set", a.iface, "up"]):
            subprocess.run(o, check=False)
    os.makedirs(os.path.dirname(a.pcap), exist_ok=True)
    with open(a.pcap, "wb") as f:
        f.write(pcap_bytes(paquetes))
    if uid >= 0:
        os.chown(a.pcap, uid, gid)
    print(f"\nTramas del mando: {len(tramas)}  {resumen(tramas)}\nGuardadas en {a.pcap}")
    avisos = [d for d in tramas if tipo(d).startswith("aviso")]
    if avisos:
        print(f"Primer aviso ({len(avisos[0])} B): {avisos[0].hex()}")
    if wowl and avisos:
        # Con o sin los 4 últimos bytes: en monitor algunos drivers dejan la FCS al final
        for etiqueta, d in (("", avisos[0]), (", sin FCS", avisos[0][:-4])):
            r = drc_wowl.descifrar(d, wowl[0])
            if r:
                print(f"Aviso DESCIFRADO con la clave del mando ({r[0]}{etiqueta}): cuerpo de {len(r[1])} B {r[1].hex()}")
                break
        else:
            print("El aviso NO se descifra con la clave del mando en ninguna variante.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
