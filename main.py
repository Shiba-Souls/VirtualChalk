"""
main.py - VirtualChalk: coordinador.

    track.py     camara + MediaPipe + calibracion (donde esta la mano, en el Board)
    gestures.py  que significa cada pose (pinch, OK)
    chalk.py     todo lo que se DIBUJA (pizarra, cursores, HUD, ventana)

main.py solo conecta las tres piezas.

Ejecucion:
    python main.py            # uso normal (fullscreen en el proyector duplicado)
    python main.py --debug    # ventana normal + vista de camara con landmarks

Teclas (siempre disponibles, son de desarrollo):
    ESC   salir
    C     recalibrar
    F     alternar fullscreen
    D     alternar vista de camara (solo con --debug)
"""

import argparse
import sys
import time

import cv2
import numpy as np
import pygame

from chalk import (BOARD_W, BOARD_H, ERASE_RADIUS, FG, ChalkRenderer,
                   calibration_targets)
from gestures import GestureDetector, GestureState
from track import (Tracker, apply_homography, compute_homography, draw_debug,
                   load_config, save_config)

# --- Umbrales de pinch (con histeresis) --------------------------------------
# pinch = distancia pulgar-indice / tamano de mano. Ajustar segun tus pruebas.
PINCH_ON = 0.30                       # por debajo de esto se considera pinch
PINCH_OFF = 0.45                      # hay que superar esto para soltarlo
PINCH_FRAMES = 3                      # frames consecutivos para confirmar

# --- Borrador (gesto OK) y limpiar pizarra -----------------------------------
OK_FRAMES = 3                         # frames consecutivos para confirmar el OK
EXT_RATIO = 1.15                      # dedo extendido: dist(punta, muneca) > 1.15 * dist(PIP, muneca)
CLEAR_HOLD = 0.7                      # segundos con AMBAS manos en OK para limpiar

BOARD_SIZE = (BOARD_W, BOARD_H)


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

    # -- bucle principal -----------------------------------------------------------
    def run(self):
        print("[main] Iniciando camara y tracker...")
        self.tracker.start()
        try:
            while self.running:
                self._handle_events()
                hands = self.tracker.get_hands()
                hand = hands[0] if hands else None    # calibracion y HUD

                if self.state == self.STATE_CALIB:
                    self._update_calibration(hand)
                else:
                    self._update_run(hands)

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
                    self.chalk.stroke(ctx.last_pos, pos, FG)
                ctx.last_pos = pos
            elif state == GestureState.ERASE and not both_ok:
                pts = [ctx.last_pos, pos] if ctx.last_pos is not None else [pos]
                self.chalk.erase(pts, ERASE_RADIUS)
                ctx.last_pos = pos
            else:
                ctx.last_pos = None

    def _handle_events(self):
        for e in self.chalk.poll_events():
            if e.type == pygame.QUIT:
                self.running = False
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    self.running = False
                elif e.key == pygame.K_c:
                    self.start_calibration()
                elif e.key == pygame.K_f:
                    self.chalk.toggle_fullscreen()
                elif e.key == pygame.K_d and self.debug:
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
        )

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