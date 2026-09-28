"""
track.py - Camara + OpenCV + MediaPipe (HandLandmarker, API Tasks).

Responde a una sola pregunta: "donde esta la mano?" (y en que punto del Board
cae, gracias a la calibracion por homografia).
No decide que significa un gesto (eso es gestures.py) ni dibuja nada
sobre el Board (eso es chalk.py).

Uso:
    tracker = Tracker()
    tracker.start()
    ...
    hand = tracker.get_hand()   # HandData | None (ultima lectura disponible)
    frame = tracker.get_frame() # ultimo frame BGR espejado (solo para debug)
    ...
    tracker.stop()

Coordenadas: todas normalizadas 0.0 -> 1.0 sobre la imagen ESPEJADA.
"""

import json
import math
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
MODEL_PATH = Path(__file__).parent / "hand_landmarker.task"

# Indices de landmarks de MediaPipe Hands
WRIST = 0
THUMB_TIP = 4
INDEX_MCP = 5
INDEX_TIP = 8
MIDDLE_MCP = 9

# Conexiones para dibujar el esqueleto en modo debug
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]


# ---------------------------------------------------------------------------
# Filtro One Euro (suaviza el temblor sin agregar mucha latencia)
# ---------------------------------------------------------------------------
class OneEuroFilter:
    """
    Filtro One Euro para una senal escalar.
    - min_cutoff: menor = mas suave en reposo (menos temblor) pero mas retraso.
    - beta: mayor = reacciona mas rapido cuando el movimiento es veloz.
    """

    def __init__(self, min_cutoff=1.0, beta=0.02, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self):
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None

    def __call__(self, x, t):
        if self._x_prev is None or self._t_prev is None:
            self._x_prev, self._t_prev = x, t
            return x
        dt = max(t - self._t_prev, 1e-6)
        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1.0 - a_d) * self._dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1.0 - a) * self._x_prev
        self._x_prev, self._dx_prev, self._t_prev = x_hat, dx_hat, t
        return x_hat


# ---------------------------------------------------------------------------
# Datos que entrega el tracker
# ---------------------------------------------------------------------------
@dataclass
class HandData:
    """Ultima lectura de la mano (coordenadas normalizadas, imagen espejada)."""
    landmarks: list                 # 21 tuplas (x, y) crudas
    index_tip: tuple                # (x, y) suavizada
    handedness: str                 # "Left" / "Right" (ya corregida por el espejo)
    pinch: float                    # distancia pulgar-indice / tamano de mano
    timestamp: float                # time.perf_counter() del frame de origen
    latency_ms: float = 0.0         # captura -> resultado disponible
    extra: dict = field(default_factory=dict)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------
class Tracker:
    def __init__(
        self,
        camera_index=0,
        width=640,
        height=480,
        mirror=True,
        min_cutoff=1.0,
        beta=0.02,
    ):
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.mirror = mirror

        self._min_cutoff = min_cutoff
        self._beta = beta
        self._filters = {}             # handedness -> (filtro_x, filtro_y)

        self._cap = None
        self._landmarker = None
        self._thread = None
        self._running = False

        self._lock = threading.Lock()
        self._hands = []
        self._frame = None
        self._capture_times = {}       # timestamp_ms -> perf_counter de captura

        self.fps = 0.0                 # FPS de captura/inferencia
        self.latency_ms = 0.0          # latencia camara -> resultado (suavizada)

    # -- ciclo de vida ------------------------------------------------------
    def start(self):
        self._ensure_model()
        self._open_camera()
        self._create_landmarker()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._landmarker is not None:
            self._landmarker.close()
            self._landmarker = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    # -- lectura desde otros hilos -------------------------------------------
    def get_hands(self):
        """Manos detectadas en la ultima lectura (0, 1 o 2 HandData)."""
        with self._lock:
            hands = list(self._hands)
        now = time.perf_counter()
        return [h for h in hands if now - h.timestamp <= 0.25]

    def get_hand(self):
        """Primera mano detectada o None (atajo para calibracion y HUD)."""
        hands = self.get_hands()
        return hands[0] if hands else None

    def get_frame(self):
        """Ultimo frame BGR (espejado si mirror=True). Solo para modo debug."""
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    # -- inicializacion -------------------------------------------------------
    @staticmethod
    def _ensure_model():
        if MODEL_PATH.exists():
            return
        print(f"[track] Descargando modelo a {MODEL_PATH} ...")
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
        print("[track] Modelo descargado.")

    def _open_camera(self):
        # CAP_DSHOW abre mas rapido y con menos latencia en Windows
        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            raise RuntimeError(
                f"No se pudo abrir la camara {self.camera_index}. "
                "Esta ocupada por otra aplicacion o no esta conectada."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # minimo buffer = menos latencia
        self._cap = cap

    def _create_landmarker(self):
        BaseOptions = mp.tasks.BaseOptions
        HandLandmarker = mp.tasks.vision.HandLandmarker
        HandLandmarkerOptions = mp.tasks.vision.HandLandmarkerOptions
        RunningMode = mp.tasks.vision.RunningMode

        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
            running_mode=RunningMode.LIVE_STREAM,
            num_hands=2,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
            result_callback=self._on_result,
        )
        self._landmarker = HandLandmarker.create_from_options(options)

    # -- bucle de captura -----------------------------------------------------
    def _loop(self):
        last_fps_t = time.perf_counter()
        frames = 0
        while self._running:
            ok, frame = self._cap.read()
            if not ok:
                time.sleep(0.005)
                continue

            if self.mirror:
                frame = cv2.flip(frame, 1)

            t_capture = time.perf_counter()
            ts_ms = int(t_capture * 1000)      # debe ser estrictamente creciente
            self._capture_times[ts_ms] = t_capture
            # limpiar entradas viejas para no acumular memoria
            if len(self._capture_times) > 64:
                for k in sorted(self._capture_times)[:-32]:
                    self._capture_times.pop(k, None)

            with self._lock:
                self._frame = frame

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            try:
                self._landmarker.detect_async(mp_image, ts_ms)
            except Exception as e:                # timestamp repetido, etc.
                print(f"[track] detect_async: {e}")

            frames += 1
            now = time.perf_counter()
            if now - last_fps_t >= 1.0:
                self.fps = frames / (now - last_fps_t)
                frames = 0
                last_fps_t = now

    def _on_result(self, result, output_image, timestamp_ms):
        t_capture = self._capture_times.pop(timestamp_ms, None)
        t_now = time.perf_counter()
        t = t_capture if t_capture is not None else t_now

        if not result.hand_landmarks:
            for fx, fy in self._filters.values():
                fx.reset()
                fy.reset()
            with self._lock:
                self._hands = []
            return

        hands = []
        used = set()
        for i, lms in enumerate(result.hand_landmarks):
            lm = [(p.x, p.y) for p in lms]

            # La imagen esta espejada, asi que MediaPipe invierte Left/Right.
            raw = result.handedness[i][0].category_name
            if self.mirror:
                label = "Left" if raw == "Right" else "Right"
            else:
                label = raw
            # Si MediaPipe etiqueta las dos manos igual, desambiguar: la etiqueta
            # se usa como clave estable (filtros y detector de gestos por mano).
            if label in used:
                label = "Left" if label == "Right" else "Right"
            used.add(label)

            fx, fy = self._filters.setdefault(label, (
                OneEuroFilter(min_cutoff=self._min_cutoff, beta=self._beta),
                OneEuroFilter(min_cutoff=self._min_cutoff, beta=self._beta),
            ))
            sx = fx(lm[INDEX_TIP][0], t)
            sy = fy(lm[INDEX_TIP][1], t)

            hand_size = max(_dist(lm[WRIST], lm[MIDDLE_MCP]), 1e-6)
            pinch = _dist(lm[THUMB_TIP], lm[INDEX_TIP]) / hand_size

            latency = (t_now - t) * 1000.0 if t_capture is not None else 0.0
            if i == 0:
                self.latency_ms = latency if self.latency_ms == 0 else (
                    0.9 * self.latency_ms + 0.1 * latency
                )

            hands.append(HandData(
                landmarks=lm,
                index_tip=(sx, sy),
                handedness=label,
                pinch=pinch,
                timestamp=t,
                latency_ms=latency,
            ))

        # reiniciar filtros de las manos que ya no estan
        for label, (fx, fy) in self._filters.items():
            if label not in used:
                fx.reset()
                fy.reset()

        with self._lock:
            self._hands = hands


# ---------------------------------------------------------------------------
# Calibracion: camara (normalizada) -> Board (pixeles) mediante homografia
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.json"


def compute_homography(cam_pts, board_pts):
    """cam_pts y board_pts: listas de 4 (x, y). Devuelve matriz 3x3 o None."""
    src = np.array(cam_pts, dtype=np.float32)
    dst = np.array(board_pts, dtype=np.float32)
    H, _ = cv2.findHomography(src, dst, method=0)
    return H


def apply_homography(H, pt):
    """Convierte un punto normalizado de camara a coordenadas del Board."""
    v = np.array([pt[0], pt[1], 1.0], dtype=np.float64)
    r = H @ v
    if abs(r[2]) < 1e-9:
        return None
    return (float(r[0] / r[2]), float(r[1] / r[2]))


def save_config(H, cam_pts, board_size):
    """Guarda la homografia y los puntos de camara usados para calcularla."""
    data = {
        "board": list(board_size),
        "homography": H.tolist(),
        "camera_points": cam_pts,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_config(board_size):
    """Devuelve la homografia guardada o None si falta, esta corrupta o no coincide."""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if data.get("board") != list(board_size):
            return None                        # resolucion del Board distinta
        H = np.array(data["homography"], dtype=np.float64)
        if H.shape != (3, 3):
            return None
        return H
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Utilidad de debug: dibujar el esqueleto sobre un frame BGR
# ---------------------------------------------------------------------------
def draw_debug(frame, hands):
    """Dibuja landmarks y punta del indice de cada mano (modifica el frame)."""
    h, w = frame.shape[:2]

    # Aceptar HandData suelto, lista de manos o None
    if hands is None:
        hands = []
    elif not isinstance(hands, (list, tuple)):
        hands = [hands]

    if not hands:
        cv2.putText(frame, "sin mano", (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (0, 0, 255), 2)
        return frame

    for i, hand in enumerate(hands):
        pts = [(int(x * w), int(y * h)) for x, y in hand.landmarks]
        for a, b in HAND_CONNECTIONS:
            cv2.line(frame, pts[a], pts[b], (0, 200, 0), 2)
        for p in pts:
            cv2.circle(frame, p, 3, (0, 255, 255), -1)
        ix, iy = int(hand.index_tip[0] * w), int(hand.index_tip[1] * h)
        cv2.circle(frame, (ix, iy), 8, (255, 0, 255), 2)
        cv2.putText(frame, f"{hand.handedness}  pinch={hand.pinch:.2f}",
                    (10, 30 + 30 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2)
    return frame