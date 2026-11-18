// Copyright (c) 2013, Mema Hacking, All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are met:
//
// 1. Redistributions of source code must retain the above copyright notice,
// this list of conditions and the following disclaimer.
//
// 2. Redistributions in binary form must reproduce the above copyright
// notice, this list of conditions and the following disclaimer in the
// documentation and/or other materials provided with the distribution.
//
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
// AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
// IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
// ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
// LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
// CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
// SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
// INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
// CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
// ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
// POSSIBILITY OF SUCH DAMAGE.

#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <drc/internal/h264-encoder.h>
#include <drc/internal/encoder-flags.h>
#include <drc/internal/chunk-balance.h>
#include <drc/internal/frame-cap.h>
#include <drc/internal/mb-constant.h>
#include <drc/screen.h>
#include <drc/types.h>
#include <tuple>
#include <vector>

extern "C" {
#include <x264.h>
}

namespace drc {

namespace {
const char* const kEncoderQuality = "ultrafast"; //"slow";

// Enable intra prediction to flush decoding errors away. In practice we
// shouldn't need this (the DRC will send libdrc a message when a frame fails
// to be decoded), but at the moment it helps make decoding errors better.
const bool kEnableIntraRefresh = true;

// Debugging feature to dump the encoded h264 stream to a .h264 file. If
// enabled, a file called "dump.h264" will be created in the current directory.
const bool kDebugDumpToFile = false;

void LogEncoderMessages(void* data, int i_level, const char* format,
                        va_list va) {
  static const bool stats = getenv("DRC_ENC_STATS") && getenv("DRC_ENC_STATS")[0] == '1';
  if (stats) {
    fputs("[x264] ", stderr);
    vfprintf(stderr, format, va);
  }
}

void DumpH264Headers(FILE* fp) {
  u8 nal_start_code[] = { 0x00, 0x00, 0x00, 0x01 };
  u8 gamepad_sps[] = { 0x67, 0x64, 0x00, 0x20, 0xac, 0x2b, 0x40,
                       0x6c, 0x1e, 0xf3, 0x68 };
  u8 gamepad_pps[] = { 0x68, 0xee, 0x06, 0x0c, 0xe8 };

  fwrite(nal_start_code, sizeof (nal_start_code), 1, fp);
  fwrite(gamepad_sps, sizeof (gamepad_sps), 1, fp);
  fwrite(nal_start_code, sizeof (nal_start_code), 1, fp);
  fwrite(gamepad_pps, sizeof (gamepad_pps), 1, fp);
}

size_t NalEscape(byte* dst, const byte* src, size_t size) {
  byte* dst_start = dst;
  const byte* end = src + size;
  if (src < end) {
    *dst++ = *src++;
  }
  if (src < end) {
    *dst++ = *src++;
  }

  while (src < end) {
    if (src[0] <= 0x03 && !dst[-2] && !dst[-1]) {
      *dst++ = 0x03;
    }
    *dst++ = *src++;
  }

  return dst - dst_start;
}

void DumpH264Frame(FILE* fp, const H264ChunkArray& chunks, bool is_idr) {
  // TODO(delroth): make this a property of the encoder.
  static u32 frame_number = 0;

  u8 nal_start_code[] = { 0x00, 0x00, 0x00, 0x01 };
  u8 nal_idr_frame[] = { 0x25, 0xb8, 0x04, 0xff };
  u8 nal_p_frame[] = { 0x21, 0xe0, 0x03, 0xff };

  fwrite(nal_start_code, sizeof (nal_start_code), 1, fp);

  if (is_idr) {
    frame_number = 0;
    fwrite(nal_idr_frame, sizeof (nal_idr_frame), 1, fp);
  } else {
    frame_number = (frame_number + 1) & 0xFF;
    nal_p_frame[1] |= frame_number >> 3;
    nal_p_frame[2] |= frame_number << 5;
    fwrite(nal_p_frame, sizeof (nal_p_frame), 1, fp);
  }

  size_t nal_length = 0;
  for (const auto& chunk : chunks) {
    nal_length += std::get<1>(chunk);
  }

  std::vector<byte> unescaped;
  unescaped.reserve(nal_length);
  for (const auto& chunk : chunks) {
    unescaped.insert(unescaped.end(), std::get<0>(chunk),
                     std::get<0>(chunk) + std::get<1>(chunk));
  }

  std::vector<byte> escaped(2 * nal_length);
  size_t escaped_size = NalEscape(escaped.data(), unescaped.data(),
                                  unescaped.size());
  fwrite(escaped.data(), escaped_size, 1, fp);
}

drc::FrameCap& FrameCapInstance() {
  static drc::FrameCap frame_cap = [] {
    const char* q = getenv("DRC_QP");
    int base = q ? atoi(q) : 32;
    if (base < 10 || base > 51) base = 32;
    const bool dyn = getenv("DRC_QP_DYN") && getenv("DRC_QP_DYN")[0] == '1';
    int max_bytes = drc::FrameMaxFromEnv(getenv("DRC_FRAME_MAX"));
    if (getenv("DRC_VBV_KBPS")) return drc::FrameCap(base, 0, 2);  // x264 controla la tasa
    if (dyn && max_bytes == 0) max_bytes = 25000;  // la consola no pasa de ~25 KB por frame
    const char* st = getenv("DRC_FRAME_QP_STEP");
    int step = st ? atoi(st) : 2;
    if (max_bytes) fprintf(stderr, "[libdrc] frame_max=%d qp_step=%d qp_dyn=%d\n", max_bytes, step, dyn ? 1 : 0);
    if (dyn) {
      int qp_lo, qp_hi;
      drc::QpRangeFromEnv(getenv("DRC_QP_MIN"), getenv("DRC_QP_MAX"), &qp_lo, &qp_hi);
      fprintf(stderr, "[libdrc] qp_dyn %d-%d\n", qp_lo, qp_hi);
      return drc::FrameCap(qp_lo, max_bytes, step, qp_hi, qp_hi);
    }
    return drc::FrameCap(base, max_bytes, step);
  }();
  return frame_cap;
}

}  // namespace

H264Encoder::H264Encoder(int threads)
    : encoder_(nullptr), threads_(threads), dump_file_(nullptr) {
  if (threads_ <= 0) {
    const char* env = getenv("DRC_ENC_THREADS");
    threads_ = env ? atoi(env) : 1;
    if (threads_ < 1 || threads_ > 4) threads_ = 1;
  }
  empty_.fill(H264Chunk(nullptr, 0));
  for (auto& slot : slots_) slot.encoder = this;
  CreateEncoder();
}

H264Encoder::~H264Encoder() {
  DestroyEncoder();
}

void H264Encoder::CreateEncoder() {
  x264_param_t param;

  x264_param_default_preset(&param, kEncoderQuality, "zerolatency");
  param.i_width = kScreenWidth;
  param.i_height = kScreenHeight;

  // DRC_PARTS=1: particiones inter como la consola (16x8, 8x16 y 8x8: 2,9/2,7/1,0 % de los
  // macrobloques P en su captura de juego). El preset ultrafast las desactiva.
  // DRC_PARTS=2: solo 16x8 y 8x16 (sin los sub-bloques 8x4/4x8/4x4 del 8x8).
  const char* parts_env = getenv("DRC_PARTS");
  const bool parts = parts_env && (parts_env[0] == '1' || parts_env[0] == '2');
  if (parts) {
    param.analyse.inter = parts_env[0] == '2'
        ? X264_ANALYSE_PSUB16x16
        : (X264_ANALYSE_PSUB16x16 | X264_ANALYSE_PSUB8x8);
    if (param.analyse.i_subpel_refine < 2) param.analyse.i_subpel_refine = 2;
    fprintf(stderr, "[libdrc] parts=1 subme=%d\n", param.analyse.i_subpel_refine);
  } else {
    param.analyse.inter &= ~X264_ANALYSE_PSUB16x16;
  }

  const int subme = drc::SubmeFromEnv(getenv("DRC_SUBME"));
  if (subme >= 0) {
    param.analyse.i_subpel_refine = subme;
    fprintf(stderr, "[libdrc] subme=%d\n", subme);
  }

  if (drc::Intra4Enabled(getenv("DRC_INTRA4"))) {
    param.analyse.intra |= X264_ANALYSE_I4x4;
    param.analyse.inter |= X264_ANALYSE_I4x4;
    fprintf(stderr, "[libdrc] intra4=1\n");
  }

  // DRC_NO_INTRA_REFRESH=1: P-frames sin la columna de macrobloques intra del
  // intra refresh (diagnóstico del rechazo de P-frames en el GamePad).
  const bool intra_refresh = kEnableIntraRefresh && !getenv("DRC_NO_INTRA_REFRESH");
  fprintf(stderr, "[libdrc] intra_refresh=%d\n", intra_refresh ? 1 : 0);

  if (intra_refresh) {
    param.i_keyint_min = 10;
    param.i_keyint_max = 30;
  } else {
    param.i_keyint_min = param.i_keyint_max = X264_KEYINT_MAX_INFINITE;
  }

  param.i_scenecut_threshold = -1;
  param.i_csp = X264_CSP_I420;
  param.b_cabac = 1;
  param.b_interlaced = 0;
  param.i_bframe = 0;
  param.i_bframe_pyramid = 0;
  param.i_frame_reference = 1;
  param.b_constrained_intra = 1;
  param.b_intra_refresh = intra_refresh;
  param.analyse.i_weighted_pred = 0;
  param.analyse.b_weighted_bipred = 0;
  param.analyse.b_transform_8x8 = 0; //maybe change this to 1..?
  param.analyse.i_chroma_qp_offset = 0;

  // La cabecera de slice fija del GamePad declara QP 32 (slice_qp_delta=0);
  // DRC_QP fija el QP de los macrobloques (x264 lo codifica como mb_qp_delta)
  // para acercar el tamaño de los frames al de la consola real.
  const char* qp_env = getenv("DRC_QP");
  int qp = qp_env ? atoi(qp_env) : 32;
  if (qp < 10 || qp > 51) qp = 32;
  fprintf(stderr, "[libdrc] qp=%d\n", qp);
  param.rc.i_rc_method = X264_RC_CQP;
  param.rc.i_qp_constant = param.rc.i_qp_min = param.rc.i_qp_max = qp;
  // DRC_FRAME_MAX: el QP por frame (frame-cap.h) necesita margen hacia arriba.
  if (drc::FrameMaxFromEnv(getenv("DRC_FRAME_MAX")) > 0) param.rc.i_qp_max = 51;
  // DRC_QP_DYN=1: QP dinámico entre 12 y 32 como la consola (QP 12-32 por macrobloque en su captura).
  if (getenv("DRC_QP_DYN") && getenv("DRC_QP_DYN")[0] == '1') {
    // El IDR sale a QP 32 aunque el máximo de los P sea menor: si no, pasaría de 3 veces el tamaño de los de la consola.
    int qp_lo, qp_hi;
    drc::QpRangeFromEnv(getenv("DRC_QP_MIN"), getenv("DRC_QP_MAX"), &qp_lo, &qp_hi);
    param.rc.i_qp_min = qp_lo;
    param.rc.i_qp_max = qp_hi > 32 ? qp_hi : 32;
  }
  param.rc.f_ip_factor = 1.0;

  const drc::RateConfig rate = drc::RateConfigFromEnv(getenv("DRC_VBV_KBPS"), getenv("DRC_CRF"), getenv("DRC_AQ"),
                                                      getenv("DRC_RATE_MODE"));
  if (rate.enabled()) {
    if (rate.fixed) {
      // Bitrate constante: ABR con el mismo valor de media y de máximo (el CBR de x264).
      param.rc.i_rc_method = X264_RC_ABR;
      param.rc.i_bitrate = rate.vbv_kbps;
    } else {
      param.rc.i_rc_method = X264_RC_CRF;
      param.rc.f_rf_constant = static_cast<float>(rate.crf);
    }
    param.rc.i_vbv_max_bitrate = rate.vbv_kbps;
    const int fmax = drc::FrameMaxFromEnv(getenv("DRC_FRAME_MAX"));
    param.rc.i_vbv_buffer_size = fmax > 0 ? (fmax * 8 / 1000) : drc::VbvBufferKbit(rate.vbv_kbps);
    param.rc.f_vbv_buffer_init = 0.9f;
    param.rc.i_qp_min = 4;
    param.rc.i_qp_max = 40;
    if (getenv("DRC_QP_MIN") || getenv("DRC_QP_MAX")) {
      drc::QpRangeFromEnv(getenv("DRC_QP_MIN"), getenv("DRC_QP_MAX") ? getenv("DRC_QP_MAX") : "40",
                          &param.rc.i_qp_min, &param.rc.i_qp_max);
    }
    param.rc.i_aq_mode = rate.aq > 0.0 ? X264_AQ_VARIANCE : X264_AQ_NONE;
    param.rc.f_aq_strength = static_cast<float>(rate.aq);
    param.rc.b_mb_tree = 0;
    param.rc.i_lookahead = 0;
    param.i_fps_num = 60000;
    param.i_fps_den = 1001;
    param.b_vfr_input = 0;
    fprintf(stderr, "[libdrc] rate %s vbv_kbps=%d vbv_buf=%d crf=%.1f aq=%.2f qp=%d-%d\n",
            rate.fixed ? "fija" : "max", rate.vbv_kbps, param.rc.i_vbv_buffer_size, rate.crf, rate.aq, param.rc.i_qp_min, param.rc.i_qp_max);
  }

  // El GamePad aplica siempre el filtro de desbloqueo (cabecera de slice fija con idc=0, offsets 0): reconstruir
  // las referencias igual. ultrafast lo apaga por defecto. DRC_DEBLOCK=0 lo desactiva (diagnóstico).
  const bool deblock = drc::DeblockEnabled(getenv("DRC_DEBLOCK"));
  param.b_deblocking_filter = deblock ? 1 : 0;
  param.i_deblocking_filter_alphac0 = 0;
  param.i_deblocking_filter_beta = 0;
  fprintf(stderr, "[libdrc] deblock=%d\n", deblock ? 1 : 0);

  // Do not output SPS/PPS/SEI/unit delimeters.
  param.b_repeat_headers = 0;
  param.b_aud = 0;

  // return macroblock rows intead of NAL units
  // XXX x264 must also be modified to not produce macroblocks utilizing
  // planar prediction. this isn't toggleable at runtime for now...
  param.b_drh_mode = 1;

  // Yield one complete frame serially.
  param.i_threads = threads_;
  param.i_sync_lookahead = 0;
  param.b_sliced_threads = 0;
  param.i_slice_count = 1;
  param.nalu_process = H264Encoder::ProcessNalUnitTrampoline;

  // Logging parameters.
  param.i_log_level = X264_LOG_INFO;
  param.pf_log = LogEncoderMessages;
  // DRC_ENC_STATS=1: x264 calcula PSNR y SSIM y los imprime al cerrar (solo para medir; cuesta CPU).
  if (getenv("DRC_ENC_STATS") && getenv("DRC_ENC_STATS")[0] == '1') {
    param.analyse.b_psnr = 1;
    param.analyse.b_ssim = 1;
  }

  mb_info_ = drc::MbInfoEnabled(getenv("DRC_MB_INFO"), getenv("DRC_ALLOW_PSKIP"));
  param.analyse.b_mb_info = mb_info_ ? 1 : 0;
  fprintf(stderr, "[libdrc] mb_info=%d\n", mb_info_ ? 1 : 0);

  x264_param_apply_profile(&param, "main");

  encoder_ = x264_encoder_open(&param);
  assert(x264_encoder_maximum_delayed_frames(encoder_) == 0 ||
             "Encoder parameters will cause frames to be delayed");

  // If dumping to file, open the dump file here.
  if (kDebugDumpToFile) {
    dump_file_ = fopen("dump.h264", "wb");
    if (dump_file_) {
      DumpH264Headers(dump_file_);
    }
  }
}

void H264Encoder::DestroyEncoder() {
  if (encoder_) {
    x264_encoder_close(encoder_);
    encoder_ = nullptr;
  }
  if (dump_file_) {
    fclose(dump_file_);
    dump_file_ = nullptr;
  }
}

const H264ChunkArray& H264Encoder::Encode(const std::vector<byte>& frame,
                                          bool idr) {
  x264_picture_t input;
  x264_picture_init(&input);

  // x264 requires us to pass a non-const pointer even though it does not
  // modify the input data. Cheat a bit to keep a clean interface.
  byte* data = const_cast<byte*>(frame.data());

  // Cada frame lleva su plaza: con varios hilos, x264 avisa de frames distintos a la vez.
  FrameSlot* slot = &slots_[next_slot_++ % kFrameSlots];
  slot->chunks.fill(H264Chunk(nullptr, 0));
  slot->count = 0;
  input.opaque = slot;
  input.i_pts = next_pts_++;

  // Input image parameters.
  input.img.i_csp = X264_CSP_I420;
  input.img.i_plane = 3;

  // Plane 0: Y, offset 0, 1 byte per horizontal pixel
  input.img.i_stride[0] = kScreenWidth;
  input.img.plane[0] = data;

  // Plane 1: U, offset sizeof(Y), 0.5 byte per horizontal pixel
  input.img.i_stride[1] = kScreenWidth / 2;
  input.img.plane[1] = input.img.plane[0] + (kScreenWidth * kScreenHeight);

  // Plane 2: V, offset sizeof(Y)+sizeof(U), 0.5 byte per horizontal pixel
  input.img.i_stride[2] = kScreenWidth / 2;
  input.img.plane[2] = input.img.plane[1] + (kScreenWidth * kScreenHeight) / 4;

  if (idr) {
    input.i_type = X264_TYPE_IDR;
    // DRC_IDR_QP: QP propio para los IDR (más alto = IDR más pequeño). Los IDR de
    // 20-30 KB en ráfaga son los que el GamePad no acepta y encadenan resync.
    static const int idr_qp = [] {
      const char* e = getenv("DRC_IDR_QP");
      int v = e ? atoi(e) : 0;
      if (v < 10 || v > 51) v = 0;
      if (v) fprintf(stderr, "[libdrc] idr_qp=%d\n", v);
      return v;
    }();
    if (idr_qp) input.i_qpplus1 = idr_qp + 1;
  } else {
    input.i_type = X264_TYPE_P;
  }

  // DRC_FRAME_MAX (bytes) y DRC_FRAME_QP_STEP: tope por frame de lazo cerrado (frame-cap.h).
  drc::FrameCap& frame_cap = FrameCapInstance();
  if (frame_cap.enabled() && !(idr && input.i_qpplus1)) {
    // Con QP dinámico el IDR sale al QP máximo (32): a QP 12 sería enorme.
    const bool dyn_idr = idr && getenv("DRC_QP_DYN") && getenv("DRC_QP_DYN")[0] == '1';
    input.i_qpplus1 = (dyn_idr ? 32 : frame_cap.qp()) + 1;
  }

  // Since we use a nalu_process callback, these will/might not contain correct
  // data (especially since we do not behave well and NAL encode the NAL units
  // sent to us by x264).
  if (mb_info_) {
    // x264 se queda con el puntero hasta terminar el frame (con hilos, más tarde) y lo libera con mb_info_free.
    byte* info = static_cast<byte*>(malloc(drc::MbCount(kScreenWidth, kScreenHeight)));
    if (info) {
      drc::MarkConstantMbs(frame.data(), &prev_frame_, kScreenWidth, kScreenHeight, info);
      input.prop.mb_info = info;
      input.prop.mb_info_free = free;
    }
  }

  x264_nal_t* nals;
  int nals_count;
  x264_picture_t output;

  has_output_ = x264_encoder_encode(encoder_, &nals, &nals_count, &input, &output) > 0;
  if (!has_output_) {
    return empty_;
  }
  return TakeOutput(&output);
}

int H264Encoder::Delayed() const {
  return x264_encoder_delayed_frames(encoder_);
}

const H264ChunkArray& H264Encoder::Flush() {
  has_output_ = false;
  if (x264_encoder_delayed_frames(encoder_) <= 0) {
    return empty_;
  }
  x264_nal_t* nals;
  int nals_count;
  x264_picture_t output;
  has_output_ = x264_encoder_encode(encoder_, &nals, &nals_count, nullptr, &output) > 0;
  if (!has_output_) {
    return empty_;
  }
  return TakeOutput(&output);
}

const H264ChunkArray& H264Encoder::TakeOutput(x264_picture_t* output) {
  output_idr_ = output->b_keyframe != 0;
  drc::FrameCap& frame_cap = FrameCapInstance();
  H264ChunkArray& chunks_ = static_cast<FrameSlot*>(output->opaque)->chunks;

  // DRC_MIN_CHUNK: mínimo de bytes por trozo (por defecto desactivado; 4 = como la consola): se mueven las
  // fronteras del flujo continuo cuando un trozo queda con 1-2 bytes.
  static const int min_chunk = drc::MinChunkFromEnv(getenv("DRC_MIN_CHUNK"));
  if (min_chunk > 0) {
    int len[kH264ChunksPerFrame];
    bool contiguous = true;
    for (int k = 0; k < kH264ChunksPerFrame; k++) {
      len[k] = static_cast<int>(std::get<1>(chunks_[k]));
      if (k > 0 && std::get<0>(chunks_[k]) != std::get<0>(chunks_[k - 1]) + std::get<1>(chunks_[k - 1])) {
        contiguous = false;
      }
    }
    if (contiguous) {
      drc::BalanceChunkLengths(len, kH264ChunksPerFrame, min_chunk);
      const auto* base = std::get<0>(chunks_[0]);
      size_t off = 0;
      for (int k = 0; k < kH264ChunksPerFrame; k++) {
        chunks_[k] = std::make_tuple(base + off, static_cast<size_t>(len[k]));
        off += len[k];
      }
    }
  }

  if (frame_cap.enabled()) {
    int total = 0;
    for (const auto& c : chunks_) total += static_cast<int>(std::get<1>(c));
    frame_cap.Update(total);
  }

  // If debug dumping is enabled, create a frame from the generated chunks.
  if (dump_file_) {
    DumpH264Frame(dump_file_, chunks_, output_idr_);
  }

  return chunks_;
}

void H264Encoder::ProcessNalUnit(FrameSlot* slot, x264_nal_t* nal) {
  if (nal->i_type == NAL_SEI) {
    return;
  }
  int mb_per_frame = ((kScreenWidth + 15) / 16) * ((kScreenHeight + 15) / 16);
  int mb_per_chunk = mb_per_frame / kH264ChunksPerFrame;
  int chunk_idx = nal->i_first_mb / mb_per_chunk;
  slot->chunks[chunk_idx] = std::make_tuple(nal->p_payload, nal->i_payload);

  slot->count++;
  assert(slot->count <= 5);
}

void H264Encoder::ProcessNalUnitTrampoline(x264_t* h, x264_nal_t* nal,
                                           void* opaque) {
  FrameSlot* slot = static_cast<FrameSlot*>(opaque);
  slot->encoder->ProcessNalUnit(slot, nal);
}

}  // namespace drc
