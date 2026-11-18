#!/bin/bash
# drc-pad: estado y controles del Wii U GamePad para la barra del escritorio (waybar).
# Habla con drc_stream por /tmp/drc_ctl y lee /tmp/drc_status (los publica drc_stream).
#
#   drc-pad battery                       -> JSON para waybar (nivel y carga)
#   drc-pad volume [up|down|mute|N]       -> JSON / cambia la ganancia del audio (0..100)
#   drc-pad brightness [up|down|N]        -> JSON / brillo de la pantalla del mando (1..5)
#   drc-pad nfc [on|off|toggle]           -> JSON / enciende o apaga el lector NFC
#   drc-pad workspace                     -> JSON con el escritorio de wiiu-wm
#   drc-pad ir [on|off|toggle]            -> JSON / luces infrarrojas de la barra de sensores del mando
#   drc-pad menu                          -> abre/cierra el desplegable de ajustes del mando (panel GTK, drc_menu.py)
#   drc-pad mode [usb|internal]           -> JSON / modo del enlace: USB (wlan1) o WiFi interna (wlan0)
set -u

CTL=/tmp/drc_ctl
OUT=/tmp/drc_ctl.out
STATUS=/tmp/drc_status
STATE_DIR=${XDG_RUNTIME_DIR:-/tmp}/drc-pad
mkdir -p "$STATE_DIR"

ctl() {   # orden al mando; no se cuelga si drc_stream no está
    [ -p "$CTL" ] || return 1
    timeout 2 sh -c "printf '%s\n' \"\$1\" > $CTL" sh "$1"
}

status_get() { [ -r "$STATUS" ] && grep -m1 "^$1=" "$STATUS" | cut -d= -f2; }

json() {  # texto, tooltip, clase, porcentaje
    printf '{"text":"%s","tooltip":"%s","class":"%s","percentage":%s}\n' "$1" "$2" "$3" "${4:-0}"
}

refresh_bar() { pkill -RTMIN+9 -x waybar 2>/dev/null || true; }

# Respuesta a la última orden «raw» (NFC): espera a que drc_stream la añada a la salida
raw_reply() {
    local before=$1 i
    for i in $(seq 30); do
        local now; now=$(wc -l < "$OUT" 2>/dev/null || echo 0)
        if [ "$now" -gt "$before" ]; then
            tail -n +"$((before + 1))" "$OUT" | grep -m1 "^raw " && return 0
        fi
        sleep 0.1
    done
    return 1
}

nfc_cmd() {  # (5,16) con carga de 592 B: primer byte = orden (0x00 encender, 0x06 apagar)
    local cmd=$1 payload="" i
    payload="$cmd"
    for i in $(seq 591); do payload="$payload 00"; done
    local before; before=$(wc -l < "$OUT" 2>/dev/null || echo 0)
    ctl "raw 5 16 $payload" || return 1
    raw_reply "$before"
}

case "${1:-}" in
battery)
    valid=$(status_get valid)
    if [ "${valid:-0}" != 1 ]; then json "▱▱▱▱▱ --" "GamePad sin conectar" "disconnected" 0; exit 0; fi
    raw=$(status_get battery); charging=$(status_get charging); ac=$(status_get ac)
    # El valor crudo (~140..160) se compara con la tabla de calibración del propio mando
    # (`get battery` = CCRCDCPerGetBatteryParams: 02 + 5 umbrales descendentes, p. ej. 9e 9a 97 95 8d).
    th_file="$STATE_DIR/battery_thresholds"
    if [ ! -s "$th_file" ]; then
        before=$(wc -l < "$OUT" 2>/dev/null || echo 0)
        ctl "get battery"
        for i in $(seq 20); do
            line=$(tail -n +"$((before + 1))" "$OUT" 2>/dev/null | grep -m1 "^battery ok")
            [ -n "$line" ] && break; sleep 0.1
        done
        # «battery ok <12 B de cabecera> 02 t1 t2 t3 t4 t5»
        set -- $line
        if [ $# -ge 20 ]; then shift 15; echo "$((16#$1)) $((16#$2)) $((16#$3)) $((16#$4)) $((16#$5))" > "$th_file"; fi
    fi
    read -r t1 t2 t3 t4 t5 < "$th_file" 2>/dev/null || { t1=158; t2=154; t3=151; t4=149; t5=141; }
    if   [ "$raw" -ge "${t1:-158}" ]; then pct=100
    elif [ "$raw" -ge "${t2:-154}" ]; then pct=80
    elif [ "$raw" -ge "${t3:-151}" ]; then pct=60
    elif [ "$raw" -ge "${t4:-149}" ]; then pct=40
    elif [ "$raw" -ge "${t5:-141}" ]; then pct=20
    else pct=5; fi
    cls=normal
    [ "$pct" -le 34 ] && cls=warning
    [ "$pct" -le 17 ] && cls=critical
    bars=""; for i in 1 2 3 4 5; do [ $((i * 20)) -le $((pct + 10)) ] && bars="$bars▰" || bars="$bars▱"; done
    icon=""
    if [ "${charging:-0}" = 1 ] || [ "${ac:-0}" = 1 ]; then cls=charging; icon="⚡"; fi
    json "$icon$bars $pct%" "Batería del GamePad: $pct% (valor $raw)$([ "$charging" = 1 ] && echo ', cargando')" "$cls" "$pct"
    ;;
volume)
    cur=$(status_get volume); cur=${cur:-100}
    case "${2:-}" in
        up) ctl "volume +10"; refresh_bar ;;
        down) ctl "volume -10"; refresh_bar ;;
        mute)
            if [ "$cur" -gt 0 ]; then echo "$cur" > "$STATE_DIR/volume_before_mute"; ctl "volume 0"
            else ctl "volume $(cat "$STATE_DIR/volume_before_mute" 2>/dev/null || echo 70)"; fi
            refresh_bar ;;
        [0-9]*) ctl "volume $2"; refresh_bar ;;
        *)
            json "♪ $([ "$cur" -eq 0 ] && echo silencio || echo "$cur%")" "Volumen del audio del GamePad (el deslizador físico también cuenta)" \
                 "$([ "$cur" -eq 0 ] && echo muted || echo normal)" "$cur" ;;
    esac
    ;;
brightness)
    file="$STATE_DIR/backlight"; cur=$(cat "$file" 2>/dev/null || echo 3)
    case "${2:-}" in
        up|down|[1-5])
            new=$cur
            [ "$2" = up ] && new=$((cur + 1)); [ "$2" = down ] && new=$((cur - 1))
            [[ "$2" =~ ^[1-5]$ ]] && new=$2
            [ "$new" -lt 1 ] && new=1; [ "$new" -gt 5 ] && new=5
            ctl "set backlight $new" && echo "$new" > "$file"
            refresh_bar ;;
        *) json "☀ $cur/5" "Brillo de la pantalla del GamePad" normal $((cur * 20)) ;;
    esac
    ;;
nfc)
    file="$STATE_DIR/nfc"; cur=$(cat "$file" 2>/dev/null || echo off)
    want=${2:-}
    [ "$want" = toggle ] && { [ "$cur" = on ] && want=off || want=on; }
    case "$want" in
        on)
            if reply=$(nfc_cmd 00) && [[ "$reply" == "raw ok"* ]]; then echo on > "$file"; fi
            refresh_bar ;;
        off)
            nfc_cmd 06 >/dev/null; echo off > "$file"; refresh_bar ;;
        *)
            if [ "$cur" = on ]; then json "NFC ●" "Lector NFC encendido (toca para apagar)" on 100
            else json "NFC ○" "Lector NFC apagado (toca para encender)" off 0; fi ;;
    esac
    ;;
ir)
    file="$STATE_DIR/ir"; cur=$(cat "$file" 2>/dev/null || echo off)
    want=${2:-}
    [ "$want" = toggle ] && { [ "$cur" = on ] && want=off || want=on; }
    case "$want" in
        on)  ctl "set sensorbar 1" && echo on > "$file"; refresh_bar ;;
        off) ctl "set sensorbar 0" && echo off > "$file"; refresh_bar ;;
        *)
            if [ "$cur" = on ]; then json "IR ●" "Luces infrarrojas encendidas (toca para apagar)" on 100
            else json "IR ○" "Luces infrarrojas apagadas (toca para encender)" off 0; fi ;;
    esac
    ;;
mode)
    cur=usb; [ -f /etc/systemd/system/drc.service.d/zzz-wlan0.conf ] && cur=internal
    pend="$STATE_DIR/mode_pending"
    now=$(date +%s)
    switch_to() {  # reinicia drc: se lanza fuera del servicio (si no, moriría con él)
        sudo -n systemd-run --no-block --collect --unit=drc-mode-switch /usr/local/bin/drc-mode apply "$1" >/dev/null 2>&1
        echo "$now" > "$STATE_DIR/mode_busy"
    }
    case "${2:-}" in
        usb|internal) switch_to "$2"; refresh_bar; exit 0 ;;
        toggle)
            # Dos toques seguidos (en 5 s): cambiar de modo corta el enlace un minuto, no debe ser accidental
            if [ -f "$pend" ] && [ $((now - $(cat "$pend" 2>/dev/null || echo 0))) -le 5 ]; then
                rm -f "$pend"
                [ "$cur" = usb ] && switch_to internal || switch_to usb
            else
                echo "$now" > "$pend"
                ( sleep 6; rm -f "$pend"; refresh_bar ) >/dev/null 2>&1 &
            fi
            refresh_bar; exit 0 ;;
    esac
    busy=$(cat "$STATE_DIR/mode_busy" 2>/dev/null || echo 0)
    if [ $((now - busy)) -le 90 ] && systemctl is-active --quiet drc-mode-switch 2>/dev/null; then
        json "Enlace …" "Cambiando el modo del enlace del GamePad…" busy 0
    elif [ -f "$pend" ] && [ $((now - $(cat "$pend" 2>/dev/null || echo 0))) -le 5 ]; then
        json "¿Cambiar?" "Toca otra vez para cambiar de modo (se corta el enlace ~1 min)" confirm 0
    elif [ "$cur" = internal ]; then
        json "Enlace: WiFi" "Transmisión por la WiFi interna (wlan0). Toca dos veces para pasar a la WiFi USB" internal 0
    else
        json "Enlace: USB" "Transmisión por la WiFi USB (wlan1). Toca dos veces para pasar a la WiFi interna" usb 0
    fi
    ;;
menu)   # desplegable de ajustes del mando (panel GTK con interruptores, /opt/drc/drc_menu.py); volver a tocar el ⚙ lo cierra
    if pkill -f '^python3 .*drc_menu\.py' 2>/dev/null; then exit 0; fi
    setsid python3 "${GP_MENU_APP:-/opt/drc/drc_menu.py}" >/dev/null 2>&1 &
    ;;
workspace)
    st=${XDG_RUNTIME_DIR:-/tmp}/wiiu-wm.state
    ws=$(grep -m1 '^workspace=' "$st" 2>/dev/null | cut -d= -f2)
    n=$(grep -m1 '^workspaces=' "$st" 2>/dev/null | cut -d= -f2)
    w=$(grep -m1 '^windows_here=' "$st" 2>/dev/null | cut -d= -f2)
    t=$(grep -m1 '^tiled=' "$st" 2>/dev/null | cut -d= -f2)
    json "▣ ${ws:-1}/${n:-1}$([ "${t:-0}" = 1 ] && echo '  ▦')" "Escritorio ${ws:-1} de ${n:-1}: ${w:-0} ventana(s). HOME+L/R para cambiar" normal 0
    ;;
*)
    sed -n '2,11p' "$0"; exit 1 ;;
esac
