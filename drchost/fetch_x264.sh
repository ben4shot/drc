#!/usr/bin/env bash
# Descarga x264 de VideoLAN en third_party/x264, en el commit sobre el que están hechos los parches DRC
# (drchost/patch_x264_drc.py). Después: bash drchost/build_x264_local.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
X264_COMMIT="0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee"
DEST="$ROOT/third_party/x264"
[ -d "$DEST/.git" ] || git clone https://code.videolan.org/videolan/x264.git "$DEST"
git -C "$DEST" fetch --quiet origin "$X264_COMMIT" 2>/dev/null || true
git -C "$DEST" checkout --quiet "$X264_COMMIT"
echo "x264 en $DEST ($X264_COMMIT)"
