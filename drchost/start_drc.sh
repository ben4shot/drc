#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="${SCRIPT_DIR}/../build_local/prefix/bin:${PATH}"
export LD_LIBRARY_PATH="${SCRIPT_DIR}/../build_local/prefix/lib:${LD_LIBRARY_PATH:-}"
DRC_DRY_RUN="${DRC_DRY_RUN:-0}"
CONF_DIR="/tmp/drc_conf"
LOG_FILE="${CONF_DIR}/start_ap.log"
TIMEOUT_SECS="${DRC_TIMEOUT:-120}"

# --conf <fichero> se lee ANTES de cargar nada: así manda en todo (perfil, interfaz, modo...), no solo en lo que
# se lea después de los argumentos.
_args=("$@")
for ((_i = 0; _i < ${#_args[@]}; _i++)); do
    case "${_args[_i]}" in
        --conf) DRC_CONF_FILE="${_args[_i + 1]:-}"; _conf_given=1 ;;
        --conf=*) DRC_CONF_FILE="${_args[_i]#*=}"; _conf_given=1 ;;
    esac
done
unset _args _i
if [ -n "${_conf_given:-}" ] && [ ! -f "${DRC_CONF_FILE}" ]; then
    echo "[!] --conf: no existe «${DRC_CONF_FILE}»." >&2
    exit 1
fi
# Cargar configuración central desde drc.conf si existe
DRC_CONF_FILE="${DRC_CONF_FILE:-/etc/drc/drc.conf}"
if [ ! -f "${DRC_CONF_FILE}" ] && [ -f "${SCRIPT_DIR}/../config/drc.conf" ]; then
    DRC_CONF_FILE="${SCRIPT_DIR}/../config/drc.conf"
fi
# Sin drc.conf propio, el ejemplo: es la configuración que funciona, no los valores del código.
if [ ! -f "${DRC_CONF_FILE}" ] && [ -f "${SCRIPT_DIR}/../config/drc.conf.example" ]; then
    DRC_CONF_FILE="$(cd "${SCRIPT_DIR}/../config" && pwd)/drc.conf.example"
fi
if [ -f "${DRC_CONF_FILE}" ]; then
    # Los DRC_* de drc.conf (ajustes del codificador y de libdrc) pasan a drc_stream; los del entorno mandan.
    _env_drc="$(export -p | grep -E '^declare -x DRC_' || true)"
    # Perfil de plataforma (config/platforms/<DRC_PLATFORM>.conf): valores base del hardware; drc.conf los sobrescribe.
    _plat="${DRC_PLATFORM:-$(sed -n "s/^DRC_PLATFORM=[\"']\{0,1\}\([A-Za-z0-9_-]*\).*/\1/p" "${DRC_CONF_FILE}" | head -1)}"
    # «auto»: perfil según el driver de la interfaz (rtw89* → usb-rtw89, brcmfmac → rpi4-internal; si no, generic)
    if [ "${_plat}" = "auto" ]; then
        _if="${DRC_IFACE:-$(sed -n "s/^WIFI_IFACE=[\"']\{0,1\}\([A-Za-z0-9_.-]*\).*/\1/p" "${DRC_CONF_FILE}" | head -1)}"
        _drv="$(basename "$(readlink -f "${DRC_SYSFS_NET:-/sys/class/net}/${_if:-wlan0}/device/driver" 2>/dev/null)" 2>/dev/null)"
        case "${_drv}" in
            rtw89*) _plat="usb-rtw89" ;;
            brcmfmac*) _plat="rpi4-internal" ;;
            *) _plat="generic" ;;
        esac
    fi
    DRC_PLATFORM_NAME="${_plat}"   # el perfil que se usa de verdad (con «auto», el que corresponde al driver)
    DRC_PLATFORM_FILE=""
    for _pd in "$(dirname "${DRC_CONF_FILE}")/platforms" "${SCRIPT_DIR}/../config/platforms"; do
        if [ -n "${_plat}" ] && [ -f "${_pd}/${_plat}.conf" ]; then
            DRC_PLATFORM_FILE="$(cd "${_pd}" && pwd)/${_plat}.conf"
            # shellcheck disable=SC1090
            source "${DRC_PLATFORM_FILE}"; break
        fi
    done
    if [ -n "${_plat}" ] && [ -z "${DRC_PLATFORM_FILE}" ]; then
        echo "[!] DRC_PLATFORM=«${_plat}» no existe en config/platforms/: se usan los valores genéricos." >&2
    fi
    # shellcheck disable=SC1090
    source "${DRC_CONF_FILE}"
    export DRC_CONF_FILE    # los programas en Python leen el mismo fichero (también con --conf)
    eval "${_env_drc}"
    for _v in "${!DRC_@}"; do export "${_v?}"; done
    [ -n "${GAMEPAD_AUTOSTART:-}" ] && export GAMEPAD_AUTOSTART
fi

# Ajustes en caliente de libdrc (diagnóstico: los relee cada segundo y mandan sobre drc.conf y el entorno). Uno que
# sobre de una prueba anterior cambiaría esta sesión sin avisar: se borran al empezar.
DRC_HOT_FILES="/tmp/drc_ts_offset_us /tmp/drc_audio_ts_offset_us /tmp/drc_astrm_ts_delta_us /tmp/drc_idr_ts_shift_us /tmp/drc_kick"
if [ "${DRC_DRY_RUN}" != "1" ]; then
    for _f in ${DRC_HOT_FILES}; do
        [ -e "${_f}" ] && { echo "[*] Se borra ${_f} (ajuste en caliente de una sesión anterior)."; rm -f "${_f}"; }
    done
fi

IFACE="${DRC_IFACE:-wlan0}"
[ -n "${WIFI_IFACE:-}" ] && [ -z "${DRC_IFACE:-}" ] && IFACE="${WIFI_IFACE}"
MODE="${DRC_MODE:-separate}"
FPS="${DRC_FPS:-${DISPLAY_FPS:-60}}"
AUDIO="${DRC_AUDIO:-${AUDIO_OUTPUT:-gamepad}}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --conf)
            shift 2   # ya leído al principio
            ;;
        --conf=*)
            shift 1
            ;;
        --iface)
            IFACE="$2"
            shift 2
            ;;
        --iface=*)
            IFACE="${1#*=}"
            shift 1
            ;;
        --mode)
            MODE="$2"
            shift 2
            ;;
        --mode=*)
            MODE="${1#*=}"
            shift 1
            ;;
        --audio)
            AUDIO="$2"
            shift 2
            ;;
        --audio=*)
            AUDIO="${1#*=}"
            shift 1
            ;;
        --fps)
            FPS="$2"
            shift 2
            ;;
        --fps=*)
            FPS="${1#*=}"
            shift 1
            ;;
        *)
            shift 1
            ;;
    esac
done

# ¿Hay en dmesg un fallo real del chip Wi-Fi? (0 = sí, hay que reiniciar el hardware)
# Solo cuentan los fallos graves conocidos (firmware caído, timeout del bus SDIO). Un filtro
# amplio como "brcmf.*(error|failed|err=-)" casaba con mensajes normales (vndr ie set error,
# no txcap_blob available, Scan abort failed, del_station ... failed) y el reinicio del bus
# SDIO dejaba la Pi sin wlan0 hasta reiniciarla; cada reinicio dejaba el siguiente motivo.
REAL_BRCMF_FAULT='Firmware has halted or crashed|resumed on timeout|err=-110|bus is down'
wifi_needs_hw_reset() {
    tail -n 20 "${DRC_DMESG_FILE:-/dev/stdin}" | grep -E -q "brcmf.*(${REAL_BRCMF_FAULT})"
}

if [ "${DRC_CHECK_DMESG:-0}" = "1" ]; then
    wifi_needs_hw_reset
    exit $?
fi

# Salida de audio: "gamepad" (por defecto) o "aux" (jack de la Pi). Se valida antes de tocar nada.
case "${AUDIO}" in
    gamepad|aux) ;;
    *)
        echo "Error: --audio '${AUDIO}' no válido; usa gamepad|aux." >&2
        exit 2
        ;;
esac

# Audio "gamepad": las aplicaciones de la Pi suenan en un dispositivo ALSA virtual (snd-aloop) cuyo lado
# de captura (hw:Loopback,1) se manda a los altavoces del GamePad por el fd 4 de drc_stream.
# dmix permite varias aplicaciones a la vez; plug convierte formato/frecuencia a 48 kHz s16 estéreo.
# Periodos de 10 ms: con los 125 ms por defecto de arecord, cada lectura (12000 s16) superaba
# el tope de 100 ms de la cola de audio de libdrc y se perdían 25 ms ocho veces por segundo.
DRC_AUDIO_BUFFER_SIZE="${DRC_AUDIO_BUFFER_SIZE:-1920}"
AUDIO_CAPTURE_CMD="arecord -D hw:Loopback,1 -f S16_LE -r 48000 -c 2 -t raw --period-size=480 --buffer-size=${DRC_AUDIO_BUFFER_SIZE}"
# El esclavo dmix se fija a S16_LE: por defecto abre el loopback en S32_LE y arecord (S16_LE)
# no puede negociar el formato. ipc_perm 0666: el escritorio del GamePad corre como root y un
# dmix creado por root con 0600 dejaba sin audio a los programas de usuario.
# period_size/buffer_size iguales a los de arecord: con los del dmix por defecto RetroArch se quedaba
# colgado para siempre en snd_pcm_wait en ~3 de cada 8 arranques (0 de 16 con estos valores).
# Esclavo hw:Loopback,0,0 (el par de hw:Loopback,1,0 que lee arecord): sin subdispositivo, un
# segundo cliente del dmix no abría (Invalid argument) y un reproductor cerrado de golpe dejaba
# la memoria compartida huérfana y nada volvía a sonar hasta borrarla con ipcrm.
ASOUND_CONF='pcm.drc_loop {
    type dmix
    ipc_key 5978293
    ipc_perm 0666
    slave {
        pcm "hw:Loopback,0,0"
        format S16_LE
        rate 48000
        channels 2
        period_size 480
        buffer_size '"${DRC_AUDIO_BUFFER_SIZE}"'
    }
}
pcm.!default {
    type plug
    slave.pcm "drc_loop"
}
ctl.!default {
    type hw
    card Loopback
}'

# Audio "aux": la salida por defecto es el jack de la Pi (tarjeta Headphones); no hay loopback,
# arecord ni --audio-fd.
ASOUND_CONF_AUX='pcm.!default {
    type plug
    slave.pcm "plughw:Headphones"
}
ctl.!default {
    type hw
    card Headphones
}'
if [ "${AUDIO}" = "aux" ]; then
    ASOUND_CONF="${ASOUND_CONF_AUX}"
    AUDIO_CAPTURE_CMD=""
    AUDIO_FD_ARG=""
    AUDIO_DRY_LINE="3b. Audio (aux): /etc/asound.conf con la salida por defecto en la tarjeta Headphones (jack de la Pi)"
    AUDIO_TRAP_ITEM=""
else
    AUDIO_FD_ARG=" --audio-fd 4 4< <(${AUDIO_CAPTURE_CMD})"
    AUDIO_DRY_LINE="3b. Audio: modprobe snd-aloop; /etc/asound.conf con la salida por defecto en dmix:Loopback,0 (S16_LE, 48 kHz)"
    AUDIO_TRAP_ITEM=" arecord,"
fi

# Prioridad tiempo real de drc_stream (0 = sin chrt): bajo carga de CPU se perdían
# slots de frame y el GamePad pedía resync sin parar.
DRC_RT_PRIO="${DRC_RT_PRIO:-50}"

# B17: vídeo como la consola. Paquetes vstrm de 1456 B (16 vstrm + 8 UDP + 20 IP = 1500, sin
# fragmentación IP: el CYW43455 no transmite tramas >1500) y cola fq en wlan0, que reparte
# cada frame según el ritmo del socket de vídeo (DRC_PACING_KBPS en libdrc).
export DRC_VSTRM_PAYLOAD="${DRC_VSTRM_PAYLOAD:-${VSTRM_PAYLOAD_SIZE:-1456}}"
export DRC_TS_OFFSET_US="${DRC_TS_OFFSET_US:-${TS_OFFSET_US:-22000}}"
# Alinear el sello de tiempo de audio (astrm) con DRC_TS_OFFSET_US: libdrc resta 25000 µs por defecto,
# lo que desfasaba el audio -8.3 ms al usar 16684 µs (borde de la ventana de 8 ms del GamePad).
if [ -n "${DRC_AUDIO_TS_OFFSET_US:-}" ]; then
    printf '%s\n' "${DRC_AUDIO_TS_OFFSET_US}" > /tmp/drc_audio_ts_offset_us 2>/dev/null || true
elif [[ "${DRC_TS_OFFSET_US}" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "-${DRC_TS_OFFSET_US}" > /tmp/drc_audio_ts_offset_us 2>/dev/null || true
fi
export DRC_TSF_SAWTOOTH="${DRC_TSF_SAWTOOTH:-${TSF_SAWTOOTH_FILTER:-0}}"
export DRC_TSF_RTT_GATE="${DRC_TSF_RTT_GATE:-${TSF_RTT_GATE:-0}}"
export DRC_MV_RANGE="${DRC_MV_RANGE:-64}"
export DRC_FILLER="${DRC_FILLER:-1}"
# Bitrate de vídeo: VIDEO_BITRATE_MODE «max» (calidad variable con DRC_CRF, VIDEO_BITRATE_KBPS de tope) o «fija»
# (VIDEO_BITRATE_KBPS constante, DRC_CRF no se usa). DRC_VBV_KBPS/DRC_RATE_MODE (drc.conf antiguos o el entorno) mandan.
_rate_mode="${DRC_RATE_MODE:-${VIDEO_BITRATE_MODE:-max}}"
if [ "${_rate_mode}" != "max" ] && [ "${_rate_mode}" != "fija" ]; then
    echo "[!] VIDEO_BITRATE_MODE=«${_rate_mode}» no existe (max o fija): se usa max." >&2
    _rate_mode="max"
fi
export DRC_RATE_MODE="${_rate_mode}"
if [ -n "${DRC_VBV_KBPS:-}" ] && [ -n "${VIDEO_BITRATE_KBPS:-}" ] && [ "${DRC_VBV_KBPS}" != "${VIDEO_BITRATE_KBPS}" ]; then
    echo "[!] DRC_VBV_KBPS=${DRC_VBV_KBPS} pisa VIDEO_BITRATE_KBPS=${VIDEO_BITRATE_KBPS}: quita DRC_VBV_KBPS de drc.conf." >&2
fi
[ -n "${DRC_VBV_KBPS:-${VIDEO_BITRATE_KBPS:-}}" ] && export DRC_VBV_KBPS="${DRC_VBV_KBPS:-${VIDEO_BITRATE_KBPS}}"
if [ "${ENABLE_PACING_FQ:-1}" = "1" ]; then
    PACING_QDISC_CMD="tc qdisc replace dev ${IFACE} root fq"
else
    PACING_QDISC_CMD="true"
fi

# Usuario del escritorio del GamePad (labwc y lo que se lance desde él). Con root, el audio de
# las aplicaciones (root) y el de las pruebas por ssh (usuario) chocaban en el dmix.
DESKTOP_USER="${DRC_DESKTOP_USER:-${SUDO_USER:-root}}"
DESKTOP_UID="$(id -u "${DESKTOP_USER}" 2>/dev/null || echo 0)"
export DRC_DESKTOP_USER="${DESKTOP_USER}"

# Ayudas para anfitriones de escritorio (PC): audio por PipeWire, permisos de entrada, Hyprland.
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/host_pc.sh"
# Hyprland del usuario: en «extended» el GamePad es un monitor virtual suyo; en «separate» solo se le pide
# que ignore la pantalla táctil del mando (el mando tiene su propio escritorio).
HOST_HYPR=0
[ "${DESKTOP_UID}" != "0" ] && hypr_detect && HOST_HYPR=1
HYPR_EXTENDED=0
[ "${MODE}" = "extended" ] && [ "${HOST_HYPR}" = "1" ] && HYPR_EXTENDED=1
HYPR_MIRROR=0
[ "${MODE}" = "mirror" ] && [ "${HOST_HYPR}" = "1" ] && HYPR_MIRROR=1
# separate con Hyprland: otra sesión de Hyprland solo para el GamePad (anidada en un monitor virtual), salvo
# que drc.conf pida otro escritorio (DESKTOP_SESSION, DRC_COMPOSITOR o un DESKTOP_COMMAND que no sea Hyprland).
HYPR_NESTED=0
if [ "${MODE}" = "separate" ] && [ "${HOST_HYPR}" = "1" ] && [ -z "${DRC_COMPOSITOR:-}" ]; then
    case "${DESKTOP_SESSION:-auto}" in auto|hyprland|Hyprland)
        case "${DESKTOP_COMMAND:-Hyprland}" in Hyprland|Hyprland\ *) HYPR_NESTED=1 ;; esac ;;
    esac
fi
# separate con sesión ligera «kiosco» (DESKTOP_SESSION=cage|kiosk|steam): cage ejecuta solo GAMEPAD_AUTOSTART
# (p. ej. Steam) en un compositor headless propio, sin Hyprland ni nada de la sesión del PC.
KIOSK=0
if [ "${MODE}" = "separate" ] && [ "${HYPR_NESTED}" != "1" ]; then
    case "${DESKTOP_SESSION:-auto}" in cage|kiosk|steam) KIOSK=1 ;; esac
fi
# Táctil: «pointer» (ratón), «touch» (toques nativos) o «pen» (lápiz con presión, una tableta para Krita, GIMP...). El lápiz solo
# sirve si quien lo lee admite tabletas: el escritorio del equipo (extended/mirror) o, en separate, una sesión que lea
# libinput (labwc, sway). cage y wiiu-wm no las admiten, y con Hyprland anidado lo cogería el escritorio del PC.
TOUCH_MODE="${DRC_TOUCH_MODE:-pointer}"
if [ "${TOUCH_MODE}" = "pen" ] && [ "${MODE}" = "separate" ]; then
    _pen_why=""
    [ "${KIOSK}" = "1" ] && _pen_why="la sesión del mando es cage, que no admite tabletas"
    [ "${HYPR_NESTED}" = "1" ] && _pen_why="con Hyprland anidado el lápiz iría al escritorio del equipo, no a la sesión del mando"
    case "${DESKTOP_SESSION:-auto}:${DRC_COMPOSITOR:-}" in wiiu-wm:*|*:wiiu-wm) _pen_why="wiiu-wm no admite tabletas" ;; esac
    if [ -n "${_pen_why}" ]; then
        echo "[!] DRC_TOUCH_MODE=pen: ${_pen_why}; el táctil sigue como pantalla táctil (sin presión)." >&2
        TOUCH_MODE="pointer"
    fi
fi
# «touch»: el táctil como toques nativos (gestos en apps Wayland); «pointer»: como ratón (touch2pointer en Hyprland)
[ "${TOUCH_MODE}" = "pen" ] || [ "${TOUCH_MODE}" = "touch" ] || TOUCH_MODE="pointer"
export DRC_TOUCH_MODE="${TOUCH_MODE}"

# Audio: «pipewire» (PC) o «alsa» (Pi: snd-aloop + arecord). AUDIO_BACKEND en drc.conf lo fuerza.
AUDIO_BACKEND_SEL="${DRC_AUDIO_BACKEND:-${AUDIO_BACKEND:-auto}}"
if [ "${AUDIO_BACKEND_SEL}" = "auto" ]; then
    AUDIO_BACKEND_SEL="alsa"
    pc_audio_available && AUDIO_BACKEND_SEL="pipewire"
fi

# Qué configuración se usa de verdad (queda en el registro de la sesión)
CONF_SUMMARY="drc.conf: $([ -f "${DRC_CONF_FILE}" ] && echo "${DRC_CONF_FILE}" || echo "ninguno (valores por defecto)")"
_plat_auto=""; [ "${DRC_PLATFORM:-}" = auto ] && _plat_auto=" [auto]"
CONF_SUMMARY+=" · perfil ${DRC_PLATFORM_NAME:-ninguno}${_plat_auto}${DRC_PLATFORM_FILE:+ (${DRC_PLATFORM_FILE})}"
CONF_SUMMARY+=" · interfaz ${IFACE} · modo ${MODE} · audio ${AUDIO}/${AUDIO_BACKEND_SEL} · táctil ${DRC_TOUCH_MODE}"
if [ -n "${DRC_VBV_KBPS:-}" ]; then
    CONF_SUMMARY+=" · vídeo ${DRC_RATE_MODE} ${DRC_VBV_KBPS} kbps"
    [ "${DRC_RATE_MODE}" = "max" ] && CONF_SUMMARY+=" (CRF ${DRC_CRF:-18})"
else
    CONF_SUMMARY+=" · vídeo QP fijo ${DRC_QP:-32} (sin VIDEO_BITRATE_KBPS)"
fi
CONF_SUMMARY+=" · TS_OFFSET_US=${DRC_TS_OFFSET_US} payload=${DRC_VSTRM_PAYLOAD} sawtooth=${DRC_TSF_SAWTOOTH} rtt_gate=${DRC_TSF_RTT_GATE}"

if [ "${DRC_DRY_RUN}" = "1" ]; then
    echo "=== DRC START DRY RUN (modo '${MODE}') ==="
    echo "0. ${CONF_SUMMARY}"
    echo "1. Ejecutando AP: python3 ${SCRIPT_DIR}/drc_ap.py --start-ap --iface ${IFACE} > ${LOG_FILE} 2>&1 &"
    echo "2. Esperando lease DHCP: [DHCP] Sent Ack ... 192.168.1.11 (timeout ${TIMEOUT_SECS}s)"
    echo "3. Salida virtual: python3 ${SCRIPT_DIR}/drc_ap.py --setup-virtual-display ${MODE} (escritorio como ${DESKTOP_USER}, XDG_RUNTIME_DIR=/tmp/drc_runtime_${DESKTOP_UID})"
    CAPTURE_EXAMPLE=$(python3 "${SCRIPT_DIR}/drc_ap.py" --capture-cmd --mode "${MODE}" 2>/dev/null || echo "wf-recorder")
    echo "${AUDIO_DRY_LINE}"
    echo "4. Captura y transmisión directa: ${CAPTURE_EXAMPLE} | chrt -f ${DRC_RT_PRIO} drc_stream${AUDIO_FD_ARG}"
    echo "4b. Vídeo sin fragmentar y repartido: DRC_VSTRM_PAYLOAD=${DRC_VSTRM_PAYLOAD}; ${PACING_QDISC_CMD}"
    echo "5. Trap de limpieza: trap cleanup INT TERM EXIT -> matar drc_stream,${AUDIO_TRAP_ITEM} ffmpeg, wf-recorder, labwc, hostapd; ip addr flush dev ${IFACE}"
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: Este script debe ejecutarse con privilegios de superusuario (sudo)." >&2
    exit 1
fi

if { [ "${HYPR_EXTENDED}" = "1" ] || [ "${HYPR_NESTED}" = "1" ] || [ "${HYPR_MIRROR}" = "1" ]; } && ! command -v wf-recorder >/dev/null 2>&1; then
    echo "Error: con Hyprland la pantalla del GamePad se captura con wf-recorder (sudo pacman -S wf-recorder)." >&2
    exit 2
fi

# El socket de Wayland vive en el directorio del usuario del escritorio; root (wf-recorder,
# wlr-randr) también entra en él.
if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -d "${XDG_RUNTIME_DIR}" ]; then
    export XDG_RUNTIME_DIR="/tmp/drc_runtime_${DESKTOP_UID}"
    mkdir -p "${XDG_RUNTIME_DIR}"
    chown "${DESKTOP_USER}:" "${XDG_RUNTIME_DIR}"
    chmod 700 "${XDG_RUNTIME_DIR}"
fi

mkdir -p "${CONF_DIR}"
rm -f "${LOG_FILE}"
# Copia de todo lo que sale por pantalla (estadísticas de libdrc incluidas) para poder revisarlo después
exec > >(tee "${CONF_DIR}/session.log") 2>&1
echo $$ > "${CONF_DIR}/start_drc.pid"

AP_PID=""
STREAM_PID=""

reset_wifi() {
    # Solo en ejecución real como root y con hardware Wi-Fi presente
    if [ "${DRC_DRY_RUN}" != "1" ] && [ "$(id -u)" -eq 0 ]; then
        hostapd_cli -p /var/run/hostapd -i "${IFACE}" terminate >/dev/null 2>&1 || true
        pkill -9 -f hostapd 2>/dev/null || true
        pkill -9 -f drc_ap.py 2>/dev/null || true
        ip addr flush dev "${IFACE}" 2>/dev/null || true

        # Si se definió un hook de reinicio en drc.conf, ejecutarlo
        if [ -n "${WIFI_RESET_HOOK:-}" ]; then
            echo "[*] Ejecutando WIFI_RESET_HOOK..."
            eval "${WIFI_RESET_HOOK}" || true
        else
            # Solo reiniciar el módulo y bus SDIO si wlan0 no existe o si hay errores en dmesg
            local need_hw_reset=0
            if [ "${IFACE}" = "wlan0" ] && ! ip link show dev "${IFACE}" >/dev/null 2>&1; then
                need_hw_reset=1
            elif [ "${IFACE}" = "wlan0" ] && dmesg 2>/dev/null | DRC_DMESG_FILE=/dev/stdin wifi_needs_hw_reset; then
                need_hw_reset=1
            fi

            if [ "${need_hw_reset}" -eq 1 ]; then
                echo "[*] Limpiando estado del chip Wi-Fi y bus SDIO tras error detectado..."
                modprobe -r brcmfmac_wcc brcmfmac 2>/dev/null || true
                if [ -e /sys/bus/platform/drivers/mmc-bcm2835/fe300000.mmcnr ]; then
                    echo fe300000.mmcnr > /sys/bus/platform/drivers/mmc-bcm2835/unbind 2>/dev/null || true
                    sleep 0.3
                    echo fe300000.mmcnr > /sys/bus/platform/drivers/mmc-bcm2835/bind 2>/dev/null || true
                    sleep 0.5
                fi
                modprobe brcmfmac 2>/dev/null || true
                for i in $(seq 1 30); do
                    ip link show dev "${IFACE}" >/dev/null 2>&1 && break
                    sleep 0.1
                done
            fi
        fi

        ip link set dev "${IFACE}" up 2>/dev/null || true
        if [ -x /usr/sbin/iw ]; then
            /usr/sbin/iw dev "${IFACE}" set power_save off 2>/dev/null || true
        fi
    fi
}

cleanup() {
    trap - INT TERM EXIT
    echo ""
    echo "[*] Deteniendo servicios y limpiando transmisión DRC..."
    
    [ "${HYPR_EXTENDED}" = "1" ] && hypr_output_cleanup
    [ "${HYPR_NESTED}" = "1" ] && hypr_nested_cleanup
    [ "${HOST_HYPR}" = "1" ] && hypr_restore_session_env
    [ "${HYPR_MIRROR}" = "1" ] && { pkill -INT -x wf-recorder 2>/dev/null || true; }
    [ "${AUDIO_BACKEND_SEL}" = "pipewire" ] && pc_audio_cleanup
    pc_input_cleanup
    pkill -TERM -f "labwc" 2>/dev/null || true
    pkill -TERM -x "wiiu-wm" 2>/dev/null || true
    pkill -TERM -x touch2pointer 2>/dev/null || true
    [ "${KIOSK}" = "1" ] && { pkill -TERM -x cage 2>/dev/null || true; python3 "${SCRIPT_DIR}/drc_ap.py" --stop-kiosk >/dev/null 2>&1 || true; }
    pkill -INT -f "wf-recorder" 2>/dev/null || true
    pkill -INT -f "ffmpeg.*kmsgrab" 2>/dev/null || true
    pkill -INT -f "drc_stream" 2>/dev/null || true
    pkill -INT -f "arecord" 2>/dev/null || true
    # drc_stream lee el TSF del firmware mientras transmite: si hostapd se termina antes de
    # que salga, el núcleo D11 se apaga durante una lectura y el firmware hace un data abort
    # (epc 0x1b1806, el hook del TSF) que deja a la Pi sin wlan0. Espera acotada (5 s).
    # drc_stream ignora SIGINT mientras lee frames y solo sale al cerrarse su tubería; con
    # labwc ya terminado, wf-recorder se cuelga: pasado 1 s se fuerza su cierre.
    # Aun así Streamer::Stop() de libdrc puede tardar hasta ~10 s (Query síncrono al GamePad
    # con 10 reintentos de 1 s) mientras el hilo de vídeo sigue leyendo el TSF: pasados 2 s se
    # mata drc_stream y se espera a que desaparezca antes de tocar hostapd.
    for i in $(seq 1 20); do
        pgrep -f "drc_stream" >/dev/null || break
        [ "$i" -eq 10 ] && { pkill -9 -f "wf-recorder" 2>/dev/null || true; }
        sleep 0.1
    done
    pkill -9 -f "drc_stream" 2>/dev/null || true
    for i in $(seq 1 20); do
        pgrep -f "drc_stream" >/dev/null || break
        sleep 0.1
    done
    pkill -TERM -f "drc_ap.py" 2>/dev/null || true
    hostapd_cli -p /var/run/hostapd -i "${IFACE}" terminate >/dev/null 2>&1 || true
    pkill -TERM -f hostapd 2>/dev/null || true
    ip addr flush dev "${IFACE}" 2>/dev/null || true
    /usr/sbin/tc qdisc del dev "${IFACE}" root 2>/dev/null || true
    
    for i in $(seq 1 10); do
        pgrep -f "hostapd|drc_stream|labwc|wiiu-wm|cage|wf-recorder" >/dev/null || break
        sleep 0.1
    done
    
    pkill -9 -f "labwc" 2>/dev/null || true
    pkill -9 -x "wiiu-wm" 2>/dev/null || true
    pkill -9 -f "wf-recorder" 2>/dev/null || true
    pkill -9 -f "ffmpeg.*kmsgrab" 2>/dev/null || true
    pkill -9 -f "drc_stream" 2>/dev/null || true
    pkill -9 -f "arecord" 2>/dev/null || true
    pkill -9 -f "drc_ap.py" 2>/dev/null || true
    pkill -9 -f hostapd 2>/dev/null || true
    
    # Limpiar solo la interfaz wlan0, preservando eth0
    ip addr flush dev "${IFACE}" 2>/dev/null || true
    rm -f "${CONF_DIR}/start_drc.pid"
    rm -rf "${XDG_RUNTIME_DIR}/wayland-*" 2>/dev/null || true
    rm -f ./- /tmp/- 2>/dev/null || true
    echo "[*] Limpieza completada con éxito."
    exit 0
}

trap cleanup INT TERM EXIT

# 0. Asegurar chip Wi-Fi limpio antes de arrancar AP
reset_wifi

# 1a. Audio. En un PC: sumidero virtual «GamePad» de PipeWire (no se toca /etc/asound.conf); lo que arranque
# el escritorio del mando suena ahí por las variables PULSE_*/PIPEWIRE_*. En la Pi: snd-aloop (más abajo).
if [ "${AUDIO_BACKEND_SEL}" = "pipewire" ]; then
    if [ "${AUDIO}" = "gamepad" ] && [ "${MODE}" = "mirror" ] && pc_audio_setup default; then
        AUDIO_CAPTURE_CMD="${CONF_DIR}/capture_audio.sh"
        echo "[*] Audio (PipeWire): el mando reproduce lo mismo que la salida por defecto del equipo."
    elif [ "${AUDIO}" = "gamepad" ] && [ "${MODE}" != "mirror" ] && pc_audio_setup; then
        AUDIO_CAPTURE_CMD="${CONF_DIR}/capture_audio.sh"
        [ "${HYPR_EXTENDED}" = "1" ] || pc_audio_export_env
        echo "[*] Audio (PipeWire): lo que suene en la salida «GamePad» va al mando."
    else
        [ "${AUDIO}" = "gamepad" ] && echo "[!] No se pudo crear la salida «GamePad» en PipeWire: sesión sin audio en el mando." >&2
        AUDIO="aux"
    fi
fi

if [ "${HYPR_NESTED}" = "1" ]; then
# 1 (separate con Hyprland). Otra sesión de Hyprland solo para el GamePad
echo "[*] Arrancando otra sesión de Hyprland para el GamePad (${DESKTOP_COMMAND:-Hyprland})..."
pc_input_isolate   # el mando solo para la sesión del GamePad (el Steam del escritorio no lo ve)
hypr_nested_setup "${FPS}" "${DESKTOP_COMMAND:-Hyprland}" || { echo "[!] No se pudo arrancar la sesión del GamePad." >&2; exit 1; }
NESTED_TOUCH=0
hypr_nested_binds
if hypr_nested_touch; then NESTED_TOUCH=1; else echo "[!] Sin touch2pointer: el táctil no entrará en la sesión del mando (bash tools/touch2pointer/build.sh)." >&2; fi
export DRC_CAPTURE_CMD="${DRC_CAPTURE_CMD:-${CONF_DIR}/capture_video.sh}"
elif [ "${HYPR_MIRROR}" = "1" ]; then
# 1 (mirror con Hyprland). El GamePad muestra un monitor de la sesión, reescalado
hypr_mirror_setup "${FPS}" || { echo "[!] No encuentro el monitor que duplicar." >&2; exit 1; }
echo "[*] Hyprland de ${DESKTOP_USER}: el GamePad duplica ${HYPR_MIRROR_OUT}."
export DRC_CAPTURE_CMD="${DRC_CAPTURE_CMD:-${CONF_DIR}/capture_video.sh}"
elif [ "${HYPR_EXTENDED}" = "1" ]; then
# 1 (extended con Hyprland). El GamePad es un monitor virtual más de la sesión del usuario
echo "[*] Hyprland de ${DESKTOP_USER}: creando el monitor virtual ${HYPR_OUTPUT} (${DISPLAY_WIDTH:-854}x${DISPLAY_HEIGHT:-480})..."
hypr_output_setup "${FPS}" || { echo "[!] No se pudo crear la salida virtual en Hyprland." >&2; exit 1; }
hypr_extended_autostart
export DRC_CAPTURE_CMD="${DRC_CAPTURE_CMD:-${CONF_DIR}/capture_video.sh}"
else
# 1. Asegurar estado de la pantalla y salida virtual según el modo
echo "[*] Preparando pantalla para modo '${MODE}'..."
if [ "${HOST_HYPR}" != "1" ] && [ -f /sys/class/drm/card1-HDMI-A-1/status ]; then
    HDMI_STATUS=$(cat /sys/class/drm/card1-HDMI-A-1/status)
    if [ "${HDMI_STATUS}" = "disconnected" ]; then
        echo on > /sys/class/drm/card1-HDMI-A-1/status 2>/dev/null || true
    fi
fi

# separate: el mando solo para la sesión del GamePad, que lo ve en su propio /dev/input (el Steam del
# escritorio no). Ver pc_input_isolate en host_pc.sh.
[ "${MODE}" = "separate" ] && pc_input_isolate
python3 "${SCRIPT_DIR}/drc_ap.py" --setup-virtual-display "${MODE}"
if [ "${KIOSK}" = "1" ]; then
    # Táctil del mando -> puntero de la sesión kiosco (cage no lee dispositivos del PC)
    T2P="${SCRIPT_DIR}/../build_local/prefix/bin/touch2pointer"
    [ -x "${T2P}" ] || { command -v cc >/dev/null 2>&1 && bash "${SCRIPT_DIR}/../tools/touch2pointer/build.sh" >/dev/null 2>&1; } || true
    if [ -x "${T2P}" ]; then
        _ru=(); [ "$(id -u)" = "0" ] && [ "${DESKTOP_UID}" != "0" ] && _ru=(runuser -u "${DESKTOP_USER}" --)
        "${_ru[@]}" env XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR}" WAYLAND_DISPLAY=wayland-drc "${T2P}" >> "${CONF_DIR}/touch2pointer.log" 2>&1 &
    else
        echo "[!] Sin touch2pointer: el táctil no entrará en la sesión del mando (bash tools/touch2pointer/build.sh)." >&2
    fi
fi
fi

if [ "${AUDIO_BACKEND_SEL}" != "pipewire" ]; then
# 1b. Salida de audio por defecto de la Pi: el dispositivo virtual snd-aloop
if [ "${AUDIO}" = "aux" ]; then
    echo "[*] Preparando audio (jack de la Pi)..."
else
    echo "[*] Preparando audio (snd-aloop -> GamePad)..."
    modprobe snd-aloop 2>/dev/null || echo "[!] No se pudo cargar snd-aloop; sin audio."
fi
printf '%s\n' "${ASOUND_CONF}" > /etc/asound.conf

fi

# 2. Iniciar punto de acceso y DHCP en segundo plano
echo "[*] ${CONF_SUMMARY}"
echo "[*] Iniciando AP y DHCP para Wii U GamePad..."
python3 "${SCRIPT_DIR}/drc_ap.py" --start-ap --iface "${IFACE}" --out-dir "${CONF_DIR}" --timeout "${TIMEOUT_SECS}" >> "${LOG_FILE}" 2>&1 &
AP_PID=$!

echo "[*] Esperando conexión y asignación de IP del GamePad (timeout ${TIMEOUT_SECS}s)..."
echo "    (Enciende tu Wii U GamePad presionando su botón POWER)"

CONNECTED=0
ASSOC_TIME=0
START_TIME=$(date +%s)
IW_BIN="$(command -v iw 2>/dev/null || echo /usr/sbin/iw)"
while true; do
    if [ -f "${LOG_FILE}" ]; then
        if grep -q "Sent Ack" "${LOG_FILE}" && grep -q "192.168.1.11" "${LOG_FILE}"; then
            CONNECTED=1
            break
        fi
    fi

    # Si el GamePad ya estaba encendido, se asocia por WPA2-PSK pero suele conservar su IP
    # (192.168.1.11) sin repetir DHCP: detectar asociación autenticada + ARP o tras 3 s.
    STA_DUMP="$("${IW_BIN}" dev "${IFACE}" station dump 2>/dev/null || true)"
    if printf '%s\n' "${STA_DUMP}" | grep -q "authorized:.*yes" && printf '%s\n' "${STA_DUMP}" | grep -q "associated:.*yes"; then
        NEIGH_LINE="$(ip -4 neigh show dev "${IFACE}" 192.168.1.11 2>/dev/null || true)"
        if printf '%s\n' "${NEIGH_LINE}" | grep -q "lladdr" && ! printf '%s\n' "${NEIGH_LINE}" | grep -q "FAILED"; then
            echo "[+] GamePad ya asociado y presente en 192.168.1.11 (sin nuevo DHCP)."
            CONNECTED=1
            break
        fi
        if [ "${ASSOC_TIME}" -eq 0 ]; then
            ASSOC_TIME=$(date +%s)
        elif [ $(( $(date +%s) - ASSOC_TIME )) -ge 3 ]; then
            echo "[+] GamePad ya asociado y autenticado (conservando IP 192.168.1.11 sin nuevo DHCP)."
            CONNECTED=1
            break
        fi
    else
        ASSOC_TIME=0
    fi

    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))
    if [ "${ELAPSED}" -ge "${TIMEOUT_SECS}" ]; then
        echo "[!] Timeout alcanzado sin recibir asignación DHCP del GamePad."
        break
    fi

    if ! kill -0 "${AP_PID}" 2>/dev/null; then
        echo "[!] El proceso del AP finalizó inesperadamente. Revisa ${LOG_FILE}"
        break
    fi

    sleep 1
done

if [ "${CONNECTED}" -ne 1 ]; then
    echo "[!] No se pudo conectar con el GamePad. Abortando."
    exit 1
fi

echo "[+] ¡GamePad conectado con IP 192.168.1.11!"
echo "[*] Iniciando transmisión directa (${MODE}, ${FPS} fps) hacia el GamePad..."

# Optimizar búferes UDP de red para streaming sin pérdidas
sysctl -w net.core.wmem_max=16777216 >/dev/null 2>&1 || true
sysctl -w net.core.rmem_max=16777216 >/dev/null 2>&1 || true
sysctl -w net.core.wmem_default=2097152 >/dev/null 2>&1 || true

CAPTURE_CMD=$(python3 "${SCRIPT_DIR}/drc_ap.py" --capture-cmd --mode "${MODE}" --fps "${FPS}")
# wf-recorder passes the destination string directly to avio_open2. If given '-',
# libavformat writes to a literal file named '-' instead of stdout.
# Replacing '-f -' with '-f /dev/stdout' streams directly into the drc_stream pipe.
CAPTURE_CMD="${CAPTURE_CMD//-f -/-f /dev/stdout}"
# wf-recorder escribe texto por stdout ("selected region ...") mezclado con el vídeo:
# el vídeo sale por el fd 3 (el pipe) y su stdout se manda a stderr.
# (entre comillas: en bash ≥5.2 un & sin escapar en el reemplazo es el texto encontrado)
VIDEO_FD3='-f /dev/fd/3 3>&1 1>&2'
CAPTURE_CMD="${CAPTURE_CMD//-f \/dev\/stdout/"${VIDEO_FD3}"}"
# cage: su salida se pone a 854x480 con wlr-randr (drc_ap.cage_argv); si falta, queda en 1280x720 y aquí se reduce.
[ "${KIOSK}" = "1" ] && CAPTURE_CMD="${CAPTURE_CMD//scale=out_range/scale=${DISPLAY_WIDTH:-854}:${DISPLAY_HEIGHT:-480}:out_range}"
# DRC_CAPTURE_CMD: sustituye la captura (debe escribir 854x480 YUV420P en stdout).
CAPTURE_CMD="${DRC_CAPTURE_CMD:-${CAPTURE_CMD}}"

# Captura de vídeo: «wm» (por defecto) = wiiu-wm entrega directamente el búfer que acaba de renderizar
# por el socket $XDG_RUNTIME_DIR/wiiu-wm-frames (sin wf-recorder ni segunda conversión de pantalla);
# «recorder» = wf-recorder como antes. Solo en modo separate y sin DRC_CAPTURE_CMD; si el WM no
# ofrece el socket (labwc, otro modo) se usa wf-recorder.
DRC_CAPTURE="${DRC_CAPTURE:-wm}"
[ "${KIOSK}" = "1" ] && DRC_CAPTURE="recorder"
VIDEO_SOCKET="${XDG_RUNTIME_DIR}/wiiu-wm-frames"
USE_WM_CAPTURE=0
if [ "${DRC_CAPTURE}" = "wm" ] && [ "${MODE}" = "separate" ] && [ -z "${DRC_CAPTURE_CMD:-}" ]; then
    for _ in $(seq 1 50); do
        [ -S "${VIDEO_SOCKET}" ] && { USE_WM_CAPTURE=1; break; }
        sleep 0.1
    done
    [ "${USE_WM_CAPTURE}" = "1" ] || echo "[!] wiiu-wm no ofrece ${VIDEO_SOCKET}: se usa wf-recorder" >&2
fi

export DRC_STREAM_STATS="${DRC_STREAM_STATS:-1}"
${PACING_QDISC_CMD} 2>/dev/null || echo "[!] No se pudo poner la cola fq en ${IFACE}: el vídeo saldrá en ráfaga" >&2
# La redirección del fd 4 (arecord) va literal: guardada en una variable bash no la interpreta.
DRC_STREAM_BIN="${DRC_STREAM_BIN:-drc_stream}"
[ -x "${SCRIPT_DIR}/../build_local/prefix/bin/drc_stream" ] && DRC_STREAM_BIN="${SCRIPT_DIR}/../build_local/prefix/bin/drc_stream"
[ -x /usr/local/bin/drc_stream ] && DRC_STREAM_BIN="/usr/local/bin/drc_stream"
DRC_STREAM_ARGS="--dest 192.168.1.11 --mode ${MODE} --fps ${FPS}"
# Hyprland asigna el táctil y el lápiz al monitor del mando (hypr_map_touch): su lienzo es solo el del mando. Sin
# esto, drc_stream usa el de la Pi (2774x1080, mando a la derecha de 1920) y el toque cae fuera de sitio.
[ "${HYPR_EXTENDED}" = "1" ] && DRC_STREAM_ARGS+=" --canvas-w 854 --canvas-h 480"
if [ "${USE_WM_CAPTURE}" = "1" ]; then
    # Captura directa: drc_stream conecta al WM (sin tubería de vídeo)
    RT=""
    [ "${DRC_RT_PRIO}" != "0" ] && RT="chrt -f ${DRC_RT_PRIO}"
    if [ "${AUDIO}" = "aux" ]; then
        ${RT} ${DRC_STREAM_BIN} ${DRC_STREAM_ARGS} --video-socket "${VIDEO_SOCKET}" &
    elif [ "${DRC_RT_PRIO}" != "0" ]; then
        ${RT} ${DRC_STREAM_BIN} ${DRC_STREAM_ARGS} --video-socket "${VIDEO_SOCKET}" --audio-fd 4 4< <(chrt -f "${DRC_RT_PRIO}" ${AUDIO_CAPTURE_CMD}) &
    else
        ${DRC_STREAM_BIN} ${DRC_STREAM_ARGS} --video-socket "${VIDEO_SOCKET}" --audio-fd 4 4< <(${AUDIO_CAPTURE_CMD}) &
    fi
elif [ "${AUDIO}" = "aux" ]; then
    if [ "${DRC_RT_PRIO}" != "0" ]; then
        eval "${CAPTURE_CMD}" | chrt -f "${DRC_RT_PRIO}" ${DRC_STREAM_BIN} ${DRC_STREAM_ARGS} &
    else
        eval "${CAPTURE_CMD}" | ${DRC_STREAM_BIN} ${DRC_STREAM_ARGS} &
    fi
else
    if [ "${DRC_RT_PRIO}" != "0" ]; then
        eval "${CAPTURE_CMD}" | chrt -f "${DRC_RT_PRIO}" ${DRC_STREAM_BIN} ${DRC_STREAM_ARGS} --audio-fd 4 4< <(chrt -f "${DRC_RT_PRIO}" ${AUDIO_CAPTURE_CMD}) &
    else
        eval "${CAPTURE_CMD}" | ${DRC_STREAM_BIN} ${DRC_STREAM_ARGS} --audio-fd 4 4< <(${AUDIO_CAPTURE_CMD}) &
    fi
fi
STREAM_PID=$!
# Pantalla táctil del mando en la sesión Hyprland del PC: en extended va a su monitor virtual; en separate
# se ignora (pertenece al escritorio del mando)
if [ "${HOST_HYPR}" = "1" ]; then
    sleep 2
    # DRC_TOUCH_MODE=pointer: además, touch2pointer lo convierte en ratón en ese monitor («touch» = toques nativos)
    _t2p_err="[!] Sin touch2pointer: el táctil queda como toques nativos (bash tools/touch2pointer/build.sh)"
    if [ "${HYPR_MIRROR}" = "1" ]; then
        hypr_map_touch "${HYPR_MIRROR_OUT}"
        if [ "${DRC_TOUCH_MODE}" = "pointer" ]; then hypr_touch_pointer "${HYPR_MIRROR_OUT}" || echo "${_t2p_err}" >&2; fi
    elif [ "${HYPR_NESTED}" = "1" ] && [ "${NESTED_TOUCH:-0}" = "1" ]; then :   # lo lleva touch2pointer
    elif [ "${HYPR_EXTENDED}" = "1" ] || [ "${HYPR_NESTED}" = "1" ]; then
        hypr_map_touch
        if [ "${HYPR_EXTENDED}" = "1" ] && [ "${DRC_TOUCH_MODE}" = "pointer" ]; then
            hypr_touch_pointer "${HYPR_OUTPUT}" || echo "${_t2p_err}" >&2
        fi
    elif [ "${MODE}" = "separate" ]; then hypr_ignore_touch; fi
    hypr_restore_session_env
fi

while kill -0 "${STREAM_PID}" 2>/dev/null; do
    # Sin punto de acceso el mando se queda con la última imagen y no puede volver a conectar:
    # mejor cerrar la sesión con un aviso que seguir transmitiendo a nadie.
    if ! kill -0 "${AP_PID}" 2>/dev/null; then
        echo "[!] El punto de acceso (hostapd/drc_ap.py) ha terminado: se cierra la sesión. Mira ${LOG_FILE} y 'journalctl -k | grep hostapd'." >&2
        break
    fi
    [ "${HOST_HYPR}" = "1" ] && hypr_restore_session_env
    sleep 2
done
