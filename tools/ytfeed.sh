#!/usr/bin/env bash
# ytfeed — подливает роутеру свежий адрес googlevideo, чтобы ytwatch мерил
# настоящую скорость видео, а не страницу плеера.
#
# Зачем отдельно и с Дека: прямая ссылка на поток подписана и живёт несколько
# часов, а достать её умеет только yt-dlp с JS-рантаймом (Google шифрует
# параметр `n`, без рантайма отдаёт заведомо порезанный поток). На роутере
# ни того, ни другого нет и не будет.
#
#   ./ytfeed.sh [ROUTER] [VIDEO_URL]
#
# Ставится в крон Дека, если хочется постоянного видео-замера:
#   0 */4 * * * ~/Dev/openwrt-bypass-kit/tools/ytfeed.sh >/dev/null 2>&1
set -euo pipefail

ROUTER=${1:-192.168.1.1}
VIDEO=${2:-https://www.youtube.com/watch?v=aqz-KE-bpKQ}
YTDLP=${YTDLP:-$HOME/.local/bin/yt-dlp}
NODE=${NODE:-$HOME/.local/bin/node}

[ -x "$YTDLP" ] || { echo "нет yt-dlp: $YTDLP"; exit 1; }
[ -x "$NODE" ]  || { echo "нет node (без него Google режет поток): $NODE"; exit 1; }

url=$("$YTDLP" --js-runtimes "node:$NODE" -g -f 'bv[height<=1080][ext=mp4]' "$VIDEO" 2>/dev/null | tail -1)
[ -n "$url" ] || { echo "yt-dlp не отдал прямую ссылку"; exit 1; }

printf '%s' "$url" | ssh -o BatchMode=yes "root@$ROUTER" '/usr/sbin/ytwatch url -'
