#!/usr/bin/env python3
"""DRC Access Point and WPS pairing management for Raspberry Pi 4."""

import enum
import os
import re
import secrets
import shlex
import signal
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ensure system tools in /usr/sbin and /sbin (e.g. iw, hostapd) are available in PATH
for p in ("/usr/sbin", "/sbin"):
    if p not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = f"{p}:{os.environ.get('PATH', '')}"

ATTR_AUTH_TYPE = 0x1003
ATTR_CRED = 0x100e
ATTR_ENCR_TYPE = 0x100f
ATTR_MAC_ADDR = 0x1020
ATTR_NETWORK_INDEX = 0x1026
ATTR_NETWORK_KEY = 0x1027
ATTR_SSID = 0x1045

DEFAULT_CONF_DIR = Path(__file__).parent / "conf"

# Pairing symbols: 0 = Spade (♠), 1 = Heart (♥), 2 = Diamond (♦), 3 = Club (♣)
SYMBOLS = ["♠", "♥", "♦", "♣"]

DHCP_MAGIC_COOKIE = 0x63825363
DHCP_HEADER_SIZE = 236


class DhcpMessageType(enum.Enum):
    Discover = 1
    Offer = 2
    Request = 3
    Decline = 4
    Ack = 5
    Nak = 6
    Release = 7
    Inform = 8


def _conf_value(raw: str) -> str:
    """Valor de una línea KEY=valor de drc.conf como lo lee el shell: con comillas hasta la de cierre, sin
    ellas hasta un « #» (comentario de línea)."""
    v = raw.strip()
    if v[:1] in ('"', "'"):
        end = v.find(v[0], 1)
        return v[1:end] if end != -1 else v[1:]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


# DRC_PLATFORM="auto": perfil según el driver de la interfaz del GamePad (lo mismo hace start_drc.sh).
PLATFORM_BY_DRIVER = (("rtw89", "usb-rtw89"), ("brcmfmac", "rpi4-internal"))


def resolve_platform(plat: str, iface: str, sys_net: Optional[Path] = None) -> str:
    """«auto» → usb-rtw89 (rtw89*), rpi4-internal (brcmfmac) o generic; cualquier otro nombre se deja igual."""
    if (plat or "").strip() != "auto":
        return plat
    sys_net = Path(os.environ.get("DRC_SYSFS_NET", "/sys/class/net")) if sys_net is None else sys_net
    try:
        driver = os.path.basename(os.path.realpath(sys_net / iface / "device" / "driver"))
    except OSError:
        driver = ""
    for prefix, name in PLATFORM_BY_DRIVER:
        if driver.startswith(prefix):
            return name
    return "generic"


def load_drc_conf(conf_path: Optional[str] = None) -> dict:
    """Carga configuración clave=valor desde drc.conf si existe."""
    candidates = []
    if conf_path:
        candidates.append(Path(conf_path))
    if os.environ.get("DRC_CONF_FILE"):
        candidates.append(Path(os.environ["DRC_CONF_FILE"]))
    candidates.extend([
        Path("/etc/drc/drc.conf"),
        Path(__file__).resolve().parent.parent / "config" / "drc.conf",
        Path(__file__).resolve().parent / "drc.conf",
        # Sin drc.conf propio, el ejemplo: es la configuración que funciona, no los valores del código.
        Path(__file__).resolve().parent.parent / "config" / "drc.conf.example",
    ])
    conf = {}
    for p in candidates:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            conf[k] = _conf_value(v)
                # Perfil de plataforma: valores base del hardware (config/platforms/<DRC_PLATFORM>.conf); drc.conf manda.
                plat = os.environ.get("DRC_PLATFORM") or conf.get("DRC_PLATFORM", "")
                plat = resolve_platform(plat, os.environ.get("DRC_IFACE") or conf.get("WIFI_IFACE", "wlan0"))
                if plat and re.fullmatch(r"[A-Za-z0-9_-]+", plat):
                    for pd in (p.parent / "platforms", Path(__file__).resolve().parent.parent / "config" / "platforms"):
                        pf = pd / f"{plat}.conf"
                        if pf.is_file():
                            base = {}
                            for line in pf.read_text(encoding="utf-8").splitlines():
                                line = line.strip()
                                if line and not line.startswith("#") and "=" in line:
                                    k, v = line.split("=", 1)
                                    base[k.strip()] = _conf_value(v)
                            base.update(conf)
                            conf = base
                            break
                break
            except Exception:
                pass
    return conf


def parse_dhcp_packet(data: bytes) -> Optional[dict]:
    """Parse DHCP/BOOTP packet and return header fields and message type."""
    if len(data) < DHCP_HEADER_SIZE + 4:
        return None
    op, htype, hlen, hops, xid, secs, flags = struct.unpack(">BBBBIHH", data[:12])
    if op != 1 or htype != 1 or hlen != 6:
        return None

    cookie = struct.unpack(">I", data[236:240])[0]
    if cookie != DHCP_MAGIC_COOKIE:
        return None

    chaddr_raw = data[28:44]
    client_mac = ":".join(f"{b:02x}" for b in chaddr_raw[:6])

    # Parse options
    msg_type = None
    idx = 240
    while idx < len(data):
        code = data[idx]
        idx += 1
        if code == 0:
            continue
        if code == 255 or idx >= len(data):
            break
        opt_len = data[idx]
        idx += 1
        if idx + opt_len > len(data):
            break
        opt_val = data[idx : idx + opt_len]
        idx += opt_len
        if code == 53 and opt_len == 1:
            try:
                msg_type = DhcpMessageType(opt_val[0])
            except ValueError:
                pass

    return {
        "op": op,
        "htype": htype,
        "hlen": hlen,
        "hops": hops,
        "xid": xid,
        "secs": secs,
        "flags": flags,
        "chaddr": client_mac,
        "raw_chaddr": chaddr_raw,
        "msg_type": msg_type,
    }


def drc_mtu() -> int:
    """MTU del enlace DRC: 1500 salvo DRC_MTU numérico entre 576 y 1756."""
    try:
        v = int(os.environ.get("DRC_MTU", ""))
    except ValueError:
        return 1500
    return v if 576 <= v <= 1756 else 1500


def build_dhcp_reply(
    request: dict,
    response_type: DhcpMessageType,
    allowed_mac: Optional[str] = None,
    ip_console: str = "192.168.1.10",
    ip_gamepad: str = "192.168.1.11",
    lease_seconds: int = 3600,
) -> Optional[bytes]:
    """Build binary DHCP reply (DHCPOFFER or DHCPACK) for allowed GamePad MAC."""
    if allowed_mac and request["chaddr"].lower() != allowed_mac.lower():
        return None

    reply = bytearray(DHCP_HEADER_SIZE)
    reply[0] = 2  # BOOTREPLY
    reply[1] = request["htype"]
    reply[2] = request["hlen"]
    struct.pack_into(">I", reply, 4, request["xid"])
    struct.pack_into(">H", reply, 8, request["secs"])
    struct.pack_into(">H", reply, 10, request["flags"])
    reply[16:20] = socket.inet_aton(ip_gamepad)
    reply[20:24] = socket.inet_aton(ip_console)
    reply[28:44] = request["raw_chaddr"]

    reply += struct.pack(">I", DHCP_MAGIC_COOKIE)

    options = bytearray()
    # Option 53: Message Type
    options += bytes([53, 1, response_type.value])
    # Option 54: Server Identifier
    options += bytes([54, 4]) + socket.inet_aton(ip_console)
    # Option 51: Lease Time
    options += bytes([51, 4]) + struct.pack(">I", lease_seconds)
    # Option 1: Subnet Mask (255.255.255.248 for /29)
    options += bytes([1, 4, 255, 255, 255, 248])
    # Option 3: Router
    options += bytes([3, 4]) + socket.inet_aton(ip_console)
    # Option 28: Broadcast Address (192.168.1.15)
    options += bytes([28, 4, 192, 168, 1, 15])
    # Option 26: Interface MTU (1500 por defecto; DRC_MTU para probar 1756 como la consola)
    options += bytes([26, 2]) + struct.pack(">H", drc_mtu())
    # Option 255: End
    options += bytes([255])

    reply += options
    return bytes(reply)


class DHCPServer:
    """Lightweight standalone DHCP server restricted to DRC GamePad on wlan0."""

    def __init__(
        self,
        iface: str = "wlan0",
        allowed_mac: Optional[str] = None,
        ip_console: str = "192.168.1.10",
        ip_gamepad: str = "192.168.1.11",
    ):
        self.iface = iface
        self.allowed_mac = allowed_mac
        self.ip_console = ip_console
        self.ip_gamepad = ip_gamepad
        self.running = False
        self.sock: Optional[socket.socket] = None
        self.raw_sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None
        self.lease_granted = False
        self._last_key = None
        self._last_time = 0.0

    def start(self):
        # 1. Socket UDP estándar en 0.0.0.0:67
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        try:
            SO_BINDTODEVICE = 25
            self.sock.setsockopt(
                socket.SOL_SOCKET,
                SO_BINDTODEVICE,
                self.iface.encode("ascii"),
            )
        except Exception as e:
            print(f"[DHCP] Advertencia SO_BINDTODEVICE en {self.iface}: {e}", flush=True)

        self.sock.bind(("", 67))
        self.sock.settimeout(0.5)

        # 2. Socket raw AF_PACKET (inmune a rp_filter/iptables en Linux)
        try:
            ETH_P_IP = 0x0800
            self.raw_sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_IP))
            self.raw_sock.bind((self.iface, 0))
            self.raw_sock.settimeout(0.5)
        except Exception:
            self.raw_sock = None

        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _is_duplicate(self, data: bytes, now: Optional[float] = None) -> bool:
        """El mismo datagrama llega por los dos sockets (UDP y AF_PACKET) cuando el cortafuegos no lo
        descarta. Contestarlo dos veces deja al GamePad en un bucle de Discover/Request (~37 ciclos/s)."""
        now = time.monotonic() if now is None else now
        key = hash(bytes(data))
        dup = key == self._last_key and now - self._last_time < 0.5
        self._last_key, self._last_time = key, now
        return dup

    def _process_packet(self, data: bytes):
        if self._is_duplicate(data):
            return
        parsed = parse_dhcp_packet(data)
        if not parsed or not parsed.get("msg_type"):
            return

        msg_type = parsed["msg_type"]
        chaddr = parsed.get("chaddr", "desconocido")
        print(f"[DHCP] Solicitud recibida: {msg_type.name} desde MAC {chaddr}", flush=True)

        resp_type = None
        if msg_type == DhcpMessageType.Discover:
            resp_type = DhcpMessageType.Offer
        elif msg_type == DhcpMessageType.Request:
            resp_type = DhcpMessageType.Ack

        if resp_type:
            reply = build_dhcp_reply(
                parsed,
                response_type=resp_type,
                allowed_mac=self.allowed_mac,
                ip_console=self.ip_console,
                ip_gamepad=self.ip_gamepad,
            )
            if reply:
                # Una sola respuesta, por difusión (el socket está atado a la interfaz del AP)
                try:
                    self.sock.sendto(reply, ("255.255.255.255", 68))
                except Exception as e:
                    print(f"[DHCP] No se pudo enviar la respuesta: {e}", flush=True)
                print(f"[DHCP] Sent {resp_type.name} to {parsed['chaddr']} -> {self.ip_gamepad}", flush=True)
                if resp_type == DhcpMessageType.Ack:
                    self.lease_granted = True
            else:
                print(f"[DHCP] MAC {chaddr} rechazada (esperada: {self.allowed_mac})", flush=True)

    def _run_loop(self):
        import select
        while self.running:
            socks = [s for s in (self.sock, self.raw_sock) if s is not None]
            if not socks:
                break
            try:
                r, _, _ = select.select(socks, [], [], 0.5)
            except Exception:
                continue

            for s in r:
                try:
                    data, addr = s.recvfrom(2048)
                except (socket.timeout, OSError):
                    continue

                if s == self.raw_sock:
                    if len(data) >= 28 and data[9] == 17:
                        ihl = (data[0] & 0x0f) * 4
                        if len(data) >= ihl + 8:
                            dport = struct.unpack(">H", data[ihl+2:ihl+4])[0]
                            if dport == 67:
                                self._process_packet(data[ihl+8:])
                else:
                    self._process_packet(data)

    def stop(self):
        self.running = False
        for s in (self.sock, self.raw_sock):
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)


def pairing_channel(cfg: Dict[str, str]) -> int:
    """Canal del AP de vinculación: 36, el de la consola y el que usaba la Pi (con él vinculaba bien). Va aparte
    de WIFI_CHANNEL (el de la transmisión): el mando guarda la red y luego la busca en cualquier canal.
    DRC_PAIR_CHANNEL lo cambia."""
    return int((cfg.get("DRC_PAIR_CHANNEL") or "").strip() or 36)


def gamepad_wants_normal_network(line: str, normal_ssid: str) -> bool:
    """¿La línea de hostapd muestra al mando buscando su red normal (ya vinculado) en vez de la de vinculación?
    Solo cuentan sus tramas: un probe con ese SSID (en el hexdump de la trama) o un Data/PS-poll. El SSID en texto
    no sirve: hostapd imprime su propio Device Name WPS, que es justo WiiU<mac>."""
    if "Data/PS-poll" in line:
        return True
    hx = " ".join("%02x" % b for b in normal_ssid.encode("ascii"))
    return "MLME event frame" in line and hx in line


def hostapd_failure_message(code: int, out: str, channel_key: str) -> str:
    """Error de hostapd al arrancar. Si es porque el canal no admite AP (dominio regulatorio «00» sin wireless-regdb,
    o un canal DFS/no permitido) lo dice claro; si no, devuelve el volcado."""
    m = re.search(r"Configured channel \((\d+)\) not found", out)
    if m:
        ch = m.group(1)
        return (f"hostapd no puede crear el punto de acceso en el canal {ch}: el dominio regulatorio no lo permite en modo AP.\n"
                f"  - Comprueba «iw reg get»: si dice «country 00», falta wireless-regdb (Arch: sudo pacman -S wireless-regdb)\n"
                f"    y WIFI_REGDOM no se aplica.\n"
                f"  - O elige otro canal con {channel_key} en config/drc.conf (los permitidos: «iw phy» sin «No IR»).")
    return f"hostapd terminated early with code {code}:\n{out}"


def derive_pairing_mac_and_ssid(
    base_mac: str,
    digits: Tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Tuple[str, str, str]:
    """Derive the AP MAC, pairing SSID and WPS PIN from the 4 pairing symbols."""
    clean_mac = base_mac.lower().replace(":", "").replace("-", "")
    if len(clean_mac) != 12:
        raise ValueError(f"Invalid MAC address: {base_mac}")

    mac_bytes = bytearray.fromhex(clean_mac)
    d0, d1, d2, d3 = digits
    pin_byte = ((d0 & 0x03) << 6) | ((d1 & 0x03) << 4) | ((d2 & 0x03) << 2) | (d3 & 0x03)
    mac_bytes[5] = pin_byte

    code_mac_hex = mac_bytes.hex()
    code_mac = ":".join(f"{b:02x}" for b in mac_bytes)
    pairing_ssid = f"WiiU{code_mac_hex[:11]}{code_mac_hex}_STA1"
    wps_pin = f"{d0}{d1}{d2}{d3}5678"

    return code_mac, pairing_ssid, wps_pin


def build_tlv(attr_type: int, value: bytes) -> bytes:
    """Pack attribute type, length (16-bit big endian) and value."""
    return struct.pack(">HH", attr_type, len(value)) + value


def build_wps_cred_blob(ssid: str, psk_hex: str, bssid_mac: Optional[str] = None) -> bytes:
    """Build binary WPS Credential attribute (ATTR_CRED) containing network info."""
    inner = bytearray()
    inner += build_tlv(ATTR_NETWORK_INDEX, bytes([1]))
    inner += build_tlv(ATTR_SSID, ssid.encode("ascii"))
    inner += build_tlv(ATTR_AUTH_TYPE, struct.pack(">H", 0x0020))
    inner += build_tlv(ATTR_ENCR_TYPE, struct.pack(">H", 0x0008))
    inner += build_tlv(ATTR_NETWORK_KEY, psk_hex.encode("ascii"))
    if bssid_mac:
        mac_bytes = bytes.fromhex(bssid_mac.replace(":", ""))
        inner += build_tlv(ATTR_MAC_ADDR, mac_bytes)

    return build_tlv(ATTR_CRED, bytes(inner))


def get_ctrl_interface_group() -> str:
    """Devuelve el grupo administrativo para ctrl_interface_group de hostapd."""
    cfg = load_drc_conf()
    grp_name = cfg.get("HOSTAPD_CTRL_GROUP", "").strip()
    if grp_name:
        return grp_name
    try:
        import grp
        for g in ("wheel", "sudo", "admin", "root"):
            try:
                grp.getgrnam(g)
                return g
            except KeyError:
                continue
    except Exception:
        pass
    return "0"


def generate_pairing_configs(
    wlan_mac: str,
    psk_hex: str,
    out_dir: Path,
    iface: str = "wlan0",
    channel: int = 36,
    digits: Tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Tuple[Path, Path, Path]:
    """Generate hostapd pairing and normal configurations along with the extra_cred binary blob."""
    out_dir.mkdir(parents=True, exist_ok=True)
    code_mac, pairing_ssid, _ = derive_pairing_mac_and_ssid(wlan_mac, digits)
    code_mac_clean = code_mac.lower().replace(":", "")

    ssid_normal = f"WiiU{code_mac_clean}"
    device_name = f"WiiU{code_mac_clean}"
    ctrl_grp = get_ctrl_interface_group()
    cfg = load_drc_conf()

    # 1. Credential blob
    cred_blob = build_wps_cred_blob(ssid=ssid_normal, psk_hex=psk_hex, bssid_mac=code_mac)
    cred_bin_path = out_dir / "wiiu_ap.cred"
    cred_bin_path.write_bytes(cred_blob)

    # 2. Pairing configuration
    pairing_conf = f"""# WiiU Pairing Mode AP
interface={iface}
driver=nl80211
logger_stdout=-1
logger_stdout_level=0
ctrl_interface=/var/run/hostapd
ctrl_interface_group={ctrl_grp}
hw_mode=a
channel={channel}
beacon_int=100
dtim_period=3
macaddr_acl=0
auth_algs=3
wmm_enabled=1
uapsd_advertisement_enabled=1

wmm_ac_be_acm=0
wmm_ac_be_aifs=2
wmm_ac_be_cwmin=4
wmm_ac_be_cwmax=5
wmm_ac_be_txop_limit=47

wmm_ac_bk_acm=0
wmm_ac_bk_aifs=7
wmm_ac_bk_cwmin=4
wmm_ac_bk_cwmax=10
wmm_ac_bk_txop_limit=0

wmm_ac_vi_acm=0
wmm_ac_vi_aifs=3
wmm_ac_vi_cwmin=4
wmm_ac_vi_cwmax=5
wmm_ac_vi_txop_limit=94

wmm_ac_vo_acm=0
wmm_ac_vo_aifs=3
wmm_ac_vo_cwmin=4
wmm_ac_vo_cwmax=5
wmm_ac_vo_txop_limit=47

ieee80211n=1

ssid={pairing_ssid}
ieee8021x=1
eapol_version=1
eap_server=1

wps_state=2
skip_cred_build=1
extra_cred={cred_bin_path.resolve()}
wps_cred_processing=1
uuid=22210203-0405-0607-0809-0a0b0c0d0e0f
manufacturer=Broadcom
model_name=SoftAP
model_number=0
serial_number=0
device_type=6-a4c0e1f4-1
device_name={device_name}
os_version=80000000
config_methods=label push_button
vendor_elements=dd0800904c0700513206dd09001018020100040000
"""
    pairing_conf_path = out_dir / "wiiu_ap_pairing.conf"
    pairing_conf_path.write_text(pairing_conf)

    # 3. Normal configuration
    normal_conf = f"""# WiiU Normal Mode AP
ssid={ssid_normal}
wpa_psk={psk_hex}
interface={iface}

driver=nl80211
logger_stdout=-1
logger_stdout_level=0
ctrl_interface=/var/run/hostapd
ctrl_interface_group={ctrl_grp}
hw_mode=a
channel={channel}
beacon_int=100
dtim_period=3
macaddr_acl=0
auth_algs=3
wmm_enabled=1
uapsd_advertisement_enabled=1

wmm_ac_be_acm=0
wmm_ac_be_aifs=2
wmm_ac_be_cwmin=4
wmm_ac_be_cwmax=5
wmm_ac_be_txop_limit=47

wmm_ac_bk_acm=0
wmm_ac_bk_aifs=7
wmm_ac_bk_cwmin=4
wmm_ac_bk_cwmax=10
wmm_ac_bk_txop_limit=0

wmm_ac_vi_acm=0
wmm_ac_vi_aifs=3
wmm_ac_vi_cwmin=4
wmm_ac_vi_cwmax=5
wmm_ac_vi_txop_limit=94

wmm_ac_vo_acm=0
wmm_ac_vo_aifs=3
wmm_ac_vo_cwmin=4
wmm_ac_vo_cwmax=5
wmm_ac_vo_txop_limit=47

ieee80211n=1
ignore_broadcast_ssid=2
wpa=2
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
"""
    normal_conf_path = out_dir / "generated_ap_normal.conf"
    normal_conf_path.write_text(normal_conf)

    return pairing_conf_path, normal_conf_path, cred_bin_path


def set_conf_interface(conf_text: str, iface: str) -> str:
    """Pone `interface=<iface>` en una configuración de hostapd (la guardada fija wlan0)."""
    return re.sub(r"^interface=.*$", f"interface={iface}", conf_text, count=1, flags=re.MULTILINE)


def apply_conf_options(conf_text: str, opts: Optional[str]) -> str:
    """Cambia o añade líneas `clave=valor` en una configuración de hostapd (DRC_HOSTAPD_OPTS, piezas
    separadas por `;`). Las piezas sin `=`, con clave o valor vacíos se ignoran. Idempotente."""
    if not opts:
        return conf_text
    out = conf_text
    for piece in opts.split(";"):
        if "=" not in piece:
            continue
        key, value = (x.strip() for x in piece.split("=", 1))
        if not key or not value:
            continue
        line = f"{key}={value}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(out):
            out = pattern.sub(line, out, count=1)
        else:
            out = out if out.endswith("\n") else out + "\n"
            out += line + "\n"
    return out


def prepare_wlan0(iface: str = "wlan0", target_mac: Optional[str] = None, region: Optional[str] = None) -> None:
    """Prepare Wi-Fi interface: stop interfering managers, set regulatory domain, MTU and MAC."""
    cfg = load_drc_conf()
    tool = cfg.get("WIFI_MGMT_TOOL", "auto").strip().lower()

    # 1. Desvincular de NetworkManager u otro gestor si existe hook o nmcli
    unmanage_cmd = cfg.get("WIFI_UNMANAGE_CMD", "").strip()
    if unmanage_cmd:
        cmd = shlex.split(unmanage_cmd.format(iface=iface))
        subprocess.run(cmd, check=False)
    elif tool in ("auto", "nmcli") and shutil.which("nmcli"):
        subprocess.run(["nmcli", "device", "disconnect", iface], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["nmcli", "device", "set", iface, "managed", "no"], check=False)

    # 2. Detener suplicantes en conflicto si no se preservan explícitamente
    if iface == "wlan0" and os.environ.get("DRC_KEEP_SUPPLICANT") != "1":
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "stop", "wpa_supplicant"], check=False)
        subprocess.run(["pkill", "-9", "-f", "wpa_supplicant"], check=False)

    subprocess.run(["rfkill", "unblock", "wifi"], check=False)

    ctrl_dir = Path("/var/run/hostapd")
    ctrl_dir.mkdir(parents=True, exist_ok=True)
    stale_sock = ctrl_dir / iface
    if stale_sock.exists():
        stale_sock.unlink()
    subprocess.run(["pkill", "-9", "-f", "hostapd"], check=False)

    # 3. Dominio regulatorio según región configurable (jpn -> JP, usa -> US, eur -> DE) o WIFI_REGDOM
    regdom_cmd = cfg.get("WIFI_REGDOM_CMD", "").strip()
    chosen_region = (region or os.environ.get("WIIU_REGION") or cfg.get("WIIU_REGION", "jpn")).strip().lower()
    regdom_map = {"jpn": "JP", "usa": "US", "eur": "DE"}
    regdom = os.environ.get("WIFI_REGDOM") or cfg.get("WIFI_REGDOM") or regdom_map.get(chosen_region, "US")
    if regdom_cmd:
        cmd = shlex.split(regdom_cmd.format(iface=iface))
        subprocess.run(cmd, check=False)
    else:
        iw_bin = shutil.which("iw") or "/usr/sbin/iw"
        subprocess.run([iw_bin, "reg", "set", regdom], check=False)

    # 4. Configurar interfaz de red vía ip
    subprocess.run(["ip", "link", "set", "dev", iface, "down"], check=False)
    time.sleep(0.5)
    if target_mac:
        subprocess.run(["ip", "link", "set", "dev", iface, "address", target_mac], check=False)
    subprocess.run(["ip", "link", "set", "dev", iface, "mtu", str(drc_mtu())], check=False)
    subprocess.run(["ip", "link", "set", "dev", iface, "up"], check=True)

    # 5. Desactivar ahorro de energía (opcional por comando, o iw si está disponible)
    powersave_cmd = cfg.get("WIFI_POWERSAVE_CMD", "").strip()
    if powersave_cmd:
        cmd = shlex.split(powersave_cmd.format(iface=iface))
        subprocess.run(cmd, check=False)
    elif tool in ("auto", "iw") and shutil.which("iw"):
        subprocess.run(["iw", "dev", iface, "set", "power_save", "off"], check=False)

    # 6. Desactivar rp_filter para permitir recepción de broadcasts DHCP (src 0.0.0.0)
    subprocess.run(["sysctl", "-w", f"net.ipv4.conf.{iface}.rp_filter=0"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sysctl", "-w", "net.ipv4.conf.all.rp_filter=0"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sysctl", "-w", f"net.ipv4.conf.{iface}.bc_forwarding=1"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)


def tsf_sysfs_path(iface: str, sys_net: Path = Path("/sys/class/net")) -> Optional[Path]:
    """Ruta del TSF que expone el driver parcheado (la misma búsqueda que libdrc), o None si no existe."""
    for cand in (sys_net / iface / "device" / "tsf", sys_net / iface / "tsf"):
        if cand.exists():
            return cand
    return None


TSF_MISSING_HELP = (
    "el driver Wi-Fi de {iface} no expone el TSF (/sys/class/net/{iface}/tsf). Sin él los frames de vídeo "
    "llevan sellos de tiempo que el GamePad descarta: se asocia pero no muestra imagen. Instala el driver "
    "parcheado (repo drc-wifi-drivers; rtw89: sudo bash tools/install_rtw89.sh; brcmfmac: tools/install_brcmfmac.sh) o fuerza el "
    "arranque con DRC_ALLOW_NO_TSF=1."
)


def firewall_needs_opening(input_rules: str) -> bool:
    """¿Filtra algo la cadena INPUT? (salida de `iptables -S INPUT`). Con la política ACCEPT y sin reglas, no."""
    lines = [l.strip() for l in input_rules.splitlines() if l.strip()]
    return lines != ["-P INPUT ACCEPT"]


def firewall_cmds(iface: str, ip_gamepad: str, action: str) -> List[List[str]]:
    """Órdenes para aceptar (open) o dejar de aceptar (close) lo que llega del GamePad por `iface`.

    ufw y similares descartan por defecto el UDP entrante (entrada 50022, comandos 50023, mensajes 50010):
    libdrc no recibe ni las respuestas ni las peticiones de resync y el mando se queda sin imagen."""
    rule = ["INPUT", "-i", iface, "-s", ip_gamepad, "-j", "ACCEPT"]
    if action == "open":
        return [["iptables", "-D"] + rule, ["iptables", "-I"] + rule]
    return [["iptables", "-D"] + rule]


def firewall_open(iface: str, ip_gamepad: str) -> bool:
    """Abre el cortafuegos al GamePad si hace falta. FIREWALL_OPEN en drc.conf: auto (por defecto) | 1 | 0."""
    mode = str(load_drc_conf().get("FIREWALL_OPEN", "auto")).strip().lower()
    if mode in ("0", "no", "none") or not shutil.which("iptables"):
        return False
    if mode == "auto":
        res = subprocess.run(["iptables", "-S", "INPUT"], capture_output=True, text=True, check=False)
        if res.returncode != 0 or not firewall_needs_opening(res.stdout):
            return False
    for cmd in firewall_cmds(iface, ip_gamepad, "open"):
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[*] Cortafuegos: se acepta el tráfico de {ip_gamepad} por {iface} mientras dure la sesión.", flush=True)
    return True


def firewall_close(iface: str, ip_gamepad: str) -> None:
    for cmd in firewall_cmds(iface, ip_gamepad, "close"):
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)



# Estado de la vinculación que debe sobrevivir a un reinicio (/tmp se vacía).
PERSIST_DIR = Path("/var/lib/drc")
PAIRING_FILES = ("generated_ap_normal.conf", "generated_netboot_info.conf")


DEFAULT_BASE_OUI = "d8:3a:dd"
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def resolve_base_mac(explicit: Optional[str], cfg: dict, persist_dir: Path = None) -> str:
    """MAC base del emparejamiento (de ella sale el SSID). Orden: argumento, WIFI_BASE_MAC de drc.conf, la ya
    guardada y, si no hay ninguna, una nueva (OUI de WIFI_BASE_OUI + 2 bytes al azar (el último lo fija el PIN)) que se guarda para que el
    SSID siga igual entre arranques: el GamePad lo recuerda tras vincularse."""
    persist_dir = PERSIST_DIR if persist_dir is None else persist_dir
    for cand in (explicit, cfg.get("WIFI_BASE_MAC")):
        cand = (cand or "").strip().lower()
        if cand:
            return cand
    saved = persist_dir / "base_mac"
    try:
        cand = saved.read_text().strip().lower()
        if _MAC_RE.match(cand):
            return cand
    except OSError:
        pass
    oui = (cfg.get("WIFI_BASE_OUI") or DEFAULT_BASE_OUI).strip().lower()
    mac = oui + "".join(":%02x" % b for b in secrets.token_bytes(2)) + ":00"
    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
        saved.write_text(mac + "\n")
    except OSError:
        print(f"[!] No se pudo guardar la MAC base {mac} en {saved}; el SSID cambiará en el próximo arranque.", flush=True)
    return mac


def persist_pairing(out_dir: Path, persist_dir: Path = PERSIST_DIR) -> None:
    """Copia la config del AP y la MAC del GamePad vinculado al directorio persistente."""
    persist_dir.mkdir(parents=True, exist_ok=True)
    for name in PAIRING_FILES:
        src = out_dir / name
        if src.exists():
            shutil.copy2(src, persist_dir / name)


def restore_pairing(out_dir: Path, persist_dir: Path = PERSIST_DIR) -> bool:
    """Si hay vinculación guardada, la copia (y manda) sobre el directorio de trabajo."""
    source_dir = persist_dir
    if not (source_dir / "generated_ap_normal.conf").exists():
        if persist_dir == PERSIST_DIR:
            fallback_cfg = Path(__file__).resolve().parent.parent / "config"
            if (fallback_cfg / "generated_ap_normal.conf").exists():
                source_dir = fallback_cfg
            else:
                return False
        else:
            return False
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in PAIRING_FILES:
        src = source_dir / name
        if src.exists():
            shutil.copy2(src, out_dir / name)
    return True


def get_wlan_mac(iface: str = "wlan0") -> str:
    """Read permanent hardware MAC address for iface."""
    # 1. Priorizar dirección permanente de fábrica en sysfs phy80211
    perm_path = Path(f"/sys/class/net/{iface}/phy80211/addresses")
    if perm_path.exists():
        try:
            lines = [l.strip().lower() for l in perm_path.read_text().splitlines() if l.strip()]
            if lines and re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", lines[0]):
                return lines[0]
        except Exception:
            pass

    # 2. Dirección por ethtool -P si está disponible
    try:
        out = subprocess.check_output(["ethtool", "-P", iface], text=True, stderr=subprocess.DEVNULL)
        m = re.search(r"Permanent address:\s+([0-9a-fA-F:]{17})", out)
        if m:
            return m.group(1).lower()
    except Exception:
        pass

    # 3. Dirección configurada en la interfaz
    mac_path = Path(f"/sys/class/net/{iface}/address")
    if mac_path.exists():
        return mac_path.read_text().strip().lower()
    out = subprocess.check_output(["ip", "link", "show", iface], text=True)
    m = re.search(r"link/ether\s+([0-9a-fA-F:]{17})", out)
    if not m:
        raise RuntimeError(f"Could not find MAC address for {iface}")
    return m.group(1).lower()


def send_hostapd_command(
    cmd: str,
    iface: str = "wlan0",
    ctrl_dir: str = "/var/run/hostapd",
    timeout: float = 5.0,
) -> str:
    """Send a command string to hostapd control interface using a UNIX datagram socket."""
    server_sock_path = Path(ctrl_dir) / iface
    if not server_sock_path.exists():
        raise FileNotFoundError(f"Hostapd control socket not found: {server_sock_path}")

    client_sock_path = Path(f"/tmp/drc_ctrl_{os.getpid()}_{int(time.time() * 1000)}.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(str(client_sock_path))
        sock.settimeout(timeout)
        sock.connect(str(server_sock_path))
        sock.send(cmd.encode("utf-8"))
        data = sock.recv(4096)
        return data.decode("utf-8", errors="replace")
    finally:
        sock.close()
        if client_sock_path.exists():
            client_sock_path.unlink()


def pair_gamepad_wps(
    iface: str = "wlan0",
    digits: Tuple[int, int, int, int] = (0, 0, 0, 0),
    out_dir: Path = Path("/tmp/drc_conf"),
    hostapd_bin: Optional[str] = None,
    timeout: int = 180,
    test_mode: bool = False,
    region: Optional[str] = None,
    base_mac: Optional[str] = None,
) -> Tuple[str, Path, Path]:
    """Execute hostapd in pairing mode with derived code MAC, SSID and WPS PIN."""
    if hostapd_bin is None:
        local_built_hostapd = str(Path(__file__).resolve().parent.parent / "build_local" / "prefix" / "bin" / "hostapd")
        candidates = [
            load_drc_conf().get("HOSTAPD_BIN", "").strip(),
            os.environ.get("DRC_HOSTAPD_BIN", ""),
            local_built_hostapd,
            "/usr/local/bin/hostapd",
            "/usr/sbin/hostapd",
        ]
        for c in candidates:
            if c and Path(c).exists():
                hostapd_bin = c
                break
        if hostapd_bin is None:
            hostapd_bin = shutil.which("hostapd") or "hostapd"

    cfg = load_drc_conf()
    base_hw_mac = resolve_base_mac(base_mac, cfg)
    pair_channel = pairing_channel(cfg)
    code_mac, pairing_ssid, pin = derive_pairing_mac_and_ssid(base_hw_mac, digits)
    normal_ssid = "WiiU" + code_mac.replace(":", "")
    symbols_str = " ".join(SYMBOLS[d] for d in digits)

    dummy_psk = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

    pairing_conf_path, normal_conf_path, _ = generate_pairing_configs(
        wlan_mac=base_hw_mac,
        psk_hex=dummy_psk,
        out_dir=out_dir,
        iface=iface,
        channel=pair_channel,
        digits=digits,
    )

    print(f"[*] Preparando adaptador Wi-Fi {iface} y arrancando hostapd... (espera a que esté listo)", flush=True)
    prepare_wlan0(iface, target_mac=code_mac, region=region)

    sock_path = Path(f"/var/run/hostapd/{iface}")
    if sock_path.exists():
        sock_path.unlink()

    if tsf_sysfs_path(iface) is None:
        print("[!] Nota (emparejar no lo necesita, transmitir sí): " + TSF_MISSING_HELP.format(iface=iface), flush=True)

    cmd = ["sudo", hostapd_bin, "-dd", str(pairing_conf_path)]
    log_file_path = out_dir / "hostapd_pairing.log"
    log_file = open(log_file_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    registered_mac = None
    try:
        t0 = time.time()
        ready = False
        while time.time() - t0 < 15.0:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                log_file.write(out)
                raise RuntimeError(hostapd_failure_message(proc.returncode, out, "DRC_PAIR_CHANNEL"))
            if sock_path.exists():
                try:
                    resp = send_hostapd_command("PING", iface=iface, timeout=0.5)
                    if "PONG" in resp:
                        ready = True
                        break
                except Exception:
                    pass
            time.sleep(0.3)

        if not ready:
            raise TimeoutError(f"hostapd control socket {sock_path} did not respond to PING")

        # Esperar a que el AP esté completamente inicializado (state=ENABLED)
        t_state = time.time()
        while time.time() - t_state < 10.0:
            if proc.poll() is not None:
                break
            try:
                status_resp = send_hostapd_command("STATUS", iface=iface, timeout=0.5)
                if "state=ENABLED" in status_resp:
                    break
            except Exception:
                pass
            time.sleep(0.3)

        # Registrar PIN WPS con reintentos mientras hostapd termina de activar el registrador WPS
        resp = ""
        t_pin = time.time()
        while time.time() - t_pin < 10.0:
            if proc.poll() is not None:
                break
            try:
                resp = send_hostapd_command(f"WPS_PIN any {pin} 600", iface=iface, timeout=1.0)
                if pin in resp or "OK" in resp:
                    break
            except Exception:
                pass
            time.sleep(0.5)

        print(f"[*] PIN WPS '{pin}' registrado en hostapd (respuesta: {resp.strip()})", flush=True)
        if pin not in resp and "OK" not in resp:
            raise RuntimeError(f"Failed to register WPS PIN: {resp}")

        if test_mode:
            return pin, pairing_conf_path, normal_conf_path

        print("\n" + "=" * 65, flush=True)
        print(f"[*] ¡AP LISTO Y EMITIENDO EN 5 GHz (canal {pair_channel})!", flush=True)
        print(f"[*] MAC del AP: {code_mac} | SSID: {pairing_ssid}", flush=True)
        print(f"[*] >> AHORA presiona el botón SYNC detrás del GamePad e introduce: {symbols_str} <<", flush=True)
        print("=" * 65 + "\n", flush=True)

        t_start = time.time()
        warned_normal = False
        while time.time() - t_start < timeout:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue

            log_file.write(line)
            log_file.flush()
            line_str = line.strip()

            if any(k in line_str for k in [
                "STA", "WPS", "Probe Request", "Authentication", "Association",
                "rx_mgmt", "AP-STA", "EAPOL", "deauth", "disassoc", "recv",
                "CTRL-EVENT", "AP-ENABLED", "MLME event", "stype=4"
            ]):
                print(f"[WIFI/DRC] {line_str}", flush=True)

            if not warned_normal and gamepad_wants_normal_network(line_str, normal_ssid):
                warned_normal = True
                print(f"\n[!] AVISO: El GamePad está buscando la red normal '{normal_ssid}' en vez de emparejarse.", flush=True)
                print("[!] Para emparejarlo por WPS, pulsa el botón SYNC (el botón rojo detrás del GamePad) hasta ver los 4 símbolos en su pantalla táctil.\n", flush=True)

            if "WPS-REG-SUCCESS" in line_str:
                parts = line_str.split()
                idx = parts.index("WPS-REG-SUCCESS") if "WPS-REG-SUCCESS" in parts else -1
                if idx >= 0 and idx + 1 < len(parts):
                    registered_mac = parts[idx + 1]
                    print(f"\n[+] ¡ÉXITO TOTAL! GamePad emparejado con MAC: {registered_mac}", flush=True)
                    break

        if not registered_mac:
            raise TimeoutError(f"Timed out waiting for WPS-REG-SUCCESS (revisa {log_file_path})")

        netboot_path = out_dir / "generated_netboot_info.conf"
        netboot_path.write_text(registered_mac + "\n")
        persist_pairing(out_dir)
        print(f"[*] Vinculación guardada en {PERSIST_DIR}", flush=True)

        return registered_mac, pairing_conf_path, normal_conf_path
    finally:
        log_file.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        if sock_path.exists():
            sock_path.unlink()


def get_drc_stream_bin() -> str:
    """Busca el binario drc_stream compilado localmente o instalado en el sistema."""
    cfg = load_drc_conf()
    custom = cfg.get("DRC_STREAM_BIN", "").strip()
    candidates = [
        custom,
        str(Path(__file__).resolve().parent.parent / "build_local" / "prefix" / "bin" / "drc_stream"),
        "/usr/local/bin/drc_stream",
        shutil.which("drc_stream") or "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return "drc_stream"


def start_normal_ap(
    iface: str = "wlan0",
    out_dir: Path = Path("/tmp/drc_conf"),
    hostapd_bin: Optional[str] = None,
    ip_console: str = "192.168.1.10",
    ip_gamepad: str = "192.168.1.11",
    test_mode: bool = False,
    timeout: int = 120,
    stream: bool = False,
    region: Optional[str] = None,
) -> None:
    """Start normal 5 GHz AP on wlan0 and run DHCP server serving ip_gamepad."""
    cfg = load_drc_conf()
    if hostapd_bin is None:
        local_built_hostapd = str(Path(__file__).resolve().parent.parent / "build_local" / "prefix" / "bin" / "hostapd")
        candidates = [
            load_drc_conf().get("HOSTAPD_BIN", "").strip(),
            os.environ.get("DRC_HOSTAPD_BIN", ""),
            local_built_hostapd,
            "/usr/local/bin/hostapd",
            "/usr/sbin/hostapd",
        ]
        for c in candidates:
            if c and Path(c).exists():
                hostapd_bin = c
                break
        if hostapd_bin is None:
            hostapd_bin = shutil.which("hostapd") or "hostapd"

    if restore_pairing(out_dir):
        print(f"[*] Vinculación restaurada desde {PERSIST_DIR}", flush=True)
    normal_conf_path = out_dir / "generated_ap_normal.conf"
    if not normal_conf_path.exists():
        raise FileNotFoundError(f"Config normal not found: {normal_conf_path}")

    # Read paired GamePad MAC if present
    netboot_path = out_dir / "generated_netboot_info.conf"
    gamepad_mac = None
    if netboot_path.exists():
        gamepad_mac = netboot_path.read_text().strip().lower()

    # Read SSID from normal_conf_path to derive AP MAC
    conf_text = normal_conf_path.read_text()
    fixed = set_conf_interface(conf_text, iface)
    # Strip any country_code or ieee80211d to prevent rtw89 UNINITIALIZED->COUNTRY_UPDATE hangs
    fixed = re.sub(r"^country_code=.*$\n?", "", fixed, flags=re.MULTILINE)
    fixed = re.sub(r"^ieee80211d=.*$\n?", "", fixed, flags=re.MULTILINE)
    target_channel = int(cfg.get("WIFI_CHANNEL", 40))
    fixed = re.sub(r"^channel=\d+", f"channel={target_channel}", fixed, flags=re.MULTILINE)
    fixed = apply_conf_options(fixed, os.environ.get("DRC_HOSTAPD_OPTS"))
    if fixed != conf_text:
        normal_conf_path.write_text(fixed)
        conf_text = fixed
    m_ssid = re.search(r"^ssid=(WiiU([0-9a-fA-F]{12}))", conf_text, re.MULTILINE)
    target_ap_mac = None
    if m_ssid:
        hex_mac = m_ssid.group(2)
        target_ap_mac = ":".join(hex_mac[i : i + 2] for i in range(0, 12, 2))

    # 1. Prepare interface
    prepare_wlan0(iface, target_mac=target_ap_mac, region=region)

    # 2. Assign IP and route (/29 to avoid overlapping with eth0 192.168.1.0/24)
    subprocess.run(["ip", "addr", "flush", "dev", iface], check=False)
    subprocess.run(
        ["ip", "addr", "add", f"{ip_console}/29", "broadcast", "192.168.1.15", "dev", iface],
        check=True,
    )
    subprocess.run(
        ["ip", "route", "replace", f"{ip_gamepad}/32", "dev", iface, "src", ip_console],
        check=True,
    )

    # 3. Clean control socket
    sock_path = Path(f"/var/run/hostapd/{iface}")
    if sock_path.exists():
        sock_path.unlink()

    # 4. Launch hostapd
    cmd = ["sudo", hostapd_bin, "-dd", str(normal_conf_path)]
    if shutil.which("stdbuf"):
        # Sin esto hostapd escribe en bloques de 4 KB al ir a una tubería: «AP-STA-CONNECTED» llega tarde
        # o nunca y la espera de conexión caduca con el mando ya asociado.
        cmd = ["stdbuf", "-oL"] + cmd
    log_file_path = out_dir / "hostapd_normal.log"
    log_file = open(log_file_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    dhcp_server = DHCPServer(
        iface=iface,
        allowed_mac=gamepad_mac,
        ip_console=ip_console,
        ip_gamepad=ip_gamepad,
    )

    proc_ffmpeg = None
    proc_stream = None
    capture_log_file = None
    firewall_opened = False
    capture_log_path = None

    try:
        # Wait up to 15s for AP-ENABLED
        t0 = time.time()
        ready = False
        while time.time() - t0 < 15.0:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                log_file.write(out)
                raise RuntimeError(hostapd_failure_message(proc.returncode, out, "WIFI_CHANNEL"))
            if sock_path.exists():
                try:
                    resp = send_hostapd_command("STATUS", iface=iface, timeout=0.5)
                    if "state=ENABLED" in resp:
                        ready = True
                        break
                    elif "PONG" in send_hostapd_command("PING", iface=iface, timeout=0.5):
                        # Socket open, keep polling for ENABLED
                        pass
                except Exception:
                    pass
            time.sleep(0.3)

        if not ready:
            log_tail = ""
            if Path(log_file_path).exists():
                try:
                    log_tail = Path(log_file_path).read_text(errors="replace")[-1000:]
                except Exception:
                    pass
            raise TimeoutError(f"hostapd did not reach state=ENABLED within 15s. Recent logs:\n{log_tail}")

        # Start DHCP server
        dhcp_server.start()
        print(f"[*] AP Normal activo en {iface} (IP {ip_console}/29).", flush=True)
        print(f"[*] Servidor DHCP escuchando en {iface} para GamePad MAC: {gamepad_mac or 'Cualquiera'}", flush=True)

        if not test_mode:
            if tsf_sysfs_path(iface) is None:
                if os.environ.get("DRC_ALLOW_NO_TSF") != "1":
                    raise RuntimeError("Sin TSF: " + TSF_MISSING_HELP.format(iface=iface))
                print("[!] AVISO: " + TSF_MISSING_HELP.format(iface=iface), flush=True)
            firewall_opened = firewall_open(iface, ip_gamepad)

        if test_mode:
            # Check route and TSF
            route_out = subprocess.check_output(["ip", "route", "get", ip_gamepad], text=True)
            if iface not in route_out or ip_console not in route_out:
                raise RuntimeError(f"Unexpected route: {route_out}")
            tsf_path = Path(f"/sys/class/net/{iface}/tsf")
            if not tsf_path.exists() or len(tsf_path.read_bytes()) != 8:
                raise RuntimeError("TSF sysfs invalid or not 8 bytes")
            print("SUCCESS: NORMAL_AP_READY", flush=True)
            return

        if stream:
            cfg = load_drc_conf()
            capture_cmd_str = (
                os.environ.get("DRC_CAPTURE_CMD")
                or cfg.get("DRC_CAPTURE_CMD", "").strip()
            )
            if not capture_cmd_str:
                capture_cmd_str = kms_capture_cmd()

            print(f"[*] Iniciando transmisión directa hacia el GamePad ({capture_cmd_str})...", flush=True)
            hdmi_status = Path("/sys/class/drm/card1-HDMI-A-1/status")
            if hdmi_status.exists():
                try:
                    if hdmi_status.read_text().strip() == "disconnected":
                        hdmi_status.write_text("on")
                except Exception:
                    pass

            capture_log_path = out_dir / "capture_video.log"
            capture_log_file = open(capture_log_path, "w", encoding="utf-8")
            proc_ffmpeg = subprocess.Popen(
                shlex.split(capture_cmd_str),
                stdout=subprocess.PIPE,
                stderr=capture_log_file,
            )

            stream_env = os.environ.copy()
            stream_env["DRC_VSTRM_PAYLOAD"] = str(os.environ.get("DRC_VSTRM_PAYLOAD") or cfg.get("VSTRM_PAYLOAD_SIZE", "1456"))
            stream_env["DRC_TS_OFFSET_US"] = str(os.environ.get("DRC_TS_OFFSET_US") or cfg.get("TS_OFFSET_US", "22000"))
            stream_env["DRC_FILLER"] = str(os.environ.get("DRC_FILLER") or cfg.get("DRC_FILLER", "1"))
            stream_env["DRC_MV_RANGE"] = str(os.environ.get("DRC_MV_RANGE") or cfg.get("DRC_MV_RANGE", "64"))
            stream_env["DRC_STREAM_STATS"] = "1"
            local_lib = REPO_ROOT / "build_local" / "prefix" / "lib"
            if local_lib.is_dir():    # el x264 parcheado de build_local; el del sistema hace caer drc_stream
                stream_env["LD_LIBRARY_PATH"] = f"{local_lib}:{stream_env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
            stream_env["DRC_PACING_KBPS"] = str(os.environ.get("DRC_PACING_KBPS", "16000"))

            if cfg.get("ENABLE_PACING_FQ", "1") == "1":
                subprocess.run(["tc", "qdisc", "replace", "dev", iface, "root", "fq"], check=False)

            subprocess.run(["sysctl", "-w", "net.core.wmem_max=16777216"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sysctl", "-w", "net.core.rmem_max=16777216"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sysctl", "-w", "net.core.wmem_default=2097152"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            drc_bin = get_drc_stream_bin()
            drc_mode = cfg.get("DRC_MODE", "separate")
            drc_fps = str(cfg.get("DISPLAY_FPS", "60"))
            proc_stream = subprocess.Popen(
                [drc_bin, "--dest", ip_gamepad, "--mode", drc_mode, "--fps", drc_fps, "--shutdown-pad"],
                stdin=proc_ffmpeg.stdout,
                env=stream_env,
            )
            print("[*] Transmisor DRC activo y escuchando puertos del GamePad.", flush=True)

        print(f"[*] Esperando que el Wii U GamePad se conecte (timeout {timeout}s)...", flush=True)
        print("    (Enciende tu Wii U GamePad presionando su botón POWER)", flush=True)
        t_start = time.time()
        gamepad_connected = False
        import select

        while time.time() - t_start < timeout:
            line = ""
            if proc.stdout:
                r, _, _ = select.select([proc.stdout], [], [], 0.1)
                if r:
                    line = proc.stdout.readline()
            if line:
                log_file.write(line)
                log_file.flush()
                line_str = line.strip()

                if any(k in line_str for k in [
                    "STA", "Probe Request", "Authentication", "Association",
                    "rx_mgmt", "AP-STA", "EAPOL", "deauth", "disassoc", "recv",
                    "CTRL-EVENT", "AP-ENABLED", "MLME event", "pairwise key handshake completed", "WPA:",
                ]):
                    print(f"[WIFI/DRC] {line_str}", flush=True)

                if any(k in line_str for k in ("pairwise key handshake completed", "AP-STA-CONNECTED", "WPA_PTK entering state PTKINITDONE")):
                    if not gamepad_connected:
                        gamepad_connected = True
                        print(f"[+] ¡GamePad asociado y autenticado vía WPA2-PSK!", flush=True)

            if proc.poll() is not None:
                break
            if proc_stream and proc_stream.poll() is not None:
                break

            if not line:    # con líneas pendientes no se duerme: hostapd -d es muy verboso y el bucle iba retrasado minutos
                time.sleep(0.02)

        # Con el transmisor ya en marcha (start_drc.sh espera al mando por DHCP) agotar la espera no es un error:
        # antes tumbaba la sesión a los 120 s aunque el mando estuviera transmitiendo.
        # hostapd escribe por una tubería con búfer, así que sus líneas llegan tarde y a veces el aviso de conexión no
        # se ve en el plazo. Quien espera de verdad al mando es start_drc.sh (DHCP, con su propio plazo): aquí agotar
        # la espera solo se avisa, que si no la sesión se cae a los 120 s con el mando ya transmitiendo.
        if not gamepad_connected:
            if proc.poll() is not None:
                raise TimeoutError("hostapd terminó antes de que el GamePad se conectara al AP normal")
            print("[!] No se vio el aviso de conexión del GamePad en el log de hostapd; se sigue (lo vigila start_drc.sh).", flush=True)

        print("[*] Enlace DRC en vivo. Presiona Ctrl+C para detener.", flush=True)
        while proc.poll() is None and (proc_stream is None or proc_stream.poll() is None):
            line = ""
            if proc.stdout:
                r, _, _ = select.select([proc.stdout], [], [], 0.1)
                if r:
                    line = proc.stdout.readline()
            if line:
                log_file.write(line)
                log_file.flush()
                line_str = line.strip()
                if any(k in line_str for k in ["AP-STA-DISCONNECTED", "deauthenticated", "disassociated"]):
                    print(f"[WIFI/DRC] {line_str}", flush=True)
            if proc_ffmpeg and proc_ffmpeg.poll() is not None and proc_ffmpeg.returncode != 0:
                capture_log_file.flush()
                err = capture_log_path.read_text(errors="replace")
                print(f"[!] El proceso de captura finalizó con código {proc_ffmpeg.returncode}:\n{err}", flush=True)
                break
            if not line:
                time.sleep(0.1)

    finally:
        if cfg.get("ENABLE_PACING_FQ", "1") == "1":
            subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc_stream is not None and proc_stream.poll() is None:
            proc_stream.terminate()
            try:
                proc_stream.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc_stream.kill()
        if proc_ffmpeg is not None and proc_ffmpeg.poll() is None:
            proc_ffmpeg.terminate()
            try:
                proc_ffmpeg.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc_ffmpeg.kill()
        dhcp_server.stop()
        if capture_log_file and not capture_log_file.closed:
            capture_log_file.close()
        log_file.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        if sock_path.exists():
            sock_path.unlink()
        subprocess.run(["ip", "addr", "flush", "dev", iface], check=False)
        if firewall_opened:   # lo último: el mando sigue enviando mientras se para todo lo demás
            firewall_close(iface, ip_gamepad)


def kms_capture_cmd(card: str = "/dev/dri/card1", fmt: Optional[str] = None) -> str:
    """Comando ffmpeg para capturar la consola KMS en formato YUV420P 854x480 a 60 fps."""
    if fmt is None:
        bpp_file = Path("/sys/class/graphics/fb0/bits_per_pixel")
        if bpp_file.exists():
            try:
                bpp = bpp_file.read_text().strip()
                if bpp == "16":
                    fmt = "rgb565le"
                elif bpp == "32":
                    fmt = "bgr0"
            except Exception:
                pass
        if fmt is None:
            fmt = "bgr0"

    return (
        f"ffmpeg -device {card} -f kmsgrab -i - "
        f"-vf 'hwdownload,format={fmt},scale=854:480:force_original_aspect_ratio=decrease,pad=854:480:(ow-iw)/2:(oh-ih)/2,scale=out_range=limited,format=yuv420p' "
        "-r 60 -f rawvideo -"
    )


def capture_cmd(mode: str = "separate", output: Optional[str] = None, fps: int = 60) -> str:
    """Devuelve el pipeline de captura de vídeo para el modo indicado (salida YUV420P a 30 o 60 fps).

    Modos:
    - separate: captura el socket wayland-drc con wf-recorder en y4m (-m yuv4mpegpipe) a <fps>
    - extended: captura la salida secundaria de 854x480 con wf-recorder -o <output>
    - mirror: ejecuta kms_capture_cmd() sobre la pantalla principal
    """
    mode = mode.lower()
    if mode == "separate":
        return f"WAYLAND_DISPLAY=wayland-drc wf-recorder -D -y -m yuv4mpegpipe -c rawvideo -x yuv420p -F scale=out_range=limited -r {fps} -f -"
    elif mode == "extended":
        out = output or "HDMI-A-2"
        return f"wf-recorder -D -o {out} -y -m yuv4mpegpipe -c rawvideo -x yuv420p -F scale=out_range=limited -r {fps} -f -"
    elif mode == "mirror":
        cmd = kms_capture_cmd()
        if fps == 30:
            cmd = cmd.replace("-r 60", "-r 30")
        return cmd
    else:
        raise ValueError(f"Modo no válido: {mode}. Opciones: separate, extended, mirror")


def connected_outputs(drm_dir: Path = Path("/sys/class/drm")) -> List[str]:
    """Conectores DRM con un monitor conectado (p. ej. ["HDMI-A-1"])."""
    out = []
    for status in sorted(drm_dir.glob("card*-*/status")):
        try:
            if status.read_text().strip() == "connected":
                out.append(status.parent.name.split("-", 1)[1])
        except OSError:
            pass
    return out


def check_monitor_for_mode(mode: str, drm_dir: Path = Path("/sys/class/drm")) -> None:
    """extended y mirror necesitan un escritorio en un monitor; separate no."""
    if mode.lower() in ("extended", "mirror") and not connected_outputs(drm_dir):
        raise RuntimeError(
            f"El modo '{mode}' necesita un monitor conectado con escritorio, y no hay ninguno "
            "(HDMI desconectados). Usa --mode separate para usar solo el GamePad."
        )


REPO_ROOT = Path(__file__).resolve().parent.parent


def find_wiiu_wm() -> Optional[str]:
    """Ruta de wiiu-wm (el escritorio propio del GamePad): en el PATH o compilado en build_local."""
    found = shutil.which("wiiu-wm")
    if found:
        return found
    local = REPO_ROOT / "build_local" / "prefix" / "bin" / "wiiu-wm"
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def wiiu_wm_config() -> str:
    """Configuración del escritorio: la instalada en /etc o, si no hay, la del repositorio."""
    etc = Path("/etc/wiiu-shell/config.json")
    return str(etc if etc.exists() else REPO_ROOT / "desktop" / "config.json")


# Sesión ligera «kiosco»: cage ejecuta una sola aplicación (GAMEPAD_AUTOSTART, p. ej. Steam) a pantalla completa.
KIOSK_SESSIONS = ("cage", "kiosk", "steam")


def pick_compositor(desktop_session: str = "auto", explicit: str = "", host_de: str = "") -> Optional[str]:
    """Compositor del modo separate: el pedido (DRC_COMPOSITOR o DESKTOP_SESSION); si no, el del equipo; si no, labwc."""
    if explicit:
        return find_wiiu_wm() if explicit == "wiiu-wm" else shutil.which(explicit)
    if desktop_session == "wiiu-wm":
        return find_wiiu_wm()
    if desktop_session in KIOSK_SESSIONS:
        return shutil.which("cage")
    for name, exe in (("hyprland", "Hyprland"), ("sway", "sway")):
        if (name in host_de or desktop_session == name) and shutil.which(exe):
            return shutil.which(exe)
    return shutil.which("labwc")


def desktop_user_popen_kwargs(user: Optional[str]) -> dict:
    """Argumentos de Popen para lanzar el escritorio (labwc) como `user` en vez de root.

    Todo lo que se lanza desde el escritorio hereda ese usuario, así que el audio de las
    aplicaciones y el de las pruebas por ssh comparten el mismo dmix (root y usuario a la vez
    no funcionan). libinput abre los dispositivos por el grupo `input` con libseat `noop`
    (no hay sesión de logind). Sin usuario o con root devuelve {} (comportamiento anterior).
    """
    if not user or user == "root":
        return {}
    import pwd
    pw = pwd.getpwnam(user)
    return {
        "user": pw.pw_uid,
        "group": pw.pw_gid,
        "extra_groups": os.getgrouplist(user, pw.pw_gid),
        "env": {"HOME": pw.pw_dir, "USER": user, "LOGNAME": user, "LIBSEAT_BACKEND": "noop"},
    }


def steam_kiosk_command(app: str, steam_home: str, libraries: List[str], user: Optional[str] = None) -> str:
    """Steam del mando con su propio HOME: Steam admite una sola instancia por usuario, y la del PC ya corre.

    Con otro HOME arranca otra instancia (con su propio inicio de sesión) que comparte las bibliotecas de juegos
    por rutas (libraryfolders.vdf, solo la primera vez). Devuelve la orden para `sh -c`.
    """
    home = Path(steam_home).expanduser()
    steam_cfg = home / ".local/share/Steam/config"
    created = [d for d in (steam_cfg, *steam_cfg.parents) if not d.exists() and (d == home or home in d.parents)]
    steam_cfg.mkdir(parents=True, exist_ok=True)
    vdf = steam_cfg / "libraryfolders.vdf"
    if libraries and not vdf.exists():
        body = "".join(f'\t"{i + 1}"\n\t{{\n\t\t"path"\t\t"{lib}"\n\t}}\n' for i, lib in enumerate(libraries))
        vdf.write_text('"libraryfolders"\n{\n' + body + "}\n")
        created.append(vdf)
    if user and os.getuid() == 0:
        # Solo lo que se acaba de crear como root pasa al usuario (Steam, ya como él, crea el resto). Nada de
        # recorrer todo el HOME: pesa gigas y tiene enlaces rotos (.steampath), que además hay que no seguir.
        import pwd
        pw = pwd.getpwnam(user)
        for path in created:
            os.lchown(path, pw.pw_uid, pw.pw_gid)
    unset = " ".join(f"-u {v}" for v in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"))
    return f"env {unset} HOME={shlex.quote(str(home))} {app}"


def cage_argv(app: str, wayland_disp: str, width: int, height: int) -> List[str]:
    """cage con la pantalla del tamaño del mando: los juegos ofrecen la resolución que ven (cage crea 1280x720).
    wlr-randr cambia el modo antes de arrancar la aplicación; si falta, se avisa en cage.log y la captura reduce."""
    mode = f"{width}x{height}@60Hz"
    return ["cage", "--", "sh", "-c",
            f"ln -sf $WAYLAND_DISPLAY ${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}/{wayland_disp} 2>/dev/null || true; "
            f"wlr-randr --output HEADLESS-1 --custom-mode {mode} "
            f"|| echo '[!] sin wlr-randr: la pantalla del GamePad queda en 1280x720 (sudo pacman -S wlr-randr)' >&2; "
            f"exec {app}"]


def isolated_input_argv(argv: List[str], ns_dir: str, uid: int, gid: int) -> List[str]:
    """Ejecuta `argv` (como uid/gid) en un espacio de montaje propio con `ns_dir` montado como /dev/input.

    Así la sesión del GamePad ve el mando y el Steam del escritorio no (ver pc_input_isolate en host_pc.sh).
    Necesita root para unshare/mount; setpriv baja al usuario justo antes de ejecutar `argv`.
    """
    # /dev/shm propio: los dos Steam (mismo uid) comparten memoria con nombre fijo (u1000-ValveIPCSharedObj-Steam)
    script = ('d=$1 u=$2 g=$3; shift 3; mount --bind "$d" /dev/input || exit 1; '
              'mount -t tmpfs -o mode=1777,nosuid,nodev drc-shm /dev/shm || exit 1; '
              'exec setpriv --reuid="$u" --regid="$g" --init-groups -- "$@"')
    return ["unshare", "--mount", "--propagation", "slave", "--", "sh", "-c", script, "drc-input",
            ns_dir, str(uid), str(gid), *argv]


def stop_steam_home(steam_home: str, proc_dir: Path = Path("/proc"), grace: float = 4.0) -> int:
    """Cierra los procesos del Steam que corre con ese HOME (el del mando). Devuelve cuántos había.

    Hace falta al empezar (un Steam viejo con el mismo HOME recibe el `steam` nuevo y se queda con él: la
    llamada sale al instante, cage se cierra y no hay pantalla) y al terminar (cage no se lleva a sus hijos).
    """
    marker = str(Path(steam_home).expanduser() / ".local/share/Steam")
    me = os.getpid()
    pids = []
    for d in proc_dir.iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if marker in cmd:
            pids.append(int(d.name))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        t0 = time.time()
        while sig == signal.SIGTERM and time.time() - t0 < grace and any((proc_dir / str(p)).exists() for p in pids):
            time.sleep(0.2)
    return len(pids)


def steam_libraries(steam_root: Path) -> List[str]:
    """Rutas de las bibliotecas de Steam del usuario, salvo la propia del cliente."""
    vdf = steam_root / "config/libraryfolders.vdf"
    try:
        paths = re.findall(r'"path"\s+"([^"]+)"', vdf.read_text())
    except OSError:
        return []
    return [p for p in paths if Path(p) != steam_root and Path(p).is_dir()]


def setup_virtual_display(
    mode: str = "separate",
    width: int = 854,
    height: int = 480,
    start: Optional[bool] = None,
) -> dict:
    """Configura o arranca la pantalla virtual/secundaria de 854x480 según el modo.

    Modos:
    - separate: arranca labwc en socket WAYLAND_DISPLAY=wayland-drc a 854x480 con foot.
    - extended: habilita salida secundaria en conector DRM card1-HDMI-A-2 a 854x480.
    - mirror: reutiliza la salida principal card1-HDMI-A-1.
    """
    import shutil

    mode = mode.lower()
    if mode not in ("separate", "extended", "mirror"):
        raise ValueError(f"Modo no válido: {mode}. Opciones: separate, extended, mirror")

    info = {
        "mode": mode,
        "width": width,
        "height": height,
        "proc": None,
    }

    if mode == "separate":
        wayland_disp = "wayland-drc"
        env = {
            "WAYLAND_DISPLAY": wayland_disp,
            "WLR_BACKENDS": "headless",
            "WLR_HEADLESS_OUTPUTS": "1",
        }
        # Compositor: en modo separate ejecuta el entorno de escritorio del equipo (no wiiu-shell).
        cfg = load_drc_conf()
        desktop_cmd = os.environ.get("DESKTOP_COMMAND", cfg.get("DESKTOP_COMMAND", "")).strip()
        desktop_session = os.environ.get("DESKTOP_SESSION", cfg.get("DESKTOP_SESSION", "auto")).strip().lower()
        compositor = os.environ.get("DRC_COMPOSITOR", cfg.get("DRC_COMPOSITOR", "")).strip()

        if not desktop_cmd and not compositor and desktop_session == "wiiu-wm":
            compositor = "wiiu-wm"   # solo si se pide (DESKTOP_SESSION o DRC_COMPOSITOR)
        if not desktop_cmd and not compositor and desktop_session in KIOSK_SESSIONS:
            compositor = "cage"

        autostart = os.environ.get("GAMEPAD_AUTOSTART", cfg.get("GAMEPAD_AUTOSTART", "")).strip()
        launcher = f"; {autostart}" if autostart else "; foot -m"

        if desktop_cmd:
            comp_argv = shlex.split(desktop_cmd)
            compositor = comp_argv[0]
            startup_cmd = ""
        elif compositor == "wiiu-wm":
            # Caso explícito para pruebas internas de wiiu-wm
            startup_cmd = (
                f"sh -c 'ln -sf $WAYLAND_DISPLAY ${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}/{wayland_disp} 2>/dev/null || true; "
                f"wlr-randr --output HEADLESS-1 --custom-mode {width}x{height}@60Hz 2>/dev/null || true; "
                f"wvkbd-mobintl --hidden -L 180 --fn \"DejaVu Sans 14\" >/dev/null 2>&1 & echo hidden > /tmp/wvkbd_state{launcher}'"
            )
            comp_argv = [find_wiiu_wm() or "wiiu-wm", "--config", wiiu_wm_config(), "--size", f"{width}x{height}", "-s", startup_cmd]
        elif compositor == "cage":
            # Sesión mínima: cage + una sola aplicación. Sin libinput (ver más abajo): no lee el teclado ni el
            # ratón del PC; el táctil del mando entra con touch2pointer (wlr-virtual-pointer).
            startup_cmd = ""
            app = autostart or "foot"
            steam_home = os.environ.get("GAMEPAD_STEAM_HOME", cfg.get("GAMEPAD_STEAM_HOME", "")).strip()
            if steam_home and app.split()[:1] == ["steam"]:
                owner = os.environ.get("DRC_DESKTOP_USER") or None
                real_home = Path(f"~{owner}" if owner else "~").expanduser()
                stop_steam_home(steam_home.replace("~", str(real_home), 1))
                app = steam_kiosk_command(app, steam_home.replace("~", str(real_home), 1),
                                          steam_libraries(real_home / ".local/share/Steam"), owner)
            comp_argv = cage_argv(app, wayland_disp, width, height)
        elif compositor == "labwc":
            startup_cmd = (
                f"sh -c 'ln -sf $WAYLAND_DISPLAY ${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}/{wayland_disp} 2>/dev/null || true; "
                f"wlr-randr --output HEADLESS-1 --custom-mode {width}x{height}@60Hz 2>/dev/null || true; "
                f"wvkbd-mobintl --hidden -L 180 --fn \"DejaVu Sans 14\" >/dev/null 2>&1 & echo hidden > /tmp/wvkbd_state{launcher}'"
            )
            comp_argv = ["labwc", "-s", startup_cmd]
        else:
            host_de = (os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("XDG_SESSION_DESKTOP", "")).lower()
            startup_cmd = (
                f"sh -c 'ln -sf $WAYLAND_DISPLAY ${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}/{wayland_disp} 2>/dev/null || true; "
                f"wlr-randr --output HEADLESS-1 --custom-mode {width}x{height}@60Hz 2>/dev/null || true; "
                f"wvkbd-mobintl --hidden -L 180 --fn \"DejaVu Sans 14\" >/dev/null 2>&1 & echo hidden > /tmp/wvkbd_state{launcher}'"
            )
            if ("hyprland" in host_de or desktop_session == "hyprland") and shutil.which("Hyprland"):
                compositor = "Hyprland"
                comp_argv = ["Hyprland"]
            elif ("sway" in host_de or desktop_session == "sway") and shutil.which("sway"):
                compositor = "sway"
                comp_argv = ["sway"]
            else:
                compositor = "labwc"
                comp_argv = ["labwc", "-s", startup_cmd]

        info["compositor"] = compositor
        cmd = f"WLR_BACKENDS=headless WLR_HEADLESS_OUTPUTS=1 WAYLAND_DISPLAY={wayland_disp} " + " ".join(comp_argv)
        if startup_cmd and "-s" not in comp_argv:
            cmd += f" \"{startup_cmd}\""

        info["wayland_display"] = wayland_disp
        info["env"] = env
        info["cmd"] = cmd

        if start and not (shutil.which(comp_argv[0]) or Path(comp_argv[0]).is_file()):
            raise RuntimeError(
                f"No está el compositor '{comp_argv[0]}' para el escritorio del GamePad. Compila wiiu-wm con "
                "scripts/build_desktop_local.sh (en el repo wiiu-wm), o indica otro con DESKTOP_COMMAND / DRC_COMPOSITOR en drc.conf."
            )
        should_start = start if start is not None else bool(shutil.which(compositor))
        if should_start:
            xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
            if not xdg_runtime or not os.path.isdir(xdg_runtime):
                xdg_runtime = f"/tmp/drc_runtime_{os.getuid()}"
                os.makedirs(xdg_runtime, mode=0o700, exist_ok=True)
                os.chmod(xdg_runtime, 0o700)
                os.environ["XDG_RUNTIME_DIR"] = xdg_runtime

            full_env = os.environ.copy()
            full_env.update(env)
            full_env["XDG_RUNTIME_DIR"] = xdg_runtime
            full_env.pop("WAYLAND_DISPLAY", None)
            if compositor == "cage":
                # La aplicación del mando no debe hablar con la sesión del PC (su X, su Hyprland, su escritorio).
                for var in ("DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "HYPRLAND_CMD", "SWAYSOCK"):
                    full_env.pop(var, None)
                full_env["XDG_CURRENT_DESKTOP"] = "cage"
                full_env["XDG_SESSION_TYPE"] = "wayland"
            user_kw = {}
            if os.getuid() == 0:
                if compositor != "cage":
                    full_env["WLR_BACKENDS"] = "headless,libinput"
                user_kw = desktop_user_popen_kwargs(os.environ.get("DRC_DESKTOP_USER"))
                full_env.update(user_kw.pop("env", {}))
                # sudo quita DBUS_SESSION_BUS_ADDRESS y Steam (steam-runtime-launcher) muere al instante con
                # «Can't find session bus»: se le da el bus de sesión del usuario si existe.
                bus = Path(f"/run/user/{user_kw.get('user', 0)}/bus")
                if user_kw and bus.exists() and "DBUS_SESSION_BUS_ADDRESS" not in full_env:
                    full_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
                # El mando solo para esta sesión: su propio /dev/input (lo prepara pc_input_isolate de host_pc.sh).
                # El cambio de usuario lo hace setpriv tras montar, así que Popen arranca como root.
                ns_dir = os.environ.get("DRC_INPUT_NS_DIR", "")
                if ns_dir and "user" in user_kw and Path(ns_dir).is_dir():
                    comp_argv = isolated_input_argv(comp_argv, ns_dir, user_kw.pop("user"), user_kw.pop("group"))
                    user_kw.pop("extra_groups", None)
            # wiiu-wm deja su registro en /tmp/wiiu-wm.log y cage en /tmp/drc_conf/cage.log (el resto, sin registro)
            log_out = subprocess.DEVNULL
            log_path = {"wiiu-wm": "/tmp/wiiu-wm.log", "cage": "/tmp/drc_conf/cage.log"}.get(compositor)
            if log_path:
                try:
                    os.makedirs(os.path.dirname(log_path), exist_ok=True)
                    log_out = open(log_path, "w")
                    os.chmod(log_path, 0o644)
                except OSError:
                    log_path = f"/tmp/drc_{compositor}_{os.getuid()}.log"    # /tmp/drc_conf es de root: otro sitio
                    try:
                        log_out = open(log_path, "w")
                    except OSError:
                        log_path, log_out = None, subprocess.DEVNULL
            # Un enlace wayland-drc de una sesión anterior apunta a un socket que ya no es el de este escritorio.
            try:
                (Path(xdg_runtime) / wayland_disp).unlink()
            except OSError:
                pass
            proc = subprocess.Popen(
                comp_argv,
                env=full_env,
                stdout=log_out,
                stderr=subprocess.STDOUT if log_out is not subprocess.DEVNULL else subprocess.DEVNULL,
                **user_kw,
            )
            info["proc"] = proc
            sock_path = Path(xdg_runtime) / wayland_disp
            t0 = time.time()
            # En un arranque en frío labwc tardó ~21 s en crear el socket; con 5 s, wf-recorder
            # arrancaba antes y fallaba con 'failed to create display'.
            while time.time() - t0 < 60.0:
                if sock_path.exists() or proc.poll() is not None:
                    break
                time.sleep(0.1)
            if not sock_path.exists():
                tail = ""
                if log_path:
                    try:
                        tail = "\n" + "".join(Path(log_path).read_text(errors="replace").splitlines(True)[-12:])
                    except OSError:
                        pass
                why = f"terminó con código {proc.returncode}" if proc.poll() is not None else "no creó su pantalla en 60 s"
                if proc.poll() is None:
                    proc.terminate()
                raise RuntimeError(f"El escritorio del GamePad ({compositor}) {why}.{tail}")

            time.sleep(0.3)
            env_client = os.environ.copy()
            env_client["WAYLAND_DISPLAY"] = wayland_disp
            if shutil.which("wlr-randr"):   # wiiu-wm ya crea la salida con --size; es para labwc y similares
                subprocess.run(
                    ["wlr-randr", "--output", "HEADLESS-1", "--custom-mode", f"{width}x{height}"],
                    env=env_client,
                    capture_output=True,
                    check=False,
                )

    elif mode == "extended":
        drm_conn = "/sys/class/drm/card1-HDMI-A-2"
        cmd = f"echo on > {drm_conn}/status 2>/dev/null || true"
        info["drm_connector"] = drm_conn
        info["cmd"] = cmd
        should_start = start if start is not None else Path(drm_conn).exists()
        if should_start:
            status_file = Path(f"{drm_conn}/status")
            if status_file.exists():
                try:
                    status_file.write_text("on")
                except Exception:
                    pass

    elif mode == "mirror":
        drm_conn = "/sys/class/drm/card1-HDMI-A-1"
        cmd = f"echo on > {drm_conn}/status 2>/dev/null || true"
        info["drm_connector"] = drm_conn
        info["cmd"] = cmd
        should_start = start if start is not None else Path(drm_conn).exists()
        if should_start:
            status_file = Path(f"{drm_conn}/status")
            if status_file.exists():
                try:
                    status_file.write_text("on")
                except Exception:
                    pass

    return info



def main():
    import argparse

    cfg = load_drc_conf()
    default_iface = cfg.get("WIFI_IFACE", "wlan0")

    parser = argparse.ArgumentParser(description="DRC Wi-Fi AP and WPS Manager")
    parser.add_argument("--conf", default=None, help="Path to drc.conf configuration file")
    parser.add_argument("--kms-cmd", action="store_true", help="Print ffmpeg KMS capture command line")
    parser.add_argument("--which-compositor", action="store_true", help="Print the compositor separate mode would start (empty if none)")
    parser.add_argument(
        "--mode",
        choices=["separate", "extended", "mirror"],
        default="separate",
        help="Display capture mode (default: separate)",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=60,
        choices=[30, 60],
        help="Capture framerate: 30 or 60 (default: 60)",
    )
    parser.add_argument(
        "--capture-cmd",
        action="store_true",
        help="Print video capture command line for specified --mode",
    )
    parser.add_argument(
        "--setup-virtual-display",
        choices=["separate", "extended", "mirror"],
        help="Setup virtual display for specified mode (separate, extended, mirror)",
    )
    parser.add_argument("--stop-kiosk", action="store_true", help="Cierra el Steam del mando (GAMEPAD_STEAM_HOME) si quedó abierto")
    parser.add_argument("--pair", action="store_true", help="Start WPS pairing mode for Wii U GamePad")
    parser.add_argument("--test-ap", action="store_true", help="Verify pairing AP startup and PIN registration")
    parser.add_argument("--start-ap", action="store_true", help="Start normal 5 GHz AP and DHCP server")
    parser.add_argument("--test-normal-ap", action="store_true", help="Verify normal AP startup, routing and coexistence")
    default_region = cfg.get("WIIU_REGION", "jpn").strip().lower()
    parser.add_argument(
        "--region",
        choices=["jpn", "usa", "eur"],
        default=default_region,
        help=f"Región del GamePad y consola (jpn, usa, eur; por defecto: {default_region})",
    )
    parser.add_argument("--base-mac", default=None, help="Base MAC address for WPS pairing derivation (default: from drc.conf or iface)")
    parser.add_argument("--stream", action="store_true", help="Start KMS video streaming and uinput automatically once GamePad connects")
    parser.add_argument("--iface", default=default_iface, help=f"Wi-Fi interface (default: {default_iface})")
    parser.add_argument("--symbols", default="0,0,0,0", help="Pairing symbols: 0=Spade, 1=Heart, 2=Diamond, 3=Club (default: 0,0,0,0)")
    parser.add_argument("--out-dir", default="/tmp/drc_conf", help="Output directory for configs")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout in seconds to wait for GamePad (default: 120)")
    parser.add_argument("--hostapd", default=None, help="Path to hostapd binary")

    args = parser.parse_args()

    if args.kms_cmd:
        print(kms_capture_cmd())
        return

    if args.stop_kiosk:
        sh = os.environ.get("GAMEPAD_STEAM_HOME", cfg.get("GAMEPAD_STEAM_HOME", "")).strip()
        owner = os.environ.get("DRC_DESKTOP_USER") or None
        if sh:
            stop_steam_home(sh.replace("~", str(Path(f"~{owner}" if owner else "~").expanduser()), 1))
        return

    if args.which_compositor:
        cfg = load_drc_conf()
        host_de = (os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("XDG_SESSION_DESKTOP", "")).lower()
        cmd = os.environ.get("DESKTOP_COMMAND", cfg.get("DESKTOP_COMMAND", "")).strip()
        found = shutil.which(shlex.split(cmd)[0]) if cmd else pick_compositor(
            os.environ.get("DESKTOP_SESSION", cfg.get("DESKTOP_SESSION", "auto")).strip().lower(),
            os.environ.get("DRC_COMPOSITOR", cfg.get("DRC_COMPOSITOR", "")).strip(),
            host_de,
        )
        print(found or "")
        return

    if args.capture_cmd:
        print(capture_cmd(args.mode, fps=args.fps))
        return

    if args.setup_virtual_display:
        try:
            check_monitor_for_mode(args.setup_virtual_display)
        except RuntimeError as e:
            print(f"[!] {e}", file=sys.stderr)
            sys.exit(2)
        info = setup_virtual_display(args.setup_virtual_display, start=True)
        print(f"VIRTUAL_DISPLAY_READY mode={info['mode']} wayland={info.get('wayland_display')}")
        return

    if args.start_ap or args.test_normal_ap:
        start_normal_ap(
            iface=args.iface,
            out_dir=Path(args.out_dir),
            hostapd_bin=args.hostapd,
            test_mode=args.test_normal_ap,
            timeout=args.timeout,
            stream=args.stream,
            region=args.region,
        )
    elif args.pair or args.test_ap:
        # Parse symbols
        try:
            parts = [int(p.strip()) for p in args.symbols.split(",")]
            if len(parts) != 4 or any(p < 0 or p > 3 for p in parts):
                raise ValueError()
            digits = (parts[0], parts[1], parts[2], parts[3])
        except Exception:
            parser.error("--symbols must be 4 comma-separated digits between 0 and 3 (e.g. 0,0,0,0 for ♠♠♠♠)")

        mac_or_pin, pair_conf, norm_conf = pair_gamepad_wps(
            iface=args.iface,
            digits=digits,
            out_dir=Path(args.out_dir),
            hostapd_bin=args.hostapd,
            timeout=args.timeout,
            test_mode=args.test_ap,
            region=args.region,
            base_mac=args.base_mac,
        )
        print(f"SUCCESS: {mac_or_pin}")

        if args.stream and not args.test_ap:
            print("\n[*] Vinculación completada. Arrancando automáticamente el AP normal y streaming DRC...", flush=True)
            time.sleep(1.0)
            start_normal_ap(
                iface=args.iface,
                out_dir=Path(args.out_dir),
                hostapd_bin=args.hostapd,
                timeout=args.timeout,
                stream=True,
                region=args.region,
            )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
