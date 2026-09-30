# VirtualChalk

**VirtualChalk** es una interfaz digital que se controla mediante visión por computadora y gestos naturales de la mano.

El sistema utiliza una cámara estándar para detectar el movimiento y la pose de la mano en tiempo real, convirtiéndolos en acciones dentro de una pizarra o interfaz virtual.

---

## Características de la v0.1

- **Control gestual sin contacto:**
  - **Pinch (Pulgar + Índice):** Activa el modo de dibujo para trazar líneas en la pizarra.
  - **Gosto OK (Pulgar + Índice juntos con otros dedos extendidos):** Activa el borrador con pincel circular continuo.
  - **Limpieza total:** Sostener la pose `OK` con ambas manos simultáneamente limpia la pizarra por completo.
- **Calibración por Homografía (4 Puntos):** Ajuste preciso entre la perspectiva de la cámara y la superficie de proyección mediante OpenCV (`cv2.findHomography`).
- **Filtrado de movimiento suave:** Integración del algoritmo **One Euro Filter** para reducir el temblor (*jitter*) en la punta de los dedos sin añadir latencia apreciable.
- **Arquitectura Multihilo:** El seguimiento y procesamiento del modelo corren en un hilo secundario dedicado para garantizar un renderizado fluido a 60 FPS en Pygame.
- **Modo Debug Integrado:** Visualización en tiempo real del frame de la cámara, esqueleto de la mano y métricas de rendimiento (FPS e indicador de latencia extremo a extremo).

---

## Arquitectura del Sistema

CÁMARA → TRACKING (MediaPipe + OneEuro) → GESTOS (FSM) → CHALK RENDERER (Pygame) → PROYECTOR

Módulo     Responsabilidad
main.py  Coordinador principal. Conecta la captura, la lógica de gestos y la interfaz visual.
track.py Captura de frames, inferencia con MediaPipe Tasks (HandLandmarker), suavizado de coordenadas y cálculo de homografía.
gestures.py Reconocimiento y estabilización de gestos (PINCH, OK) mediante hisféresis y conteo de frames.
chalk.py Motor de renderizado en Pygame, gestión del búfer de la pizarra, HUD y calibración visual.
board.py Módulo de compatibilidad e interfaz con la pizarra persistente.

 Requisitos e Instalación
 -
Python 3.10+

Instalación
-
  1.Clonar el repositorio:
  -
    git clone [https://github.com/tu-usuario/VirtualChalk.git](https://github.com/Shiba-Souls/VirtualChalk.git)
    cd VirtualChalk
  2.Instalar dependencias:
  -
  (Al ejecutar por primera vez, el sistema descargará automáticamente el modelo hand_landmarker.task de MediaPipe si no se encuentra en el directorio raíz).
  
    pip install opencv-python mediapipe pygame numpy


Controles y Atajos de Teclado
Gestos con la mano:
-
Mover cursor: Mano abierta.

Dibujar: Juntar índice y pulgar (Pinch).

Borrar: Pose OK (Índice y pulgar juntos + otros 3 dedos extendidos).

Limpiar pizarra: Sostener la pose OK con ambas manos simultáneamente durante ~0.7 segundos.

Teclas de atajo
C: Iniciar/Repetir el proceso de calibración de 4 puntos.
M: Menu que incluye todo lo de arriba e integra el cambio de cámara si es que el dispositivo tiene mas de una.
P: Paleta de colores y sliders de resize interactivos para el puntero.
+/-: Atajo al resize directamente en el teclado.

F: Alternar entre pantalla completa y modo ventana.

D: Mostrar/Ocultar la ventana de cámara (solo en modo --debug).

ESC: Salir de la aplicación.
