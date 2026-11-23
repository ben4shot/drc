// Cola de muestras s16 estéreo (48 kHz) entre el productor de audio (PushSamples) y el
// temporizador de paquetes astrm (PopSamples). Con tope de 100 ms: si el productor va por
// delante, se descartan las muestras más antiguas para que el sonido no se retrase
// respecto al vídeo. Si faltan muestras, se rellena con silencio.

#pragma once

#include <cstddef>
#include <cstdio>
#include <cstring>
#include <deque>
#include <drc/types.h>
#include <mutex>
#include <sched.h>
#include <string>
#include <vector>

namespace drc {

const size_t kMaxQueuedAudioSamples = 48000 * 2 / 10;  // 100 ms estéreo = 9600 s16.

// Muestras por canal de cada paquete astrm: 384 (8 ms, 1544 B; la consola manda 416 = 8,67 ms).
// Con MTU 1500 (wlan0) 384 se fragmenta en 2 paquetes IP; 336 (7 ms, 1352 B UDP / 1380 B IP)
// cabe entero sin fragmentar. 192 (4 ms) sonó entrecortado. DRC_AUDIO_PACKET_SAMPLES=336|192.
inline unsigned AudioPacketSamplesFromEnv(const char* env) {
  if (env && std::strcmp(env, "192") == 0) return 192;
  if (env && std::strcmp(env, "336") == 0) return 336;
  return 384;
}

inline unsigned AudioPacketIntervalMs(unsigned samples_per_packet) {
  return samples_per_packet / 48;
}

// Prioridad SCHED_FIFO del hilo del temporizador de audio: 10 por encima del proceso (tope 99),
// para que la codificación de vídeo (misma prioridad, ~8 ms por frame) no retrase los paquetes.
// 0 = no tocar (el proceso no es SCHED_FIFO).
inline int AudioThreadPriority(int policy, int process_prio) {
  if (policy != SCHED_FIFO) return 0;
  return process_prio + 10 > 99 ? 99 : process_prio + 10;
}

// Desfase (µs) sumado al timestamp de los paquetes astrm de audio. GetTsf ya incluye DRC_TS_OFFSET_US, así
// que este valor mantiene fija la relación con el vídeo: audio_ts − ts del astrm de formato anterior ≈ 0,
// como la consola (mediana −0,3 ms, rango ±8 ms). Con 0 iba 25 ms por delante y el GamePad sonaba roto.
// /tmp/drc_audio_ts_offset_us lo sustituye (diagnóstico, releído 1 vez/s).
const long long kAudioTimestampOffsetUs = -25000;

inline long long AudioTimestampOffsetUs(bool have_file_value, long long file_value) {
  return have_file_value ? file_value : kAudioTimestampOffsetUs;
}

// B19: lo que le pasó a la cola desde el último TakeStats(), en s16 (96 = 1 ms estéreo).
struct AudioQueueStats {
  size_t underrun_samples = 0;  // silencio metido por Pop porque faltaban muestras
  size_t dropped_samples = 0;   // muestras antiguas tiradas por el tope de 100 ms
  unsigned pops = 0;
  unsigned short_pops = 0;      // Pop con relleno (incluye los vacíos)
  unsigned empty_pops = 0;      // Pop con la cola vacía
  size_t min_level = 0;         // nivel mínimo antes de un Pop (sin Pop: el nivel actual)
  size_t max_level = 0;         // nivel máximo tras un Push
};

class AudioQueue {
 public:
  void Push(const std::vector<s16>& samples) {
    std::lock_guard<std::mutex> lk(mutex_);
    samples_.insert(samples_.end(), samples.begin(), samples.end());
    if (samples_.size() > kMaxQueuedAudioSamples) {
      size_t drop = samples_.size() - kMaxQueuedAudioSamples;
      samples_.erase(samples_.begin(), samples_.begin() + drop);
      stats_.dropped_samples += drop;
    }
    if (samples_.size() > stats_.max_level) stats_.max_level = samples_.size();
  }

  // Añade a *out exactamente `count` muestras: las disponibles y silencio para el resto.
  void Pop(std::vector<s16>* out, size_t count) {
    std::lock_guard<std::mutex> lk(mutex_);
    if (stats_.pops == 0 || samples_.size() < stats_.min_level) stats_.min_level = samples_.size();
    stats_.pops++;
    size_t n = samples_.size() < count ? samples_.size() : count;
    if (n < count) {
      stats_.short_pops++;
      stats_.underrun_samples += count - n;
      if (n == 0) stats_.empty_pops++;
    }
    out->insert(out->end(), samples_.begin(), samples_.begin() + n);
    samples_.erase(samples_.begin(), samples_.begin() + n);
    out->resize(out->size() + (count - n), 0);
  }

  AudioQueueStats TakeStats() {
    std::lock_guard<std::mutex> lk(mutex_);
    AudioQueueStats s = stats_;
    if (s.pops == 0) s.min_level = samples_.size();
    stats_ = AudioQueueStats();
    return s;
  }

  size_t size() {
    std::lock_guard<std::mutex> lk(mutex_);
    return samples_.size();
  }

 private:
  std::mutex mutex_;
  std::deque<s16> samples_;
  AudioQueueStats stats_;
};

// Vencimientos del timerfd que el callback no atendió (el timerfd cuenta todos desde la última lectura).
inline u64 TimerMissedTicks(u64 expirations) {
  return expirations > 1 ? expirations - 1 : 0;
}

// Diagnóstico de 1 s del emisor de audio (B19 Paso 2).
struct AudioDiag {
  AudioQueueStats q;
  u64 missed_ticks = 0;  // paquetes no enviados porque el hilo llegó tarde
  u64 max_gap_us = 0;    // mayor separación entre dos callbacks del temporizador
};

inline bool AudioDiagAnomalous(const AudioDiag& d) {
  return d.q.underrun_samples > 0 || d.q.dropped_samples > 0 || d.missed_ticks > 0;
}

// Cada segundo con anomalía; si no, un resumen cada 10 s.
inline bool AudioDiagShouldLog(const AudioDiag& d, unsigned seconds_since_log) {
  return AudioDiagAnomalous(d) || seconds_since_log >= 10;
}

inline std::string FormatAudioDiag(const AudioDiag& d) {
  const double kSamplesPerMs = 96.0;
  char buf[256];
  snprintf(buf, sizeof(buf),
           "[audio] relleno=%.1fms cortos=%u/%u vacios=%u tirado=%.1fms perdidos=%llu "
           "hueco_max=%.1fms cola=%.1f..%.1fms",
           d.q.underrun_samples / kSamplesPerMs, d.q.short_pops, d.q.pops, d.q.empty_pops,
           d.q.dropped_samples / kSamplesPerMs, static_cast<unsigned long long>(d.missed_ticks),
           d.max_gap_us / 1000.0, d.q.min_level / kSamplesPerMs, d.q.max_level / kSamplesPerMs);
  return buf;
}

}  // namespace drc
