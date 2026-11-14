"""Persistent app settings (QSettings, INI file under the user's AppData)."""
from __future__ import annotations

import os
import pathlib

from PySide6.QtCore import QSettings, QStandardPaths

ORG = "CoDriverStudio"
APP = "CoDriverStudio"
MAX_RECENT = 8


def _standard_dir(loc, fallback: str) -> str:
    """Real user folder from the OS (follows OneDrive / folder redirection), not ~/<fallback>."""
    try:
        p = QStandardPaths.writableLocation(loc)
    except Exception:  # noqa: BLE001
        p = ""
    return p or str(pathlib.Path.home() / fallback)


def documents_dir() -> str:
    return _standard_dir(QStandardPaths.DocumentsLocation, "Documents")


def desktop_dir() -> str:
    return _standard_dir(QStandardPaths.DesktopLocation, "Desktop")


def settings() -> QSettings:
    custom = os.environ.get("CDS_SETTINGS_DIR")   # tests / portable use
    if custom:
        return QSettings(os.path.join(custom, "settings.ini"), QSettings.IniFormat)
    return QSettings(QSettings.IniFormat, QSettings.UserScope, ORG, APP)


def get(key: str, default=None, type_=None):
    s = settings()
    if type_ is bool:
        v = s.value(key, default)
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes")
        return bool(v) if v is not None else default
    if type_ is not None:
        try:
            v = s.value(key, default)
            return type_(v) if v is not None and v != "" else default
        except (TypeError, ValueError):
            return default
    return s.value(key, default)


def put(key: str, value) -> None:
    s = settings()
    if value is None:
        s.remove(key)
    else:
        s.setValue(key, value)
    s.sync()


def recent_projects() -> list[str]:
    v = settings().value("recent_projects", [])
    if isinstance(v, str):
        v = [v] if v else []
    return [str(p) for p in (v or [])]


def add_recent(folder) -> None:
    folder = str(pathlib.Path(folder))
    lst = [p for p in recent_projects() if pathlib.Path(p) != pathlib.Path(folder)]
    lst.insert(0, folder)
    put("recent_projects", lst[:MAX_RECENT])
    put("last_project", folder)


def remove_recent(folder) -> None:
    folder = str(pathlib.Path(folder))
    put("recent_projects", [p for p in recent_projects() if pathlib.Path(p) != pathlib.Path(folder)])
    if get("last_project") == folder:
        put("last_project", None)
