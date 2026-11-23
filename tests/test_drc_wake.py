"""Pruebas de drc_wake: reconocer la ráfaga con la que el GamePad intenta despertar a su consola (la Pi)."""
import os
import struct
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "drchost"))
import drc_wake as w  # noqa: E402

PI = bytes.fromhex("d83add123400")
PAD = bytes.fromhex("aabbccddee01")
OTRO = bytes.fromhex("021122334455")
RADIOTAP = bytes([0, 0, 8, 0, 0, 0, 0, 0])


def trama_wake(a1=PI, a2=PAD, a3=PI, fc1=0x41, pn=1, largo=71, tid=7, fc0=0x88):
    """QoS data ToDS protegida con cabecera CCMP, como la ráfaga capturada (71 B)."""
    cab = bytes([fc0, fc1, 0x2C, 0x00]) + a1 + a2 + a3 + b"\x10\x00" + bytes([tid, 0])
    ccmp = bytes([pn & 0xFF, (pn >> 8) & 0xFF, 0, 0x20]) + (pn >> 16).to_bytes(4, "little")
    return cab + ccmp + bytes(largo - len(cab) - len(ccmp))


def test_reconoce_la_trama_de_despertar():
    assert w.es_trama_wake(trama_wake(), PI)


def test_con_radiotap_por_delante():
    assert w.dot11_de_radiotap(RADIOTAP + trama_wake()) == trama_wake()
    assert w.dot11_de_radiotap(b"\x00") is None


def test_rechaza_otro_destino_o_tipo():
    assert not w.es_trama_wake(trama_wake(a1=OTRO, a3=OTRO), PI)      # no va a la Pi
    assert not w.es_trama_wake(trama_wake(fc1=0x42), PI)               # FromDS (la consola hacia la Pi)
    assert not w.es_trama_wake(trama_wake(fc1=0x01), PI)               # sin cifrar
    assert not w.es_trama_wake(trama_wake(fc0=0x80), PI)               # beacon
    assert not w.es_trama_wake(trama_wake(largo=300), PI)              # tráfico normal, no el aviso
    assert not w.es_trama_wake(b"\x88\x41", PI)                        # truncada


def test_rechaza_pn_distinto_de_uno():
    # el aviso estrena clave (PN=1); una sesión en curso lleva PN altos
    assert not w.es_trama_wake(trama_wake(pn=5000), PI)


def test_filtro_por_mac_del_mando():
    assert w.es_trama_wake(trama_wake(), PI, mando=PAD)
    assert not w.es_trama_wake(trama_wake(a2=OTRO), PI, mando=PAD)
    assert w.es_trama_wake(trama_wake(a2=OTRO), PI)                    # sin filtro vale cualquier emisor


def test_detector_dispara_con_una_rafaga():
    d = w.DetectorWake(PI, minimas=20, ventana=0.5)
    t = 100.0
    res = [d.ver(t + i * 0.0002, trama_wake()) for i in range(30)]
    assert not any(res[:19]) and res[19] is True


def test_detector_ignora_tramas_sueltas_y_lentas():
    d = w.DetectorWake(PI, minimas=20, ventana=0.5)
    assert not any(d.ver(i * 1.0, trama_wake()) for i in range(50))    # una por segundo: no es ráfaga


def test_detector_ignora_otras_tramas():
    d = w.DetectorWake(PI, minimas=3, ventana=0.5)
    assert not any(d.ver(i * 0.001, trama_wake(largo=100)) for i in range(10))


def test_detector_se_rearma_tras_la_pausa():
    d = w.DetectorWake(PI, minimas=3, ventana=0.5, enfriamiento=2.0)
    assert [d.ver(0.001 * i, trama_wake()) for i in range(3)][-1] is True
    assert d.ver(0.5, trama_wake()) is False                           # dentro del enfriamiento
    assert [d.ver(5.0 + 0.001 * i, trama_wake()) for i in range(3)][-1] is True


def test_mac_de_interfaz(tmp_path):
    f = tmp_path / "address"
    f.write_text("d8:3a:dd:12:34:00\n")
    assert w.mac_de(str(f)) == PI


def test_orden_de_arranque_pone_la_interfaz_en_gestionada_y_arranca_drc():
    ordenes = w.ordenes_arranque("wlan1")
    assert ordenes[0][:3] == ["ip", "link", "set"]
    assert ["iw", "dev", "wlan1", "set", "type", "managed"] in ordenes
    assert ordenes[-1] == ["systemctl", "start", "--no-block", "drc"]


class _Sock:
    """Socket falso: entrega las tramas dadas (con radiotap) y luego corta."""

    def __init__(self, tramas):
        self.tramas = list(tramas)

    def recv(self, n):
        if not self.tramas:
            raise OSError("fin")
        return RADIOTAP + self.tramas.pop(0)


def test_vigilar_arranca_drc_y_lo_dice(capsys):
    ejecutadas = []
    tramas = [trama_wake(largo=100)] * 5 + [trama_wake()] * 25
    t = iter(i * 0.001 for i in range(1000))
    ok = w.vigilar("wlan1", PI, PAD, ejecutar=lambda o, **k: ejecutadas.append(o),
                   ahora=lambda: next(t), canal_socket=_Sock(tramas))
    assert ok is True
    assert ejecutadas == w.ordenes_arranque("wlan1")
    sal = capsys.readouterr().out
    assert "aviso de encendido" in sal and "aa:bb:cc:dd:ee:01" in sal


def test_vigilar_sin_aviso_no_arranca_nada():
    ejecutadas = []
    ok = w.vigilar("wlan1", PI, PAD, ejecutar=lambda o, **k: ejecutadas.append(o),
                   ahora=lambda: 0.0, canal_socket=_Sock([trama_wake(largo=100)] * 30))
    assert ok is False and ejecutadas == []


def test_mando_vinculado_lee_la_mac_guardada(tmp_path):
    f = tmp_path / "generated_netboot_info.conf"
    f.write_text("aa:bb:cc:dd:ee:01\n")
    assert w.mando_vinculado(str(f)) == PAD
    assert w.mando_vinculado(str(tmp_path / "no_existe")) is None
    f.write_text("basura\n")
    assert w.mando_vinculado(str(f)) is None


# ---- unidades systemd: la Pi vigila al arrancar; el mando enciende drc; al parar drc se vuelve a vigilar
def _unidad(nombre):
    return open(os.path.join(str(__import__("pathlib").Path(__file__).resolve().parent.parent / "drchost"), nombre)).read()


def test_unidad_wake_vigila_y_se_excluye_con_drc():
    u = _unidad("drc-wake.service")
    assert "Conflicts=drc.service" in u
    assert "/opt/drc/drc_wake.py" in u and "--mando auto" in u
    assert "Restart=on-failure" in u                      # si falla preparar la interfaz, reintenta
    assert "WantedBy=multi-user.target" in u


def test_drc_al_parar_reactiva_el_vigilante():
    u = _unidad("systemd/drc-wake-al-parar.conf")
    assert "[Service]" in u
    assert "ExecStopPost=-/bin/systemctl start --no-block drc-wake.service" in u


def test_el_instalador_copia_la_unidad_y_el_drop_in():
    s = _unidad("install_drc.sh")
    assert "drc-wake.service" in s and "drc-wake-al-parar.conf" in s
    assert "/etc/systemd/system/drc.service.d/" in s


# ---- el mando a veces no manda el aviso: solo probe requests buscando la red (saltando de canal)
def probe(emisor=PAD, ssid=b"WiiUd83add123400"):
    return (bytes([0x40, 0x00, 0, 0]) + b"\xff" * 6 + emisor + b"\xff" * 6 + b"\x10\x00"
            + bytes([0, len(ssid)]) + ssid)


def test_probe_del_mando_vinculado_dispara_al_instante():
    d = w.DetectorWake(PI, mando=PAD)
    assert d.ver(1.0, probe()) is True


def test_probe_de_otro_dispositivo_no_dispara():
    d = w.DetectorWake(PI, mando=PAD)
    assert d.ver(1.0, probe(emisor=bytes.fromhex("0cb2b7a656 99".replace(" ", "")))) is False


def test_sin_mac_del_mando_el_probe_no_basta():
    d = w.DetectorWake(PI)
    assert d.ver(1.0, probe()) is False


def test_probe_respeta_el_enfriamiento():
    d = w.DetectorWake(PI, mando=PAD, enfriamiento=5.0)
    assert d.ver(1.0, probe()) is True
    assert d.ver(2.0, probe()) is False
    assert d.ver(7.0, probe()) is True


def test_vigilar_salta_de_canal_si_se_le_pide():
    canales = []
    t = iter(i * 0.1 for i in range(10000))
    ok = w.vigilar("wlan1", PI, PAD, ejecutar=lambda o, **k: canales.append(o) if "channel" in o else None,
                   ahora=lambda: next(t), canal_socket=_Sock([trama_wake(largo=100)] * 40 + [probe()]),
                   canales=[36, 40, 44], dwell=0.5)
    assert ok is True
    vistos = [o[-1] for o in canales]
    assert "40" in vistos and "44" in vistos


def test_un_cambio_de_canal_colgado_no_para_el_vigilante():
    import subprocess
    llamadas = []

    def ejecutar(o, **k):
        llamadas.append((o, k))
        if "channel" in o:
            assert k.get("timeout"), "el cambio de canal debe llevar plazo"
            raise subprocess.TimeoutExpired(o, k["timeout"])

    t = iter(i * 0.1 for i in range(10000))
    ok = w.vigilar("wlan1", PI, PAD, ejecutar=ejecutar, ahora=lambda: next(t),
                   canal_socket=_Sock([trama_wake(largo=100)] * 20 + [probe()]), canales=[36, 40], dwell=0.3)
    assert ok is True
    assert llamadas[-1][0] == ["systemctl", "start", "--no-block", "drc"]


def test_con_iw_lento_lee_muchas_tramas_por_canal():
    reloj = [0.0]
    saltos = []

    def ahora():
        reloj[0] += 0.001
        return reloj[0]

    def ejecutar(o, **k):
        if "channel" in o:
            saltos.append(o[-1])
            reloj[0] += 1.0                        # el cambio de canal tarda ~1 s en el TP-Link

    ok = w.vigilar("wlan1", PI, PAD, ejecutar=ejecutar, ahora=ahora,
                   canal_socket=_Sock([trama_wake(largo=100)] * 200 + [probe()]), canales=[36, 40], dwell=0.3)
    assert ok is True
    assert len(saltos) <= 3, f"saltó {len(saltos)} veces para 200 tramas: lee una por canal"


# ---- vigía de ausencia: con drc activo, si el mando desaparece del AP, se para drc (y vuelve el vigilante)
DUMP_CON = "Station aa:bb:cc:dd:ee:01 (on wlan1)\n\tinactive time:\t20 ms\n"


def test_estacion_presente_en_el_volcado_de_iw():
    assert w.estacion_presente(DUMP_CON, PAD)
    assert not w.estacion_presente("", PAD)
    assert not w.estacion_presente("Station aa:bb:cc:dd:ee:02 (on wlan1)\n", PAD)


def test_ausencia_solo_tras_el_plazo_seguido():
    a = w.ContadorAusencia(plazo=30)
    assert a.ver(0, True) is False
    assert a.ver(10, False) is False
    assert a.ver(39, False) is False
    assert a.ver(41, False) is True                     # 31 s sin el mando


def test_ausencia_se_reinicia_si_vuelve():
    a = w.ContadorAusencia(plazo=30)
    a.ver(0, False)
    a.ver(25, True)                                     # volvió
    assert a.ver(50, False) is False
    assert a.ver(79, False) is False
    assert a.ver(81, False) is True                     # 31 s desde el nuevo corte


def test_ausencia_cuenta_desde_el_arranque_si_nunca_llega():
    a = w.ContadorAusencia(plazo=30)
    assert a.ver(0, False) is False
    assert a.ver(31, False) is True


def test_vigia_para_drc_cuando_el_mando_falta():
    ordenes = []
    t = iter(range(0, 1000, 5))
    volcados = iter([DUMP_CON] * 3 + [""] * 20)
    w.vigiar_ausencia("wlan1", PAD, plazo=30, ejecutar=lambda o, **k: ordenes.append(o),
                      leer=lambda: next(volcados), ahora=lambda: next(t), dormir=lambda s: None)
    assert ordenes == [["systemctl", "stop", "--no-block", "drc"]]


def test_unidad_del_vigia_va_atada_a_drc():
    u = _unidad("drc-ausencia.service")
    assert "BindsTo=drc.service" in u and "After=drc.service" in u
    assert "--ausencia" in u and "--iface" not in u
    d = _unidad("systemd/drc-wake-al-parar.conf")
    assert "Wants=drc-ausencia.service" in d
    assert "drc-ausencia.service" in _unidad("install_drc.sh")


def test_unidad_wake_usa_interfaz_auto_y_instalador_habilita_wake():
    u = _unidad("drc-wake.service")
    assert "--iface" not in u
    s = _unidad("install_drc.sh")
    assert "systemctl disable drc" in s
    assert "systemctl enable drc-wake" in s


def test_interfaz_configurada_respeta_dropins_y_drc_conf(tmp_path):
    dd = tmp_path / "drc.service.d"
    dd.mkdir()
    (dd / "wlan1.conf").write_text("[Service]\nEnvironment=DRC_IFACE=wlan1\n")
    assert w.interfaz_configurada(drop_dir=str(dd), conf_path=str(tmp_path / "no.conf")) == "wlan1"
    (dd / "zzz-wlan0.conf").write_text("[Service]\nEnvironment=DRC_IFACE=wlan0\n")
    assert w.interfaz_configurada(drop_dir=str(dd), conf_path=str(tmp_path / "no.conf")) == "wlan0"
    conf = tmp_path / "drc.conf"
    conf.write_text('WIFI_IFACE="wlan0"\n')
    assert w.interfaz_configurada(drop_dir=str(tmp_path / "vacio"), conf_path=str(conf)) == "wlan0"


def test_soporta_monitor_parsea_iw_phy():
    con_mon = "\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n\t\t * monitor\n\tBand 1:\n"
    sin_mon = "\tSupported interface modes:\n\t\t * IBSS\n\t\t * managed\n\t\t * AP\n\tBand 1:\n"
    assert w.soporta_monitor("wlan1", leer_phy=lambda _: "phy2", ejecutar=lambda _: con_mon) is True
    assert w.soporta_monitor("wlan0", leer_phy=lambda _: "phy0", ejecutar=lambda _: sin_mon) is False
    assert w.soporta_monitor("wlan9", leer_phy=lambda _: "", ejecutar=lambda _: con_mon) is False


def test_vigilar_ap_levanta_hostapd_detecta_mando_y_arranca_drc(tmp_path):
    ap_conf = tmp_path / "generated_ap_normal.conf"
    ap_conf.write_text("ssid=WiiUd83add56c500\ninterface=wlan1\nchannel=40\n")

    class _FakeProc:
        def __init__(self):
            self.terminated = False
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    proc = _FakeProc()
    lanzados = []
    ejecutadas = []

    def fake_popen(cmd):
        lanzados.append((cmd, open(cmd[1]).read()))
        return proc

    volcados = iter(["", "", f"Station {PAD.hex(':')} (on wlan0)\n"])
    ok = w.vigilar_ap(
        "wlan0",
        PAD,
        ap_conf=str(ap_conf),
        hostapd_bin="/usr/bin/fake-hostapd",
        ejecutar=lambda o, **k: ejecutadas.append(o),
        lanzar=fake_popen,
        leer=lambda: next(volcados),
        dormir=lambda _: None,
    )
    assert ok is True
    assert proc.terminated is True
    assert lanzados and "interface=wlan0" in lanzados[0][1]
    assert ["ip", "link", "set", "dev", "wlan0", "address", "d8:3a:dd:56:c5:00"] in ejecutadas
    assert ejecutadas[-1] == ["systemctl", "start", "--no-block", "drc"]
