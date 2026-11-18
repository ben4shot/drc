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

#include <drc/internal/cmd-protocol.h>
#include <drc/internal/events.h>
#include <atomic>
#include <mutex>
#include <vector>

namespace drc {

class UvcUacStateSynchronizer : public ThreadedEventMachine {
 public:
  // This class does not take ownership of the CmdClient
  // and it should not out live CmdClient.
  explicit UvcUacStateSynchronizer(CmdClient*);
  virtual ~UvcUacStateSynchronizer();

  bool Start();
  void Stop();

  // Se aplican en el siguiente envío (1 por segundo) y se mantienen frente a lo que responda el mando.
  void SetMicEnabled(bool on) { mic_wanted_ = on ? 1 : 0; }
  // Pedir la cámara repite el pulso de apertura de la consola (Q1) en el siguiente envío.
  void SetCamEnabled(bool on) { cam_wanted_ = on ? 1 : 0; cam_open_pulse_ = on; }
  // Ajustes de parámetros de cámara
  void SetCameraExposure(int exp) { std::lock_guard<std::mutex> l(cam_mutex_); cam_settings_.exposure = exp; }
  void SetCameraGain(int gain) { std::lock_guard<std::mutex> l(cam_mutex_); cam_settings_.gain = gain; }
  void SetCameraWb(bool auto_wb, int blue = 0x80, int red = 0x40) {
    std::lock_guard<std::mutex> l(cam_mutex_);
    cam_settings_.wb_auto = auto_wb;
    cam_settings_.wb_blue = blue;
    cam_settings_.wb_red = red;
  }
  void SetCameraBrightness(int val) { std::lock_guard<std::mutex> l(cam_mutex_); cam_settings_.brightness = val; }
  void SetCameraContrast(int val) { std::lock_guard<std::mutex> l(cam_mutex_); cam_settings_.contrast = val; }
  void SetCameraSaturation(int val) { std::lock_guard<std::mutex> l(cam_mutex_); cam_settings_.saturation = val; }
  void SetCameraBitrate(int val) { std::lock_guard<std::mutex> l(cam_mutex_); cam_settings_.bitrate = val; }
  CameraSettings GetCameraSettings() { std::lock_guard<std::mutex> l(cam_mutex_); return cam_settings_; }

  // Campos de cámara (bytes 14..47) como los manda la consola: Q1 (byte17=1) una vez y luego Q2
  // (byte17=0 con los registros del sensor que devolvió el mando); apagada: bytes 14..17 a 0.
  void FillCamFields(UvcUacCmdPacket* p);
  // Guarda los registros del sensor de la respuesta UVC/UAC (bytes 4..11) para copiarlos en Q2.
  void OnCamReply(const byte* d, size_t n);
  int MicWanted() const { return mic_wanted_.load(); }
  int CamWanted() const { return cam_wanted_.load(); }
  // Consulta UVC/UAC en crudo (48 B) que sustituye a la calculada; vacía = volver a la normal.
  void SetRaw(const std::vector<byte>& raw) { std::lock_guard<std::mutex> l(raw_mutex_); raw_ = raw; }

 protected:
  virtual void InitEventsAndRun();

 private:
  UvcUacCmdPacket state_;
  CmdClient* cmd_client_;
  std::atomic<int> mic_wanted_{-1};
  std::atomic<int> cam_wanted_{-1};
  std::atomic<bool> cam_open_pulse_{false};
  std::mutex cam_mutex_;
  byte cam_regs_[8] = {0};
  CameraSettings cam_settings_;
  std::mutex raw_mutex_;
  std::vector<byte> raw_;
};

}  // namespace drc
