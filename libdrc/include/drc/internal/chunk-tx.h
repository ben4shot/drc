// Instante de salida de cada paquete de vídeo de un frame, con los chunks espaciados como la consola.
// La consola envía los 5 chunks de un frame separados ~2,8 ms (los va produciendo su codificador) y con los paquetes
// de cada chunk seguidos. DRC_CHUNK_SPACING_US lo intentó con sleep_for dentro del temporizador del frame y bajaba
// a 30 fps; aquí cada paquete se programa con SO_TXTIME (DRC_CHUNK_TX_US), sin dormir nada.

#pragma once

#include <cstdlib>
#include <vector>

namespace drc {

// Offsets (ns desde el inicio del frame) de cada paquete. `chunk_end[i]` marca el último paquete de un chunk.
// El chunk k empieza en k*spacing_us; dentro de un chunk los paquetes salen separados intra_us. spacing_us <= 0:
// todo a 0 (el llamante usa entonces el reparto uniforme).
inline std::vector<unsigned long long> ChunkTxOffsetsNs(const std::vector<bool>& chunk_end, int spacing_us,
                                                         int intra_us) {
  std::vector<unsigned long long> out(chunk_end.size(), 0ULL);
  if (spacing_us <= 0) return out;
  unsigned long long chunk = 0, in_chunk = 0;
  for (size_t i = 0; i < chunk_end.size(); ++i) {
    out[i] = (chunk * static_cast<unsigned long long>(spacing_us) +
              in_chunk * static_cast<unsigned long long>(intra_us < 0 ? 0 : intra_us)) * 1000ULL;
    ++in_chunk;
    if (chunk_end[i]) {
      ++chunk;
      in_chunk = 0;
    }
  }
  return out;
}

// DRC_CHUNK_TX_US: espaciado entre chunks en µs. Vacío, no numérico, negativo o absurdo (>20 ms): 0 = desactivado.
inline int ChunkTxSpacingFromEnv(const char* env) {
  if (!env || !env[0]) return 0;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0 || v > 20000) return 0;
  return static_cast<int>(v);
}

}  // namespace drc
