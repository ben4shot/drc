// Diagnóstico del arranque temporizado del GamePad (LVC.bin 0x1811e, ver wiiu_dump/disasm/INDICE.md «Arranque temporizado tras un IDR»).
// Tras aceptar un IDR el mando espera a que venza su alarma (≈15 ms tras un vsync) y exige |vsync_local - ts_IDR| <= 1000 us:
// si el IDR es «del futuro» espera; si es «del pasado» más de 1 ms reinicia (causa 4) y pide resync.
//
// DRC_IDR_TS_SHIFT_US (o /tmp/drc_idr_ts_shift_us, 1 lectura/s) desplaza SOLO el timestamp del vstrm de los frames IDR (µs, con signo).
// No toca el de los P, ni el del astrm. Por defecto 0 (comportamiento anterior). Sirve para barrer la fase del IDR en pasos de ~1 ms
// a lo largo de un periodo (16 683 us) y localizar la ventana de ≈2 ms en la que el mando acepta el IDR.

#pragma once

#include <cstdint>
#include <cstdlib>

namespace drc {

inline int64_t IdrTsShiftFromEnv(const char* env) {
  if (!env || !env[0]) return 0;
  char* end = nullptr;
  long long v = strtoll(env, &end, 10);
  return (end && *end == '\0') ? static_cast<int64_t>(v) : 0;
}

inline int64_t IdrTsShiftValue(bool have_file_value, int64_t file_value, int64_t default_value) {
  return have_file_value ? file_value : default_value;
}

// Solo los frames IDR se desplazan. Aritmética módulo 2^32 (el ts es u32).
inline uint32_t IdrTsApply(uint32_t ts, bool idr, int64_t shift_us) {
  return idr ? static_cast<uint32_t>(static_cast<int64_t>(ts) + shift_us) : ts;
}

}  // namespace drc
