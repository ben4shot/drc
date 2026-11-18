# Cámara

Funciona en `drc`: `set camera 1|0`, ajustes con `cam …` y `drchost/drc_cam_v4l2.py` para verla como cámara web.

## Qué emite [mando]

- **H.264 de 640×480, ~10 fps**, en paquetes con el mismo formato vstrm que el vídeo (cabecera de 16 B con
  extensión), **del mando 50120 al 50020 de la consola**.
- Cabecera extendida de cada frame: `80 82 02 83 85 1e` (0x85 = 30 filas de macrobloques).
- El SPS no viaja: hay que fabricarlo (640×480: `67640020ac2b40501ec8`). PPS y cabeceras de slice, como las del
  vídeo de la consola.
- Caudal según la tasa del `(4,8)`: `0x03e8` → ~430 kbit/s, `0x0fbc` → ~1,7 Mbit/s, `0x2ee0` → ~2,65 Mbit/s.
- La cámara de la consola, en el creador de Miis, emite **en ráfagas** de 1–4 s, no en continuo **[consola]**.
- Lente fija: no hay enfoque.

## Secuencia de apertura (la de la consola) [consola + mando]

Capturada en claro vinculando la Pi a una Wii U como si fuera un GamePad y abriendo el creador de Miis:

1. `(4,9)`: leer el modo de cámara. Devuelve `00 00 17 be` con el sensor apagado y `00 04 17 be` encendido (**bit
   4 del byte 1 = sensor encendido**).
2. `(4,8)` `00 02 0f bc` = display 0, fps 2, tasa `0x0fbc`.
3. Consulta UVC/UAC con **byte 16 = 1 y byte 17 = 1** (el resto de los ajustes a 0):
   `010000000000001600190000803e000001010002000000000000000070000000404003002d0000010000000000000000`
4. Consulta UVC/UAC con **byte 16 = 1 y byte 17 = 0**, copiando los registros del sensor que devolvió el mando
   (respuesta, bytes 4–7 → consulta, bytes 20–23; respuesta, bytes 8–11 → consulta, bytes 40–43):
   `010000000000001600190000803e0000010000029e0000000000000070000000404003002d0000018000400000000000`

Apagar: bytes 14–17 de la consulta a 0.

Errores que impedían que saliera vídeo antes de copiar la secuencia de la consola:

- modo 2 en el byte 16, en vez de 1;
- byte 17 fijo a 1, cuando es un **pulso** (1 y luego 0);
- otra tasa en el `(4,8)`;
- los bytes 18–47 de la consulta a cero: con ceros el sensor no volvía a encenderse.

## `(4,8)` y `(4,9)` [firmware + mando]

- `(4,8)`, 4 B `{display, fps, tasa u16 BE}`: **no arranca nada**, solo guarda opciones. Lo único que tiene efecto
  en el modo 1 es la **tasa**. `display` (espejo y salida de 640 px) y `fps` solo se aplican en el modo 2, y aun así
  la cámara sale siempre a 10 fps.
- `(4,9)`: devuelve el modo y la tasa. Tras abrir pasa a `00 04 00 06 0f bc`.

No hay más comandos de cámara: toda la configuración va en la consulta UVC/UAC **[firmware]**.

## Ajustes (bytes de la consulta UVC/UAC) [firmware + mando]

| bytes | ajuste | registros del sensor | valor de la consola | comprobado |
|---|---|---|---|---|
| 16 | modo de cámara (1 o 2; 0 = apagada) | — | 1 | ✅ |
| 17 | pulso de apertura (1 y luego 0): reinicia las ganancias y pone el sensor en marcha | 0x0e | 1→0 | ✅ |
| 18 | ¿antiparpadeo? (0 → bit 7, 1 → bit 6, 2 → nada) | 0xEC | 0 | — |
| 19 | exposición: 2 = automática | 0x13 bit 0 | 2 | ✅ |
| 20–23 | exposición manual (si byte 19 ≠ 2) | 0x0F/0x10 | eco de la respuesta | ✅ 0x28 oscuro, 0x5dc claro |
| 24–25 | brillo (s16) | 0xE3, signo 0xE4 bit 3 | 0 | ✅ ±100 |
| 26–27 | contraste | 0xE1/0xE2 | 0 | ✅ +60 |
| 28–29 | ganancia: nibble alto de 28 = 7 → automática; si no, manual (< 0x70) | 0x13 bit 2, 0x15, 0x00 | 0x70 | ✅ |
| 30–31 | tono | 0xDA, 0xDB/0xDC, 0xE4 | 0 | sin cambio visible |
| 32 / 33 | saturación V / U | 0xDE / 0xDD | 0x40/0x40 | ✅ 0 = blanco y negro |
| 34 | nitidez 1…7 | 0x86 | 3 | sin cambio visible |
| 36–37 | gamma (umbral → tabla 0…5) | 0x9C…0xAB | 0x2d | sin cambio visible |
| 38 bit 0 | reinicia la captura | — | 0 | sin efecto visible |
| 39 | balance de blancos: 1 automático; 0 → manual con 40 (azul) y 42 (rojo) | 0x13 bit 1, 0x01/0x02 | 1 | ✅ |
| 40–43 | ganancias azul/rojo | 0x01/0x02 | eco de la respuesta | ✅ |

Respuesta del mando: bytes 4–7 = exposición actual; bytes 8–11 = ganancias azul/rojo actuales. Cambian en vivo
(balance automático).

`drc`: `cam exposure auto|<v>`, `cam gain auto|<v>`, `cam wb auto|<b r>`, `cam brightness`, `cam contrast`,
`cam saturation`, `cam bitrate`.

## Trampas [mando]

- Con el sensor encendido aparecen **líneas blancas finas y una mancha** en la pantalla del mando, sea cual sea la
  imagen. Desaparecen al apagar la cámara. No dependen del modo ni del vídeo.
- Mide la cámara **≥ 4 s después de abrirla**: al principio salen cifras transitorias (30 fps, frames sin
  decodificar).
- `uvc off` (consulta en crudo desactivada) **no apaga la cámara**: usa `set camera 0`.
- En la Raspberry Pi, `/dev/video10…31` son el códec por hardware: no los uses como salida de `v4l2loopback`.
