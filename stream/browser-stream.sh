#!/bin/sh
# stream index.html to YouTube RTMP: Xvfb + chromium + pulse null-sink + ffmpeg x11grab

RES=${RES:-1920x1080}
FPS=${FPS:-30}
BITRATE=${BITRATE:-5000k}
PAGE_URL=${PAGE_URL:-file:///app/index.html}
KEY=""
[ -r /run/key.txt ] && KEY=$(tr -d '\r\n' < /run/key.txt)
DEST=${RTMP_URL:-rtmp://a.rtmp.youtube.com/live2/${KEY}}
[ -n "$DEST" ] || { echo "no destination: mount /run/key.txt or set RTMP_URL"; exit 1; }
W=${RES%%x*}; H=${RES##*x}
KBPS=${BITRATE%[kK]}

export DISPLAY=:99
export XDG_RUNTIME_DIR=/tmp/xdg
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

echo "browser stream: $RES @ ${FPS}fps $BITRATE -> ${DEST%/*}/<key>"

Xvfb :99 -screen 0 "${RES}x24" &
sleep 1
# without a WM chromium ignores kiosk/window-size geometry and opens small
openbox &

pulseaudio --start --exit-idle-time=-1 2>/dev/null || pulseaudio --start --exit-idle-time=-1
pactl load-module module-null-sink sink_name=gamesink sink_properties=device.description=GameSink
pactl set-default-sink gamesink

# keep chromium alive; the page only opens its AudioContext on a click, so fake one
keep_browser() {
  while true; do
    chromium --no-sandbox --disable-gpu --disable-dev-shm-usage --test-type --kiosk \
      --force-device-scale-factor=1 --autoplay-policy=no-user-gesture-required "$PAGE_URL" &
    BPID=$!
    sleep 8
    xdotool mousemove $((W / 2)) $((H / 2)) mousedown 1 mouseup 1
    wait $BPID || true
    echo "chromium died — restarting in 5 s"
    sleep 5
  done
}
keep_browser &

while true; do
  ffmpeg -hide_banner -loglevel warning \
    -f x11grab -video_size "$RES" -framerate "$FPS" -i :99 \
    -f pulse -i gamesink.monitor \
    -map 0:v -map 1:a \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
    -g $((FPS * 2)) -b:v "$BITRATE" -maxrate "$BITRATE" -bufsize "$((KBPS * 2))k" \
    -c:a aac -b:a 128k -ar 44100 \
    -f flv "$DEST" \
    || echo "ffmpeg exited — reconnecting in 5 s"
  sleep 5
done
