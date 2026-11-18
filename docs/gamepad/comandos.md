# Comandos genéricos (canal de comandos, UDP 50123)

Protocolo base: `libdrc/web/docs/re/sc-cmd.rst`. El canal lleva tres tipos de paquete: **0 = comando genérico**,
**1 = consulta UVC/UAC** ([consulta-uvc-uac.md](consulta-uvc-uac.md)) y **2 = hora**.

## Formato

Un comando genérico es `(servicio, método, carga)`. Los nombres de la consola (`CCRCDC…` en `nsysccr.rpl`) dan el
significado: **id de la consola = 100 × servicio + método** (p. ej. CCR 506 = `(5,6)`). Coinciden en longitud 26 de
31 comparables **[firmware]**.

Respuesta: 12 B de cabecera `7e 01 00 08 00 09 <serv> <mét> 00 00 00 <len>` + datos **[mando]**.

Para hacerse pasar por un mando ante una consola real hay que contestar con un ACK (tipo 1) antes de cada respuesta;
sin él la consola repite la orden **[consola]**.

## En `drc`

Canal `/tmp/drc_ctl` de `drc_stream`, con lista blanca (`libdrc/include/drc/internal/pad-control.h`); las respuestas
van a `/tmp/drc_ctl.out`. Órdenes: `get <nombre>`, `set <nombre> <valor>`, `vibrate <ms>`, `cam …`, `uvc …`. Desde
el menú: `drc_ctl.py`.

## Tabla completa (59 comandos del firmware)

Estado: ✅ comprobado en el mando · 📖 solo leído en el firmware o en la documentación de libdrc · ⛔ peligroso,
fuera de la lista blanca.

| serv | mét | carga | qué hace | nombre en `drc` | estado |
|--:|--:|--:|---|---|---|
| 0 | 0x00 | 0 | versión del firmware (en marcha e instalada, 2×u32) | `fw_version` | ✅ |
| 0 | 0x01 | 0 | ? | | 📖 |
| 0 | 0x02–0x05 | var. | empezar / datos / terminar / abortar la actualización del firmware | | ⛔ |
| 0 | 0x09 | 1 | devuelve valores fijos | | 📖 |
| 0 | 0x0a | 1 | versión guardada en la EEPROM (idioma, mando de TV, servicio, pantallas de arranque…) | | 📖 |
| 0 | 0x0b–0x0f | var. | actualizar el código de TV / servicio / pantallas / idiomas | | ⛔ |
| 1 | 0x05 | 0 | configuración Wi-Fi (incluye SSID y **PSK**, 113 B) | | ⛔ (clave) |
| 1 | 0x08 | 0 | clave WoWL (16 B) | | ⛔ (clave) |
| 2 | 0x00 | 0 | no hace nada (devuelve 0) | | 📖 |
| 2 | 0x0c | 0 | id del aparato + certificado (272 B) | | ⛔ (clave) |
| 3 | 0x00 | 1 | ? (Wi-Fi) | | 📖 |
| 3 | 0x03 | 1 | estadísticas PHY (13 B) | `phy_stats` | ✅ |
| 3 | 0x04 | 1 | estadísticas de paquetes (152 B) | `pkt_stats` | ✅ |
| 3 | 0x05 | 1 | estadísticas WMM (448 B) | `wmm_stats` | 📖 |
| 3 | 0x06 | 0 | RSSI por antena (2 B) | `rssi` | ✅ |
| 4 | 0x01 | 1 | cambiar de estado: `0x06` apaga; otros reinician en otro modo | | ⛔ |
| 4 | 0x04 | 0 | información del sistema (28 B) | `sysinfo` | ✅ |
| 4 | 0x05 | 0 | estado del mando (1 B) | `state` | ✅ |
| 4 | 0x08 | 4 | modo de cámara `{display, fps, tasa}` | (`set camera`) | ✅ |
| 4 | 0x09 | 0 | leer el modo de cámara | `camera_mode` | ✅ |
| 4 | 0x0a | 1 | modo del micrófono | | 📖 |
| 4 | 0x0b | 0 | leer el modo del micrófono | `mic_mode` | ✅ |
| 4 | 0x0e | 1 | ? (por su id parece `SetVideoEncodingRate`, pero no coincide en longitud) | | 📖 |
| 4 | 0x14 | 4 | ? | | 📖 |
| 4 | 0x19 | 0 | modo de fábrica | | ⛔ |
| 4 | 0x1a | 0 | parámetro de arranque; puede apagar | | ⛔ |
| 4 | 0x1c | 0 | parámetros de lanzamiento de app | | 📖 |
| 4 | 0x1f, 0x21 | 32 | ? | | 📖 |
| 5 | 0x00 | 0 | batería (6 B) | `battery` | ✅ |
| 5 | 0x01 | 1 | barra de sensores IR | `sensorbar` | ✅ |
| 5 | 0x02 | 102 | patrón del LED de mensaje | | 📖 |
| 5 | 0x06 | 0 | versión del UIC + EEPROM del UIC (772 B; calibración, clave WoWL) | (config) | ✅ |
| 5 | 0x08 | 1 | LCD: `0xff` enciende, `00/01/02` apagan ([pantalla.md](pantalla.md)) | `lcd_on` | ✅ |
| 5 | 0x0a | 1 | silenciar el audio | `audio_on` | 📖 |
| 5 | 0x0b | 0 | leer el silencio del audio | `audio_mute` | ✅ |
| 5 | 0x0c | 258 | **escribir la EEPROM del UIC** | | ⛔ |
| 5 | 0x0e / 0x0f | 5 / 0 | fijar / leer CABC del LCD | `lcd_cabc` | ✅ (leer) |
| 5 | 0x10 | 592 | NFC ([nfc.md](nfc.md)) | | ✅ |
| 5 | 0x11 | 520 | IrDA ([infrarrojos.md](infrarrojos.md)) | | ⛔ (enviar) |
| 5 | 0x14 | 1 | fijar el brillo 1…5 | `backlight` | ✅ |
| 5 | 0x15 | 0 | leer el brillo | `brightness` | ✅ |
| 5 | 0x16 | 1 | modo de mando de TV | | ✅ (acepta) |
| 5 | 0x17 | 7 | enviar un código de TV de prueba | | 📖 |
| 5 | 0x18 | 10 | fijar los dos identificadores del mando de TV | | 📖 |
| 5 | 0x19 / 0x1a | 13 / 0 | fijar / leer la tabla de brillo del LCD | `lcd_table` | 📖 |
| 5 | 0x1b | 16 | calibración del táctil | | 📖 |
| 5 | 0x1c | 32 | ? (sonido / NVRAM) | | 📖 |
| 5 | 0x1d | 32 | forzar volumen | | 📖 |
| 5 | 0x1e | 32 | ? (configuración / hora) | | 📖 |

`(5,0x03)` (`SetRumbleParams` de la consola) **no tiene manejador** en el mando; la vibración va por el audio
([vibracion.md](vibracion.md)).

Comandos de la consola que **no llegan al mando** (los atiende el lado de la consola): 400, 402, 403, 406, 407, 412,
413, 415–419 y 421–424, entre ellos `SetVideoEncodingHint` (416).
