#pragma once

#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <linux/input.h>
#include <linux/uinput.h>
#include <sstream>
#include <string>
#include <unistd.h>
#include <vector>

#include "uinput_map.h"

namespace drc_stream {

inline int open_uinput_fd() {
    const char* paths[] = {"/dev/uinput", "/dev/input/uinput", "/dev/misc/uinput"};
    for (const char* p : paths) {
        int fd = ::open(p, O_RDWR | O_NONBLOCK);
        if (fd >= 0) return fd;
    }
    return -1;
}

inline void emit_event(int fd, uint16_t type, uint16_t code, int32_t val) {
    struct input_event ie;
    std::memset(&ie, 0, sizeof(ie));
    ie.type = type;
    ie.code = code;
    ie.value = val;
    if (::write(fd, &ie, sizeof(ie)) < 0) {
        // ignore write error
    }
}

inline void emit_syn(int fd) {
    emit_event(fd, EV_SYN, SYN_REPORT, 0);
}

class UinputGamepad {
public:
    UinputGamepad() : fd_(-1) {}
    ~UinputGamepad() { close(); }

    bool open() {
        fd_ = open_uinput_fd();
        if (fd_ < 0) {
            std::cerr << "[uinput] Failed to open /dev/uinput for gamepad\n";
            return false;
        }

        // Enable Key events
        ioctl(fd_, UI_SET_EVBIT, EV_KEY);
        uint16_t buttons[] = {
            BTN_EAST, BTN_SOUTH, BTN_NORTH, BTN_WEST,
            BTN_TL, BTN_TR, BTN_TL2, BTN_TR2,
            BTN_SELECT, BTN_START, BTN_MODE,
            BTN_THUMBL, BTN_THUMBR, KEY_KEYBOARD,
            BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT
        };
        for (uint16_t b : buttons) {
            ioctl(fd_, UI_SET_KEYBIT, b);
        }

        // Enable ABS events
        // Standard Linux Gamepad (Linux Gamepad Specification / evdev / SDL / Steam):
        // ABS_X (0), ABS_Y (1)   -> Axis 0, 1: Left Stick [-32767, 32767]
        // ABS_Z (2)              -> Axis 2: Left Trigger ZL [0, 255]
        // ABS_RX (3), ABS_RY (4) -> Axis 3, 4: Right Stick [-32767, 32767]
        // ABS_RZ (5)             -> Axis 5: Right Trigger ZR [0, 255]
        // ABS_HAT0X, ABS_HAT0Y   -> Axis 6, 7: D-Pad Hat [-1, 1]
        ioctl(fd_, UI_SET_EVBIT, EV_ABS);
        uint16_t axes[] = {ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y};
        for (uint16_t a : axes) {
            ioctl(fd_, UI_SET_ABSBIT, a);
        }

        struct uinput_user_dev dev;
        std::memset(&dev, 0, sizeof(dev));
        std::strncpy(dev.name, "Wii U GamePad", UINPUT_MAX_NAME_SIZE - 1);
        dev.id.bustype = BUS_USB;
        dev.id.vendor = 0x057e;  // Nintendo
        dev.id.product = 0x0330;
        dev.id.version = 1;

        // Left and Right Joysticks [-32767, 32767]
        dev.absmin[ABS_X] = -32767;  dev.absmax[ABS_X] = 32767;
        dev.absmin[ABS_Y] = -32767;  dev.absmax[ABS_Y] = 32767;
        dev.absmin[ABS_RX] = -32767; dev.absmax[ABS_RX] = 32767;  // Right Stick X
        dev.absmin[ABS_RY] = -32767; dev.absmax[ABS_RY] = 32767;  // Right Stick Y

        // Triggers ZL / ZR [0, 255]
        dev.absmin[ABS_Z] = 0;       dev.absmax[ABS_Z] = 255;    // ZL (Left Trigger)
        dev.absmin[ABS_RZ] = 0;      dev.absmax[ABS_RZ] = 255;    // ZR (Right Trigger)

        // Hat / D-pad [-1, 1]
        dev.absmin[ABS_HAT0X] = -1; dev.absmax[ABS_HAT0X] = 1;
        dev.absmin[ABS_HAT0Y] = -1; dev.absmax[ABS_HAT0Y] = 1;

        if (::write(fd_, &dev, sizeof(dev)) < 0) {
            std::cerr << "[uinput] Failed to write uinput_user_dev for gamepad\n";
            close();
            return false;
        }

        if (ioctl(fd_, UI_DEV_CREATE) < 0) {
            std::cerr << "[uinput] UI_DEV_CREATE failed for gamepad\n";
            close();
            return false;
        }

        std::cout << "[uinput] Created 'Wii U GamePad' input device\n";
        return true;
    }

    void close() {
        if (fd_ >= 0) {
            ioctl(fd_, UI_DEV_DESTROY);
            ::close(fd_);
            fd_ = -1;
            std::cout << "[uinput] Destroyed 'Wii U GamePad' device\n";
        }
    }

    void update(uint32_t buttons, float lx, float ly, float rx, float ry) {
        if (fd_ < 0) return;

        bool changed = false;

        // 1. Buttons
        if (buttons != prev_buttons_) {
            uint32_t diff = buttons ^ prev_buttons_;
            // Check each button
            struct BtnMap { uint32_t mask; uint16_t code; };
            BtnMap bmap[] = {
                {kBtnA, BTN_EAST}, {kBtnB, BTN_SOUTH}, {kBtnX, BTN_NORTH}, {kBtnY, BTN_WEST},
                {kBtnL, BTN_TL}, {kBtnR, BTN_TR}, {kBtnZL, BTN_TL2}, {kBtnZR, BTN_TR2},
                {kBtnMinus, BTN_SELECT}, {kBtnPlus, BTN_START}, {kBtnHome, BTN_MODE},
                {kBtnL3, BTN_THUMBL}, {kBtnR3, BTN_THUMBR},
                {kBtnTV, KEY_KEYBOARD},
                {kBtnUp, BTN_DPAD_UP}, {kBtnDown, BTN_DPAD_DOWN},
                {kBtnLeft, BTN_DPAD_LEFT}, {kBtnRight, BTN_DPAD_RIGHT}
            };
            for (const auto& bm : bmap) {
                if (diff & bm.mask) {
                    emit_event(fd_, EV_KEY, bm.code, (buttons & bm.mask) ? 1 : 0);
                    changed = true;
                }
            }

            // Triggers as analog axes ABS_Z (ZL) and ABS_RZ (ZR) (0 or 255)
            if (diff & kBtnZL) {
                emit_event(fd_, EV_ABS, ABS_Z, (buttons & kBtnZL) ? 255 : 0);
                changed = true;
            }
            if (diff & kBtnZR) {
                emit_event(fd_, EV_ABS, ABS_RZ, (buttons & kBtnZR) ? 255 : 0);
                changed = true;
            }

            // D-Pad Hat
            int new_hat_x = 0;
            if (buttons & kBtnRight) new_hat_x += 1;
            if (buttons & kBtnLeft)  new_hat_x -= 1;
            if (new_hat_x != prev_hat_x_) {
                emit_event(fd_, EV_ABS, ABS_HAT0X, new_hat_x);
                prev_hat_x_ = new_hat_x;
                changed = true;
            }

            int new_hat_y = 0;
            if (buttons & kBtnDown) new_hat_y += 1;
            if (buttons & kBtnUp)   new_hat_y -= 1;
            if (new_hat_y != prev_hat_y_) {
                emit_event(fd_, EV_ABS, ABS_HAT0Y, new_hat_y);
                prev_hat_y_ = new_hat_y;
                changed = true;
            }

            prev_buttons_ = buttons;
        }

        // 2. Sticks
        int16_t cur_lx = map_stick(lx);
        int16_t cur_ly = map_stick_y(ly);
        int16_t cur_rx = map_stick(rx);
        int16_t cur_ry = map_stick_y(ry);

        if (cur_lx != prev_lx_) { emit_event(fd_, EV_ABS, ABS_X, cur_lx); prev_lx_ = cur_lx; changed = true; }
        if (cur_ly != prev_ly_) { emit_event(fd_, EV_ABS, ABS_Y, cur_ly); prev_ly_ = cur_ly; changed = true; }
        if (cur_rx != prev_rx_) { emit_event(fd_, EV_ABS, ABS_RX, cur_rx); prev_rx_ = cur_rx; changed = true; }
        if (cur_ry != prev_ry_) { emit_event(fd_, EV_ABS, ABS_RY, cur_ry); prev_ry_ = cur_ry; changed = true; }

        if (changed) {
            emit_syn(fd_);
        }
    }

private:
    int fd_;
    uint32_t prev_buttons_ = 0;
    int prev_hat_x_ = 0;
    int prev_hat_y_ = 0;
    int16_t prev_lx_ = 0;
    int16_t prev_ly_ = 0;
    int16_t prev_rx_ = 0;
    int16_t prev_ry_ = 0;
};

class UinputTouchscreen {
public:
    UinputTouchscreen() : fd_(-1) {}
    ~UinputTouchscreen() { close(); }

    bool open(const CanvasMapping& canvas = {}) {
        canvas_ = canvas;
        fd_ = open_uinput_fd();
        if (fd_ < 0) {
            std::cerr << "[uinput] Failed to open /dev/uinput for touchscreen\n";
            return false;
        }

        // Enable Key / Touch
        ioctl(fd_, UI_SET_EVBIT, EV_KEY);
        ioctl(fd_, UI_SET_KEYBIT, BTN_TOUCH);

        // Enable Direct Touchscreen property
        ioctl(fd_, UI_SET_PROPBIT, INPUT_PROP_DIRECT);

        // Enable ABS events
        ioctl(fd_, UI_SET_EVBIT, EV_ABS);
        ioctl(fd_, UI_SET_ABSBIT, ABS_X);
        ioctl(fd_, UI_SET_ABSBIT, ABS_Y);

        struct uinput_user_dev dev;
        std::memset(&dev, 0, sizeof(dev));
        std::strncpy(dev.name, "Wii U GamePad Touchscreen", UINPUT_MAX_NAME_SIZE - 1);
        dev.id.bustype = BUS_USB;
        dev.id.vendor = 0x057e;
        dev.id.product = 0x0331;
        dev.id.version = 1;

        int32_t max_x = (canvas_.total_w > 0) ? (canvas_.total_w - 1) : 853;
        int32_t max_y = (canvas_.total_h > 0) ? (canvas_.total_h - 1) : 479;

        dev.absmin[ABS_X] = 0; dev.absmax[ABS_X] = max_x;
        dev.absmin[ABS_Y] = 0; dev.absmax[ABS_Y] = max_y;

        if (::write(fd_, &dev, sizeof(dev)) < 0) {
            std::cerr << "[uinput] Failed to write uinput_user_dev for touchscreen\n";
            close();
            return false;
        }

        if (ioctl(fd_, UI_DEV_CREATE) < 0) {
            std::cerr << "[uinput] UI_DEV_CREATE failed for touchscreen\n";
            close();
            return false;
        }

        std::cout << "[uinput] Created 'Wii U GamePad Touchscreen' device (bounds: "
                  << max_x + 1 << "x" << max_y + 1 << ")\n";
        return true;
    }

    void close() {
        if (fd_ >= 0) {
            ioctl(fd_, UI_DEV_DESTROY);
            ::close(fd_);
            fd_ = -1;
            std::cout << "[uinput] Destroyed 'Wii U GamePad Touchscreen' device\n";
        }
    }

    void update(bool pressed, float ts_x, float ts_y, const TouchCalib& calib) {
        if (fd_ < 0) return;

        if (pressed) {
            TouchCoord coord = map_touch_extended(ts_x, ts_y, calib, canvas_);
            bool pos_changed = (coord.x != prev_x_ || coord.y != prev_y_);
            bool press_changed = !prev_pressed_;

            if (press_changed || pos_changed) {
                emit_event(fd_, EV_ABS, ABS_X, coord.x);
                emit_event(fd_, EV_ABS, ABS_Y, coord.y);
                if (press_changed) {
                    emit_event(fd_, EV_KEY, BTN_TOUCH, 1);
                }
                emit_syn(fd_);
                prev_x_ = coord.x;
                prev_y_ = coord.y;
                prev_pressed_ = true;
            }
        } else {
            if (prev_pressed_) {
                emit_event(fd_, EV_KEY, BTN_TOUCH, 0);
                emit_syn(fd_);
                prev_pressed_ = false;
            }
        }
    }

private:
    int fd_;
    CanvasMapping canvas_{};
    bool prev_pressed_ = false;
    int32_t prev_x_ = -1;
    int32_t prev_y_ = -1;
};

inline TouchCalib load_touch_calibration(const std::string& path = "/etc/drc_stream/touch.conf") {
    TouchCalib calib;
    std::ifstream f(path);
    if (f.is_open()) {
        float x_min, x_max, y_min, y_max;
        if (f >> x_min >> x_max >> y_min >> y_max) {
            calib.x_min = x_min;
            calib.x_max = x_max;
            calib.y_min = y_min;
            calib.y_max = y_max;
            std::cout << "[uinput] Loaded touch calibration from " << path
                      << ": " << x_min << " " << x_max << " " << y_min << " " << y_max << "\n";
            return calib;
        }
    }
    // Default calibration
    return calib;
}

}  // namespace drc_stream
