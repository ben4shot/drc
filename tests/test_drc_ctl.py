"""Pruebas de drc_ctl: requisitos dinámicos, interfaces, estado y menú (sin tocar el sistema real)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "drchost"))
import drc_ctl as c  # noqa: E402
c._orig_list = c.list_wifi_ifaces

IW_PHY = """Wiphy phy10
\tSupported interface modes:
\t\t * managed
\t\t * AP
\t\t * monitor
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412.0 MHz [1] (20.0 dBm)
\tBand 2:
\t\tFrequencies:
\t\t\t* 5180.0 MHz [36] (20.0 dBm)
\t\t\t* 5200.0 MHz [40] (20.0 dBm)
\t\t\t* 5260.0 MHz [52] (20.0 dBm) (no IR, radar detection)
\t\t\t* 5280.0 MHz [56] (disabled)
"""
IW_PHY_SIN_AP = IW_PHY.replace("\t\t * AP\n", "")
IW_PHY_SIN_5G = "Wiphy phy0\n\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n\tBand 1:\n\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _estado_aislado(tmp_path, monkeypatch):
    """Ninguna prueba toca ~/.config/drc/ctl.json ni las funciones reales de búsqueda."""
    monkeypatch.setattr(c, "STATE_FILE", tmp_path / "estado.json")
    for nombre in ("find_hostapd", "find_drc_stream", "find_touch2pointer", "paired_gamepad"):
        monkeypatch.setattr(c, nombre, getattr(c, nombre))


class FakeHost(c.Host):
    def __init__(self, tmp, phys, default_dev="wlp3s0", binaries=("hostapd", "wf-recorder", "cage", "steam",
                                                                     "Xwayland", "sudo", "pkexec", "Hyprland"),
                 procs=(), root=False, env=None, regdb=True):
        super().__init__(sys_net=tmp / "net")
        self.regdb = regdb
        self.phys, self.default_dev, self.bins, self.procs, self.root = phys, default_dev, set(binaries), set(procs), root
        self._env = env or {}

    def which(self, name):
        return "/usr/bin/" + name if name in self.bins else None

    def is_root(self):
        return self.root

    def regdb_present(self):
        return self.regdb

    def user(self):
        return "tester"

    def env(self, name, default=""):
        return self._env.get(name, default)

    def run(self, cmd, timeout=5.0):
        cmd = list(cmd)
        if cmd[:3] == ["ip", "route", "show"]:
            return 0, f"default via 10.0.0.1 dev {self.default_dev} proto dhcp\n"
        if cmd[0] == "rfkill":
            return 0, "0: phy0: Wireless LAN\n\tSoft blocked: no\n\tHard blocked: no\n"
        if cmd[:2] == ["iw", "phy"]:
            return 0, self.phys.get(cmd[2], "")
        if cmd[:2] == ["iw", "dev"]:
            return 1, "Not connected.\n"
        if cmd[0] == "pgrep":
            name = cmd[-1]
            return (0 if name in self.procs else 1), ""
        if cmd[:3] == ["ip", "-4", "addr"]:
            return 0, "inet 192.168.1.10/24 scope global\n" if "ap" in self.procs else ""
        if cmd[0] == "ping":
            return (0 if "gamepad" in self.procs else 1), ""
        return 1, ""


def make_net(tmp, names_to_phy):
    for name, phy in names_to_phy.items():
        d = tmp / "net" / name
        (d / "phy80211").mkdir(parents=True)
        (d / "wireless").mkdir()
        (d / "phy80211" / "name").write_text(phy + "\n")
        (d / "address").write_text("aa:bb:cc:dd:ee:0%d\n" % len(name))


def test_parse_symbols():
    assert c.parse_symbols("0,1,2,3") == (0, 1, 2, 3)
    assert c.parse_symbols("0123") == (0, 1, 2, 3)
    assert c.parse_symbols("♠♥♦♣") == (0, 1, 2, 3)
    assert c.parse_symbols("3 3 0 0") == (3, 3, 0, 0)
    for malo in ("012", "01234", "0,1,2,9", "abcd", ""):
        try:
            c.parse_symbols(malo)
        except ValueError:
            continue
        raise AssertionError(malo)
    assert c.show_symbols((0, 1, 2, 3)) == "♠ ♥ ♦ ♣"


def test_parse_iw_phy_canales_utilizables():
    p = c.parse_iw_phy(IW_PHY)
    assert p["ap"] and p["band5"]
    assert p["channels5"] == [36, 40]            # el 52 (no IR) y el 56 (disabled) no sirven como AP
    assert not c.parse_iw_phy(IW_PHY_SIN_AP)["ap"]
    assert not c.parse_iw_phy(IW_PHY_SIN_5G)["band5"]


def test_lista_interfaces_y_recomendada(tmp_path):
    make_net(tmp_path, {"wlp3s0": "phy0", "wlan9": "phy10"})
    (tmp_path / "net/wlan9/tsf").write_text("0\n")
    host = FakeHost(tmp_path, {"phy0": IW_PHY, "phy10": IW_PHY})
    ifs = c.list_wifi_ifaces(host)
    by = {i.name: i for i in ifs}
    assert by["wlp3s0"].default_route and not by["wlp3s0"].suitable      # es la salida a Internet
    assert by["wlan9"].suitable and by["wlan9"].tsf
    assert c.recommend_iface(ifs).name == "wlan9"
    txt = c.format_ifaces(ifs, current="wlp3s0")
    assert "recomendada" in txt and "actual" in txt and "es tu Internet" in txt


def test_check_pair_todo_listo(tmp_path):
    make_net(tmp_path, {"wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    cfg = {"WIFI_CHANNEL": "40"}
    ifs = c.list_wifi_ifaces(host)
    c.find_hostapd = lambda h: "/x/hostapd"
    checks = c.check_pair(host, "wlan9", (0, 0, 0, 0), cfg, ifs)
    assert not c.blocking(checks)
    keys = {k.key: k for k in checks}
    assert "physical" not in keys                      # la instrucción del botón SYNC se da al lanzar
    assert "♠ ♠ ♠ ♠" in keys["symbols"].detail


def test_check_pair_dice_que_falta(tmp_path):
    make_net(tmp_path, {"wlp3s0": "phy0", "wlan8": "phy8"})
    host = FakeHost(tmp_path, {"phy0": IW_PHY, "phy8": IW_PHY_SIN_AP}, binaries=("sudo",))
    c.find_hostapd = lambda h: None
    ifs = c.list_wifi_ifaces(host)
    # la interfaz de Internet, sin hostapd y con símbolos inválidos
    k = {x.key: x for x in c.check_pair(host, "wlp3s0", (0, 0, 0, 9), {"WIFI_CHANNEL": "40"}, ifs)}
    assert k["internet"].status == c.FAIL and "Elige otra" in k["internet"].fix
    assert k["hostapd"].status == c.FAIL and "build_local" in k["hostapd"].fix
    assert k["symbols"].status == c.FAIL
    # una tarjeta sin AP
    k = {x.key: x for x in c.check_pair(host, "wlan8", (0, 0, 0, 0), {"WIFI_CHANNEL": "40"}, ifs)}
    assert k["ap"].status == c.FAIL
    # una interfaz que no existe
    k = {x.key: x for x in c.check_pair(host, "nada0", (0, 0, 0, 0), {}, ifs)}
    assert k["iface"].status == c.FAIL and "wlp3s0" in k["iface"].detail


def test_canal_no_permitido_falla(tmp_path):
    make_net(tmp_path, {"wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    c.find_hostapd = lambda h: "/x/hostapd"
    k = {x.key: x for x in c.check_pair(host, "wlan9", (0, 0, 0, 0), {"DRC_PAIR_CHANNEL": "52"}, c.list_wifi_ifaces(host))}
    assert k["band"].status == c.FAIL and "WIFI_REGDOM" in k["band"].fix


def test_check_start_requiere_emparejado_tsf_y_binarios(tmp_path):
    make_net(tmp_path, {"wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    ifs = c.list_wifi_ifaces(host)
    c.find_hostapd = lambda h: "/x/hostapd"
    c.find_drc_stream = lambda h: None
    c.find_touch2pointer = lambda: None
    cfg = {"WIFI_CHANNEL": "40", "DESKTOP_SESSION": "cage", "GAMEPAD_AUTOSTART": "steam -gamepadui"}
    k = {x.key: x for x in c.check_start(host, "wlan9", cfg, ifs, persist=tmp_path / "vacio")}
    c.paired_gamepad = lambda *a, **kw: None
    assert k["tsf"].status == c.FAIL and "install_rtw89" in k["tsf"].fix
    assert k["drc_stream"].status == c.FAIL
    assert k["compositor"].status == c.OK
    assert not {"steam", "xwayland", "touch"} & set(k)    # lo del setup de la app no se pregunta: solo lo mínimo de DRC


def test_check_start_sin_cage_ni_steam(tmp_path):
    make_net(tmp_path, {"wlan9": "phy10"})
    (tmp_path / "net/wlan9/tsf").write_text("0\n")
    host = FakeHost(tmp_path, {"phy10": IW_PHY}, binaries=("hostapd", "sudo"))
    c.find_hostapd = lambda h: "/x/hostapd"
    c.find_drc_stream = lambda h: "/x/drc_stream"
    cfg = {"WIFI_CHANNEL": "40", "DESKTOP_SESSION": "cage", "GAMEPAD_AUTOSTART": "steam -gamepadui"}
    k = {x.key: x for x in c.check_start(host, "wlan9", cfg, c.list_wifi_ifaces(host))}
    assert k["compositor"].status == c.FAIL and "pacman -S cage" in k["compositor"].fix
    assert k["recorder"].status == c.FAIL


def test_no_se_puede_empezar_si_ya_transmite(tmp_path):
    make_net(tmp_path, {"wlan9": "phy10"})
    (tmp_path / "net/wlan9/tsf").write_text("0\n")
    host = FakeHost(tmp_path, {"phy10": IW_PHY}, procs=("drc_stream",))
    c.find_hostapd = lambda h: "/x/hostapd"
    c.find_drc_stream = lambda h: "/x/drc_stream"
    k = {x.key: x for x in c.check_start(host, "wlan9", {"WIFI_CHANNEL": "40"}, c.list_wifi_ifaces(host))}
    assert k["running"].status == c.FAIL


def test_estado(tmp_path):
    make_net(tmp_path, {"wlan9": "phy10"})
    assert c.status(FakeHost(tmp_path, {}), "wlan9", {})["state"] == "stop"
    assert c.status(FakeHost(tmp_path, {}, procs=("hostapd", "ap")), "wlan9", {})["state"] == "esperando al GamePad"
    st = c.status(FakeHost(tmp_path, {}, procs=("hostapd", "ap", "gamepad")), "wlan9", {})
    assert st["state"] == "GamePad conectado (sin transmitir)"
    st = c.status(FakeHost(tmp_path, {}, procs=("hostapd", "ap", "gamepad", "drc_stream")), "wlan9", {})
    assert st["state"] == "transmitiendo" and "transmitiendo" in c.format_status(st).lower()


def test_comandos_con_privilegios(tmp_path):
    h = FakeHost(tmp_path, {})
    pair = c.pair_command(h, "wlan9", (0, 1, 2, 3))
    assert pair[0] == "sudo" and "--pair" in pair and pair[pair.index("--symbols") + 1] == "0,1,2,3"
    assert pair[pair.index("--iface") + 1] == "wlan9" and "DRC_DESKTOP_USER=tester" in pair
    assert c.pair_command(h, "wlan9", (0, 0, 0, 0), gui=True)[0] == "pkexec"
    assert c.start_command(h, "wlan9", "separate")[-2:] == ["--mode", "separate"]
    assert c.start_command(FakeHost(tmp_path, {}, root=True), "wlan9")[0] == "env"      # root: sin prefijo
    assert c.stop_command(h) == ["sudo", "pkill", "-INT", "-f", "start_drc.sh"]


def test_menu_pair_muestra_requisitos_y_lanza(tmp_path, monkeypatch):
    make_net(tmp_path, {"wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    c.find_hostapd = lambda h: "/x/hostapd"
    monkeypatch.setattr(c, "STATE_FILE", tmp_path / "ctl.json")
    monkeypatch.setattr(c.drc_ap, "load_drc_conf", lambda *a, **k: {"WIFI_IFACE": "wlan9", "WIFI_CHANNEL": "40"})
    monkeypatch.setattr(c, "list_wifi_ifaces", lambda h=None: c.__dict__["_orig_list"](host))
    entradas = iter(["1", "1,1,1,1", "", "7"])       # emparejar, símbolos, confirmar, salir
    salida, lanzado = [], []
    c.menu(host, lambda p="": next(entradas), salida.append, lambda cmd: lanzado.append(cmd) or 0, interactive=False)
    texto = "\n".join(salida)
    assert "Para emparejar hace falta" not in texto and "✓" not in texto        # todo bien: no se muestra la lista
    assert "♥ ♥ ♥ ♥" in texto and "SYNC" in texto
    assert lanzado and "--pair" in lanzado[0] and "1,1,1,1" in lanzado[0]
    assert c.load_state(tmp_path / "ctl.json")["symbols"] == "1,1,1,1"


def test_menu_no_lanza_si_falta_algo(tmp_path, monkeypatch):
    make_net(tmp_path, {"wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    c.find_hostapd = lambda h: None
    monkeypatch.setattr(c, "STATE_FILE", tmp_path / "ctl.json")
    monkeypatch.setattr(c.drc_ap, "load_drc_conf", lambda *a, **k: {"WIFI_IFACE": "wlan9", "WIFI_CHANNEL": "40"})
    entradas = iter(["1", "", "7"])
    salida, lanzado = [], []
    c.menu(host, lambda p="": next(entradas), salida.append, lambda cmd: lanzado.append(cmd) or 0, interactive=False)
    assert not lanzado and "Falta resolver" in "\n".join(salida) and "build_local" in "\n".join(salida)


def test_elegir_interfaz_se_guarda_en_drc_conf(tmp_path, monkeypatch):
    """Una sola fuente: la interfaz elegida va a WIFI_IFACE de drc.conf, que es la que leen también drc_wake,
    drc_wowl, la cámara, padtest y el probador (antes quedaba en ctl.json y solo la veía drc_ctl)."""
    make_net(tmp_path, {"wlp3s0": "phy0", "wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy0": IW_PHY, "phy10": IW_PHY})
    conf = tmp_path / "drc.conf"
    conf.write_text('WIFI_IFACE="wlan0"   # la del AP\n')
    monkeypatch.setenv("DRC_CONF_FILE", str(conf))
    monkeypatch.setattr(c, "STATE_FILE", tmp_path / "ctl.json")
    salida = []
    assert c.choose_iface(host, lambda p="": "1", salida.append) == "wlan9"
    assert c.drc_ap.load_drc_conf()["WIFI_IFACE"] == "wlan9" and "# la del AP" in conf.read_text()
    assert c.current_iface() == "wlan9"
    assert c.main(["iface", "wlp3s0"]) == 0 and c.drc_ap.load_drc_conf()["WIFI_IFACE"] == "wlp3s0"


def test_una_interfaz_vieja_en_ctl_json_no_manda(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "STATE_FILE", tmp_path / "ctl.json")
    c.save_state({"iface": "wlvieja"})
    assert c.current_iface({"WIFI_IFACE": "wlan9"}) == "wlan9"


def test_sin_drc_conf_elegir_interfaz_lo_crea_del_ejemplo(tmp_path, monkeypatch):
    ejemplo = tmp_path / "drc.conf.example"
    ejemplo.write_text('# ejemplo\nWIFI_IFACE="wlan0"\nDRC_MODE="separate"\n')
    destino = tmp_path / "drc.conf"
    monkeypatch.delenv("DRC_CONF_FILE", raising=False)
    monkeypatch.setattr(c, "conf_path", lambda: destino if destino.exists() else None)
    monkeypatch.setattr(c, "DEFAULT_CONF", destino)
    monkeypatch.setattr(c, "EXAMPLE_CONF", ejemplo)
    assert c.set_iface("wlx1") == destino
    txt = destino.read_text()
    assert 'WIFI_IFACE="wlx1"' in txt and 'DRC_MODE="separate"' in txt and txt.startswith("# ejemplo")


def test_check_start_extended_no_pide_sesion_propia(tmp_path):
    make_net(tmp_path, {"wlan9": "phy10"})
    (tmp_path / "net/wlan9/tsf").write_text("0\n")
    host = FakeHost(tmp_path, {"phy10": IW_PHY}, binaries=("hostapd", "sudo", "wf-recorder", "hyprctl"))
    c.find_hostapd = lambda h: "/x/hostapd"
    c.find_drc_stream = lambda h: "/x/drc_stream"
    cfg = {"WIFI_CHANNEL": "40", "DESKTOP_SESSION": "cage", "DRC_MODE": "extended"}
    k = {x.key: x for x in c.check_start(host, "wlan9", cfg, c.list_wifi_ifaces(host))}
    assert "compositor" not in k and k["desktop"].status == c.OK
    k = {x.key: x for x in c.check_start(host, "wlan9", cfg, c.list_wifi_ifaces(host), mode="separate")}
    assert k["compositor"].status == c.FAIL            # separate sí necesita cage (no está en el equipo falso)


CONF_EJEMPLO = """# Comentario
DRC_MODE="extended"          # modo
WIFI_CHANNEL=40
GAMEPAD_AUTOSTART="steam -gamepadui"
"""


def test_set_conf_value_conserva_comentarios_y_comillas(tmp_path):
    f = tmp_path / "drc.conf"
    f.write_text(CONF_EJEMPLO)
    c.set_conf_value("DRC_MODE", "separate", f)
    c.set_conf_value("WIFI_CHANNEL", "44", f)
    c.set_conf_value("NUEVA", 'a "b" $x', f)
    txt = f.read_text()
    assert 'DRC_MODE="separate"          # modo' in txt and 'WIFI_CHANNEL="44"' in txt
    assert txt.startswith("# Comentario") and 'GAMEPAD_AUTOSTART="steam -gamepadui"' in txt
    assert txt.rstrip().endswith('NUEVA="a \\"b\\" \\$x"')
    assert c.drc_ap.load_drc_conf(str(f))["DRC_MODE"] == "separate"          # y drc_ap lo sigue leyendo


def test_validar_ajustes():
    f = c.FIELDS_BY_KEY
    assert c.validate_field(f["DRC_MODE"], " separate ") == "separate"
    assert c.validate_field(f["WIFI_CHANNEL"], "40") == "40"
    si_no = c.Field("X", "x", "g", "bool")
    assert c.validate_field(si_no, "sí") == "1" and c.validate_field(si_no, "no") == "0"
    for campo, malo in (("DRC_MODE", "otro"), ("WIFI_CHANNEL", "999"), ("WIFI_CHANNEL", "x"), (si_no, "quizá"),
                        ("GAMEPAD_AUTOSTART", "a\nb")):
        try:
            c.validate_field(f[campo] if isinstance(campo, str) else campo, malo)
        except ValueError:
            continue
        raise AssertionError((campo, malo))


def test_menu_configuracion_cambia_el_modo(tmp_path, monkeypatch):
    f = tmp_path / "drc.conf"
    f.write_text(CONF_EJEMPLO)
    monkeypatch.setenv("DRC_CONF_FILE", str(f))
    monkeypatch.setattr(c, "conf_path", lambda: f)
    entradas = iter(["6", "1", "1", "", "7"])    # Configuración, Modo de pantalla, 1=separate, volver, salir
    salida = []
    host = FakeHost(tmp_path, {})
    (tmp_path / "net").mkdir(exist_ok=True)
    c.menu(host, lambda p="": next(entradas), salida.append, lambda cmd: 0, interactive=False)
    assert 'DRC_MODE="separate"' in f.read_text()
    assert "Guardado: Modo de pantalla = separate" in "\n".join(salida)


def test_start_command_siempre_lleva_el_modo(tmp_path, monkeypatch):
    monkeypatch.setattr(c.drc_ap, "load_drc_conf", lambda *a, **k: {"DRC_MODE": "extended"})
    h = FakeHost(tmp_path, {})
    cmd = c.start_command(h, "wlan9")
    assert cmd[-2:] == ["--mode", "extended"]            # el de drc.conf, pero explícito
    assert c.start_command(h, "wlan9", "separate")[-2:] == ["--mode", "separate"]


def test_el_modo_extended_avisa(tmp_path):
    assert c.mode_check("separate").status == c.OK
    m = c.mode_check("extended")
    assert m.status == c.WARN and "separate" in m.fix


def test_start_con_todo_bien_no_muestra_estado_ni_pregunta(tmp_path, monkeypatch):
    make_net(tmp_path, {"wlan9": "phy10"})
    (tmp_path / "net/wlan9/tsf").write_text("0\n")
    host = FakeHost(tmp_path, {"phy10": IW_PHY}, binaries=("hostapd", "sudo", "wf-recorder", "cage", "wlr-randr"))
    c.find_hostapd = lambda h: "/x/hostapd"
    c.find_drc_stream = lambda h: "/x/drc_stream"
    c.paired_gamepad = lambda *a, **k: "aa:bb"
    monkeypatch.setattr(c.drc_ap, "load_drc_conf", lambda *a, **k: {"WIFI_IFACE": "wlan9", "WIFI_CHANNEL": "40",
                                                                    "DESKTOP_SESSION": "cage", "DRC_MODE": "separate"})
    salida, lanzado = [], []
    entradas = iter([])                                   # si preguntara algo, next() fallaría
    rc = c.do_start(host, lambda p="": next(entradas), salida.append, lambda cmd: lanzado.append(cmd) or 0)
    assert rc == 0 and lanzado and "✓" not in "\n".join(salida)


def test_start_solo_muestra_lo_que_falla(tmp_path, monkeypatch):
    make_net(tmp_path, {"wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy10": IW_PHY}, binaries=("sudo",))
    c.find_hostapd = lambda h: None
    c.find_drc_stream = lambda h: "/x/drc_stream"
    c.paired_gamepad = lambda *a, **k: "aa:bb"
    monkeypatch.setattr(c.drc_ap, "load_drc_conf", lambda *a, **k: {"WIFI_IFACE": "wlan9", "WIFI_CHANNEL": "40", "DRC_MODE": "separate"})
    salida = []
    assert c.do_start(host, lambda p="": "", salida.append, lambda cmd: 0) == 1
    txt = "\n".join(salida)
    assert "hostapd" in txt and "✗" in txt and "✓" not in txt


def test_tsf_con_la_interfaz_apagada_no_bloquea_el_arranque(tmp_path):
    """El rtw89 parcheado crea /sys/class/net/<if>/tsf al levantar el AP: con la interfaz apagada no existe y eso no
    es un fallo del driver (drc_ap.py lo vuelve a comprobar con el AP en marcha)."""
    make_net(tmp_path, {"wlan9": "phy10"})
    (tmp_path / "net/wlan9/flags").write_text("0x1002\n")          # sin IFF_UP
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    c.find_hostapd = lambda h: "/x/hostapd"
    c.find_drc_stream = lambda h: "/x/drc_stream"
    ifs = c.list_wifi_ifaces(host)
    assert ifs[0].down and not ifs[0].tsf and "sin comprobar" in ifs[0].summary()
    k = {x.key: x for x in c.check_start(host, "wlan9", {"WIFI_CHANNEL": "40"}, ifs)}
    assert k["tsf"].status == c.WARN
    (tmp_path / "net/wlan9/flags").write_text("0x1003\n")          # levantada y sin tsf: ahí sí falta el parche
    ifs = c.list_wifi_ifaces(host)
    k = {x.key: x for x in c.check_start(host, "wlan9", {"WIFI_CHANNEL": "40"}, ifs)}
    assert k["tsf"].status == c.FAIL


def test_cage_sin_wlr_randr_avisa_de_la_resolucion(tmp_path):
    """Sin wlr-randr cage se queda en 1280x720 y los juegos no ofrecen 854x480."""
    host = FakeHost(tmp_path, {}, binaries=("cage", "wf-recorder"))
    chk = {k.key: k for k in c._session_tool_checks(host, {"DESKTOP_SESSION": "cage"})}
    assert chk["resolution"].status == c.WARN and "wlr-randr" in chk["resolution"].fix
    host = FakeHost(tmp_path, {}, binaries=("cage", "wf-recorder", "wlr-randr"))
    chk = {k.key: k for k in c._session_tool_checks(host, {"DESKTOP_SESSION": "cage"})}
    assert chk["resolution"].status == c.OK and "854x480" in chk["resolution"].detail


def test_emparejar_comprueba_el_canal_de_vinculacion_no_el_de_transmision(tmp_path):
    """check_pair mira el canal del AP de vinculación (DRC_PAIR_CHANNEL, 36 por defecto), no WIFI_CHANNEL."""
    make_net(tmp_path, {"wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    c.find_hostapd = lambda h: "/x/hostapd"
    k = {x.key: x for x in c.check_pair(host, "wlan9", (0, 0, 0, 0), {"WIFI_CHANNEL": "40"}, c.list_wifi_ifaces(host))}
    assert k["band"].name.startswith("Canal 36")
    k = {x.key: x for x in c.check_pair(host, "wlan9", (0, 0, 0, 0), {"WIFI_CHANNEL": "40", "DRC_PAIR_CHANNEL": "52"},
                                        c.list_wifi_ifaces(host))}
    assert k["band"].status == c.FAIL and "DRC_PAIR_CHANNEL" in k["band"].fix


def test_sin_wireless_regdb_se_avisa(tmp_path):
    """Sin regulatory.db el dominio se queda en 00 (iw reg set no hace nada) y el 36 no admite AP: se dice qué instalar."""
    make_net(tmp_path, {"wlan9": "phy10"})
    c.find_hostapd = lambda h: "/x/hostapd"
    host = FakeHost(tmp_path, {"phy10": IW_PHY}, regdb=False)
    k = {x.key: x for x in c.check_pair(host, "wlan9", (0, 0, 0, 0), {}, c.list_wifi_ifaces(host))}
    assert k["regdb"].status == c.WARN and "wireless-regdb" in k["regdb"].fix
    k = {x.key: x for x in c.check_start(host, "wlan9", {}, c.list_wifi_ifaces(host))}
    assert k["regdb"].status == c.WARN
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    k = {x.key: x for x in c.check_pair(host, "wlan9", (0, 0, 0, 0), {}, c.list_wifi_ifaces(host))}
    assert k["regdb"].status == c.OK


def test_emparejar_y_transmitir_en_una_sola_orden(tmp_path):
    """Con start_mode, la vinculación sigue sin pausa con la sesión (start_drc.sh) si sale bien (&&), en una sola
    orden con privilegios: una sola contraseña en la GUI, y stop (pkill start_drc.sh) la sigue parando."""
    h = FakeHost(tmp_path, {})
    cmd = c.pair_command(h, "wlan9", (0, 1, 2, 3), start_mode="separate")
    assert cmd[0] == "sudo" and "DRC_DESKTOP_USER=tester" in cmd and cmd[-3:-1] == ["bash", "-c"]
    script = cmd[-1]
    pair, start = script.split(" && exec ")
    assert "drc_ap.py --pair --iface wlan9 --symbols 0,1,2,3" in pair
    assert start.endswith("start_drc.sh --iface wlan9 --mode separate")
    assert c.pair_command(h, "wlan9", (0, 0, 0, 0), gui=True, start_mode="mirror")[0] == "pkexec"


def _menu_pair(tmp_path, monkeypatch, start_checks):
    make_net(tmp_path, {"wlan9": "phy10"})
    host = FakeHost(tmp_path, {"phy10": IW_PHY})
    c.find_hostapd = lambda h: "/x/hostapd"
    monkeypatch.setattr(c, "STATE_FILE", tmp_path / "ctl.json")
    monkeypatch.setattr(c.drc_ap, "load_drc_conf", lambda *a, **k: {"WIFI_IFACE": "wlan9", "DRC_MODE": "separate"})
    monkeypatch.setattr(c, "list_wifi_ifaces", lambda h=None: c.__dict__["_orig_list"](host))
    monkeypatch.setattr(c, "check_start", lambda *a, **k: start_checks)
    entradas = iter(["1", "0,0,0,0", "", "7"])
    salida, lanzado = [], []
    c.menu(host, lambda p="": next(entradas), salida.append, lambda cmd: lanzado.append(cmd) or 0, interactive=False)
    return "\n".join(salida), lanzado


def test_menu_emparejar_arranca_la_transmision_al_terminar(tmp_path, monkeypatch):
    """Si para transmitir solo falta el emparejamiento, el menú empareja y transmite seguido."""
    texto, lanzado = _menu_pair(tmp_path, monkeypatch, [c.Check("paired", "GamePad emparejado", c.FAIL, "no")])
    assert lanzado and "start_drc.sh --iface wlan9 --mode separate" in lanzado[0][-1]
    assert "empezará la transmisión" in texto


def test_menu_emparejar_sin_transmitir_si_falta_otra_cosa(tmp_path, monkeypatch):
    """Si falta algo más para transmitir (p. ej. drc_stream), solo empareja y dice qué falta."""
    texto, lanzado = _menu_pair(tmp_path, monkeypatch, [c.Check("paired", "GamePad emparejado", c.FAIL, "no"),
                                                        c.Check("drc_stream", "drc_stream", c.FAIL, "no", "compila")])
    assert lanzado and "start_drc.sh" not in " ".join(lanzado[0]) and "--pair" in lanzado[0]
    assert "drc_stream" in texto
