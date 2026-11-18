"""Pruebas de drc_nfc: NDEF, APDUs de etiqueta tipo 4 y orden de las órdenes (5,16) al GamePad, con un mando simulado."""
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "drchost"))
import drc_nfc as n  # noqa: E402


# ---------------------------------------------------------------- NDEF
def test_ndef_texto_ida_y_vuelta():
    msg = n.ndef_text("hola Wii U 123")
    assert msg == bytes.fromhex("d101115402") + b"en" + b"hola Wii U 123"
    recs = n.parse_ndef(msg)
    assert recs == [{"tipo": "texto", "valor": "hola Wii U 123", "idioma": "en"}]


def test_ndef_url_usa_prefijo_abreviado():
    msg = n.ndef_url("https://www.nintendo.com/")
    assert msg[:4] == bytes([0xD1, 0x01, len(msg) - 4, 0x55]) and msg[4] == 0x02   # https://www.
    assert n.parse_ndef(msg) == [{"tipo": "url", "valor": "https://www.nintendo.com/"}]


def test_ndef_url_sin_prefijo_conocido():
    msg = n.ndef_url("ftp://x.org")
    assert msg[4] == 0x00 and n.parse_ndef(msg)[0]["valor"] == "ftp://x.org"


def test_ndef_registro_largo_y_varios_registros():
    largo = "x" * 300
    msg = n.ndef_text(largo)
    assert n.parse_ndef(msg)[0]["valor"] == largo
    dos = bytes([0x91, 0x01, 0x05, 0x54, 0x02]) + b"en" + b"ab"  # MB, sin ME
    dos += bytes([0x51, 0x01, 0x05, 0x54, 0x02]) + b"en" + b"cd"  # ME
    assert [r["valor"] for r in n.parse_ndef(dos)] == ["ab", "cd"]


def test_ndef_mime_y_desconocido():
    msg = bytes([0xD2, 0x0A, 0x03]) + b"text/plain" + b"abc"
    assert n.parse_ndef(msg) == [{"tipo": "mime", "valor": "abc", "mime": "text/plain"}]
    assert n.parse_ndef(bytes([0xD1, 0x01, 0x02, 0x58]) + b"zz")[0]["tipo"] == "otro"


def test_ndef_vacio_o_corrupto_no_revienta():
    assert n.parse_ndef(b"") == []
    assert n.parse_ndef(bytes([0xD1, 0x01, 0x20, 0x54, 0x02])) == []   # longitud mayor que los datos


# ---------------------------------------------------------------- tramas (5,16)
def test_payload_nfc_mide_592_y_orden_en_el_primer_byte():
    p = n.nfc_payload([0x0C, 1])
    assert len(p) == 592 and p[:3] == bytes([0x0C, 1, 0]) and set(p[2:]) == {0}


def test_cabecera_passthrough_formato_validado():
    h = n.passthrough_header(act=1, discover_ms=30000, cmd_ms=1500, cmd_len=13, resp_len=16)
    assert h == bytes.fromhex("0a 01 7530 000005dc 000d 0010".replace(" ", ""))


def test_respuesta_raw_se_parsea():
    linea = "raw ok 7e 01 00 08 00 09 05 10 00 00 00 0b 08 00 00 00 00 00 00 00 00 00 00"
    assert n.parse_reply(linea) == bytes.fromhex("08000000000000000000 00".replace(" ", ""))
    assert n.parse_reply("raw fallo") is None
    assert n.parse_reply("battery ok 7e") is None


def test_uid_de_4_y_7_bytes():
    assert n.parse_detect(bytes.fromhex("00 01020304 000000 00 00 08".replace(" ", ""))) == {
        "presente": True, "uid": "01:02:03:04", "solo_lectura": False, "bloqueada": False}
    d = n.parse_detect(bytes.fromhex("00 04112233445566 01 01 08".replace(" ", "")))
    assert d["uid"] == "04:11:22:33:44:55:66" and d["solo_lectura"] and d["bloqueada"]
    assert n.parse_detect(bytes.fromhex("08" + "00" * 10))["presente"] is False


# ---------------------------------------------------------------- mando simulado
class MandoFalso:
    """Hace de GamePad+etiqueta tipo 4: responde a las (5,16) como el mando real."""

    def __init__(self, ndef=b"", presente=True):
        self.presente = presente
        self.fichero = bytearray(2 + 4096)
        self.fichero[:2 + len(ndef)] = len(ndef).to_bytes(2, "big") + ndef
        self.seleccion = None
        self.cmds = []
        self.pendiente = b""

    def __call__(self, payload):
        self.cmds.append(payload[0])
        op = payload[0]
        if op in (0x00, 0x05, 0x06, 0x0C, 0x0D):
            return b"\x00"
        if op == 0x0E:
            if not self.presente:
                return bytes([0x08]) + bytes(10)
            return bytes.fromhex("00 01020304 000000 00 00 08".replace(" ", ""))
        if op == 0x0A:
            act = payload[1]
            clen = int.from_bytes(payload[8:10], "big")
            apdu = payload[12:12 + clen]
            if not self.presente:
                self.pendiente = None
                return b"\x00"
            self.pendiente = bytes.fromhex("20 04112233 0c 0578 7780 00".replace(" ", "")) if act else self._apdu(apdu)
            return b"\x00"
        if op == 0x0B:
            if self.pendiente is None:
                return bytes([0x0E, 0, 0])
            d = self.pendiente
            return b"\x00" + len(d).to_bytes(2, "big") + d
        return b"\x00"

    def _apdu(self, a):
        ins = a[1]
        if ins == 0xA4:
            if a[2] == 0x04:
                return bytes.fromhex("9000")
            self.seleccion = a[5:7]
            return bytes.fromhex("9000")
        if ins == 0xB0:
            off = (a[2] << 8) | a[3]
            le = a[4]
            if self.seleccion == bytes.fromhex("e103"):
                cc = bytes([0x00, 0x0F, 0x20, 0x00, 0x3B, 0x00, 0x34, 0x04, 0x06, 0xE1, 0x04, 0x10, 0x00, 0x00, 0x00])
                return cc[off:off + le] + bytes.fromhex("9000")
            return bytes(self.fichero[off:off + le]) + bytes.fromhex("9000")
        if ins == 0xD6:
            off = (a[2] << 8) | a[3]
            lc = a[4]
            self.fichero[off:off + lc] = a[5:5 + lc]
            return bytes.fromhex("9000")
        return bytes.fromhex("6d00")


def test_detectar_devuelve_uid_y_apaga_al_final():
    m = MandoFalso()
    nfc = n.Nfc(m, sleep=lambda s: None)
    d = nfc.detect(timeout_s=2)
    assert d["presente"] and d["uid"] == "01:02:03:04"
    assert m.cmds[0] == 0x00 and m.cmds[-1] == 0x06   # startup ... shutdown


def test_detectar_sin_etiqueta_agota_el_tiempo():
    m = MandoFalso(presente=False)
    t = [0.0]
    nfc = n.Nfc(m, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert nfc.detect(timeout_s=2)["presente"] is False


def test_leer_ndef_de_etiqueta_tipo_4():
    m = MandoFalso(ndef=n.ndef_text("hola Wii U 123"))
    r = n.Nfc(m, sleep=lambda s: None).read_ndef()
    assert r["ok"] and r["registros"] == [{"tipo": "texto", "valor": "hola Wii U 123", "idioma": "en"}]
    assert r["activacion"].startswith("20 04 11 22 33")


def test_leer_etiqueta_vacia():
    r = n.Nfc(MandoFalso(ndef=b""), sleep=lambda s: None).read_ndef()
    assert r["ok"] and r["registros"] == []


def test_leer_sin_etiqueta_da_error_claro():
    t = [0.0]
    r = n.Nfc(MandoFalso(presente=False), sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0]).read_ndef(timeout_s=3)
    assert not r["ok"] and "etiqueta" in r["error"].lower()


def test_escribir_ndef_orden_nlen0_mensaje_nlen_y_releer():
    m = MandoFalso(ndef=n.ndef_text("viejo"))
    nfc = n.Nfc(m, sleep=lambda s: None)
    msg = n.ndef_text("hola desde el mando")
    r = nfc.write_ndef(msg)
    assert r["ok"]
    assert bytes(m.fichero[:2]) == len(msg).to_bytes(2, "big")
    assert bytes(m.fichero[2:2 + len(msg)]) == msg
    assert n.Nfc(m, sleep=lambda s: None).read_ndef()["registros"][0]["valor"] == "hola desde el mando"


def test_escribir_mensaje_largo_se_parte_en_trozos():
    m = MandoFalso()
    msg = n.ndef_text("y" * 500)
    assert n.Nfc(m, sleep=lambda s: None).write_ndef(msg)["ok"]
    assert bytes(m.fichero[2:2 + len(msg)]) == msg


def test_escribir_rechaza_mensaje_mayor_que_la_etiqueta():
    r = n.Nfc(MandoFalso(), sleep=lambda s: None).write_ndef(b"z" * 5000)
    assert not r["ok"] and "grande" in r["error"].lower()
