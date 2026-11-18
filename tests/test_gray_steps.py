from drchost.gray_steps import DARK, LIGHT, make_frame

W, H = 854, 480


def test_gray_steps_frame_layout():
    """Prueba 52: patrón de barras de luma exacta para averiguar si el GamePad
    decodifica en rango limitado (16-235) o completo (0-255).
    Mitad superior: barras oscuras DARK; inferior: claras LIGHT; croma neutra (128)."""
    assert DARK[0] == 0 and 16 in DARK
    assert LIGHT[-1] == 255 and 235 in LIGHT
    f = make_frame()
    assert len(f) == W * H * 3 // 2
    bar_w = W // len(DARK)
    for i, y in enumerate(DARK):
        assert f[(H // 4) * W + i * bar_w + bar_w // 2] == y
    for i, y in enumerate(LIGHT):
        assert f[(3 * H // 4) * W + i * bar_w + bar_w // 2] == y
    assert set(f[W * H:]) == {128}
