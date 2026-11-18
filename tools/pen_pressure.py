#!/usr/bin/env python3
"""Mide la presión del táctil del GamePad (en crudo, 12 bits) para calibrar el modo lápiz.

Se ejecuta con root mientras `drc_stream` transmite: lee los paquetes de entrada del mando (UDP 50122 -> 50022) en
la interfaz del GamePad. Va pidiendo fases (lápiz suave, lápiz fuerte, dedo...), muestra la presión en directo y al
final resume cada fase (mínimo, mediana, máximo). Guarda todas las muestras en un CSV.

Uso:  sudo python3 tools/pen_pressure.py [--iface IF] [--segundos 6] [--csv ~/drc_capturas/presion.csv]
"""
import argparse
import csv
import os
import socket
import statistics
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "drchost"))
from drchost import padtest as pt  # noqa: E402

FASES = [
    ("sin tocar", "No toques la pantalla"),
    ("lapiz suave", "Lápiz: apoya MUY suave, casi sin presión, y muévelo despacio"),
    ("lapiz medio", "Lápiz: presión normal, como al escribir"),
    ("lapiz fuerte", "Lápiz: aprieta fuerte (sin pasarte)"),
    ("dedo suave", "Dedo: apoya suave"),
    ("dedo fuerte", "Dedo: aprieta fuerte"),
    ("trazo", "Lápiz: haz un trazo largo variando la presión de suave a fuerte"),
    # Calibración: misma fuerza en toda la pantalla, para separar la presión de la posición (panel resistivo).
    ("barrido suave", "Lápiz SUAVE y constante: recorre toda la pantalla (izquierda-derecha y arriba-abajo)"),
    ("barrido fuerte", "Lápiz FUERTE y constante: recorre toda la pantalla (izquierda-derecha y arriba-abajo)"),
]


def _resolver(m, b):
    """Mínimos cuadrados por ecuaciones normales (sin numpy): devuelve c con m·c ≈ b."""
    n = len(m[0])
    a = [[sum(r[i] * r[j] for r in m) for j in range(n)] + [sum(r[i] * y for r, y in zip(m, b))] for i in range(n)]
    for i in range(n):
        piv = max(range(i, n), key=lambda k: abs(a[k][i]))
        a[i], a[piv] = a[piv], a[i]
        d = a[i][i]
        if abs(d) < 1e-12:
            raise ValueError("datos insuficientes")
        a[i] = [v / d for v in a[i]]
        for k in range(n):
            if k != i:
                f = a[k][i]
                a[k] = [vk - f * vi for vk, vi in zip(a[k], a[i])]
    return [a[i][n] for i in range(n)]


TERMINOS = ("kx", "ky", "kxx", "kyy", "kxy")


def _terminos(x, y):
    return [x, y, x * x, y * y, x * y]


def calibrar(suave, fuerte, leve=None, duro=None):
    """suave/fuerte: listas de (x, y, presión) a fuerza constante recorriendo la pantalla. Ajusta presión = nivel +
    kx·x + ky·y + kxx·x² + kyy·y² + kxy·x·y (un nivel por fuerza, la misma forma: con una recta el centro quedaba
    alto) y devuelve los k y el rango corregido: p25 de los toques más suaves (leve) y p75 de los más fuertes (duro)
    si los hay; si no, p5 del barrido suave y p95 del fuerte."""
    suave = [t for t in suave if t[2] > 0]
    fuerte = [t for t in fuerte if t[2] > 0]
    s = 1e-3   # x, y en miles: las ecuaciones normales con x⁴ ~ 1e14 pierden precisión
    m = [[1, 0] + _terminos(x * s, y * s) for x, y, _ in suave] + [[0, 1] + _terminos(x * s, y * s) for x, y, _ in fuerte]
    b = [p for _, _, p in suave] + [p for _, _, p in fuerte]
    sol = _resolver(m, b)[2:]
    k = dict(zip(TERMINOS, (sol[0] * s, sol[1] * s, sol[2] * s * s, sol[3] * s * s, sol[4] * s * s)))

    def corr(ts):
        return sorted(p - sum(ki * ti for ki, ti in zip(k.values(), _terminos(x, y))) for x, y, p in ts if p > 0)

    def pct(v, q):
        return v[int(q * (len(v) - 1))]

    lo = corr(leve) if leve else []
    hi = corr(duro) if duro else []
    rmin = pct(lo, 0.25) if lo else pct(corr(suave), 0.05)
    rmax = pct(hi, 0.75) if hi else pct(corr(fuerte), 0.95)
    return dict(k, min=rmin, max=rmax)


def resumen(valores):
    """(n, mínimo, mediana, máximo) de las muestras con presión (> 0)."""
    v = [x for x in valores if x > 0]
    if not v:
        return (0, None, None, None)
    return (len(v), min(v), int(statistics.median(v)), max(v))


def barra(p, ancho=40):
    n = min(ancho, p * ancho // 4095)
    return "#" * n + "." * (ancho - n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iface", default=None, help="interfaz del GamePad (por defecto WIFI_IFACE de drc.conf)")
    ap.add_argument("--segundos", type=float, default=6.0, help="duración de cada fase")
    ap.add_argument("--csv", default=os.path.expanduser(f"~{os.environ.get('SUDO_USER', '')}/drc_capturas/presion.csv"))
    a = ap.parse_args(argv)
    iface = a.iface
    if not iface:
        import drc_ap
        iface = drc_ap.load_drc_conf().get("WIFI_IFACE", "wlan0")
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0800))
        s.bind((iface, 0))
        s.settimeout(0.2)
    except OSError as e:
        sys.exit(f"No se puede escuchar en {iface}: {e} (hace falta sudo y una sesión transmitiendo)")
    os.makedirs(os.path.dirname(a.csv), exist_ok=True)
    muestras = {nombre: [] for nombre, _ in FASES}
    puntos = {nombre: [] for nombre, _ in FASES}
    with open(a.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "fase", "x", "y", "presion"])
        t0 = time.time()
        for nombre, texto in FASES:
            print(f"\n>>> {texto} ({a.segundos:.0f} s)")
            for i in (3, 2, 1):
                print(f"    empieza en {i}...", end="\r", flush=True)
                time.sleep(1)
            fin = time.time() + a.segundos
            ultimo = 0.0
            while time.time() < fin:
                try:
                    pkt = s.recv(2048)
                except socket.timeout:
                    continue
                if len(pkt) < 42 or pkt[23] != 17 or struct.unpack("!HH", pkt[34:38]) != (50122, 50022):
                    continue
                h = pt.parse_hid(pkt[42:])
                if not h or "touch_raw" not in h:
                    continue
                t = h["touch_raw"]
                muestras[nombre].append(t["pressure"])
                puntos[nombre].append((t["x"], t["y"], t["pressure"]))
                w.writerow([f"{time.time() - t0:.3f}", nombre, t["x"], t["y"], t["pressure"]])
                if time.time() - ultimo > 0.1:
                    ultimo = time.time()
                    print(f"    presión {t['pressure']:4d} {barra(t['pressure'])}  x={t['x']:4d} y={t['y']:4d}",
                          end="\r", flush=True)
            print(" " * 78, end="\r")
    print("\nResumen (solo muestras con presión > 0):")
    print(f"  {'fase':<14}{'muestras':>9}{'mín':>7}{'mediana':>9}{'máx':>7}")
    for nombre, _ in FASES:
        n, lo, med, hi = resumen(muestras[nombre])
        print(f"  {nombre:<14}{n:>9}{'-' if lo is None else lo:>7}{'-' if med is None else med:>9}"
              f"{'-' if hi is None else hi:>7}")
    try:
        c = calibrar(puntos["barrido suave"], puntos["barrido fuerte"], puntos["lapiz suave"], puntos["lapiz fuerte"])
        print("\nCorrección por posición: presión = crudo − (kx·x + ky·y + kxx·x² + kyy·y² + kxy·x·y)")
        print(f"Rango del lápiz corregido: {c['min']:.0f} (suave) … {c['max']:.0f} (fuerte)")
        print("Valores propuestos para drc.conf:")
        for n in TERMINOS:
            print(f"  DRC_PEN_PRESSURE_{n.upper()}={c[n]:.4g}")
        print(f"  DRC_PEN_PRESSURE_MIN={c['min']:.0f}\n  DRC_PEN_PRESSURE_MAX={c['max']:.0f}")
    except (ValueError, ZeroDivisionError, IndexError) as e:
        print(f"\nSin calibración ({e}): repite los barridos tocando toda la pantalla.")
    print(f"\nMuestras guardadas en {a.csv}")


if __name__ == "__main__":
    main()
