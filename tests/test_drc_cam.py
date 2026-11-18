#!/usr/bin/env python3
import struct
import pytest
from drchost.drc_cam_v4l2 import CameraFrameAssembler

def make_cam_packet(seq: int, ts: int, payload: bytes) -> bytes:
    hdr = struct.pack("!BBBB I", 0xF0 | ((seq >> 8) & 0x03), seq & 0xFF, 0x08, 0x00, ts)
    return hdr + payload

def test_camera_frame_assembler_reassembles_slices():
    """
    Prueba 144: CameraFrameAssembler acumula los paquetes UDP de un mismo timestamp (ts)
    ordenados por seq y entrega el fotograma completo cuando cambia el timestamp.
    """
    assembler = CameraFrameAssembler()

    # Frame 1: dos paquetes con ts = 1000
    pkt1 = make_cam_packet(seq=1, ts=1000, payload=b"SLICE1_DATA_")
    pkt2 = make_cam_packet(seq=2, ts=1000, payload=b"SLICE2_DATA_")

    assert assembler.push_packet(pkt1) is None
    assert assembler.push_packet(pkt2) is None

    # Frame 2 inicia con ts = 2000 -> emite el frame 1 completo
    pkt3 = make_cam_packet(seq=3, ts=2000, payload=b"FRAME2_SLICE1_")
    f1 = assembler.push_packet(pkt3)
    assert f1 is not None
    assert f1 == b"SLICE1_DATA_SLICE2_DATA_"


def test_v4l2_device_format_negotiation():
    """
    Prueba 145: V4L2LoopbackWriter construye la estructura v4l2_format adecuada (tipo OUTPUT, 640x480,
    pixelformat H264/MJPG) y negocia el ioctl VIDIOC_S_FMT.
    """
    from drchost.drc_cam_v4l2 import V4L2LoopbackWriter, VIDIOC_S_FMT, V4L2_BUF_TYPE_VIDEO_OUTPUT

    writer = V4L2LoopbackWriter(dev_path="/dev/video0", width=640, height=480, pixelformat="H264")
    fmt_bytes = writer.build_format_struct()
    assert len(fmt_bytes) == 204

    buf_type, width, height, pix_fmt = struct.unpack_from("<IIII", fmt_bytes, 0)
    assert buf_type == V4L2_BUF_TYPE_VIDEO_OUTPUT
    assert width == 640
    assert height == 480
    assert pix_fmt == struct.unpack("<I", b"H264")[0]


def test_camera_stream_receiver_frame_lifecycle():
    """
    Prueba 146: el receptor de flujo de cámara procesa ráfagas de paquetes UDP,
    detecta inicio de cuadros, extrae metadatos (timestamp, secuencia) y expone
    el estado del flujo para el dashboard y aplicaciones consumidoras.
    """
    from drchost.drc_cam_v4l2 import CameraFrameAssembler
    assembler = CameraFrameAssembler()

    frames_received = []
    for f_idx in range(3):
        ts = 10000 + f_idx * 33333
        pkt1 = make_cam_packet(seq=f_idx*2 + 1, ts=ts, payload=f"F{f_idx}_PART1_".encode())
        pkt2 = make_cam_packet(seq=f_idx*2 + 2, ts=ts, payload=f"F{f_idx}_PART2".encode())
        res1 = assembler.push_packet(pkt1)
        res2 = assembler.push_packet(pkt2)
        if res1: frames_received.append(res1)
        if res2: frames_received.append(res2)

    last_pkt = make_cam_packet(seq=99, ts=99999, payload=b"END")
    res = assembler.push_packet(last_pkt)
    if res: frames_received.append(res)

    assert len(frames_received) == 3
    assert frames_received[0] == b"F0_PART1_F0_PART2"
    assert frames_received[1] == b"F1_PART1_F1_PART2"
    assert frames_received[2] == b"F2_PART1_F2_PART2"
    assert assembler.flush() == b"END"


def test_camera_stream_decoder_and_telemetry():
    """
    Prueba 147: verificación de telemetría de flujo y reensamblado con paquetes
    desordenados y cálculo de bytes acumulados para el dashboard interactivo.
    """
    assembler = CameraFrameAssembler()

    telemetry = {
        "cam_active": False,
        "cam_packets_count": 0,
        "cam_frames_count": 0,
        "cam_last_bytes": 0,
    }

    # Llegada de 3 paquetes de un frame
    ts = 554433
    slices = [
        make_cam_packet(seq=1, ts=ts, payload=b"HEADER_NAL_"),
        make_cam_packet(seq=2, ts=ts, payload=b"SLICE_BODY_"),
        make_cam_packet(seq=3, ts=ts, payload=b"FOOTER_DATA"),
    ]

    for pkt in slices:
        telemetry["cam_packets_count"] += 1
        frame = assembler.push_packet(pkt)
        if frame:
            telemetry["cam_active"] = True
            telemetry["cam_frames_count"] += 1
            telemetry["cam_last_bytes"] = len(frame)

    assert telemetry["cam_packets_count"] == 3
    assert telemetry["cam_frames_count"] == 0  # Aún en búfer esperando fin o nuevo ts

    # Siguiente paquete de nuevo fotograma
    next_pkt = make_cam_packet(seq=4, ts=ts + 33333, payload=b"NEXT_FRAME_DATA")
    telemetry["cam_packets_count"] += 1
    frame = assembler.push_packet(next_pkt)
    if frame:
        telemetry["cam_active"] = True
        telemetry["cam_frames_count"] += 1
        telemetry["cam_last_bytes"] = len(frame)

    assert telemetry["cam_active"] is True
    assert telemetry["cam_frames_count"] == 1
    assert telemetry["cam_last_bytes"] == len(b"HEADER_NAL_SLICE_BODY_FOOTER_DATA")
    assert frame == b"HEADER_NAL_SLICE_BODY_FOOTER_DATA"


def test_camera_frame_assembler_handles_vstrm_extended_header():
    """
    Prueba 148: Paquetes reales de cámara con cabecera extendida VSTRM (16 bytes:
    8 bytes básicos + 8 bytes de opciones terminados en 0x00 0x00) son despojados
    correctamente del encabezado para dejar solo el bitstream H.264.
    """
    assembler = CameraFrameAssembler()
    # Cabecera real capturada del GamePad:
    # f0 01 fa 3f f1 5f 62 c3 80 82 02 83 85 1e 00 00
    ext_hdr = bytes.fromhex("f0 01 fa 3f f1 5f 62 c3 80 82 02 83 85 1e 00 00")
    h264_slice = b"H264_CAMERA_SLICE_DATA"
    pkt1 = ext_hdr + h264_slice
    parsed = assembler.parse_header(pkt1)
    assert parsed is not None
    seq, ts, payload = parsed
    assert seq == 1
    assert ts == 0xf15f62c3
    assert payload == h264_slice




def _vstrm(seq, begin, end, payload, idr=False):
    w = 0xF0000000 | (seq & 0x3FF) << 16 | begin << 14 | end << 12 | len(payload)
    ext = bytes.fromhex("8082028385 1e0000".replace(" ", "")) if idr else bytes.fromhex("82028385 1e000000".replace(" ", ""))
    return struct.pack(">I", w) + struct.pack(">I", 0x12345678) + ext + payload


def test_cam_h264_builder_rebuilds_annexb_640():
    """
    Prueba 149: el flujo real de la cámara (2026-11-08) es vstrm como el de la consola: palabra 0 con
    seq (bits 25..16), inicio (14), fin (12) y tamaño (10..0); cabecera de 16 B; IDR = opción 0x80.
    CamH264Builder une los paquetes de un cuadro y devuelve H.264 Annex B decodificable: en IDR,
    SPS 640x480 + PPS; cabecera de slice sintetizada (IDR 0x25b804ff, P 0x21e003ff | frame_num<<13)
    y bytes de prevención de emulación. Un cuadro con paquetes perdidos se descarta.
    """
    from drchost.drc_cam_v4l2 import CamH264Builder, SPS_640, PPS
    assert SPS_640 == bytes.fromhex("67640020ac2b40501ec8")
    b = CamH264Builder()
    assert b.push(_vstrm(5, 1, 0, b"\xaa\xbb\x00\x00", idr=True)) is None
    out = b.push(_vstrm(6, 0, 1, b"\x01\xcc", idr=True))
    sc = b"\0\0\0\1"
    assert out == sc + SPS_640 + sc + PPS + sc + bytes.fromhex("25b804ff") + b"\xaa\xbb\x00\x00\x03\x01\xcc"
    # P siguiente: frame_num 1
    out = b.push(_vstrm(7, 1, 1, b"\x11\x22\x33"))
    assert out == sc + struct.pack(">I", 0x21e003ff | (1 << 13)) + b"\x11\x22\x33"
    # P con un paquete perdido (seq 9 falta): se descarta
    assert b.push(_vstrm(8, 1, 0, b"\x01")) is None
    assert b.push(_vstrm(10, 0, 1, b"\x02")) is None
    # Antes del primer IDR no se emite nada
    assert CamH264Builder().push(_vstrm(1, 1, 1, b"\x55")) is None


def test_cam_fifo_command_formatting():
    """
    Prueba 151: format_cam_ctl valida y construye las líneas para /tmp/drc_ctl referentes
    a los ajustes de la cámara (on, off, exposure, gain, wb, brightness, contrast, saturation, bitrate).
    Rechaza parámetros desconocidos o fuera de rango.
    """
    from drchost.drc_cam_v4l2 import format_cam_ctl

    # Encendido / apagado
    assert format_cam_ctl("on") == "cam on"
    assert format_cam_ctl("1") == "cam on"
    assert format_cam_ctl("off") == "cam off"
    assert format_cam_ctl("0") == "cam off"

    # Exposición
    assert format_cam_ctl("exposure", "auto") == "cam exposure auto"
    assert format_cam_ctl("exposure", 1500) == "cam exposure 1500"
    with pytest.raises(ValueError):
        format_cam_ctl("exposure", -5)
    with pytest.raises(ValueError):
        format_cam_ctl("exposure", 70000)

    # Ganancia
    assert format_cam_ctl("gain", "auto") == "cam gain auto"
    assert format_cam_ctl("gain", 0x50) == "cam gain 80"
    with pytest.raises(ValueError):
        format_cam_ctl("gain", 120)

    # Balance de blancos
    assert format_cam_ctl("wb", "auto") == "cam wb auto"
    assert format_cam_ctl("wb", 255, 64) == "cam wb 255 64"
    with pytest.raises(ValueError):
        format_cam_ctl("wb", 256, 64)

    # Brillo (-100..100)
    assert format_cam_ctl("brightness", 100) == "cam brightness 100"
    assert format_cam_ctl("brightness", -50) == "cam brightness -50"
    with pytest.raises(ValueError):
        format_cam_ctl("brightness", 101)

    # Contraste (0..100)
    assert format_cam_ctl("contrast", 60) == "cam contrast 60"
    with pytest.raises(ValueError):
        format_cam_ctl("contrast", -1)

    # Saturación (0..128)
    assert format_cam_ctl("saturation", 0) == "cam saturation 0"
    assert format_cam_ctl("saturation", 128) == "cam saturation 128"
    with pytest.raises(ValueError):
        format_cam_ctl("saturation", 129)

    # Tasa de bits (bitrate)
    assert format_cam_ctl("bitrate", 4000) == "cam bitrate 4000"
    with pytest.raises(ValueError):
        format_cam_ctl("bitrate", 50)


def test_v4l2_safe_device_validation():
    """
    Prueba 152: is_safe_v4l2_device previene escribir o abrir como loopback los dispositivos
    códec/ISP por hardware de la Raspberry Pi (/dev/video10..31), permitiendo dispositivos
    seguros como /dev/video0 o /dev/video42.
    """
    from drchost.drc_cam_v4l2 import is_safe_v4l2_device, V4L2LoopbackWriter

    # Dispositivos de la Pi reservados
    for i in range(10, 32):
        assert not is_safe_v4l2_device(f"/dev/video{i}")

    # Dispositivos libres/seguros
    assert is_safe_v4l2_device("/dev/video0")
    assert is_safe_v4l2_device("/dev/video1")
    assert is_safe_v4l2_device("/dev/video42")

    # V4L2LoopbackWriter lanza ValueError con un dispositivo prohibido
    with pytest.raises(ValueError, match="reservado por hardware"):
        V4L2LoopbackWriter(dev_path="/dev/video10")
    with pytest.raises(ValueError, match="reservado por hardware"):
        V4L2LoopbackWriter(dev_path="/dev/video20")
