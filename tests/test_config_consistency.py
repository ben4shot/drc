"""La configuración tiene sentido: cada clave que se ofrece la usa alguien, los perfiles de plataforma se aplican igual
en Python y en el shell, la interfaz elegida es una sola y los ajustes de driver no hacen cosas peligrosas."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "drchost"))
import drc_ap  # noqa: E402
import drc_ctl  # noqa: E402

CONFIG = REPO / "config"
PROFILES = sorted((CONFIG / "platforms").glob("*.conf"))
PENDING = set()


def _keys(path: Path):
    """Claves KEY= de un .conf, también las comentadas (opciones documentadas)."""
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", path.read_text(), re.M))


def _code_text():
    """Todo el código que puede leer una clave (sin el menú, que solo las muestra, ni las pruebas)."""
    files = [p for p in (REPO / "drchost").rglob("*") if p.suffix in (".py", ".sh", ".service", ".conf")]
    files += list((REPO / "src").rglob("*.[ch]*")) + list((REPO / "libdrc" / "src").rglob("*.cpp"))
    files += list((REPO / "libdrc" / "include").rglob("*.h")) + list((REPO / "apps").rglob("*.py"))
    files += list((REPO / "tools").rglob("*.py"))
    return "\n".join(p.read_text(errors="replace") for p in files if p.name not in ("drc_ctl.py", "drc_gui.py"))


@pytest.fixture(scope="module")
def code():
    return _code_text()


@pytest.mark.parametrize("conf", [CONFIG / "drc.conf.example", *PROFILES, *sorted((CONFIG / "examples").glob("*.conf"))],
                         ids=lambda p: p.name)
def test_cada_clave_de_los_conf_la_usa_alguien(conf, code):
    if conf.name == "hyprland-gamepad.conf":
        pytest.skip("configuración de Hyprland, no de drc")
    muertas = sorted(k for k in _keys(conf) - PENDING if not re.search(rf"\b{k}\b", code))
    assert not muertas, f"{conf.name}: claves que nadie lee: {muertas}"


def test_cada_ajuste_del_menu_hace_algo(code):
    muertos = sorted(f.key for f in drc_ctl.CONFIG_FIELDS
                     if f.key not in PENDING and not re.search(rf"\b{f.key}\b", code))
    assert not muertos, f"el menú deja cambiar ajustes que no hacen nada: {muertos}"


def test_quitados_los_ajustes_que_no_hacian_nada():
    for k in ("AUDIO_ENABLED", "UINPUT_ENABLED", "CAMERA_ENABLED", "NFC_ENABLED"):
        assert k not in drc_ctl.FIELDS_BY_KEY
        assert k not in _keys(CONFIG / "drc.conf.example")


def test_el_perfil_rtw89_no_carga_el_driver_del_kernel():
    """`modprobe rtw89_8852au` carga el driver del KERNEL (sin parche de TSF) aunque esté en la lista negra; el
    parcheado de morrownr es rtw89_8852au_git. Recargarlo en cada sesión tampoco hace falta."""
    for prof in PROFILES:
        hook = drc_ap.load_drc_conf(str(_conf_with(prof.stem))).get("WIFI_RESET_HOOK", "")
        assert not re.search(r"modprobe(\s+-r)?\s+rtw89_\w+?(?<!_git)\b", hook), f"{prof.name}: {hook}"


_TMP = []


def _conf_with(platform, extra=""):
    import tempfile
    d = Path(tempfile.mkdtemp())
    _TMP.append(d)
    (d / "drc.conf").write_text(f'DRC_PLATFORM="{platform}"\n{extra}')
    return d / "drc.conf"


def _active_keys(path: Path):
    return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", path.read_text(), re.M))


def test_drc_conf_example_no_pisa_los_perfiles():
    """Si el ejemplo definía TS_OFFSET_US=22000 (y el resto), cambiar DRC_PLATFORM no cambiaba nada."""
    perfil = set().union(*(_active_keys(p) for p in PROFILES))
    pisa = _active_keys(CONFIG / "drc.conf.example") & perfil
    assert not pisa, f"drc.conf.example anula el perfil con: {sorted(pisa)}"
    vals = drc_ap.load_drc_conf(str(CONFIG / "drc.conf.example"))
    rtw = drc_ap.load_drc_conf(str(_conf_with("usb-rtw89", (CONFIG / "drc.conf.example").read_text()
                                              .replace('DRC_PLATFORM="auto"', ""))))
    assert vals["DRC_PLATFORM"] == "auto" and rtw["TS_OFFSET_US"] == "17000"


def test_los_perfiles_solo_traen_valores_del_adaptador():
    for p in PROFILES:
        assert _active_keys(p) <= {"DRC_MTU", "VSTRM_PAYLOAD_SIZE", "TS_OFFSET_US", "TSF_SAWTOOTH_FILTER", "TSF_RTT_GATE",
                                   "ENABLE_PACING_FQ"}, p.name


def test_el_ejemplo_usa_el_perfil_sin_repetirlo():
    ej = CONFIG / "examples" / "pc-usb-rtw89-steam.conf"
    vals = drc_ap.load_drc_conf(str(ej))
    assert vals.get("DRC_PLATFORM") == "usb-rtw89"
    repetidas = _keys(CONFIG / "platforms" / "usb-rtw89.conf") & {
        k for k in re.findall(r"^([A-Z][A-Z0-9_]+)=", ej.read_text(), re.M)}
    assert not repetidas, f"el ejemplo pisa el perfil con: {sorted(repetidas)}"
    assert vals.get("DRC_MODE") == "separate" and vals.get("DESKTOP_SESSION") == "cage"


def _shell_values(conf: Path, keys, platform_env=""):
    """Lo que ve start_drc.sh: ejecuta su bloque de carga de configuración y vuelca las claves."""
    src = (REPO / "drchost" / "start_drc.sh").read_text()
    bloque = src[src.index("# Cargar configuración central"):src.index("\nIFACE=")]
    dump = "; ".join(f'echo "{k}=${{{k}:-}}"' for k in keys)
    env = dict(os.environ, DRC_CONF_FILE=str(conf))
    env.pop("DRC_PLATFORM", None)
    if platform_env:
        env["DRC_PLATFORM"] = platform_env
    out = subprocess.run(["bash", "-c", f'SCRIPT_DIR="{REPO}/drchost"\n{bloque}\n{dump}'],
                         capture_output=True, text=True, env=env, check=True).stdout
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


@pytest.mark.parametrize("prof", PROFILES, ids=lambda p: p.stem)
def test_python_y_shell_leen_lo_mismo(prof):
    conf = _conf_with(prof.stem, 'TS_OFFSET_US=12345\n')
    keys = sorted(_keys(prof) | {"TS_OFFSET_US", "DRC_PLATFORM"})
    py = drc_ap.load_drc_conf(str(conf))
    sh = _shell_values(conf, keys)
    assert py.get("TS_OFFSET_US") == sh["TS_OFFSET_US"] == "12345"           # drc.conf manda sobre el perfil
    for k in keys:
        assert py.get(k, "") == sh[k], k


def test_ningun_conf_usa_variables_que_python_no_expande():
    for conf in [CONFIG / "drc.conf.example", *PROFILES, *sorted((CONFIG / "examples").glob("*.conf"))]:
        for line in conf.read_text().splitlines():
            if re.match(r"^\s*[A-Z][A-Z0-9_]+=", line) and "WIFI_RESET_HOOK" not in line:
                assert "${" not in line and "$(" not in line, f"{conf.name}: {line}"


def test_un_perfil_que_no_existe_se_avisa(tmp_path, monkeypatch):
    conf = _conf_with("no-existe")
    monkeypatch.setenv("DRC_CONF_FILE", str(conf))
    chk = drc_ctl.platform_check(drc_ap.load_drc_conf(str(conf)))
    assert chk.status == drc_ctl.FAIL and "no-existe" in chk.detail
    assert drc_ctl.platform_check({"DRC_PLATFORM": "usb-rtw89"}).status == drc_ctl.OK
    assert drc_ctl.platform_check({}).status == drc_ctl.OK


def test_conf_explicito_manda_tambien_en_la_interfaz(tmp_path):
    """start_drc.sh --conf X: la interfaz, el perfil y lo que leen los programas en Python salen de X."""
    conf = _conf_with("usb-rtw89", 'WIFI_IFACE="wlconf"\n')
    env = dict(os.environ, DRC_DRY_RUN="1")
    env.pop("DRC_CONF_FILE", None)
    out = subprocess.run(["bash", str(REPO / "drchost" / "start_drc.sh"), "--conf", str(conf)],
                         capture_output=True, text=True, env=env, timeout=60).stdout
    assert "--iface wlconf" in out, out
    assert f"drc.conf: {conf}" in out and "perfil usb-rtw89" in out


def test_install_drc_lleva_todo_lo_que_usa_start_drc():
    """En la Pi, /opt/drc/start_drc.sh carga host_pc.sh (y este llama a drc_audio_router/drc_padbinds), y los
    perfiles se buscan junto al drc.conf (/etc/drc/platforms). Antes solo se copiaban 4 scripts y ningún .conf."""
    env = dict(os.environ, DRC_DRY_RUN="1")
    out = subprocess.run(["bash", str(REPO / "drchost" / "install_drc.sh"), "pi@host"],
                         capture_output=True, text=True, env=env, check=True).stdout
    src = (REPO / "drchost" / "start_drc.sh").read_text() + (REPO / "drchost" / "host_pc.sh").read_text()
    usados = set(re.findall(r'\$\{SCRIPT_DIR\}/([A-Za-z0-9_]+\.(?:sh|py))', src))
    assert {"host_pc.sh", "drc_ap.py"} <= usados
    for f in usados:
        assert f in out, f"install_drc.sh no instala {f}"
    assert "/etc/drc/platforms" in out
    for p in PROFILES:
        assert p.name in out, p.name


def test_drc_wake_toma_interfaz_y_canal_de_drc_conf(monkeypatch):
    import drc_wake
    conf = _conf_with("usb-rtw89", 'WIFI_IFACE="wlx9"\nWIFI_CHANNEL=44\n')
    monkeypatch.setenv("DRC_CONF_FILE", str(conf))
    assert drc_wake._conf_iface() == "wlx9" and drc_wake._conf_channel() == 44


def test_los_servicios_siguen_a_drc_conf():
    """Sin --iface/--canal fijos: salen de WIFI_IFACE y WIFI_CHANNEL de drc.conf."""
    for svc in (REPO / "drchost").glob("*.service"):
        exec_line = next(l for l in svc.read_text().splitlines() if l.startswith("ExecStart="))
        assert "--iface" not in exec_line and "--canal" not in exec_line, f"{svc.name}: {exec_line}"


def _dry_summary(conf_text, platform="usb-rtw89"):
    conf = _conf_with(platform, conf_text)
    env = dict(os.environ, DRC_DRY_RUN="1")
    for k in list(env):
        if k.startswith("DRC_") and k != "DRC_DRY_RUN":
            env.pop(k)
    r = subprocess.run(["bash", str(REPO / "drchost" / "start_drc.sh"), "--conf", str(conf)],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.stdout, r.stderr


def test_bitrate_maximo_con_crf():
    out, _ = _dry_summary('VIDEO_BITRATE_MODE="max"\nVIDEO_BITRATE_KBPS=9300\nDRC_CRF=14\n')
    assert "vídeo max 9300 kbps (CRF 14)" in out, out


def test_bitrate_fijo_ignora_crf():
    out, _ = _dry_summary('VIDEO_BITRATE_MODE="fija"\nVIDEO_BITRATE_KBPS=8000\nDRC_CRF=14\n')
    assert "vídeo fija 8000 kbps" in out and "CRF" not in out.split("vídeo fija")[1].split("·")[0], out


def test_bitrate_modo_desconocido_avisa_y_usa_max():
    out, err = _dry_summary('VIDEO_BITRATE_MODE="cbr"\nVIDEO_BITRATE_KBPS=8000\n')
    assert "VIDEO_BITRATE_MODE" in err and "vídeo max 8000 kbps" in out, (out, err)


def test_drc_vbv_kbps_antiguo_sigue_valiendo():
    """Un drc.conf de antes (solo DRC_VBV_KBPS) da el mismo tope."""
    out, _ = _dry_summary("DRC_VBV_KBPS=9300\nDRC_CRF=14\n")
    assert "vídeo max 9300 kbps (CRF 14)" in out, out


def test_bitrate_una_sola_clave_en_el_ejemplo_y_el_menu():
    ex = _active_keys(CONFIG / "drc.conf.example")
    assert {"VIDEO_BITRATE_MODE", "VIDEO_BITRATE_KBPS"} <= ex and "DRC_VBV_KBPS" not in ex
    assert "DRC_VBV_KBPS" not in drc_ctl.FIELDS_BY_KEY
    f = drc_ctl.FIELDS_BY_KEY["VIDEO_BITRATE_MODE"]
    assert f.kind == "choice" and set(f.choices) == {"max", "fija"}
    assert drc_ctl.FIELDS_BY_KEY["VIDEO_BITRATE_KBPS"].default == "9300"


def test_start_drc_borra_los_ajustes_en_caliente_de_otra_sesion():
    """libdrc relee cada segundo /tmp/drc_ts_offset_us y compañía, y mandan sobre drc.conf y el entorno: un archivo
    que sobra de una prueba anterior cambiaba en silencio la sesión siguiente."""
    src = (REPO / "drchost" / "start_drc.sh").read_text()
    lib = "\n".join(p.read_text() for p in list((REPO / "libdrc" / "src").glob("*.cpp")) +
                    list((REPO / "libdrc" / "include").rglob("*.h")))
    calientes = set(re.findall(r'"(/tmp/drc_[a-z_]+_us|/tmp/drc_kick)"', lib))
    assert "/tmp/drc_ts_offset_us" in calientes
    linea = next(l for l in src.splitlines() if l.startswith("DRC_HOT_FILES="))
    for f in calientes:
        assert f in linea, f


def test_el_perfil_rtw89_es_el_de_la_pi_con_el_mismo_adaptador():
    """En la Pi, con este TP-Link, el mando iba bien con MTU 1756 y paquetes de 1694 B (drop-in wlan1.conf); el
    perfil del PC usaba los 1500/1456 del chip interno."""
    rtw = drc_ap.load_drc_conf(str(_conf_with("usb-rtw89")))
    assert rtw["DRC_MTU"] == "1756" and rtw["VSTRM_PAYLOAD_SIZE"] == "1694" and rtw["TS_OFFSET_US"] == "17000"
    assert int(rtw["VSTRM_PAYLOAD_SIZE"]) + 62 == int(rtw["DRC_MTU"])


def _fake_iface(tmp_path, name, driver):
    """/sys/class/net/<name>/device/driver -> .../drivers/<driver>, como en el sistema."""
    drv = tmp_path / "drivers" / driver
    drv.mkdir(parents=True, exist_ok=True)
    dev = tmp_path / "net" / name / "device"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "driver").symlink_to(drv)
    return tmp_path / "net"


def test_perfil_auto_segun_el_driver_de_la_interfaz(tmp_path):
    net = _fake_iface(tmp_path, "wlx1", "rtw89_8852au_git")
    _fake_iface(tmp_path, "wlan0", "brcmfmac")
    _fake_iface(tmp_path, "wlx2", "mt7921u")
    assert drc_ap.resolve_platform("auto", "wlx1", net) == "usb-rtw89"
    assert drc_ap.resolve_platform("auto", "wlan0", net) == "rpi4-internal"
    assert drc_ap.resolve_platform("auto", "wlx2", net) == "generic"
    assert drc_ap.resolve_platform("auto", "noexiste", net) == "generic"
    assert drc_ap.resolve_platform("usb-rtw89", "wlan0", net) == "usb-rtw89"    # uno explícito no se toca


def test_perfil_auto_en_python_y_en_shell(tmp_path, monkeypatch):
    net = _fake_iface(tmp_path, "wlx1", "rtw89_8852au_git")
    conf = _conf_with("auto", 'WIFI_IFACE="wlx1"\n')
    monkeypatch.setenv("DRC_SYSFS_NET", str(net))
    assert drc_ap.load_drc_conf(str(conf))["VSTRM_PAYLOAD_SIZE"] == "1694"
    env = dict(os.environ, DRC_DRY_RUN="1", DRC_SYSFS_NET=str(net))
    out = subprocess.run(["bash", str(REPO / "drchost" / "start_drc.sh"), "--conf", str(conf)],
                         capture_output=True, text=True, env=env, timeout=60).stdout
    assert "perfil usb-rtw89" in out and "payload=1694" in out, out


def test_sin_drc_conf_se_usa_el_ejemplo(tmp_path):
    """Lo que funciona tiene que ser lo de por defecto: sin drc.conf, start_drc.sh usaba los valores del código
    (QP fijo 32, 1456 B, offset 22000, sin rejilla ni relleno)."""
    import shutil
    shutil.copytree(REPO / "drchost", tmp_path / "drchost", symlinks=True)
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "drc.conf.example", tmp_path / "config")
    shutil.copytree(REPO / "config" / "platforms", tmp_path / "config" / "platforms")
    env = dict(os.environ, DRC_DRY_RUN="1", DRC_CONF_FILE=str(tmp_path / "no-existe.conf"))
    out = subprocess.run(["bash", str(tmp_path / "drchost" / "start_drc.sh")],
                         capture_output=True, text=True, env=env, timeout=60).stdout
    assert "drc.conf.example" in out and "vídeo max 9300 kbps (CRF 14)" in out, out


def test_el_ejemplo_usa_perfil_auto():
    vals = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?', (CONFIG / "drc.conf.example").read_text(), re.M))
    assert vals["DRC_PLATFORM"] == "auto"
