"""Take panel: waveform trim editor + takes list for one phrase (used on Phrases and Record pages)."""
from __future__ import annotations

import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget)

from . import theme
from .errors import log
from .waveform import WaveformView
from .widgets import ElidedLabel, FlowLayout, hint_label

TRIM_HELP = ("Drag the orange handles, or click to move the nearest one.  ←/→ nudge the selected handle "
             "10 ms (Shift = 1 ms), [ and ] pick a handle, wheel zooms, Alt/middle-drag pans.")

TOKEN_TRIM = "panel-trim"


def is_single_take(ph) -> bool:
    v = getattr(ph, "is_single_take", None)
    if v is None:
        return bool(getattr(ph, "single_take", False)) or ph.is_system
    return bool(v() if callable(v) else v)
TOKEN_RAW = "panel-raw"


def take_label(i: int, t, phrase_is_system: bool) -> str:
    length = max(0.0, t.trim_end - t.trim_start)
    when = ""
    try:
        when = datetime.datetime.fromisoformat(str(t.created)).strftime("%H:%M")
    except (ValueError, TypeError):
        pass
    bits = [f"Take {i + 1}", f"{length:.2f} s"]
    if when:
        bits.append(when)
    bits.append("auto-trim" if t.auto_trimmed else "hand-trimmed")
    return "   ·   ".join(bits)


class TakeList(QListWidget):
    """Takes list sized in rows of text (not pixels): prefers ``preferred_rows`` takes, shrinks to 2
    before the waveform shrinks, never grows past 5 (the waveform gets the space)."""

    preferred_rows = 3

    def _row_h(self):
        return self.fontMetrics().lineSpacing() + 10      # item padding 4+4 (theme) + indicator slack

    def _frame(self):
        return 2 * self.frameWidth() + 2

    def minimumSizeHint(self):
        return QSize(super().minimumSizeHint().width(), 2 * self._row_h() + self._frame())

    def sizeHint(self):
        return QSize(super().sizeHint().width(), max(2, self.preferred_rows) * self._row_h() + self._frame())

    def showEvent(self, e):
        super().showEvent(e)
        self.setMaximumHeight(5 * self._row_h() + self._frame())


class TakePanel(QWidget):
    tookFocus = Signal()

    def __init__(self, ctl, parent=None, show_header=True, show_record_button=True, show_trim_hint=True):
        super().__init__(parent)
        self.ctl = ctl
        self.pid: str | None = None
        self.idx: int | None = None
        self._ntakes = 0
        self._token = f"{TOKEN_TRIM}-{id(self)}"
        self._token_raw = f"{TOKEN_RAW}-{id(self)}"
        self._raw = None  # (x, sr) of current take
        self._loading = False
        self._ro = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.title = QLabel("Select a phrase")
        self.title.setStyleSheet("font-size: 15pt; font-weight: 600;")
        self.title.setWordWrap(True)
        self.subtitle = ElidedLabel("")
        self.subtitle.setProperty("muted", True)
        if show_header:
            lay.addWidget(self.title)
            lay.addWidget(self.subtitle)
        else:
            self.title.hide()
            self.subtitle.hide()

        self.wave = WaveformView()
        self.wave.set_placeholder("No take yet. Record one to see its waveform here.")
        self.wave.setToolTip(TRIM_HELP)
        self.wave.trimCommitted.connect(self._trim_committed)
        self.wave.trimDragging.connect(self._update_length_label)
        lay.addWidget(self.wave, 1)

        # transport + trim tools: groups that wrap onto a second row when the panel is narrow
        self.btn_play = QPushButton("▶  Play trimmed")
        self.btn_play.setToolTip("Play the trimmed take with your effects (P)")
        self.btn_play.setProperty("primary", True)
        self.btn_raw = QPushButton("Play raw")
        self.btn_raw.setToolTip("Play the whole untrimmed recording, no effects (Shift+P)")
        self.btn_stop = QPushButton("■  Stop")
        self.btn_fit = QPushButton("Fit")
        self.btn_fit.setToolTip("Show the whole take (double-click the waveform)")
        self.btn_zin = QPushButton("+")
        self.btn_zin.setToolTip("Zoom in (mouse wheel)")
        self.btn_zout = QPushButton("−")
        self.btn_zout.setToolTip("Zoom out (mouse wheel)")
        self.btn_play.clicked.connect(self.play_trimmed)
        self.btn_raw.clicked.connect(self.play_raw)
        self.btn_stop.clicked.connect(self.ctl.audio.stop_playback)
        self.btn_fit.clicked.connect(self.wave.fit)
        self.btn_zin.clicked.connect(self._zoom_in)
        self.btn_zout.clicked.connect(self._zoom_out)
        self.len_label = QLabel("")
        self.len_label.setProperty("muted", True)
        # reserve the width of the longest label so the row doesn't jump while dragging
        self.len_label.setMinimumWidth(self.len_label.fontMetrics().horizontalAdvance("00.00 → 00.00 s  (00.00 s)") + 4)

        self.btn_auto = QPushButton("Re-auto-trim")
        self.btn_auto.setToolTip("Find the speech again with the sensitivity on the right")
        self.sens = QSlider(Qt.Horizontal)
        self.sens.setRange(0, 100)
        self.sens.setMinimumWidth(self.sens.fontMetrics().horizontalAdvance("M") * 6)
        self.sens.setMaximumWidth(self.sens.fontMetrics().horizontalAdvance("M") * 12)
        self.sens.setToolTip("Higher keeps quieter sounds (soft word endings); lower cuts more aggressively")
        self.sens_val = QLabel("")
        self.sens_val.setMinimumWidth(self.sens_val.fontMetrics().horizontalAdvance("100%") + 4)
        self.btn_reset = QPushButton("Reset")
        self.btn_reset.setToolTip("Keep the whole recording (no trim)")
        self.btn_auto.clicked.connect(self.reauto_trim)
        self.btn_reset.clicked.connect(self.reset_trim)
        self.sens.valueChanged.connect(self._sens_changed)
        self.sens.sliderReleased.connect(self._sens_commit)
        sens_lab = QLabel("Sensitivity")
        sens_lab.setProperty("muted", True)

        def group(*ws, spacing=6):
            g = QWidget()
            h = QHBoxLayout(g)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(spacing)
            for w in ws:
                h.addWidget(w)
            return g
        self.tools = FlowLayout(spacing=6, vspacing=6)
        self.tools.addWidget(group(self.btn_play, self.btn_raw, self.btn_stop))
        self.tools.addWidget(group(self.len_label, self.btn_zout, self.btn_zin, self.btn_fit))
        self.tools.addWidget(group(self.btn_auto, sens_lab, self.sens, self.sens_val, self.btn_reset))
        lay.addLayout(self.tools)
        self.trim_hint = hint_label(TRIM_HELP)
        lay.addWidget(self.trim_hint)
        if not show_trim_hint:
            self.trim_hint.hide()

        # takes list (header row carries the take buttons: no extra row)
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        tl = QLabel("Takes")
        tl.setStyleSheet("font-weight: 600;")
        self.takes_hint = ElidedLabel("")
        self.takes_hint.setProperty("hint", True)
        self.btn_record = QPushButton("●  Record another take")
        self.btn_record.setStyleSheet(f"QPushButton {{ color: {theme.RED}; }}")
        self.btn_record.clicked.connect(self._request_record)
        self.btn_delete = QPushButton("Delete take")
        self.btn_delete.clicked.connect(self.delete_current)
        hdr.addWidget(tl)
        hdr.addWidget(self.takes_hint, 1)
        if show_record_button:
            hdr.addWidget(self.btn_record)
        else:
            self.btn_record.hide()
        hdr.addWidget(self.btn_delete)
        lay.addLayout(hdr)
        self.list = TakeList()
        self.list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.list.currentRowChanged.connect(self._row_changed)
        self.list.itemChanged.connect(self._item_changed)
        lay.addWidget(self.list)

        QShortcut(QKeySequence("P"), self, self.play_trimmed, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Shift+P"), self, self.play_raw, context=Qt.WidgetWithChildrenShortcut)

        ctl.audio.playback_position.connect(self._on_pos)
        ctl.audio.playback_finished.connect(self._on_finished)
        ctl.phraseChanged.connect(self._phrase_changed_elsewhere)
        self.sens.setValue(int(round(float(getattr(ctl.project, "trim_sensitivity", 0.5)) * 100)))
        self._enable(False)

    def _zoom_in(self):
        self.wave.zoom(1.5)

    def _zoom_out(self):
        self.wave.zoom(1 / 1.5)

    def _request_record(self):
        if self.pid:
            self.ctl.recordRequested.emit(self.pid)

    def set_compact(self):
        """Tight page (Record): the waveform and the takes list only ask for their minimum, so the
        rest of the page gets the space first; they still grow when there is room to spare."""
        self.wave.preferred_height = 0
        self.wave.updateGeometry()
        self.list.preferred_rows = 2
        self.list.updateGeometry()

    def set_keyboard_passive(self):
        """Mouse-only controls: keyboard focus stays with the owner (Record page shortcuts).

        Only the waveform keeps focus (it claims ←/→ just while a handle is selected)."""
        for w in (self.list, self.sens, self.btn_play, self.btn_raw, self.btn_stop, self.btn_fit,
                  self.btn_zin, self.btn_zout, self.btn_auto, self.btn_reset, self.btn_record, self.btn_delete):
            w.setFocusPolicy(Qt.NoFocus)

    def set_read_only(self, ro: bool):
        """View + listen only: no trimming, enabling, deleting or recording."""
        self._ro = bool(ro)
        self.wave.setEnabled(not self._ro)
        self._fill_list()
        self._enable(self._raw is not None)

    def flush_pending(self):
        """Commit a pending trim nudge now (it lands on the take it was made on)."""
        self.wave.flush_pending()

    # ------------------------------------------------------------------ loading
    def _enable(self, has_take: bool):
        for w in (self.btn_play, self.btn_raw, self.btn_stop, self.btn_fit, self.btn_zin, self.btn_zout,
                  self.btn_auto, self.btn_reset, self.btn_delete):
            w.setEnabled(has_take)
        self.btn_record.setEnabled(self.pid is not None and not self._ro)
        for w in (self.btn_auto, self.btn_reset, self.btn_delete, self.sens):
            if self._ro:
                w.setEnabled(False)

    def show_phrase(self, pid: str | None, take_index: int | None = None):
        self.flush_pending()   # before pid/idx change
        self.pid = pid
        if pid is None:
            self.title.setText("Select a phrase")
            self.subtitle.setText("")
            self.idx = None
            self._fill_list()
            self.wave.clear("Select a phrase on the left")
            self._enable(False)
            return
        ph = self.ctl.phrase(pid)
        self.title.setText(f"“{self.ctl.project.spoken_text(pid)}”")
        hint = getattr(ph, "hint", None)
        self.subtitle.setText(f"{ph.category}  ·  {ph.file_stem}.ogg" + (f"  ·  Hint: {hint}" if hint else ""))
        takes = self.ctl.takes(pid)
        if take_index is None or not (0 <= take_index < len(takes)):
            fe = self.ctl.first_enabled(pid)
            take_index = fe if fe is not None else (len(takes) - 1 if takes else None)
        self.idx = take_index
        self._fill_list()
        self._load_take()

    def _fill_list(self):
        self._loading = True
        self.list.clear()
        takes = self.ctl.takes(self.pid) if self.pid else []
        self._ntakes = len(takes)
        is_sys = bool(self.pid) and is_single_take(self.ctl.phrase(self.pid))
        for i, t in enumerate(takes):
            it = QListWidgetItem(take_label(i, t, is_sys))
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | (Qt.NoItemFlags if self._ro else Qt.ItemIsUserCheckable))
            it.setCheckState(Qt.Checked if t.enabled else Qt.Unchecked)
            it.setToolTip("Ticked takes go into the pack. " +
                          ("Only one take is used in-game for this call." if is_sys else
                           "Several ticked takes = the game picks one at random each time."))
            self.list.addItem(it)
        if self.idx is not None and 0 <= self.idx < self.list.count():
            self.list.setCurrentRow(self.idx)
        if not takes:
            self.takes_hint.setText("  none yet")
        elif is_sys:
            self.takes_hint.setText("  Only one take is used in-game for this call")
        else:
            self.takes_hint.setText("  tick several to let the game vary between them")
        self._loading = False

    def _load_take(self):
        self._raw = None
        self.ctl.audio.stop_playback() if self.ctl.audio.play_token in (self._token, self._token_raw) else None
        if self.pid is None or self.idx is None:
            self.wave.clear("No take yet. Record one to see its waveform here.")
            self.len_label.setText("")
            self._enable(False)
            return
        t = self.ctl.takes(self.pid)[self.idx]
        try:
            x, sr = self.ctl.project.load_take_audio(self.pid, self.idx)
        except Exception as e:  # noqa: BLE001 - missing file etc.
            log.warning("load take failed: %s", e)
            self.wave.clear(f"Audio file missing: {t.file}")
            self._enable(False)
            self.btn_delete.setEnabled(True)
            return
        self._raw = (x, sr)
        self.wave.set_audio(x, sr, t.trim_start, t.trim_end, context=(self.pid, t.file))
        self._update_length_label(t.trim_start, t.trim_end)
        self._enable(True)

    def _update_length_label(self, s, e):
        self.len_label.setText(f"{s:.2f} → {e:.2f} s  ({e - s:.2f} s)")

    def _row_changed(self, row):
        if self._loading or row < 0 or row == self.idx:
            return
        self.flush_pending()
        self.idx = row
        self._load_take()

    def _item_changed(self, item):
        if self._loading or self.pid is None:
            return
        row = self.list.row(item)
        en = item.checkState() == Qt.Checked
        try:
            self.ctl.project.set_take_enabled(self.pid, row, en)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Take", str(e))
        self._fill_list()
        self.ctl.notify(self.pid)

    def _phrase_changed_elsewhere(self, pid):
        if pid == self.pid and not self._loading:
            n = len(self.ctl.takes(pid))
            if n != self._ntakes:
                self.show_phrase(pid, self.idx if self.idx is not None and self.idx < n else None)
            else:
                cur = self.idx
                self._fill_list()
                self.idx = cur

    # ------------------------------------------------------------------ editing
    def _find_take(self, pid, file) -> int | None:
        for i, t in enumerate(self.ctl.takes(pid)):
            if t.file == file:
                return i
        return None

    def _trim_committed(self, ctx, s, e):
        """Apply a committed trim to the take the edit was MADE on (identified by its file),
        even if the panel has since moved to another take/phrase; drop it if that take is gone."""
        if not ctx:
            return
        pid, file = ctx
        lock = getattr(self.ctl.project, "lock", None)
        try:
            if lock is not None:
                lock.acquire()
            try:
                idx = self._find_take(pid, file)
                if idx is None:
                    log.info("dropping trim edit for a take that no longer exists: %s", file)
                    return
                self.ctl.project.set_trim(pid, idx, s, e)
                t = self.ctl.takes(pid)[idx]
            finally:
                if lock is not None:
                    lock.release()
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, "Trim", str(ex))
            return
        if (pid, idx) == (self.pid, self.idx) and self.wave.context == ctx:
            if abs(t.trim_start - s) > 1e-6 or abs(t.trim_end - e) > 1e-6:
                self.wave.set_trim(t.trim_start, t.trim_end)
            self._update_length_label(t.trim_start, t.trim_end)
            self._refresh_item()
        self._loading = True
        self.ctl.notify(pid)
        self._loading = False

    def _trim_edited(self, s, e):   # kept for callers that commit the current take directly
        if self.pid is None or self.idx is None:
            return
        takes = self.ctl.takes(self.pid)
        if 0 <= self.idx < len(takes):
            self._trim_committed((self.pid, takes[self.idx].file), s, e)

    def _refresh_item(self):
        if self.idx is None:
            return
        it = self.list.item(self.idx)
        if it is not None:
            self._loading = True
            it.setText(take_label(self.idx, self.ctl.takes(self.pid)[self.idx], is_single_take(self.ctl.phrase(self.pid))))
            self._loading = False

    def _sens_changed(self, v):
        self.sens_val.setText(f"{v}%")
        if not self.sens.isSliderDown():
            self._sens_commit()

    def _sens_commit(self):
        v = self.sens.value() / 100.0
        if abs(float(getattr(self.ctl.project, "trim_sensitivity", 0.5)) - v) > 1e-6:
            self.ctl.project.trim_sensitivity = v
            self.ctl.save()

    def reauto_trim(self):
        if self.pid is None or self.idx is None:
            return
        self.flush_pending()
        try:
            self.ctl.project.reauto_trim(self.pid, self.idx, self.sens.value() / 100.0)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Auto-trim", str(e))
            return
        t = self.ctl.takes(self.pid)[self.idx]
        self.wave.set_trim(t.trim_start, t.trim_end)
        self._update_length_label(t.trim_start, t.trim_end)
        self._refresh_item()
        self._loading = True
        self.ctl.notify(self.pid)
        self._loading = False

    def reset_trim(self):
        if self._raw is None:
            return
        x, sr = self._raw
        self.wave.set_trim(0.0, len(x) / sr, emit=True)
        self.wave.fit()

    def delete_current(self, confirm=True):
        if self.pid is None or self.idx is None:
            return
        if confirm:
            r = QMessageBox.question(self, "Delete take", f"Delete take {self.idx + 1}? The recording file is removed.",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        self.flush_pending()
        self.ctl.audio.stop_playback()
        try:
            self.ctl.project.delete_take(self.pid, self.idx)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Delete take", str(e))
        n = len(self.ctl.takes(self.pid))
        self._loading = True
        self.ctl.notify(self.pid)
        self._loading = False
        self.show_phrase(self.pid, min(self.idx, n - 1) if n else None)

    # ------------------------------------------------------------------ playback
    def play_trimmed(self):
        if self.pid is None or self.idx is None or self._raw is None:
            return
        self.wave._flush_commit()
        pid, idx = self.pid, self.idx
        start = self.wave.start

        def go(y, sr):
            if (self.pid, self.idx) == (pid, idx):
                self._play_offset = start
                self.ctl.audio.play(y, sr, self._token)
        self.ctl.render_async(pid, idx, go, owner=self)

    def play_raw(self):
        if self._raw is None:
            return
        self._play_offset = 0.0
        self.ctl.audio.play(self._raw[0], self._raw[1], self._token_raw)

    _play_offset = 0.0

    def _on_pos(self, token, pos):
        if token in (self._token, self._token_raw):
            self.wave.set_playhead(self._play_offset + pos)

    def _on_finished(self, token):
        if token in (self._token, self._token_raw):
            self.wave.set_playhead(None)
