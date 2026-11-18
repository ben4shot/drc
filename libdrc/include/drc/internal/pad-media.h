// Lo que el mando envía a la consola además del HID: micrófono (astrm hacia 50021) y cámara (hacia 50020). Se activan con
// el paquete de estado UVC/UAC. Formato del micrófono: ASTRM_Send_tsk (LVC 0x35246).

#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>

#include <drc/internal/pad-control.h>
#include <drc/internal/cmd-packet.h>

namespace drc {

struct PadAudio {
  unsigned seq = 0;
  unsigned size = 0;
  uint32_t ts = 0;
  const unsigned char* payload = nullptr;
};

inline bool ParsePadAudio(const unsigned char* data, size_t n, PadAudio* out) {
  if (n < 8 || (data[0] & 0xF0) != 0xD0) return false;
  const unsigned size = (static_cast<unsigned>(data[2]) << 8) | data[3];
  if (size + 8 != n) return false;
  out->seq = (static_cast<unsigned>(data[0] & 3) << 8) | data[1];
  out->size = size;
  out->ts = (static_cast<uint32_t>(data[4]) << 24) | (static_cast<uint32_t>(data[5]) << 16) |
            (static_cast<uint32_t>(data[6]) << 8) | data[7];
  out->payload = data + 8;
  return true;
}

struct PadCameraPacket {
  unsigned seq = 0;
  unsigned size = 0;
  uint32_t ts = 0;
  uint8_t flags = 0;
  const unsigned char* payload = nullptr;
};

inline bool ParsePadCamera(const unsigned char* data, size_t n, PadCameraPacket* out) {
  if (n < 8 || (data[0] & 0xF0) != 0xF0) return false;
  out->seq = (static_cast<unsigned>(data[0] & 3) << 8) | data[1];
  out->flags = data[2];
  out->ts = (static_cast<uint32_t>(data[4]) << 24) | (static_cast<uint32_t>(data[5]) << 16) |
            (static_cast<uint32_t>(data[6]) << 8) | data[7];
  out->size = static_cast<unsigned>(n - 8);
  out->payload = data + 8;
  return true;
}

inline GenericCmdPacket MakeCameraConfigPacket(uint8_t display_mode = 0, uint8_t fps_mode = 2, uint16_t bitrate = 0x0fbc) {
  GenericCmdPacket pkt;
  pkt.SetFlags(GenericCmdPacket::kQueryFlag);
  pkt.SetServiceId(4);
  pkt.SetMethodId(0x08);
  byte payload[4] = {
    display_mode,
    fps_mode,
    static_cast<byte>((bitrate >> 8) & 0xFF),
    static_cast<byte>(bitrate & 0xFF)
  };
  pkt.SetPayload(payload, 4);
  return pkt;
}

// Guarda lo recibido en un fichero con tope de tamaño (al llegar al tope vuelve a empezar).
class CappedDump {
 public:
  CappedDump(const std::string& path, size_t cap) : path_(path), cap_(cap), written_(0), packets_(0) {}
  ~CappedDump() { if (f_) fclose(f_); }
  void Write(const unsigned char* data, size_t n) {
    if (!f_ || written_ + n > cap_) {
      if (f_) fclose(f_);
      f_ = fopen(path_.c_str(), "wb");
      written_ = 0;
    }
    if (f_) {
      fwrite(data, 1, n, f_);
      fflush(f_);
      written_ += n;
    }
    packets_++;
  }
  unsigned long packets() const { return packets_; }

 private:
  std::string path_;
  size_t cap_;
  size_t written_;
  unsigned long packets_;
  FILE* f_ = nullptr;
};

}  // namespace drc
