// Bit «init» de la cabecera vstrm, como la consola real. En su captura de juego (37 518 frames) los
// frames P nunca lo llevan y 5 de sus 6 IDR lo llevan en TODOS sus paquetes; el único IDR sin él es el
// primero del flujo. Antes libdrc lo forzaba siempre a cero («la consola no lo usa»).
// DRC_IDR_INIT=0 devuelve el comportamiento anterior.

#pragma once

namespace drc {

inline bool IdrInitEnabled(const char* env) { return !(env && env[0] == '0'); }

inline bool VstrmInitFlag(bool idr, bool first_frame_of_stream, bool enabled) {
  return enabled && idr && !first_frame_of_stream;
}

}  // namespace drc
