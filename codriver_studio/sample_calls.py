"""Compose full pacenote calls from recorded takes for previewing (no Qt).

Timing mirrors the game (audioManager.lua ``_getAudioObjGapAfterMs`` / ``_audioObjEffectiveLen``):
each clip is followed by a gap and the next clip starts at ``start + max(len + gap, 10 ms)``.

  * after a link word ("into", "and"): ``link_word_gap_ms``
  * between two clips of the same corner call (corner → length → shape, i.e. categories
    "corners" / "corner modifiers"): ``corner_gap_ms``
  * between any other two clips of a note: ``phrase_gap_ms``
  * after the last clip of a note (``compose_stage``): ``pacenote_gap_ms``

A negative gap starts the next clip early while the previous keeps playing: a plain MIX
(sum), no crossfade. Gaps are clamped like the game (-200..200, pacenote -100..400 ms).
"""
from __future__ import annotations

import re

import numpy as np

from .project import clamp_gap_ms

__all__ = ["LINK_IDS", "compose", "compose_stage", "random_call", "parse_call", "normalize_text",
           "gap_ms_after"]

LINK_IDS = {"and", "into"}
CORNER_PART_CATEGORIES = {"corners", "corner modifiers"}
MIN_STEP_S = 0.01
LENGTH_IDS = ("short", "half_long", "long", "extra_long")


# ---------------------------------------------------------------------------- composing
def _category(catalog, pid):
    return catalog.get(pid).category if pid in catalog else None


def _is_link(catalog, pid) -> bool:
    return pid in LINK_IDS or _category(catalog, pid) == "links"


def _is_corner_part(catalog, pid) -> bool:
    return _category(catalog, pid) in CORNER_PART_CATEGORIES


def gap_ms_after(project, pid: str, next_pid: str | None) -> int:
    """Gap after clip ``pid`` when followed by ``next_pid`` in the same note
    (``next_pid=None`` → end of note → pacenote gap)."""
    pack, cat = project.pack, project.catalog
    if _is_link(cat, pid):
        return clamp_gap_ms("link_word_gap_ms", pack.link_word_gap_ms)
    if next_pid is None:
        return clamp_gap_ms("pacenote_gap_ms", pack.pacenote_gap_ms)
    if _is_corner_part(cat, pid) and _is_corner_part(cat, next_pid):
        return clamp_gap_ms("corner_gap_ms", pack.corner_gap_ms)
    return clamp_gap_ms("phrase_gap_ms", pack.phrase_gap_ms)


def _pick_take(project, pid, rng):
    takes = project.exportable_takes(pid) if pid in project.catalog else []
    if not takes:
        return None
    if rng is None or len(takes) == 1:
        return takes[0][0]
    return takes[_randint(rng, len(takes))][0]


def _render_note(project, phrase_ids, out_sr, rng, cache):
    segs, missing = [], []
    for pid in phrase_ids:
        idx = _pick_take(project, pid, rng)
        if idx is None:
            missing.append(pid)
            continue
        key = (pid, idx, out_sr)
        try:
            if cache is not None and key in cache:
                y = cache[key]
            else:
                y, _ = project.render_take(pid, idx, out_sr=out_sr)
                y = np.asarray(y, np.float32).reshape(-1)
                if cache is not None:
                    cache[key] = y
        except Exception:
            missing.append(pid)
            continue
        if y.size:
            segs.append((pid, y))
        else:
            missing.append(pid)
    return segs, missing


def _mix(placed, sr):
    """placed: list of (start_sample, audio). Sum into one buffer, peak-safe."""
    if not placed:
        return np.zeros(0, np.float32)
    n = max(s + len(y) for s, y in placed)
    out = np.zeros(n, np.float32)
    for s, y in placed:
        out[s:s + len(y)] += y
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 1.0:
        out /= peak
    return out


def _schedule(project, notes_segs, sr):
    """Place clips like the game's queue: next start = start + max(len + gap, 10 ms)."""
    placed, pos = [], 0
    min_step = int(round(MIN_STEP_S * sr))
    flat = []
    for segs in notes_segs:
        for i, (pid, y) in enumerate(segs):
            nxt = segs[i + 1][0] if i + 1 < len(segs) else None
            flat.append((pid, y, nxt))
    for k, (pid, y, nxt) in enumerate(flat):
        placed.append((pos, y))
        if k + 1 < len(flat):
            gap = int(round(gap_ms_after(project, pid, nxt) * sr / 1000.0))
            pos += max(len(y) + gap, min_step)
    return _mix(placed, sr)


def compose(project, phrase_ids, out_sr: int = 48000, rng=None, cache: dict | None = None):
    """One note: one enabled take per phrase, timed with the pack's gap settings.

    Uses the first enabled take, or a random enabled take if ``rng`` (``random.Random`` or
    ``numpy.random.Generator``) is given. ``cache`` (optional dict) memoises rendered takes
    keyed by (phrase_id, take_index, sr) — drop it when trims/effects change.
    Returns ``(audio float32, sr, missing_ids)``; missing/unreadable phrases are skipped
    (silent in the game too). Renders every take (~10-50 ms each): call off the GUI thread.
    """
    out_sr = int(out_sr)
    segs, missing = _render_note(project, list(phrase_ids), out_sr, rng, cache)
    return _schedule(project, [segs], out_sr), out_sr, missing


def compose_stage(project, notes, out_sr: int = 48000, rng=None, cache: dict | None = None):
    """Several notes joined with ``pacenote_gap_ms`` → ``(audio, sr, missing_ids)``."""
    out_sr = int(out_sr)
    all_segs, missing = [], []
    for note in notes:
        segs, miss = _render_note(project, list(note), out_sr, rng, cache)
        missing += miss
        if segs:
            all_segs.append(segs)
    return _schedule(project, all_segs, out_sr), out_sr, missing


# ---------------------------------------------------------------------------- random calls
def _randint(rng, n: int) -> int:
    if hasattr(rng, "integers"):
        return int(rng.integers(n))
    return int(rng.randrange(n))


def _rand(rng) -> float:
    return float(rng.random())


def _choice(rng, seq):
    seq = list(seq)
    return seq[_randint(rng, len(seq))] if seq else None


def _ids(catalog, category, keep=None):
    return [p.id for p in catalog.by_category(category) if keep is None or keep(p)]


def random_call(catalog, rng) -> list[str]:
    """A plausible note in game order ``[into|and] corner [length] [shape] [modifiers...]
    [distance]`` — e.g. ``into three left long tightens don't cut 150``, ``hairpin right``,
    ``crest 50``. ``rng``: ``random.Random`` or ``numpy.random.Generator``."""
    distances = _ids(catalog, "distances")
    short_d = [d for d in distances if "km" not in catalog.get(d).text] or distances
    corners = _ids(catalog, "corners", lambda p: p.text not in ("left", "right")) or _ids(catalog, "corners")
    lengths = [i for i in LENGTH_IDS if i in catalog]
    shapes = _ids(catalog, "corner modifiers", lambda p: p.id not in LENGTH_IDS)
    crests = _ids(catalog, "crests & jumps")
    hazards = _ids(catalog, "hazards")
    cuts = _ids(catalog, "cuts & lines", lambda p: p.id not in ("dont", "up", "down", "early", "late", "middle"))
    links = [i for i in ("into", "and") if i in catalog]

    call: list[str] = []
    if crests and _rand(rng) < 0.15:            # no corner: "crest 50", "big jump"
        call.append(_choice(rng, crests))
        if hazards and _rand(rng) < 0.3:
            call.append(_choice(rng, hazards))
    else:
        if links and _rand(rng) < 0.3:
            call.append(_choice(rng, links))
        if corners:
            call.append(_choice(rng, corners))
        if lengths and _rand(rng) < 0.25:
            call.append(_choice(rng, lengths))
        if shapes and _rand(rng) < 0.3:
            call.append(_choice(rng, shapes))
        if cuts and _rand(rng) < 0.25:
            call.append(_choice(rng, cuts))
        if hazards and _rand(rng) < 0.1:
            call.append(_choice(rng, hazards))
        if crests and _rand(rng) < 0.08:
            call.append(_choice(rng, crests))
    if distances and _rand(rng) < 0.6:          # distance ENDS a note
        call.append(_choice(rng, short_d if _rand(rng) < 0.85 else distances))
    call = [c for c in call if c is not None]
    if not call or all(_is_link(catalog, c) for c in call):
        call = [corners[0]] if corners else call
    return call


# ---------------------------------------------------------------------------- parsing
_WORD_DIGITS = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six"}
_SYNONYMS = {"kilometre": "km", "kilometres": "km", "kilometer": "km", "kilometers": "km",
             "k": "km", "kms": "km", "&": "and"}


def normalize_text(text: str) -> list[str]:
    """Lower-case word tokens; apostrophes dropped (don't → dont), punctuation ignored,
    decimals spelled the catalog way (``1.25km`` → ``1 point 2 5 km``)."""
    s = str(text).lower().replace("'", "").replace("’", "")
    s = re.sub(r"(\d+)[.,](\d+)", lambda m: f"{m.group(1)} point {' '.join(m.group(2))}", s)
    s = re.sub(r"(\d)([a-z])", r"\1 \2", s)
    s = re.sub(r"([a-z])(\d)", r"\1 \2", s)
    toks = re.findall(r"[a-z0-9]+|&", s)
    return [_SYNONYMS.get(t, t) for t in toks]


def _phrase_index(catalog):
    cache = getattr(catalog, "_cds_parse_index", None)
    if cache is not None:
        return cache
    index: dict[tuple, list[str]] = {}
    # non-system phrases first so they win over system ones with the same words ("three")
    for p in sorted(catalog.phrases, key=lambda p: p.is_system):
        key = tuple(normalize_text(p.text))
        if key:
            index.setdefault(key, []).append(p.id)
    maxlen = max((len(k) for k in index), default=1)
    cache = (index, maxlen)
    try:
        object.__setattr__(catalog, "_cds_parse_index", cache)
    except Exception:
        pass
    return cache


def _disambiguate(catalog, candidates: list[str], prev: str | None) -> str:
    """Same text, several phrases (e.g. "over crest"): right after a corner call prefer the
    corner-shape phrase ("corner modifiers"), otherwise the other one."""
    if len(candidates) == 1:
        return candidates[0]
    non_system = [c for c in candidates if not catalog.get(c).is_system] or candidates
    after_corner = prev is not None and _is_corner_part(catalog, prev)
    shape = [c for c in non_system if catalog.get(c).category == "corner modifiers"]
    other = [c for c in non_system if catalog.get(c).category != "corner modifiers"]
    if after_corner and shape:
        return shape[0]
    return (other or shape)[0]


def parse_call(catalog, text) -> tuple[list[str], list[str]]:
    """``"100 three left into hard right"`` → (phrase ids, unknown words). Greedy longest match.

    A lone digit 1-6 directly before left/right is read as the corner number (``3 left`` →
    ``three left``). "over crest" right after a corner → ``over_crest_corner``, else ``over_crest``.
    """
    index, maxlen = _phrase_index(catalog)
    toks = normalize_text(text)
    for i in range(len(toks) - 1):
        if toks[i] in _WORD_DIGITS and toks[i + 1] in ("left", "right"):
            toks[i] = _WORD_DIGITS[toks[i]]
    ids, unknown = [], []
    i = 0
    while i < len(toks):
        for n in range(min(maxlen, len(toks) - i), 0, -1):
            cands = index.get(tuple(toks[i:i + n]))
            if cands:
                ids.append(_disambiguate(catalog, cands, ids[-1] if ids else None))
                i += n
                break
        else:
            unknown.append(toks[i])
            i += 1
    return ids, unknown
