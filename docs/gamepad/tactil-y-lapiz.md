# Pantalla táctil y modo lápiz

El panel es **resistivo** (no multitáctil): un solo punto, con presión.

## Valores en crudo [mando]

- Posición: 12 bits por eje (0…4095), 10 muestras por informe de entrada. Sin calibrar: la calibración está en la
  EEPROM del UIC y la consola la fija con `(5,0x1b)` `SetTpCalibConfig` (16 B, sin probar).
- Presión: 12 bits. **Sube con la posición** (sobre todo en x, y de forma curva), y la fuerza solo desplaza el nivel.

Medido el 2026-11-17 con `tools/pen_pressure.py`, después de restar la posición: toque suave ≈ −224, normal ≈ −32,
fuerte ≈ −10. El panel **satura enseguida** con una punta dura, así que normal y fuerte casi no se distinguen: es un
límite del hardware.

## Modelo de presión de `drc` [mando]

`src/drc_stream/pen_pressure.h`:

```
crudo ≈ nivel + kx·x + ky·y + kxx·x² + kyy·y² + kxy·x·y
```

Se resta la parte que depende de la posición, el nivel se lleva a 0…1 con un rango `[mín, máx]` (por defecto
−350…20), se aplica una curva `t^2` y un suavizado. Salida 1…1023 (0 = levantado). Un modelo lineal dejaba el centro
150–280 puntos más alto que los bordes y lo saturaba todo.

Ajustes: `DRC_PEN_PRESSURE_*` en `config/drc.conf`.

## Modos del táctil en `drc` (`DRC_TOUCH_MODE`)

| modo | qué ve el escritorio |
|---|---|
| `pointer` (por defecto) | ratón: el cursor va donde tocas y un toque es un clic (`touch2pointer`, atado al monitor del mando en extended/mirror con Hyprland) |
| `touch` | toques nativos (gestos en apps Wayland, sin cursor) |
| `pen` | tableta con presión («Wii U GamePad Pen») |

En `extended` con Hyprland el lienzo es el del mando (854×480): Hyprland asigna el dispositivo al monitor `DRC-1`.
Con el lienzo de la Pi (2774×1080) el punto caía desplazado.

Para ver lo que recibe el escritorio, lee el evdev «Wii U GamePad Pen» como root.
