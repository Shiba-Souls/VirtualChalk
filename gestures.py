"""
gestures.py - Deteccion de gestos a partir de HandData (una instancia por mano).

Gestos:
    PINCH  pulgar + indice unidos                          -> dibujar
    OK     pulgar + indice unidos y medio/anular/menique
           extendidos                                      -> ERASE (borrador)
    Dos manos en OK a la vez (lo decide main.py)           -> limpiar la pizarra

GestureDetector tiene estado: usa UNA instancia por mano.
"""

import math
from dataclasses import dataclass
from enum import Enum, auto

WRIST = 0
# (punta, PIP) de medio, anular y menique
_OTHER_FINGERS = ((12, 10), (16, 14), (20, 18))


class GestureState(Enum):
    IDLE = auto()
    PINCH_DOWN = auto()
    PINCH_DRAG = auto()
    PINCH_UP = auto()
    ERASE = auto()


@dataclass
class GestureData:
    state: GestureState
    pos: tuple  # (x, y) en coordenadas Board


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def is_ok_pose(hand, pinch_on=0.30, ext_ratio=1.15):
    """
    Pose OK (sin estado): pulgar e indice unidos + medio, anular y menique extendidos.

    Un dedo cuenta como extendido si su punta esta mas lejos de la muneca que su
    PIP (articulacion media) por un margen (ext_ratio). Sirve con la mano rotada,
    a diferencia de comparar solo la coordenada y.
    """
    if hand is None or not hasattr(hand, "landmarks"):
        return False
    if hand.pinch >= pinch_on:
        return False
    lm = hand.landmarks
    wrist = lm[WRIST]
    for tip, pip in _OTHER_FINGERS:
        if _dist(lm[tip], wrist) < ext_ratio * _dist(lm[pip], wrist):
            return False
    return True


class GestureDetector:
    def __init__(self, pinch_on=0.30, pinch_off=0.45, frames=3,
                 ok_frames=3, ok_grace=2, ext_ratio=1.15, enable_erase=True):
        self.pinch_on = pinch_on
        self.pinch_off = pinch_off
        self.frames = frames
        self.ok_frames = ok_frames        # frames seguidos para confirmar el OK
        self.ok_grace = ok_grace          # frames de parpadeo tolerados dentro del OK
        self.ext_ratio = ext_ratio
        self.enable_erase = enable_erase  # False en la calibracion (ahi se hace pinch)

        self._count = 0
        self._active = False
        self._ok_count = 0
        self._ok_miss = 0
        self.state = GestureState.IDLE

    def is_ok_gesture(self, hand):
        return is_ok_pose(hand, self.pinch_on, self.ext_ratio)

    def update(self, hand):
        """Actualiza y devuelve el estado del gesto para esta mano."""
        if hand is None:
            was_active = self._active
            self._active = False
            self._count = 0
            self._ok_count = 0
            self._ok_miss = 0
            self.state = GestureState.PINCH_UP if was_active else GestureState.IDLE
            return self.state

        # --- OK = borrador (con confirmacion y tolerancia a parpadeos) -------
        if self.enable_erase:
            if self.is_ok_gesture(hand):
                self._ok_count += 1
                self._ok_miss = 0
            else:
                self._ok_miss += 1
                if self._ok_miss > self.ok_grace:
                    self._ok_count = 0

            if self._ok_count >= self.ok_frames:
                # corta cualquier trazo en curso: al salir del OK empieza uno nuevo
                self._active = False
                self._count = 0
                self.state = GestureState.ERASE
                return self.state

        # --- Pinch con histeresis (dibujo) ------------------------------------
        if self._active:
            if hand.pinch > self.pinch_off:
                self._active = False
                self.state = GestureState.PINCH_UP
            else:
                self.state = GestureState.PINCH_DRAG
        else:
            if hand.pinch < self.pinch_on:
                self._count += 1
                if self._count >= self.frames:
                    self._active = True
                    self.state = GestureState.PINCH_DOWN
                    self._count = 0
                else:
                    self.state = GestureState.IDLE
            else:
                self._count = 0
                self.state = GestureState.IDLE

        return self.state