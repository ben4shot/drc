# drc — Wii U GamePad como pantalla y mando de un PC o una Raspberry Pi

*(English: stream a Linux desktop to a Wii U GamePad without a Wii U console — video, audio, touch, buttons, sensors,
camera, NFC and rumble. See `docs/PORTING.md` for adapting it to your hardware.)*

Todo lo de DRC vive aquí: el servidor de transmisión (`drc_stream`), `libdrc` (fork con el transporte afinado), los
parches de x264 y de hostapd, el control del AP/emparejamiento, las apps para el mando y el **probador visual** de todas
sus funciones. Los parches del *driver Wi-Fi* (rtw89 USB, brcmfmac de la Raspberry Pi) están en el repo
`drc-wifi-drivers`; el escritorio propio del mando (`wiiu-wm`) en `wiiu-wm`.

## Estructura

| Carpeta | Contenido |
|---|---|
| `drchost/` | paquete Python y scripts: `start_drc.sh` (sesión completa), `drc_ap.py` (AP, DHCP, emparejamiento), `drc_ctl.py` / `drc_gui.py` (control), audio, atajos del mando, NFC, cámara, wake-on-WLAN, servicios systemd |
| `src/drc_stream/` | programa C++ que codifica y envía vídeo/audio y crea los dispositivos de entrada (`uinput`) |
| `libdrc/` | libdrc (BSD-2) con los cambios de este proyecto |
| `third_party/` | `drc-hostap` (hostapd/wpa_supplicant con el parche Wii U) y, tras `drchost/fetch_x264.sh`, x264 de VideoLAN (los parches DRC están en `drchost/patch_x264_drc.py`) |
| `config/` | `drc.conf.example`, **perfiles de plataforma** (`platforms/`) y ejemplos (`examples/`) |
| `tools/` | `gamepad_test.py` (probador visual), `touch2pointer` (táctil → puntero Wayland), banco del codificador |
| `apps/` | apps táctiles para el mando (cámara, NFC) |
| `tests/` | pruebas (`pytest`); las de hardware (`*_on_rpi4`) necesitan `DRC_TEST_HOST=usuario@host` |
| `docs/gamepad/` | **cómo funciona el GamePad**: valores, comandos, formatos y trampas, un archivo por función ([índice](docs/gamepad/README.md)) |

## Puesta en marcha (resumen)

### Requisitos (instálalos antes de armar)

Compilar (obligatorio):

| | Arch | Debian / Ubuntu / Raspberry Pi OS |
|---|---|---|
| Compilador y make | `base-devel` | `build-essential` |
| git, pkg-config | `git pkgconf` | `git pkg-config` |
| nasm (x264) | `nasm` | `nasm` |
| ffmpeg (libswscale, libavutil) | `ffmpeg` | `libswscale-dev libavutil-dev ffmpeg` |
| hostapd/wpa_supplicant propios | `libnl openssl` | `libnl-3-dev libnl-genl-3-dev libssl-dev` |
| Python 3 | `python` | `python3` |
| `touch2pointer` (táctil como ratón, el modo por defecto) | `wayland` | `libwayland-dev libwayland-bin` |

Ejecutar (obligatorio):

| | Arch | Debian / Ubuntu / Raspberry Pi OS |
|---|---|---|
| Wi-Fi y red | `iw iproute2 networkmanager rfkill ethtool` | `iw iproute2 network-manager rfkill ethtool` |
| Captura de pantalla | `wf-recorder wlr-randr` | `wf-recorder wlr-randr` |
| Compositor del mando (`--mode separate`) | `cage` | `cage` |
| Audio | `pipewire-pulse alsa-utils` | `pulseaudio-utils pipewire-bin alsa-utils` |
| Reglas de red | `iptables-nft` | `iptables` |

Opcionales:

| Para qué | Arch | Debian / Ubuntu / Raspberry Pi OS |
|---|---|---|
| Probador del mando y sus apps (`tools/gamepad_test.py`, `apps/`): **instálalo para probar el mando** | `python-pygame` | `python3-pygame` |
| Interfaz gráfica `drc_gui.py` | `python-gobject gtk4 libadwaita` | `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` |
| Encendido remoto del mando (`drc_wowl.py`, `tools/wowl_captura.py`) | `python-cryptography` | `python3-cryptography` |
| Cámara del mando como cámara web (`drc_cam_v4l2.py`) | `v4l2loopback-dkms` | `v4l2loopback-dkms` |
| Compilar y desplegar a otra máquina (`build_userspace.sh`) | `rsync openssh` | `rsync openssh-client` |
| Pruebas | `python-pytest` | `python3-pytest` |

Además, un adaptador Wi-Fi con el parche del TSF: ver el repo `drc-wifi-drivers`.

### Armado

```bash
cp config/drc.conf.example config/drc.conf     # edita WIFI_IFACE, DESKTOP_SESSION... (sin drc.conf se usa el ejemplo)
bash drchost/build_local.sh                    # x264 parcheado (necesita nasm), libdrc, drc_stream y hostapd en build_local/prefix
sudo bash drchost/start_drc.sh --mode separate
```

El ejemplo trae la configuración con la que el mando va bien (medida en la Raspberry Pi y en un PC con el TP-Link) y
`DRC_PLATFORM="auto"`, que elige el perfil de hardware por el driver de `WIFI_IFACE` (rtw89 → `usb-rtw89`, brcmfmac →
`rpi4-internal`, otro → `generic`). Al arrancar, la primera línea dice qué fichero, perfil e interfaz se usan.

**Toda la configuración propia de tu equipo va en `config/drc.conf`** (interfaz, perfil de hardware, sesión del mando,
calidad `DRC_*`, usuario del escritorio...). El código no contiene rutas, interfaces ni usuarios concretos.

## Probar todas las funciones del mando

```bash
sudo python3 tools/gamepad_test.py        # ventana con 8 pestañas en este PC
sudo python3 tools/gamepad_test.py --en-mando   # la misma ventana en la pantalla del GamePad (con la transmisión en modo separate)
python3 -m drchost.padtest --out informe.json   # solo las comprobaciones automáticas, sin interfaz
python3 tools/gamepad_test.py --shots /tmp/p    # captura cada página sin mando (SDL dummy)
```

**Pantalla del mando:** `set lcd_on 0` la apaga y `set lcd_on 1` la vuelve a encender (manda 0xff: según el
firmware, LVC 0x36e82, 0x00, 0x01 y 0x02 son modos de apagado y solo 0xff enciende; comprobado en el mando el
2026-11-16). El botón «Apagar 1 s» del probador la apaga y la enciende.

Comprueba botones, sticks, táctil (9 zonas), acelerómetro, giroscopio, micrófono, altavoces, volumen, cámara,
retroiluminación, vibración, barra sensora, batería, señal, firmware y NFC. Sensores, micrófono y cámara viajan en
paquetes UDP y piden root; sin él quedan «omitidos» y el resto funciona. Guarda un informe JSON.

## Licencias

El código propio de este proyecto se publica bajo **AGPL-3.0** (`LICENSE`). Componentes de
terceros con su licencia: libdrc (BSD-2, Mema Hacking, `libdrc/LICENSE`), hostapd/wpa_supplicant (`third_party/drc-hostap/COPYING`),
x264 (GPL-2, se descarga aparte).

Este proyecto es un desarrollo derivado de pc2drc (thefloppydriver), que se usó como base. `libdrc/` es la libreria
original de Mema Hacking (2013, BSD-2, `libdrc/LICENSE`), tomada de la rama `input-mods` de
https://bitbucket.org/kordariffic/libdrc a traves de pc2drc, que le añadio las opciones de framerate y el codificador
H.264 de ywh233; aqui lleva ademas modificaciones propias (sincronía B17, relleno en reposo, control de camara y
microfono, etc.). La licencia BSD-2 original se conserva. Qué otras partes proceden de pc2drc no esta auditado: si se
reutiliza codigo de su repo original, aplica su licencia (GPL-2).
Créditos: Mema Hacking (libdrc), kordariffic (input-mods), ywh233 (framerate y H.264), thefloppydriver (pc2drc),
rolandoislas (drc-hostap, drc-x264).
