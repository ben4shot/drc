#!/usr/bin/env bash
# Recompila en ESTE equipo el x264 con los parches DRC y lo deja en build_local/prefix (sin root).
# Con ensamblador (nasm): sin él x264 es varias veces más lento (≈13 ms por frame en un Ryzen en vez de 1–2),
# los frames llegan tarde y el GamePad pide resync sin parar (imagen a tirones).
# Arch:  sudo pacman -S --needed nasm
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v nasm >/dev/null 2>&1 || { echo "Falta nasm (Arch: sudo pacman -S --needed nasm)." >&2; exit 1; }
B="$(mktemp -d)"; trap 'rm -rf "$B"' EXIT
cp -r "$ROOT/third_party/x264/." "$B/"      # copia: el árbol puede tener objetos de otra compilación
python3 "$ROOT/drchost/patch_x264_drc.py" --apply --src-dir "$B" >/dev/null
cd "$B"
make distclean >/dev/null 2>&1 || true
./configure --prefix="$ROOT/build_local/prefix" --enable-shared --enable-static --enable-pic --disable-cli
grep -q '^#define HAVE_MMX 1' config.h || { echo "x264 se configuró sin ensamblador; revisa nasm." >&2; exit 1; }
make -j"$(nproc)"
make install
echo "OK: x264 con ensamblador en $ROOT/build_local/prefix/lib"
