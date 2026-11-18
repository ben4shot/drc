# Microcontrolador STM8 (UIC, `UMI.bin`)

Lee botones, sticks, táctil y sensores, gestiona la energía y los LEDs, y guarda la calibración en su EEPROM. Habla
con el procesador principal por SPI. Todo lo de esta página es **[firmware]**.

## Qué chip es

**STM8L15x** (STM8L151/152), no STM8S: usa TIM1/TIM2 de la serie L, RTC, DMA1, EEPROM de datos y ADC1, y no tiene
USART ni CAN. Base de carga 0x9000, 16 MHz. Vector = IRQ + 2.

Herramienta: `tools/stm8_flow.py` en `wiiu-gamepad-research`. Cubre el 95,6 % del código: 473 funciones.

## Enlace con el ARM

SPI esclavo con DMA y dos líneas de handshake: PB4 la mueve el ARM («pedido») y PB7 el STM8 («listo»). El primer
byte es el número de orden; `0x7f` es un ping y se contesta `0x3f`.

| orden | sentido | datos | uso |
|--:|---|--:|---|
| 5 | STM8→ARM | 1 B | estado de energía |
| 7 | ARM→STM8 | 128 B | bloque de configuración |
| 8 | ARM→STM8 | 2 B | (canal PWM, nivel 0–200). Nadie la llama desde el ARM |
| 10 | STM8→ARM | 6 B | batería (comando `(5,0)`) |
| 11 | ARM→STM8 | 4 B | barra de sensores (comando `(5,1)`) |
| 12 | ARM→STM8 | 102 B | patrón del LED de mensaje (comando `(5,2)`) |
| 25, 26 | STM8→ARM | 255 B | informe de entrada (botones, sticks, táctil, sensores) |
| 27, 28 | ambos | 4 B | ? |

Hay 28 órdenes en total; las demás, sin identificar.

## Periféricos

- **I²C maestro** (modo rápido): sensores.
- **ADC1** bajo demanda: sticks y batería.
- **RTC**, **watchdog**, **EEPROM de datos**: la configuración del UIC, 768 B, que se lee con `(5,6)`.
- **4 PWM** de LEDs: TIM2 CH1, TIM1 CH2, TIM1 CH3 y TIM2 CH2; periodo 0xFFFF (~244 Hz); nivel 0–200 → duty ×327.
  Modos: apagado, fijo y parpadeo (~4 Hz).
- Planificador a ~10 Hz (TIM4).

## Lo que no hace

No mueve el motor de vibración: el motor lo maneja el GPIO4 del ARM ([vibracion.md](vibracion.md)).
