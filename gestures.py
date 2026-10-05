"""
gestures.py - Deteccion de gestos a partir de HandData (una instancia por mano).

Gestos:
    PINCH  pulgar + indice unidos                          -> dibujar
    OK     pulgar + indice unidos y medio/anular/menique
           extendidos                                      -> ERASE (borrador)
    Dos manos en OK a la vez (lo decide main.py)           -> limpiar la pizarra

Mouse Mode (funciones sin estado):
    is_click_pose        pulgar + indice, anular/menique cerrados   -> click izquierdo
    is_triple_pinch      pulgar + indice + medio                    -> doble click
    is_right_click_pose  pulgar + medio, indice SEPARADO            -> click derecho
    is_fist_pose         puno cerrado                               -> "levantar el mouse"
                                                                       (embrague del modo relativo)

GestureDetector tiene estado: usa UNA instancia por mano.
"""

import math
from dataclasses import dataclass
from enum import Enum, auto

WRIST = 0
# (punta, PIP) de medio, anular y menique
_OTHER_FINGERS = ((12, 10), (16, 14), (20, 18))
# (punta, PIP) de anular y menique: los que deben estar cerrados en el CLICK
_CLICK_FINGERS = ((16, 14), (20, 18))


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


def is_click_pose(hand, pinch_thr=0.30, curl_ratio=1.05):
    """
    Pose de CLICK (Mouse Mode): pulgar e indice unidos + anular y menique
    CERRADOS (la punta no se aleja de la muneca mas que su PIP, por curl_ratio).
    El medio NO se evalua (queda libre; el doble click lo usa y tiene prioridad).
    Con la mano abierta devuelve False aunque haya pinch.
    """
    if hand is None or not hasattr(hand, "landmarks"):
        return False
    if hand.pinch >= pinch_thr:
        return False
    lm = hand.landmarks
    wrist = lm[WRIST]
    for tip, pip in _CLICK_FINGERS:
        if _dist(lm[tip], wrist) > curl_ratio * _dist(lm[pip], wrist):
            return False
    return True


THUMB_TIP, INDEX_TIP, MIDDLE_TIP, MIDDLE_MCP = 4, 8, 12, 9


def is_triple_pinch(hand, thr=0.35):
    """
    Pinch de tres dedos (DOBLE CLICK en Mouse Mode): las puntas de pulgar, indice
    y medio unidas a la vez. Ambas distancias (pulgar-indice y pulgar-medio) se
    normalizan por el tamano de la mano, igual que hand.pinch.
    """
    if hand is None or not hasattr(hand, "landmarks"):
        return False
    lm = hand.landmarks
    size = max(_dist(lm[WRIST], lm[MIDDLE_MCP]), 1e-6)
    d_index = _dist(lm[THUMB_TIP], lm[INDEX_TIP]) / size
    d_middle = _dist(lm[THUMB_TIP], lm[MIDDLE_TIP]) / size
    return max(d_index, d_middle) < thr


# (punta, PIP) de indice, medio, anular y menique
_FIST_FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))


def is_fist_pose(hand, curl_ratio=0.95):
    """
    Puno cerrado (Mouse Mode relativo = "levantar el mouse"): los cuatro dedos
    doblados, con la punta mas cerca de la muneca que su PIP (por curl_ratio).
    curl_ratio < 1 exige un cierre claro: asi el pinch del click (donde el
    indice sigue medio extendido) no se confunde con un puno.
    """
    if hand is None or not hasattr(hand, "landmarks"):
        return False
    lm = hand.landmarks
    wrist = lm[WRIST]
    for tip, pip in _FIST_FINGERS:
        if _dist(lm[tip], wrist) > curl_ratio * _dist(lm[pip], wrist):
            return False
    return True


def is_right_click_pose(hand, thr=0.30, index_min=0.50):
    """
    Pose de CLICK DERECHO (Mouse Mode): pulgar y dedo medio unidos, con el
    indice SEPARADO del pulgar.

    La distancia pulgar-medio se normaliza por el tamano de la mano (igual que
    hand.pinch). Exigir el indice lejos del pulgar (index_min) evita confundirlo
    con el click izquierdo (pulgar-indice) y con el doble click (los tres juntos).
    """
    if hand is None or not hasattr(hand, "landmarks"):
        return False
    lm = hand.landmarks
    size = max(_dist(lm[WRIST], lm[MIDDLE_MCP]), 1e-6)
    d_middle = _dist(lm[THUMB_TIP], lm[MIDDLE_TIP]) / size
    d_index = _dist(lm[THUMB_TIP], lm[INDEX_TIP]) / size
    return d_middle < thr and d_index > index_min


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