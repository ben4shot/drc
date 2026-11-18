# Encendido remoto (Wake-on-WLAN)

Funciona en los dos sentidos con la consola: el mando enciende la Wii U (Inicio rápido) y la Wii U enciende el
mando. Con `drc`, la primera dirección está resuelta; la segunda no.

## El mando avisa al encenderse [consola + mando]

Al pulsar POWER, el mando emite una **ráfaga** (cientos de copias en 50–200 ms) de una trama de datos QoS **ToDS,
protegida (CCMP), de 71 B**:

- dirección 1 = dirección 3 = MAC de la consola (o del equipo con `drc`), dirección 2 = MAC del mando;
- QoS `07 00` (TID 7);
- cabecera CCMP con **PN = 1**;
- 29 B cifrados + MIC.

La consola en reposo emite su primer beacon ~1,2 s después y el mando se asocia ~0,5 s más tarde. A veces el mando
solo manda probes por todos los canales, sin la ráfaga.

`drc-wake` arranca la sesión al ver esa ráfaga **o** un probe del mando vinculado, sin descifrar nada.

Captura en el PC: `sudo python3 tools/wowl_captura.py` (con la sesión parada).

## La consola enciende el mando [firmware]

El mando apagado mantiene la radio escuchando con un **filtro de paquetes** (no con el WoWL estándar de Broadcom,
que este firmware no trae): id `0x80`, patrón de 6 B en el **offset 15** del cuerpo = la MAC WoWL guardada. Cuerpo de
**25 B**. La trama se descifra con la **clave WoWL** (16 B, AES-CCM).

- El mando da su clave con `(1,0x08)` (16 B), y está también en el bloque de configuración que devuelve `(5,0x06)`:
  clave en `0x6d`, MAC en `0x7f`, cada una seguida de su CRC16 (inicial `0xffff`, polinomio reflejado `0x8408`).
- La consola la configura con `SetWowlSettings` (comando de la consola 107, 31 B: clave + byte + MAC) y despierta
  con `WowlWakeDrc` (209, 7 B: MAC + 1 B).

## Lo que falta [mando]

El equipo ya inyecta tramas y el mando las confirma (ACK). Pero `drchost/drc_wowl.py`, con esa clave y esa MAC, **no
enciende el mando**, ni siquiera imitando la ráfaga de la consola (800 tramas, PN = 1). Tampoco se descifra el aviso
del propio mando con esa clave, en ninguna variante (2026-11-17). Así que falta saber cuál es la clave real o qué
cuerpo exacto espera el mando.
