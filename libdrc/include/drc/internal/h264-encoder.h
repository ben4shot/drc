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
#include <cstdio>
#include <drc/types.h>
#include <tuple>
#include <vector>

extern "C" {
#include <x264.h>
}

namespace drc {

// A chunk is defined as a pointer to encoded data and the size of that data.
// There are 5 chunks in a frame.
const int kH264ChunksPerFrame = 5;
typedef std::tuple<const byte*, size_t> H264Chunk;
typedef std::array<H264Chunk, kH264ChunksPerFrame> H264ChunkArray;

class H264Encoder {
 public:
  // threads: hilos de frame de x264; 0 = DRC_ENC_THREADS (por defecto 1). Con más de 1 hay retraso: Encode entrega un
  // frame anterior, o ninguno al principio (HasOutput).
  explicit H264Encoder(int threads = 0);
  virtual ~H264Encoder();

  // The returned chunk array is only valid until the next call to Encode.
  const H264ChunkArray& Encode(const std::vector<byte>& frame, bool idr);

  // Pide a x264 el siguiente frame retenido (hilos > 1) sin darle entrada. Sin nada retenido: HasOutput() == false.
  const H264ChunkArray& Flush();
  // Frames entregados a x264 que aún no han salido.
  int Delayed() const;

  bool HasOutput() const { return has_output_; }
  bool OutputIsIdr() const { return output_idr_; }

 private:
  // Trozos de un frame en curso: x264 avisa desde sus hilos, y hasta que entrega el frame los bytes pueden no estar completos.
  struct FrameSlot {
    H264Encoder* encoder = nullptr;
    H264ChunkArray chunks;
    int count = 0;
  };
  static const int kFrameSlots = 8;

  void ProcessNalUnit(FrameSlot* slot, x264_nal_t* nal);
  static void ProcessNalUnitTrampoline(x264_t* h, x264_nal_t* nal, void* arg);

  const H264ChunkArray& TakeOutput(x264_picture_t* output);

  void CreateEncoder();
  void DestroyEncoder();

  x264_t* encoder_;
  int threads_;
  FrameSlot slots_[kFrameSlots];
  unsigned next_slot_ = 0;
  H264ChunkArray empty_;
  bool has_output_ = false;
  bool output_idr_ = false;
  long long next_pts_ = 0;
  bool mb_info_ = false;           // DRC_MB_INFO=1 (mb-constant.h)
  std::vector<byte> prev_frame_;   // último frame entregado a x264, para comparar por macrobloque
  FILE* dump_file_;
};

}  // namespace drc
