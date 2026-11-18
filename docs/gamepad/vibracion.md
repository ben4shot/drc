# Vibración

## Cómo se activa [firmware + mando]

El motor se enciende con el **bit `vibrate` de la cabecera de los paquetes de audio** (astrm, UDP 50121): **bit 3
del byte 0**. En el aire el byte 0 pasa de `0x23` a `0x2b` mientras vibra.

Camino dentro del mando: la tarea que recibe el audio lee el bit → el módulo de control del juego → **GPIO4 del
SoC** (todo o nada, sin intensidad). **Si en 18 ms no llega otro paquete con el bit, una alarma apaga el motor.**

Consecuencias:

- Para vibrar hay que **mantener un flujo de audio continuo** (silencio si no hay sonido) con el bit a 1.
- La intensidad o el patrón se consiguen modulando el bit de paquete en paquete.
- El STM8 no interviene, y tampoco el comando `(5,3)` (`SetRumbleParams` de la consola): ese comando no tiene
  manejador en el mando **[firmware]**.

## En `drc` [mando]

`vibrate <ms>` por el canal de control (`/tmp/drc_ctl`), hasta 60 000 ms. Comprobado con el mando el 2026-11-10.

## Trampa

En una primera prueba el bit iba bien en el aire pero el motor no se movía: **el motor del mando estaba
desconectado** por dentro. Si no vibra con el bit correcto, revisa el hardware antes que el protocolo.
