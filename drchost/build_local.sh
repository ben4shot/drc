#!/usr/bin/env bash
# Compila en ESTE equipo todo lo que drc necesita y lo deja en build_local/prefix (sin root):
#   x264 parcheado, libdrc, drc_stream y hostapd/wpa_supplicant (drc-hostap).
# Uso:  bash drchost/build_local.sh [--x264] [--libdrc] [--stream] [--hostap]     (sin opciones: todo)
# Requisitos: ver «Requisitos» del README (compilador, nasm, ffmpeg-dev, libnl-dev, openssl-dev...).
# Para compilar en otro equipo por ssh (p. ej. una Raspberry Pi) usa drchost/build_userspace.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="$ROOT/build_local/prefix"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
DO_X264=0; DO_LIBDRC=0; DO_STREAM=0; DO_HOSTAP=0
for a in "$@"; do
    case "$a" in
        --x264) DO_X264=1 ;; --libdrc) DO_LIBDRC=1 ;; --stream) DO_STREAM=1 ;; --hostap) DO_HOSTAP=1 ;;
        --all) DO_X264=1; DO_LIBDRC=1; DO_STREAM=1; DO_HOSTAP=1 ;;
        *) echo "Opción desconocida: $a" >&2; exit 1 ;;
    esac
done
[ $((DO_X264 + DO_LIBDRC + DO_STREAM + DO_HOSTAP)) -gt 0 ] || { DO_X264=1; DO_LIBDRC=1; DO_STREAM=1; DO_HOSTAP=1; }
mkdir -p "$PREFIX/bin" "$PREFIX/lib" "$PREFIX/include"
JOBS="$(nproc)"
B="$(mktemp -d)"; trap 'rm -rf "$B"' EXIT

if [ "$DO_X264" = 1 ]; then
    echo "=== x264 parcheado ==="
    [ -d "$ROOT/third_party/x264/.git" ] || bash "$ROOT/drchost/fetch_x264.sh"
    bash "$ROOT/drchost/build_x264_local.sh"
fi

if [ "$DO_LIBDRC" = 1 ]; then
    echo "=== libdrc ==="
    pkg-config --exists x264 libswscale libavutil || { echo "Faltan x264 (parcheado, --x264) o las cabeceras de ffmpeg (libswscale, libavutil)." >&2; exit 1; }
    mkdir -p "$B/libdrc" && cp -r "$ROOT/libdrc/." "$B/libdrc/"
    ( cd "$B/libdrc" && ./configure --disable-demos --disable-debug --prefix="$PREFIX" && make -j"$JOBS" && make install )
fi

if [ "$DO_STREAM" = 1 ]; then
    echo "=== drc_stream ==="
    [ -e "$PREFIX/lib/libdrc.so" ] || [ -e "$PREFIX/lib/libdrc.a" ] || { echo "Falta libdrc en $PREFIX (usa --libdrc)." >&2; exit 1; }
    g++ -O3 -std=c++17 -Wall -Wextra -I"$PREFIX/include" "$ROOT/src/drc_stream/drc_stream.cpp" \
        -L"$PREFIX/lib" -Wl,-rpath,"$PREFIX/lib" -ldrc -lx264 -lswscale -lavutil -lpthread -lm -ldl -o "$PREFIX/bin/drc_stream"
    echo "OK: $PREFIX/bin/drc_stream"
fi

if [ "$DO_HOSTAP" = 1 ]; then
    echo "=== drc-hostap (hostapd, wpa_supplicant) ==="
    mkdir -p "$B/hostap" && cp -r "$ROOT/third_party/drc-hostap/." "$B/hostap/"
    python3 "$ROOT/drchost/patch_hostap_drc.py" --hostap-dir "$B/hostap" >/dev/null
    ( cd "$B/hostap/src" && make clean >/dev/null 2>&1 || true )
    ( cd "$B/hostap/hostapd" && make clean >/dev/null 2>&1; make -j"$JOBS" hostapd hostapd_cli )
    ( cd "$B/hostap/wpa_supplicant" && make clean >/dev/null 2>&1; make -j"$JOBS" wpa_supplicant wpa_cli )
    install -m 755 "$B/hostap/hostapd/hostapd" "$B/hostap/hostapd/hostapd_cli" "$B/hostap/wpa_supplicant/wpa_supplicant" \
        "$B/hostap/wpa_supplicant/wpa_cli" "$PREFIX/bin/"
    echo "OK: hostapd y wpa_supplicant en $PREFIX/bin"
fi
# touch2pointer: el táctil como ratón (DRC_TOUCH_MODE=pointer con Hyprland y la sesión separate). Sin él, el táctil no
# mueve el cursor; no es motivo para abortar el resto.
echo "=== touch2pointer ==="
if command -v wayland-scanner >/dev/null 2>&1 && pkg-config --exists wayland-client; then
    bash "$ROOT/tools/touch2pointer/build.sh"
else
    echo "[!] Sin wayland-scanner o wayland-client (Arch: wayland; Debian: libwayland-dev libwayland-bin): no se compila touch2pointer." >&2
fi
echo "=== Listo: $PREFIX ==="
