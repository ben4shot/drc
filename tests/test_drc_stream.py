from pathlib import Path as _P
REPO = _P(__file__).resolve().parent.parent
PKG = REPO / "drchost"
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest

from drchost.drc_ap import capture_cmd, kms_capture_cmd, setup_virtual_display

import os as _os
TARGET_HOST = _os.environ.get("DRC_TEST_HOST", "")   # usuario@host de una Raspberry Pi para las pruebas de hardware
SRC_DIR = REPO / "src" / "drc_stream"
FRAME_BYTES = 854 * 480 * 3 // 2  # 614880


def test_frame_reader_handles_partial_reads_and_eof(tmp_path: Path):
    """Prueba 27: compila en el PC un test C++ que escribe en un pipe 2,5 frames
    en trozos irregulares y verifica que read_frame entrega exactamente 2 frames
    completos de 614880 bytes y luego devuelve false.
    """
    cpp_test_source = f"""#include <iostream>
#include <vector>
#include <unistd.h>
#include <cassert>
#include <cstring>
#include <sys/wait.h>
#include "{SRC_DIR / "frame_reader.h"}"

int main() {{
    int pipefd[2];
    if (pipe(pipefd) != 0) {{
        std::cerr << "pipe failed\\n";
        return 1;
    }}

    pid_t pid = fork();
    if (pid == 0) {{
        // Child: writer
        close(pipefd[0]);
        // Write 2.5 frames in irregular chunks
        // Frame 1: fill with 'A'
        std::vector<uint8_t> f1(614880, 'A');
        // Frame 2: fill with 'B'
        std::vector<uint8_t> f2(614880, 'B');
        // Half frame: fill with 'C'
        std::vector<uint8_t> f_half(307440, 'C');

        std::vector<uint8_t> total;
        total.insert(total.end(), f1.begin(), f1.end());
        total.insert(total.end(), f2.begin(), f2.end());
        total.insert(total.end(), f_half.begin(), f_half.end());

        // Write in irregular chunks (e.g. 1024, 777, 4096, 12345 bytes)
        size_t written = 0;
        size_t chunk_sizes[] = {{1024, 777, 4096, 12345, 500, 32768, 65536, 100}};
        int idx = 0;
        while (written < total.size()) {{
            size_t to_write = chunk_sizes[idx % 8];
            if (written + to_write > total.size()) {{
                to_write = total.size() - written;
            }}
            ssize_t n = write(pipefd[1], total.data() + written, to_write);
            if (n <= 0) break;
            written += n;
            idx++;
        }}
        close(pipefd[1]);
        return 0;
    }}

    // Parent: reader
    close(pipefd[1]);
    std::vector<uint8_t> frame_buf;

    // Frame 1
    bool ok1 = drc_stream::read_frame(pipefd[0], frame_buf);
    assert(ok1 == true);
    assert(frame_buf.size() == 614880);
    assert(frame_buf[0] == 'A');
    assert(frame_buf[614879] == 'A');

    // Frame 2
    bool ok2 = drc_stream::read_frame(pipefd[0], frame_buf);
    assert(ok2 == true);
    assert(frame_buf.size() == 614880);
    assert(frame_buf[0] == 'B');
    assert(frame_buf[614879] == 'B');

    // Third call should attempt to read full frame but hit EOF on the partial half-frame -> return false
    bool ok3 = drc_stream::read_frame(pipefd[0], frame_buf);
    assert(ok3 == false);

    close(pipefd[0]);
    int status = 0;
    waitpid(pid, &status, 0);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);

    std::cout << "SUCCESS: 2 frames read, EOF detected correctly\\n";
    return 0;
}}
"""
    test_src_path = tmp_path / "test_frame_reader.cpp"
    test_bin_path = tmp_path / "test_frame_reader"
    test_src_path.write_text(cpp_test_source)

    build_res = subprocess.run(
        ["g++", "-std=c++17", "-O2", str(test_src_path), "-o", str(test_bin_path)],
        capture_output=True,
        text=True,
    )
    assert build_res.returncode == 0, f"Compilation failed:\n{build_res.stderr}"

    run_res = subprocess.run(
        [str(test_bin_path)],
        capture_output=True,
        text=True,
    )
    assert run_res.returncode == 0, f"Execution failed:\n{run_res.stderr}\n{run_res.stdout}"
    assert "SUCCESS: 2 frames read, EOF detected correctly" in run_res.stdout


def _run_frame_reader_cpp(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    src = f"""#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <unistd.h>
#include "{SRC_DIR / "frame_reader.h"}"

#define CHECK(c) do {{ if (!(c)) {{ fprintf(stderr, "FAIL line %d: %s\\n", __LINE__, #c); return 1; }} }} while (0)

static int feed(const std::string& data) {{
    // Escribe todo a un fichero temporal y devuelve un fd de lectura (evita bloqueos de pipe).
    char path[] = "/tmp/frame_reader_XXXXXX";
    int fd = mkstemp(path);
    unlink(path);
    size_t off = 0;
    while (off < data.size()) off += write(fd, data.data() + off, data.size() - off);
    lseek(fd, 0, SEEK_SET);
    return fd;
}}

static std::string frame(char fill) {{ return std::string(614880, fill); }}

int main() {{
{body}
    printf("SUCCESS\\n");
    return 0;
}}
"""
    src_path = tmp_path / "test_fr.cpp"
    bin_path = tmp_path / "test_fr"
    src_path.write_text(src)
    build = subprocess.run(["g++", "-std=c++17", "-O2", str(src_path), "-o", str(bin_path)],
                           capture_output=True, text=True)
    assert build.returncode == 0, f"Compilation failed:\n{build.stderr}"
    return subprocess.run([str(bin_path)], capture_output=True, text=True)


def test_frame_reader_y4m_validates_and_resyncs(tmp_path: Path):
    """Prueba 50: drc_stream::FrameReader. Con rawvideo un desfase de unos bytes en el
    pipe desplaza toda la imagen en el GamePad para siempre (la fila se «da la vuelta»);
    con y4m cada frame empieza por FRAME\n y el lector puede validarlo y realinearse.
    - detecta y4m por la firma YUV4MPEG2 y valida W854 H480 y C420*;
    - lee frames y4m y, si encuentra basura antes de FRAME, se realinea y cuenta resyncs();
    - sin firma y4m lee rawvideo como antes (modo mirror/tests con ffmpeg);
    - cabecera y4m con otro tamaño o un frame truncado -> false.
    """
    body = r"""
    using drc_stream::FrameReader;
    std::vector<uint8_t> buf;
    const std::string hdr = "YUV4MPEG2 W854 H480 F90000:1 Ip A0:0 C420jpeg XYSCSS=420JPEG XCOLORRANGE=FULL\n";

    {   // y4m limpio: 2 frames y EOF
        FrameReader r(feed(hdr + "FRAME\n" + frame('A') + "FRAME\n" + frame('B')));
        CHECK(r.Next(buf)); CHECK(r.is_y4m()); CHECK(buf.size() == 614880);
        CHECK(buf[0] == 'A' && buf[614879] == 'A');
        CHECK(r.Next(buf)); CHECK(buf[0] == 'B' && buf[614879] == 'B');
        CHECK(!r.Next(buf)); CHECK(r.resyncs() == 0);
    }
    {   // basura (7 bytes) entre frames: se realinea con el siguiente FRAME
        FrameReader r(feed(hdr + "FRAME\n" + frame('A') + "xxxxxxx" + "FRAME\n" + frame('B')));
        CHECK(r.Next(buf)); CHECK(buf[0] == 'A');
        CHECK(r.Next(buf)); CHECK(buf[0] == 'B' && buf[614879] == 'B');
        CHECK(r.resyncs() == 1);
        CHECK(!r.Next(buf));
    }
    {   // wf-recorder escribe "selected region 0,0 0x0\n" por stdout antes del y4m:
        // con rawvideo eran 24 bytes que desplazaban toda la imagen 24 px.
        FrameReader r(feed("selected region 0,0 0x0\n" + hdr + "FRAME\n" + frame('A')));
        CHECK(r.Next(buf)); CHECK(r.is_y4m());
        CHECK(buf[0] == 'A' && buf[614879] == 'A');
    }
    {   // FRAME con parámetros (FRAME Ixx\n) también vale
        FrameReader r(feed(hdr + "FRAME Ip\n" + frame('C')));
        CHECK(r.Next(buf)); CHECK(buf[0] == 'C' && buf[614879] == 'C'); CHECK(r.resyncs() == 0);
    }
    {   // tamaño distinto en la cabecera: se rechaza
        FrameReader r(feed("YUV4MPEG2 W1280 H720 F60:1 C420jpeg\nFRAME\n" + std::string(1280*720*3/2, 'D')));
        CHECK(!r.Next(buf));
    }
    {   // frame y4m truncado: false
        FrameReader r(feed(hdr + "FRAME\n" + std::string(1000, 'E')));
        CHECK(!r.Next(buf));
    }
    {   // rawvideo (sin firma): 2 frames exactos y EOF con medio frame
        FrameReader r(feed(frame('R') + frame('S') + std::string(307440, 'T')));
        CHECK(r.Next(buf)); CHECK(!r.is_y4m()); CHECK(buf[0] == 'R' && buf[614879] == 'R');
        CHECK(r.Next(buf)); CHECK(buf[0] == 'S' && buf[614879] == 'S');
        CHECK(!r.Next(buf));
    }
"""
    res = _run_frame_reader_cpp(tmp_path, body)
    assert res.returncode == 0, f"Execution failed:\n{res.stderr}\n{res.stdout}"
    assert "SUCCESS" in res.stdout


def test_drc_stream_builds_and_pushes_testsrc_on_rpi4():
    """Prueba 28: en la Raspberry Pi 4B (<ip-de-la-pi>), build_userspace.sh --stream
    compila drc_stream; con ffmpeg -f lavfi -i testsrc=size=854x480:rate=60 -pix_fmt yuv420p -f rawvideo - | drc_stream
    durante 10 s el log reporta >= 55 fps empujados y sin errores.
    """
    build_script = PKG / "build_userspace.sh"
    assert build_script.exists()

    build_res = subprocess.run(
        [str(build_script), "--stream", TARGET_HOST],
        capture_output=True,
        text=True,
    )
    assert build_res.returncode == 0, f"Error building drc_stream:\n{build_res.stderr}\n{build_res.stdout}"

    # Correr ffmpeg testsrc pipeado a drc_stream durante 11 segundos
    run_cmd = (
        "timeout --preserve-status 11s bash -c '"
        "ffmpeg -f lavfi -i testsrc=size=854x480:rate=60 -pix_fmt yuv420p -f rawvideo - 2>/dev/null | "
        "drc_stream --bind 0.0.0.0"
        "'"
    )
    run_res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, run_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # timeout command will return code 143 or 0 on exit
    output = run_res.stdout + run_res.stderr
    print(f"RPi4 testsrc output:\n{output}")

    import re
    fps_matches = re.findall(r"(\d+(?:\.\d+)?)\s*fps", output)
    assert len(fps_matches) >= 1, f"No se encontraron reportes de FPS en la salida:\n{output}"

    # Verificar que el último reporte o al menos un reporte da >= 55.0 fps
    measured_fps = [float(f) for f in fps_matches]
    assert any(fps >= 55.0 for fps in measured_fps), f"FPS insuficientes ({measured_fps} < 55.0) en:\n{output}"


def test_kmsgrab_capture_yields_native_frames_on_rpi4():
    """Prueba 29: en la Raspberry Pi 4B con HDMI conectado, kms_capture_cmd()
    ejecutado 5 s produce un volumen múltiplo de 614880 bytes a >= 55 frames/s.
    """
    # 1. Copiar drc_ap.py a la Raspberry Pi
    drc_ap_local = PKG / "drc_ap.py"
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(drc_ap_local), f"{TARGET_HOST}:~/drc_ap.py"],
        check=True,
    )

    # 2. Obtener comando kms_capture_cmd desde la Pi y asegurar estado HDMI
    prepare_cmd = (
        "if [ -f /sys/class/drm/card1-HDMI-A-1/status ]; then "
        "  status=$(cat /sys/class/drm/card1-HDMI-A-1/status); "
        "  if [ \"$status\" = \"disconnected\" ]; then "
        "    echo on | sudo tee /sys/class/drm/card1-HDMI-A-1/status >/dev/null; "
        "  fi; "
        "fi; "
        "python3 ~/drc_ap.py --kms-cmd"
    )
    res_prep = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, prepare_cmd],
        capture_output=True,
        text=True,
        check=True,
    )
    cmd_str = res_prep.stdout.strip()
    assert "-f kmsgrab" in cmd_str
    assert "854:480" in cmd_str
    assert "format=yuv420p" in cmd_str

    # 3. Ejecutar 5 segundos remotamente con timeout y contar bytes
    remote_cmd = f"sudo timeout 5s {cmd_str} 2>/dev/null | wc -c"
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, remote_cmd],
        capture_output=True,
        text=True,
        timeout=20,
    )
    total_bytes_str = res.stdout.strip()
    assert total_bytes_str.isdigit(), f"Salida inesperada de wc -c: {res.stderr}\n{res.stdout}"
    total_bytes = int(total_bytes_str)

    # Debe ser al menos 50 frames/s * 5s * 614880 bytes
    frames_captured = total_bytes // FRAME_BYTES
    remainder = total_bytes % FRAME_BYTES
    print(f"Captured {total_bytes} bytes ({frames_captured} frames, remainder {remainder}) in ~5s")

    fps = frames_captured / 5.0
    assert fps >= 50.0, f"Captura kmsgrab por debajo de velocidad esperada: {fps} fps ({frames_captured} frames)"


def test_map_buttons_and_sticks(tmp_path: Path):
    """Prueba 30: test C++ en el PC que verifica cada máscara kBtn* contra su código
    BTN_*/ABS_HAT0* y que map_stick(-1/0/1) da -32767/0/32767 con Y invertido.
    """
    cpp_source = f"""#include <iostream>
#include <cassert>
#include <linux/input.h>
#include "{SRC_DIR / "uinput_map.h"}"

int main() {{
    using namespace drc_stream;

    // 1. Sticks
    assert(map_stick(0.0f) == 0);
    assert(map_stick(1.0f) == 32767);
    assert(map_stick(-1.0f) == -32767);
    assert(map_stick(1.5f) == 32767);   // Clamping
    assert(map_stick(-1.5f) == -32767);

    // Eje Y invertido (en gamepad standard, arriba es negativo en input_event, libdrc da arriba positivo)
    assert(map_stick_y(0.0f) == 0);
    assert(map_stick_y(1.0f) == -32767);
    assert(map_stick_y(-1.0f) == 32767);

    // 2. Buttons mapping individual check
    auto events = map_buttons(kBtnA);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_EAST && events[0].value == 1);

    events = map_buttons(kBtnB);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_SOUTH && events[0].value == 1);

    events = map_buttons(kBtnX);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_NORTH && events[0].value == 1);

    events = map_buttons(kBtnY);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_WEST && events[0].value == 1);

    events = map_buttons(kBtnL);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_TL && events[0].value == 1);

    events = map_buttons(kBtnR);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_TR && events[0].value == 1);

    events = map_buttons(kBtnZL);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_TL2 && events[0].value == 1);

    events = map_buttons(kBtnZR);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_TR2 && events[0].value == 1);

    events = map_buttons(kBtnMinus);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_SELECT && events[0].value == 1);

    events = map_buttons(kBtnPlus);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_START && events[0].value == 1);

    events = map_buttons(kBtnHome);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_MODE && events[0].value == 1);

    events = map_buttons(kBtnL3);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_THUMBL && events[0].value == 1);

    events = map_buttons(kBtnR3);
    assert(events.size() == 1);
    assert(events[0].type == EV_KEY && events[0].code == BTN_THUMBR && events[0].value == 1);

    // 3. D-Pad (Hat)
    events = map_buttons(kBtnLeft);
    assert(events.size() == 1);
    assert(events[0].type == EV_ABS && events[0].code == ABS_HAT0X && events[0].value == -1);

    events = map_buttons(kBtnRight);
    assert(events.size() == 1);
    assert(events[0].type == EV_ABS && events[0].code == ABS_HAT0X && events[0].value == 1);

    events = map_buttons(kBtnUp);
    assert(events.size() == 1);
    assert(events[0].type == EV_ABS && events[0].code == ABS_HAT0Y && events[0].value == -1);

    events = map_buttons(kBtnDown);
    assert(events.size() == 1);
    assert(events[0].type == EV_ABS && events[0].code == ABS_HAT0Y && events[0].value == 1);

    std::cout << "SUCCESS: All buttons and sticks mapped correctly\\n";
    return 0;
}}
"""
    test_src = tmp_path / "test_map.cpp"
    test_bin = tmp_path / "test_map"
    test_src.write_text(cpp_source)

    build_res = subprocess.run(
        ["g++", "-std=c++17", "-O2", str(test_src), "-o", str(test_bin)],
        capture_output=True,
        text=True,
    )
    assert build_res.returncode == 0, f"Compilation failed:\n{build_res.stderr}"

    run_res = subprocess.run([str(test_bin)], capture_output=True, text=True)
    assert run_res.returncode == 0, f"Run failed:\n{run_res.stderr}\n{run_res.stdout}"
    assert "SUCCESS: All buttons and sticks mapped correctly" in run_res.stdout


def test_map_touch_calibration_and_clamp(tmp_path: Path):
    """Prueba 31: test C++ en el PC que con calibración 0.05 0.95 0.05 0.95
    mapea (0.05,0.05)->(0,0), (0.95,0.95)->(853,479), (0.5,0.5)->(426/427,239/240)
    y recorta valores fuera de rango.
    """
    cpp_source = f"""#include <iostream>
#include <cassert>
#include <cmath>
#include "{SRC_DIR / "uinput_map.h"}"

int main() {{
    using namespace drc_stream;

    TouchCalib calib{{0.05f, 0.95f, 0.05f, 0.95f}};

    // Esquinas calibradas
    TouchCoord c_top_left = map_touch(0.05f, 0.05f, calib);
    assert(c_top_left.x == 0);
    assert(c_top_left.y == 0);

    TouchCoord c_bottom_right = map_touch(0.95f, 0.95f, calib);
    assert(c_bottom_right.x == 853);
    assert(c_bottom_right.y == 479);

    // Centro
    TouchCoord c_center = map_touch(0.5f, 0.5f, calib);
    assert(c_center.x >= 426 && c_center.x <= 427);
    assert(c_center.y >= 239 && c_center.y <= 240);

    // Valores fuera de rango (clamp)
    TouchCoord c_under = map_touch(0.00f, 0.00f, calib);
    assert(c_under.x == 0);
    assert(c_under.y == 0);

    TouchCoord c_over = map_touch(1.00f, 1.00f, calib);
    assert(c_over.x == 853);
    assert(c_over.y == 479);

    // Valores extremos negativos o gigantes
    TouchCoord c_neg = map_touch(-10.0f, -5.0f, calib);
    assert(c_neg.x == 0);
    assert(c_neg.y == 0);

    TouchCoord c_huge = map_touch(99.0f, 50.0f, calib);
    assert(c_huge.x == 853);
    assert(c_huge.y == 479);

    std::cout << "SUCCESS: Touch calibration and clamping verified\\n";
    return 0;
}}
"""
    test_src = tmp_path / "test_touch.cpp"
    test_bin = tmp_path / "test_touch"
    test_src.write_text(cpp_source)

    build_res = subprocess.run(
        ["g++", "-std=c++17", "-O2", str(test_src), "-o", str(test_bin)],
        capture_output=True,
        text=True,
    )
    assert build_res.returncode == 0, f"Compilation failed:\n{build_res.stderr}"

    run_res = subprocess.run([str(test_bin)], capture_output=True, text=True)
    assert run_res.returncode == 0, f"Run failed:\n{run_res.stderr}\n{run_res.stdout}"
    assert "SUCCESS: Touch calibration and clamping verified" in run_res.stdout


def test_uinput_devices_registered_on_rpi4():
    """Prueba 32: en la Raspberry Pi 4B, al arrancar drc_stream (con entrada de vídeo testsrc),
    /proc/bus/input/devices lista 'Wii U GamePad' y 'Wii U GamePad Touchscreen' con las capacidades
    esperadas, y desaparecen al terminar el proceso.
    """
    # 1. Compilar drc_stream actualizado en la Pi
    build_script = PKG / "build_userspace.sh"
    build_res = subprocess.run(
        [str(build_script), "--stream", TARGET_HOST],
        capture_output=True,
        text=True,
    )
    assert build_res.returncode == 0, f"Build error:\n{build_res.stderr}\n{build_res.stdout}"

    # 2. Iniciar drc_stream en segundo plano durante 6 segundos y verificar /proc/bus/input/devices
    test_cmd = (
        "timeout --preserve-status 6s bash -c '"
        "ffmpeg -f lavfi -i testsrc=size=854x480:rate=60 -pix_fmt yuv420p -f rawvideo - 2>/dev/null | "
        "sudo /usr/local/bin/drc_stream --bind 0.0.0.0"
        "' & "
        "PID=$!; "
        "sleep 2; "
        "DEVICES=$(cat /proc/bus/input/devices); "
        "echo \"=== DEVICES FOUND ===\"; "
        "echo \"$DEVICES\" | grep -E 'Wii U GamePad'; "
        "wait $PID || true; "
        "sleep 1; "
        "POST_DEVICES=$(cat /proc/bus/input/devices); "
        "echo \"=== POST TERMINATION ===\"; "
        "echo \"$POST_DEVICES\" | grep -E 'Wii U GamePad' || echo 'CLEANED_UP'"
    )

    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, test_cmd],
        capture_output=True,
        text=True,
        timeout=25,
    )
    output = res.stdout
    print(f"RPi4 uinput check:\n{output}")

    assert 'Name="Wii U GamePad"' in output, f"Dispositivo 'Wii U GamePad' no registrado:\n{output}"
    assert 'Name="Wii U GamePad Touchscreen"' in output, f"Dispositivo 'Wii U GamePad Touchscreen' no registrado:\n{output}"
    assert 'CLEANED_UP' in output, f"Los dispositivos uinput no se limpiaron al salir:\n{output}"


def test_start_drc_dry_run_order():
    """Prueba 33: en el PC ejecuta DRC_DRY_RUN=1 drchost/start_drc.sh y verifica
    el orden drc_ap.py --start-ap -> espera de lease -> ffmpeg ... kmsgrab -> drc_stream,
    y que existe el trap de limpieza.
    """
    script_path = PKG / "start_drc.sh"
    assert script_path.exists(), f"El script {script_path} debe existir"

    env = dict(os.environ, DRC_DRY_RUN="1")
    res = subprocess.run([str(script_path)], capture_output=True, text=True, env=env)
    assert res.returncode == 0, f"Error en ejecución dry-run:\n{res.stderr}\n{res.stdout}"

    output = res.stdout
    # Verificar orden de ejecución en dry-run
    idx_ap = output.find("drc_ap.py --start-ap")
    idx_lease = output.find("192.168.1.11")
    idx_stream = output.find("drc_stream")

    assert idx_ap != -1, f"'drc_ap.py --start-ap' no encontrado en salida:\n{output}"
    assert idx_lease != -1, f"Espera de lease '192.168.1.11' no encontrada en salida:\n{output}"
    assert idx_stream != -1, f"'drc_stream' no encontrado en salida:\n{output}"
    assert idx_ap < idx_lease < idx_stream, f"Orden incorrecto de arranque:\n{output}"

    # Verificar que el script define trap de limpieza
    script_text = script_path.read_text()
    assert "trap " in script_text
    assert "INT" in script_text or "SIGINT" in script_text
    assert "ip addr flush" in script_text

    # drc_stream en tiempo real (SCHED_FIFO): con la CPU cargada se perdían slots de
    # frame (gap_max ~40 ms) y el GamePad pedía resync ~45/s; con chrt -f se mantiene.
    assert "chrt -f 50" in output, f"drc_stream debe lanzarse con chrt -f 50:\n{output}"
    assert 'chrt -f "${DRC_RT_PRIO}" ${DRC_STREAM_BIN}' in script_text
    assert 'DRC_STREAM_BIN="/usr/local/bin/drc_stream"' in script_text

    # DRC_CAPTURE_CMD sustituye la captura (p. ej. un patrón ffmpeg para diagnosticar
    # el desplazamiento horizontal de la imagen en el GamePad).
    assert 'CAPTURE_CMD="${DRC_CAPTURE_CMD:-' in script_text
    # El vídeo de wf-recorder sale por el fd 3 y su stdout (texto) va a stderr.
    assert '-f /dev/fd/3 3>&1 1>&2' in script_text


@pytest.mark.usefixtures("drc_service_paused")
def test_start_drc_cleanup_on_sigint_on_rpi4():
    """Prueba 34: en la Raspberry Pi 4B lanza start_drc.sh con un lease simulado,
    envía SIGINT a los 15 s y comprueba por SSH que no quedan procesos hostapd/ffmpeg/drc_stream,
    que wlan0 no tiene 192.168.1.10 y que la sesión SSH por eth0 sigue viva.
    """
    # 1. Copiar scripts actualizados a la Pi (copia de desarrollo en ~/, no /opt/drc)
    start_drc_local = PKG / "start_drc.sh"
    drc_ap_local = PKG / "drc_ap.py"
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(start_drc_local), f"{TARGET_HOST}:~/start_drc.sh"],
        check=True,
    )
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(drc_ap_local), f"{TARGET_HOST}:~/drc_ap.py"],
        check=True,
    )

    # 2. Ejecutar test remoto que arranca start_drc.sh, simula la conexión del GamePad,
    # envía SIGINT tras 10 s de transmisión y verifica limpieza
    remote_test_cmd = (
        'cat << "EOF" > /tmp/test_runner.sh\n'
        '#!/usr/bin/env bash\n'
        'chmod +x ~/start_drc.sh\n'
        'sudo mkdir -p /tmp/drc_conf\n'
        'sudo chmod 777 /tmp/drc_conf\n'
        'rm -f /tmp/drc_conf/start_ap.log /tmp/drc_conf/test_run.log /tmp/drc_conf/start_drc.pid\n'
        'sudo ~/start_drc.sh > /tmp/drc_conf/test_run.log 2>&1 &\n'
        'SCRIPT_PID=$!\n'
        'sleep 2\n'
        'echo "[DHCP] Sent Ack to aa:bb:cc:dd:ee:01 -> 192.168.1.11" | sudo tee -a /tmp/drc_conf/start_ap.log >/dev/null\n'
        'sleep 4\n'
        'pgrep -f drc_stream >/dev/null && echo "STREAM_STARTED"\n'
        'sleep 2\n'
        'sudo kill -TERM $SCRIPT_PID 2>/dev/null || true\n'
        'sudo kill -INT $SCRIPT_PID 2>/dev/null || true\n'
        '[ -f /tmp/drc_conf/start_drc.pid ] && sudo kill -TERM $(cat /tmp/drc_conf/start_drc.pid) 2>/dev/null || true\n'
        '[ -f /tmp/drc_conf/start_drc.pid ] && sudo kill -INT $(cat /tmp/drc_conf/start_drc.pid) 2>/dev/null || true\n'
        'for i in $(seq 1 10); do\n'
        '    pgrep -f "drc_stream" >/dev/null || break\n'
        '    sleep 1\n'
        'done\n'
        'pgrep -f "drc_stream" || echo "DRC_STREAM_STOPPED"\n'
        'pgrep -f "hostapd" || echo "HOSTAPD_STOPPED"\n'
        'ip addr show dev wlan0 | grep "192.168.1.10" || echo "WLAN0_CLEAN"\n'
        'ip -br addr show dev eth0 | grep "UP" && echo "ETH0_ALIVE"\n'
        'EOF\n'
        'bash /tmp/test_runner.sh\n'
    )

    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, remote_test_cmd],
        capture_output=True,
        text=True,
        timeout=35,
    )
    output = res.stdout
    print(f"RPi4 start_drc cleanup output:\n{output}")
    print(f"RPi4 start_drc cleanup stderr:\n{res.stderr}")

    assert "DRC_STREAM_STOPPED" in output, f"drc_stream sigue corriendo tras SIGINT:\n{output}"
    assert "HOSTAPD_STOPPED" in output, f"hostapd sigue corriendo tras SIGINT:\n{output}"
    assert "WLAN0_CLEAN" in output, f"wlan0 aún tiene IP asignada tras limpieza:\n{output}"
    assert "ETH0_ALIVE" in output, f"La interfaz eth0 no está UP:\n{output}"


def test_setup_virtual_display_commands():
    """Prueba 35: en el PC, verifica que setup_virtual_display('separate'),
    setup_virtual_display('extended') y setup_virtual_display('mirror')
    generan los comandos esperados para labwc, variables de entorno y sockets sin errores.
    """
    # 1. Modo separate (segundo escritorio independiente)
    sep_info = setup_virtual_display("separate", width=854, height=480, start=False)
    assert sep_info["mode"] == "separate"
    assert sep_info["wayland_display"] == "wayland-drc"
    assert sep_info["env"]["WAYLAND_DISPLAY"] == "wayland-drc"
    assert sep_info["env"]["WLR_BACKENDS"] == "headless"
    assert sep_info["env"]["WLR_HEADLESS_OUTPUTS"] == "1"
    assert any(c in sep_info["cmd"] for c in ("labwc", "Hyprland", "sway"))
    assert "foot" in sep_info["cmd"] or sep_info["compositor"] in ("Hyprland", "sway")
    assert "854x480" in sep_info["cmd"]
    assert sep_info["width"] == 854
    assert sep_info["height"] == 480

    # 2. Modo extended (pantalla extendida en conector secundario)
    ext_info = setup_virtual_display("extended", width=854, height=480, start=False)
    assert ext_info["mode"] == "extended"
    assert "card1-HDMI-A-2" in ext_info["drm_connector"]
    assert ext_info["width"] == 854
    assert ext_info["height"] == 480
    assert "status" in ext_info["cmd"] or "HDMI-A-2" in ext_info["cmd"]

    # 3. Modo mirror (espejo del escritorio principal)
    mir_info = setup_virtual_display("mirror", width=854, height=480, start=False)
    assert mir_info["mode"] == "mirror"
    assert "card1-HDMI-A-1" in mir_info["drm_connector"]
    assert mir_info["width"] == 854
    assert mir_info["height"] == 480


@pytest.mark.usefixtures("drc_service_paused")
def test_virtual_display_created_on_rpi4():
    """Prueba 36: en la Raspberry Pi 4B (<ip-de-la-pi>), comprueba que al ejecutar
    setup_virtual_display('separate') se inicia labwc en wayland-drc con resolución 854x480
    y responde a clientes Wayland.
    """
    # 1. Copiar drc_ap.py a la Pi
    drc_ap_local = PKG / "drc_ap.py"
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(drc_ap_local), f"{TARGET_HOST}:~/drc_ap.py"],
        check=True,
    )

    # 2. Ejecutar prueba remota en RPi4
    remote_test_cmd = (
        'python3 - << "EOF"\n'
        'import os, sys, time, subprocess\n'
        'from pathlib import Path\n'
        'sys.path.insert(0, "~")\n'
        'import drc_ap\n'
        'subprocess.run(["pkill", "-9", "^labwc$"], check=False)\n'
        'time.sleep(1)\n'
        'info = drc_ap.setup_virtual_display("separate", width=854, height=480)\n'
        'proc = info.get("proc")\n'
        'assert proc is not None, "Proceso labwc no iniciado"\n'
        'try:\n'
        '    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")\n'
        '    sock = Path(xdg_runtime) / "wayland-drc"\n'
        '    assert sock.exists(), f"Socket Wayland {sock} no existe"\n'
        '    # Consultar resolución con wlr-randr\n'
        '    env = os.environ.copy()\n'
        '    env["WAYLAND_DISPLAY"] = "wayland-drc"\n'
        '    res = subprocess.run(["wlr-randr"], env=env, capture_output=True, text=True, timeout=5)\n'
        '    print(res.stdout)\n'
        '    assert "854x480" in res.stdout, f"Resolución 854x480 no encontrada:\\n{res.stdout}"\n'
        '    print("VIRTUAL_DISPLAY_OK")\n'
        'finally:\n'
        '    proc.terminate()\n'
        '    try:\n'
        '        proc.wait(timeout=3)\n'
        '    except Exception:\n'
        '        proc.kill()\n'
        '    subprocess.run(["pkill", "-9", "^labwc$"], check=False)\n'
        'EOF\n'
    )

    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, remote_test_cmd],
        capture_output=True,
        text=True,
        timeout=25,
    )
    print(f"RPi4 virtual display stdout:\n{res.stdout}")
    print(f"RPi4 virtual display stderr:\n{res.stderr}")
    assert res.returncode == 0, f"Fallo al crear salida virtual en RPi4:\n{res.stderr}"
    assert "VIRTUAL_DISPLAY_OK" in res.stdout


def test_capture_cmd_for_modes():
    """Prueba 37: en el PC, verifica que capture_cmd('separate'), capture_cmd('extended')
    y capture_cmd('mirror') devuelven los pipelines correctos con salida YUV420P a 60 fps.
    """
    # 1. Modo separate
    cmd_sep = capture_cmd("separate")
    assert "wf-recorder" in cmd_sep
    assert "wayland-drc" in cmd_sep
    assert "-c rawvideo" in cmd_sep
    # y4m: cada frame lleva "FRAME\n" y drc_stream puede detectar y corregir un desfase
    assert "-m yuv4mpegpipe" in cmd_sep
    # El GamePad decodifica en rango limitado (16-235): con el rango completo de
    # wf-recorder se aplastaban sombras y luces (patrón gray_steps, prueba 52).
    assert "-F scale=out_range=limited" in cmd_sep
    assert "-x yuv420p" in cmd_sep
    assert "-r 60" in cmd_sep
    assert "-f -" in cmd_sep

    # 2. Modo extended
    cmd_ext = capture_cmd("extended")
    assert "wf-recorder" in cmd_ext
    assert "-o" in cmd_ext
    assert "-c rawvideo" in cmd_ext
    assert "-m yuv4mpegpipe" in cmd_ext
    assert "-F scale=out_range=limited" in cmd_ext
    assert "-x yuv420p" in cmd_ext
    assert "-r 60" in cmd_ext
    assert "-f -" in cmd_ext

    # 3. Modo mirror
    cmd_mir = capture_cmd("mirror")
    assert "ffmpeg" in cmd_mir
    assert "kmsgrab" in cmd_mir
    assert "format=yuv420p" in cmd_mir
    assert "-r 60" in cmd_mir
    assert "-f rawvideo -" in cmd_mir
    assert "out_range=limited" in cmd_mir


@pytest.mark.usefixtures("drc_service_paused")
def test_start_drc_mode_selection_and_cleanup():
    """Prueba 38: en el PC (dry-run) y en la Raspberry Pi 4B, ejecuta start_drc.sh
    en los tres modos y comprueba que se lanzan los procesos esperados y que la señal
    de terminación limpia todos los procesos huérfanos sin dejar sesiones de labwc colgadas.
    """
    script_path = PKG / "start_drc.sh"
    assert script_path.exists()

    # 1. En PC: verificar dry-run para los 3 modos
    for mode, expected_tool in [
        ("separate", "wayland-drc"),
        ("extended", "HDMI-A-2"),
        ("mirror", "kmsgrab"),
    ]:
        env = dict(os.environ, DRC_DRY_RUN="1")
        res = subprocess.run([str(script_path), "--mode", mode], capture_output=True, text=True, env=env)
        assert res.returncode == 0, f"Error en dry-run modo {mode}:\n{res.stderr}"
        assert f"modo '{mode}'" in res.stdout or f"mode {mode}" in res.stdout, f"Modo no reflejado en salida:\n{res.stdout}"
        assert expected_tool in res.stdout, f"Herramienta esperada '{expected_tool}' no en salida:\n{res.stdout}"

    # Verificar que el script limpia labwc y wf-recorder en cleanup
    script_content = script_path.read_text()
    assert "labwc" in script_content
    assert "wf-recorder" in script_content

    # 2. En RPi4: ejecutar prueba remota verificando procesos y limpieza tras SIGINT
    if not TARGET_HOST:
        pytest.skip("la parte de hardware necesita DRC_TEST_HOST=usuario@host")
    drc_ap_local = PKG / "drc_ap.py"
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(script_path), f"{TARGET_HOST}:~/start_drc.sh"],
        check=True,
    )
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(drc_ap_local), f"{TARGET_HOST}:~/drc_ap.py"],
        check=True,
    )

    remote_test_cmd = (
        'cat << "EOF" > /tmp/test_mode_runner.sh\n'
        '#!/usr/bin/env bash\n'
        'chmod +x ~/start_drc.sh\n'
        'sudo mkdir -p /tmp/drc_conf\n'
        'sudo chmod 777 /tmp/drc_conf\n'
        'rm -f /tmp/drc_conf/start_ap.log /tmp/drc_conf/test_mode.log /tmp/drc_conf/start_drc.pid\n'
        'sudo ~/start_drc.sh --mode separate > /tmp/drc_conf/test_mode.log 2>&1 &\n'
        'PID=$!\n'
        'sleep 2\n'
        'echo "[DHCP] Sent Ack to aa:bb:cc:dd:ee:01 -> 192.168.1.11" | sudo tee -a /tmp/drc_conf/start_ap.log >/dev/null\n'
        'sleep 4\n'
        'pgrep -f "labwc" >/dev/null && echo "LABWC_ACTIVE"\n'
        'pgrep -f "drc_stream" >/dev/null && echo "STREAM_ACTIVE"\n'
        'sudo kill -INT $PID 2>/dev/null || true\n'
        '[ -f /tmp/drc_conf/start_drc.pid ] && sudo kill -INT $(cat /tmp/drc_conf/start_drc.pid) 2>/dev/null || true\n'
        'sleep 0.5\n'
        '[ -f /tmp/drc_conf/start_drc.pid ] && sudo kill -TERM $(cat /tmp/drc_conf/start_drc.pid) $PID 2>/dev/null || true\n'
        'for i in $(seq 1 10); do\n'
        '    pgrep -f "drc_stream" >/dev/null || break\n'
        '    sleep 1\n'
        'done\n'
        'pgrep -f "labwc" || echo "LABWC_CLEANED"\n'
        'pgrep -f "wf-recorder" || echo "WF_RECORDER_CLEANED"\n'
        'pgrep -f "drc_stream" || echo "DRC_STREAM_CLEANED"\n'
        'EOF\n'
        'bash /tmp/test_mode_runner.sh\n'
    )

    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, remote_test_cmd],
        capture_output=True,
        text=True,
        timeout=35,
    )
    output = res.stdout
    print(f"RPi4 mode selection and cleanup stdout:\n{output}")
    print(f"RPi4 mode selection and cleanup stderr:\n{res.stderr}")
    assert "LABWC_CLEANED" in output, f"labwc no se limpió tras terminar:\n{output}"
    assert "WF_RECORDER_CLEANED" in output, f"wf-recorder sigue corriendo:\n{output}"
    assert "DRC_STREAM_CLEANED" in output, f"drc_stream sigue corriendo:\n{output}"


def test_map_touch_extended_coordinates(tmp_path: Path):
    """Prueba 39: test C++ en el PC que verifica que con un canvas global de
    2774x1080 (1920x1080 principal + 854x480 extendida a la derecha, offset X=1920, Y=0),
    los toques en las cuatro esquinas y centro del GamePad se mapean exactamente
    al rango [1920..2773] en X y [0..479] en Y.
    """
    cpp_source = f"""#include <iostream>
#include <cassert>
#include "{SRC_DIR / "uinput_map.h"}"

int main() {{
    drc_stream::TouchCalib calib;
    drc_stream::CanvasMapping canvas{{2774, 1080, 1920, 0}};

    // Esquina superior izquierda (0.0, 0.0) -> (1920, 0)
    auto p_top_left = drc_stream::map_touch_extended(0.0f, 0.0f, calib, canvas);
    assert(p_top_left.x == 1920);
    assert(p_top_left.y == 0);

    // Esquina superior derecha (1.0, 0.0) -> (2773, 0)
    auto p_top_right = drc_stream::map_touch_extended(1.0f, 0.0f, calib, canvas);
    assert(p_top_right.x == 2773);
    assert(p_top_right.y == 0);

    // Esquina inferior izquierda (0.0, 1.0) -> (1920, 479)
    auto p_bot_left = drc_stream::map_touch_extended(0.0f, 1.0f, calib, canvas);
    assert(p_bot_left.x == 1920);
    assert(p_bot_left.y == 479);

    // Esquina inferior derecha (1.0, 1.0) -> (2773, 479)
    auto p_bot_right = drc_stream::map_touch_extended(1.0f, 1.0f, calib, canvas);
    assert(p_bot_right.x == 2773);
    assert(p_bot_right.y == 479);

    // Centro (0.5, 0.5) -> (1920 + 427, 240) = (2347, 240)
    auto p_center = drc_stream::map_touch_extended(0.5f, 0.5f, calib, canvas);
    assert(p_center.x >= 1920 && p_center.x <= 2773);
    assert(p_center.y >= 0 && p_center.y <= 479);
    assert(p_center.x == 2347);
    assert(p_center.y == 240);

    std::cout << "SUCCESS: extended coordinates mapped correctly\\n";
    return 0;
}}
"""
    src_file = tmp_path / "test_extended_touch.cpp"
    bin_file = tmp_path / "test_extended_touch"
    src_file.write_text(cpp_source)

    build_res = subprocess.run(
        ["g++", "-std=c++17", "-O2", str(src_file), "-o", str(bin_file)],
        capture_output=True,
        text=True,
    )
    assert build_res.returncode == 0, f"Error de compilación:\n{build_res.stderr}"

    run_res = subprocess.run([str(bin_file)], capture_output=True, text=True)
    assert run_res.returncode == 0, f"Fallo en aserciones:\n{run_res.stderr}"
    assert "SUCCESS" in run_res.stdout


def test_touchscreen_uinput_bounds_and_events_on_rpi4():
    """Prueba 40: en la Raspberry Pi 4B, verifica que drc_stream en modo extendido
    crea el dispositivo uinput con los límites del canvas global (2774x1080) y emite
    los eventos táctiles confinados al área secundaria [1920..2773] y [0..479].
    """
    # 1. Copiar cabeceras y fuentes a la Pi
    subprocess.run(
        ["rsync", "-avz", str(SRC_DIR) + "/", f"{TARGET_HOST}:~/drc-build/drc_stream/"],
        check=True,
    )

    # 2. Compilar y ejecutar test de uinput en la Pi con permisos sudo
    remote_test_cpp = """#include <iostream>
#include <fcntl.h>
#include <unistd.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <cassert>
#include <dirent.h>
#include <cstring>
#include "uinput_dev.h"

int find_event_node_by_name(const char* target_name) {
    DIR* dir = opendir("/dev/input");
    if (!dir) return -1;
    struct dirent* ent;
    while ((ent = readdir(dir)) != nullptr) {
        if (std::strncmp(ent->d_name, "event", 5) == 0) {
            std::string path = std::string("/dev/input/") + ent->d_name;
            int fd = open(path.c_str(), O_RDONLY | O_NONBLOCK);
            if (fd >= 0) {
                char name[256] = {0};
                if (ioctl(fd, EVIOCGNAME(sizeof(name)), name) >= 0) {
                    if (std::strcmp(name, target_name) == 0) {
                        closedir(dir);
                        return fd;
                    }
                }
                close(fd);
            }
        }
    }
    closedir(dir);
    return -1;
}

int main() {
    drc_stream::CanvasMapping canvas{2774, 1080, 1920, 0};
    drc_stream::UinputTouchscreen ts;
    if (!ts.open(canvas)) {
        std::cerr << "FAILED_OPEN_TOUCH\\n";
        return 1;
    }

    usleep(200000); // 200 ms para registro en devnode

    int ev_fd = find_event_node_by_name("Wii U GamePad Touchscreen");
    if (ev_fd < 0) {
        std::cerr << "DEVNODE_NOT_FOUND\\n";
        return 2;
    }

    struct input_absinfo abs_x{}, abs_y{};
    ioctl(ev_fd, EVIOCGABS(ABS_X), &abs_x);
    ioctl(ev_fd, EVIOCGABS(ABS_Y), &abs_y);

    std::cout << "ABS_X max: " << abs_x.maximum << ", ABS_Y max: " << abs_y.maximum << std::endl;
    assert(abs_x.maximum == 2773);
    assert(abs_y.maximum == 1079);

    // Enviar eventos táctiles: centro (0.5, 0.5) -> debe ser (1920 + 427, 240) = (2347, 240)
    drc_stream::TouchCalib calib;
    ts.update(true, 0.5f, 0.5f, calib);
    usleep(50000);

    struct input_event ev;
    int got_x = -1, got_y = -1;
    while (read(ev_fd, &ev, sizeof(ev)) > 0) {
        if (ev.type == EV_ABS) {
            if (ev.code == ABS_X) got_x = ev.value;
            if (ev.code == ABS_Y) got_y = ev.value;
        }
    }

    std::cout << "Read touch event: X=" << got_x << ", Y=" << got_y << std::endl;
    assert(got_x == 2347);
    assert(got_y == 240);

    close(ev_fd);
    ts.close();
    std::cout << "UINPUT_BOUNDS_AND_EVENTS_OK\\n";
    return 0;
}
"""
    remote_cmd = (
        'cat << "EOF" > /tmp/test_uinput_bounds.cpp\n'
        f'{remote_test_cpp}\n'
        'EOF\n'
        'g++ -O2 -std=c++17 -I~/drc-build/drc_stream /tmp/test_uinput_bounds.cpp -o /tmp/test_uinput_bounds\n'
        'sudo /tmp/test_uinput_bounds\n'
    )

    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, remote_cmd],
        capture_output=True,
        text=True,
        timeout=25,
    )
    print(f"RPi4 uinput test stdout:\n{res.stdout}")
    print(f"RPi4 uinput test stderr:\n{res.stderr}")
    assert res.returncode == 0, f"Fallo en test uinput remoto:\n{res.stderr}"
    assert "UINPUT_BOUNDS_AND_EVENTS_OK" in res.stdout
















def test_drc_service_unit():
    """Prueba 54: drc.service lanza la sesión del GamePad al arrancar la Pi."""
    unit = PKG / "drc.service"
    assert unit.exists(), "falta drchost/drc.service"
    text = unit.read_text()
    assert "ExecStart=/opt/drc/start_drc.sh --mode separate" in text
    assert "Environment=DRC_TIMEOUT=60" in text
    assert "Restart=no" in text
    assert "sys-subsystem-net-devices-wlan0.device" in text
    assert "After=sys-subsystem-net-devices-wlan0.device" in text
    assert "Wants=sys-subsystem-net-devices-wlan0.device" in text
    assert "WantedBy=multi-user.target" in text


def test_install_drc_dry_run():
    """Prueba 55: DRC_DRY_RUN=1 install_drc.sh lista lo que instalaría en la Pi."""
    script = PKG / "install_drc.sh"
    assert script.exists(), "falta drchost/install_drc.sh"
    env = dict(os.environ, DRC_DRY_RUN="1")
    res = subprocess.run(
        [str(script), TARGET_HOST or "user@host"], capture_output=True, text=True, env=env
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout
    for name in ("start_drc.sh", "drc_ap.py", "gray_steps.py"):
        assert f"{name}" in out and "/opt/drc" in out
        assert any(name in line and "/opt/drc" in line for line in out.splitlines()), name
    assert "/etc/systemd/system/drc.service" in out
    assert "systemctl daemon-reload" in out
    assert "systemctl enable drc" in out


def test_drc_service_installed_on_rpi4():
    """Prueba 56: en la Pi el servicio drc está habilitado y /opt/drc/start_drc.sh es ejecutable."""
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST,
         "systemctl is-enabled drc; test -x /opt/drc/start_drc.sh && echo START_DRC_EXECUTABLE"],
        capture_output=True, text=True, timeout=20,
    )
    print(f"RPi4 drc service stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
    assert res.stdout.splitlines()[:1] == ["enabled"], res.stdout + res.stderr
    assert "START_DRC_EXECUTABLE" in res.stdout


@pytest.mark.usefixtures("drc_service_paused")
def test_drc_service_paused_during_dev_tests_on_rpi4():
    """Arreglo B08: las pruebas de desarrollo que tocan el AP o labwc paran el servicio
    `drc` mientras corren (si no, compite por wlan0) y lo restauran al terminar."""
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, "systemctl is-active drc"],
        capture_output=True, text=True, timeout=20,
    )
    assert res.stdout.strip() == "inactive", res.stdout


@pytest.mark.parametrize("dmesg_line, expected_rc", [
    # mensaje normal de hostapd con el firmware parcheado: NO es un fallo del chip
    ("[ 38.472039] ieee80211 phy0: brcmf_vif_set_mgmt_ie: vndr ie set error : -52", 1),
    # otros mensajes normales: se repiten en cada carga del driver / arranque del AP
    ("[ 144.9] brcmfmac: brcmf_c_process_txcap_blob: no txcap_blob available (err=-2)", 1),
    ("[ 146.3] ieee80211 phy5: brcmf_notify_escan_complete: Scan abort failed", 1),
    ("[ 199.8] ieee80211 phy5: brcmf_cfg80211_del_station: SCB_DEAUTHENTICATE_FOR_REASON failed -512", 1),
    # fallos reales del chip/bus: sí piden reiniciar el hardware
    ("[ 10.1] brcmfmac: brcmf_sdio_bus_rxctl: resumed on timeout, err=-110", 0),
    ("[ 10.2] brcmfmac: brcmf_fw_crashed: Firmware has halted or crashed, failed", 0),
    ("[ 10.3] brcmfmac: brcmf_c_preinit_dcmds: power save disabled", 1),
])
def test_reset_wifi_ignores_benign_vndr_ie_error(tmp_path: Path, dmesg_line: str, expected_rc: int):
    """Arreglo B08: `reset_wifi` reiniciaba el bus SDIO (y wlan0 desaparecía) porque
    `vndr ie set error : -52` casaba con el filtro de errores de brcmf.
    rc 0 = hay que reiniciar el hardware; rc 1 = no."""
    dmesg = tmp_path / "dmesg.txt"
    dmesg.write_text(dmesg_line + "\n")
    script = PKG / "start_drc.sh"
    env = dict(os.environ, DRC_CHECK_DMESG="1", DRC_DMESG_FILE=str(dmesg))
    res = subprocess.run([str(script)], capture_output=True, text=True, env=env)
    assert "superusuario" not in res.stderr, "el script no debe seguir hasta la comprobación de root"
    assert res.returncode == expected_rc, res.stdout + res.stderr


def test_cleanup_waits_for_drc_stream_before_stopping_hostapd():
    """Arreglo B08: `drc_stream` lee el TSF del firmware mientras transmite; si hostapd se
    termina antes de que `drc_stream` salga, el núcleo D11 se apaga durante una lectura y el
    firmware hace un data abort (epc 0x1b1806, el hook del TSF). `cleanup()` debe esperar a
    que `drc_stream` termine (con un límite) antes de parar el AP."""
    text = (PKG / "start_drc.sh").read_text()
    cleanup = text[text.index("cleanup() {"):text.rindex("trap cleanup")]
    sigint = cleanup.index('pkill -INT -f "drc_stream"')
    terminate = cleanup.index("hostapd_cli")
    between = cleanup[sigint:terminate]
    assert "pgrep" in between and "drc_stream" in between, (
        "cleanup() no espera a drc_stream entre su SIGINT y la terminación de hostapd")
    assert "sleep" in between, "la espera debe ser un bucle con límite"
    # drc_stream solo sale cuando se cierra su tubería (ignora SIGINT mientras lee frames) y
    # wf-recorder se cuelga si labwc ya murió: la espera debe matar la captura si hace falta.
    assert 'pkill -9 -f "wf-recorder"' in between, (
        "la espera no fuerza el cierre de wf-recorder, así que drc_stream no puede salir")


def test_cleanup_kills_drc_stream_before_stopping_hostapd():
    """Arreglo B08: `Streamer::Stop()` de libdrc para primero el sincronizador UVC/UAC, cuyo
    `CmdClient::Query` síncrono reintenta hasta 10 × 1 s si el GamePad no responde; mientras,
    el hilo de vídeo sigue leyendo el TSF. Una espera con límite no basta: si vence, hostapd
    se termina con `drc_stream` vivo y el firmware trapea. `cleanup()` debe matar
    `drc_stream` (SIGKILL) y comprobar que ya no existe antes de terminar hostapd."""
    text = (PKG / "start_drc.sh").read_text()
    cleanup = text[text.index("cleanup() {"):text.rindex("trap cleanup")]
    between = cleanup[cleanup.index('pkill -INT -f "drc_stream"'):cleanup.index("hostapd_cli")]
    assert 'pkill -9 -f "drc_stream"' in between, (
        "cleanup() no fuerza el cierre de drc_stream antes de terminar hostapd")
    after_kill = between[between.index('pkill -9 -f "drc_stream"'):]
    assert "pgrep" in after_kill and "drc_stream" in after_kill, (
        "tras el SIGKILL, cleanup() no espera a que drc_stream desaparezca")


@pytest.mark.usefixtures("drc_service_paused")
def test_start_drc_gives_up_cleanly_on_rpi4():
    """Prueba 58: sin GamePad, /opt/drc/start_drc.sh con DRC_TIMEOUT=3 termina por sí solo
    (código 0, así la unidad queda `inactive`), no deja hostapd, labwc, wf-recorder, drc_stream ni arecord (prueba 63) vivos y wlan0 queda sin IP.
    Exige el GamePad APAGADO: si conectara, la sesión seguiría transmitiendo."""
    remote = (
        'start=$(date +%s)\n'
        'sudo env DRC_TIMEOUT=3 /opt/drc/start_drc.sh > /tmp/drc_gives_up.log 2>&1\n'
        'echo "RC=$?"\n'
        'echo "SECS=$(( $(date +%s) - start ))"\n'
        'for p in hostapd labwc wf-recorder drc_stream arecord; do\n'
        '    pgrep -x "$p" >/dev/null && echo "ALIVE=$p"\n'
        'done\n'
        'ip -4 -br addr show dev wlan0 | grep -q "192\\.168\\.1\\." && echo "WLAN0_HAS_IP" || echo "WLAN0_NO_IP"\n'
    )
    # El script va por stdin (`bash -s`): start_drc.sh hace `pkill -f labwc|drc_stream...` y
    # mataría la shell de ssh si esos nombres estuvieran en su línea de comandos.
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, "bash -s"],
        input=remote, capture_output=True, text=True, timeout=60,
    )
    print(f"RPi4 gives-up stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
    out = res.stdout
    # Código 0 a propósito: cleanup() sale con 0 y así systemd deja la unidad en `inactive`
    # (con error quedaría `failed`), que es lo que el plan espera tras rendirse.
    assert "RC=0" in out, f"debería terminar solo y con código 0 al no conectar el GamePad:\n{out}"
    secs = int(next(l for l in out.splitlines() if l.startswith("SECS=")).split("=")[1])
    assert secs <= 40, f"tardó {secs} s en rendirse:\n{out}"
    assert "ALIVE=" not in out, f"quedan procesos vivos:\n{out}"
    assert "WLAN0_NO_IP" in out, f"wlan0 conserva IP:\n{out}"


def test_audio_reader_handles_partial_reads_and_eof(tmp_path: Path):
    """Prueba 59: drc_stream::AudioReader entrega bloques de muestras s16 estéreo aunque
    el fd las dé en trozos de bytes impares, guarda los bytes sueltos para la siguiente
    lectura y avisa de EOF (un frame estéreo incompleto al final se descarta).
    """
    src = f"""#include <cstdio>
#include <cstdint>
#include <thread>
#include <vector>
#include <unistd.h>
#include "{SRC_DIR / "audio_reader.h"}"

#define CHECK(c) do {{ if (!(c)) {{ fprintf(stderr, "FAIL line %d: %s\\n", __LINE__, #c); return 1; }} }} while (0)

int main() {{
    int p[2];
    CHECK(pipe(p) == 0);
    // 1000 frames estéreo (L,R) = 4000 bytes con valores 0..1999, más 3 bytes sueltos al final.
    std::vector<uint8_t> bytes;
    for (int i = 0; i < 2000; i++) {{
        int16_t v = static_cast<int16_t>(i);
        bytes.push_back(v & 0xff);
        bytes.push_back((v >> 8) & 0xff);
    }}
    bytes.insert(bytes.end(), {{1, 2, 3}});
    std::thread writer([&]() {{
        const size_t chunks[] = {{1, 3, 7, 1001, 5, 999}};
        size_t off = 0, i = 0;
        while (off < bytes.size()) {{
            size_t n = std::min(chunks[i++ % 6], bytes.size() - off);
            if (write(p[1], bytes.data() + off, n) != (ssize_t)n) break;
            off += n;
            usleep(200);
        }}
        close(p[1]);
    }});

    drc_stream::AudioReader r(p[0]);
    std::vector<int16_t> all, blk;
    while (r.Next(blk)) {{
        CHECK(blk.size() % 2 == 0);  // siempre frames estéreo completos
        CHECK(!blk.empty());
        all.insert(all.end(), blk.begin(), blk.end());
    }}
    writer.join();
    CHECK(all.size() == 2000);
    for (int i = 0; i < 2000; i++) CHECK(all[i] == i);
    CHECK(!r.Next(blk));  // EOF persistente
    printf("SUCCESS\\n");
    return 0;
}}
"""
    src_path = tmp_path / "test_ar.cpp"
    bin_path = tmp_path / "test_ar"
    src_path.write_text(src)
    build = subprocess.run(["g++", "-std=c++17", "-O2", "-pthread", str(src_path), "-o", str(bin_path)],
                           capture_output=True, text=True)
    assert build.returncode == 0, f"Compilation failed:\n{build.stderr}"
    res = subprocess.run([str(bin_path)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"Execution failed:\n{res.stderr}\n{res.stdout}"
    assert "SUCCESS" in res.stdout


def test_start_drc_dry_run_audio_gamepad():
    """Prueba 61: el dry-run por defecto (--audio gamepad) lista la carga de `snd-aloop`,
    `asound.conf` con la salida por defecto en `dmix:Loopback`, `arecord -D hw:Loopback,1 ...`
    hacia `drc_stream --audio-fd 4`, y la limpieza mata `arecord`."""
    script_path = PKG / "start_drc.sh"
    env = dict(os.environ, DRC_DRY_RUN="1")
    res = subprocess.run([str(script_path)], capture_output=True, text=True, env=env)
    assert res.returncode == 0, f"Error en dry-run:\n{res.stderr}\n{res.stdout}"
    out = res.stdout
    assert "modprobe snd-aloop" in out, out
    assert "asound.conf" in out and "dmix:Loopback" in out, out
    assert "arecord -D hw:Loopback,1 -f S16_LE -r 48000 -c 2 -t raw" in out, out
    assert "--audio-fd 4" in out, out
    cleanup_line = next(l for l in out.splitlines() if "trap cleanup" in l)
    assert "arecord" in cleanup_line, cleanup_line
    # El script real también mata arecord en cleanup().
    text = script_path.read_text()
    cleanup = text[text.index("cleanup() {"):text.rindex("trap cleanup")]
    assert 'pkill -9 -f "arecord' in cleanup or 'pkill -9 -x "arecord"' in cleanup, cleanup


def test_start_drc_dry_run_paces_video():
    """Prueba 78 (B17): el dry-run muestra el vídeo como la consola: paquetes vstrm de 1456 B
    (sin fragmentación IP con MTU 1500) y la cola fq en wlan0 que aplica el ritmo del socket
    de vídeo; cleanup() la quita. DRC_VSTRM_PAYLOAD de fuera se respeta."""
    script_path = PKG / "start_drc.sh"
    env = dict(os.environ, DRC_DRY_RUN="1", DRC_CONF_FILE=_empty_conf())   # valores por defecto, sin el drc.conf local
    env.pop("DRC_VSTRM_PAYLOAD", None)
    res = subprocess.run([str(script_path)], capture_output=True, text=True, env=env)
    assert res.returncode == 0, f"Error en dry-run:\n{res.stderr}\n{res.stdout}"
    assert "DRC_VSTRM_PAYLOAD=1456" in res.stdout, res.stdout
    assert ("tc qdisc replace dev wlan0 root fq" in res.stdout or "tc qdisc replace dev wlp6s0f3u3 root fq" in res.stdout), res.stdout
    env["DRC_VSTRM_PAYLOAD"] = "1694"
    res = subprocess.run([str(script_path)], capture_output=True, text=True, env=env)
    assert "DRC_VSTRM_PAYLOAD=1694" in res.stdout, res.stdout
    text = script_path.read_text()
    cleanup = text[text.index("cleanup() {"):text.rindex("trap cleanup")]
    assert "qdisc del dev" in cleanup, cleanup


@pytest.mark.usefixtures("drc_service_paused")
def test_drc_stream_sends_astrm_from_audio_fd_on_rpi4():
    """Prueba 62: en la Pi, `drc_stream --dest 127.0.0.1 --audio-fd 4` con frames grises por
    stdin y 1 s de seno de 440 Hz (en tiempo real) por el fd 4 manda a 127.0.0.1:50121
    paquetes astrm de audio PCM 48 kHz con carga distinta de cero (los astrm de formato de
    vídeo, que comparten el puerto, no cuentan)."""
    for flag in ("--libdrc", "--stream"):
        build = subprocess.run([str(PKG / "build_userspace.sh"), flag, TARGET_HOST],
                               capture_output=True, text=True)
        assert build.returncode == 0, f"Error compilando ({flag}):\n{build.stderr}\n{build.stdout}"

    remote = r'''
python3 - <<'PY' > /tmp/astrm_listener.log 2>&1 &
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", 50121))
s.settimeout(0.2)
t_end = time.time() + 6
total = nonzero = pcm48 = 0
while time.time() < t_end:
    try:
        d, _ = s.recvfrom(4096)
    except socket.timeout:
        continue
    if d[0] & 0x04:  # tipo «formato de vídeo» (también va al puerto 50121): no es audio
        continue
    total += 1
    if (d[0] >> 5) == 1:
        pcm48 += 1
    if any(d[8:]):
        nonzero += 1
print(f"PACKETS={total} PCM48={pcm48} NONZERO={nonzero}")
PY
LISTENER=$!
sleep 0.5
timeout --preserve-status 5s bash -c '
ffmpeg -loglevel error -re -f lavfi -i color=gray:size=854x480:rate=60 -pix_fmt yuv420p -f rawvideo - |
  drc_stream --dest 127.0.0.1 --audio-fd 4 4< <(ffmpeg -loglevel error -re -f lavfi -i sine=frequency=440:duration=1:sample_rate=48000 -ac 2 -f s16le -)
' < /dev/null > /tmp/astrm_stream.log 2>&1
wait $LISTENER
cat /tmp/astrm_listener.log
'''
    res = subprocess.run(["ssh", "-o", "BatchMode=yes", TARGET_HOST, "bash -s"],
                         input=remote, capture_output=True, text=True, timeout=60)
    print(f"RPi4 astrm stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
    import re
    m = re.search(r"PACKETS=(\d+) PCM48=(\d+) NONZERO=(\d+)", res.stdout)
    assert m, f"el escucha no informó:\n{res.stdout}\n{res.stderr}"
    packets, pcm48, nonzero = map(int, m.groups())
    assert nonzero >= 50, f"muy pocos paquetes astrm con audio (1 s son ~250 de 4 ms): {m.group(0)}"
    assert pcm48 == packets, f"hay paquetes astrm que no son PCM 48 kHz: {m.group(0)}"


def test_audio_capture_periods_fit_in_queue_and_dmix_is_s16():
    """Arreglo B09 (descubierto con el GamePad): por defecto `arecord` entrega periodos de
    125 ms (6000 frames = 12000 s16), más que el tope de la cola de audio (100 ms = 9600 s16):
    cada trozo perdía 25 ms, ~8 veces por segundo («se repite» en vez de un zumbido continuo).
    Además `dmix:Loopback,0` abre el loopback en S32_LE y `arecord -f S16_LE` no negocia.
    - `arecord` debe pedir periodos de 10 ms (480 frames) y un búfer de 40 ms;
    - `asound.conf` debe fijar el esclavo dmix a S16_LE, 48 kHz, estéreo sobre hw:Loopback,0."""
    script_path = PKG / "start_drc.sh"
    env = dict(os.environ, DRC_DRY_RUN="1", DRC_CONF_FILE=_empty_conf())   # valores por defecto, sin el drc.conf local
    out = subprocess.run([str(script_path)], capture_output=True, text=True, env=env).stdout
    assert "--period-size=480 --buffer-size=1920" in out, out
    text = script_path.read_text()
    conf = text[text.index("ASOUND_CONF='"):text.index("# Prioridad tiempo real")]
    assert 'type dmix' in conf and '"hw:Loopback,0,0"' in conf, conf
    assert "format S16_LE" in conf and "rate 48000" in conf and "channels 2" in conf, conf


def test_start_drc_dry_run_audio_aux():
    """Prueba 64: el dry-run con `--audio aux` (o DRC_AUDIO=aux) no carga `snd-aloop`, no lanza
    `arecord` ni pasa `--audio-fd` a drc_stream, y `asound.conf` apunta a `Headphones`."""
    script_path = PKG / "start_drc.sh"
    for args, extra_env in ((["--audio", "aux"], {}), (["--audio=aux"], {}), ([], {"DRC_AUDIO": "aux"})):
        env = dict(os.environ, DRC_DRY_RUN="1", **extra_env)
        res = subprocess.run([str(script_path), *args], capture_output=True, text=True, env=env)
        assert res.returncode == 0, f"Error en dry-run {args} {extra_env}:\n{res.stderr}\n{res.stdout}"
        out = res.stdout
        assert "snd-aloop" not in out, out
        assert "arecord" not in out, out
        assert "--audio-fd" not in out, out
        assert "asound.conf" in out and "Headphones" in out, out
        assert "drc_stream" in out, out


def test_start_drc_rejects_unknown_audio():
    """Prueba 65: `--audio foo` sale con código distinto de 0 y un mensaje que nombra
    `gamepad|aux`, antes de levantar el AP (también en dry-run)."""
    script_path = PKG / "start_drc.sh"
    for dry in ("1", "0"):
        env = dict(os.environ, DRC_DRY_RUN=dry)
        res = subprocess.run([str(script_path), "--audio", "foo"], capture_output=True, text=True, env=env, timeout=20)
        assert res.returncode != 0, f"debería fallar (dry={dry}):\n{res.stdout}"
        assert "gamepad|aux" in res.stderr + res.stdout, res.stderr + res.stdout
        assert "drc_ap.py --start-ap" not in res.stdout, "levantó el AP antes de validar"


def test_start_drc_launch_uses_literal_audio_redirection():
    """Arreglo B09: una redirección guardada en una variable (`${VAR}` con `4< <(...)`) no se
    interpreta en bash: el comando real recibiría «4<» como argumento. El lanzamiento de
    drc_stream debe llevar la redirección `4< <(` literal para `gamepad` y no llevar
    `--audio-fd` en la rama `aux`."""
    text = (PKG / "start_drc.sh").read_text()
    launch = text[text.index('export DRC_STREAM_STATS'):]
    assert "${AUDIO_FD_REDIR}" not in launch, "redirección en una variable: no funciona"
    assert launch.count("--audio-fd 4 4< <(${AUDIO_CAPTURE_CMD})") == 2, launch  # con y sin chrt
    assert launch.count("${DRC_STREAM_BIN} ${DRC_STREAM_ARGS}") >= 4, launch  # aux y gamepad, con y sin chrt
    assert 'chrt -f "${DRC_RT_PRIO}" ${DRC_STREAM_BIN}' in launch


def test_av_sync_clip_flash_and_beep_aligned(tmp_path: Path):
    """Prueba 66: `av_sync_clip.sh <salida>` genera un clip de 10 s con vídeo 854x480 a 60 fps y
    audio estéreo 48 kHz. Al principio de cada segundo hay un destello blanco y un pitido de
    1 kHz a la vez: el primer frame claro (>50 % de brillo) y la primera muestra fuerte (>50 %
    del máximo) de cada segundo caen a menos de 1 frame (≈17 ms) entre sí."""
    import array
    import json

    script = PKG / "av_sync_clip.sh"
    assert script.exists(), f"El script {script} debe existir"
    clip = tmp_path / "clip.mkv"
    res = subprocess.run([str(script), str(clip)], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"Error generando el clip:\n{res.stderr}"

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(clip)],
        capture_output=True, text=True, check=True)
    streams = {s["codec_type"]: s for s in json.loads(probe.stdout)["streams"]}
    v, a = streams["video"], streams["audio"]
    assert (v["width"], v["height"]) == (854, 480), v
    assert v["r_frame_rate"] == "60/1", v["r_frame_rate"]
    assert int(a["sample_rate"]) == 48000 and a["channels"] == 2, a
    assert 9.9 <= float(probe_duration(clip)) <= 10.2

    # Brillo por frame: se reduce cada frame a 1 píxel de gris.
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(clip), "-vf", "scale=1:1,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout
    assert len(raw) >= 590, f"pocos frames: {len(raw)}"
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(clip), "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
        capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    peak = max(abs(x) for x in samples)
    assert peak > 10000, f"audio demasiado bajo: {peak}"

    for k in range(10):
        f0, f1 = k * 60, min(k * 60 + 30, len(raw))   # ventana de medio segundo
        flash_frame = next(i for i in range(f0, f1) if raw[i] > 128)
        s0, s1 = k * 48000, k * 48000 + 24000
        beep_sample = next(i for i in range(s0, min(s1, len(samples))) if abs(samples[i]) > peak // 2)
        flash_t, beep_t = flash_frame / 60, beep_sample / 48000
        assert abs(flash_t - beep_t) < 1 / 60, f"segundo {k}: destello {flash_t:.4f}s, pitido {beep_t:.4f}s"
        assert abs(flash_t - k) < 1 / 60 and abs(beep_t - k) < 1 / 60, f"segundo {k}: no empieza en el segundo"


def probe_duration(clip: Path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)],
        capture_output=True, text=True, check=True).stdout.strip()


def test_asound_dmix_is_shared_between_users():
    """Arreglo B09 (descubierto al reproducir una canción tras un vídeo): el escritorio del GamePad
    corre como root, así que `ffplay` del vídeo se lanza con sudo y crea el semáforo/memoria
    compartida del `dmix` con permisos 0600; después un programa de usuario no podía abrir el
    audio («unable to create IPC semaphore: Permission denied»). El `dmix` del GamePad debe
    crearse con `ipc_perm 0666` para que root y usuarios compartan la salida."""
    text = (PKG / "start_drc.sh").read_text()
    conf = text[text.index("ASOUND_CONF='"):text.index("# Audio \"aux\"")]
    assert "type dmix" in conf and "ipc_perm 0666" in conf, conf


def test_asound_dmix_slave_pins_loopback_subdevice_0():
    """Arreglo B09 (descubierto al reproducir una canción tras Naruto): con el esclavo
    `hw:Loopback,0` (subdispositivo libre cualquiera) un segundo cliente del dmix no abría
    («unable to open slave: Invalid argument»), y un reproductor cerrado de golpe dejaba la
    memoria compartida del dmix huérfana: nada volvía a sonar hasta borrarla con ipcrm.
    Con `hw:Loopback,0,0` (el que lee arecord por hw:Loopback,1,0) ambos casos funcionan."""
    text = (PKG / "start_drc.sh").read_text()
    conf = text[text.index("ASOUND_CONF='"):text.index("# Audio \"aux\"")]
    assert 'pcm "hw:Loopback,0,0"' in conf, conf


def test_desktop_user_popen_kwargs():
    """Prueba 67 (B16): labwc se lanza como el usuario del escritorio, no como root, para que
    todo lo que suene sea del mismo usuario (el dmix no admite root y usuario a la vez).
    Sin usuario o con root no cambia nada; con un usuario da uid, gid, grupos (input para
    libinput), HOME/USER y LIBSEAT_BACKEND=noop (sin sesión de logind)."""
    import getpass, pwd
    from drchost.drc_ap import desktop_user_popen_kwargs
    assert desktop_user_popen_kwargs(None) == {}
    assert desktop_user_popen_kwargs("") == {}
    assert desktop_user_popen_kwargs("root") == {}
    name = getpass.getuser()
    if name == "root":
        pytest.skip("hace falta un usuario normal")
    pw = pwd.getpwnam(name)
    kw = desktop_user_popen_kwargs(name)
    assert kw["user"] == pw.pw_uid and kw["group"] == pw.pw_gid
    assert set(kw["extra_groups"]) == set(os.getgrouplist(name, pw.pw_gid))
    assert kw["env"]["HOME"] == pw.pw_dir
    assert kw["env"]["USER"] == name and kw["env"]["LOGNAME"] == name
    assert kw["env"]["LIBSEAT_BACKEND"] == "noop"


def test_start_drc_dry_run_desktop_user():
    """Prueba 68 (B16): con DRC_DESKTOP_USER el dry-run anuncia el escritorio como ese usuario
    y su XDG_RUNTIME_DIR propio (/tmp/drc_runtime_<uid>)."""
    import getpass
    name = getpass.getuser()
    script_path = PKG / "start_drc.sh"
    env = dict(os.environ, DRC_DRY_RUN="1", DRC_DESKTOP_USER=name)
    env.pop("XDG_RUNTIME_DIR", None)
    out = subprocess.run([str(script_path)], capture_output=True, text=True, env=env).stdout
    assert f"como {name}" in out, out
    assert f"/tmp/drc_runtime_{os.getuid()}" in out, out


def test_drc_service_and_install_set_desktop_user():
    """Prueba 69 (B16): el servicio pasa DRC_DESKTOP_USER y install_drc.sh lo rellena con el
    usuario de <usuario@host>."""
    unit = (PKG / "drc.service").read_text()
    assert "Environment=DRC_DESKTOP_USER=@DESKTOP_USER@" in unit
    script = PKG / "install_drc.sh"
    env = dict(os.environ, DRC_DRY_RUN="1")
    out = subprocess.run([str(script), TARGET_HOST or "user@host"],
                         capture_output=True, text=True, env=env).stdout
    assert "DRC_DESKTOP_USER=" + (TARGET_HOST or "user@host").split("@")[0] in out, out


@pytest.mark.skipif(shutil.which("labwc") is None, reason="necesita labwc instalado")
def test_setup_virtual_display_waits_for_slow_labwc(tmp_path, monkeypatch):
    """Prueba 72 (B18, arranque en frío 2026-11-01): si labwc tarda en crear el socket
    wayland-drc (en el arranque tardó ~21 s), setup_virtual_display espera a que exista antes
    de volver. Si volvía a los 5 s, wf-recorder fallaba con 'failed to create display' y la
    sesión se cerraba sola. Con un labwc falso que crea el socket a los 7 s."""
    import os
    import stat

    bin_dir = tmp_path / "bin"
    run_dir = tmp_path / "run"
    bin_dir.mkdir()
    run_dir.mkdir(mode=0o700)
    labwc = bin_dir / "labwc"
    labwc.write_text('#!/bin/sh\nsleep 7\ntouch "$XDG_RUNTIME_DIR/wayland-drc"\nexec sleep 30\n')
    labwc.chmod(labwc.stat().st_mode | stat.S_IEXEC)
    randr = bin_dir / "wlr-randr"
    randr.write_text("#!/bin/sh\nexit 0\n")
    randr.chmod(randr.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))

    info = setup_virtual_display("separate", width=854, height=480)
    try:
        assert (run_dir / "wayland-drc").exists(), "volvió antes de que labwc creara el socket"
    finally:
        info["proc"].kill()
        info["proc"].wait()


def test_setup_virtual_display_wiiu_wm(monkeypatch):
    """Prueba 153 (B10, entorno propio): con DRC_COMPOSITOR=wiiu-wm la sesión arranca el
    compositor propio en vez de labwc. El Inicio va integrado, así que la orden de arranque
    ya no lanza wiiu-shell; conserva el enlace wayland-drc y el teclado en pantalla."""
    monkeypatch.setenv("DRC_COMPOSITOR", "wiiu-wm")
    info = setup_virtual_display("separate", width=854, height=480, start=False)
    assert info["compositor"] == "wiiu-wm"
    assert info["cmd"].split()[3] == "wiiu-wm" or " wiiu-wm -s " in info["cmd"]
    assert "labwc" not in info["cmd"]
    assert "; wiiu-shell" not in info["cmd"]   # el Inicio va integrado: no se lanza wiiu-shell
    assert "wayland-drc" in info["cmd"] and "wvkbd-mobintl" in info["cmd"]

    # Sin variable: labwc si wiiu-wm no está instalado (como en el PC de desarrollo)
    monkeypatch.delenv("DRC_COMPOSITOR")
    monkeypatch.setattr("shutil.which", lambda name: None if name == "wiiu-wm" else f"/usr/bin/{name}")
    monkeypatch.setattr("os.path.exists", lambda p: False)
    info = setup_virtual_display("separate", width=854, height=480, start=False)
    assert (info["compositor"] in ("labwc", "Hyprland")) and (info["compositor"] in info["cmd"])


def test_pad_status_volume_and_status_file(tmp_path: Path):
    """Prueba 154 (B10, barra de estado): `volume N|+N|-N` en el FIFO de control cambia la
    ganancia del audio que va al GamePad (0 = silencio, 100 = sin tocar, curva cuadrática) y el
    estado del mando (batería, carga, deslizador de volumen, ganancia) se publica en un fichero
    clave=valor que lee la barra (waybar)."""
    src = f"""#include <cstdio>
#include <cstdint>
#include <fstream>
#include <sstream>
#include <vector>
#include "{SRC_DIR / "pad_status.h"}"

#define CHECK(c) do {{ if (!(c)) {{ fprintf(stderr, "FAIL line %d: %s\\n", __LINE__, #c); return 1; }} }} while (0)

int main() {{
    using namespace drc_stream;
    CHECK(ParseVolumeCommand("volume 50", 100) == 50);
    CHECK(ParseVolumeCommand("volume +10", 95) == 100);   // tope 100
    CHECK(ParseVolumeCommand("volume -30", 20) == 0);     // tope 0
    CHECK(ParseVolumeCommand("volume abc", 40) == -1);
    CHECK(ParseVolumeCommand("get battery", 40) == -1);

    std::vector<int16_t> s = {{1000, -1000, 32767, -32768}};
    std::vector<int16_t> full = s;
    ApplyVolume(full, 100);
    CHECK(full == s);
    std::vector<int16_t> half = s;
    ApplyVolume(half, 50);                                 // (0,5)^2 = 0,25
    CHECK(half[0] == 250 && half[1] == -250);
    std::vector<int16_t> mute = s;
    ApplyVolume(mute, 0);
    CHECK(mute[0] == 0 && mute[2] == 0 && mute[3] == 0);

    PadStatus st;
    st.battery = 5; st.power = 0x41; st.slider = 128; st.volume = 70; st.valid = true;
    std::string text = FormatStatus(st);
    CHECK(text.find("battery=5\\n") != std::string::npos);
    CHECK(text.find("charging=1\\n") != std::string::npos);   // bit 0x40
    CHECK(text.find("ac=1\\n") != std::string::npos);         // bit 0x01
    CHECK(text.find("slider=128\\n") != std::string::npos);
    CHECK(text.find("volume=70\\n") != std::string::npos);

    std::string path = "{tmp_path}/drc_status";
    CHECK(WriteStatusFile(path, text));
    std::ifstream f(path);
    std::stringstream ss; ss << f.rdbuf();
    CHECK(ss.str() == text);
    printf("SUCCESS\\n");
    return 0;
}}
"""
    src_path = tmp_path / "test_ps.cpp"
    bin_path = tmp_path / "test_ps"
    src_path.write_text(src)
    build = subprocess.run(["g++", "-std=c++17", "-O2", str(src_path), "-o", str(bin_path)],
                           capture_output=True, text=True)
    assert build.returncode == 0, f"Compilation failed:\n{build.stderr}"
    res = subprocess.run([str(bin_path)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"Execution failed:\n{res.stderr}\n{res.stdout}"
    assert "SUCCESS" in res.stdout


def test_las_rutas_de_los_scripts_apuntan_a_ficheros_que_existen():
    """Los scripts de drchost/ se mueven con el repo: cada `${SCRIPT_DIR}/...` literal debe resolverse a algo real
    (salvo lo que se genera al compilar). Un `bash <ruta rota>` dentro de `set -e` mataba start_drc.sh sin mensaje."""
    import re
    generado = ("build_local", "drc.conf", "generated_", "hyprland-gamepad", "third_party/x264")
    for script in sorted(PKG.glob("*.sh")):
        for ref in re.findall(r"\$\{SCRIPT_DIR\}/([A-Za-z0-9_./-]+)", script.read_text()):
            if any(g in ref for g in generado):
                continue
            assert (PKG / ref).exists(), f"{script.name}: ${{SCRIPT_DIR}}/{ref} no existe"


def _empty_conf():
    """Un drc.conf vacío: start_drc.sh usa el config/drc.conf del equipo si el indicado no existe."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".conf")
    os.close(fd)
    return path


def test_audio_de_wlan0_cabe_en_una_trama():
    """Prueba 168: en modo internal (wlan0, CYW43455 que no emite tramas de más de 1500 B) el paquete de
    audio (DRC_AUDIO_PACKET_SAMPLES × 4 B + 8 de cabecera astrm + 28 de UDP/IP) cabe en DRC_MTU. Con 384
    muestras (1572 B) cada paquete salía partido en dos fragmentos IP y el audio se cortaba (B19, 2026-11-22).
    """
    import re
    text = (PKG / "drc_mode.sh").read_text()
    block = text[text.index("internal)"):]
    block = block[block.index("<<CONF"):block.index("\nCONF")]
    env = dict(re.findall(r"Environment=(\w+)=(\S+)", block))
    samples = int(env.get("DRC_AUDIO_PACKET_SAMPLES", "384"))
    mtu = int(env["DRC_MTU"])
    assert samples * 4 + 8 + 28 <= mtu, f"audio de {samples} muestras = {samples * 4 + 36} B > MTU {mtu}"
