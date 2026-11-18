// Tope de tamaño por frame (B17): la consola Wii U nunca pasa de ~25 KB por frame y los resync
// nuestros siguen a frames de 30-43 KB. Controlador de lazo cerrado sobre el QP: un frame que se
// pasa del tope sube el QP del siguiente; con margen holgado vuelve poco a poco al QP base.

#pragma once

#include <cstdlib>

namespace drc {

// DRC_FRAME_MAX: bytes máximos por frame (0 = desactivado). NULL, vacío, negativo o no numérico = 0.
inline int FrameMaxFromEnv(const char* env) {
  if (!env || !env[0]) return 0;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0) return 0;
  return static_cast<int>(v);
}

// DRC_QP_MIN / DRC_QP_MAX: rango del QP dinámico de los P. Por defecto 12-32 (la consola). Valores fuera de 10-51 o
// con mínimo mayor que máximo se ignoran.
inline void QpRangeFromEnv(const char* min_env, const char* max_env, int* lo, int* hi) {
  *lo = 12;
  *hi = 32;
  if (!min_env || !max_env) return;
  const int a = atoi(min_env), b = atoi(max_env);
  if (a >= 10 && b <= 51 && a <= b) {
    *lo = a;
    *hi = b;
  }
}

class FrameCap {
 public:
  // base_qp = mínimo, max_qp = máximo (51 por defecto), start_qp < 0 = arrancar en base_qp.
  FrameCap(int base_qp, int max_bytes, int step, int max_qp = 51, int start_qp = -1)
      : base_qp_(base_qp), max_bytes_(max_bytes), step_(step < 1 ? 1 : step),
        max_qp_(max_qp), qp_(start_qp < 0 ? base_qp : start_qp) {}

  bool enabled() const { return max_bytes_ > 0; }
  int qp() const { return qp_; }

  // Tamaño en bytes del frame recién codificado.
  void Update(int frame_bytes) {
    if (!enabled()) return;
    if (frame_bytes > max_bytes_) {
      qp_ += step_;
      if (qp_ > max_qp_) qp_ = max_qp_;
    } else if (static_cast<long long>(frame_bytes) * 10 < static_cast<long long>(max_bytes_) * 7) {
      if (qp_ > base_qp_) qp_--;
    }
  }

 private:
  int base_qp_;
  int max_bytes_;
  int step_;
  int max_qp_;
  int qp_;
};

}  // namespace drc
