// Timestamps del vídeo en rejilla regular (B17). En la captura de la consola los frames van sellados
// cada 16683,33 us (deltas 16683 +-1 us, desvío máx 6 us); sellar con el TSF leído en cada envío
// añade el jitter del temporizador (hasta cientos de us, a veces ms). TsGrid sigue al TSF con una
// pendiente limitada por tick en lugar de copiar su ruido.

#pragma once

#include <cmath>
#include <cstdint>
#include <cstdlib>

namespace drc {

// Corrección máxima por tick. El PLL del mando solo sigue ±3,2 µs por frame: más deprisa, la fase del ts se le escapa.
const double kDefaultTsSlewUs = 3.0;

// DRC_TS_SLEW_US: vacío, no numérico o no positivo = kDefaultTsSlewUs.
inline double TsSlewFromEnv(const char* env) {
  if (!env || !env[0]) return kDefaultTsSlewUs;
  char* end = nullptr;
  double v = strtod(env, &end);
  return (*end == '\0' && v > 0.0) ? v : kDefaultTsSlewUs;
}

class TsGrid {
 public:
  // period_us: periodo del frame (1e6/59.94); max_slew_us: corrección máxima hacia el TSF por tick.
  TsGrid(double period_us, double max_slew_us)
      : period_(period_us), slew_(max_slew_us), ts_(0), err_(0), inited_(false) {}

  // Llamar en cada tick (también en los de hueco) con el sello "en bruto" (TSF - intervalo).
  // Devuelve el timestamp en rejilla.
  uint32_t Next(uint32_t target) {
    if (!inited_) {
      inited_ = true;
      ts_ = target;
      return Round();
    }
    const double diff = static_cast<double>(static_cast<int32_t>(target - Round()));
    if (std::fabs(diff) > 1000000.0) {               // salto grande (reinicio del TSF): re-anclar
      ts_ = target;
      return Round();
    }
    long n = std::lround(diff / period_);            // ticks transcurridos (>= 1 salvo ruido)
    if (n < 1) n = 1;
    ts_ += n * period_;
    double e = static_cast<double>(static_cast<int32_t>(target - Round()));
    err_ = e;
    if (e > slew_) e = slew_;
    if (e < -slew_) e = -slew_;
    ts_ += e;
    return Round();
  }

  // Diferencia entre el objetivo y la rejilla antes de corregir en el último Next() (diagnóstico).
  double LastError() const { return err_; }

  // Desplaza la fase de golpe (solo para pruebas: /tmp/drc_kick).
  void Shift(double us) { ts_ += us; }

 private:
  uint32_t Round() const { return static_cast<uint32_t>(static_cast<int64_t>(std::llround(ts_))); }
  double period_, slew_, ts_, err_;
  bool inited_;
};

}  // namespace drc
