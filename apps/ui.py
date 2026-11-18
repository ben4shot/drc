"""Widgets mínimos de pygame para las apps táctiles del GamePad (854x480): botones, texto y teclado en pantalla."""
import pygame

W, H = 854, 480

BG = (12, 9, 20)
PANEL = (34, 23, 56)
PANEL2 = (52, 36, 84)
ACCENT = (124, 58, 237)
TEXT = (243, 232, 255)
DIM = (150, 135, 180)
OK = (46, 196, 182)
WARN = (255, 214, 10)
ERR = (230, 57, 70)

_fonts = {}


def ajuste_ventana(ww, wh):
    """Dónde cae el lienzo lógico (W x H) en una ventana de ww x wh conservando la proporción: (x, y, ancho, alto).
    El WM da a las apps menos alto que la pantalla (la barra ocupa 30 px) y SDL no escala por su cuenta."""
    s = min(ww / W, wh / H)
    w, h = max(1, int(W * s)), max(1, int(H * s))
    return (ww - w) // 2, (wh - h) // 2, w, h


def a_logica(pos, ww, wh):
    """Toque/ratón en la ventana -> coordenadas del lienzo lógico (recortadas al lienzo)."""
    ox, oy, w, h = ajuste_ventana(ww, wh)
    x = int((pos[0] - ox) * W / w)
    y = int((pos[1] - oy) * H / h)
    return max(0, min(W - 1, x)), max(0, min(H - 1, y))


class Lienzo:
    """Superficie lógica de W x H donde dibuja la app; `presentar()` la escala al tamaño real de la ventana."""

    def __init__(self, rapido=False):
        # rapido: escalado sin suavizar (0,8 ms en la Pi frente a 12 ms): para vídeo a 30 fps
        self.canvas = pygame.Surface((W, H))
        self.rapido = rapido
        self._destino = None

    def presentar(self, flip=True):
        win = pygame.display.get_surface()
        ww, wh = win.get_size()
        ox, oy, w, h = ajuste_ventana(ww, wh)
        if (ww, wh) == (W, H):
            win.blit(self.canvas, (0, 0))
        elif self.rapido:
            if self._destino is None or self._destino.get_size() != (w, h):
                self._destino = pygame.Surface((w, h))
                win.fill(BG)
            pygame.transform.scale(self.canvas, (w, h), self._destino)
            win.blit(self._destino, (ox, oy))
        else:
            win.fill(BG)
            win.blit(pygame.transform.smoothscale(self.canvas, (w, h)), (ox, oy))
        if flip:
            pygame.display.flip()

    def pos(self, p):
        ww, wh = pygame.display.get_surface().get_size()
        return a_logica(p, ww, wh)


def font(size, bold=False):
    k = (size, bold)
    if k not in _fonts:
        _fonts[k] = pygame.font.SysFont("dejavusans,freesans,arial", size, bold=bold)
    return _fonts[k]


def wrap(text, fnt, maxw):
    """Parte `text` en líneas que quepan en `maxw` px (respeta los saltos de línea)."""
    out = []
    for parrafo in str(text).split("\n"):
        linea = ""
        for palabra in parrafo.split(" "):
            prueba = palabra if not linea else linea + " " + palabra
            if fnt.size(prueba)[0] <= maxw or not linea:
                linea = prueba
                while fnt.size(linea)[0] > maxw and len(linea) > 1:   # palabra más larga que la línea
                    cut = len(linea)
                    while cut > 1 and fnt.size(linea[:cut])[0] > maxw:
                        cut -= 1
                    out.append(linea[:cut])
                    linea = linea[cut:]
            else:
                out.append(linea)
                linea = palabra
        out.append(linea)
    return out


def text(surf, s, pos, size=16, color=TEXT, bold=False, maxw=None, center=False, line_gap=3, maxlines=None):
    f = font(size, bold)
    lines = wrap(s, f, maxw) if maxw else [str(s)]
    if maxlines:
        lines = lines[:maxlines]
    x, y = pos
    for ln in lines:
        img = f.render(ln, True, color)
        r = img.get_rect()
        if center:
            r.midtop = (x, y)
        else:
            r.topleft = (x, y)
        surf.blit(img, r)
        y += img.get_height() + line_gap
    return y


def panel(surf, rect, color=PANEL, radius=10, border=None):
    pygame.draw.rect(surf, color, rect, border_radius=radius)
    if border:
        pygame.draw.rect(surf, border, rect, width=2, border_radius=radius)


class Button:
    def __init__(self, rect, label, key=None, color=PANEL2, fg=TEXT, size=17):
        self.rect = pygame.Rect(rect)
        self.label, self.key, self.color, self.fg, self.size = label, key or label, color, fg, size
        self.enabled = True

    def hit(self, pos):
        return self.enabled and self.rect.collidepoint(pos)

    def draw(self, surf, active=False, color=None):
        col = ACCENT if active else (color or self.color)
        if not self.enabled:
            col = PANEL
        panel(surf, self.rect, col, 10)
        img = font(self.size, True).render(self.label, True, self.fg if self.enabled else DIM)
        surf.blit(img, img.get_rect(center=self.rect.center))


class Keyboard:
    """Teclado QWERTY táctil. `press(pos)` devuelve el carácter, '\\b' (borrar), '\\n' (aceptar) o None."""
    LETRAS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"]
    SIMBOLOS = ["1234567890", "-_./:@#&?!", "()=+%*;,'\"", "~<>[]{}|\\^"]

    def __init__(self, top=258, height=222):
        self.top, self.h = top, height
        self.mayus = False
        self.simbolos = False
        self.keys = []        # (rect, valor, etiqueta)
        self._layout()

    def _layout(self):
        self.keys = []
        filas = self.SIMBOLOS if self.simbolos else self.LETRAS
        rh = (self.h - 8) // 5
        for r, fila in enumerate(filas):
            kw = 64 if r == 3 else 78          # la 4.ª fila deja sitio a ⇧ y ⌫ a los lados
            x0 = (W - len(fila) * kw) // 2
            for i, ch in enumerate(fila):
                lab = ch.upper() if (self.mayus and not self.simbolos) else ch
                self.keys.append((pygame.Rect(x0 + i * kw + 2, self.top + 4 + r * rh + 2, kw - 4, rh - 4), lab, lab))
        y = self.top + 4 + 3 * rh + 2
        self.keys.append((pygame.Rect(12, y, 86, rh - 4), "\x01", "⇧" if not self.simbolos else "·"))
        self.keys.append((pygame.Rect(W - 12 - 86, y, 86, rh - 4), "\b", "⌫"))
        y = self.top + 4 + 4 * rh + 2
        self.keys.append((pygame.Rect(12, y, 110, rh - 4), "\x02", "abc" if self.simbolos else "123"))
        self.keys.append((pygame.Rect(130, y, 440, rh - 4), " ", "espacio"))
        self.keys.append((pygame.Rect(578, y, 80, rh - 4), "/", "/"))
        self.keys.append((pygame.Rect(666, y, 80, rh - 4), ".", "."))
        self.keys.append((pygame.Rect(754, y, 88, rh - 4), "\n", "OK"))

    def press(self, pos):
        for rect, val, _ in self.keys:
            if rect.collidepoint(pos):
                if val == "\x01":
                    self.mayus = not self.mayus
                    self._layout()
                    return None
                if val == "\x02":
                    self.simbolos = not self.simbolos
                    self._layout()
                    return None
                return val
        return None

    def draw(self, surf):
        pygame.draw.rect(surf, (20, 14, 34), (0, self.top, W, self.h))
        for rect, val, lab in self.keys:
            col = ACCENT if val == "\n" else (PANEL2 if val in ("\b", "\x01", "\x02") else PANEL)
            panel(surf, rect, col, 8)
            img = font(18, True).render(lab, True, TEXT)
            surf.blit(img, img.get_rect(center=rect.center))


def aplicar_tecla(valor, tecla):
    """Aplica una tecla del teclado al texto: borra, ignora el salto de línea y añade el resto."""
    if tecla == "\b":
        return valor[:-1]
    if tecla in ("\n", None):
        return valor
    return valor + tecla
