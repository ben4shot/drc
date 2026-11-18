#!/bin/bash
# drc_toggle_lock.sh - Bloquear / Desbloquear pantalla del Wii U GamePad
export XDG_RUNTIME_DIR=/tmp/drc_runtime_1000
export WAYLAND_DISPLAY=wayland-drc

ACTION="$1"
LOCK_FILE="/tmp/drc_locked"

if [ -z "$ACTION" ]; then
    if [ -f "$LOCK_FILE" ]; then
        ACTION="unlock"
    else
        ACTION="lock"
    fi
fi

if [ "$ACTION" = "lock" ]; then
    touch "$LOCK_FILE"
    echo "raw 5 8 01" > /tmp/drc_ctl 2>/dev/null || true
    # Si el teclado virtual wvkbd está visible, ocultarlo
    PID=$(pgrep -x wvkbd-mobintl || true)
    if [ -n "$PID" ]; then
        kill -34 $PID 2>/dev/null || true
    fi
elif [ "$ACTION" = "unlock" ]; then
    rm -f "$LOCK_FILE"
    # (5,8): 00/01/02 son modos de apagado; solo ff vuelve a encender el LCD (LVC 0x36e82; comprobado en el mando 2026-11-16)
    echo "raw 5 8 ff" > /tmp/drc_ctl 2>/dev/null || true
    echo "set backlight 4" > /tmp/drc_ctl 2>/dev/null || true
fi
