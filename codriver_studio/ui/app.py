"""Application bootstrap."""
from __future__ import annotations

import os
import pathlib
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from . import settings as st
from . import theme
from .errors import install_exception_hook, log, setup_logging


def make_app(argv=None) -> QApplication:
    app = QApplication.instance()
    if app is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(list(argv if argv is not None else sys.argv))
        app.setApplicationName("Co-Driver Studio")
        app.setOrganizationName(st.ORG)
        theme.apply_theme(app)
        app.setWindowIcon(theme.app_icon())
    install_exception_hook()
    return app


def run(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    setup_logging()
    log.info("Co-Driver Studio starting (%s)", " ".join(argv))
    app = make_app(argv)
    from .main_window import MainWindow
    win = MainWindow()
    win.show()

    args = [a for a in argv[1:] if not a.startswith("-")]
    target = None
    if args and (pathlib.Path(args[0]) / "project.json").exists():
        target = args[0]
    elif "--welcome" not in argv:
        last = st.get("last_project")
        if last and (pathlib.Path(last) / "project.json").exists():
            target = last
    if target:
        QTimer.singleShot(0, lambda: win.open_project(target))
    quit_after = os.environ.get("CDS_QUIT_AFTER_MS")
    if quit_after:
        QTimer.singleShot(int(quit_after), app.quit)
    return app.exec()
