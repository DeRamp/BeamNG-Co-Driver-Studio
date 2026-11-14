"""Phrase catalogs (what the co-driver has to record).

A catalog is generated from the game install by ``tools/build_catalog.py`` and shipped as
``codriver_studio/data/phrases_<language>.json``.  ``Phrase.id`` is stable and
language-independent; it is the key used everywhere else (project, exporter, UI).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

__all__ = ["Phrase", "Catalog", "load_catalog", "resource_path", "available_languages"]

_PACKAGE_DIR = Path(__file__).resolve().parent
_PACKAGE_NAME = _PACKAGE_DIR.name  # "codriver_studio"


def _resource_roots() -> list[Path]:
    """Candidate base directories that may contain the package's data files.

    Order: PyInstaller bundle (``sys._MEIPASS``, one-folder or one-file), the folder next to a
    frozen executable (``_internal`` layout of PyInstaller >= 6 and the older flat layout),
    then the source tree.
    """
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / _PACKAGE_NAME)
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots += [exe_dir / "_internal" / _PACKAGE_NAME, exe_dir / _PACKAGE_NAME,
                  exe_dir / "_internal", exe_dir]
    roots.append(_PACKAGE_DIR)
    seen, out = set(), []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def resource_path(*parts: str) -> Path:
    """Absolute path of a bundled resource, e.g. ``resource_path("data", "phrases_en.json")``.

    Paths are relative to the ``codriver_studio`` package. Works from source and from a
    PyInstaller build as long as the spec maps the data as
    ``datas=[("codriver_studio/data", "codriver_studio/data")]`` (other common layouts are
    probed too). If nothing exists, the most likely location is returned so the caller's
    error message names a sensible path.
    """
    roots = _resource_roots()
    for root in roots:
        p = root.joinpath(*parts)
        if p.exists():
            return p
    return roots[0].joinpath(*parts)


@dataclass(frozen=True)
class Phrase:
    id: str
    text: str
    category: str
    file_stem: str
    system_key: str | None = None
    system_variant: int | None = None
    # The game requests one fixed file name for this phrase (system notes, and phrases the
    # compositor asks for with a fixed variant such as "over crest" → pacenote_over_crest_2.ogg):
    # exactly one take is exported, as ``<file_stem>.ogg``.
    single_take: bool = False
    hint: str | None = None          # recording hint shown by the UI (e.g. which "over crest")

    @property
    def is_system(self) -> bool:
        return self.category == "system"

    @property
    def is_single_take(self) -> bool:
        return self.single_take or self.is_system


@dataclass(frozen=True)
class Catalog:
    language: str
    language_label: str
    source_script_hash: str
    categories: list[str]
    phrases: list[Phrase]
    _index: dict = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self):
        index: dict[str, Phrase] = {}
        for p in self.phrases:
            if p.id in index:
                raise ValueError(f"duplicate phrase id in catalog: {p.id!r}")
            index[p.id] = p
        object.__setattr__(self, "_index", index)

    def get(self, phrase_id: str) -> Phrase:
        try:
            return self._index[phrase_id]
        except KeyError:
            raise KeyError(phrase_id) from None

    def __contains__(self, phrase_id) -> bool:
        return phrase_id in self._index

    def __len__(self) -> int:
        return len(self.phrases)

    def __iter__(self):
        return iter(self.phrases)

    @property
    def ids(self) -> list[str]:
        return [p.id for p in self.phrases]

    def by_category(self, cat: str) -> list[Phrase]:
        return [p for p in self.phrases if p.category == cat]


def catalog_from_dict(d: dict) -> Catalog:
    phrases = [Phrase(id=str(p["id"]), text=str(p["text"]), category=str(p["category"]),
                      file_stem=str(p["file_stem"]), system_key=p.get("system_key"),
                      system_variant=(int(p["system_variant"]) if p.get("system_variant") is not None else None),
                      single_take=bool(p.get("single_take", False)),
                      hint=(str(p["hint"]) if p.get("hint") else None))
               for p in d["phrases"]]
    cats = list(d.get("categories") or [])
    for p in phrases:  # tolerate catalogs whose category list is incomplete
        if p.category not in cats:
            cats.append(p.category)
    return Catalog(language=str(d.get("language", "en")), language_label=str(d.get("language_label", "")),
                   source_script_hash=str(d.get("source_script_hash", "")), categories=cats, phrases=phrases)


@lru_cache(maxsize=None)
def load_catalog(language: str = "en") -> Catalog:
    """Load ``data/phrases_<language>.json``. Cached (catalogs are immutable)."""
    path = resource_path("data", f"phrases_{language}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"phrase catalog for language {language!r} not found: {path}") from None
    return catalog_from_dict(data)


def available_languages() -> list[str]:
    data_dir = resource_path("data")
    if not data_dir.is_dir():
        return []
    return sorted(p.stem[len("phrases_"):] for p in data_dir.glob("phrases_*.json"))
