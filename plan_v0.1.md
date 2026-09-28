# VirtualChalk — Plan v0.1

## 1. Concepto

VirtualChalk es una interfaz digital proyectada sobre una superficie física, controlada mediante una cámara y gestos de una mano.

La cámara reemplaza la necesidad de un sensor al ras de la pared. El sistema detecta la mano y convierte su posición y forma en acciones dentro de una interfaz virtual, que un proyector muestra sobre la superficie.

No es un sistema de projection mapping. Es una **interfaz espacial proyectada**.

## 2. Objetivo de la v0.1

Demostrar el ciclo completo del concepto, con lo mínimo necesario para que sea creíble:

> Una persona se para frente a la superficie, mueve el cursor con la mano, arrastra un archivo al Board, lo abre, dibuja encima, borra el dibujo y cierra el contenido, todo con la cámara y sin mouse ni teclado.

El archivo original nunca se modifica por las acciones dentro del Board. Las anotaciones son una capa independiente.

## 3. Decisiones de diseño fijadas para v0.1

Estas decisiones estaban abiertas en el documento original y ahora quedan cerradas.

| Tema | Decisión | Motivo |
|---|---|---|
| Detección de "toque" | **Gestos en el aire.** No se detecta contacto con la superficie. | Una cámara 2D no distingue de forma fiable si el dedo toca la pared. |
| Manos | **Una sola mano** (`max_num_hands=1`). | Los gestos con dos manos están previstos para v0.3. Reduce carga y ambigüedad. |
| Selección de archivos | **Carpeta fija** `board_files/`. | Un explorador completo con gestos es difícil de hacer usable. Queda para v0.2. |
| Cambio de herramienta | **Botones virtuales** en pantalla, activados con pinch. | Evita gestos extra que se confundan con los existentes. |
| Calibración | **Tocar 4 puntos proyectados** con la punta del índice. | Más simple y robusto que detectar marcadores con OpenCV. |
| Suavizado | **Filtro incluido desde el inicio** (exponencial o One Euro). | Sin filtro el cursor tiembla y la demo no es usable. |
| Imagen de cámara | **Espejada horizontalmente**. | El movimiento resulta natural para quien mira la superficie. |

**Limitación conocida:** al trabajar en el aire sin detectar contacto, el brazo se cansa con el uso prolongado y el pinch se reconoce peor si la mano queda de perfil respecto a la cámara. Se acepta para v0.1.

## 4. Arquitectura

```
CAMERA → TRACKING → GESTURE RECOGNITION → BOARD STATE → RENDER → VENTANA → PROYECTOR → SUPERFICIE
```

Todo el procesamiento (tracking, gestos, archivos, dibujo) ocurre en backend. La ventana de Pygame es el único frontend visible. El frame de cámara nunca se proyecta.

**[CAMBIO] Hilos.** La captura y el tracking corren en un hilo aparte. El render usa siempre la última lectura disponible, de modo que la interfaz sigue fluida aunque el tracking se retrase puntualmente.

```
HILO 1 (tracking):  cámara → MediaPipe → landmarks suavizados ─┐
                                                                ├─ última lectura
HILO 2 (principal): gestos → Board → Chalk → render Pygame ◄───┘
```

## 5. Hardware objetivo

- CPU Intel Core i5-1235U, 8 GB de RAM, GPU Intel UHD integrada.
- Cámara web y proyector convencional.
- Superficie plana y de color claro.

Parámetros iniciales:

- Tracking: cámara a 640x480, objetivo ~30 FPS.
- Render: 1280x720.
- MediaPipe Hands con `model_complexity=0` y una sola mano.

**[CAMBIO] Métrica principal: latencia.** Se mide de extremo a extremo (movimiento de la mano → movimiento del cursor), no solo FPS. Referencia: por encima de ~100 ms la interacción empieza a sentirse mal aunque los FPS sean altos.

## 6. Tecnologías

**Núcleo:** Python, OpenCV (`opencv-python`), MediaPipe, Pygame, NumPy.

**Biblioteca estándar:** `pathlib`, `os`, `json`, `threading`.

La versión concreta de MediaPipe se fija en la implementación según compatibilidad con la versión de Python instalada. No se usa TouchDesigner, Resolume, MadMapper ni ningún software externo de projection mapping.

## 7. Estructura del proyecto

```
VirtualChalk/
├── main.py
├── track.py
├── gestures.py
├── chalk.py
├── board.py
├── config.json          # calibración y ajustes (se genera al calibrar)
└── board_files/         # carpeta fija de archivos disponibles
```

Se mantiene compacta. No se crean módulos adicionales hasta que una parte lo necesite de verdad. `board.py` es el candidato más probable a crecer demasiado; se vigila, pero no se divide ahora.

### Responsabilidades

| Archivo | Responsabilidad | No debe hacer |
|---|---|---|
| `main.py` | Inicializar sistemas, ejecutar el loop, conectar los módulos, controlar el cierre. | Lógica de MediaPipe, gestos, archivos o dibujo. |
| `track.py` | Capturar frames, detectar la mano, entregar landmarks suavizados en coordenadas normalizadas. | Decidir qué significa un gesto. |
| `gestures.py` | Convertir landmarks en gestos y acciones estables. | Acceder a la cámara o al Board. |
| `board.py` | Objetos, ventanas, selección, movimiento, apertura, cierre y archivos. | Detectar gestos o dibujar trazos. |
| `chalk.py` | Trazos, colores, grosor, borrado y capa de anotaciones. | Decidir cuándo dibujar (lo dicen los gestos y el modo activo). |

## 8. Sistema de coordenadas

El tracking entrega coordenadas normalizadas (0.0 a 1.0), lo que desacopla las resoluciones de cámara, Board y proyector.

```
CAMERA SPACE → NORMALIZED SPACE → BOARD SPACE → PROJECTOR SPACE
```

**[CAMBIO]** La homografía obtenida en la calibración convierte de espacio de cámara a espacio del Board. Las anotaciones se guardan en coordenadas **relativas al contenido** (no a la pantalla), para sobrevivir a cambios de tamaño de ventana.

## 9. Gestos

**[CAMBIO]** Se definen reglas explícitas para eliminar los solapamientos del documento original.

| Gesto | Definición | Acción |
|---|---|---|
| **OPEN HAND** | Los cinco dedos extendidos. | Mover el cursor. |
| **PINCH** | Pulgar e índice juntos, otros dedos libres. | Seleccionar / pulsar botón virtual. |
| **GRAB** | Mano cerrada, iniciada **desde un pinch sobre un objeto**. | Agarrar y mover. |
| **RELEASE** | Mano vuelve a abrirse. | Soltar. |
| **DRAW** | Solo el índice extendido y los demás dedos cerrados. Solo en modo Dibujo. | Trazar. |

Reglas de estabilidad:

- Ningún gesto importante se activa por un solo frame. Debe mantenerse un mínimo de frames consecutivos.
- **Histéresis:** el umbral para entrar en un gesto es distinto (más estricto) que para salir de él, para evitar parpadeos.
- Si la mano desaparece, se sale del gesto actual de forma segura (ver sección 12).

### Modos

La herramienta activa se cambia con **botones virtuales** pulsados con pinch:

- **Mover / Seleccionar** (modo por defecto).
- **Dibujar.**
- **Borrar anotaciones.**

En modo Dibujo el gesto DRAW traza; en modo Mover el mismo gesto no hace nada. Así se evita que el cursor deje trazos por accidente.

## 10. Board, archivos y ventanas

Una sola ventana de Pygame. Dentro viven los objetos y las ventanas virtuales; no se abre ninguna ventana del sistema por archivo.

Un archivo se convierte en un **objeto del Board** con: ruta original, tipo, posición, tamaño, estado de selección, ventana asociada, capa de anotaciones y visibilidad.

### Selección de archivos (v0.1)

**[CAMBIO]** El contenido de `board_files/` aparece como iconos en un panel lateral. El usuario los arrastra al Board (pinch + grab + release). No hay explorador de carpetas todavía.

### Visualización

- Imágenes: PNG, JPG/JPEG (WebP opcional).
- Texto: TXT y código fuente básico (PY) como texto plano.

Las imágenes se cargan una vez y se cachean; nunca se recargan por frame.

## 11. Chalk (dibujo y anotaciones)

Las anotaciones son una capa independiente encima del contenido original:

```
CONTENIDO ORIGINAL + CHALK LAYER = RESULTADO VISUAL
```

Funciones de v0.1: crear trazos, cambiar grosor y color, borrar anotaciones (limpiar la capa).

Formato de guardado opcional, junto al original:

```
image.png
image.virtualchalk    # JSON
```

El `.virtualchalk` contiene: ruta relativa del original, fecha de modificación del original y la lista de trazos en coordenadas relativas al contenido.

## 12. Seguridad frente a acciones accidentales

Principio heredado del documento original, con reglas concretas:

- **No hay puño = borrar.** La eliminación de objetos usará arrastre a una zona de eliminación (papelera) con confirmación. Queda para v0.2.
- **En v0.1, "cerrar" un objeto solo lo quita del Board.** No borra ni modifica el archivo original.
- **Pérdida de tracking a mitad de un gesto:**
  - Arrastre: el objeto se suelta en su última posición válida.
  - Trazo: se corta el trazo, no se continúa.
  - Nunca se dispara una acción destructiva al perder la mano.
- **Objetivos generosos:** botones y objetos de al menos ~80-100 px a 1280x720, con zona de tolerancia e histéresis al entrar/salir de un objeto.

## 13. Calibración

Se realiza al iniciar o cuando cambia la posición del proyector o la cámara.

Proceso:

1. La ventana proyecta un marcador en cada una de las 4 esquinas, uno a la vez.
2. El usuario toca cada marcador con la punta del índice y confirma con pinch.
3. Se guardan los 4 pares de correspondencias (posición del dedo en la cámara ↔ posición conocida del marcador en el Board).
4. Se calcula la homografía con OpenCV (`cv2.findHomography`).
5. Se guarda en `config.json`.

Al iniciar, si existe una calibración guardada se carga y se ofrece recalibrar. Debe haber una forma de forzar la recalibración (por ejemplo, un botón virtual y una tecla de desarrollo).

## 14. Feedback visual

**[CAMBIO]** Sin feedback el usuario no distingue entre "el sistema falló" y "hice mal el gesto". Indicadores mínimos en v0.1:

- Cursor con forma distinta según el gesto reconocido.
- Indicador de "mano detectada / no detectada".
- Anillo o barra que se llena mientras se confirma un pinch o grab.
- Resaltado del objeto bajo el cursor y del objeto seleccionado.
- Modo activo (Mover / Dibujar / Borrar) siempre visible.

## 15. Modo desarrollo

**[CAMBIO]** Un modo aparte, activado por argumento o tecla, que **no** forma parte de la experiencia final:

- Ventana con el frame de cámara, landmarks y gesto detectado superpuestos.
- Board en ventana normal (sin proyector).
- Teclas de atajo para salir, recalibrar y mostrar FPS y latencia.

No contradice la ventana única: en uso normal solo existe la ventana del Board.

## 16. Manejo de errores y arranque

Casos a contemplar desde el inicio:

- Sin cámara o cámara ocupada por otra aplicación.
- Proyector no conectado (el Board se muestra en el monitor principal).
- `config.json` ausente o corrupto (se pide calibrar).
- Archivo de `board_files/` ilegible o de formato no soportado (se muestra un icono de error, no se cierra el programa).

Fullscreen en el proyector: resolver pronto la elección de pantalla, el modo sin bordes y el escalado de DPI de Windows, porque afectan a la calibración.

## 17. Optimización

- Cámara a 640x480 y MediaPipe sin procesar imágenes innecesarias.
- Tracking en hilo separado del render.
- Imágenes cacheadas, con `convert()` / `convert_alpha()` al cargarlas.
- No cargar archivos dentro del loop.
- Cantidad de objetos abiertos limitada a un número razonable.
- Sin efectos visuales pesados. **Latencia > complejidad visual.**

## 18. Hitos de implementación

**[CAMBIO]** La lista de v0.1 se divide en hitos demostrables por separado. Cada uno valida la base del siguiente.

### Hito A — Cursor calibrado
- Captura de cámara, MediaPipe (una mano), suavizado.
- Ventana Pygame en el proyector y calibración de 4 puntos.
- Cursor que sigue la punta del índice sobre la superficie.
- Modo desarrollo con landmarks y medición de FPS/latencia.

**Criterio de éxito:** el cursor proyectado cae donde está el dedo, con latencia aceptable y sin temblor apreciable.

### Hito B — Gestos y objetos de prueba
- Reconocimiento de OPEN HAND, PINCH, GRAB y RELEASE con estabilidad temporal e histéresis.
- Objetos de prueba (rectángulos) que se seleccionan, se arrastran y se sueltan.
- Feedback visual y manejo de pérdida de tracking.

**Criterio de éxito:** se puede arrastrar un objeto de un lado a otro de forma fiable y sin soltarlo por error.

### Hito C — Archivos
- Panel con el contenido de `board_files/`.
- Arrastrar un archivo al Board para crear su objeto.
- Visores de imagen y de texto en ventana virtual, con botón de cierre.
- Mover ventanas.

**Criterio de éxito:** una imagen y un TXT se pueden traer al Board, abrir, mover y cerrar.

### Hito D — Dibujo
- Botones de modo (Mover / Dibujar / Borrar).
- Gesto DRAW, trazos sobre el contenido, grosor y color.
- Borrar anotaciones.
- Guardado opcional en `.virtualchalk`.

**Criterio de éxito:** se puede dibujar sobre una imagen, borrar el dibujo y cerrar el contenido sin alterar el archivo original.

## 19. Fuera de alcance de v0.1

Multiusuario, tracking corporal, reconocimiento de voz, efectos visuales pesados, edición avanzada de documentos, integraciones externas, explorador de carpetas con gestos, eliminación de objetos con papelera, deshacer/rehacer, zoom y rotación.

## 20. Hoja de ruta posterior

**v0.2:** explorador de carpetas, papelera con confirmación, deshacer/rehacer, guardado de anotaciones por defecto, zoom con dos dedos, redimensionar y rotar, menú contextual, mejor suavizado y calibración, más tipos de archivo.

**v0.3:** PDF, audio, video, visor de código con resaltado, varias ventanas simultáneas, pestañas, selección múltiple, herramientas avanzadas de dibujo, gestos con ambas manos.

**Futuro:** teclado virtual, control de música, dibujo 3D, proyección sobre objetos, tracking corporal, multiusuario, varias superficies, interacción con objetos físicos, integración con otros proyectos.

## 21. Riesgos conocidos

| Riesgo | Mitigación |
|---|---|
| La proyección interfiere con la detección de la mano. | Fondo oscuro o neutro en la interfaz; probar pronto; considerar modo de bajo brillo. |
| Cansancio del brazo por gestos en el aire. | Objetivos grandes, gestos cortos; aceptado como limitación de v0.1. |
| Pinch poco fiable con la mano de perfil. | Umbrales con histéresis; indicar cuándo la mano no se ve bien. |
| Latencia alta en el i5-1235U con iGPU. | Hilo de tracking separado, `model_complexity=0`, una sola mano, medir latencia desde el Hito A. |
| Calibración que se descalibra al mover el equipo. | Guardado en JSON y recalibración rápida siempre disponible. |
| Problemas de pantalla/DPI en Windows con el proyector. | Resolverlo en el Hito A, antes de construir sobre él. |

## 22. Filosofía (sin cambios)

1. Baja latencia.
2. Interacción natural.
3. Código simple.
4. Pocos módulos.
5. Bajo consumo.
6. Separación clara de responsabilidades.
7. Seguridad frente a acciones accidentales.
8. Independencia de hardware especializado.
9. Una única interfaz visible.
10. Backend invisible para el usuario.

La meta no es una demo llena de efectos. La meta es una interfaz realmente interactiva.