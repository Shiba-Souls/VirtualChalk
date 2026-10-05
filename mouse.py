"""
mouse.py - Mouse Mode: convierte la posicion de la mano en control del mouse de Windows.

Arquitectura:

    track.py      -> posicion normalizada de la mano (0.0 - 1.0, imagen espejada)
    gestures.py   -> intento del usuario (pinch, pinch sostenido, release)
    mouse.py      -> ejecuta la accion sobre Windows (PyAutoGUI / AutoPy)
    WINDOW

Flujo interno del cursor:

    posicion detectada
    -> smoothing
    -> deadzone
    -> posicion final
    -> mouse

Responsabilidades:

    * Movimiento del cursor
    * Click izquierdo
    * Click derecho   (se reserva un gesto, se implementa despues)
    * Mouse down / up
    * Doble click
    * Scroll          (se implementa despues con gesto especifico)
    * Estado del boton presionado
    * Conversion de coordenadas (normalizada -> pantalla)
    * Espejo de camera (x_mouse = 1 - x_hand)
    * Calibracion del area de movimiento
    * Smoothing especifico del cursor
    * Deadzone para evitar micro-movimientos

Principio central (plan v0.2):

    TRACKING   -> detecta la mano
    GESTURES   -> interpreta la intencion
    MOUSE      -> ejecuta la accion
    WINDOWS    -> recibe la accion

    gestures.py NO debe llamar a PyAutoGUI/AutoPy: esa es la unica
    responsabilidad de mouse.py.

Gestos de Mouse Mode (primera implementacion):

    MANO ABIERTA          -> mover cursor
    PINCH                 -> click izquierdo
    PINCH + movimiento    -> drag
    RELEASE               -> soltar boton

    DOBLE CLICK: dos PINCH en rapida sucesion.

Failsafe:

    * ESC -> desactiva Mouse Mode inmediatamente (sin depender de la mano)
    * Si el tracking deja de actualizar > 2 s, se libera el boton y se avisa.
"""

import time
import math

import autopy
import pyautogui

pyautogui.PAUSE = 0          # sin la pausa de 0.1 s tras cada llamada de pyautogui


def _button(down):
    """
    Presiona/suelta el boton izquierdo.

    autopy NO tiene mouse.down()/mouse.up() (solo click, move, toggle, location):
    esas llamadas lanzaban AttributeError, que los try/except tragaban en silencio,
    por eso el pinch nunca hacia clic. Se usa toggle(), con pyautogui de respaldo.
    """
    try:
        autopy.mouse.toggle(autopy.mouse.Button.LEFT, down)
    except Exception:
        if down:
            pyautogui.mouseDown()
        else:
            pyautogui.mouseUp()

# =============================================================================
# Configuracion
# =============================================================================

# Margen de pantalla: el cursor no llega a los bordes exactos (mas comodo).
MARGIN = 20       # pixeles desde los bordes izquierdo/derecho/arriba/abajo

# Parametros del filtro One Euro del cursor.
# min_cutoff bajo = mas suave en reposo (menos temblor) pero mas retraso.
# beta alto  = reacciona mejor al movimiento rapido.
CURSOR_MIN_CUTOFF = 1.2
CURSOR_BETA = 0.12
CURSOR_D_CUTOFF = 1.0

# Deadzone: movimiento minimo en pixeles de pantalla antes de mover el cursor.
DEADZONE_PIXELS = 2.0

# Frames consecutivos necesarios para CONFIRMAR un gesto (evita "lluvia de clicks").
CONFIRM_FRAMES = 2

# Umbral de tiempo (s) para considerar dos clicks consecutivos como doble click.
DOUBLE_CLICK_INTERVAL = 0.45

# Sensibilidad inicial: 1.0 = mapeo 1:1 entre la mano y el mouse.
# Valor bajo = movimientos precisos; valor alto = movimientos rapidos.
SENSITIVITY_DEFAULT = 1.2

# Tiempo sin actualizacion (s) para activar el failsafe (tracking fallido).
FAILSAFE_TIMEOUT = 2.0


# =============================================================================
# Estados del mouse
# =============================================================================

class MouseState:
    """Estados de la maquina de estados del mouse (plan v0.2, seccion 14)."""
    IDLE = "idle"            # nada: libre, listo para pinch
    MOVING = "moving"        # solo moviendo el cursor
    PRESSING = "pressing"    # pinch confirmado -> boton down
    DRAGGING = "dragging"    # boton abajo + movimiento
    RELEASING = "releasing"  # release confirmado -> boton up
    CLICK = "click"          # click simple completado
    DOUBLE_CLICK = "double_click"  # doble click completado


# =============================================================================
# Filtro One Euro (suavizado del cursor)
# =============================================================================

class _OneEuroFilter:
    """
    Filtro One Euro para una senal escalar.

    - min_cutoff: menor = mas suave en reposo (menos temblor) pero mas retraso.
    - beta: mayor = reacciona mas rapido cuando el movimiento es veloz.
    - d_cutoff: cutoff para la derivada (velocidad).

    Igual al de track.py pero autocontenido en mouse.py para que el modulo
    sea independiente.
    """

    def __init__(self, min_cutoff=CURSOR_MIN_CUTOFF, beta=CURSOR_BETA, d_cutoff=CURSOR_D_CUTOFF):
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


# =============================================================================
# Controlador principal del mouse
# =============================================================================

class MouseController:
    """
    Controlador del mouse del sistema.

    Recibe:
        - posicion normalizada de la mano (0.0 - 1.0, coordenadas de la
          camara, ya espejada si mirror=True)
        - estado del gesto (pinch / no pinch)

    Ejecuta sobre Windows:
        - movimiento del cursor (con smoothing + deadzone)
        - click izquierdo, drag, doble click
        - liberacion del boton

    La conversion final a coordenadas de pantalla, el espejo, la
    calibracion y la sensibilidad se hacen aqui: mouse.py es el unico
    intermediario entre VirtualChalk y el mouse de Windows.

    El pinch NO se ejecuta por un solo frame: se confirma despues de
    CONFIRM_FRAMES frames consecutivos (seccion 16 del plan).
    """

    def __init__(self, screen_width=None, screen_height=None, mirror=False):
        """
        Inicializa el controlador del mouse.

        Args:
            screen_width: ancho de pantalla en px (None = auto-detectar).
            screen_height: alto de pantalla en px (None = auto-detectar).
            mirror: si True, invierte el eje X: x_mouse = 1 - x_hand.
        """
        # Dimensiones de pantalla (seccion 17: calibration -> todo el monitor).
        info = autopy.screen.size()
        self.screen_w = screen_width or info[0]
        self.screen_h = screen_height or info[1]

        self.mirror = mirror          # seccion 7: espejo configurable

        # Filtros One Euro para suavizar el cursor (seccion 8).
        self._filter_x = _OneEuroFilter()
        self._filter_y = _OneEuroFilter()

        # Estado de la posicion del cursor.
        self._prev_smoothed = None    # ultima posicion suavizada en px de pantalla
        self._raw_pos = None          # ultima posicion cruda recibida (0.0-1.0)

        # Calibracion del area de movimiento (seccion 17).
        # Normalizado [cal_min, cal_max] -> pantalla [MARGIN, WIDTH-MARGIN].
        self._cal_min = (0.0, 0.0)
        self._cal_max = (1.0, 1.0)

        # Seccion 18: sensibilidad.
        self.sensitivity = SENSITIVITY_DEFAULT

        # Seccion 14/15: maquina de estados del mouse.
        self.state = MouseState.IDLE

        # Seccion 16: confirmacion de gestos.
        self._pinch_active = False    # pinch actualmente activo
        self._pinch_confirmed = False # pinch confirmado (>= CONFIRM_FRAMES frames)
        self._pinch_frames = 0        # frames seguidos con pinch
        self._release_frames = 0      # frames seguidos SIN pinch, despues de pinch activo

        # Seccion 11: doble click.
        self._last_click_time = 0.0
        self._click_count = 0
        self._awaiting_second = False # esperando segundo click en la misma posicion

        # Seccion 3: estado de arrastre.
        self._drag_start = None       # posicion donde empezaron pinchar
        self._is_dragging = False

        # Estado del boton (evita click/doubleclick sin presionar).
        self._button_pressed = False

        # Failsafe: ultima vez que se movio el cursor (seccion 25).
        self._last_move_time = time.perf_counter()

        # Estado de doble click en curso.
        self._double_click_active = False

    # ---------------------------------------------------------------------
    # Calibracion (seccion 17)
    # ---------------------------------------------------------------------
    def set_calibration(self, min_normalized, max_normalized):
        """
        Define el area de movimiento en coordenadas normalizadas (0.0-1.0).

        La esquina superior izquierda de `min_normalized` -> cursor en la
        esquina superior izquierda de la pantalla (con margen).
        La esquina inferior derecha de `max_normalized` -> cursor en la
        esquina inferior derecha de la pantalla.

        Args:
            min_normalized: (x, y) esquina superior izquierda de la camara.
            max_normalized: (x, y) esquina inferior derecha de la camara.
        """
        self._cal_min = tuple(min_normalized)
        self._cal_max = tuple(max_normalized)

    def reset_calibration(self):
        """Restaura la calibracion por defecto: toda la camara mapea a la pantalla."""
        self._cal_min = (0.0, 0.0)
        self._cal_max = (1.0, 1.0)

    def get_calibration(self):
        """Devuelve la calibracion actual como (min, max) normalizado."""
        return self._cal_min, self._cal_max

    # ---------------------------------------------------------------------
    # Conversion de coordenadas (seccion 6, 7, 17, 18)
    # ---------------------------------------------------------------------
    def _normalize_to_screen(self, nx, ny):
        """
        Convierte coordenadas normalizadas de camara a pixeles de pantalla.

        Pasos:
        1. Aplica la calibracion: mapea [cal_min, cal_max] -> [0, 1].
        2. Aplica el margen de pantalla (no llega a los bordes exactos).
        3. Aplica el espejo si mirror=True (x_mouse = 1 - x_hand).
        4. Aplica la sensibilidad (desplazamiento centrado en 0.5).

        Secciones 6-7, 17-18 del plan.
        """
        cal_w = self._cal_max[0] - self._cal_min[0]
        cal_h = self._cal_max[1] - self._cal_min[1]
        if cal_w <= 0 or cal_h <= 0:
            return None

        # 1) Mapear de [cal_min, cal_max] a [0, 1].
        norm_x = (nx - self._cal_min[0]) / cal_w
        norm_y = (ny - self._cal_min[1]) / cal_h

        # 2) Clamp al rango.
        norm_x = max(0.0, min(1.0, norm_x))
        norm_y = max(0.0, min(1.0, norm_y))

        # 3) Espejo configurable (seccion 7).
        if self.mirror:
            norm_x = 1.0 - norm_x

        # 4) Sensibilidad (seccion 18): desplazamiento centrado en 0.5.
        cx, cy = 0.5, 0.5
        norm_x = cx + (norm_x - cx) * self.sensitivity
        norm_y = cy + (norm_y - cy) * self.sensitivity

        # Clamp de nuevo tras la sensibilidad.
        norm_x = max(0.0, min(1.0, norm_x))
        norm_y = max(0.0, min(1.0, norm_y))

        # 5) Convertir a pixeles de pantalla.
        x = MARGIN + norm_x * (self.screen_w - 2 * MARGIN)
        y = MARGIN + norm_y * (self.screen_h - 2 * MARGIN)

        return (x, y)

    # ---------------------------------------------------------------------
    # Deadzone (seccion 9)
    # ---------------------------------------------------------------------
    def _apply_deadzone(self, x, y):
        """
        Si el movimiento es menor que DEADZONE_PIXELS, devuelve la posicion
        anterior (no mover el cursor).

        Se evitan micro-movimientos sin perder precision (seccion 9).
        """
        if self._prev_smoothed is None:
            return (x, y)
        dx = x - self._prev_smoothed[0]
        dy = y - self._prev_smoothed[1]
        if math.hypot(dx, dy) < DEADZONE_PIXELS:
            return self._prev_smoothed
        return (x, y)

    # ---------------------------------------------------------------------
    # Confirmacion de gestos (seccion 16)
    # ---------------------------------------------------------------------
    def _update_gesture_confirmation(self, is_pinch):
        """
        Filtro temporal de gestos: requiere frames consecutivos para
        confirmar (y para dar por perdido).

        Evita: PINCH / no pinch / PINCH / no pinch -> lluvia de clicks.

        Secciones 11, 15, 16 del plan.
        """
        if is_pinch:
            self._release_frames = 0          # el parpadeo no cuenta como soltar
            self._pinch_frames += 1
            if self._pinch_frames >= CONFIRM_FRAMES:
                self._pinch_confirmed = True
            return self._pinch_confirmed

        # Sin pinch: el pinch sigue "confirmado" hasta CONFIRM_FRAMES frames sin pinch.
        if self._pinch_confirmed:
            self._release_frames += 1
            if self._release_frames >= CONFIRM_FRAMES:
                self._pinch_confirmed = False
                self._release_frames = 0
                self._pinch_frames = 0
                return False
            return True
        self._release_frames = 0
        self._pinch_frames = 0
        return False

    # ---------------------------------------------------------------------
    # Movimiento del cursor (seccion 1, 8, 9)
    # ---------------------------------------------------------------------
    def move_cursor(self, norm_x, norm_y):
        """
        Actualiza la posicion del cursor basandose en la posicion de la mano.

        Flujo: convert -> smoothing -> deadzone -> autopy.mouse.move.

        Secciones 1, 6, 8, 9 del plan.

        Args:
            norm_x: coordenada X normalizada (0.0-1.0).
            norm_y: coordenada Y normalizada (0.0-1.0).

        Returns:
            (screen_x, screen_y) posicion actual del cursor en pantalla,
            o None si fallo la conversion.
        """
        now = time.perf_counter()
        self._last_move_time = now
        self._raw_pos = (norm_x, norm_y)

        pos = self._normalize_to_screen(norm_x, norm_y)
        if pos is None:
            return None

        # Smoothing: One Euro filter en X e Y (seccion 8).
        smoothed = (
            self._filter_x(pos[0], now),
            self._filter_y(pos[1], now),
        )

        # Deadzone (seccion 9).
        final = self._apply_deadzone(smoothed[0], smoothed[1])

        # Mover el cursor solo si la posicion cambio.
        if self._prev_smoothed is None or final != self._prev_smoothed:
            autopy.mouse.move(int(final[0]), int(final[1]))
            self._prev_smoothed = final
            if self.state == MouseState.IDLE:
                self.state = MouseState.MOVING

        return final

    # ---------------------------------------------------------------------
    # Gestion del pinch -> click / drag (secciones 10, 11, 15)
    # ---------------------------------------------------------------------
    def update_pinch(self, is_pinch):
        """
        Actualiza el estado del pinch y ejecuta la accion correspondiente.

        Comportamiento (seccion 10 del plan):
            PINCH             -> click izquierdo
            PINCH + movimiento -> drag
            RELEASE           -> soltar boton

        Comportamiento (seccion 11 del plan):
            dos PINCH rapidos -> doble click

        Secciones 3, 11, 14, 15, 16 del plan.

        Args:
            is_pinch: True si pulgar e indice estan juntos (gesto pinch).

        Returns:
            El estado actual de la maquina de estados.
        """
        confirmed = self._update_gesture_confirmation(is_pinch)

        now = time.perf_counter()

        # ---- Transicion: NO pinch -> pinch (presionar) -------------------
        if confirmed and not self._pinch_active:
            self._pinch_active = True
            self._pinch_frames = 0
            self._release_frames = 0
            self._is_dragging = False

            if self._button_pressed:
                # Ya estaba presionado -> arrastre
                self.state = MouseState.DRAGGING
            else:
                # Primer pinch: presionar boton
                self.state = MouseState.PRESSING
                self._button_pressed = True
                try:
                    _button(True)
                except Exception:
                    self._button_pressed = False

                self._drag_start = self._prev_smoothed
                self._click_count += 1

                # Seccion 11: verificar doble click.
                if (self._click_count >= 2
                        and (now - self._last_click_time) < DOUBLE_CLICK_INTERVAL):
                    self.state = MouseState.DOUBLE_CLICK
                    self._double_click_active = True
                else:
                    self._double_click_active = False

                self._last_click_time = now
                self._awaiting_second = True
                return self.state

        # ---- Durante pinch: arrastrar ------------------------------------
        if confirmed and self._button_pressed and self._prev_smoothed is not None:
            if self._drag_start is not None and self._is_dragging is False:
                # Movimiento significativo desde donde pinche -> empezar drag
                dx = self._prev_smoothed[0] - self._drag_start[0]
                dy = self._prev_smoothed[1] - self._drag_start[1]
                if math.hypot(dx, dy) > DEADZONE_PIXELS * 3:
                    self._is_dragging = True
                    self.state = MouseState.DRAGGING
                elif self.state == MouseState.PRESSING:
                    # Pinch sin movimiento -> reportar como click (el release lo confirma)
                    self.state = MouseState.PRESSING
            else:
                if self._is_dragging or self.state == MouseState.DRAGGING:
                    self.state = MouseState.DRAGGING
                elif self.state == MouseState.PRESSING:
                    self.state = MouseState.PRESSING
            autopy.mouse.move(int(self._prev_smoothed[0]), int(self._prev_smoothed[1]))

        # ---- Transicion: pinch -> RELEASE (soltar) -----------------------
        if not confirmed and self._pinch_active:
            self._pinch_active = False
            self._release_frames = 0

            if self._is_dragging:
                # Termina un arrastre
                self.state = MouseState.RELEASING
                try:
                    _button(False)
                except Exception:
                    pass
                self._button_pressed = False
                self._is_dragging = False
                self._drag_start = None
            elif self._button_pressed:
                # Click simple o segundo click del doble
                if self._double_click_active:
                    # Primer click del doble: el segundo click (presion) lo ejecuta.
                    pass
                try:
                    _button(False)
                except Exception:
                    pass
                self._button_pressed = False

                if self._double_click_active:
                    self.state = MouseState.DOUBLE_CLICK
                    self._double_click_active = False
                    self._click_count = 0
                    self._awaiting_second = False
                    self._last_click_time = 0.0
                else:
                    self.state = MouseState.CLICK
                    # Resetear cuenta de doble click despues del timeout
                    if now - self._last_click_time >= DOUBLE_CLICK_INTERVAL:
                        self._click_count = 0
                        self._awaiting_second = False
                        self._last_click_time = 0.0

        return self.state

    # ---------------------------------------------------------------------
    # Click y doble click explicitos
    # ---------------------------------------------------------------------
    def click(self, button="left"):
        """Ejecuta un click simple en la posicion actual del cursor."""
        if button == "left":
            autopy.mouse.click()
            self.state = MouseState.CLICK
        else:
            pyautogui.rightClick()
            self.state = MouseState.CLICK

    def double_click(self):
        """Ejecuta un doble click en la posicion actual del cursor."""
        autopy.mouse.click()
        time.sleep(0.05)
        autopy.mouse.click()
        self.state = MouseState.DOUBLE_CLICK

    # ---------------------------------------------------------------------
    # Scroll (seccion 13, implementacion base - gestos especificos despues)
    # ---------------------------------------------------------------------
    def scroll(self, direction, amount=3):
        """
        Ejecuta scroll del mouse.

        direction: 1 (arriba) o -1 (abajo).
        amount: unidades de scroll; pyautogui.scroll usa 120 por defecto.
        """
        pyautogui.scroll(-direction * amount * 120)

    # ---------------------------------------------------------------------
    # Click derecho (seccion 12 - reserva de gesto, API disponible)
    # ---------------------------------------------------------------------
    def right_click(self, pos=None):
        """Ejecuta click derecho. `pos` en px de pantalla (None = cursor actual)."""
        if pos is not None:
            autopy.mouse.move(pos[0], pos[1])
        pyautogui.rightClick()
        self.state = MouseState.CLICK

    # ---------------------------------------------------------------------
    # Estado
    # ---------------------------------------------------------------------
    def is_button_pressed(self):
        """True si el boton del mouse esta presionado en este momento."""
        return self._button_pressed

    def get_state(self):
        """Devuelve el estado actual de la maquina de estados."""
        return self.state

    def get_raw_position(self):
        """Devuelve la ultima posicion normalizada recibida (0.0-1.0) o None."""
        return self._raw_pos

    def get_screen_position(self):
        """Devuelve la posicion actual del cursor en px de pantalla."""
        try:
            pos = autopy.mouse.location()
            return (pos[0], pos[1])
        except Exception:
            return self._prev_smoothed

    # ---------------------------------------------------------------------
    # Failsafe (seccion 25)
    # ---------------------------------------------------------------------
    def check_failsafe(self):
        """
        True si el tracking no ha actualizado el cursor en > FAILSAFE_TIMEOUT
        segundos (posible fallo de tracking).

        Se recomienda que main.py devuelva el control al mouse en este caso.
        """
        return (time.perf_counter() - self._last_move_time) > FAILSAFE_TIMEOUT

    def reset(self, release_button=True):
        """
        Resetea el estado del controlador (por ejemplo al perder la mano
        o al cambiar de modo).

        Args:
            release_button: si True, suelta el boton del mouse si estaba
                            presionado (seccion 14: evitar boton sostenido).
        """
        if release_button and self._button_pressed:
            try:
                _button(False)
            except Exception:
                pass
            self._button_pressed = False
            self._is_dragging = False
            self._drag_start = None

        self.state = MouseState.IDLE
        self._pinch_active = False
        self._pinch_confirmed = False
        self._pinch_frames = 0
        self._release_frames = 0
        self._last_click_time = 0.0
        self._click_count = 0
        self._awaiting_second = False
        self._double_click_active = False
        self._filter_x.reset()
        self._filter_y.reset()


def list_screens():
    """
    Devuelve los tamaños de todas las pantallas.

    Returns:
        [(name, width, height), ...]
    """
    screens = []
    try:
        n = autopy.display.count()
        for i in range(n):
            w, h = autopy.display.size(i)
            screens.append((f"Pantalla {i + 1}", w, h))
    except Exception:
        screens.append(("Monitor principal", autopy.screen.size()[0],
                        autopy.screen.size()[1]))
    return screens