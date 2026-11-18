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

#include <array>
#include <cstring>
#include <drc/video-frame-rate.h>
#include <cstdlib>
#include <drc/types.h>
#include <vector>

namespace drc {

const size_t kVstrmHeaderSize = 16;

// Limit our payload size to 1400 bytes so that the full packet with vstrm header
// (16 bytes), UDP (8 bytes) and IPv4 (20 bytes) remains under standard 1500 MTU.
const size_t kMaxVstrmPayloadSize = 1694;  // como el libdrc original y la consola (~1700); la Pi fragmenta en IP

// DRC_VSTRM_PAYLOAD (B17): payload máximo por paquete vstrm. 1456 hace que el paquete
// (16 vstrm + 8 UDP + 20 IP) quepa en MTU 1500 sin fragmentación IP. Sin variable, vacía,
// no numérica o fuera de 256..kMaxVstrmPayloadSize = kMaxVstrmPayloadSize.
inline size_t VstrmPayloadFromEnv(const char* env) {
  if (!env || !env[0]) return kMaxVstrmPayloadSize;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 256 || v > static_cast<long>(kMaxVstrmPayloadSize)) {
    return kMaxVstrmPayloadSize;
  }
  return static_cast<size_t>(v);
}

class VstrmPacket {
 public:
  VstrmPacket();
  explicit VstrmPacket(const std::vector<byte>& packet);
  virtual ~VstrmPacket();

  u16 SeqId() const;
  u32 Timestamp() const;
  bool InitFlag() const;
  bool FrameBeginFlag() const;
  bool ChunkEndFlag() const;
  bool FrameEndFlag() const;
  bool IdrFlag() const;
  VideoFrameRate FrameRate() const;
  const byte* Payload() const;
  size_t PayloadSize() const;

  void SetSeqId(u16 seqid);
  void SetTimestamp(u32 ts);
  void SetInitFlag(bool flag);
  void SetFrameBeginFlag(bool flag);
  void SetChunkEndFlag(bool flag);
  void SetFrameEndFlag(bool flag);
  void SetIdrFlag(bool flag);
  void SetFrameRate(VideoFrameRate framerate);
  void SetPayload(const byte* payload, size_t size);

  const byte* GetBytes() const { return pkt_.data(); }
  size_t GetSize() const { return kVstrmHeaderSize + PayloadSize(); }

  // Reset the packet to use default values for everything.
  void ResetPacket();

  // Escribe los 8 bytes de la cabecera extendida tal cual (formato de la consola).
  void SetRawExtHeader(const byte* ext) { memcpy(pkt_.data() + 8, ext, 8); }

 private:
  bool GetExtOption(u8 opt, u8* val = NULL) const;
  void SetExtOption(u8 opt, u8* val = NULL);
  void ClearExtOption(u8 opt, bool has_val);

  std::array<byte, kVstrmHeaderSize + kMaxVstrmPayloadSize> pkt_;
};

}  // namespace drc
