#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <vector>

#include <drc/streamer.h>
#include <drc/video-frame-rate.h>

#include "audio_reader.h"
#include "pad_status.h"
#include "ctl_fifo.h"
#include "pen_pressure.h"
#include "frame_reader.h"
#include "video_socket.h"
#include "uinput_dev.h"

namespace {
std::atomic<bool> g_running{true};

void sigint_handler(int) {
    g_running = false;
}
}  // namespace

int main(int argc, char* argv[]) {
    std::string bind_ip = "0.0.0.0";
    std::string dest_ip = "192.168.1.11";
    std::string mode = "separate";
    drc_stream::CanvasMapping canvas = {854, 480, 0, 0};
    bool canvas_given = false;
    bool shutdown_on_exit = false;
    int fps = 60;
    int audio_fd = -1;
    std::string video_socket;   // --video-socket: imagen directa de wiiu-wm (sin wf-recorder)

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--bind" && i + 1 < argc) {
            bind_ip = argv[++i];
        } else if (arg == "--dest" && i + 1 < argc) {
            dest_ip = argv[++i];
        } else if (arg == "--fps" && i + 1 < argc) {
            fps = std::atoi(argv[++i]);
        } else if (arg.rfind("--fps=", 0) == 0) {
            fps = std::atoi(arg.substr(6).c_str());
        } else if (arg == "--audio-fd" && i + 1 < argc) {
            audio_fd = std::atoi(argv[++i]);
        } else if (arg == "--video-socket" && i + 1 < argc) {
            video_socket = argv[++i];
        } else if (arg == "--mode" && i + 1 < argc) {
            mode = argv[++i];
        } else if (arg.rfind("--mode=", 0) == 0) {
            mode = arg.substr(7);
        } else if (arg == "--canvas-w" && i + 1 < argc) {
            canvas.total_w = std::atoi(argv[++i]);
            canvas_given = true;
        } else if (arg == "--canvas-h" && i + 1 < argc) {
            canvas.total_h = std::atoi(argv[++i]);
            canvas_given = true;
        } else if (arg == "--offset-x" && i + 1 < argc) {
            canvas.offset_x = std::atoi(argv[++i]);
            canvas_given = true;
        } else if (arg == "--offset-y" && i + 1 < argc) {
            canvas.offset_y = std::atoi(argv[++i]);
            canvas_given = true;
        } else if (arg == "--shutdown-pad") {
            shutdown_on_exit = true;
        } else if (arg == "--help" || arg == "-h") {
            std::cout << "Usage: drc_stream [--bind <ip>] [--dest <ip>] [--mode <separate|extended|mirror>] [--audio-fd <n>] [--video-socket <ruta>] [--shutdown-pad]\n";
            return 0;
        }
    }

    canvas = drc_stream::resolve_canvas(mode, canvas, canvas_given);

    std::signal(SIGINT, sigint_handler);
    std::signal(SIGTERM, sigint_handler);

    std::string vid_dst = dest_ip + ":50120";
    std::string aud_dst = dest_ip + ":50121";
    std::string cmd_dst = dest_ip + ":50123";
    std::string msg_bind = bind_ip + ":50010";
    std::string input_bind = bind_ip + ":50022";
    std::string cmd_bind = bind_ip + ":50023";

    std::cout << "[drc_stream] Starting streamer with bind=" << bind_ip
              << ", dest=" << dest_ip << "..." << std::endl;

    drc::Streamer streamer(vid_dst, aud_dst, cmd_dst, msg_bind, input_bind, cmd_bind);
    if (!streamer.Start()) {
        std::cerr << "[drc_stream] Failed to start drc::Streamer!" << std::endl;
        return 1;
    }

    if (fps <= 30) {
        streamer.SetFrameRate(drc::VideoFrameRate::k29_97Hz);
        std::cout << "[drc_stream] Configured for 30 FPS (29.97 Hz native mode)" << std::endl;
    } else {
        streamer.SetFrameRate(drc::VideoFrameRate::k59_94Hz);
        std::cout << "[drc_stream] Configured for 60 FPS (59.94 Hz native mode)" << std::endl;
    }

    // Initialize uinput virtual devices
    drc_stream::UinputGamepad uinput_gamepad;
    drc_stream::UinputTouchscreen uinput_touch;
    // DRC_TOUCH_MODE="pen": el táctil como lápiz con presión (tableta) en vez de pantalla táctil (start_drc.sh solo
    // lo deja pasar si la sesión admite tabletas).
    const char* touch_mode = getenv("DRC_TOUCH_MODE");
    const bool pen_mode = touch_mode && std::string(touch_mode) == "pen";
    drc_stream::UinputPen uinput_pen;
    drc_stream::PenPressure pen_pressure(drc_stream::LoadPenConfig());
    uinput_gamepad.open();
    if (pen_mode) {
        uinput_pen.open(canvas);
    } else {
        uinput_touch.open(canvas);
    }

    drc_stream::TouchCalib touch_calib = drc_stream::load_touch_calibration();

    // Asegurar que el audio del GamePad está desmuteado / encendido
    streamer.SendPadCommand(5, 0x0a, {0x01}, nullptr);

    drc_stream::LoadVolume();

    // Start 180 Hz input polling thread
    std::thread input_thread([&]() {
        const auto poll_interval = std::chrono::microseconds(5555);  // ~180 Hz
        drc::InputData inp;
        bool prev_tv = false;
        auto last_status = std::chrono::steady_clock::now() - std::chrono::seconds(2);
        while (g_running.load()) {
            streamer.PollInput(&inp);
            // Estado para la barra del escritorio (batería, carga, volumen), ~1 vez por segundo
            auto now = std::chrono::steady_clock::now();
            if (now - last_status >= std::chrono::seconds(1)) {
                last_status = now;
                drc_stream::PadStatus st;
                st.valid = inp.valid;
                st.battery = inp.battery_charge;
                st.power = inp.power_status;
                st.slider = inp.audio_volume;
                st.volume = drc_stream::Volume().load();
                drc_stream::WriteStatusFile(drc_stream::kStatusFile, drc_stream::FormatStatus(st));
            }
            if (inp.valid) {
                bool tv_now = (inp.buttons & drc_stream::kBtnTV) != 0;
                if (tv_now && !prev_tv) {
                    std::thread([]() {
                        system("/usr/local/bin/drc_toggle_keyboard.sh");
                    }).detach();
                }
                prev_tv = tv_now;

                uinput_gamepad.update(
                    inp.buttons,
                    inp.left_stick_x, inp.left_stick_y,
                    inp.right_stick_x, inp.right_stick_y
                );
                if (pen_mode) {
                    uinput_pen.update(
                        pen_pressure.Update(inp.ts_pressed, static_cast<int>(inp.ts_pressure),
                                            inp.ts_raw_x, inp.ts_raw_y),
                        inp.ts_x, inp.ts_y, touch_calib);
                } else {
                    uinput_touch.update(
                        inp.ts_pressed,
                        inp.ts_x, inp.ts_y,
                        touch_calib
                    );
                }
            }
            std::this_thread::sleep_for(poll_interval);
        }
    });

    // Canal de control: /tmp/drc_ctl (ctl_fifo.h).
    std::thread ctl_thread([&]() { drc_stream::CtlLoop(streamer, g_running); });

    // Audio s16 estéreo 48 kHz por un fd (arecord): si se acaba, solo termina este hilo.
    std::thread audio_thread;
    std::atomic<bool> audio_done{false};
    if (audio_fd >= 0) {
        audio_thread = std::thread([&]() {
            drc_stream::AudioReader audio(audio_fd);
            std::vector<int16_t> samples;
            while (g_running.load() && audio.Next(samples)) {
                drc_stream::ApplyVolume(samples, drc_stream::Volume().load());
                streamer.PushAudSamples(samples);
            }
            std::cout << "[drc_stream] Fin del audio." << std::endl;
            audio_done = true;
        });
        std::cout << "[drc_stream] Leyendo audio PCM del fd " << audio_fd << std::endl;
    }

    std::vector<uint8_t> frame_buf;
    uint64_t total_frames = 0;
    uint64_t frames_since_report = 0;
    uint64_t frames_changed = 0, last_frame_hash = 0;

    auto start_time = std::chrono::steady_clock::now();
    auto last_report_time = start_time;
    auto next_frame_time = start_time;
    const auto target_interval = std::chrono::nanoseconds(1000000000ULL / (fps <= 30 ? 30 : 60));

    struct stat stdin_st;
    bool is_live_pipe = false;
    int video_fd = STDIN_FILENO;
    if (!video_socket.empty()) {
        // Captura directa: el WM entrega el búfer que acaba de renderizar (y4m por socket unix)
        video_fd = drc_stream::ConnectVideoSocket(video_socket, 30000);
        if (video_fd < 0) {
            // Sin imagen no hay transmisión: se sigue por la salida normal (los hilos se cierran solos)
            std::cerr << "[drc_stream] No se pudo conectar con " << video_socket << std::endl;
            g_running = false;
        } else {
            std::cout << "[drc_stream] Imagen directa de wiiu-wm por " << video_socket << std::endl;
        }
        is_live_pipe = true;
    } else if (fstat(STDIN_FILENO, &stdin_st) == 0) {
        if (S_ISFIFO(stdin_st.st_mode) || S_ISSOCK(stdin_st.st_mode)) {
            is_live_pipe = true;
        }
    }

    std::cout << "[drc_stream] Reading 854x480 YUV420P frames from stdin"
              << (is_live_pipe ? " (live pipe, zero sleep)..." : "...") << std::endl;

    drc_stream::FrameReader reader(video_fd);
    uint64_t reported_resyncs = 0;
    while (g_running.load() && reader.Next(frame_buf)) {
        {   // diagnóstico: cuántos fotogramas empujados difieren del anterior (muestra de 1 de cada 61 bytes)
            uint64_t h = 1469598103934665603ULL;
            for (size_t i = 0; i < frame_buf.size(); i += 61) h = (h ^ frame_buf[i]) * 1099511628211ULL;
            if (h != last_frame_hash) { frames_changed++; last_frame_hash = h; }
        }
        streamer.PushNativeVidFrame(&frame_buf);
        total_frames++;
        frames_since_report++;

        auto now = std::chrono::steady_clock::now();
        auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_report_time).count();
        if (elapsed_ms >= 5000) {
            double fps = (frames_since_report * 1000.0) / elapsed_ms;
            std::cout << "[drc_stream] Pushed " << total_frames << " frames ("
                      << std::fixed << std::setprecision(1) << fps << " fps, cambian " << frames_changed << ", "
                      << (reader.is_y4m() ? "y4m" : "raw") << ", resyncs="
                      << reader.resyncs() << ")" << std::endl;
            if (reader.resyncs() != reported_resyncs) {
                std::cerr << "[drc_stream] ¡Desfase en el flujo de captura! Realineado ("
                          << reader.resyncs() - reported_resyncs << " veces)" << std::endl;
                reported_resyncs = reader.resyncs();
            }
            last_report_time = now;
            frames_since_report = 0;
            frames_changed = 0;
        }

        if (!is_live_pipe) {
            next_frame_time += target_interval;
            if (next_frame_time > now) {
                std::this_thread::sleep_until(next_frame_time);
            } else {
                next_frame_time = now;
            }
        }
    }

    if (frames_since_report > 0) {
        auto now = std::chrono::steady_clock::now();
        auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_report_time).count();
        if (elapsed_ms > 0) {
            double fps = (frames_since_report * 1000.0) / elapsed_ms;
            std::cout << "[drc_stream] Final: pushed " << total_frames << " frames ("
                      << std::fixed << std::setprecision(1) << fps << " fps)" << std::endl;
        }
    }

    if (shutdown_on_exit) {
        std::cout << "[drc_stream] Shutting down GamePad..." << std::endl;
        streamer.ShutdownPad();
    }

    g_running = false;
    if (ctl_thread.joinable()) {
        ctl_thread.join();
    }
    if (input_thread.joinable()) {
        input_thread.join();
    }
    uinput_gamepad.close();
    uinput_touch.close();
    uinput_pen.close();

    std::cout << "[drc_stream] Stopping streamer..." << std::endl;
    streamer.Stop();
    std::cout << "[drc_stream] Terminated cleanly." << std::endl;

    if (audio_thread.joinable()) {
        if (audio_done.load()) {
            audio_thread.join();
        } else {
            // Sigue bloqueado en read() (arecord vivo): el hilo usa `streamer`, que no puede
            // destruirse con él en marcha. Se sale sin ejecutar destructores.
            std::cout.flush();
            std::_Exit(0);
        }
    }
    return 0;
}
