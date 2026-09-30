"""
palette.py - Popup de paleta: rueda de color + brillo + tamano (brush y borrador).

Modelo + dibujo + interaccion en un solo lugar. No conoce camara ni gestos:
recibe eventos de puntero ("down" / "move" / "up") con coordenadas Board,
vengan del mouse o del dedo indice (pinch = clic, lo traduce main.py).

Estado que usa el resto del programa:
    palette.color            (r, g, b) del brush
    palette.size             ancho del brush en px
    palette.erase_radius     radio del borrador en px (escala con `size`)
    palette.change_size(d)   para las teclas + y -
"""

import colorsys
import math

import numpy as np
import pygame

# colores propios (chalk.py importa este modulo, asi que no se puede importar de alla)
BG = (18, 20, 24)
FG = (230, 232, 236)
ACCENT = (80, 200, 255)
DIM = (120, 126, 138)
FRAME = (40, 44, 52)
WARN = (255, 170, 60)

SIZE_MIN, SIZE_MAX = 2, 24
DEFAULT_SIZE = 5                  # ancho de trazo original
ERASE_PER_SIZE = 5.6              # 5 px de brush -> 28 px de borrador (valores originales)
VAL_MIN = 0.25                    # brillo minimo: por debajo no se ve sobre el fondo oscuro

PRESETS = [
    (230, 232, 236), (240, 90, 90), (255, 170, 60), (250, 225, 80),
    (90, 220, 130), (80, 200, 255), (120, 120, 255), (220, 110, 230),
]


def _rgb255(rgb01):
    return tuple(int(round(v * 255)) for v in rgb01)


def _make_wheel(radius, val):
    """Rueda HSV (tono = angulo, saturacion = radio) con borde suavizado."""
    d = radius * 2
    y, x = np.mgrid[0:d, 0:d]
    dx, dy = x - radius + 0.5, y - radius + 0.5
    dist = np.hypot(dx, dy)
    h = (np.arctan2(dy, dx) / (2 * np.pi)) % 1.0
    s = np.clip(dist / radius, 0, 1)

    h6 = h * 6
    i = np.floor(h6).astype(int) % 6
    f = h6 - np.floor(h6)
    v = np.full_like(s, val)
    p, q, t = val * (1 - s), val * (1 - f * s), val * (1 - (1 - f) * s)
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    rgb = (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)
    alpha = (np.clip(radius - dist, 0, 1) * 255).astype(np.uint8)

    surf = pygame.Surface((d, d), pygame.SRCALPHA)
    px = pygame.surfarray.pixels3d(surf)
    px[:] = rgb.swapaxes(0, 1)
    del px
    pa = pygame.surfarray.pixels_alpha(surf)
    pa[:] = alpha.swapaxes(0, 1)
    del pa
    return surf


def _text(surf, font, text, pos, color=FG, center=False):
    img = font.render(text, True, color)
    rect = img.get_rect()
    if center:
        rect.center = pos
    else:
        rect.topleft = pos
    surf.blit(img, rect)


class Palette:
    W, H, R = 840, 470, 150

    def __init__(self, screen_w, screen_h, color=FG, size=DEFAULT_SIZE):
        self.px = (screen_w - self.W) // 2
        self.py = (screen_h - self.H) // 2
        self.wheel_c = (self.px + 50 + self.R, self.py + 90 + self.R)
        rx, rw = self.px + 400, 400
        self.close_rect = pygame.Rect(self.px + self.W - 52, self.py + 14, 38, 38)
        self.preview_rect = pygame.Rect(rx, self.py + 90, rw, 90)
        self.val_rect = pygame.Rect(rx, self.py + 235, rw, 14)
        self.size_rect = pygame.Rect(rx, self.py + 305, rw, 14)
        self.preset_rects = [pygame.Rect(rx + i * 48, self.py + 350, 40, 40)
                             for i in range(len(PRESETS))]
        self.size = size
        self._drag = None
        self._wheel = (None, None)        # (clave de brillo, superficie)
        self.set_color(color)

    # -- estado ------------------------------------------------------------------
    def set_color(self, rgb):
        self.hue, self.sat, self.val = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        self.val = max(self.val, VAL_MIN)

    @property
    def color(self):
        return _rgb255(colorsys.hsv_to_rgb(self.hue, self.sat, self.val))

    @property
    def erase_radius(self):
        return max(int(round(self.size * ERASE_PER_SIZE)), 4)

    def change_size(self, delta):
        self.size = min(max(self.size + delta, SIZE_MIN), SIZE_MAX)

    # -- interaccion --------------------------------------------------------------
    def _hit(self, pos):
        cx, cy = self.wheel_c
        if math.hypot(pos[0] - cx, pos[1] - cy) <= self.R + 6:
            return "wheel"
        if self.val_rect.inflate(0, 28).collidepoint(pos):
            return "val"
        if self.size_rect.inflate(0, 28).collidepoint(pos):
            return "size"
        for i, r in enumerate(self.preset_rects):
            if r.collidepoint(pos):
                return i
        return None

    def _apply_drag(self, pos):
        if self._drag == "wheel":
            cx, cy = self.wheel_c
            dx, dy = pos[0] - cx, pos[1] - cy
            self.hue = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
            self.sat = min(math.hypot(dx, dy) / self.R, 1.0)
        elif self._drag == "val":
            t = min(max((pos[0] - self.val_rect.x) / self.val_rect.w, 0), 1)
            self.val = VAL_MIN + (1 - VAL_MIN) * t
        elif self._drag == "size":
            t = min(max((pos[0] - self.size_rect.x) / self.size_rect.w, 0), 1)
            self.size = int(round(SIZE_MIN + t * (SIZE_MAX - SIZE_MIN)))

    def pointer(self, kind, pos):
        """kind: "down" | "move" | "up". Devuelve True si se pidio cerrar el popup."""
        if kind == "up":
            self._drag = None
            return False
        if kind == "down":
            if self.close_rect.collidepoint(pos):
                return True
            hit = self._hit(pos)
            if isinstance(hit, int):
                self.set_color(PRESETS[hit])
                self._drag = None
            else:
                self._drag = hit
        if self._drag is not None:
            self._apply_drag(pos)
        return False

    # -- dibujo -------------------------------------------------------------------
    def _wheel_surface(self):
        key = round(self.val * 40)
        if self._wheel[0] != key:
            self._wheel = (key, _make_wheel(self.R, key / 40))
        return self._wheel[1]

    def _knob(self, surf, x, y, fill=None):
        if fill is not None:
            pygame.draw.circle(surf, fill, (x, y), 10)
        pygame.draw.circle(surf, FG, (x, y), 11, 3)

    def draw(self, surf, font, font_big, font_small):
        px, py, W, H = self.px, self.py, self.W, self.H
        pygame.draw.rect(surf, BG, (px, py, W, H), border_radius=14)
        pygame.draw.rect(surf, ACCENT, (px, py, W, H), 2, border_radius=14)
        _text(surf, font_big, "Paleta", (px + W // 2, py + 42), FG, center=True)

        # cerrar
        cr = self.close_rect
        pygame.draw.rect(surf, FRAME, cr, border_radius=8)
        pygame.draw.line(surf, FG, (cr.x + 11, cr.y + 11), (cr.right - 11, cr.bottom - 11), 3)
        pygame.draw.line(surf, FG, (cr.x + 11, cr.bottom - 11), (cr.right - 11, cr.y + 11), 3)

        # rueda
        cx, cy = self.wheel_c
        surf.blit(self._wheel_surface(), (cx - self.R, cy - self.R))
        pygame.draw.circle(surf, FRAME, (cx, cy), self.R + 2, 2)
        ang = self.hue * 2 * math.pi
        kx = int(cx + math.cos(ang) * self.sat * self.R)
        ky = int(cy + math.sin(ang) * self.sat * self.R)
        self._knob(surf, kx, ky, self.color)

        # vista previa del trazo
        pr = self.preview_rect
        pygame.draw.rect(surf, (10, 11, 14), pr, border_radius=8)
        pygame.draw.rect(surf, FRAME, pr, 2, border_radius=8)
        pts = [(pr.x + 30 + i * 6, pr.centery + int(math.sin(i * 0.25) * 18))
               for i in range(60)]
        pygame.draw.lines(surf, self.color, False, pts, self.size)
        for p in pts:
            pygame.draw.circle(surf, self.color, p, max(self.size // 2, 1))

        # brillo
        vr = self.val_rect
        _text(surf, font_small, "Brillo", (vr.x, vr.y - 30), DIM)
        for i in range(0, vr.w, 2):
            col = _rgb255(colorsys.hsv_to_rgb(
                self.hue, self.sat, VAL_MIN + (1 - VAL_MIN) * i / vr.w))
            pygame.draw.rect(surf, col, (vr.x + i, vr.y, 2, vr.h))
        t = (self.val - VAL_MIN) / (1 - VAL_MIN)
        self._knob(surf, int(vr.x + t * vr.w), vr.centery)

        # tamano
        sr = self.size_rect
        _text(surf, font_small,
              f"Tamano: {self.size} px   (borrador: {self.erase_radius} px)",
              (sr.x, sr.y - 30), DIM)
        t = (self.size - SIZE_MIN) / (SIZE_MAX - SIZE_MIN)
        pygame.draw.rect(surf, FRAME, sr, border_radius=7)
        pygame.draw.rect(surf, ACCENT, (sr.x, sr.y, int(t * sr.w), sr.h), border_radius=7)
        self._knob(surf, int(sr.x + t * sr.w), sr.centery)

        # colores rapidos
        for rect, col in zip(self.preset_rects, PRESETS):
            pygame.draw.rect(surf, col, rect, border_radius=8)
            if col == self.color:
                pygame.draw.rect(surf, FG, rect.inflate(6, 6), 2, border_radius=10)

        _text(surf, font_small,
              "rueda / sliders: mouse o pinch   + / -: tamano   P/ESC: cerrar",
              (px + W // 2, py + H - 28), DIM, center=True)
