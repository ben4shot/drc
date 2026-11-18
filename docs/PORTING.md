# Llevar drc a otro sistema

## 1. Lo que necesita el equipo
- Un adaptador Wi-Fi 5 GHz que pueda ser AP **y exponga el TSF** (`/sys/class/net/<if>/tsf`). El rtw89 del kernel no lo
  expone: hay parche en `drc-wifi-drivers`. En la Raspberry Pi 4 se usa el chip interno con brcmfmac parcheado.
- Python 3.10+, `ffmpeg`, `wf-recorder` (capturar una sesión Wayland), PipeWire o ALSA; `cage`/`labwc`/`Hyprland` según la
  sesión del mando; `nasm` para compilar x264 con ensamblador (sin él, imagen a tirones).

## 2. Describe tu hardware, no toques el código
1. Con un adaptador conocido no hace falta: `DRC_PLATFORM="auto"` (el del ejemplo) elige el perfil por el driver
   (rtw89 → `usb-rtw89`, brcmfmac → `rpi4-internal`). Para otro, copia `config/platforms/generic.conf` a
   `config/platforms/<mi-equipo>.conf` y ajusta `DRC_MTU`, `VSTRM_PAYLOAD_SIZE` (= MTU − 62), `TS_OFFSET_US` y, si tu
   driver lo necesita, `WIFI_RESET_HOOK`.
2. En `config/drc.conf`: `WIFI_IFACE`, `WIFI_CHANNEL`, `DESKTOP_SESSION`, `GAMEPAD_AUTOSTART`, `DRC_*` y, si hiciste un
   perfil propio, `DRC_PLATFORM="<mi-equipo>"`.
   Lo que escribas en `drc.conf` pisa al perfil. Se puede forzar por entorno (`DRC_PLATFORM`, `DRC_HOSTAPD_BIN`...).
3. Si algo del sistema no se puede expresar así (gestor de red, firewall, audio), hay ganchos en `drc.conf`:
   `WIFI_UNMANAGE_CMD`, `WIFI_REGDOM_CMD`, `WIFI_RESET_HOOK`, `FIREWALL_OPEN`, `AUDIO_BACKEND`, `DRC_CAPTURE_CMD`.

## 3. Compilar
- Todo en este equipo (x264 parcheado, libdrc, `drc_stream`, hostapd/wpa_supplicant): `bash drchost/build_local.sh`
  → `build_local/prefix` (sin root; también por partes: `--x264 --libdrc --stream --hostap`). `start_drc.sh` toma las
  librerías de `build_local/prefix/lib`.
- Compilar en otro equipo por ssh (p. ej. una Raspberry Pi ARM): `drchost/build_userspace.sh` con
  `DRC_BUILD_HOST=usuario@host`.
- hostapd lleva `CONFIG_TENDONIN` (`drchost/patch_hostap_drc.py` ajusta el defconfig); indica otro binario con
  `HOSTAPD_BIN` en `drc.conf` si no está en `build_local/prefix/bin`.

## 4. Verificar
`sudo bash drchost/diag_pc.sh 60` (patrón de prueba, sin escritorio) y después `sudo python3 tools/gamepad_test.py`.
`pytest` sin variables corre las pruebas que no necesitan hardware.

## Qué es de tu setup y qué es del proyecto
Del proyecto: protocolo, `libdrc`, parches, `drc_ap.py`, `start_drc.sh`. Del setup: todo lo que está en `drc.conf` y en tu
perfil de plataforma. Los servicios de `drchost/systemd` y `drc_mode.sh`/`drc_pad.sh`/`drc_menu.py` son el montaje de
«consola» de la Raspberry Pi con dos Wi-Fi (USB para el mando, interna para gestión); en un PC no hacen falta.
