"""Pruebas de la logica de drc_menu (sin GTK): lectura de estados de drc-pad y circulos de brillo."""
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "drchost"))
import drc_menu as m  # noqa: E402


def test_clase_de_json():
    assert m.clase_de_json('{"text":"NFC ●","tooltip":"x","class":"on","percentage":100}\n') == "on"
    assert m.clase_de_json('{"text":"IR ○","class":"off"}') == "off"
    assert m.clase_de_json("") == "" and m.clase_de_json("basura") == ""


def test_brillo_de_json():
    assert m.brillo_de_json('{"text":"☀ 3/5","percentage":60}') == 3
    assert m.brillo_de_json('{"text":"☀ 9/5"}') == 5
    assert m.brillo_de_json('{"text":"nada"}') is None and m.brillo_de_json("") is None


def test_circulos():
    assert m.circulos(3) == "●●●○○"
    assert m.circulos(5) == "●●●●●" and m.circulos(0) == "○○○○○"
    assert m.circulos(None) == "○○○○○" and m.circulos(99) == "●●●●●"


def test_modo_interno_por_dropin(tmp_path):
    f = tmp_path / "zzz-wlan0.conf"
    assert m.modo_interno(str(f)) is False
    f.write_text("x")
    assert m.modo_interno(str(f)) is True
