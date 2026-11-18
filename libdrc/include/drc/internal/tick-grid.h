// Tick del vídeo sobre una rejilla fija. El mando sigue la fase del ts con un PLL de ±3,2 µs por frame (LVC 0x17cac: última
// línea 838..1046 ciclos a 32 MHz; ver wiiu_dump/disasm/INDICE.md). Antes, un tick que acababa tarde reiniciaba el
// instante del siguiente y cambiaba la fase de todos los posteriores. Aquí salta a la siguiente posición de la misma rejilla.
// DRC_TICK_GRID=0 devuelve el comportamiento anterior.

#pragma once

#include <cstdint>

namespace drc {

inline bool TickGridEnabled(const char* env) { return !(env && env[0] == '0'); }

class TickGrid {
 public:
  explicit TickGrid(double period_us, double min_delay_us = 1000.0)
      : period_(period_us), min_delay_(min_delay_us), next_(0.0), inited_(false), skipped_(0) {}

  // Llamar al acabar cada tick con el reloj monótono. Devuelve los µs hasta el siguiente disparo.
  int64_t NextDelayUs(uint64_t now_us) {
    const double now = static_cast<double>(now_us);
    if (!inited_) {
      inited_ = true;
      next_ = now;
    }
    next_ += period_;
    while (next_ - now < min_delay_) {
      next_ += period_;
      skipped_++;
    }
    return static_cast<int64_t>(next_ - now + 0.5);
  }

  uint64_t Skipped() const { return skipped_; }

 private:
  double period_, min_delay_, next_;
  bool inited_;
  uint64_t skipped_;
};

}  // namespace drc
