"""tools/wowl_captura.py: captura en monitor lo que emite el mando al encenderse sin su consola (el aviso WoWL)."""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import wowl_captura as wc  # noqa: E402

PC = bytes.fromhex("d83add123400")
PAD = bytes.fromhex("02aabbccddee")


def _trama(fc0, fc1, a1, a2, a3, largo):
    d = bytes([fc0, fc1, 0, 0]) + a1 + a2 + a3 + bytes(2)
    return d + bytes(largo - len(d))


def test_se_queda_con_lo_que_emite_el_mando():
    aviso = _trama(0x88, 0x41, PC, PAD, PC, 71)
    probe = _trama(0x40, 0x00, b"\xff" * 6, PAD, b"\xff" * 6, 60)
    ajeno = _trama(0x88, 0x41, PC, bytes(6), PC, 71)
    assert wc.del_mando(aviso, PAD) and wc.del_mando(probe, PAD)
    assert not wc.del_mando(ajeno, PAD) and not wc.del_mando(b"\x88", PAD)
    r = wc.resumen([aviso, aviso, probe])
    assert r == {"aviso WoWL (71 B, ToDS)": 2, "probe request": 1}


def test_pcap_con_radiotap():
    d = _trama(0x40, 0, PC, PAD, PC, 30)
    p = wc.pcap_bytes([(1.5, b"\x00\x00\x08\x00\x00\x00\x00\x00" + d)])
    assert struct.unpack_from("<IHHiIII", p, 0) == (0xA1B2C3D4, 2, 4, 0, 0, 65535, 127)   # linktype radiotap
    sec, usec, incl, orig = struct.unpack_from("<IIII", p, 24)
    assert (sec, usec, incl, orig) == (1, 500000, 38, 38) and p[40:] == b"\x00\x00\x08\x00\x00\x00\x00\x00" + d


def test_no_toca_la_interfaz_con_la_sesion_abierta():
    assert wc.sesion_abierta(lambda: "123 /x/hostapd -dd /tmp/drc_conf/generated_ap_normal.conf\n")
    assert not wc.sesion_abierta(lambda: "")
