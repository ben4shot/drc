#!/usr/bin/env bash
# Diagnóstico del enlace con el GamePad en un PC: arranca el AP con un patrón de prueba a 60 fps reales
# durante DUR segundos y guarda, con marcas de tiempo, la salida de la sesión, los eventos Wi-Fi, el TSF,
# la estación asociada y los contadores del cortafuegos.   Uso:  sudo bash drchost/diag_pc.sh [segundos]
set -u
[ "$(id -u)" -eq 0 ] || exec sudo bash "${BASH_SOURCE[0]}" "$@"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUR="${1:-30}"
# libdrc y el x264 parcheado están en build_local; con el x264 del sistema drc_stream se cae al crear el codificador
export LD_LIBRARY_PATH="$DIR/../build_local/prefix/lib:${LD_LIBRARY_PATH:-}"
# Restos de una ejecución anterior interrumpida (muestreadores huérfanos)
for p in $(pgrep -f '^bash .*diag_pc\.sh'); do [ "$p" != "$$" ] && kill -9 "$p" 2>/dev/null; done   # nunca los sudo
pkill -x iw 2>/dev/null
trap 'echo "(interrumpido: se recogen los resultados igualmente)"' INT
OUT=/tmp/drc_diag; rm -rf "$OUT"; mkdir -p "$OUT"
# Como start_drc.sh: los DRC_* de drc.conf (calidad, límite de frame, relleno...) pasan a drc_stream y los del entorno mandan
_env_drc="$(export -p | grep -E '^declare -x DRC_' || true)"
[ -f /etc/drc/drc.conf ] && . /etc/drc/drc.conf || . "$DIR/../config/drc.conf"
eval "${_env_drc}"
for _v in "${!DRC_@}"; do export "${_v?}"; done
IF="${WIFI_IFACE}"
stamp() { while IFS= read -r l; do printf '%s %s\n' "$(date +%H:%M:%S.%3N)" "$l"; done; }

{ uname -r; lsmod | grep rtw89; iw dev "$IF" info; ip -d link show "$IF"; iptables -S INPUT | head -5; } > "$OUT/antes.txt" 2>&1
dmesg -c > /dev/null 2>&1 || true
iw event 2>&1 | stamp > "$OUT/iw_event.log" &
EV=$!
(
  while sleep 1; do
    t=$(od -An -tu8 "/sys/class/net/$IF/tsf" 2>&1 | tr -s ' \n' ' ')
    st=$(iw dev "$IF" station dump 2>/dev/null | grep -E 'Station|connected time|tx packets|tx failed|tx retries|rx packets|signal:|tx bitrate' | tr -s ' \t\n' ' ')
    fw=$(iptables -vnL INPUT 2>/dev/null | grep 192.168.1.11 | awk '{print $1" pkts"}')
    printf 'tsf=%s| fw=%s | mtu=%s | %s\n' "$t" "$fw" "$(cat /sys/class/net/$IF/mtu)" "$st"
  done | stamp > "$OUT/muestras.log"
) &
SM=$!

# DIAG_SRC=mandelbrot|testsrc2|... cambia el patrón (con mucho movimiento y detalle, para ver si las ráfagas dependen del contenido)
export DRC_CAPTURE_CMD="ffmpeg -hide_banner -loglevel error -re -f lavfi -i ${DIAG_SRC:-testsrc}=size=854x480:rate=60 -pix_fmt yuv420p -f rawvideo -"
export DRC_STREAM_STATS=1
stdbuf -oL -eL python3 -u "$DIR/drc_ap.py" --start-ap --stream --timeout 60 2>&1 | grep --line-buffered -v 'nl80211: \|hexdump' | stamp > "$OUT/sesion.log" &
sleep 2
echo "Enciende el GamePad AHORA. La prueba dura ${DUR} s (no la cortes); mira si aparece el patrón de colores."
sleep "$DUR"
pkill -INT -f "drc_ap.py --start-ap"
# El apagado ordenado (aviso al mando, hostapd, IP, cortafuegos) tarda: hasta 20 s antes de forzarlo
for _ in $(seq 1 40); do pgrep -f "drc_ap.py --start-ap" >/dev/null || break; sleep 0.5; done
pkill -9 -f "drc_ap.py --start-ap" 2>/dev/null; pkill -9 -f drc_stream 2>/dev/null; pkill -9 -x hostapd 2>/dev/null; pkill -f 'lavfi -i testsrc' 2>/dev/null
kill $EV $SM 2>/dev/null; pkill -x -f "iw event" 2>/dev/null
for p in $(pgrep -f '^bash .*diag_pc\.sh'); do [ "$p" != "$$" ] && kill -9 "$p" 2>/dev/null; done
# Por si hubo que forzar el cierre: que no quede el AP emitiendo ni la regla del cortafuegos
ip addr flush dev "$IF" 2>/dev/null; ip link set "$IF" down 2>/dev/null
iptables -D INPUT -i "$IF" -s 192.168.1.11 -j ACCEPT 2>/dev/null
dmesg > "$OUT/dmesg.log" 2>&1
cp /tmp/drc_conf/hostapd_normal.log /tmp/drc_conf/capture_video.log "$OUT/" 2>/dev/null
chmod -R a+rX "$OUT"
echo "Listo: resultados en $OUT"
