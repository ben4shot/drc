#!/usr/bin/env python3
"""Lector/escritor NFC del Wii U GamePad (órdenes (5,16) por la FIFO de drc_stream).

Validado con el mando real y un móvil que emula una etiqueta NFC Forum tipo 4 (app android_nfc/):
  - detección: 0x0d (empezar, timeout BE u32 en ms) / 0x0e (consultar) -> `result, uid[7], readOnly, hasLocked, flags`.
  - ISO-DEP por «pass-through»: 0x0a envía un APDU, 0x0b recoge `00 | tam u16 | datos`; la 1.ª llamada con
    activación=1 devuelve los datos de activación (SAK, UID, ATS).
El `Read` normal (0x01/0x02) del mando NO sirve con ISO-DEP, por eso se usan APDUs directos.
Uso:  drc_nfc.py detect | read | write-text TEXTO | write-url URL
"""
import os
import sys
import time

CTL = "/tmp/drc_ctl"
OUT = "/tmp/drc_ctl.out"
PAYLOAD_LEN = 592
AID_NDEF = bytes.fromhex("d2760000850101")

URI_PREFIJOS = ["", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:"]


# ---------------------------------------------------------------- NDEF
def _registro(tipo: bytes, carga: bytes, tnf=1, mb=True, me=True) -> bytes:
    sr = len(carga) < 256
    flags = (0x80 if mb else 0) | (0x40 if me else 0) | (0x10 if sr else 0) | tnf
    cab = bytes([flags, len(tipo)]) + (bytes([len(carga)]) if sr else len(carga).to_bytes(4, "big"))
    return cab + tipo + carga


def ndef_text(texto: str, idioma: str = "en") -> bytes:
    lang = idioma.encode()
    return _registro(b"T", bytes([len(lang)]) + lang + texto.encode("utf-8"))


def ndef_url(url: str) -> bytes:
    mejor = 0
    for i, pre in enumerate(URI_PREFIJOS):
        if pre and url.startswith(pre) and len(pre) > len(URI_PREFIJOS[mejor]):
            mejor = i
    return _registro(b"U", bytes([mejor]) + url[len(URI_PREFIJOS[mejor]):].encode("utf-8"))


def parse_ndef(data: bytes) -> list:
    """Mensaje NDEF -> lista de registros {tipo: texto|url|mime|otro, valor, ...}. Corrupto -> lo que se pudo leer."""
    out, i = [], 0
    while i + 3 <= len(data):
        flags = data[i]
        sr, il, tnf = bool(flags & 0x10), bool(flags & 0x08), flags & 7
        tl = data[i + 1]
        i += 2
        if sr:
            if i >= len(data):
                break
            pl, i = data[i], i + 1
        else:
            if i + 4 > len(data):
                break
            pl, i = int.from_bytes(data[i:i + 4], "big"), i + 4
        idl = 0
        if il:
            if i >= len(data):
                break
            idl, i = data[i], i + 1
        if i + tl + idl + pl > len(data):
            break
        tipo, i = data[i:i + tl], i + tl
        i += idl
        carga, i = data[i:i + pl], i + pl
        out.append(_interpretar(tnf, tipo, carga))
        if flags & 0x40:
            break
    return out


def _interpretar(tnf, tipo, carga):
    if tnf == 1 and tipo == b"T" and carga:
        n = carga[0] & 0x3F
        return {"tipo": "texto", "valor": carga[1 + n:].decode("utf-8", "replace"),
                "idioma": carga[1:1 + n].decode("ascii", "replace")}
    if tnf == 1 and tipo == b"U" and carga:
        pre = URI_PREFIJOS[carga[0]] if carga[0] < len(URI_PREFIJOS) else ""
        return {"tipo": "url", "valor": pre + carga[1:].decode("utf-8", "replace")}
    if tnf == 2:
        return {"tipo": "mime", "valor": carga.decode("utf-8", "replace"), "mime": tipo.decode("ascii", "replace")}
    return {"tipo": "otro", "valor": carga.hex(" "), "tnf": tnf}


# ---------------------------------------------------------------- tramas
def nfc_payload(cmd) -> bytes:
    return bytes(cmd).ljust(PAYLOAD_LEN, b"\0")


def passthrough_header(act, discover_ms, cmd_ms, cmd_len, resp_len) -> bytes:
    return (bytes([0x0A, act]) + discover_ms.to_bytes(2, "big") + cmd_ms.to_bytes(4, "big")
            + cmd_len.to_bytes(2, "big") + resp_len.to_bytes(2, "big"))


def parse_reply(linea: str):
    """Línea «raw ok 7e 01 00 08 00 09 05 10 <len BE u32> <carga>» -> bytes de la carga (None si no es una respuesta)."""
    t = linea.split()
    if len(t) < 3 or t[0] != "raw" or t[1] != "ok":
        return None
    try:
        b = bytes(int(x, 16) for x in t[2:])
    except ValueError:
        return None
    if len(b) < 12:
        return None
    n = int.from_bytes(b[8:12], "big")
    return b[12:12 + n]


def parse_detect(r: bytes) -> dict:
    if len(r) < 11 or r[0] != 0x00:
        return {"presente": False}
    uid = r[1:8]
    if not any(uid[4:]):
        uid = uid[:4]
    return {"presente": True, "uid": ":".join("%02X" % x for x in uid),
            "solo_lectura": bool(r[8]), "bloqueada": bool(r[9])}


# ---------------------------------------------------------------- transporte con el mando
class PadLink:
    """Envía una orden (5,16) por /tmp/drc_ctl y espera su respuesta en /tmp/drc_ctl.out."""

    def __init__(self, ctl=CTL, out=OUT, wait=4.0):
        self.ctl, self.out, self.wait = ctl, out, wait

    def _lineas(self):
        try:
            with open(self.out) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def __call__(self, payload: bytes):
        antes = len(self._lineas())
        txt = "raw 5 16 " + " ".join("%02x" % x for x in payload) + "\n"
        with open(self.ctl, "w") as f:
            f.write(txt)
        fin = time.monotonic() + self.wait
        while time.monotonic() < fin:
            time.sleep(0.1)
            for linea in self._lineas()[antes:]:
                if linea.startswith("raw "):
                    return parse_reply(linea)
        return None


# ---------------------------------------------------------------- lector/escritor
class Nfc:
    def __init__(self, link, sleep=time.sleep, clock=time.monotonic):
        self.link, self.sleep, self.clock = link, sleep, clock

    def _cmd(self, cmd):
        return self.link(nfc_payload(cmd))

    def _encender(self):
        self._cmd([0x00])
        self._cmd([0x0C, 1])

    def _apagar(self):
        try:
            self._cmd([0x05])
        finally:
            self._cmd([0x06])

    def detect(self, timeout_s=10.0) -> dict:
        """Enciende el lector y espera una etiqueta; devuelve {presente, uid, ...}."""
        self._encender()
        try:
            ms = int(timeout_s * 1000)
            self._cmd([0x0D] + list(ms.to_bytes(4, "big")))
            fin = self.clock() + timeout_s
            while self.clock() < fin:
                r = self._cmd([0x0E])
                d = parse_detect(r) if r else {"presente": False}
                if d["presente"]:
                    return d
                self.sleep(0.5)
            return {"presente": False}
        finally:
            self._apagar()

    # --- ISO-DEP por pass-through
    def _apdu(self, data: bytes, act=0, rsize=16, timeout_s=6.0, discover_ms=30000):
        hdr = passthrough_header(act, discover_ms, 1500, len(data), rsize)
        if self._cmd(list(hdr) + list(data)) is None:
            return None
        fin = self.clock() + timeout_s
        while self.clock() < fin:
            r = self._cmd([0x0B])
            if r and r[0] == 0x00 and len(r) >= 3:
                n = int.from_bytes(r[1:3], "big")
                return r[3:3 + n]
            self.sleep(0.2)
        return None

    def _sw_ok(self, r):
        return r is not None and len(r) >= 2 and r[-2:] == b"\x90\x00"

    def _abrir_ndef(self, timeout_s):
        """Activa la etiqueta y deja seleccionado el fichero NDEF. -> (activación, tamaño máximo) o (None, error)."""
        sel_aid = bytes([0x00, 0xA4, 0x04, 0x00, len(AID_NDEF)]) + AID_NDEF + b"\x00"
        act = self._apdu(sel_aid, act=1, rsize=16, timeout_s=timeout_s)
        if act is None:
            return None, "No se detectó ninguna etiqueta (acércala al GamePad)"
        if not self._sw_ok(self._apdu(sel_aid, rsize=8)):
            return None, "La etiqueta no es NFC Forum tipo 4 (no tiene la aplicación NDEF)"
        if not self._sw_ok(self._apdu(bytes.fromhex("00a4000c02e103"), rsize=8)):
            return None, "Falta el fichero de capacidades"
        cc = self._apdu(bytes.fromhex("00b000000f"), rsize=17)
        if not self._sw_ok(cc) or len(cc) < 17:
            return None, "No se pudo leer el fichero de capacidades"
        maxsize = int.from_bytes(cc[11:13], "big")
        if not self._sw_ok(self._apdu(bytes.fromhex("00a4000c02e104"), rsize=8)):
            return None, "Falta el fichero NDEF"
        return (" ".join("%02x" % x for x in act), maxsize), None

    def read_ndef(self, timeout_s=30.0) -> dict:
        self._encender()
        try:
            res, err = self._abrir_ndef(timeout_s)
            if res is None:
                return {"ok": False, "error": err}
            act, _maxsize = res
            nlen = self._apdu(bytes.fromhex("00b0000002"), rsize=4)
            if not self._sw_ok(nlen) or len(nlen) < 4:
                return {"ok": False, "error": "No se pudo leer la longitud del mensaje"}
            n = int.from_bytes(nlen[:2], "big")
            datos = bytearray()
            while len(datos) < n:
                le = min(0xC8, n - len(datos))
                off = 2 + len(datos)
                r = self._apdu(bytes([0x00, 0xB0, off >> 8, off & 255, le]), rsize=le + 2)
                if not self._sw_ok(r):
                    return {"ok": False, "error": "Lectura interrumpida (¿se movió la etiqueta?)"}
                datos += r[:-2]
            return {"ok": True, "activacion": act, "bytes": bytes(datos), "registros": parse_ndef(bytes(datos))}
        finally:
            self._apagar()

    def write_ndef(self, mensaje: bytes, timeout_s=30.0) -> dict:
        self._encender()
        try:
            res, err = self._abrir_ndef(timeout_s)
            if res is None:
                return {"ok": False, "error": err}
            act, maxsize = res
            if len(mensaje) + 2 > maxsize:
                return {"ok": False, "error": "El mensaje es demasiado grande para la etiqueta (%d B, máximo %d)" % (len(mensaje), maxsize - 2)}

            def upd(off, d):
                return self._sw_ok(self._apdu(bytes([0x00, 0xD6, off >> 8, off & 255, len(d)]) + d, rsize=8))

            if not upd(0, b"\x00\x00"):
                return {"ok": False, "error": "La etiqueta no permite escribir (¿protegida?)"}
            pos = 0
            while pos < len(mensaje):
                trozo = mensaje[pos:pos + 0xC8]
                if not upd(2 + pos, trozo):
                    return {"ok": False, "error": "Escritura interrumpida (¿se movió la etiqueta?)"}
                pos += len(trozo)
            if not upd(0, len(mensaje).to_bytes(2, "big")):
                return {"ok": False, "error": "No se pudo cerrar la escritura"}
            return {"ok": True, "activacion": act, "bytes": len(mensaje)}
        finally:
            self._apagar()


def main(argv):
    nfc = Nfc(PadLink())
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "detect":
        print(nfc.detect(10))
    elif argv[0] == "read":
        r = nfc.read_ndef()
        print({k: v for k, v in r.items() if k != "bytes"})
    elif argv[0] == "write-text" and len(argv) > 1:
        print(nfc.write_ndef(ndef_text(" ".join(argv[1:]))))
    elif argv[0] == "write-url" and len(argv) > 1:
        print(nfc.write_ndef(ndef_url(argv[1])))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
