#!/bin/bash
# drc_toggle_keyboard.sh - Conmuta visibilidad del teclado digital wvkbd en el escritorio DRC
USER_NAME="${DRC_DESKTOP_USER:-${SUDO_USER:-$(id -un)}}"
USER_ID="$(id -u "$USER_NAME")"
export XDG_RUNTIME_DIR="/tmp/drc_runtime_${USER_ID}"
export WAYLAND_DISPLAY=wayland-drc

PID=$(pgrep -x wvkbd-mobintl || true)

if [ -z "$PID" ]; then
    su - "$USER_NAME" -c "export WAYLAND_DISPLAY=wayland-drc XDG_RUNTIME_DIR=/tmp/drc_runtime_${USER_ID}; wvkbd-mobintl -L 180 --fn 'DejaVu Sans 14' >/dev/null 2>&1 &"
    exit 0
fi

# Signal 34 (SIGRTMIN) es el toggle nativo de wvkbd (oculta si visible, muestra si oculto)
kill -34 $PID 2>/dev/null || kill -SIGUSR1 $PID 2>/dev/null || true
