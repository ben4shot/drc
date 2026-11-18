// Presión del lápiz (DRC_TOUCH_MODE="pen"): del valor en crudo del panel resistivo del GamePad a 0..1023.
//
// El valor en crudo (12 bits, libdrc input-receiver.cpp) sube con la posición: medido con tools/pen_pressure.py,
// crudo ≈ nivel + kx·x + ky·y + kxx·x² + kyy·y² + kxy·x·y (x, y en crudo, 0..4095; curvo en x: con una recta el
// centro quedaba ~150-280 más alto que los bordes) y la fuerza solo cambia el nivel. Se descuenta la posición, el
// nivel se lleva a 0..1 con [min, max], se aplica la curva (t^curve) y un suavizado exponencial.
// Medido el 2026-11-17: toque suave ≈ -224, normal ≈ -32, fuerte ≈ -10 (el panel satura pronto con la punta dura);
// la curva 2 deja fino el toque suave y separa algo el normal del fuerte.
// Tocando nunca vale 0 (1..1023): 0 es «levantado».

#pragma once

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <string>

namespace drc_stream {

struct PenConfig {
    double kx = 0.7538;    // DRC_PEN_PRESSURE_KX: cuánto sube el crudo por unidad de x
    double ky = 0.0071;    // DRC_PEN_PRESSURE_KY: ídem por unidad de y
    double kxx = -8.68e-5; // DRC_PEN_PRESSURE_KXX, _KYY, _KXY: términos cuadráticos
    double kyy = 8.0e-6;
    double kxy = 2.51e-5;
    double min = -350;     // DRC_PEN_PRESSURE_MIN: nivel corregido del toque más suave (→ 1)
    double max = 20;       // DRC_PEN_PRESSURE_MAX: nivel corregido del toque más fuerte (→ 1023)
    double curve = 2.0;    // DRC_PEN_PRESSURE_CURVE: >1 = hace falta apretar más, <1 = más sensible
    double smooth = 0.3;   // DRC_PEN_PRESSURE_SMOOTH: 0 = sin suavizar … 0.9 = muy suave (el panel es ruidoso)
};

inline bool EnvDouble(const char* name, double* out) {
    const char* e = getenv(name);
    if (!e || !e[0]) return false;
    char* end = nullptr;
    double v = strtod(e, &end);
    if (!end || *end != '\0' || !std::isfinite(v)) return false;
    *out = v;
    return true;
}

inline PenConfig LoadPenConfig() {
    PenConfig c;
    EnvDouble("DRC_PEN_PRESSURE_KX", &c.kx);
    EnvDouble("DRC_PEN_PRESSURE_KY", &c.ky);
    EnvDouble("DRC_PEN_PRESSURE_KXX", &c.kxx);
    EnvDouble("DRC_PEN_PRESSURE_KYY", &c.kyy);
    EnvDouble("DRC_PEN_PRESSURE_KXY", &c.kxy);
    double lo = c.min, hi = c.max;
    EnvDouble("DRC_PEN_PRESSURE_MIN", &lo);
    EnvDouble("DRC_PEN_PRESSURE_MAX", &hi);
    if (hi > lo) { c.min = lo; c.max = hi; }                 // un rango vacío o al revés se ignora
    double v;
    if (EnvDouble("DRC_PEN_PRESSURE_CURVE", &v) && v > 0.05 && v < 20) c.curve = v;
    if (EnvDouble("DRC_PEN_PRESSURE_SMOOTH", &v) && v >= 0 && v < 1) c.smooth = v;
    return c;
}

class PenPressure {
public:
    explicit PenPressure(const PenConfig& c) : c_(c) {}

    // touching: hay contacto; raw: presión en crudo; rx, ry: posición en crudo. Devuelve 0 (levantado) o 1..1023.
    int Update(bool touching, int raw, int rx, int ry) {
        if (!touching) {
            down_ = false;
            return 0;
        }
        const double x = rx, y = ry;
        double level = raw - (c_.kx * x + c_.ky * y + c_.kxx * x * x + c_.kyy * y * y + c_.kxy * x * y);
        double t = std::clamp((level - c_.min) / (c_.max - c_.min), 0.0, 1.0);
        t = std::pow(t, c_.curve);
        t_ = down_ ? c_.smooth * t_ + (1.0 - c_.smooth) * t : t;   // el primer valor del toque entra entero
        down_ = true;
        return 1 + static_cast<int>(std::lround(t_ * 1022.0));
    }

private:
    PenConfig c_;
    bool down_ = false;
    double t_ = 0.0;
};

}  // namespace drc_stream
