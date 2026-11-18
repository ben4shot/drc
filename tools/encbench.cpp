// Banco del codificador de libdrc: lee I420 854x480 de un fichero y mide tiempo por frame.
// ENCBENCH_DUP=1   cada frame se codifica dos veces seguidas (contenido a 30 fps con ticks a 60, como hace el streamer).
// ENCBENCH_BOX=N   imagen fija (el frame 0) y solo cambia un recuadro de NxN con el contenido del clip (escritorio casi quieto).
// ENCBENCH_OUT=f   escribe el flujo en Annex-B con las cabeceras del GamePad, para decodificarlo con ffmpeg.
// ENCBENCH_FLUSH=1 con ENCBENCH_DUP: el duplicado no se codifica; se vacía x264 (Flush) o cuenta como relleno (DRC_ENC_FLUSH).
//                  Necesita compilar con -DENCBENCH_HAS_FLUSH (libdrc con H264Encoder::Flush).
// ENCBENCH_SRC=f   escribe la entrada tal como se codificó (I420), para comparar con lo decodificado.
#include <cstring>
#include <sys/resource.h>
#include <drc/internal/h264-encoder.h>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <algorithm>
int main(int argc, char** argv) {
  const size_t fsz = 854 * 480 * 3 / 2;
  FILE* f = fopen(argv[1], "rb");
  if (!f) return 1;
  std::vector<std::vector<drc::byte>> frames;
  for (;;) { std::vector<drc::byte> b(fsz); if (fread(b.data(), 1, fsz, f) != fsz) break; frames.push_back(std::move(b)); }
  int reps = argc > 2 ? atoi(argv[2]) : 1;
  const int box = getenv("ENCBENCH_BOX") ? atoi(getenv("ENCBENCH_BOX")) : 0;
  if (box > 0) {
    const int W = 854, bx = 400, by = 200;
    for (size_t i = 1; i < frames.size(); i++) {
      std::vector<drc::byte> b = frames[0];
      for (int y = 0; y < box; y++) memcpy(&b[(by + y) * W + bx], &frames[i][(by + y) * W + bx], box);
      for (int p = 0; p < 2; p++) {
        const size_t off = W * 480 + p * (W / 2) * 240;
        for (int y = 0; y < box / 2; y++)
          memcpy(&b[off + (by / 2 + y) * (W / 2) + bx / 2], &frames[i][off + (by / 2 + y) * (W / 2) + bx / 2], box / 2);
      }
      frames[i] = std::move(b);
    }
  }
  if (getenv("ENCBENCH_DUP")) {
    std::vector<std::vector<drc::byte>> d;
    for (size_t i = 0; i < frames.size() / 2; i++) { d.push_back(frames[i]); d.push_back(frames[i]); }
    frames = std::move(d);
  }
  if (const char* sp = getenv("ENCBENCH_SRC")) {
    FILE* sf = fopen(sp, "wb");
    for (size_t i = 0; i < frames.size(); i++)
      if (!(getenv("ENCBENCH_FLUSH") && getenv("ENCBENCH_DUP") && (i & 1))) fwrite(frames[i].data(), 1, fsz, sf);
    fclose(sf);
  }
  FILE* out = getenv("ENCBENCH_OUT") ? fopen(getenv("ENCBENCH_OUT"), "wb") : nullptr;
  unsigned frame_num = 0;
  if (out) {
    static const unsigned char hdr[] = {0, 0, 0, 1, 0x67, 0x64, 0x00, 0x20, 0xac, 0x2b, 0x40, 0x6c, 0x1e, 0xf3, 0x68,
                                        0, 0, 0, 1, 0x68, 0xee, 0x06, 0x0c, 0xe8};
    fwrite(hdr, 1, sizeof(hdr), out);
  }
  drc::H264Encoder enc;
  std::vector<double> ms; size_t bytes = 0; std::vector<size_t> fb;
  const clock_t cpu0 = clock();
  auto t0 = std::chrono::steady_clock::now();
  for (int r = 0; r < reps; r++)
    for (size_t i = 0; i < frames.size(); i++) {
      auto a = std::chrono::steady_clock::now();
#ifdef ENCBENCH_HAS_FLUSH
      static const bool flush = getenv("ENCBENCH_FLUSH") && getenv("ENCBENCH_DUP");
      // ENCBENCH_TICKS=N: como el streamer con contenido lento: tras cada frame, N ticks sin imagen (vaciar si x264 retiene algo).
      static const int idle_ticks = getenv("ENCBENCH_TICKS") ? atoi(getenv("ENCBENCH_TICKS")) : 0;
      if (idle_ticks > 0) {
        const auto* first = &enc.Encode(frames[i], r == 0 && i == 0);
        for (int k = 0; k <= idle_ticks; k++) {
          const auto& cc = k == 0 ? *first : enc.Flush();
          if (k > 0 && enc.Delayed() == 0 && !enc.HasOutput()) break;
          if (out && enc.HasOutput()) {
            std::vector<unsigned char> nal = {0, 0, 0, 1};
            if (enc.OutputIsIdr()) { frame_num = 0; nal.insert(nal.end(), {0x25, 0xb8, 0x04, 0xff}); }
            else { frame_num = (frame_num + 1) & 0xFF; nal.insert(nal.end(), {0x21, (unsigned char)(0xe0 | (frame_num >> 3)), (unsigned char)(0x03 | (frame_num << 5)), 0xff}); }
            for (auto& c : cc) for (size_t q = 0; q < std::get<1>(c); q++) {
              const unsigned char v = std::get<0>(c)[q]; const size_t n = nal.size();
              if (v <= 3 && nal[n - 1] == 0 && nal[n - 2] == 0) nal.push_back(3);
              nal.push_back(v);
            }
            fwrite(nal.data(), 1, nal.size(), out);
            size_t fs2 = 0; for (auto& c : cc) fs2 += std::get<1>(c); bytes += fs2; fb.push_back(fs2);
          }
        }
        ms.push_back(std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - a).count());
        continue;
      }
      const auto& ch = (flush && (i & 1)) ? enc.Flush() : enc.Encode(frames[i], r == 0 && i == 0);
#else
      const auto& ch = enc.Encode(frames[i], r == 0 && i == 0);
#endif
      auto b = std::chrono::steady_clock::now();
      size_t fs = 0; for (auto& c : ch) fs += std::get<1>(c); bytes += fs; if (!(r == 0 && i < 4)) fb.push_back(fs);
      if (out && enc.HasOutput()) {
        std::vector<unsigned char> nal = {0, 0, 0, 1};
        if (enc.OutputIsIdr()) {
          frame_num = 0;
          nal.insert(nal.end(), {0x25, 0xb8, 0x04, 0xff});
        } else {
          frame_num = (frame_num + 1) & 0xFF;
          nal.insert(nal.end(), {0x21, (unsigned char)(0xe0 | (frame_num >> 3)), (unsigned char)(0x03 | (frame_num << 5)), 0xff});
        }
        for (auto& c : ch)
          for (size_t k = 0; k < std::get<1>(c); k++) {
            const unsigned char v = std::get<0>(c)[k];
            const size_t n = nal.size();
            if (v <= 3 && nal[n - 1] == 0 && nal[n - 2] == 0) nal.push_back(3);
            nal.push_back(v);
          }
        fwrite(nal.data(), 1, nal.size(), out);
      }
      ms.push_back(std::chrono::duration<double, std::milli>(b - a).count());
    }
#ifdef ENCBENCH_HAS_FLUSH
  // ENCBENCH_TAIL=N: al final la imagen se queda quieta: N recodificaciones del último frame y luego se vacía x264.
  if (out && getenv("ENCBENCH_TAIL")) {
    const int tail = atoi(getenv("ENCBENCH_TAIL"));
    for (int k = 0; k < tail + 4; k++) {
      const auto& cc = k < tail ? enc.Encode(frames.back(), false) : enc.Flush();
      if (!enc.HasOutput()) continue;
      std::vector<unsigned char> nal = {0, 0, 0, 1};
      frame_num = (frame_num + 1) & 0xFF;
      nal.insert(nal.end(), {0x21, (unsigned char)(0xe0 | (frame_num >> 3)), (unsigned char)(0x03 | (frame_num << 5)), 0xff});
      size_t fs2 = 0;
      for (auto& c : cc) { fs2 += std::get<1>(c); for (size_t q = 0; q < std::get<1>(c); q++) {
        const unsigned char v = std::get<0>(c)[q]; const size_t n = nal.size();
        if (v <= 3 && nal[n - 1] == 0 && nal[n - 2] == 0) nal.push_back(3);
        nal.push_back(v); } }
      fwrite(nal.data(), 1, nal.size(), out);
      fprintf(stderr, "cola %d: %zu B\n", k, fs2);
    }
  }
#endif
  double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  std::sort(ms.begin(), ms.end());
  size_t n = ms.size(); double sum = 0; for (double v : ms) sum += v;
  std::sort(fb.begin(), fb.end()); size_t big = 0; for (size_t v : fb) big += v > 26000;
  printf("fmax=%zu f>26k=%zu fp99=%zu ", fb.back(), big, fb[fb.size() * 99 / 100]);
  printf("frames=%zu wall=%.2fs fps=%.1f avg=%.2fms p50=%.2f p95=%.2f max=%.2f kbps@60=%.0f cpu_s=%.2f\n", n, wall, n / wall,
         sum / n, ms[n / 2], ms[n * 95 / 100], ms[n - 1], bytes * 8.0 / n * 59.94 / 1000, (double)(clock() - cpu0) / CLOCKS_PER_SEC);
  if (out) fclose(out);
}
