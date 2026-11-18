# NFC

El mando es **lector/escritor** (ISO 14443: T1T, T2T, ISO-DEP; NDEF; contraseña de NTAG). **No emula etiquetas.**
Todo va por el comando genérico **`(5,0x10)`**, con una carga fija de **592 B** (rellena con ceros) cuyo primer byte
es la orden. Enteros en big-endian **[mando]**.

## Órdenes comprobadas en el mando (2026-11-08)

| orden | qué hace | respuesta observada |
|---|---|---|
| `0x00` | arrancar el lector | `00` |
| `0x13` | comprobar la antena | `0x16` |
| `0x0c` | fijar modo (1) | `0x0f` (¿ya activo?) |
| `0x0d` | empezar detección, `timeout u32` en ms (p. ej. 25000) | `00` |
| `0x0e` | consultar la detección (cada 0,5 s) | 11 B: `resultado, uid[7], solo_lectura, bloqueada, flags`. `0x08` = sin etiqueta, `0x13` = etiqueta perdida |
| `0x01` / `0x02` | leer | **no sirve con ISO-DEP**: solo entiende NTAG/T2T y el formato de Nintendo |
| `0x0a` | **pass-through**: enviar una APDU | ver abajo |
| `0x0b` | recoger la respuesta del pass-through | `00`, `tamaño u16`, datos |
| `0x05` | abortar | `0x06` |
| `0x06` | apagar el lector | `00` |

Ejemplo de detección: `00 | 01 02 03 04 00 00 00 | 00 | 00 | 08` = etiqueta con UID de 4 B `01:02:03:04`.

## Pass-through (ISO-DEP / tipo 4)

```
0a | activación (1 B) | timeout de descubrimiento u16 | timeout del comando u32 | tamaño del comando u16 |
     tamaño de la respuesta u16 | APDU
```

- La primera llamada, con activación = 1, devuelve los datos de activación (SAK 0x20, UID, ATS).
- Las siguientes envían la APDU; `0b` recoge la respuesta.

Con esto el mando lee y **escribe** NDEF en un teléfono Android que emula una etiqueta tipo 4 por HCE: SELECT AID
`D2760000850101`, SELECT `E103` (CC) / `E104` (NDEF), READ/UPDATE BINARY. App de prueba: `android_nfc/` en
`wiiu-gamepad-research`.

## Trampas

- Si el mando está colgado por otra orden (p. ej. IrDA, ver [peligros.md](peligros.md)), el NFC no contesta.
  Comprueba antes que el mando responde (`get battery`).
- El HCE de algunos móviles usa un UID fijo (`01:02:03:04`).
