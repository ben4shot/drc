#!/bin/bash
# Instala las apps táctiles del GamePad (cámara y NFC) en /opt/drc, sus iconos y las registra en el menú.
# Uso (en la Pi, desde drchost/):  sudo -E ./apps/install_apps.sh        (idempotente)
# El menú lee la configuración al arrancar la sesión: aparecen tras el próximo `systemctl restart drc`.
set -eu
AQUI=$(cd "$(dirname "$0")" && pwd)
RAIZ=$(dirname "$AQUI")
USUARIO=${SUDO_USER:-$USER}
HOME_U=$(getent passwd "$USUARIO" | cut -d: -f6)
install -d /opt/drc/apps
install -m644 "$RAIZ/drc_nfc.py" "$RAIZ/drc_cam_v4l2.py" /opt/drc/
install -m644 "$AQUI/ui.py" "$AQUI/drc_camera_app.py" "$AQUI/drc_nfc_app.py" /opt/drc/apps/
IMG="$HOME_U/.config/wiiu-shell/images"
install -d -o "$USUARIO" "$IMG"
install -m644 -o "$USUARIO" "$AQUI/icons/camera.svg" "$AQUI/icons/nfc.svg" "$IMG/"
python3 - "$HOME_U/.config/wiiu-shell/config.json" /etc/wiiu-shell/config.json "$IMG" <<'PY'
import json, sys, os
*configs, img = sys.argv[1:]
NUEVAS = [
    {"name": "Cámara", "exec": "sudo -E env SDL_VIDEODRIVER=wayland python3 /opt/drc/apps/drc_camera_app.py",
     "icon": img + "/camera.svg", "description": "Cámara del GamePad: foto y ajustes",
     "page": 1, "poly": [[668.0, 44.0], [846.0, 44.0], [846.0, 236.0], [636.0, 236.0]]},
    {"name": "NFC", "exec": "env SDL_VIDEODRIVER=wayland python3 /opt/drc/apps/drc_nfc_app.py",
     "icon": img + "/nfc.svg", "description": "Leer y escribir etiquetas NFC",
     "page": 1, "poly": [[10.0, 258.0], [250.0, 258.0], [226.0, 468.0], [10.0, 468.0]]},
]
for ruta in configs:
    if not os.path.exists(ruta):
        continue
    c = json.load(open(ruta))
    ya = {a["name"] for a in c["apps"]}
    anadidas = [a for a in NUEVAS if a["name"] not in ya]
    if not anadidas:
        print(ruta, ": ya registradas")
        continue
    c["apps"].extend(anadidas)
    tmp = ruta + ".tmp"
    json.dump(c, open(tmp, "w"), indent=2, ensure_ascii=False)
    os.replace(tmp, ruta)
    print(ruta, ": añadidas", [a["name"] for a in anadidas])
PY
chown "$USUARIO" "$HOME_U/.config/wiiu-shell/config.json" 2>/dev/null || true
