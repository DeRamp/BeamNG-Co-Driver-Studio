import random

import numpy as np
import pytest

from cds_helpers import SR, tone
from codriver_studio import sample_calls as sc
from codriver_studio.catalog import load_catalog
from codriver_studio.project import Project


@pytest.fixture
def cat():
    return load_catalog()


@pytest.fixture
def proj(tmp_path):
    p = Project.create(tmp_path / "p", "Calls")
    for pid in ("150", "three_left", "into", "hard_right", "dont_cut", "and", "crest", "50"):
        p.add_take(pid, tone(dur=0.3, lead=0.0, tail=0.0), SR, auto_trim=False)
    p.pack.link_word_gap_ms = 0
    p.pack.corner_gap_ms = 0
    p.pack.phrase_gap_ms = 0
    return p


def seg_len(p, pid, sr=48000):
    y, _ = p.render_take(pid, 0, out_sr=sr)
    return len(y)


def MS(v):
    return int(round(v * 48000 / 1000))


# ------------------------------------------------------------------ parse_call
@pytest.mark.parametrize("text,ids", [
    ("150 three left into hard right don't cut", ["150", "three_left", "into", "hard_right", "dont_cut"]),
    ("Hairpin Right!", ["hairpin_right"]),
    ("50, crest.", ["50", "crest"]),
    ("100 3 left and 4 right", ["100", "three_left", "and", "four_right"]),
    ("1.5km keep left over crest", ["1_point_5_km", "keep_left_over_crest"]),
    ("1.25 km", ["1_point_2_5_km"]),
    ("tightens and opens", ["tightens_and_opens"]),          # longest match beats tightens + and + opens
    ("dont cut late", ["dont_cut_late"]),
    ("five", ["system_countdown5_1"]),                       # only a system phrase has this text
    ("three", ["system_countdown3_1"]),
    ("three left over crest", ["three_left", "over_crest_corner"]),
    ("three left long over crest 100", ["three_left", "long", "over_crest_corner", "100"]),
    ("over crest", ["over_crest"]),
    ("jump over crest", ["jump", "over_crest"]),
])
def test_parse_call(cat, text, ids):
    got, unknown = sc.parse_call(cat, text)
    assert got == ids and unknown == []


def test_parse_call_unknown_words(cat):
    ids, unknown = sc.parse_call(cat, "100 banana left flibber")
    assert ids == ["100", "left"]
    assert unknown == ["banana", "flibber"]
    assert sc.parse_call(cat, "") == ([], [])


def test_every_phrase_text_parses_to_itself(cat):
    for p in cat.phrases:
        ids, unknown = sc.parse_call(cat, p.text)
        assert unknown == [], p.text
        if not p.is_system and p.id != "over_crest_corner":  # that one needs a corner before it
            assert ids == [p.id], p.text


# ------------------------------------------------------------------ random_call
ORDER = {"links": 0, "corners": 1, "length": 2, "shape": 3, "mod": 4, "distances": 5}


def _slot(cat, pid):
    c = cat.get(pid).category
    if c == "corner modifiers":
        return "length" if pid in sc.LENGTH_IDS else "shape"
    return c if c in ("links", "corners", "distances") else "mod"


@pytest.mark.parametrize("rng", [random.Random(1), np.random.default_rng(1)])
def test_random_call_follows_game_order(cat, rng):
    seen_link, seen_cornerless, seen_dist, lengths = False, False, False, set()
    for _ in range(500):
        call = sc.random_call(cat, rng)
        assert call and all(pid in cat for pid in call)
        assert not any(cat.get(pid).is_system for pid in call)
        slots = [_slot(cat, pid) for pid in call]
        ranks = [ORDER[x] for x in slots]
        assert ranks == sorted(ranks), call          # [into|and] corner [len] [shape] [mods] [dist]
        for x in ("links", "corners", "length", "shape", "distances"):
            assert slots.count(x) <= 1, call
        if "distances" in slots:
            assert slots[-1] == "distances"          # distance ENDS a note
            seen_dist = True
        if "links" in slots:
            assert slots[0] == "links" and "corners" in slots
            seen_link = True
        if "corners" not in slots:
            assert "crests & jumps" in {cat.get(p).category for p in call}
            seen_cornerless = True
        lengths.add(len(call))
    assert seen_link and seen_cornerless and seen_dist and len(lengths) >= 3


def test_random_call_deterministic(cat):
    a = [sc.random_call(cat, random.Random(7)) for _ in range(3)]
    b = [sc.random_call(cat, random.Random(7)) for _ in range(3)]
    assert a == b


# ------------------------------------------------------------------ compose
def test_compose_defaults_48k_and_zero_gaps(proj):
    ids = ["150", "three_left", "into", "hard_right"]
    y, sr, missing = sc.compose(proj, ids)
    assert sr == 48000 and missing == [] and y.dtype == np.float32
    assert len(y) == sum(seg_len(proj, i) for i in ids)


def test_gap_rules_like_game(proj):
    pk = proj.pack
    pk.link_word_gap_ms, pk.corner_gap_ms, pk.phrase_gap_ms, pk.pacenote_gap_ms = 100, 150, 70, 300
    g = sc.gap_ms_after
    assert g(proj, "into", "hard_right") == 100            # after a link word
    assert g(proj, "three_left", "into") == 70              # before a link word: ordinary clip gap
    assert g(proj, "three_left", "long") == 150             # corner → length
    assert g(proj, "long", "tightens") == 150               # length → shape
    assert g(proj, "three_left", "over_crest_corner") == 150
    assert g(proj, "three_left", "dont_cut") == 70          # corner → modifier
    assert g(proj, "three_left", "hard_right") == 150
    assert g(proj, "150", None) == 300 and g(proj, "into", None) == 100
    pk.corner_gap_ms = 900
    assert g(proj, "three_left", "long") == 200             # clamped like audioTiming.lua


def test_compose_positive_gaps(proj):
    pk = proj.pack
    pk.link_word_gap_ms, pk.corner_gap_ms, pk.phrase_gap_ms = 100, 50, 30
    ids = ["into", "three_left", "hard_right", "dont_cut", "150"]
    base = {i: seg_len(proj, i) for i in ids}
    y, _, _ = sc.compose(proj, ids)
    assert len(y) == sum(base.values()) + MS(100) + MS(50) + MS(30) + MS(30)
    off = base["into"] + MS(100) + base["three_left"]
    assert np.all(y[off:off + MS(50)] == 0)


def test_compose_negative_gap_is_plain_mix(proj):
    proj.pack.link_word_gap_ms = -40
    a = proj.render_take("into", 0, out_sr=48000)[0]
    b = proj.render_take("three_left", 0, out_sr=48000)[0]
    y, _, _ = sc.compose(proj, ["into", "three_left"])
    ov = MS(40)
    assert len(y) == len(a) + len(b) - ov
    expect = np.zeros(len(y), np.float32)
    expect[:len(a)] += a
    expect[len(a) - ov:] += b
    peak = np.max(np.abs(expect))
    if peak > 1:
        expect /= peak
    assert np.allclose(y, expect, atol=1e-6)                # sum, no crossfade


def test_compose_min_step_10ms(proj):
    proj.pack.link_word_gap_ms = -200
    proj.add_take("and", np.full(int(0.05 * SR), 0.1, np.float32), SR, auto_trim=False)
    proj.set_take_enabled("and", 0, False)                  # use the 50 ms take (index 1)
    n_and = len(proj.render_take("and", 1, out_sr=48000)[0])
    assert n_and < MS(200)
    b = seg_len(proj, "three_left")
    y, _, _ = sc.compose(proj, ["and", "three_left"])
    assert len(y) == max(MS(10) + b, n_and)


def test_compose_missing_skipped(proj):
    y, _, missing = sc.compose(proj, ["150", "hairpin_left", "not_a_phrase", "three_left"])
    assert missing == ["hairpin_left", "not_a_phrase"]
    assert len(y) == seg_len(proj, "150") + seg_len(proj, "three_left")
    y, sr, missing = sc.compose(proj, ["hairpin_left"])
    assert y.size == 0 and missing == ["hairpin_left"]


def test_compose_disabled_and_corrupt(proj):
    proj.set_take_enabled("crest", 0, False)
    (proj.folder / proj.takes("50")[0].file).write_bytes(b"junk")
    y, _, missing = sc.compose(proj, ["50", "crest", "150"])
    assert missing == ["50", "crest"] and len(y) == seg_len(proj, "150")


def test_compose_random_take_and_cache(proj):
    proj.add_take("crest", tone(880, dur=0.6, lead=0, tail=0), SR, auto_trim=False)
    proj.set_take_enabled("crest", 0, True)
    lens = {len(sc.compose(proj, ["crest"], rng=random.Random(s))[0]) for s in range(20)}
    assert len(lens) == 2  # both takes get picked
    cache = {}
    y1, _, _ = sc.compose(proj, ["150", "crest"], cache=cache)
    assert ("150", 0, 48000) in cache
    y2, _, _ = sc.compose(proj, ["150", "crest"], cache=cache)
    assert np.array_equal(y1, y2)


def test_compose_stage(proj):
    proj.pack.pacenote_gap_ms = 250
    proj.pack.phrase_gap_ms = 0
    y, sr, missing = sc.compose_stage(proj, [["three_left", "150"], ["hairpin_left"], ["crest"]])
    assert sr == 48000 and missing == ["hairpin_left"]
    assert len(y) == seg_len(proj, "three_left") + seg_len(proj, "150") + MS(250) + seg_len(proj, "crest")
    proj.pack.pacenote_gap_ms = -500                         # clamped to -100
    y2, _, _ = sc.compose_stage(proj, [["150"], ["crest"]])
    assert len(y2) == seg_len(proj, "150") + seg_len(proj, "crest") - MS(100)


def test_random_call_composes(proj, cat):
    rng = random.Random(3)
    notes = [sc.random_call(cat, rng) for _ in range(5)]
    y, sr, missing = sc.compose_stage(proj, notes)
    assert y.dtype == np.float32
