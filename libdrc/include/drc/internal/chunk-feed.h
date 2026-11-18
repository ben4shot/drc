// Frames H.264 precodificados para DRC_CHUNK_FEED=<fichero>: el vídeo de la consola (rpi4/chunkfeed.py)
// entra por el camino de los chunks precodificados de VideoStreamer, sin pasar por x264, pero con
// nuestro empaquetado, timestamps y reparto. Formato: "CFD1", u32 LE nº de frames; por frame u8 idr,
// u8 nº de chunks y por chunk u32 LE longitud + datos.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace drc {

struct FeedFrame {
  bool idr = false;
  std::vector<std::string> chunks;
};

class ChunkFeed {
 public:
  // false (y queda vacío) si la firma es mala, el fichero está truncado o no hay ningún frame.
  bool Load(const std::string& blob) {
    frames_.clear();
    next_ = 0;
    if (blob.size() < 8 || blob.compare(0, 4, "CFD1") != 0) return false;
    size_t pos = 4;
    uint32_t n = U32(blob, &pos);
    for (uint32_t i = 0; i < n; ++i) {
      if (pos + 2 > blob.size()) return Fail();
      FeedFrame f;
      f.idr = blob[pos] != 0;
      size_t nc = static_cast<unsigned char>(blob[pos + 1]);
      pos += 2;
      for (size_t c = 0; c < nc; ++c) {
        if (pos + 4 > blob.size()) return Fail();
        uint32_t len = U32(blob, &pos);
        if (len > blob.size() - pos) return Fail();
        f.chunks.push_back(blob.substr(pos, len));
        pos += len;
      }
      frames_.push_back(std::move(f));
    }
    return frames_.empty() ? Fail() : true;
  }

  size_t size() const { return frames_.size(); }

  // Vuelve al primer frame (un IDR): la forma de «pedir un IDR» a un flujo precodificado.
  void Rewind() { next_ = 0; }

  // ¿El siguiente frame es un IDR? Para dejar el hueco previo sin consumirlo.
  bool NextIsIdr() const { return !frames_.empty() && frames_[next_].idr; }

  // Índice del próximo frame que dará Next() (para el registro de resync).
  size_t Position() const { return next_; }

  // Salta al siguiente IDR del fichero (DRC_FEED_SEEK=idr); si no hay más, vuelve al primero.
  void SkipToNextIdr() {
    for (size_t i = next_; i < frames_.size(); ++i) {
      if (frames_[i].idr) { next_ = i; return; }
    }
    next_ = 0;
  }

  // Siguiente frame; al llegar al final vuelve al primero (que es un IDR).
  const FeedFrame& Next() {
    const FeedFrame& f = frames_[next_];
    next_ = (next_ + 1) % frames_.size();
    return f;
  }

 private:
  static uint32_t U32(const std::string& s, size_t* pos) {
    uint32_t v = 0;
    for (int i = 0; i < 4; ++i) v |= uint32_t(static_cast<unsigned char>(s[*pos + i])) << (8 * i);
    *pos += 4;
    return v;
  }
  bool Fail() { frames_.clear(); return false; }

  std::vector<FeedFrame> frames_;
  size_t next_ = 0;
};

// Graba frames en memoria en el mismo formato (DRC_CFD_DUMP): sin E/S en el tick; se escribe una vez lleno.
class ChunkFeedWriter {
 public:
  explicit ChunkFeedWriter(uint32_t max_frames) : max_(max_frames), count_(0) {}

  bool Full() const { return count_ >= max_; }

  void Add(bool idr, const std::vector<std::string>& chunks) {
    if (Full()) return;
    body_.push_back(idr ? 1 : 0);
    body_.push_back(static_cast<char>(chunks.size()));
    for (const auto& c : chunks) {
      PutU32(&body_, static_cast<uint32_t>(c.size()));
      body_ += c;
    }
    count_++;
  }

  std::string Blob() const {
    std::string out("CFD1");
    PutU32(&out, count_);
    return out + body_;
  }

 private:
  static void PutU32(std::string* s, uint32_t v) {
    for (int i = 0; i < 4; ++i) s->push_back(static_cast<char>((v >> (8 * i)) & 0xFF));
  }

  uint32_t max_, count_;
  std::string body_;
};

}  // namespace drc
