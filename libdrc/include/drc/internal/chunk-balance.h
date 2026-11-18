// Tamaño mínimo por trozo (B17): los 5 trozos de un frame son cortes de un único flujo CABAC
// continuo. La consola nunca manda trozos de menos de 3-4 bytes; los nuestros miden 1-2 a menudo.
// Se mueven las fronteras entre trozos vecinos (el flujo concatenado no cambia).

#pragma once

#include <cstdlib>

namespace drc {

const int kDefaultMinChunk = 0;  // desactivado: no validado con el GamePad

// DRC_MIN_CHUNK: bytes mínimos por trozo (0 = desactivado, por defecto). NULL, vacío, negativo o no numérico = 0.
inline int MinChunkFromEnv(const char* env) {
  if (!env || !env[0]) return kDefaultMinChunk;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0) return kDefaultMinChunk;
  return static_cast<int>(v);
}

// Ajusta las longitudes `len[0..n)` para que ninguna quede por debajo de `min_len` moviendo lo
// mínimo las fronteras acumuladas (clamp hacia delante y luego hacia atrás). Si la suma no alcanza
// n*min_len no toca nada. n <= 16.
inline void BalanceChunkLengths(int* len, int n, int min_len) {
  if (min_len <= 0 || n <= 0 || n > 16) return;
  long total = 0;
  long bound[16];  // bound[k] = fin acumulado del trozo k
  for (int i = 0; i < n; i++) { total += len[i]; bound[i] = total; }
  if (total < static_cast<long>(n) * min_len) return;
  long prev = 0;
  for (int k = 0; k < n - 1; k++) {                        // hacia delante: al menos min_len cada uno
    long lo = prev + min_len;
    if (bound[k] < lo) bound[k] = lo;
    prev = bound[k];
  }
  bound[n - 1] = total;
  for (int k = n - 2; k >= 0; k--) {                       // hacia atrás: dejar min_len a los siguientes
    long hi = bound[k + 1] - min_len;
    if (bound[k] > hi) bound[k] = hi;
  }
  prev = 0;
  for (int k = 0; k < n; k++) { len[k] = static_cast<int>(bound[k] - prev); prev = bound[k]; }
}

}  // namespace drc
