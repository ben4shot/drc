"""Pruebas de la lógica de las apps táctiles (cámara y NFC). Necesitan pygame (se saltan si no está: se ejecutan en la Pi)."""
import os
import sys
import time

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
pygame = pytest.importorskip("pygame")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "apps"))
import drc_camera_app as cam  # noqa: E402
import ui  # noqa: E402


def test_interfaz_ap_es_la_que_tiene_la_ip_del_ap(monkeypatch):
    monkeypatch.delenv("DRC_IFACE", raising=False)
    salida = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
              "3: wlan0    inet 192.168.0.20/24 brd 192.168.0.255 scope global wlan0\n"
              "4: wlan1    inet 192.168.1.10/29 brd 192.168.1.15 scope global wlan1\n")
    assert cam.interfaz_ap(salida) == "wlan1"
    assert cam.interfaz_ap("3: wlan0    inet 192.168.0.20/24 scope global wlan0\n") is None


def test_interfaz_ap_respeta_drc_iface(monkeypatch):
    monkeypatch.setenv("DRC_IFACE", "wlan0")
    assert cam.interfaz_ap("") == "wlan0"


def test_ajuste_con_auto_empieza_en_su_valor_inicial_y_se_limita():
    a = cam.Ajuste("gain", "Ganancia", 0, 111, 8, 32, auto=True)
    assert a.texto() == "auto" and a.orden() == "cam gain auto"
    assert a.mover(+1) == "cam gain 32"          # el primer toque saca de «auto» sin saltar
    assert a.mover(+1) == "cam gain 40"
    for _ in range(30):
        a.mover(+1)
    assert a.valor == 111
    for _ in range(30):
        a.mover(-1)
    assert a.valor == 0
    assert a.restablecer() == "cam gain auto" and a.texto() == "auto"


def test_ajuste_sin_auto_restablece_a_su_valor_normal():
    a = cam.Ajuste("brightness", "Brillo", -100, 100, 10, 0)
    assert a.texto() == "normal"
    a.mover(-1)
    assert a.valor == 0 and a.mover(-1) == "cam brightness -10"
    assert a.restablecer() == "cam brightness 0"


def test_ordenes_de_los_ajustes_por_defecto_son_validas_para_drc_stream():
    rangos = {"brightness": (-100, 100), "contrast": (0, 100), "saturation": (0, 128), "exposure": (0, 65535), "gain": (0, 111)}
    for a in cam.ajustes_por_defecto():
        lo, hi = rangos[a.clave]
        assert (a.min, a.max) == (lo, hi)
        assert lo <= a.inicio <= hi


def test_ctl_no_se_cuelga_sin_fifo(tmp_path):
    assert cam.ctl("set camera 1", ruta=str(tmp_path / "no_existe")) is False


def test_ctl_escribe_en_la_fifo(tmp_path):
    ruta = str(tmp_path / "ctl")
    os.mkfifo(ruta)
    lector = os.open(ruta, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert cam.ctl("cam brightness 10", ruta=ruta) is True
        assert os.read(lector, 100) == b"cam brightness 10\n"
    finally:
        os.close(lector)


def test_nombre_de_foto():
    assert cam.nombre_foto(time.struct_time((2026, 11, 9, 18, 5, 7, 0, 0, -1))) == "gamepad-camara-20261109-180507.png"


def test_wrap_respeta_el_ancho_y_las_palabras_largas():
    pygame.font.init()
    f = ui.font(16)
    for linea in ui.wrap("una frase bastante larga que no cabe en una sola linea de pantalla", f, 150):
        assert f.size(linea)[0] <= 150
    for linea in ui.wrap("x" * 200, f, 100):
        assert f.size(linea)[0] <= 100
    assert ui.wrap("a\nb", f, 100) == ["a", "b"]


def test_teclado_escribe_borra_y_cambia_de_capa():
    pygame.font.init()
    k = ui.Keyboard()
    por_valor = {v: r for r, v, _ in k.keys}
    assert k.press(por_valor["q"].center) == "q"
    assert ui.aplicar_tecla("ab", "\b") == "a" and ui.aplicar_tecla("ab", "c") == "abc" and ui.aplicar_tecla("ab", "\n") == "ab"
    assert k.press(por_valor["\x01"].center) is None and k.mayus
    assert any(v == "Q" for _, v, _ in k.keys)
    assert k.press(por_valor["\x02"].center) is None and k.simbolos
    assert any(v == "@" for _, v, _ in k.keys)
    assert k.press((0, 0)) is None


def test_todas_las_teclas_caben_en_pantalla():
    for simbolos in (False, True):
        k = ui.Keyboard()
        k.simbolos = simbolos
        k._layout()
        for rect, _, _ in k.keys:
            assert 0 <= rect.left and rect.right <= ui.W and rect.top >= k.top and rect.bottom <= ui.H


# ------------------------------------------------ lienzo escalado (el WM le quita a la ventana el alto de la barra)
def test_ajuste_ventana_conserva_la_proporcion_y_centra():
    assert ui.ajuste_ventana(854, 480) == (0, 0, 854, 480)
    ox, oy, w, h = ui.ajuste_ventana(854, 450)          # ventana bajo la barra de 30 px
    assert h == 450 and w == 800 and ox == 27 and oy == 0
    ox, oy, w, h = ui.ajuste_ventana(1708, 960)
    assert (w, h) == (1708, 960) and (ox, oy) == (0, 0)
    ox, oy, w, h = ui.ajuste_ventana(400, 480)           # muy estrecha: manda el ancho
    assert w == 400 and abs(h - 225) <= 1 and ox == 0 and abs(oy - 127) <= 1


def test_a_logica_deshace_el_escalado_y_ajusta_al_borde():
    assert ui.a_logica((100, 50), 854, 480) == (100, 50)
    x, y = ui.a_logica((27 + 400, 225), 854, 450)        # centro de la ventana = centro del lienzo
    assert abs(x - 427) <= 1 and abs(y - 240) <= 1
    assert ui.a_logica((0, 0), 854, 450) == (0, 0)       # en la banda lateral: se queda en el borde, no negativo
    x, y = ui.a_logica((853, 449), 854, 450)
    assert x == ui.W - 1 and y >= ui.H - 3


def test_lienzo_presenta_en_ventana_mas_baja_sin_cortar():
    pygame.display.init()
    win = pygame.display.set_mode((854, 450), pygame.RESIZABLE)
    lz = ui.Lienzo()
    lz.canvas.fill((10, 20, 30))
    pygame.draw.rect(lz.canvas, (255, 0, 0), (0, ui.H - 20, ui.W, 20))   # franja roja en el borde de abajo
    lz.presentar(flip=False)
    r, g, b = win.get_at((427, 445))[:3]
    assert r > 240 and g < 15 and b < 15                  # la franja del borde de abajo se ve (smoothscale mezcla un poco)
    r, g, b = win.get_at((427, 200))[:3]
    assert abs(r - 10) <= 3 and abs(g - 20) <= 3 and abs(b - 30) <= 3


# ------------------------------------------------ lienzo rápido y galería de fotos de la cámara
def test_lienzo_rapido_presenta_igual_de_bien():
    pygame.display.init()
    win = pygame.display.set_mode((854, 450), pygame.RESIZABLE)
    lz = ui.Lienzo(rapido=True)
    lz.canvas.fill((10, 20, 30))
    pygame.draw.rect(lz.canvas, (255, 0, 0), (0, ui.H - 20, ui.W, 20))
    lz.presentar(flip=False)
    assert win.get_at((427, 445))[:3] == (255, 0, 0)       # sin mezcla: píxel exacto
    assert win.get_at((427, 200))[:3] == (10, 20, 30)
    lz.presentar(flip=False)                                # segunda vez reutiliza el destino sin fallar


def test_listar_fotos_las_mas_nuevas_primero_y_solo_las_de_la_camara(tmp_path):
    for n in ("gamepad-camara-20261110-010000.png", "gamepad-camara-20261110-030000.png",
              "gamepad-camara-20261109-235959.png", "otra.png", "gamepad-camara-x.txt"):
        (tmp_path / n).write_bytes(b"x")
    nombres = [os.path.basename(r) for r in cam.listar_fotos(str(tmp_path))]
    assert nombres == ["gamepad-camara-20261110-030000.png", "gamepad-camara-20261110-010000.png",
                       "gamepad-camara-20261109-235959.png"]
    assert cam.listar_fotos(str(tmp_path / "no_existe")) == []


def test_ruta_corta_usa_la_tilde():
    assert cam.ruta_corta("/home/ben/Pictures/a.png", "/home/ben") == "~/Pictures/a.png"
    assert cam.ruta_corta("/tmp/a.png", "/home/ben") == "/tmp/a.png"
    assert cam.ruta_corta("/home/bender/a.png", "/home/ben") == "/home/bender/a.png"


def test_cargar_foto_cabe_en_el_area_y_conserva_la_proporcion(tmp_path):
    pygame.display.init()
    ruta = str(tmp_path / "gamepad-camara-1.png")
    pygame.image.save(pygame.Surface((640, 480)), ruta)
    img = cam.cargar_foto(ruta, 640, 480)
    assert img.get_size() == (640, 480)
    pygame.image.save(pygame.Surface((1280, 480)), ruta)
    assert cam.cargar_foto(ruta, 640, 480).get_size() == (640, 240)
    (tmp_path / "rota.png").write_bytes(b"no es un png")
    assert cam.cargar_foto(str(tmp_path / "rota.png"), 640, 480) is None
