# Red Wi-Fi y vinculación

## Vinculación (sync)

El mando se vincula por **WPS con PIN**. El usuario introduce en el mando 4 símbolos, que son 4 dígitos de 0 a 3:

| dígito | símbolo |
|--:|---|
| 0 | ♠ |
| 1 | ♥ |
| 2 | ♦ |
| 3 | ♣ |

A partir de los 4 dígitos `d0 d1 d2 d3` y de la MAC base del punto de acceso **[mando]** (`drchost/drc_ap.py`,
`derive_pairing_mac_and_ssid`):

- **PIN WPS** = `d0 d1 d2 d3` seguido de `5678` (♥♣♠♦ → `13025678`).
- **MAC del AP de vinculación**: la MAC base con el último byte = `d0<<6 | d1<<4 | d2<<2 | d3`.
- **SSID de vinculación**: `WiiU` + 11 primeras cifras hex de esa MAC + la MAC entera en hex + `_STA1`.

Tras el WPS el mando guarda la red normal (SSID `WiiU<mac>`, WPA2-PSK). **Cada vinculación genera una PSK nueva**: la
de una vinculación anterior no descifra una sesión posterior **[consola]**. Un mando vinculado a la Wii U y luego
a `drc` (o al revés) hay que volver a vincularlo.

Pulsar SYNC detrás del mando abre la pantalla de símbolos.

`drc` levanta el AP de vinculación en el **canal 36** (`DRC_PAIR_CHANNEL`), aparte del de la transmisión (`WIFI_CHANNEL`) **[mando]**: en la Raspberry Pi se vinculaba bien en el 36; con el AP en el 40 (PC con TP-Link) el mando, tras pulsar SYNC, no envió ninguna trama en ese canal. Que el mando solo busque en el 36 durante la vinculación es una **[hipótesis]**.

## El enlace de la consola [consola]

Medido con una captura en modo monitor de una Wii U real con su GamePad:

| parámetro | consola |
|---|---|
| banda / canal / ancho | 5 GHz, canal 36, 20 MHz |
| acceso | RTS → CTS → una trama → Block Ack, en los dos sentidos |
| agregación | ninguna (una trama por turno) |
| QoS consola → mando | TID 5 (vídeo) en el 98,8 % de las tramas |
| QoS mando → consola | TID 0 |
| reintentos | < 0,03 % |
| beacon / DTIM | 100 / 3 |
| WMM | be 2/4/5/47, bk 7/4/10/0, vi 3/4/5/94, vo 3/4/5/47 |
| **MTU / paquete máximo** | **1756 / 1736 B** |
| reparto de un frame de vídeo | ~12,7 ms, en 5 trozos separados ~2,8 ms |
| caudal medio en juego | ~11 Mbit/s |
| SSID | oculto en los beacons; en claro en la respuesta a probe |

Asociación: probe → auth (con un IE de Broadcom) → assoc → ADDBA → 4-way EAPOL (6 ms) → tráfico. Al apagar el
mando con su botón, la consola le manda una desasociación (motivo 8) y sigue emitiendo.

El mando **exige asociación WME** **[firmware]**.

## Valores que usa `drc` [mando]

| ajuste | valor | por qué |
|---|---|---|
| `DRC_MTU` | 1756 | el de la consola |
| `VSTRM_PAYLOAD_SIZE` | 1694 | paquete vstrm sin fragmentar dentro de ese MTU |
| `DRC_TOS` | 0xA0 | para que salga por la cola de vídeo (TID 5) como en la consola |

Con MTU 1500 / payload 1456 (límite del chip interno de la Raspberry Pi, que no transmite tramas de más de 1500 B)
había bucles de resync durante el 63–100 % del tiempo. Con los valores de la consola casi desaparecen.

**No fijar las tasas con `iw … set bitrates`**: deja al mando sin imagen **[mando]**.

## Lo que el mando hace con su radio [firmware]

- Monitor de pérdida: cada segundo calcula el % de paquetes perdidos; > 80 % durante ~4 s ⇒ enlace caído; < 20 % ⇒
  recuperado. Es un detector de corte, no la causa de los resync frame a frame.
- La sensibilidad (`phy_rxdesens`) y la potencia baja (`ntd_gds_lowtxpwr`) las fija **la consola** con
  `SetWlanSettings` (comando de la consola 102, 113 B); 0xFF = no tocar. `drc` no los envía.
- Estadísticas que el mando da si se le piden (canal de comandos, servicio 3): PHY (13 B), paquetes (152 B), WMM
  (448 B) y RSSI por antena (2 B). Ver [comandos.md](comandos.md).

## Encendido remoto

Ver [wowl.md](wowl.md).
