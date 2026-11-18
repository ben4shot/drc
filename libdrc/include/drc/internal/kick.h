// Inyección de fallos para comprobar el modelo del firmware en el mando. /tmp/drc_kick se lee una vez por segundo y se borra:
//   ts N      desplaza N µs la fase de los ts (vídeo y astrm)
//   seq N     salta N números de secuencia (hueco de seq: reinicio y estado 6)
//   reanchor  fuerza un reanclaje (reanchor.h)
// Se pueden combinar: «ts 4000 seq 1».

#pragma once

#include <cstdint>
#include <cstdio>
#include <cstring>

namespace drc {

struct Kick {
  long ts_us = 0;
  int seq = 0;
  bool reanchor = false;
};

inline Kick ParseKick(const char* text) {
  Kick k;
  const char* p = text;
  char word[16];
  int used = 0;
  while (sscanf(p, "%15s%n", word, &used) == 1) {
    p += used;
    if (!strcmp(word, "reanchor")) {
      k.reanchor = true;
    } else if (!strcmp(word, "ts") || !strcmp(word, "seq")) {
      long v = 0;
      if (sscanf(p, "%ld%n", &v, &used) != 1) break;
      p += used;
      if (word[0] == 't') k.ts_us = v; else k.seq = static_cast<int>(v);
    } else {
      break;
    }
  }
  return k;
}

}  // namespace drc
