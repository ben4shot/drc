# Audio (astrm, UDP 50121)

Formato base: `libdrc/web/docs/re/sc-astrm.rst`. Cabecera de 8 B: formato (3 bits), mono, **vibrate**, tipo
(0 = audio, 1 = formato de vídeo), secuencia (10 bits), tamaño de la carga (16 bits) y timestamp (32 bits).

## Audio hacia el mando

- PCM de 16 bits a 48 kHz en estéreo (formato 1). El primer byte de la cabecera vale `0x23` en un paquete normal.
- La consola **no manda nada en silencio** **[consola]**; `drc` tampoco (`DRC_AUDIO_SEND_SILENCE=0`), salvo cuando
  hace falta para la vibración ([vibracion.md](vibracion.md)).
- `DRC_AUDIO_BUFFER_SIZE=9600` es el valor que va bien en el PC **[mando]**.

## Paquete de formato de vídeo (tipo 1)

Uno por frame de vídeo, con el timestamp del frame. Es el que **sincroniza el reloj del mando con el vídeo**
**[firmware]**:

- Se acepta solo si `0 ≤ reloj_local − ts < 8000 µs`.
- Si se acepta, fija el desfase del reloj (21 000 µs) y el timestamp de referencia con el que el PLL del vsync
  corrige la fase ([video.md](video.md)).
- Por eso importa la fase relativa astrm ↔ vídeo (±1 ms), y por eso el adelanto óptimo de `DRC_TS_OFFSET_US` es una
  ventana y no un punto.
- `DRC_ASTRM_TS_MATCH=1` hace que el paquete de formato lleve el mismo timestamp que el frame **[mando]**.
- La palabra que la documentación original llama «desconocida» vale 16000 y el mando la usa como retardo del
  arranque (`ts_IDR + 16000`) **[firmware]**.

Hay notas contradictorias sobre si la consola manda el paquete de formato antes o después del último trozo del
frame. `drc` lo manda antes.

## Silenciar el audio [firmware + mando]

- `(5,0x0a)`, 1 B: silencia (0) o activa el audio del mando. `set audio_on 0|1` en `drc`.
- `(5,0x0b)`: lee el estado (devuelve -1 o 0). `get audio_mute`, comprobado en el mando.
- `(5,0x1d)`, 32 B: forzar volumen (`SetVolumeOverride` de la consola). Sin probar.

## Micrófono

- Va por astrm desde el mando hacia la consola y se activa con la consulta UVC/UAC (byte 4 = activar micrófono,
  5 = silencio, 6–9 = volúmenes, 12–15 = muestreo; ver [consulta-uvc-uac.md](consulta-uvc-uac.md)).
- `(4,0x0a)` / `(4,0x0b)`: fijar/leer el modo del micrófono (1 B). La lectura está comprobada en el mando
  (`get mic_mode`).
- `drc`: `set mic 0|1`. El probador (`tools/gamepad_test.py`) lo graba.
- El firmware tiene cancelación de eco y selección automática de micrófono **[firmware]**.
