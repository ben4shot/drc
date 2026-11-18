# Infrarrojos

Tres cosas distintas: la barra de sensores (LEDs para el Wiimote), IrDA (datos) y el mando de TV.

## Barra de sensores: `(5,0x01)` [mando]

1 B: 1 la enciende, 0 la apaga (GPIO5). Comprobado el 2026-11-08 con la cámara de un móvil (las luces IR se ven en
la pantalla del móvil). En `drc`: `set sensorbar 0|1`.

## IrDA: `(5,0x11)`, hasta 520 B

Primer byte: 0 conectar, 1 enviar, 2 recibir, 3 desconectar.

| orden | resultado en el mando |
|---|---|
| `(5,0x16)` con `01` (activar) | `00`, aceptado |
| conectar: `00`, timeout u32 BE (1000 ms), velocidad 9600 (`0x0b`), tipo cualquiera (2) | `00`, aceptado |
| **enviar** (`01`, tamaño BE, 8 B de datos) **sin las cabeceras `0xa5…` del protocolo** | **el mando deja de contestar y muestra el error 165-6421**; hay que apagarlo |

El formato correcto del envío (con cabeceras `0xa5…`) no se ha leído en el firmware. **No mandes la orden de envío
hasta conocerlo.** `drc` no la tiene en su lista blanca.

## Mando de TV [firmware]

| comando | carga | qué hace |
|---|--:|---|
| `(5,0x16)` | 1 B | activar (0) / desactivar (−1) el modo de mando de TV |
| `(5,0x17)` | 7 B | enviar un código de prueba (5 B = identificador del mando de TV) |
| `(5,0x18)` | 10 B | fijar los dos identificadores (5 B cada uno) |

Sin probar.

## Modo Wii

La consulta UVC/UAC con el byte 3 a 1 también enciende las luces de la barra, pero mete al mando en modo Wii (ver
[consulta-uvc-uac.md](consulta-uvc-uac.md)). Para la barra, usa `(5,0x01)`.
