"""Project model + persistence (no Qt).

A project is a folder::

    <folder>/project.json
    <folder>/takes/<phrase_id>/take_001.wav    (raw, untrimmed, device sample rate, PCM_24)

``project.json`` is versioned (``format_version``), human readable, saved atomically and
forward tolerant: unknown keys (top level, in ``pack`` and in every take) are preserved on
save, missing keys get defaults. Take paths are stored relative to the project folder with
forward slashes.

Thread safety: every public method takes ``Project.lock`` (an RLock). Long operations
(``render_take``, ``add_take`` with auto-trim) hold it only while touching the model, not
while doing DSP, so a worker thread can export while the GUI thread edits.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import zlib
from dataclasses import dataclass, field, fields
from pathlib import Path

import numpy as np

from . import dsp
from .catalog import Catalog, load_catalog

__all__ = ["FORMAT_VERSION", "PROJECT_FILE", "Take", "PackInfo", "Project", "ProjectError",
           "slugify", "make_dirname", "clamp_gap_ms", "GAP_BOUNDS_MS", "BUILTIN_DIRNAME_PREFIXES"]

FORMAT_VERSION = 1
PROJECT_FILE = "project.json"
TAKES_DIR = "takes"

# Dirname prefixes of voicepacks shipped with BeamNG (ak_2026-04-16, rh_…, dirtwheel_…,
# fluffy_…). A user pack named e.g. "ak" would otherwise be able to shadow/clash with them.
BUILTIN_DIRNAME_PREFIXES = ("ak", "rh", "dirtwheel", "fluffy")
_SLUG_MAX = 40


class ProjectError(Exception):
    """Readable, user-facing project problem (bad folder, unreadable project.json, …)."""


def slugify(text: str, fallback: str = "codriver") -> str:
    """persona → ``[a-z0-9_]`` slug, never empty, never a BeamNG built-in pack prefix.

    Accents are folded (``"Jérôme"`` → ``"jerome"``). A leading underscore would hide the pack
    in the game (``voicepack.scan`` skips ``^_``), so underscores are stripped from both ends.
    """
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = s.encode("ascii", "ignore").decode("ascii").lower().replace("'", "")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    s = s[:_SLUG_MAX].strip("_")
    if not s:
        s = fallback
    if s in BUILTIN_DIRNAME_PREFIXES or re.fullmatch(r"(ak|rh|dirtwheel|fluffy)_\d{4}.*", s):
        s = f"{s}_custom"
    return s


def _today() -> str:
    return _dt.date.today().isoformat()


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Take:
    file: str                       # relative posix path, e.g. "takes/hard_left/take_001.wav"
    trim_start: float = 0.0
    trim_end: float = 0.0
    enabled: bool = True
    auto_trimmed: bool = True
    created: str = ""
    duration: float = 0.0           # raw file duration in seconds
    # runtime only (not saved): file missing or unreadable
    missing: bool = field(default=False, compare=False)
    # unknown keys from a newer project.json, written back unchanged
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    _SAVED = ("file", "trim_start", "trim_end", "enabled", "auto_trimmed", "created", "duration")

    @property
    def exportable(self) -> bool:
        return self.enabled and not self.missing

    def to_dict(self) -> dict:
        d = dict(self.extra)
        d.update(file=self.file, trim_start=round(float(self.trim_start), 6),
                 trim_end=round(float(self.trim_end), 6), enabled=bool(self.enabled),
                 auto_trimmed=bool(self.auto_trimmed), created=self.created,
                 duration=round(float(self.duration), 6))
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Take":
        if not isinstance(d, dict) or not isinstance(d.get("file"), str) or not d["file"]:
            raise ValueError("take entry without a file")
        extra = {k: v for k, v in d.items() if k not in cls._SAVED}
        return cls(file=d["file"].replace("\\", "/"),
                   trim_start=_as_float(d.get("trim_start"), 0.0),
                   trim_end=_as_float(d.get("trim_end"), 0.0),
                   enabled=bool(d.get("enabled", True)),
                   auto_trimmed=bool(d.get("auto_trimmed", True)),
                   created=str(d.get("created", "")),
                   duration=_as_float(d.get("duration"), 0.0),
                   extra=extra)


# Languages / dialects the game already has translation keys for
# (ui.options.rally.languages.<id>.name / ui.options.rally.dialects.<code>.name).
BUILTIN_LANGUAGES = {"english": "English", "spanish": "Spanish"}
BUILTIN_DIALECTS = {"en-US": ("english", "English (US)"),
                    "en-GB": ("english", "English (UK)"),
                    "es-ES": ("spanish", "Spanish (Spain)")}


def language_id(text: str) -> str:
    """Map user input to a builtin language id when it names one ("English", " spanish "), else keep it."""
    t = (text or "").strip()
    for key, label in BUILTIN_LANGUAGES.items():
        if t.lower() in (key, label.lower()):
            return key
    return t


def dialects_for(language: str) -> list[tuple[str, str]]:
    """[(code, label)] of builtin dialects for a language id ([] for custom languages)."""
    lang = language_id(language)
    return [(code, label) for code, (owner, label) in BUILTIN_DIALECTS.items() if owner == lang]


GAP_BOUNDS_MS = {"link_word_gap_ms": (-200, 200), "corner_gap_ms": (-200, 200), "phrase_gap_ms": (-200, 200),
                 "pacenote_gap_ms": (-100, 400), "casual_link_word_gap_ms": (-200, 200),
                 "casual_corner_gap_ms": (-200, 200), "casual_phrase_gap_ms": (-200, 200),
                 "casual_pacenote_gap_ms": (-100, 400)}
_DIRNAME_RE = re.compile(r"^(?P<slug>[a-z0-9][a-z0-9_]*?)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<hex>[0-9a-f]{6})$")


def clamp_gap_ms(name: str, value) -> int:
    """Same clamp as the game's audioTiming.lua (round half up, then bounds)."""
    lo, hi = GAP_BOUNDS_MS[name]
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    if not np.isfinite(v):
        v = 0.0
    return int(max(lo, min(hi, np.floor(v + 0.5))))


def make_dirname(persona: str) -> str:
    """``<slug>_<yyyy-mm-dd>_<6 random hex>`` — the pack's global identity in the game's VFS."""
    return f"{slugify(persona)}_{_today()}_{secrets.token_hex(3)}"


def casual_dirname_for(dirname: str) -> str:
    m = _DIRNAME_RE.match(dirname)
    if m:
        return f"{m['slug']}_casual_{m['date']}_{m['hex']}"
    return f"{dirname}_casual"


def valid_dirname(name) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 96 and re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name) is not None
            and name not in (".", ".."))


@dataclass
class PackInfo:
    persona: str = "My Co-Driver"          # literal display name, shown raw in the game's picker
    author: str = ""
    # Language shown in the game's co-driver picker: a BUILTIN_LANGUAGES id ("english", "spanish")
    # becomes the game's translation key; anything else (e.g. "Русский") is written literally.
    language: str = "english"
    dialect: str = "en-US"                 # BUILTIN_DIALECTS code → game key; anything else literal; "" = none
    # Generated ONCE at creation (``make_dirname``) and never regenerated, even if the persona
    # changes: two mods with the same dirname merge in the game, and the in-game selection is
    # remembered by dirname. Re-exports therefore always overwrite the same pack.
    dirname: str = ""
    version: str = "1.0"
    export_sample_rate: int = 48000
    link_word_gap_ms: int = -40
    corner_gap_ms: int = 0
    phrase_gap_ms: int = 0
    pacenote_gap_ms: int = 100
    include_casual: bool = True
    codriver_timing_offset_s: float = 0.0
    # --- extensions to the contract ---
    dirname_casual: str = ""               # derived from dirname when empty, then stored
    # Gap settings of the casual voicepack.json (official casual packs mostly use 0).
    casual_link_word_gap_ms: int = 0
    casual_corner_gap_ms: int = 0
    casual_phrase_gap_ms: int = 0
    casual_pacenote_gap_ms: int = 0
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        if not valid_dirname(self.dirname):
            self.dirname = make_dirname(self.persona)
        if not valid_dirname(self.dirname_casual) or self.dirname_casual == self.dirname:
            self.dirname_casual = casual_dirname_for(self.dirname)
        self.clamp_gaps()

    def clamp_gaps(self):
        for name in GAP_BOUNDS_MS:
            setattr(self, name, clamp_gap_ms(name, getattr(self, name)))

    @property
    def slug(self) -> str:
        m = _DIRNAME_RE.match(self.dirname)
        return m["slug"] if m else self.dirname

    @property
    def zip_name(self) -> str:
        """Stable file name for the mod zip (derived from the fixed dirname)."""
        return f"codriver_{self.dirname}.zip"

    # -- (de)serialisation -----------------------------------------------------------------
    def to_dict(self) -> dict:
        self.clamp_gaps()
        d = dict(self.extra)
        for f in fields(self):
            if f.name != "extra":
                d[f.name] = getattr(self, f.name)
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "PackInfo":
        d = d if isinstance(d, dict) else {}
        defaults = cls(dirname="x_2000-01-01_000000")
        kw, extra = {}, {}
        names = {f.name: f for f in fields(cls) if f.name != "extra"}
        for k, v in d.items():
            if k not in names:
                extra[k] = v
                continue
            kw[k] = _coerce(v, getattr(defaults, k))
        info = cls(**kw)
        info.extra = extra
        return info


def _as_float(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if np.isfinite(f) else default


def _coerce(v, default):
    """Coerce a JSON value to the type of ``default``; fall back to the default."""
    try:
        if isinstance(default, bool):
            return v if isinstance(v, bool) else default
        if isinstance(default, int):
            return int(v) if not isinstance(v, bool) else default
        if isinstance(default, float):
            return _as_float(v, default)
        if isinstance(default, str):
            return v if isinstance(v, str) else default
    except (TypeError, ValueError):
        return default
    return v


def _probe_audio(path: Path) -> float | None:
    """Duration in seconds if ``path`` is a readable audio file, else None (header only)."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
    except OSError:
        return None
    try:
        import soundfile as sf
        info = sf.info(str(path))
        if info.frames <= 0 or info.samplerate <= 0:
            return None
        return info.frames / info.samplerate
    except Exception:
        try:
            x, sr = dsp.read_audio(path)
            return len(x) / sr if sr and len(x) else None
        except Exception:
            return None


def _effects_from_dict(d) -> "dsp.EffectSettings":
    try:
        return dsp.EffectSettings.from_dict(d if isinstance(d, dict) else {})
    except Exception:
        return dsp.EffectSettings()


def _replace_with_retry(src: str, dst: str, attempts: int = 8):
    """os.replace with retries: on Windows AV/indexers briefly lock freshly written files."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


LOCK_FILE = ".codriver.lock"
LOCK_STALE_S = 12 * 3600          # a lock not refreshed for this long is stale (any host)
_LOCK_REFRESH_S = 600             # save() rewrites the lock timestamp at most this often


def _hostname() -> str:
    try:
        import socket
        return socket.gethostname() or "?"
    except Exception:
        return os.environ.get("COMPUTERNAME", "?")


def _pid_alive(pid: int) -> bool:
    """Is a process with this pid running on this machine? (No extra dependencies.)"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5           # ERROR_ACCESS_DENIED: exists, not ours
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259                      # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}                                          # unreadable / half written


def _lock_is_stale(info: dict, path: Path) -> bool:
    if "pid" not in info or "host" not in info:
        # empty / garbage: either being written right now (fresh) or left by a crash
        try:
            return time.time() - path.stat().st_mtime > 5.0
        except OSError:
            return True
    ts = _as_float(info.get("time"), 0.0)
    if ts <= 0:
        try:
            ts = path.stat().st_mtime
        except OSError:
            return True
    if time.time() - ts > LOCK_STALE_S:
        return True
    if str(info.get("host", "")).lower() == _hostname().lower():
        try:
            return not _pid_alive(int(info.get("pid", 0)))
        except (TypeError, ValueError):
            return True
    return False                                           # other machine, recent: trust it


def _take_number(rel: str) -> int:
    m = re.search(r"take_(\d+)\.wav$", rel, re.IGNORECASE)
    return int(m.group(1)) if m else -1


class Project:
    """See module docstring. Construct via :meth:`create` or :meth:`open`."""

    def __init__(self, folder, catalog: Catalog, pack: PackInfo | None = None, effects=None,
                 trim_sensitivity: float = 0.5, translations: dict | None = None):
        self.folder = Path(folder).resolve()
        self.catalog = catalog
        self.language = catalog.language
        self.pack = pack or PackInfo()
        self.effects = effects if effects is not None else dsp.EffectSettings()
        self.trim_sensitivity = float(trim_sensitivity)
        self.translations: dict[str, str] = dict(translations or {})
        self.created = _now_iso()
        self.load_warnings: list[str] = []
        self.lock = threading.RLock()
        self._takes: dict[str, list[Take]] = {}
        self._extra: dict = {}
        # --- multi-instance safety ---
        self.read_only = False
        self.writer_id = secrets.token_hex(8)       # random per Project instance
        self.revision = 0                           # project.json "revision" we last loaded/wrote
        self._disk_sig: tuple | None = None         # (revision, writer_id) of project.json as we last saw it
        self.last_merge_added = 0                   # takes pulled in from another writer by the last save()
        self._lock_token: str | None = None
        self._lock_written = 0.0
        # take files whose unlink failed (locked); persisted so they are never "recovered"
        self._deleted_files: list[str] = []
        # single-take phrases: file of the take that was enabled before add_take/enable displaced it
        self._prev_enabled: dict[str, str] = {}

    # ------------------------------------------------------------------ create / open / save
    @classmethod
    def create(cls, folder, persona: str, language: str = "en") -> "Project":
        folder = Path(folder)
        if (folder / PROJECT_FILE).exists():
            raise ProjectError(f"a project already exists in {folder}")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / TAKES_DIR).mkdir(exist_ok=True)
        pack = PackInfo(persona=persona or PackInfo.persona)
        proj = cls(folder, load_catalog(language), pack=pack)
        proj.save()
        return proj

    @classmethod
    def open(cls, folder, read_only: bool = False) -> "Project":
        """Load a project. ``read_only=True``: nothing is ever written (``save()`` raises
        ProjectError) — for viewing a project another instance holds the lock on.
        Does NOT take the lock; call :meth:`acquire_lock`."""
        folder = Path(folder)
        if folder.is_file() and folder.name == PROJECT_FILE:
            folder = folder.parent
        path = folder / PROJECT_FILE
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise ProjectError(f"no {PROJECT_FILE} in {folder}") from None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ProjectError(f"cannot read {path}: {e}") from None
        if not isinstance(data, dict):
            raise ProjectError(f"{path} is not a Co-Driver Studio project")
        return cls._from_dict(folder, data, read_only=read_only)

    @classmethod
    def _from_dict(cls, folder: Path, data: dict, read_only: bool = False) -> "Project":
        warnings = []
        version = data.get("format_version", FORMAT_VERSION)
        if isinstance(version, int) and version > FORMAT_VERSION:
            warnings.append(f"project was saved by a newer version (format {version}); "
                            "unknown settings are kept but ignored")
        language = data.get("language") if isinstance(data.get("language"), str) else "en"
        try:
            catalog = load_catalog(language)
        except FileNotFoundError:
            warnings.append(f"no phrase catalog for language {language!r}; using English")
            catalog = load_catalog("en")
        translations = data.get("translations")
        translations = {str(k): str(v) for k, v in translations.items()} if isinstance(translations, dict) else {}
        proj = cls(folder, catalog,
                   pack=PackInfo.from_dict(data.get("pack")),
                   effects=_effects_from_dict(data.get("effects")),
                   trim_sensitivity=min(1.0, max(0.0, _as_float(data.get("trim_sensitivity"), 0.5))),
                   translations=translations)
        proj.language = language
        proj.read_only = bool(read_only)
        if isinstance(data.get("created"), str):
            proj.created = data["created"]
        proj.revision, sig_writer = cls._disk_signature(data)
        proj._disk_sig = (proj.revision, sig_writer)
        dels = data.get("deleted_files")
        if isinstance(dels, list):
            proj._deleted_files = [d.replace("\\", "/") for d in dels if isinstance(d, str) and d]
        known = {"format_version", "app", "language", "created", "pack", "effects",
                 "trim_sensitivity", "translations", "takes", "revision", "writer_id", "deleted_files"}
        proj._extra = {k: v for k, v in data.items() if k not in known}
        takes = data.get("takes")
        if isinstance(takes, dict):
            for pid, lst in takes.items():
                if not isinstance(lst, list):
                    continue
                out = []
                for td in lst:
                    try:
                        out.append(Take.from_dict(td))
                    except Exception:
                        warnings.append(f"ignored a malformed take entry of {pid!r}")
                if out:
                    proj._takes[str(pid)] = out
        proj.refresh_missing()
        for pid, lst in proj._takes.items():
            n_missing = sum(t.missing for t in lst)
            if n_missing:
                warnings.append(f"{pid}: {n_missing} take file(s) missing or unreadable")
            if pid not in catalog:
                warnings.append(f"{pid}: takes for a phrase that is not in the catalog are kept but not exported")
        if not proj.read_only:
            proj._retry_pending_deletes()
        warnings.extend(proj._recover_orphans())
        raw_pack = data.get("pack") if isinstance(data.get("pack"), dict) else {}
        if raw_pack.get("dirname") != proj.pack.dirname:
            warnings.append(f"pack folder name was missing or invalid; generated {proj.pack.dirname!r}")
            if not proj.read_only:
                try:  # persist immediately so the identity never changes again
                    proj.save()
                except OSError:
                    pass
        proj.load_warnings = warnings
        return proj

    @staticmethod
    def _disk_signature(data: dict) -> tuple[int, str | None]:
        rev = data.get("revision", 0)
        rev = rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else 0
        wid = data.get("writer_id")
        return rev, (wid if isinstance(wid, str) else None)

    def _retry_pending_deletes(self):
        keep = []
        for rel in self._deleted_files:
            try:
                self.abs_path(rel).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                keep.append(rel)
        self._deleted_files = keep

    def _recover_orphans(self) -> list[str]:
        """Re-attach ``takes/<phrase>/take_*.wav`` files that project.json does not reference
        (e.g. written by another instance whose save was lost) as DISABLED, auto-trimmed takes."""
        warnings: list[str] = []
        root = self.folder / TAKES_DIR
        if not root.is_dir():
            return warnings
        with self.lock:
            known = {t.file.lower() for lst in self._takes.values() for t in lst}
            known |= {d.lower() for d in self._deleted_files}
        recovered = 0
        try:
            dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            return warnings
        for d in dirs:
            pid = d.name
            files = sorted((p for p in d.glob("take_*.wav")
                            if re.fullmatch(r"take_\d+\.wav", p.name, re.IGNORECASE)),
                           key=lambda p: _take_number(p.name))
            orphans = [p for p in files if f"{TAKES_DIR}/{pid}/{p.name}".lower() not in known]
            if not orphans:
                continue
            if pid not in self.catalog:
                warnings.append(f"takes/{pid}: {len(orphans)} recording(s) of an unknown phrase were ignored")
                continue
            for p in orphans:
                rel = f"{TAKES_DIR}/{pid}/{p.name}"
                try:
                    x, sr = dsp.read_audio(p)
                except Exception:
                    continue                    # empty reservation / corrupt file: leave it alone
                if not sr or len(x) == 0:
                    continue
                dur = len(x) / sr
                try:
                    start, end = dsp.auto_trim(x, sr, sensitivity=self.trim_sensitivity)
                    start, end = self._clamp_trim(float(start), float(end), dur)
                except Exception:
                    start, end = 0.0, round(dur, 6)
                try:
                    created = _dt.datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                except OSError:
                    created = _now_iso()
                take = Take(file=rel, trim_start=start, trim_end=end, enabled=False, auto_trimmed=True,
                            created=created, duration=dur)
                with self.lock:
                    self._takes.setdefault(pid, []).append(take)
                recovered += 1
        if recovered:
            warnings.append(f"Recovered {recovered} recordings that were not in the project file "
                            "(added as disabled takes)")
        return warnings

    # ------------------------------------------------------------------ lock
    @property
    def lock_path(self) -> Path:
        return self.folder / LOCK_FILE

    def acquire_lock(self) -> bool:
        """Take the single-writer lock ``<folder>/.codriver.lock`` (pid + host + time).
        Returns False if another live instance holds it. A stale lock (dead pid on this host,
        or not refreshed for 12 h) is taken over. Re-acquiring our own lock returns True."""
        with self.lock:
            path = self.lock_path
            for _ in range(3):
                info = _read_lock(path)
                if info is not None:
                    if self._lock_token and info.get("token") == self._lock_token:
                        self._write_lock(path)
                        return True
                    if not _lock_is_stale(info, path):
                        return False
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        return False
                token = secrets.token_hex(8)
                try:
                    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    continue                    # someone else won the race: re-check
                except OSError:
                    return False
                os.close(fd)
                self._lock_token = token
                try:
                    self._write_lock(path)
                except OSError:
                    self._lock_token = None
                    return False
                return True
            return False

    def _write_lock(self, path: Path):
        info = {"pid": os.getpid(), "host": _hostname(), "time": time.time(), "time_iso": _now_iso(),
                "token": self._lock_token, "writer_id": self.writer_id}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f)
        self._lock_written = time.time()

    def release_lock(self):
        """Remove the lock file if (and only if) this instance holds it."""
        with self.lock:
            token, self._lock_token = self._lock_token, None
            if not token:
                return
            info = _read_lock(self.lock_path)
            if info is not None and info.get("token") == token:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass

    @property
    def has_lock(self) -> bool:
        return self._lock_token is not None

    def lock_holder(self) -> str | None:
        """Readable description of ANOTHER live holder of the lock, else None (free, stale or ours)."""
        path = self.lock_path
        info = _read_lock(path)
        if info is None:
            return None
        if self._lock_token and info.get("token") == self._lock_token:
            return None
        if _lock_is_stale(info, path):
            return None
        host = str(info.get("host") or "?")
        since = str(info.get("time_iso") or "")
        where = "this computer" if host.lower() == _hostname().lower() else f"computer {host}"
        return f"process {info.get('pid', '?')} on {where}" + (f" since {since}" if since else "")

    def _refresh_lock(self):
        if self._lock_token and time.time() - self._lock_written > _LOCK_REFRESH_S:
            try:
                info = _read_lock(self.lock_path)
                if info is not None and info.get("token") == self._lock_token:
                    self._write_lock(self.lock_path)
            except OSError:
                pass

    def to_dict(self) -> dict:
        with self.lock:
            d = copy.deepcopy(self._extra)
            d.update({
                "format_version": FORMAT_VERSION,
                "app": "Co-Driver Studio",
                "language": self.language,
                "created": self.created,
                "pack": self.pack.to_dict(),
                "effects": self.effects.to_dict(),
                "trim_sensitivity": self.trim_sensitivity,
                "translations": dict(self.translations),
                "takes": {pid: [t.to_dict() for t in lst] for pid, lst in self._takes.items() if lst},
                "revision": self.revision,
                "writer_id": self.writer_id,
            })
            if self._deleted_files:
                d["deleted_files"] = list(self._deleted_files)
            return d

    def save(self):
        """Atomic write: ``project.json.tmp`` then ``os.replace``.

        Merge-safe: project.json is re-read first; if another writer saved since we loaded /
        last saved (its ``revision``/``writer_id`` differ from what we last saw), its takes are
        merged into ours (see :meth:`_merge_from_disk`) instead of being overwritten.
        """
        with self.lock:
            if self.read_only:
                raise ProjectError("this project is open read-only (another Co-Driver Studio window "
                                   "is using it); changes cannot be saved")
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / PROJECT_FILE
            self.last_merge_added = 0
            disk = None
            try:
                with open(path, encoding="utf-8") as f:
                    disk = json.load(f)
            except (OSError, UnicodeDecodeError, ValueError):
                disk = None
            disk_rev = 0
            if isinstance(disk, dict):
                sig = self._disk_signature(disk)
                disk_rev = sig[0]
                if sig != self._disk_sig:
                    self.last_merge_added = self._merge_from_disk(disk)
            self.revision = max(self.revision, disk_rev) + 1
            text = json.dumps(self.to_dict(), indent=1, ensure_ascii=False)
            tmp = self.folder / f"{PROJECT_FILE}.{self.writer_id}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(str(tmp), str(path))
            self._disk_sig = (self.revision, self.writer_id)
            self._refresh_lock()

    def _merge_from_disk(self, disk: dict) -> int:
        """Another writer saved project.json since we last saw it. Union of takes by file path:
        - takes only on disk (added by the other instance) are appended (disabled if a
          single-take phrase already has an enabled take here) — never dropped;
        - for takes present in both, our fields win;
        - our takes that the other writer no longer lists AND whose file is gone were deleted
          there, so they are dropped here too;
        - takes whose file no longer exists (deleted here or there) are not resurrected.
        Returns the number of takes added. Caller holds ``self.lock``."""
        dels = disk.get("deleted_files")
        if isinstance(dels, list):
            have = {d.lower() for d in self._deleted_files}
            for d in dels:
                if isinstance(d, str) and d and d.lower() not in have:
                    self._deleted_files.append(d.replace("\\", "/"))
        deleted = {d.lower() for d in self._deleted_files}
        disk_takes: dict[str, list[Take]] = {}
        raw = disk.get("takes")
        if isinstance(raw, dict):
            for pid, lst in raw.items():
                if not isinstance(lst, list):
                    continue
                for td in lst:
                    try:
                        disk_takes.setdefault(str(pid), []).append(Take.from_dict(td))
                    except Exception:
                        pass
        disk_files = {t.file.lower() for lst in disk_takes.values() for t in lst}
        # drop takes the other writer deleted (not listed there any more and file gone)
        for pid in list(self._takes):
            lst = self._takes[pid]
            keep = [t for t in lst if t.file.lower() in disk_files or _probe_audio(self.abs_path(t.file)) is not None]
            if len(keep) != len(lst):
                self._takes[pid] = keep
                if not keep:
                    del self._takes[pid]
        ours = {t.file.lower() for lst in self._takes.values() for t in lst}
        added = 0
        for pid, lst in disk_takes.items():
            for t in lst:
                key = t.file.lower()
                if key in ours or key in deleted:
                    continue
                dur = _probe_audio(self.abs_path(t.file))
                if dur is None:
                    continue                    # deleted here (or never fully written)
                mine = self._takes.setdefault(pid, [])
                if t.enabled and self._is_single(pid) and any(m.enabled for m in mine):
                    t.enabled = False
                mine.append(t)
                ours.add(key)
                added += 1
        return added

    # ------------------------------------------------------------------ queries
    def abs_path(self, rel: str) -> Path:
        return self.folder / Path(*rel.split("/"))

    def spoken_text(self, phrase_id: str) -> str:
        t = self.translations.get(phrase_id)
        if t and t.strip():
            return t
        return self.catalog.get(phrase_id).text

    def takes(self, phrase_id: str) -> list[Take]:
        """The take list of a phrase (live objects — treat as read-only, use the setters)."""
        with self.lock:
            return list(self._takes.get(phrase_id, []))

    def exportable_takes(self, phrase_id: str) -> list[tuple[int, Take]]:
        """(index, take) pairs that would be exported (system phrases: at most one)."""
        with self.lock:
            out = [(i, t) for i, t in enumerate(self._takes.get(phrase_id, [])) if t.exportable]
            if out and self._is_single(phrase_id):
                out = out[:1]
            return out

    def status(self, phrase_id: str) -> str:
        """"missing" | "recorded" | "disabled" (usable takes exist but none enabled)."""
        with self.lock:
            usable = [t for t in self._takes.get(phrase_id, []) if not t.missing]
            if not usable:
                return "missing"
            return "recorded" if any(t.enabled for t in usable) else "disabled"

    def progress(self) -> tuple[int, int]:
        with self.lock:
            done = sum(1 for p in self.catalog.phrases if self.status(p.id) == "recorded")
            return done, len(self.catalog.phrases)

    def refresh_missing(self):
        """Re-check every take file on disk (header probe only; cheap)."""
        with self.lock:
            items = [t for lst in self._takes.values() for t in lst]
        for t in items:
            dur = _probe_audio(self.abs_path(t.file))
            t.missing = dur is None
            if dur is not None and t.duration <= 0:
                t.duration = dur
                if t.trim_end <= 0:
                    t.trim_end = dur

    # ------------------------------------------------------------------ editing
    def _get(self, phrase_id: str, take_index: int) -> Take:
        lst = self._takes.get(phrase_id, [])
        if not 0 <= take_index < len(lst):
            raise IndexError(f"{phrase_id}: no take #{take_index}")
        return lst[take_index]

    def _is_single(self, phrase_id: str) -> bool:
        """System / fixed-name phrases: only one take can be enabled (and exported)."""
        return phrase_id in self.catalog and self.catalog.get(phrase_id).is_single_take

    def _next_take_file(self, phrase_id: str) -> str:
        """Next free ``take_NNN.wav`` (above every number in the model or on disk), reserved
        on disk with an exclusive create so a concurrent instance can never pick the same name."""
        d = self.folder / TAKES_DIR / phrase_id
        d.mkdir(parents=True, exist_ok=True)
        used = {_take_number(t.file) for t in self._takes.get(phrase_id, [])}
        used |= {_take_number(f) for f in self._deleted_files}
        for p in d.glob("take_*.wav"):
            if re.fullmatch(r"take_\d+\.wav", p.name, re.IGNORECASE):
                used.add(_take_number(p.name))
        n = max(used, default=0) + 1
        for _ in range(10000):
            rel = f"{TAKES_DIR}/{phrase_id}/take_{n:03d}.wav"
            try:
                fd = os.open(str(self.abs_path(rel)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                n += 1
                continue
            os.close(fd)
            return rel
        raise ProjectError(f"cannot allocate a take file in {d}")

    def add_take(self, phrase_id: str, x: np.ndarray, sr: int, auto_trim: bool = True) -> Take:
        """Write the raw take as WAV, auto-trim it, append it and save. Returns the new Take.

        For single-take phrases (system notes, fixed-variant phrases) the new take becomes the
        (only) enabled one.
        """
        self.catalog.get(phrase_id)  # KeyError for unknown phrases
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if x.size == 0:
            raise ValueError("cannot add an empty take")
        sr = int(sr)
        dur = len(x) / sr
        if auto_trim:
            start, end = dsp.auto_trim(x, sr, sensitivity=self.trim_sensitivity)
            start, end = self._clamp_trim(float(start), float(end), dur)
        else:
            start, end = 0.0, dur
        with self.lock:
            if self.read_only:
                raise ProjectError("this project is open read-only; cannot record into it")
            rel = self._next_take_file(phrase_id)
            path = self.abs_path(rel)
            try:
                dsp.write_wav(path, x, sr)
            except BaseException:
                try:
                    path.unlink()               # drop the empty reservation
                except OSError:
                    pass
                raise
            take = Take(file=rel, trim_start=start, trim_end=end, enabled=True,
                        auto_trimmed=bool(auto_trim), created=_now_iso(), duration=dur)
            lst = self._takes.setdefault(phrase_id, [])
            if self._is_single(phrase_id):
                self._remember_displaced(phrase_id, lst)
                for t in lst:
                    t.enabled = False
            lst.append(take)
            self.save()
            return take

    def _remember_displaced(self, phrase_id: str, lst: list[Take]):
        """Single-take phrase: remember which take was enabled before a new one displaces it."""
        cur = next((t for t in lst if t.enabled), None)
        if cur is not None:
            self._prev_enabled[phrase_id] = cur.file
        else:
            self._prev_enabled.pop(phrase_id, None)

    def delete_take(self, phrase_id: str, take_index: int):
        """Remove a take and delete its file. Single-take phrase: if the enabled take was deleted,
        the take that was enabled before it (if it still exists) becomes enabled again, else
        the newest remaining usable take."""
        with self.lock:
            if self.read_only:
                raise ProjectError("this project is open read-only; cannot delete takes")
            take = self._get(phrase_id, take_index)
            lst = self._takes[phrase_id]
            del lst[take_index]
            if self._is_single(phrase_id):
                prev = self._prev_enabled.pop(phrase_id, None)
                if take.enabled and lst and not any(t.enabled for t in lst):
                    restore = next((t for t in lst if prev and t.file == prev and not t.missing), None)
                    if restore is None:
                        usable = [t for t in lst if not t.missing] or lst
                        restore = usable[-1]
                    restore.enabled = True
                elif prev and any(t.file == prev for t in lst) and not take.enabled:
                    self._prev_enabled[phrase_id] = prev   # unrelated delete: keep the memory
            if not lst:
                del self._takes[phrase_id]
            try:
                self.abs_path(take.file).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # locked (e.g. being played): remember it so it is never reused or "recovered",
                # and retry deleting on the next open
                if take.file not in self._deleted_files:
                    self._deleted_files.append(take.file)
            self.save()

    def set_take_enabled(self, phrase_id: str, take_index: int, enabled: bool):
        with self.lock:
            take = self._get(phrase_id, take_index)
            if enabled and self._is_single(phrase_id) and not take.enabled:
                self._remember_displaced(phrase_id, self._takes[phrase_id])
            take.enabled = bool(enabled)
            if enabled and self._is_single(phrase_id):
                for i, t in enumerate(self._takes[phrase_id]):
                    if i != take_index:
                        t.enabled = False
            self.save()

    @staticmethod
    def _clamp_trim(start: float, end: float, duration: float) -> tuple[float, float]:
        if duration <= 0:
            return 0.0, 0.0
        if not np.isfinite(start):
            start = 0.0
        if not np.isfinite(end):
            end = duration
        if end < start:
            start, end = end, start
        start = min(max(0.0, start), duration)
        end = min(max(0.0, end), duration)
        min_len = min(0.01, duration)
        if end - start < min_len:
            end = min(duration, start + min_len)
            start = max(0.0, end - min_len)
        return round(start, 6), round(end, 6)  # same precision as project.json

    def set_trim(self, phrase_id: str, take_index: int, start: float, end: float):
        with self.lock:
            take = self._get(phrase_id, take_index)
            take.trim_start, take.trim_end = self._clamp_trim(float(start), float(end), take.duration)
            take.auto_trimmed = False
            self.save()

    def reauto_trim(self, phrase_id: str, take_index: int, sensitivity: float | None = None):
        """Re-run auto trim on the raw audio (reads the WAV; a few ms per take)."""
        sens = self.trim_sensitivity if sensitivity is None else float(sensitivity)
        x, sr = self.load_take_audio(phrase_id, take_index)
        start, end = dsp.auto_trim(x, sr, sensitivity=sens)
        with self.lock:
            take = self._get(phrase_id, take_index)
            take.duration = len(x) / sr
            take.trim_start, take.trim_end = self._clamp_trim(float(start), float(end), take.duration)
            take.auto_trimmed = True
            self.save()
        return take.trim_start, take.trim_end

    def set_translation(self, phrase_id: str, text: str | None):
        with self.lock:
            if text and text.strip():
                self.translations[phrase_id] = text
            else:
                self.translations.pop(phrase_id, None)
            self.save()

    # ------------------------------------------------------------------ audio
    def load_take_audio(self, phrase_id: str, take_index: int) -> tuple[np.ndarray, int]:
        """Raw take audio. Marks the take missing and re-raises if the file is unreadable."""
        with self.lock:
            take = self._get(phrase_id, take_index)
            path = self.abs_path(take.file)
        try:
            x, sr = dsp.read_audio(path)
        except Exception:
            take.missing = True
            raise
        if len(x) == 0:
            take.missing = True
            raise ValueError(f"take file is empty: {path}")
        take.missing = False
        return x, sr

    def take_seed(self, phrase_id: str, take_index: int) -> int:
        """Deterministic effect seed per take (stable across sessions)."""
        with self.lock:
            return zlib.crc32(self._get(phrase_id, take_index).file.encode("utf-8")) & 0x7FFFFFFF

    def render_take(self, phrase_id: str, take_index: int, out_sr: int | None = None) -> tuple[np.ndarray, int]:
        """Trimmed + effected audio at ``out_sr`` (default: pack export rate). CPU heavy-ish
        (tens of ms); call off the GUI thread for batches."""
        out_sr = int(out_sr or self.pack.export_sample_rate)
        with self.lock:
            take = self._get(phrase_id, take_index)
            start, end = take.trim_start, take.trim_end
            effects = self.effects
            seed = self.take_seed(phrase_id, take_index)
        x, sr = self.load_take_audio(phrase_id, take_index)
        dur = len(x) / sr
        if end <= 0 or end > dur + 1e-6:
            end = dur
        start, end = self._clamp_trim(start, end, dur)
        y = dsp.render_take(x, sr, start, end, effects, out_sr=out_sr, seed=seed)
        return np.asarray(y, dtype=np.float32).reshape(-1), out_sr
