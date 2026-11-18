// Copyright (c) 2013, Mema Hacking, All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are met:
//
// 1. Redistributions of source code must retain the above copyright notice,
// this list of conditions and the following disclaimer.
//
// 2. Redistributions in binary form must reproduce the above copyright
// notice, this list of conditions and the following disclaimer in the
// documentation and/or other materials provided with the distribution.
//
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
// AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
// IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
// ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
// LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
// CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
// SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
// INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
// CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
// ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
// POSSIBILITY OF SUCH DAMAGE.

#pragma once

#include <drc/internal/events.h>
#include <drc/types.h>
#include <cstdlib>
#include <functional>
#include <netinet/in.h>
#include <string>
#include <vector>

namespace drc {

// DRC_PACING_KBPS (B17): ritmo máximo del socket de vídeo en kbit/s, devuelto en bytes/s
// para SO_MAX_PACING_RATE (lo aplica la cola fq de wlan0). La consola reparte cada frame
// de ~25 KB en ~12,7 ms; 16000 kbit/s lo reparte en ~12,5 ms. Sin variable, vacía, no
// numérica o negativa = 16000; "0" = sin límite.
const unsigned kDefaultPacingKbps = 16000;
inline unsigned PacingRateFromEnv(const char* env) {
  long kbps = kDefaultPacingKbps;
  if (env && env[0]) {
    char* end = nullptr;
    long v = strtol(env, &end, 10);
    if (*end == '\0' && v >= 0) kbps = v;
  }
  return static_cast<unsigned>(kbps * 1000 / 8);
}

// DRC_FRAME_SPREAD_US (B17): tiempo en que se reparte cada frame de vídeo. La consola envía
// frames de 1 a 53 KB en 11-15 ms (ritmo por frame, no fijo). Por defecto 12500 us; "0" =
// desactivado (ritmo fijo de DRC_PACING_KBPS); vacío, negativo o no numérico = por defecto.
const int kDefaultFrameSpreadUs = 12500;
inline int FrameSpreadUsFromEnv(const char* env) {
  if (!env || !env[0]) return kDefaultFrameSpreadUs;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0) return kDefaultFrameSpreadUs;
  return static_cast<int>(v);
}

// Instante de salida (ns desde el inicio del frame) del paquete `index` de `count`, repartidos de
// forma uniforme en `spread_us`. 0 si el reparto está desactivado o no hay paquetes.
inline unsigned long long FrameTxOffsetNs(int index, int count, int spread_us) {
  if (spread_us <= 0 || count <= 0 || index <= 0) return 0;
  return static_cast<unsigned long long>(index) * spread_us * 1000ULL / count;
}

// DRC_TOS: campo TOS de los paquetes de vídeo y audio (0-255, decimal o 0x..). mac80211 saca el TID de
// la precedencia: 0xA0 -> TID 5 (AC_VI), el que usa la consola (98,8 % de sus tramas). -1 = no tocar.
inline int TosFromEnv(const char* env) {
  if (!env || !env[0]) return -1;
  char* end = nullptr;
  long v = strtol(env, &end, 0);
  if (*end != '\0' || v < 0 || v > 255) return -1;
  return static_cast<int>(v);
}

class UdpClient {
 public:
  explicit UdpClient(const std::string& dst_addr);
  virtual ~UdpClient();

  bool Start();
  void Stop();

  // SO_MAX_PACING_RATE en bytes/s; 0 no toca el socket. Llamar tras Start().
  bool SetMaxPacingRate(unsigned bytes_per_s);
  // IP_TOS del socket (tos < 0 = no tocar).
  bool SetTos(int tos);
  int Fd() const { return sock_fd_; }

  // SO_TXTIME con CLOCK_MONOTONIC: la cola fq retiene cada paquete hasta su instante de salida.
  bool EnableTxTime();
  // Envía con instante de salida `txtime_ns` (CLOCK_MONOTONIC); si EnableTxTime() no se activó, envía ya.
  bool SendAt(const byte* data, size_t size, unsigned long long txtime_ns);

  bool Send(const byte* data, size_t size);
  bool Send(const std::vector<byte>& msg) {
    return Send(msg.data(), msg.size());
  }

 private:
  int sock_fd_;
  bool txtime_;
  std::string dst_addr_;
  sockaddr_in dst_addr_parsed_;
};

class UdpServer : public ThreadedEventMachine {
 public:
  typedef std::function<void(const std::vector<byte>&)> ReceiveCallback;
  typedef std::function<void(void)> TimeoutCallback;

  explicit UdpServer(const std::string& bind_addr);
  virtual ~UdpServer();

  bool Start();
  void Stop();

  void SetTimeout(u64 us) { timeout_us_ = us; }

  void SetReceiveCallback(ReceiveCallback cb) { recv_cb_ = cb; }
  void SetTimeoutCallback(TimeoutCallback cb) { timeout_cb_ = cb; }

 protected:
  virtual void InitEventsAndRun();

 private:
  int sock_fd_;
  u64 timeout_us_;

  std::string bind_addr_;

  ReceiveCallback recv_cb_;
  TimeoutCallback timeout_cb_;
};

}  // namespace drc
