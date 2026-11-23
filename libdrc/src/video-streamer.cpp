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

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <drc/internal/astrm-packet.h>
#include <drc/internal/astrm-ts.h>
#include <drc/internal/async-encoder.h>
#include <drc/internal/chunk-feed.h>
#include <drc/internal/chunk-tx.h>
#include <drc/internal/h264-encoder.h>
#include <drc/internal/replay.h>
#include <drc/internal/ts-grid.h>
#include <drc/internal/idr-scheduler.h>
#include <drc/internal/idr-ts.h>
#include <drc/internal/init-flag.h>
#include <drc/internal/kick.h>
#include <drc/internal/reanchor.h>
#include <drc/internal/stream-stats.h>
#include <drc/internal/tick-grid.h>
#include <drc/internal/tsf.h>
#include <drc/internal/udp.h>
#include <time.h>
#include <unistd.h>
#include <drc/internal/tsf-clock.h>
#include <drc/internal/video-streamer.h>
#include <drc/internal/vstrm-packet.h>
#include <drc/screen.h>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <tuple>
#include <vector>




namespace drc {

namespace {



s32 GetTimestamp() {
  u64 tsf;
  GetTsf(&tsf);
  return static_cast<s32>(tsf & 0xFFFFFFFF);
}

// Diagnóstico (DRC_IDR_TS_SHIFT_US / /tmp/drc_idr_ts_shift_us, 1 lectura/s): desplazamiento del ts del vstrm de los IDR. Ver idr-ts.h.
static int64_t IdrTsShiftNow() {
  static const int64_t default_shift = IdrTsShiftFromEnv(getenv("DRC_IDR_TS_SHIFT_US"));
  static int64_t shift_us = default_shift;
  static std::chrono::steady_clock::time_point next_read;
  const auto now = std::chrono::steady_clock::now();
  if (now >= next_read) {
    next_read = now + std::chrono::seconds(1);
    long long v = 0;
    bool have = false;
    if (FILE* f = fopen("/tmp/drc_idr_ts_shift_us", "r")) {
      have = fscanf(f, "%lld", &v) == 1;
      fclose(f);
    }
    shift_us = IdrTsShiftValue(have, v, default_shift);
  }
  return shift_us;
}

void GenerateVstrmPackets(std::vector<VstrmPacket>* vstrm_packets,
                          const H264ChunkArray& chunks, u32 timestamp,
                          bool idr, bool* vstrm_inited, u16* vstrm_seqid,
                          VideoFrameRate frame_rate) {
  // Set the init flag on the first frame ever sent.
  const int64_t idr_ts_shift = idr ? IdrTsShiftNow() : 0;
  bool init_flag = !*vstrm_inited;
  if (init_flag) {
    *vstrm_inited = true;
  }

  static const size_t max_payload = VstrmPayloadFromEnv(getenv("DRC_VSTRM_PAYLOAD"));
  static const bool idr_init = IdrInitEnabled(getenv("DRC_IDR_INIT"));
  static bool logged = false;
  if (!logged) {
    fprintf(stderr, "[libdrc] vstrm_payload=%zu idr_init=%d\n", max_payload, idr_init ? 1 : 0);
    logged = true;
  }

  for (size_t i = 0; i < chunks.size(); ++i) {
    bool first_chunk = (i == 0);
    bool last_chunk = (i == chunks.size() - 1);

    const byte* chunk_data;
    size_t chunk_size;
    std::tie(chunk_data, chunk_size) = chunks[i];

    bool first_packet = true;
    do {
      const byte* pkt_data = chunk_data;
      size_t pkt_size = std::min(max_payload, chunk_size);

      chunk_data += pkt_size;
      chunk_size -= pkt_size;

      bool last_packet = (chunk_size == 0);

      vstrm_packets->resize(vstrm_packets->size() + 1);
      VstrmPacket* pkt = &vstrm_packets->at(vstrm_packets->size() - 1);

      u16 seqid = (*vstrm_seqid)++;
      if (*vstrm_seqid >= 1024) {
        *vstrm_seqid = 0;
      }
      pkt->SetSeqId(seqid);

      pkt->SetPayload(pkt_data, pkt_size);
      pkt->SetTimestamp(IdrTsApply(timestamp, idr, idr_ts_shift));

      pkt->SetInitFlag(init_flag);
      pkt->SetFrameBeginFlag(first_chunk && first_packet);
      pkt->SetChunkEndFlag(last_packet);
      pkt->SetFrameEndFlag(last_chunk && last_packet);

      pkt->SetIdrFlag(idr);
      pkt->SetFrameRate(frame_rate);
      // Como la consola real (stream capturado conectando la Pi a una Wii U como
      // GamePad): frame rate primero y nunca flag init.
      //   P:   82 fr 83 85 06 00 00 00     IDR: 80 82 fr 83 85 06 00 00
      if (!getenv("DRC_LIBDRC_EXT_HEADER")) {
        byte fr = static_cast<byte>(frame_rate);
        const byte ext_p[8] = {0x82, fr, 0x83, 0x85, 0x06, 0x00, 0x00, 0x00};
        const byte ext_idr[8] = {0x80, 0x82, fr, 0x83, 0x85, 0x06, 0x00, 0x00};
        pkt->SetRawExtHeader(idr ? ext_idr : ext_p);
        pkt->SetInitFlag(VstrmInitFlag(idr, init_flag, idr_init));
      }

      first_packet = false;
    } while (chunk_size != 0);
  }
}

// Primera palabra de 16000 del astrm de formato: cambiarla hace que el mando reancle el vsync (reanchor.h).
u32 g_astrm_format_word = AstrmFormatWord(0);

void GenerateAstrmPacket(AstrmPacket* pkt, u32 ts) {
  pkt->ResetPacket();
  pkt->SetPacketType(AstrmPacketType::kVideoFormat);
  //pkt->SetTimestamp(0x00100000);
  pkt->SetTimestamp(0x00000000);

  // Diagnóstico: desplazar solo el ts del astrm (1 lectura/s de /tmp/drc_astrm_ts_delta_us).
  static const int64_t default_delta = AstrmTsDeltaFromEnv(getenv("DRC_ASTRM_TS_DELTA_US"));
  static int64_t delta_us = default_delta;
  static std::chrono::steady_clock::time_point next_read;
  const auto now = std::chrono::steady_clock::now();
  if (now >= next_read) {
    next_read = now + std::chrono::seconds(1);
    long long v = 0;
    bool have = false;
    if (FILE* f = fopen("/tmp/drc_astrm_ts_delta_us", "r")) {
      have = fscanf(f, "%lld", &v) == 1;
      fclose(f);
    }
    delta_us = AstrmTsDeltaValue(have, v, default_delta);
  }
  ts = AstrmTsApplyDelta(ts, delta_us);

  // Valores medidos en el stream real de la consola (la Pi conectada a una Wii U
  // como GamePad): 16000, 16000, 21000, 21000. libdrc enviaba 16000 en los cuatro.
  byte payload[24] = {
      0x00, 0x00, 0x00, 0x00,
      0x80, 0x3e, 0x00, 0x00,
      0x80, 0x3e, 0x00, 0x00,
      0x08, 0x52, 0x00, 0x00,
      0x08, 0x52, 0x00, 0x00,
      0x00, 0x00, 0x00, 0x00,
  };
  payload[0] = ts & 0xFF;
  payload[1] = (ts >> 8) & 0xFF;
  payload[2] = (ts >> 16) & 0xFF;
  payload[3] = (ts >> 24) & 0xFF;
  payload[4] = payload[8] = g_astrm_format_word & 0xFF;
  payload[5] = payload[9] = (g_astrm_format_word >> 8) & 0xFF;
  pkt->SetPayload(payload, sizeof (payload));
}

std::unique_ptr<H264ChunkArray> AsChunkArray(
    const std::vector<std::string>& chunks) {
  auto chunk_array = std::make_unique<H264ChunkArray>();
  size_t index = 0u;
  for (const auto& chunk : chunks) {
    if (index >= kH264ChunksPerFrame) {
      // More chunks than expected.
      break;
    }
    (*chunk_array)[index] = std::make_tuple(
        reinterpret_cast<const unsigned char*>(chunk.data()), chunk.size());
    index++;
  }
  return chunk_array;
}

}  // namespace

VideoStreamer::VideoStreamer(const std::string& vid_dst,
                             const std::string& aud_dst)
    : astrm_client_(new UdpClient(aud_dst)),
      vstrm_client_(new UdpClient(vid_dst)),
      encoder_(new H264Encoder()),
      resync_evt_(NULL) {
}

VideoStreamer::~VideoStreamer() {
  Stop();
}

bool VideoStreamer::Start() {
  if (!astrm_client_->Start() || !vstrm_client_->Start()) {
    Stop();
    return false;
  }
  const unsigned pacing = PacingRateFromEnv(getenv("DRC_PACING_KBPS"));
  if (!vstrm_client_->SetMaxPacingRate(pacing)) {
    fprintf(stderr, "[libdrc] SO_MAX_PACING_RATE falló\n");
  }
  fprintf(stderr, "[libdrc] pacing_kbps=%u\n", pacing * 8 / 1000);
  const int tos = TosFromEnv(getenv("DRC_TOS"));
  if (tos >= 0) {
    vstrm_client_->SetTos(tos);
    astrm_client_->SetTos(tos);
    fprintf(stderr, "[libdrc] tos=0x%02x\n", tos);
  }

  const char* replay_path = getenv("DRC_REPLAY");
  if (replay_path && replay_path[0]) {
    replay_stop_ = false;
    replay_thread_ = std::thread([this, replay_path]() {
      // El GamePad se desasocia si no recibe vídeo enseguida (con 8 s de espera lo hizo antes de la
      // primera reproducción): DRC_REPLAY_DELAY_MS, por defecto 500 ms.
      const char* d = getenv("DRC_REPLAY_DELAY_MS");
      std::this_thread::sleep_for(std::chrono::milliseconds(d ? atoi(d) : 500));
      RunReplay(replay_path);
    });
  }

  StartEM();
  return true;
}

void VideoStreamer::Stop() {
  replay_stop_ = true;
  if (replay_thread_.joinable()) replay_thread_.join();
  StopEM();
  resync_evt_ = NULL;

  astrm_client_->Stop();
  vstrm_client_->Stop();
}

void VideoStreamer::PushFrame(std::vector<byte>* frame) {
  std::lock_guard<std::mutex> lk(frame_mutex_);
  frame_ = std::move(*frame);
}

void VideoStreamer::PushH264Chunks(std::vector<std::string>&& chunks) {
  std::lock_guard<std::mutex> lk(frame_mutex_);
  h264_chunks_ = chunks;
}

// Reenvía una captura de la consola (registros "<dQHI": instante, tsf, puerto, longitud + datos
// UDP; los que escribe rpi4/wiiu_as_gamepad.py) tal cual: mismos paquetes vstrm (50120) y astrm
// (50121), misma separación temporal, solo con el timestamp desplazado al TSF de nuestro AP.
void VideoStreamer::RunReplay(const std::string& path) {
  struct Rec { double t; unsigned short port; std::vector<unsigned char> d; };
  std::vector<Rec> recs;
  FILE* f = fopen(path.c_str(), "rb");
  if (!f) { fprintf(stderr, "[libdrc] replay: no se pudo abrir %s\n", path.c_str()); return; }
  unsigned char h[22];
  while (fread(h, 1, sizeof (h), f) == sizeof (h)) {
    Rec r; unsigned n = 0;
    memcpy(&r.t, h, 8); memcpy(&r.port, h + 16, 2); memcpy(&n, h + 18, 4);
    r.d.resize(n);
    if (n && fread(r.d.data(), 1, n, f) != n) break;
    if (r.port == 50120 || r.port == 50121) recs.push_back(std::move(r));
  }
  fclose(f);
  size_t first = 0;
  while (first < recs.size() && recs[first].port != 50120) first++;  // empezar en el primer vstrm
  if (first >= recs.size()) { fprintf(stderr, "[libdrc] replay: sin paquetes vstrm\n"); return; }
  const double t0 = recs[first].t;
  const unsigned first_ts = (recs[first].d[4] << 24) | (recs[first].d[5] << 16) |
                            (recs[first].d[6] << 8) | recs[first].d[7];
  const s32 interval = 16683;
  // El primer paquete sale `lead_ns` después de este instante: el timestamp debe corresponder a ese
  // instante de envío (si no, todos salían 300 ms caducados y el GamePad los descartaba).
  const unsigned long long lead_ns = 100000000ULL;
  const unsigned delta = SendTimestamp(GetTimestamp() + static_cast<s32>(lead_ns / 1000), interval) - first_ts;
  vstrm_client_->EnableTxTime();
  astrm_client_->EnableTxTime();
  struct timespec ts_now;
  clock_gettime(CLOCK_MONOTONIC, &ts_now);
  const unsigned long long start_ns =
      static_cast<unsigned long long>(ts_now.tv_sec) * 1000000000ULL + ts_now.tv_nsec + lead_ns;
  fprintf(stderr, "[libdrc] replay: %zu paquetes de %s, desfase de timestamp %u\n",
          recs.size() - first, path.c_str(), delta);
  const ReplayAstrm astrm_mode = ReplayAstrmFromEnv(getenv("DRC_REPLAY_ASTRM"));
  fprintf(stderr, "[libdrc] replay astrm=%s\n", astrm_mode == ReplayAstrm::kNone ? "none" :
          astrm_mode == ReplayAstrm::kFormat ? "fmt" : "all");
  size_t sent = 0;
  for (size_t i = first; i < recs.size() && !replay_stop_; ++i) {
    Rec& r = recs[i];
    const unsigned long long tx = start_ns + static_cast<unsigned long long>((r.t - t0) * 1e9);
    // No adelantarse más de 20 ms: la cola fq tiene un límite por flujo.
    for (;;) {
      clock_gettime(CLOCK_MONOTONIC, &ts_now);
      unsigned long long now = static_cast<unsigned long long>(ts_now.tv_sec) * 1000000000ULL + ts_now.tv_nsec;
      if (replay_stop_ || tx <= now + 20000000ULL) break;
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    if (r.port == 50120) {
      RetimeVstrm(r.d.data(), r.d.size(), delta);
      vstrm_client_->SendAt(r.d.data(), r.d.size(), tx);
    } else {
      if (!ReplayKeepAstrm(astrm_mode, r.d.data(), r.d.size())) continue;
      RetimeAstrm(r.d.data(), r.d.size(), delta);
      astrm_client_->SendAt(r.d.data(), r.d.size(), tx);
    }
    sent++;
  }
  fprintf(stderr, "[libdrc] replay: terminado, %zu paquetes enviados\n", sent);
}

void VideoStreamer::ResyncStream() {
  if (resync_evt_) {
    resync_evt_->Trigger();
  }
}

void VideoStreamer::SetFrameRate(VideoFrameRate frame_rate) {
  frame_rate_ = frame_rate;
}

void VideoStreamer::InitEventsAndRun() {
  const bool stats_enabled = getenv("DRC_STREAM_STATS") != NULL;
  // La consola real envía cada chunk según lo codifica (~2.8 ms entre chunks,
  // medido con una captura del enlace Wii U <-> GamePad). Por defecto 0
  // (ráfaga): con 2800 us no cambió el resync y bajaba a 30 fps.
  const char* spacing_env = getenv("DRC_CHUNK_SPACING_US");
  const int chunk_spacing_us = spacing_env ? atoi(spacing_env) : 0;
  fprintf(stderr, "[libdrc] chunk_spacing_us=%d\n",
          chunk_spacing_us);
  // DRC_FRAME_SPREAD_US: ritmo de envío por frame (udp.h); el de DRC_PACING_KBPS es el mínimo.
  const int frame_spread_us = FrameSpreadUsFromEnv(getenv("DRC_FRAME_SPREAD_US"));
  const bool txtime_on = frame_spread_us > 0 && vstrm_client_->EnableTxTime();
  fprintf(stderr, "[libdrc] frame_spread_us=%d txtime=%d\n", frame_spread_us, txtime_on ? 1 : 0);
  // DRC_CHUNK_TX_US: los chunks de un frame salen espaciados con SO_TXTIME (la consola: ~2800 us), sin dormir.
  const int chunk_tx_us = txtime_on ? ChunkTxSpacingFromEnv(getenv("DRC_CHUNK_TX_US")) : 0;
  const char* chunk_intra_env = getenv("DRC_CHUNK_INTRA_US");
  const int chunk_intra_us = chunk_intra_env ? atoi(chunk_intra_env) : 0;
  fprintf(stderr, "[libdrc] chunk_tx_us=%d chunk_intra_us=%d\n", chunk_tx_us, chunk_intra_us);
  StreamStats stats;
  auto stats_start = std::chrono::steady_clock::now();
  auto last_tick = stats_start;

  // DRC_RESYNC_LOG=1: una línea por resync con los ticks desde el último IDR
  // (¿pide el GamePad resync en respuesta a cada IDR periódico / su hueco?).
  const bool resync_log = getenv("DRC_RESYNC_LOG") != NULL;
  const int idr_interval = IdrIntervalFromEnv(getenv("DRC_IDR_INTERVAL"));
  fprintf(stderr, "[libdrc] idr_interval=%d\n", idr_interval);
  const int idr_min_ticks = IdrMinTicksFromEnv(getenv("DRC_IDR_MIN_TICKS"));
  fprintf(stderr, "[libdrc] idr_min_ticks=%d\n", idr_min_ticks);
  IdrScheduler idr_scheduler(idr_interval, idr_min_ticks);
  u64 tick_count = 0;
  u64 last_idr_tick = 0;
  bool resync_requested = false;
  bool resync_this_tick = false;
  // DRC_REANCHOR_TICKS: tras una ráfaga de N ticks, realinear el vsync del mando (reanchor.h).
  ReanchorPolicy reanchor(ReanchorTicksFromEnv(getenv("DRC_REANCHOR_TICKS")), kReanchorCooldownTicks);
  unsigned reanchor_generation = 0;
  unsigned reanchor_count = 0;
  fprintf(stderr, "[libdrc] reanchor_ticks=%d\n", ReanchorTicksFromEnv(getenv("DRC_REANCHOR_TICKS")));
  // DRC_IDR_RESYNC_IGNORE_TICKS: un resync en vuelo (hasta N ticks tras salir el IDR) no pide otro IDR.
  // La ventana cuenta desde el tick en que el IDR SALE (se envía), no desde el que se codifica.
  const int resync_ignore_ticks = IdrResyncIgnoreTicksFromEnv(getenv("DRC_IDR_RESYNC_IGNORE_TICKS"));
  fprintf(stderr, "[libdrc] idr_resync_ignore_ticks=%d\n", resync_ignore_ticks);
  bool idr_sent_once = false;
  bool idr_in_packets = false;  // los paquetes pendientes de enviar son de un IDR
  bool idr_in_pipeline = false;  // hay un IDR pedido a x264 que aún no ha salido
  u64 idr_sent_tick = 0;
  // Tamaño de los 3 últimos frames enviados (el más reciente primero; negativo = IDR),
  // para ver si los resync siguen a frames grandes.
  long recent_bytes[3] = {0, 0, 0};
  resync_evt_ = NewTriggerableEvent([&](Event*) {
    // Con x264 en varios hilos el IDR tarda varios ticks: los avisos de mientras tanto no deben pedir otro.
    const bool in_flight = idr_in_pipeline || (idr_sent_once &&
        ResyncInFlight(static_cast<int>(tick_count - idr_sent_tick), resync_ignore_ticks));
    if (resync_log) {
      fprintf(stderr, "[libdrc] resync tick=%llu since_idr_tick=%llu pending=%d last=%ld,%ld,%ld%s\n",
              (unsigned long long)tick_count,
              (unsigned long long)(tick_count - last_idr_tick),
              resync_requested ? 1 : 0,
              recent_bytes[0], recent_bytes[1], recent_bytes[2],
              in_flight ? " ignored=1" : "");
    }
    if (!in_flight) resync_requested = true;
    resync_this_tick = true;
    stats.AddResync();
    return true;
  });

  std::vector<std::string> h264_chunks;
  // DRC_CHUNK_FEED=<fichero CFD1>: en vez de x264, cada tick toma el siguiente frame H.264 de la
  // consola (rpi4/chunkfeed.py) y lo entrega por el camino de los chunks precodificados.
  ChunkFeed chunk_feed;
  bool feed_on = false;
  bool feed_idr = false;
  uint64_t feed_rewind_tick = 0;
  if (const char* feed_path = getenv("DRC_CHUNK_FEED")) {
    if (feed_path[0]) {
      std::string blob;
      if (FILE* ff = fopen(feed_path, "rb")) {
        char b[65536];
        size_t n;
        while ((n = fread(b, 1, sizeof (b), ff)) > 0) blob.append(b, n);
        fclose(ff);
      }
      feed_on = chunk_feed.Load(blob);
      fprintf(stderr, "[libdrc] chunk_feed=%s frames=%zu\n", feed_on ? "on" : "ERROR", chunk_feed.size());
    }
  }
  std::vector<byte> encoding_frame;

  // DRC_CFD_DUMP=<fichero>: graba lo que produce x264 (DRC_CFD_DUMP_FRAMES frames) para reproducirlo con DRC_CHUNK_FEED.
  const char* cfd_dump_path = getenv("DRC_CFD_DUMP");
  const char* cfd_frames_env = getenv("DRC_CFD_DUMP_FRAMES");
  ChunkFeedWriter cfd_writer(cfd_frames_env ? static_cast<uint32_t>(atoi(cfd_frames_env)) : 4200);
  bool cfd_written = false;
  auto cfd_add = [&](const H264ChunkArray& chunks, bool idr) {
    if (!cfd_dump_path || cfd_written) return;
    std::vector<std::string> copy;
    for (const auto& c : chunks) {
      const byte* ptr;
      size_t n;
      std::tie(ptr, n) = c;
      copy.emplace_back(reinterpret_cast<const char*>(ptr), ptr ? n : 0);
    }
    cfd_writer.Add(idr, copy);
    if (cfd_writer.Full()) {
      cfd_written = true;
      const std::string blob = cfd_writer.Blob();
      if (FILE* f = fopen(cfd_dump_path, "wb")) {
        fwrite(blob.data(), 1, blob.size(), f);
        fclose(f);
      }
      fprintf(stderr, "[libdrc] cfd_dump escrito: %zu B\n", blob.size());
    }
  };

  bool vstrm_inited = false;
  u16 vstrm_seqid = 0;
  std::vector<VstrmPacket> vstrm_packets;
  bool send_idr = false;
  // La consola real deja un frame vacío (solo astrm de formato, sin vstrm)
  // antes y después de cada IDR: ... P | [hueco] | IDR | [hueco] | P ...
  // (30/30 IDR en el stream real descifrado). DRC_IDR_GAP=0 lo desactiva.
  const char* idr_gap_env = getenv("DRC_IDR_GAP");
  const bool idr_gaps = !(idr_gap_env && idr_gap_env[0] == '0');
  bool idr_after_gap = false;  // el hueco previo ya se emitió: este tick va el IDR
  int gap_ticks = 0;           // huecos pendientes tras un IDR
  const int idr_gap_after = idr_gaps ? IdrGapAfterFromEnv(getenv("DRC_IDR_GAP_AFTER")) : 0;
  fprintf(stderr, "[libdrc] idr_gaps=%d gap_after=%d\n", idr_gaps ? 1 : 0, idr_gap_after);

  // DRC_ASTRM_TS_MATCH=1: el astrm de formato lleva el mismo timestamp que el vídeo (como la consola).
  const bool astrm_ts_match = AstrmTsMatchEnabled(getenv("DRC_ASTRM_TS_MATCH"));
  fprintf(stderr, "[libdrc] astrm_ts_match=%d\n", astrm_ts_match ? 1 : 0);
  const char* stamp_env = getenv("DRC_STAMP_AT_SEND");
  const bool stamp_at_send = !(stamp_env && stamp_env[0] == '0');
  const s32 send_frame_interval = static_cast<s32>(
      1000000.0 / (frame_rate_ == VideoFrameRate::k29_97Hz ? 29.97 :
                   frame_rate_ == VideoFrameRate::k25Hz ? 25.0 :
                   frame_rate_ == VideoFrameRate::k50Hz ? 50.0 : 59.94));
  fprintf(stderr, "[libdrc] stamp_at_send=%d\n", stamp_at_send ? 1 : 0);
  // DRC_TS_GRID=1: timestamps en rejilla regular como la consola (deltas 16683 +-1 us).
  const bool ts_grid_on = getenv("DRC_TS_GRID") && getenv("DRC_TS_GRID")[0] == '1';
  const double grid_period_us = frame_rate_ == VideoFrameRate::k29_97Hz ? 1000000.0 / 29.97 :
                                frame_rate_ == VideoFrameRate::k25Hz ? 40000.0 :
                                frame_rate_ == VideoFrameRate::k50Hz ? 20000.0 : 1000000.0 / 59.94;
  const double ts_slew_us = TsSlewFromEnv(getenv("DRC_TS_SLEW_US"));
  TsGrid ts_grid(grid_period_us, ts_slew_us);
  fprintf(stderr, "[libdrc] ts_grid=%d ts_slew_us=%.1f\n", ts_grid_on ? 1 : 0, ts_slew_us);
  const bool tick_grid_on = TickGridEnabled(getenv("DRC_TICK_GRID"));
  TickGrid tick_grid(grid_period_us);
  fprintf(stderr, "[libdrc] tick_grid=%d\n", tick_grid_on ? 1 : 0);
  double ts_err_max = 0.0;
  unsigned filler_count = 0;
  unsigned flush_count = 0;
  unsigned refine_count = 0;
  auto next_kick_read = std::chrono::steady_clock::now();
  // Una línea de fase por segundo: separa los disparadores de ráfaga (ver INDICE.md).
  auto phase_line = [&]() {
    long long anchor_max = 0;
    int anchor_n = 0;
    TakeTsfAnchorStats(&anchor_max, &anchor_n);
    const int gate_rej = TakeTsfGateRejects();
    fprintf(stderr, "[libdrc] phase ts_err=%.0f ts_err_max=%.0f tick_skip=%llu anchor_n=%d anchor_max=%lld gate_rej=%d reanchor=%u filler=%u flush=%u refine=%u\n",
            ts_grid.LastError(), ts_err_max, (unsigned long long)tick_grid.Skipped(), anchor_n, anchor_max, gate_rej,
            reanchor_count, filler_count, flush_count, refine_count);
    ts_err_max = 0.0;
  };

  AstrmPacket astrm_packet;
  s32 next_timestamp = GetTimestamp();
  const bool replay_mode = getenv("DRC_REPLAY") && getenv("DRC_REPLAY")[0];
  // DRC_ASYNC_ENCODE=1: x264 en su propio hilo con una sola plaza; el tick no se bloquea codificando.
  std::unique_ptr<AsyncEncoder> async_enc;
  if (AsyncEncodeEnabled(getenv("DRC_ASYNC_ENCODE")) && !replay_mode && !feed_on) {
    async_enc.reset(new AsyncEncoder([this](const std::vector<byte>& frame, bool idr) {
      // Un trabajo sin imagen es un vaciado (DRC_ENC_FLUSH): x264 entrega el siguiente frame que retiene.
      const H264ChunkArray& c = frame.empty() ? encoder_->Flush() : encoder_->Encode(frame, idr);
      std::vector<std::string> out;
      for (const auto& t : c) {
        const byte* ptr;
        size_t n;
        std::tie(ptr, n) = t;
        out.emplace_back(reinterpret_cast<const char*>(ptr), ptr ? n : 0);
      }
      if (!encoder_->HasOutput()) out.clear();
      return out;
    }, [this] { return encoder_->OutputIsIdr(); }));
  }
  fprintf(stderr, "[libdrc] async_encode=%d\n", async_enc ? 1 : 0);
  // DRC_ENC_FLUSH=1: un tick sin frame nuevo no recodifica el último; vacía x264 o deja salir el relleno (idr-scheduler.h).
  const bool enc_flush = async_enc && EncFlushEnabled(getenv("DRC_ENC_FLUSH"));
  const std::vector<byte> no_frame;
  // Afinado de la imagen quieta: DRC_REFINE_TICKS recodificaciones tras DRC_REFINE_AFTER ticks sin frame nuevo.
  const int refine_ticks = RefineFromEnv(getenv("DRC_REFINE_TICKS"), kDefaultRefineTicks);
  const int refine_after = RefineFromEnv(getenv("DRC_REFINE_AFTER"), kDefaultRefineAfter);
  int refine_left = 0;
  fprintf(stderr, "[libdrc] refine_ticks=%d refine_after=%d\n", refine_ticks, refine_after);
  fprintf(stderr, "[libdrc] enc_flush=%d\n", enc_flush ? 1 : 0);

  // Frame de relleno: P todo P_Skip. Repite la referencia sin cambiarla, así que x264 no se entera. Sale de codificar dos veces
  // la misma imagen gris en un codificador aparte: exige P_Skip permitido y sin intra refresh, y se comprueba por tamaño.
  std::vector<std::string> filler;
  bool prev_gap_tick = false;
  std::deque<AsyncEncoder::Result> pending;  // frames entregados por x264 aún sin enviar
  IdrSendGate send_gate(idr_gap_after);
  if (FillerEnabled(getenv("DRC_FILLER")) && !feed_on && !replay_mode) {
    H264Encoder gen(1);
    const std::vector<byte> gray(kScreenWidth * kScreenHeight * 3 / 2, 128);
    gen.Encode(gray, true);
    gen.Encode(gray, false);
    const H264ChunkArray& fc = gen.Encode(gray, false);
    size_t total = 0;
    std::vector<std::string> candidate;
    for (const auto& c : fc) {
      const byte* ptr;
      size_t n;
      std::tie(ptr, n) = c;
      candidate.emplace_back(reinterpret_cast<const char*>(ptr), ptr ? n : 0);
      total += ptr ? n : 0;
    }
    if (total > 0 && total <= 64) filler = candidate;
    fprintf(stderr, "[libdrc] filler=%d bytes=%zu\n", filler.empty() ? 0 : 1, total);
  }
  const bool idle_filler = IdleFillerEnabled(getenv("DRC_IDLE_FILLER"));
  const int idle_flush_ticks = IdleFlushTicksFromEnv(getenv("DRC_IDLE_FLUSH_TICKS"));
  int ticks_no_frame = 0;
  fprintf(stderr, "[libdrc] idle_filler=%d flush_ticks=%d\n", idle_filler ? 1 : 0, idle_flush_ticks);
  TimerEvent* timer_evt = NewTimerEvent(1, [&](Event*) {
    if (replay_mode) {
      // Reproducción íntegra: no se codifica ni se envía nada desde el tick, pero sí se imprimen las
      // estadísticas (antes no: los resync del replay se contaban y nunca se veían).
      if (stats_enabled) {
        auto now = std::chrono::steady_clock::now();
        double elapsed_s = std::chrono::duration<double>(now - stats_start).count();
        if (elapsed_s >= 1.0) {
          fprintf(stderr, "%s\n", stats.FormatAndReset(elapsed_s).c_str());
          stats_start = now;
        }
      }
      timer_evt->RearmTimer(static_cast<u64>(16683) * 1000);
      return true;
    }
    auto tick = std::chrono::steady_clock::now();
    stats.AddTickGap(std::chrono::duration_cast<std::chrono::microseconds>(
        tick - last_tick).count());
    last_tick = tick;
    tick_count++;

    // La rejilla avanza en cada tick (también en los de hueco), no solo al enviar.
    const u32 grid_ts = ts_grid_on
        ? ts_grid.Next(SendTimestamp(GetTimestamp(), send_frame_interval)) : 0;
    ts_err_max = std::max(ts_err_max, std::fabs(ts_grid.LastError()));

    bool do_reanchor = reanchor.Tick(resync_this_tick);
    resync_this_tick = false;
    if (tick >= next_kick_read) {
      next_kick_read = tick + std::chrono::seconds(1);
      if (FILE* f = fopen("/tmp/drc_kick", "r")) {
        char text[64] = {0};
        const size_t n = fread(text, 1, sizeof (text) - 1, f);
        fclose(f);
        unlink("/tmp/drc_kick");
        text[n] = '\0';
        const Kick kick = ParseKick(text);
        fprintf(stderr, "[libdrc] kick ts=%ld seq=%d reanchor=%d\n", kick.ts_us, kick.seq, kick.reanchor ? 1 : 0);
        if (kick.ts_us != 0) ts_grid.Shift(static_cast<double>(kick.ts_us));
        if (kick.seq != 0) vstrm_seqid = static_cast<u16>((vstrm_seqid + kick.seq) & 0x3FF);
        do_reanchor = do_reanchor || kick.reanchor;
      }
    }
    if (do_reanchor) {
      g_astrm_format_word = AstrmFormatWord(++reanchor_generation);
      reanchor_count++;
      resync_requested = true;
      if (ts_grid_on) ts_grid.Reset();
      fprintf(stderr, "[libdrc] reanchor word=%u\n", g_astrm_format_word);
    }

    bool send_gap = false;
    if (async_enc) {
      AsyncEncoder::Result taken;
      if (async_enc->TryTake(&taken) && !taken.chunks.empty()) pending.push_back(std::move(taken));
      const bool have = !pending.empty();
      send_gap = !send_gate.Next(have, have && pending.front().idr, vstrm_inited);
      if (!send_gap && have) {
        AsyncEncoder::Result r = std::move(pending.front());
        pending.pop_front();
        if (r.idr) idr_in_pipeline = false;
        std::unique_ptr<H264ChunkArray> arr = AsChunkArray(r.chunks);
        size_t frame_bytes = 0;
        for (const auto& c : r.chunks) frame_bytes += c.size();
        stats.AddFrame(r.idr, r.enc_ms, frame_bytes);
        recent_bytes[2] = recent_bytes[1];
        recent_bytes[1] = recent_bytes[0];
        recent_bytes[0] = r.idr ? -(long)frame_bytes : (long)frame_bytes;
        cfd_add(*arr, r.idr);
        GenerateVstrmPackets(&vstrm_packets, *arr, r.tag, r.idr, &vstrm_inited, &vstrm_seqid, frame_rate_);
        idr_in_packets = r.idr;
        GenerateAstrmPacket(&astrm_packet, r.tag);
        vstrm_inited = true;
      }
    }

    if (vstrm_inited && ShouldSendFiller(vstrm_packets.empty(), prev_gap_tick || send_gap, false, !filler.empty())) {
      std::unique_ptr<H264ChunkArray> arr = AsChunkArray(filler);
      GenerateVstrmPackets(&vstrm_packets, *arr, static_cast<u32>(GetTimestamp()), false, &vstrm_inited, &vstrm_seqid,
                           frame_rate_);
      filler_count++;
    }

    if (vstrm_inited) {
      // El astrm de formato va ANTES de los paquetes de su frame (mismo ts),
      // verificado en el stream real descifrado de la consola.
      if (stamp_at_send && (!vstrm_packets.empty() || astrm_ts_match)) {
        // Sellar con el TSF de este instante: si el tick llega tarde (CPU ocupada),
        // el frame no sale con un timestamp ya caducado para el GamePad.
        u32 ts = ts_grid_on ? grid_ts : SendTimestamp(GetTimestamp(), send_frame_interval);
        for (auto& p : vstrm_packets) {
          p.SetTimestamp(ts);
        }
        // Con match el astrm se resella en cada tick, también en los de hueco (sin vídeo).
        if (astrm_ts_match) {
          GenerateAstrmPacket(&astrm_packet, AstrmTickTimestamp(true, ts, 0));
        }
      }
      astrm_client_->Send(astrm_packet.GetBytes(), astrm_packet.GetSize());
      // Como la consola: el frame entero se reparte de forma uniforme en ~12,5 ms (SO_TXTIME).
      struct timespec now_ts;
      clock_gettime(CLOCK_MONOTONIC, &now_ts);
      const unsigned long long frame_start_ns =
          static_cast<unsigned long long>(now_ts.tv_sec) * 1000000000ULL + now_ts.tv_nsec;
      std::vector<unsigned long long> chunk_offsets;
      if (chunk_tx_us > 0) {
        std::vector<bool> chunk_end;
        for (const auto& p : vstrm_packets) chunk_end.push_back(p.ChunkEndFlag());
        chunk_offsets = ChunkTxOffsetsNs(chunk_end, chunk_tx_us, chunk_intra_us);
      }
      for (size_t i = 0; i < vstrm_packets.size(); ++i) {
        const auto& pkt = vstrm_packets[i];
        const unsigned long long offset_ns = chunk_tx_us > 0
            ? chunk_offsets[i]
            : FrameTxOffsetNs(static_cast<int>(i), static_cast<int>(vstrm_packets.size()), frame_spread_us);
        const bool sent = txtime_on
            ? vstrm_client_->SendAt(pkt.GetBytes(), pkt.GetSize(), frame_start_ns + offset_ns)
            : vstrm_client_->Send(pkt.GetBytes(), pkt.GetSize());
        if (!sent) {
          stats.AddSendFailure();
        }
        bool more_chunks = i + 1 < vstrm_packets.size();
        if (chunk_spacing_us > 0 && pkt.ChunkEndFlag() && more_chunks) {
          std::this_thread::sleep_for(std::chrono::microseconds(chunk_spacing_us));
        }
      }
      if (idr_in_packets && !vstrm_packets.empty()) {
        idr_sent_once = true;
        idr_sent_tick = tick_count;
        idr_in_packets = false;
      }
      vstrm_packets.clear();
      if (send_idr && resync_requested) {
        resync_requested = false;
      }
    }

    s32 timestamp = GetTimestamp();
    bool feed_gap = false;
    if (feed_on) {
      // Un flujo precodificado no puede generar un IDR: ante un resync del GamePad se vuelve al
      // principio del fichero (un IDR), como mucho cada DRC_FEED_REWIND_TICKS (def. 6, ~100 ms).
      static const int rewind_min = [] {
        const char* e = getenv("DRC_FEED_REWIND_TICKS");
        return e ? atoi(e) : 6;
      }();
      if (resync_requested && tick_count - feed_rewind_tick >= rewind_min) {
        chunk_feed.Rewind();
        feed_rewind_tick = tick_count;
      }
      // Como la consola: ... P | hueco | IDR | hueco | P ...
      if (idr_gaps && vstrm_inited && (gap_ticks > 0 || (chunk_feed.NextIsIdr() && !idr_after_gap))) {
        feed_gap = true;
        h264_chunks.clear();
        if (gap_ticks > 0) gap_ticks--; else idr_after_gap = true;
      } else {
        const FeedFrame& ff = chunk_feed.Next();
        h264_chunks = ff.chunks;
        feed_idr = ff.idr;
      }
    } else {
      LatchOnCurrentChunks(&h264_chunks);
    }
    bool has_new_frame = LatchOnCurrentFrame(&encoding_frame);
    ticks_no_frame = has_new_frame ? 0 : ticks_no_frame + 1;
    std::unique_ptr<H264ChunkArray> chunk_array;
    if (h264_chunks.size() > 0) {
      chunk_array = AsChunkArray(h264_chunks);
    }
    bool has_last_frame = encoding_frame.size() > 0;
    IdrScheduler::Action action = IdrScheduler::Action::kSkip;
    bool gap_tick = feed_gap;
    if (feed_gap) {
      // Hueco del flujo precodificado: ya contado arriba, x264 no interviene.
    } else if (feed_on && !chunk_array) {
      // Sin frame que entregar en este modo.
    } else if (idr_gaps && vstrm_inited && !chunk_array && gap_ticks > 0) {
      // Hueco tras un IDR: solo astrm con el timestamp de este tick.
      gap_ticks--;
      gap_tick = true;
    } else if (!chunk_array && async_enc && (async_enc->busy() || !pending.empty())) {
      // Codificador asíncrono ocupado: este tick no se codifica ni se avanza la máquina de estados del IDR.
    } else if (!chunk_array) {
      action = idr_scheduler.Decide(has_new_frame, has_last_frame, resync_requested);
      // Con DRC_ENC_FLUSH la ventana de vaciado no aplica: x264 se vacía explícitamente, también tras el afinado.
      if (!enc_flush) {
        action = ApplyIdleFiller(action, has_new_frame, idle_filler, !filler.empty(), ticks_no_frame, idle_flush_ticks);
      }
      if (enc_flush && !has_new_frame && action == IdrScheduler::Action::kEncodeP) {
        // El codificador está libre (se comprobó arriba), así que se puede consultar desde este hilo.
        const IdleAction idle = IdleFlushDecision(true, !filler.empty(), encoder_->Delayed(), ticks_no_frame,
                                                  refine_left, refine_after);
        if (idle == IdleAction::kReencode && refine_left > 0) {
          refine_left--;
          refine_count++;
        }
        if (idle == IdleAction::kFlush) {
          async_enc->Submit(no_frame, false, static_cast<u32>(timestamp));
          flush_count++;
        }
        if (idle != IdleAction::kReencode) {
          action = IdrScheduler::Action::kSkip;
          // Antes lo bajaba la recodificación del duplicado. Si se queda en true tras un IDR, el bloque de envío borra
          // en cada tick la petición de resync y el mando espera un IDR que no sale hasta que cambie la imagen.
          send_idr = false;
        }
      }
      if (idr_after_gap && has_last_frame) {
        action = IdrScheduler::Action::kEncodeIdr;
      } else if (idr_gaps && vstrm_inited && !async_enc &&
                 (action == IdrScheduler::Action::kEncodeIdr ||
                  action == IdrScheduler::Action::kReencodeLastIdr)) {
        // Hueco antes del IDR; el IDR sale en el tick siguiente.
        idr_after_gap = true;
        gap_tick = true;
        action = IdrScheduler::Action::kSkip;
      }
    }
    // Un IDR (también el recodificado desde el último frame tras un resync) queda dentro de x264 con varios hilos hasta
    // que lleguen más entradas: reiniciar la ventana de vaciado de DRC_IDLE_FILLER para que sigan codificándose ticks.
    if (action == IdrScheduler::Action::kEncodeIdr || action == IdrScheduler::Action::kReencodeLastIdr) {
      ticks_no_frame = 0;
    }
    if (has_new_frame || action == IdrScheduler::Action::kEncodeIdr || action == IdrScheduler::Action::kReencodeLastIdr) {
      refine_left = refine_ticks;
    }
    if (gap_tick) {
      GenerateAstrmPacket(&astrm_packet, timestamp);
    }
    prev_gap_tick = gap_tick;
    if (chunk_array || action != IdrScheduler::Action::kSkip) {
      if (chunk_array) {
        // Los chunks precodificados no pueden forzar un IDR real.
        send_idr = feed_on ? feed_idr : (resync_requested || !vstrm_inited);
      } else {
        send_idr = action != IdrScheduler::Action::kEncodeP;
      }
      if (send_idr) {
        idr_after_gap = false;
        gap_ticks = idr_gap_after;
        last_idr_tick = tick_count;
      }
      if (async_enc && !chunk_array) {
        gap_ticks = 0;
        if (send_idr) idr_in_pipeline = true;
        // busy() se comprobó al decidir (solo este hilo entrega trabajos): hay plaza.
        async_enc->Submit(encoding_frame, send_idr, static_cast<u32>(timestamp));
        if (send_idr) {
          resync_requested = false;
        }
      } else {
      auto enc_t0 = std::chrono::steady_clock::now();
      const H264ChunkArray& chunks =
          chunk_array ?
              *chunk_array : encoder_->Encode(encoding_frame, send_idr);
      double enc_ms = std::chrono::duration<double, std::milli>(
          std::chrono::steady_clock::now() - enc_t0).count();
      size_t frame_bytes = 0;
      for (const auto& c : chunks) {
        frame_bytes += std::get<1>(c);
      }
      stats.AddFrame(send_idr, enc_ms, frame_bytes);
      recent_bytes[2] = recent_bytes[1];
      recent_bytes[1] = recent_bytes[0];
      recent_bytes[0] = send_idr ? -(long)frame_bytes : (long)frame_bytes;
      cfd_add(chunks, send_idr);
      GenerateVstrmPackets(&vstrm_packets, chunks, timestamp, send_idr,
                           &vstrm_inited, &vstrm_seqid, frame_rate_);
      idr_in_packets = send_idr;
      GenerateAstrmPacket(&astrm_packet, timestamp);

      vstrm_inited = true;
      if (send_idr) {
        resync_requested = false;
      }
      }  // codificación síncrona
    }

    if (stats_enabled) {
      double elapsed_s = std::chrono::duration<double>(
          tick - stats_start).count();
      if (elapsed_s >= 1.0) {
        fprintf(stderr, "%s\n", stats.FormatAndReset(elapsed_s).c_str());
        phase_line();
        stats_start = tick;
      }
    }

    if (tick_grid_on) {
      const u64 now_us = std::chrono::duration_cast<std::chrono::microseconds>(
          std::chrono::steady_clock::now().time_since_epoch()).count();
      timer_evt->RearmTimer(static_cast<u64>(tick_grid.NextDelayUs(now_us)) * 1000);
      return true;
    }

    timestamp = GetTimestamp();
    double fps = 59.94;
    if (frame_rate_ == VideoFrameRate::k29_97Hz) {
      fps = 29.97;
    } else if (frame_rate_ == VideoFrameRate::k25Hz) {
      fps = 25.0;
    } else if (frame_rate_ == VideoFrameRate::k50Hz) {
      fps = 50.0;
    }
    s32 frame_interval = static_cast<s32>(1000000.0 / fps);
    next_timestamp += frame_interval;
    s32 delta_next = next_timestamp - timestamp;
    if (delta_next < 1000) {
      // Si el reloj real adelanta al timer (p.ej. por codificación o scheduling),
      // resincronizar en lugar de disparar ráfagas consecutivas a 1 ms que
      // saturan el búfer del GamePad y el bus SDIO de la Wi-Fi.
      next_timestamp = timestamp + frame_interval;
      delta_next = frame_interval;
    }
    timer_evt->RearmTimer((u64)delta_next * 1000);

    return true;
  });

  ThreadedEventMachine::InitEventsAndRun();
}

bool VideoStreamer::LatchOnCurrentFrame(std::vector<byte>* latched_frame) {
  std::lock_guard<std::mutex> lk(frame_mutex_);
  if (frame_.size() != 0) {
    *latched_frame = std::move(frame_);
    return true;
  }
  return false;
}

void VideoStreamer::LatchOnCurrentChunks(
    std::vector<std::string>* latched_chunk) {
  std::lock_guard<std::mutex> lk(frame_mutex_);
  if (h264_chunks_.size() != 0) {
    *latched_chunk = std::move(h264_chunks_);
  }
}

}  // namespace drc
