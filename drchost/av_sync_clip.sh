#!/usr/bin/env bash
# Genera un clip de 10 s para comprobar la sincronía audio/vídeo en el GamePad:
# 854x480 a 60 fps con un destello blanco y un pitido de 1 kHz a la vez (100 ms) al principio
# de cada segundo; audio estéreo a 48 kHz. Uso: av_sync_clip.sh <salida.mkv>
set -euo pipefail

OUT="${1:?uso: av_sync_clip.sh <salida>}"

ffmpeg -v error -y \
    -f lavfi -i "color=c=black:s=854x480:r=60:d=10,drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,1),0.1)'" \
    -f lavfi -i "aevalsrc='if(lt(mod(t,1),0.1),0.8*sin(2*PI*1000*t),0)|if(lt(mod(t,1),0.1),0.8*sin(2*PI*1000*t),0)':s=48000:d=10" \
    -c:v libx264 -preset ultrafast -crf 18 -pix_fmt yuv420p -r 60 \
    -c:a pcm_s16le -ar 48000 -ac 2 \
    "${OUT}"
