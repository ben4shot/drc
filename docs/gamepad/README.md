# El Wii U GamePad por dentro: lo que sabemos

Documentación de lo averiguado al hacer que un PC o una Raspberry Pi sustituya a la consola: valores, comandos,
formatos, tiempos y trampas. Complementa la documentación de libdrc (`libdrc/web/docs/re/`, del proyecto original),
que describe los protocolos básicos; aquí está sobre todo lo que **no** estaba allí o estaba incompleto.

## Cómo leer estas notas

Cada dato dice de dónde sale:

| marca | significa |
|---|---|
| **[mando]** | comprobado con un GamePad real |
| **[consola]** | capturado del enlace de una Wii U real con su GamePad |
| **[firmware]** | leído en el firmware del mando (análisis estático), sin comprobar en el aparato |
| **[hipótesis]** | coherente con lo anterior, sin demostrar |

Las direcciones del tipo `LVC 0x36e82` son posiciones dentro de la imagen principal del firmware del mando
(`LVC.bin`, ver [firmware.md](firmware.md)); sirven para volver a encontrar el código, no hace falta entenderlas para
usar el mando. El firmware no se incluye en este repositorio.

## Índice

| tema | archivo | estado en `drc` |
|---|---|---|
| Hardware, firmware, procesadores y sistema operativo del mando | [firmware.md](firmware.md) | — |
| Red Wi-Fi, vinculación (símbolos, PIN WPS) y parámetros del punto de acceso | [red-y-vinculacion.md](red-y-vinculacion.md) | funciona |
| Vídeo hacia el mando (vstrm): formato, tiempos, resync | [video.md](video.md) | funciona |
| Audio hacia el mando y micrófono (astrm) | [audio.md](audio.md) | funciona |
| Vibración | [vibracion.md](vibracion.md) | funciona |
| Botones, sticks, sensores, batería (informe de entrada) | [entrada.md](entrada.md) | funciona |
| Pantalla táctil y modo lápiz con presión | [tactil-y-lapiz.md](tactil-y-lapiz.md) | funciona |
| Pantalla LCD: encendido, brillo, CABC | [pantalla.md](pantalla.md) | funciona |
| Cámara | [camara.md](camara.md) | funciona |
| Consulta UVC/UAC (cámara, micrófono, modo del mando) | [consulta-uvc-uac.md](consulta-uvc-uac.md) | funciona |
| NFC | [nfc.md](nfc.md) | funciona (pass-through) |
| Infrarrojos: barra de sensores, IrDA, mando de TV | [infrarrojos.md](infrarrojos.md) | barra sí; IrDA no |
| Energía, batería, estado y LEDs | [energia.md](energia.md) | lectura sí |
| Encendido remoto (Wake-on-WLAN) | [wowl.md](wowl.md) | mando→equipo sí; equipo→mando no |
| Comandos genéricos (canal de comandos): tabla completa | [comandos.md](comandos.md) | lista blanca |
| Microcontrolador STM8 (UIC) | [stm8.md](stm8.md) | — |
| Órdenes peligrosas y trampas | [peligros.md](peligros.md) | — |

## Puertos UDP (resumen)

| puerto del mando | puerto de la consola | sentido | contenido |
|--:|--:|---|---|
| 50120 | 50020 | consola → mando / mando → consola | vídeo (vstrm); la cámara sube por aquí al 50020 |
| 50121 | 50021 | ambos | audio (astrm), paquete de formato de vídeo, bit de vibración |
| 50122 | 50022 | mando → consola | informe de entrada (HID) |
| 50123 | 50023 | ambos | comandos genéricos, consulta UVC/UAC, hora |
| 50110 | 50010 | ambos | mensajes cortos (resync) |

Direcciones IP en la red del mando: consola `192.168.1.10`, mando `192.168.1.11` **[consola]**.
