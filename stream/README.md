# 24/7 YouTube live stream — deploy notes

`battle_stream.py` is a headless Python port of `index.html`: same simulation,
rendered with numpy+PIL, audio resynthesized with numpy, piped into ffmpeg and
pushed to YouTube's RTMP ingest. No browser, no audio device, no display.

## VPS setup (Ubuntu/Debian, Oracle free tier micro is fine)

```bash
sudo apt update
sudo apt install -y python3 ffmpeg python3-numpy python3-pil fonts-dejavu-core
# 1 GB boxes: add swap so ffmpeg never OOMs
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

sudo mkdir -p /opt/battle
sudo cp battle_stream.py /opt/battle/
echo 'YOUR-YT-STREAM-KEY' | sudo tee /opt/battle/key.txt
sudo cp battle-stream.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now battle-stream
journalctl -u battle-stream -f        # watch the stats line
```

YouTube side: create a channel, enable live streaming (new channels wait ~24h),
YouTube Studio → Go live → Streaming software → copy the **stream key**.
Set the stream to **public** and "unlisted first" is handy while testing.

## Tuning for the 1 GB / burstable micro

The x86 micro sustains only ~1/8 of a core, so encoding is the bottleneck —
the streamer drops video frames to stay realtime when ffmpeg falls behind
(audio is unaffected). If the stats line shows heavy drops, step down:

```bash
# 480p30 — usually sustainable on the micro
ExecStart=... battle_stream.py --key-file ... --width 854 --height 480 --gain 3
# 360p if even that struggles
--width 640 --height 360 --fps 30
```

If you can grab one of Oracle's always-free **Ampere A1** instances
(up to 4 OCPU / 24 GB), run 720p or 1080p at `--preset veryfast` — no contest.

Other flags: `--music 1..5` (ambient, lo-fi, synthwave, chiptune, deep drone),
`--gain` (audio boost; the page mix is quiet — 3 is a good default, lower if
bomb booms distort), `--match-seconds`, `--fps`, `--preset`, `--video-bitrate`.

## Local testing (no stream key needed)

```bash
# offline dump, fast, with audio (video mp4 + raw pcm)
python battle_stream.py --outfile test.mp4 --unthrottled --seconds 20
# realtime pacing smoke test
python battle_stream.py --outfile test.mp4 --seconds 10
# png frames for eyeballing
python battle_stream.py --outfile test.mp4 --unthrottled --seconds 20 --dump-dir frames
```

Requires: python3, numpy, pillow, ffmpeg. On Windows the audio feeds ffmpeg
via a loopback TCP socket (fd passing is POSIX-only); Linux uses an inherited
`pipe:3`, which is what production runs on.
