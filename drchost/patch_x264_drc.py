"""
patch_x264_drc.py - Aplica las modificaciones DRC a x264 para compatibilidad
con Wii U GamePad (DRC/DRH mode) sobre x264 moderno con soporte NEON en AArch64.
"""
from pathlib import Path
import re
import sys
import argparse


def patch_x264_h(content: str) -> str:
    """Inyecta el parámetro b_drh_mode en struct x264_param_t."""
    if "b_drh_mode;" in content:
        return content

    target = "    int         b_bluray_compat;\n"
    if target not in content:
        # Fallback a otra línea de configuración si b_bluray_compat cambia
        target = "    int         b_open_gop;\n"
        if target not in content:
            raise ValueError("No se encontró el punto de inserción para b_drh_mode en x264.h")

    replacement = target + "    int         b_drh_mode;         /* Compatibility mode for Wii U Gamepad video encoding */\n"
    return content.replace(target, replacement, 1)


def patch_set_c(content: str) -> str:
    """Fuerza pps->i_pic_init_qp = 32 + QP_BD_OFFSET en encoder/set.c."""
    if "pps->i_pic_init_qp = 32 + QP_BD_OFFSET;" in content:
        return content

    pattern = re.compile(r"pps->i_pic_init_qp\s*=\s*[^;]+;")
    match = pattern.search(content)
    if not match:
        raise ValueError("No se encontró la asignación de pps->i_pic_init_qp en encoder/set.c")

    return content[:match.start()] + "pps->i_pic_init_qp = 32 + QP_BD_OFFSET;" + content[match.end():]


def patch_encoder_c(content: str) -> str:
    """
    Modifica encoder/encoder.c:
    1. Define cabac_size_bytes()
    2. En b_drh_mode omite slice_header_write y fuerza i_qp = 32 + QP_BD_OFFSET
    3. Emula el chunking DRC de Wii U: emite un chunk NAL cada 6 filas de macrobloques (5 sub-slices por frame de 480p).
    """
    if "cabac_size_bytes" in content and "b_drh_mode" in content and "nal_next->i_padding = 0;" in content:
        return content

    # 1. Inyectar cabac_size_bytes antes de slice_write
    if "cabac_size_bytes" not in content:
        slice_write_decl = re.search(r"static\s+(?:int|intptr_t)\s+(?:x264_)?slice_write\s*\(\s*x264_t\s*\*h\s*\)", content)
        if not slice_write_decl:
            raise ValueError("No se encontró la función slice_write en encoder/encoder.c")

        cabac_helper = (
            "static int cabac_size_bytes( x264_cabac_t *cb )\n"
            "{\n"
            "    return cb->p - cb->p_start + cb->i_bytes_outstanding;\n"
            "}\n\n"
        )
        content = content[:slice_write_decl.start()] + cabac_helper + content[slice_write_decl.start():]

    # 2. Inicializar cabac_last_size y b_cabac_chunk_ending tras i_skip = 0;
    skip_marker = "    i_skip = 0;\n"
    if skip_marker in content and "int cabac_last_size = -2;" not in content:
        cabac_vars = (
            "    i_skip = 0;\n"
            "    int cabac_last_size = -2;\n"
            "    int b_cabac_chunk_ending = 0;\n"
        )
        content = content.replace(skip_marker, cabac_vars, 1)

    # 3. Omitir slice_header_write en b_drh_mode y forzar i_qp
    slice_header_call = re.search(r"(?:x264_)?slice_header_write\s*\(\s*&h->out\.bs,\s*&h->sh,\s*h->i_nal_ref_idc\s*\);", content)
    if slice_header_call and "if( !h->param.b_drh_mode )" not in content:
        call_str = slice_header_call.group(0)
        # Buscar el bloque previo de asignación de QP
        qp_block_match = re.search(
            r"(h->sh\.i_qp\s*=\s*SPEC_QP\(\s*h->sh\.i_qp\s*\);\s*\n\s*h->sh\.i_qp_delta\s*=\s*h->sh\.i_qp\s*-\s*h->pps->i_pic_init_qp;\s*\n)",
            content
        )
        if qp_block_match:
            qp_block = qp_block_match.group(0)
            replacement_qp = (
                "h->sh.i_qp = SPEC_QP( h->sh.i_qp );\n"
                "    if( h->param.b_drh_mode )\n"
                "        h->sh.i_qp = 32 + QP_BD_OFFSET;\n"
                "    h->sh.i_qp_delta = h->sh.i_qp - h->pps->i_pic_init_qp;\n"
            )
            content = content.replace(qp_block, replacement_qp, 1)

        content = content.replace(call_str, f"if( !h->param.b_drh_mode )\n        {call_str}", 1)

    # 4. Inyectar chunking DRC en el loop de macrobloques
    chunk_marker = "        if( mb_xy == h->sh.i_last_mb )\n            break;\n"
    if chunk_marker in content and "/* emulate Wii U DRH chunking behavior */" not in content:
        drc_chunking = (
            "        if( mb_xy == h->sh.i_last_mb )\n"
            "            break;\n\n"
            "        /* emulate Wii U DRH chunking behavior */\n"
            "        if( h->param.b_drh_mode )\n"
            "        {\n"
            "            if( b_cabac_chunk_ending && cabac_size_bytes(&h->cabac) > cabac_last_size )\n"
            "            {\n"
            "                x264_nal_t *nal = &h->out.nal[h->out.i_nal++];\n"
            "                nal->i_last_mb = mb_xy;\n"
            "                nal->i_payload = cabac_size_bytes(&h->cabac) - cabac_last_size;\n\n"
            "                if ( h->param.nalu_process )\n"
            "                    h->param.nalu_process( h, nal, h->fenc->opaque );\n"
            "                nal_check_buffer( h );\n\n"
            "                /* nal_check_buffer may realloc out.nal */\n"
            "                nal = &h->out.nal[h->out.i_nal - 1];\n"
            "                x264_nal_t *nal_next = &h->out.nal[h->out.i_nal];\n\n"
            "                nal_next->i_ref_idc = nal->i_ref_idc;\n"
            "                nal_next->i_type = nal->i_type;\n"
            "                nal_next->b_long_startcode = nal->b_long_startcode;\n"
            "                nal_next->i_first_mb = mb_xy + 1;\n"
            "                nal_next->i_payload = 0;\n"
            "                nal_next->i_padding = 0;\n"
            "                nal_next->p_payload = nal->p_payload + nal->i_payload;\n\n"
            "                cabac_last_size = cabac_size_bytes(&h->cabac);\n"
            "                b_cabac_chunk_ending = 0;\n"
            "            }\n\n"
            "            if( i_mb_y > 0 && (i_mb_y % 6) == 0 && i_mb_x == 0 )\n"
            "                b_cabac_chunk_ending = 1;\n"
            "        }\n"
        )
        content = content.replace(chunk_marker, drc_chunking, 1)

    return content


def patch_encoder_cqp_range(content: str) -> str:
    """En b_drh_mode conserva i_qp_min/i_qp_max en CQP: libdrc fija el QP de cada frame con i_qpplus1."""
    if "if( !h->param.b_drh_mode )\n        {\n            h->param.rc.i_qp_min" in content:
        return content
    pair = (
        "        h->param.rc.i_qp_min = x264_clip3( (int)(X264_MIN3( qp_p, qp_i, qp_b )), 0, QP_MAX );\n"
        "        h->param.rc.i_qp_max = x264_clip3( (int)(X264_MAX3( qp_p, qp_i, qp_b ) + .999), 0, QP_MAX );\n"
    )
    if pair not in content:
        raise ValueError("No se encontró el recorte de QP de CQP en encoder/encoder.c")
    guarded = (
        "        if( !h->param.b_drh_mode )\n        {\n"
        + "".join("    " + l + "\n" for l in pair.splitlines())
        + "        }\n"
    )
    return content.replace(pair, guarded, 1)


def patch_encoder_keep_nalu(content: str) -> str:
    """x264 anula nalu_process con hilos de frame; el troceado para el mando depende de ese aviso."""
    new = "if( h->i_thread_frames > 1 && !h->param.b_drh_mode )\n        h->param.nalu_process = NULL;"
    if new in content:
        return content
    old = "if( h->i_thread_frames > 1 )\n        h->param.nalu_process = NULL;"
    if old not in content:
        raise ValueError("No se encontró la anulación de nalu_process en encoder/encoder.c")
    return content.replace(old, new, 1)


def patch_encoder_no_row_reencode(content: str) -> str:
    """Sin retroceso de fila por VBV en b_drh_mode: el troceado ya cortó esos bytes."""
    new = "if( x264_ratecontrol_mb( h, mb_size ) < 0 && !h->param.b_drh_mode )"
    if new in content:
        return content
    old = "if( x264_ratecontrol_mb( h, mb_size ) < 0 )"
    if old not in content:
        raise ValueError("No se encontró el retroceso de fila en encoder/encoder.c")
    return content.replace(old, new, 1)


def patch_encoder_flush(content: str) -> str:
    """
    x264_encoder_encode(pic_in=NULL) da por terminada la entrada (b_exit_thread) y ya no acepta más frames. En b_drh_mode
    no hay hilo de lookahead: no se marca, y libdrc puede pedir los frames retenidos por los hilos sin recodificar nada.
    """
    target = "    else\n    {\n        /* signal kills for lookahead thread */\n"
    patched = "    else if( !h->param.b_drh_mode )\n    {\n        /* signal kills for lookahead thread */\n"
    if patched in content:
        return content
    if content.count(target) != 1:
        raise ValueError("No se encontró el cierre del lookahead en x264_encoder_encode")
    return content.replace(target, patched, 1)


def patch_ratecontrol_c(content: str) -> str:
    """
    En b_drh_mode desactiva can_reencode_row dentro de x264_ratecontrol_mb (encoder/ratecontrol.c):
    si can_reencode_row vale 1 pero slice_write no rehace la fila, x264_ratecontrol_mb ponía a cero
    i_row_bits[y], revertía qpa_rc/qpa_aq y descartaba la subida de QP de emergencia del VBV,
    dejando ciego el control de tasa por fila en el resto del frame.
    """
    new = "int can_reencode_row = !h->param.b_drh_mode && h->sh.i_first_mb <= ((h->mb.i_mb_y - SLICE_MBAFF) * h->mb.i_mb_stride);"
    if new in content:
        return content
    old = "int can_reencode_row = h->sh.i_first_mb <= ((h->mb.i_mb_y - SLICE_MBAFF) * h->mb.i_mb_stride);"
    if old not in content:
        raise ValueError("No se encontró can_reencode_row en encoder/ratecontrol.c")
    return content.replace(old, new, 1)


def patch_analyse_c(content: str) -> str:
    """Elimina I_PRED_16x16_P y I_PRED_CHROMA_P de las tablas de modos de predicción en encoder/analyse.c."""
    content = content.replace("I_PRED_16x16_P, -1", "-1, -1")
    content = content.replace("I_PRED_CHROMA_P, -1", "-1, -1")
    return patch_analyse_mv_range(content)


def patch_analyse_mv_range(content: str) -> str:
    """Limita el vector de movimiento en los dos ejes con DRC_MV_RANGE (píxeles, 0 = sin límite) en b_drh_mode."""
    if "DRC_MV_RANGE" in content:
        return content
    helper = (
        "/* Wii U GamePad (DRH): la consola no pasa de 70x55 px; DRC_MV_RANGE limita el vector en píxeles. */\n"
        "static int drh_mv_limit( x264_t *h )\n"
        "{\n"
        "    static int limit = -1;\n"
        "    if( limit < 0 )\n"
        "    {\n"
        "        const char *env = getenv( \"DRC_MV_RANGE\" );\n"
        "        limit = env ? atoi( env ) * 4 : 0;\n"
        "    }\n"
        "    return h->param.b_drh_mode ? limit : 0;\n"
        "}\n\n"
    )
    marker = "static void mb_analyse_init( x264_t *h, x264_mb_analysis_t *a, int qp )\n"
    if marker not in content:
        raise ValueError("No se encontró mb_analyse_init en encoder/analyse.c")
    content = content.replace(marker, helper + marker, 1)
    for axis, line in (
        (0, "        h->mb.mv_max_spel[0] = X264_MIN( h->mb.mv_max[0], i_fmv_range-1 );\n"),
        (1, "                h->mb.mv_max_spel[1] = X264_MIN3( h->mb.mv_max[1], i_fmv_range-1, 4*thread_mvy_range );\n"),
    ):
        if line not in content:
            raise ValueError(f"No se encontró el límite del eje {axis} en encoder/analyse.c")
        pad = line[: len(line) - len(line.lstrip())]
        clamp = (
            f"{pad}if( drh_mv_limit( h ) )\n"
            f"{pad}{{\n"
            f"{pad}    h->mb.mv_min_spel[{axis}] = X264_MAX( h->mb.mv_min_spel[{axis}], -drh_mv_limit( h ) );\n"
            f"{pad}    h->mb.mv_max_spel[{axis}] = X264_MIN( h->mb.mv_max_spel[{axis}], drh_mv_limit( h ) );\n"
            f"{pad}}}\n"
        )
        content = content.replace(line, line + clamp, 1)
    return content


def patch_macroblock_c(content: str) -> str:
    """
    En b_drh_mode prohíbe P_Skip (h->mb.b_allow_skip = 0) en common/macroblock.c: x264
    convierte entonces cada P_SKIP en P_L0 16x16 con el vector de predicción del skip.
    Ojo: la consola SÍ usa P_Skip (56 % de los macrobloques de sus P). Usar DRC_ALLOW_PSKIP=1.
    DRC_ALLOW_PSKIP=1 lo vuelve a permitir para comparar.
    """
    if "DRC_ALLOW_PSKIP" in content:
        return content
    target = "    h->mb.b_allow_skip = 1;\n"
    if content.count(target) != 1:
        raise ValueError("No se encontró 'h->mb.b_allow_skip = 1;' en common/macroblock.c")
    block = (
        target
        + "    /* Wii U GamePad (DRH): P_Skip solo con DRC_ALLOW_PSKIP=1. */\n"
        + "    if( h->param.b_drh_mode )\n"
        + "    {\n"
        + "        static int drh_allow_pskip = -1;\n"
        + "        if( drh_allow_pskip < 0 )\n"
        + "        {\n"
        + "            const char *env = getenv( \"DRC_ALLOW_PSKIP\" );\n"
        + "            drh_allow_pskip = env && env[0] == '1';\n"
        + "        }\n"
        + "        if( !drh_allow_pskip )\n"
        + "            h->mb.b_allow_skip = 0;\n"
        + "    }\n"
    )
    content = content.replace(target, block, 1)
    if "#include <stdlib.h>" not in content:
        content = content.replace('#include "common.h"\n', '#include "common.h"\n#include <stdlib.h>\n', 1)
    return content


def apply_x264_drc_patch(x264_dir: Path):
    """Aplica las modificaciones DRC a un árbol de x264."""
    x264_dir = Path(x264_dir)
    h_file = x264_dir / "x264.h"
    set_file = x264_dir / "encoder" / "set.c"
    enc_file = x264_dir / "encoder" / "encoder.c"
    rc_file = x264_dir / "encoder" / "ratecontrol.c"
    ana_file = x264_dir / "encoder" / "analyse.c"
    mb_file = x264_dir / "common" / "macroblock.c"

    for f in (h_file, set_file, enc_file, rc_file, ana_file, mb_file):
        if not f.exists():
            raise FileNotFoundError(f"Archivo no encontrado en árbol x264: {f}")

    h_file.write_text(patch_x264_h(h_file.read_text(encoding="utf-8")), encoding="utf-8")
    set_file.write_text(patch_set_c(set_file.read_text(encoding="utf-8")), encoding="utf-8")
    enc_file.write_text(patch_encoder_flush(patch_encoder_no_row_reencode(patch_encoder_keep_nalu(patch_encoder_cqp_range(patch_encoder_c(enc_file.read_text(encoding="utf-8")))))), encoding="utf-8")
    rc_file.write_text(patch_ratecontrol_c(rc_file.read_text(encoding="utf-8")), encoding="utf-8")
    ana_file.write_text(patch_analyse_c(ana_file.read_text(encoding="utf-8")), encoding="utf-8")
    mb_file.write_text(patch_macroblock_c(mb_file.read_text(encoding="utf-8")), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aplicar parches DRC a x264")
    parser.add_argument("--src-dir", type=Path, default=Path(__file__).resolve().parent.parent / "third_party" / "x264",
                        help="Directorio raíz del código fuente de x264")
    parser.add_argument("--apply", action="store_true", help="Aplica los parches")
    args = parser.parse_args()

    if args.apply:
        apply_x264_drc_patch(args.src_dir)
        print(f"Parches DRC aplicados con éxito en {args.src_dir}")
    else:
        parser.print_help()
