#!/usr/bin/env python3
"""Métricas de las tormentas de resync a partir del log de libdrc (DRC_RESYNC_LOG=1).

Lee líneas `[libdrc] resync tick=N since_idr_tick=S ...` y calcula ráfagas por minuto, IDR por ráfaga y porcentaje
de tiempo en resync. Una ráfaga termina tras 20 ticks sin resync.
Uso: rafagas.py LOG [--seconds S | --ticks N]   (sin duración: del primer al último resync)
"""
import argparse
import re
import sys

TICK_HZ = 59.94
GAP_TICKS = 20
_RESYNC = re.compile(r"\[libdrc\] resync tick=(\d+) since_idr_tick=(\d+)")


def analyze(lines, total_ticks=None):
    ev = []
    for line in lines:
        m = _RESYNC.search(line)
        if m:
            ev.append((int(m.group(1)), int(m.group(2))))
    if not ev:
        return {"rafagas": 0, "rafagas_por_min": 0.0, "idr_por_rafaga": 0.0, "pct_resync": 0.0}
    bursts = [[ev[0]]]
    for e in ev[1:]:
        if e[0] - bursts[-1][-1][0] > GAP_TICKS:
            bursts.append([e])
        else:
            bursts[-1].append(e)
    if total_ticks is None:
        total_ticks = ev[-1][0] - ev[0][0] + 1
    covered = sum(b[-1][0] - b[0][0] + 1 for b in bursts)
    idrs = [len({t - s for t, s in b}) for b in bursts]
    minutes = total_ticks / TICK_HZ / 60
    return {
        "rafagas": len(bursts),
        "rafagas_por_min": len(bursts) / minutes,
        "idr_por_rafaga": sum(idrs) / len(bursts),
        "pct_resync": covered / total_ticks * 100,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default="-")
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--ticks", type=float)
    a = ap.parse_args(argv)
    lines = sys.stdin if a.log == "-" else open(a.log, errors="replace")
    total = a.ticks if a.ticks else (a.seconds * TICK_HZ if a.seconds else None)
    r = analyze(lines, total)
    print("rafagas=%d rafagas_por_min=%.2f idr_por_rafaga=%.2f pct_resync=%.1f" %
          (r["rafagas"], r["rafagas_por_min"], r["idr_por_rafaga"], r["pct_resync"]))


if __name__ == "__main__":
    main()
