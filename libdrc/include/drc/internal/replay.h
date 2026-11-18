// Reproducción íntegra de una captura de la consola (DRC_REPLAY=<fichero>): los paquetes vstrm y
// astrm capturados se envían tal cual, con su separación temporal original; solo se cambia el
// timestamp (u32, bytes 4-7) para que corresponda al TSF de nuestro AP.

#pragma once

#include <cstddef>
#include <cstdint>

namespace drc {

// vstrm: timestamp big-endian en los bytes 4-7.
inline void RetimeVstrm(unsigned char* p, size_t n, uint32_t delta) {
  if (n < 8) return;
  uint32_t ts = (uint32_t(p[4]) << 24) | (uint32_t(p[5]) << 16) | (uint32_t(p[6]) << 8) | p[7];
  ts += delta;
  p[4] = ts >> 24; p[5] = ts >> 16; p[6] = ts >> 8; p[7] = ts;
}

// astrm: timestamp little-endian en los bytes 4-7 (audio). El paquete de formato (32 B, tipo 0x04)
// lleva ts=0 en la cabecera y el timestamp real dentro de la carga: bytes 8-11 (como
// GenerateAstrmPacket); se cambia solo ese campo.
inline void RetimeAstrm(unsigned char* p, size_t n, uint32_t delta) {
  size_t off = 4;
  if (n == 32 && p[0] == 0x04) off = 8;
  if (n < off + 4) return;
  uint32_t ts = p[off] | (uint32_t(p[off + 1]) << 8) | (uint32_t(p[off + 2]) << 16) | (uint32_t(p[off + 3]) << 24);
  ts += delta;
  p[off] = ts; p[off + 1] = ts >> 8; p[off + 2] = ts >> 16; p[off + 3] = ts >> 24;
}

// DRC_REPLAY_ASTRM: qué paquetes astrm (50121) de la captura se reenvían: all (por defecto), none o fmt
// (solo los de formato de 32 B tipo 0x04, sin el audio).
enum class ReplayAstrm { kAll, kNone, kFormat };

inline ReplayAstrm ReplayAstrmFromEnv(const char* e) {
  if (!e) return ReplayAstrm::kAll;
  if (e[0] == 'n') return ReplayAstrm::kNone;
  if (e[0] == 'f') return ReplayAstrm::kFormat;
  return ReplayAstrm::kAll;
}

inline bool ReplayKeepAstrm(ReplayAstrm m, const unsigned char* p, size_t n) {
  if (m == ReplayAstrm::kNone) return false;
  if (m == ReplayAstrm::kFormat) return n == 32 && p[0] == 0x04;
  return true;
}

}  // namespace drc
