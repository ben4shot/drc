# Hardware y firmware del mando

El firmware del GamePad no está cifrado. Se puede leer una vez extraído del almacenamiento de la consola. Las
herramientas para analizarlo están en el repositorio `wiiu-gamepad-research` (`tools/`). El volcado tienes que
obtenerlo tú; no se distribuye.

## Imágenes del firmware

| imagen | procesador | dirección de carga | contenido |
|---|---|---|---|
| `LVC.bin` | ARM926EJ-S (ARM + Thumb) | 0 | sistema operativo, drivers, aplicación, pila NFC, WPS |
| `WIFI.bin` | Cortex-M3 (Thumb-2) del chip Broadcom | archivo − 0x20 (cabecera de 32 B) | firmware de la radio |
| `UMI.bin` | STM8L15x | 0x9000 | microcontrolador «UIC»: botones, sticks, táctil, energía, LEDs (ver [stm8.md](stm8.md)) |
| `VER.bin` | — | — | versión (4 B) |

Versión analizada: `0x190c0117` **[firmware]**. `(0,0x00)` del canal de comandos devuelve la versión en marcha y la
instalada **[mando]**.

## Chips (por los nombres de los drivers) [firmware]

| función | chip |
|---|---|
| Wi-Fi | Broadcom BCM4337 (SDIO) |
| Cámara | OmniVision (mapa de registros tipo OV7725/OV7740, VGA) |
| Audio | códec TI TLV320AIC3xxx |
| LCD | JDI R69208 + Panasonic MN838985 |
| Flash de configuración | SPI tipo x25 |
| Botones/sensores/energía | STM8L15x |

## Sistema operativo [firmware]

µITRON (kernel propio; llamadas por SWI). 53 tareas, 6 buzones, 12 pools, 43 grupos de flags, 40 semáforos, 5
cíclicos y 13 interrupciones. Datos que importan para el streaming:

- La recepción del vídeo (`SC_VSTRM_RECEIVE`/`SEND`) corre con **menos prioridad** (27–28) que el decodificador (12)
  y la salida de audio (3).
- El buzón de sincronía admite 4 mensajes y solo deja **un resync en vuelo** a la vez.
- Ciclos: contador de segundos 1 s, monitor de pérdida Wi-Fi 1 s, vídeo 5 s.

## Estados de la aplicación del mando [firmware]

activo, en segundo plano, subactivo, **modo Wii**, ajustes, vinculación, actualización de firmware y ajuste de
WoWL. Cada estado tiene su propia cola de comandos.

## Bloques de hardware (por uso en el código) [firmware]

| dirección | qué es |
|---|---|
| `0xf0000400` | contador de 1 µs (reloj local) |
| `0xf0008440…8c40` | decodificador H.264 por hardware (anillo de slices) |
| `0xf0009000…9800` | salida de vídeo/LCD, generador de vsync y su PLL; también lo lee la captura de cámara |
| `0xf0002000…2100` | DMA y sensor de cámara |
| `0xf0003000…3100` | LCD |
| `0xf0005114` | GPIO4 = motor de vibración, GPIO5 = alimentación de la barra de sensores (de libdrc `gpio.rst`) |

## Reloj del mando [firmware]

El reloj local de 1 µs se pone en hora con el TSF del chip Wi-Fi: el firmware de la radio publica un evento con el
TSF cada ~5 ms; si la diferencia supera 8 ms, salta, y aplica una corrección fija de +0,27 %. Por eso el punto de
acceso debe exponer un TSF estable, y con `rtw89` hace falta el parche de `drc-wifi-drivers` (sin él el mando se
asocia pero no muestra imagen) **[mando]**.

## Comandos de actualización

El servicio 0 del canal de comandos (métodos 2–5 y 0x0b–0x0f) escribe firmware y EEPROM. **No enviarlos.** `drc` los
deja fuera de su lista blanca (ver [comandos.md](comandos.md)).
