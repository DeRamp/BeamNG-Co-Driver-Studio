"""Logging + global exception hook.

The frozen exe has no console, so every uncaught exception is written to
%LOCALAPPDATA%\\CoDriverStudio\\log.txt and shown in a dialog instead of killing the app.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys
import threading
import traceback

from PySide6.QtCore import QObject, Qt, QThread, Signal, QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QMessageBox

log = logging.getLogger("codriver_studio")


def log_dir() -> pathlib.Path:
    base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home())
    d = pathlib.Path(base) / "CoDriverStudio"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def log_path() -> pathlib.Path:
    return log_dir() / "log.txt"


def setup_logging() -> None:
    if getattr(setup_logging, "_done", False):
        return
    setup_logging._done = True
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        p = log_path()
        # keep the log small: start fresh if it grew beyond 2 MB
        if p.exists() and p.stat().st_size > 2_000_000:
            p.unlink()
        fh = logging.FileHandler(p, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)

    def qt_handler(mode, ctx, msg):
        if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            # these two are noise from the offscreen platform / font fallback
            if "propagateSizeHints" in msg or "This plugin does not support" in msg:
                return
            log.warning("Qt: %s", msg)

    qInstallMessageHandler(qt_handler)


class _Bridge(QObject):
    show = Signal(str, str)


_bridge: _Bridge | None = None
_showing = False


def _show_dialog(summary: str, details: str) -> None:
    global _showing
    if _showing or QApplication.instance() is None:
        return
    _showing = True
    try:
        box = QMessageBox(QApplication.activeWindow())
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Something went wrong")
        box.setText("Co-Driver Studio hit an unexpected error, but your recordings are safe "
                    "(everything is saved automatically).")
        box.setInformativeText(f"{summary}\n\nDetails were written to:\n{log_path()}")
        box.setDetailedText(details)
        box.exec()
    finally:
        _showing = False


def report_exception(exc_type, exc, tb) -> None:
    details = "".join(traceback.format_exception(exc_type, exc, tb))
    log.error("Uncaught exception:\n%s", details)
    summary = f"{exc_type.__name__}: {exc}"
    if _bridge is not None:
        # queued if we're not on the GUI thread
        _bridge.show.emit(summary, details)


def install_exception_hook() -> None:
    global _bridge
    setup_logging()
    if _bridge is None and QApplication.instance() is not None:
        _bridge = _Bridge()
        _bridge.show.connect(_show_dialog, Qt.QueuedConnection)

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        report_exception(exc_type, exc, tb)

    sys.excepthook = hook
    threading.excepthook = lambda a: report_exception(a.exc_type, a.exc_value, a.exc_traceback)


def is_gui_thread() -> bool:
    app = QApplication.instance()
    return app is not None and QThread.currentThread() is app.thread()
