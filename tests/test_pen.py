"""Modo lápiz (DRC_TOUCH_MODE="pen"): el táctil del GamePad como tableta de Linux con presión.

La presión del panel resistivo sube con la posición (tools/pen_pressure.py: crudo ≈ nivel + 0,44·x + 0,095·y, el
nivel es la fuerza): se descuenta, se lleva a 0..1023 con un rango y una curva configurables y se suaviza. El
dispositivo es una tableta estándar (BTN_TOOL_PEN + ABS_PRESSURE) que cualquier compositor con libinput reconoce;
donde la sesión no admite tabletas (cage, wiiu-wm, Hyprland anidado) se queda en puntero y se avisa.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_DIR = REPO / "src" / "drc_stream"
sys.path.insert(0, str(REPO / "drchost"))
import drc_ctl  # noqa: E402


def _run_cpp(tmp_path: Path, body: str):
    src = tmp_path / "t.cpp"
    src.write_text(body)
    exe = tmp_path / "t"
    subprocess.run(["g++", "-std=c++17", "-I", str(SRC_DIR), str(src), "-o", str(exe)], check=True,
                   capture_output=True, text=True)
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "SUCCESS" in r.stdout, r.stdout + r.stderr


def test_presion_corregida_por_posicion_rango_curva_y_suavizado(tmp_path):
    _run_cpp(tmp_path, r"""#include <cstdio>
#include <cmath>
#include <cstdlib>
#include "pen_pressure.h"
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #c); return 1; } } while (0)
using drc_stream::PenConfig; using drc_stream::PenPressure;
// Modelo medido (tools/pen_pressure.py, 2026-11-17): cuadrático en x; el lineal dejaba el centro ~150-280 alto.
static double pos(double x, double y) {
    return 0.7538 * x + 0.0071 * y - 8.68e-5 * x * x + 8.0e-6 * y * y + 2.51e-5 * x * y;
}
static int raw(double nivel, int x, int y) { return (int)std::lround(nivel + pos(x, y)); }
// Nivel a mitad del rango por defecto (-350..20).
static const double kMid = -165;
int main() {
    PenConfig c;
    CHECK(c.kx == 0.7538 && c.ky == 0.0071 && c.kxx == -8.68e-5 && c.kyy == 8.0e-6 && c.kxy == 2.51e-5);
    CHECK(c.min == -350 && c.max == 20 && c.curve == 2.0);
    c.smooth = 0.0; c.curve = 1.0;
    PenPressure p(c);
    CHECK(p.Update(false, 0, 0, 0) == 0);                          // sin tocar: 0
    CHECK(p.Update(true, raw(-350, 2000, 1000), 2000, 1000) <= 2);  // en el mínimo (±0,5 del redondeo): 1 (tocando nunca es 0)
    CHECK(p.Update(true, raw(-600, 2000, 1000), 2000, 1000) == 1);  // por debajo: se acota
    CHECK(p.Update(true, raw(20, 2000, 1000), 2000, 1000) == 1023);
    CHECK(p.Update(true, raw(900, 2000, 1000), 2000, 1000) == 1023);
    int mid = p.Update(true, raw(kMid, 2000, 1000), 2000, 1000);
    CHECK(mid > 500 && mid < 525);
    // La misma fuerza en otro sitio de la pantalla da la misma presión.
    // La misma fuerza en otro sitio de la pantalla da la misma presión (bordes y centro: el modelo es curvo).
    CHECK(abs(p.Update(true, raw(kMid, 3500, 3000), 3500, 3000) - mid) <= 3);
    CHECK(abs(p.Update(true, raw(kMid, 300, 300), 300, 300) - mid) <= 3);
    CHECK(abs(p.Update(true, raw(kMid, 3900, 200), 3900, 200) - mid) <= 3);
    // Curva 2: el centro baja a ~1/4.
    c.curve = 2.0;
    PenPressure q(c);
    int m2 = q.Update(true, raw(kMid, 2000, 1000), 2000, 1000);
    CHECK(m2 > 240 && m2 < 275);
    // Suavizado: el primer valor de un toque entra entero; los siguientes se acercan poco a poco.
    c.curve = 1.0; c.smooth = 0.5;
    PenPressure s(c);
    CHECK(s.Update(true, raw(-350, 2000, 1000), 2000, 1000) <= 2);
    int a1 = s.Update(true, raw(20, 2000, 1000), 2000, 1000);
    int a2 = s.Update(true, raw(20, 2000, 1000), 2000, 1000);
    CHECK(a1 > 480 && a1 < 540 && a2 > a1 && a2 < 1023);
    CHECK(s.Update(false, 0, 0, 0) == 0);                         // al levantar se reinicia
    CHECK(s.Update(true, raw(20, 2000, 1000), 2000, 1000) == 1023);
    // Configuración por entorno (drc.conf exporta los DRC_*).
    setenv("DRC_PEN_PRESSURE_KX", "0.5", 1); setenv("DRC_PEN_PRESSURE_KY", "0", 1);
    setenv("DRC_PEN_PRESSURE_MIN", "10", 1); setenv("DRC_PEN_PRESSURE_MAX", "20", 1);
    setenv("DRC_PEN_PRESSURE_CURVE", "1.5", 1); setenv("DRC_PEN_PRESSURE_SMOOTH", "0.2", 1);
    setenv("DRC_PEN_PRESSURE_KXX", "-1e-4", 1); setenv("DRC_PEN_PRESSURE_KYY", "0", 1);
    setenv("DRC_PEN_PRESSURE_KXY", "3e-5", 1);
    PenConfig e = drc_stream::LoadPenConfig();
    CHECK(e.kx == 0.5 && e.ky == 0.0 && e.min == 10 && e.max == 20 && e.curve == 1.5 && e.smooth == 0.2);
    CHECK(e.kxx == -1e-4 && e.kyy == 0.0 && e.kxy == 3e-5);
    setenv("DRC_PEN_PRESSURE_MAX", "5", 1);                         // max <= min: se ignora el rango
    PenConfig bad = drc_stream::LoadPenConfig();
    CHECK(bad.min == -350 && bad.max == 20);
    printf("SUCCESS\n");
    return 0;
}
""")


def test_el_lapiz_es_una_tableta_estandar():
    src = (SRC_DIR / "uinput_dev.h").read_text()
    pen = src[src.index("class UinputPen"):]
    pen = pen[:pen.index("\n};")]
    for cap in ("BTN_TOOL_PEN", "BTN_TOUCH", "ABS_PRESSURE", "INPUT_PROP_DIRECT", "UI_ABS_SETUP", "resolution"):
        assert cap in pen, cap
    assert '"Wii U GamePad Pen"' in pen


def test_drc_stream_elige_lapiz_o_puntero():
    src = (SRC_DIR / "drc_stream.cpp").read_text()
    assert 'getenv("DRC_TOUCH_MODE")' in src and "UinputPen" in src and "ts_raw_x" in src


def test_libdrc_entrega_la_posicion_en_crudo():
    """La corrección de la presión necesita x/y sin calibrar (0..4095)."""
    hdr = (REPO / "libdrc" / "include" / "drc" / "input.h").read_text()
    cpp = (REPO / "libdrc" / "src" / "input-receiver.cpp").read_text()
    assert "ts_raw_x" in hdr and "ts_raw_y" in hdr
    assert "data.ts_raw_x = ts_x" in cpp and "data.ts_raw_y = ts_y" in cpp


def _dry(args, env_extra):
    fd, conf = tempfile.mkstemp(suffix=".conf")
    os.close(fd)
    env = dict(os.environ, DRC_DRY_RUN="1", DRC_CONF_FILE=conf, **env_extra)
    env.pop("DRC_IFACE", None)
    r = subprocess.run(["bash", str(REPO / "drchost" / "start_drc.sh"), *args], capture_output=True, text=True,
                       env=env, timeout=60)
    return r.stdout, r.stderr


def test_start_drc_lapiz_solo_donde_la_sesion_admite_tabletas():
    out, err = _dry([], {"DRC_TOUCH_MODE": "pen", "DESKTOP_SESSION": "cage"})
    assert "táctil pointer" in out and "cage" in err and "DRC_TOUCH_MODE" in err, (out, err)
    out, err = _dry(["--mode", "extended"], {"DRC_TOUCH_MODE": "pen"})
    assert "táctil pen" in out, (out, err)
    out, _ = _dry([], {"DESKTOP_SESSION": "cage"})
    assert "táctil pointer" in out


def test_menu_y_requisitos_del_modo_lapiz():
    f = drc_ctl.FIELDS_BY_KEY["DRC_TOUCH_MODE"]
    assert f.kind == "choice" and set(f.choices) == {"pointer", "touch", "pen"} and f.default == "pointer"
    for k in ("DRC_PEN_PRESSURE_MIN", "DRC_PEN_PRESSURE_MAX", "DRC_PEN_PRESSURE_CURVE"):
        assert k in drc_ctl.FIELDS_BY_KEY
    assert drc_ctl.FIELDS_BY_KEY["DRC_PEN_PRESSURE_MIN"].default == "-350"
    assert drc_ctl.FIELDS_BY_KEY["DRC_PEN_PRESSURE_MAX"].default == "20"
    assert drc_ctl.FIELDS_BY_KEY["DRC_PEN_PRESSURE_CURVE"].default == "2.0"
    c = drc_ctl.touch_mode_check({"DRC_TOUCH_MODE": "pen", "DESKTOP_SESSION": "cage"}, "separate")
    assert c.status == drc_ctl.WARN and "cage" in c.detail
    assert drc_ctl.touch_mode_check({"DRC_TOUCH_MODE": "pen"}, "extended").status == drc_ctl.OK
    assert drc_ctl.touch_mode_check({"DRC_TOUCH_MODE": "pen", "DESKTOP_SESSION": "labwc"}, "separate").status == drc_ctl.OK
    assert drc_ctl.touch_mode_check({}, "separate").status == drc_ctl.OK


def test_en_separate_el_lapiz_es_solo_de_la_sesion_del_mando():
    out = subprocess.run(["bash", "-c", f'DESKTOP_UID=1000 DESKTOP_USER=pepe; source "{REPO}/drchost/host_pc.sh"; '
                                        'pc_input_kiosk_rules pepe'], capture_output=True, text=True, check=True).stdout
    pen = [l for l in out.splitlines() if '"Wii U GamePad Pen"' in l]
    assert pen and 'TAG-="uaccess"' in pen[0] and 'DRC_PAD_ONLY}="1"' in pen[0]


def test_extended_canvas_only_defaults_to_rpi_layout_without_explicit_canvas(tmp_path: Path):
    """En extended, el lienzo de 2774x1080 (pantalla de 1920 + mando a la derecha) es el de la Pi, donde el
    compositor reparte el dispositivo por todo el escritorio. Si quien lanza da el lienzo (--canvas-w/h, p. ej.
    854x480 cuando Hyprland asigna el dispositivo al monitor del mando), se respeta: si no, la esquina inferior
    izquierda del mando caía al 69 % en x y al 44 % en y."""
    src = tmp_path / "canvas.cpp"
    src.write_text(f"""#include <cassert>
#include <iostream>
#include "{SRC_DIR / "uinput_map.h"}"
int main() {{
    using drc_stream::CanvasMapping;
    CanvasMapping def;
    auto a = drc_stream::resolve_canvas("extended", def, false);
    assert(a.total_w == 2774 && a.total_h == 1080 && a.offset_x == 1920 && a.offset_y == 0);
    auto b = drc_stream::resolve_canvas("extended", def, true);       // 854x480 dado a mano
    assert(b.total_w == 854 && b.total_h == 480 && b.offset_x == 0 && b.offset_y == 0);
    auto c = drc_stream::resolve_canvas("mirror", def, false);
    assert(c.total_w == 854 && c.offset_x == 0);
    drc_stream::TouchCalib calib;
    auto p = drc_stream::map_touch_extended(0.0f, 1.0f, calib, b);     // esquina inferior izquierda
    assert(p.x == 0 && p.y == 479);
    std::cout << "OK\\n";
}}
""")
    exe = tmp_path / "canvas"
    r = subprocess.run(["g++", "-std=c++17", str(src), "-o", str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip() == "OK"


def test_start_drc_passes_gamepad_canvas_when_hyprland_maps_the_device():
    """Con Hyprland en extended, hypr_map_touch asigna el táctil y el lápiz al monitor del mando: drc_stream debe
    usar el lienzo del mando (854x480) y no el de la Pi."""
    text = (REPO / "drchost" / "start_drc.sh").read_text()
    i = text.index('DRC_STREAM_ARGS="--dest')
    tail = text[i:i + 600]
    assert '[ "${HYPR_EXTENDED}" = "1" ]' in tail and "--canvas-w 854 --canvas-h 480" in tail, tail
    src = (SRC_DIR / "drc_stream.cpp").read_text()
    assert "resolve_canvas(" in src
