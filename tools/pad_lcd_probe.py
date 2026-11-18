#!/usr/bin/env python3
"""Sonda para averiguar cómo se reenciende la pantalla del GamePad tras `set lcd_on 0`. Medido: con 0x01, `resync`,
`backlight` y `audio_on` no vuelve. El firmware (LVC 0x36e82) dice que 0x00/0x01/0x02 son modos de apagado y solo 0xff
enciende; por eso `set lcd_on 1` manda ya 0xff y la variante 1 es `raw 5 8 ff`. Comprobado en el mando (2026-11-16): con
0xff la pantalla vuelve. La sonda queda para mandos o firmwares distintos.
Con la transmisión en marcha, prueba variantes una a una, apaga la pantalla
antes de cada una y espera `--pausa` segundos para que mires el mando: anota el número de la variante que la enciende.

    python3 tools/pad_lcd_probe.py [--pausa 6] [--solo 3,5]

Además imprime la respuesta del mando a `get audio_mute` y `get state` antes y después de cada variante (el firmware
guarda ahí el estado del silencio: -1 = encendido, 0 = apagado). No necesita root: escribe en /tmp/drc_ctl.
"""
import argparse
import os
import sys
import time

OUT, CTL = "/tmp/drc_ctl.out", "/tmp/drc_ctl"


def enviar(cmd, espera=0.8):
    n = os.path.getsize(OUT) if os.path.exists(OUT) else 0
    with open(CTL, "w") as f:
        f.write(cmd + "\n")
    time.sleep(espera)
    with open(OUT, "rb") as f:
        f.seek(n)
        return f.read().decode(errors="replace").strip()


def estado():
    mute = enviar("get audio_mute").split()
    return mute[-1] if mute else "?"


VARIANTES = [
    ("raw 5 8 ff (el único valor de encendido según el firmware)", ["raw 5 8 ff"]),
    ("lcd_on 1 solo (= 0xff)", ["set lcd_on 1"]),
    ("lcd_on 1 dos veces", ["set lcd_on 1", "sleep=1", "set lcd_on 1"]),
    ("audio_on 1 y luego lcd_on 1 (orden de la consola al apagar: lcd, audio)", ["set audio_on 1", "set lcd_on 1"]),
    ("lcd_on 1 y backlight 1..5", ["set lcd_on 1"] + [f"set backlight {n}" for n in (1, 2, 3, 4, 5, 3)]),
    ("lcd_on 1, resync y espera", ["set lcd_on 1", "resync", "sleep=2", "resync"]),
    ("lcd_on 0 otra vez y 1 tras 10 s", ["set lcd_on 0", "sleep=10", "set lcd_on 1"]),
    ("sensorbar y vibración (algo que despierte al mando)", ["set lcd_on 1", "vibrate 150", "set sensorbar 1"]),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pausa", type=float, default=6.0)
    ap.add_argument("--solo", default="")
    a = ap.parse_args()
    if not os.path.exists(CTL):
        sys.exit("No hay /tmp/drc_ctl: arranca antes la transmisión (python3 drchost/drc_ctl.py start --mode separate)")
    solo = {int(x) for x in a.solo.split(",") if x.strip()}
    for i, (nombre, cmds) in enumerate(VARIANTES, 1):
        if solo and i not in solo:
            continue
        print(f"\n=== Variante {i}: {nombre}", flush=True)
        enviar("set lcd_on 0")
        time.sleep(2)
        print(f"  tras apagar: audio_mute={estado()}", flush=True)
        for c in cmds:
            if c.startswith("sleep="):
                time.sleep(float(c[6:]))
                continue
            print(f"  {time.strftime('%H:%M:%S')} {c}", flush=True)
            enviar(c, 0.5)
        print(f"  tras la variante: audio_mute={estado()}  ¿se ve la pantalla? (mira {a.pausa:g} s)", flush=True)
        time.sleep(a.pausa)
    print("\nHecho. Si ninguna la encendió, reinicia la transmisión: python3 drchost/drc_ctl.py stop && ... start")


if __name__ == "__main__":
    main()
