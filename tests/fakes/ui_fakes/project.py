import datetime
import json
import os
import pathlib
import re
from dataclasses import dataclass, asdict, field, fields

from codriver_studio import catalog as catalog_mod
from codriver_studio import dsp


@dataclass
class Take:
    file: str
    trim_start: float
    trim_end: float
    enabled: bool = True
    auto_trimmed: bool = True
    created: str = ""
    duration: float = 0.0


def _slug(s):
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", s.lower())).strip("_") or "codriver"


@dataclass
class PackInfo:
    persona: str = "My Co-Driver"
    author: str = ""
    dialect: str = "en-US"
    dirname_slug: str = ""
    version: str = "1.0"
    export_sample_rate: int = 24000
    link_word_gap_ms: int = -40
    corner_gap_ms: int = 0
    phrase_gap_ms: int = 0
    pacenote_gap_ms: int = 100
    include_casual: bool = True
    codriver_timing_offset_s: float = 0.0


class Project:
    def __init__(self, folder, language="en"):
        self.folder = pathlib.Path(folder)
        self.catalog = catalog_mod.load_catalog(language)
        self.pack = PackInfo()
        self.effects = dsp.EffectSettings()
        self.trim_sensitivity = 0.5
        self.translations = {}
        self._takes = {}

    @classmethod
    def create(cls, folder, persona, language="en"):
        p = cls(folder, language)
        p.folder.mkdir(parents=True, exist_ok=True)
        p.pack.persona = persona
        p.pack.dirname_slug = _slug(persona)
        p.save()
        return p

    @classmethod
    def open(cls, folder):
        folder = pathlib.Path(folder)
        d = json.loads((folder / "project.json").read_text("utf8"))
        p = cls(folder, d.get("language", "en"))
        p.pack = PackInfo(**{k: v for k, v in d.get("pack", {}).items() if k in {f.name for f in fields(PackInfo)}})
        p.effects = dsp.EffectSettings.from_dict(d.get("effects", {}))
        p.trim_sensitivity = d.get("trim_sensitivity", 0.5)
        p._takes = {k: [Take(**t) for t in v] for k, v in d.get("takes", {}).items()}
        return p

    def spoken_text(self, phrase_id):
        return self.translations.get(phrase_id) or self.catalog.get(phrase_id).text

    def takes(self, phrase_id):
        return list(self._takes.get(phrase_id, []))

    def add_take(self, phrase_id, x, sr, auto_trim=True):
        lst = self._takes.setdefault(phrase_id, [])
        d = self.folder / "takes" / phrase_id
        d.mkdir(parents=True, exist_ok=True)
        n = 1
        while (d / f"take_{n:03d}.wav").exists():
            n += 1
        dsp.write_wav(d / f"take_{n:03d}.wav", x, sr)
        dur = len(x) / sr
        s, e = dsp.auto_trim(x, sr, self.trim_sensitivity) if auto_trim else (0.0, dur)
        phrase = self.catalog.get(phrase_id)
        enabled = not (phrase.is_system and any(t.enabled for t in lst))
        t = Take(f"takes/{phrase_id}/take_{n:03d}.wav", s, e, enabled, auto_trim,
                 datetime.datetime.now().isoformat(timespec="seconds"), dur)
        lst.append(t)
        self.save()
        return t

    def delete_take(self, phrase_id, take_index):
        t = self._takes[phrase_id].pop(take_index)
        try:
            (self.folder / t.file).unlink()
        except FileNotFoundError:
            pass
        self.save()

    def set_take_enabled(self, phrase_id, take_index, enabled):
        lst = self._takes[phrase_id]
        if enabled and self.catalog.get(phrase_id).is_system:
            for t in lst:
                t.enabled = False
        lst[take_index].enabled = enabled
        self.save()

    def set_trim(self, phrase_id, take_index, start, end):
        t = self._takes[phrase_id][take_index]
        start = max(0.0, min(start, t.duration))
        end = max(start, min(end, t.duration))
        t.trim_start, t.trim_end, t.auto_trimmed = start, end, False
        self.save()

    def reauto_trim(self, phrase_id, take_index, sensitivity=None):
        x, sr = self.load_take_audio(phrase_id, take_index)
        t = self._takes[phrase_id][take_index]
        t.trim_start, t.trim_end = dsp.auto_trim(x, sr, self.trim_sensitivity if sensitivity is None else sensitivity)
        t.auto_trimmed = True
        self.save()

    def load_take_audio(self, phrase_id, take_index):
        return dsp.read_audio(self.folder / self._takes[phrase_id][take_index].file)

    def render_take(self, phrase_id, take_index, out_sr=None):
        x, sr = self.load_take_audio(phrase_id, take_index)
        t = self._takes[phrase_id][take_index]
        out_sr = out_sr or self.pack.export_sample_rate
        return dsp.render_take(x, sr, t.trim_start, t.trim_end, self.effects, out_sr), out_sr

    def status(self, phrase_id):
        lst = self._takes.get(phrase_id, [])
        if not lst:
            return "missing"
        return "recorded" if any(t.enabled for t in lst) else "disabled"

    def progress(self):
        return sum(self.status(p.id) == "recorded" for p in self.catalog.phrases), len(self.catalog.phrases)

    def save(self):
        d = {"version": 1, "language": self.catalog.language, "pack": asdict(self.pack),
             "effects": self.effects.to_dict(), "trim_sensitivity": self.trim_sensitivity,
             "translations": self.translations,
             "takes": {k: [asdict(t) for t in v] for k, v in self._takes.items()}}
        tmp = self.folder / "project.json.tmp"
        tmp.write_text(json.dumps(d, indent=1), "utf8")
        os.replace(tmp, self.folder / "project.json")
