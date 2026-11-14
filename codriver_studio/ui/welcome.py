"""Welcome screen (new / open / recent) and the New Project dialog."""
from __future__ import annotations

import json
import pathlib

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from .. import __version__
from . import settings as st
from . import theme
from .widgets import hint_label


def default_projects_root() -> pathlib.Path:
    # QStandardPaths follows OneDrive / folder redirection (Path.home()/"Documents" may not exist)
    return pathlib.Path(st.documents_dir()) / "Co-Driver Studio"


# Windows device names: invalid as a file/folder name, also with an extension ("nul.txt")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
             *(f"COM{c}" for c in "0123456789¹²³"), *(f"LPT{c}" for c in "0123456789¹²³")}


def _safe_folder_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    s = "".join("_" if (c in bad or ord(c) < 32) else c for c in name)
    s = s.strip().rstrip(". ").strip()   # Windows silently drops trailing dots / spaces
    if not s:
        return "My Co-Driver"
    stem, dot, rest = s.partition(".")
    if stem.rstrip(" ").upper() in _RESERVED:
        s = f"{stem.rstrip(' ')} pack{dot}{rest}"   # "nul.txt" -> "nul pack.txt"
    return s


class NewProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New voice pack")
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance("M") * 36)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        t = QLabel("Create a new co-driver voice pack")
        t.setStyleSheet("font-size: 14pt; font-weight: 600;")
        lay.addWidget(t)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.name = QLineEdit("My Co-Driver")
        self.name.selectAll()
        self.name.textChanged.connect(self._name_changed)
        self.folder = QLineEdit()
        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._browse)
        fr = QHBoxLayout()
        fr.addWidget(self.folder, 1)
        fr.addWidget(self.btn_browse)
        form.addRow("Co-driver name", self.name)
        form.addRow("", hint_label("This is how the voice appears in BeamNG's co-driver list. You can change it later."))
        form.addRow("Project folder", fr)
        form.addRow("", hint_label("Your raw recordings and settings live here. Pick a folder you back up."))
        lay.addLayout(form)
        self.error = QLabel("")
        self.error.setProperty("error", True)
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Create project")
        bb.button(QDialogButtonBox.Ok).setProperty("primary", True)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._root = pathlib.Path(st.get("new_project_root") or default_projects_root())
        self._auto_folder = True
        self._name_changed(self.name.text())
        self.folder.textEdited.connect(lambda _: setattr(self, "_auto_folder", False))

    def _name_changed(self, text):
        if self._auto_folder:
            self.folder.setText(str(self._root / _safe_folder_name(text)))

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose where to create the project", str(self._root))
        if d:
            p = pathlib.Path(d)
            # an empty folder is used as is; otherwise create a sub-folder named after the co-driver
            if p.exists() and any(p.iterdir()):
                p = p / _safe_folder_name(self.name.text())
                self._root = pathlib.Path(d)
            self.folder.setText(str(p))
            self._auto_folder = False

    def _accept(self):
        name = self.name.text().strip()
        if not name:
            self.error.setText("Please enter a name.")
            return
        p = pathlib.Path(self.folder.text().strip())
        if not str(p):
            self.error.setText("Please choose a folder.")
            return
        if (p / "project.json").exists():
            self.error.setText("There's already a project in that folder. Use Open project instead.")
            return
        st.put("new_project_root", str(p.parent))
        self.result_name, self.result_folder = name, p
        self.accept()


def project_summary(folder: pathlib.Path) -> str:
    try:
        d = json.loads((folder / "project.json").read_text("utf-8"))
        persona = (d.get("pack") or {}).get("persona") or folder.name
        n = sum(1 for v in (d.get("takes") or {}).values() if v)
        return f"{persona}   ·   {n} phrases with takes"
    except Exception:  # noqa: BLE001
        return folder.name


class WelcomePage(QWidget):
    newRequested = Signal()
    openRequested = Signal()
    recentRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 16)
        outer.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(14)
        outer.addLayout(col, 3)
        outer.addStretch(1)

        brand = QLabel("CO-DRIVER STUDIO")
        brand.setObjectName("Brand")
        brand.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 700; font-size: 12pt; letter-spacing: 3px;")
        title = QLabel("Be the voice in the passenger seat.")
        title.setStyleSheet("font-size: 24pt; font-weight: 600;")
        title.setWordWrap(True)
        sub = QLabel("Record your own rally co-driver for BeamNG.drive: read the lines, we trim, polish "
                     "and package them into a mod. About 180 short phrases, roughly 20 minutes.")
        sub.setWordWrap(True)
        sub.setProperty("muted", True)
        sub.setStyleSheet("font-size: 11pt;")
        col.addStretch(1)
        col.addWidget(brand)
        col.addWidget(title)
        col.addWidget(sub)
        col.addSpacing(10)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.btn_new = QPushButton("＋  New voice pack")
        self.btn_new.setProperty("primary", True)
        self.btn_open = QPushButton("Open project…")
        for b in (self.btn_new, self.btn_open):
            b.setStyleSheet("font-size: 11pt; padding: 8px 18px;")
        self.btn_new.clicked.connect(self.newRequested.emit)
        self.btn_open.clicked.connect(self.openRequested.emit)
        btns.addWidget(self.btn_new)
        btns.addWidget(self.btn_open)
        btns.addStretch(1)
        col.addLayout(btns)
        col.addSpacing(16)

        rl = QLabel("Recent projects")
        rl.setStyleSheet("font-weight: 600;")
        col.addWidget(rl)
        self.recent = QListWidget()
        # sized in two-line items (summary + path), not pixels: 2 visible at least, 5 at most
        item_h = 2 * self.recent.fontMetrics().lineSpacing() + 10
        self.recent.setMinimumHeight(2 * item_h + 6)
        self.recent.setMaximumHeight(5 * item_h + 6)
        # long paths are elided in the middle (full path in the tooltip), never scrolled sideways
        self.recent.setTextElideMode(Qt.ElideMiddle)
        self.recent.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recent.setResizeMode(QListWidget.Adjust)
        self.recent.setIconSize(QSize(14, 14))
        self.recent.itemActivated.connect(self._activated)
        self.recent.itemDoubleClicked.connect(self._activated)
        col.addWidget(self.recent)
        self.empty = hint_label("No recent projects yet.")
        col.addWidget(self.empty)
        col.addStretch(2)
        ver = QLabel(f"v{__version__}")
        ver.setProperty("hint", True)
        col.addWidget(ver, 0, Qt.AlignRight)
        self.refresh()

    def refresh(self):
        self.recent.clear()
        for p in st.recent_projects():
            path = pathlib.Path(p)
            ok = (path / "project.json").exists()
            it = QListWidgetItem(f"{project_summary(path) if ok else path.name + '   ·   (folder not found)'}\n{path}")
            it.setData(Qt.UserRole, str(path))
            it.setToolTip(str(path))
            it.setIcon(theme.status_icon("recorded" if ok else "missing"))
            if not ok:
                it.setForeground(Qt.gray)
            self.recent.addItem(it)
        self.empty.setVisible(self.recent.count() == 0)
        self.recent.setVisible(self.recent.count() > 0)

    def _activated(self, it):
        self.recentRequested.emit(it.data(Qt.UserRole))
