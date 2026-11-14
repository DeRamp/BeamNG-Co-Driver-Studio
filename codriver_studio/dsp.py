"""Audio DSP for Co-Driver Studio: file IO, resampling, speech trimming, radio effects.

Pure numpy (no scipy, no Qt). All audio arrays are float32, mono, 1-D, nominal range [-1, 1].
Times are seconds unless a name ends in ``_ms``.
"""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, fields
from typing import Any

import numpy as np
import soundfile as sf

__all__ = [
    "read_audio", "write_wav", "write_ogg", "resample", "measure_noise_floor_db",
    "auto_trim", "LiveSilenceDetector", "EffectSettings", "PRESETS", "apply_effects",
    "render_take", "peaks_for_display", "active_rms_db",
]

_TINY = 1e-13           # power floor (-130 dB)
_DIGITAL_SILENCE_DB = -100.0


# ----------------------------------------------------------------------------------------------
# File IO
# ----------------------------------------------------------------------------------------------

def _as_mono_f32(x) -> np.ndarray:
    a = np.asarray(x, dtype=np.float32)
    if a.ndim == 2:
        a = a.mean(axis=1, dtype=np.float32) if a.shape[1] > 1 else a[:, 0]
    elif a.ndim != 1:
        a = a.reshape(-1)
    return np.ascontiguousarray(a, dtype=np.float32)


def _safe_for_write(x) -> np.ndarray:
    a = _as_mono_f32(x)
    a = np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.clip(a, -1.0, 1.0)


def read_audio(path) -> tuple[np.ndarray, int]:
    """Read any soundfile-supported file, mixed down to mono float32."""
    data, sr = sf.read(os.fspath(path), dtype="float32", always_2d=True)
    return _as_mono_f32(data), int(sr)


def write_wav(path, x, sr) -> None:
    """Write a lossless PCM_24 WAV (raw takes)."""
    sf.write(os.fspath(path), _safe_for_write(x), int(sr), subtype="PCM_24", format="WAV")


def write_ogg(path, x, sr, quality: float = 0.6) -> None:
    """Write mono OGG Vorbis. ``quality`` 0..1 (libsndfile: compression_level = 1 - quality)."""
    a = _safe_for_write(x)
    if a.size == 0:                       # libvorbis dislikes empty streams
        a = np.zeros(max(1, int(sr) // 100), dtype=np.float32)
    q = float(min(1.0, max(0.0, quality)))
    with sf.SoundFile(os.fspath(path), mode="w", samplerate=int(sr), channels=1,
                      format="OGG", subtype="VORBIS", compression_level=1.0 - q) as f:
        block = 1 << 15                   # chunked writes: older libsndfile crashed on huge vorbis writes
        for i in range(0, a.size, block):
            f.write(a[i:i + block])


# ----------------------------------------------------------------------------------------------
# Resampling: windowed-sinc (Kaiser) polyphase, rational ratio
# ----------------------------------------------------------------------------------------------

_RS_ZEROS = 16          # sinc zero crossings each side
_RS_ROLLOFF = 0.92      # cutoff as a fraction of the lower Nyquist
_RS_BETA = 9.0          # Kaiser beta (~ -90 dB sidelobes)
_RS_CHUNK = 16384
_rs_cache: dict[tuple[int, int], tuple[np.ndarray, int]] = {}


def _rs_kernel(L: int, M: int, phases: np.ndarray | None = None):
    cutoff = 0.5 * min(1.0, L / M) * _RS_ROLLOFF          # cycles per input sample
    half = _RS_ZEROS / (2.0 * cutoff)                     # kernel half-width in input samples
    K0 = int(math.ceil(half))
    k = np.arange(-K0 + 1, K0 + 1, dtype=np.float64)       # tap offsets relative to floor(t)
    frac = (np.arange(L, dtype=np.float64) / L) if phases is None else phases
    d = frac[:, None] - k[None, :]
    w = np.zeros_like(d)
    inside = np.abs(d) < half
    w[inside] = np.i0(_RS_BETA * np.sqrt(1.0 - (d[inside] / half) ** 2)) / np.i0(_RS_BETA)
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * d) * w
    h /= h.sum(axis=1, keepdims=True)                    # exact unity DC gain per phase
    return h.astype(np.float32), K0


def resample(x, sr_in, sr_out) -> np.ndarray:
    """Band-limited sample-rate conversion (windowed-sinc polyphase). Output length = round(n*sr_out/sr_in)."""
    a = _as_mono_f32(x)
    sr_in, sr_out = int(round(sr_in)), int(round(sr_out))
    if sr_in <= 0 or sr_out <= 0:
        raise ValueError("sample rates must be positive")
    if sr_in == sr_out or a.size == 0:
        return a.copy()
    g = math.gcd(sr_in, sr_out)
    L, M = sr_out // g, sr_in // g
    n_out = max(1, int(round(a.size * L / M)))
    use_table = L <= 4096
    if use_table:
        key = (L, M)
        if key not in _rs_cache:
            if len(_rs_cache) > 16:
                _rs_cache.clear()
            _rs_cache[key] = _rs_kernel(L, M)
        table, K0 = _rs_cache[key]
    else:
        K0 =int(math.ceil(_RS_ZEROS / (2.0 * 0.5 * min(1.0, L / M) * _RS_ROLLOFF)))
    K = 2 * K0
    xp = np.concatenate([np.zeros(K0, np.float32), a, np.zeros(K0 + 1, np.float32)])
    out = np.empty(n_out, dtype=np.float32)
    # Tap j of output n reads xp[base + 1 + j], base = floor(n*M/L) (xp carries K0 zeros in front).
    sw = np.lib.stride_tricks.sliding_window_view(xp, K)
    if use_table:
        # Outputs n0, n0+L, n0+2L, ... share one phase and step M input samples: a strided view
        # of the sliding windows times one kernel row -> no gather copies.
        for n0 in range(min(L, n_out)):
            base0 = (n0 * M) // L
            p = (n0 * M) % L
            cnt = (n_out - 1 - n0) // L + 1
            rows = sw[base0 + 1: base0 + 1 + (cnt - 1) * M + 1: M]
            out[n0::L] = rows @ table[p]
        return out
    for s in range(0, n_out, _RS_CHUNK):
        n = np.arange(s, min(n_out, s + _RS_CHUNK), dtype=np.int64)
        nm = n * M
        base = nm // L
        phase = nm % L
        frames = sw[np.minimum(base + 1, sw.shape[0] - 1)]
        wts, _ = _rs_kernel(L, M, phase.astype(np.float64) / L)
        out[s:s + n.size] = np.einsum("ij,ij->i", frames, wts)
    return out


# ----------------------------------------------------------------------------------------------
# Speech analysis (shared by auto_trim and LiveSilenceDetector)
# ----------------------------------------------------------------------------------------------

_MID_BAND = (120.0, 3800.0)      # main speech energy (excludes DC, rumble, most desk thumps)
_HI_BAND = (3800.0, 10000.0)     # fricatives ("s", "f", "sh")
_ANALYSIS_SR = 22050             # trim analysis rate (keeps fricative band up to ~10 kHz)
_VOICE_BAND = (150.0, 2000.0)    # band for periodicity analysis
_PITCH = (75.0, 450.0)           # Hz
_VOICED_THR = 0.65


def _next_pow2(n: int) -> int:
    return 1 << max(1, int(n - 1).bit_length())


class _FrameAnalyzer:
    """Computes per-frame band levels (dB) and voicing (0..1) for fixed-size frames."""

    def __init__(self, sr: int, win_e_ms: float = 20.0, win_v_ms: float = 40.0):
        self.sr = int(sr)
        self.win_e = max(16, int(round(sr * win_e_ms / 1000.0)))
        self.win_v = max(32, int(round(sr * win_v_ms / 1000.0)))
        self.nfft_e = _next_pow2(self.win_e)
        self.nfft_v = _next_pow2(2 * self.win_v)
        self.w_e = np.hanning(self.win_e + 2)[1:-1].astype(np.float64)
        self.w_v = np.hanning(self.win_v + 2)[1:-1].astype(np.float64)
        f_e = np.fft.rfftfreq(self.nfft_e, 1.0 / sr)
        nyq = 0.49 * sr
        self.mid_bins = (f_e >= _MID_BAND[0]) & (f_e < min(_MID_BAND[1], nyq))
        self.hi_bins = (f_e >= _HI_BAND[0]) & (f_e < min(_HI_BAND[1], nyq))
        self.scale_e = 2.0 / (self.nfft_e * float(np.sum(self.w_e ** 2)))
        f_v = np.fft.rfftfreq(self.nfft_v, 1.0 / sr)
        self.v_mask = (f_v >= _VOICE_BAND[0]) & (f_v <= _VOICE_BAND[1])
        self.min_lag = max(1, int(sr / _PITCH[1]))
        self.max_lag = min(self.win_v // 2, int(math.ceil(sr / _PITCH[0])))
        Wv = np.fft.rfft(self.w_v, self.nfft_v)
        rw = np.fft.irfft(np.abs(Wv) ** 2, self.nfft_v)[: self.max_lag + 1]
        self.rw = rw / rw[0]

    def levels(self, frames_e: np.ndarray):
        f = frames_e.astype(np.float64)
        f = f - f.mean(axis=1, keepdims=True)
        X = np.fft.rfft(f * self.w_e, self.nfft_e, axis=1)
        P = (X.real ** 2 + X.imag ** 2) * self.scale_e
        mid = P[:, self.mid_bins].sum(axis=1) if self.mid_bins.any() else np.zeros(len(f))
        hi = P[:, self.hi_bins].sum(axis=1) if self.hi_bins.any() else np.zeros(len(f))
        return (10.0 * np.log10(mid + _TINY), 10.0 * np.log10(hi + _TINY))

    def voicing(self, frames_v: np.ndarray) -> np.ndarray:
        f = frames_v.astype(np.float64)
        f = (f - f.mean(axis=1, keepdims=True)) * self.w_v
        X = np.fft.rfft(f, self.nfft_v, axis=1)
        P = X.real ** 2 + X.imag ** 2
        P[:, ~self.v_mask] = 0.0
        r = np.fft.irfft(P, self.nfft_v, axis=1)[:, : self.max_lag + 1]
        r0 = r[:, :1]
        rn = r / np.maximum(r0, 1e-20)
        rn = rn[:, self.min_lag:] / np.maximum(self.rw[self.min_lag:], 0.2)
        v = rn.max(axis=1)
        v[r0[:, 0] <= 1e-14] = 0.0
        return np.clip(v, 0.0, 1.0)


def _analysis_signal(x, sr):
    a = _as_mono_f32(x).astype(np.float64)
    a = np.nan_to_num(a)
    if a.size:
        a = a - a.mean()
    asr = int(sr)
    if asr > _ANALYSIS_SR:
        a = resample(a.astype(np.float32), asr, _ANALYSIS_SR).astype(np.float64)
        asr = _ANALYSIS_SR
    return a, asr


def _analyze(x, sr, hop_ms: float = 5.0):
    """Frame features of a whole buffer. Frame i is centred on sample i*hop (analysis rate)."""
    a, asr = _analysis_signal(x, sr)
    an = _FrameAnalyzer(asr)
    hop = max(1, int(round(asr * hop_ms / 1000.0)))
    n = a.size
    pad = an.win_v // 2 + 1
    if n > pad + 1:
        xp = np.pad(a, pad, mode="reflect")
    else:
        xp = np.pad(a, pad, mode="constant")
    n_frames = n // hop + 1
    sw_e = np.lib.stride_tricks.sliding_window_view(xp, an.win_e)
    sw_v = np.lib.stride_tricks.sliding_window_view(xp, an.win_v)
    off_e = pad - an.win_e // 2
    off_v = pad - an.win_v // 2
    fe = sw_e[off_e::hop][:n_frames]
    fv = sw_v[off_v::hop][:n_frames]
    n_frames = min(len(fe), len(fv))
    mid, hi = an.levels(fe[:n_frames])
    voi = an.voicing(fv[:n_frames])
    return mid, hi, voi, hop / asr, n / asr


def _robust_floor(levels_db: np.ndarray, pct: float = 10.0) -> float:
    if levels_db.size == 0:
        return -120.0
    live = levels_db[levels_db > _DIGITAL_SILENCE_DB]
    # digital silence (zeros) does not count as the floor unless it is most of the buffer
    src = live if live.size >= max(3, 0.5 * levels_db.size) else levels_db
    return float(max(np.percentile(src, pct), -120.0))


def _thresholds(sensitivity: float):
    s = float(min(1.0, max(0.0, sensitivity)))
    t_high = 15.0 - 8.0 * s           # dB over floor that marks a frame as certainly active
    t_low = 6.0 - 3.0 * s             # dB over floor where sound has decayed back to ~floor
    abs_min = -62.0 - 20.0 * s        # absolute dBFS gate (ignores dither in digital silence)
    weak_db = 18.0 + 12.0 * s         # voiced groups this far below the loudest one are background
    return s, t_high, t_low, abs_min, weak_db


def _snr_track(mid, hi, floor_mid, floor_hi, abs_min):
    snr = np.maximum(mid - floor_mid, hi - floor_hi - 3.0)
    level = 10.0 * np.log10(10.0 ** (mid / 10.0) + 10.0 ** (hi / 10.0))
    snr = np.where(level > abs_min, snr, np.minimum(snr, 0.0))
    return snr, level


def measure_noise_floor_db(x, sr) -> float:
    """Background noise level in dBFS (power, 120 Hz..8 kHz), estimated from the quietest frames."""
    a = _as_mono_f32(x)
    if a.size < max(16, int(sr * 0.02)):
        return -120.0
    mid, hi, _, _, _ = _analyze(a, sr)
    total = 10.0 * np.log10(10.0 ** (mid / 10.0) + 10.0 ** (hi / 10.0))
    return _robust_floor(total)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive (start, end) index pairs of True runs."""
    if mask.size == 0:
        return []
    m = np.concatenate([[False], mask.astype(bool), [False]])
    d = np.diff(m.astype(np.int8))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))


_EDGE_S = 0.6           # the start / stop keypress clicks live in the first / last ~0.6 s of a take


def _sustained(level: np.ndarray, n: int) -> np.ndarray:
    """Running minimum over n frames (centred): the level a sound holds for >= n frames."""
    if n <= 1 or level.size < n:
        return level.copy()
    h = n // 2
    p = np.pad(level, (h, n - 1 - h), mode="edge")
    return np.lib.stride_tricks.sliding_window_view(p, n).min(axis=1)


def _regions(low, high, voiced, power):
    """Hysteresis regions: runs of `low` containing >= 1 `high` frame.
    Each region: [start, end, peak_db, core_frames, voiced_frames, energy]."""
    out = []
    for st, en in _runs(low):
        core = int(high[st:en + 1].sum())
        if core == 0:
            continue
        p = power[st:en + 1]
        out.append([st, en, 10.0 * math.log10(float(p.max()) + _TINY), core,
                    int(voiced[st:en + 1].sum()), float(p.sum())])
    return out


def _padded(st, en, n_frames, hop_s, dur, pre_pad_ms, post_pad_ms):
    start_s = 0.0 if st == 0 else max(0.0, st * hop_s - 0.005)
    end_s = dur if en >= n_frames - 1 else min(dur, en * hop_s + 0.005)
    start_s = max(0.0, start_s - max(0.0, pre_pad_ms) / 1000.0)
    end_s = min(dur, end_s + max(0.0, post_pad_ms) / 1000.0)
    if end_s <= start_s:
        return 0.0, dur
    return float(start_s), float(end_s)


def auto_trim(x, sr, sensitivity: float = 0.5, pre_pad_ms: float = 40, post_pad_ms: float = 80) -> tuple[float, float]:
    """Locate the spoken words in a take. Returns (start_s, end_s) including padding, clamped to the buffer.

    Features: 20 ms frames / 5 ms hop at 22.05 kHz; speech-band (120-3800 Hz) and fricative-band
    (3.8-10 kHz) levels relative to an adaptive noise floor (10th percentile), plus periodicity (voicing).

    1. Reference = the loudest level that voiced sound holds for >= 60 ms. Frames ~50 dB under it count
       as silence even when a noise-suppressing mic puts the floor at -100 dBFS.
    2. Hysteresis regions (high threshold to trigger, low threshold to extend tails / soft onsets);
       speech = >= ~45 ms over the high threshold with voiced frames. Speech regions closer than
       350 ms form groups.
    3. The group with the most energy is the core of the utterance. Groups further away join only if
       they look like part of it: >= 8 % of the core's energy (20 % inside the first / last 0.6 s, where
       the start / stop keypress clicks live; 25 % beyond a 1 s pause), >= 60 ms of voicing and not far
       quieter. Key clicks / thocks, mouse clicks and chair creaks are dropped; multi-sentence calls with
       natural pauses stay whole.
    4. A weak part at the outer edge of the kept groups (>= 100 ms from the words, 12 dB quieter and
       < 5 % of the neighbouring word's energy: a click just before / after the words) is cut off.
    5. Short unvoiced bits close to the words (plosive releases, fricatives) are attached.
    Nothing speech-like -> the loudest sustained sound (+pads); only flat input (silence, steady noise)
    returns (0, len/sr).
    """
    a = _as_mono_f32(x)
    sr = int(sr)
    dur = a.size / sr if sr > 0 else 0.0
    if a.size == 0 or dur < 0.05:
        return 0.0, dur
    s, t_high, t_low, abs_min, weak_db = _thresholds(sensitivity)
    mid, hi, voi, hop_s, _ = _analyze(a, sr)
    floor_mid = _robust_floor(mid)
    floor_hi = _robust_floor(hi)
    snr, level = _snr_track(mid, hi, floor_mid, floor_hi, abs_min)
    if snr.size >= 3:                                  # light 3-frame smoothing (15 ms)
        snr = np.convolve(np.pad(snr, 1, mode="edge"), np.ones(3) / 3.0, mode="valid")
    n_frames = snr.size
    high0 = snr > t_high
    voiced0 = (voi > _VOICED_THR) & high0

    # 1. reference level
    sus = _sustained(level, max(1, int(round(0.06 / hop_s))))
    if voiced0.any():
        ref = float(sus[voiced0].max())
    elif high0.any():
        ref = float(sus[high0].max())
    else:
        return 0.0, dur                                # flat: silence or steady noise
    rel_db = 44.0 + 12.0 * s                           # 50 dB under the reference at s = 0.5
    high = high0 & (level > ref - (rel_db - 12.0))
    low = (snr > t_low) & (level > ref - rel_db)
    voiced = (voi > _VOICED_THR) & high
    power = np.where(low, 10.0 ** (np.minimum(level, 20.0) / 10.0), 0.0)

    # 2. regions -> speech / aux
    min_core = int(math.ceil(0.045 / hop_s))           # ~40 ms burst + window smear
    min_voiced = max(3, int(math.ceil(0.02 / hop_s)))
    regs = _regions(low, high, voiced, power)
    is_sp = [r[3] >= min_core and r[4] >= min_voiced for r in regs]
    speech = [r for r, f in zip(regs, is_sp) if f]
    aux = [r for r, f in zip(regs, is_sp) if not f]
    if not speech:
        # unvoiced (whispered) takes: long energetic regions
        long_min = int(math.ceil(0.15 / hop_s))
        speech = [r for r in aux if r[3] >= long_min and snr[r[0]:r[1] + 1].max() > t_high + 6.0]
        aux = [r for r in aux if r not in speech]
    if not speech:
        # nothing speech-like: the loudest sustained sound, if there clearly is one
        cand = [r for r in regs if snr[r[0]:r[1] + 1].max() > t_high + 6.0]
        if not cand:
            return 0.0, dur
        best = max(cand, key=lambda r: (r[3] >= min_core, r[5]))
        return _padded(best[0], best[1], n_frames, hop_s, dur, pre_pad_ms, post_pad_ms)

    merge_gap = int(round(0.35 / hop_s))
    groups: list[list] = []                            # [start, end, peak, energy, voiced, members]
    for r in sorted(speech):
        if groups and r[0] - groups[-1][1] <= merge_gap:
            g = groups[-1]
            g[1] = max(g[1], r[1])
            g[2] = max(g[2], r[2])
            g[3] += r[5]
            g[4] += r[4]
            g[5].append(r)
        else:
            groups.append([r[0], r[1], r[2], r[5], r[4], [r]])

    # 3. utterance core + the groups that belong to it (nearest first)
    main = max(groups, key=lambda g: g[3])
    e_main, p_main = main[3], main[2]
    edge_n = int(round(_EDGE_S / hop_s))
    far_n = int(round(1.0 / hop_s))
    min_vdur = max(min_voiced, int(round(0.06 / hop_s)))
    st, en = main[0], main[1]
    kept = [main]
    rest = [g for g in groups if g is not main]
    while rest:
        g = min(rest, key=lambda g: max(st - g[1], g[0] - en))
        gap = max(st - g[1], g[0] - en)
        at_edge = g[1] < edge_n or g[0] > n_frames - 1 - edge_n
        need = 0.20 if at_edge else 0.08
        if gap > far_n:
            need = max(need, 0.25)
        rest.remove(g)
        if gap <= merge_gap or (g[3] >= need * e_main and g[4] >= min_vdur and g[2] >= p_main - weak_db):
            kept.append(g)
            st, en = min(st, g[0]), max(en, g[1])

    # 4. a weak click just before / after the words, inside the outermost groups
    min_sep = int(round(0.10 / hop_s))
    for lead in (True, False):
        g = min(kept, key=lambda g: g[0]) if lead else max(kept, key=lambda g: g[1])
        mem = sorted(g[5])
        while len(mem) > 1:
            r, nb = (mem[0], mem[1]) if lead else (mem[-1], mem[-2])
            sep = nb[0] - r[1] if lead else r[0] - nb[1]
            if sep >= min_sep and r[2] <= nb[2] - 12.0 and r[5] < 0.05 * nb[5]:
                mem.remove(r)
            else:
                break
        if lead:
            st = max(st, mem[0][0])
        else:
            en = min(en, mem[-1][1])
    st0, en0 = st, en

    # 5. attach nearby short / unvoiced regions (plosive releases, isolated fricatives).
    # Weak fricatives are attached when (nearly) touching the speech; plosive releases after a stop
    # closure ("jump", "stop") up to 200 ms later. Breaths, clicks and thumps further away are left out.
    touch = int(round(0.04 / hop_s))
    pre_gap = int(round(0.10 / hop_s))
    post_gap = int(round(0.20 / hop_s))
    # Extension limits: consonants are short. Faint stuff (35+ dB under the words: breath, room
    # rumble running into the words) may add at most 80 / 120 ms.
    max_pre, max_post = int(round(0.25 / hop_s)), int(round(0.35 / hop_s))
    faint_pre, faint_post = int(round(0.08 / hop_s)), int(round(0.12 / hop_s))
    aux_floor = p_main - 40.0
    aux = [r for r in aux if r[1] < st or r[0] > en]
    changed = True
    while changed:
        changed = False
        for r in aux:
            # loud, long unvoiced-looking regions (creaky or noisy vowels) merge like speech
            strong = r[3] >= min_core and r[2] >= p_main - 20.0
            faint = r[2] < p_main - 35.0
            if r[1] < st and st0 - r[1] < max_pre:
                g = st - r[1]
                if g <= touch or (g <= pre_gap and r[2] >= aux_floor) or (strong and g <= merge_gap):
                    lim = st0 - (faint_pre if faint else max_pre)
                    if r[1] >= lim:
                        st, changed = max(r[0], lim), True
            elif r[0] > en and r[0] - en0 < max_post:
                g = r[0] - en
                if g <= touch or (g <= post_gap and r[2] >= aux_floor) or (strong and g <= merge_gap):
                    lim = en0 + (faint_post if faint else max_post)
                    if r[0] <= lim:
                        en, changed = min(r[1], lim), True
        aux = [r for r in aux if r[1] < st or r[0] > en]

    # 6. faint edges (room rumble / breath running into the words without a gap): keep at most
    # 80 ms before the first and 120 ms after the last frame within 35 dB of the reference.
    clear = np.flatnonzero(level[st:en + 1] >= ref - (29.0 + 12.0 * s))
    if clear.size:
        c0, c1 = st + int(clear[0]), st + int(clear[-1])
        st, en = max(st, c0 - faint_pre), min(en, c1 + faint_post)
    return _padded(st, en, n_frames, hop_s, dur, pre_pad_ms, post_pad_ms)


class LiveSilenceDetector:
    """Streaming end-of-utterance detector for hands-free recording.

    Calibrates the noise floor from the first ~300 ms, then tracks it slowly while nobody speaks.
    ``feed`` returns True once speech has started and has been followed by ``silence_ms`` of silence;
    it stays True until ``reset``.

    "Speech started" needs ``min_speech_ms`` (default 130 ms) of loud, voiced sound that holds its
    level (within 12 dB): a key click / thock is loud and can even ring periodically, but it decays
    within a few tens of ms, so the start / stop keypress never arms the detector (which would end
    the take before the user has said anything). Nothing in the first ``ignore_initial_ms`` (default
    250 ms: the keypress that started the recording) can arm it either. After the start, silence is
    anything 40 dB under the speech (or near the noise floor); short transients don't reset it.
    """

    CALIB_S = 0.3
    HOP_S = 0.01
    HOLD_DB = 12.0          # level range a voiced stretch may span and still count as sustained

    def __init__(self, sr, silence_ms=700, sensitivity=0.5, ignore_initial_ms=250, min_speech_ms=130):
        self.sr = int(sr)
        self.silence_ms = float(silence_ms)
        self.sensitivity = float(sensitivity)
        self.ignore_initial_ms = max(0.0, float(ignore_initial_ms))
        self.min_speech_ms = max(self.HOP_S * 1000.0 * 3, float(min_speech_ms))
        self._an = _FrameAnalyzer(self.sr)
        self._hop = max(1, int(round(self.sr * self.HOP_S)))
        self.reset()

    def reset(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self._calib_mid: list[float] = []
        self._calib_hi: list[float] = []
        self._floor_mid = None
        self._floor_hi = None
        n = max(3, int(round(self.min_speech_ms / 1000.0 / self.HOP_S)))
        self._win_lv = np.full(n, -200.0)          # last n frames: level, loud+voiced flags
        self._win_ok = np.zeros(n, dtype=bool)
        self._win_i = 0
        self._speech_db = -200.0
        self._loud_run = 0
        self._loud_voiced = 0
        self._silence = 0
        self.speech_started = False
        self.done = False
        self.frames_seen = 0

    @property
    def calibrated(self) -> bool:
        return self._floor_mid is not None

    def feed(self, block) -> bool:
        b = np.nan_to_num(_as_mono_f32(block))
        if self.done:
            return True
        if b.size:
            self._buf = np.concatenate([self._buf, b])
        an = self._an
        need = an.win_v
        if self._buf.size < need:
            return False
        n_fr = (self._buf.size - need) // self._hop + 1
        fv = np.lib.stride_tricks.sliding_window_view(self._buf, need)[::self._hop][:n_fr]
        off = need // 2 - an.win_e // 2
        fe = np.lib.stride_tricks.sliding_window_view(self._buf[off:], an.win_e)[::self._hop][:n_fr]
        mid, hi = an.levels(fe)
        voi = an.voicing(fv)
        self._buf = self._buf[n_fr * self._hop:].copy()
        for i in range(n_fr):
            if self._step(float(mid[i]), float(hi[i]), float(voi[i])):
                self.done = True
                return True
        return False

    def _step(self, mid, hi, voi) -> bool:
        self.frames_seen += 1
        s, t_high, t_low, abs_min, _ = _thresholds(self.sensitivity)
        if self._floor_mid is None:
            self._calib_mid.append(mid)
            self._calib_hi.append(hi)
            if len(self._calib_mid) * self.HOP_S >= self.CALIB_S:
                self._floor_mid = _robust_floor(np.array(self._calib_mid), 20.0)
                self._floor_hi = _robust_floor(np.array(self._calib_hi), 20.0)
            return False
        snr_a, lvl_a = _snr_track(np.array([mid]), np.array([hi]), self._floor_mid, self._floor_hi, abs_min)
        snr, level = float(snr_a[0]), float(lvl_a[0])
        # Floor tracking: fast down, slow up, only while quiet.
        if snr < t_low:
            for name, lv in (("_floor_mid", mid), ("_floor_hi", hi)):
                fl = getattr(self, name)
                if lv < fl:
                    fl += 0.3 * (lv - fl)
                else:
                    fl += 0.01 * (lv - fl)
                setattr(self, name, max(fl, -120.0))
        voiced = voi > _VOICED_THR
        if not self.speech_started:
            # time since the first fed sample (frame centre) -> keypress window
            t_ms = (self.frames_seen - 1) * self.HOP_S * 1000.0 + 0.5 * self._an.win_v / self.sr * 1000.0
            i = self._win_i % self._win_lv.size
            self._win_lv[i] = level
            self._win_ok[i] = snr > t_high and voiced and t_ms >= self.ignore_initial_ms
            self._win_i += 1
            ok = self._win_ok
            if ok.mean() >= 0.75 and ok.any():
                lv = self._win_lv[ok]
                if lv.max() - lv.min() <= self.HOLD_DB:
                    self.speech_started = True
                    self._speech_db = float(np.median(lv))
                    self._silence = 0
            return False
        if voiced and snr > t_high:
            self._speech_db = max(self._speech_db, level - 3.0)
        loud = snr > 0.5 * (t_low + t_high) and level > self._speech_db - (34.0 + 12.0 * s)
        if loud:
            self._loud_run += 1
            self._loud_voiced += int(voiced)
            # speech again (voiced, or a sustained fricative) - not a click / thump
            if self._loud_run >= 3 and (self._loud_voiced >= 2 or self._loud_run >= 8):
                self._silence = 0
                return False
        else:
            self._loud_run = 0
            self._loud_voiced = 0
        self._silence += 1
        return self._silence * self.HOP_S * 1000.0 >= self.silence_ms


# ----------------------------------------------------------------------------------------------
# Effects
# ----------------------------------------------------------------------------------------------

@dataclass
class EffectSettings:
    enabled: bool = True
    highpass_hz: float = 0.0          # 0 = off
    lowpass_hz: float = 0.0           # 0 = off
    filter_order: int = 4             # Butterworth order of each of HP / LP (2, 4, 6, 8)
    drive: float = 0.0                # 0..1 tanh saturation
    bitcrush_bits: int = 0            # 0 = off, else 2..16
    compression: float = 0.0          # 0..1
    noise_db: float | None = None     # None = off, else hiss bed RMS in dB re. the speech peak
    squelch: bool = False             # key-up click + squelch tail at start / end
    squelch_db: float = -20.0         # squelch burst RMS in dB re. the speech peak
    reverb: float = 0.0               # 0..1 small "helmet" room
    gain_db: float = 0.0              # extra gain; with normalize on it offsets the RMS target
    normalize: bool = True            # RMS-normalise the speech part to target_rms_db
    target_rms_db: float = -23.0      # dBFS RMS of the active (speech) part
    peak_ceiling_db: float = -3.0     # limiter ceiling (dBFS) applied after RMS normalisation

    def to_dict(self) -> dict:
        d = asdict(self)
        v = d["noise_db"]
        if v is not None and not math.isfinite(v):
            d["noise_db"] = None
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "EffectSettings":
        out = cls()
        if not isinstance(d, dict):
            return out
        if "normalize" not in d and "normalize_db" in d:      # legacy peak-normalize key
            out.normalize = d["normalize_db"] is not None
        for f in fields(cls):
            if f.name not in d:
                continue
            v = d[f.name]
            try:
                setattr(out, f.name, _coerce_field(f.name, v, getattr(out, f.name)))
            except (TypeError, ValueError):
                pass                        # keep default on garbage
        return out


def _coerce_field(name: str, v: Any, default: Any):
    if name in ("enabled", "squelch", "normalize"):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    if name == "noise_db":
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none", "off", "-inf", "inf")):
            return None
        fv = float(v)
        return fv if math.isfinite(fv) else None
    if name in ("filter_order", "bitcrush_bits"):
        iv = int(round(float(v)))
        if name == "filter_order":
            return int(min(8, max(2, iv + (iv % 2))))
        return 0 if iv <= 0 else int(min(16, max(2, iv)))
    fv = float(v)
    if not math.isfinite(fv):
        raise ValueError
    if name in ("drive", "compression", "reverb"):
        return min(1.0, max(0.0, fv))
    if name in ("highpass_hz", "lowpass_hz"):
        return max(0.0, fv)
    if name == "target_rms_db":
        return min(-6.0, max(-60.0, fv))
    if name == "peak_ceiling_db":
        return min(0.0, max(-30.0, fv))
    return fv


PRESETS: dict[str, EffectSettings] = {
    "Clean": EffectSettings(),
    "Helmet Intercom": EffectSettings(highpass_hz=300, lowpass_hz=3800, filter_order=4, drive=0.15,
                                      compression=0.45, reverb=0.3, noise_db=-60.0),
    "Rally Radio": EffectSettings(highpass_hz=400, lowpass_hz=3000, filter_order=6, drive=0.35,
                                  compression=0.6, noise_db=-42.0, squelch=True, squelch_db=-20.0),
    "Old CB": EffectSettings(highpass_hz=500, lowpass_hz=2500, filter_order=6, drive=0.75,
                             bitcrush_bits=7, compression=0.5, noise_db=-33.0, squelch=True,
                             squelch_db=-15.0),
    "Walkie-Talkie": EffectSettings(highpass_hz=600, lowpass_hz=3200, filter_order=8, drive=0.5,
                                    compression=0.85, noise_db=-40.0, squelch=True, squelch_db=-18.0),
}


def _fast_len(n: int) -> int:
    """Smallest 5-smooth integer >= n (fast pocketfft sizes)."""
    n = max(2, int(n))
    best = 1 << (n - 1).bit_length()
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            q = p35
            while q < n:
                q *= 2
            best = min(best, q)
            p35 *= 3
        p5 *= 5
    return best


def _butter_sections(kind: str, f0: float, order: int, sr: int):
    """RBJ biquads (b, a) forming a Butterworth filter of the given even order."""
    order = max(2, int(order) + int(order) % 2)
    w0 = 2.0 * math.pi * min(f0, 0.49 * sr) / sr
    cw, sw = math.cos(w0), math.sin(w0)
    secs = []
    for k in range(1, order // 2 + 1):
        Q = 1.0 / (2.0 * math.cos((2 * k - 1) * math.pi / (2 * order)))
        alpha = sw / (2.0 * Q)
        if kind == "lp":
            b = ((1 - cw) / 2, 1 - cw, (1 - cw) / 2)
        else:
            b = ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2)
        a = (1 + alpha, -2 * cw, 1 - alpha)
        secs.append((np.array(b) / a[0], np.array(a) / a[0]))
    return secs


def _band_response(hp: float, lp: float, order: int, sr: int, nfft: int) -> np.ndarray | None:
    secs = []
    if hp and hp > 10.0:
        secs += _butter_sections("hp", hp, order, sr)
    if lp and lp < 0.48 * sr:
        secs += _butter_sections("lp", lp, order, sr)
    if not secs:
        return None
    w = 2.0 * np.pi * np.fft.rfftfreq(nfft)
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    H = np.ones_like(z1)
    for b, a in secs:
        H *= (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)
    return H


def _filter_fft(x: np.ndarray, sr: int, hp: float, lp: float, order: int, tail_s: float = 0.05) -> np.ndarray:
    """Causal IIR (Butterworth cascade) applied via its frequency response, length preserved."""
    n = x.size
    if n == 0:
        return x
    nfft = _fast_len(n + int(tail_s * sr) + 64)
    H = _band_response(hp, lp, order, sr, nfft)
    if H is None:
        return x
    return np.fft.irfft(np.fft.rfft(x, nfft) * H, nfft)[:n]


def _peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


def _drive(x: np.ndarray, amount: float) -> np.ndarray:
    pk = _peak(x)
    if pk <= 1e-9 or amount <= 0:
        return x
    g = 1.0 + 15.0 * amount ** 1.5
    bias = 0.12 * amount
    xn = x / pk
    y = (np.tanh(g * xn + bias) - math.tanh(bias)) / math.tanh(g)
    return y * pk


def _bitcrush(x: np.ndarray, bits: int) -> np.ndarray:
    pk = _peak(x)
    if bits <= 0 or pk <= 1e-9:
        return x
    q = float(2 ** (int(bits) - 1))
    return np.round(x / pk * q) / q * pk


def _compress(x: np.ndarray, sr: int, amount: float) -> np.ndarray:
    if amount <= 0 or x.size == 0:
        return x
    blk = max(1, int(sr * 0.002))
    nb = int(math.ceil(x.size / blk))
    xp = np.zeros(nb * blk)
    xp[: x.size] = x
    env = np.sqrt(np.mean(xp.reshape(nb, blk) ** 2, axis=1) + 1e-12)
    a_att = math.exp(-blk / (sr * 0.003))
    a_rel = math.exp(-blk / (sr * 0.060))
    sm = np.empty(nb)
    e = 0.0
    for i, v in enumerate(env.tolist()):       # ~500 iterations per second of audio
        c = a_att if v > e else a_rel
        e = c * e + (1.0 - c) * v
        sm[i] = e
    sm_db = 20.0 * np.log10(sm + 1e-12)
    thr = float(sm_db.max()) - (6.0 + 24.0 * amount)
    ratio = 1.0 + 9.0 * amount
    over = np.maximum(sm_db - thr, 0.0)
    gain_db = -over * (1.0 - 1.0 / ratio)
    centers = (np.arange(nb) + 0.5) * blk
    g = 10.0 ** (np.interp(np.arange(x.size), centers, gain_db) / 20.0)
    return x * g


def _room_ir(sr: int, amount: float, rng: np.random.Generator) -> np.ndarray:
    length = int(sr * (0.025 + 0.06 * amount))
    t = np.arange(length) / sr
    tau = 0.012 + 0.025 * amount
    ir = rng.standard_normal(length) * np.exp(-t / tau)
    ir[: int(sr * 0.0015)] *= np.linspace(0, 1, int(sr * 0.0015))       # no direct-sound overlap
    for ms, gain in ((1.9, 0.9), (3.1, -0.7), (4.6, 0.55), (6.3, -0.4), (8.7, 0.3)):
        i = int(sr * ms / 1000.0)
        if i < length:
            ir[i] += gain * 4.0
    ir = np.convolve(ir, np.ones(3) / 3.0, mode="same")                 # slightly damped walls
    return ir / (np.sqrt(np.sum(ir ** 2)) + 1e-12)


def _reverb(x: np.ndarray, sr: int, amount: float, rng: np.random.Generator) -> np.ndarray:
    if amount <= 0 or x.size == 0:
        return x
    ir = _room_ir(sr, amount, rng)
    nfft = _fast_len(x.size + ir.size)
    wet = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(ir, nfft), nfft)[: x.size]
    mix = 0.55 * amount
    return x * (1.0 - 0.3 * mix) + wet * mix


def _squelch_parts(sr: int, level: float, rng: np.random.Generator):
    """(pre, post) arrays: key-up click + short hiss / squelch tail ("kssht") + close click."""
    n_pre, n_post = int(sr * 0.045), int(sr * 0.13)
    pre = rng.standard_normal(n_pre) * level
    pre *= np.minimum(1.0, np.linspace(0.0, 4.0, n_pre)) * np.linspace(1.0, 0.35, n_pre)
    click = np.exp(-np.arange(int(sr * 0.004)) / (sr * 0.0007))
    click *= np.sign(rng.standard_normal(click.size)) * 0.5 + 0.5
    pre[: click.size] += click * min(1.0, level * 6.0)
    post = rng.standard_normal(n_post) * level * 1.4
    env = np.ones(n_post)
    rise = int(sr * 0.008)
    env[:rise] = np.linspace(0.3, 1.0, rise)
    fall = int(sr * 0.03)
    env[-fall:] = np.linspace(1.0, 0.0, fall) ** 2
    post *= env
    k = n_post - fall - click.size
    if k > 0:
        post[k:k + click.size] -= click * min(1.0, level * 5.0)
    return pre, post


def apply_effects(x, sr, settings: EffectSettings | None, seed: int = 0) -> np.ndarray:
    """Run the effect chain. Deterministic for a given seed. Squelch (if on) adds <= 45 ms before and
    130 ms after the input; otherwise the length is preserved."""
    a = np.nan_to_num(_as_mono_f32(x)).astype(np.float64)
    sr = int(sr)
    st = settings if settings is not None else EffectSettings()
    if not st.enabled or a.size == 0:
        return a.astype(np.float32)
    rng = np.random.default_rng(int(seed) & 0xFFFFFFFF)
    a = a - a.mean()                                          # DC removal
    hp = st.highpass_hz if st.highpass_hz and st.highpass_hz > 10 else 0.0
    lp = st.lowpass_hz if st.lowpass_hz and st.lowpass_hz < 0.48 * sr else 0.0
    order = int(st.filter_order or 4)
    has_band = bool(hp or lp)
    if has_band:
        a = _filter_fft(a, sr, hp, lp, order)
    nonlinear = False
    if st.drive > 0:
        a = _drive(a, st.drive)
        nonlinear = True
    if st.bitcrush_bits and st.bitcrush_bits > 0:
        a = _bitcrush(a, int(st.bitcrush_bits))
        nonlinear = True
    if nonlinear:
        a = a - a.mean()
        if has_band:                                          # tame distortion products outside the band
            a = _filter_fft(a, sr, hp * 0.8 if hp else 0.0, lp * 1.25 if lp else 0.0, 2)
    if st.compression > 0:
        a = _compress(a, sr, st.compression)
    if st.reverb > 0:
        a = _reverb(a, sr, st.reverb, rng)
    pk = _peak(a)
    if pk > 1e-9:
        a = a / pk                                            # internal reference: speech peak = 1
    noise_on = st.noise_db is not None and math.isfinite(st.noise_db)
    sp0, sp1 = 0, a.size                                      # speech span (excl. squelch) for loudness
    if st.squelch or noise_on:
        if st.squelch:
            lvl = 10.0 ** (float(st.squelch_db) / 20.0)
            pre, post = _squelch_parts(sr, lvl, rng)
            # Squelch/noise colour: radio band if filters on, else a generic voice band.
            shp, slp = (hp, lp) if has_band else (300.0, 3400.0)
            pre = _filter_fft(pre, sr, shp, slp, 4, tail_s=0.01)
            post = _filter_fft(post, sr, shp, slp, 4, tail_s=0.01)
            sp0, sp1 = pre.size, pre.size + a.size
            a = np.concatenate([pre, a, post])
        if noise_on:
            nz = rng.standard_normal(a.size)
            nhp, nlp = (hp, lp) if has_band else (0.0, 0.0)
            if nhp or nlp:
                nz = _filter_fft(nz, sr, nhp, nlp, max(2, order // 2))
            rms = math.sqrt(float(np.mean(nz ** 2))) + 1e-12
            # slow flutter (+-1.5 dB) so the bed isn't perfectly static
            t = np.arange(a.size) / sr
            flutter = 1.0 + 0.18 * np.sin(2 * np.pi * (0.7 + rng.random()) * t + rng.random() * 6.28)
            a = a + nz / rms * 10.0 ** (float(st.noise_db) / 20.0) * flutter
    gain = 10.0 ** (float(st.gain_db or 0.0) / 20.0)
    if st.normalize:
        a = _loudness_normalize(a, sr, sp0, sp1, float(st.target_rms_db) + float(st.gain_db or 0.0),
                                float(st.peak_ceiling_db))
    else:
        a = a * gain
        pk = _peak(a)
        if pk > 0.999:
            a = a * (0.999 / pk)
    return np.nan_to_num(a).astype(np.float32)


def active_rms_db(x, sr) -> float:
    """RMS (dBFS) over the active part: 10 ms frames within 35 dB of the loudest frame."""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    if a.size == 0:
        return -120.0
    blk = max(1, int(sr * 0.01))
    nb = max(1, a.size // blk)
    fr = a[: nb * blk].reshape(nb, blk) if a.size >= blk else a.reshape(1, -1)
    p = np.mean(fr ** 2, axis=1)
    pmax = float(p.max())
    if pmax <= 1e-14:
        return -120.0
    act = p[p >= pmax * 10 ** (-35 / 10)]
    return float(10.0 * np.log10(np.mean(act)))


def _limit(a: np.ndarray, sr: int, ceiling: float) -> np.ndarray:
    """Look-ahead peak limiter (1 ms blocks, ~2 ms attack via look-ahead, 50 ms release), then a hard
    safety scale so |a| <= ceiling holds exactly."""
    if a.size == 0 or _peak(a) <= ceiling:
        return a
    blk = max(1, int(sr * 0.001))
    nb = int(math.ceil(a.size / blk))
    xp = np.zeros(nb * blk)
    xp[: a.size] = np.abs(a)
    need = np.minimum(1.0, ceiling / np.maximum(xp.reshape(nb, blk).max(axis=1), 1e-12))
    la = 2
    padded = np.concatenate([np.ones(la), need, np.ones(la)])
    need = np.min(np.lib.stride_tricks.sliding_window_view(padded, 2 * la + 1), axis=1)
    rel = math.exp(-1.0 / 50.0)
    g = np.empty(nb)
    cur = 1.0
    for i, v in enumerate(need.tolist()):
        cur = v if v < cur else rel * cur + (1.0 - rel) * v
        g[i] = cur
    gs = np.interp(np.arange(a.size), (np.arange(nb) + 0.5) * blk, g)
    # interp could exceed the per-block minimum between block centres: take the lower of both
    gs = np.minimum(gs, np.repeat(g, blk)[: a.size])
    y = a * gs
    pk = _peak(y)
    if pk > ceiling:
        y *= ceiling / pk
    return y


def _loudness_normalize(a, sr, sp0, sp1, target_db, ceiling_db):
    speech = a[sp0:sp1] if sp1 > sp0 else a
    cur = active_rms_db(speech, sr)
    if cur <= -110.0:
        return a
    ceiling = 10.0 ** (min(0.0, ceiling_db) / 20.0) * 0.995     # small margin for resampling overshoot
    y = a * 10.0 ** ((target_db - cur) / 20.0)
    for _ in range(3):                       # limiting lowers RMS: re-gain and re-limit
        y = _limit(y, sr, ceiling)
        short = target_db - active_rms_db(y[sp0:sp1] if sp1 > sp0 else y, sr)
        if short < 0.3:
            break
        y = y * 10.0 ** (short / 20.0)
    return _limit(y, sr, ceiling)


def render_take(x, sr, trim_start, trim_end, settings, out_sr=48000, fade_ms=8, seed=0) -> np.ndarray:
    """crop -> fade in/out -> effects (incl. RMS normalize + peak ceiling) -> resample -> final peak safety.

    Never returns an empty array (an empty crop yields 10 ms of silence)."""
    a = _as_mono_f32(x)
    sr = int(sr)
    out_sr = int(out_sr or sr)
    n = a.size
    i0 = int(round(max(0.0, float(trim_start)) * sr))
    i1 = int(round(max(0.0, float(trim_end)) * sr))
    i0, i1 = min(i0, n), min(max(i1, 0), n)
    if i1 <= i0:
        return np.zeros(max(1, out_sr // 100), dtype=np.float32)
    seg = a[i0:i1].astype(np.float64)
    nf = min(int(sr * max(0.0, fade_ms) / 1000.0), seg.size // 2)
    if nf > 0:
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, nf))
        seg[:nf] *= ramp
        seg[-nf:] *= ramp[::-1]
    y = apply_effects(seg.astype(np.float32), sr, settings, seed=seed)
    y = resample(y, sr, out_sr)
    y = np.nan_to_num(y)
    st = settings if settings is not None else EffectSettings()
    limit = 0.999
    if st.enabled and st.normalize:
        limit = min(limit, 10.0 ** (min(0.0, float(st.peak_ceiling_db)) / 20.0))
    pk = _peak(y)
    if pk > limit:                         # resampling overshoot -> scale back under the ceiling
        y = y * (limit / pk)
    if y.size == 0:
        y = np.zeros(max(1, out_sr // 100), dtype=np.float32)
    return y.astype(np.float32)


def peaks_for_display(x, n_buckets) -> tuple[np.ndarray, np.ndarray]:
    """(mins, maxs) per bucket for waveform drawing. Buckets beyond a short signal repeat samples."""
    a = _as_mono_f32(x)
    nb = max(0, int(n_buckets))
    if nb == 0:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    if a.size == 0:
        return np.zeros(nb, np.float32), np.zeros(nb, np.float32)
    edges = np.linspace(0, a.size, nb + 1)
    starts = np.minimum(np.floor(edges[:-1]).astype(np.int64), a.size - 1)
    mins = np.minimum.reduceat(a, starts)
    maxs = np.maximum.reduceat(a, starts)
    # reduceat with non-increasing next index returns a single element; that's fine for display.
    return mins.astype(np.float32), maxs.astype(np.float32)
