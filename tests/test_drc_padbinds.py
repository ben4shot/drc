import pytest

import drc_padbinds as pb


def test_parse_bind_home_x():
    assert pb.parse_bind("HOME+X=cerrar") == ((pb.BUTTONS["HOME"],), pb.BUTTONS["X"], "cerrar")
    assert pb.parse_bind(" home + zl + a = echo a=b ") == ((pb.BUTTONS["HOME"], pb.BUTTONS["ZL"]), pb.BUTTONS["A"], "echo a=b")


@pytest.mark.parametrize("malo", ["HOME+X", "HOME+X=", "=orden", "HOME+Q=orden"])
def test_parse_bind_rechaza_lo_mal_escrito(malo):
    with pytest.raises(ValueError):
        pb.parse_bind(malo)


def test_combo_solo_dispara_con_el_modificador_mantenido():
    home, x = pb.BUTTONS["HOME"], pb.BUTTONS["X"]
    c = pb.Combos([pb.parse_bind("HOME+X=cerrar")])
    assert c.key(x, 1) == [] and c.key(x, 0) == []          # X sola: nada (es del juego)
    assert c.key(home, 1) == []
    assert c.key(x, 1) == ["cerrar"]                         # HOME mantenido + X
    assert c.key(x, 2) == []                                 # autorrepetición: no repite
    assert c.key(x, 0) == [] and c.key(home, 0) == []
    assert c.key(x, 1) == []                                 # HOME ya suelto
