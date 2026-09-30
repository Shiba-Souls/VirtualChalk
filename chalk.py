"""
chalk.py - Todo lo que tiene que ver con DIBUJAR.

Contiene:
    Board          superficie persistente con los trazos (dibujar / borrar / limpiar)
    ChalkRenderer  ventana pygame + render de pizarra, cursores, calibracion y HUD

No sabe nada de camara, MediaPipe ni gestos: recibe datos ya calculados
(posiciones en coordenadas Board, estados como strings) y los pinta.
Quien decide QUE dibujar y CUANDO es main.py.
"""

import time

import pygame

from palette import DEFAULT_SIZE, ERASE_PER_SIZE  # noqa: F401

# --- Parametros del Board ---------------------------------------------------
BOARD_W, BOARD_H = 1280, 720          # resolucion interna del render
MARGIN = 90                           # distancia de los marcadores a las esquinas
ERASE_RADIUS = 28                     # radio del borrador en px del Board
STROKE_WIDTH = 5

# --- Colores -----------------------------------------------------------------
BG = (18, 20, 24)                     # fondo oscuro: menos interferencia con la camara
FG = (230, 232, 236)
ACCENT = (80, 200, 255)
OK = (90, 220, 130)
WARN = (255, 170, 60)
BAD = (240, 90, 90)
DIM = (120, 126, 138)
FRAME = (40, 44, 52)


def calibration_targets():
    """Los 4 puntos objetivo en espacio Board: TL, TR, BR, BL."""
    return [
        (MARGIN, MARGIN),
        (BOARD_W - MARGIN, MARGIN),
        (BOARD_W - MARGIN, BOARD_H - MARGIN),
        (MARGIN, BOARD_H - MARGIN),
    ]


# =============================================================================
# Board: la pizarra persistente
# =============================================================================
class Board:
    def __init__(self, w=BOARD_W, h=BOARD_H, bg_color=BG):
        self.w = w
        self.h = h
        self.bg_color = bg_color
        self.surface = pygame.Surface((w, h))
        self.clear()

    def clear(self):
        self.surface.fill(self.bg_color)

    def draw_stroke(self, color, points, width=STROKE_WIDTH):
        if len(points) < 2:
            return
        pygame.draw.lines(self.surface, color, False, points, width)
        if width > 3:                       # puntas redondas: sin huecos entre segmentos
            r = width // 2
            for p in (points[0], points[-1]):
                pygame.draw.circle(self.surface, color, (int(p[0]), int(p[1])), r)

    def erase(self, points, radius=ERASE_RADIUS):
        """Borra con un pincel circular del color de fondo a lo largo de `points`."""
        if not points:
            return
        pts = [(int(x), int(y)) for x, y in points]
        if len(pts) >= 2:
            pygame.draw.lines(self.surface, self.bg_color, False, pts, radius * 2)
        for p in pts:                       # circulos en los extremos: sin huecos
            pygame.draw.circle(self.surface, self.bg_color, p, radius)


# =============================================================================
# Utilidades de dibujo
# =============================================================================
def draw_text(surf, font, text, pos, color=FG, center=False):
    img = font.render(text, True, color)
    rect = img.get_rect()
    if center:
        rect.center = pos
    else:
        rect.topleft = pos
    surf.blit(img, rect)


def draw_marker(surf, pos, radius, color, pulse=0.0):
    x, y = int(pos[0]), int(pos[1])
    pygame.draw.circle(surf, color, (x, y), radius, 3)
    pygame.draw.circle(surf, color, (x, y), max(radius // 5, 3))
    pygame.draw.line(surf, color, (x - radius - 14, y), (x + radius + 14, y), 2)
    pygame.draw.line(surf, color, (x, y - radius - 14), (x, y + radius + 14), 2)
    if pulse > 0:
        pygame.draw.circle(surf, color, (x, y), int(radius + 8 + pulse * 12), 1)


# =============================================================================
# Renderer: ventana + todas las pantallas
# =============================================================================
class ChalkRenderer:
    """
    Dueno de la ventana pygame y de todo el render.

    Estados de cursor (strings, para no depender de gestures.py):
        "draw"   pinch activo (dibujando)
        "erase"  borrador (gesto OK)
        "idle"   cualquier otro
    """

    def __init__(self, fullscreen=True):
        pygame.init()
        pygame.display.set_caption("VirtualChalk")
        self.fullscreen = fullscreen
        self._set_display()

        self.canvas = pygame.Surface((BOARD_W, BOARD_H))   # buffer de composicion
        self.board = Board()

        self.font = pygame.font.SysFont("segoeui", 26)
        self.font_big = pygame.font.SysFont("segoeui", 40, bold=True)
        self.font_small = pygame.font.SysFont("consolas", 18)
        self.clock = pygame.time.Clock()
        self.menu_rects = []          # rects de las filas del menu de camara (para el mouse)

    # -- ventana ---------------------------------------------------------------
    def _set_display(self):
        if self.fullscreen:
            # SCALED conserva la resolucion interna y la escala al monitor/proyector
            flags = pygame.FULLSCREEN | pygame.SCALED
        else:
            flags = pygame.RESIZABLE | pygame.SCALED
        self.screen = pygame.display.set_mode((BOARD_W, BOARD_H), flags, vsync=1)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self._set_display()

    def tick(self, fps=60):
        self.clock.tick(fps)

    @property
    def render_fps(self):
        return self.clock.get_fps()

    @staticmethod
    def poll_events():
        """Eventos pygame crudos; main.py decide que hacer con cada uno."""
        return pygame.event.get()

    @staticmethod
    def quit():
        pygame.quit()

    def menu_hit(self, pos):
        """Indice de la fila del menu de camara bajo `pos` (coords Board) o None."""
        for i, rect in enumerate(self.menu_rects):
            if rect.collidepoint(pos):
                return i
        return None

    # -- operaciones sobre la pizarra ---------------------------------------------
    def clear_board(self):
        self.board.clear()

    def stroke(self, p0, p1, color=FG, width=STROKE_WIDTH):
        self.board.draw_stroke(color, [p0, p1], width)

    def erase(self, points, radius=ERASE_RADIUS):
        self.board.erase(points, radius)

    # -- render principal --------------------------------------------------------------
    def render(self, mode, hand_present, stats,
               calib=None, cursors=None, clear_progress=0.0,
               pinch_value=None, pinching=False, menu=None,
               palette=None, pointer=None, brush=None, toast=None):
        """
        mode            "calibrating" | "running"
        hand_present    bool, para el indicador de mano
        stats           dict con cam_fps, latency_ms
        calib           dict {step, msg} (solo en calibracion)
        cursors         [(pos, estado_str)] en coordenadas Board (solo en running)
        clear_progress  0..1 de limpiar la pizarra
        pinch_value     valor de pinch a mostrar en calibracion (o None)
        pinching        True si el pinch esta activo (color del texto en calibracion)
        menu            dict {title, items, selected, current, status, hint} o None
                        (popup de lista: menu principal o camaras)
        palette         objeto Palette a dibujar como popup, o None
        pointer         posicion del puntero de la mano en coords Board (popups) o None
        brush           dict {color, size, erase_radius} del pincel actual
        toast           texto breve inferior (p. ej. cambio de tamano) o None
        """
        c = self.canvas
        c.fill(BG)
        c.blit(self.board.surface, (0, 0))

        if mode == "calibrating":
            self._render_calibration(c, calib or {}, pinch_value, pinching)
        else:
            self._render_run(c, cursors or [], clear_progress,
                             brush or {"color": FG, "size": DEFAULT_SIZE,
                                       "erase_radius": ERASE_RADIUS})

        self._render_status(c, hand_present, stats)
        self.menu_rects = []
        if menu is not None:
            self._render_menu(c, menu)
        elif palette is not None:
            self._veil(c)
            palette.draw(c, self.font, self.font_big, self.font_small)
        if toast:
            draw_text(c, self.font, toast, (BOARD_W // 2, BOARD_H - 70), ACCENT, center=True)
        if pointer is not None:
            x = int(min(max(pointer[0], 0), BOARD_W - 1))
            y = int(min(max(pointer[1], 0), BOARD_H - 1))
            pygame.draw.circle(c, ACCENT, (x, y), 14, 2)
            pygame.draw.circle(c, ACCENT, (x, y), 3)
        self.screen.blit(c, (0, 0))
        pygame.display.flip()

    # -- pantallas ------------------------------------------------------------------------
    def _render_calibration(self, c, calib, pinch_value, pinching):
        step = calib.get("step", 0)
        targets = calibration_targets()

        draw_text(c, self.font_big, "Calibracion", (BOARD_W // 2, 150), FG, center=True)
        draw_text(c, self.font, f"Punto {step + 1} de 4",
                  (BOARD_W // 2, 205), ACCENT, center=True)
        draw_text(c, self.font, calib.get("msg", ""),
                  (BOARD_W // 2, 250), FG, center=True)

        # marcadores ya confirmados
        for i in range(step):
            draw_marker(c, targets[i], 22, OK)

        # marcador activo con pulso
        if step < 4:
            pulse = (time.time() * 1.5) % 1.0
            draw_marker(c, targets[step], 26, ACCENT, pulse)

        if pinch_value is not None:
            draw_text(c, self.font_small, f"pinch={pinch_value:.2f}",
                      (BOARD_W // 2, 300), OK if pinching else FG, center=True)

    def _render_run(self, c, cursors, clear_progress, brush):
        # marco del area calibrada
        pygame.draw.rect(c, FRAME, (0, 0, BOARD_W, BOARD_H), 2)
        for p in calibration_targets():
            pygame.draw.circle(c, (60, 66, 78), (int(p[0]), int(p[1])), 6, 1)

        draw_text(c, self.font, "VirtualChalk", (24, 20), FG)
        draw_text(c, self.font_small,
                  "pinch: dibujar   OK: borrar   2 manos OK: limpiar   "
                  "M menu   P paleta   +/- tamano   C recalibrar   F fullscreen   ESC salir",
                  (24, 58), DIM)

        for pos, state in cursors:
            # se limita a los bordes visibles para que no se pierda fuera del Board
            cx = min(max(pos[0], 0), BOARD_W - 1)
            cy = min(max(pos[1], 0), BOARD_H - 1)
            if state == "erase":
                # circulo con el tamano real del borrador
                pygame.draw.circle(c, WARN, (int(cx), int(cy)), brush["erase_radius"], 2)
                continue
            inside = (0 <= pos[0] < BOARD_W) and (0 <= pos[1] < BOARD_H)
            col = ACCENT if inside else WARN
            pygame.draw.circle(c, col, (int(cx), int(cy)), 16, 3)
            pygame.draw.circle(c, brush["color"] if inside else col,
                               (int(cx), int(cy)), max(brush["size"] // 2, 4))
            if state == "draw":
                pygame.draw.circle(c, OK, (int(cx), int(cy)), 26, 3)

        # muestra del pincel actual (esquina inferior derecha)
        bx0, by0 = BOARD_W - 50, BOARD_H - 50
        pygame.draw.circle(c, DIM, (bx0, by0), 22, 1)
        pygame.draw.circle(c, brush["color"], (bx0, by0), min(brush["size"] // 2 + 1, 20))

        # progreso de "limpiar pizarra" (dos manos en OK sostenido)
        if clear_progress > 0:
            bw = 320
            bx = (BOARD_W - bw) // 2
            draw_text(c, self.font,
                      "Limpiando pizarra..." if clear_progress < 1.0 else "Pizarra limpia",
                      (BOARD_W // 2, 110), WARN, center=True)
            pygame.draw.rect(c, FRAME, (bx, 140, bw, 10), border_radius=5)
            pygame.draw.rect(c, WARN, (bx, 140, int(bw * clear_progress), 10),
                             border_radius=5)

    def _veil(self, c):
        veil = pygame.Surface((BOARD_W, BOARD_H), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 170))
        c.blit(veil, (0, 0))

    def _render_menu(self, c, menu):
        """Popup modal de lista (menu principal / camaras), encima de todo."""
        self._veil(c)

        items = menu.get("items", [])
        row_h, pw = 52, 760
        ph = 120 + max(len(items), 1) * row_h + 60
        px, py = (BOARD_W - pw) // 2, (BOARD_H - ph) // 2
        pygame.draw.rect(c, BG, (px, py, pw, ph), border_radius=14)
        pygame.draw.rect(c, ACCENT, (px, py, pw, ph), 2, border_radius=14)

        draw_text(c, self.font_big, menu.get("title", ""), (BOARD_W // 2, py + 42), FG, center=True)
        if menu.get("status"):
            draw_text(c, self.font_small, menu["status"],
                      (BOARD_W // 2, py + 88), WARN, center=True)

        self.menu_rects = []
        y = py + 120
        for i, label in enumerate(items):
            rect = pygame.Rect(px + 24, y, pw - 48, row_h - 8)
            pygame.draw.rect(c, FRAME, rect, border_radius=8)
            if i == menu.get("selected"):
                pygame.draw.rect(c, ACCENT, rect, 2, border_radius=8)
            active = i == menu.get("current")
            draw_text(c, self.font, label + ("   (activa)" if active else ""),
                      (rect.x + 16, rect.y + (rect.h - self.font.get_height()) // 2),
                      OK if active else FG)
            self.menu_rects.append(rect)
            y += row_h

        draw_text(c, self.font_small,
                  menu.get("hint", ""),
                  (BOARD_W // 2, py + ph - 28), DIM, center=True)

    def _render_status(self, c, hand_present, stats):
        """Indicador minimo de mano + metricas de rendimiento."""
        col = OK if hand_present else BAD
        pygame.draw.circle(c, col, (BOARD_W - 30, 30), 10)
        draw_text(c, self.font_small,
                  "mano" if hand_present else "sin mano",
                  (BOARD_W - 100, 22), col)
        text = (f"cam {stats.get('cam_fps', 0.0):4.1f} fps | "
                f"latencia {stats.get('latency_ms', 0.0):5.1f} ms | "
                f"render {self.render_fps:4.1f} fps")
        draw_text(c, self.font_small, text, (24, BOARD_H - 32), DIM)