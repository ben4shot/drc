# Órdenes peligrosas y trampas

## Cuelgan el mando (hay que apagarlo y encenderlo) [mando]

| orden | qué pasa |
|---|---|
| IrDA **enviar** (`(5,0x11)` con primer byte 1) sin las cabeceras `0xa5…` | deja de contestar y muestra el error **165-6421** |
| consulta UVC/UAC con el **byte 3 ≠ 0** | entra en **modo Wii** (pantalla de carga, luces IR) y no vuelve solo |

Después de un cuelgue, **todas las órdenes** fallan (el NFC, por ejemplo, parece roto). Antes de echarle la culpa
a una función, comprueba que el mando contesta: `get battery` o `raw 4 9`. Prueba las órdenes nuevas **de una en
una**.

## Escriben en el mando: no enviarlas [firmware]

- Servicio 0, métodos 2–5 y 0x0b–0x0f: actualización de firmware, idiomas y pantallas.
- `(5,0x0c)`: escribe la EEPROM del UIC (calibración).
- `(4,0x01)`, `(4,0x1a)`: apagan o reinician en otros modos.
- `(4,0x19)`: modo de fábrica.

`drc` no las deja enviar (lista blanca, ver [comandos.md](comandos.md)).

## Trampas de medida [mando]

- **Resync por segundo no es una métrica**: en el estado 6 el mando pide uno por vsync. Mide ráfagas por minuto e
  IDR por ráfaga, y fíjate siempre en la imagen y el sonido ([video.md](video.md)).
- **No cambies `DRC_TS_OFFSET_US` en caliente**: el mando se queda pegado en resync; cada valor, en sesión nueva.
- **No fijes tasas con `iw … set bitrates`**: el mando se queda sin imagen.
- La cámara da cifras falsas los primeros ~4 s.
- `uvc off` no apaga la cámara; `set camera 0` sí.
- `uvc` exige los bytes separados por espacios.
- Un mando que se vinculó con otra consola ya no se conecta: hay que volver a vincularlo
  ([red-y-vinculacion.md](red-y-vinculacion.md)).
- Si no vibra con el bit correcto, mira si el motor está conectado.
- En la Raspberry Pi, `/dev/video10…31` son el códec por hardware.

## Trampas de la radio [mando]

- `rtw89` sin el parche de TSF (`drc-wifi-drivers`): el mando se asocia pero no muestra imagen.
- El chip interno de la Raspberry Pi no transmite tramas de más de 1500 B: con él hay que bajar el MTU, y los
  resync aumentan.
