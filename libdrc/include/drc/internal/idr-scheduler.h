// Decide, tick a tick, si el VideoStreamer debe codificar un IDR, un P-frame,
// recodificar el último frame como IDR o no hacer nada.
//
// El decodificador del GamePad descarta P-frames tras perder macrobloques y
// pide resync por UDP; sin IDR periódico o de recuperación puede quedarse
// congelado. La captura (wf-recorder) solo entrega frames cuando hay daño en
// pantalla, así que el enfriamiento se mide en ticks del temporizador (~16.6 ms)
// y ante resync con pantalla estática recodifica el último frame como IDR en
// cuanto expira el cooldown (~100 ms).

#pragma once

#include <cstdlib>

namespace drc {

const int kIdrIntervalFrames = 30;    // ~0.5 s a 59.94 fps.
const int kMinIdrIntervalTicks = 6;   // ~100 ms a 59.94 fps: previene ráfagas de IDRs consecutivos

// Cadencia por defecto del VideoStreamer: sin IDR periódico. Medido con el GamePad
// (TSF vivo): el hueco previo a cada IDR provocaba un resync (since_idr_tick=0) y
// costaba 4 frames/s; sin IDR periódico, resync=0 y 60 fps sostenidos.
const int kDefaultIdrIntervalFrames = 0;

// DRC_IDR_INTERVAL: frames entre IDR periódicos (0 = solo IDR inicial y de resync).
// NULL, vacío, negativo o no numérico = kDefaultIdrIntervalFrames.
inline int IdrIntervalFromEnv(const char* env) {
  if (!env || !env[0]) return kDefaultIdrIntervalFrames;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0) return kDefaultIdrIntervalFrames;
  return static_cast<int>(v);
}

// DRC_IDR_MIN_TICKS: ticks mínimos entre IDR (cooldown). NULL, vacío, <1 o no numérico =
// kMinIdrIntervalTicks. Un valor largo evita que un IDR rechazado por el GamePad dispare otro.
inline int IdrMinTicksFromEnv(const char* env) {
  if (!env || !env[0]) return kMinIdrIntervalTicks;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 1) return kMinIdrIntervalTicks;
  return static_cast<int>(v);
}

// DRC_IDR_RESYNC_IGNORE_TICKS: un resync que llega hasta N ticks después de que el IDR salió se cuenta pero no pide
// otro IDR (el aviso es del frame anterior al IDR, aún en vuelo). NULL, vacío, negativo o no numérico = 0 (apagado).
inline int IdrResyncIgnoreTicksFromEnv(const char* env) {
  if (!env || !env[0]) return 0;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0) return 0;
  return static_cast<int>(v);
}

// true si un resync a `ticks_desde_envio_idr` ticks de que el IDR salió debe ignorarse (ventana 0..ignorar).
inline bool ResyncInFlight(int ticks_desde_envio_idr, int ignorar) {
  return ticks_desde_envio_idr <= ignorar && ignorar > 0;
}

// DRC_IDR_GAP_AFTER: ticks sin vídeo tras un IDR, de 1 a 4. START ocurre en ts + 31 ms y un P anterior reinicia el
// decodificador (causa 3). Cualquier otro valor = 1, como la consola.
inline int IdrGapAfterFromEnv(const char* env) {
  if (!env || !env[0]) return 1;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 1 || v > 4) return 1;
  return static_cast<int>(v);
}

// DRC_FILLER=0 desactiva el relleno. Medido: los mismos frames de x264 a 60 fps exactos no dan ráfagas, y en vivo un tick
// sin frame sí. El codificador de la consola nunca se salta un tick salvo en los huecos del IDR.
inline bool FillerEnabled(const char* env) { return !(env && env[0] == '0'); }

inline bool ShouldSendFiller(bool no_packets, bool gap_tick, bool idr_pending, bool have_filler) {
  return no_packets && !gap_tick && !idr_pending && have_filler;
}

// Huecos del IDR al enviar, para cuando x264 entrega con retraso (DRC_ENC_THREADS > 1). Next() devuelve false si este tick
// es un hueco y true si se puede enviar lo que haya.
class IdrSendGate {
 public:
  explicit IdrSendGate(int gap_after) : after_(gap_after), hold_(0), pre_done_(false) {}

  bool Next(bool have_frame, bool frame_is_idr, bool stream_started) {
    if (hold_ > 0) {
      hold_--;
      return false;
    }
    if (have_frame && frame_is_idr) {
      if (stream_started && !pre_done_) {
        pre_done_ = true;
        return false;
      }
      pre_done_ = false;
      hold_ = after_;
    }
    return true;
  }

 private:
  int after_, hold_;
  bool pre_done_;
};

class IdrScheduler {
 public:
  enum class Action { kSkip, kEncodeP, kEncodeIdr, kReencodeLastIdr };

  explicit IdrScheduler(int interval = kIdrIntervalFrames,
                        int min_interval = kMinIdrIntervalTicks)
      : interval_(interval),
        min_interval_(min_interval),
        ticks_since_idr_(0),
        inited_(false) {}

  Action Decide(bool has_new_frame, bool has_last_frame,
                bool resync_requested) {
    if (!inited_) {
      if (has_new_frame || has_last_frame) {
        inited_ = true;
        ticks_since_idr_ = 0;
        return Action::kEncodeIdr;
      }
      return Action::kSkip;
    }

    if (!has_new_frame && !has_last_frame) {
      return Action::kSkip;
    }

    ticks_since_idr_++;

    bool cooldown_expired = (ticks_since_idr_ >= min_interval_);
    bool interval_expired = (interval_ > 0 && ticks_since_idr_ >= interval_);

    if (resync_requested && cooldown_expired) {
      ticks_since_idr_ = 0;
      return has_new_frame ? Action::kEncodeIdr : Action::kReencodeLastIdr;
    }

    if (interval_expired) {
      ticks_since_idr_ = 0;
      return has_new_frame ? Action::kEncodeIdr : Action::kReencodeLastIdr;
    }

    return Action::kEncodeP;
  }

  int TicksSinceIdr() const { return ticks_since_idr_; }

 private:
  int interval_;
  int min_interval_;
  int ticks_since_idr_;
  bool inited_;
};

// DRC_IDLE_FILLER=1: un tick sin frame nuevo (pantalla quieta: el escritorio solo entrega imagen cuando cambia) no
// vuelve a codificar el último frame en x264 (3 ms y todos sus hilos, 60 veces por segundo): sale el relleno P_Skip, que
// repite la referencia sin tocarla. Desactivado por defecto. Solo con "1" exacto.
inline bool IdleFillerEnabled(const char* env) { return env && env[0] == '1' && env[1] == '\0'; }

// Ticks seguidos sin frame nuevo que se siguen codificando antes de pasar al relleno. x264 con varios hilos
// (DRC_ENC_THREADS > 1) entrega con retraso: necesita seguir recibiendo entradas para soltar los últimos frames reales
// (sin esto no salía ni el IDR y el GamePad no recibía vídeo). DRC_IDLE_FLUSH_TICKS lo cambia (>= 1).
const int kDefaultIdleFlushTicks = 8;
inline int IdleFlushTicksFromEnv(const char* env) {
  if (!env || !env[0]) return kDefaultIdleFlushTicks;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 1) return kDefaultIdleFlushTicks;
  return static_cast<int>(v);
}

// kEncodeP sin frame nuevo durante al menos flush_ticks ticks y con relleno disponible -> kSkip. Los IDR (inicial,
// resync, periódico) no se tocan.
inline IdrScheduler::Action ApplyIdleFiller(IdrScheduler::Action a, bool has_new_frame, bool enabled, bool have_filler,
                                            int ticks_without_frame, int flush_ticks) {
  if (enabled && have_filler && !has_new_frame && a == IdrScheduler::Action::kEncodeP &&
      ticks_without_frame >= flush_ticks) {
    return IdrScheduler::Action::kSkip;
  }
  return a;
}

// DRC_ENC_FLUSH=1: en un tick sin frame nuevo no se recodifica el último (un frame idéntico cuesta ~13 ms de CPU con
// 3 hilos, medido). Si x264 retiene frames (hilos > 1) se le pide el siguiente sin darle entrada; si no retiene nada sale
// el relleno P_Skip. Sin relleno y sin nada retenido se recodifica como antes (el mando necesita un frame por tick).
enum class IdleAction { kReencode, kFlush, kFiller };
inline bool EncFlushEnabled(const char* env) { return env && env[0] == '1' && env[1] == '\0'; }

// Afinado: cuando la imagen se para de verdad (refine_after ticks seguidos sin frame) el último frame se recodifica hasta
// refine_left veces, porque x264 baja el QP y la imagen quieta gana nitidez (medido: +0,6 dB tras 7, y ahí converge; el
// usuario notó la pérdida sin esto). Con vídeo a 24-30 fps los huecos son de 1-2 ticks y no se afina: ahí está el ahorro.
const int kDefaultRefineTicks = 7;
const int kDefaultRefineAfter = 3;
inline int RefineFromEnv(const char* env, int def) {
  if (!env || !env[0]) return def;
  char* end = nullptr;
  long v = strtol(env, &end, 10);
  if (*end != '\0' || v < 0 || v > 600) return def;
  return static_cast<int>(v);
}

inline IdleAction IdleFlushDecision(bool enabled, bool have_filler, int frames_in_encoder, int ticks_without_frame = 0,
                                    int refine_left = 0, int refine_after = kDefaultRefineAfter) {
  if (!enabled) return IdleAction::kReencode;
  if (refine_left > 0 && ticks_without_frame >= refine_after) return IdleAction::kReencode;
  if (frames_in_encoder > 0) return IdleAction::kFlush;
  return have_filler ? IdleAction::kFiller : IdleAction::kReencode;
}

}  // namespace drc
