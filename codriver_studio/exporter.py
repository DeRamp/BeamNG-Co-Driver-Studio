"""Build the BeamNG.drive voicepack mod zip (no Qt).

Layout (see ARCHITECTURE.md, mirrors the official packs and community mods)::

    lua/ge/extensions/gameplay/rally/compositors/voicepacks/<dirname>/voicepack.json
    lua/ge/extensions/gameplay/rally/compositors/voicepacks/<dirname>/metadata.json
    lua/ge/extensions/gameplay/rally/compositors/voicepacks/<dirname>/audio/pacenote_*.ogg
    lua/ge/extensions/gameplay/rally/compositors/voicepacks/<dirname_casual>/voicepack.json
    README.txt

No translation json: ``persona`` is the literal display name (the game shows it raw) and the
language/dialect keys already exist in the game.

``export_mod`` is slow (renders + Vorbis-encodes every take: roughly 20-80 ms per take) —
run it in a worker thread. It never leaves a half-written zip behind: the archive is built
as ``<target>.<random>.partial`` next to the target and moved into place with ``os.replace``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import soundfile as sf

from . import dsp
from .project import clamp_gap_ms

__all__ = ["ExportReport", "ExportError", "ExportCancelled", "missing_phrases", "export_mod",
           "beamng_mods_dir", "beamng_running", "plan_export", "build_voicepack_infos",
           "VOICEPACKS_DIR", "default_zip_path", "COUNTDOWN_MAX_S", "RESTART_NOTICE"]

VOICEPACKS_DIR = "lua/ge/extensions/gameplay/rally/compositors/voicepacks"
BUILTIN_DIALECTS = {"en-US", "en-GB"}
BEAMNG_EXE = "BeamNG.drive.x64.exe"
COUNTDOWN_MAX_S = 0.9       # countdown clips are queued once per second
RESTART_NOTICE = ("BeamNG caches voice packs for the whole session: restart the game (or reload Lua "
                  "with Ctrl+L) to hear the new export.")
# dirnames of voicepacks that ship with the game (as of the 2026 builds)
BUILTIN_DIRNAMES = {"ak_2026-04-16", "ak_2026-06-15", "dirtwheel_2026-07-19", "dirtwheel_2026-07-20",
                    "fluffy_2026-04-01", "rh_2026-06-05", "rh_2026-06-15"}


class ExportError(Exception):
    """Readable, user-facing export failure."""


class ExportCancelled(ExportError):
    pass


@dataclass
class ExportReport:
    zip_path: Path
    files_written: int = 0                       # number of .ogg files in the pack
    missing: list[str] = field(default_factory=list)   # phrase ids without any exported audio
    warnings: list[str] = field(default_factory=list)
    dirname: str = ""
    dirname_casual: str | None = None
    audio_lengths: dict = field(default_factory=dict)  # basename -> audioLen written to metadata


@dataclass
class _PlannedFile:
    phrase_id: str
    take_index: int
    basename: str        # e.g. "pacenote_hard_left_2.ogg"


def missing_phrases(project) -> list[str]:
    """Catalog phrase ids that have no exportable (enabled, readable) take."""
    return [p.id for p in project.catalog.phrases if not project.exportable_takes(p.id)]


def plan_export(project) -> tuple[list[_PlannedFile], list[str], list[str]]:
    """(files, missing ids, warnings) — pure bookkeeping, no audio work. Exposed for tests/UI."""
    files, missing, warnings = [], [], []
    for phrase in project.catalog.phrases:
        takes = project.exportable_takes(phrase.id)
        n_missing_files = sum(1 for t in project.takes(phrase.id) if t.enabled and t.missing)
        if not takes:
            missing.append(phrase.id)
            if n_missing_files:
                warnings.append(f"{phrase.id}: {n_missing_files} enabled take file(s) missing — phrase skipped")
            continue
        if n_missing_files:
            warnings.append(f"{phrase.id}: {n_missing_files} enabled take file(s) missing — skipped")
        if phrase.is_single_take or len(takes) == 1:
            files.append(_PlannedFile(phrase.id, takes[0][0], f"{phrase.file_stem}.ogg"))
        else:
            for n, (idx, _t) in enumerate(takes, 1):
                files.append(_PlannedFile(phrase.id, idx, f"{phrase.file_stem}_{n}.ogg"))
    # the game discovers system variants by probing _1, _2, … and stops at the first gap
    have = {f.phrase_id for f in files}
    by_key: dict[str, list] = {}
    for p in project.catalog.phrases:
        if p.is_system and p.system_key:
            by_key.setdefault(p.system_key, []).append(p)
    for key, phrases in by_key.items():
        phrases.sort(key=lambda p: p.system_variant or 0)
        gap = False
        for p in phrases:
            if p.id not in have:
                gap = True
            elif gap:
                warnings.append(f"{p.id}: earlier variant of '{key}' is not recorded, the game may not find this one")
    return files, missing, warnings


def _language_value(project) -> str:
    cat = project.catalog
    if cat.language == "en":
        return "ui.options.rally.languages.english.name"
    return cat.language_label or cat.language     # shown raw by the game


def _dialect_value(dialect: str) -> str:
    dialect = (dialect or "en-US").strip() or "en-US"
    if dialect in BUILTIN_DIALECTS:
        return f"ui.options.rally.dialects.{dialect}.name"
    return dialect                                # literal (the game falls back to the raw string)


def build_voicepack_infos(project) -> tuple[dict, dict | None]:
    """(enthusiast voicepack.json, casual voicepack.json or None). Gaps clamped like the game."""
    pack = project.pack
    common = {
        "persona": (pack.persona or "").strip() or pack.slug,
        "language": _language_value(project),
        "dialect": _dialect_value(pack.dialect),
    }
    enthusiast = {
        **common,
        "compositorStyle": "english/enthusiast",
        "notebooks": ["enthusiast", "structured"],
        "styleLabel": "ui.options.rally.styles.enthusiast.name",
        "styleDetailLevel": 50,
        "enableSpeedScaling": True,
        "codriverTimingOffsetSeconds": float(pack.codriver_timing_offset_s),
        "scriptHash": project.catalog.source_script_hash,
        "linkWordGapMs": clamp_gap_ms("link_word_gap_ms", pack.link_word_gap_ms),
        "cornerGapMs": clamp_gap_ms("corner_gap_ms", pack.corner_gap_ms),
        "phraseGapMs": clamp_gap_ms("phrase_gap_ms", pack.phrase_gap_ms),
        "pacenoteGapMs": clamp_gap_ms("pacenote_gap_ms", pack.pacenote_gap_ms),
    }
    casual = None
    if pack.include_casual:
        casual = {
            **common,
            "compositorStyle": "english/casual",
            "notebooks": ["casual", "structured"],
            "styleLabel": "ui.options.rally.styles.casual.name",
            "styleDetailLevel": 10,
            "enableSpeedScaling": True,
            "codriverTimingOffsetSeconds": float(pack.codriver_timing_offset_s),
            "scriptHash": project.catalog.source_script_hash,
            # only ever our own enthusiast pack — never official packs (that would mix voices)
            "reuseAudioFrom": [pack.dirname],
            "linkWordGapMs": clamp_gap_ms("casual_link_word_gap_ms", pack.casual_link_word_gap_ms),
            "cornerGapMs": clamp_gap_ms("casual_corner_gap_ms", pack.casual_corner_gap_ms),
            "phraseGapMs": clamp_gap_ms("casual_phrase_gap_ms", pack.casual_phrase_gap_ms),
            "pacenoteGapMs": clamp_gap_ms("casual_pacenote_gap_ms", pack.casual_pacenote_gap_ms),
        }
    return enthusiast, casual


def _readme(project, n_files: int, missing: list[str]) -> str:
    pack = project.pack
    lines = [
        f"{pack.persona} - BeamNG.drive rally co-driver voice pack",
        f"Version {pack.version}" + (f" by {pack.author}" if pack.author else ""),
        "",
        r"Install: put this zip into %LOCALAPPDATA%\BeamNG\BeamNG.drive\current\mods\ ",
        "then (re)start BeamNG.drive and choose the co-driver in the rally settings.",
        "",
        f"Voice packs: {pack.dirname}" + (f", {pack.dirname_casual}" if pack.include_casual else ""),
        f"Audio files: {n_files}; phrases not recorded: {len(missing)}",
        "",
        "Made with Co-Driver Studio.",
        "",
    ]
    return "\r\n".join(lines)


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def default_zip_path(project, folder=None) -> Path:
    folder = Path(folder) if folder else (beamng_mods_dir() or project.folder)
    return folder / project.pack.zip_name


def _is_countdown(phrase) -> bool:
    return bool(phrase.is_system and phrase.system_key and phrase.system_key.startswith("countdown"))


def export_mod(project, out_zip_path, progress_cb=None, allow_missing=True, *, cancel=None) -> ExportReport:
    """Render every exportable take, encode OGG and write the mod zip.

    ``progress_cb(done, total)`` is called from the calling thread after every file.
    ``cancel``: optional ``threading.Event``; when set the export stops (ExportCancelled) and
    leaves any existing target zip untouched. Raises ExportError with a readable message on
    failure (e.g. target zip locked by a running BeamNG).
    """
    out_zip_path = Path(out_zip_path)
    if out_zip_path.suffix.lower() != ".zip":
        out_zip_path = out_zip_path.with_name(out_zip_path.name + ".zip")
    pack = project.pack
    sr = int(pack.export_sample_rate)
    files, missing, warnings = plan_export(project)
    if missing and not allow_missing:
        raise ExportError(f"{len(missing)} phrase(s) have no recorded take: " + ", ".join(missing[:10])
                          + (" …" if len(missing) > 10 else ""))
    if not files:
        raise ExportError("nothing to export: no phrase has an enabled take")
    if pack.dirname in BUILTIN_DIRNAMES or pack.dirname_casual in BUILTIN_DIRNAMES:
        raise ExportError(f"voicepack folder name {pack.dirname!r} clashes with a pack that ships with BeamNG")

    total = len(files) + 1
    done = 0

    def tick():
        if progress_cb is not None:
            progress_cb(done, total)

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise ExportCancelled("export cancelled")

    tick()
    try:
        out_zip_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ExportError(f"cannot create folder {out_zip_path.parent}: {e}") from None

    work = Path(tempfile.mkdtemp(prefix="cds_export_"))
    partial = None
    try:
        metadata: dict[str, dict] = {}
        written: list[tuple[str, str, Path]] = []
        exported_ids = set()
        for pf in files:
            check_cancel()
            try:
                y, out_sr = project.render_take(pf.phrase_id, pf.take_index, out_sr=sr)
                if len(y) == 0:
                    raise ValueError("rendered audio is empty (check the trim)")
                ogg = work / pf.basename
                dsp.write_ogg(ogg, y, out_sr)
                # the game advances the queue and force-stops clips by audioLen: measure the
                # encoded file, not the array that went into the encoder
                info = sf.info(str(ogg))
                if info.frames <= 0 or info.samplerate <= 0:
                    raise ValueError("encoded OGG is empty")
                audio_len = round(info.frames / info.samplerate, 6)
                data = ogg.read_bytes()
            except Exception as e:  # missing/corrupt wav, DSP failure, disk issue…
                warnings.append(f"{pf.phrase_id}: take {pf.take_index + 1} skipped ({e})")
                done += 1
                tick()
                continue
            metadata[pf.basename] = {
                "audioLen": audio_len,
                "sha1": hashlib.sha1(project.spoken_text(pf.phrase_id).encode("utf-8")).hexdigest(),
                "audioSha1": hashlib.sha1(data).hexdigest(),
            }
            written.append((pf.phrase_id, pf.basename, ogg))
            exported_ids.add(pf.phrase_id)
            done += 1
            tick()

        # multi-take phrases where some takes failed: renumber so names stay contiguous _1.._N
        written, metadata = _renumber(project, written, metadata)
        missing = [p.id for p in project.catalog.phrases if p.id not in exported_ids]
        if not written:
            raise ExportError("no audio could be rendered: " + "; ".join(warnings[-3:]))
        if missing and not allow_missing:
            raise ExportError(f"{len(missing)} phrase(s) could not be exported: " + ", ".join(missing[:10]))
        for pid, basename, _ in written:
            alen = metadata[basename]["audioLen"]
            if _is_countdown(project.catalog.get(pid)) and alen >= COUNTDOWN_MAX_S:
                warnings.append(f"{pid}: countdown clip is {alen:.2f} s — the game plays countdown calls "
                                f"once per second, keep it under {COUNTDOWN_MAX_S} s (trim it tighter)")

        check_cancel()
        enthusiast, casual = build_voicepack_infos(project)
        vp_dir = f"{VOICEPACKS_DIR}/{pack.dirname}"
        fd, partial_name = tempfile.mkstemp(prefix=out_zip_path.name + ".", suffix=".partial",
                                            dir=str(out_zip_path.parent))
        os.close(fd)
        partial = Path(partial_name)
        with zipfile.ZipFile(partial, "w") as zf:
            dirs = set()
            stamp = _zip_time()

            def add_dirs(entry):
                parts = entry.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    d = "/".join(parts[:i]) + "/"
                    if d not in dirs:
                        dirs.add(d)
                        zf.writestr(zipfile.ZipInfo(d, date_time=stamp), b"")

            def add_bytes(entry, data, compress):
                add_dirs(entry)
                zi = zipfile.ZipInfo(entry, date_time=stamp)
                zi.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
                zi.external_attr = 0o644 << 16
                zf.writestr(zi, data)

            add_bytes(f"{vp_dir}/voicepack.json", _json_bytes(enthusiast), True)
            add_bytes(f"{vp_dir}/metadata.json", _json_bytes(dict(sorted(metadata.items()))), True)
            for _pid, basename, path in sorted(written, key=lambda w: w[1]):
                add_bytes(f"{vp_dir}/audio/{basename}", path.read_bytes(), False)
            if casual is not None:
                add_bytes(f"{VOICEPACKS_DIR}/{pack.dirname_casual}/voicepack.json", _json_bytes(casual), True)
            add_bytes("README.txt", _readme(project, len(written), missing).encode("utf-8"), True)

        check_cancel()
        _move_into_place(partial, out_zip_path)
        partial = None
        warnings.append(RESTART_NOTICE)
        done = total
        tick()
        return ExportReport(zip_path=out_zip_path, files_written=len(written), missing=missing,
                            warnings=warnings, dirname=pack.dirname,
                            dirname_casual=pack.dirname_casual if casual is not None else None,
                            audio_lengths={b: m["audioLen"] for b, m in metadata.items()})
    except ExportError:
        raise
    except OSError as e:
        raise ExportError(f"export failed: {e}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if partial is not None:
            try:
                partial.unlink()
            except OSError:
                pass


def _renumber(project, written, metadata):
    """If some takes of a multi-take phrase failed, rename the survivors to a contiguous
    series (a single survivor becomes the plain ``<stem>.ogg``)."""
    by_pid: dict[str, list[str]] = {}
    for pid, basename, _ in written:
        by_pid.setdefault(pid, []).append(basename)
    rename = {}
    for pid, names in by_pid.items():
        phrase = project.catalog.get(pid)
        if phrase.is_single_take:
            continue
        stem = phrase.file_stem
        expected = [f"{stem}_{i}.ogg" for i in range(1, len(names) + 1)] if len(names) > 1 else [f"{stem}.ogg"]
        for old, new in zip(names, expected):  # names are in planned (take) order
            if old != new:
                rename[old] = new
    if not rename:
        return written, metadata
    out_w, out_m = [], {}
    for pid, basename, path in written:
        new = rename.get(basename, basename)
        out_w.append((pid, new, path))
        out_m[new] = metadata[basename]
    return out_w, out_m


def _zip_time():
    t = time.localtime()
    return (max(1980, t.tm_year), t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec - t.tm_sec % 2)


def _move_into_place(src: Path, dst: Path, attempts: int = 6):
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                hint = ("BeamNG.drive is running and keeps the mod open — quit the game"
                        if beamng_running() else
                        "BeamNG.drive may be running, or another program has it open — close it")
                raise ExportError(f"cannot replace {dst.name}: the file is in use. {hint} and export again.") from None
            time.sleep(0.1 * (i + 1))
        except OSError as e:
            raise ExportError(f"cannot write {dst}: {e}") from None


def beamng_running() -> bool:
    """True if ``BeamNG.drive.x64.exe`` is in ``tasklist``. Never raises (False on any problem
    or on non-Windows). Spawns a process (~50-200 ms) — don't call it every frame."""
    if not sys.platform.startswith("win"):
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {BEAMNG_EXE}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return BEAMNG_EXE.lower() in (out.stdout or "").lower()
    except Exception:
        return False


def beamng_mods_dir() -> Path | None:
    r"""``%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\mods`` if it exists, else None."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    p = Path(local) / "BeamNG" / "BeamNG.drive" / "current" / "mods"
    return p if p.is_dir() else None
