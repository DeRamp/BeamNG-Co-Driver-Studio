"""Main window: welcome screen or the project workspace (nav + pages + top bar)."""
from __future__ import annotations

import os
import pathlib

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QAction, QFont, QFontMetrics, QKeySequence
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import __version__
from .. import project as project_mod
from . import settings as st
from . import theme
from .audio import AudioService
from .audio_settings import AudioSettingsDialog
from .controller import Controller
from .errors import log, log_dir
from .pages.effects import EffectsPage
from .pages.export import ExportPage, open_in_explorer
from .pages.phrases import PhrasesPage
from .pages.record import KEYS_HELP, RecordPage
from .pages.sample_calls import SampleCallsPage
from .welcome import NewProjectDialog, WelcomePage
from .widgets import ElidedLabel, LevelMeter, PageScrollArea

MIN_WINDOW = QSize(960, 560)
PAGES = ["Phrases", "Record", "Effects", "Sample Calls", "Pack & Export"]
NAV_ICONS = ["☰", "●", "≋", "▶", "⇩"]


class Workspace(QWidget):
    def __init__(self, ctl, window, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # top bar
        top = QWidget()
        top.setObjectName("TopBar")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(16, 10, 16, 10)
        tl.setSpacing(12)
        brand = QLabel("CO-DRIVER STUDIO")
        brand.setObjectName("Brand")
        self.title = ElidedLabel("")
        self.title.setObjectName("ProjectTitle")
        tl.addWidget(brand)
        tl.addWidget(self.title)
        self.ro_badge = QLabel("READ-ONLY")
        self.ro_badge.setToolTip("This project is open in another Co-Driver Studio window. "
                                 "You can browse, listen and export, but not change anything here.")
        self.ro_badge.setStyleSheet(f"color: {theme.AMBER}; font-weight: 700; letter-spacing: 1px;")
        self.ro_badge.setVisible(ctl.read_only)
        tl.addWidget(self.ro_badge)
        tl.addStretch(1)
        self.progress = QProgressBar()
        fm = self.progress.fontMetrics()
        # wide enough for "180 / 180 recorded" in the bold progress font, shrinks before the title clips
        self.progress.setMinimumWidth(fm.horizontalAdvance("0000 / 0000 recorded") + 24)
        self.progress.setMaximumWidth(fm.horizontalAdvance("M") * 18)
        self.progress.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.progress.setTextVisible(True)
        self.progress.setToolTip("Phrases with at least one ticked take")
        tl.addWidget(self.progress)
        tl.addSpacing(10)
        mic = QLabel("Mic")
        mic.setProperty("muted", True)
        tl.addWidget(mic)
        self.meter = LevelMeter(compact=True)
        self.meter.setFixedWidth(self.meter.fontMetrics().horizontalAdvance("M") * 8)
        tl.addWidget(self.meter)
        self.btn_audio = QPushButton("Audio settings")
        self.btn_audio.setToolTip("Choose microphone and speakers")
        self.btn_audio.clicked.connect(window.open_audio_settings)
        tl.addWidget(self.btn_audio)
        lay.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_font = QFont(self.font())
        nav_font.setPointSizeF(11)                       # #NavList font-size in the theme
        nfm = QFontMetrics(nav_font)
        item_h = nfm.lineSpacing() + 22                  # item padding 11 + 11 (theme)
        for i, (name, icon) in enumerate(zip(PAGES, NAV_ICONS)):
            it = QListWidgetItem(f"{icon}   {name}")
            it.setToolTip(f"{name}  (Ctrl+{i + 1})")
            it.setSizeHint(QSize(nfm.horizontalAdvance(it.text()) + 40, item_h))
            self.nav.addItem(it)
        # widest entry + item padding (16 + 14) + selection border (3) + some air
        self.nav.setFixedWidth(max(nfm.horizontalAdvance(f"{ic}   {n}") for ic, n in zip(NAV_ICONS, PAGES)) + 64)
        body.addWidget(self.nav)
        self.stack = QStackedWidget()
        self.phrases = PhrasesPage(ctl)
        self.record = RecordPage(ctl)
        self.effects = EffectsPage(ctl)
        self.samples = SampleCallsPage(ctl)
        self.export = ExportPage(ctl)
        self.pages = [self.phrases, self.record, self.effects, self.samples, self.export]
        for p in self.pages:
            # a page never gets squeezed below its minimum size: on a small screen (e.g. 1080p at
            # 150 % scaling) or a small window it scrolls instead
            self.stack.addWidget(PageScrollArea(p))
        body.addWidget(self.stack, 1)
        lay.addLayout(body, 1)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.record.settingsRequested.connect(window.open_audio_settings)
        ctl.progressChanged.connect(self._progress)
        ctl.projectChanged.connect(self._title)
        ctl.audio.level.connect(self.meter.set_level)
        ctl.audio.clipped.connect(self.meter.mark_clip)
        ctl.recordRequested.connect(self.go_record)
        ctl.openPhraseRequested.connect(self.go_phrase)
        self._progress(*ctl.progress())
        self._title()
        if ctl.read_only:
            for p in (self.phrases.panel, self.record, self.effects, self.export):
                p.set_read_only(True)

    def _title(self):
        self.title.setText(self.ctl.project.pack.persona)

    def _progress(self, done, total):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        self.progress.setFormat(f"{done} / {total} recorded")

    def show_page(self, i):
        self.nav.setCurrentRow(i)

    def go_record(self, pid):
        self.show_page(1)
        self.record.open_phrase(pid)

    def go_phrase(self, pid, idx=-1):
        self.show_page(0)
        self.phrases.open_phrase(pid, idx)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(theme.app_icon())
        self.resize(1320, 860)
        # small enough for 1366×768 at 125 % (≈1093×580 logical work area); pages scroll below their
        # own minimum instead of overlapping
        self.setMinimumSize(MIN_WINDOW)
        self.audio = AudioService(self)
        self.ctl: Controller | None = None
        self.workspace: Workspace | None = None
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.welcome = WelcomePage()
        self.welcome.newRequested.connect(self.new_project)
        self.welcome.openRequested.connect(self.open_project_dialog)
        self.welcome.recentRequested.connect(self.open_project)
        self._welcome_scroll = PageScrollArea(self.welcome)
        self.stack.addWidget(self._welcome_scroll)
        self._build_menu()
        self._update_title()
        geo = st.get("window/geometry")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)

    # ------------------------------------------------------------------ menu
    def _build_menu(self):
        mb = self.menuBar()
        m = mb.addMenu("&Project")
        self.act_new = m.addAction("New voice pack…", self.new_project, QKeySequence.New)
        self.act_open = m.addAction("Open project…", self.open_project_dialog, QKeySequence.Open)
        self.recent_menu = m.addMenu("Recent projects")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        self.act_folder = m.addAction("Show project folder", self._open_project_folder)
        self.act_close = m.addAction("Close project", self.close_project)
        m.addSeparator()
        m.addAction("Quit", self.close, QKeySequence.Quit)
        v = mb.addMenu("&Go")
        self.page_actions = []
        for i, name in enumerate(PAGES):
            a = QAction(name, self)
            a.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
            a.triggered.connect(lambda _=False, i=i: self.workspace and self.workspace.show_page(i))
            v.addAction(a)
            self.page_actions.append(a)
        a = mb.addMenu("&Audio")
        a.addAction("Audio settings…", self.open_audio_settings, QKeySequence("Ctrl+,"))
        h = mb.addMenu("&Help")
        h.addAction("Keyboard shortcuts", self._shortcuts, QKeySequence("F1"))
        h.addAction("Open log folder", lambda: open_in_explorer(log_dir()))
        h.addAction("About", self._about)
        self._update_actions()

    def _update_actions(self):
        has = self.workspace is not None
        for a in (self.act_folder, self.act_close, *self.page_actions):
            a.setEnabled(has)

    def _fill_recent(self):
        self.recent_menu.clear()
        rec = st.recent_projects()
        if not rec:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for p in rec:
            self.recent_menu.addAction(p, lambda p=p: self.open_project(p))

    def _update_title(self):
        if self.ctl:
            ro = "  (read-only)" if self.ctl.read_only else ""
            self.setWindowTitle(f"{self.ctl.project.pack.persona}{ro} · Co-Driver Studio")
        else:
            self.setWindowTitle("Co-Driver Studio")

    # ------------------------------------------------------------------ projects
    def new_project(self):
        dlg = NewProjectDialog(self)
        if dlg.exec() != NewProjectDialog.Accepted:
            return
        self.create_project(dlg.result_folder, dlg.result_name)

    def create_project(self, folder, persona):
        try:
            proj = project_mod.Project.create(pathlib.Path(folder), persona)
        except Exception as e:  # noqa: BLE001
            log.exception("create project failed")
            QMessageBox.critical(self, "Couldn't create project", str(e))
            return None
        proj = self._claim(proj, pathlib.Path(folder))
        if proj is None:
            return None
        self._set_project(proj)
        self.workspace.show_page(1)
        return proj

    def open_project_dialog(self):
        start = st.get("last_project") or st.documents_dir()
        d = QFileDialog.getExistingDirectory(self, "Open a Co-Driver Studio project folder", start)
        if d:
            self.open_project(d)

    def open_project(self, folder):
        folder = pathlib.Path(folder)
        if not (folder / "project.json").exists():
            r = QMessageBox.question(self, "Not a project",
                                     f"{folder}\n\ndoesn't contain a Co-Driver Studio project (project.json). "
                                     "Remove it from the recent list?", QMessageBox.Yes | QMessageBox.No)
            if r == QMessageBox.Yes:
                st.remove_recent(folder)
                self.welcome.refresh()
            return None
        if self.ctl is not None and pathlib.Path(self.ctl.project.folder) == folder.resolve():
            return self.ctl.project          # already open in this window
        try:
            proj = project_mod.Project.open(folder)
        except Exception as e:  # noqa: BLE001
            log.exception("open project failed")
            QMessageBox.critical(self, "Couldn't open project", f"{folder}\n\n{e}")
            return None
        proj = self._claim(proj, folder)
        if proj is None:
            return None
        self._set_project(proj)
        warns = list(getattr(proj, "load_warnings", []) or [])
        if warns:
            QMessageBox.warning(self, "Project opened with warnings", "\n".join(f"• {w}" for w in warns[:15]))
        return proj

    def _claim(self, proj, folder):
        """Take the project's single-writer lock; if another live instance holds it, offer to
        open it read-only. Returns the project to use, or None (cancelled)."""
        acquire = getattr(proj, "acquire_lock", None)
        if acquire is None:
            return proj
        try:
            if acquire():
                return proj
        except Exception as e:  # noqa: BLE001 - e.g. unwritable folder: still usable, unprotected
            log.warning("acquire_lock failed: %s", e)
            return proj
        holder = None
        try:
            holder = proj.lock_holder()
        except Exception:  # noqa: BLE001
            pass
        holder = holder or "another Co-Driver Studio window"
        r = QMessageBox.question(
            self, "Project already open",
            f"{folder}\n\nis already open in {holder}.\n\nTwo windows editing the same project could "
            "overwrite each other's work. Open it read-only here (browse, listen, export; no recording "
            "or editing)?", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r != QMessageBox.Yes:
            return None
        try:
            return project_mod.Project.open(folder, read_only=True)
        except Exception as e:  # noqa: BLE001
            log.exception("read-only open failed")
            QMessageBox.critical(self, "Couldn't open project", f"{folder}\n\n{e}")
            return None

    @staticmethod
    def _release(proj):
        rel = getattr(proj, "release_lock", None)
        if rel is not None:
            try:
                rel()
            except Exception as e:  # noqa: BLE001
                log.warning("release_lock failed: %s", e)

    def _set_project(self, proj):
        self._teardown()
        self.ctl = Controller(proj, self.audio, self)
        self.ctl.status.connect(self._show_status)
        self.workspace = Workspace(self.ctl, self)
        self.stack.addWidget(self.workspace)
        self.stack.setCurrentWidget(self.workspace)
        done, total = self.ctl.progress()
        self.workspace.show_page(0 if done else 1)
        self.ctl.projectChanged.connect(self._update_title)
        st.add_recent(proj.folder)
        self._update_title()
        self._update_actions()
        self.statusBar().showMessage(f"Opened {proj.folder}", 5000)

    def _show_status(self, msg):
        self.statusBar().showMessage(msg, 6000)

    def _teardown(self):
        if self.workspace is not None:
            self.audio.cancel_recording()
            self.audio.stop_playback()
            self.audio.set_monitoring(False)
            self.stack.removeWidget(self.workspace)
            self.workspace.deleteLater()
            self.workspace = None
        if self.ctl is not None:
            try:
                self.ctl.audio.error.disconnect(self.ctl._audio_error)
            except (RuntimeError, TypeError):
                pass
            self._release(self.ctl.project)
            self.ctl.deleteLater()
            self.ctl = None

    def close_project(self):
        self._teardown()
        st.put("last_project", None)
        self.welcome.refresh()
        self.stack.setCurrentWidget(self._welcome_scroll)
        self._update_title()
        self._update_actions()

    def _open_project_folder(self):
        if self.ctl:
            open_in_explorer(self.ctl.project.folder)

    # ------------------------------------------------------------------ dialogs
    def open_audio_settings(self):
        dlg = AudioSettingsDialog(self.audio, self)
        try:
            dlg.exec()
        finally:
            dlg.deleteLater()   # don't keep one hidden dialog per opening around

    def _shortcuts(self):
        QMessageBox.information(self, "Keyboard shortcuts",
                                "<b>Record page</b><br>" + KEYS_HELP.replace("&nbsp;·&nbsp;", "<br>") +
                                "<br>Esc cancel recording / countdown"
                                "<br><br><b>Trim editor</b><br>Drag the orange handles or click to move the nearest"
                                "<br>←/→ nudge selected handle 10 ms (Shift = 1 ms)<br>[ and ] select start / end handle"
                                "<br>Mouse wheel zoom, Shift+wheel scroll, double-click fit"
                                "<br>P play trimmed, Shift+P play raw"
                                "<br><br><b>Anywhere</b><br>Ctrl+1 … Ctrl+5 switch pages<br>Ctrl+, audio settings")

    def _about(self):
        QMessageBox.about(self, "About Co-Driver Studio",
                          f"<b>Co-Driver Studio</b> v{__version__}<br>Record your own rally co-driver voice pack "
                          "for BeamNG.drive.<br><br>Log file: " + str(log_dir() / "log.txt"))

    def closeEvent(self, e):
        st.put("window/geometry", self.saveGeometry())
        try:
            if self.ctl and not self.ctl.read_only:
                self.ctl.project.save()
        except Exception:  # noqa: BLE001
            log.exception("final save failed")
        if self.ctl:
            self._release(self.ctl.project)
        self.audio.shutdown()
        super().closeEvent(e)
