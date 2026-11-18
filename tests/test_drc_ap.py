from pathlib import Path as _P
REPO = _P(__file__).resolve().parent.parent
PKG = REPO / "drchost"
import socket
import struct
import time
from pathlib import Path
import pytest

from drchost.drc_ap import (
    build_dhcp_reply,
    build_wps_cred_blob,
    derive_pairing_mac_and_ssid,
    generate_pairing_configs,
    parse_dhcp_packet,
    DhcpMessageType,
)

def parse_wps_tlvs(data: bytes):
    """Helper to parse top-level WPS TLVs into a dict of type -> list of values."""
    tlvs = {}
    idx = 0
    while idx + 4 <= len(data):
        t, l = struct.unpack(">HH", data[idx:idx+4])
        idx += 4
        val = data[idx:idx+l]
        idx += l
        tlvs.setdefault(t, []).append(val)
    return tlvs


def test_derive_pairing_mac_and_ssid():
    """Verifica la derivación de MAC, SSID y PIN a partir de los 4 símbolos de emparejamiento."""
    # Caso 1: ♠ ♠ ♠ ♠ (0, 0, 0, 0)
    mac, ssid, pin = derive_pairing_mac_and_ssid("d8:3a:dd:12:34:09", (0, 0, 0, 0))
    assert mac == "d8:3a:dd:12:34:00"
    assert ssid == "WiiUd83add12340d83add123400_STA1"
    assert len(ssid) == 32
    assert pin == "00005678"

    # Caso 2: Barista reference test (♦ ♦ ♣ ♦ = 2, 2, 3, 2 -> 0xae)
    mac, ssid, pin = derive_pairing_mac_and_ssid("f8:54:f6:7a:5c:00", (2, 2, 3, 2))
    assert mac == "f8:54:f6:7a:5c:ae"
    assert ssid == "WiiUf854f67a5caf854f67a5cae_STA1"
    assert pin == "22325678"


def test_generate_pairing_and_normal_configs_for_wlan0(tmp_path: Path):
    """Prueba 23: Verifica que generate_pairing_configs produce wiiu_ap_pairing.conf y
    generated_ap_normal.conf usando interface=wlan0 (sin WiiUAP ni mtu 1756) y que el blob
    extra_cred contiene el TLV ATTR_CRED (0x100e) con ATTR_SSID, ATTR_AUTH_TYPE (0x0020),
    ATTR_ENCR_TYPE (0x0008) y ATTR_NETWORK_KEY (64 caracteres hex).
    """
    wlan_mac = "dc:a6:32:01:23:45"
    digits = (0, 0, 0, 0)
    code_mac_clean = "dca632012300"
    psk_hex = "a" * 64
    
    out_dir = tmp_path / "conf"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    pairing_conf_path, normal_conf_path, cred_bin_path = generate_pairing_configs(
        wlan_mac=wlan_mac,
        psk_hex=psk_hex,
        out_dir=out_dir,
        iface="wlan0",
        digits=digits,
    )
    
    assert pairing_conf_path.exists()
    assert normal_conf_path.exists()
    assert cred_bin_path.exists()
    
    # 1. Verificar wiiu_ap_pairing.conf
    pairing_text = pairing_conf_path.read_text()
    assert "interface=wlan0" in pairing_text
    assert "WiiUAP" not in pairing_text
    assert "1756" not in pairing_text
    assert "channel=36" in pairing_text
    expected_pairing_ssid = f"WiiU{code_mac_clean[:11]}{code_mac_clean}_STA1"
    assert f"ssid={expected_pairing_ssid}" in pairing_text
    assert f"device_name=WiiU{code_mac_clean}" in pairing_text
    assert "device_type=6-a4c0e1f4-1" in pairing_text
    assert "vendor_elements=dd0800904c0700513206dd09001018020100040000" in pairing_text
    assert "skip_cred_build=1" in pairing_text
    assert "wps_cred_processing=1" in pairing_text
    assert f"extra_cred={cred_bin_path}" in pairing_text
    
    # 2. Verificar generated_ap_normal.conf
    normal_text = normal_conf_path.read_text()
    assert "interface=wlan0" in normal_text
    assert "WiiUAP" not in normal_text
    assert f"ssid=WiiU{code_mac_clean}" in normal_text
    assert f"wpa_psk={psk_hex}" in normal_text
    
    # 3. Verificar el blob extra_cred
    blob = cred_bin_path.read_bytes()
    top_tlvs = parse_wps_tlvs(blob)
    
    assert 0x100e in top_tlvs
    cred_content = top_tlvs[0x100e][0]
    
    inner_tlvs = parse_wps_tlvs(cred_content)
    assert 0x1045 in inner_tlvs
    assert inner_tlvs[0x1045][0].decode("ascii") == f"WiiU{code_mac_clean}"
    
    assert 0x1003 in inner_tlvs
    assert struct.unpack(">H", inner_tlvs[0x1003][0])[0] == 0x0020
    
    assert 0x100f in inner_tlvs
    assert struct.unpack(">H", inner_tlvs[0x100f][0])[0] == 0x0008
    
    assert 0x1027 in inner_tlvs
    assert len(inner_tlvs[0x1027][0]) == 64
    assert inner_tlvs[0x1027][0].decode("ascii") == psk_hex


import os as _os
TARGET_HOST = _os.environ.get("DRC_TEST_HOST", "")   # usuario@host de una Raspberry Pi para las pruebas de hardware


@pytest.mark.usefixtures("drc_service_paused")
def test_wps_pairing_ap_starts_on_rpi4_wlan0():
    """Prueba 24: ejecuta en la Raspberry Pi 4B (<ip-de-la-pi>) el arranque del AP de emparejamiento
    sobre wlan0 sin perder la sesión SSH en eth0, registra el PIN WPS 00005678 en
    /var/run/hostapd/wlan0 verificando respuesta 00005678, y comprueba que wlan0 emite en 5 GHz con
    /sys/class/net/wlan0/tsf activo.
    """
    import subprocess
    drc_ap_local = PKG / "drc_ap.py"
    assert drc_ap_local.exists()

    scp_res = subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(drc_ap_local), f"{TARGET_HOST}:~/drc_ap.py"],
        capture_output=True,
        text=True,
    )
    assert scp_res.returncode == 0, f"Error copiando drc_ap.py a RPi4:\n{scp_res.stderr}"

    remote_cmd = (
        "sudo python3 ~/drc_ap.py --test-ap --symbols 0,0,0,0 && "
        "test -r /sys/class/net/wlan0/tsf && "
        "ip -br link show eth0"
    )
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, remote_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"Error ejecutando --test-ap en RPi4:\n{res.stderr}\n{res.stdout}"
    assert "SUCCESS: 00005678" in res.stdout
    assert "UP" in res.stdout


def test_dhcp_responder_assigns_192_168_1_11_to_gamepad_mac():
    """Prueba 26: prueba que un DHCPDISCOVER y DHCPREQUEST con la MAC del GamePad reciben
    DHCPOFFER y DHCPACK con IP 192.168.1.11 y servidor 192.168.1.10, ignorando otras MACs.
    """
    gamepad_mac = "aa:bb:cc:dd:ee:01"
    other_mac = "11:22:33:44:55:66"

    # Construir paquete DHCPDISCOVER de prueba
    def build_test_dhcp_discover(client_mac: str, xid: int = 0x12345678) -> bytes:
        mac_bytes = bytes.fromhex(client_mac.replace(":", ""))
        pkt = bytearray(236)
        pkt[0] = 1  # op: BOOTREQUEST
        pkt[1] = 1  # htype: Ethernet
        pkt[2] = 6  # hlen: 6
        struct.pack_into(">I", pkt, 4, xid)
        pkt[28:34] = mac_bytes
        # Magic cookie
        pkt += struct.pack(">I", 0x63825363)
        # Option 53: DHCPDISCOVER (1)
        pkt += bytes([53, 1, 1])
        # End
        pkt += bytes([255])
        return bytes(pkt)

    # 1. DISCOVER de la MAC autorizada del GamePad -> OFFER
    disc_data = build_test_dhcp_discover(gamepad_mac)
    parsed = parse_dhcp_packet(disc_data)
    assert parsed is not None
    assert parsed["msg_type"] == DhcpMessageType.Discover
    assert parsed["chaddr"] == gamepad_mac

    reply = build_dhcp_reply(
        parsed,
        response_type=DhcpMessageType.Offer,
        allowed_mac=gamepad_mac,
        ip_console="192.168.1.10",
        ip_gamepad="192.168.1.11",
    )
    assert reply is not None
    # Verificar yiaddr = 192.168.1.11 (offset 16 en header DHCP)
    yiaddr_str = socket.inet_ntoa(reply[16:20])
    assert yiaddr_str == "192.168.1.11"
    siaddr_str = socket.inet_ntoa(reply[20:24])
    assert siaddr_str == "192.168.1.10"

    # 2. DISCOVER de otra MAC no autorizada -> ignorado (None)
    other_disc = build_test_dhcp_discover(other_mac)
    parsed_other = parse_dhcp_packet(other_disc)
    reply_other = build_dhcp_reply(
        parsed_other,
        response_type=DhcpMessageType.Offer,
        allowed_mac=gamepad_mac,
    )
    assert reply_other is None


def test_dhcp_reassigns_same_ip_on_reconnect():
    """Prueba 57: si el GamePad se apaga y se enciende, su segundo DISCOVER/REQUEST (misma MAC,
    otro xid) recibe otra vez OFFER y ACK con 192.168.1.11; el servidor no guarda estado que
    lo impida. Una MAC ajena sigue ignorada entre ambas conexiones."""
    gamepad_mac = "aa:bb:cc:dd:ee:01"

    def dhcp_request(client_mac: str, msg_type: int, xid: int) -> bytes:
        pkt = bytearray(236)
        pkt[0], pkt[1], pkt[2] = 1, 1, 6
        struct.pack_into(">I", pkt, 4, xid)
        pkt[28:34] = bytes.fromhex(client_mac.replace(":", ""))
        pkt += struct.pack(">I", 0x63825363) + bytes([53, 1, msg_type, 255])
        return bytes(pkt)

    def answer(mac: str, msg_type: int, xid: int, resp: DhcpMessageType):
        parsed = parse_dhcp_packet(dhcp_request(mac, msg_type, xid))
        return build_dhcp_reply(parsed, response_type=resp, allowed_mac=gamepad_mac)

    for connection, xid in enumerate((0x1111, 0x2222), start=1):
        offer = answer(gamepad_mac, 1, xid, DhcpMessageType.Offer)
        ack = answer(gamepad_mac, 3, xid, DhcpMessageType.Ack)
        for reply, expected in ((offer, DhcpMessageType.Offer), (ack, DhcpMessageType.Ack)):
            assert reply is not None, f"conexión {connection}: sin respuesta"
            assert socket.inet_ntoa(reply[16:20]) == "192.168.1.11"
            assert struct.unpack(">I", reply[4:8])[0] == xid
            assert bytes([53, 1, expected.value]) in reply[240:]
        assert answer("11:22:33:44:55:66", 1, xid, DhcpMessageType.Offer) is None


@pytest.mark.usefixtures("drc_service_paused")
def test_normal_ap_network_routing_coexists_with_eth0_on_rpi4():
    """Prueba 25: en la Raspberry Pi 4B (<ip-de-la-pi>), levanta start_normal_ap en modo prueba
    y comprueba por SSH que hostapd queda activo en 5 GHz, ip route get 192.168.1.11 enruta por
    wlan0 con origen 192.168.1.10, eth0 conserva su conexión en <ip-de-la-pi>/24 y
    /sys/class/net/wlan0/tsf devuelve valores u64 crecientes.
    """
    import subprocess
    drc_ap_local = PKG / "drc_ap.py"

    # Copiar drc_ap.py a RPi4
    scp_res = subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(drc_ap_local), f"{TARGET_HOST}:~/drc_ap.py"],
        capture_output=True,
        text=True,
    )
    assert scp_res.returncode == 0

    remote_cmd = "sudo python3 ~/drc_ap.py --test-normal-ap"
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, remote_cmd],
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert res.returncode == 0, f"Error en test-normal-ap en RPi4:\n{res.stderr}\n{res.stdout}"
    assert "SUCCESS: NORMAL_AP_READY" in res.stdout


def test_pairing_persists_and_restores_across_reboot(tmp_path: Path):
    """Prueba 51: la config del AP vivía solo en /tmp/drc_conf y se perdía al reiniciar la Pi.
    - persist_pairing() copia la config normal y la MAC del GamePad al directorio persistente;
    - restore_pairing() la devuelve al directorio de trabajo (y manda sobre una config
      regenerada allí, p. ej. por un test con otros símbolos);
    - sin copia persistente, restore_pairing() devuelve False y no toca nada.
    """
    from drchost.drc_ap import persist_pairing, restore_pairing, PAIRING_FILES

    assert "generated_ap_normal.conf" in PAIRING_FILES
    assert "generated_netboot_info.conf" in PAIRING_FILES

    work = tmp_path / "tmp_drc_conf"
    persist = tmp_path / "var_lib_drc"
    work.mkdir()
    (work / "generated_ap_normal.conf").write_text("ssid=WiiUd83add123400\n")
    (work / "generated_netboot_info.conf").write_text("aa:bb:cc:dd:ee:01\n")
    (work / "hostapd_normal.log").write_text("log\n")

    # Sin copia persistente: nada que restaurar
    assert restore_pairing(work, persist) is False
    assert (work / "generated_ap_normal.conf").read_text() == "ssid=WiiUd83add123400\n"

    persist_pairing(work, persist)
    assert (persist / "generated_ap_normal.conf").read_text() == "ssid=WiiUd83add123400\n"
    assert (persist / "generated_netboot_info.conf").read_text() == "aa:bb:cc:dd:ee:01\n"
    assert not (persist / "hostapd_normal.log").exists()

    # «Reinicio»: /tmp vacío -> se restaura
    for f in work.iterdir():
        f.unlink()
    work.rmdir()
    assert restore_pairing(work, persist) is True
    assert (work / "generated_ap_normal.conf").read_text() == "ssid=WiiUd83add123400\n"
    assert (work / "generated_netboot_info.conf").read_text() == "aa:bb:cc:dd:ee:01\n"

    # Config regenerada en /tmp con otros símbolos: manda la persistente
    (work / "generated_ap_normal.conf").write_text("ssid=WiiUd83add1234ff\n")
    assert restore_pairing(work, persist) is True
    assert (work / "generated_ap_normal.conf").read_text() == "ssid=WiiUd83add123400\n"


def test_monitor_required_for_extended_and_mirror(tmp_path: Path):
    """Prueba 53: la Pi suele ir sin monitor (multi-user.target, HDMI desconectados).
    Los modos extended/mirror necesitan un escritorio en un monitor: sin ninguno conectado,
    check_monitor_for_mode() lo dice claramente en vez de transmitir una pantalla vacía.
    El modo separate no necesita monitor."""
    from drchost.drc_ap import check_monitor_for_mode, connected_outputs

    drm = tmp_path / "drm"
    for name, status in (("card0-HDMI-A-1", "disconnected"), ("card0-HDMI-A-2", "disconnected"),
                         ("card0-Writeback-1", "unknown")):
        (drm / name).mkdir(parents=True)
        (drm / name / "status").write_text(status + "\n")
    (drm / "card0").mkdir()  # sin fichero status: se ignora

    assert connected_outputs(drm) == []
    check_monitor_for_mode("separate", drm)  # no lanza
    for mode in ("extended", "mirror"):
        with pytest.raises(RuntimeError, match="monitor"):
            check_monitor_for_mode(mode, drm)

    (drm / "card0-HDMI-A-1" / "status").write_text("connected\n")
    assert connected_outputs(drm) == ["HDMI-A-1"]
    check_monitor_for_mode("extended", drm)
    check_monitor_for_mode("mirror", drm)


def _dhcp_option(reply: bytes, code: int):
    i = 240
    while i < len(reply) and reply[i] != 255:
        if reply[i] == code:
            return reply[i + 2:i + 2 + reply[i + 1]]
        i += 2 + reply[i + 1]
    return None


def _discover(mac: str) -> dict:
    raw = bytearray(240)
    raw[0], raw[1], raw[2] = 1, 1, 6
    raw[28:34] = bytes.fromhex(mac.replace(":", ""))
    raw[236:240] = struct.pack(">I", 0x63825363)
    return parse_dhcp_packet(bytes(raw) + bytes([53, 1, 1, 255]))


def test_dhcp_mtu_option_follows_drc_mtu(monkeypatch):
    """Prueba 80: la opción 26 (MTU) anuncia 1500 por defecto y DRC_MTU si viene de fuera
    (el GamePad descartaría tramas mayores que el MTU anunciado)."""
    req = _discover("aa:bb:cc:dd:ee:01")
    monkeypatch.delenv("DRC_MTU", raising=False)
    r = build_dhcp_reply(req, response_type=DhcpMessageType.Offer)
    assert struct.unpack(">H", _dhcp_option(r, 26))[0] == 1500
    monkeypatch.setenv("DRC_MTU", "1756")
    r = build_dhcp_reply(req, response_type=DhcpMessageType.Offer)
    assert struct.unpack(">H", _dhcp_option(r, 26))[0] == 1756


def test_prepare_otra_interfaz_no_toca_el_wpa_supplicant_global(monkeypatch):
    """Prueba 84: con el AP en wlan1 (TP-Link), wlan0 queda de acceso por NetworkManager; preparar
    la interfaz no puede matar `wpa_supplicant` ni pararlo (es el de wlan0). Con wlan0 sigue igual."""
    from drchost import drc_ap

    def comandos(iface):
        llamadas = []
        monkeypatch.setattr(drc_ap.subprocess, "run", lambda cmd, **kw: llamadas.append(list(cmd)))
        monkeypatch.setattr(drc_ap.time, "sleep", lambda s: None)
        monkeypatch.setattr(drc_ap.Path, "mkdir", lambda *a, **k: None)
        monkeypatch.setattr(drc_ap.Path, "exists", lambda self: False)
        drc_ap.prepare_wlan0(iface)
        return llamadas

    otra = comandos("wlan1")
    assert ["nmcli", "device", "set", "wlan1", "managed", "no"] in otra
    assert not any("wpa_supplicant" in " ".join(c) for c in otra), otra
    assert any(c[:4] == ["ip", "link", "set", "dev"] and "up" in c for c in otra)
    assert any("wpa_supplicant" in " ".join(c) for c in comandos("wlan0"))


def test_prepare_wlan0_conserva_el_wpa_supplicant_si_se_pide(monkeypatch):
    """Prueba 118: con el AP en wlan0 y la gestión (SSH) en otra interfaz, `DRC_KEEP_SUPPLICANT=1`
    evita matar `wpa_supplicant`, que sirve también a esa otra interfaz."""
    from drchost import drc_ap

    llamadas = []
    monkeypatch.setattr(drc_ap.subprocess, "run", lambda cmd, **kw: llamadas.append(list(cmd)))
    monkeypatch.setattr(drc_ap.time, "sleep", lambda s: None)
    monkeypatch.setattr(drc_ap.Path, "mkdir", lambda *a, **k: None)
    monkeypatch.setattr(drc_ap.Path, "exists", lambda self: False)
    monkeypatch.setenv("DRC_KEEP_SUPPLICANT", "1")
    drc_ap.prepare_wlan0("wlan0")
    assert ["nmcli", "device", "set", "wlan0", "managed", "no"] in llamadas
    assert not any("wpa_supplicant" in " ".join(c) for c in llamadas), llamadas
    assert any(c[:4] == ["ip", "link", "set", "dev"] and "up" in c for c in llamadas)


def test_start_drc_dry_run_iface_configurable():
    """Prueba 85: `DRC_IFACE=wlan1` hace que start_drc.sh levante el AP en esa interfaz y no
    reinicie brcmfmac/SDIO (el chip interno sigue siendo el acceso de gestión)."""
    import os, subprocess
    script = PKG / "start_drc.sh"
    res = subprocess.run([str(script)], capture_output=True, text=True,
                         env=dict(os.environ, DRC_DRY_RUN="1", DRC_IFACE="wlan1"))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "--iface wlan1" in res.stdout, res.stdout
    texto = script.read_text()
    assert 'IFACE="${DRC_IFACE:-wlan0}"' in texto
    assert '"${IFACE}" = "wlan0"' in texto  # el reinicio del chip Wi-Fi solo aplica a wlan0


def test_conf_de_hostapd_toma_la_interfaz_pedida():
    """Prueba 86: la vinculación guardada fija `interface=wlan0`; con el AP en wlan1 hostapd no
    levantaba (sin socket de control). La configuración debe llevar la interfaz pedida."""
    from drchost.drc_ap import set_conf_interface

    conf = "# WiiU\nssid=WiiUd83add123400\ninterface=wlan0\n\ndriver=nl80211\n"
    assert "interface=wlan1\n" in set_conf_interface(conf, "wlan1")
    assert "interface=wlan0" not in set_conf_interface(conf, "wlan1")
    assert set_conf_interface(conf, "wlan0") == conf


def test_conf_hostapd_acepta_opciones_por_entorno():
    """Prueba 94: DRC_HOSTAPD_OPTS ("clave=valor;clave=valor") cambia o añade líneas de la configuración
    de hostapd sin tocar la vinculación guardada. Sirve para probar el modo de radio de la consola
    (que usa RTS/CTS y una sola trama por turno con Block Ack): ieee80211n=0 desactiva HT y con
    él la agregación; rts_threshold=0 pide RTS/CTS en cada trama."""
    from drchost.drc_ap import apply_conf_options

    conf = "ssid=WiiUd83add123400\nieee80211n=1\nwmm_enabled=1\nwpa=2\n"
    out = apply_conf_options(conf, "ieee80211n=0;rts_threshold=0")
    assert "ieee80211n=0\n" in out and "ieee80211n=1" not in out
    assert out.count("rts_threshold=0") == 1 and out.endswith("rts_threshold=0\n")
    assert "ssid=WiiUd83add123400\n" in out and "wpa=2\n" in out
    assert apply_conf_options(out, "ieee80211n=0;rts_threshold=0") == out       # idempotente
    assert apply_conf_options(conf, None) == conf and apply_conf_options(conf, "") == conf
    assert apply_conf_options(conf, "basura;;=x;a=") == conf                    # piezas inválidas: se ignoran


def test_tsf_sysfs_path_busca_en_device_y_en_la_interfaz(tmp_path):
    import drc_ap
    (tmp_path / "wlanX").mkdir()
    assert drc_ap.tsf_sysfs_path("wlanX", tmp_path) is None   # driver sin parchear: no hay TSF
    (tmp_path / "wlanX" / "tsf").write_bytes(b"\0" * 8)
    assert drc_ap.tsf_sysfs_path("wlanX", tmp_path) == tmp_path / "wlanX" / "tsf"
    (tmp_path / "wlanX" / "device").mkdir()
    (tmp_path / "wlanX" / "device" / "tsf").write_bytes(b"\0" * 8)
    assert drc_ap.tsf_sysfs_path("wlanX", tmp_path) == tmp_path / "wlanX" / "device" / "tsf"


def test_firewall_solo_se_abre_si_input_filtra():
    import drc_ap
    assert not drc_ap.firewall_needs_opening("-P INPUT ACCEPT\n")
    assert drc_ap.firewall_needs_opening("-P INPUT DROP\n")
    assert drc_ap.firewall_needs_opening("-P INPUT ACCEPT\n-A INPUT -j ufw-before-input\n")


def test_firewall_cmds_aceptan_al_gamepad_sin_duplicar_y_se_retiran():
    import drc_ap
    regla = ["INPUT", "-i", "wlx", "-s", "192.168.1.11", "-j", "ACCEPT"]
    assert drc_ap.firewall_cmds("wlx", "192.168.1.11", "open") == [["iptables", "-D"] + regla, ["iptables", "-I"] + regla]
    assert drc_ap.firewall_cmds("wlx", "192.168.1.11", "close") == [["iptables", "-D"] + regla]


def test_dhcp_ignora_el_mismo_datagrama_recibido_por_los_dos_sockets():
    import drc_ap
    srv = drc_ap.DHCPServer("wlx")
    assert not srv._is_duplicate(b"discover-1", now=10.0)
    assert srv._is_duplicate(b"discover-1", now=10.001)      # copia por el segundo socket
    assert not srv._is_duplicate(b"request-1", now=10.02)
    assert not srv._is_duplicate(b"discover-1", now=12.5)    # retransmisión real del mando


def test_pick_compositor_usa_el_entorno_del_equipo_y_wiiu_wm_solo_si_se_pide(monkeypatch):
    import drc_ap
    monkeypatch.setattr(drc_ap, "find_wiiu_wm", lambda: "/x/wiiu-wm")
    monkeypatch.setattr(drc_ap.shutil, "which", lambda n: "/usr/bin/" + n if n in ("Hyprland", "labwc") else None)
    assert drc_ap.pick_compositor("auto", "", "hyprland") == "/usr/bin/Hyprland"   # el entorno del equipo
    assert drc_ap.pick_compositor("hyprland", "", "") == "/usr/bin/Hyprland"       # sesión pedida en drc.conf
    assert drc_ap.pick_compositor("wiiu-wm", "", "hyprland") == "/x/wiiu-wm"       # solo si se pide
    assert drc_ap.pick_compositor("auto", "wiiu-wm", "hyprland") == "/x/wiiu-wm"
    assert drc_ap.pick_compositor("auto", "", "") == "/usr/bin/labwc"              # sin entorno conocido
    monkeypatch.setattr(drc_ap.shutil, "which", lambda n: None)
    assert drc_ap.pick_compositor("auto", "", "") is None


def test_pick_compositor_kiosco_usa_cage_aunque_el_equipo_sea_hyprland(monkeypatch):
    import drc_ap
    monkeypatch.setattr(drc_ap.shutil, "which", lambda n: "/usr/bin/" + n if n in ("Hyprland", "cage") else None)
    for sesion in ("cage", "kiosk", "steam"):
        assert drc_ap.pick_compositor(sesion, "", "hyprland") == "/usr/bin/cage"
    assert drc_ap.pick_compositor("auto", "", "hyprland") == "/usr/bin/Hyprland"


def test_load_drc_conf_aplica_el_perfil_de_plataforma_y_drc_conf_manda(tmp_path, monkeypatch):
    """DRC_PLATFORM carga config/platforms/<nombre>.conf como base; lo escrito en drc.conf lo sobrescribe."""
    from drchost.drc_ap import load_drc_conf
    (tmp_path / "platforms").mkdir()
    (tmp_path / "platforms" / "mihw.conf").write_text('WIFI_CHANNEL=40\nTS_OFFSET_US=9000\n')
    conf = tmp_path / "drc.conf"
    conf.write_text('DRC_PLATFORM="mihw"\nTS_OFFSET_US=17000\n')
    monkeypatch.delenv("DRC_PLATFORM", raising=False)
    cfg = load_drc_conf(str(conf))
    assert cfg["WIFI_CHANNEL"] == "40"          # del perfil
    assert cfg["TS_OFFSET_US"] == "17000"       # drc.conf gana


def test_resolve_base_mac_se_genera_una_vez_y_se_conserva(tmp_path):
    from drchost.drc_ap import resolve_base_mac
    a = resolve_base_mac(None, {}, tmp_path)
    assert len(a.split(":")) == 6 and a.endswith(":00") and a.startswith("d8:3a:dd:")
    assert resolve_base_mac(None, {}, tmp_path) == a          # misma en el siguiente arranque
    assert (tmp_path / "base_mac").read_text().strip() == a


def test_resolve_base_mac_prioriza_argumento_y_config(tmp_path):
    from drchost.drc_ap import resolve_base_mac
    assert resolve_base_mac("AA:BB:CC:DD:EE:00", {"WIFI_BASE_MAC": "11:22:33:44:55:00"}, tmp_path) == "aa:bb:cc:dd:ee:00"
    assert resolve_base_mac(None, {"WIFI_BASE_MAC": "11:22:33:44:55:00"}, tmp_path) == "11:22:33:44:55:00"
    assert not (tmp_path / "base_mac").exists()
    assert resolve_base_mac(None, {"WIFI_BASE_OUI": "a4:c0:e1"}, tmp_path).startswith("a4:c0:e1:")


def test_cage_ofrece_la_resolucion_del_mando_antes_de_arrancar_la_app():
    """Los juegos ven la pantalla de cage: debe ser 854x480 (la del mando) antes de que arranque Steam."""
    from drchost.drc_ap import cage_argv
    argv = cage_argv("steam -gamepadui", "wayland-drc", 854, 480)
    assert argv[:3] == ["cage", "--", "sh"]
    script = argv[-1]
    i = script.index("wlr-randr --output HEADLESS-1 --custom-mode 854x480@60Hz")
    assert i < script.index("exec steam -gamepadui")
    assert "1280x720" in script     # si falta wlr-randr queda en el registro de cage
