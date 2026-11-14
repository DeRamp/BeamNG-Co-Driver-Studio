import json
import re
import shutil
import sys

import pytest

from codriver_studio import catalog as catmod
from codriver_studio.catalog import load_catalog, resource_path


def pacenote_hash(text):  # mirror of compositorUtil.lua pacenoteHash
    h = text.lower().replace("'", "")
    h = re.sub(r"[^a-z0-9]", "_", h)
    return re.sub(r"_+", "_", h).strip("_")


def test_load_english():
    cat = load_catalog("en")
    assert cat.language == "en"
    assert cat.source_script_hash
    assert len(cat.phrases) > 150
    assert len({p.id for p in cat.phrases}) == len(cat.phrases)
    assert cat.get("hard_left").text == "hard left"
    assert cat.get("hard_left").category == "corners"
    with pytest.raises(KeyError):
        cat.get("no_such_phrase")
    assert "hard_left" in cat and "nope" not in cat
    assert all(p.category == "corners" for p in cat.by_category("corners"))
    assert {p.category for p in cat.phrases} <= set(cat.categories)
    assert load_catalog("en") is cat  # cached


def test_file_stems_and_system_flags():
    cat = load_catalog()
    systems = cat.by_category("system")
    assert systems and all(p.is_system for p in systems)
    for p in cat.phrases:
        if p.is_system:
            assert p.system_key and p.system_variant >= 1
            assert p.file_stem == f"pacenote_system_{p.system_key}_{p.system_variant}"
            assert p.is_single_take
        elif p.single_take:  # fixed numbered slot requested by the game, e.g. pacenote_over_crest_2
            assert p.is_single_take and p.system_key is None
            assert re.fullmatch(re.escape("pacenote_" + pacenote_hash(p.text)) + r"_\d+", p.file_stem)
        else:
            assert not p.is_system and not p.is_single_take and p.system_key is None
            assert p.file_stem == "pacenote_" + pacenote_hash(p.text)
    assert len({p.file_stem for p in cat.phrases}) == len(cat.phrases)


def test_over_crest_slots():
    cat = load_catalog()
    a, b = cat.get("over_crest"), cat.get("over_crest_corner")
    assert a.text == b.text == "over crest"
    assert (a.file_stem, b.file_stem) == ("pacenote_over_crest_1", "pacenote_over_crest_2")
    assert a.single_take and b.single_take and a.hint and b.hint
    assert b.category == "corner modifiers"


def test_missing_language():
    with pytest.raises(FileNotFoundError):
        load_catalog("xx_nope")


def test_resource_path_source():
    p = resource_path("data", "phrases_en.json")
    assert p.is_file()
    assert "en" in catmod.available_languages()


def _fake_bundle(tmp_path, sub):
    src = resource_path("data", "phrases_en.json")
    dst = tmp_path.joinpath(*sub, "data")
    dst.mkdir(parents=True)
    shutil.copy(src, dst / "phrases_en.json")
    return dst / "phrases_en.json"


def test_resource_path_pyinstaller_meipass(tmp_path, monkeypatch):
    expected = _fake_bundle(tmp_path, ["codriver_studio"])
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert resource_path("data", "phrases_en.json") == expected


def test_resource_path_frozen_internal_dir(tmp_path, monkeypatch):
    expected = _fake_bundle(tmp_path, ["_internal", "codriver_studio"])
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "CoDriverStudio.exe"))
    assert resource_path("data", "phrases_en.json") == expected


def test_resource_path_falls_back_to_source(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)  # empty bundle dir
    assert resource_path("data", "phrases_en.json").is_file()


def test_catalog_from_dict_tolerates_missing_categories():
    d = {"language": "xx", "phrases": [{"id": "a", "text": "a", "category": "new", "file_stem": "pacenote_a"}]}
    cat = catmod.catalog_from_dict(d)
    assert cat.categories == ["new"]
    with pytest.raises(ValueError):
        catmod.catalog_from_dict({"phrases": [d["phrases"][0], d["phrases"][0]]})
