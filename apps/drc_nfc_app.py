#!/usr/bin/env python3
"""App táctil de NFC del GamePad: leer y escribir etiquetas NDEF (texto o URL) y ver el UID.

Se lanza desde el menú: `env SDL_VIDEODRIVER=wayland python3 /opt/drc/apps/drc_nfc_app.py` (no necesita root).
Las etiquetas válidas son NFC Forum tipo 4 (p. ej. un móvil con la app android_nfc, o tarjetas ISO-DEP).
"""
import os
import sys
import threading

import pygame

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui  # noqa: E402
import drc_nfc  # noqa: E402

TABS = ["Leer", "Escribir", "Detectar"]


class Estado:
    def __init__(self):
        self.tab = 0
        self.busy = ""             # texto de lo que está haciendo ("" = libre)
        self.msg = ("Elige una acción y acerca la etiqueta a la marca NFC del mando.", ui.DIM)
        self.lectura = None        # último resultado de leer
        self.uid = None            # último resultado de detectar
        self.tipo = 0              # 0 = texto, 1 = URL
        self.valor = ""            # contenido a escribir
        self.campo = False         # teclado visible
        self.t0 = 0.0


def lanzar(estado, trabajo):
    """Ejecuta `trabajo` en otro hilo para no congelar la pantalla."""
    def run():
        try:
            trabajo()
        except Exception as e:   # el mando puede no estar: se muestra, no se cierra la app
            estado.msg = ("Error: %s" % e, ui.ERR)
        finally:
            estado.busy = ""
    threading.Thread(target=run, daemon=True).start()


def accion_leer(estado, nfc):
    estado.busy = "Esperando etiqueta… (30 s)"
    estado.t0 = pygame.time.get_ticks()
    r = nfc.read_ndef(timeout_s=30)
    if r["ok"]:
        estado.lectura = r["registros"]
        n = len(r["registros"])
        estado.msg = ("Etiqueta leída: %s." % ("vacía" if n == 0 else "%d registro%s" % (n, "s" if n != 1 else "")), ui.OK)
    else:
        estado.lectura = None
        estado.msg = (r["error"], ui.ERR)


def accion_escribir(estado, nfc):
    contenido = estado.valor.strip()
    if not contenido:
        estado.msg = ("Escribe algo primero.", ui.WARN)
        return
    estado.busy = "Acerca la etiqueta para escribir… (30 s)"
    estado.t0 = pygame.time.get_ticks()
    msg = drc_nfc.ndef_url(contenido) if estado.tipo == 1 else drc_nfc.ndef_text(contenido)
    r = nfc.write_ndef(msg, timeout_s=30)
    if r["ok"]:
        estado.msg = ("Escrito (%d bytes). Pulsa Leer para comprobarlo." % r["bytes"], ui.OK)
    else:
        estado.msg = (r["error"], ui.ERR)


def accion_detectar(estado, nfc):
    estado.busy = "Buscando etiqueta… (15 s)"
    estado.t0 = pygame.time.get_ticks()
    d = nfc.detect(timeout_s=15)
    estado.uid = d
    estado.msg = (("Etiqueta detectada." if d["presente"] else "No se detectó ninguna etiqueta."), ui.OK if d["presente"] else ui.WARN)


def dibujar(pantalla, estado, botones, teclado):
    pantalla.fill(ui.BG)
    ui.text(pantalla, "NFC del GamePad", (14, 10), 24, ui.TEXT, True)
    botones["salir"].draw(pantalla, color=ui.ERR)
    for i, b in enumerate(botones["tabs"]):
        b.draw(pantalla, active=(i == estado.tab))

    col = ui.WARN if estado.busy else estado.msg[1]
    txt = estado.busy or estado.msg[0]
    if estado.busy:
        seg = (pygame.time.get_ticks() - estado.t0) // 1000
        txt += "  [%d s]" % seg
    y_msg = 420 if (estado.tab != 1 or not estado.campo) else 200
    if estado.tab == 0:
        ui.panel(pantalla, (12, 108, 250, 74), ui.PANEL)
        botones["leer"].draw(pantalla)
        ui.text(pantalla, "Acerca la etiqueta a la marca NFC del mando (bajo el stick derecho).", (12, 192), 14, ui.DIM, maxw=250)
        ui.panel(pantalla, (278, 108, 564, 300), ui.PANEL)
        if estado.lectura is None:
            ui.text(pantalla, "Aquí aparecerá lo que haya en la etiqueta.", (296, 124), 16, ui.DIM, maxw=530)
        elif not estado.lectura:
            ui.text(pantalla, "La etiqueta está vacía.", (296, 124), 18, ui.TEXT)
        else:
            y = 120
            for r in estado.lectura[:4]:
                y = ui.text(pantalla, {"texto": "TEXTO", "url": "URL", "mime": "DATOS"}.get(r["tipo"], "OTRO"), (296, y), 13, ui.ACCENT, True)
                y = ui.text(pantalla, r["valor"], (296, y), 18, ui.TEXT, maxw=530, maxlines=3) + 8
            if estado.lectura and estado.lectura[0]["tipo"] in ("texto", "url"):
                botones["copiar"].draw(pantalla)
    elif estado.tab == 1:
        botones["tipo"][0].draw(pantalla, active=(estado.tipo == 0))
        botones["tipo"][1].draw(pantalla, active=(estado.tipo == 1))
        ui.panel(pantalla, (12, 150, 830, 42), ui.PANEL, border=ui.ACCENT if estado.campo else None)
        if estado.valor:
            ui.text(pantalla, estado.valor[-60:] + ("|" if estado.campo else ""), (24, 160), 20, ui.TEXT)
        else:
            ui.text(pantalla, "Toca aquí y escribe el %s…" % ("texto" if estado.tipo == 0 else "enlace"), (24, 160), 18, ui.DIM)
        botones["escribir"].draw(pantalla, color=ui.OK)
    else:
        botones["detectar"].draw(pantalla)
        ui.panel(pantalla, (278, 108, 564, 300), ui.PANEL)
        d = estado.uid
        if d is None:
            ui.text(pantalla, "Muestra el identificador (UID) de la etiqueta.", (296, 124), 16, ui.DIM, maxw=530)
        elif not d["presente"]:
            ui.text(pantalla, "Sin etiqueta.", (296, 124), 20, ui.WARN, True)
        else:
            y = ui.text(pantalla, "UID", (296, 124), 13, ui.ACCENT, True)
            y = ui.text(pantalla, d["uid"], (296, y), 28, ui.TEXT, True) + 10
            y = ui.text(pantalla, "Solo lectura: %s" % ("sí" if d["solo_lectura"] else "no"), (296, y), 16, ui.TEXT)
            ui.text(pantalla, "Bloqueada: %s" % ("sí" if d["bloqueada"] else "no"), (296, y), 16, ui.TEXT)

    if estado.tab == 1:
        ui.text(pantalla, txt, (276, y_msg - 2 if estado.campo else 204), 15, col, maxw=566, maxlines=2)
    else:
        ui.text(pantalla, txt, (14, y_msg), 16, col, maxw=826, maxlines=2)
    if estado.tab == 1 and estado.campo:
        teclado.draw(pantalla)


def main():
    pygame.display.init()   # sin pygame.init(): el mezclador de audio abriría el ALSA del GamePad (underruns)
    pygame.font.init()
    pygame.display.set_mode((ui.W, ui.H), pygame.RESIZABLE)   # el WM da menos alto (barra): se escala el lienzo
    lienzo = ui.Lienzo()
    pantalla = lienzo.canvas
    pygame.display.set_caption("NFC")
    reloj = pygame.time.Clock()
    estado = Estado()
    nfc = drc_nfc.Nfc(drc_nfc.PadLink())
    teclado = ui.Keyboard()
    botones = {
        "salir": ui.Button((756, 8, 90, 38), "Salir"),
        "tabs": [ui.Button((12 + i * 158, 56, 150, 42), t) for i, t in enumerate(TABS)],
        "leer": ui.Button((16, 112, 242, 66), "LEER ETIQUETA", size=19),
        "copiar": ui.Button((296, 360, 270, 40), "Usar en Escribir", size=15),
        "tipo": [ui.Button((12, 108, 120, 34), "Texto", size=16), ui.Button((140, 108, 120, 34), "URL", size=16)],
        "escribir": ui.Button((12, 198, 250, 48), "ESCRIBIR EN ETIQUETA", size=16),
        "detectar": ui.Button((16, 112, 242, 66), "BUSCAR ETIQUETA", size=19),
    }
    corriendo = True
    while corriendo:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                corriendo = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    corriendo = False
                elif estado.tab == 1 and estado.campo:
                    if ev.key == pygame.K_BACKSPACE:
                        estado.valor = ui.aplicar_tecla(estado.valor, "\b")
                    elif ev.unicode and ev.unicode.isprintable():
                        estado.valor += ev.unicode
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                p = lienzo.pos(ev.pos)
                if botones["salir"].hit(p):
                    corriendo = False
                    continue
                for i, b in enumerate(botones["tabs"]):
                    if b.hit(p) and not estado.busy:
                        estado.tab, estado.campo = i, False
                if estado.busy:
                    continue
                if estado.tab == 0:
                    if botones["leer"].hit(p):
                        lanzar(estado, lambda: accion_leer(estado, nfc))
                    elif botones["copiar"].hit(p) and estado.lectura:
                        estado.valor, estado.tipo, estado.tab = estado.lectura[0]["valor"], (1 if estado.lectura[0]["tipo"] == "url" else 0), 1
                elif estado.tab == 1:
                    if estado.campo and p[1] >= teclado.top:
                        k = teclado.press(p)
                        estado.valor = ui.aplicar_tecla(estado.valor, k)
                        if k == "\n":
                            estado.campo = False
                    elif pygame.Rect(12, 150, 830, 42).collidepoint(p):
                        estado.campo = True
                    elif botones["escribir"].hit(p):
                        estado.campo = False
                        lanzar(estado, lambda: accion_escribir(estado, nfc))
                    else:
                        for i, b in enumerate(botones["tipo"]):
                            if b.hit(p):
                                estado.tipo = i
                else:
                    if botones["detectar"].hit(p):
                        lanzar(estado, lambda: accion_detectar(estado, nfc))
        dibujar(pantalla, estado, botones, teclado)
        lienzo.presentar()
        reloj.tick(30)
    pygame.quit()


if __name__ == "__main__":
    main()
