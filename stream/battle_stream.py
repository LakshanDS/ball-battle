#!/usr/bin/env python3
"""color ball battle — headless 24/7 YouTube live streamer.

Python port of index.html: the same simulation, rendered with numpy+PIL,
all audio resynthesized with numpy (no browser, no audio device needed).
Raw video + PCM frames are piped into ffmpeg, which pushes to YouTube's
RTMP ingest or writes a local file.

Local test:   python battle_stream.py --outfile test.mp4 --unthrottled --seconds 20
Live stream:  python battle_stream.py --key-file key.txt
"""

import argparse
import math
import os
import random
import signal
import socket
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---- palette (mirrors index.html) ----
BONE = (169, 169, 195)   # light territory
VOID = (34, 31, 62)      # dark territory
REF = (138, 138, 168)    # referee tone
COLORS = [BONE, VOID]
BALL_COLORS = [(244, 244, 249), (21, 18, 40)]
BALL_OUTLINE = [(34, 31, 62, 102), (244, 244, 249, 102)]  # css rgba(...,.4)
BOMB_TXT = [(34, 31, 62, 229), (244, 244, 249, 229)]      # css rgba(...,.9)
CHIP_BG = VOID + (217,)                                   # css rgba(...,.85)
SIDE = ["WHITE", "BLUE"]

BASE_SPEED = 368.0
SUDDEN_DEATH_AT = 180.0
TARGET_BLOCKS = 500

TOKENS = {
    "speed":  {"glyph": "»", "label": "speed",    "speed_mul": 2},
    "big":    {"glyph": "O", "label": "big ball", "size_mul": 1.7},
    "multi":  {"glyph": "+", "label": "multiball"},
    "ghost":  {"glyph": "~", "label": "ghost"},
    "freeze": {"glyph": "*", "label": "freeze"},
    "shrink": {"glyph": "-", "label": "shrink",   "size_mul": 0.55},
    "bomb":   {"glyph": "@", "label": "bomb",     "arm": 12},
}
TOKEN_KEYS = list(TOKENS)


def mix(a, b, t):
    return tuple(round(a[i] * (1 - t) + b[i] * t) for i in range(3))


# token tint = opposite side blended 20% toward the ground
TOKEN_TONE = [mix(VOID, BONE, 0.2), mix(BONE, VOID, 0.2)]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ============================================================ audio synth ===
SR = 44100
CHUNK = 11025  # 0.25 s of mono s16 per synth chunk


def _env_exp(n, gain, dur):
    # WebAudio exponentialRamp: gain -> 0.001 over dur
    tau = np.arange(n) / max(1, n - 1)
    return gain * (0.001 / gain) ** tau


def _wave(kind, ph):
    if kind == "sine":
        return np.sin(ph)
    if kind == "square":
        return np.sign(np.sin(ph))
    if kind == "sawtooth":
        f = (ph / (2 * np.pi)) % 1.0
        return 2.0 * f - 1.0
    return (2 / np.pi) * np.arcsin(np.sin(ph))  # triangle, starts at 0


def _lowpass(x, cutoff):
    if cutoff <= 0:
        return x
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / SR)
    H = 1.0 / np.sqrt(1.0 + (f / cutoff) ** 4)
    return np.fft.irfft(X * H, len(x))


def _tone_buf(freq, dur, wave, gain, slide=0.0):
    n = max(8, int(dur * SR))
    t = np.arange(n) / SR
    if slide:
        f1 = max(30.0, freq + slide)
        fr = freq * (f1 / freq) ** (t / dur)
    else:
        fr = np.full(n, float(freq))
    ph = 2 * np.pi * np.cumsum(fr) / SR
    return _wave(wave, ph) * _env_exp(n, gain, dur)


def _noise_buf(dur, gain, cutoff):
    n = max(8, int(dur * SR))
    x = np.random.uniform(-1, 1, n)
    x = _lowpass(x, cutoff * 0.5)  # the original sweeps cutoff -> 100; this is close enough
    return x * _env_exp(n, gain, dur)


def _pad_buf(freq, dur, wave, gain, attack, cutoff):
    # two detuned oscillators -> lowpass -> slow attack/release
    n = int(dur * SR)
    t = np.arange(n) / SR
    ph = 2 * np.pi * freq * t
    x = (_wave(wave, ph) + _wave(wave, ph * 1.005)) * 0.5
    x = _lowpass(x, cutoff)
    env = np.clip(np.minimum(t / attack, (dur - t) / max(0.001, dur - attack)), 0, 1)
    return x * env * gain


class Synth:
    """Sample-accurate mixer: one-shot voices + sustained voices + interval
    sequencers, all scheduled in absolute audio time. Mirrors the WebAudio
    graph in index.html (master 0.5, music bus 0.14 * style volume)."""

    def __init__(self, music_idx=0, gain=1.0):
        self.master = 0.5 * gain
        self.music_gain = 0.14 * gain
        self.vol = 1.0
        self.sp = 0  # absolute sample position = the audio clock
        self.voices = []      # (start_sample, float32 mono buffer)
        self.sustained = []   # {"render": fn(c0, n) -> mono, "on": bool}
        self.intervals = []   # {"next": sec, "period": sec, "fn": fn(abs_t)}
        self.announce_cb = None
        self._last = {}
        self._synth_last = -1
        self.MUSIC = [
            {"name": "ambient", "vol": 1.0, "start": self._start_ambient},
            {"name": "lo-fi", "vol": 1.0, "start": self._start_lofi},
            {"name": "synthwave", "vol": 0.8, "reroll": True, "start": self._start_synthwave},
            {"name": "chiptune", "vol": 0.9, "start": self._start_chiptune},
            {"name": "deep drone", "vol": 1.1, "start": self._start_drone},
        ]
        self.set_music(music_idx, quiet=True)
        # startup confirmation blip (mirrors the browser audio-unlock chime)
        self.tone(523.25, 0.12, "triangle", 0.12, when=0.15)
        self.tone(783.99, 0.18, "triangle", 0.12, when=0.25)

    # ---- clocking / mixing ----
    @property
    def now(self):
        return self.sp / SR

    def _g(self, dest):
        return self.master if dest == "master" else self.music_gain * self.vol

    def _add(self, t0, buf, dest):
        self.voices.append((int(t0 * SR), (buf * self._g(dest)).astype(np.float32)))

    def add_interval(self, period, fn):
        self.intervals.append({"next": self.now, "period": period, "fn": fn})

    def add_sustained(self, render):
        h = {"render": render, "on": True}
        self.sustained.append(h)
        return h

    def render_chunk(self):
        """Produce the next 0.25 s chunk; fire sequencer ticks that are due."""
        c0, n = self.sp, CHUNK
        t1 = (c0 + n) / SR
        for iv in self.intervals:
            while iv["next"] < t1:
                iv["fn"](iv["next"])
                iv["next"] += iv["period"]
        out = np.zeros(n, dtype=np.float32)
        live = []
        for t0, buf in self.voices:
            end = t0 + len(buf)
            a, b = max(t0, c0), min(end, c0 + n)
            if a < b:
                out[a - c0:b - c0] += buf[a - t0:b - t0]
            if end > c0 + n:
                live.append((t0, buf))
        self.voices = live
        for h in self.sustained[:]:
            if h["on"]:
                out += h["render"](c0, n) * self._g("music")
            else:
                self.sustained.remove(h)
        self.sp += n
        return (np.clip(out, -1, 1) * 32767).astype("<i2").tobytes()

    # ---- voices ----
    def tone(self, freq, dur, wave="sine", gain=0.15, when=0.0, slide=0.0, dest="master"):
        self._add(self.now + when, _tone_buf(freq, dur, wave, gain, slide), dest)

    def noise(self, dur, gain=0.3, freq=1000, when=0.0, dest="master"):
        self._add(self.now + when, _noise_buf(dur, gain, freq), dest)

    def pad(self, freq, t0, dur, wave="sawtooth", gain=0.05, attack=2.2, cutoff=900):
        self._add(t0, _pad_buf(freq, dur, wave, gain, attack, cutoff), "music")

    def _ok(self, key, gap):
        t = self.now
        if t - self._last.get(key, -9) < gap:
            return False
        self._last[key] = t
        return True

    # ---- sfx (ports of the Sfx methods) ----
    def bounce(self, color):
        if self._ok("b", 0.05):
            base = 520 if color == 0 else 300
            self.tone(base * (0.92 + random.random() * 0.16), 0.07, "square", 0.05)

    def wall(self):
        if self._ok("w", 0.08):
            self.tone(130, 0.06, "sine", 0.06)

    def clack(self):
        if self._ok("c", 0.1):
            self.tone(900, 0.05, "triangle", 0.07)

    def phase(self):
        if self._ok("p", 0.06):
            self.tone(980 * (0.9 + random.random() * 0.2), 0.05, "sine", 0.04)

    def power_down(self):
        if self._ok("po", 0.12):
            self.tone(420, 0.22, "triangle", 0.08, slide=-220)

    def grab(self, k):
        base = {"speed": 660, "big": 440, "multi": 550, "ghost": 740,
                "freeze": 392, "shrink": 494, "bomb": 220}.get(k, 500)
        self.tone(base, 0.1, "triangle", 0.16)
        self.tone(base * 1.5, 0.14, "triangle", 0.16, when=0.09)

    def boom(self):
        self.noise(0.7, 0.5, 900)
        self.tone(70, 0.5, "sine", 0.4, slide=-40)

    def sting(self):
        self.tone(110, 0.9, "sawtooth", 0.11)
        self.tone(113.5, 0.9, "sawtooth", 0.11)

    def tick(self):
        self.tone(880, 0.05, "square", 0.06)

    def fanfare(self):
        for i, f in enumerate([523, 659, 784, 1047]):
            self.tone(f, 0.35, "triangle", 0.18, when=i * 0.13)

    def on_new_match(self):
        if self.MUSIC[self.music_idx].get("reroll"):
            self.set_music(self.music_idx, quiet=True)

    def set_music(self, idx, quiet=False):
        self.intervals = []
        self.sustained = []
        self.music_idx = idx % len(self.MUSIC)
        st = self.MUSIC[self.music_idx]
        self.vol = st["vol"]
        st["start"]()
        if not quiet and self.announce_cb:
            self.announce_cb(f"music: {st['name']}")

    # ---- music styles ----
    def _start_ambient(self):
        chords = [[220, 261.6, 329.6], [174.6, 220, 261.6], [196, 246.9, 293.7], [164.8, 196, 246.9]]
        scale = [440, 523.25, 587.33, 659.25, 783.99]
        st = {"n": 0}

        def tick(t):
            for f in chords[st["n"] % 4]:
                self.pad(f, t, 5.5, attack=0.8)
            if random.random() < 0.75:
                self.tone(scale[random.randrange(len(scale))], 1.2, "sine", 0.06,
                          when=t - self.now + random.random() * 2, dest="music")
            st["n"] += 1

        self.add_interval(4.0, tick)

    def _start_lofi(self):
        chords = [[220, 261.6, 329.6, 392], [174.6, 220, 261.6, 329.6],
                  [130.8, 164.8, 196, 246.9], [196, 246.9, 293.7, 349.2]]
        mel = [523.25, 587.33, 659.25, 783.99, 880]
        # 1 s vinyl-crackle buffer, looped forever
        n = SR
        raw = np.random.uniform(-1, 1, n)
        d = raw * np.where(np.random.random(n) < 0.01, 1.0, 0.06)
        f = np.fft.rfftfreq(n, 1.0 / SR)
        H = (1 / np.sqrt(1 + (f / 6000.0) ** 4)) * ((f / 1500.0) ** 2 / np.sqrt(1 + (f / 1500.0) ** 4))
        buf = (np.fft.irfft(np.fft.rfft(d) * H, n) * 0.4).astype(np.float32)
        self.add_sustained(lambda c0, cnt: np.resize(buf, cnt))
        st = {"bar": 0}

        def tick(t):
            dt = t - self.now
            for fq in chords[st["bar"] % 4]:
                self.pad(fq, t, 7, wave="triangle", gain=0.035, attack=1.5, cutoff=650)
            for off in (0.9, 2.3, 3.6, 5.0):
                if random.random() < 0.6:
                    self.tone(mel[random.randrange(5)], 1.1, "sine", 0.05, when=dt + off, dest="music")
            st["bar"] += 1

        self.add_interval(6.0, tick)

    def _start_synthwave(self):
        variants = [
            {"label": "classic", "step": 125, "roots": [110, 87.31, 65.41, 98],
             "arps": [[220, 261.6, 329.6, 440], [174.6, 220, 261.6, 349.2],
                      [130.8, 164.8, 196, 261.6], [196, 246.9, 293.7, 392]]},
            {"label": "night drive", "step": 250, "kick": 16, "bass": 4, "arpStep": 2,
             "arpType": "triangle", "arpGain": 0.022, "cutoff": 700,
             "roots": [73.42, 65.41, 58.27, 73.42],
             "arps": [[146.8, 174.6, 220], [130.8, 164.8, 196], [116.5, 146.8, 174.6], [130.8, 164.8, 196]]},
            {"label": "turbo", "step": 95, "bass": 1, "bassOct": True, "arpStep": 1,
             "arpType": "square", "arpGain": 0.024,
             "roots": [82.41, 65.41, 73.42, 61.74],
             "arps": [[164.8, 196, 246.9, 329.6], [130.8, 164.8, 196, 261.6],
                      [146.8, 174.6, 220, 293.7], [123.5, 146.8, 185, 246.9]]},
            {"label": "pulse", "step": 133, "kickOff": True, "bass": 8, "bassSync": [0, 3, 6],
             "arpStep": 2, "arpType": "sawtooth", "arpGain": 0.014, "cutoff": 1000,
             "roots": [110, 98, 87.31, 82.41],
             "arps": [[220, 261.6, 329.6, 392], [196, 246.9, 293.7, 349.2],
                      [174.6, 220, 261.6, 329.6], [164.8, 207.7, 246.9, 311.1]]},
        ]
        vi = random.randrange(len(variants))
        while vi == self._synth_last:  # never the same variant twice in a row
            vi = random.randrange(len(variants))
        self._synth_last = vi
        v = variants[vi]
        if self.announce_cb:
            self.announce_cb(f"synthwave: {v['label']}")
        period = v["step"] / 1000.0
        st = {"s": 0}

        def tick(t):
            dt = t - self.now
            s = st["s"]
            bar, stp = s // 16, s % 16
            root = v["roots"][bar % 4]
            ch = v["arps"][bar % 4]
            kick = stp % 8 == 4 if v.get("kickOff") else stp % v.get("kick", 8) == 0
            if kick:
                self.tone(120, 0.12, "sine", 0.35, when=dt, slide=-80, dest="music")
            if "bassSync" in v:
                on = (stp % 8) in v["bassSync"]
            elif "bass" in v:
                on = stp % v["bass"] == 0
            else:
                on = False
            if on:
                octv = 2 if (v.get("bassOct") and stp % 4 == 2) else 1
                self.tone(root * octv, 0.22, "sawtooth", 0.09, when=dt, dest="music")
            asp = v["arpStep"]
            if s % asp == 0:
                self.tone(ch[(s // asp) % len(ch)] * 2, 0.09, v.get("arpType", "square"),
                          v.get("arpGain", 0.016), when=dt, dest="music")
            if stp == 0:
                for fq in ch:
                    self.pad(fq, t, period * 16 + 1, gain=0.02, attack=0.4, cutoff=v.get("cutoff", 1200))
            st["s"] += 1

        self.add_interval(period, tick)

    def _start_chiptune(self):
        A = [659.25, 783.99, 880, 783.99, 659.25, 523.25, 587.33, 659.25]
        B = [523.25, 587.33, 659.25, 523.25, 440, 493.88, 523.25, 0]
        lead = A * 2 + B + A
        bass = [130.81, 130.81, 98, 98, 87.31, 87.31, 98, 98]
        st = {"s": 0}

        def tick(t):
            dt = t - self.now
            s = st["s"]
            k = s % 32
            if lead[k]:
                self.tone(lead[k], 0.12, "square", 0.05, when=dt, dest="music")
            if k % 4 == 0:
                self.tone(bass[k // 4], 0.13, "square", 0.05, when=dt, dest="music")
            if k % 2 == 1:
                self.noise(0.03, 0.015, 6000, when=dt, dest="music")
            st["s"] += 1

        self.add_interval(0.14, tick)

    def _start_drone(self):
        def mk(freq, gain, lfo):
            def render(c0, n):
                s = np.arange(c0, c0 + n) / SR
                x = _wave("sawtooth", 2 * np.pi * freq * s)
                x = _lowpass(x, 260)
                return x * gain * (1 + 0.5 * np.sin(2 * np.pi * lfo * s))
            self.add_sustained(mk)

        mk(55, 0.05, 0.06)
        mk(82.5, 0.035, 0.045)
        mk(110.3, 0.02, 0.08)
        st = {"n": 0}

        def tick(t):
            dt = t - self.now
            if st["n"] % 2 == 0:
                self.tone(46, 2.8, "sine", 0.22, when=dt, slide=-16, dest="music")
            if random.random() < 0.35:
                self.tone(1244.5, 3.0, "sine", 0.012, when=dt, dest="music")
            st["n"] += 1

        self.add_interval(4.0, tick)


class SilentSfx:
    """--no-audio stand-in: every synth call becomes a no-op."""

    def __getattr__(self, name):
        return lambda *a, **k: None


# ================================================================== fonts ===
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
    "C:/Windows/Fonts/consolab.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def find_font():
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    sys.exit("no monospace font found — install fonts-dejavu-core (apt) or Consolas")


class FontBook:
    def __init__(self):
        self.path = find_font()
        self.cache = {}

    def get(self, size):
        size = max(6, int(size))
        f = self.cache.get(size)
        if f is None:
            f = self.cache[size] = ImageFont.truetype(self.path, size)
        return f


# ==================================================================== ball ===
class Ball:
    __slots__ = ("color", "x", "y", "temp", "die_at", "spd", "vx", "vy",
                 "speed_until", "big_until", "ghost_until", "frozen_until",
                 "shrink_until", "rally_until", "armed", "armed_until",
                 "last_arm_tick", "last_x", "last_y", "last_move_t", "prev_fx")

    def __init__(self, color, x, y, temp=False, die_at=0.0):
        # random diagonal-ish heading so the motion never looks flat
        a = math.radians(random.random() * 50 + 20)
        self.color = color
        self.x, self.y = x, y
        self.temp, self.die_at = temp, die_at
        self.spd = BASE_SPEED * (0.85 + random.random() * 0.3)
        self.vx = math.cos(a) * (1 if random.random() < 0.5 else -1)
        self.vy = math.sin(a) * (1 if random.random() < 0.5 else -1)
        self.speed_until = self.big_until = self.ghost_until = 0.0
        self.frozen_until = self.shrink_until = self.rally_until = 0.0
        self.armed = False
        self.armed_until = self.last_arm_tick = 0
        self.last_x, self.last_y, self.last_move_t = x, y, 0.0
        self.prev_fx = 0


# ==================================================================== game ===
class Game:
    def __init__(self, w, h, sfx, match_s=600.0, restart_s=10.0):
        self.w, self.h = w, h
        self.sfx = sfx
        self.MATCH = float(match_s)
        self.RESTART = float(restart_s)
        self.cell = max(24, int(math.sqrt(w * h / TARGET_BLOCKS) + 0.5))
        self.fonts = FontBook()
        self.reset()

    def announce(self, text, dur=10.0):
        self.event_txt = text
        self.event_until = self.clock + dur

    def reset(self):
        c = self.cell
        self.cols = math.ceil(self.w / c)
        self.rows = math.ceil(self.h / c)
        self.grid = np.zeros(self.cols * self.rows, dtype=np.uint8)
        # even split: dark blue territory on the right, blocky staircase edge
        for row in range(self.rows):
            edge = self.cols // 2 + random.randrange(5) - 2
            self.grid[row * self.cols + max(0, edge):row * self.cols + self.cols] = 1
        self.counts = [0, 0]
        self.recount()
        self.balls = [Ball(0, self.w * 0.25, self.h * 0.5),
                      Ball(1, self.w * 0.75, self.h * 0.4)]
        self.tokens, self.flashes, self.sparks = [], [], []
        for _ in range(5):
            self.spawn_token()
        self.match_left = self.MATCH
        self.clock = 0.0
        self.state = "play"
        self.sd = False
        self.next_token_in = 2.0
        self.last_tick = -1
        self.chip_sd = self.chip_last10 = False
        self.over_t = self.restart_in = 0.0
        self.winner = 0
        self.sfx.on_new_match()
        self.event_txt, self.event_until = "", 0.0

    def recount(self):
        self.counts = [int((self.grid == 0).sum()), int((self.grid == 1).sum())]

    # ---- geometry helpers ----
    def ball_radius(self, b):
        r = self.cell * 0.45
        if b.big_until > self.clock:
            r *= TOKENS["big"]["size_mul"]
        if b.shrink_until > self.clock:
            r *= TOKENS["shrink"]["size_mul"]
        return r

    def ball_speed(self, b):
        if b.frozen_until > self.clock:
            return 0.0
        s = b.spd
        if b.speed_until > self.clock:
            s *= TOKENS["speed"]["speed_mul"]
        if b.rally_until > self.clock:
            s *= 1.2  # multiball squad bonus
        if self.sd:  # sudden-death ramp
            s *= 1 + 0.5 * (1 - self.match_left / SUDDEN_DEATH_AT)
        return s

    def overlapping_cell(self, b, r):
        # only blocks of the OTHER color are solid; own color is open floor
        c = self.cell
        c0 = max(0, int((b.x - r) / c)); c1 = min(self.cols - 1, int((b.x + r) / c))
        r0 = max(0, int((b.y - r) / c)); r1 = min(self.rows - 1, int((b.y + r) / c))
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                i = row * self.cols + col
                if self.grid[i] == b.color:
                    continue
                cx = min(max(b.x, col * c), col * c + c)
                cy = min(max(b.y, row * c), row * c + c)
                dx, dy = b.x - cx, b.y - cy
                if dx * dx + dy * dy < r * r:
                    return i
        return -1

    def flip(self, i, color):
        if self.grid[i] == color:
            return
        self.counts[self.grid[i]] -= 1
        self.counts[color] += 1
        self.grid[i] = color

    def convert_around(self, b, r):
        # flip every enemy cell the circle touches (ghost phasing, bomb blasts)
        c = self.cell
        c0 = max(0, int((b.x - r) / c)); c1 = min(self.cols - 1, int((b.x + r) / c))
        r0 = max(0, int((b.y - r) / c)); r1 = min(self.rows - 1, int((b.y + r) / c))
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                i = row * self.cols + col
                if self.grid[i] == b.color:
                    continue
                cx = min(max(b.x, col * c), col * c + c)
                cy = min(max(b.y, row * c), row * c + c)
                dx, dy = b.x - cx, b.y - cy
                if dx * dx + dy * dy < r * r:
                    self.flip(i, b.color)

    def detonate(self, b):
        b.armed = False
        self.convert_around(b, self.cell * 3.5)
        self.flashes.append({"x": b.x, "y": b.y, "at": self.clock, "big": True})
        self.sfx.boom()
        self.announce(f"{SIDE[b.color]} detonates @ bomb")

    def step_ball(self, b, dt):
        r = self.ball_radius(b)
        sp = self.ball_speed(b)
        ghost = b.ghost_until > self.clock
        # substep so the ball can never tunnel through a block
        dist = sp * dt
        steps = max(1, math.ceil(dist / (self.cell * 0.4)))
        sx = b.vx * sp * dt / steps
        sy = b.vy * sp * dt / steps

        for _ in range(steps):
            b.x += sx
            hit = self.overlapping_cell(b, r)
            if hit >= 0:
                if ghost:
                    self.flip(hit, b.color); self.sfx.phase()
                else:
                    b.x -= sx; b.vx = -b.vx
                    self.flip(hit, b.color); self.sfx.bounce(b.color)
                    if b.armed:
                        self.detonate(b)
            b.y += sy
            hit = self.overlapping_cell(b, r)
            if hit >= 0:
                if ghost:
                    self.flip(hit, b.color); self.sfx.phase()
                else:
                    b.y -= sy; b.vy = -b.vy
                    self.flip(hit, b.color); self.sfx.bounce(b.color)
                    if b.armed:
                        self.detonate(b)
            if b.x < r:
                b.x = r; b.vx = abs(b.vx); self.sfx.wall()
            if b.x > self.w - r:
                b.x = self.w - r; b.vx = -abs(b.vx); self.sfx.wall()
            if b.y < r:
                b.y = r; b.vy = abs(b.vy); self.sfx.wall()
            if b.y > self.h - r:
                b.y = self.h - r; b.vy = -abs(b.vy); self.sfx.wall()
        if ghost:
            self.convert_around(b, r)

        # token pickup
        for t in range(len(self.tokens) - 1, -1, -1):
            tk = self.tokens[t]
            dx, dy = b.x - tk["x"], b.y - tk["y"]
            rr = r + 14
            if dx * dx + dy * dy < rr * rr:
                self.tokens.pop(t)
                self.flashes.append({"x": tk["x"], "y": tk["y"], "at": self.clock})
                self.sfx.grab(tk["k"])
                self.apply_token(b, tk)

    def apply_token(self, b, tk):
        k = tk["k"]
        side = SIDE[b.color]
        dur = 10 + random.random() * 5      # most effects last 10-15 s
        short = 5 + random.random() * 3     # ghost & freeze burn out faster
        if k == "speed":
            b.speed_until = self.clock + dur
        if k == "big":
            b.big_until = self.clock + dur
        if k == "ghost":
            b.ghost_until = self.clock + short
        if k == "multi":
            n = 1 + random.randrange(5)     # 1-5 extra balls
            for _ in range(n):
                extra = Ball(b.color, b.x, b.y, True, self.clock + dur)
                a = random.random() * math.pi * 2  # fan out in random directions
                extra.vx, extra.vy = math.cos(a), math.sin(a)
                self.balls.append(extra)
            # whole squad gets +20% speed for the duration
            for o in self.balls:
                if o.color == b.color:
                    o.rally_until = self.clock + dur
            self.announce(f"{side} grabs + ×{n} multiball & speed")
        if k == "freeze":
            for o in self.balls:
                if o.color != b.color:
                    o.frozen_until = self.clock + short
        if k == "shrink":
            for o in self.balls:
                if o.color != b.color:
                    o.shrink_until = self.clock + dur
        if k == "bomb":
            b.armed = True                          # next block impact sets it off
            b.armed_until = self.clock + TOKENS["bomb"]["arm"]  # unless the fuse runs out
            b.last_arm_tick = TOKENS["bomb"]["arm"]
            self.flashes.append({"x": tk["x"], "y": tk["y"], "at": self.clock})
        self.announce(f"{side} grabs {TOKENS[k]['glyph']} {TOKENS[k]['label']}")

    def spawn_token(self):
        k = TOKEN_KEYS[random.randrange(len(TOKEN_KEYS))]
        m = self.cell * 1.5
        self.tokens.append({
            "k": k,
            "x": m + random.random() * (self.w - 2 * m),
            "y": m + random.random() * (self.h - 2 * m),
            "phase": random.random() * 6.28,
            "ttl": 30 + random.random() * 30,  # unclaimed tokens expire and respawn
        })

    def ball_collisions(self):
        # equal-mass elastic: swap velocities so two balls can't share a pocket
        bs = self.balls
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                a, b = bs[i], bs[j]
                dx, dy = b.x - a.x, b.y - a.y
                rr = self.ball_radius(a) + self.ball_radius(b)
                d2 = dx * dx + dy * dy
                if d2 >= rr * rr or d2 == 0:
                    continue
                d = math.sqrt(d2)
                nx, ny = dx / d, dy / d
                push = (rr - d) / 2
                a.x -= nx * push; a.y -= ny * push
                b.x += nx * push; b.y += ny * push
                a.vx, b.vx = b.vx, a.vx
                a.vy, b.vy = b.vy, a.vy
                self.sfx.clack()

    def relocate(self, b):
        # safety net: drop a stuck ball into a random own-color cell
        own = np.nonzero(self.grid == b.color)[0]
        if len(own) == 0:
            return
        i = int(own[random.randrange(len(own))])
        b.x = ((i % self.cols) + 0.5) * self.cell
        b.y = ((i // self.cols) + 0.5) * self.cell
        a = random.random() * math.pi * 2
        b.vx, b.vy = math.cos(a), math.sin(a)
        b.last_x, b.last_y, b.last_move_t = b.x, b.y, self.clock

    def check_stuck(self):
        for b in self.balls:
            if b.frozen_until > self.clock:
                b.last_x, b.last_y, b.last_move_t = b.x, b.y, self.clock
                continue
            dx, dy = b.x - b.last_x, b.y - b.last_y
            if dx * dx + dy * dy > 100:
                b.last_x, b.last_y, b.last_move_t = b.x, b.y, self.clock
            elif self.clock - b.last_move_t > 2.5:
                self.relocate(b)

    @staticmethod
    def fx_mask(b, clock):
        return ((b.speed_until > clock) | (b.big_until > clock) << 1 |
                (b.rally_until > clock) << 2 | (b.ghost_until > clock) << 3 |
                (b.frozen_until > clock) << 4 | (b.shrink_until > clock) << 5)

    def wear_off(self, x, y):
        # little block-sparks: the one true "power wore off" moment
        for _ in range(9):
            a = random.random() * math.pi * 2
            sp = 60 + random.random() * 130
            self.sparks.append({"x": x, "y": y, "vx": math.cos(a) * sp, "vy": math.sin(a) * sp,
                                "life": 0.45 + random.random() * 0.35,
                                "size": 3 + random.random() * 4})
        self.sfx.power_down()

    def update_sparks(self, dt):
        for i in range(len(self.sparks) - 1, -1, -1):
            s = self.sparks[i]
            s["life"] -= dt
            if s["life"] <= 0:
                self.sparks.pop(i)
                continue
            s["x"] += s["vx"] * dt
            s["y"] += s["vy"] * dt

    def tick_game(self, dt):
        self.clock += dt
        self.match_left -= dt

        if not self.sd and self.MATCH > SUDDEN_DEATH_AT and self.match_left <= SUDDEN_DEATH_AT:
            self.sd = True
            self.chip_sd = True
            self.announce("sudden death", 5)
            self.sfx.sting()
        # final countdown ticks
        if 0 < self.match_left <= 10:
            s = math.ceil(self.match_left)
            if s != self.last_tick:
                self.last_tick = s
                self.sfx.tick()
        if self.match_left <= 10:
            self.chip_last10 = True

        # keep 5 tokens on the board; expired ones respawn elsewhere
        for i in range(len(self.tokens) - 1, -1, -1):
            self.tokens[i]["ttl"] -= dt
            if self.tokens[i]["ttl"] <= 0:
                self.tokens.pop(i)
        self.next_token_in -= dt
        if len(self.tokens) < 5 and self.next_token_in <= 0:
            self.spawn_token()
            self.next_token_in = 1 + random.random() if self.sd else 2 + random.random() * 2

        for b in self.balls:
            self.step_ball(b, dt)
        self.ball_collisions()
        self.check_stuck()

        # effect expiry -> wear-off burst; armed bombs defuse when the fuse ends
        for b in self.balls:
            m = self.fx_mask(b, self.clock)
            if b.prev_fx & ~m:
                self.wear_off(b.x, b.y)
            b.prev_fx = m
            if b.armed:
                s = math.ceil(b.armed_until - self.clock)
                if s != b.last_arm_tick:
                    b.last_arm_tick = s
                    if s > 0:
                        self.sfx.tick()
                if b.armed_until <= self.clock:
                    b.armed = False
                    self.wear_off(b.x, b.y)
                    self.announce(f"{SIDE[b.color]} bomb defused")
        # multiball extras expire into the same burst
        for i in range(len(self.balls) - 1, -1, -1):
            b = self.balls[i]
            if b.temp and b.die_at <= self.clock:
                self.wear_off(b.x, b.y)
                self.balls.pop(i)
        self.flashes = [f for f in self.flashes if self.clock - f["at"] < 0.5]

        # domination win: one color takes 75% of the board
        total = self.cols * self.rows
        if self.counts[0] >= total * 0.75 or self.counts[1] >= total * 0.75:
            self.end_match()
            return
        if self.match_left <= 0:
            self.end_match()

    def end_match(self):
        if self.state == "over":
            return
        self.state = "over"
        self.match_left = 0.0
        self.over_t = 0.0
        self.restart_in = self.RESTART
        if self.counts[0] == self.counts[1]:
            self.winner = random.randrange(2)
        else:
            self.winner = 0 if self.counts[0] > self.counts[1] else 1
        self.tokens = []
        self.sfx.fanfare()
        self.chip_sd = self.chip_last10 = False
        self.announce("")

    def tick_over(self, dt):
        self.over_t += dt
        self.restart_in -= dt
        if self.restart_in <= 0:
            self.reset()

    def update(self, dt):
        self.update_sparks(dt)
        if self.state == "play":
            self.tick_game(dt)
        else:
            self.tick_over(dt)

    # ================================================================ render ===
    def font(self, size):
        return self.fonts.get(size)

    def _layer(self, img, x0, y0, x1, y1, fn):
        """Draw translucent RGBA content over a region: fn(draw, ox, oy) gets
        local coordinates (subtract ox/oy from game coords)."""
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(self.w, int(x1)), min(self.h, int(y1))
        if x1 <= x0 or y1 <= y0:
            return
        base = img.crop((x0, y0, x1, y1)).convert("RGBA")
        lay = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        fn(ImageDraw.Draw(lay), x0, y0)
        img.paste(Image.alpha_composite(base, lay).convert("RGB"), (x0, y0))

    @staticmethod
    def _text_center(d, x, y, s, font, fill):
        b = d.textbbox((0, 0), s, font=font)
        d.text((x - (b[0] + b[2]) / 2, y - (b[1] + b[3]) / 2), s, font=font, fill=fill)

    def _draw_tokens(self, img):
        c = self.cell
        f = self.font(c * 0.33)
        for t in self.tokens:
            col, row = int(t["x"] // c), int(t["y"] // c)
            if 0 <= row < self.rows and 0 <= col < self.cols:
                tone = TOKEN_TONE[self.grid[row * self.cols + col]] + (255,)
            else:
                tone = REF + (255,)
            expiring = t["ttl"] < 5
            pulse = 1 + (0.28 if expiring else 0.14) * math.sin(self.clock * 5 + t["phase"])
            s = c * 0.28 * pulse
            glyph = str(math.ceil(t["ttl"])) if expiring else TOKENS[t["k"]]["glyph"]

            def fn(d, ox, oy, t=t, s=s, tone=tone, glyph=glyph):
                x, y = t["x"] - ox, t["y"] - oy
                pts = [(x, y - s), (x + s, y), (x, y + s), (x - s, y), (x, y - s)]
                d.line(pts, fill=tone, width=3, joint="curve")
                self._text_center(d, x, y, glyph, f, tone)

            pad = s + 14
            self._layer(img, t["x"] - pad, t["y"] - pad, t["x"] + pad, t["y"] + pad, fn)

    def _draw_flashes(self, img):
        for fl in self.flashes:
            p = (self.clock - fl["at"]) / 0.5
            rad = self.cell * 0.2 + p * (self.cell * 4 if fl.get("big") else self.cell * 0.7)
            w = 5 if fl.get("big") else 2
            col = REF + (int(255 * (1 - p)),)

            def fn(d, ox, oy, fl=fl, rad=rad, w=w, col=col):
                x, y = fl["x"] - ox, fl["y"] - oy
                d.ellipse((x - rad, y - rad, x + rad, y + rad), outline=col, width=w)

            self._layer(img, fl["x"] - rad - 4, fl["y"] - rad - 4, fl["x"] + rad + 4, fl["y"] + rad + 4, fn)

    def _draw_sparks(self, img):
        for s in self.sparks:
            a = int(255 * min(1, s["life"] * 2.5))

            def fn(d, ox, oy, s=s, a=a):
                d.rectangle((s["x"] - ox, s["y"] - oy,
                             s["x"] - ox + s["size"], s["y"] - oy + s["size"]), fill=REF + (a,))

            self._layer(img, s["x"], s["y"], s["x"] + s["size"] + 1, s["y"] + s["size"] + 1, fn)

    def _draw_ball(self, img, b):
        clock = self.clock
        r = self.ball_radius(b)
        # extra balls pulse faster & harder over their last 5 s
        if b.temp and b.die_at - clock < 5:
            k = 1 - max(0.0, b.die_at - clock) / 5
            r *= 1 + (0.08 + 0.22 * k) * abs(math.sin(clock * (5 + 5 * k)))
        outline = BALL_OUTLINE[b.color]

        def fn(d, ox, oy, b=b, r=r, outline=outline, clock=clock):
            x, y = b.x - ox, b.y - oy
            if b.speed_until > clock or b.big_until > clock or b.rally_until > clock:
                rr = r + self.cell * 0.18 + self.cell * 0.05 * math.sin(clock * 9)
                d.ellipse((x - rr, y - rr, x + rr, y + rr), outline=outline, width=3)
            if b.frozen_until > clock or b.shrink_until > clock:
                rr = r + self.cell * 0.14
                d.ellipse((x - rr, y - rr, x + rr, y + rr), outline=REF + (255,), width=3)
            d.ellipse((x - r, y - r, x + r, y + r), fill=outline)
            d.ellipse((x - r * 0.8, y - r * 0.8, x + r * 0.8, y + r * 0.8), fill=BALL_COLORS[b.color] + (255,))
            if b.armed:  # live bomb: blinking referee ring + fuse countdown
                al = int(255 * (0.45 + 0.55 * abs(math.sin(clock * 6))))
                rr = r + self.cell * 0.2
                d.ellipse((x - rr, y - rr, x + rr, y + rr), outline=REF + (al,), width=4)
                f = self.font(r * 0.78)
                self._text_center(d, x, y, str(max(0, math.ceil(b.armed_until - clock))), f, BOMB_TXT[b.color])

        pad = r + self.cell * 0.5
        self._layer(img, b.x - pad, b.y - pad, b.x + pad, b.y + pad, fn)

    def render(self):
        c = self.cell
        pal = np.full((self.rows, self.cols, 3), BONE, dtype=np.uint8)
        pal[self.grid.reshape(self.rows, self.cols) == 1] = VOID
        img = Image.new("RGB", (self.w, self.h))
        img.paste(Image.fromarray(pal).resize((self.cols * c, self.rows * c), Image.NEAREST), (0, 0))

        if self.state == "play":
            self._draw_tokens(img)
        self._draw_flashes(img)
        self._draw_sparks(img)
        for b in self.balls:
            self._draw_ball(img, b)

        if self.state == "over":
            # the winning color floods the board
            a = clamp((self.over_t - 0.3) / 1.1, 0, 1)
            if a > 0:
                self._layer(img, 0, 0, self.w, self.h,
                            lambda d, ox, oy: d.rectangle((0, 0, self.w, self.h),
                                                          fill=COLORS[self.winner] + (int(255 * a),)))

        self._draw_hud(img)
        # the overlay paints above the HUD, like the page's DOM order
        if self.state == "over" and self.over_t > 1.2:
            self._draw_overlay(img)
        return img

    def _draw_hud(self, img):
        d = ImageDraw.Draw(img)
        w, h = self.w, self.h
        wl = round(self.counts[0] / (self.cols * self.rows) * 100)

        # ---- territory bar ----
        bw = min(900, int(w * 0.7))
        bx, by, bh = (w - bw) // 2, 14, 30
        d.rectangle((bx, by, bx + bw, by + bh), fill=VOID, outline=REF, width=2)
        fw = int((bw - 4) * wl / 100)
        if fw > 0:
            d.rectangle((bx + 2, by + 2, bx + 2 + fw, by + bh - 2), fill=BONE)
        d.rectangle((bx, by, bx + bw, by + bh), outline=REF, width=2)
        for frac in (0.15, 0.85):
            nx = bx + int(bw * frac)
            d.rectangle((nx, by, nx + 2, by + bh), fill=REF)
        mid = by + bh / 2
        f18, f15 = self.font(18), self.font(15)
        d.text((bx + 12, mid), f"{wl}%", font=f18, fill=VOID, anchor="lm")
        d.text((bx + bw - 12, mid), f"{100 - wl}%", font=f18, fill=BONE, anchor="rm")
        edge = bx + 2 + (bw - 4) * wl / 100
        d.text((edge - 5, mid), str(self.counts[0]), font=f15, fill=VOID, anchor="rm")
        d.text((edge + 5, mid), str(self.counts[1]), font=f15, fill=BONE, anchor="lm")

        # ---- event chip ----
        remain = self.event_until - self.clock
        if remain > 0 and self.event_txt:
            txt = self.event_txt.upper()
            f12 = self.font(12)
            tw = d.textlength(txt, font=f12)
            ew, eh = int(tw) + 24, 22
            ex, ey = (w - ew) // 2, 62
            a = min(1.0, remain / 2)  # fade out over the last 2 s

            def fn(d, ox, oy):
                d.rectangle((ex - ox, ey - oy, ex - ox + ew, ey - oy + eh),
                            fill=(VOID[0], VOID[1], VOID[2], int(217 * a)), outline=REF + (int(255 * a),), width=2)
                d.text((ex - ox + ew / 2, ey - oy + eh / 2), txt, font=f12,
                       fill=BONE + (int(255 * a),), anchor="mm")

            self._layer(img, ex - 3, ey - 3, ex + ew + 3, ey + eh + 3, fn)

        # ---- match clock chip ----
        if self.state == "play":
            s = max(0, math.ceil(self.match_left))
            txt = f"{s // 60}:{s % 60:02d}"
        else:
            txt = "0:00"
        f24 = self.font(24)
        cw = max(100, int(d.textlength(txt, font=f24)) + 36)
        ch = 40
        cx, cy = (w - cw) // 2, h - 14 - ch
        sd = self.chip_sd and self.state == "play"
        if sd:  # sudden death: inverted chip
            bg, fg, border = BONE, VOID, VOID
        else:
            bg, fg, border = VOID, BONE, REF
        period = 0.55 if (self.chip_last10 or sd) else 0.0
        alpha = 255
        if period:
            alpha = int(255 * (0.4 + 0.6 * abs(math.sin(2 * math.pi * self.clock / period))))

        def fn(d, ox, oy):
            d.rectangle((cx - ox, cy - oy, cx - ox + cw, cy - oy + ch),
                        fill=bg + (alpha,) if sd else (VOID[0], VOID[1], VOID[2], alpha),
                        outline=border + (alpha,), width=2)
            d.text((cx - ox + cw / 2, cy - oy + ch / 2), txt, font=f24,
                   fill=fg + (alpha,), anchor="mm")

        self._layer(img, cx - 3, cy - 3, cx + cw + 3, cy + ch + 3, fn)

    def _draw_overlay(self, img):
        d = ImageDraw.Draw(img)
        col = VOID if self.winner == 0 else BONE  # contrast with the flood
        fwin = self.font(clamp(self.w * 0.08, 42, 96))
        ffin, fnx = self.font(22), self.font(13)
        win = "WHITE WINS" if self.winner == 0 else "BLUE WINS"
        fin = f"white {self.counts[0]} — blue {self.counts[1]}"
        nxt = f"next match in {max(0, math.ceil(self.restart_in))}"
        win_sz = fwin.size
        total = win_sz + 10 + 22 + 14 + 13
        y = self.h / 2 - total / 2
        self._text_center(d, self.w / 2, y + win_sz / 2, win, fwin, col)
        self._text_center(d, self.w / 2, y + win_sz + 10 + 11, fin, ffin, col)
        self._text_center(d, self.w / 2, y + win_sz + 10 + 22 + 14 + 6, nxt, fnx, col)


# =================================================================== main ===
def spawn_ffmpeg(args, w, h, fps):
    """Returns (proc, audio_writer, cleanup).

    The raw PCM goes to ffmpeg as a second input: via an inherited fd 3 pipe
    on posix, or via a loopback TCP socket where fd passing is unavailable
    (Windows) — ffmpeg listens, we connect and send."""
    audio_in = []
    pass_fds = ()
    writer = None
    cleanup = []

    if not args.no_audio:
        if os.name == "posix":
            r, w = os.pipe()
            if r != 3:
                os.dup2(r, 3)
                os.close(r)
                r = 3
            audio_in = ["-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:3"]
            pass_fds = (3,)
            writer = lambda b: os.write(w, b)
            cleanup += [lambda: os.close(w), lambda: os.close(r)]
        else:
            port = args.audio_port
            audio_in = ["-f", "s16le", "-ar", str(SR), "-ac", "1",
                        "-i", f"tcp://127.0.0.1:{port}?listen=1&timeout=15000000"]

    if args.outfile:
        tail = ["-f", "mp4", "-movflags", "+faststart", args.outfile]
    else:
        key = args.key
        if not key and args.key_file:
            with open(args.key_file, encoding="utf-8") as fh:
                key = fh.read().strip()
        if not key and not args.rtmp:
            sys.exit("stream destination required: --key / --key-file / --rtmp / --outfile")
        url = args.rtmp or f"rtmp://a.rtmp.youtube.com/live2/{key}"
        print(f"streaming to {url.rsplit('/', 1)[0]}/<key>", flush=True)
        tail = ["-f", "flv", url]

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps),
           "-i", "pipe:0", *audio_in, "-map", "0:v"]
    if audio_in:
        cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    kbps = args.video_bitrate.rstrip("kKmM")
    cmd += ["-c:v", "libx264", "-preset", args.preset, "-pix_fmt", "yuv420p",
            "-g", str(2 * fps), "-b:v", args.video_bitrate,
            "-maxrate", args.video_bitrate,
            "-bufsize", f"{int(kbps) * 2}k"]
    if args.ffmpeg:
        cmd[0] = args.ffmpeg
    cmd += tail
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, pass_fds=pass_fds)

    if audio_in and writer is None:  # windows path: dial ffmpeg's listening socket
        s = socket.socket()
        for _ in range(50):
            try:
                s.connect(("127.0.0.1", args.audio_port))
                break
            except OSError:
                time.sleep(0.1)
        else:
            proc.kill()
            sys.exit("could not connect the audio socket to ffmpeg")
        writer = s.sendall
        cleanup.append(s.close)
    return proc, writer, cleanup


def main():
    ap = argparse.ArgumentParser(description="24/7 ball battle YouTube stream")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--match-seconds", type=int, default=600)
    ap.add_argument("--restart-seconds", type=int, default=10)
    ap.add_argument("--music", type=int, default=1, help="1-5, like the page's music param")
    ap.add_argument("--gain", type=float, default=1.0,
                    help="audio multiplier; ~3 before booms start clipping (the page mix is quiet)")
    ap.add_argument("--preset", default="ultrafast", help="x264 preset (ultrafast is right for a weak VPS)")
    ap.add_argument("--video-bitrate", default="2500k")
    ap.add_argument("--key", help="YouTube stream key")
    ap.add_argument("--key-file", help="file containing the stream key")
    ap.add_argument("--rtmp", help="full RTMP url (overrides --key)")
    ap.add_argument("--outfile", help="write to a file instead of RTMP (testing)")
    ap.add_argument("--ffmpeg", help="path to the ffmpeg binary (default: from PATH)")
    ap.add_argument("--audio-port", type=int, default=18421,
                    help="loopback port for the audio feed (Windows path only)")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--unthrottled", action="store_true",
                    help="render as fast as possible (offline test dumps)")
    ap.add_argument("--seconds", type=float, help="stop after this much content (testing)")
    ap.add_argument("--dump-dir", help="also save PNG frames here (testing)")
    ap.add_argument("--dump-every", type=int, default=24)
    ap.add_argument("--seed", type=int, help="rng seed for reproducible test runs")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)

    audio_writer = None
    audio_cleanup = []
    ff = None
    try:
        sfx = None if args.no_audio else Synth(args.music - 1, args.gain)
        game = Game(args.width, args.height, sfx or SilentSfx(),
                    args.match_seconds, args.restart_seconds)
        if sfx:
            sfx.announce_cb = game.announce

        if args.outfile or args.rtmp or args.key or args.key_file:
            ff, audio_writer, audio_cleanup = spawn_ffmpeg(args, args.width, args.height, args.fps)

        if args.dump_dir:
            os.makedirs(args.dump_dir, exist_ok=True)

        fps = args.fps
        realtime = not args.unthrottled
        content_t = 0.0  # cumulative sim time; game.clock resets each match
        start = time.perf_counter()
        last = 0.0
        frame = rendered = 0
        next_stat = 30.0
        print(f"sim started: {args.width}x{args.height}@{fps}, cell {game.cell}, "
              f"grid {game.cols}x{game.rows}, match {args.match_seconds}s", flush=True)

        while True:
            wall = time.perf_counter() - start
            # audio keeps a small realtime lead; in unthrottled mode it tracks the sim
            if audio_writer:
                target = (wall if realtime else game.clock) + 0.2
                while sfx.now < target:
                    audio_writer(sfx.render_chunk())

            due = frame / fps
            if realtime:
                if wall < due:
                    time.sleep(due - wall)
                    wall = time.perf_counter() - start
                if wall > due + 0.15:  # encoding can't keep up: skip, stay realtime
                    frame += 1
                    continue
                dt = min(0.05, wall - last)
            else:
                dt = 1.0 / fps
            last = wall

            game.update(dt)
            content_t += dt
            img = game.render()
            if ff:
                ff.stdin.write(img.tobytes())
            if args.dump_dir and frame % args.dump_every == 0:
                img.save(os.path.join(args.dump_dir, f"f{frame:06d}.png"))
            frame += 1
            rendered += 1

            if wall >= next_stat:
                wl = round(game.counts[0] / (game.cols * game.rows) * 100)
                lead = f"{sfx.now - wall:+.2f}s" if sfx else "n/a"
                print(f"[{int(wall):5d}s] sim {int(game.clock // 60)}:{int(game.clock % 60):02d} "
                      f"white {wl}% balls {len(game.balls)} fps {rendered / wall:.1f} "
                      f"dropped {frame - rendered} audio-lead {lead}", flush=True)
                next_stat += 30

            if args.seconds and (wall if realtime else content_t) >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        print("shutting down", flush=True)
        if ff:
            try:
                ff.stdin.close()
                ff.wait(timeout=10)
            except Exception:
                ff.kill()
        for fn in audio_cleanup:
            try:
                fn()
            except OSError:
                pass


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
