// Timestamp del astrm de formato (32 B, tipo 0x04). En la captura de la consola va en su carga (bytes 8-11 LE) y es
// EXACTAMENTE el del frame vstrm al que acompaña (diferencia 0 en 1739 de 1744 frames); sale justo antes del frame.
// DRC_ASTRM_TS_MATCH=1 hace que libdrc lo estampe con el mismo valor que el vídeo en el momento de enviar.

#pragma once

#include <cstdint>
#include <cstdlib>

namespace drc {

inline bool AstrmTsMatchEnabled(const char* env) { return env && env[0] == '1'; }

inline uint32_t AstrmFormatTimestamp(bool match, uint32_t vstrm_ts, uint32_t tick_ts) {
  return match ? vstrm_ts : tick_ts;
}

// ts del astrm de un tick (Paso 1 de plan.md). Con match, el ts del tick (rejilla), haya o no paquetes de vídeo:
// los ticks de hueco también llevan astrm y antes quedaban con el ts del momento de codificar.
// Sin match, conserva el valor anterior.
inline uint32_t AstrmTickTimestamp(bool match, uint32_t tick_ts, uint32_t previous) {
  return match ? tick_ts : previous;
}

// DRC_ASTRM_TS_DELTA_US / /tmp/drc_astrm_ts_delta_us: desplaza SOLO el ts del astrm de formato (µs, con signo).
// El firmware del mando (mc_sync, LVC.bin 0x30eca) lo acepta únicamente si 0 <= contador_local - ts < 8000 µs.
inline int64_t AstrmTsDeltaFromEnv(const char* env) {
  if (!env || !env[0]) return 0;
  char* end = nullptr;
  long long v = strtoll(env, &end, 10);
  return (end && *end == '\0') ? static_cast<int64_t>(v) : 0;
}

inline int64_t AstrmTsDeltaValue(bool have_file_value, int64_t file_value, int64_t default_value) {
  return have_file_value ? file_value : default_value;
}

inline uint32_t AstrmTsApplyDelta(uint32_t ts, int64_t delta_us) {
  return static_cast<uint32_t>(static_cast<int64_t>(ts) + delta_us);
}

}  // namespace drc
