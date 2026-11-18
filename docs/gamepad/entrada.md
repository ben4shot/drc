# Informe de entrada (HID, UDP 50122 → 50022)

Formato: `libdrc/web/docs/re/sc-input.rst` y `libdrc/include/drc/input.h`.

## Lo que manda el mando [consola + mando]

- Un paquete de **128 B a ~175 por segundo** (no es una tasa exacta de 180 Hz).
- Botones (incluidos TV, HOME, L3/R3 y encendido), 2 sticks, táctil (10 muestras por paquete), acelerómetro,
  giroscopio, magnetómetro, nivel de batería, posición del deslizador de volumen y estado de energía.

## Valores

| campo | rango | notas |
|---|---|---|
| sticks | −1,0 … 1,0 en libdrc | cada mando necesita su calibración (en la EEPROM del UIC) |
| táctil | 12 bits en crudo (0…4095) por eje | libdrc devuelve la media de las 10 muestras, sin calibrar (`ts_raw_x/y`) y normalizada (0…1) |
| presión del táctil | 12 bits en crudo | depende de la posición, ver [tactil-y-lapiz.md](tactil-y-lapiz.md) |
| batería | 1 byte | `battery_charge` |
| volumen | 1 byte | posición del deslizador, no el volumen real |
| estado de energía | bits | `0x01` corriente, `0x02` botón de encendido pulsado, `0x40` cargando, `0x80` USB |

## Cómo llega a Linux en `drc` [mando]

`drc_stream` crea dispositivos `uinput`:

- un mando estándar (botones, sticks; Steam lo reconoce);
- «Wii U GamePad Touchscreen» (pantalla táctil), o «Wii U GamePad Pen» (tableta con presión) en modo lápiz;
- los sensores (acelerómetro y giroscopio) los lee el probador (`tools/gamepad_test.py`) del UDP directamente.

## Botones de la tapa del mando

- **HOME**, **TV** y **encendido** llegan en el informe como cualquier otro botón. El botón de encendido apaga el
  mando por su cuenta si se mantiene pulsado.
- **SYNC** (detrás) no llega a la consola: abre en el mando la pantalla de vinculación.
