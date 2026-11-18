#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <linux/input.h>
#include <string>
#include <vector>

namespace drc_stream {

// Wii U GamePad Button Masks (from libdrc/input.h)
constexpr uint32_t kBtnSync   = 0x1;
constexpr uint32_t kBtnHome   = 0x2;
constexpr uint32_t kBtnMinus  = 0x4;
constexpr uint32_t kBtnPlus   = 0x8;
constexpr uint32_t kBtnR      = 0x10;
constexpr uint32_t kBtnL      = 0x20;
constexpr uint32_t kBtnZR     = 0x40;
constexpr uint32_t kBtnZL     = 0x80;
constexpr uint32_t kBtnDown   = 0x100;
constexpr uint32_t kBtnUp     = 0x200;
constexpr uint32_t kBtnRight  = 0x400;
constexpr uint32_t kBtnLeft   = 0x800;
constexpr uint32_t kBtnY      = 0x1000;
constexpr uint32_t kBtnX      = 0x2000;
constexpr uint32_t kBtnB      = 0x4000;
constexpr uint32_t kBtnA      = 0x8000;
constexpr uint32_t kBtnTV     = 0x200000;
constexpr uint32_t kBtnR3     = 0x400000;
constexpr uint32_t kBtnL3     = 0x800000;

struct InputEventDef {
    uint16_t type;  // EV_KEY or EV_ABS
    uint16_t code;  // BTN_*, ABS_*
    int32_t value;  // 0 or 1 for key, or axis coordinate
};

struct TouchCalib {
    float x_min = 0.0f;
    float x_max = 1.0f;
    float y_min = 0.0f;
    float y_max = 1.0f;
};

struct TouchCoord {
    int32_t x;
    int32_t y;
};

struct CanvasMapping {
    int32_t total_w = 854;
    int32_t total_h = 480;
    int32_t offset_x = 0;
    int32_t offset_y = 0;
};

// Maps float in [-1.0, 1.0] to int16 range [-32767, 32767] with clamping
inline int16_t map_stick(float v) {
    if (v > 1.0f) v = 1.0f;
    if (v < -1.0f) v = -1.0f;
    return static_cast<int16_t>(std::round(v * 32767.0f));
}

// In standard Linux gamepads, stick UP is negative and DOWN is positive.
// libdrc provides stick Y as +1.0 for UP and -1.0 for DOWN, so we invert Y.
inline int16_t map_stick_y(float v) {
    return map_stick(-v);
}

// Maps buttons bitmask to input events (EV_KEY and EV_ABS for Hat)
inline std::vector<InputEventDef> map_buttons(uint32_t buttons) {
    std::vector<InputEventDef> evs;

    // Standard buttons (EV_KEY)
    if (buttons & kBtnA)     evs.push_back({EV_KEY, BTN_EAST,   1});
    if (buttons & kBtnB)     evs.push_back({EV_KEY, BTN_SOUTH,  1});
    if (buttons & kBtnX)     evs.push_back({EV_KEY, BTN_NORTH,  1});
    if (buttons & kBtnY)     evs.push_back({EV_KEY, BTN_WEST,   1});

    if (buttons & kBtnL)     evs.push_back({EV_KEY, BTN_TL,     1});
    if (buttons & kBtnR)     evs.push_back({EV_KEY, BTN_TR,     1});
    if (buttons & kBtnZL)    evs.push_back({EV_KEY, BTN_TL2,    1});
    if (buttons & kBtnZR)    evs.push_back({EV_KEY, BTN_TR2,    1});

    if (buttons & kBtnMinus) evs.push_back({EV_KEY, BTN_SELECT, 1});
    if (buttons & kBtnPlus)  evs.push_back({EV_KEY, BTN_START,  1});
    if (buttons & kBtnHome)  evs.push_back({EV_KEY, BTN_MODE,   1});

    if (buttons & kBtnL3)    evs.push_back({EV_KEY, BTN_THUMBL, 1});
    if (buttons & kBtnR3)    evs.push_back({EV_KEY, BTN_THUMBR, 1});

    // D-Pad / Cruceta (EV_ABS HAT0X / HAT0Y)
    int hat_x = 0;
    if (buttons & kBtnRight) hat_x += 1;
    if (buttons & kBtnLeft)  hat_x -= 1;
    if (hat_x != 0) {
        evs.push_back({EV_ABS, ABS_HAT0X, hat_x});
    }

    int hat_y = 0;
    if (buttons & kBtnDown) hat_y += 1;
    if (buttons & kBtnUp)   hat_y -= 1;
    if (hat_y != 0) {
        evs.push_back({EV_ABS, ABS_HAT0Y, hat_y});
    }

    return evs;
}

// Maps touchscreen normalized coordinates [0.0, 1.0] to [0..853, 0..479] with calibration and clamping
inline TouchCoord map_touch(float ts_x, float ts_y, const TouchCalib& c) {
    float norm_x = (ts_x - c.x_min) / (c.x_max - c.x_min);
    float norm_y = (ts_y - c.y_min) / (c.y_max - c.y_min);

    norm_x = std::clamp(norm_x, 0.0f, 1.0f);
    norm_y = std::clamp(norm_y, 0.0f, 1.0f);

    int32_t px_x = static_cast<int32_t>(std::round(norm_x * 853.0f));
    int32_t px_y = static_cast<int32_t>(std::round(norm_y * 479.0f));

    return {std::clamp(px_x, 0, 853), std::clamp(px_y, 0, 479)};
}

// Lienzo del táctil/lápiz. En extended, si nadie lo da (--canvas-*/--offset-*), el de la Pi: 1920x1080 + el mando a
// la derecha (2774x1080, offset 1920), donde el compositor reparte el dispositivo por todo el escritorio. Si el
// compositor asigna el dispositivo al monitor del mando (Hyprland: hypr_map_touch), start_drc.sh da 854x480.
inline CanvasMapping resolve_canvas(const std::string& mode, const CanvasMapping& canvas, bool explicit_canvas) {
    if (mode == "extended" && !explicit_canvas) return {2774, 1080, 1920, 0};
    return canvas;
}

// Maps touchscreen coordinates to extended canvas sub-rectangle [offset_x..offset_x+853, offset_y..offset_y+479]
inline TouchCoord map_touch_extended(float ts_x, float ts_y, const TouchCalib& calib, const CanvasMapping& canvas) {
    TouchCoord local = map_touch(ts_x, ts_y, calib);
    int32_t min_x = canvas.offset_x;
    int32_t max_x = std::min(canvas.offset_x + 853, canvas.total_w - 1);
    int32_t min_y = canvas.offset_y;
    int32_t max_y = std::min(canvas.offset_y + 479, canvas.total_h - 1);

    int32_t x = std::clamp(local.x + canvas.offset_x, min_x, max_x);
    int32_t y = std::clamp(local.y + canvas.offset_y, min_y, max_y);
    return {x, y};
}

}  // namespace drc_stream
