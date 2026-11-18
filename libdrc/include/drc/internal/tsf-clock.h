// Reloj de timestamps vstrm/astrm basado en el TSF del chip Wi-Fi.
//
// El GamePad compara los timestamps de cada paquete con su propio TSF (el del
// BSS). El TSF que expone sysfs en el CYW43455 no avanza de forma regular:
// frente al reloj local se atrasa ~9 ms/s (lecturas del firmware atrasadas) y
// cada ~8 s salta ~+80 ms (diente de sierra). Seguirlo hace oscilar los
// timestamps hasta ~80 ms y el GamePad alterna tramos aceptados y rechazados.
//
// TsfClock sigue la envolvente superior: extrapola con el reloj local, consulta
// el TSF cada kTsfResyncIntervalUs y solo se re-ancla si la lectura va por
// delante de la predicción (lectura fresca o deriva real). Las lecturas por
// detrás se ignoran, salvo un retroceso enorme (TSF reiniciado al relanzar el AP).

#pragma once

#include <cstdlib>

#include <deque>
#include <utility>
#include <drc/types.h>

namespace drc {

const u64 kTsfResyncIntervalUs = 1000000ULL;   // Consulta de sysfs cada 1 s.
const u64 kTsfResetThresholdUs = 1000000ULL;   // Retroceso > 1 s: TSF reiniciado.

// Adelanto de los timestamps vstrm/astrm sobre el TSF vivo. Medido con el GamePad
// (B07): acepta P-frames con +5..+13 ms; fuera de esa ventana pide resync sin parar.
const s64 kTimestampLeadUs = 9000;

// Desfase a aplicar: el de /tmp/drc_ts_offset_us si existe (diagnóstico), si no el
// adelanto por defecto.
inline s64 TimestampOffsetUs(bool have_file_value, s64 file_value, s64 default_lead) {
  return have_file_value ? file_value : default_lead;
}
inline s64 TimestampOffsetUs(bool have_file_value, s64 file_value) {
  return TimestampOffsetUs(have_file_value, file_value, kTimestampLeadUs);
}

// DRC_TS_OFFSET_US: adelanto por defecto cambiado por entorno. El 9000 se afinó con el chip interno
// (wlan0); con el TP-Link (wlan1) la ventana buena está en 20000-22000 µs (barrido del 2026-11-03).
// Vacío o no numérico: kTimestampLeadUs.
inline s64 TimestampLeadFromEnv(const char* env) {
  if (!env || !env[0]) return kTimestampLeadUs;
  char* end = nullptr;
  long long v = strtoll(env, &end, 10);
  return (end && *end == '\0') ? static_cast<s64>(v) : kTimestampLeadUs;
}

// Timestamp vstrm sellado justo al enviar: el frame se capturó un tick antes, así que
// equivale al ts nominal del tick de captura sin perder margen si el tick se retrasa.
inline u32 SendTimestamp(u32 tsf_now, s32 frame_interval_us) {
  return tsf_now - static_cast<u32>(frame_interval_us);
}

// DRC_TSF_SAWTOOTH=1: ignora lecturas por detrás de la predicción (diente de sierra del CYW43455 en wlan0).
// Por defecto (0 o vacío, ej. rtw89 en wlan1) TsfClock corrige la deriva del cristal en ambos sentidos.
inline bool TsfSawtoothFromEnv(const char* env) {
  return env && env[0] == '1';
}

// DRC_TSF_RTT_GATE=1: re-anclar solo con lecturas rápidas del TSF (ver TsfRttGate).
inline bool TsfRttGateFromEnv(const char* env) {
  return env && env[0] == '1';
}

// Instante local al que corresponde una lectura del TSF que empezó en t0 y terminó en t1.
inline u64 TsfMidpoint(u64 t0_us, u64 t1_us) {
  return t0_us + (t1_us - t0_us) / 2;
}

// El TSF del CYW43455 (wlan0) se lee del firmware por SDIO: la lectura tarda 0,25 ms con el bus libre y
// hasta 20 ms con vídeo, y el valor se toma en algún punto de ese intervalo. Anclar con todas las lecturas
// mete un error de hasta -16 ms (medido); con las que tardan poco, ±0,08 ms. Acepta una lectura si su
// duración no pasa del mínimo de las últimas `window` + `slack_us`. Las que tardan menos de `cached_us`
// vienen de la caché de 100 ms del driver (valor interpolado, sin tocar el bus): se descartan y no
// cuentan para el mínimo.
class TsfRttGate {
 public:
  explicit TsfRttGate(u64 slack_us = 150, size_t window = 16, u64 cached_us = 100)
      : slack_us_(slack_us), window_(window), cached_us_(cached_us) {}

  bool Accept(u64 rtt_us) {
    if (rtt_us < cached_us_) return false;
    rtts_.push_back(rtt_us);
    while (rtts_.size() > window_) rtts_.pop_front();
    u64 lo = rtts_.front();
    for (u64 r : rtts_) {
      if (r < lo) lo = r;
    }
    return rtt_us <= lo + slack_us_;
  }

 private:
  u64 slack_us_;
  size_t window_;
  u64 cached_us_;
  std::deque<u64> rtts_;
};

class TsfClock {
 public:
  explicit TsfClock(u64 resync_us = kTsfResyncIntervalUs,
                    u64 reset_threshold_us = kTsfResetThresholdUs,
                    bool bidirectional = false)
      : resync_us_(resync_us),
        reset_threshold_us_(reset_threshold_us),
        bidirectional_(bidirectional),
        anchored_(false),
        base_tsf_(0),
        base_now_us_(0),
        last_check_us_(0) {}

  bool NeedsResync(u64 now_us) const {
    return !anchored_ || now_us - last_check_us_ >= resync_us_;
  }

  void TouchCheck(u64 now_us) {
    last_check_us_ = now_us;
  }

  // Tras una lectura descartada: volver a consultar dentro de delay_us en vez de esperar el intervalo entero.
  void RetryIn(u64 now_us, u64 delay_us) {
    last_check_us_ = delay_us >= resync_us_ ? now_us : now_us - (resync_us_ - delay_us);
  }

  // Devuelve el salto aplicado frente a la predicción (0 si la lectura se ignora o es el primer anclaje).
  s64 Anchor(u64 hw_tsf, u64 now_us) {
    last_check_us_ = now_us;
    s64 step = 0;
    if (anchored_) {
      u64 predicted = base_tsf_ + (now_us - base_now_us_);
      bool behind = hw_tsf < predicted;
      if (!bidirectional_ && behind && predicted - hw_tsf < reset_threshold_us_) {
        return 0;  // Lectura atrasada (diente de sierra): se ignora.
      }
      step = static_cast<s64>(hw_tsf - predicted);
    }
    base_tsf_ = hw_tsf;
    base_now_us_ = now_us;
    anchored_ = true;
    return step;
  }

  bool Get(u64 now_us, u64* out) const {
    if (!anchored_) {
      return false;
    }
    *out = base_tsf_ + (now_us - base_now_us_);
    return true;
  }

 private:
  u64 resync_us_;
  u64 reset_threshold_us_;
  bool bidirectional_;
  bool anchored_;
  u64 base_tsf_;
  u64 base_now_us_;
  u64 last_check_us_;
};

// Timestamps suaves a partir del TSF en diente de sierra del CYW43455 (se atrasa
// ~10 ms/s durante ~10 s y salta +100 ms). Guarda el desfase (TSF - reloj local)
// de las muestras de la ventana y devuelve reloj local + desfase de la envolvente
// superior (kPeak) o de su punto medio (kMid): avanza al ritmo del reloj local,
// sin los saltos ni la oscilación de ±50 ms de la lectura instantánea.
const u64 kTsfEnvelopeWindowUs = 12000000ULL;  // > 1 ciclo del diente de sierra.

class TsfEnvelope {
 public:
  enum Mode { kPeak, kMid };

  explicit TsfEnvelope(u64 window_us = kTsfEnvelopeWindowUs)
      : window_us_(window_us) {}

  void AddSample(u64 hw_tsf, u64 now_us) {
    s64 off = static_cast<s64>(hw_tsf - now_us);
    samples_.push_back(std::make_pair(now_us, off));
    while (!samples_.empty() && now_us - samples_.front().first > window_us_) {
      samples_.pop_front();
    }
  }

  bool Get(u64 now_us, Mode mode, u64* out) const {
    if (samples_.empty()) {
      return false;
    }
    s64 hi = samples_.front().second, lo = hi;
    for (const auto& s : samples_) {
      if (s.second > hi) hi = s.second;
      if (s.second < lo) lo = s.second;
    }
    s64 off = mode == kPeak ? hi : lo + (hi - lo) / 2;
    *out = now_us + static_cast<u64>(off);
    return true;
  }

 private:
  u64 window_us_;
  std::deque<std::pair<u64, s64>> samples_;
};

}  // namespace drc
