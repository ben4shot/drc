// Opciones del codificador que dependen de lo que fija el GamePad.

#pragma once

#include <cstdlib>
#include <cstring>

namespace drc {

// La cabecera de slice fija del GamePad lleva disable_deblocking_filter_idc=0 y alpha/beta=0: el mando SIEMPRE
// aplica el filtro de desbloqueo. El codificador tiene que reconstruir sus referencias igual (deblock 0:0); el
// preset ultrafast de x264 lo desactiva, y el mando predecía los frames P desde una referencia distinta de la del
// codificador (deriva acumulada hasta el siguiente IDR). DRC_DEBLOCK=0 devuelve el comportamiento anterior.
inline bool DeblockEnabled(const char* env) { return !(env && env[0] == '0'); }

// DRC_INTRA4=1: Intra 4x4 como la consola (2,8 % de los macrobloques de sus P). El preset ultrafast solo deja Intra 16x16.
inline bool Intra4Enabled(const char* env) { return env && env[0] == '1'; }

// DRC_SUBME: refinamiento subpíxel (0..9, p.ej. 2 = cuarto de píxel + SATD como el 19,6 % de vectores subpíxel de la consola)
// independiente de DRC_PARTS. Devuelve -1 si no se especifica o está fuera de rango.
inline int SubmeFromEnv(const char* env) {
  if (!env || !env[0]) return -1;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0 || v > 9) return -1;
  return static_cast<int>(v);
}

// Control de tasa como la consola: calidad objetivo con tope de tamaño por frame y QP variable por zona. Su flujo gasta hasta
// ~20 KB por frame, baja a QP 1-3 en lo quieto y usa QP 1/12/18/24/32 según la zona. DRC_VBV_KBPS (9300 = ~19,4 KB por frame)
// activa CRF+VBV; DRC_CRF y DRC_AQ ajustan calidad y fuerza del QP por zona.
struct RateConfig {
  int vbv_kbps = 0;
  double crf = 18.0;
  double aq = 1.0;
  // DRC_RATE_MODE=fija: bitrate constante (vbv_kbps), sin CRF. Si no, CRF con vbv_kbps como máximo.
  bool fixed = false;
  bool enabled() const { return vbv_kbps > 0; }
};

inline RateConfig RateConfigFromEnv(const char* kbps, const char* crf, const char* aq,
                                    const char* mode = nullptr) {
  RateConfig r;
  r.fixed = mode && strcmp(mode, "fija") == 0;
  if (!kbps || !kbps[0]) return r;
  char* end = nullptr;
  long v = strtol(kbps, &end, 10);
  if (*end != '\0' || v <= 0) return r;
  r.vbv_kbps = static_cast<int>(v);
  if (crf && crf[0]) {
    double c = strtod(crf, &end);
    if (*end == '\0' && c >= 1.0 && c <= 51.0) r.crf = c;
  }
  if (aq && aq[0]) {
    double a = strtod(aq, &end);
    if (*end == '\0' && a >= 0.0 && a <= 3.0) r.aq = a;
  }
  return r;
}

// Buffer VBV de dos frames a 60 fps (kbit).
inline int VbvBufferKbit(int kbps) { return kbps / 30; }

}  // namespace drc
