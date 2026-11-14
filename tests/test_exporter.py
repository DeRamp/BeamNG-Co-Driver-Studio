import hashlib
import io
import os
import json
import re
import threading
import zipfile
from pathlib import Path, PurePosixPath

import numpy as np
import pytest
import soundfile as sf

from cds_helpers import SR, tone
from codriver_studio import exporter
from codriver_studio.exporter import ExportError, export_mod, missing_phrases, plan_export
from codriver_studio.project import Project

GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive")
GAME_VOICEPACKS = GAME / "lua/ge/extensions/gameplay/rally/compositors/voicepacks"
COMMUNITY_MOD = Path(os.environ.get("CDS_COMMUNITY_MOD", Path.home() / "Downloads" / "ru_codriver_mikhail.zip"))
VP = exporter.VOICEPACKS_DIR

MULTI = {"hard_left": 3, "three_right": 2, "100": 4}          # phrases with several takes
SINGLE_MULTI = {"system_countdown3_1": 3, "over_crest_corner": 2}   # single-take phrases, several takes recorded


def pacenote_hash(text):  # port of compositorUtil.lua pacenoteHash
    h = text.lower().replace("'", "")
    h = re.sub(r"[^a-z0-9]", "_", h)
    return re.sub(r"_+", "_", h).strip("_")


# --- Lua-free ports of lua/ge/extensions/gameplay/rally/voicepack.lua -------------------
def lua_read_info_entry_ok(info) -> bool:
    """readInfoEntry: persona, language, styleLabel truthy and notebooks a non-empty table."""
    return bool(isinstance(info, dict) and info.get("persona") and info.get("language")
                and info.get("styleLabel") and isinstance(info.get("notebooks"), list)
                and len(info["notebooks"]) > 0)


def lua_get_audio_candidates(metadata: dict, basename: str, allowed_variants=None) -> list[str]:
    """getAudioCandidates(metadata, basename, _, allowedVariants).
    Without allowedVariants: index every *.ogg key by its exact stem (variant 1) and, if it matches
    ^(.*)_(%d+)%.ogg$, by the numbered stem. With allowedVariants {v}: probe <stem>.ogg (v == 1 only)
    and <stem>_<v>.ogg, keeping those listed in metadata."""
    stem = basename[:-4] if basename.endswith(".ogg") else basename
    if allowed_variants:
        out = []
        for v in sorted({int(v) for v in allowed_variants if int(v) > 0}):
            names = ([f"{stem}.ogg"] if v == 1 else []) + [f"{stem}_{v}.ogg"]
            out += [(v, n) for n in names if isinstance(metadata.get(n), dict)]
        return [n for _, n in sorted(out)]
    index: dict[str, list[tuple[int, str]]] = {}
    for b, entry in metadata.items():
        if isinstance(b, str) and isinstance(entry, dict) and b.endswith(".ogg"):
            index.setdefault(b[:-4], []).append((1, b))
            m = re.match(r"^(.*)_(\d+)\.ogg$", b)
            if m and int(m.group(2)) > 0:
                index.setdefault(m.group(1), []).append((int(m.group(2)), b))
    return [b for _, b in sorted(index.get(stem, []))]


# --- fixtures ----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def full_project(tmp_path_factory):
    """Every catalog phrase recorded with a synthetic tone; a few have several takes,
    one regular take disabled, the system phrase has 3 takes with the middle one enabled."""
    folder = tmp_path_factory.mktemp("full") / "proj"
    p = Project.create(folder, "Test Driver")
    p.save = lambda: None  # speed: skip 200 saves while filling
    for i, ph in enumerate(p.catalog.phrases):
        n = MULTI.get(ph.id, SINGLE_MULTI.get(ph.id, 1))
        for k in range(n):
            dur = 0.25 + 0.01 * (i % 30) + 0.05 * k
            p.add_take(ph.id, tone(200 + 3 * i + 40 * k, dur=dur), SR)
    p.add_take("hard_left", tone(900), SR)
    p.set_take_enabled("hard_left", 3, False)          # 4th take recorded but disabled
    p.set_take_enabled("system_countdown3_1", 1, True)
    p.set_take_enabled("over_crest_corner", 0, True)
    del p.save
    p.save()
    return Project.open(folder)


@pytest.fixture(scope="module")
def exported(full_project, tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "codriver_test_driver.zip"
    calls = []
    report = export_mod(full_project, out, progress_cb=lambda d, t: calls.append((d, t)))
    return full_project, report, out, calls


def _zip(out):
    return zipfile.ZipFile(out)


def _vp(project):
    return f"{VP}/{project.pack.dirname}"


# --- the full-pack validation ------------------------------------------------------------
def test_report_and_progress(exported):
    project, report, out, calls = exported
    assert report.zip_path == out and out.is_file()
    assert report.missing == []
    expected_files = len(project.catalog.phrases) + sum(n - 1 for n in MULTI.values())
    assert report.files_written == expected_files
    assert calls[0] == (0, calls[0][1]) and calls[-1][0] == calls[-1][1]
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)
    assert not list(out.parent.glob("*.partial"))


def test_every_file_stem_present_with_variant_rules(exported):
    project, report, out, _ = exported
    oggs = {PurePosixPath(n).name for n in _zip(out).namelist()
            if n.startswith(_vp(project) + "/audio/") and n.endswith(".ogg")}
    expected = set()
    for ph in project.catalog.phrases:
        if ph.is_system:
            expected.add(f"{ph.file_stem}.ogg")
        else:
            n = MULTI.get(ph.id, 1)
            expected |= {f"{ph.file_stem}.ogg"} if n == 1 else {f"{ph.file_stem}_{k}.ogg" for k in range(1, n + 1)}
    assert oggs == expected
    assert "pacenote_hard_left_4.ogg" not in oggs  # disabled take not exported
    assert "pacenote_system_countdown3_1.ogg" in oggs and not any(o.startswith("pacenote_system_countdown3_1_") for o in oggs)
    # the two fixed "over crest" slots, never a plain pacenote_over_crest.ogg
    assert {"pacenote_over_crest_1.ogg", "pacenote_over_crest_2.ogg"} <= oggs and "pacenote_over_crest.ogg" not in oggs
    assert not any(o.startswith("pacenote_over_crest_2_") for o in oggs)


def test_metadata_matches_audio(exported):
    project, report, out, _ = exported
    z = _zip(out)
    meta = json.loads(z.read(f"{_vp(project)}/metadata.json"))
    audio = {PurePosixPath(n).name: n for n in z.namelist() if n.startswith(_vp(project) + "/audio/") and not n.endswith("/")}
    assert set(meta) == set(audio)
    stem_to_pid = {p.file_stem: p for p in project.catalog.phrases}
    for base, entry in meta.items():
        assert set(entry) == {"audioLen", "sha1", "audioSha1"}
        data = z.read(audio[base])
        info = sf.info(io.BytesIO(data))
        assert info.format == "OGG" and info.subtype == "VORBIS" and info.channels == 1
        assert info.samplerate == 48000
        assert entry["audioLen"] == pytest.approx(info.frames / info.samplerate, abs=1e-6)
        x, sr = sf.read(io.BytesIO(data), dtype="float32")
        assert entry["audioLen"] == pytest.approx(len(x) / sr, abs=1e-6)
        assert report.audio_lengths[base] == entry["audioLen"]
        assert entry["audioLen"] == round(entry["audioLen"], 6)
        assert entry["audioSha1"] == hashlib.sha1(data).hexdigest()
        stem = base[:-4]
        phrase = stem_to_pid.get(stem) or stem_to_pid[stem.rpartition("_")[0]]
        assert entry["sha1"] == hashlib.sha1(phrase.text.encode("utf-8")).hexdigest()
    # rendered from a 0.25-0.6 s tone + pads: lengths must be plausible
    assert all(0.1 < e["audioLen"] < 2.0 for e in meta.values())


def test_voicepack_jsons_pass_loader_check(exported):
    project, report, out, _ = exported
    z = _zip(out)
    pack = project.pack
    ent = json.loads(z.read(f"{_vp(project)}/voicepack.json"))
    cas = json.loads(z.read(f"{VP}/{pack.dirname_casual}/voicepack.json"))
    for info in (ent, cas):
        assert lua_read_info_entry_ok(info) and info["compositorStyle"]
        assert info["persona"] == "Test Driver"  # literal: the picker shows it raw
        assert info["language"] == "ui.options.rally.languages.english.name"
        assert info["dialect"] == "ui.options.rally.dialects.en-US.name"
        assert info["scriptHash"] == project.catalog.source_script_hash
    assert ent["compositorStyle"] == "english/enthusiast" and ent["notebooks"] == ["enthusiast", "structured"]
    assert ent["styleDetailLevel"] == 50 and "reuseAudioFrom" not in ent
    assert ent["linkWordGapMs"] == -40 and ent["pacenoteGapMs"] == 100
    assert cas["compositorStyle"] == "english/casual" and cas["notebooks"] == ["casual", "structured"]
    assert cas["styleDetailLevel"] == 10 and cas["reuseAudioFrom"] == [pack.dirname]
    names = z.namelist()
    assert not any(n.startswith("locales/") for n in names)  # no translation json needed
    assert not any(n.startswith(f"{VP}/{pack.dirname_casual}/audio") for n in names)
    assert f"{VP}/{pack.dirname_casual}/metadata.json" not in names


def test_voicepack_keys_match_official_pack(exported):
    ref = GAME_VOICEPACKS / "ak_2026-04-16" / "voicepack.json"
    ref_cas = GAME_VOICEPACKS / "ak_2026-06-15" / "voicepack.json"
    if not ref.is_file() or not ref_cas.is_file():
        pytest.skip("BeamNG install not found")
    project, report, out, _ = exported
    z = _zip(out)
    ent = json.loads(z.read(f"{_vp(project)}/voicepack.json"))
    cas = json.loads(z.read(f"{VP}/{project.pack.dirname_casual}/voicepack.json"))
    official = json.loads(ref.read_text(encoding="utf-8"))
    official_cas = json.loads(ref_cas.read_text(encoding="utf-8"))
    assert set(ent) == set(official)
    assert set(cas) == set(official_cas)
    assert cas["reuseAudioFrom"] == [project.pack.dirname]  # never an official pack
    for k in ("compositorStyle", "notebooks", "styleLabel", "styleDetailLevel", "language"):
        assert ent[k] == official[k] and cas[k] == official_cas[k]
    for name in GAME_VOICEPACKS.iterdir():  # our dirnames never shadow a shipped pack
        assert name.name not in (project.pack.dirname, project.pack.dirname_casual)


def test_zip_entries_format(exported):
    project, report, out, _ = exported
    z = _zip(out)
    assert z.testzip() is None
    for zi in z.infolist():
        assert "\\" not in zi.filename and not zi.filename.startswith("/")
        if zi.filename.endswith(".ogg"):
            assert zi.compress_type == zipfile.ZIP_STORED
        elif zi.filename.endswith(".json"):
            assert zi.compress_type == zipfile.ZIP_DEFLATED
            json.loads(z.read(zi))  # valid utf-8 JSON


def _layout_template(names, dirnames, mod_ids):
    """Abstract a zip namelist into a layout: dirnames → <DIR>, oggs → <OGG>, mod id → <MOD>,
    locale folder → <LOCALE>."""
    out = set()
    for n in names:
        parts = n.split("/")
        t = []
        for i, part in enumerate(parts):
            if part in dirnames:
                part = "<CASUAL>" if "_casual_" in part else "<DIR>"
            elif part.endswith(".ogg"):
                part = "<OGG>"
            elif part.endswith(".translation.json"):
                part = "<MOD>.translation.json"
            elif i == 2 and parts[:2] == ["locales", "translations"] and part:
                part = "<LOCALE>"
            t.append(part)
        out.add("/".join(t))
    return out


def test_zip_layout_mirrors_community_mod(exported):
    if not COMMUNITY_MOD.is_file():
        pytest.skip("community mod zip not found")
    project, report, out, _ = exported
    ref = zipfile.ZipFile(COMMUNITY_MOD)
    ref_names = [n for n in ref.namelist() if not n.startswith("mod_info/")]  # repo metadata, added by the mod site
    k = len(VP.split("/"))
    ref_dirs = {n.split("/")[k] for n in ref_names if n.startswith(VP + "/") and n.split("/")[k]}
    assert len(ref_dirs) == 2  # enthusiast + casual
    ref_t = _layout_template(ref_names, ref_dirs, set())
    ours = [n for n in _zip(out).namelist() if n != "README.txt"]
    ours_t = _layout_template(ours, {project.pack.dirname, project.pack.dirname_casual}, set())
    # every path pattern of ours (incl. explicit directory entries) exists in the community mod;
    # the only thing it has that we deliberately don't is the locales/ translation json
    assert ours_t <= ref_t, sorted(ours_t - ref_t)
    assert all(t.startswith("locales/") for t in ref_t - ours_t), sorted(ref_t - ours_t)
    # same top-level folders
    assert {n.split("/")[0] for n in ours} == {n.split("/")[0] for n in ref_names} - {"locales"}


def test_game_candidate_lookup_resolves_all_takes(exported):
    project, report, out, _ = exported
    meta = json.loads(_zip(out).read(f"{_vp(project)}/metadata.json"))
    for ph in project.catalog.phrases:
        if ph.is_system:
            # text compositor / custom mode use makeSystemPacenoteAudioFilename(key, variant)
            basename = f"pacenote_system_{ph.system_key}_{ph.system_variant}.ogg"
            assert basename in meta
            assert lua_get_audio_candidates(meta, basename) == [basename]
            continue
        basename = "pacenote_" + pacenote_hash(ph.text) + ".ogg"   # makePacenoteAudioFilename(hash)
        if ph.single_take:  # requested with a fixed variant (compositor entry.variant)
            variant = int(ph.file_stem.rpartition("_")[2])
            assert lua_get_audio_candidates(meta, basename, [variant]) == [f"{ph.file_stem}.ogg"], ph.id
            continue
        cands = lua_get_audio_candidates(meta, basename)
        n = MULTI.get(ph.id, 1)
        if n == 1:
            assert cands == [basename], ph.id
        else:
            assert cands == [f"pacenote_{pacenote_hash(ph.text)}_{k}.ogg" for k in range(1, n + 1)], ph.id


def test_reexport_overwrites_same_mod(exported, tmp_path):
    project, report, out, _ = exported
    before = set(_zip(out).namelist())
    r2 = export_mod(project, out)
    assert r2.zip_path == out and set(_zip(out).namelist()) == before


# --- smaller behaviours ------------------------------------------------------------------
@pytest.fixture
def small(tmp_path):
    p = Project.create(tmp_path / "p", "Small")
    p.add_take("hard_left", tone(), SR)
    p.add_take("hard_left", tone(500), SR)
    p.add_take("easy_right", tone(), SR)
    p.add_take("system_countdown5_1", tone(dur=0.3, lead=0.05, tail=0.05), SR)
    p.add_take("system_precountdown_2", tone(), SR)
    return p


def test_partial_export_missing_and_warnings(small, tmp_path):
    (small.folder / small.takes("hard_left")[0].file).unlink()
    small.refresh_missing()
    assert "hard_right" in missing_phrases(small) and "hard_left" not in missing_phrases(small)
    files, missing, warnings = plan_export(small)
    assert [f.basename for f in files if f.phrase_id == "hard_left"] == ["pacenote_hard_left.ogg"]
    assert any("system_precountdown_2" in w for w in warnings)  # variant 1 not recorded
    out = tmp_path / "o.zip"
    rep = export_mod(small, out)
    assert rep.files_written == 4
    assert len(rep.missing) == len(small.catalog.phrases) - 4
    assert any("hard_left" in w and "missing" in w for w in rep.warnings)
    with pytest.raises(ExportError):
        export_mod(small, tmp_path / "strict.zip", allow_missing=False)
    assert not (tmp_path / "strict.zip").exists()


def test_corrupt_take_discovered_during_export_is_skipped(small, tmp_path):
    # file becomes corrupt after the project was opened: probe said fine, render fails
    (small.folder / small.takes("hard_left")[1].file).write_bytes(b"RIFF....junk")
    rep = export_mod(small, tmp_path / "o.zip")
    names = [PurePosixPath(n).name for n in zipfile.ZipFile(rep.zip_path).namelist()]
    assert "pacenote_hard_left.ogg" in names            # renumbered: single survivor → plain stem
    assert "pacenote_hard_left_1.ogg" not in names
    assert any("hard_left" in w and "skipped" in w for w in rep.warnings)


def test_no_casual_and_custom_dialect(small, tmp_path):
    small.pack.include_casual = False
    small.pack.dialect = "Scottish"
    rep = export_mod(small, tmp_path / "o")  # .zip appended
    assert rep.zip_path.suffix == ".zip" and rep.dirname_casual is None
    z = zipfile.ZipFile(rep.zip_path)
    assert not any("_casual_" in n for n in z.namelist())
    ent = json.loads(z.read(f"{VP}/{small.pack.dirname}/voicepack.json"))
    assert ent["dialect"] == "Scottish" and ent["persona"] == "Small"   # literal, shown raw
    small.pack.dialect = "en-GB"
    assert exporter.build_voicepack_infos(small)[0]["dialect"] == "ui.options.rally.dialects.en-GB.name"


def test_gaps_clamped_in_voicepack(small):
    small.pack.link_word_gap_ms = -999
    small.pack.pacenote_gap_ms = 9999
    ent, cas = exporter.build_voicepack_infos(small)
    assert ent["linkWordGapMs"] == -200 and ent["pacenoteGapMs"] == 400


def test_restart_notice_and_long_countdown_warning(small, tmp_path):
    small.add_take("system_countdown4_1", tone(dur=1.2, lead=0, tail=0), SR, auto_trim=False)
    rep = export_mod(small, tmp_path / "w.zip")
    assert exporter.RESTART_NOTICE in rep.warnings
    long_warn = [w for w in rep.warnings if "countdown" in w and "once per second" in w]
    assert len(long_warn) == 1 and long_warn[0].startswith("system_countdown4_1")
    assert rep.audio_lengths["pacenote_system_countdown4_1.ogg"] >= 0.9


def test_beamng_running_never_raises(monkeypatch):
    assert isinstance(exporter.beamng_running(), bool)

    def boom(*a, **k):
        raise OSError("no tasklist")
    monkeypatch.setattr(exporter.subprocess, "run", boom)
    assert exporter.beamng_running() is False


def test_nothing_to_export(tmp_path):
    p = Project.create(tmp_path / "e", "Empty")
    with pytest.raises(ExportError):
        export_mod(p, tmp_path / "e.zip")


def test_locked_target_gives_readable_error(small, tmp_path, monkeypatch):
    out = tmp_path / "locked.zip"
    out.write_bytes(b"old zip content")

    def locked(src, dst):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")
    monkeypatch.setattr(exporter.os, "replace", locked)
    monkeypatch.setattr(exporter.time, "sleep", lambda s: None)
    monkeypatch.setattr(exporter, "beamng_running", lambda: True)
    with pytest.raises(ExportError, match="BeamNG.drive is running"):
        export_mod(small, out)
    assert out.read_bytes() == b"old zip content"
    assert not list(tmp_path.glob("*.partial"))


def test_failure_mid_export_leaves_no_partial_zip(small, tmp_path, monkeypatch):
    out = tmp_path / "x.zip"
    out.write_bytes(b"previous")

    def bad_write(path, *a, **k):
        raise OSError("disk full")
    dsp = exporter.dsp  # the module object the exporter actually uses
    real = dsp.write_ogg
    monkeypatch.setattr(dsp, "write_ogg", bad_write)
    with pytest.raises(ExportError):
        export_mod(small, out)  # every take fails → nothing rendered
    assert out.read_bytes() == b"previous" and not list(tmp_path.glob("*.partial"))
    monkeypatch.setattr(dsp, "write_ogg", real)


def test_cancel(small, tmp_path):
    ev = threading.Event()
    ev.set()
    with pytest.raises(exporter.ExportCancelled):
        export_mod(small, tmp_path / "c.zip", cancel=ev)
    assert not (tmp_path / "c.zip").exists() and not list(tmp_path.glob("*.partial"))


def test_beamng_mods_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert exporter.beamng_mods_dir() is None
    mods = tmp_path / "BeamNG" / "BeamNG.drive" / "current" / "mods"
    mods.mkdir(parents=True)
    assert exporter.beamng_mods_dir() == mods
    monkeypatch.delenv("LOCALAPPDATA")
    assert exporter.beamng_mods_dir() is None


def test_translation_used_for_text_sha1(small, tmp_path):
    small.set_translation("easy_right", "leicht rechts")
    rep = export_mod(small, tmp_path / "t.zip")
    meta = json.loads(zipfile.ZipFile(rep.zip_path).read(f"{VP}/{small.pack.dirname}/metadata.json"))
    assert meta["pacenote_easy_right.ogg"]["sha1"] == hashlib.sha1("leicht rechts".encode()).hexdigest()


# ---------------------------------------------------------------- pack language
def test_language_defaults_to_english_and_old_projects_load(small, tmp_path):
    assert small.pack.language == "english"
    ent, _ = exporter.build_voicepack_infos(small)
    assert ent["language"] == "ui.options.rally.languages.english.name"
    assert ent["dialect"] == "ui.options.rally.dialects.en-US.name"
    # a project saved before the language field existed opens as English
    data = json.loads((small.folder / "project.json").read_text(encoding="utf-8"))
    data["pack"].pop("language")
    (small.folder / "project.json").write_text(json.dumps(data), encoding="utf-8")
    assert Project.open(small.folder).pack.language == "english"


def test_builtin_spanish_uses_game_keys(small):
    small.pack.language, small.pack.dialect = "spanish", "es-ES"
    ent, cas = exporter.build_voicepack_infos(small)
    for vp in (ent, cas):
        assert vp["language"] == "ui.options.rally.languages.spanish.name"
        assert vp["dialect"] == "ui.options.rally.dialects.es-ES.name"


def test_custom_language_written_literally_and_saved(small, tmp_path):
    small.pack.language, small.pack.dialect = "Русский", ""
    small.save()
    reopened = Project.open(small.folder)
    assert reopened.pack.language == "Русский"
    rep = export_mod(reopened, tmp_path / "ru.zip")
    z = zipfile.ZipFile(rep.zip_path)
    ent = json.loads(z.read(f"{VP}/{reopened.pack.dirname}/voicepack.json").decode("utf-8"))
    assert ent["language"] == "Русский"
    assert "dialect" not in ent                 # empty dialect is simply omitted
    assert lua_read_info_entry_ok(ent)          # still listed by the game


def test_language_id_and_dialects_for():
    from codriver_studio.project import dialects_for, language_id
    assert language_id(" English ") == "english" and language_id("SPANISH") == "spanish"
    assert language_id("Deutsch") == "Deutsch"
    assert [c for c, _ in dialects_for("english")] == ["en-US", "en-GB"]
    assert [c for c, _ in dialects_for("Spanish")] == ["es-ES"]
    assert dialects_for("Deutsch") == []
