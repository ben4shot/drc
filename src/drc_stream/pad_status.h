// Estado del GamePad para la barra del escritorio y volumen del audio que se le envía.
//
//   /tmp/drc_status (clave=valor, se reescribe ~1 vez por segundo):
//     battery=<0..6 crudo del mando>  charging=0|1  ac=0|1  slider=<deslizador 0..255>
//     volume=<ganancia 0..100>  valid=0|1
//   echo "volume 60" > /tmp/drc_ctl   (también «volume +10» / «volume -10»)

#pragma once

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace drc_stream {

constexpr const char* kStatusFile = "/tmp/drc_status";

struct PadStatus {
    bool valid = false;
    int battery = 0;      // InputData::battery_charge
    int power = 0;        // InputData::power_status (0x01 corriente, 0x40 cargando)
    int slider = 0;       // InputData::audio_volume (deslizador físico del mando)
    int volume = 100;     // ganancia de software del audio (0..100)
};

constexpr const char* kVolumeFile = "/var/lib/drc/volume";

// Ganancia actual (la comparten el hilo de control y el de audio)
inline std::atomic<int>& Volume() {
    static std::atomic<int> v{100};
    return v;
}

inline void LoadVolume() {
    if (FILE* f = fopen(kVolumeFile, "r")) {
        int v = 100;
        if (fscanf(f, "%d", &v) == 1 && v >= 0 && v <= 100) Volume() = v;
        fclose(f);
    }
}

inline void SaveVolume() {
    if (FILE* f = fopen(kVolumeFile, "w")) {
        fprintf(f, "%d\n", Volume().load());
        fclose(f);
    }
}

// Devuelve el nuevo volumen, o -1 si la línea no es una orden de volumen válida.
inline int ParseVolumeCommand(const std::string& line, int current) {
    if (line.rfind("volume ", 0) != 0) return -1;
    std::string arg = line.substr(7);
    if (arg.empty()) return -1;
    bool rel = arg[0] == '+' || arg[0] == '-';
    char* end = nullptr;
    long v = std::strtol(arg.c_str(), &end, 10);
    if (end == arg.c_str() || *end != '\0') return -1;
    long out = rel ? current + v : v;
    if (out < 0) out = 0;
    if (out > 100) out = 100;
    return static_cast<int>(out);
}

// Ganancia con curva cuadrática (el oído es logarítmico: así 50 suena a «la mitad»).
inline void ApplyVolume(std::vector<int16_t>& samples, int volume) {
    if (volume >= 100) return;
    if (volume <= 0) {
        for (auto& s : samples) s = 0;
        return;
    }
    const int32_t gain = volume * volume;   // sobre 10000
    for (auto& s : samples) s = static_cast<int16_t>(static_cast<int32_t>(s) * gain / 10000);
}

inline std::string FormatStatus(const PadStatus& st) {
    char buf[256];
    snprintf(buf, sizeof(buf),
             "valid=%d\nbattery=%d\ncharging=%d\nac=%d\nslider=%d\nvolume=%d\n",
             st.valid ? 1 : 0, st.battery, (st.power & 0x40) ? 1 : 0, (st.power & 0x01) ? 1 : 0,
             st.slider, st.volume);
    return buf;
}

// Escritura atómica (tmp + rename): quien lee nunca ve el fichero a medias.
inline bool WriteStatusFile(const std::string& path, const std::string& text) {
    std::string tmp = path + ".tmp";
    FILE* f = fopen(tmp.c_str(), "w");
    if (!f) return false;
    bool ok = fwrite(text.data(), 1, text.size(), f) == text.size();
    ok = (fclose(f) == 0) && ok;
    if (!ok || rename(tmp.c_str(), path.c_str()) != 0) {
        remove(tmp.c_str());
        return false;
    }
    return true;
}

}  // namespace drc_stream
