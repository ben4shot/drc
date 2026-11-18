#!/usr/bin/env python3
"""
drc_audio_router.py - Enruta dinámicamente el audio por ventana/escritorio.
Las aplicaciones en el escritorio del GamePad (por defecto workspace 10)
suenan por el GamePad (drc-gamepad); el resto suena por la salida del PC.
"""
import sys
import time
import json
import subprocess
from pathlib import Path

def get_ppid(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # el formato es: pid (comm) state ppid ...
        parts = stat.split(")")
        if len(parts) >= 2:
            fields = parts[1].split()
            return int(fields[1])
    except Exception:
        pass
    return None

def get_pid_ancestry(pid):
    ancestry = set()
    curr = pid
    while curr and curr > 1 and curr not in ancestry:
        ancestry.add(curr)
        curr = get_ppid(curr)
    return ancestry

def get_hypr_clients():
    try:
        out = subprocess.check_output(["hyprctl", "-j", "clients"], timeout=2).decode("utf-8", errors="replace")
        return json.loads(out)
    except Exception:
        return []

def get_sink_inputs():
    try:
        out = subprocess.check_output(["pactl", "-f", "json", "list", "sink-inputs"], timeout=2).decode("utf-8", errors="replace")
        return json.loads(out)
    except Exception:
        return []

def main():
    if len(sys.argv) < 3:
        print("Uso: drc_audio_router.py <pc_sink> <gamepad_sink> [gamepad_workspace]")
        sys.exit(1)

    pc_sink = sys.argv[1]
    gamepad_sink = sys.argv[2]
    gamepad_ws = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    # Estado previo de cada stream: {stream_id: sink_actual}
    tracked = {}

    while True:
        try:
            clients = get_hypr_clients()
            gamepad_pids = set()
            pc_pids = set()

            for c in clients:
                pid = c.get("pid")
                if not pid:
                    continue
                ws_id = c.get("workspace", {}).get("id")
                if ws_id == gamepad_ws:
                    gamepad_pids.add(pid)
                else:
                    pc_pids.add(pid)

            inputs = get_sink_inputs()
            for inp in inputs:
                stream_id = inp.get("index")
                if stream_id is None:
                    continue

                props = inp.get("properties", {})
                raw_pid = props.get("application.process.id")
                app_pid = int(raw_pid) if raw_pid else None

                target_sink = None
                if app_pid:
                    ancestry = get_pid_ancestry(app_pid)
                    if ancestry & gamepad_pids:
                        target_sink = gamepad_sink
                    elif ancestry & pc_pids:
                        target_sink = pc_sink

                if target_sink and tracked.get(stream_id) != target_sink:
                    # Mover solo si no está ya en el destino
                    current_sink = str(inp.get("sink"))
                    subprocess.run(
                        ["pactl", "move-sink-input", str(stream_id), target_sink],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    tracked[stream_id] = target_sink

            # Limpiar streams cerrados
            active_ids = {inp.get("index") for inp in inputs}
            for sid in list(tracked.keys()):
                if sid not in active_ids:
                    del tracked[sid]

        except Exception:
            pass

        time.sleep(0.5)

if __name__ == "__main__":
    main()
