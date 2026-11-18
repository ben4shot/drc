// Cola de muestras s16 estéreo (48 kHz) entre el productor de audio (PushSamples) y el
// temporizador de paquetes astrm (PopSamples). Con tope de 100 ms: si el productor va por
// delante, se descartan las muestras más antiguas para que el sonido no se retrase
// respecto al vídeo. Si faltan muestras, se rellena con silencio.

#pragma once

#include <cstddef>
#include <cstring>
#include <deque>
#include <drc/types.h>
#include <mutex>
#include <sched.h>
#include <vector>

namespace drc {

const size_t kMaxQueuedAudioSamples = 48000 * 2 / 10;  // 100 ms estéreo = 9600 s16.

// Muestras por canal de cada paquete astrm: 384 (8 ms, 1544 B); la consola manda 416 (8,67 ms, 1672 B). Se
// fragmenta en IP con MTU 1500. Medido con el GamePad: con 192 (4 ms, 776 B, cabe sin fragmentar)
// el tono suena entrecortado; con 384, continuo. DRC_AUDIO_PACKET_SAMPLES=192 lo restaura.
inline unsigned AudioPacketSamplesFromEnv(const char* env) {
  return (env && std::strcmp(env, "192") == 0) ? 192 : 384;
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

class AudioQueue {
 public:
  void Push(const std::vector<s16>& samples) {
    std::lock_guard<std::mutex> lk(mutex_);
    samples_.insert(samples_.end(), samples.begin(), samples.end());
    if (samples_.size() > kMaxQueuedAudioSamples) {
      samples_.erase(samples_.begin(),
                     samples_.begin() + (samples_.size() - kMaxQueuedAudioSamples));
    }
  }

  // Añade a *out exactamente `count` muestras: las disponibles y silencio para el resto.
  void Pop(std::vector<s16>* out, size_t count) {
    std::lock_guard<std::mutex> lk(mutex_);
    size_t n = samples_.size() < count ? samples_.size() : count;
    out->insert(out->end(), samples_.begin(), samples_.begin() + n);
    samples_.erase(samples_.begin(), samples_.begin() + n);
    out->resize(out->size() + (count - n), 0);
  }

  size_t size() {
    std::lock_guard<std::mutex> lk(mutex_);
    return samples_.size();
  }

 private:
  std::mutex mutex_;
  std::deque<s16> samples_;
};

}  // namespace drc
