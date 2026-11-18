"""Táctil como ratón en el escritorio del PC (DRC_TOUCH_MODE="pointer", extended/mirror con Hyprland).

Hyprland entrega los toques como toques: no mueven el cursor y en el escritorio vacío o en apps X11 (Steam) no hacen
nada. En pointer, touch2pointer toma el táctil y lo convierte en ratón (cursor + clic) atado al monitor del mando
(wlr-virtual-pointer v2, T2P_OUTPUT). DRC_TOUCH_MODE="touch" deja el táctil nativo, como antes.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
T2P = REPO / "tools" / "touch2pointer"
START = (REPO / "drchost" / "start_drc.sh").read_text()
HOST = (REPO / "drchost" / "host_pc.sh").read_text()
sys.path.insert(0, str(REPO / "drchost"))
import drc_ctl  # noqa: E402


def test_touch2pointer_se_ata_a_un_monitor():
    src = (T2P / "touch2pointer.c").read_text()
    assert 'getenv("T2P_OUTPUT")' in src
    assert "create_virtual_pointer_with_output" in src and "wl_output_interface" in src


@pytest.mark.skipif(not (shutil.which("wayland-scanner") and shutil.which("cc")), reason="sin wayland-scanner o cc")
def test_touch2pointer_compila_sin_avisos(tmp_path):
    x = T2P / "wlr-virtual-pointer-unstable-v1.xml"
    subprocess.run(["wayland-scanner", "client-header", str(x), str(tmp_path / "wlr-virtual-pointer-unstable-v1-client-protocol.h")], check=True)
    subprocess.run(["wayland-scanner", "private-code", str(x), str(tmp_path / "proto.c")], check=True)
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "wayland-client"], capture_output=True, text=True, check=True).stdout.split()
    r = subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(tmp_path), str(T2P / "touch2pointer.c"),
                        str(tmp_path / "proto.c"), *flags, "-o", str(tmp_path / "t2p")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_start_drc_lanza_el_puntero_en_el_monitor_del_mando():
    assert "hypr_touch_pointer() {" in HOST
    fn = HOST[HOST.index("hypr_touch_pointer() {"):]
    fn = fn[:fn.index("\n}\n")]
    assert 'T2P_OUTPUT="${1}"' in fn and "WAYLAND_DISPLAY=" in fn and "touch2pointer.log" in fn
    bloque = START[START.index("# Pantalla táctil del mando en la sesión Hyprland del PC"):]
    bloque = bloque[:bloque.index("hypr_restore_session_env")]
    assert 'hypr_touch_pointer "${HYPR_MIRROR_OUT}"' in bloque and 'hypr_touch_pointer "${HYPR_OUTPUT}"' in bloque
    assert '"${DRC_TOUCH_MODE}" = "pointer"' in bloque
    # al parar se cierra en cualquier modo (antes solo en el quiosco)
    limpieza = START[START.index("cleanup() {"):]
    limpieza = limpieza[:limpieza.index("\n}\n")]
    assert "pkill -TERM -x touch2pointer 2>/dev/null || true\n" in limpieza


def test_modo_touch_nativo_en_menu_y_arranque():
    f = drc_ctl.FIELDS_BY_KEY["DRC_TOUCH_MODE"]
    assert set(f.choices) == {"pointer", "touch", "pen"} and f.default == "pointer"
    assert '"${TOUCH_MODE}" = "touch"' in START
