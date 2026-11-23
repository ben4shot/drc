#!/usr/bin/env bash
# Despliega la sesión DRC en la Pi: scripts en /opt/drc, configuración en /etc/drc y servicio systemd habilitado.
# Uso: install_drc.sh <usuario@host>      (DRC_DRY_RUN=1 solo imprime lo que haría)
set -euo pipefail

HOST="${1:?uso: install_drc.sh <usuario@host>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/opt/drc"
# Todos los scripts de drchost/: start_drc.sh carga host_pc.sh, que llama a drc_audio_router.py y drc_padbinds.py,
# y las herramientas (drc_ctl, drc_wake, la cámara...) se importan entre sí.
FILES=()
for _f in "${SCRIPT_DIR}"/*.sh "${SCRIPT_DIR}"/*.py; do FILES+=("$(basename "${_f}")"); done
# Configuración: los perfiles van junto al drc.conf que se lee en la Pi (/etc/drc), que es donde los buscan
# start_drc.sh y drc_ap.load_drc_conf. El drc.conf de la Pi solo se instala si lo pides con
# DRC_INSTALL_CONF=<fichero> (el config/drc.conf de este equipo suele ser el del PC: otra interfaz, otro modo...).
CONF_SRC="${SCRIPT_DIR}/../config"
PROFILES=()
for _f in "${CONF_SRC}"/platforms/*.conf; do [ -f "${_f}" ] && PROFILES+=("$(basename "${_f}")"); done

run() {
    echo "+ $*"
    if [ "${DRC_DRY_RUN:-0}" != "1" ]; then
        "$@"
    fi
}

STAGE="/tmp/drc_install"
# El escritorio del GamePad corre como el usuario de <usuario@host>.
DESKTOP_USER="${HOST%@*}"
[ "${DESKTOP_USER}" = "${HOST}" ] && DESKTOP_USER="$(whoami)"
UNIT_TMP="$(mktemp)"
trap 'rm -f "${UNIT_TMP}"' EXIT
sed "s/@DESKTOP_USER@/${DESKTOP_USER}/" "${SCRIPT_DIR}/drc.service" > "${UNIT_TMP}"
echo "+ drc.service con Environment=DRC_DESKTOP_USER=${DESKTOP_USER}"

run ssh -o BatchMode=yes "${HOST}" "rm -rf ${STAGE} && mkdir -p ${STAGE}/platforms"
for f in "${FILES[@]}"; do
    run scp -o BatchMode=yes "${SCRIPT_DIR}/${f}" "${HOST}:${STAGE}/${f}"
done
for f in "${PROFILES[@]}"; do
    run scp -o BatchMode=yes "${CONF_SRC}/platforms/${f}" "${HOST}:${STAGE}/platforms/${f}"
done
CONF_INSTALL=""
if [ -n "${DRC_INSTALL_CONF:-}" ]; then
    [ -f "${DRC_INSTALL_CONF}" ] || { echo "DRC_INSTALL_CONF: no existe ${DRC_INSTALL_CONF}" >&2; exit 1; }
    run scp -o BatchMode=yes "${DRC_INSTALL_CONF}" "${HOST}:${STAGE}/drc.conf"
    CONF_INSTALL=" && sudo install -m 644 ${STAGE}/drc.conf /etc/drc/drc.conf"
else
    echo "+ /etc/drc/drc.conf de la Pi sin tocar (DRC_INSTALL_CONF=<fichero> para instalar uno)"
fi
run scp -o BatchMode=yes "${UNIT_TMP}" "${HOST}:${STAGE}/drc.service"
run scp -o BatchMode=yes "${SCRIPT_DIR}/drc-wake.service" "${HOST}:${STAGE}/drc-wake.service"
run scp -o BatchMode=yes "${SCRIPT_DIR}/drc-ausencia.service" "${HOST}:${STAGE}/drc-ausencia.service"
run scp -o BatchMode=yes "${SCRIPT_DIR}/systemd/drc-wake-al-parar.conf" "${HOST}:${STAGE}/drc-wake-al-parar.conf"

INSTALL_LIST=""
for f in "${FILES[@]}"; do INSTALL_LIST+=" ${STAGE}/${f}"; done
run ssh -o BatchMode=yes "${HOST}" "sudo mkdir -p ${DEST} /etc/drc/platforms \
&& sudo install -m 755${INSTALL_LIST} ${DEST}/ \
&& sudo install -m 644 ${STAGE}/platforms/*.conf /etc/drc/platforms/${CONF_INSTALL} \
&& sudo install -m 644 ${STAGE}/drc.service /etc/systemd/system/drc.service \
&& sudo install -m 644 ${STAGE}/drc-wake.service /etc/systemd/system/drc-wake.service \
&& sudo install -m 644 ${STAGE}/drc-ausencia.service /etc/systemd/system/drc-ausencia.service \
&& sudo mkdir -p /etc/systemd/system/drc.service.d \
&& sudo install -m 644 ${STAGE}/drc-wake-al-parar.conf /etc/systemd/system/drc.service.d/zzzz-wake-al-parar.conf \
&& sudo systemctl daemon-reload && sudo systemctl disable drc && sudo systemctl enable drc-wake"
