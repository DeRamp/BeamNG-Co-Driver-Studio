import numpy as np

LINK_IDS = {"and", "into"}


def _first_enabled(project, pid):
    for i, t in enumerate(project.takes(pid)):
        if t.enabled:
            return i
    return None


def compose(project, phrase_ids, out_sr=24000, rng=None):
    parts, missing = [], []
    for pid in phrase_ids:
        i = _first_enabled(project, pid)
        if i is None:
            missing.append(pid)
            continue
        y, _ = project.render_take(pid, i, out_sr)
        parts.append(y)
        parts.append(np.zeros(int(out_sr * 0.05), np.float32))
    x = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return x, out_sr, missing


def random_call(catalog, rng):
    corners = [p.id for p in catalog.by_category("corners")]
    dist = [p.id for p in catalog.by_category("distances")]
    out = []
    if rng.random() < 0.5:
        out.append(dist[_ri(rng, len(dist))])
    out.append(corners[_ri(rng, len(corners))])
    if rng.random() < 0.4:
        out += ["into", corners[_ri(rng, len(corners))]]
    return out


def parse_call(catalog, text):
    words = text.lower().split()
    by_text = {p.text.lower(): p.id for p in catalog.phrases if not p.is_system}
    ids, unknown, i = [], [], 0
    while i < len(words):
        for n in range(min(5, len(words) - i), 0, -1):
            cand = " ".join(words[i:i + n])
            if cand in by_text:
                ids.append(by_text[cand])
                i += n
                break
        else:
            unknown.append(words[i])
            i += 1
    return ids, unknown


def _ri(rng, n):
    return int(rng.integers(n)) if hasattr(rng, "integers") else int(rng.randrange(n))
