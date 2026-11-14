import numpy as np
import soundfile as sf
from dataclasses import dataclass, asdict, fields


def read_audio(path):
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return x.mean(axis=1).astype(np.float32), sr


def write_wav(path, x, sr):
    sf.write(str(path), x, sr, subtype="PCM_24")


def write_ogg(path, x, sr, quality=0.6):
    sf.write(str(path), x, sr, format="OGG", subtype="VORBIS")


def resample(x, sr_in, sr_out):
    if sr_in == sr_out:
        return x.astype(np.float32)
    n = int(round(len(x) * sr_out / sr_in))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def measure_noise_floor_db(x, sr):
    return -60.0


def auto_trim(x, sr, sensitivity=0.5, pre_pad_ms=40, post_pad_ms=80):
    thr = 0.02 * (1.0 - sensitivity) + 0.002
    idx = np.nonzero(np.abs(x) > thr)[0]
    if len(idx) == 0:
        return 0.0, len(x) / sr
    return max(0.0, idx[0] / sr - pre_pad_ms / 1000), min(len(x) / sr, idx[-1] / sr + post_pad_ms / 1000)


class LiveSilenceDetector:
    def __init__(self, sr, silence_ms=700, sensitivity=0.5):
        self.sr, self.silence_ms = sr, silence_ms
        self.reset()

    def reset(self):
        self.started, self.quiet = False, 0

    def feed(self, block):
        loud = bool(len(block)) and np.max(np.abs(block)) > 0.02
        if loud:
            self.started, self.quiet = True, 0
        elif self.started:
            self.quiet += len(block)
        return self.started and self.quiet >= self.sr * self.silence_ms / 1000


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
    normalize_db: float = -1.0
    gain_db: float = 0.0
    reverb: float = 0.0

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


PRESETS = {
    "Clean": EffectSettings(),
    "Helmet Intercom": EffectSettings(highpass_hz=200, lowpass_hz=5000, compression=0.6, reverb=0.2),
    "Rally Radio": EffectSettings(highpass_hz=300, lowpass_hz=3500, drive=0.3, compression=0.7, noise_db=-45, squelch=True),
    "Old CB": EffectSettings(highpass_hz=400, lowpass_hz=2800, drive=0.5, bitcrush_bits=8, noise_db=-35, squelch=True),
    "Walkie-Talkie": EffectSettings(highpass_hz=500, lowpass_hz=3000, drive=0.4, compression=0.8, noise_db=-40, squelch=True),
}


def apply_effects(x, sr, settings, seed=0):
    y = x.astype(np.float32).copy()
    if not settings.enabled:
        return y
    if settings.drive:
        y = np.tanh(y * (1 + 8 * settings.drive)).astype(np.float32)
    if settings.noise_db is not None:
        y = y + np.random.default_rng(seed).normal(0, 10 ** (settings.noise_db / 20), len(y)).astype(np.float32)
    pk = np.max(np.abs(y)) if len(y) else 0
    if pk > 0:
        y = y * (10 ** (settings.normalize_db / 20) / pk)
    return (y * 10 ** (settings.gain_db / 20)).astype(np.float32)


def render_take(x, sr, trim_start, trim_end, settings, out_sr=24000, fade_ms=8, seed=0):
    y = x[int(trim_start * sr):int(trim_end * sr)]
    y = apply_effects(y, sr, settings, seed)
    y = resample(y, sr, out_sr)
    pk = np.max(np.abs(y)) if len(y) else 0
    return (y / pk * 0.99 if pk > 0.99 else y).astype(np.float32)


def peaks_for_display(x, n_buckets):
    n_buckets = max(1, int(n_buckets))
    if len(x) == 0:
        z = np.zeros(n_buckets, np.float32)
        return z, z
    edges = np.linspace(0, len(x), n_buckets + 1).astype(int)
    mins = np.zeros(n_buckets, np.float32)
    maxs = np.zeros(n_buckets, np.float32)
    for i in range(n_buckets):
        seg = x[edges[i]:max(edges[i + 1], edges[i] + 1)]
        mins[i], maxs[i] = seg.min(), seg.max()
    return mins, maxs
