#!/usr/bin/env python3
"""
drc_cam_v4l2.py - Receptor y alimentador V4L2 de la cámara del Wii U GamePad.
Reensambla los paquetes UDP (50020) y los emite en el dispositivo virtual loopback.
"""

import os
import struct
from typing import Optional, List, Tuple

class CameraFrameAssembler:
    """
    Reensambla paquetes de vídeo de la cámara del GamePad (puerto UDP 50020).
    Cada paquete lleva cabecera VSTRM:
      Byte 0: 0xF0 | (seq >> 8)
      Byte 1: seq & 0xFF
      Byte 2: flags (0x08 has timestamp)
      Byte 3: reservado
      Bytes 4..7: timestamp (32-bit big-endian)
    Los paquetes con el mismo timestamp forman un fotograma.
    """
    def __init__(self):
        self.current_ts: Optional[int] = None
        self.chunks: List[Tuple[int, bytes]] = []

    def parse_header(self, data: bytes) -> Optional[Tuple[int, int, bytes]]:
        if len(data) < 8 or (data[0] & 0xF0) != 0xF0:
            return None
        seq = ((data[0] & 0x03) << 8) | data[1]
        ts = struct.unpack("!I", data[4:8])[0]
        offset = 8
        if len(data) >= 16 and (data[14:16] == b"\x00\x00" or (data[8] & 0x80)):
            offset = 16
        payload = data[offset:]
        return seq, ts, payload

    def push_packet(self, data: bytes) -> Optional[bytes]:
        parsed = self.parse_header(data)
        if not parsed:
            return None
        seq, ts, payload = parsed

        frame_to_return = None
        if self.current_ts is not None and ts != self.current_ts:
            if self.chunks:
                self.chunks.sort(key=lambda item: item[0])
                frame_to_return = b"".join(chunk for _, chunk in self.chunks)
            self.chunks = []

        self.current_ts = ts
        self.chunks.append((seq, payload))
        return frame_to_return

    def flush(self) -> Optional[bytes]:
        """Cierra y entrega cualquier fotograma pendiente en el búfer."""
        if not self.chunks:
            return None
        self.chunks.sort(key=lambda item: item[0])
        frame = b"".join(chunk for _, chunk in self.chunks)
        self.chunks = []
        self.current_ts = None
        return frame

SC = b"\0\0\0\1"
# SPS del vídeo de la consola (854x480) con el ancho cambiado a 640 (40 MB); mismo PPS (capture-analysis/camdec.py en wiiu-gamepad-research).
SPS_640 = bytes.fromhex("67640020ac2b40501ec8")
PPS = bytes([0x68, 0xee, 0x06, 0x0c, 0xe8])


class CamH264Builder:
    """Flujo de la cámara (vstrm, cabecera de 16 B) -> H.264 Annex B decodificable, cuadro a cuadro."""

    def __init__(self):
        self.cur = None
        self.fn = 0
        self.started = False

    def push(self, d: bytes) -> Optional[bytes]:
        if len(d) < 16:
            return None
        w = struct.unpack(">I", d[:4])[0]
        if (w >> 28) != 0xF:
            return None
        seq = (w >> 16) & 0x3FF
        begin, end = (w >> 14) & 1, (w >> 12) & 1
        if begin:
            self.cur = dict(seq0=seq, parts={}, idr=0x80 in d[8:16])
        if self.cur is None:
            return None
        self.cur["parts"][seq] = d[16:16 + (w & 0x7FF)]
        if not end:
            return None
        cur, self.cur = self.cur, None
        n = (seq - cur["seq0"]) % 1024 + 1
        if any(((cur["seq0"] + i) % 1024) not in cur["parts"] for i in range(n)):
            return None
        data = b"".join(cur["parts"][(cur["seq0"] + i) % 1024] for i in range(n))
        if cur["idr"]:
            self.fn, self.started = 0, True
        elif self.started:
            self.fn = (self.fn + 1) & 0xFF
        if not self.started:
            return None
        out = bytearray()
        if cur["idr"]:
            out += SC + SPS_640 + SC + PPS
        sh = 0x25b804ff if cur["idr"] else (0x21e003ff | (self.fn << 13))
        out += SC + struct.pack(">I", sh) + data[:2]
        for b in data[2:]:
            if b <= 3 and out[-1] == 0 and out[-2] == 0:
                out.append(3)
            out.append(b)
        return bytes(out)


HARDWARE_VIDEO_DEVICES = {f"/dev/video{i}" for i in range(10, 32)}

def is_safe_v4l2_device(dev_path: str) -> bool:
    """Evita sobreescribir los dispositivos hardware bcm2835 (video10..31 en Raspberry Pi)."""
    canon = os.path.realpath(dev_path)
    base = os.path.basename(canon)
    if canon in HARDWARE_VIDEO_DEVICES or base in {f"video{i}" for i in range(10, 32)}:
        return False
    return True

def format_cam_ctl(command: str, *args) -> str:
    """Construye y valida líneas para /tmp/drc_ctl referentes a la cámara."""
    cmd = command.strip().lower()
    if cmd in ("on", "1"):
        return "cam on"
    if cmd in ("off", "0"):
        return "cam off"
    if cmd == "exposure":
        if not args:
            raise ValueError("cam exposure requiere 'auto' o un valor numérico")
        val = str(args[0]).lower()
        if val == "auto":
            return "cam exposure auto"
        ival = int(val)
        if not (0 <= ival <= 65535):
            raise ValueError(f"exposición fuera de rango (0..65535): {ival}")
        return f"cam exposure {ival}"
    if cmd == "gain":
        if not args:
            raise ValueError("cam gain requiere 'auto' o un valor numérico")
        val = str(args[0]).lower()
        if val == "auto":
            return "cam gain auto"
        ival = int(val)
        if not (0 <= ival <= 111):
            raise ValueError(f"ganancia fuera de rango (0..111): {ival}")
        return f"cam gain {ival}"
    if cmd == "wb":
        if not args:
            raise ValueError("cam wb requiere 'auto' o dos valores (azul rojo)")
        val = str(args[0]).lower()
        if val == "auto":
            return "cam wb auto"
        if len(args) < 2:
            raise ValueError("cam wb manual requiere ganancias azul y rojo (0..255)")
        b, r = int(args[0]), int(args[1])
        if not (0 <= b <= 255 and 0 <= r <= 255):
            raise ValueError("ganancias WB fuera de rango (0..255)")
        return f"cam wb {b} {r}"
    if cmd == "brightness":
        if not args: raise ValueError("cam brightness requiere un valor (-100..100)")
        ival = int(args[0])
        if not (-100 <= ival <= 100):
            raise ValueError(f"brillo fuera de rango (-100..100): {ival}")
        return f"cam brightness {ival}"
    if cmd == "contrast":
        if not args: raise ValueError("cam contrast requiere un valor (0..100)")
        ival = int(args[0])
        if not (0 <= ival <= 100):
            raise ValueError(f"contraste fuera de rango (0..100): {ival}")
        return f"cam contrast {ival}"
    if cmd == "saturation":
        if not args: raise ValueError("cam saturation requiere un valor (0..128)")
        ival = int(args[0])
        if not (0 <= ival <= 128):
            raise ValueError(f"saturación fuera de rango (0..128): {ival}")
        return f"cam saturation {ival}"
    if cmd == "bitrate":
        if not args: raise ValueError("cam bitrate requiere una tasa (100..65535)")
        ival = int(args[0])
        if not (100 <= ival <= 65535):
            raise ValueError(f"bitrate fuera de rango (100..65535): {ival}")
        return f"cam bitrate {ival}"
    raise ValueError(f"Comando de cámara desconocido: {cmd}")

V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
VIDIOC_S_FMT = 0xc0cc5605

def v4l2_fourcc(cc: str) -> int:
    b = cc.encode("ascii")[:4].ljust(4, b" ")
    return struct.unpack("<I", b)[0]

class V4L2LoopbackWriter:
    """
    Controlador para escribir fotogramas de vídeo en un dispositivo v4l2loopback
    (ej. /dev/video0 o /dev/video42) como fuente de cámara web virtual en Linux.
    """
    def __init__(self, dev_path: str = "/dev/video0", width: int = 640, height: int = 480, pixelformat: str = "H264"):
        if not is_safe_v4l2_device(dev_path):
            raise ValueError(f"{dev_path} es un dispositivo códec/ISP reservado por hardware de la Pi (video10..31)")
        self.dev_path = dev_path
        self.width = width
        self.height = height
        self.pixelformat = pixelformat
        self.fd: Optional[int] = None

    def build_format_struct(self) -> bytes:
        fourcc_int = v4l2_fourcc(self.pixelformat)
        pix_part = struct.pack(
            "<IIIIIIIIIIII",
            self.width,
            self.height,
            fourcc_int,
            0,  # field V4L2_FIELD_NONE
            0,  # bytesperline
            0,  # sizeimage
            1,  # colorspace V4L2_COLORSPACE_SRGB
            0, 0, 0, 0, 0
        )
        fmt = struct.pack("<I", V4L2_BUF_TYPE_VIDEO_OUTPUT) + pix_part + (b"\x00" * (200 - len(pix_part)))
        return fmt

    def open(self):
        import fcntl
        self.fd = os.open(self.dev_path, os.O_RDWR)
        fmt = self.build_format_struct()
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)

    def write_frame(self, frame: bytes) -> int:
        if self.fd is None:
            raise RuntimeError("Dispositivo V4L2 no abierto")
        return os.write(self.fd, frame)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

def run_camera_service(iface: str = "wlan0", v4l2_dev: Optional[str] = None, stop_event=None,
                       control: bool = True):
    """
    Escucha (AF_PACKET, sin competir con el socket 50020 de drc_stream) los paquetes mando:50120 -> 50020,
    rehace el H.264 (CamH264Builder) y lo saca por stdout o, con v4l2_dev, decodificado (ffmpeg, yuv420p)
    hacia un dispositivo v4l2loopback ya creado. ¡En la Pi /dev/video10..20 son el códec/ISP por hardware!
    """
    import socket
    import subprocess
    import sys
    sink, ff = None, None
    if v4l2_dev:
        if not is_safe_v4l2_device(v4l2_dev):
            sys.exit(f"[drc_cam_v4l2] Error: {v4l2_dev} es un dispositivo códec/ISP hardware reservado (video10..31). Usa un número libre como video0 o video42.")
        if not os.path.exists(v4l2_dev):
            sys.exit(f"[drc_cam_v4l2] {v4l2_dev} no existe (cargar v4l2loopback con video_nr libre)")
        ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-f", "h264", "-i", "-",
                               "-pix_fmt", "yuv420p", "-f", "v4l2", v4l2_dev], stdin=subprocess.PIPE)
        sink = ff.stdin
    else:
        sink = sys.stdout.buffer
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0800))
    s.bind((iface, 0))
    s.settimeout(0.5)
    if control:
        with open("/tmp/drc_ctl", "w") as f:
            f.write("set camera 1\n")
    builder = CamH264Builder()
    frames = 0
    print(f"[drc_cam_v4l2] cámara en {iface} -> {v4l2_dev or 'stdout'}", file=sys.stderr, flush=True)
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                pkt = s.recv(4096)
            except socket.timeout:
                continue
            if len(pkt) < 42 or pkt[12:14] != b"\x08\x00" or pkt[23] != 17:
                continue
            ihl = (pkt[14] & 15) * 4
            sport, dport = struct.unpack("!HH", pkt[14 + ihl:18 + ihl])
            if sport != 50120 or dport != 50020:
                continue
            frame = builder.push(pkt[22 + ihl:])
            if frame:
                sink.write(frame)
                sink.flush()
                frames += 1
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if control:
            with open("/tmp/drc_ctl", "w") as f:
                f.write("set camera 0\n")
        s.close()
        if ff:
            ff.stdin.close()
            ff.wait()
        print(f"[drc_cam_v4l2] {frames} cuadros", file=sys.stderr, flush=True)


def _conf_iface() -> str:
    """Interfaz Wi-Fi del GamePad: WIFI_IFACE de drc.conf (o wlan0)."""
    try:
        from drc_ap import load_drc_conf
        return load_drc_conf().get("WIFI_IFACE", "wlan0")
    except Exception:
        return "wlan0"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Cámara del Wii U GamePad (necesita root y drc activo)")
    ap.add_argument("--iface", default=_conf_iface())
    ap.add_argument("--v4l2", help="dispositivo v4l2loopback de salida (p. ej. /dev/video42); sin él, H.264 a stdout")
    ap.add_argument("--no-control", action="store_true", help="no enviar set camera 1/0 por /tmp/drc_ctl")
    a = ap.parse_args()
    run_camera_service(a.iface, a.v4l2, control=not a.no_control)
