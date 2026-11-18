# Energía, batería, estado y LEDs

## Batería: `(5,0x00)` [mando]

Devuelve 6 B (los pide al STM8 con su orden 10). `get battery`. El informe de entrada trae además un byte de carga y
los bits de energía (`0x01` corriente, `0x02` botón de encendido, `0x40` cargando, `0x80` USB): ver
[entrada.md](entrada.md).

## Estado del mando [mando]

- `(4,0x05)`: devuelve el estado (1 B). `get state`.
- `(4,0x04)`: información del sistema (28 B). `get sysinfo`.
- `(4,0x01)`, 1 B: cambiar de estado: `0x06` = apagar; otros valores reinician en otro modo (actualización de
  firmware…). **Fuera de la lista blanca de `drc`**.
- `(4,0x1a)`: parámetro de arranque; con ciertos flags apaga el mando. Fuera de la lista blanca.

## LED de mensaje: `(5,0x02)`, 102 B [firmware]

Formato: `u16, u16, {u8, u8, u8}[32], u16`: una secuencia de 32 pasos. El mando la pasa tal cual al STM8 (orden 12),
que la guarda y la reproduce con el PWM. Sin probar.

## LEDs (PWM del STM8) [firmware]

El STM8 maneja **4 canales PWM** (TIM1/TIM2, ~244 Hz). Modos: apagado, fijo y parpadeo (~4 Hz al 50 %). Nivel de 0
a 200 (= 0–100 %). La orden directa para fijar un canal existe en el ARM pero **nada la llama**: los LEDs solo se
mueven con el patrón del LED de mensaje y con los estados internos. Ver [stm8.md](stm8.md).

## Apagado al perder la red

Al cortar la consola, el mando manda 7 DELBA, reintenta ADDBA cada ~1 s y probes durante unos 10 s, y se calla
**[consola]**. Con `drc`, el servicio `drc-ausencia` cierra la sesión si el mando falta más de 30 s.
