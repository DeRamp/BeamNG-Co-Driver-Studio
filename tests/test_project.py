import datetime as dt
import json
import os
import re

import numpy as np
import pytest

from cds_helpers import SR, tone
from codriver_studio import project as projmod
from codriver_studio.project import PackInfo, Project, ProjectError, slugify


@pytest.fixture
def proj(tmp_path):
    return Project.create(tmp_path / "proj", "Jérôme O'Neil")


def read_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------- slug / pack info
@pytest.mark.parametrize("persona,slug", [
    ("Jérôme O'Neil", "jerome_oneil"),
    ("My Co-Driver", "my_co_driver"),
    ("  __Hello__ World!! ", "hello_world"),
    ("", "codriver"),
    ("!!!", "codriver"),
    ("Михаил", "codriver"),
    ("AK", "ak_custom"),
    ("rh", "rh_custom"),
    ("dirtwheel", "dirtwheel_custom"),
    ("Fluffy", "fluffy_custom"),
    ("ak 2026", "ak_2026_custom"),
    ("Akira", "akira"),
])
def test_slugify(persona, slug):
    assert slugify(persona) == slug


def test_slug_length_and_charset():
    s = slugify("x" * 200 + " y")
    assert len(s) <= 40 and s.strip("_") == s
    assert all(c.isalnum() or c == "_" for c in slugify("A-b.c/d\\e:f"))


def test_dirname_generated_once(proj):
    p = proj.pack
    today = dt.date.today().isoformat()
    m = re.fullmatch(r"jerome_oneil_(\d{4}-\d{2}-\d{2})_([0-9a-f]{6})", p.dirname)
    assert m and m.group(1) == today
    assert p.dirname_casual == f"jerome_oneil_casual_{today}_{m.group(2)}"
    assert p.slug == "jerome_oneil"
    assert p.zip_name == f"codriver_{p.dirname}.zip"
    # stored, never regenerated: not on reopen, not on persona change, not on another day
    data = read_json(proj.folder / "project.json")
    assert data["pack"]["dirname"] == p.dirname and data["pack"]["dirname_casual"] == p.dirname_casual
    again = Project.open(proj.folder)
    assert again.pack.dirname == p.dirname and again.load_warnings == []
    again.pack.persona = "Somebody Else"
    again.save()
    assert Project.open(proj.folder).pack.dirname == p.dirname
    # random part differs between projects
    assert PackInfo(persona="Jérôme O'Neil").dirname != p.dirname


def test_missing_dirname_generated_and_persisted(tmp_path):
    (tmp_path / "project.json").write_text(json.dumps({"pack": {"persona": "Old"}}), encoding="utf-8")
    p = Project.open(tmp_path)
    assert p.pack.dirname.startswith("old_") and any("generated" in w for w in p.load_warnings)
    assert read_json(tmp_path / "project.json")["pack"]["dirname"] == p.pack.dirname
    assert Project.open(tmp_path).pack.dirname == p.pack.dirname
    for bad in ("_hidden_2026-01-01_abcdef", "a/b", "C:x", ""):
        assert PackInfo(dirname=bad).dirname != bad


def test_builtin_names_never_produced():
    for persona in ("ak", "RH", "dirtwheel", "fluffy"):
        assert PackInfo(persona=persona).dirname.startswith(f"{slugify(persona)}_") and slugify(persona).endswith("_custom")


def test_gap_clamps():
    p = PackInfo(link_word_gap_ms=-500, corner_gap_ms=999, phrase_gap_ms=12.6, pacenote_gap_ms=-300)
    assert (p.link_word_gap_ms, p.corner_gap_ms, p.phrase_gap_ms, p.pacenote_gap_ms) == (-200, 200, 13, -100)
    p.pacenote_gap_ms = 1000
    assert p.to_dict()["pacenote_gap_ms"] == 400
    assert projmod.clamp_gap_ms("phrase_gap_ms", "junk") == 0
    assert PackInfo().export_sample_rate == 48000


# --------------------------------------------------------------------- create / save / open
def test_create_writes_versioned_json(proj):
    data = read_json(proj.folder / "project.json")
    assert data["format_version"] == 1
    assert data["language"] == "en"
    assert data["translations"] == {}
    assert data["pack"]["persona"] == "Jérôme O'Neil"
    assert data["takes"] == {}
    assert isinstance(data["effects"], dict)
    assert (proj.folder / "takes").is_dir()
    assert not (proj.folder / "project.json.tmp").exists()
    # human readable (indented, utf-8 not escaped)
    text = (proj.folder / "project.json").read_text(encoding="utf-8")
    assert "\n" in text and "Jérôme" in text


def test_create_refuses_existing(proj):
    with pytest.raises(ProjectError):
        Project.create(proj.folder, "again")


def test_open_errors(tmp_path):
    with pytest.raises(ProjectError):
        Project.open(tmp_path)
    (tmp_path / "project.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.open(tmp_path)
    (tmp_path / "project.json").write_text("[1,2]", encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.open(tmp_path)


def test_add_take_and_roundtrip(proj):
    take = proj.add_take("hard_left", tone(), SR)
    assert take.file == "takes/hard_left/take_001.wav"
    assert (proj.folder / "takes" / "hard_left" / "take_001.wav").is_file()
    assert take.enabled and take.auto_trimmed and take.created
    assert take.duration == pytest.approx(0.9, abs=1e-3)
    assert 0.0 <= take.trim_start < 0.2 + 1e-3 and 0.6 - 1e-3 < take.trim_end <= take.duration
    data = read_json(proj.folder / "project.json")
    assert data["takes"]["hard_left"][0]["file"] == "takes/hard_left/take_001.wav"

    again = Project.open(proj.folder)
    t2 = again.takes("hard_left")[0]
    assert t2 == take and not t2.missing
    assert again.status("hard_left") == "recorded"
    assert again.load_warnings == []
    x, sr = again.load_take_audio("hard_left", 0)
    assert sr == SR and len(x) == len(tone())


def test_no_autotrim(proj):
    t = proj.add_take("hard_left", tone(), SR, auto_trim=False)
    assert (t.trim_start, t.trim_end) == (0.0, pytest.approx(t.duration)) and not t.auto_trimmed


def test_unlimited_takes_enable_disable_status(proj):
    pid = "three_left"
    assert proj.status(pid) == "missing"
    for i in range(7):
        proj.add_take(pid, tone(300 + 20 * i), SR)
    takes = proj.takes(pid)
    assert len(takes) == 7 and all(t.enabled for t in takes)
    assert [t.file.rsplit("/", 1)[1] for t in takes] == [f"take_{i:03d}.wav" for i in range(1, 8)]
    for i in range(7):
        proj.set_take_enabled(pid, i, False)
    assert proj.status(pid) == "disabled"
    proj.set_take_enabled(pid, 3, True)
    proj.set_take_enabled(pid, 5, True)
    assert proj.status(pid) == "recorded"
    assert [i for i, _ in proj.exportable_takes(pid)] == [3, 5]
    assert proj.progress() == (1, len(proj.catalog.phrases))


def test_system_phrase_single_enabled(proj):
    pid = "system_countdown3_1"
    assert proj.catalog.get(pid).is_system
    for i in range(3):
        proj.add_take(pid, tone(500 + i * 50), SR)
    assert [t.enabled for t in proj.takes(pid)] == [False, False, True]  # newest wins
    proj.set_take_enabled(pid, 0, True)
    assert [t.enabled for t in proj.takes(pid)] == [True, False, False]
    proj.set_take_enabled(pid, 0, False)
    assert proj.status(pid) == "disabled"
    proj.set_take_enabled(pid, 1, True)
    assert [i for i, _ in proj.exportable_takes(pid)] == [1]
    # deleting the enabled take re-enables the newest remaining one
    proj.delete_take(pid, 1)
    assert [t.enabled for t in proj.takes(pid)] == [False, True]


def test_regular_phrase_exportable_many(proj):
    for i in range(3):
        proj.add_take("hard_right", tone(), SR)
    assert len(proj.exportable_takes("hard_right")) == 3


def test_delete_take_removes_file_and_never_reuses_number(proj):
    pid = "hard_left"
    proj.add_take(pid, tone(), SR)
    proj.add_take(pid, tone(), SR)
    f2 = proj.folder / proj.takes(pid)[1].file
    proj.delete_take(pid, 1)
    assert not f2.exists()
    t = proj.add_take(pid, tone(), SR)
    assert t.file.endswith("take_002.wav")  # 2 was freed by delete, 1 still used → 2
    proj.delete_take(pid, 0)
    proj.delete_take(pid, 0)
    assert proj.takes(pid) == [] and proj.status(pid) == "missing"
    with pytest.raises(IndexError):
        proj.delete_take(pid, 0)
    assert "hard_left" not in read_json(proj.folder / "project.json")["takes"]


def test_orphan_wav_not_overwritten(proj):
    d = proj.folder / "takes" / "hard_left"
    d.mkdir(parents=True)
    (d / "take_004.wav").write_bytes(b"orphan")
    t = proj.add_take("hard_left", tone(), SR)
    assert t.file.endswith("take_005.wav")
    assert (d / "take_004.wav").read_bytes() == b"orphan"


def test_set_trim_clamps(proj):
    proj.add_take("hard_left", tone(), SR)
    proj.set_trim("hard_left", 0, -1.0, 99.0)
    t = proj.takes("hard_left")[0]
    assert t.trim_start == 0.0 and t.trim_end == pytest.approx(t.duration) and not t.auto_trimmed
    proj.set_trim("hard_left", 0, 0.5, 0.3)  # swapped
    t = proj.takes("hard_left")[0]
    assert (t.trim_start, t.trim_end) == (pytest.approx(0.3), pytest.approx(0.5))
    proj.set_trim("hard_left", 0, 0.4, 0.4)  # zero length → minimal region
    t = proj.takes("hard_left")[0]
    assert t.trim_end > t.trim_start
    s, e = proj.reauto_trim("hard_left", 0, sensitivity=0.7)
    t = proj.takes("hard_left")[0]
    assert t.auto_trimmed and (t.trim_start, t.trim_end) == (s, e)
    assert Project.open(proj.folder).takes("hard_left")[0].trim_end == pytest.approx(e, abs=1e-6)


def test_render_take(proj):
    proj.add_take("hard_left", tone(), SR, auto_trim=False)
    proj.set_trim("hard_left", 0, 0.1, 0.7)
    y, sr = proj.render_take("hard_left", 0)
    assert sr == proj.pack.export_sample_rate == 48000
    assert y.dtype == np.float32 and y.ndim == 1
    assert len(y) / sr == pytest.approx(0.6, abs=0.06)
    assert np.max(np.abs(y)) <= 1.0
    y2, sr2 = proj.render_take("hard_left", 0, out_sr=16000)
    assert sr2 == 16000 and len(y2) / sr2 == pytest.approx(0.6, abs=0.06)


def test_unknown_phrase_rejected(proj):
    with pytest.raises(KeyError):
        proj.add_take("not_a_phrase", tone(), SR)
    with pytest.raises(ValueError):
        proj.add_take("hard_left", np.zeros(0, np.float32), SR)


# --------------------------------------------------------------------- tolerance
def test_missing_and_corrupt_take_files(proj):
    proj.add_take("hard_left", tone(), SR)
    proj.add_take("hard_left", tone(), SR)
    proj.add_take("easy_right", tone(), SR)
    proj.add_take("five_left", tone(), SR)
    (proj.folder / proj.takes("hard_left")[0].file).unlink()
    (proj.folder / proj.takes("easy_right")[0].file).write_bytes(b"RIFF garbage, not a wav")
    (proj.folder / proj.takes("five_left")[0].file).write_bytes(b"")
    again = Project.open(proj.folder)
    hl = again.takes("hard_left")
    assert hl[0].missing and not hl[1].missing
    assert again.status("hard_left") == "recorded"
    assert again.status("easy_right") == "missing"
    assert again.status("five_left") == "missing"
    assert [i for i, _ in again.exportable_takes("hard_left")] == [1]
    assert any("hard_left" in w for w in again.load_warnings)
    with pytest.raises(Exception):
        again.load_take_audio("easy_right", 0)
    # the entries are kept (user may restore the files)
    again.save()
    assert len(read_json(again.folder / "project.json")["takes"]["hard_left"]) == 2


def test_forward_tolerance_unknown_keys_preserved(proj):
    proj.add_take("hard_left", tone(), SR)
    data = read_json(proj.folder / "project.json")
    data["future_feature"] = {"a": 1}
    data["pack"]["future_pack_key"] = "x"
    data["takes"]["hard_left"][0]["rating"] = 5
    data["takes"]["ghost_phrase"] = [{"file": "takes/ghost_phrase/take_001.wav"}]
    data["format_version"] = 2
    (proj.folder / "project.json").write_text(json.dumps(data), encoding="utf-8")
    again = Project.open(proj.folder)
    assert any("newer" in w for w in again.load_warnings)
    assert again.takes("ghost_phrase")[0].missing
    again.set_take_enabled("hard_left", 0, False)  # triggers save
    out = read_json(proj.folder / "project.json")
    assert out["future_feature"] == {"a": 1}
    assert out["pack"]["future_pack_key"] == "x"
    assert out["takes"]["hard_left"][0]["rating"] == 5
    assert out["takes"]["hard_left"][0]["enabled"] is False
    assert "ghost_phrase" in out["takes"]


def test_missing_keys_defaulted(tmp_path):
    (tmp_path / "project.json").write_text(json.dumps({
        "takes": {"hard_left": [{"file": "takes\\hard_left\\take_001.wav"}, {"no_file": 1}, "junk"],
                  "easy_left": "junk"},
        "pack": {"persona": "Bob", "export_sample_rate": "bad", "include_casual": "yes"},
        "effects": "junk",
        "trim_sensitivity": "nan",
    }), encoding="utf-8")
    p = Project.open(tmp_path)
    assert p.pack.persona == "Bob" and p.pack.dirname.startswith("bob_")
    assert p.pack.export_sample_rate == 48000 and p.pack.include_casual is True
    assert p.pack.link_word_gap_ms == -40 and p.pack.pacenote_gap_ms == 100
    assert p.translations == {} and p.trim_sensitivity == 0.5
    t = p.takes("hard_left")
    assert len(t) == 1 and t[0].file == "takes/hard_left/take_001.wav" and t[0].missing
    assert p.status("hard_left") == "missing"
    p.save()
    assert read_json(tmp_path / "project.json")["format_version"] == 1


def test_atomic_save_keeps_old_file_on_failure(proj, monkeypatch):
    before = (proj.folder / "project.json").read_bytes()

    def boom(*a, **k):
        raise OSError("disk on fire")
    monkeypatch.setattr(projmod.os, "replace", boom)
    proj.pack.persona = "Changed"
    with pytest.raises(OSError):
        proj.save()
    assert (proj.folder / "project.json").read_bytes() == before


def test_translations(proj):
    assert proj.spoken_text("hard_left") == "hard left"
    proj.set_translation("hard_left", "fort links")
    assert proj.spoken_text("hard_left") == "fort links"
    again = Project.open(proj.folder)
    assert again.translations == {"hard_left": "fort links"}
    again.set_translation("hard_left", "")
    assert again.spoken_text("hard_left") == "hard left"
    with pytest.raises(KeyError):
        proj.spoken_text("nope")


def test_effects_roundtrip(proj):
    proj.effects.gain_db = 3.0
    proj.save()
    assert Project.open(proj.folder).effects.gain_db == 3.0


def test_single_take_regular_phrase(proj):
    pid = "over_crest_corner"
    assert proj.catalog.get(pid).single_take and not proj.catalog.get(pid).is_system
    proj.add_take(pid, tone(), SR)
    proj.add_take(pid, tone(600), SR)
    assert [t.enabled for t in proj.takes(pid)] == [False, True]
    proj.set_take_enabled(pid, 0, True)
    assert [t.enabled for t in proj.takes(pid)] == [True, False]
    assert [i for i, _ in proj.exportable_takes(pid)] == [0]
