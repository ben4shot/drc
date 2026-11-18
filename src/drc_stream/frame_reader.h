#pragma once

#include <algorithm>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <unistd.h>
#include <vector>

namespace drc_stream {

constexpr size_t kFrameWidth = 854;
constexpr size_t kFrameHeight = 480;
// YUV420P: 854 * 480 (Y) + 854 * 480 / 4 (U) + 854 * 480 / 4 (V) = 854 * 480 * 1.5 = 614880 bytes
constexpr size_t kFrameBytes = kFrameWidth * kFrameHeight * 3 / 2;

/**
 * Reads exactly kFrameBytes (614880 bytes) from fd into buf.
 * Handles partial reads and signals (EINTR).
 * Returns true if a full frame was read, or false on EOF / unrecoverable read error.
 */
inline bool read_frame(int fd, std::vector<uint8_t>& buf) {
    buf.resize(kFrameBytes);
    size_t total_read = 0;
    while (total_read < kFrameBytes) {
        ssize_t n = ::read(fd, buf.data() + total_read, kFrameBytes - total_read);
        if (n == 0) {
            // EOF reached before reading the full frame
            return false;
        }
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return false;
        }
        total_read += static_cast<size_t>(n);
    }
    return true;
}

/**
 * Lector de frames 854x480 YUV420P desde un fd. Detecta el formato por la firma:
 * - "YUV4MPEG2": y4m. Valida la cabecera (W854 H480, C420*) y cada "FRAME...\n";
 *   si encuentra basura antes de un FRAME se realinea y lo cuenta en resyncs().
 *   Con rawvideo un desfase de unos bytes desplaza la imagen para siempre.
 * - otra cosa: rawvideo de kFrameBytes por frame (modo mirror, tests con ffmpeg).
 */
class FrameReader {
public:
    explicit FrameReader(int fd) : fd_(fd) {}

    bool Next(std::vector<uint8_t>& out) {
        if (!started_) {
            started_ = true;
            if (!fill(9)) return false;
            // wf-recorder escribe texto por stdout ("selected region ...\n") antes
            // del y4m: se descarta lo que preceda a la firma en las primeras líneas.
            fill(kMaxPrefix + 9);  // puede quedarse corto (frame raw pequeño/EOF)
            size_t avail = std::min(buf_.size() - pos_, kMaxPrefix + 9);
            const uint8_t* b = buf_.data() + pos_;
            const char sig[] = "YUV4MPEG2";
            const uint8_t* hit = std::search(b, b + avail, sig, sig + 9);
            if (hit != b + avail && (hit == b || hit[-1] == '\n')) {
                pos_ += static_cast<size_t>(hit - b);
                std::string header;
                if (!read_line(header) || !header_ok(header)) return false;
                y4m_ = true;
            }
        }
        if (y4m_ && !sync_to_frame()) return false;
        if (!fill(kFrameBytes)) return false;
        out.assign(buf_.begin() + pos_, buf_.begin() + pos_ + kFrameBytes);
        pos_ += kFrameBytes;
        return true;
    }

    bool is_y4m() const { return y4m_; }
    uint64_t resyncs() const { return resyncs_; }

private:
    static constexpr size_t kMaxPrefix = 4096;

    // Garantiza al menos n bytes disponibles desde pos_.
    bool fill(size_t n) {
        if (buf_.size() - pos_ >= n) return true;
        buf_.erase(buf_.begin(), buf_.begin() + pos_);
        pos_ = 0;
        while (buf_.size() < n) {
            size_t old = buf_.size();
            size_t want = std::max(n - old, size_t(65536));
            buf_.resize(old + want);
            ssize_t r = ::read(fd_, buf_.data() + old, want);
            if (r < 0 && errno == EINTR) {
                buf_.resize(old);
                continue;
            }
            if (r <= 0) {
                buf_.resize(old);
                return false;
            }
            buf_.resize(old + static_cast<size_t>(r));
        }
        return true;
    }

    bool read_line(std::string& line) {
        line.clear();
        for (;;) {
            if (!fill(1)) return false;
            char c = static_cast<char>(buf_[pos_++]);
            if (c == '\n') return true;
            line.push_back(c);
            if (line.size() > 1024) return false;
        }
    }

    static bool header_ok(const std::string& h) {
        bool w = false, hh = false, csp = true;  // sin campo C, y4m asume 4:2:0
        size_t i = 0;
        while (i < h.size()) {
            size_t j = h.find(' ', i);
            if (j == std::string::npos) j = h.size();
            std::string tok = h.substr(i, j - i);
            if (tok == "W" + std::to_string(kFrameWidth)) w = true;
            else if (tok == "H" + std::to_string(kFrameHeight)) hh = true;
            else if (!tok.empty() && tok[0] == 'C') csp = tok.compare(0, 4, "C420") == 0;
            i = j + 1;
        }
        return w && hh && csp;
    }

    // Consume "FRAME[ params]\n"; si no está justo aquí, busca el siguiente.
    bool sync_to_frame() {
        bool skipped = false;
        for (;;) {
            if (!fill(5)) return false;
            if (std::memcmp(buf_.data() + pos_, "FRAME", 5) == 0) break;
            pos_++;
            skipped = true;
        }
        if (skipped) resyncs_++;
        std::string line;
        return read_line(line);
    }

    int fd_;
    std::vector<uint8_t> buf_;
    size_t pos_ = 0;
    bool started_ = false;
    bool y4m_ = false;
    uint64_t resyncs_ = 0;
};

}  // namespace drc_stream
