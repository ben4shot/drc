"""Modo separate con cage: el mando solo para la sesión del GamePad.

El Steam del escritorio también abría el «Wii U GamePad» y se quedaba con sus botones (el Steam del mando solo
recibía HOME). En el sistema el dispositivo pasa a ser solo de root (sin uaccess) y la sesión del mando corre en
su propio espacio de montaje con una copia de /dev/input donde el mando sí es del usuario.
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "drchost"
sys.path.insert(0, str(HERE))
import drc_ap  # noqa: E402


def _host_pc(func, *args):
    script = f'DESKTOP_UID=1000 DESKTOP_USER=pepe; source "{HERE}/host_pc.sh"; {func} ' + " ".join(args)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


def test_cage_corre_en_su_propio_dev_input_como_el_usuario():
    argv = drc_ap.isolated_input_argv(["cage", "--", "sh", "-c", "exec steam"], "/dev/drc_input", 1000, 1001)
    assert argv[:4] == ["unshare", "--mount", "--propagation", "slave"]
    script = argv[argv.index("-c") + 1]
    assert 'mount --bind "$d" /dev/input' in script and "setpriv" in script and "--init-groups" in script
    assert argv[-5:] == ["cage", "--", "sh", "-c", "exec steam"]
    assert ["/dev/drc_input", "1000", "1001"] == argv[argv.index("-c") + 3:argv.index("-c") + 6]


def test_la_sesion_tiene_su_propio_dev_shm():
    """Los dos Steam (mismo uid) comparten memoria con nombre fijo (/dev/shm/u1000-ValveIPCSharedObj-Steam,
    u1000-Shm_*): aun sin ver el mando, el del escritorio seguía estorbando al de la sesión del GamePad."""
    argv = drc_ap.isolated_input_argv(["x"], "/d", 1, 2)
    script = argv[argv.index("-c") + 1]
    assert "mount -t tmpfs" in script and "/dev/shm" in script
    assert script.index("/dev/shm") < script.index("setpriv")      # montado como root, antes de bajar al usuario


def test_el_argv_aislado_funciona_en_sh(tmp_path):
    """El guion de sh recibe bien los argumentos (se sustituye mount/setpriv por eco para probarlo sin root)."""
    argv = drc_ap.isolated_input_argv(["echo", "hola"], str(tmp_path), 1000, 1001)
    i = argv.index("-c")
    script = (argv[i + 1].replace("mount --bind", "echo MOUNT").replace("mount -t tmpfs", "echo SHM")
              .replace("exec setpriv", "echo SETPRIV"))
    out = subprocess.run(["sh", "-c", script, *argv[i + 2:]], capture_output=True, text=True).stdout
    assert f"MOUNT {tmp_path} /dev/input" in out
    assert "SETPRIV --reuid=1000 --regid=1001 --init-groups -- echo hola" in out


def test_reglas_del_mando_solo_root_y_sin_uaccess():
    reglas = _host_pc("pc_input_kiosk_rules", "pepe")
    pad = [l for l in reglas.splitlines() if 'ATTRS{name}=="Wii U GamePad"' in l]
    assert len(pad) == 1
    assert 'OWNER="root"' in pad[0] and 'MODE="0600"' in pad[0] and 'TAG-="uaccess"' in pad[0]
    assert 'ENV{DRC_PAD_ONLY}="1"' in pad[0]
    tactil = [l for l in reglas.splitlines() if "Touchscreen" in l]
    assert tactil and 'OWNER="pepe"' in tactil[0]          # touch2pointer lo lee como el usuario


def test_orden_de_las_reglas_respecto_a_uaccess():
    """TAG-= tiene que ir después de 70-uaccess (que pone la etiqueta) y antes de 73-seat-late (que da el
    permiso); la copia, después de todo (ya con los ACL puestos)."""
    pad = Path(_host_pc("echo", "$UDEV_RULE_PAD").strip()).name
    mirror = Path(_host_pc("echo", "$UDEV_RULE_MIRROR").strip()).name
    assert "70-uaccess.rules" < pad < "73-seat-late.rules"
    assert mirror > "73-seat-late.rules"


def test_la_copia_de_dev_input_esta_en_un_sistema_de_archivos_sin_nodev():
    """Con nodev (como /run) los nodos copiados existen con el dueño correcto pero no se abren: Steam ni ve el
    mando (pasó en la primera prueba real)."""
    d = Path(_host_pc("echo", "$DRC_INPUT_DIR").strip())
    opts = subprocess.run(["findmnt", "-no", "OPTIONS", "--target", str(d.parent)],
                          capture_output=True, text=True, check=True).stdout
    assert "nodev" not in opts.split(","), f"{d.parent} está montado con nodev: {opts}"


def test_el_guion_de_bash_es_el_mismo_que_el_de_python():
    argv = drc_ap.isolated_input_argv(["x"], "/d", 1, 2)
    assert _host_pc("printf", "%s", '"$DRC_ISOLATE_SH"') == argv[argv.index("-c") + 1]


def test_pc_run_isolated_sin_root_ejecuta_la_orden_tal_cual():
    assert _host_pc("DRC_INPUT_NS_DIR=/tmp", "pc_run_isolated", "echo", "hola").strip() == "hola"


def test_toda_sesion_separate_usa_el_dev_input_propio():
    """No solo cage: Hyprland anidado (y sus atajos) y los compositores de drc_ap también."""
    start = (HERE / "start_drc.sh").read_text()
    host = (HERE / "host_pc.sh").read_text()
    assert "pc_input_setup" not in start and "pc_input_setup()" not in host
    assert start.count("pc_input_isolate") >= 2                     # Hyprland anidado y el resto de separate
    nested = host[host.index("hypr_nested_setup() {"):host.index("hypr_nested_binds() {")]
    assert "pc_run_isolated setsid env -i" in nested and "runuser" not in nested
    binds = host[host.index("hypr_nested_binds() {"):host.index("hypr_nested_cleanup() {")]
    assert "pc_run_isolated env" in binds and "drc_padbinds.py" in binds
    src = (HERE / "drc_ap.py").read_text()
    assert 'if ns_dir and "user" in user_kw' in src                # cualquier compositor, no solo cage


def test_guion_de_copia_es_sh_valido_y_quita_al_desconectar(tmp_path):
    guion = tmp_path / "mirror.sh"
    guion.write_text(_host_pc("pc_input_mirror_script", "1000", "1000", str(tmp_path / "d")))
    subprocess.run(["sh", "-n", str(guion)], check=True)
    d = tmp_path / "d"
    d.mkdir()
    (d / "event7").write_text("")
    env = dict(os.environ, ACTION="remove", DEVNAME="/dev/input/event7")
    subprocess.run(["sh", str(guion)], env=env, check=True)
    assert not (d / "event7").exists()
    texto = guion.read_text()
    assert "chown 1000:1000" in texto and "setfacl" in texto and "mknod" in texto
