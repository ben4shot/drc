"""Medidor de audio hacia el GamePad (diagnóstico B09/B16).

En la Pi: sudo python3 audio_probe.py <etiqueta>. Toca 4 s de seno de 440 Hz como el usuario del escritorio por la
salida por defecto y captura en wlan0 los astrm (puerto 50121, reensamblando fragmentos IP).
Informa paquetes, saltos de secuencia, discontinuidades de la onda, ceros e intervalo máximo.
Si sale limpio y en el GamePad suena mal, el problema está del lado del GamePad.
"""
import math, socket, struct, subprocess, sys, time, threading
label = sys.argv[1]
N = 48000 * 4
w = 2 * math.pi * 440 / 48000
raw = b"".join(struct.pack("<hh", int(8000 * math.sin(w * i)), int(8000 * math.sin(w * i))) for i in range(N))
open("/tmp/tone440.raw", "wb").write(raw)
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
s.bind(("wlan0", 0)); s.settimeout(0.2)
pkts = []
frags = {}
def sniff():
    t_end = time.time() + 6
    while time.time() < t_end:
        try:
            d = s.recv(4096)
        except socket.timeout:
            continue
        if d[12:14] != b"\x08\x00":
            continue
        ip = d[14:]
        if len(ip) < 28 or ip[9] != 17 or socket.inet_ntoa(ip[16:20]) != "192.168.1.11":
            continue
        ihl = (ip[0] & 15) * 4
        tot = struct.unpack(">H", ip[2:4])[0]
        ident = struct.unpack(">H", ip[4:6])[0]
        fl = struct.unpack(">H", ip[6:8])[0]
        off, mf = (fl & 0x1FFF) * 8, fl & 0x2000
        frag = frags.setdefault(ident, {})
        frag[off] = ip[ihl:tot]
        if not mf:
            parts = b"".join(frag[k] for k in sorted(frag))
            del frags[ident]
            if len(parts) >= 8 and struct.unpack(">H", parts[2:4])[0] == 50121:
                pkts.append((time.time(), parts[8:]))
th = threading.Thread(target=sniff); th.start()
time.sleep(0.5)
r = subprocess.run(["sudo", "-u", __import__("os").environ.get("DRC_DESKTOP_USER") or __import__("os").environ.get("SUDO_USER") or "root", "aplay", "-q", "-f", "S16_LE", "-r", "48000", "-c", "2", "-t", "raw", "/tmp/tone440.raw"], capture_output=True, text=True)
th.join()
data = [(t, p) for t, p in pkts if len(p) > 40]
seqs = [((p[0] << 8 | p[1]) & 0x3FF) for _, p in data]
jumps = sum(1 for a, b in zip(seqs, seqs[1:]) if (b - a) & 0x3FF != 1)
L = []
for _, p in data:
    L += [struct.unpack_from("<h", p, 8 + 4 * k)[0] for k in range(0, (len(p) - 8) // 4)]
c = 2 * math.cos(w)
bad = [i for i in range(2, len(L)) if abs(L[i] - (c * L[i - 1] - L[i - 2])) > 400 and (L[i] or L[i - 1])]
zeros = sum(1 for v in L if v == 0)
iv = [b - a for (a, _), (b, _) in zip(data, data[1:])]
print(f"[{label}] aplay rc={r.returncode} {r.stderr.strip()[:80]}")
print(f"[{label}] paquetes audio={len(data)} (4 s de tono) saltos_seq={jumps} muestras={len(L)} discontinuidades={len(bad)} ceros={zeros} intervalo_max={max(iv)*1000 if iv else 0:.1f}ms")
print(f"[{label}] primeras discontinuidades (s): {[round(i/48000,3) for i in bad[:10]]}")
