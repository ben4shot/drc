#pragma once

#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <unistd.h>
#include <vector>

namespace drc_stream {

/**
 * Lector de muestras s16 little-endian estéreo (L,R intercaladas) desde un fd.
 * Next() bloquea hasta tener al menos un frame estéreo completo (4 bytes) y entrega
 * todos los que ya hay; los bytes sueltos se guardan para la siguiente lectura.
 * Devuelve false en EOF o error (un frame incompleto final se descarta).
 */
class AudioReader {
public:
    explicit AudioReader(int fd) : fd_(fd) {}

    bool Next(std::vector<int16_t>& out) {
        out.clear();
        while (pending_.size() < kFrameBytes) {
            uint8_t tmp[4096];
            ssize_t n = ::read(fd_, tmp, sizeof(tmp));
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) return false;
            pending_.insert(pending_.end(), tmp, tmp + n);
        }
        size_t usable = pending_.size() / kFrameBytes * kFrameBytes;
        out.resize(usable / sizeof(int16_t));
        std::memcpy(out.data(), pending_.data(), usable);  // little-endian, como la Pi
        pending_.erase(pending_.begin(), pending_.begin() + usable);
        return true;
    }

private:
    static constexpr size_t kFrameBytes = 2 * sizeof(int16_t);
    int fd_;
    std::vector<uint8_t> pending_;
};

}  // namespace drc_stream
