#pragma once

#include <cerrno>
#include <chrono>
#include <cstring>
#include <string>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>

namespace drc_stream {

/**
 * Conecta al socket unix donde wiiu-wm entrega su imagen renderizada (flujo y4m 854x480).
 * Reintenta hasta timeout_ms (el WM puede tardar en arrancar). Devuelve el fd o -1.
 */
inline int ConnectVideoSocket(const std::string& path, int timeout_ms) {
    auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    for (;;) {
        int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd < 0) return -1;
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
        if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0) {
            int sz = 1 << 20;
            ::setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &sz, sizeof(sz));
            return fd;
        }
        ::close(fd);
        if (std::chrono::steady_clock::now() >= deadline) return -1;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}

}  // namespace drc_stream
