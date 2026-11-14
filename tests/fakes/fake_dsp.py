"""Minimal stand-in for codriver_studio.dsp (contract signatures only) used by the
project/exporter/sample_calls tests when the real module is not available.
Installed by tests/conftest.py; set CDS_FAKE_DSP=1 to force it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import numpy as np
import soundfile as sf


def read_audio(path):
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return x.mean(axis=1).astype(np.float32), int(sr)


def write_wav(path, x, sr):
    sf.write(str(path), np.asarray(x, np.float32), int(sr), subtype="PCM_24", format="WAV")


def write_ogg(path, x, sr, quality=0.6):
    sf.write(str(path), np.asarray(x, np.float32), int(sr), format="OGG", subtype="VORBIS")


def resample(x, sr_in, sr_out):
    x = np.asarray(x, np.float32)
    if sr_in == sr_out or x.size == 0:
        return x.copy()
    n = max(1, int(round(len(x) * sr_out / sr_in)))
    t = np.arange(n) * (sr_in / sr_out)
    return np.interp(t, np.arange(len(x)), x).astype(np.float32)


def measure_noise_floor_db(x, sr):
    return -90.0


def auto_trim(x, sr, sensitivity=0.5, pre_pad_ms=40, post_pad_ms=80):
    x = np.asarray(x, np.float32)
    dur = len(x) / sr
    idx = np.flatnonzero(np.abs(x) > 0.05)
    if idx.size == 0:
        return 0.0, dur
    return max(0.0, idx[0] / sr - pre_pad_ms / 1000), min(dur, (idx[-1] + 1) / sr + post_pad_ms / 1000)


@dataclass
class EffectSettings:
    enabled: bool = True
    highpass_hz: float = 0.0
    lowpass_hz: float = 0.0
    drive: float = 0.0
    bitcrush_bits: int = 0
    compression: float = 0.0
    noise_db: float | None = None
    squelch: bool = False
    squelch_db: float = -20.0
    normalize: bool = True
    target_rms_db: float = -23.0
    peak_ceiling_db: float = -3.0
    normalize_db: float | None = None
    gain_db: float = 0.0
    reverb: float = 0.0

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


PRESETS = {"Clean": EffectSettings()}


def apply_effects(x, sr, settings, seed=0):
    x = np.asarray(x, np.float32) * (10 ** (settings.gain_db / 20))
    if settings.normalize and x.size:
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        if rms > 0:
            x = x * (10 ** (settings.target_rms_db / 20) / rms)
        peak = float(np.max(np.abs(x)))
        ceil = 10 ** (settings.peak_ceiling_db / 20)
        if peak > ceil:
            x = x * (ceil / peak)
    return x.astype(np.float32)


def render_take(x, sr, trim_start, trim_end, settings, out_sr=48000, fade_ms=8, seed=0):
    x = np.asarray(x, np.float32)
    seg = x[int(round(trim_start * sr)):int(round(trim_end * sr))].copy()
    f = min(len(seg) // 2, int(fade_ms * sr / 1000))
    if f > 0:
        seg[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        seg[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    if settings.enabled:
        seg = apply_effects(seg, sr, settings, seed)
    y = resample(seg, sr, out_sr)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 0.999:
        y = y * (0.999 / peak)
    return y.astype(np.float32)


class LiveSilenceDetector:
    def __init__(self, sr, silence_ms=700, sensitivity=0.5):
        self.sr = sr

    def feed(self, block):
        return False

    def reset(self):
        pass


def peaks_for_display(x, n_buckets):
    x = np.asarray(x, np.float32)
    if x.size == 0:
        return np.zeros(n_buckets, np.float32), np.zeros(n_buckets, np.float32)
    parts = np.array_split(x, n_buckets)
    return (np.array([p.min() if p.size else 0 for p in parts], np.float32),
            np.array([p.max() if p.size else 0 for p in parts], np.float32))
