import os
import pathlib
import time
import zipfile
from dataclasses import dataclass, field


@dataclass
class ExportReport:
    zip_path: pathlib.Path
    files_written: int
    missing: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def missing_phrases(project):
    return [p.id for p in project.catalog.phrases if project.status(p.id) != "recorded"]


def export_mod(project, out_zip_path, progress_cb=None, allow_missing=True):
    missing = missing_phrases(project)
    if missing and not allow_missing:
        raise ValueError("missing phrases")
    n = 0
    ids = [p.id for p in project.catalog.phrases]
    pathlib.Path(out_zip_path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip_path, "w") as z:
        for i, pid in enumerate(ids):
            if progress_cb:
                progress_cb(i + 1, len(ids))
            if pid in missing:
                continue
            z.writestr(f"audio/{pid}.txt", "x")
            n += 1
        time.sleep(0.05)
    return ExportReport(pathlib.Path(out_zip_path), n, missing, [])


def beamng_mods_dir():
    p = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "BeamNG" / "BeamNG.drive" / "current" / "mods"
    return p if p.is_dir() else None
