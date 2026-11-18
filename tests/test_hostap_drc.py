from pathlib import Path as _P
REPO = _P(__file__).resolve().parent.parent
PKG = REPO / "drchost"
import shutil
from pathlib import Path
import pytest

from drchost.patch_hostap_drc import configure_hostap_drc

LOCAL_HOSTAP_DIR = REPO / "third_party" / "drc-hostap"


def test_drc_hostap_configs_use_internal_tls_and_tendonin(tmp_path: Path):
    """
    Prueba 19: comprueba que .config de hostapd y wpa_supplicant tienen
    CONFIG_TENDONIN=y, CONFIG_TLS=internal y CONFIG_INTERNAL_LIBTOMMATH=y
    sin referencias a openssl-1.0.1u.
    """
    assert LOCAL_HOSTAP_DIR.exists(), f"El código fuente de drc-hostap debe existir en {LOCAL_HOSTAP_DIR}"

    # Copiar a directorio temporal para no ensuciar el árbol
    temp_hostap = tmp_path / "drc-hostap"
    shutil.copytree(LOCAL_HOSTAP_DIR, temp_hostap)

    configure_hostap_drc(temp_hostap)

    # Verificar hostapd/.config
    hostapd_conf = (temp_hostap / "hostapd" / ".config").read_text(encoding="utf-8")
    assert "CONFIG_TENDONIN=y" in hostapd_conf
    assert "CONFIG_TLS=internal" in hostapd_conf
    assert "CONFIG_INTERNAL_LIBTOMMATH=y" in hostapd_conf
    assert "openssl-1.0.1u" not in hostapd_conf
    assert "_123sedreplaceme" not in hostapd_conf
    assert "external_pkg_install_dir" not in hostapd_conf

    # Verificar wpa_supplicant/.config
    wpa_conf = (temp_hostap / "wpa_supplicant" / ".config").read_text(encoding="utf-8")
    assert "CONFIG_TENDONIN=y" in wpa_conf
    assert "CONFIG_TLS=internal" in wpa_conf
    assert "CONFIG_INTERNAL_LIBTOMMATH=y" in wpa_conf
    assert "openssl-1.0.1u" not in wpa_conf
    assert "_123sedreplaceme" not in wpa_conf
    assert "external_pkg_install_dir" not in wpa_conf


import os as _os
TARGET_HOST = _os.environ.get("DRC_TEST_HOST", "")   # usuario@host de una Raspberry Pi para las pruebas de hardware


def test_hostapd_and_wpa_supplicant_build_on_rpi4():
    """
    Prueba 20: compila drc-hostap/hostapd/hostapd, drc-hostap/wpa_supplicant/wpa_supplicant
    y drc-hostap/netboot/netboot en la Raspberry Pi 4B y verifica que los binarios ARM64 arrancan (-v).
    """
    import subprocess

    build_script = PKG / "build_userspace.sh"
    assert build_script.exists(), f"El script {build_script} debe existir"

    # 1. Compilar hostapd, wpa_supplicant y netboot en la Raspberry Pi 4
    res = subprocess.run([str(build_script), "--hostap", TARGET_HOST], capture_output=True, text=True)
    assert res.returncode == 0, f"Error en compilación remota de drc-hostap:\n{res.stderr}\n{res.stdout}"

    # 2. Verificar hostapd -v
    res_h = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, "~/drc-build/drc-hostap/hostapd/hostapd -v || true"],
        capture_output=True,
        text=True,
    )
    assert res_h.returncode == 0, f"hostapd -v falló:\n{res_h.stderr}\n{res_h.stdout}"
    assert "hostapd v" in res_h.stdout or "hostapd v" in res_h.stderr

    # 3. Verificar wpa_supplicant -v
    res_w = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, "~/drc-build/drc-hostap/wpa_supplicant/wpa_supplicant -v"],
        capture_output=True,
        text=True,
    )
    assert res_w.returncode == 0, f"wpa_supplicant -v falló:\n{res_w.stderr}\n{res_w.stdout}"
    assert "wpa_supplicant v" in res_w.stdout or "wpa_supplicant v" in res_w.stderr

    # 4. Verificar netboot
    res_n = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, "test -x ~/drc-build/drc-hostap/netboot/netboot && file ~/drc-build/drc-hostap/netboot/netboot"],
        capture_output=True,
        text=True,
    )
    assert res_n.returncode == 0, f"netboot no es ejecutable:\n{res_n.stderr}\n{res_n.stdout}"
    assert "ARM aarch64" in res_n.stdout

    # 5. Verificar que CONFIG_TENDONIN está compilado
    res_t = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, "grep -q CONFIG_TENDONIN ~/drc-build/drc-hostap/hostapd/.config && grep -q CONFIG_TENDONIN ~/drc-build/drc-hostap/wpa_supplicant/.config"],
        capture_output=True,
        text=True,
    )
    assert res_t.returncode == 0, "CONFIG_TENDONIN no está presente en las configuraciones remotas"


def test_ap_de_vinculacion_anuncia_el_wps_con_el_oui_de_nintendo(tmp_path: Path):
    """El mando, al vincular, solo prueba los AP con un IE de WPS «dd .. a4 c0 e1 f4» (LVC FUN_0007f28e) y descarta
    los que solo traen el estándar 00:50:f2:04. Tras configure_hostap_drc, wps_registrar.c añade a beacon y probe
    response una copia de cada IE de WPS con el OUI de Nintendo (con CONFIG_TENDONIN). Idempotente."""
    from drchost.patch_hostap_drc import patch_wps_registrar
    src = (LOCAL_HOSTAP_DIR / "src" / "wps" / "wps_registrar.c").read_text(encoding="utf-8")
    out = patch_wps_registrar(src)
    assert "WPS_DEV_OUI_NIN" in out and "wps_ie_add_nintendo" in out
    assert out.count("beacon = wps_ie_add_nintendo(beacon);") == 1
    assert out.count("probe = wps_ie_add_nintendo(probe);") == 1
    # la copia va detrás del encapsulado estándar y solo con CONFIG_TENDONIN
    i = out.index("probe = wps_ie_encapsulate(probe);")
    assert out.index("#ifdef CONFIG_TENDONIN", i) < out.index("beacon = wps_ie_add_nintendo(beacon);")
    assert patch_wps_registrar(out) == out

    temp_hostap = tmp_path / "drc-hostap"
    shutil.copytree(LOCAL_HOSTAP_DIR, temp_hostap)
    configure_hostap_drc(temp_hostap)
    assert "wps_ie_add_nintendo" in (temp_hostap / "src" / "wps" / "wps_registrar.c").read_text(encoding="utf-8")
