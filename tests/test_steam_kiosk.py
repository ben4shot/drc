"""steam_kiosk_command: HOME propio de Steam para el mando (sin recorrer el HOME ni seguir enlaces rotos)."""
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "drchost"))
import drc_ap  # noqa: E402


def test_crea_la_config_y_comparte_bibliotecas(tmp_path):
    home = tmp_path / "gp"
    cmd = drc_ap.steam_kiosk_command("steam -gamepadui", str(home), ["/mnt/lib1", "/mnt/lib2"])
    vdf = (home / ".local/share/Steam/config/libraryfolders.vdf").read_text()
    assert '"path"\t\t"/mnt/lib1"' in vdf and "/mnt/lib2" in vdf
    assert f"HOME={home}" in cmd and "-u XDG_DATA_HOME" in cmd and cmd.endswith("steam -gamepadui")


def test_no_pisa_el_vdf_existente(tmp_path):
    home = tmp_path / "gp"
    cfg = home / ".local/share/Steam/config"
    cfg.mkdir(parents=True)
    (cfg / "libraryfolders.vdf").write_text("propio")
    drc_ap.steam_kiosk_command("steam", str(home), ["/mnt/lib1"])
    assert (cfg / "libraryfolders.vdf").read_text() == "propio"


def test_como_root_con_enlaces_rotos_no_falla_y_solo_cambia_lo_creado(tmp_path, monkeypatch):
    home = tmp_path / "gp"
    (home / ".local/share").mkdir(parents=True)
    (home / ".local/share/.steampath").symlink_to(home / "no_existe")       # el caso del fallo real
    cambiados = []
    monkeypatch.setattr(drc_ap.os, "getuid", lambda: 0)
    monkeypatch.setattr(drc_ap.os, "lchown", lambda p, u, g: cambiados.append(str(p)))
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name
    drc_ap.steam_kiosk_command("steam", str(home), ["/mnt/lib1"], user=me)
    assert str(home / ".local/share/Steam/config") in cambiados
    assert str(home / ".local/share/.steampath") not in cambiados            # lo que ya existía no se toca


def test_stop_steam_home_solo_mata_los_de_ese_home(tmp_path):
    import subprocess
    home = tmp_path / "gp"
    steam = home / ".local/share/Steam/ubuntu12_32"
    steam.mkdir(parents=True)
    script = steam / "steam"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    mio = subprocess.Popen([str(script)])
    ajeno = subprocess.Popen(["sleep", "30"])
    try:
        assert drc_ap.stop_steam_home(str(home), grace=2) >= 1
        mio.wait(timeout=5)
        assert ajeno.poll() is None                    # otro proceso cualquiera (el Steam del PC) no se toca
    finally:
        ajeno.kill()
        mio.kill()
