// Codificador en su propio hilo, con UNA sola plaza (DRC_ASYNC_ENCODE=1).
//
// Hoy la codificación ocurre dentro del tick de 16,7 ms: un frame lento (a QP bajo hay picos de 25-30 ms) retrasa el
// temporizador y se pierde el slot siguiente. Aquí el tick solo entrega un frame al hilo (Submit) y recoge el
// resultado cuando está listo (TryTake), sin bloquearse nunca. Si el hilo sigue ocupado, Submit() devuelve false y el
// llamante descarta ese frame: nunca hay cola, así que un frame lento no acumula retraso.

#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace drc {

inline bool AsyncEncodeEnabled(const char* env) { return env && env[0] == '1'; }

class AsyncEncoder {
 public:
  struct Result {
    std::vector<std::string> chunks;
    double enc_ms = 0;
    bool idr = false;
    uint32_t tag = 0;   // el timestamp con que se envió el trabajo
  };
  using EncodeFn = std::function<std::vector<std::string>(const std::vector<unsigned char>&, bool)>;

  // output_idr (opcional): tipo del frame que acaba de salir, cuando el codificador entrega con retraso.
  explicit AsyncEncoder(EncodeFn fn, std::function<bool()> output_idr = nullptr)
      : fn_(std::move(fn)), output_idr_(std::move(output_idr)), thread_([this] { Run(); }) {}

  ~AsyncEncoder() {
    {
      std::lock_guard<std::mutex> lk(mu_);
      stop_ = true;
    }
    cv_.notify_all();
    thread_.join();
  }

  AsyncEncoder(const AsyncEncoder&) = delete;
  AsyncEncoder& operator=(const AsyncEncoder&) = delete;

  // Hay un trabajo en curso o un resultado sin recoger.
  bool busy() const {
    std::lock_guard<std::mutex> lk(mu_);
    return job_pending_ || running_ || have_result_;
  }

  // Copia el frame y lo entrega al hilo. false (y se cuenta) si no hay plaza.
  bool Submit(const std::vector<unsigned char>& frame, bool idr, uint32_t tag) {
    {
      std::lock_guard<std::mutex> lk(mu_);
      if (job_pending_ || running_ || have_result_) {
        ++rejected_;
        return false;
      }
      frame_ = frame;
      idr_ = idr;
      tag_ = tag;
      job_pending_ = true;
    }
    cv_.notify_all();
    return true;
  }

  // No bloquea. true si había un resultado, que se mueve a *out y libera la plaza.
  bool TryTake(Result* out) {
    std::lock_guard<std::mutex> lk(mu_);
    if (!have_result_) return false;
    *out = std::move(result_);
    have_result_ = false;
    return true;
  }

  uint64_t rejected() const {
    std::lock_guard<std::mutex> lk(mu_);
    return rejected_;
  }

 private:
  void Run() {
    for (;;) {
      std::vector<unsigned char> frame;
      bool idr;
      uint32_t tag;
      {
        std::unique_lock<std::mutex> lk(mu_);
        cv_.wait(lk, [this] { return stop_ || job_pending_; });
        if (stop_) return;
        frame = std::move(frame_);
        idr = idr_;
        tag = tag_;
        job_pending_ = false;
        running_ = true;
      }
      auto t0 = std::chrono::steady_clock::now();
      Result r;
      r.chunks = fn_(frame, idr);
      r.enc_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
      r.idr = output_idr_ ? output_idr_() : idr;
      r.tag = tag;
      {
        std::lock_guard<std::mutex> lk(mu_);
        result_ = std::move(r);
        have_result_ = true;
        running_ = false;
      }
    }
  }

  EncodeFn fn_;
  std::function<bool()> output_idr_;
  mutable std::mutex mu_;
  std::condition_variable cv_;
  std::vector<unsigned char> frame_;
  bool idr_ = false;
  uint32_t tag_ = 0;
  bool job_pending_ = false;
  bool running_ = false;
  bool have_result_ = false;
  bool stop_ = false;
  uint64_t rejected_ = 0;
  Result result_;
  std::thread thread_;   // el último miembro: arranca cuando todo lo demás ya está construido
};

}  // namespace drc
