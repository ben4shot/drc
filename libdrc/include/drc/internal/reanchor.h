// Reanclaje del vsync del mando desde el emisor. Fuera de ±1000 µs el PLL no corrige, y en el estado 6 el reinicio por timesync
// (causa 5) no hace nada, así que el mando no se realinea solo y cada IDR falla en START. mc_video reconfigura la salida
// (drvvout_set_mode, que ancla el vsync a ts+palabra) cuando cambia la primera palabra de 16000 del astrm de formato
// (LVC FUN_00032382). Tras una ráfaga larga se alterna esa palabra entre 16000 y 16001.

#pragma once

#include <cstdint>
#include <cstdlib>

namespace drc {

const int kDefaultReanchorTicks = 0;      // apagado hasta validarlo con el GamePad
const int kReanchorCooldownTicks = 300;   // ~5 s entre reanclajes
const int kReanchorQuietTicks = 20;       // ~1/3 s sin resync: la ráfaga ha terminado

// DRC_REANCHOR_TICKS: ticks de ráfaga antes de reanclar (0 = apagado). Vacío, negativo o no numérico: valor por defecto.
inline int ReanchorTicksFromEnv(const char* env) {
  if (!env || !env[0]) return kDefaultReanchorTicks;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0) return kDefaultReanchorTicks;
  return static_cast<int>(v);
}

inline uint32_t AstrmFormatWord(unsigned generation) { return 16000u + (generation & 1u); }

class ReanchorPolicy {
 public:
  ReanchorPolicy(int after_ticks, int cooldown_ticks)
      : after_(after_ticks), cooldown_(cooldown_ticks), burst_(0), quiet_(0), since_fire_(cooldown_ticks) {}

  // Llamar una vez por tick; true = reanclar ahora.
  bool Tick(bool resync_this_tick) {
    if (since_fire_ < cooldown_) since_fire_++;
    if (after_ <= 0) return false;
    if (resync_this_tick) {
      quiet_ = 0;
    } else if (++quiet_ >= kReanchorQuietTicks) {
      burst_ = 0;
      return false;
    }
    if (burst_ > 0 || resync_this_tick) burst_++;
    if (burst_ >= after_ && since_fire_ >= cooldown_) {
      burst_ = 0;
      since_fire_ = 0;
      return true;
    }
    return false;
  }

 private:
  int after_, cooldown_, burst_, quiet_, since_fire_;
};

}  // namespace drc
