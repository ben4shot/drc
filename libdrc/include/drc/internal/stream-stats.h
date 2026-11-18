// Acumulador de diagnóstico por segundo del VideoStreamer. Se activa con la
// variable de entorno DRC_STREAM_STATS=1 y escribe una línea por segundo en
// stderr para correlacionar los congelamientos del GamePad con resyncs,
// IDRs, bitrate, tiempo de codificación y huecos del bucle de envío.

#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>

namespace drc {

// Paquetes de entrada recibidos del mando (InputReceiver, ~180/s). Lo lee StreamStats cada segundo: con el mando
// apagado o fuera de alcance baja a 0, cosa que «resync=0» no distingue de una sesión sana.
inline std::atomic<unsigned>& InputPacketCounter() {
  static std::atomic<unsigned> n{0};
  return n;
}
inline void CountInputPacket() { InputPacketCounter().fetch_add(1, std::memory_order_relaxed); }

class StreamStats {
 public:
  StreamStats() { Reset(); }

  void AddResync() { resyncs_++; }
  void AddSendFailure() { send_failures_++; }

  void AddFrame(bool idr, double encode_ms, size_t bytes) {
    frames_++;
    if (idr) idrs_++;
    bytes_ += bytes;
    if (bytes > frame_bytes_max_) frame_bytes_max_ = bytes;
    enc_ms_sum_ += encode_ms;
    if (encode_ms > enc_ms_max_) enc_ms_max_ = encode_ms;
  }

  void AddTickGap(int64_t gap_us) {
    if (gap_us > gap_us_max_) gap_us_max_ = gap_us;
  }

  std::string FormatAndReset(double elapsed_s) {
    char line[256];
    double kbps = elapsed_s > 0 ? bytes_ * 8.0 / 1000.0 / elapsed_s : 0.0;
    double enc_avg = frames_ ? enc_ms_sum_ / frames_ : 0.0;
    snprintf(line, sizeof(line),
             "[libdrc] frames=%u idr=%u resync=%u kbps=%.0f "
             "enc_avg=%.1fms enc_max=%.1fms gap_max=%.1fms send_fail=%u fmax=%zu input=%.0f/s",
             frames_, idrs_, resyncs_, kbps, enc_avg, enc_ms_max_,
             gap_us_max_ / 1000.0, send_failures_, frame_bytes_max_,
             elapsed_s > 0 ? InputPacketCounter().exchange(0) / elapsed_s : 0.0);
    Reset();
    return line;
  }

 private:
  void Reset() {
    frames_ = idrs_ = resyncs_ = send_failures_ = 0;
    bytes_ = 0;
    frame_bytes_max_ = 0;
    enc_ms_sum_ = enc_ms_max_ = 0.0;
    gap_us_max_ = 0;
  }

  unsigned frames_, idrs_, resyncs_, send_failures_;
  uint64_t bytes_;
  size_t frame_bytes_max_;  // frame más grande del segundo (B17: ¿resync tras frames grandes?)
  double enc_ms_sum_, enc_ms_max_;
  int64_t gap_us_max_;
};

}  // namespace drc
