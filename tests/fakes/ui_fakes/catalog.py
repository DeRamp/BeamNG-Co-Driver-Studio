import json
import pathlib
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Phrase:
    id: str
    text: str
    category: str
    file_stem: str
    system_key: str | None = None
    system_variant: int | None = None
    single_take: bool = False
    hint: str | None = None

    @property
    def is_system(self):
        return self.category == "system"

    @property
    def is_single_take(self):
        return self.single_take or self.is_system


@dataclass(frozen=True)
class Catalog:
    language: str
    language_label: str
    source_script_hash: str
    categories: list
    phrases: list

    def get(self, phrase_id):
        for p in self.phrases:
            if p.id == phrase_id:
                return p
        raise KeyError(phrase_id)

    def by_category(self, cat):
        return [p for p in self.phrases if p.category == cat]


def resource_path(*parts):
    base = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parents[3] / "codriver_studio"))
    return base.joinpath(*parts)


def load_catalog(language="en"):
    d = json.loads(resource_path("data", f"phrases_{language}.json").read_text("utf8"))
    return Catalog(d["language"], d["language_label"], d["source_script_hash"], d["categories"],
                   [Phrase(**p) for p in d["phrases"]])
