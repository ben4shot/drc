from pathlib import Path as _P
REPO = _P(__file__).resolve().parent.parent
PKG = REPO / "drchost"
import shutil
import subprocess
from pathlib import Path
import pytest

from drchost.patch_x264_drc import (
    patch_x264_h,
    patch_set_c,
    patch_encoder_c,
    patch_analyse_c,
    patch_encoder_cqp_range,
    patch_encoder_keep_nalu,
    patch_encoder_no_row_reencode,
    patch_ratecontrol_c,
    apply_x264_drc_patch,
)

LOCAL_SRC_DIR = REPO / "third_party" / "x264"
needs_x264 = pytest.mark.skipif(not (LOCAL_SRC_DIR / ".git").exists(), reason="ejecuta drchost/fetch_x264.sh")


@needs_x264
def test_x264_drc_patch_applies_and_exposes_b_drh_mode(tmp_path: Path):
    """Prueba 17: verifica que patch_x264_drc.py inyecta las 4 modificaciones y que x264.h define b_drh_mode."""
    assert LOCAL_SRC_DIR.exists(), f"El código fuente de x264 debe existir en {LOCAL_SRC_DIR}"

    # Copiar a un directorio temporal para validar la aplicación limpia del parche
    temp_x264 = tmp_path / "x264"
    shutil.copytree(LOCAL_SRC_DIR, temp_x264)
    subprocess.run(["git", "-C", str(temp_x264), "checkout", "."], check=True)

    # 1. Probar funciones individuales de parcheo
    # x264.h
    h_path = temp_x264 / "x264.h"
    orig_h = h_path.read_text(encoding="utf-8")
    assert "b_drh_mode;" not in orig_h
    patched_h = patch_x264_h(orig_h)
    assert "int         b_drh_mode;" in patched_h
    # Idempotencia
    assert patch_x264_h(patched_h) == patched_h

    # encoder/set.c
    set_path = temp_x264 / "encoder" / "set.c"
    orig_set = set_path.read_text(encoding="utf-8")
    patched_set = patch_set_c(orig_set)
    assert "pps->i_pic_init_qp = 32 + QP_BD_OFFSET;" in patched_set
    assert patch_set_c(patched_set) == patched_set

    # encoder/encoder.c
    enc_path = temp_x264 / "encoder" / "encoder.c"
    orig_enc = enc_path.read_text(encoding="utf-8")
    assert "cabac_size_bytes" not in orig_enc
    assert "b_drh_mode" not in orig_enc
    patched_enc = patch_encoder_c(orig_enc)
    assert "cabac_size_bytes" in patched_enc
    assert "if( !h->param.b_drh_mode )" in patched_enc
    assert "b_cabac_chunk_ending" in patched_enc
    assert patch_encoder_c(patched_enc) == patched_enc

    # encoder/analyse.c
    ana_path = temp_x264 / "encoder" / "analyse.c"
    orig_ana = ana_path.read_text(encoding="utf-8")
    assert "I_PRED_16x16_P" in orig_ana
    assert "I_PRED_CHROMA_P" in orig_ana
    patched_ana = patch_analyse_c(orig_ana)
    # Debe eliminar I_PRED_16x16_P y I_PRED_CHROMA_P de las tablas de modos disponibles
    i16_pos = patched_ana.find("i16x16_mode_available[5][5]")
    assert i16_pos != -1
    i16_chunk = patched_ana[i16_pos : i16_pos + 300]
    assert "I_PRED_16x16_P" not in i16_chunk

    chroma_pos = patched_ana.find("chroma_mode_available[5][5]")
    assert chroma_pos != -1
    chroma_chunk = patched_ana[chroma_pos : chroma_pos + 300]
    assert "I_PRED_CHROMA_P" not in chroma_chunk

    assert patch_analyse_c(patched_ana) == patched_ana

    # 2. Probar apply_x264_drc_patch sobre el árbol temporal
    apply_x264_drc_patch(temp_x264)

    assert "int         b_drh_mode;" in h_path.read_text(encoding="utf-8")
    assert "pps->i_pic_init_qp = 32 + QP_BD_OFFSET;" in set_path.read_text(encoding="utf-8")
    assert "b_drh_mode" in enc_path.read_text(encoding="utf-8")
    i16_post = ana_path.read_text(encoding="utf-8")
    assert "I_PRED_16x16_P" not in i16_post[i16_post.find("i16x16_mode_available[5][5]") : i16_post.find("i16x16_mode_available[5][5]") + 300]


import os as _os
TARGET_HOST = _os.environ.get("DRC_TEST_HOST", "")   # usuario@host de una Raspberry Pi para las pruebas de hardware


def test_drc_x264_builds_with_aarch64_neon_on_rpi4():
    """
    Prueba 18: compila e instala libx264.a en la Raspberry Pi 4B verificando
    que config.h tiene HAVE_AARCH64 1 y HAVE_AS_FUNC 1 y que un frame 854x480
    con b_drh_mode=1 genera exactamente 5 chunks NAL.
    """
    import subprocess

    build_script = PKG / "build_userspace.sh"
    assert build_script.exists(), f"El script {build_script} debe existir"

    # 1. Ejecutar build_userspace.sh --x264 en la Raspberry Pi 4
    res = subprocess.run([str(build_script), "--x264", TARGET_HOST], capture_output=True, text=True)
    assert res.returncode == 0, f"Error en compilación remota de x264:\n{res.stderr}\n{res.stdout}"

    # 2. Verificar config.h en la Raspberry Pi 4
    check_config_cmd = (
        "grep -E '#define HAVE_AARCH64 1' ~/drc-build/x264/config.h && "
        "grep -E '#define HAVE_AS_FUNC 1' ~/drc-build/x264/config.h"
    )
    res_cfg = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, check_config_cmd],
        capture_output=True,
        text=True,
    )
    assert res_cfg.returncode == 0, f"config.h no tiene soporte AArch64/NEON:\n{res_cfg.stderr}\n{res_cfg.stdout}"

    # 3. Verificar que libx264.a está instalado en /usr/local/lib
    check_lib_cmd = "test -f /usr/local/lib/libx264.a && ls -l /usr/local/lib/libx264.a"
    res_lib = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, check_lib_cmd],
        capture_output=True,
        text=True,
    )
    assert res_lib.returncode == 0, f"/usr/local/lib/libx264.a no instalado:\n{res_lib.stderr}"

    # 4. Compilar y ejecutar test de codificación 854x480 con b_drh_mode=1
    test_c_src = r"""#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <x264.h>

int main(void)
{
    x264_param_t param;
    x264_param_default_preset(&param, "ultrafast", "zerolatency");
    param.i_threads = 1;
    param.b_sliced_threads = 0;
    param.i_slice_count = 1;
    param.rc.i_rc_method = X264_RC_CQP;
    param.rc.i_qp_constant = param.rc.i_qp_min = param.rc.i_qp_max = 32;
    param.i_width = 854;
    param.i_height = 480;
    param.b_vfr_input = 0;
    param.b_repeat_headers = 0;
    param.b_annexb = 1;
    param.b_cabac = 1;
    param.b_drh_mode = 1;
    param.i_fps_num = 60;
    param.i_fps_den = 1;

    x264_t *encoder = x264_encoder_open(&param);
    if (!encoder) {
        fprintf(stderr, "Failed to open x264 encoder\n");
        return 1;
    }

    x264_picture_t pic_in, pic_out;
    x264_picture_alloc(&pic_in, X264_CSP_I420, 854, 480);
    memset(pic_in.img.plane[0], 128, 854 * 480);
    memset(pic_in.img.plane[1], 128, (854/2) * (480/2));
    memset(pic_in.img.plane[2], 128, (854/2) * (480/2));
    pic_in.i_pts = 0;

    x264_nal_t *nals = NULL;
    int i_nals = 0;
    int frame_size = x264_encoder_encode(encoder, &nals, &i_nals, &pic_in, &pic_out);

    int slice_chunks = 0;
    for (int i = 0; i < i_nals; i++) {
        if (nals[i].i_type == NAL_SLICE || nals[i].i_type == NAL_SLICE_IDR) {
            slice_chunks++;
            printf("  Chunk %d: type=%d, payload=%d bytes, first_mb=%d, last_mb=%d\n",
                   slice_chunks, nals[i].i_type, nals[i].i_payload, nals[i].i_first_mb, nals[i].i_last_mb);
        }
    }

    x264_picture_clean(&pic_in);
    x264_encoder_close(encoder);

    printf("Frame size: %d, slice_chunks: %d\n", frame_size, slice_chunks);
    if (slice_chunks != 5) {
        fprintf(stderr, "Expected 5 slice chunks, got %d\n", slice_chunks);
        return 2;
    }

    printf("TEST_5_CHUNKS_PASSED\n");
    return 0;
}
"""
    test_run_cmd = (
        f"cat << 'EOF' > /tmp/test_x264_chunks.c\n{test_c_src}\nEOF\n"
        "gcc -O2 /tmp/test_x264_chunks.c -I/usr/local/include -L/usr/local/lib -lx264 -lpthread -lm -ldl -o /tmp/test_x264_chunks && "
        "/tmp/test_x264_chunks"
    )

    res_run = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", TARGET_HOST, test_run_cmd],
        capture_output=True,
        text=True,
    )
    assert res_run.returncode == 0, f"Prueba de 5 chunks en RPi4 falló:\n{res_run.stderr}\n{res_run.stdout}"
    assert "TEST_5_CHUNKS_PASSED" in res_run.stdout


@needs_x264
def test_patch_macroblock_c_forbids_pskip_in_drh_mode():
    """
    Prueba 46: la captura del enlace real Wii U <-> GamePad muestra que, con pantalla
    estática, la consola envía P-frames de ~2.8 KB (341–763 B por chunk): codifica cada
    macrobloque como P16x16 sin usar P_Skip. patch_macroblock_c debe hacer que, en
    b_drh_mode, x264 no permita P_Skip (h->mb.b_allow_skip = 0), salvo con
    DRC_ALLOW_PSKIP=1 para comparar, y ser idempotente.
    """
    from drchost.patch_x264_drc import patch_macroblock_c
    src = subprocess.run(["git", "-C", str(LOCAL_SRC_DIR), "show", "HEAD:common/macroblock.c"],
                         capture_output=True, text=True, check=True).stdout
    assert "b_drh_mode" not in src
    patched = patch_macroblock_c(src)
    assert "h->param.b_drh_mode" in patched
    assert "DRC_ALLOW_PSKIP" in patched
    anchor = patched.index("h->mb.b_allow_skip = 1;")
    assert patched.index("h->param.b_drh_mode", anchor) > anchor
    assert patch_macroblock_c(patched) == patched


NO_PSKIP_CPP = r"""#include <cstdio>
#include <tuple>
#include <vector>
#include <drc/internal/h264-encoder.h>
#include <drc/screen.h>
int main() {
    const int W = drc::kScreenWidth, H = drc::kScreenHeight;
    std::vector<uint8_t> f(W * H * 3 / 2, 128);
    for (int y = 0; y < H; y++) for (int x = 0; x < W; x++) f[y * W + x] = ((x / 8 + y / 8) & 1) ? 200 : 40;
    drc::H264Encoder enc;
    enc.Encode(f, true);
    size_t min_chunk = 1u << 30;
    for (int n = 0; n < 10; n++) {
        const auto& chunks = enc.Encode(f, false);
        for (const auto& c : chunks) if (std::get<1>(c) < min_chunk) min_chunk = std::get<1>(c);
    }
    printf("MIN_P_CHUNK=%zu\n", min_chunk);
    return 0;
}
"""


def test_static_p_frames_do_not_use_pskip_on_rpi4():
    """
    Prueba 47: en la Raspberry Pi 4B, con la libdrc/x264 instaladas, 10 P-frames de una
    imagen estática codificados sin P_Skip deben producir chunks mayores que con
    DRC_ALLOW_PSKIP=1 (con P_Skip un chunk estático ocupa ~1 B).
    """
    cmd = (
        f"cat << 'EOF' > /tmp/test_nopskip.cpp\n{NO_PSKIP_CPP}\nEOF\n"
        "g++ -O2 -std=c++11 /tmp/test_nopskip.cpp -I/usr/local/include -L/usr/local/lib "
        "-ldrc -lx264 -lswscale -lavutil -lpthread -lm -ldl -o /tmp/test_nopskip && "
        "echo SIN_SKIP; LD_LIBRARY_PATH=/usr/local/lib /tmp/test_nopskip && "
        "echo CON_SKIP; DRC_ALLOW_PSKIP=1 LD_LIBRARY_PATH=/usr/local/lib /tmp/test_nopskip"
    )
    res = subprocess.run(["ssh", "-o", "BatchMode=yes", TARGET_HOST, cmd], capture_output=True, text=True)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    vals = [int(l.split("=")[1]) for l in res.stdout.splitlines() if l.startswith("MIN_P_CHUNK=")]
    no_skip, with_skip = vals
    print(f"chunk P mínimo: sin P_Skip {no_skip} B, con P_Skip {with_skip} B")
    assert no_skip > with_skip, f"Sin P_Skip ({no_skip} B) debería pesar más que con P_Skip ({with_skip} B)"


def test_patch_analyse_c_limita_vectores_en_drh():
    """
    Prueba 133: la consola nunca pasa de 70x55 px y x264 llegaba a 251 px; en el mando eso se ve como bloques con la imagen
    desplazada. DRC_MV_RANGE (px) limita el vector en los dos ejes; x264 por sí solo no limita el horizontal.
    """
    src = (
        "static void mb_analyse_init( x264_t *h, x264_mb_analysis_t *a, int qp )\n{\n"
        "        h->mb.mv_max_spel[0] = X264_MIN( h->mb.mv_max[0], i_fmv_range-1 );\n"
        "                h->mb.mv_max_spel[1] = X264_MIN3( h->mb.mv_max[1], i_fmv_range-1, 4*thread_mvy_range );\n"
    )
    out = patch_analyse_c(src)
    assert "DRC_MV_RANGE" in out
    assert out.count("drh_mv_limit( h )") == 6
    assert out.index("drh_mv_limit") < out.index("static void mb_analyse_init")
    assert patch_analyse_c(out) == out


def test_patch_encoder_c_respeta_el_rango_de_qp_en_cqp():
    """
    Prueba 134: x264 en CQP reescribe i_qp_min/i_qp_max con el QP constante, así que el QP por frame (i_qpplus1) se
    recortaba y DRC_QP_DYN nunca hizo nada. En b_drh_mode se conserva el rango que fija libdrc.
    """
    src = (
        "    if( h->param.rc.i_rc_method == X264_RC_CQP )\n    {\n"
        "        h->param.rc.i_qp_min = x264_clip3( (int)(X264_MIN3( qp_p, qp_i, qp_b )), 0, QP_MAX );\n"
        "        h->param.rc.i_qp_max = x264_clip3( (int)(X264_MAX3( qp_p, qp_i, qp_b ) + .999), 0, QP_MAX );\n"
        "    }\n"
    )
    out = patch_encoder_cqp_range(src)
    assert out.count("if( !h->param.b_drh_mode )") == 1
    assert out.index("if( !h->param.b_drh_mode )") < out.index("i_qp_min")
    assert patch_encoder_cqp_range(out) == out


def test_patch_encoder_c_mantiene_nalu_process_con_hilos():
    """Prueba 139: x264 anula nalu_process con hilos de frame, y sin él no hay trozos: en b_drh_mode debe conservarse."""
    src = "    if( h->i_thread_frames > 1 )\n        h->param.nalu_process = NULL;\n"
    out = patch_encoder_keep_nalu(src)
    assert "if( h->i_thread_frames > 1 && !h->param.b_drh_mode )" in out
    assert patch_encoder_keep_nalu(out) == out


def test_patch_encoder_c_sin_rehacer_filas_en_drh():
    """
    Prueba 140: con VBV x264 puede volver atrás y rehacer una fila, pero el troceado para el mando ya cortó esos bytes: salían
    más de 5 trozos y fronteras falsas. En b_drh_mode el ajuste de QP se aplica a la fila siguiente, sin retroceder.
    """
    src = "        if( x264_ratecontrol_mb( h, mb_size ) < 0 )\n        {\n"
    out = patch_encoder_no_row_reencode(src)
    assert "if( x264_ratecontrol_mb( h, mb_size ) < 0 && !h->param.b_drh_mode )" in out
    assert patch_encoder_no_row_reencode(out) == out


def test_patch_ratecontrol_c_no_borra_bits_de_fila_en_drh():
    """
    Prueba 141: en x264_ratecontrol_mb, si can_reencode_row vale 1 pero slice_write no rehace la fila,
    x264 ponía a cero i_row_bits[y], revertía qpa_rc/qpa_aq y descartaba la subida de QP del VBV.
    En b_drh_mode can_reencode_row debe valer 0.
    """
    src = "    int can_reencode_row = h->sh.i_first_mb <= ((h->mb.i_mb_y - SLICE_MBAFF) * h->mb.i_mb_stride);\n"
    out = patch_ratecontrol_c(src)
    assert "int can_reencode_row = !h->param.b_drh_mode && h->sh.i_first_mb <=" in out
    assert patch_ratecontrol_c(out) == out


def test_patch_encoder_c_vaciar_no_cierra_la_entrada_en_drh():
    """
    Prueba 157: x264_encoder_encode(pic_in=NULL) entrega un frame retenido por los hilos, pero marca la entrada como
    terminada (b_exit_thread) y el siguiente frame real falla con «lookahead thread is already stopped». En b_drh_mode
    (sin hilo de lookahead) no se marca, así libdrc puede vaciar x264 en los ticks sin imagen nueva (DRC_ENC_FLUSH).
    """
    from patch_x264_drc import patch_encoder_flush
    src = (
        "    else\n    {\n"
        "        /* signal kills for lookahead thread */\n"
        "        x264_pthread_mutex_lock( &h->lookahead->ifbuf.mutex );\n"
        "        h->lookahead->b_exit_thread = 1;\n"
    )
    out = patch_encoder_flush(src)
    assert "else if( !h->param.b_drh_mode )\n    {\n        /* signal kills for lookahead thread */" in out
    assert patch_encoder_flush(out) == out
