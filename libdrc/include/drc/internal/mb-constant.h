// DRC_MB_INFO=1: marca los macrobloques idénticos al frame anterior para que x264 los resuelva como P_Skip sin
// analizarlos (x264_picture_t.prop.mb_info + X264_MBINFO_CONSTANT). Medido en la Pi 4: un frame sin cambios cuesta
// ~13 ms de CPU con 3 hilos, casi lo mismo que uno con movimiento.

#pragma once

#include <cstdint>
#include <cstring>
#include <vector>

namespace drc {

// Solo con P_Skip permitido: el atajo de x264 emite P_Skip sin mirar b_allow_skip (DRC_ALLOW_PSKIP).
inline bool MbInfoEnabled(const char* env, const char* allow_pskip_env) {
  return env && env[0] == '1' && env[1] == '\0' && allow_pskip_env && allow_pskip_env[0] == '1';
}

inline int MbCount(int w, int h) { return ((w + 15) / 16) * ((h + 15) / 16); }

// Compara el frame I420 `cur` con `*prev` por macrobloque (luma 16x16 y las dos cromas 8x8; el borde derecho/inferior
// puede ser parcial). info[i] = 1 si el macrobloque i es idéntico, 0 si no; los distintos se copian a `*prev`, que queda
// igual a `cur`. Con `*prev` vacío (primer frame) nada es constante. Devuelve cuántos son constantes.
inline int MarkConstantMbs(const uint8_t* cur, std::vector<uint8_t>* prev, int w, int h, uint8_t* info) {
  const int mbw = (w + 15) / 16, mbh = (h + 15) / 16;
  const size_t total = static_cast<size_t>(w) * h * 3 / 2;
  if (prev->size() != total) {
    prev->assign(cur, cur + total);
    memset(info, 0, static_cast<size_t>(mbw) * mbh);
    return 0;
  }
  uint8_t* old = prev->data();
  const int cw = w / 2, ch = h / 2;
  const size_t u_off = static_cast<size_t>(w) * h, v_off = u_off + static_cast<size_t>(cw) * ch;
  int constant = 0;
  for (int my = 0; my < mbh; my++) {
    const int y0 = my * 16, y1 = y0 + 16 < h ? y0 + 16 : h;
    const int cy0 = my * 8, cy1 = cy0 + 8 < ch ? cy0 + 8 : ch;
    for (int mx = 0; mx < mbw; mx++) {
      const int x0 = mx * 16, lw = x0 + 16 < w ? 16 : w - x0;
      const int cx0 = mx * 8, clw = cx0 + 8 < cw ? 8 : cw - cx0;
      bool same = true;
      for (int y = y0; y < y1 && same; y++) {
        same = memcmp(cur + static_cast<size_t>(y) * w + x0, old + static_cast<size_t>(y) * w + x0, lw) == 0;
      }
      for (int y = cy0; y < cy1 && same; y++) {
        const size_t o = static_cast<size_t>(y) * cw + cx0;
        same = memcmp(cur + u_off + o, old + u_off + o, clw) == 0 && memcmp(cur + v_off + o, old + v_off + o, clw) == 0;
      }
      info[my * mbw + mx] = same ? 1 : 0;
      constant += same ? 1 : 0;
    }
  }
  // Poner al día la copia: con todo distinto (movimiento) una sola copia del frame, mucho más barata que 1620 trozos.
  if (constant == 0) {
    memcpy(old, cur, total);
    return 0;
  }
  for (int my = 0; my < mbh; my++) {
    const int y0 = my * 16, y1 = y0 + 16 < h ? y0 + 16 : h;
    const int cy0 = my * 8, cy1 = cy0 + 8 < ch ? cy0 + 8 : ch;
    for (int mx = 0; mx < mbw; mx++) {
      if (info[my * mbw + mx]) continue;
      const int x0 = mx * 16, lw = x0 + 16 < w ? 16 : w - x0;
      const int cx0 = mx * 8, clw = cx0 + 8 < cw ? 8 : cw - cx0;
      for (int y = y0; y < y1; y++) {
        memcpy(old + static_cast<size_t>(y) * w + x0, cur + static_cast<size_t>(y) * w + x0, lw);
      }
      for (int y = cy0; y < cy1; y++) {
        const size_t o = static_cast<size_t>(y) * cw + cx0;
        memcpy(old + u_off + o, cur + u_off + o, clw);
        memcpy(old + v_off + o, cur + v_off + o, clw);
      }
    }
  }
  return constant;
}

}  // namespace drc
