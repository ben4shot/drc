# Ayudas para un anfitrión de escritorio (PC con PipeWire y, si la hay, una sesión Hyprland). Lo carga
# start_drc.sh. En la Pi nada de esto se usa: allí el audio va por snd-aloop y no hay sesión de usuario.
#   - Audio (cualquier modo): sumidero virtual de PipeWire «GamePad» + pw-record, sin tocar /etc/asound.conf.
#   - Entrada (modo separate): los dispositivos uinput del mando solo para el escritorio del mando.
#   - Modo extended con Hyprland: el GamePad es un monitor virtual más de la sesión (wf-recorder).
PC_SINK="drc-gamepad"
PC_RUNTIME="/run/user/${DESKTOP_UID}"
HYPR_OUTPUT="${DRC_HYPR_OUTPUT:-DRC-1}"
UDEV_RULE="/run/udev/rules.d/70-drc-gamepad.rules"
# Sesión kiosco: el mando solo para el GamePad (ver pc_input_isolate). 72 = tras 70-uaccess y antes de 73-seat-late.
UDEV_RULE_PAD="/run/udev/rules.d/72-drc-gamepad-only.rules"
UDEV_RULE_MIRROR="/run/udev/rules.d/99-drc-input-mirror.rules"
# En /dev (devtmpfs): /run está montado con nodev y ahí los nodos copiados no se pueden abrir (EACCES)
DRC_INPUT_DIR="/dev/drc_input"
DRC_INPUT_MIRROR="/run/drc_input_mirror.sh"

# Ejecuta una orden como el usuario del escritorio (cambia de usuario solo si corre como root).
pc_run() {
    if [ "$(id -u)" = "0" ]; then
        runuser -u "${DESKTOP_USER}" -- "$@"
    else
        "$@"
    fi
}

# Igual que pc_run, pero dentro del /dev/input propio de la sesión del GamePad (ver pc_input_isolate): para el
# compositor de separate y lo que lee el mando por fuera de él (drc_padbinds). El mismo guion que
# drc_ap.isolated_input_argv (test_input_isolation lo comprueba).
DRC_ISOLATE_SH='d=$1 u=$2 g=$3; shift 3; mount --bind "$d" /dev/input || exit 1; mount -t tmpfs -o mode=1777,nosuid,nodev drc-shm /dev/shm || exit 1; exec setpriv --reuid="$u" --regid="$g" --init-groups -- "$@"'
pc_run_isolated() {
    if [ "$(id -u)" = "0" ] && [ -n "${DRC_INPUT_NS_DIR:-}" ] && [ -d "${DRC_INPUT_NS_DIR}" ] && [ "${DESKTOP_UID}" != "0" ]; then
        unshare --mount --propagation slave -- sh -c "${DRC_ISOLATE_SH}" drc-input \
            "${DRC_INPUT_NS_DIR}" "${DESKTOP_UID}" "$(id -g "${DESKTOP_USER}")" "$@"
    else
        pc_run "$@"
    fi
}

# Ejecuta una orden dentro de la sesión Wayland principal del usuario del escritorio.
pc_user() {
    pc_run env XDG_RUNTIME_DIR="${PC_RUNTIME}" WAYLAND_DISPLAY="${HYPR_WL:-}" \
        HYPRLAND_INSTANCE_SIGNATURE="${HYPR_SIG:-}" DBUS_SESSION_BUS_ADDRESS="unix:path=${PC_RUNTIME}/bus" \
        PULSE_SERVER="unix:${PC_RUNTIME}/pulse/native" DISPLAY="${DISPLAY:-:0}" "$@"
}

hypr_json() { pc_user hyprctl -j "$@" 2>/dev/null; }

# ---------------------------------------------------------------- audio por PipeWire
# ¿Tiene el usuario del escritorio un PipeWire en marcha y están las herramientas?
pc_audio_available() {
    [ "${DESKTOP_UID}" != "0" ] && [ -S "${PC_RUNTIME}/pipewire-0" ] \
        && command -v pw-record >/dev/null 2>&1 && command -v pactl >/dev/null 2>&1
}

pc_audio_cleanup() {
    pkill -f 'drc_audio_router\.py' 2>/dev/null || true
    pkill -INT -x pw-record 2>/dev/null || true
    if [ -s "${CONF_DIR}/pc_old_sink.txt" ]; then
        local old_sink
        old_sink="$(cat "${CONF_DIR}/pc_old_sink.txt")"
        pc_user pactl set-default-sink "${old_sink}" 2>/dev/null || true
        for id in $(pc_user pactl list sink-inputs short 2>/dev/null | cut -f1); do
            pc_user pactl move-sink-input "$id" "${old_sink}" 2>/dev/null || true
        done
        rm -f "${CONF_DIR}/pc_old_sink.txt"
    fi
    if [ -s "${CONF_DIR}/pc_sink.id" ]; then
        local mod_id
        mod_id="$(cat "${CONF_DIR}/pc_sink.id")"
        pc_user pactl unload-module "${mod_id}" >/dev/null 2>&1 || true
        rm -f "${CONF_DIR}/pc_sink.id"
    fi
    # Descargar cualquier null-sink drc-gamepad huérfano si quedara
    local rem_id
    for rem_id in $(pc_user pactl list modules short 2>/dev/null | grep 'sink_name=drc-gamepad' | cut -f1); do
        pc_user pactl unload-module "${rem_id}" >/dev/null 2>&1 || true
    done
}

# Crea el sumidero «GamePad» y el guion que lo graba (48 kHz, s16, estéreo, crudo) para --audio-fd.
# Con «default» no se crea nada: se graba lo que suena en la salida por defecto del PC (modo mirror).
pc_audio_setup() {   # [default]
    local target="--target ${PC_SINK}"
    pc_audio_cleanup
    if [ "${1:-}" = "default" ]; then
        target=""
    else
        pc_user pactl load-module module-null-sink sink_name="${PC_SINK}" \
            sink_properties=device.description=GamePad rate=48000 channels=2 > "${CONF_DIR}/pc_sink.id" 2>/dev/null || return 1
        # Guardar salida previa del PC
        local prev_sink
        prev_sink="$(pc_user pactl get-default-sink 2>/dev/null)"
        [ -n "${prev_sink}" ] && echo "${prev_sink}" > "${CONF_DIR}/pc_old_sink.txt"
        # En modo extended: enrutador dinámico que solo mueve al GamePad las aplicaciones de su escritorio
        if [ "${MODE:-extended}" = "extended" ]; then
            local ws="${DRC_WORKSPACE:-10}"
            pc_user python3 "${SCRIPT_DIR}/drc_audio_router.py" "${prev_sink}" "${PC_SINK}" "${ws}" >> "${CONF_DIR}/audio_router.log" 2>&1 &
        fi
    fi
    cat > "${CONF_DIR}/capture_audio.sh" <<AUD
#!/bin/sh
exec runuser -u ${DESKTOP_USER} -- env XDG_RUNTIME_DIR=${PC_RUNTIME} \\
    pw-record ${target} -P '{ stream.capture.sink = true }' --format s16 --rate 48000 --channels 2 --latency 10ms --raw -
AUD
    chmod +x "${CONF_DIR}/capture_audio.sh"
}

# Variables para que lo que arranque el escritorio del mando (otro XDG_RUNTIME_DIR) suene en «GamePad».
pc_audio_export_env() {
    export PULSE_SERVER="unix:${PC_RUNTIME}/pulse/native" PULSE_SINK="${PC_SINK}"
    export PIPEWIRE_RUNTIME_DIR="${PC_RUNTIME}" PIPEWIRE_NODE="${PC_SINK}"
}

# ---------------------------------------------------------------- entrada del mando
# El escritorio del mando corre como el usuario, que en un PC no suele estar en el grupo «input» (y mejor:
# así no ve el teclado ni el ratón del PC). (UDEV_RULE es la regla antigua: solo se borra si quedó de antes.)

# Modo separate (cualquier sesión): el mando SOLO para la sesión del GamePad. Si no, el Steam del escritorio también
# abre el «Wii U GamePad», aplica su Steam Input y se queda con los botones (el del mando solo recibe HOME).
# En el sistema el mando es solo de root (sin uaccess); la sesión del mando corre en su propio espacio de montaje
# (drc_ap.isolated_input_argv / pc_run_isolated) con DRC_INPUT_DIR como /dev/input: una copia de todos los dispositivos (con sus
# permisos y ACL, así ve también los mandos virtuales de Steam) donde el mando sí es del usuario.
pc_input_kiosk_rules() {   # <usuario>
    printf 'SUBSYSTEM=="input", ATTRS{name}=="Wii U GamePad Touchscreen", OWNER="%s", MODE="0660"\n' "$1"
    printf 'SUBSYSTEM=="input", KERNEL=="event*|js*", ATTRS{name}=="Wii U GamePad", OWNER="root", GROUP="root", MODE="0600", TAG-="uaccess", ENV{DRC_PAD_ONLY}="1"\n'
    # El lápiz (DRC_TOUCH_MODE=pen) también es solo de la sesión del mando: si no, el escritorio del PC dibujaría con él.
    printf 'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="Wii U GamePad Pen", OWNER="root", GROUP="root", MODE="0600", TAG-="uaccess", ENV{DRC_PAD_ONLY}="1"\n'
}

pc_input_mirror_script() {   # <uid> <gid> <directorio>
    cat <<EOF
#!/bin/sh
# Lo ejecuta udev (y pc_input_isolate al empezar) por cada dispositivo de entrada: lo copia en $3, el
# /dev/input de la sesión del GamePad. El mando (DRC_PAD_ONLY) queda del usuario; el resto, como en el sistema.
D="$3"
n="\${DEVNAME##*/}"
[ -n "\$n" ] && [ -d "\$D" ] || exit 0
rm -f "\$D/\$n"
[ "\$ACTION" = remove ] && exit 0
[ -n "\$MAJOR" ] && [ -n "\$MINOR" ] || exit 0
mknod -m 600 "\$D/\$n" c "\$MAJOR" "\$MINOR" || exit 0
if [ "\$DRC_PAD_ONLY" = 1 ]; then
    chown $1:$2 "\$D/\$n"
else
    chown --reference="\$DEVNAME" "\$D/\$n"; chmod --reference="\$DEVNAME" "\$D/\$n"
    getfacl -p "\$DEVNAME" 2>/dev/null | setfacl --set-file=- "\$D/\$n" 2>/dev/null
fi
exit 0
EOF
}

pc_input_isolate() {
    [ "${DESKTOP_UID}" != "0" ] && [ "$(id -u)" = "0" ] || return 0   # sin root (pruebas en seco) no se toca nada
    local gid f mm
    gid="$(id -g "${DESKTOP_USER}")"
    mkdir -p "$(dirname "${UDEV_RULE_PAD}")"
    rm -rf "${DRC_INPUT_DIR}"; mkdir -m 0755 "${DRC_INPUT_DIR}"
    pc_input_mirror_script "${DESKTOP_UID}" "${gid}" "${DRC_INPUT_DIR}" > "${DRC_INPUT_MIRROR}"
    chmod 0755 "${DRC_INPUT_MIRROR}"
    pc_input_kiosk_rules "${DESKTOP_USER}" > "${UDEV_RULE_PAD}"
    printf 'SUBSYSTEM=="input", KERNEL=="event*|js*", RUN+="/bin/sh %s"\n' "${DRC_INPUT_MIRROR}" > "${UDEV_RULE_MIRROR}"
    udevadm control --reload 2>/dev/null || true
    for f in /dev/input/event* /dev/input/js*; do   # los que ya existen (los nuevos los copia udev)
        [ -c "$f" ] || continue
        mm="$(stat -c '%t %T' "$f")"
        ACTION=add DEVNAME="$f" MAJOR="$((0x${mm% *}))" MINOR="$((0x${mm#* }))" sh "${DRC_INPUT_MIRROR}"
    done
    export DRC_INPUT_NS_DIR="${DRC_INPUT_DIR}"
}

pc_input_cleanup() {
    local r changed=0
    for r in "${UDEV_RULE}" "${UDEV_RULE_PAD}" "${UDEV_RULE_MIRROR}"; do
        [ -f "$r" ] && { rm -f "$r"; changed=1; }
    done
    [ "${changed}" = 1 ] && { udevadm control --reload 2>/dev/null || true; }
    rm -f "${DRC_INPUT_MIRROR}"; rm -rf "${DRC_INPUT_DIR}"
    return 0
}

# ---------------------------------------------------------------- Hyprland
# Datos de la sesión Hyprland del usuario del escritorio: 0 si hay una.
hypr_detect() {
    command -v hyprctl >/dev/null 2>&1 || return 1
    # La instancia viva más antigua es la sesión del PC (las anidadas del GamePad son posteriores; los
    # directorios de instancias ya cerradas pueden quedar en hypr/ y no cuentan).
    local datos
    datos="$(pc_run env XDG_RUNTIME_DIR="${PC_RUNTIME}" hyprctl -j instances 2>/dev/null | python3 -c 'import json,sys
i=sorted(json.load(sys.stdin), key=lambda x: x["time"])
print(i[0]["instance"], i[0]["wl_socket"]) if i else None' 2>/dev/null)" || return 1
    HYPR_SIG="${datos%% *}"; HYPR_WL="${datos##* }"
    [ -n "${HYPR_SIG}" ] && [ -n "${HYPR_WL}" ] && [ -S "${PC_RUNTIME}/hypr/${HYPR_SIG}/.socket.sock" ]
}

# Orden de configuración: Hyprland con configuración Lua usa «eval»; los anteriores, «keyword».
hypr_conf() {   # <lua> <keyword> <valor>
    pc_user hyprctl eval "$1" 2>/dev/null | grep -q '^ok' || pc_user hyprctl keyword "$2" "$3" >/dev/null 2>&1 || true
}

# Modo separate: la sesión del PC no debe reaccionar a la pantalla táctil del mando (es del otro escritorio).
hypr_ignore_touch() {
    local d
    for d in wii-u-gamepad-touchscreen wii-u-gamepad-pen; do
        hypr_conf "hl.device({ name = \"${d}\", enabled = false })" "device[${d}]:enabled" false
    done
}

# Modo extended: salida virtual de 854x480 en la sesión y guion de captura (y4m por el fd 3).
hypr_output_setup() {
    local w="${DISPLAY_WIDTH:-854}" h="${DISPLAY_HEIGHT:-480}" fps="$1"
    local ws="${DRC_WORKSPACE:-10}"
    pc_user hyprctl output remove "${HYPR_OUTPUT}" >/dev/null 2>&1 || true
    pc_user hyprctl output create headless "${HYPR_OUTPUT}" >/dev/null || return 1
    hypr_conf "hl.monitor({ output = \"${HYPR_OUTPUT}\", mode = \"${w}x${h}@60\", position = \"auto\", scale = 1 })" \
        monitor "${HYPR_OUTPUT},${w}x${h}@60,auto,1"
    # Vincular workspace exclusivo al monitor del GamePad
    hypr_conf "hl.workspace_rule({ workspace = \"${ws}\", monitor = \"${HYPR_OUTPUT}\", default = true })" \
        workspace "${ws},monitor:${HYPR_OUTPUT},default:true"
    # Conmutar DRC-1 al workspace del GamePad y devolver el foco a la pantalla de la PC
    hypr_conf "hl.dispatch(hl.dsp.focus({ monitor = \"${HYPR_OUTPUT}\" }))" focusmonitor "${HYPR_OUTPUT}"
    hypr_conf "hl.dispatch(hl.dsp.focus({ workspace = ${ws} }))" workspace "${ws}"
    local main_mon
    main_mon="$(hypr_json monitors | python3 -c 'import json,sys
m=[x["name"] for x in json.load(sys.stdin) if x.get("name") != "'"${HYPR_OUTPUT}"'"]
print(m[0] if m else "")' 2>/dev/null)"
    [ -n "${main_mon}" ] && hypr_conf "hl.dispatch(hl.dsp.focus({ monitor = \"${main_mon}\" }))" focusmonitor "${main_mon}"
    cat > "${CONF_DIR}/capture_video.sh" <<VID
#!/bin/sh
exec runuser -u ${DESKTOP_USER} -- env XDG_RUNTIME_DIR=${PC_RUNTIME} WAYLAND_DISPLAY=${HYPR_WL} \\
    wf-recorder -D -y -o ${HYPR_OUTPUT} -m yuv4mpegpipe -c rawvideo -x yuv420p -F scale=out_range=limited -r ${fps} -f pipe:3 3>&1 1>&2
VID
    chmod +x "${CONF_DIR}/capture_video.sh"
}

hypr_map_touch() {   # [salida]  (por defecto, el monitor virtual del GamePad)
    local out="${1:-${HYPR_OUTPUT}}"
    local d
    for d in wii-u-gamepad-touchscreen wii-u-gamepad-pen; do   # el lápiz (DRC_TOUCH_MODE=pen) es una tableta
        hypr_conf "hl.device({ name = \"${d}\", output = \"${out}\" })" "device[${d}]:output" "${out}"
    done
}

# Lanzar autostart en modo extended asignado al workspace del GamePad
hypr_extended_autostart() {
    local cmd="${GAMEPAD_AUTOSTART:-}" ws="${DRC_WORKSPACE:-10}"
    [ -n "${cmd}" ] || return 0
    echo "[*] Iniciando autostart en GamePad (workspace ${ws}): ${cmd}"
    local home; home="$(getent passwd "${DESKTOP_USER}" 2>/dev/null | cut -d: -f6)"
    local _runuser=()
    [ "$(id -u)" = "0" ] && _runuser=(runuser -u "${DESKTOP_USER}" --)
    "${_runuser[@]}" env HOME="${home}" USER="${DESKTOP_USER}" LOGNAME="${DESKTOP_USER}" \
        PATH="/usr/local/bin:/usr/bin:/bin" SHELL=/bin/bash \
        DISPLAY="${DISPLAY:-:0}" WAYLAND_DISPLAY="${HYPR_WL}" XDG_RUNTIME_DIR="${PC_RUNTIME}" \
        XDG_DATA_DIRS="/usr/local/share:/usr/share" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=${PC_RUNTIME}/bus" \
        hyprctl dispatch "hl.dsp.exec_cmd('[workspace ${ws}] ${cmd}')" >/dev/null 2>&1 || \
    ( setsid "${_runuser[@]}" env HOME="${home}" USER="${DESKTOP_USER}" LOGNAME="${DESKTOP_USER}" \
        PATH="/usr/local/bin:/usr/bin:/bin" SHELL=/bin/bash \
        DISPLAY="${DISPLAY:-:0}" WAYLAND_DISPLAY="${HYPR_WL}" XDG_RUNTIME_DIR="${PC_RUNTIME}" \
        XDG_DATA_DIRS="/usr/local/share:/usr/share" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=${PC_RUNTIME}/bus" \
        ${cmd} & ) 2>/dev/null || true
}

# Modo mirror: el GamePad muestra un monitor real de la sesión, reescalado a 854x480.
# DRC_MIRROR_OUTPUT elige cuál (por defecto el primero de la sesión).
hypr_mirror_setup() {
    local w="${DISPLAY_WIDTH:-854}" h="${DISPLAY_HEIGHT:-480}" fps="$1"
    HYPR_MIRROR_OUT="${DRC_MIRROR_OUTPUT:-$(hypr_json monitors | python3 -c 'import json,sys
m=sorted(json.load(sys.stdin), key=lambda x: x["id"])
print(m[0]["name"] if m else "")' 2>/dev/null)}"
    [ -n "${HYPR_MIRROR_OUT}" ] || return 1
    cat > "${CONF_DIR}/capture_video.sh" <<VID
#!/bin/sh
exec runuser -u ${DESKTOP_USER} -- env XDG_RUNTIME_DIR=${PC_RUNTIME} WAYLAND_DISPLAY=${HYPR_WL} \\
    wf-recorder -D -y -o ${HYPR_MIRROR_OUT} -m yuv4mpegpipe -c rawvideo -x yuv420p -F scale=${w}:${h}:out_range=limited -r ${fps} -f pipe:3 3>&1 1>&2
VID
    chmod +x "${CONF_DIR}/capture_video.sh"
}

hypr_output_cleanup() {
    pkill -INT -x wf-recorder 2>/dev/null || true
    pc_user hyprctl output remove "${HYPR_OUTPUT}" >/dev/null 2>&1 || true
    echo detect > /sys/class/drm/card1-HDMI-A-1/status 2>/dev/null || true
}

# ---------------------------------------------------------------- modo separate con Hyprland
# Otra sesión de Hyprland solo para el GamePad. Hyprland no arranca sin pantalla mientras otra instancia tiene
# la gráfica, así que la segunda corre anidada: su ventana ocupa a pantalla completa un monitor virtual de la
# sesión del PC (que nadie ve) y de ahí se captura. Es una instancia aparte: su propio socket Wayland, sus
# propios espacios de trabajo y sus propias aplicaciones.
NESTED_LOG_NAME="nested_desktop.log"

# PID de la ventana «aquamarine» (la instancia anidada) más reciente en la sesión del PC.
hypr_nested_pid() {
    hypr_json clients | python3 -c 'import json,sys
c=[w["pid"] for w in json.load(sys.stdin) if w.get("class")=="aquamarine"]
print(c[-1] if c else "")' 2>/dev/null
}

hypr_output_workspace() {
    hypr_json monitors | python3 -c 'import json,sys
m=[x["activeWorkspace"]["id"] for x in json.load(sys.stdin) if x.get("name")==sys.argv[1]]
print(m[0] if m else "")' "${HYPR_OUTPUT}" 2>/dev/null
}

# Despachador: sintaxis Lua (eval) y, si no, la clásica (dispatch).
hypr_dispatch() {   # <lua> <despachador> <args>
    pc_user hyprctl eval "hl.dispatch($1)" 2>/dev/null | grep -q '^ok' || pc_user hyprctl dispatch "$2" "$3" >/dev/null 2>&1 || true
}

# Resuelve la ruta al archivo de configuración del compositor para el GamePad (definido en drc.conf)
hypr_nested_find_config() {
    if [ -n "${DESKTOP_CONFIG:-}" ] && [ -f "${DESKTOP_CONFIG}" ]; then
        echo "${DESKTOP_CONFIG}"
        return 0
    fi
    for path in "${SCRIPT_DIR}/../config/hyprland-gamepad.conf" "/etc/drc/hyprland-gamepad.conf"; do
        if [ -f "${path}" ]; then
            echo "${path}"
            return 0
        fi
    done
    return 1
}

# Lanza la aplicación de inicio configurada en drc.conf (GAMEPAD_AUTOSTART) en la sesión del GamePad
hypr_nested_autostart() {
    [ -n "${GAMEPAD_AUTOSTART:-}" ] || return 0
    [ -n "${NESTED_WL:-}" ] || return 0
    echo "[*] Arrancando aplicación en el GamePad: ${GAMEPAD_AUTOSTART}"
    pc_user env WAYLAND_DISPLAY="${NESTED_WL}" ${GAMEPAD_AUTOSTART} >> "${CONF_DIR}/${NESTED_LOG_NAME}" 2>&1 &
}

hypr_nested_setup() {   # <fps> <orden del escritorio>
    local fps="$1" cmd="${2:-Hyprland}" home pid ws i
    hypr_output_setup "${fps}" || return 1
    home="$(getent passwd "${DESKTOP_USER}" | cut -d: -f6)"
    : > "${CONF_DIR}/${NESTED_LOG_NAME}"; chown "${DESKTOP_USER}:" "${CONF_DIR}/${NESTED_LOG_NAME}"

    # Shims en PATH para impedir que nada en la segunda sesión pise WAYLAND_DISPLAY en systemd/D-Bus
    local shim_dir="${CONF_DIR}/shim_bin"
    mkdir -p "${shim_dir}"
    cat > "${shim_dir}/dbus-update-activation-environment" <<'SHIM'
#!/bin/sh
exit 0
SHIM
    cat > "${shim_dir}/systemctl" <<'SHIM'
#!/bin/sh
for arg in "$@"; do
    case "$arg" in
        import-environment|set-environment) exit 0 ;;
    esac
done
exec /usr/bin/systemctl "$@"
SHIM
    chmod +x "${shim_dir}/dbus-update-activation-environment" "${shim_dir}/systemctl"
    chown -R "${DESKTOP_USER}:" "${shim_dir}"

    # Si se pide Hyprland (por defecto), usar el archivo de configuración dedicado (drc.conf / config/)
    if [ "${cmd}" = "Hyprland" ]; then
        local conf
        if conf="$(hypr_nested_find_config)"; then
            cmd="Hyprland -c ${conf}"
        fi
    fi

    # Entorno de usuario limpio: el Wayland de la sesión del PC como «pantalla» y el audio hacia «GamePad»
    # En su propio /dev/input (el mando solo para esta sesión; los juegos que abra lo heredan)
    # shellcheck disable=SC2086
    pc_run_isolated setsid env -i HOME="${home}" USER="${DESKTOP_USER}" LOGNAME="${DESKTOP_USER}" \
        PATH="${shim_dir}:/usr/local/bin:/usr/bin:/bin" SHELL=/bin/sh LANG="${LANG:-C.UTF-8}" \
        XDG_DATA_DIRS="/usr/local/share:/usr/share" \
        XDG_RUNTIME_DIR="${PC_RUNTIME}" WAYLAND_DISPLAY="${HYPR_WL}" XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=Hyprland \
        DBUS_SESSION_BUS_ADDRESS="unix:path=${PC_RUNTIME}/bus" DRC_GAMEPAD_SESSION=1 HYPRLAND_NO_SD_VARS=1 \
        ${PULSE_SINK:+PULSE_SINK=${PULSE_SINK}} ${PIPEWIRE_NODE:+PIPEWIRE_NODE=${PIPEWIRE_NODE}} \
        ${cmd} >> "${CONF_DIR}/${NESTED_LOG_NAME}" 2>&1 &
    for i in $(seq 1 50); do
        pid="$(hypr_nested_pid)"; [ -n "${pid}" ] && break; sleep 0.2
    done
    [ -n "${pid}" ] || { echo "[!] La segunda sesión no abrió su ventana; mira ${CONF_DIR}/${NESTED_LOG_NAME}" >&2; return 1; }
    echo "${pid}" > "${CONF_DIR}/nested.pid"
    hypr_restore_session_env
    # Socket Wayland de la segunda sesión (para inyectarle el táctil del mando)
    local datos
    datos="$(hypr_json instances | python3 -c 'import json,sys
i=[x for x in json.load(sys.stdin) if x.get("pid")==int(sys.argv[1])]
print(i[0]["wl_socket"], i[0]["instance"]) if i else None' "${pid}" 2>/dev/null)"
    NESTED_WL="${datos%% *}"; NESTED_SIG="${datos##* }"
    ws="$(hypr_output_workspace)"
    [ -n "${ws}" ] || { echo "[!] No encuentro el espacio de trabajo de ${HYPR_OUTPUT}." >&2; return 1; }
    hypr_dispatch "hl.dsp.window.move({ workspace = ${ws}, window = \"pid:${pid}\", follow = false })" movetoworkspacesilent "${ws},pid:${pid}"
    hypr_dispatch "hl.dsp.window.fullscreen({ window = \"pid:${pid}\", mode = \"fullscreen\" })" fullscreenstate "2 2,pid:${pid}"
    sleep 0.5
    hypr_nested_scale
    hypr_nested_autostart
}

# La pantalla de la segunda sesión, a escala 1: Hyprland le asigna escala 2 por su cuenta (pantalla «pequeña»),
# con lo que todo sale al doble de tamaño (427x240 útiles) y los toques no caen donde se pulsa.
hypr_nested_scale() {
    local w="${DISPLAY_WIDTH:-854}" h="${DISPLAY_HEIGHT:-480}" out
    [ -n "${NESTED_SIG:-}" ] || return 0
    nested() { pc_run env XDG_RUNTIME_DIR="${PC_RUNTIME}" HYPRLAND_INSTANCE_SIGNATURE="${NESTED_SIG}" "$@"; }
    for out in $(nested hyprctl -j monitors 2>/dev/null | python3 -c 'import json,sys
[print(m["name"]) for m in json.load(sys.stdin)]' 2>/dev/null); do
        nested hyprctl eval "hl.monitor({ output = \"${out}\", mode = \"${w}x${h}@60\", position = \"0x0\", scale = 1 })" 2>/dev/null | grep -q '^ok' \
            || nested hyprctl keyword monitor "${out},${w}x${h}@60,0x0,1" >/dev/null 2>&1 || true
    done
}

# La segunda sesión no debe quedarse con el entorno de la sesión del PC: si su configuración exporta
# WAYLAND_DISPLAY a systemd/D-Bus (uwsm, dbus-update-activation-environment), lo que el usuario lance con
# sus atajos se abriría en el escritorio del mando. Se vuelve a dejar el de la sesión del PC.
hypr_restore_session_env() {
    [ -n "${HYPR_WL:-}" ] || hypr_detect || return 0
    [ -n "${HYPR_WL}" ] || return 0
    pc_user systemctl --user set-environment WAYLAND_DISPLAY="${HYPR_WL}" HYPRLAND_INSTANCE_SIGNATURE="${HYPR_SIG}" DISPLAY=":0" 2>/dev/null || true
    pc_user dbus-update-activation-environment WAYLAND_DISPLAY="${HYPR_WL}" HYPRLAND_INSTANCE_SIGNATURE="${HYPR_SIG}" DISPLAY=":0" >/dev/null 2>&1 || true
}

# El táctil del mando, como puntero dentro de la segunda sesión: un Hyprland anidado no recibe toques del
# que lo contiene. touch2pointer toma el dispositivo en exclusiva (la sesión del PC no lo ve). 0 si arrancó.
hypr_nested_touch() {
    local bin="${SCRIPT_DIR}/../build_local/prefix/bin/touch2pointer"
    [ -n "${NESTED_WL:-}" ] || return 1
    if [ ! -x "${bin}" ] && command -v cc >/dev/null 2>&1; then
        pc_run bash "${SCRIPT_DIR}/../tools/touch2pointer/build.sh" >/dev/null 2>&1 || true
    fi
    [ -x "${bin}" ] || return 1
    pc_user env WAYLAND_DISPLAY="${NESTED_WL}" "${bin}" >> "${CONF_DIR}/${NESTED_LOG_NAME}" 2>&1 &
}

# Modo puntero (DRC_TOUCH_MODE=pointer) en el escritorio del PC (extended/mirror): Hyprland entrega los toques como
# toques (no mueven el cursor; en el escritorio vacío o en apps X11 como Steam no hacen nada). touch2pointer toma el
# táctil y lo convierte en ratón atado al monitor del mando. Como root: en extended el dispositivo es de root.
hypr_touch_pointer() {   # <salida>
    local bin="${SCRIPT_DIR}/../build_local/prefix/bin/touch2pointer"
    [ -n "${HYPR_WL:-}" ] || hypr_detect || return 1
    if [ ! -x "${bin}" ] && command -v cc >/dev/null 2>&1; then
        pc_run bash "${SCRIPT_DIR}/../tools/touch2pointer/build.sh" >/dev/null 2>&1 || true
    fi
    [ -x "${bin}" ] || return 1
    env XDG_RUNTIME_DIR="${PC_RUNTIME}" WAYLAND_DISPLAY="${PC_RUNTIME}/${HYPR_WL}" T2P_OUTPUT="${1}" "${bin}" \
        >> "${CONF_DIR}/touch2pointer.log" 2>&1 &
}

# Atajos con los botones del mando dentro de la segunda sesión. GAMEPAD_BINDS en drc.conf: combinaciones
# separadas por «;», cada una «BOTONES=acción». Acciones: close (cerrar la ventana activa, como SUPER+Q) o
# cmd:<orden de shell>. Por defecto HOME+X=close.
hypr_nested_binds() {
    local spec="${GAMEPAD_BINDS-HOME+X=close}" item combo accion args=()
    [ -n "${spec}" ] && [ -n "${NESTED_SIG:-}" ] || return 0
    local cerrar="hyprctl eval 'hl.dispatch(hl.dsp.window.close())' 2>/dev/null | grep -q '^ok' || hyprctl dispatch killactive"
    while IFS= read -r item; do
        [ -n "${item}" ] || continue
        combo="${item%%=*}"; accion="${item#*=}"
        case "${accion}" in
            close) args+=("${combo}=${cerrar}") ;;
            cmd:*) args+=("${combo}=${accion#cmd:}") ;;
            *) echo "[!] GAMEPAD_BINDS: acción desconocida «${accion}» (valen close y cmd:<orden>)" >&2 ;;
        esac
    done < <(printf '%s\n' "${spec}" | tr ';' '\n')
    [ "${#args[@]}" -gt 0 ] || return 0
    pc_run_isolated env XDG_RUNTIME_DIR="${PC_RUNTIME}" WAYLAND_DISPLAY="${NESTED_WL}" \
        HYPRLAND_INSTANCE_SIGNATURE="${NESTED_SIG}" DBUS_SESSION_BUS_ADDRESS="unix:path=${PC_RUNTIME}/bus" \
        python3 "${SCRIPT_DIR}/drc_padbinds.py" "${args[@]}" >> "${CONF_DIR}/${NESTED_LOG_NAME}" 2>&1 &
}

hypr_nested_cleanup() {
    pkill -x touch2pointer 2>/dev/null || true
    pkill -f '^python3 .*drc_padbinds[.]py' 2>/dev/null || true
    [ -s "${CONF_DIR}/nested.pid" ] && kill "$(cat "${CONF_DIR}/nested.pid")" 2>/dev/null
    rm -f "${CONF_DIR}/nested.pid"
    rm -rf "${CONF_DIR}/shim_bin"
    sleep 0.5; hypr_restore_session_env
    hypr_output_cleanup
}
