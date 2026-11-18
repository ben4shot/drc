# Vídeo hacia el mando (vstrm, UDP 50120)

El formato de los paquetes está en `libdrc/web/docs/re/sc-vstrm.rst`. Aquí: lo que el mando comprueba, cuándo pide
resync y qué valores funcionan.

## Formato que acepta [firmware]

- H.264 de 854×480, con SPS/PPS fijos (el mando no los lee del flujo), un frame repartido en **5 trozos (chunks)**
  de un único flujo CABAC.
- Cabecera de 4 B (+4 B de timestamp opcional). El mando comprueba que `tamaño del paquete == carga + 12 (+4 si
  lleva ts)` y descarta lo que no cuadra.
- Cabecera extendida: `0x80` IDR, `0x82` frame rate, `0x83`/`0x84` forzar decodificación, `0x85` MbRows. La consola
  manda `80 82 fr 83 85 06…` en los IDR y `82 fr 83 85 06…` en los P; `drc` manda lo mismo **[consola]**.
- Número de secuencia de 10 bits (módulo 1024). Un hueco ⇒ error de secuencia.
- Anillo del decodificador: **162 416 B** y **30 trozos** como máximo en cola. Si no cabe ⇒ desbordamiento ⇒ reinicio.
  Un frame de ≤ 25 KB cabe de sobra.

## Rejilla de tiempo [firmware + consola]

- Periodo del vsync: **16 683 µs** (59,94 Hz). La consola pone los timestamps en esa rejilla (±1 µs).
- El mando ajusta la fase de su vsync con un PLL **solo con los timestamps de los paquetes de formato de astrm
  aceptados** ([audio.md](audio.md)), con una corrección máxima de unos ±3,2 µs por frame (±190 µs/s). Un salto de
  fase tarda decenas de segundos en recuperarse.
- La rejilla se ancla al (re)configurar el modo: `ts_astrm + 16000 µs` (mod 16 683).

## Estados de la salida y arranque tras un IDR [firmware]

| estado | significado |
|--:|---|
| 6 | reiniciado: espera un IDR y descarta el resto en silencio. **En este estado el mando pide resync en cada vsync (~60 por segundo)** |
| 2 / 4 / 5 | IDR aceptado, esperando el arranque (START) |
| 3 | marcha normal |

START se dispara con una alarma rearmada en cada vsync (15/18/15 ms) y compara `ts_IDR + 16000 µs` con el último
vsync local: si la diferencia es `≤ ±1000 µs` arranca; si llega tarde, reinicia (⇒ resync). Mientras no arranca,
cualquier paquete que no sea IDR provoca otro reinicio.

## Resync

El mando pide un IDR mandando `01 00 00 00` al puerto de mensajes (50110 → 50010). Lo manda cada vez que reinicia el
decodificador. Causas **[firmware]**:

1. hueco en el número de secuencia;
2. desbordamiento del anillo o de la cola de 30 trozos;
3. paquete no IDR antes de START;
4. START fallido (IDR fuera de la ventana de ±1 ms);
5. error del timestamp ≥ 1000 µs durante 2 frames seguidos;
6. reloj local sin sincronizar, o 2 vsync seguidos sin timestamp.

«Resync por segundo» no cuenta fallos: en el estado 6 sale uno por vsync. Para comparar configuraciones hay que
medir **ráfagas por minuto** e **IDR por ráfaga** **[mando]**.

La consola, en 60 s de juego: 13 resync y unos 5 IDR (a petición, de ~16 KB, con huecos antes y después)
**[consola]**.

## Lo que funciona [mando]

| ajuste de `drc` | valor | efecto medido |
|---|---|---|
| `DRC_TS_OFFSET_US` | 17000 | adelanto del timestamp ≈ instante de envío; tras un hueco de secuencia se recupera con 1 IDR (8 de 8). Con 21000, 3 de cada 8 tardaban 17–35 s |
| `DRC_TS_GRID` | 1 | timestamps en la rejilla de 16 683 µs |
| `DRC_IDLE_FILLER` / `DRC_FILLER` | 1 | si el codificador no llega a tiempo, se manda un P todo P_Skip. Las ráfagas de resync bajan de ~50/min a 0–2/min |
| `DRC_ALLOW_PSKIP`, `DRC_NO_INTRA_REFRESH` | 1 | la consola usa P_Skip (56 % de los macrobloques) y no refresco intra |
| `DRC_MV_RANGE` | 64 | vectores de movimiento largos (x264 llegaba a 251 px; la consola no pasa de 70×55) producían bloques desplazados en el mando |

Con el H.264 de la consola enviado por el transporte de `drc` (reproducción de una captura) se obtuvieron 2–4
ráfagas/min, 0,1–0,8 % del tiempo y 59,9 fps, con imagen nítida: el transporte y la sincronía están resueltos; lo
que queda es del codificador.

**Cambiar `DRC_TS_OFFSET_US` en caliente** deja al mando en el estado 6 (~52 resync/s) hasta volver a la fase con la
que se ancló: cada valor hay que probarlo en una sesión nueva **[mando]**.

Con `rtw89` en un PC el bucle de resync empieza con un offset de unos 19 700 µs (borde de aceptación del astrm,
latencia ≈ 3 ms). El valor óptimo para `rtw89` no está medido **[mando]**.

## Lo que hace la consola al codificar [consola]

- QP de 1 a 32 por macrobloque, en escalones (1, 12–14, 18–20, 24–26, 30–32), media ~12.
- Particiones pequeñas (16×8/8×16/8×8) solo en el 6,6 %.
- Frames estáticos: P de 341–763 B por trozo; el 78 % de los P sin macrobloques intra.
- El codificador es el chip DRH de la consola; el software solo fija `SetVideoEncodingRate` y
  `SetVideoEncodingHint(hint, peso de luminancia, mv_x, mv_y)`. Esos comandos **no llegan al mando**.

## Modos de salida del LCD [firmware]

| modo | total | activo | desplazamiento |
|--:|---|--:|--:|
| 0 | 950×488 | 854 | 96 |
| 1 | 1056×488 | 854 | 202 |
| 2 | 950×488 | 640 (4:3) | 203 |

El modo 2 es el de la cámara con `display≠0` ([camara.md](camara.md)).
