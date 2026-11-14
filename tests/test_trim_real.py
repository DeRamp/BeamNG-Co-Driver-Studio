"""auto_trim / LiveSilenceDetector on REAL recordings.

tests/data/real/*.wav are untouched takes of the user's own voice (Razer Kiyo Pro webcam mic, 44.1 kHz,
recorded with Space start/stop and ~120 ms pre-roll). Each starts with the mechanical Space-key
click / thock picked up by the mic, ~0.7-1 s before the words.

Hand-labelled word energy (first / last frame of the words, incl. plosive bursts and release tails):
    good_luck.wav     "Good luck!"     1.22 - 1.96 s   (user's manual trim 1.21 - 2.01)
    best_of_luck.wav  "Best of luck!"  1.30 - 2.29 s   (user's manual trim 1.20 - 2.39)
    five.wav          "five"           2.06 - 2.87 s   (a -55 dB lip click at 1.96 is not a word)

The composite set mixes cut-outs of that material (words, real key clicks, real room noise) and, when the
game is installed, official BeamNG co-driver clips; the ground truth comes from the construction.
"""
from __future__ import annotations

import glob
import os
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

from codriver_studio import dsp

if not hasattr(dsp, "_analyze"):        # conftest injected the fake dsp module
    pytest.skip("real dsp module required", allow_module_level=True)

DATA = Path(__file__).parent / "data" / "real"
if not all((DATA / f"{n}.wav").is_file() for n in ("good_luck", "best_of_luck", "five")):
    # The real voice recordings are kept out of the public repo; drop your own takes here to run these.
    pytest.skip("tests/data/real/*.wav not present", allow_module_level=True)
SR = 44100
PRE, POST = 40, 80                       # auto_trim default pads (ms)

# Allowed deviation of the trim points from the true word energy (seconds):
# start in [onset - START_EARLY, onset + START_LATE], end in [offset - END_EARLY, offset + END_LATE]
START_EARLY, START_LATE = 0.10, 0.02
END_EARLY, END_LATE = 0.03, 0.16

TAKES = {
    #  file              words (s)       user's manual trim   key click / thock (s)
    "good_luck": dict(words=(1.22, 1.96), manual=(1.211, 2.014), click=(0.24, 0.52)),
    "best_of_luck": dict(words=(1.30, 2.29), manual=(1.203, 2.395), click=(0.11, 0.56)),
    "five": dict(words=(2.06, 2.87), manual=(1.906, 2.958), click=(0.10, 0.36)),
}
NOISE_PARTS = {"good_luck": [(2.5, 4.4)], "best_of_luck": [(2.6, 3.2)], "five": [(0.45, 1.5), (3.05, 3.8)]}

BEAMNG = (r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\lua\ge\extensions\gameplay\rally"
          r"\compositors\voicepacks")


@lru_cache(maxsize=None)
def take(name):
    x, sr = dsp.read_audio(DATA / f"{name}.wav")
    assert sr == SR
    return x.astype(np.float64)


def cut(name, a, b):
    x = take(name)
    return x[int(round(a * SR)):int(round(b * SR))].copy()


# ---------------------------------------------------------------------------------------------
# material
# ---------------------------------------------------------------------------------------------

class Seg:
    """A piece of speech with known word energy at [on, off] (seconds, relative to the piece)."""

    def __init__(self, x, on, off, label):
        self.x, self.on, self.off, self.label = x, on, off, label

    @property
    def dur(self):
        return len(self.x) / SR


def user_word(name, margin=0.06):
    """The words of a real take, cut with `margin` of the take's own room tone on both sides."""
    on, off = TAKES[name]["words"]
    a, b = on - margin, off + margin
    return Seg(cut(name, a, b), on - a, off - a, name)


def user_click(name):
    a, b = TAKES[name]["click"]
    return cut(name, a, b)


@lru_cache(maxsize=None)
def room_noise():
    """The silent stretches of the takes, each scaled to a common RMS (a real room is steady; without
    this the loop would step between -90 and -100 dB, i.e. by 10 dB once boosted for the noisy cases)."""
    parts = [cut(n, a, b) for n, rr in NOISE_PARTS.items() for a, b in rr]
    ref = np.sqrt(np.mean(parts[0] ** 2))
    return np.concatenate([p * ref / np.sqrt(np.mean(p ** 2)) for p in parts])


def noise_bed(dur, seed):
    """Real room tone of the user's mic, looped from a random offset with 20 ms crossfades."""
    src = room_noise()
    rng = np.random.default_rng(seed)
    n = int(round(dur * SR))
    out = np.zeros(0)
    xf = int(0.02 * SR)
    ramp = np.linspace(0.0, 1.0, xf)
    while out.size < n + xf:
        k = int(rng.integers(0, src.size // 2))
        piece = src[k:].copy()
        if out.size:
            piece[:xf] = out[-xf:] * (1 - ramp) + piece[:xf] * ramp
            out = np.concatenate([out[:-xf], piece])
        else:
            out = piece
    return out[:n]


@lru_cache(maxsize=None)
def beamng_clip(rel):
    path = os.path.join(BEAMNG, rel)
    x, sr = dsp.read_audio(path)
    x = dsp.resample(x, sr, SR).astype(np.float64)
    nf = int(0.008 * SR)                                    # tiny fades: the clips are trimmed tight
    x[:nf] *= np.linspace(0, 1, nf)
    x[-nf:] *= np.linspace(1, 0, nf)
    b = int(0.01 * SR)
    n = len(x) // b
    r = 10 * np.log10(np.mean(x[:n * b].reshape(n, b) ** 2, axis=1) + 1e-15)
    act = np.flatnonzero(r > r.max() - 40.0)
    # official clips carry their own noise floor; match the user's speech level (RMS of the active part)
    gain = _speech_rms(user_word("good_luck").x) / _speech_rms(x)
    return Seg(x * gain, act[0] * 0.01, (act[-1] + 1) * 0.01, os.path.basename(rel))


def _speech_rms(x):
    return 10 ** (dsp.active_rms_db(x, SR) / 20)


def have_beamng():
    return os.path.isdir(BEAMNG)


def mix(total, items, noise_seed=0, gain_db=0.0, noise_gain_db=0.0):
    """items: (start_s, signal). Returns float32 take = gain * (room tone + items)."""
    y = noise_bed(total, noise_seed) * 10 ** (noise_gain_db / 20)
    for st, sig in items:
        i = int(round(st * SR))
        m = min(len(sig), len(y) - i)
        y[i:i + m] += sig[:m]
    return (y * 10 ** (gain_db / 20)).astype(np.float32)


def phrase(segs, gaps, gains_db=None):
    """Concatenate speech segments (room tone between them comes from the bed). Returns
    (items relative to phrase start, length, (onset, offset))."""
    items, t = [], 0.0
    gains_db = gains_db or [0.0] * len(segs)
    on = off = None
    for i, (s, g) in enumerate(zip(segs, gains_db)):
        items.append((t, s.x * 10 ** (g / 20)))
        if on is None:
            on = t + s.on
        off = t + s.off
        t += s.dur + (gaps[i] if i < len(gaps) else 0.0)
    return items, t, (on, off)


def case(segs, gaps=(), *, lead=1.2, tail=1.0, clicks_pre=(), clicks_post=(), gain_db=0.0,
         seg_gains=None, noise_gain_db=0.0, seed=0, click_gain_db=0.0):
    """clicks_pre: (click, time relative to take start) or (click, -gap) meaning the click ENDS `gap`
    seconds before the first word onset. clicks_post: (click, delay after the phrase end)."""
    items, plen, (on, off) = phrase(segs, gaps, seg_gains)
    total = lead + plen + tail
    out = [(lead + st, sig) for st, sig in items]
    cg = 10 ** (click_gain_db / 20)
    for c, t in clicks_pre:
        if t < 0:
            t = lead + on - (-t) - len(c) / SR
        out.append((t, c * cg))
    for c, d in clicks_post:
        out.append((lead + plen + d, c * cg))
    x = mix(total, out, noise_seed=seed, gain_db=gain_db, noise_gain_db=noise_gain_db)
    return x, (lead + on, lead + off)


def composite_cases():
    """[(id, x, truth)] built from the user's real material (+ BeamNG clips when installed)."""
    cases = []
    words = {n: user_word(n) for n in TAKES}
    clicks = {n: user_click(n) for n in TAKES}
    seed = 0

    def add(cid, x_truth):
        cases.append((cid, *x_truth))

    for n, w in words.items():
        own = clicks[n]
        other = clicks["best_of_luck" if n != "best_of_luck" else "good_luck"]
        seed += 1
        add(f"{n}-click-start", case([w], lead=1.1, clicks_pre=[(own, 0.12)], seed=seed))
        add(f"{n}-click-end", case([w], lead=0.5, tail=1.2, clicks_post=[(own, 0.7)], seed=seed))
        add(f"{n}-click-both", case([w], lead=1.0, tail=1.2, clicks_pre=[(own, 0.1)],
                                    clicks_post=[(other, 0.6)], seed=seed))
        add(f"{n}-double-click-start", case([w], lead=1.3, clicks_pre=[(own, 0.05), (other, 0.45)], seed=seed))
        add(f"{n}-click-150ms-before", case([w], lead=1.0, clicks_pre=[(own, -0.15)], seed=seed))
        add(f"{n}-gain-20dB", case([w], lead=1.1, clicks_pre=[(own, 0.12)], clicks_post=[(other, 0.7)],
                                   gain_db=-20, seed=seed))
        add(f"{n}-gain-10dB-loud-click", case([w], lead=1.0, clicks_pre=[(other, 0.15)], gain_db=-10,
                                              click_gain_db=6, seed=seed))
        add(f"{n}-noisy-room", case([w], lead=1.0, clicks_pre=[(own, 0.1)], clicks_post=[(own, 0.6)],
                                    noise_gain_db=30, seed=seed))
    gl, bl, fv = words["good_luck"], words["best_of_luck"], words["five"]
    c1, c2, c3 = clicks["good_luck"], clicks["best_of_luck"], clicks["five"]
    add("phrase-gl-bl-400", case([gl, bl], [0.4], clicks_pre=[(c1, 0.1)], clicks_post=[(c2, 0.6)], seed=11))
    add("phrase-fv-gl-bl", case([fv, gl, bl], [0.3, 0.5], clicks_pre=[(c2, 0.1)], clicks_post=[(c1, 0.7)],
                                seed=12))
    add("phrase-bl-fv-soft-end", case([bl, fv], [0.45], seg_gains=[0, -8], clicks_pre=[(c3, 0.15)],
                                      clicks_post=[(c2, 0.6)], seed=13))
    add("phrase-soft-start", case([fv, bl, gl], [0.35, 0.4], seg_gains=[-8, 0, -2], clicks_pre=[(c2, 0.1)],
                                  seed=14))
    add("phrase-gain-20dB", case([gl, fv, bl], [0.5, 0.3], gain_db=-20, clicks_pre=[(c1, 0.1)],
                                 clicks_post=[(c2, 0.6)], seed=15))

    if have_beamng():
        ak = "ak_2026-04-16/audio/pacenote_"
        sysd = "dirtwheel_2026-07-19/audio/pacenote_system_"
        sets = [
            ("resched", [sysd + "rescheduled_1.ogg"], []),
            ("resched+falseStart", [sysd + "rescheduled_1.ogg", sysd + "falseStart_1.ogg"], [0.45]),
            ("comms+belts", [sysd + "commsCheck_1.ogg", sysd + "beltsCheck_1.ogg"], [0.35]),
            ("1.5km+100+300", [ak + "1_point_5_km.ogg", ak + "100.ogg", ak + "300.ogg"], [0.3, 0.5]),
            ("warn30+precount", [sysd + "warningSeconds30_1.ogg", sysd + "precountdown_2.ogg"], [0.4]),
            ("40+2km", [ak + "40.ogg", ak + "2_km.ogg"], [0.5]),
            ("count5", [sysd + "countdown5_1.ogg"], []),
            ("go", [sysd + "countdowngo_1.ogg"], []),
        ]
        for i, (cid, rels, gaps) in enumerate(sets):
            segs = [beamng_clip(r) for r in rels]
            pre = [(c1, 0.1)] if i % 2 == 0 else [(c2, 0.12), (c3, 0.5)]
            post = [(c2, 0.65)] if i % 3 != 2 else []
            add(f"beamng-{cid}", case(segs, gaps, lead=1.1, tail=1.1, clicks_pre=pre, clicks_post=post,
                                      gain_db=-6.0 * (i % 3), seed=20 + i))
        segs = [beamng_clip(sysd + "warningSeconds15_1.ogg"), gl]
        add("beamng+user-mixed", case(segs, [0.4], clicks_pre=[(c1, 0.1)], clicks_post=[(c2, 0.6)], seed=40))
        add("beamng-click-150ms-before", case([beamng_clip(sysd + "commsCheck_1.ogg")],
                                              clicks_pre=[(c2, -0.15)], seed=41))
    return cases


def trim_errors(x, truth):
    st, en = dsp.auto_trim(x, SR, 0.5, PRE, POST)
    return st, en, st - truth[0], en - truth[1]


def trim_ok(x, truth):
    st, en, e0, e1 = trim_errors(x, truth)
    return (-START_EARLY <= e0 <= START_LATE) and (-END_EARLY <= e1 <= END_LATE), st, en


# ---------------------------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(TAKES))
def test_real_take_trims_to_words(name):
    x = take(name).astype(np.float32)
    on, off = TAKES[name]["words"]
    st, en = dsp.auto_trim(x, SR)
    assert on - 0.06 - 0.005 <= st <= on + START_LATE, f"start {st:.3f} vs words {on:.2f}"
    assert off - END_EARLY <= en <= off + 0.10 + 0.005, f"end {en:.3f} vs words {off:.2f}"


_CASES = composite_cases()


def test_composite_set_is_big_enough():
    assert len(_CASES) >= 30


@pytest.mark.parametrize("cid,x,truth", _CASES, ids=[c[0] for c in _CASES])
def test_composite(cid, x, truth):
    ok, st, en = trim_ok(x, truth)
    assert ok, f"{cid}: got {st:.3f}-{en:.3f}, words {truth[0]:.3f}-{truth[1]:.3f}"


@pytest.mark.parametrize("name", list(TAKES))
def test_click_only_take_is_not_full_range(name):
    """No words at all -> loudest sustained sound + pads, never the lazy full range."""
    x = mix(1.1, [(0.1, user_click(name))], noise_seed=3)
    st, en = dsp.auto_trim(x, SR)
    dur = len(x) / SR
    assert en - st < 0.75 * dur
    assert st <= 0.1 + 0.05 and en >= 0.1 + 0.04


def test_real_take_speed():
    x = np.concatenate([take("good_luck"), take("five")])[: 5 * SR].astype(np.float32)
    dsp.auto_trim(x, SR)
    t = time.perf_counter()
    for _ in range(3):
        dsp.auto_trim(x, SR)
    assert (time.perf_counter() - t) / 3 < 0.15          # ~25 ms here; loose bound for slow CI boxes


# ---------------------------------------------------------------------------------------------
# LiveSilenceDetector on real audio
# ---------------------------------------------------------------------------------------------

def run_detector(x, silence_ms=900, block=1024, skip_preroll=True, **kw):
    """Feed like the app does: blocks from the keypress on (the 120 ms pre-roll is not fed).
    Returns (speech-start time, fire time) in take time, or None."""
    x0 = int(0.12 * SR) if skip_preroll else 0
    det = dsp.LiveSilenceDetector(SR, silence_ms=silence_ms, **kw)
    started = None
    for i in range(x0, len(x), block):
        fired = det.feed(x[i:i + block])
        t = (i + block) / SR
        if det.speech_started and started is None:
            started = t
        if fired:
            return started, t
    return started, None


@pytest.mark.parametrize("name", list(TAKES))
@pytest.mark.parametrize("skip_preroll", [True, False])
def test_live_detector_real_takes(name, skip_preroll):
    on, off = TAKES[name]["words"]
    started, fired = run_detector(take(name).astype(np.float32), 700, skip_preroll=skip_preroll)
    assert started is not None and started > on, f"armed at {started} before the words ({on})"
    assert fired is not None and off + 0.6 <= fired <= off + 0.7 + 0.25


@pytest.mark.parametrize("name", list(TAKES))
@pytest.mark.parametrize("click_at", [0.0, 0.15, 0.25, 0.35, 0.5])
def test_live_detector_click_then_long_silence(name, click_at):
    """Start keypress click at various times after the keypress, then 1.5 s of room tone, then words:
    must not fire before the words and must fire after them."""
    w = user_word(name)
    t_words = 0.12 + click_at + 0.5 + 1.5
    x = mix(t_words + w.dur + 1.6, [(0.12 + click_at, user_click(name)), (t_words, w.x)], noise_seed=5)
    started, fired = run_detector(x, 700)
    assert fired is not None and fired > t_words + w.off + 0.5, (started, fired)
