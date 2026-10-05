"""
main.py - VirtualChalk: coordinador.

    track.py     camara + MediaPipe + calibracion (donde esta la mano, en el Board)
    gestures.py  que significa cada pose (pinch, OK)
    chalk.py     todo lo que se DIBUJA (pizarra, cursores, HUD, ventana)
    mouse.py     control del mouse de Windows (cursor, click, drag) via mano

main.py solo conecta las cuatro piezas.

Modos (plan v0.2, seccion 21-22):
    MOUSE   -> controlar Windows (cursor, click, drag, double click)
    CHALK   -> dibujar sobre la pizarra
    BOARD   -> trabajar dentro de VirtualChalk (popups, menu) - mismo que CHALK

Ejecucion:
    python main.py            # uso normal (fullscreen en el proyector duplicado)
    python main.py --debug    # ventana normal + vista de camara con landmarks

Teclas (siempre disponibles, son de desarrollo):
    ESC   salir (o recuperar Mouse Mode)
    U     alternar Mouse Mode (seccion 22 del plan)
    M     menu popup (camara, paleta, recalibrar, fullscreen, limpiar)
    P     popup de paleta: rueda de color + brillo + tamano (mouse o pinch)
    + / - tamano del brush y del borrador (tambien dentro de la paleta)
    F     alternar fullscreen
    D     alternar vista de camara (solo con --debug)
"""

import argparse
import platform
import sys
import threading
import time

import cv2
import numpy as np
import pygame

from chalk import BOARD_W, BOARD_H, ChalkRenderer, calibration_targets
from gestures import GestureDetector, GestureState, is_click_pose
from mouse import MouseController, MouseState
from palette import Palette
from track import (Tracker, apply_homography, compute_homography, draw_debug,
                   list_cameras, load_config, save_config)

# --- Umbrales de pinch (con histeresis) --------------------------------------
# pinch = distancia pulgar-indice / tamano de mano. Ajustar segun tus pruebas.
PINCH_ON = 0.30                       # por debajo de esto se considera pinch
PINCH_OFF = 0.45                      # hay que superar esto para soltarlo
PINCH_FRAMES = 3                      # frames consecutivos para confirmar

# --- Borrador (gesto OK) y limpiar pizarra -----------------------------------
OK_FRAMES = 3                         # frames consecutivos para confirmar el OK
EXT_RATIO = 1.15                      # dedo extendido: dist(punta, muneca) > 1.15 * dist(PIP, muneca)
CURL_RATIO = 1.05                     # dedo cerrado: dist(punta, muneca) <= 1.05 * dist(PIP, muneca) (click del Mouse Mode)
CLEAR_HOLD = 0.7                      # segundos con AMBAS manos en OK para limpiar

BOARD_SIZE = (BOARD_W, BOARD_H)

# --- Modos de VirtualChalk (plan v0.2, seccion 21-22) -------------------------
MODE_MOUSE = "mouse"        # controlar Windows (cursor, click, drag, ...)
MODE_CHALK = "chalk"        # dibujar sobre la pizarra
MODE_BOARD = "board"        # trabajar dentro de VirtualChalk (popups, menu)

# --- Mouse Mode ---------------------------------------------------------------
# Margen de pantalla: el cursor no llega a los bordes exactos (plan v0.2, s.17).
MOUSE_MARGIN = 20
# Calibracion inicial del area de movimiento (plan v0.2, s.17).
MOUSE_CALIB_MIN = (0.0, 0.0)
MOUSE_CALIB_MAX = (1.0, 1.0)
# Sensibilidad inicial del cursor (plan v0.2, s.18).
MOUSE_SENSITIVITY = 1.2
# Tiempo sin actualizacion (s) para activar el failsafe (plan v0.2, s.25).
MOUSE_FAILSAFE_TIMEOUT = 2.0
# Frames consecutivos para confirmar un gesto (plan v0.2, s.16).
MOUSE_CONFIRM_FRAMES = 2
# Umbral de tiempo (s) para considerar dos clicks como doble click (plan v0.2, s.11).
MOUSE_DOUBLE_CLICK_INTERVAL = 0.45
# Deadzone minima en px de pantalla (plan v0.2, s.9).
MOUSE_DEADZONE = 2.0
# Failsafe temporal: si el tracking no actualiza el cursor en > N s, se libera
# el botón y se avisa al usuario (plan v0.2, s.25).
MOUSE_TRACK_TIMEOUT = 2.0

# --- Menu principal (tecla M) --------------------------------------------------
MENU_ITEMS = [
    ("mode", "Cambiar modo: MOUSE / CHALK (actual: CHALK)"),
    ("cameras", "Cambiar camara"),
    ("palette", "Paleta de colores  (P)"),
    ("calib", "Recalibrar  (C)"),
    ("full", "Pantalla completa  (F)"),
    ("clear", "Limpiar pizarra"),
    ("close", "Cerrar"),
]
RESUME_DELAY = 0.4                    # s sin dibujar tras cerrar un popup con pinch
TOAST_TIME = 1.2


def make_detector(enable_erase=True):
    return GestureDetector(PINCH_ON, PINCH_OFF, PINCH_FRAMES,
                           ok_frames=OK_FRAMES, ext_ratio=EXT_RATIO,
                           enable_erase=enable_erase)


def cursor_label(state):
    """GestureState -> string que entiende ChalkRenderer."""
    if state in (GestureState.PINCH_DOWN, GestureState.PINCH_DRAG):
        return "draw"
    if state == GestureState.ERASE:
        return "erase"
    return "idle"


class HandCtx:
    """Estado por mano: su detector de gestos, su cursor y el ultimo punto del trazo."""

    def __init__(self):
        self.gestures = make_detector()
        self.cursor = None            # posicion en el Board o None
        self.last_pos = None          # ultimo punto dibujado/borrado (None = trazo cortado)


class App:
    STATE_CALIB = "calibrating"
    STATE_RUN = "running"

    def __init__(self, debug=False, camera=0, fullscreen=True):
        self.debug = debug
        self.show_cam = debug

        self.chalk = ChalkRenderer(fullscreen=fullscreen and not debug)
        self.tracker = Tracker(camera_index=camera)
        self.gestures = make_detector(enable_erase=False)   # solo para calibrar
        self.mouse = MouseController()

        self.H = load_config(BOARD_SIZE)
        self.state = self.STATE_RUN if self.H is not None else self.STATE_CALIB
        self.calib_step = 0
        self.calib_cam_pts = []
        self.calib_msg = ""
        self.cursors = []             # [(pos, estado_str)] de las manos visibles, coords Board
        self.hand_ctx = {}            # handedness -> HandCtx (gestos por mano)
        self._clear_since = None      # instante en que ambas manos hicieron OK
        self._clear_done = False
        self.clear_progress = 0.0     # 0..1, feedback visual de limpiar
        self.running = True

        # Modo actual: mouse, chalk o board (plan v0.2, seccion 21-22)
        self.mode = MODE_CHALK  # comienza en modo dibujo (compatibilidad)
        self.mouse_enabled = False  # tracking activo solo en Mouse Mode

        # popups: None | "menu" (tecla M) | "camera" (desde el menu) | "palette" (tecla P)
        self.palette = Palette(BOARD_W, BOARD_H)
        self.popup = None
        self.sel = {"menu": 0, "camera": 0}
        self.menu_cams = []           # [{"index", "label"}]
        self.menu_status = ""
        self.ptr_gestures = make_detector(enable_erase=False)   # pinch = clic en popups
        self.pointer = None           # puntero de la mano en coords Board (solo popups)
        self._ptr_last = None
        self._resume_at = 0.0         # hasta cuando no se dibuja tras cerrar un popup
        self._toast = ("", 0.0)
        self._scan_thread = None
        self._scan_result = None      # lo escribe el hilo de busqueda, lo lee el bucle

    # -- calibracion -------------------------------------------------------------
    def start_calibration(self):
        self.state = self.STATE_CALIB
        self.calib_step = 0
        self.calib_cam_pts = []
        self.calib_msg = ""
        self.cursors = []
        # en calibracion se hace pinch con la mano abierta: el OK/borrador no debe interferir
        self.gestures = make_detector(enable_erase=False)

    def _update_calibration(self, hand):
        gesture = self.gestures.update(hand)
        confirmed = (gesture == GestureState.PINCH_DOWN)
        if hand is None:
            self.calib_msg = "No veo tu mano"
            return
        self.calib_msg = "Toca el marcador con la punta del indice y haz pinch"
        if confirmed:
            self.calib_cam_pts.append(tuple(hand.index_tip))
            self.calib_step += 1
            if self.calib_step >= 4:
                self._finish_calibration()

    def _finish_calibration(self):
        H = compute_homography(self.calib_cam_pts, calibration_targets())
        if H is None or not np.all(np.isfinite(H)):
            self.calib_msg = "Calibracion invalida, repite"
            self.start_calibration()
            return
        self.H = H
        save_config(H, [list(p) for p in self.calib_cam_pts], BOARD_SIZE)
        self.state = self.STATE_RUN
        self.gestures = make_detector(enable_erase=False)
        self.hand_ctx = {}
        self.cursors = []

    # -- popups --------------------------------------------------------------------
    def _reset_hands(self):
        """Estado de gestos limpio (quedaba congelado mientras habia un popup)."""
        self.hand_ctx = {}
        self.cursors = []
        self.gestures = make_detector(enable_erase=False)
        self.ptr_gestures = make_detector(enable_erase=False)
        self.pointer = None
        self._ptr_last = None
        self._clear_since = None
        self._clear_done = False
        self.clear_progress = 0.0

    def open_popup(self, name):
        if self.popup is None:
            self._reset_hands()       # entre popups NO se resetea: el pinch en curso sigue
        self.popup = name
        if name in self.sel:
            self.sel[name] = 0
        if name == "camera":
            self.menu_cams = []
            self._start_scan()

    def close_popup(self):
        self.popup = None
        self.menu_status = ""
        self._reset_hands()
        self._resume_at = time.perf_counter() + RESUME_DELAY

    def toggle_popup(self, name):
        if self.popup == name:
            self.close_popup()
        else:
            self.open_popup(name)

    def _change_size(self, delta):
        self.palette.change_size(delta)
        p = self.palette
        self._toast = (f"Tamano {p.size} px | borrador {p.erase_radius} px",
                       time.perf_counter() + TOAST_TIME)

    def _activate(self, page, i):
        if page == "menu":
            self._activate_menu(i)
        else:
            self._apply_camera(i)

    def _activate_menu(self, i):
        action = MENU_ITEMS[i][0]
        if action == "cameras":
            self.open_popup("camera")
        elif action == "palette":
            self.open_popup("palette")
        elif action == "calib":
            self.close_popup()
            self.start_calibration()
        elif action == "full":
            self.chalk.toggle_fullscreen()
        elif action == "clear":
            self.chalk.clear_board()
            self.close_popup()
        elif action == "mode":
            self.close_popup()
            self._toggle_mouse_mode()
        else:
            self.close_popup()

    def _start_scan(self):
        """Busca camaras en un hilo aparte para no congelar la ventana."""
        self.menu_status = "Buscando camaras..."
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        self._scan_result = None
        in_use = self.tracker.camera_index

        def work():
            self._scan_result = list_cameras(in_use=in_use)

        self._scan_thread = threading.Thread(target=work, daemon=True)
        self._scan_thread.start()

    def _poll_scan(self):
        if self._scan_result is None:
            return
        self.menu_cams, self._scan_result = self._scan_result, None
        cur = self._current_cam_pos()
        self.sel["camera"] = max(cur, 0)
        self.menu_status = "" if self.menu_cams else "No se encontraron camaras"

    def _current_cam_pos(self):
        for i, c in enumerate(self.menu_cams):
            if c["index"] == self.tracker.camera_index:
                return i
        return -1

    def _apply_camera(self, pos):
        if not (0 <= pos < len(self.menu_cams)):
            return
        cam = self.menu_cams[pos]
        if cam["index"] == self.tracker.camera_index:
            self.open_popup("menu")
            return
        # switch_camera bloquea ~1 s: se pinta antes el aviso
        self.menu_status = f"Cambiando a camara {cam['index']}..."
        self._render(None)
        try:
            switched = self.tracker.switch_camera(cam["index"])
        except RuntimeError as e:
            self.menu_status = f"Error: {e}"
            return
        if not switched:
            self.menu_status = f"No se pudo abrir la camara {cam['index']}"
            return
        print(f"[main] Camara activa: {cam['index']}")
        self.close_popup()
        # la homografia guardada es de la camara anterior: hay que recalibrar
        self.start_calibration()

    def _handle_list_key(self, e):
        page = self.popup
        n = len(MENU_ITEMS) if page == "menu" else len(self.menu_cams)
        k = e.key
        if k == pygame.K_ESCAPE:
            if page == "camera":
                self.popup = "menu"           # volver al menu principal
            else:
                self.close_popup()
        elif k in (pygame.K_UP, pygame.K_w) and n:
            self.sel[page] = (self.sel[page] - 1) % n
        elif k in (pygame.K_DOWN, pygame.K_s) and n:
            self.sel[page] = (self.sel[page] + 1) % n
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER) and n:
            self._activate(page, self.sel[page])
        elif k == pygame.K_r and page == "camera":
            self.menu_cams = []
            self._start_scan()

    def _pointer(self, kind, pos):
        """Puntero unificado (mouse o mano): kind = "down" | "move" | "up"."""
        if self.popup == "palette":
            if self.palette.pointer(kind, pos):
                self.close_popup()
            return
        hit = self.chalk.menu_hit(pos)
        if hit is None:
            return
        self.sel[self.popup] = hit
        if kind == "down":
            self._activate(self.popup, hit)

    def _handle_mouse(self, e):
        if e.type == pygame.MOUSEMOTION:
            self._pointer("move", e.pos)
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            self._pointer("down", e.pos)
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self._pointer("up", e.pos)

    def _update_popup(self, hands):
        """Con un popup abierto, el indice de la mano es el puntero y el pinch es el clic."""
        self.pointer = None
        if self.state != self.STATE_RUN or self.H is None:
            return
        hand = hands[0] if hands else None
        st = self.ptr_gestures.update(hand)
        if hand is not None:
            pos = apply_homography(self.H, hand.index_tip)
            self._ptr_last = pos
            self.pointer = pos
        else:
            pos = self._ptr_last
        if pos is None:
            return
        if st == GestureState.PINCH_DOWN:
            self._pointer("down", pos)
        elif st == GestureState.PINCH_UP:
            self._pointer("up", pos)
        elif hand is not None:
            self._pointer("move", pos)

    # -- bucle principal -----------------------------------------------------------
    def run(self):
        print("[main] Iniciando camara y tracker...")
        self.tracker.start()
        try:
            while self.running:
                self._handle_events()
                hands = self.tracker.get_hands()
                hand = hands[0] if hands else None    # calibracion y HUD

                if self.mode == MODE_MOUSE:
                    # Mouse Mode: la mano controla el mouse de Windows.
                    # VirtualChalk puede estar minimizado o en una pequeña
                    # ventana flotante (plan v0.2, seccion 20).
                    self._update_mouse(hands)
                elif self.popup:
                    self._poll_scan()          # con un popup abierto no se dibuja ni calibra
                    self._update_popup(hands)
                elif self.state == self.STATE_CALIB:
                    self._update_calibration(hand)
                else:
                    self._update_run(hands)

                if self.mode != MODE_MOUSE:
                    self._render(hand)
                if self.show_cam:
                    self._show_camera(hands)
                self.chalk.tick(60)
        finally:
            self.tracker.stop()
            cv2.destroyAllWindows()
            self.chalk.quit()

    def _update_run(self, hands):
        """Dibujar (pinch), borrar (OK) y limpiar (dos manos en OK), por mano."""
        if time.perf_counter() < self._resume_at:   # acaba de cerrarse un popup
            self.cursors = []
            return
        seen = set()
        for h in hands:
            ctx = self.hand_ctx.setdefault(h.handedness, HandCtx())
            seen.add(h.handedness)
            ctx.gestures.update(h)
            ctx.cursor = (apply_homography(self.H, h.index_tip)
                          if self.H is not None else None)
        for label, ctx in self.hand_ctx.items():
            if label not in seen:              # mano perdida: corta el trazo
                ctx.gestures.update(None)
                ctx.cursor = None
                ctx.last_pos = None

        # -- limpiar: las DOS manos en OK sostenido ---------------------------
        now = time.perf_counter()
        both_ok = len(seen) == 2 and all(
            self.hand_ctx[k].gestures.state == GestureState.ERASE for k in seen)
        if both_ok:
            if self._clear_since is None:
                self._clear_since = now
            self.clear_progress = min((now - self._clear_since) / CLEAR_HOLD, 1.0)
            if self.clear_progress >= 1.0 and not self._clear_done:
                self.chalk.clear_board()
                self._clear_done = True        # hay que soltar el OK para volver a limpiar
        else:
            self._clear_since = None
            self._clear_done = False
            self.clear_progress = 0.0

        # -- dibujar / borrar ---------------------------------------------------
        self.cursors = []
        for label in seen:
            ctx = self.hand_ctx[label]
            state, pos = ctx.gestures.state, ctx.cursor
            if pos is None:
                ctx.last_pos = None
                continue
            self.cursors.append((pos, cursor_label(state)))

            if state == GestureState.PINCH_DOWN:
                ctx.last_pos = pos
            elif state == GestureState.PINCH_DRAG:
                if ctx.last_pos is not None:
                    self.chalk.stroke(ctx.last_pos, pos, self.palette.color, self.palette.size)
                ctx.last_pos = pos
            elif state == GestureState.ERASE and not both_ok:
                pts = [ctx.last_pos, pos] if ctx.last_pos is not None else [pos]
                self.chalk.erase(pts, self.palette.erase_radius)
                ctx.last_pos = pos
            else:
                ctx.last_pos = None

    # -- Mouse Mode ------------------------------------------------------------
    # Plan v0.2: seccion 20 (ventana minimizada en segundo plano) y seccion 10
    # (gestos: pinch = click, pinch + movimiento = drag, release = soltar).
    @staticmethod
    def _esc_down():
        """ESC global (Windows): pygame no recibe teclas con la ventana minimizada."""
        if platform.system() != "Windows":
            return False
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000)

    def _update_mouse(self, hands):
        """
        Control del mouse de Windows con la mano (seccion 10 del plan).
        Salida: ESC (global) o la tecla U / menu desde la ventana.
        """
        # 0) ESC global: con la ventana minimizada pygame no ve el teclado.
        if self._esc_down():
            self._exit_mouse_mode()
            return

        hand = hands[0] if hands else None
        if hand is None:
            # mano perdida: suelta el boton (antes quedaba apretado para siempre)
            self.mouse.update_pinch(False)
            return

        # 1) Cursor anclado a los nudillos (MCP indice + medio), NO a la punta
        #    del indice: al hacer pinch la punta se mueve hacia el pulgar y el
        #    clic caia lejos de donde apuntabas.
        a, b = hand.landmarks[5], hand.landmarks[9]
        self.mouse.move_cursor((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

        # 2) Click = pinch con medio/anular/menique CERRADOS. Mano abierta -> no hay clic.
        #    Histeresis: ya presionado, se tolera un cierre menos estricto y PINCH_OFF.
        held = self.mouse.is_button_pressed()
        is_click = is_click_pose(hand,
                                 PINCH_OFF if held else PINCH_ON,
                                 EXT_RATIO if held else CURL_RATIO)
        self.mouse.update_pinch(is_click)

    def _toggle_mouse_mode(self):
        """Activa o desactiva Mouse Mode (secciones 20, 23, 24, 25 del plan)."""
        if self.mode == MODE_MOUSE:
            self._exit_mouse_mode()
        else:
            self._enter_mouse_mode()

    def _enter_mouse_mode(self):
        """Entra en Mouse Mode: minimiza la ventana y empieza a controlar el mouse."""
        self.mode = MODE_MOUSE
        self.mouse.reset()

        # Seccion 20: minimizar la ventana (puede quedar en segundo plano).
        self.chalk.minimize()

        # Seccion 23: recordar el estado anterior para recuperar.
        self._saved_mode = MODE_CHALK     # chalk y board son lo mismo
        self._saved_fullscreen = self.chalk.fullscreen
        self._saved_popup = self.popup
        self.popup = None                 # cerrar cualquier popup
        self._toast = ("Mouse Mode: usa la mano para mover el cursor y pinch "
                       "para hacer click. ESC para recuperar.",
                       time.perf_counter() + 2.5)

    def _exit_mouse_mode(self):
        """Sale de Mouse Mode: restaura la ventana y el estado anterior."""
        self.mouse.reset(release_button=True)

        # Seccion 23: restaurar ventana y estado anterior.
        self.chalk.restore()

        self.mode = self._saved_mode
        self.popup = self._saved_popup
        self._toast = ("Modo restaurado: pulsa M para abrir el menu.",
                       time.perf_counter() + 2.0)

    def _handle_events(self):
        for e in self.chalk.poll_events():
            if e.type == pygame.QUIT:
                self.running = False
            elif e.type == pygame.KEYDOWN:
                self._handle_key(e)
            elif self.popup and e.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN,
                                           pygame.MOUSEBUTTONUP):
                self._handle_mouse(e)

    def _handle_key(self, e):
        k = e.key
        if e.unicode == "+" or k in (pygame.K_PLUS, pygame.K_KP_PLUS, pygame.K_EQUALS):
            self._change_size(+1)
        elif e.unicode == "-" or k in (pygame.K_MINUS, pygame.K_KP_MINUS):
            self._change_size(-1)
        elif k == pygame.K_p:
            self.toggle_popup("palette")
        elif k == pygame.K_m:
            if self.popup is None:
                self.open_popup("menu")
            else:
                self.close_popup()
        elif self.popup in ("menu", "camera"):
            self._handle_list_key(e)
        elif self.popup == "palette":
            if k == pygame.K_ESCAPE:
                self.close_popup()
        elif k == pygame.K_ESCAPE:
            if self.mode == MODE_MOUSE:
                # ESC recupera el control inmediatamente (seccion 25 del plan).
                self._exit_mouse_mode()
            else:
                self.running = False
        elif k == pygame.K_c:
            self.start_calibration()
        elif k == pygame.K_f:
            self.chalk.toggle_fullscreen()
        elif k == pygame.K_u:
            # U = alternar Mouse Mode (seccion 22 del plan).
            if self.popup is None:
                self._toggle_mouse_mode()
        elif k == pygame.K_m:
            if self.popup is None:
                self.open_popup("menu")
            else:
                self.close_popup()
        elif k == pygame.K_d and self.debug:
            self.show_cam = not self.show_cam
            if not self.show_cam:
                cv2.destroyWindow("VirtualChalk - camara (debug)")

    # -- render ---------------------------------------------------------------------
    def _render(self, hand):
        calibrating = self.state == self.STATE_CALIB
        pinching = self.gestures.state in (GestureState.PINCH_DOWN,
                                           GestureState.PINCH_DRAG)
        self.chalk.render(
            mode=self.state,
            hand_present=hand is not None,
            stats={"cam_fps": self.tracker.fps,
                   "latency_ms": self.tracker.latency_ms},
            calib={"step": self.calib_step, "msg": self.calib_msg} if calibrating else None,
            cursors=self.cursors,
            clear_progress=self.clear_progress,
            pinch_value=hand.pinch if (calibrating and hand is not None) else None,
            pinching=pinching,
            menu=self._menu_view(),
            palette=self.palette if self.popup == "palette" else None,
            pointer=self.pointer if self.popup else None,
            brush={"color": self.palette.color, "size": self.palette.size,
                   "erase_radius": self.palette.erase_radius},
            toast=self._toast[0] if time.perf_counter() < self._toast[1] else None,
        )

    def _menu_view(self):
        if self.popup == "menu":
            items = []
            for i, (action, label) in enumerate(MENU_ITEMS):
                if action == "mode":
                    items.append(f"Mouse Mode  (actual: {self.mode.upper()})")
                else:
                    items.append(label)
            return {"title": "Menu", "items": items,
                    "selected": self.sel["menu"], "current": -1, "status": "",
                    "hint": "flechas: elegir  Enter/clic/pinch: aplicar  M/ESC: cerrar"}
        if self.popup == "camera":
            return {"title": "Cambiar camara",
                    "items": [c["label"] for c in self.menu_cams],
                    "selected": self.sel["camera"], "current": self._current_cam_pos(),
                    "status": self.menu_status,
                    "hint": "flechas: elegir  Enter/clic: aplicar  R: reescanear  "
                            "ESC: volver  M: cerrar"}
        return None

    # -- vista de camara (solo debug) -----------------------------------------------------
    def _show_camera(self, hands):
        frame = self.tracker.get_frame()
        if frame is None:
            return
        draw_debug(frame, hands)
        cv2.imshow("VirtualChalk - camara (debug)", frame)
        cv2.waitKey(1)


# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="VirtualChalk")
    ap.add_argument("--debug", action="store_true",
                    help="ventana normal y vista de camara con landmarks")
    ap.add_argument("--camera", type=int, default=0, help="indice de la camara")
    ap.add_argument("--windowed", action="store_true", help="no usar fullscreen")
    args = ap.parse_args()

    try:
        App(debug=args.debug, camera=args.camera,
            fullscreen=not args.windowed).run()
    except RuntimeError as e:
        print(f"[error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()