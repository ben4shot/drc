import struct
import sys
from pathlib import Path

from drchost import padtest as pt


def _ev(st, dev, et, code, val, ta=None):
    pt.apply_event(st, dev, et, code, val, ta if ta is not None else {})


def test_botones_y_cruceta_se_marcan_y_se_recuerdan():
    st = pt.PadState()
    _ev(st, "pad", pt.EV_KEY, pt.BUTTONS["A"], 1)
    assert st.buttons["A"] and "A" in st.pressed_ever
    _ev(st, "pad", pt.EV_KEY, pt.BUTTONS["A"], 0)
    assert not st.buttons["A"] and "A" in st.pressed_ever
    _ev(st, "pad", pt.EV_ABS, pt.ABS_HAT0X, -1)
    assert st.buttons["LEFT"] and not st.buttons["RIGHT"]
    _ev(st, "pad", pt.EV_ABS, pt.ABS_HAT0Y, 1)
    assert st.buttons["DOWN"]
    assert {"LEFT", "DOWN"} <= st.pressed_ever


def test_gatillos_analogicos_cuentan_como_zl_zr():
    st = pt.PadState()
    _ev(st, "pad", pt.EV_ABS, pt.ABS_Z, 255)
    _ev(st, "pad", pt.EV_ABS, pt.ABS_RZ, 255)
    assert {"ZL", "ZR"} <= st.pressed_ever


def test_sticks_registran_direcciones_al_llegar_al_80_por_ciento():
    st = pt.PadState()
    _ev(st, "pad", pt.EV_ABS, pt.ABS_X, 30000)
    _ev(st, "pad", pt.EV_ABS, pt.ABS_Y, -30000)
    _ev(st, "pad", pt.EV_ABS, pt.ABS_RX, 5000)      # no llega al 80 %
    assert st.stick_dirs == {"L+x", "L-y"}


def test_tactil_zonas_3x3_y_traza():
    st = pt.PadState()
    ta = {}
    _ev(st, "touch", pt.EV_ABS, pt.ABS_X, 10, ta)
    _ev(st, "touch", pt.EV_ABS, pt.ABS_Y, 10, ta)
    _ev(st, "touch", pt.EV_KEY, 0x14A, 1, ta)
    assert st.touch == (10, 10) and "00" in st.touch_zones
    _ev(st, "touch", pt.EV_ABS, pt.ABS_X, 850, ta)
    _ev(st, "touch", pt.EV_ABS, pt.ABS_Y, 470, ta)
    assert "22" in st.touch_zones
    _ev(st, "touch", pt.EV_KEY, 0x14A, 0, ta)
    assert st.touch is None and len(st.touch_trail) >= 2


def test_parse_hid_acelerometro_y_giroscopio():
    msg = bytearray(40)
    msg[16:22] = struct.pack(">hhh", -1500, 300, -8192)         # pitch, roll, z
    msg[22:25] = (250 * 10).to_bytes(3, "big", signed=True)     # 10 °/s
    msg[25:28] = (-250 * 20).to_bytes(3, "big", signed=True)    # -20 °/s
    h = pt.parse_hid(bytes(msg))
    assert h["acc"] == (300, -1500, -8192)
    assert h["gyro"][0] == 10.0 and h["gyro"][1] == -20.0
    assert pt.parse_hid(b"\x00" * 10) is None


def test_mic_level_silencio_y_voz():
    quiet = b"\xd0" + b"\x00" * 7 + struct.pack("<8h", *([100] * 8))
    loud = b"\xd0" + b"\x00" * 7 + struct.pack("<8h", *([3000] * 8))
    assert pt.mic_level(quiet) == 0.0
    assert pt.mic_level(loud) > 0.9
    assert pt.mic_level(b"\x00" * 20) is None


def test_parse_status_y_respuestas():
    s = pt.parse_status("battery=5 charging=1 ac=0 slider=120\nvolume=80 valid=1\n")
    assert s == {"battery": 5, "charging": 1, "ac": 0, "slider": 120, "volume": 80, "valid": 1}
    assert pt.reply_bytes("battery ok 00 05") == b"\x00\x05"
    assert pt.reply_bytes("battery fallo") is None and pt.reply_bytes(None) is None


def test_ctl_query_lee_la_respuesta_nueva(tmp_path):
    import os
    import threading
    import time
    fifo, out = str(tmp_path / "ctl"), str(tmp_path / "out")
    os.mkfifo(fifo)
    open(out, "w").write("viejo ok 01\n")
    rd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)

    def servidor():
        time.sleep(0.2)
        os.read(rd, 100)
        with open(out, "a") as f:
            f.write("battery ok 00 04\n")
    threading.Thread(target=servidor).start()
    assert pt.ctl_query("get battery", "battery", timeout=2, ctl=fifo, out=out) == "battery ok 00 04"
    os.close(rd)


def test_ctl_send_sin_fifo_devuelve_false(tmp_path):
    assert pt.ctl_send("get battery", str(tmp_path / "no_existe")) is False


def test_checks_manuales_automaticos_e_informe():
    rep, st = pt.Report(), pt.PadState()
    pt.update_input_checks(rep, st)
    assert rep.by["buttons"].state == pt.PENDING and "faltan" in rep.by["buttons"].detail
    st.pressed_ever |= set(pt.BUTTONS) | set(pt.BTN_DPAD.values())
    st.stick_dirs |= {f"{s}{sg}{ax}" for s in "LR" for sg in "+-" for ax in "xy"}
    st.touch_zones |= {f"{r}{c}" for r in range(3) for c in range(3)}
    st.acc_range, st.gyro_peak, st.mic_peak, st.cam_frames = 3000, 90, 0.4, 12
    pt.update_input_checks(rep, st)
    for k in ("buttons", "sticks", "touch", "accel", "gyro", "mic", "camera"):
        assert rep.by[k].state == pt.PASS, k

    respuestas = {"get fw_version": "fw_version ok 01 02", "get rssi": "rssi ok 30", "get brightness": "brightness ok 05",
                  "set sensorbar 1": "sensorbar ok", "set sensorbar 0": "sensorbar ok"}
    pt.auto_checks(rep, ctl=lambda c, e, **k: respuestas.get(c),
                   status=lambda: {"valid": 1, "battery": 5, "volume": 100, "slider": 3})
    assert all(rep.by[k].state == pt.PASS for k in ("link", "fw", "rssi", "brightness", "sensorbar", "battery", "volume"))
    assert rep.by["speaker"].state == pt.PENDING and rep.by["speaker"].manual
    assert '"resumen"' in rep.to_json()


def test_auto_checks_sin_transmision_falla_el_enlace():
    rep = pt.Report()
    pt.auto_checks(rep, ctl=lambda *a, **k: None, status=lambda: {})
    assert rep.by["link"].state == pt.FAIL and rep.by["fw"].state == pt.FAIL


def test_reproductor_en_pc_con_pipewire_sin_alsa():
    """Sin aplay (PC con PipeWire) el audio de prueba va al sumidero del mando; sin ningún reproductor devuelve None."""
    solo = lambda *nombres: (lambda n: "/usr/bin/" + n if n in nombres else None)
    assert pt.play_command("/t.wav", which=solo("aplay"), sink="x") == ["aplay", "-q", "/t.wav"]
    assert pt.play_command("/t.wav", which=solo("paplay"), sink="drc-gamepad") == ["paplay", "--device=drc-gamepad", "/t.wav"]
    assert pt.play_command("/t.wav", which=solo("pw-play"), sink="drc-gamepad") == ["pw-play", "--target=drc-gamepad", "/t.wav"]
    assert pt.play_command("/t.wav", which=solo(), sink="drc-gamepad") is None
    salida = "50\talsa_output.pci\tPipeWire\ts32le\tSUSPENDED\n61\tdrc-gamepad\tPipeWire\ts16le\tRUNNING\n"
    assert pt.gamepad_sink(lambda: salida) == "drc-gamepad"
    assert pt.gamepad_sink(lambda: "50\totra\tx\n") is None


def test_tacil_como_puntero_cuando_touch2pointer_tiene_el_dispositivo():
    st = pt.PadState()
    assert st.touch_events == 0
    pt.apply_pointer_touch(st, 10, 10, True)
    pt.apply_pointer_touch(st, 427, 240, True)
    pt.apply_pointer_touch(st, 850, 470, False)
    assert st.touch is None and st.touch_zones == {"00", "11"}
    pt.apply_event(st, "touch", pt.EV_ABS, pt.ABS_X, 5, {})
    assert st.touch_events == 1      # el dispositivo táctil sí entrega eventos: el puntero deja de usarse


def test_tv_cuenta_como_boton_del_probador():
    assert pt.BUTTONS["TV"] == 0x176
    st = pt.PadState()
    pt.apply_event(st, "pad", pt.EV_KEY, 0x176, 1, {})
    assert "TV" in st.pressed_ever


def test_l3_r3_tv_y_power_salen_del_paquete_de_entrada():
    """Aunque el dispositivo uinput no los entregue, el paquete 50122->50022 los lleva: buttons = msg[80]<<16|msg[2]<<8|msg[3]."""
    msg = bytearray(128)
    msg[80] = 0xE0              # TV (0x200000) + R3 (0x400000) + L3 (0x800000)
    msg[4] = 0x02               # POWER pulsado
    h = pt.parse_hid(bytes(msg))
    assert h["extra_buttons"] == {"L3": True, "R3": True, "TV": True, "POWER": True}
    msg[80], msg[4] = 0, 0
    assert not any(pt.parse_hid(bytes(msg))["extra_buttons"].values())
    assert "extra_buttons" not in pt.parse_hid(bytes(40))      # paquete corto: solo sensores


def test_audio_como_root_se_lanza_como_el_usuario_con_su_pipewire():
    env = {"SUDO_UID": "1000", "SUDO_USER": "ana"}
    cmd = pt.como_usuario_de_audio(["paplay", "x.wav"], env=env, geteuid=lambda: 0)
    assert cmd[:5] == ["runuser", "-u", "ana", "--", "env"] and cmd[-2:] == ["paplay", "x.wav"]
    assert "XDG_RUNTIME_DIR=/run/user/1000" in cmd and "PULSE_SERVER=unix:/run/user/1000/pulse/native" in cmd
    assert pt.como_usuario_de_audio(["paplay"], env=env, geteuid=lambda: 1000) == ["paplay"]     # sin root: tal cual
    assert pt.como_usuario_de_audio(["paplay"], env={}, geteuid=lambda: 0) == ["paplay"]         # root sin sudo: tal cual


def test_parse_hid_presion_y_posicion_del_tactil():
    """Igual que libdrc (input-receiver.cpp): 10 puntos de 12 bits (x, y) desde el byte 36 y la presión de 12 bits
    repartida en los bits 4-6 de los bytes 37, 39, 41 y 43 (3 bits cada uno, el 37 los más bajos)."""
    msg = bytearray(128)
    for i in range(10):
        b = 36 + 4 * i
        msg[b], msg[b + 1] = 0x34, 0x02          # x = 0x234
        msg[b + 2], msg[b + 3] = 0x56, 0x01      # y = 0x156
    p = 0b101_011_110_001                       # 12 bits: trozos de 3 bits 001, 110, 011, 101 (del 37 al 43)
    for k, byte in enumerate((37, 39, 41, 43)):
        msg[byte] |= ((p >> (3 * k)) & 7) << 4
    t = pt.parse_hid(bytes(msg))["touch_raw"]
    assert t == {"x": 0x234, "y": 0x156, "pressure": p}
    assert pt.parse_hid(bytes(128))["touch_raw"]["pressure"] == 0


def test_pen_pressure_resume_solo_las_muestras_con_presion():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import pen_pressure
    assert pen_pressure.resumen([0, 0, 5, 10, 7]) == (3, 5, 7, 10)
    assert pen_pressure.resumen([0, 0]) == (0, None, None, None)


def test_pen_pressure_calibra_la_pendiente_de_la_posicion():
    """Panel resistivo: la presión en crudo sube con la posición, de forma curva en x (medido: lineal dejaba el
    centro alto). Con dos fuerzas constantes recorriendo la pantalla se recupera el modelo cuadrático; el rango sale
    de los toques sueltos más suave (p25) y más fuerte (p75), si los hay, y si no de los barridos."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import pen_pressure

    def pos(x, y):
        return 0.75 * x + 0.01 * y - 8.7e-5 * x * x + 8e-6 * y * y + 2.5e-5 * x * y

    rejilla = [(x, y) for x in range(200, 4001, 200) for y in range(200, 3801, 300)]
    suave = [(x, y, round(300 + pos(x, y))) for x, y in rejilla]
    fuerte = [(x, y, round(450 + pos(x, y))) for x, y in rejilla]
    c = pen_pressure.calibrar(suave, fuerte)
    assert abs(c["kx"] - 0.75) < 0.01 and abs(c["ky"] - 0.01) < 0.01
    assert abs(c["kxx"] + 8.7e-5) < 3e-6 and abs(c["kyy"] - 8e-6) < 3e-6 and abs(c["kxy"] - 2.5e-5) < 3e-6
    assert 295 <= c["min"] <= 305 and 445 <= c["max"] <= 455
    leve = [(x, y, round(lvl + pos(x, y))) for (x, y), lvl in zip(rejilla, [100, 200, 300, 400, 500] * 200)]
    duro = [(x, y, round(lvl + pos(x, y))) for (x, y), lvl in zip(rejilla, [500, 600, 700, 800, 900] * 200)]
    c = pen_pressure.calibrar(suave, fuerte, leve, duro)
    assert 195 <= c["min"] <= 205 and 795 <= c["max"] <= 805   # p25 y p75 de 5 niveles


def test_presion_del_lapiz_desde_el_dispositivo_pen():
    """Modo lápiz: el probador lee «Wii U GamePad Pen» (lo que recibe Krita): posición, zonas y presión 0..1023."""
    st, ta = pt.PadState(), {}
    assert pt.pressure_shown(st) is None
    for et, code, val in ((pt.EV_ABS, pt.ABS_X, 100), (pt.EV_ABS, pt.ABS_Y, 50), (pt.EV_KEY, 0x14A, 1),
                          (pt.EV_ABS, pt.ABS_PRESSURE, 700)):
        _ev(st, "pen", et, code, val, ta)
    assert st.touch == (100, 50) and "00" in st.touch_zones
    assert pt.pressure_shown(st) == (700, "lápiz")
    _ev(st, "pen", pt.EV_KEY, 0x14A, 0, ta)
    assert st.touch is None and pt.pressure_shown(st) == (0, "lápiz")


def test_presion_calculada_del_crudo_como_drc_stream():
    """Sin lápiz (modo puntero), con root: la presión se calcula del paquete del mando con el mismo modelo que
    drc_stream (pen_pressure.h), así se ve lo que recibiría Krita."""
    cfg = pt.pen_config({})
    assert cfg["kx"] == 0.7538 and cfg["min"] == -350 and cfg["max"] == 20 and cfg["curve"] == 2.0

    def pos(x, y):
        return (cfg["kx"] * x + cfg["ky"] * y + cfg["kxx"] * x * x + cfg["kyy"] * y * y + cfg["kxy"] * x * y)

    assert pt.pen_pressure_from_raw(0, 2000, 1500, cfg) == 0                         # sin tocar
    assert pt.pen_pressure_from_raw(round(-400 + pos(2000, 1500)), 2000, 1500, cfg) == 1
    assert pt.pen_pressure_from_raw(round(100 + pos(2000, 1500)), 2000, 1500, cfg) == 1023
    mid = pt.pen_pressure_from_raw(round(-165 + pos(2000, 1500)), 2000, 1500, cfg)   # mitad del rango, curva 2
    assert 240 <= mid <= 275
    assert abs(pt.pen_pressure_from_raw(round(-165 + pos(300, 300)), 300, 300, cfg) - mid) <= 3
    c2 = pt.pen_config({"DRC_PEN_PRESSURE_MIN": "-100", "DRC_PEN_PRESSURE_MAX": "50", "DRC_PEN_PRESSURE_CURVE": "1",
                        "DRC_PEN_PRESSURE_KX": "x"})
    assert c2["min"] == -100 and c2["max"] == 50 and c2["curve"] == 1.0 and c2["kx"] == 0.7538   # lo inválido se ignora
    st = pt.PadState()
    pt.apply_raw_touch(st, {"x": 2000, "y": 1500, "pressure": round(-165 + pos(2000, 1500))}, cfg)
    assert pt.pressure_shown(st) == (mid, "calculada")
    pt.apply_raw_touch(st, {"x": 0, "y": 0, "pressure": 0}, cfg)
    assert pt.pressure_shown(st) == (0, "calculada")
    _ev(st, "pen", pt.EV_ABS, pt.ABS_PRESSURE, 5, {})                # el lápiz manda sobre lo calculado
    assert pt.pressure_shown(st) == (5, "lápiz")


def test_radio_del_circulo_crece_con_la_presion():
    assert pt.pressure_radius(None) == pt.pressure_radius(0) == 16
    assert pt.pressure_radius(1023) == 16 + 64
    assert pt.pressure_radius(200) < pt.pressure_radius(600) < pt.pressure_radius(1023)
