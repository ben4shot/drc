#!/usr/bin/env python3
"""Desplegable de ajustes del GamePad (lo abre el ⚙ de la barra con `drc-pad menu`).

Panel GTK3 anclado con gtk-layer-shell bajo la barra, a la derecha: interruptores para las luces IR,
el lector NFC y el enlace WiFi (apagado = USB/wlan1, encendido = WiFi interna/wlan0) y una fila de brillo
«−  ●●●○○  +». Cambiar de enlace corta la imagen ~1 min: pide confirmar dentro del propio panel.
Todo se hace llamando a `drc-pad` (que habla con drc_stream), así la barra y el panel comparten estado.
"""
import json
import os
import subprocess
import threading

MODO_WLAN0 = "/etc/systemd/system/drc.service.d/zzz-wlan0.conf"
MAX_BRILLO = 5


# ---------------------------------------------------------------- lógica sin interfaz (probada en test_drc_menu.py)
def clase_de_json(salida: str) -> str:
    """`drc-pad ir|nfc` imprime un JSON de waybar; devuelve su campo class ('on'/'off'/...)."""
    try:
        return json.loads(salida.strip().splitlines()[-1]).get("class", "")
    except (ValueError, IndexError, AttributeError):
        return ""


def brillo_de_json(salida: str):
    """`drc-pad brightness` -> {'text': '☀ 3/5', ...}; devuelve 3 (o None si no se entiende)."""
    try:
        txt = json.loads(salida.strip().splitlines()[-1]).get("text", "")
        n = int(txt.split()[-1].split("/")[0])
        return max(1, min(MAX_BRILLO, n))
    except (ValueError, IndexError, AttributeError):
        return None


def circulos(n, maximo=MAX_BRILLO) -> str:
    n = 0 if n is None else max(0, min(maximo, n))
    return "●" * n + "○" * (maximo - n)


def modo_interno(ruta=MODO_WLAN0) -> bool:
    """True si el enlace está en la WiFi interna (existe el drop-in de wlan0)."""
    return os.path.exists(ruta)


def drc_pad(*args, timeout=15) -> str:
    try:
        return subprocess.run(["drc-pad", *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def leer_estado() -> dict:
    return {
        "ir": clase_de_json(drc_pad("ir")) == "on",
        "nfc": clase_de_json(drc_pad("nfc")) == "on",
        "interno": modo_interno(),
        "brillo": brillo_de_json(drc_pad("brightness")),
    }


# ---------------------------------------------------------------- interfaz
CSS = b"""
window { background-color: rgba(18,12,32,0.97); border: 2px solid #7c3aed; border-radius: 12px; }
label { color: #f3e8ff; font-family: "DejaVu Sans"; font-size: 17px; }
label.dim { color: #9687b4; font-size: 14px; }
label.puntos { font-size: 20px; letter-spacing: 3px; color: #c084fc; }
switch { min-width: 70px; min-height: 36px; border-radius: 18px; background-color: #34245a; border: none; }
switch slider { min-width: 32px; min-height: 32px; border-radius: 16px; background-color: #f3e8ff; border: none; margin: 2px; }
switch:checked { background-color: #7c3aed; }
button { min-width: 52px; min-height: 44px; border-radius: 10px; background-image: none; background-color: #34245a;
         border: none; color: #f3e8ff; font-size: 22px; font-weight: bold; }
button:active { background-color: #7c3aed; }
button.peligro { background-color: #e63946; font-size: 16px; }
button.cerrar { min-width: 40px; min-height: 30px; font-size: 16px; }
"""


def main():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, GLib, Gtk, GtkLayerShell

    estado = leer_estado()
    ocupado = {"v": False}

    win = Gtk.Window()
    GtkLayerShell.init_for_window(win)
    GtkLayerShell.set_layer(win, GtkLayerShell.Layer.TOP)
    GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.TOP, True)
    GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.RIGHT, True)
    GtkLayerShell.set_margin(win, GtkLayerShell.Edge.TOP, 34)
    GtkLayerShell.set_margin(win, GtkLayerShell.Edge.RIGHT, 6)
    GtkLayerShell.set_namespace(win, "drc-menu")
    prov = Gtk.CssProvider()
    prov.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    caja = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    for lado in ("start", "end", "top", "bottom"):
        getattr(caja, "set_margin_" + lado)(14)
    win.add(caja)

    cab = Gtk.Box(spacing=8)
    t = Gtk.Label(label="Ajustes del GamePad", xalign=0)
    t.set_hexpand(True)
    cerrar = Gtk.Button(label="✕")
    cerrar.get_style_context().add_class("cerrar")
    cerrar.connect("clicked", lambda *_: Gtk.main_quit())
    cab.pack_start(t, True, True, 0)
    cab.pack_end(cerrar, False, False, 0)
    caja.pack_start(cab, False, False, 0)

    interruptores = {}

    def fila(clave, texto, sub=None):
        f = Gtk.Box(spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.pack_start(Gtk.Label(label=texto, xalign=0), False, False, 0)
        if sub:
            s = Gtk.Label(label=sub, xalign=0)
            s.get_style_context().add_class("dim")
            col.pack_start(s, False, False, 0)
        sw = Gtk.Switch()
        sw.set_valign(Gtk.Align.CENTER)
        f.pack_start(col, True, True, 0)
        f.pack_end(sw, False, False, 0)
        caja.pack_start(f, False, False, 0)
        interruptores[clave] = sw
        return sw

    sw_ir = fila("ir", "Luces IR", "barra de sensores")
    sw_nfc = fila("nfc", "Lector NFC")
    sw_wifi = fila("wifi", "WiFi interna", "apagado = USB")

    # brillo: «−  ●●●○○  +»
    fb = Gtk.Box(spacing=6)
    menos, mas = Gtk.Button(label="−"), Gtk.Button(label="+")
    puntos = Gtk.Label(label=circulos(estado["brillo"]))
    puntos.get_style_context().add_class("puntos")
    fb.pack_start(menos, False, False, 0)
    fb.pack_start(puntos, True, True, 0)
    fb.pack_end(mas, False, False, 0)
    caja.pack_start(fb, False, False, 0)

    # confirmación del cambio de enlace
    conf = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    msg = Gtk.Label(label="", xalign=0)
    msg.set_line_wrap(True)
    msg.set_max_width_chars(26)
    fbot = Gtk.Box(spacing=8)
    b_no, b_si = Gtk.Button(label="Cancelar"), Gtk.Button(label="Cambiar")
    b_si.get_style_context().add_class("peligro")
    fbot.pack_start(b_no, True, True, 0)
    fbot.pack_start(b_si, True, True, 0)
    conf.pack_start(msg, False, False, 0)
    conf.pack_start(fbot, False, False, 0)
    caja.pack_start(conf, False, False, 0)

    guardia = {"on": False}   # evita que poner el interruptor por código dispare su acción

    def pintar():
        guardia["on"] = True
        sw_ir.set_active(estado["ir"])
        sw_nfc.set_active(estado["nfc"])
        sw_wifi.set_active(estado["interno"])
        puntos.set_text(circulos(estado["brillo"]))
        guardia["on"] = False

    def en_hilo(trabajo, fin=None):
        def run():
            trabajo()
            GLib.idle_add(lambda: (fin() if fin else None, False)[1])
        threading.Thread(target=run, daemon=True).start()

    def refrescar():
        def tarea():
            estado.update(leer_estado())
        en_hilo(tarea, pintar)

    def al_cambiar_ir(sw, _p):
        if guardia["on"]:
            return
        estado["ir"] = sw.get_active()
        en_hilo(lambda: drc_pad("ir", "on" if sw.get_active() else "off"), refrescar)

    def al_cambiar_nfc(sw, _p):
        if guardia["on"]:
            return
        estado["nfc"] = sw.get_active()
        en_hilo(lambda: drc_pad("nfc", "on" if sw.get_active() else "off"), refrescar)

    destino = {"v": None}

    def al_cambiar_wifi(sw, _p):
        if guardia["on"]:
            return
        destino["v"] = "internal" if sw.get_active() else "usb"
        msg.set_text("Pasar a la WiFi %s corta la imagen ~1 min. ¿Cambiar?" % ("interna" if sw.get_active() else "USB"))
        conf.show_all()
        pintar()   # el interruptor vuelve a su sitio hasta confirmar

    def cancelar(*_):
        conf.hide()
        destino["v"] = None

    def confirmar(*_):
        d = destino["v"]
        conf.hide()
        if d:
            en_hilo(lambda: drc_pad("mode", d))
            win.hide()
            GLib.timeout_add(500, Gtk.main_quit)

    def brillo(sentido):
        def go(*_):
            if estado["brillo"] is None:
                return
            estado["brillo"] = max(1, min(MAX_BRILLO, estado["brillo"] + sentido))
            puntos.set_text(circulos(estado["brillo"]))
            en_hilo(lambda: drc_pad("brightness", "up" if sentido > 0 else "down"), refrescar)
        return go

    sw_ir.connect("notify::active", al_cambiar_ir)
    sw_nfc.connect("notify::active", al_cambiar_nfc)
    sw_wifi.connect("notify::active", al_cambiar_wifi)
    b_no.connect("clicked", cancelar)
    b_si.connect("clicked", confirmar)
    menos.connect("clicked", brillo(-1))
    mas.connect("clicked", brillo(+1))
    win.connect("destroy", Gtk.main_quit)

    pintar()
    win.set_default_size(300, -1)
    win.show_all()
    conf.hide()
    Gtk.main()


if __name__ == "__main__":
    main()
