"""
patch_hostap_drc.py - Configura hostapd y wpa_supplicant para usar TLS interno
y LibTomMath en ARM64, eliminando la dependencia de openssl-1.0.1u.
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configurar drc-hostap con TLS interno")
    parser.add_argument("--hostap-dir", type=Path, default=Path(__file__).resolve().parent.parent / "third_party" / "drc-hostap",
                        help="Directorio raíz de drc-hostap")
    args = parser.parse_args()

    configure_hostap_drc(args.hostap_dir)
    print(f"Configuraciones .config generadas con éxito en {args.hostap_dir}")
