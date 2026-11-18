#!/usr/bin/env python3
"""Patrón de barras de gris (luma exacta) en rawvideo 854x480 YUV420P por stdout, a 60 fps.

Sirve para averiguar el rango que espera el GamePad: si las barras <=16 se ven iguales
y las >=235 también, decodifica en rango limitado; si se distinguen todas, completo.
Uso: DRC_CAPTURE_CMD="python3 ~/gray_steps.py" ./start_drc.sh --mode separate
"""
import sys
import time

W, H = 854, 480
DARK = (0, 4, 8, 12, 16, 20, 24, 32)
LIGHT = (220, 228, 232, 235, 240, 245, 250, 255)


def make_frame() -> bytes:
    bar_w = W // len(DARK)

    def row(levels):
        r = bytearray()
        for i in range(W):
            r.append(levels[min(i // bar_w, len(levels) - 1)])
        return bytes(r)

    y = row(DARK) * (H // 2) + row(LIGHT) * (H - H // 2)
    return y + bytes([128]) * (W * H // 2)


def main() -> None:
    frame = make_frame()
    out = sys.stdout.buffer
    t = time.monotonic()
    while True:
        out.write(frame)
        out.flush()
        t += 1 / 60
        time.sleep(max(0.0, t - time.monotonic()))


if __name__ == "__main__":
    main()
