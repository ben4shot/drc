import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_el_probador_dibuja_todas_las_paginas_sin_mando(tmp_path):
    """--shots usa SDL «dummy»: cada pestaña se dibuja sin excepciones y deja su PNG (no toca el escritorio)."""
    pytest.importorskip("pygame")
    r = subprocess.run([sys.executable, str(REPO / "tools" / "gamepad_test.py"), "--shots", str(tmp_path)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert len(list(tmp_path.glob("*.png"))) == 8


def test_informe_json_del_probador(tmp_path):
    from drchost import padtest as pt
    rep = pt.Report()
    rep.set("speaker", pt.PASS, "confirmado")
    out = tmp_path / "r.json"
    rep.save(str(out))
    import json
    d = json.loads(out.read_text())
    assert d["resumen"]["ok"] == 1 and len(d["comprobaciones"]) == len(pt.new_checks())


def test_bajo_sudo_se_recupera_la_sesion_grafica_sin_usar_la_del_mando(tmp_path):
    """sudo quita XDG_RUNTIME_DIR/WAYLAND_DISPLAY: el probador los recupera de SUDO_UID, y nunca elige wayland-drc."""
    pytest.importorskip("pygame")
    import importlib.util
    spec = importlib.util.spec_from_file_location("gamepad_test", REPO / "tools" / "gamepad_test.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path[:0] = [str(REPO), str(REPO / "apps")]
    spec.loader.exec_module(mod)
    run = tmp_path / "1000"
    run.mkdir()
    for n in ("wayland-drc", "wayland-1", "wayland-1.lock", "wayland-1-awww-daemon.sock"):
        (run / n).write_text("")
    env = {"SUDO_UID": "1000"}
    c = mod.entorno_grafico_de_sudo(env, runtime_root=str(tmp_path))
    assert env["XDG_RUNTIME_DIR"] == str(run) and env["WAYLAND_DISPLAY"] == "wayland-1" and c["DISPLAY"] == ":0"
    assert mod.entorno_grafico_de_sudo({"XDG_RUNTIME_DIR": "/x", "SUDO_UID": "1000"}) == {}   # sin sudo o ya definido: no toca
    assert mod.entorno_grafico_de_sudo({}) == {}


def test_en_mando_apunta_a_la_sesion_wayland_drc(tmp_path):
    pytest.importorskip("pygame")
    import importlib.util
    spec = importlib.util.spec_from_file_location("gamepad_test", REPO / "tools" / "gamepad_test.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path[:0] = [str(REPO), str(REPO / "apps")]
    spec.loader.exec_module(mod)
    env = {"SUDO_UID": "1000"}
    c = mod.entorno_grafico_de_sudo(env, runtime_root=str(tmp_path / "run"), en_mando=True, drc_root=str(tmp_path))
    # la sesión del mando no usa /run/user/<uid> sino su propio /tmp/drc_runtime_<uid>
    assert c["WAYLAND_DISPLAY"] == "wayland-drc" and env["XDG_RUNTIME_DIR"] == str(tmp_path / "drc_runtime_1000")
