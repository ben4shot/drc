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

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <drc/internal/astrm-packet.h>
#include <drc/internal/audio-streamer.h>
#include <drc/internal/events.h>
#include <drc/internal/tsf.h>
#include <drc/internal/udp.h>
#include <drc/types.h>
#include <mutex>
#include <pthread.h>
#include <string>
#include <vector>

namespace drc {

namespace {

const u32 kSamplesPerSecond = 48000;

// Chosen to be the best compromise between packet size (must be < 1600 bytes)
// and number of packets sent per second, while keeping a round number of
// milliseconds between 2 packets.
//
// 384 samples == one 1536b packet every 8ms (original; por defecto; la consola usa 416). Con MTU 1500
// se fragmenta en IP, pero el GamePad lo reproduce continuo.
// 192 samples == one 768b packet every 4ms (sin fragmentación): con el GamePad el tono
// sonaba entrecortado. DRC_AUDIO_PACKET_SAMPLES=192 lo restaura.
const u32 kSamplesPerPacket =
    AudioPacketSamplesFromEnv(getenv("DRC_AUDIO_PACKET_SAMPLES"));
const u32 kPacketIntervalMs = AudioPacketIntervalMs(kSamplesPerPacket);

u64 NowMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}

s32 GetTimestamp() {
  u64 tsf;
  GetTsf(&tsf);

  // Diagnóstico: desfase solo del audio, releído 1 vez/s (ver AudioTimestampOffsetUs).
  static long long offset_us = kAudioTimestampOffsetUs;
  static u64 next_read = 0;
  if (tsf >= next_read) {
    next_read = tsf + 1000000ULL;
    long long v = 0;
    bool have = false;
    FILE* f = fopen("/tmp/drc_audio_ts_offset_us", "r");
    if (f) {
      have = fscanf(f, "%lld", &v) == 1;
      fclose(f);
    }
    offset_us = AudioTimestampOffsetUs(have, v);
  }
  return static_cast<s32>((tsf + offset_us) & 0xFFFFFFFF);
}

}  // namespace

AudioStreamer::AudioStreamer(const std::string& dst)
    : astrm_client_(new UdpClient(dst)) {
}

AudioStreamer::~AudioStreamer() {
  Stop();
}

bool AudioStreamer::Start() {
  if (!astrm_client_->Start()) {
    Stop();
    return false;
  }
  StartEM();
  return true;
}

void AudioStreamer::Stop() {
  StopEM();
  astrm_client_->Stop();
}

void AudioStreamer::InitEventsAndRun() {
  // Este hilo corre el temporizador de paquetes: ver AudioThreadPriority.
  int policy = 0;
  struct sched_param sp;
  if (pthread_getschedparam(pthread_self(), &policy, &sp) == 0) {
    int prio = AudioThreadPriority(policy, sp.sched_priority);
    if (prio > 0) {
      sp.sched_priority = prio;
      pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp);
    }
  }
  u16 seqid = 0;
  // Flujo continuo de audio (silencio cuando no hay sonido). Necesario para
  // mantener enganchado el reloj de audio y para que la vibración (bit 3) funcione.
  const char* silence_env = getenv("DRC_AUDIO_SEND_SILENCE");
  const bool send_silence = (silence_env == NULL || strcmp(silence_env, "0") != 0);

  NewRepeatedTimerEvent(kPacketIntervalMs * 1000000, [&](Event*) {
    std::vector<s16> samples;
    samples_.Pop(&samples, kSamplesPerPacket * 2);
    // El motor se apaga solo a los 18 ms sin paquetes: mientras vibra se envía aunque haya silencio.
    const bool vibrating = vibrate_.Active(NowMs());
    if (!send_silence && !vibrating &&
        std::all_of(samples.begin(), samples.end(),
                    [](s16 v) { return v == 0; })) {
      return true;
    }

    if (samples.size() < kSamplesPerPacket * 2) {
      samples.resize(kSamplesPerPacket * 2, 0);
    }

    AstrmPacket pkt;
    pkt.SetPacketType(AstrmPacketType::kAudioData);
    pkt.SetFormat(AstrmFormat::kPcm48KHz);
    pkt.SetMonoFlag(false);
    pkt.SetVibrateFlag(vibrating);
    pkt.SetSeqId(seqid);
    pkt.SetTimestamp(GetTimestamp());

    // TODO(delroth): this assumes a little endian host system
    pkt.SetPayload(reinterpret_cast<byte*>(samples.data()),
                   samples.size() * sizeof (s16));

    astrm_client_->Send(pkt.GetBytes(), pkt.GetSize());
    seqid = (seqid + 1) & 0x3FF;

    return true;
  });

  ThreadedEventMachine::InitEventsAndRun();
}

void AudioStreamer::Vibrate(int ms) {
  vibrate_.Start(NowMs(), ms);
}

void AudioStreamer::PushSamples(const std::vector<s16>& samples) {
  samples_.Push(samples);
}

}  // namespace drc
