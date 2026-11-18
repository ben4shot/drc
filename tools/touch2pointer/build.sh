#!/usr/bin/env bash
# Compila touch2pointer y lo deja en build_local/prefix/bin (sin root). Necesita wayland (wayland-scanner).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
B="$(mktemp -d)"; trap 'rm -rf "$B"' EXIT
X="$HERE/wlr-virtual-pointer-unstable-v1.xml"
wayland-scanner client-header "$X" "$B/wlr-virtual-pointer-unstable-v1-client-protocol.h"
wayland-scanner private-code "$X" "$B/proto.c"
cc -O2 -Wall -Wextra -I"$B" "$HERE/touch2pointer.c" "$B/proto.c" $(pkg-config --cflags --libs wayland-client) -o "$B/touch2pointer"
install -Dm755 "$B/touch2pointer" "$ROOT/build_local/prefix/bin/touch2pointer"
echo "OK: $ROOT/build_local/prefix/bin/touch2pointer"
