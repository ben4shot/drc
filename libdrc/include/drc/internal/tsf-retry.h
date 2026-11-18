// Reintento de la primera lectura del TSF. El atributo sysfs `tsf` de rtw89 devuelve -EAGAIN mientras
// mac80211 tiene el bloqueo del wiphy (justo cuando el GamePad se acaba de asociar); si ese primer
// intento falla no hay ancla y el vídeo salía con timestamps del reloj local de la Pi.

#pragma once

#include <unistd.h>

namespace drc {

// Llama a `read()` (devuelve true si leyó) hasta `attempts` veces (mínimo 1), esperando `sleep_us`
// entre intentos. Devuelve true si alguna lectura tuvo éxito.
template <typename F>
inline bool RetryRead(F read, int attempts, int sleep_us) {
  if (attempts < 1) attempts = 1;
  for (int i = 0; i < attempts; i++) {
    if (read()) return true;
    if (sleep_us > 0 && i + 1 < attempts) usleep(sleep_us);
  }
  return false;
}

}  // namespace drc
