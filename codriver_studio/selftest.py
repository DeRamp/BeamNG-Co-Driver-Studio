"""Headless self-test for frozen builds: ``CoDriverStudio.exe --selftest <out_dir>``.

Exercises catalog loading, DSP, OGG encoding, device enumeration and a full mod export
without opening any window, and writes ``selftest.json`` into out_dir.
"""
from __future__ import annotations

import json
import sys
import traceback
import zipfile
from pathlib import Path


def _speechish(sr: int, seconds: float, seed: int):
    import numpy as np
    rng = np.random.default_rng(seed)
    t = np.arange(int(sr * seconds)) / sr
    voiced = (np.sin(2 * np.pi * 140 * t) + 0.5 * np.sin(2 * np.pi * 280 * t) + 0.3 * np.sin(2 * np.pi * 700 * t))
    env = np.clip(np.sin(np.pi * np.clip((t - 0.3) / max(seconds - 0.6, 0.1), 0, 1)), 0, 1) ** 0.5
    x = 0.3 * voiced * env + 0.002 * rng.standard_normal(t.size)
    return x.astype(np.float32)


def run(out_dir: str) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result: dict = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "steps": {}}
    try:
        from codriver_studio import audio_io, catalog, dsp, exporter
        from codriver_studio.project import Project

        cat = catalog.load_catalog("en")
        result["steps"]["catalog_phrases"] = len(cat.phrases)
        try:
            result["steps"]["inputs"] = [n for _, n in audio_io.list_input_devices()]
            result["steps"]["outputs"] = [n for _, n in audio_io.list_output_devices()]
        except Exception as e:  # no devices is not a failure of the build
            result["steps"]["devices_error"] = repr(e)

        proj = Project.create(out / "proj", "Selftest Ünïcode")
        sr = 44100
        ids = [p.id for p in cat.phrases[:6]] + ["hard_left", "over_crest"]
        for i, pid in enumerate(ids):
            proj.add_take(pid, _speechish(sr, 1.2, i), sr)
        proj.add_take("hard_left", _speechish(sr, 1.0, 99), sr)
        t = proj.takes("hard_left")[0]
        result["steps"]["trim_hard_left"] = [round(t.trim_start, 3), round(t.trim_end, 3)]

        zpath = out / "selftest_pack.zip"
        rep = exporter.export_mod(proj, zpath, allow_missing=True)
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            meta_name = next(n for n in names if n.endswith("metadata.json"))
            meta = json.loads(z.read(meta_name))
        oggs = sorted(n.rsplit("/", 1)[1] for n in names if n.endswith(".ogg"))
        result["steps"]["export_files"] = oggs
        result["steps"]["metadata_matches_files"] = sorted(meta) == oggs
        result["steps"]["warnings"] = rep.warnings
        assert sorted(meta) == oggs, "metadata/ogg mismatch"
        assert "pacenote_hard_left_1.ogg" in oggs and "pacenote_hard_left_2.ogg" in oggs
        assert "pacenote_over_crest_1.ogg" in oggs
        result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    (out / "selftest.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1
