#!/usr/bin/env bash
set -euo pipefail

# build_userspace.sh - Compilación nativa en ARM64 de drc-x264, drc-hostap y libdrc para Raspberry Pi 4B

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_HOST=""
BUILD_X264=0
BUILD_HOSTAP=0
BUILD_LIBDRC=0
BUILD_STREAM=0

for arg in "$@"; do
    case "$arg" in
        --x264)
            BUILD_X264=1
            ;;
        --hostap)
            BUILD_HOSTAP=1
            ;;
        --libdrc)
            BUILD_LIBDRC=1
            ;;
        --stream)
            BUILD_STREAM=1
            ;;
        --all)
            BUILD_X264=1
            BUILD_HOSTAP=1
            BUILD_LIBDRC=1
            BUILD_STREAM=1
            ;;
        *@*|192.*)
            TARGET_HOST="$arg"
            ;;
        *)
            echo "Opción desconocida: $arg"
            exit 1
            ;;
    esac
done

if [ -z "${TARGET_HOST}" ]; then
    TARGET_HOST="${DRC_BUILD_HOST:-}"
    [ -n "${TARGET_HOST}" ] || { echo "Indica el equipo de compilación: usuario@host (o DRC_BUILD_HOST)." >&2; exit 1; }
fi

if [ "${BUILD_X264}" -eq 0 ] && [ "${BUILD_HOSTAP}" -eq 0 ] && [ "${BUILD_LIBDRC}" -eq 0 ] && [ "${BUILD_STREAM}" -eq 0 ]; then
    # Por defecto compilar todo si no se especifica componente
    BUILD_X264=1
fi

echo "=== Configuración de compilación userspace DRC ==="
echo "Host destino: ${TARGET_HOST}"
echo "Componentes: x264=${BUILD_X264}, hostap=${BUILD_HOSTAP}, libdrc=${BUILD_LIBDRC}, stream=${BUILD_STREAM}"

# 1. Compilación de x264 con soporte DRC y NEON AArch64
if [ "${BUILD_X264}" -eq 1 ]; then
    echo "=== Preparando y parcheando x264 DRC localmente ==="
    python3 "${SCRIPT_DIR}/patch_x264_drc.py" --apply --src-dir "${SCRIPT_DIR}/../third_party/x264"

    echo "=== Sincronizando x264 hacia ${TARGET_HOST}:~/drc-build/x264 ==="
    ssh -o BatchMode=yes "${TARGET_HOST}" "mkdir -p ~/drc-build/x264"
    rsync -avz --delete \
        --exclude="*.o" --exclude="*.a" --exclude="*.so" --exclude="config.h" --exclude="config.mak" \
        "${SCRIPT_DIR}/../third_party/x264/" "${TARGET_HOST}:~/drc-build/x264/"

    echo "=== Compilando e instalando x264 en ${TARGET_HOST} ==="
    ssh -o BatchMode=yes "${TARGET_HOST}" bash << 'EOF'
set -euo pipefail
cd ~/drc-build/x264

echo "Configurando x264 para AArch64..."
./configure \
    --prefix=/usr/local \
    --enable-shared \
    --enable-static \
    --enable-pic \
    --disable-cli

echo "Compilando x264..."
make -j$(nproc)

echo "Instalando libx264.a y cabeceras..."
sudo make install
sudo ldconfig

echo "Verificando instalación..."
test -f /usr/local/lib/libx264.a
test -f /usr/local/include/x264.h
grep -E '#define HAVE_AARCH64 1' config.h
grep -E '#define HAVE_AS_FUNC 1' config.h
echo "x264 DRC instalado con éxito en /usr/local"
EOF
fi

# 2. Compilación de drc-hostap, wpa_supplicant y netboot para AArch64
if [ "${BUILD_HOSTAP}" -eq 1 ]; then
    echo "=== Configurando drc-hostap con TLS interno y LibTomMath localmente ==="
    python3 "${SCRIPT_DIR}/patch_hostap_drc.py" --hostap-dir "${SCRIPT_DIR}/../third_party/drc-hostap"

    echo "=== Sincronizando drc-hostap hacia ${TARGET_HOST}:~/drc-build/drc-hostap ==="
    ssh -o BatchMode=yes "${TARGET_HOST}" "mkdir -p ~/drc-build/drc-hostap"
    rsync -avz --delete \
        --exclude="*.o" --exclude="*.d" \
        "${SCRIPT_DIR}/../third_party/drc-hostap/" "${TARGET_HOST}:~/drc-build/drc-hostap/"

    echo "=== Compilando netboot, hostapd y wpa_supplicant en ${TARGET_HOST} ==="
    ssh -o BatchMode=yes "${TARGET_HOST}" bash << 'EOF'
set -euo pipefail

echo "Compilando netboot..."
cd ~/drc-build/drc-hostap/netboot
gcc -O2 -o netboot netboot.c
test -x netboot

echo "Compilando hostapd con CONFIG_TENDONIN..."
cd ~/drc-build/drc-hostap/src
make clean || rm -f crypto/*.o tls/*.o
cd ~/drc-build/drc-hostap/hostapd
make clean
make -j$(nproc)
test -x hostapd
./hostapd -v || true

echo "Compilando wpa_supplicant y wpa_cli con CONFIG_TENDONIN..."
cd ~/drc-build/drc-hostap/src
make clean || rm -f crypto/*.o tls/*.o
cd ~/drc-build/drc-hostap/wpa_supplicant
make clean
make -j$(nproc) wpa_supplicant wpa_cli
test -x wpa_supplicant
./wpa_supplicant -v

echo "drc-hostap y wpa_supplicant compilados con éxito en ARM64"
EOF
fi

# 3. Compilación e instalación de libdrc
if [ "${BUILD_LIBDRC}" -eq 1 ]; then
    echo "=== Sincronizando libdrc hacia ${TARGET_HOST}:~/drc-build/libdrc ==="
    ssh -o BatchMode=yes "${TARGET_HOST}" "mkdir -p ~/drc-build/libdrc"
    rsync -avz --delete \
        --exclude="*.o" --exclude="*.d" --exclude="*.a" --exclude="*.so" --exclude="Makefile.config" \
        "${SCRIPT_DIR}/../libdrc/" "${TARGET_HOST}:~/drc-build/libdrc/"

    echo "=== Configurando, compilando e instalando libdrc en ${TARGET_HOST} ==="
    ssh -o BatchMode=yes "${TARGET_HOST}" bash << 'EOF'
set -euo pipefail
cd ~/drc-build/libdrc

export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

echo "Ejecutando configure de libdrc..."
./configure --disable-demos --disable-debug --prefix=/usr/local

echo "Compilando libdrc..."
make clean
make -j$(nproc)

echo "Instalando libdrc en /usr/local..."
sudo make install
sudo ldconfig

echo "Verificando instalación de libdrc..."
test -f /usr/local/lib/libdrc.a
test -f /usr/local/lib/libdrc.so
test -f /usr/local/include/drc/streamer.h
test -f /usr/local/include/drc/internal/h264-encoder.h
echo "libdrc instalada con éxito en /usr/local"
EOF
fi

# 4. Compilación e instalación de drc_stream
if [ "${BUILD_STREAM}" -eq 1 ]; then
    echo "=== Sincronizando drc_stream hacia ${TARGET_HOST}:~/drc-build/drc_stream ==="
    ssh -o BatchMode=yes "${TARGET_HOST}" "mkdir -p ~/drc-build/drc_stream"
    rsync -avz --delete "${SCRIPT_DIR}/../src/drc_stream/" "${TARGET_HOST}:~/drc-build/drc_stream/"

    echo "=== Instalando dependencias y compilando drc_stream en ${TARGET_HOST} ==="
    ssh -o BatchMode=yes "${TARGET_HOST}" bash << 'EOF'
set -euo pipefail
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Instalando ffmpeg..."
    sudo apt-get update -y && sudo apt-get install -y ffmpeg
fi
if ! command -v wf-recorder >/dev/null 2>&1 || ! command -v wlr-randr >/dev/null 2>&1; then
    echo "Instalando wf-recorder y wlr-randr..."
    sudo apt-get update -y && sudo apt-get install -y wf-recorder wlr-randr
fi

cd ~/drc-build/drc_stream
echo "Compilando drc_stream..."
g++ -O3 -std=c++17 -Wall -Wextra \
    -I/usr/local/include \
    drc_stream.cpp \
    -L/usr/local/lib -ldrc -lx264 -lswscale -lavutil -lpthread -lm -ldl \
    -o drc_stream

sudo install -m 755 drc_stream /usr/local/bin/drc_stream
test -x /usr/local/bin/drc_stream
echo "drc_stream compilado e instalado con éxito en /usr/local/bin/drc_stream"
EOF
fi

echo "=== Proceso de compilación userspace completado con éxito ==="
