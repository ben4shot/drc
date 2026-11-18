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
#include <cstring>
#include <drc/input.h>
#include <drc/internal/uvcuac-synchronizer.h>
#include <vector>

namespace drc {

UvcUacStateSynchronizer::UvcUacStateSynchronizer(CmdClient* cmd_client)
    : cmd_client_(cmd_client) {
}

UvcUacStateSynchronizer::~UvcUacStateSynchronizer() {
  Stop();
}

bool UvcUacStateSynchronizer::Start() {
  StartEM();
  return true;
}

void UvcUacStateSynchronizer::Stop() {
  StopEM();
}

void UvcUacStateSynchronizer::FillCamFields(UvcUacCmdPacket* p) {
  if (cam_wanted_ == 1) {
    std::lock_guard<std::mutex> l(cam_mutex_);
    p->SetCameraLikeConsole(cam_open_pulse_.exchange(false), cam_regs_, &cam_settings_);
  } else if (cam_wanted_ == 0) {
    p->SetCameraOff();
  }
}

void UvcUacStateSynchronizer::OnCamReply(const byte* d, size_t n) {
  if (n < 12) return;
  std::lock_guard<std::mutex> l(cam_mutex_);
  memcpy(cam_regs_, d + 4, 8);
}

void UvcUacStateSynchronizer::InitEventsAndRun() {
  NewRepeatedTimerEvent(1000 * 1000 * 1000, [&](Event*) {
    if (cam_wanted_ >= 0 || mic_wanted_ >= 0) {
      state_.SetControlFlag(1);
    } else {
      state_.SetControlFlag(0);
    }
    if (mic_wanted_ >= 0) {
      state_.SetMicEnabled(mic_wanted_ == 1);
      state_.SetMicMuted(mic_wanted_ == 0);
      if (mic_wanted_ == 1) {
        state_.SetMicVolume(0x0016);
        state_.SetMicJackVolume(0x0019);
      }
    }
    if (cam_wanted_ >= 0) FillCamFields(&state_);
    std::vector<byte> drc_reply_state;
    std::vector<byte> raw;
    {
      std::lock_guard<std::mutex> l(raw_mutex_);
      raw = raw_;
    }
    bool success;
    if (!raw.empty()) {
      raw.resize(state_.GetSize(), 0);
      success = cmd_client_->Query(CmdQueryType::kUvcUacCommand, raw.data(), raw.size(), &drc_reply_state);
    } else {
      success = cmd_client_->Query(CmdQueryType::kUvcUacCommand,
                                   state_.GetBytes(), state_.GetSize(),
                                   &drc_reply_state);
    }
    if (cam_wanted_ >= 0 || mic_wanted_ >= 0 || !raw.empty()) {
      fprintf(stderr, "[libdrc] UVC/UAC: query res=%d, reply_sz=%zu, cam_en=%d cam_pwr=%d\n",
              success ? 1 : 0, drc_reply_state.size(),
              (int)state_.CamEnabled(), (int)state_.CamPowerFrequency());
      if (success && !drc_reply_state.empty()) {
        fprintf(stderr, "[libdrc] UVC/UAC reply:");
        for (byte b : drc_reply_state) fprintf(stderr, " %02x", b);
        fprintf(stderr, "\n");
      }
    }
    if (success && drc_reply_state.size() >= 15) {
      const byte * d = drc_reply_state.data();
      OnCamReply(d, drc_reply_state.size());
      state_.SetMicVolume(d[0] | (d[1] << 8));
      state_.SetMicJackVolume(d[2] | (d[3] << 8));

      // TODO(parlane) complete when someone finds out the
      //               meaning of these fields
      u32 unk_mc_video_cmos = d[4] | (d[5] << 8) | (d[6] << 16) | (d[7] << 24);
      (void)unk_mc_video_cmos;
      u32 unk8 = d[8] | (d[9] << 8) | (d[10] << 16) | (d[11] << 24);
      (void)unk8;

      state_.SetMicEnabled(d[12] != 0);
      state_.SetCamPowerFrequency(d[13]);
      state_.SetCamAutoExposureEnabled(d[14] != 0);
    }
    return true;
  });

  ThreadedEventMachine::InitEventsAndRun();
}

}  // namespace drc
