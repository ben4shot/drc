"""Pruebas de drc_wowl: trama con la que la Pi (como consola) despierta al GamePad (Wake-on-WLAN)."""
import os
import struct
import sys

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.ciphers.aead import AESCCM  # noqa: E402

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "drchost"))
import drc_wowl as w  # noqa: E402

PI = bytes.fromhex("d83add123400")
PAD = bytes.fromhex("aabbccddee01")
CLAVE = bytes(range(0x10, 0x20))


def descifrar(trama, clave=CLAVE):
    """Descifrado independiente (mismo esquema CCMP de 802.11) para comprobar la trama."""
    d = trama
    pn = bytes([d[33], d[32], d[31], d[30], d[27], d[26]])
    nonce = bytes([d[24] & 0xF]) + d[10:16] + pn
    aad = bytes([d[0] & 0x8F, d[1] & 0xC7]) + d[4:22] + bytes([d[22] & 0x0F, 0]) + bytes([d[24] & 0xF, 0])
    return AESCCM(clave, 8).decrypt(nonce, d[34:], aad)


def test_cuerpo_lleva_la_mac_de_la_consola_en_el_offset_15():
    c = w.cuerpo_wake(PI)
    assert len(c) == 25 and c[15:21] == PI


def test_cabecera_fromds_protegida_qos():
    t = w.trama_wake(CLAVE, PI, PAD, pn=1, seq=5)
    assert len(t) == 26 + 8 + 25 + 8
    assert t[0] == 0x88 and t[1] == 0x42                     # QoS data, FromDS + protegida
    assert t[4:10] == PAD and t[10:16] == PI and t[16:22] == PI
    assert t[24] == 7                                          # TID 7 como la consola
    assert t[26:34] == bytes([1, 0, 0, 0x20, 0, 0, 0, 0])      # CCMP: PN=1, ExtIV, keyid 0
    assert struct.unpack_from("<H", t, 22)[0] >> 4 == 5


def test_se_descifra_con_la_clave_wowl_y_da_el_cuerpo():
    t = w.trama_wake(CLAVE, PI, PAD, pn=1, seq=0)
    assert descifrar(t) == w.cuerpo_wake(PI)


def test_pn_grande_se_reparte_en_la_cabecera_ccmp():
    t = w.trama_wake(CLAVE, PI, PAD, pn=0x010203040506, seq=0)
    assert t[26] == 0x06 and t[27] == 0x05 and t[30:34] == bytes([0x04, 0x03, 0x02, 0x01])
    assert descifrar(t) == w.cuerpo_wake(PI)


def test_otra_clave_no_descifra():
    t = w.trama_wake(CLAVE, PI, PAD, pn=1, seq=0)
    with pytest.raises(Exception):
        descifrar(t, bytes(16))


def test_rafaga_incrementa_pn_y_secuencia():
    r = w.rafaga_wake(CLAVE, PI, PAD, n=4, pn0=1)
    assert len(r) == 4
    pns = [(t[26] | t[27] << 8) for t in r]
    seqs = [struct.unpack_from("<H", t, 22)[0] >> 4 for t in r]
    assert pns == [1, 2, 3, 4] and seqs == [0, 1, 2, 3]
    assert all(descifrar(t) == w.cuerpo_wake(PI) for t in r)


def test_con_radiotap_para_inyectar():
    t = w.trama_wake(CLAVE, PI, PAD, pn=1, seq=0)
    p = w.con_radiotap(t)
    assert p[:4] == bytes([0, 0, 8, 0]) and p[8:] == t


def test_clave_wowl_del_blob(tmp_path):
    blob = bytearray(0x300)
    clave = bytes(range(1, 17))
    blob[0x6D:0x7D] = clave
    blob[0x7D:0x7F] = struct.pack("<H", w.crc16(clave))
    blob[0x7F:0x85] = PI
    blob[0x85:0x87] = struct.pack("<H", w.crc16(PI))
    f = tmp_path / "blob.bin"
    f.write_bytes(bytes(blob))
    assert w.leer_wowl(str(f)) == (clave, PI)
    blob[0x7D] ^= 0xFF                                         # CRC roto
    f.write_bytes(bytes(blob))
    assert w.leer_wowl(str(f)) is None


def test_rafaga_como_la_de_la_consola_repite_pn_1_y_avanza_la_secuencia_cada_cierto_numero():
    r = w.rafaga_wake(CLAVE, PI, PAD, n=20, pn0=1, pn_fijo=True, por_seq=5)
    pns = {(t[26] | t[27] << 8) for t in r}
    seqs = [struct.unpack_from("<H", t, 22)[0] >> 4 for t in r]
    assert pns == {1}
    assert seqs == [0] * 5 + [1] * 5 + [2] * 5 + [3] * 5
    assert all(descifrar(t) == w.cuerpo_wake(PI) for t in r)


def _trama_del_mando(clave, pc, mando, pn=1, prioridad=7):
    """Aviso del mando hacia su consola (ToDS), cifrado con el CCMP estándar: A1 = A3 = consola, A2 = mando."""
    fc, sc, qos = bytes([0x88, 0x41]), bytes([0x10, 0]), bytes([prioridad, 0])
    cab = fc + bytes([0x3C, 0]) + pc + mando + pc + sc + qos
    ccmp = bytes([pn & 0xFF, (pn >> 8) & 0xFF, 0, 0x20, 0, 0, 0, 0])
    cuerpo = bytes(15) + pc + bytes(8)      # 29 B como la ráfaga capturada (el contenido real aún no se conoce)
    return cab + ccmp + w._ccmp(clave, pc, mando, pc, fc, sc, qos, pn, cuerpo), cuerpo


def test_descifrar_el_aviso_del_mando_y_la_trama_propia():
    """Para saber qué clave y cuerpo usa el mando: se descifra su propio aviso (ToDS) probando variantes."""
    t, cuerpo = _trama_del_mando(CLAVE, PI, PAD)
    assert len(t) == 71                                          # el tamaño de la ráfaga capturada (2026-11-10)
    assert w.descifrar(t, CLAVE) == ("clave tal cual", cuerpo)
    assert w.descifrar(t, CLAVE[::-1]) == ("clave invertida", cuerpo)
    assert w.descifrar(t, bytes(16)) is None
    propia = w.trama_wake(CLAVE, PI, PAD, pn=3)                 # la nuestra (FromDS) también se descifra
    assert w.descifrar(propia, CLAVE) == ("clave tal cual", w.cuerpo_wake(PI))
    assert w.descifrar(t[:30], CLAVE) is None                     # corta: sin excepción


def test_leer_wowl_acepta_la_respuesta_con_cabecera(tmp_path):
    """`raw 5 06` por el FIFO devuelve 16 B de cabecera antes de la configuración (0x300 B)."""
    blob = bytearray(0x300)
    blob[0x6D:0x7D] = CLAVE
    struct.pack_into("<H", blob, 0x7D, w.crc16(CLAVE))
    blob[0x7F:0x85] = PI
    struct.pack_into("<H", blob, 0x85, w.crc16(PI))
    sin, con = tmp_path / "sin.bin", tmp_path / "con.bin"
    sin.write_bytes(bytes(blob))
    con.write_bytes(bytes(16) + bytes(blob))
    assert w.leer_wowl(str(sin)) == (CLAVE, PI)
    assert w.leer_wowl(str(con)) == (CLAVE, PI)
