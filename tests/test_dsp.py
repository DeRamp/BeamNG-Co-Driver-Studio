import json
import math
import time

import numpy as np
import pytest

from codriver_studio import dsp

SR = 48000
TOL = 0.030


# ---------------------------------------------------------------------------------------------
# synthetic signals
# ---------------------------------------------------------------------------------------------

def syllable(sr, dur, f0=140.0, level_db=-12.0, vowel=0, fricative_ms=0.0, fric_pos="pre", seed=1):
    """Formant-shaped harmonic burst with optional "s"-like fricative before/after it."""
    rng = np.random.default_rng(seed)
    n = int(sr * dur)
    t = np.arange(n) / sr
    formants = [(700, 1200, 2600), (400, 2000, 2800), (300, 900, 2400), (550, 1700, 2500)][vowel % 4]
    f0_t = f0 * (1.0 + 0.03 * np.sin(2 * np.pi * 5.0 * t)) * np.linspace(1.05, 0.92, n)
    phase = 2 * np.pi * np.cumsum(f0_t) / sr
    y = np.zeros(n)
    for h in range(1, int(4000 / f0)):
        fh = h * f0
        amp = sum(1.0 / (1.0 + ((fh - F) / (80 + 0.1 * F)) ** 2) for F in formants) / h ** 0.3
        y += amp * np.sin(h * phase)
    env = np.clip(np.minimum(t / 0.025, (dur - t) / 0.04), 0, 1) ** 1.5
    y *= env
    y = y / (np.sqrt(np.mean(y ** 2)) + 1e-12) * 10 ** (level_db / 20)
    if fricative_ms > 0:
        nf = int(sr * fricative_ms / 1000)
        X = np.fft.rfft(rng.standard_normal(nf))
        ff = np.fft.rfftfreq(nf, 1 / sr)
        X[(ff < 3500) | (ff > 9000)] = 0
        fr = np.fft.irfft(X, nf)
        r = int(sr * 0.015)
        fe = np.ones(nf)
        fe[:r] = np.linspace(0, 1, r)
        fe[-r:] = np.linspace(1, 0, r)
        fr = fr * fe / (np.sqrt(np.mean((fr * fe) ** 2)) + 1e-12) * 10 ** ((level_db - 14) / 20)
        y = np.concatenate([fr, y]) if fric_pos == "pre" else np.concatenate([y, fr])
    return y


def noise(sr, dur, level_db, kind="white", seed=7):
    rng = np.random.default_rng(seed)
    n = int(sr * dur)
    w = rng.standard_normal(n)
    if kind == "pink":
        X = np.fft.rfft(w)
        f = np.fft.rfftfreq(n, 1 / sr)
        f[0] = f[1]
        w = np.fft.irfft(X / np.sqrt(f), n)
    return w / (np.sqrt(np.mean(w ** 2)) + 1e-12) * 10 ** (level_db / 20)


def place(sr, total_s, items, noise_db=None, noise_kind="white", seed=3):
    n = int(sr * total_s)
    y = np.zeros(n)
    for st, sig in items:
        i = int(round(st * sr))
        m = min(len(sig), n - i)
        y[i:i + m] += sig[:m]
    if noise_db is not None:
        y += noise(sr, total_s, noise_db, noise_kind, seed)[:n]
    return y.astype(np.float32)


def click(sr, level=0.6, ms=3.0):
    n = int(sr * ms / 1000)
    return level * np.exp(-np.arange(n) / (n / 4)) * np.sign(np.sin(np.arange(n) * 2.1))


def thump(sr, level=0.5):
    n = int(sr * 0.03)
    t = np.arange(n) / sr
    return level * np.sin(2 * np.pi * 90 * t) * np.exp(-t / 0.006)


def thock(sr, level=0.08, dur=0.3):
    """Mechanical key "thock": click + decaying, *periodic* body resonance (voiced-looking, ~-26 dB)."""
    t = np.arange(int(sr * dur)) / sr
    y = (np.sin(2 * np.pi * 190 * t) + 0.6 * np.sin(2 * np.pi * 380 * t + 1.0)
         + 0.3 * np.sin(2 * np.pi * 1150 * t)) * np.exp(-t / 0.045)
    y[: int(sr * 0.003)] += click(sr, 1.0)[: int(sr * 0.003)]
    return level * y


def breath(sr, dur=0.35, level_db=-38.0, seed=11):
    rng = np.random.default_rng(seed)
    n = int(sr * dur)
    X = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sr)
    X[(f < 500) | (f > 6000)] = 0
    b = np.fft.irfft(X, n) * np.hanning(n)
    return b / (np.sqrt(np.mean(b ** 2)) + 1e-12) * 10 ** (level_db / 20)


W1 = syllable(SR, 0.22, 130, -12, vowel=0)
W2 = syllable(SR, 0.30, 120, -14, vowel=1)


def assert_trim(x, truth, sens=0.5, pre=0.0, post=0.0, tol=TOL):
    st, en = dsp.auto_trim(x, SR, sens, pre_pad_ms=pre, post_pad_ms=post)
    exp_st = max(0.0, truth[0] - pre / 1000)
    exp_en = min(len(x) / SR, truth[1] + post / 1000)
    assert abs(st - exp_st) <= tol, f"start {st:.3f} vs {exp_st:.3f}"
    assert abs(en - exp_en) <= tol, f"end {en:.3f} vs {exp_en:.3f}"
    return st, en


# ---------------------------------------------------------------------------------------------
# auto_trim
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("snr", [40, 25, 15, 10])
@pytest.mark.parametrize("kind", ["white", "pink"])
def test_two_words_200ms_gap_one_region(snr, kind):
    x = place(SR, 2.0, [(0.6, W1), (0.6 + 0.22 + 0.2, W2)], noise_db=-12 - snr, noise_kind=kind)
    assert_trim(x, (0.6, 1.32))


def test_default_pads_applied():
    x = place(SR, 2.0, [(0.6, W1), (1.02, W2)], noise_db=-60)
    assert_trim(x, (0.6, 1.32), pre=40, post=80)


def test_long_phrase_with_600ms_pause_kept_whole():
    phrase = [(0.4 + i * 0.25, syllable(SR, 0.2, 130 + 5 * i, -12 - (i % 3), vowel=i)) for i in range(6)]
    last = 0.4 + 5 * 0.25 + 0.2 + 0.6
    x = place(SR, 4.0, phrase + [(last, W2)], noise_db=-55)
    assert_trim(x, (0.4, last + 0.30))


def test_trailing_600ms_silence_removed():
    phrase = [(0.3 + i * 0.25, syllable(SR, 0.2, 140, -12, vowel=i)) for i in range(5)]
    x = place(SR, 2.15, phrase, noise_db=-50)          # speech ends at 1.5 s, then ~650 ms of room noise
    assert_trim(x, (0.3, 1.5))


def test_click_and_thump_before_speech_ignored():
    x = place(SR, 2.0, [(0.15, click(SR)), (0.35, thump(SR)), (0.8, W1)], noise_db=-60)
    assert_trim(x, (0.8, 1.02))


def test_click_after_speech_ignored():
    x = place(SR, 2.0, [(0.5, W1), (1.4, click(SR, 0.9))], noise_db=-55)
    assert_trim(x, (0.5, 0.72))


def test_breath_well_before_speech_ignored():
    x = place(SR, 2.5, [(0.2, breath(SR)), (1.1, W1), (1.52, W2)], noise_db=-60)
    assert_trim(x, (1.1, 1.82))


def test_speech_touching_start_and_end():
    x = place(SR, 1.0, [(0.0, W1)], noise_db=-55)
    assert_trim(x, (0.0, 0.22), pre=40, post=80)
    x = place(SR, 1.0, [(0.7, W2)], noise_db=-55)
    st, en = assert_trim(x, (0.7, 1.0), pre=40, post=80)
    assert en == pytest.approx(1.0)


@pytest.mark.parametrize("pos", ["pre", "post"])
@pytest.mark.parametrize("noise_db,kind", [(-70, "white"), (-50, "white"), (-50, "pink")])
def test_soft_fricatives_kept(pos, noise_db, kind):
    s = syllable(SR, 0.3, 140, -12, fricative_ms=120, fric_pos=pos)
    x = place(SR, 2.0, [(0.5, s)], noise_db=noise_db, noise_kind=kind)
    assert_trim(x, (0.5, 0.92))


def test_clipped_input_with_dc_offset():
    x = np.clip(place(SR, 2.0, [(0.5, W1 * 20)], noise_db=-50), -1, 1) + 0.2
    assert_trim(x.astype(np.float32), (0.5, 0.72))


def test_44100_input():
    sr = 44100
    x = place(sr, 2.0, [(0.5, syllable(sr, 0.4, 180, -16, vowel=2))], noise_db=-55)
    st, en = dsp.auto_trim(x, sr, 0.5, 0, 0)
    assert abs(st - 0.5) <= TOL and abs(en - 0.9) <= TOL


def test_quiet_recording_level_independent():
    x = place(SR, 2.0, [(0.6, W1), (1.02, W2)], noise_db=-60) * 0.03   # ~ -30 dB quieter overall
    assert_trim(x, (0.6, 1.32))


def test_leading_digital_silence_does_not_break_floor():
    x = place(SR, 2.0, [(0.8, W1)], noise_db=-55)
    x[: int(0.05 * SR)] = 0.0
    assert_trim(x, (0.8, 1.02))


def test_sensitivity_monotonic():
    s = syllable(SR, 0.3, 140, -12, fricative_ms=100, fric_pos="post")
    x = place(SR, 2.0, [(0.5, s)], noise_db=-55)
    lo = dsp.auto_trim(x, SR, 0.0, 0, 0)
    hi = dsp.auto_trim(x, SR, 1.0, 0, 0)
    assert hi[1] - hi[0] >= lo[1] - lo[0] - 1e-9


def test_key_thock_far_from_words_dropped():
    """Start / stop keypress picked up by the mic: periodic, loud-ish, ~0.8 s away from the words."""
    x = place(SR, 3.2, [(0.12, thock(SR)), (0.3, thock(SR, 0.03)), (1.2, W1), (1.62, W2), (2.6, thock(SR))],
              noise_db=-80)
    assert_trim(x, (1.2, 1.92))


def test_sentences_with_pause_kept_whole_despite_clicks():
    a = [(0.9 + i * 0.25, syllable(SR, 0.2, 130 + 5 * i, -12 - (i % 3), vowel=i)) for i in range(5)]
    b0 = 0.9 + 4 * 0.25 + 0.2 + 0.45                                    # 450 ms sentence pause
    b = [(b0 + i * 0.25, syllable(SR, 0.2, 125 + 4 * i, -14, vowel=i + 1)) for i in range(4)]
    end = b0 + 3 * 0.25 + 0.2
    x = place(SR, end + 1.0, [(0.1, thock(SR))] + a + b + [(end + 0.6, thock(SR))], noise_db=-75)
    assert_trim(x, (0.9, end))


def test_only_a_click_returns_the_click_not_the_full_take():
    x = place(SR, 1.1, [(0.3, thock(SR))], noise_db=-80)
    st, en = dsp.auto_trim(x, SR, 0.5, 0, 0)
    assert 0.27 <= st <= 0.33 and 0.35 <= en <= 0.62


@pytest.mark.parametrize("x", [
    np.zeros(SR, np.float32),
    noise(SR, 2.0, -40).astype(np.float32),
    noise(SR, 2.0, -40, "pink").astype(np.float32),
    noise(SR, 2.0, -10).astype(np.float32),
], ids=["silence", "white", "pink", "loud-white"])
def test_no_speech_returns_full_range(x):
    assert dsp.auto_trim(x, SR) == (0.0, pytest.approx(len(x) / SR))


@pytest.mark.parametrize("n", [0, 1, 100, int(0.03 * SR), int(0.049 * SR), int(0.06 * SR)])
def test_very_short_input(n):
    x = (np.random.default_rng(0).standard_normal(n) * 0.1).astype(np.float32)
    st, en = dsp.auto_trim(x, SR)
    assert 0.0 <= st <= en <= n / SR + 1e-9


def test_nan_input_does_not_crash():
    x = place(SR, 1.0, [(0.3, W1)], noise_db=-60)
    x[100] = np.nan
    st, en = dsp.auto_trim(x, SR)
    assert 0 <= st < en <= 1.0


def test_noise_floor_measure():
    x = place(SR, 2.0, [(0.5, W1)], noise_db=-50)
    fl = dsp.measure_noise_floor_db(x, SR)
    assert -58 < fl < -48            # band-limited to 120 Hz-10 kHz of 24 kHz white noise ~ -53.8 dB
    assert dsp.measure_noise_floor_db(np.zeros(SR, np.float32), SR) <= -100


def test_auto_trim_speed():
    x = place(SR, 10.0, [(1.0 + i * 0.4, W1) for i in range(15)], noise_db=-50)
    t = time.perf_counter()
    dsp.auto_trim(x, SR)
    assert time.perf_counter() - t < 1.0


# ---------------------------------------------------------------------------------------------
# LiveSilenceDetector
# ---------------------------------------------------------------------------------------------

def feed_blocks(det, x, block=960):
    for i in range(0, len(x), block):
        if det.feed(x[i:i + block]):
            return (i + block) / SR
    return None


def test_live_detector_triggers_after_silence():
    x = place(SR, 3.0, [(0.6, W1), (1.02, W2)], noise_db=-55)       # speech ends 1.32 s
    det = dsp.LiveSilenceDetector(SR, silence_ms=700)
    t = feed_blocks(det, x)
    assert t is not None and 1.32 + 0.6 <= t <= 1.32 + 0.85
    assert det.feed(np.zeros(100, np.float32)) is True                 # latched
    det.reset()
    assert det.feed(np.zeros(960, np.float32)) is False


def test_live_detector_not_before_speech():
    det = dsp.LiveSilenceDetector(SR, silence_ms=500)
    assert feed_blocks(det, noise(SR, 3.0, -50).astype(np.float32)) is None
    det.reset()
    x = place(SR, 3.0, [(0.5, click(SR)), (1.2, thump(SR)), (1.8, breath(SR))], noise_db=-55)
    assert feed_blocks(det, x) is None
    assert not det.speech_started


def test_live_detector_word_gap_does_not_trigger():
    x = place(SR, 2.5, [(0.5, W1), (0.5 + 0.22 + 0.35, W2)], noise_db=-55)
    det = dsp.LiveSilenceDetector(SR, silence_ms=700)
    t = feed_blocks(det, x, block=441)
    assert t is None or t > 1.37 + 0.6


def test_live_detector_speech_during_calibration():
    x = place(SR, 3.0, [(0.1, W1), (0.5, W2)], noise_db=-55)            # starts inside the calibration window
    det = dsp.LiveSilenceDetector(SR, silence_ms=600)
    t = feed_blocks(det, x)
    assert t is not None and t < 2.2


def test_live_detector_not_armed_by_key_thock():
    """Keypress thock 0.2 s / 0.4 s after the start, 1.5 s of silence, then the words: no early stop."""
    for t_click in (0.2, 0.4):
        t_words = t_click + 0.3 + 1.5
        x = place(SR, t_words + 2.5, [(t_click, thock(SR)), (t_words, W1), (t_words + 0.42, W2)], noise_db=-80)
        det = dsp.LiveSilenceDetector(SR, silence_ms=700)
        t = feed_blocks(det, x)
        assert t is not None and t_words + 0.72 + 0.6 <= t <= t_words + 0.72 + 0.85, (t_click, t)


def test_live_detector_ignore_initial_ms():
    # a sustained vowel inside the keypress window does not arm the detector (outside it, it does)
    x = place(SR, 2.0, [(0.32, syllable(SR, 0.3, 140, -12))], noise_db=-60)
    det = dsp.LiveSilenceDetector(SR, silence_ms=500, ignore_initial_ms=700)
    assert feed_blocks(det, x) is None and not det.speech_started
    det = dsp.LiveSilenceDetector(SR, silence_ms=500)
    assert feed_blocks(det, x) is not None


# ---------------------------------------------------------------------------------------------
# resample
# ---------------------------------------------------------------------------------------------

def _tone(sr, f, dur=1.0, amp=0.5):
    return (amp * np.sin(2 * np.pi * f * np.arange(int(sr * dur)) / sr)).astype(np.float32)


def _spectrum_db(y, sr):
    w = np.hanning(len(y))
    Y = np.abs(np.fft.rfft(y * w)) / (np.sum(w) / 2)
    return np.fft.rfftfreq(len(y), 1 / sr), 20 * np.log10(Y + 1e-12)


@pytest.mark.parametrize("sr_in", [44100, 48000])
@pytest.mark.parametrize("f", [440.0, 1000.0, 5000.0, 9000.0])
def test_resample_preserves_frequency_and_level(sr_in, f):
    y = dsp.resample(_tone(sr_in, f), sr_in, 24000)
    assert y.dtype == np.float32 and len(y) == 24000
    fr, db = _spectrum_db(y[1000:-1000], 24000)
    k = int(np.argmax(db))
    assert abs(fr[k] - f) < 3.0
    rms = np.sqrt(np.mean(y[1000:-1000].astype(np.float64) ** 2))
    assert abs(20 * np.log10(rms / (0.5 / np.sqrt(2)))) < 0.1


@pytest.mark.parametrize("sr_in", [44100, 48000])
def test_resample_rejects_aliases(sr_in):
    y = dsp.resample(_tone(sr_in, 15000.0, amp=0.9), sr_in, 24000)
    assert 20 * np.log10(np.max(np.abs(y[200:-200])) / 0.9 + 1e-12) < -40


def test_resample_identity_empty_and_odd_lengths():
    x = np.random.default_rng(0).standard_normal(1001).astype(np.float32)
    y = dsp.resample(x, 48000, 48000)
    assert np.array_equal(x, y) and y is not x
    assert dsp.resample(np.zeros(0, np.float32), 48000, 24000).size == 0
    for n in (1, 2, 3, 147, 1001, 44101):
        y = dsp.resample(np.ones(n, np.float32) * 0.1, 44100, 24000)
        assert len(y) == max(1, round(n * 24000 / 44100))
        assert np.all(np.isfinite(y))
    y = dsp.resample(_tone(24000, 1000.0), 24000, 48000)                 # upsampling works too
    fr, db = _spectrum_db(y, 48000)
    assert abs(fr[np.argmax(db)] - 1000) < 3


def test_resample_dc_gain_unity():
    y = dsp.resample(np.full(44100, 0.25, np.float32), 44100, 24000)
    assert np.allclose(y[200:-200], 0.25, atol=1e-4)


# ---------------------------------------------------------------------------------------------
# effects
# ---------------------------------------------------------------------------------------------

SPEECH = place(SR, 3.0, [(0.3 + i * 0.3, syllable(SR, 0.25, 130, -20, vowel=i, seed=i)) for i in range(8)],
               noise_db=-62)


def test_effect_settings_roundtrip_and_tolerance():
    for name, st in dsp.PRESETS.items():
        d = json.loads(json.dumps(st.to_dict(), allow_nan=False))
        assert dsp.EffectSettings.from_dict(d) == st, name
    st = dsp.EffectSettings.from_dict({"drive": "0.4", "bogus": 1, "noise_db": "-inf", "squelch": "true",
                                       "bitcrush_bits": 99, "compression": 7, "lowpass_hz": "garbage"})
    assert st.drive == 0.4 and st.noise_db is None and st.squelch is True
    assert st.bitcrush_bits == 16 and st.compression == 1.0 and st.lowpass_hz == 0.0
    assert dsp.EffectSettings.from_dict({}) == dsp.EffectSettings()
    assert dsp.EffectSettings.from_dict(None) == dsp.EffectSettings()
    assert dsp.EffectSettings(noise_db=float("-inf")).to_dict()["noise_db"] is None
    assert dsp.PRESETS["Clean"] == dsp.EffectSettings()
    # legacy peak-normalize key from older project files
    assert dsp.EffectSettings.from_dict({"normalize_db": -1.0}).normalize is True
    assert dsp.EffectSettings.from_dict({"normalize_db": None}).normalize is False
    assert "normalize_db" not in dsp.EffectSettings().to_dict()
    d = dsp.EffectSettings.from_dict({"target_rms_db": -500, "peak_ceiling_db": 6})
    assert d.target_rms_db == -60.0 and d.peak_ceiling_db == 0.0


@pytest.mark.parametrize("name", list(dsp.PRESETS))
@pytest.mark.parametrize("level", [1.0, 0.05, 30.0])
def test_render_loudness_target_and_ceiling(name, level):
    """Official packs: RMS ~ -24 dBFS, peaks -3.5..-8 dBFS -> we aim at -23 RMS, -3 dBFS ceiling."""
    st = dsp.PRESETS[name]
    x = np.clip(SPEECH * level, -1, 1)
    y = dsp.render_take(x, SR, 0.2, 2.8, st)
    rms = dsp.active_rms_db(y[int(0.045 * SR): len(y) - int(0.13 * SR)] if st.squelch else y, SR)
    assert abs(rms - st.target_rms_db) <= 1.5, rms
    assert 20 * np.log10(np.max(np.abs(y))) <= -3.0 + 1e-3


@pytest.mark.parametrize("name", list(dsp.PRESETS))
def test_presets_finite_no_clip_deterministic(name):
    st = dsp.PRESETS[name]
    y1 = dsp.apply_effects(SPEECH, SR, st, seed=5)
    y2 = dsp.apply_effects(SPEECH, SR, st, seed=5)
    assert y1.dtype == np.float32 and np.all(np.isfinite(y1))
    assert np.max(np.abs(y1)) <= 1.0
    assert np.array_equal(y1, y2)
    if st.squelch or st.noise_db is not None:
        y3 = dsp.apply_effects(SPEECH, SR, st, seed=6)
        assert not np.array_equal(y1, y3)


def _band_fraction(y, sr, lo, hi):
    P = np.abs(np.fft.rfft(y)) ** 2
    f = np.fft.rfftfreq(len(y), 1 / sr)
    return P[(f >= lo) & (f < hi)].sum() / P.sum()


def test_presets_distinct_and_band_limited():
    outs = {n: dsp.apply_effects(SPEECH, SR, st, seed=0) for n, st in dsp.PRESETS.items()}
    clean = outs["Clean"]
    assert len(clean) == len(SPEECH)
    for n in ("Rally Radio", "Old CB", "Walkie-Talkie"):
        y = outs[n]
        assert len(SPEECH) < len(y) <= len(SPEECH) + int(0.3 * SR)       # squelch <= 150 ms per side
        assert _band_fraction(y, SR, 0, 200) < 0.01
        assert _band_fraction(y, SR, 5000, SR / 2) < 0.02
        # squelch makes the head non-silent
        assert np.sqrt(np.mean(y[: int(0.03 * SR)] ** 2)) > 10 ** (-40 / 20)
    assert _band_fraction(clean, SR, 0, 200) > _band_fraction(outs["Helmet Intercom"], SR, 0, 200)
    names = list(outs)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = outs[names[i]], outs[names[j]]
            m = min(len(a), len(b))
            assert not np.allclose(a[:m], b[:m], atol=1e-3), (names[i], names[j])


def test_disabled_and_edge_inputs():
    st = dsp.EffectSettings(enabled=False, drive=1.0)
    assert np.array_equal(dsp.apply_effects(SPEECH, SR, st), SPEECH)
    for name, st in dsp.PRESETS.items():
        assert np.all(np.isfinite(dsp.apply_effects(np.zeros(1000, np.float32), SR, st)))
        assert np.all(np.isfinite(dsp.apply_effects(np.zeros(0, np.float32), SR, st)))
        loud = np.clip(SPEECH * 100, -1, 1)
        assert np.max(np.abs(dsp.apply_effects(loud, SR, st))) <= 1.0
    nonorm = dsp.EffectSettings(normalize=False, gain_db=30.0)
    assert np.max(np.abs(dsp.apply_effects(SPEECH, SR, nonorm))) <= 1.0


def test_render_take_and_speed():
    st = dsp.PRESETS["Old CB"]
    heaviest = dsp.EffectSettings(**{**st.to_dict(), "reverb": 0.6, "compression": 0.9})
    dsp.render_take(SPEECH, SR, 0.0, 3.0, heaviest)                     # warm-up (kernel cache)
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        y = dsp.render_take(SPEECH, SR, 0.0, 3.0, heaviest, out_sr=24000)
        best = min(best, time.perf_counter() - t)
    print(f"render_take heaviest 3 s @48k: {best * 1000:.1f} ms")
    assert best < 0.3
    assert y.dtype == np.float32 and np.max(np.abs(y)) <= 0.999 + 1e-6
    assert 3.0 * 24000 <= len(y) <= 3.3 * 24000
    clean = dsp.render_take(SPEECH, SR, 0.5, 1.5, dsp.PRESETS["Clean"], out_sr=24000)
    assert len(clean) == 24000
    assert abs(clean[0]) < 1e-3 and abs(clean[-1]) < 1e-3                # fades
    assert len(dsp.render_take(SPEECH, SR, 2.0, 1.0, dsp.PRESETS["Clean"])) > 0   # empty crop -> silence
    y44 = dsp.render_take(SPEECH, SR, 0.0, 1.0, dsp.PRESETS["Clean"], out_sr=44100)
    assert len(y44) == 44100


# ---------------------------------------------------------------------------------------------
# IO + display
# ---------------------------------------------------------------------------------------------

def test_wav_ogg_roundtrip(tmp_path):
    x = dsp.render_take(SPEECH, SR, 0.2, 2.1, dsp.PRESETS["Rally Radio"], out_sr=24000)
    p = tmp_path / "a.ogg"
    dsp.write_ogg(p, x, 24000)
    y, sr = dsp.read_audio(p)
    assert sr == 24000 and y.dtype == np.float32 and y.ndim == 1
    assert abs(len(y) - len(x)) / sr < 0.005
    # Vorbis is lossy, but the waveform should still correlate strongly
    m = min(len(x), len(y))
    assert np.corrcoef(x[:m], y[:m])[0, 1] > 0.95
    w = tmp_path / "a.wav"
    dsp.write_wav(w, SPEECH, SR)
    z, sr2 = dsp.read_audio(w)
    assert sr2 == SR and len(z) == len(SPEECH)
    assert np.max(np.abs(z - SPEECH)) < 1e-6
    import soundfile as sf
    assert sf.info(str(w)).subtype == "PCM_24"
    # stereo files are mixed down
    st = tmp_path / "s.wav"
    sf.write(str(st), np.stack([SPEECH, -SPEECH * 0.5], axis=1), SR)
    m, _ = dsp.read_audio(st)
    assert m.ndim == 1 and np.allclose(m, SPEECH * 0.25, atol=1e-4)
    dsp.write_ogg(tmp_path / "empty.ogg", np.zeros(0, np.float32), 24000)   # must not crash
    long = np.tile(x, 10)
    dsp.write_ogg(tmp_path / "long.ogg", long, 24000, quality=0.3)
    ly, _ = dsp.read_audio(tmp_path / "long.ogg")
    assert abs(len(ly) - len(long)) / 24000 < 0.005


def test_peaks_for_display():
    x = np.sin(np.linspace(0, 20 * np.pi, 10000)).astype(np.float32)
    mn, mx = dsp.peaks_for_display(x, 100)
    assert len(mn) == len(mx) == 100 and np.all(mn <= mx)
    assert mx.max() == pytest.approx(1.0, abs=1e-3) and mn.min() == pytest.approx(-1.0, abs=1e-3)
    mn, mx = dsp.peaks_for_display(x[:10], 100)
    assert len(mn) == 100
    mn, mx = dsp.peaks_for_display(np.zeros(0, np.float32), 50)
    assert len(mn) == 50 and not mn.any()
    assert len(dsp.peaks_for_display(x, 0)[0]) == 0
