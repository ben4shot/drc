# Pantalla LCD

854×480. LCD de JDI con controlador Panasonic **[firmware]**.

## Encender y apagar: `(5,0x08)` [firmware + mando]

El byte **no es un booleano** (LVC 0x36e82). Cada valor elige una combinación de 5 acciones:

| byte | efecto |
|---|---|
| `0x00` | apagado (modo 1) |
| `0x01` | apagado (modo 2) |
| `0x02` | apagado (modo 3) |
| **`0xff`** | **encendido** |
| otro | rechazado |

Se rechaza también si el módulo está ocupado o sin iniciar. Comprobado en el mando el 2026-11-16: `raw 5 8 ff`
vuelve a encender la pantalla. En `drc`, `set lcd_on 0` la apaga y `set lcd_on 1` manda `0xff`.

## Brillo: `(5,0x14)` y `(5,0x15)` [mando]

- `(5,0x14)`, 1 B: **fija** el brillo, de 1 (mínimo) a 5 (máximo). `set backlight 1…5`.
- `(5,0x15)`: **lee** el brillo. `get brightness`.

Los nombres de la consola (`LcdBrightnessIdGet` = 520 y `…IdSet` = 521) están **cruzados** respecto a lo que hace el
mando: manda lo que hace el mando.

## Otros [firmware]

| comando | carga | qué hace | estado |
|---|--:|---|---|
| `(5,0x0e)` / `(5,0x0f)` | 5 B | fijar/leer CABC (ahorro de energía por contenido) | lectura comprobada (`get lcd_cabc`) |
| `(5,0x19)` / `(5,0x1a)` | 13 B | fijar/leer la tabla de brillo del controlador del LCD (por I²C) | lectura en la lista blanca (`get lcd_table`) |

## Modos de salida

Ver [video.md](video.md) (854 px normales, 640 px para la cámara).
