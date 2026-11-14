"""Phrases page: every line of the catalog grouped by category, with status and a take panel."""
from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QSplitter, QTreeView, QVBoxLayout, QWidget)

from .. import theme
from ..take_panel import TakePanel
from ..widgets import PageScrollArea, page_header

COL_TEXT, COL_FILE, COL_STATUS, COL_TAKES, COL_LEN, COL_PLAY = range(6)
HEADERS = ["Line to read", "File", "Status", "Takes", "Length", ""]
PID_ROLE = Qt.UserRole + 1

FILTERS = [("All phrases", None), ("Missing", "missing"), ("Recorded", "recorded"), ("Disabled", "disabled")]


class PhrasesPage(QWidget):
    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self._rows: dict[str, list[QStandardItem]] = {}
        self._cat_items: dict[str, QStandardItem] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)
        outer.addWidget(page_header("Phrases", "Every line the co-driver can say. Select one to listen, "
                                    "trim or tidy its takes; double-click to jump into the editor."))

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search lines or file names…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda: self._filter_timer.start())
        self.filter = QComboBox()
        for label, _ in FILTERS:
            self.filter.addItem(label)
        self.filter.currentIndexChanged.connect(self.rebuild)
        self.lang = QComboBox()
        self.lang.addItem(f"Language: {ctl.project.catalog.language_label}")
        self.lang.setEnabled(False)
        self.lang.setToolTip("Recording in other languages (translated lines, same file names) is planned.")
        self.btn_play = QPushButton("▶  Play")
        self.btn_play.setToolTip("Play the selected phrase (first ticked take, with effects)")
        self.btn_play.clicked.connect(self.play_selected)
        self.btn_expand = QPushButton("Collapse all")
        self.btn_expand.clicked.connect(self._toggle_expand)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.filter)
        bar.addWidget(self.btn_expand)
        bar.addWidget(self.lang)
        outer.addLayout(bar)

        self.model = QStandardItemModel(0, len(HEADERS), self)
        self.model.setHorizontalHeaderLabels(HEADERS)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setIndentation(14)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setTextElideMode(Qt.ElideMiddle)
        h = self.tree.header()
        h.setStretchLastSection(False)
        h.setMinimumSectionSize(24)
        # the line to read gets the room; the other columns are sized from the font, not pixels
        fm = self.tree.fontMetrics()
        h.setSectionResizeMode(COL_TEXT, QHeaderView.Stretch)
        widths = {COL_FILE: fm.horizontalAdvance("pacenote_three_left.ogg"),
                  COL_STATUS: fm.horizontalAdvance("Recorded") + 16 + 30,     # + status icon + padding
                  COL_TAKES: fm.horizontalAdvance("Takes") + 24,
                  COL_LEN: fm.horizontalAdvance("Length") + 24,
                  COL_PLAY: fm.horizontalAdvance("▶") + 22}
        for c, w in widths.items():
            h.setSectionResizeMode(c, QHeaderView.Interactive if c == COL_FILE else QHeaderView.Fixed)
            self.tree.setColumnWidth(c, w)
        # narrower than all columns is fine (the tree scrolls sideways); the take panel needs the room more
        self.tree.setMinimumWidth(fm.horizontalAdvance("M") * 22)
        self.tree.doubleClicked.connect(self._double_clicked)
        self.tree.clicked.connect(self._clicked)

        self.panel = TakePanel(ctl)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.tree)
        lb = QHBoxLayout()
        self.count_label = QLabel("")
        self.count_label.setProperty("muted", True)
        lb.addWidget(self.count_label, 1)
        lb.addWidget(self.btn_play)
        ll.addLayout(lb)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 0, 0, 0)
        rl.addWidget(self.panel)
        # QSplitter ignores height-for-width: when the wrapped tool rows / hints need more height than
        # the splitter has, the panel scrolls instead of its widgets overlapping
        self.right_scroll = PageScrollArea(right)
        self.right_scroll.setMinimumWidth(right.minimumSizeHint().width()
                                          + self.right_scroll.verticalScrollBar().sizeHint().width())
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(left)
        self.split.addWidget(self.right_scroll)
        self.split.setStretchFactor(0, 5)
        self.split.setStretchFactor(1, 4)
        self.split.setChildrenCollapsible(False)
        self.split.setSizes([760, 560])
        outer.addWidget(self.split, 1)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self.rebuild)
        self._expanded_all = True

        ctl.phraseChanged.connect(self.refresh_phrase)
        ctl.projectChanged.connect(self.rebuild)
        self.rebuild()

    # ------------------------------------------------------------------ model
    def _matches(self, ph) -> bool:
        want = FILTERS[self.filter.currentIndex()][1]
        if want and self.ctl.project.status(ph.id) != want:
            return False
        q = self.search.text().strip().lower()
        if q:
            hay = f"{self.ctl.project.spoken_text(ph.id)} {ph.text} {ph.file_stem} {ph.category}".lower()
            return all(w in hay for w in q.split())
        return True

    def rebuild(self):
        sel = self.selected_pid()
        collapsed = {c for c, it in self._cat_items.items() if not self.tree.isExpanded(it.index())}
        self.model.removeRows(0, self.model.rowCount())
        self._rows.clear()
        self._cat_items.clear()
        cat = self.ctl.project.catalog
        shown = 0
        for c in cat.categories:
            phrases = [p for p in cat.by_category(c) if self._matches(p)]
            if not phrases:
                continue
            done = sum(self.ctl.project.status(p.id) == "recorded" for p in cat.by_category(c))
            parent = QStandardItem(f"{c.capitalize()}")
            parent.setData(c, Qt.UserRole + 2)
            f = parent.font()
            f.setBold(True)
            parent.setFont(f)
            parent.setForeground(QBrush(QColor(theme.ACCENT)))
            parent.setSelectable(False)
            prog = QStandardItem(f"{done} / {len(cat.by_category(c))}")
            prog.setForeground(QBrush(QColor(theme.MUTED)))
            prog.setSelectable(False)
            extra = [QStandardItem("") for _ in range(len(HEADERS) - 2)]
            for e in extra:
                e.setSelectable(False)
            self.model.appendRow([parent, prog] + extra)
            self._cat_items[c] = parent
            for p in phrases:
                row = [QStandardItem() for _ in HEADERS]
                for it in row:
                    it.setData(p.id, PID_ROLE)
                parent.appendRow(row)
                self._rows[p.id] = row
                self._fill_row(p.id)
                shown += 1
            idx = parent.index()
            self.tree.setFirstColumnSpanned(idx.row(), QModelIndex(), False)
            self.tree.setExpanded(idx, c not in collapsed)
        total = len(cat.phrases)
        self.count_label.setText(f"Showing {shown} of {total} phrases" if shown != total else f"{total} phrases")
        if sel and sel in self._rows:
            self.select_pid(sel, load=False)

    def _fill_row(self, pid):
        row = self._rows.get(pid)
        if not row:
            return
        proj = self.ctl.project
        ph = proj.catalog.get(pid)
        st = proj.status(pid)
        takes = self.ctl.takes(pid)
        fe = self.ctl.first_enabled(pid)
        row[COL_TEXT].setText(proj.spoken_text(pid))
        hint = getattr(ph, "hint", None)
        tips = []
        if proj.spoken_text(pid) != ph.text:
            tips.append(f"Catalog text: {ph.text}")
        if hint:
            tips.append(f"How to say it: {hint}")
            row[COL_TEXT].setText(f"{proj.spoken_text(pid)}   ({hint})")
        row[COL_TEXT].setToolTip("\n".join(tips))
        row[COL_FILE].setText(f"{ph.file_stem}.ogg")
        row[COL_FILE].setForeground(QBrush(QColor(theme.MUTED)))
        row[COL_STATUS].setText(theme.STATUS_LABELS.get(st, st))
        row[COL_STATUS].setIcon(theme.status_icon(st))
        row[COL_STATUS].setForeground(QBrush(QColor(theme.STATUS_COLORS.get(st, theme.TEXT))))
        n_en = sum(t.enabled for t in takes)
        row[COL_TAKES].setText(f"{n_en}/{len(takes)}" if takes and n_en != len(takes) else (str(len(takes)) if takes else "–"))
        row[COL_TAKES].setTextAlignment(Qt.AlignCenter)
        if fe is not None:
            t = takes[fe]
            row[COL_LEN].setText(f"{t.trim_end - t.trim_start:.2f}s")
        else:
            row[COL_LEN].setText("")
        row[COL_LEN].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row[COL_PLAY].setText("▶" if fe is not None else "")
        row[COL_PLAY].setToolTip("Play" if fe is not None else "")
        row[COL_PLAY].setTextAlignment(Qt.AlignCenter)
        row[COL_PLAY].setForeground(QBrush(QColor(theme.ACCENT)))

    def refresh_phrase(self, pid):
        if pid in self._rows:
            self._fill_row(pid)
            ph = self.ctl.phrase(pid)
            parent = self._cat_items.get(ph.category)
            if parent is not None:
                cat = self.ctl.project.catalog
                done = sum(self.ctl.project.status(p.id) == "recorded" for p in cat.by_category(ph.category))
                self.model.item(parent.row(), 1).setText(f"{done} / {len(cat.by_category(ph.category))}")

    # ------------------------------------------------------------------ selection
    def selected_pid(self):
        idx = self.tree.currentIndex()
        return idx.data(PID_ROLE) if idx.isValid() else None

    def select_pid(self, pid, load=True, take_index=None):
        if pid not in self._rows:
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self.filter.blockSignals(True)
            self.filter.setCurrentIndex(0)
            self.filter.blockSignals(False)
            self.rebuild()
        row = self._rows.get(pid)
        if not row:
            return
        idx = row[0].index()
        self.tree.expand(idx.parent())
        self.tree.selectionModel().setCurrentIndex(
            idx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        self.tree.scrollTo(idx)
        if load:
            self.panel.show_phrase(pid, take_index)

    def _clicked(self, idx):
        pid = idx.data(PID_ROLE)
        if not pid:
            parent_cat = idx.siblingAtColumn(0)
            if parent_cat.isValid() and not parent_cat.parent().isValid():
                self.tree.setExpanded(parent_cat, not self.tree.isExpanded(parent_cat))
            return
        if pid != self.panel.pid:
            self.panel.show_phrase(pid)
        if idx.column() == COL_PLAY:
            self.ctl.play_phrase(pid)

    def _double_clicked(self, idx):
        pid = idx.data(PID_ROLE)
        if pid:
            self.panel.show_phrase(pid)
            self.panel.wave.setFocus()

    def keyPressEvent(self, e):
        super().keyPressEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        # follow keyboard navigation in the tree
        sm = self.tree.selectionModel()
        if not getattr(self, "_sm_connected", False):
            sm.currentRowChanged.connect(self._current_changed)
            self._sm_connected = True

    def _current_changed(self, cur, _prev):
        pid = cur.data(PID_ROLE) if cur.isValid() else None
        if pid and pid != self.panel.pid:
            self.panel.show_phrase(pid)

    def play_selected(self):
        pid = self.selected_pid() or self.panel.pid
        if pid:
            self.ctl.play_phrase(pid)

    def _toggle_expand(self):
        if self._expanded_all:
            self.tree.collapseAll()
            self.btn_expand.setText("Expand all")
        else:
            self.tree.expandAll()
            self.btn_expand.setText("Collapse all")
        self._expanded_all = not self._expanded_all

    def open_phrase(self, pid, take_index=-1):
        self.select_pid(pid, load=True, take_index=None if take_index < 0 else take_index)
