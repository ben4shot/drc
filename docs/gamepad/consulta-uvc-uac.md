# Consulta UVC/UAC (canal de comandos, tipo 1)

Paquete de 48 B que la consola manda al mando **una vez por segundo** por el puerto 50123. Configura la cámara, el
micrófono y **el modo del mando**. El mando contesta con el estado del sensor. libdrc lo arma en
`uvcuac-synchronizer` / `cmd-packet`.

## Bytes [firmware + consola]

| byte | campo | notas |
|--:|---|---|
| 0 | control | ≠ 0 ⇒ aplicar |
| 3 | **modo del mando** | **0 = normal. 1 ⇒ modo Wii** (ver aviso). 2 y 3 son otros modos |
| 4 | activar micrófono | |
| 5 | silenciar micrófono | |
| 6–7, 8–9 | volúmenes | |
| 12–15 | muestreo | |
| 16 | modo de cámara | 0 apagada, 1 normal, 2 «display» |
| 17 | pulso de apertura del sensor | 1 y luego 0 |
| 18–45 | ajustes de cámara | ver [camara.md](camara.md) |

Consulta de la consola en reposo (capturada) **[consola]**:

```
00000000 00000080 00800000 803e0000 00010002 9a020000 00000000 70000000 40400300 2d000001 80004000 00000000
```

## Respuesta

Bytes 4–7: exposición actual del sensor; bytes 8–11: ganancias azul/rojo (registros 0x01/0x02 del sensor). Si el
sensor nunca se ha encendido, valen los de fábrica (`80 00 40 00`). Lo que se lea aquí no permite elegir otros
registros del sensor **[firmware]**.

## ⚠ Byte 3

Con el byte 3 a 1 el mando **entra en modo Wii**: pantalla de carga «Wii», luces de la barra de sensores encendidas
y **no vuelve solo**; hay que apagarlo y encenderlo **[mando]**. No lo toques salvo para eso.

## En `drc`

- `set mic 0|1` y `set camera 0|1` modifican la consulta.
- `uvc <48 bytes en hex, separados por espacios>` / `uvc off` inyectan una consulta en crudo. Con el hex seguido,
  sin espacios, se ignora sin avisar.
