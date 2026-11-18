// Funciones del mando accesibles por el canal de control de drc_stream. Comandos genéricos: wiiu_dump/disasm/CAPACIDADES_MANDO.md.
// Lista blanca: un comando que no esté aquí no se puede enviar. Quedan fuera a propósito la actualización de firmware
// (servicio 0), las claves y el certificado (1, 2), la escritura de la EEPROM del UIC (5,0x0c), el apagado y los modos
// de arranque (4,0x01; 4,0x1a) y el modo de fábrica (4,0x19).

#pragma once

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace drc {

struct PadCommand {
  const char* name;
  int service;
  int method;
  bool write;      // true: «set nombre valor», carga de 1 byte
  int min_value;
  int max_value;
  int read_arg;    // <0: «get» sin carga; >=0: un byte de carga fijo
};

const int kPadStateCommand = -1;

static const PadCommand kPadCommands[] = {
    {"fw_version", 0, 0x00, false, 0, 0, -1},
    {"phy_stats", 3, 0x03, false, 0, 0, 0},
    {"pkt_stats", 3, 0x04, false, 0, 0, 0},
    {"wmm_stats", 3, 0x05, false, 0, 0, 0},
    {"rssi", 3, 0x06, false, 0, 0, -1},
    {"sysinfo", 4, 0x04, false, 0, 0, -1},
    {"state", 4, 0x05, false, 0, 0, -1},
    {"camera_mode", 4, 0x09, false, 0, 0, -1},
    {"mic_mode", 4, 0x0b, false, 0, 0, -1},
    {"battery", 5, 0x00, false, 0, 0, -1},
    {"audio_mute", 5, 0x0b, false, 0, 0, -1},
    {"lcd_cabc", 5, 0x0f, false, 0, 0, -1},
    {"brightness", 5, 0x15, false, 0, 0, -1},
    {"lcd_table", 5, 0x1a, false, 0, 0, -1},
    {"sensorbar", 5, 0x01, true, 0, 1, -1},
    {"lcd_on", 5, 0x08, true, 0, 1, -1},
    {"audio_on", 5, 0x0a, true, 0, 1, -1},
    {"backlight", 5, 0x14, true, 1, 5, -1},
    {"mic", kPadStateCommand, 0, true, 0, 1, -1},      // estado UVC/UAC, no comando genérico
    {"camera", kPadStateCommand, 1, true, 0, 1, -1},
    {nullptr, 0, 0, false, 0, 0, -1},
};

const int kMaxVibrateMs = 60000;

struct CtlRequest {
  bool ok = false;
  const PadCommand* cmd = nullptr;
  std::vector<unsigned char> payload;
  bool vibrate = false;
  int vibrate_ms = 0;
};

inline const PadCommand* FindPadCommand(const char* name, bool write) {
  for (const PadCommand* c = kPadCommands; c->name; c++) {
    if (c->write == write && !strcmp(c->name, name)) return c;
  }
  return nullptr;
}

inline bool ParseCtlInt(const char* text, int* out) {
  char* end = nullptr;
  long v = strtol(text, &end, 10);
  if (end == text || *end != '\0' || v < -1000000 || v > 1000000) return false;
  *out = static_cast<int>(v);
  return true;
}

// «get nombre», «set nombre valor» o «vibrate ms». Con ok == false no se envía nada.
inline CtlRequest ParseCtl(const char* line) {
  CtlRequest r;
  char verb[16] = {0}, name[32] = {0}, value[16] = {0}, extra[2] = {0};
  const int n = sscanf(line, "%15s %31s %15s %1s", verb, name, value, extra);
  int v = 0;
  if (n == 2 && !strcmp(verb, "vibrate")) {
    if (ParseCtlInt(name, &v) && v >= 0 && v <= kMaxVibrateMs) {
      r.ok = true;
      r.vibrate = true;
      r.vibrate_ms = v;
    }
  } else if (n == 2 && !strcmp(verb, "get")) {
    r.cmd = FindPadCommand(name, false);
    if (r.cmd) {
      r.ok = true;
      if (r.cmd->read_arg >= 0) r.payload.push_back(static_cast<unsigned char>(r.cmd->read_arg));
    }
  } else if (n == 3 && !strcmp(verb, "set")) {
    const PadCommand* c = FindPadCommand(name, true);
    if (c && ParseCtlInt(value, &v) && v >= c->min_value && v <= c->max_value) {
      // (5,8) no es booleano en el firmware (LVC 0x36e82): 0x00, 0x01 y 0x02 son tres modos de apagado y solo
      // 0xff vuelve a encender el LCD. «set lcd_on 1» manda 0xff.
      if (c->service == 5 && c->method == 0x08 && v == 1) v = 0xff;
      r.cmd = c;
      r.ok = true;
      r.payload.push_back(static_cast<unsigned char>(v));
    }
  }
  return r;
}

// Ventana de vibración en ms de un reloj monótono.
class VibrateWindow {
 public:
  VibrateWindow() : until_ms_(0) {}
  void Start(uint64_t now_ms, int duration_ms) { until_ms_ = now_ms + static_cast<uint64_t>(duration_ms); }
  bool Active(uint64_t now_ms) const { return now_ms < until_ms_.load(); }

 private:
  std::atomic<uint64_t> until_ms_;
};

}  // namespace drc
