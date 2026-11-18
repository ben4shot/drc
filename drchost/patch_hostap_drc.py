"""
patch_hostap_drc.py - Configura hostapd y wpa_supplicant para usar TLS interno
y LibTomMath en ARM64, eliminando la dependencia de openssl-1.0.1u, y hace que el AP de
vinculación anuncie el WPS con el OUI de Nintendo (el GamePad solo vincula con esos AP).
"""
from pathlib import Path
import re
import argparse


def patch_defconfig(content: str) -> str:
    """Transforma un defconfig de hostapd/wpa_supplicant a TLS interno + LibTomMath."""
    lines = []
    has_tls_internal = False
    has_libtommath = False
    has_tendonin = False

    for line in content.splitlines():
        # Eliminar referencias a openssl-1.0.1u y rutas heredadas del script antiguo
        if "_123sedreplaceme" in line or "external_pkg_install_dir" in line or "openssl-1.0.1u" in line:
            continue
        if "-lssl" in line or "-lcrypto" in line:
            continue

        # Reemplazar selección de TLS openssl por internal
        if re.match(r"^\s*CONFIG_TLS\s*=\s*openssl", line):
            line = "CONFIG_TLS=internal"
            has_tls_internal = True
        elif re.match(r"^\s*CONFIG_TLS\s*=\s*internal", line):
            has_tls_internal = True
        elif re.match(r"^\s*CONFIG_INTERNAL_LIBTOMMATH\s*=\s*y", line):
            has_libtommath = True
        elif re.match(r"^\s*CONFIG_TENDONIN\s*=\s*y", line):
            has_tendonin = True

        lines.append(line)

    # Inyectar configuraciones obligatorias si no se activaron arriba
    extra = []
    if not has_tendonin:
        extra.append("CONFIG_TENDONIN=y")
    if not has_tls_internal:
        extra.append("CONFIG_TLS=internal")
    if not has_libtommath:
        extra.append("CONFIG_INTERNAL_LIBTOMMATH=y")

    # Asegurar flags de libnl3 y compatibilidad con gcc 14
    extra.append("CFLAGS += -I/usr/include/libnl3 -Wno-error=incompatible-pointer-types -Wno-error=int-conversion -Wno-error=implicit-function-declaration")

    return "\n".join(lines + extra) + "\n"


NINTENDO_WPS_HELPER = r"""
#ifdef CONFIG_TENDONIN
/* drc: el GamePad, al vincular, solo prueba los AP con el IE de WPS de Nintendo (a4:c0:e1:f4; LVC
 * FUN_0007f28e) y descarta los que traen solo el estándar 00:50:f2:04. Se añade una copia de cada IE de
 * WPS con ese OUI detrás de los estándar. */
static struct wpabuf * wps_ie_add_nintendo(struct wpabuf *ie)
{
	struct wpabuf *out;
	const u8 *pos, *end;

	if (ie == NULL)
		return NULL;
	out = wpabuf_alloc(2 * wpabuf_len(ie));
	if (out == NULL)
		return ie;
	wpabuf_put_buf(out, ie);
	pos = wpabuf_head(ie);
	end = pos + wpabuf_len(ie);
	while (end - pos >= 6) {
		u8 len = pos[1];
		if (len < 4 || pos + 2 + len > end)
			break;
		if (pos[0] == WLAN_EID_VENDOR_SPECIFIC && WPA_GET_BE32(pos + 2) == WPS_DEV_OUI_WFA) {
			wpabuf_put_u8(out, WLAN_EID_VENDOR_SPECIFIC);
			wpabuf_put_u8(out, len);
			wpabuf_put_be32(out, WPS_DEV_OUI_NIN);
			wpabuf_put_data(out, pos + 6, len - 4);
		}
		pos += 2 + len;
	}
	wpabuf_free(ie);
	return out;
}
#endif /* CONFIG_TENDONIN */

"""

ENCAPSULATE = "\tbeacon = wps_ie_encapsulate(beacon);\n\tprobe = wps_ie_encapsulate(probe);\n"
ADD_NINTENDO = ("#ifdef CONFIG_TENDONIN\n\tbeacon = wps_ie_add_nintendo(beacon);\n"
                "\tprobe = wps_ie_add_nintendo(probe);\n#endif /* CONFIG_TENDONIN */\n")


def patch_wps_registrar(content: str) -> str:
    """wps_registrar.c: beacon y probe response del AP llevan también el IE de WPS con el OUI de Nintendo."""
    if "wps_ie_add_nintendo" in content:
        return content
    anchor = "static int wps_set_ie(struct wps_registrar *reg)\n{"
    if content.count(anchor) != 1 or content.count(ENCAPSULATE) != 1:
        raise ValueError("wps_registrar.c no tiene la forma esperada (wps_set_ie / wps_ie_encapsulate)")
    content = content.replace(anchor, NINTENDO_WPS_HELPER.lstrip("\n") + anchor)
    return content.replace(ENCAPSULATE, ENCAPSULATE + ADD_NINTENDO)


def configure_hostap_drc(hostap_dir: Path):
    """Crea y actualiza .config para hostapd y wpa_supplicant."""
    hostap_dir = Path(hostap_dir)

    for sub in ("hostapd", "wpa_supplicant"):
        defconfig = hostap_dir / sub / "defconfig"
        if not defconfig.exists():
            raise FileNotFoundError(f"defconfig no encontrado en {defconfig}")

        orig = defconfig.read_text(encoding="utf-8")
        patched = patch_defconfig(orig)
        (hostap_dir / sub / ".config").write_text(patched, encoding="utf-8")

    reg = hostap_dir / "src" / "wps" / "wps_registrar.c"
    reg.write_text(patch_wps_registrar(reg.read_text(encoding="utf-8")), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configurar drc-hostap con TLS interno")
    parser.add_argument("--hostap-dir", type=Path, default=Path(__file__).resolve().parent.parent / "third_party" / "drc-hostap",
                        help="Directorio raíz de drc-hostap")
    args = parser.parse_args()

    configure_hostap_drc(args.hostap_dir)
    print(f"Configuraciones .config generadas con éxito en {args.hostap_dir}")
