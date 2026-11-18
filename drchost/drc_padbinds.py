#!/usr/bin/env python3
"""Atajos con los botones del GamePad para la sesión del mando (p. ej. HOME+X cierra la ventana activa).

Lee el dispositivo evdev «Wii U GamePad» que crea drc_stream SIN tomarlo en exclusiva (los juegos siguen
viendo el mando) y ejecuta una orden cuando se pulsa una combinación.

Uso:  drc_padbinds.py "HOME+X=<orden de shell>" ["HOME+Y=<otra orden>" ...]
La última tecla de la combinación la dispara; las anteriores deben estar mantenidas.
"""
import glob
import os
import struct
import subprocess
import sys
import time

DEVICE_NAME = "Wii U GamePad"
EV_KEY = 1
EVENT = struct.Struct("llHHi")   # struct input_event en 64 bits
# Los mismos códigos que emite drc_stream (uinput_map.h)
BUTTONS = {
    "A": 0x131, "B": 0x130, "X": 0x133, "Y": 0x134,
    "L": 0x136, "R": 0x137, "ZL": 0x138, "ZR": 0x139,
    "MINUS": 0x13A, "PLUS": 0x13B, "HOME": 0x13C, "L3": 0x13D, "R3": 0x13E,
}


def parse_bind(text):
    """"HOME+X=orden" -> ((códigos mantenidos...), código disparador, orden)."""
    combo, sep, cmd = text.partition("=")
    names = [n.strip().upper() for n in combo.split("+") if n.strip()]
    if not sep or not cmd.strip() or not names:
        raise ValueError(f"atajo mal escrito: {text!r} (se espera BOTON+BOTON=orden)")
    unknown = [n for n in names if n not in BUTTONS]
    if unknown:
        raise ValueError(f"botón desconocido {unknown[0]!r}; valen: {', '.join(BUTTONS)}")
    codes = [BUTTONS[n] for n in names]
    return tuple(codes[:-1]), codes[-1], cmd.strip()


class Combos:
    """Estado de botones mantenidos; `key()` devuelve las órdenes que dispara cada evento."""

    def __init__(self, binds):
        self.binds = binds
        self.held = set()

    def key(self, code, value):
        fired = []
        if value == 1:
            fired = [cmd for mods, trigger, cmd in self.binds
                     if trigger == code and all(m in self.held for m in mods)]
            self.held.add(code)
        elif value == 0:
            self.held.discard(code)
        return fired


def find_device(name=DEVICE_NAME):
    for sysname in glob.glob("/sys/class/input/event*/device/name"):
        try:
            if open(sysname).read().strip() == name:
                return "/dev/input/" + sysname.split("/")[4]
        except OSError:
            pass
    return None


def main(argv):
    try:
        binds = [parse_bind(a) for a in argv]
    except ValueError as e:
        print(f"drc_padbinds: {e}", file=sys.stderr)
        return 2
    if not binds:
        print(__doc__, file=sys.stderr)
        return 2
    while True:
        path = find_device()
        if not path:
            time.sleep(0.5)      # drc_stream lo crea al conectar el mando
            continue
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            time.sleep(0.5)      # la regla de permisos aún no se ha aplicado
            continue
        print(f"drc_padbinds: {len(binds)} atajo(s) sobre {path}", file=sys.stderr, flush=True)
        combos = Combos(binds)
        try:
            while True:
                data = os.read(fd, EVENT.size)
                if len(data) < EVENT.size:
                    break
                _, _, etype, code, value = EVENT.unpack(data)
                if etype != EV_KEY:
                    continue
                for cmd in combos.key(code, value):
                    subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL)
        except OSError:
            pass                 # dispositivo retirado: se vuelve a buscar
        finally:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
