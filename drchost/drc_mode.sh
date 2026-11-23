#!/bin/bash
# drc-mode: elige por qué Wi-Fi sale el enlace del GamePad.
#   drc-mode status            -> usb | internal
#   drc-mode apply usb         -> AP del GamePad en wlan1 (TP-Link USB); Internet y SSH por wlan0
#   drc-mode apply internal    -> AP del GamePad en wlan0 (chip interno con firmware parcheado, MTU 1500);
#                                 Internet y SSH por wlan1
# La red de gestión es la que esté activa al cambiar (VTR, POCOSHOT...): se clona su perfil para wlan1
# (`gestion-wlan1`) y se recuerda su nombre en $BASEFILE para volver a ella por wlan0.
# La conexión de gestión (SSH) pasa siempre a la otra interfaz ANTES de arrancar el AP; si esa conexión no
# se levanta, no se toca nada y se queda en el modo anterior.
set -u
DROPDIR=/etc/systemd/system/drc.service.d
WLAN0_DROP=$DROPDIR/zzz-wlan0.conf
NMCONF=/etc/NetworkManager/conf.d/10-drc-wlan1.conf
LOG=/var/log/drc-mode.log
BASEFILE=/var/lib/drc/gestion_base
GEST=gestion-wlan1
# En modo interno wlan1 es la gestión: los vigías (drc-wake la pone en monitor; drc-ausencia mira sus
# estaciones) no deben correr, y parar drc no debe lanzar drc-wake.
VIGIAS="/etc/systemd/system/drc-wake.service.d/zz-modo-interno.conf /etc/systemd/system/drc-ausencia.service.d/zz-modo-interno.conf"
SINWAKE=$DROPDIR/zzzzz-wlan0-sin-wake.conf
exec >>"$LOG" 2>&1
echo "== $(date '+%F %T') drc-mode $*"

FW=/usr/lib/firmware/cypress/cyfmac43455-sdio-standard.bin
set_fw() {  # patched|standard: el firmware parcheado (OUI Nintendo) solo sirve de AP del GamePad; como cliente no asocia
    [ -f "$FW.patched" ] || { ! cmp -s "$FW" "$FW.orig" && cp "$FW" "$FW.patched"; }
    if [ "$1" = patched ]; then cmp -s "$FW" "$FW.patched" && return 0; cp "$FW.patched" "$FW"
    else cmp -s "$FW" "$FW.orig" && return 0; cp "$FW.orig" "$FW"; fi
    modprobe -r brcmfmac_wcc brcmfmac_cyw brcmfmac_bca brcmfmac
    modprobe brcmfmac; modprobe brcmfmac_wcc
    sleep 4
}

wait_ip() {  # interfaz, segundos -> 0 si tiene IPv4 de gestión (la .10 del enlace del GamePad no cuenta)
    local i
    for i in $(seq "$2"); do ip -4 -o addr show dev "$1" 2>/dev/null | grep -v ' 192\.168\.1\.10/' | grep -q inet && return 0; sleep 1; done
    return 1
}

# B22 Paso 3: durante el cambio de modo, drc-wake no debe arrancar al parar drc (ocuparía wlan0/wlan1).
mask_wake() {
    systemctl mask --runtime drc-wake.service 2>/dev/null || true
    systemctl stop drc-wake drc-ausencia 2>/dev/null || true
}
unmask_wake() {
    systemctl unmask --runtime drc-wake.service 2>/dev/null || true
}

case "${1:-}" in
status)
    [ -f "$WLAN0_DROP" ] && echo internal || echo usb ;;
apply)
    mode=${2:-}
    case "$mode" in
    usb)
        # En modo interno wlan0 es el AP de hostapd: hay que parar drc para liberarla y conectarla como cliente
        # a VTR (gestión) antes de soltar wlan1. Si no obtiene IP, se vuelve a arrancar drc tal como estaba.
        mask_wake
        trap unmask_wake EXIT
        systemctl stop drc
        set_fw standard
        nmcli dev set wlan0 managed yes
        nmcli dev connect wlan0 >/dev/null 2>&1 || true
        base=$(cat "$BASEFILE" 2>/dev/null); base=${base:-VTR-9763219}
        nmcli con up "$base" ifname wlan0 || true
        if ! wait_ip wlan0 25; then
            echo "wlan0 no obtuvo IP: se cancela el cambio y drc vuelve a arrancar"
            set_fw patched
            systemctl start drc
            exit 1
        fi
        rm -f "$WLAN0_DROP" "$DROPDIR/zzzz-mtu1500.conf" "$SINWAKE" $VIGIAS
        [ -f "$NMCONF.disabled" ] && mv "$NMCONF.disabled" "$NMCONF"
        nmcli con down "$GEST" || true
        nmcli con delete "$GEST" || true
        nmcli general reload
        systemctl daemon-reload
        systemctl start drc
        echo "modo usb aplicado" ;;
    internal)
        # wlan1 es ahora el AP de hostapd: hay que parar drc para liberarla (sin que su ExecStopPost lance
        # drc-wake, que la pondría en monitor) y conectarla a la red de gestión ANTES de que wlan0 deje de
        # ser cliente. Si wlan1 no obtiene IP, se deshace todo y drc vuelve a arrancar en wlan1.
        base=$(nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2=="wlan0"{print $1; exit}')
        [ -n "$base" ] || { echo "wlan0 no tiene conexión de gestión activa: se cancela el cambio"; exit 1; }
        mask_wake
        trap unmask_wake EXIT
        printf '[Service]\nExecStopPost=\n' > "$SINWAKE"
        systemctl daemon-reload
        systemctl stop drc drc-wake drc-ausencia 2>/dev/null
        [ -f "$NMCONF" ] && mv "$NMCONF" "$NMCONF.disabled"
        nmcli general reload
        sleep 2
        nmcli dev set wlan1 managed yes
        mkdir -p "$(dirname "$BASEFILE")"; echo "$base" > "$BASEFILE"
        nmcli con delete "$GEST" >/dev/null 2>&1 || true
        nmcli con clone "$base" "$GEST"
        nmcli con modify "$GEST" connection.interface-name wlan1 connection.autoconnect yes 802-11-wireless.band ""
        nmcli con up "$GEST" ifname wlan1 || true
        if ! wait_ip wlan1 30; then
            echo "wlan1 no obtuvo IP: se cancela el cambio y drc vuelve a arrancar"
            nmcli con delete "$GEST" || true
            [ -f "$NMCONF.disabled" ] && mv "$NMCONF.disabled" "$NMCONF"
            nmcli general reload
            rm -f "$SINWAKE"
            systemctl daemon-reload
            systemctl start drc
            exit 1
        fi
        set_fw patched
        # El chip interno no emite tramas de más de 1500 B: MTU 1500 y payload de vídeo 1400
        # Audio en paquetes de 336 muestras: 336*4+8 = 1352 B + 28 UDP/IP = 1380 B. Con 384 (1572 B) cada paquete
        # de audio salía partido en dos fragmentos IP y perder uno cortaba el audio (2026-11-22, 20:52; B19)
        # DRC_TSF_RTT_GATE=1 descarta lecturas TSF lentas por SDIO (evita saltos de reloj, parpadeos y cortes de audio)
        cat > "$WLAN0_DROP" <<CONF
[Service]
ExecStartPre=-/usr/sbin/iw phy phy0 set retry short 15 long 15
Environment=DRC_IFACE=wlan0
Environment=DRC_KEEP_SUPPLICANT=1
Environment=DRC_MTU=1500
Environment=DRC_VSTRM_PAYLOAD=1400
Environment=DRC_PACING_KBPS=10000
Environment=DRC_VBV_KBPS=5500
Environment=DRC_CRF=19
Environment=DRC_QP_MIN=14
Environment=DRC_FRAME_MAX=18000
Environment=DRC_IDR_QP=39
Environment=DRC_AUDIO_PACKET_SAMPLES=336
Environment=DRC_TS_OFFSET_US=16684
Environment=DRC_TSF_RTT_GATE=1
Environment=DRC_TIMEOUT=120
CONF
        # Los vigías (drc-wake y drc-ausencia) leen la interfaz configurada (--iface auto):
        # en wlan0 (sin modo monitor) drc-wake vigila con AP en espera, así que ya no se bloquean.
        rm -f "$DROPDIR/zzzz-mtu1500.conf" "$SINWAKE" $VIGIAS
        systemctl daemon-reload
        systemctl start drc
        echo "modo internal aplicado" ;;
    *) echo "uso: drc-mode apply usb|internal"; exit 2 ;;
    esac ;;
*)
    sed -n '2,9p' "$0"; exit 1 ;;
esac
