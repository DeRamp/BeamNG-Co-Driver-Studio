"""Regenerate codriver_studio/data/phrases_en.json from a BeamNG install.

Usage: python tools/build_catalog.py [BeamNG install dir]
"""
import json, re, sys, glob, os

GAME = sys.argv[1] if len(sys.argv) > 1 else r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"
RALLY = os.path.join(GAME, "lua/ge/extensions/gameplay/rally")
OUT = os.path.join(os.path.dirname(__file__), "..", "codriver_studio", "data", "phrases_en.json")

def pacenote_hash(text):  # mirrors compositorUtil.lua M.pacenoteHash
    h = text.lower().replace("'", "")
    h = re.sub(r"[^a-z0-9]", "_", h)
    h = re.sub(r"_+", "_", h).strip("_")
    return h

# system notes from styles/english/systemPacenotes.lua (required order)
SYSTEM_ORDER = ["precountdown","countdown5","countdown4","countdown3","countdown2","countdown1","countdowngo",
    "warningSeconds60","warningSeconds30","warningSeconds15","warningSeconds10","commsCheck","beltsCheck","rescheduled","falseStart"]
lua = open(os.path.join(RALLY, "compositors/styles/english/systemPacenotes.lua"), encoding="utf8").read()
system = []
for key in SYSTEM_ORDER:
    m = re.search(r"\b%s\s*=\s*(\{.*?\n  \},|\"[^\"]*\")" % key, lua, re.S)
    texts = re.findall(r'"([^"]*)"', m.group(1))
    for i, t in enumerate(texts, 1):
        system.append((key, i, t))

scripts = sorted(glob.glob(os.path.join(RALLY, "compositors/voicepacks/*/beamng-voicepack-script_*.txt")), key=os.path.getmtime)
script = scripts[-1]
script_hash = re.search(r"script_([0-9a-f]+)\.txt", script).group(1)
lines = [l.strip() for l in open(script, encoding="utf8") if l.strip()]
system_texts = {t for _, _, t in system}

KW = [
    ("links", lambda t: t in ("and", "into")),
    ("distances", lambda t: bool(re.fullmatch(r"[\d ]+(point [\d ]+)?(km)?", t))),
    ("corners", lambda t: re.search(r"\b(left|right)\b", t) and not t.startswith("keep") and "chicane" not in t),
    ("corner modifiers", lambda t: t in ("short","half long","long","extra long") or t.startswith(("opens","tightens")) or t in ("over crest",)),
    ("hazards", lambda t: t in ("care","caution","double caution","deceptive","sudden","slippy","narrows","nips","twisty")),
    ("crests & jumps", lambda t: re.search(r"crest|jump|kick|drop|bump|dip", t) and not t.startswith(("keep","brake","middle"))),
    ("cuts & lines", lambda t: re.search(r"cut|keep|wide|middle|early|late|\bdon't\b|chicane", t) or t in ("up","down")),
    ("landmarks", lambda t: re.search(r"junction|rock|tree|bridge|fence|house|wall|gate|tunnel", t)),
    ("speed & braking", lambda t: re.search(r"brave|brak|faster|slow|stop|turn|finish", t)),
    ("surface", lambda t: re.search(r"onto|water", t)),
]

phrases, seen = [], set()
for key, i, t in system:
    phrases.append({"id": f"system_{key}_{i}", "text": t, "category": "system",
                    "file_stem": f"pacenote_system_{key}_{i}", "system_key": key, "system_variant": i,
                    "single_take": True, "hint": None})

# "over crest" appears twice in the script: the corner shape (enthusiast.lua overCrest, variant "2")
# and the crest modifier (modifiers.lua, variant "1"). The game requests a fixed numbered file for
# each, so they are two separate single-take phrases.
FIXED = {
    ("over crest", 0): {"id": "over_crest_corner", "category": "corner modifiers", "file_stem": "pacenote_over_crest_2",
                        "hint": "as a corner shape, e.g. \"three left over crest\""},
    ("over crest", 1): {"id": "over_crest", "category": "crests & jumps", "file_stem": "pacenote_over_crest_1",
                        "hint": "as a crest warning on its own"},
}
occurrence = {}
for t in lines:
    if t in system_texts:
        continue
    n = occurrence.get(t, 0); occurrence[t] = n + 1
    if (t, n) in FIXED:
        f = FIXED[(t, n)]
        phrases.append({"id": f["id"], "text": t, "category": f["category"], "file_stem": f["file_stem"],
                        "system_key": None, "system_variant": None, "single_take": True, "hint": f["hint"]})
        continue
    if t in seen:
        raise SystemExit(f"unexpected duplicate phrase in script: {t!r}")
    seen.add(t)
    cat = next((c for c, f in KW if f(t)), "other")
    h = pacenote_hash(t)
    phrases.append({"id": h, "text": t, "category": cat, "file_stem": f"pacenote_{h}",
                    "system_key": None, "system_variant": None})

data = {"language": "en", "language_label": "English", "source_script_hash": script_hash,
        "categories": ["system","distances","corners","corner modifiers","links","hazards","crests & jumps",
                       "cuts & lines","landmarks","speed & braking","surface","other"],
        "phrases": phrases}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump(data, open(OUT, "w", encoding="utf8"), indent=1, ensure_ascii=False)
from collections import Counter
print(len(phrases), Counter(p["category"] for p in phrases))
print([p["text"] for p in phrases if p["category"] == "other"])
