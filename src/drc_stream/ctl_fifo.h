// Canal de control de drc_stream: una orden por línea en la FIFO /tmp/drc_ctl (ver drc/internal/pad-control.h).
//   echo "get battery" > /tmp/drc_ctl ; echo "set sensorbar 1" > /tmp/drc_ctl ; echo "vibrate 300" > /tmp/drc_ctl
// Las respuestas se añaden a /tmp/drc_ctl.out como «nombre ok <hex>» o «nombre fallo».

#pragma once

#include <drc/internal/pad-control.h>
#include <drc/streamer.h>
#include "pad_status.h"
#include <fcntl.h>
#include <poll.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <cstdio>
#include <string>
#include <vector>

namespace drc_stream {

constexpr const char* kCtlFifo = "/tmp/drc_ctl";
constexpr const char* kCtlOut = "/tmp/drc_ctl.out";

inline void CtlWriteResult(const std::string& text) {
    if (FILE* f = fopen(kCtlOut, "a")) {
        fprintf(f, "%s\n", text.c_str());
        fclose(f);
    }
}

inline void CtlHandleLine(drc::Streamer& streamer, const std::string& line) {
    if (line == "resync") {
        streamer.ResyncStreamer();
        CtlWriteResult("resync ok");
        return;
    }

    if (line.rfind("volume ", 0) == 0) {   // ganancia del audio hacia el mando (pad_status.h)
        int v = ParseVolumeCommand(line, Volume().load());
        if (v < 0) {
            CtlWriteResult("rechazado: " + line);
        } else {
            Volume() = v;
            SaveVolume();
            CtlWriteResult("volume ok " + std::to_string(v));
        }
        return;
    }

    if (line.rfind("uvc ", 0) == 0) {  // "uvc off" o "uvc <hex>..." (consulta UVC/UAC en crudo, 48 B)
        std::vector<drc::byte> raw;
        std::istringstream iss(line.substr(4));
        std::string tok;
        while (iss >> tok) {
            if (tok == "off") { raw.clear(); break; }
            try { raw.push_back(static_cast<drc::byte>(std::stoul(tok, nullptr, 16))); } catch (...) {}
        }
        streamer.SetUvcRaw(raw);
        CtlWriteResult(raw.empty() ? "uvc normal" : "uvc crudo " + std::to_string(raw.size()) + " B");
        return;
    }

    if (line.rfind("cam ", 0) == 0) {
        std::istringstream iss(line.substr(4));
        std::string subcmd;
        if (iss >> subcmd) {
            if (subcmd == "on" || subcmd == "1") {
                streamer.SetCameraEnabled(true);
                CtlWriteResult("cam ok");
                return;
            } else if (subcmd == "off" || subcmd == "0") {
                streamer.SetCameraEnabled(false);
                CtlWriteResult("cam ok");
                return;
            } else if (subcmd == "exposure") {
                std::string arg;
                if (iss >> arg) {
                    if (arg == "auto") {
                        streamer.SetCameraExposure(-1);
                        CtlWriteResult("cam exposure ok");
                        return;
                    }
                    try {
                        int val = std::stoi(arg);
                        if (val >= 0 && val <= 65535) {
                            streamer.SetCameraExposure(val);
                            CtlWriteResult("cam exposure ok");
                            return;
                        }
                    } catch (...) {}
                }
            } else if (subcmd == "gain") {
                std::string arg;
                if (iss >> arg) {
                    if (arg == "auto") {
                        streamer.SetCameraGain(-1);
                        CtlWriteResult("cam gain ok");
                        return;
                    }
                    try {
                        int val = std::stoi(arg);
                        if (val >= 0 && val <= 111) {
                            streamer.SetCameraGain(val);
                            CtlWriteResult("cam gain ok");
                            return;
                        }
                    } catch (...) {}
                }
            } else if (subcmd == "wb") {
                std::string arg;
                if (iss >> arg) {
                    if (arg == "auto") {
                        streamer.SetCameraWb(true);
                        CtlWriteResult("cam wb ok");
                        return;
                    }
                    std::string arg2;
                    if (iss >> arg2) {
                        try {
                            int b = std::stoi(arg);
                            int r = std::stoi(arg2);
                            if (b >= 0 && b <= 255 && r >= 0 && r <= 255) {
                                streamer.SetCameraWb(false, b, r);
                                CtlWriteResult("cam wb ok");
                                return;
                            }
                        } catch (...) {}
                    }
                }
            } else if (subcmd == "brightness") {
                int val;
                if (iss >> val && val >= -100 && val <= 100) {
                    streamer.SetCameraBrightness(val);
                    CtlWriteResult("cam brightness ok");
                    return;
                }
            } else if (subcmd == "contrast") {
                int val;
                if (iss >> val && val >= 0 && val <= 100) {
                    streamer.SetCameraContrast(val);
                    CtlWriteResult("cam contrast ok");
                    return;
                }
            } else if (subcmd == "saturation") {
                int val;
                if (iss >> val && val >= 0 && val <= 128) {
                    streamer.SetCameraSaturation(val);
                    CtlWriteResult("cam saturation ok");
                    return;
                }
            } else if (subcmd == "bitrate") {
                int val;
                if (iss >> val && val >= 100 && val <= 65535) {
                    streamer.SetCameraBitrate(val);
                    CtlWriteResult("cam bitrate ok");
                    return;
                }
            }
        }
        CtlWriteResult("cam fallo: parametro invalido");
        return;
    }

    if (line.rfind("raw ", 0) == 0) {
        std::istringstream iss(line.substr(4));
        int service = 0, method = 0;
        if (iss >> service >> method) {
            std::vector<drc::byte> payload;
            std::string hex_str;
            while (iss >> hex_str) {
                try {
                    unsigned long val = std::stoul(hex_str, nullptr, 16);
                    payload.push_back(static_cast<drc::byte>(val));
                } catch (...) {}
            }
            streamer.SendPadCommand(service, method, payload,
                                    [](bool success, const std::vector<drc::byte>& reply) {
                std::string text = "raw " + std::string(success ? "ok" : "fallo");
                char hex[4];
                for (drc::byte b : reply) {
                    snprintf(hex, sizeof(hex), " %02x", b);
                    text += hex;
                }
                CtlWriteResult(text);
            });
            return;
        }
    }

    const drc::CtlRequest req = drc::ParseCtl(line.c_str());
    if (!req.ok) {
        CtlWriteResult("rechazado: " + line);
        return;
    }
    if (req.vibrate) {
        streamer.Vibrate(req.vibrate_ms);
        CtlWriteResult("vibrate ok");
        return;
    }
    const std::string name = req.cmd->name;
    if (req.cmd->service == drc::kPadStateCommand) {
        const bool on = req.payload[0] != 0;
        if (req.cmd->method == 0) streamer.SetMicEnabled(on); else streamer.SetCameraEnabled(on);
        CtlWriteResult(name + " ok");
        return;
    }
    std::vector<drc::byte> payload(req.payload.begin(), req.payload.end());
    streamer.SendPadCommand(req.cmd->service, req.cmd->method, payload,
                            [name](bool success, const std::vector<drc::byte>& reply) {
        std::string text = name + (success ? " ok" : " fallo");
        char hex[4];
        for (drc::byte b : reply) {
            snprintf(hex, sizeof(hex), " %02x", b);
            text += hex;
        }
        CtlWriteResult(text);
    });
}

inline void CtlLoop(drc::Streamer& streamer, const std::atomic<bool>& running) {
    unlink(kCtlFifo);
    if (mkfifo(kCtlFifo, 0666) != 0) return;
    chmod(kCtlFifo, 0666);
    const int fd = open(kCtlFifo, O_RDWR | O_NONBLOCK);  // O_RDWR: no hay EOF cuando se va el escritor
    if (fd < 0) return;
    std::string buffer;
    while (running.load()) {
        struct pollfd pfd = {fd, POLLIN, 0};
        if (poll(&pfd, 1, 200) <= 0) continue;
        char chunk[256];
        const ssize_t n = read(fd, chunk, sizeof(chunk));
        if (n <= 0) continue;
        buffer.append(chunk, static_cast<size_t>(n));
        size_t pos;
        while ((pos = buffer.find('\n')) != std::string::npos) {
            CtlHandleLine(streamer, buffer.substr(0, pos));
            buffer.erase(0, pos + 1);
        }
        if (buffer.size() > 4096) buffer.clear();
    }
    close(fd);
    unlink(kCtlFifo);
}

}  // namespace drc_stream
