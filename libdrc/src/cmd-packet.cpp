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

#include <cassert>
#include <cstring>
#include <drc/internal/cmd-packet.h>
#include <drc/types.h>
#include <vector>

namespace drc {

CmdPacket::CmdPacket() {
  ResetPacket();
}

CmdPacket::CmdPacket(const std::vector<byte>& packet) {
  memcpy(pkt_.data(), packet.data(), packet.size());
}

CmdPacket::~CmdPacket() {
}

void CmdPacket::ResetPacket() {
  memset(pkt_.data(), 0, pkt_.size());
}

CmdPacketType CmdPacket::PacketType() const {
  return static_cast<CmdPacketType>((pkt_[1] << 8) | pkt_[0]);
}

CmdQueryType CmdPacket::QueryType() const {
  return static_cast<CmdQueryType>((pkt_[3] << 8) | pkt_[2]);
}

u16 CmdPacket::SeqId() const {
  return (pkt_[7] << 8) | pkt_[6];
}

const byte* CmdPacket::Payload() const {
  return pkt_.data() + kCmdHeaderSize;
}

size_t CmdPacket::PayloadSize() const {
  return (pkt_[5] << 8) | pkt_[4];
}

void CmdPacket::SetPacketType(CmdPacketType type) {
  u16 t = static_cast<u16>(type);
  pkt_[0] = t & 0xFF;
  pkt_[1] = t >> 8;
}

void CmdPacket::SetQueryType(CmdQueryType type) {
  u16 t = static_cast<u16>(type);
  pkt_[2] = t & 0xFF;
  pkt_[3] = t >> 8;
}

void CmdPacket::SetSeqId(u16 seqid) {
  pkt_[6] = seqid & 0xFF;
  pkt_[7] = seqid >> 8;
}

void CmdPacket::SetPayload(const byte* payload, size_t size) {
  assert(size <= kMaxCmdPayloadSize);
  memcpy(pkt_.data() + kCmdHeaderSize, payload, size);

  pkt_[4] = size & 0xFF;
  pkt_[5] = size >> 8;
}


GenericCmdPacket::GenericCmdPacket() {
  ResetPacket();
}

GenericCmdPacket::GenericCmdPacket(const std::vector<byte>& packet) {
  memcpy(pkt_.data(), packet.data(), packet.size());
}

GenericCmdPacket::~GenericCmdPacket() {
}

void GenericCmdPacket::ResetPacket() {
  memset(pkt_.data(), 0, pkt_.size());

  // Set magic
  pkt_[0] = 0x7E;
  pkt_[1] = 0x01;

  // Set service and method to invalid values to avoid sending a valid command
  // with an uninitialized packet.
  SetFinalFragment(true);
  SetServiceId(0xFF);
  SetMethodId(0xFF);
}

u16 GenericCmdPacket::TransactionId() const {
  return (pkt_[2] << 8) | (pkt_[3] >> 4);
}

bool GenericCmdPacket::FinalFragment() const {
  return (pkt_[3] & 0x8) != 0;
}

u16 GenericCmdPacket::FragmentId() const {
  return ((pkt_[3] & 0x7) << 8) | pkt_[4];
}

u8 GenericCmdPacket::Flags() const {
  return pkt_[5];
}

u8 GenericCmdPacket::ServiceId() const {
  return pkt_[6];
}

u8 GenericCmdPacket::MethodId() const {
  return pkt_[7];
}

u16 GenericCmdPacket::ErrorCode() const {
  return (pkt_[8] << 8) | pkt_[9];
}

const byte* GenericCmdPacket::Payload() const {
  return pkt_.data() + kGenericCmdHeaderSize;
}

size_t GenericCmdPacket::PayloadSize() const {
  return (pkt_[10] << 8) | pkt_[11];
}

void GenericCmdPacket::SetTransactionId(u16 tid) {
  assert(tid < 0x400);
  pkt_[2] = tid >> 8;
  pkt_[3] = (pkt_[3] & 0xF) | ((tid << 4) & 0xF0);
}

void GenericCmdPacket::SetFinalFragment(bool v) {
  if (v) {
    pkt_[3] |= 0x08;
  } else {
    pkt_[3] &= ~0x08;
  }
}

void GenericCmdPacket::SetFragmentId(u16 fid) {
  assert(fid < 0x800);
  pkt_[3] = (pkt_[3] & 0xf0) | (fid >> 8);
  pkt_[4] = fid & 0xFF;
}

void GenericCmdPacket::SetFlags(u8 flags) {
  pkt_[5] = flags;
}

void GenericCmdPacket::SetServiceId(u8 sid) {
  pkt_[6] = sid;
}

void GenericCmdPacket::SetMethodId(u8 mid) {
  pkt_[7] = mid;
}

void GenericCmdPacket::SetErrorCode(u16 error_code) {
  pkt_[8] = error_code >> 8;
  pkt_[9] = error_code & 0xFF;
}

void GenericCmdPacket::SetPayload(const byte* payload, size_t size) {
  assert(size <= kMaxGenericCmdPayloadSize);
  memcpy(pkt_.data() + kGenericCmdHeaderSize, payload, size);

  pkt_[10] = (size >> 8) & 0xFF;
  pkt_[11] = size & 0xFF;
}

UvcUacCmdPacket::UvcUacCmdPacket() {
  ResetPacket();
}

UvcUacCmdPacket::UvcUacCmdPacket(const std::vector<byte>& packet) {
  memcpy(pkt_.data(), packet.data(), packet.size());
}

UvcUacCmdPacket::~UvcUacCmdPacket() {
}

void UvcUacCmdPacket::ResetPacket() {
  memset(pkt_.data(), 0, pkt_.size());

  SetMicSamplingRate(16000);
}

bool UvcUacCmdPacket::MicEnabled() const {
  return pkt_[4];
}

bool UvcUacCmdPacket::MicMuted() const {
  return pkt_[5];
}

s16 UvcUacCmdPacket::MicVolume() const {
  return (pkt_[6] << 8) | pkt_[7];
}

s16 UvcUacCmdPacket::MicJackVolume() const {
  return (pkt_[8] << 8) | pkt_[9];
}

u16 UvcUacCmdPacket::MicSamplingRate() const {
  return (pkt_[12] << 8) | pkt_[13];
}

bool UvcUacCmdPacket::CamEnabled() const {
  return pkt_[14];
}

u8 UvcUacCmdPacket::CamPowerFrequency() const {
  return pkt_[16];
}

bool UvcUacCmdPacket::CamAutoExposureEnabled() const {
  return pkt_[17] != 0;
}

void UvcUacCmdPacket::SetControlFlag(u8 f1) {
  pkt_[0] = f1;
}

u8 UvcUacCmdPacket::ControlFlag() const {
  return pkt_[0];
}

void UvcUacCmdPacket::SetMicEnabled(bool flag) {
  pkt_[4] = flag ? 1 : 0;
}

void UvcUacCmdPacket::SetMicMuted(bool flag) {
  pkt_[5] = flag ? 1 : 0;
}

void UvcUacCmdPacket::SetMicVolume(s16 volume) {
  pkt_[6] = volume >> 8;
  pkt_[7] = volume & 0xFF;
}

void UvcUacCmdPacket::SetMicJackVolume(s16 volume) {
  pkt_[8] = volume >> 8;
  pkt_[9] = volume & 0xFF;
}

void UvcUacCmdPacket::SetMicSamplingRate(u16 sampling_rate) {
  pkt_[12] = sampling_rate >> 8;
  pkt_[13] = sampling_rate & 0xFF;
}

void UvcUacCmdPacket::SetCamEnabled(bool enabled) {
  pkt_[14] = enabled ? 1 : 0;
}

void UvcUacCmdPacket::SetCamPower(u8 power) {
  pkt_[15] = power;
}

void UvcUacCmdPacket::SetCamPowerFrequency(u8 freq) {
  pkt_[16] = freq;
}

void UvcUacCmdPacket::SetCamAutoExposureEnabled(bool auto_exp) {
  pkt_[17] = auto_exp ? 1 : 0;
}

void UvcUacCmdPacket::SetCameraLikeConsole(bool pulse, const byte regs[8], const CameraSettings* settings) {
  // Q1 de la consola desde el byte 14: 0000 0101 0002 00000000 00000000 70000000 40400300 2d000001 00000000 00000000
  static const byte kQ1[34] = {
      0x00, 0x00, 0x01, 0x01, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x70, 0x00, 0x00,
      0x00, 0x40, 0x40, 0x03, 0x00, 0x2d, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
  memcpy(pkt_.data() + 14, kQ1, sizeof(kQ1));
  if (!pulse) {
    pkt_[17] = 0;
    if (settings && settings->exposure >= 0) {
      pkt_[19] = 0;  // exposición manual
      pkt_[20] = static_cast<byte>(settings->exposure & 0xFF);
      pkt_[21] = static_cast<byte>((settings->exposure >> 8) & 0xFF);
      pkt_[22] = 0;
      pkt_[23] = 0;
    } else {
      pkt_[19] = 2;  // auto exposición
      memcpy(pkt_.data() + 20, regs, 4);
    }

    if (settings) {
      int16_t b = static_cast<int16_t>(settings->brightness);
      memcpy(pkt_.data() + 24, &b, 2);
      int16_t c = static_cast<int16_t>(settings->contrast);
      memcpy(pkt_.data() + 26, &c, 2);
    }

    if (settings && settings->gain >= 0) {
      pkt_[28] = static_cast<byte>(settings->gain & 0x7F);
    } else {
      pkt_[28] = 0x70;  // AGC auto
    }

    if (settings && settings->saturation >= 0) {
      pkt_[32] = static_cast<byte>(settings->saturation & 0xFF);
      pkt_[33] = static_cast<byte>(settings->saturation & 0xFF);
    } else {
      pkt_[32] = 0x40;
      pkt_[33] = 0x40;
    }

    if (settings && !settings->wb_auto) {
      pkt_[39] = 0;  // manual
      pkt_[40] = static_cast<byte>(settings->wb_blue & 0xFF);
      pkt_[41] = 0;
      pkt_[42] = static_cast<byte>(settings->wb_red & 0xFF);
      pkt_[43] = 0;
    } else {
      pkt_[39] = 1;  // auto
      memcpy(pkt_.data() + 40, regs + 4, 4);
    }
  }
}

void UvcUacCmdPacket::SetCameraOff() {
  pkt_[14] = pkt_[15] = pkt_[16] = pkt_[17] = 0;
}

TimeCmdPacket::TimeCmdPacket() {
  ResetPacket();
}

TimeCmdPacket::TimeCmdPacket(const std::vector<byte>& packet) {
  memcpy(pkt_.data(), packet.data(), packet.size());
}

TimeCmdPacket::~TimeCmdPacket() {
}

void TimeCmdPacket::ResetPacket() {
  memset(pkt_.data(), 0, pkt_.size());
}

u16 TimeCmdPacket::Days() const {
  return (pkt_[1] << 8) | pkt_[0];
}

u32 TimeCmdPacket::Seconds() const {
  return (pkt_[7] << 24) | (pkt_[6] << 16) | (pkt_[5] << 8) | pkt_[4];
}

void TimeCmdPacket::SetDays(u16 days) {
  pkt_[0] = days & 0xFF;
  pkt_[1] = days << 8;
}

void TimeCmdPacket::SetSeconds(u32 seconds) {
  pkt_[4] = seconds & 0xFF;
  pkt_[5] = seconds >> 8;
  pkt_[6] = seconds >> 16;
  pkt_[7] = seconds >> 24;
}

}  // namespace drc
