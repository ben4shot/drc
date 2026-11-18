#!/usr/bin/env python3
"""Inyecta fallos con /tmp/drc_kick y mide la ráfaga de resync que provocan (ver kick.h e INDICE.md).

Uso: python3 kick_test.py [--cap 90] [--reps 3] [--out f] "etiqueta|kick[|segundos:kick2]" ...
Ejemplo: "fase5ms_s6|ts 5000 seq 1" "reancla|ts 5000 seq 1|6:reanchor"
"""
import argparse
import re
import subprocess
import threading
import time

KICK = "/tmp/drc_kick"
LINE = re.compile(r"frames=(\d+) idr=(\d+) resync=(\d+)")


def parse_item(item):
    parts = item.split("|")
    later = []
    for p in parts[2:]:
        sec, text = p.split(":", 1)
        later.append((float(sec), text))
    return parts[0], parts[1], later


def write_kick(text):
    with open(KICK, "w") as f:
        f.write(text + "\n")


class Stats:
    """Sigue el journal y guarda (instante, idr, resync) de cada segundo."""

    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()
        p = subprocess.Popen(["journalctl", "-u", "drc", "-f", "-n", "0", "-o", "cat"],
                             stdout=subprocess.PIPE, text=True)
        threading.Thread(target=self._run, args=(p,), daemon=True).start()

    def _run(self, p):
        for line in p.stdout:
            m = LINE.search(line)
            if m:
                with self.lock:
                    self.rows.append((time.time(), int(m.group(2)), int(m.group(3))))

    def since(self, t):
        with self.lock:
            return [r for r in self.rows if r[0] >= t]


def wait_quiet(stats, seconds, limit):
    t0 = time.time()
    while time.time() - t0 < limit:
        rows = stats.since(time.time() - seconds)
        if len(rows) >= seconds - 1 and not any(r[2] for r in rows):
            return True
        time.sleep(1)
    return False


def trial(stats, kick, later, cap):
    t0 = time.time()
    write_kick(kick)
    pending = sorted(later)
    while time.time() - t0 < cap:
        el = time.time() - t0
        while pending and el >= pending[0][0]:
            write_kick(pending.pop(0)[1])
        rows = stats.since(t0)
        hits = [r for r in rows if r[2] > 0]
        # terminado: hubo resync y lleva 6 s limpio, o 12 s sin ninguno
        if hits and time.time() - hits[-1][0] > 6 and not pending:
            break
        if not hits and el > 12 and not pending:
            break
        time.sleep(0.5)
    rows = stats.since(t0)
    hits = [r for r in rows if r[2] > 0]
    dur = hits[-1][0] - hits[0][0] + 1 if hits else 0.0
    capped = time.time() - t0 >= cap
    return dur, sum(r[2] for r in rows), sum(r[1] for r in rows), capped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("items", nargs="+")
    ap.add_argument("--cap", type=float, default=90)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default="/tmp/kick_test.txt")
    a = ap.parse_args()
    stats = Stats()
    time.sleep(3)
    with open(a.out, "a", buffering=1) as out:
        for rep in range(a.reps):
            for item in a.items:
                label, kick, later = parse_item(item)
                if not wait_quiet(stats, 8, 200):
                    out.write(f"{label} rep={rep} NO_QUIET\n")
                    continue
                dur, rs, idr, capped = trial(stats, kick, later, a.cap)
                out.write(f"{label} rep={rep} dur={dur:.0f}s resync={rs} idr={idr}{' TOPE' if capped else ''}\n")
        out.write("FIN\n")


if __name__ == "__main__":
    main()
