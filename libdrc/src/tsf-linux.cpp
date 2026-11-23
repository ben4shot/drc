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

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <drc/internal/tsf.h>
#include <drc/internal/tsf-clock.h>
#include <drc/internal/tsf-retry.h>
#include <drc/types.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <mutex>
#include <netdb.h>
#include <string>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <net/if.h>
#include <time.h>
#include <unistd.h>

namespace drc {

namespace {

int GetInterfaceOfIpv4(const std::string& addr, std::string* out) {
  struct ifaddrs* ifa = NULL;
  char ip_buffer[16];
  int rv = EAI_FAIL;

  if (getifaddrs(&ifa) == -1 || ifa == NULL) {
    return rv;
  }

  struct ifaddrs* p = ifa;
  while (p) {
    if ((p->ifa_flags & IFF_RUNNING) && (p->ifa_addr != NULL)) {
      if (p->ifa_addr->sa_family == AF_INET) {
        rv = getnameinfo(p->ifa_addr, sizeof (struct sockaddr_in), ip_buffer,
                         sizeof (ip_buffer), NULL, 0, NI_NUMERICHOST);
        if ((rv == 0) && (addr == ip_buffer)) {
          break;
        }
      }
    }
    p = p->ifa_next;
  }

  if (p) {
    *out = p->ifa_name;
  } else {
    rv = EAI_FAIL;
  }

  freeifaddrs(ifa);
  return rv;
}

u64 NowUs() {
  // CLOCK_MONOTONIC_RAW no sufre el slew de NTP (timesyncd) y coincide con ktime_get_raw_ns del kernel.
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
  return static_cast<u64>(ts.tv_sec) * 1000000ULL + static_cast<u64>(ts.tv_nsec) / 1000ULL;
}

std::atomic<long long> g_anchor_step_max(0);
std::atomic<int> g_anchor_count(0);
std::atomic<int> g_gate_rejects(0);

// Abre el TSF de sysfs de la interfaz del AP DRC (-1 si aún no existe).
int OpenTsfFile() {
  std::string drc_if;
  if (GetInterfaceOfIpv4("192.168.1.10", &drc_if) != 0) {
    return -1;
  }
  std::string tsf_path = std::string("/sys/class/net/") + drc_if +
                         "/device/tsf";
  int fd = open(tsf_path.c_str(), O_RDONLY);
  if (fd == -1) {
    tsf_path = std::string("/sys/class/net/") + drc_if + "/tsf";
    fd = open(tsf_path.c_str(), O_RDONLY);
  }
  return fd;
}

}  // namespace

void TakeTsfAnchorStats(long long* max_step_us, int* count) {
  *max_step_us = g_anchor_step_max.exchange(0);
  *count = g_anchor_count.exchange(0);
}

int TakeTsfGateRejects() {
  return g_gate_rejects.exchange(0);
}

// Devuelve el TSF del BSS. Re-ancla con sysfs cada kTsfResyncIntervalUs (ver
// tsf-clock.h) para no acumular deriva entre el reloj de la Pi y el del chip.
int GetTsf(u64 *tsf) {
  static std::mutex mu;
  static int fd = -1;
  // DRC_TSF_RESYNC_US: intervalo de re-anclaje con sysfs (0 = leer el TSF una sola
  // vez y avanzar solo con el reloj local: timestamps sin saltos).
  static TsfClock clock = [] {
    const char* env = getenv("DRC_TSF_RESYNC_US");
    long long v = env ? atoll(env) : static_cast<long long>(kTsfResyncIntervalUs);
    u64 interval = v <= 0 ? ~0ULL : static_cast<u64>(v);
    bool bidir = !TsfSawtoothFromEnv(getenv("DRC_TSF_SAWTOOTH"));
    fprintf(stderr, "[libdrc] tsf_resync_us=%lld bidir=%d\n", v, bidir ? 1 : 0);
    return TsfClock(interval, kTsfResetThresholdUs, bidir);
  }();
  static bool warned = false;
  static const bool rtt_gate_on = [] {
    bool on = TsfRttGateFromEnv(getenv("DRC_TSF_RTT_GATE"));
    fprintf(stderr, "[libdrc] tsf_rtt_gate=%d\n", on ? 1 : 0);
    return on;
  }();
  static TsfRttGate rtt_gate;

  // DRC_TSF_MODE=peak|mid: timestamps de la envolvente del TSF (muestreado cada
  // 10 ms) en lugar de la lectura instantánea en diente de sierra.
  static const int env_mode = [] {
    const char* m = getenv("DRC_TSF_MODE");
    int v = !m ? 0 : (m[0] == 'p' ? 1 : (m[0] == 'm' ? 2 : 0));
    fprintf(stderr, "[libdrc] tsf_mode=%s\n", v == 1 ? "peak" : v == 2 ? "mid" : "clock");
    return v;
  }();
  static TsfEnvelope envelope;
  static u64 last_sample_us = 0;

  std::unique_lock<std::mutex> lock(mu);
  u64 now_us = NowUs();

  bool have_env = false;
  if (env_mode) {
    if (fd == -1) {
      fd = OpenTsfFile();
    }
    u64 hw_tsf = 0;
    if (fd != -1 && now_us - last_sample_us >= 10000 &&
        pread(fd, &hw_tsf, sizeof (hw_tsf), 0) == sizeof (hw_tsf) && hw_tsf != 0) {
      envelope.AddSample(hw_tsf, now_us);
      last_sample_us = now_us;
    }
    have_env = envelope.Get(now_us, env_mode == 1 ? TsfEnvelope::kPeak : TsfEnvelope::kMid, tsf);
  }

  if (!env_mode && clock.NeedsResync(now_us)) {
    static bool ever_anchored = false;
    if (ever_anchored) {
      // Sellar last_check_us_ antes de soltar el mutex evita que fallos (-EAGAIN) o
      // lecturas USB lentas hagan reintentar pread() 340 veces/s ni bloqueen al otro hilo.
      clock.TouchCheck(now_us);
      int local_fd = fd;
      lock.unlock();
      if (local_fd == -1) {
        local_fd = OpenTsfFile();
      }
      u64 hw_tsf = 0;
      const u64 start_us = NowUs();
      bool ok = local_fd != -1 &&
                pread(local_fd, &hw_tsf, sizeof (hw_tsf), 0) == sizeof (hw_tsf) &&
                hw_tsf != 0;
      u64 done_us = NowUs();
      lock.lock();
      if (fd == -1 && local_fd != -1) {
        fd = local_fd;
      }
      now_us = done_us;
      u64 anchor_us = done_us;
      if (ok && rtt_gate_on) {
        // Lectura lenta (bus SDIO ocupado): no se ancla y se reintenta pasados 150 ms (el driver de
        // brcmfmac guarda el valor 100 ms; antes devolvería el mismo, interpolado).
        if (rtt_gate.Accept(done_us - start_us)) {
          anchor_us = TsfMidpoint(start_us, done_us);
        } else {
          ok = false;
          g_gate_rejects++;
          clock.RetryIn(done_us, 150000);
        }
      }
      if (ok) {
        const long long step = clock.Anchor(hw_tsf, anchor_us);
        if (step != 0) {
          g_anchor_count++;
          const long long mag = step < 0 ? -step : step;
          const long long prev = g_anchor_step_max.load();
          const long long prev_mag = prev < 0 ? -prev : prev;
          if (mag > prev_mag) g_anchor_step_max = step;
        }
      }
    } else {
      if (fd == -1) {
        fd = OpenTsfFile();
      }
      u64 hw_tsf = 0;
      auto try_read = [&]() {
        if (fd == -1) fd = OpenTsfFile();
        return fd != -1 &&
               pread(fd, &hw_tsf, sizeof (hw_tsf), 0) == sizeof (hw_tsf) &&
               hw_tsf != 0;
      };
      // Sin ancla se reintenta ~0,5 s (rtw89 devuelve -EAGAIN mientras mac80211 tiene el bloqueo, justo
      // al asociarse el GamePad).
      if (RetryRead(try_read, 250, 2000)) {
        now_us = NowUs();
        clock.Anchor(hw_tsf, now_us);
        ever_anchored = true;
      } else if (!warned) {
        warned = true;
        fprintf(stderr, "[libdrc] GetTsf: TSF de sysfs no disponible; "
                        "se reintentará (timestamps no fiables mientras tanto)\n");
      }
    }
  }

  // Diagnóstico: desfase en µs leído (1 vez/s) de /tmp/drc_ts_offset_us para
  // barrer en caliente la ventana de timestamps que acepta el GamePad.
  // Sin fichero se usa kTimestampLeadUs (ventana aceptada por el GamePad).
  static const s64 default_lead_us = [] {
    s64 v = TimestampLeadFromEnv(getenv("DRC_TS_OFFSET_US"));
    fprintf(stderr, "[libdrc] ts_offset_us=%lld\n", static_cast<long long>(v));
    return v;
  }();
  static s64 offset_us = default_lead_us;
  static u64 next_offset_read = 0;
  if (now_us >= next_offset_read) {
    next_offset_read = now_us + 1000000ULL;
    bool have = false;
    long long v = 0;
    FILE* f = fopen("/tmp/drc_ts_offset_us", "r");
    if (f) {
      have = fscanf(f, "%lld", &v) == 1;
      fclose(f);
    }
    offset_us = TimestampOffsetUs(have, v, default_lead_us);
  }

  if (have_env || (!env_mode && clock.Get(now_us, tsf))) {
    *tsf += offset_us;
    return 0;
  }

  // Nunca anclado: reloj local como último recurso.
  *tsf = now_us;
  return -1;
}

}  // namespace drc
