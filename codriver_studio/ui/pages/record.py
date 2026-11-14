"""Record page: guided, keyboard-driven recording session."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from .. import settings as st
from .. import theme
from ..errors import log
from ..take_panel import TakePanel, is_single_take
from ..widgets import ElidedLabel, FitTextLabel, FlowLayout, LevelMeter, restyle

REC_QSS = f"""
QPushButton#RecButton {{ border-radius: 38px; min-width: 76px; max-width: 76px; min-height: 76px; max-height: 76px;
    padding: 0; font-size: 24pt; font-weight: 700; background: #2a1a1c; border: 3px solid {theme.RED}; color: {theme.RED}; }}
QPushButton#RecButton:hover {{ background: #3a1f22; }}
QPushButton#RecButton[state="rec"] {{ background: {theme.RED}; color: white; border-color: #ff8a8d; }}
QPushButton#RecButton[state="rec"][pulse="true"] {{ background: #a8262b; border-color: {theme.RED}; }}
QPushButton#RecButton[state="count"] {{ background: #2a1a1c; color: {theme.ACCENT}; border-color: {theme.ACCENT}; }}
"""

KEYS_HELP = ("<b>Space</b> record / stop &nbsp;·&nbsp; <b>Enter</b> keep &amp; next &nbsp;·&nbsp; "
             "<b>R</b> retake &nbsp;·&nbsp; <b>←</b>/<b>Backspace</b> previous &nbsp;·&nbsp; <b>→</b> skip"
             " &nbsp;·&nbsp; <b>P</b> play take")


class RecordPage(QWidget):
    settingsRequested = Signal()

    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self.audio = ctl.audio
        self.pid: str | None = None
        self.history: list[str] = []
        self.session_takes: dict[str, list[int]] = {}
        self.state = "idle"          # idle | count | rec | review
        self._advance_after_take = False
        self._quiet_take = False   # set while a take is stopped by navigation: keep it, don't autoplay
        self._restart_after_take = False
        self._count = 0
        self.read_only = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet(REC_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 10)
        outer.setSpacing(8)

        # --- options row (the check boxes wrap onto a second row on narrow windows)
        top = QHBoxLayout()
        top.setSpacing(16)
        title = QLabel("Record")
        title.setObjectName("PageTitle")
        top.addWidget(title, 0, Qt.AlignTop)
        opts = FlowLayout(spacing=18, vspacing=4)
        self.cb_missing = QCheckBox("Only missing phrases")
        self.cb_missing.setToolTip("Skip phrases that already have a take")
        self.cb_hands = QCheckBox("Hands-free")
        self.cb_hands.setToolTip("Stops by itself when you stop talking, plays the result back and waits for "
                                 "Enter (keep) or R (retake). Enter starts the next phrase automatically.")
        self.cb_autoplay = QCheckBox("Auto-play after take")
        self.cb_count = QCheckBox("3-2-1 countdown")
        for cb, key, default in ((self.cb_missing, "rec/only_missing", True), (self.cb_hands, "rec/hands_free", False),
                                 (self.cb_autoplay, "rec/autoplay", True), (self.cb_count, "rec/countdown", False)):
            cb.setChecked(st.get(key, default, bool))
            cb.setFocusPolicy(Qt.NoFocus)
            cb.setProperty("settings_key", key)
            cb.toggled.connect(self._option_toggled)
            opts.addWidget(cb)
        self.cb_missing.toggled.connect(self._missing_toggled)
        # vertically centre the check boxes on the title's first line
        opts_box = QVBoxLayout()
        opts_box.setContentsMargins(0, max(0, (title.sizeHint().height() - self.cb_hands.sizeHint().height()) // 2), 0, 0)
        opts_box.addLayout(opts)
        top.addLayout(opts_box, 1)
        outer.addLayout(top)

        # --- the card with the line to read
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(22, 10, 22, 10)
        cl.setSpacing(2)
        meta = QHBoxLayout()
        self.meta = ElidedLabel("")
        self.meta.setProperty("muted", True)
        self.status_badge = QLabel("")
        meta.addWidget(self.meta, 1)
        meta.addWidget(self.status_badge)
        cl.addLayout(meta)
        # the line to read: as big as fits (40 pt down to 15 pt), long system lines wrap / elide
        self.big = FitTextLabel(max_pt=40, min_pt=15)
        self.big.setObjectName("BigLine")
        cl.addWidget(self.big, 1)
        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color: {theme.AMBER}; font-size: 12pt;")
        self.hint.setWordWrap(True)
        self.hint.hide()
        cl.addWidget(self.hint)
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.next_label = ElidedLabel("")
        self.next_label.setProperty("muted", True)
        self.next_label.setStyleSheet("font-size: 12pt;")
        self.pos_label = ElidedLabel("")
        self.pos_label.setProperty("muted", True)
        self.pos_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bottom.addWidget(self.next_label, 3)
        bottom.addWidget(self.pos_label, 1)
        cl.addLayout(bottom)
        outer.addWidget(card, 2)

        # --- record controls
        ctrl = QHBoxLayout()
        ctrl.setSpacing(16)
        self.btn_prev = QPushButton("◀  Previous")
        self.btn_rec = QPushButton("●")
        self.btn_rec.setObjectName("RecButton")
        self.btn_rec.setToolTip("Record / stop (Space)")
        self.btn_keep = QPushButton("Keep && next  ▶")
        self.btn_keep.setProperty("primary", True)
        self.btn_keep.setToolTip("Enter")
        self.btn_retake = QPushButton("Retake")
        self.btn_retake.setToolTip("Throw away the last take and record again (R)")
        self.btn_skip = QPushButton("Skip  ▶")
        for b in (self.btn_prev, self.btn_rec, self.btn_keep, self.btn_retake, self.btn_skip):
            b.setFocusPolicy(Qt.NoFocus)
        self.btn_prev.clicked.connect(self.go_prev)
        self.btn_rec.clicked.connect(self.toggle_record)
        self.btn_keep.clicked.connect(self.keep_next)
        self.btn_retake.clicked.connect(self.retake)
        self.btn_skip.clicked.connect(self.skip)

        meter_box = QVBoxLayout()
        meter_box.setSpacing(4)
        self.state_label = QLabel("")
        self.state_label.setStyleSheet("font-size: 12pt; font-weight: 600;")
        self.state_label.setWordWrap(True)
        self.meter = LevelMeter()
        mic_row = QHBoxLayout()
        mic_row.setSpacing(6)
        self.mic_label = ElidedLabel("")
        self.mic_label.setProperty("hint", True)
        self.clip_label = ElidedLabel("")
        self.clip_label.setProperty("error", True)
        self.clip_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.btn_dev = QPushButton("Audio settings…")
        self.btn_dev.setProperty("flat", True)
        self.btn_dev.setStyleSheet("padding: 2px 8px;")
        self.btn_dev.setFocusPolicy(Qt.NoFocus)
        self.btn_dev.clicked.connect(self.settingsRequested.emit)
        mic_row.addWidget(self.mic_label, 1)
        mic_row.addWidget(self.btn_dev)
        mic_row.addWidget(self.clip_label, 1)
        meter_box.addWidget(self.state_label)
        meter_box.addWidget(self.meter)
        meter_box.addLayout(mic_row)

        ctrl.addWidget(self.btn_rec, 0, Qt.AlignVCenter)
        ctrl.addLayout(meter_box, 1)
        nav = QGridLayout()
        nav.setSpacing(6)
        nav.addWidget(self.btn_retake, 0, 0)
        nav.addWidget(self.btn_keep, 0, 1)
        nav.addWidget(self.btn_prev, 1, 0)
        nav.addWidget(self.btn_skip, 1, 1)
        ctrl.addLayout(nav)
        outer.addLayout(ctrl)

        # --- trim editor of the current phrase
        self.panel = TakePanel(ctl, show_header=False, show_record_button=False, show_trim_hint=False)
        # Space/Enter/R/arrows always drive the recorder here: the take list, slider and panel buttons
        # are mouse-only on this page (otherwise Space would toggle a take / arrows move the slider
        # depending on where focus happened to be).
        self.panel.set_keyboard_passive()
        self.panel.set_compact()
        outer.addWidget(self.panel, 3)

        keys = QLabel(KEYS_HELP)
        keys.setProperty("hint", True)
        keys.setAlignment(Qt.AlignCenter)
        keys.setWordWrap(True)
        outer.addWidget(keys)

        # --- shortcuts (page-wide; the waveform claims ←/→ only while a handle is selected)
        def sc(seq, fn):
            s = QShortcut(QKeySequence(seq), self, fn, context=Qt.WidgetWithChildrenShortcut)
            s.setAutoRepeat(False)
            return s
        sc("Space", self.toggle_record)
        sc("Return", self.keep_next)
        sc("Enter", self.keep_next)
        sc("R", self.retake)
        sc("Backspace", self.go_prev)
        sc("Left", self.go_prev)
        sc("Right", self.skip)
        sc("Escape", self.cancel)

        self._pulse = QTimer(self)
        self._pulse.setInterval(480)
        self._pulse.timeout.connect(self._do_pulse)
        self._count_timer = QTimer(self)
        self._count_timer.setInterval(700)
        self._count_timer.timeout.connect(self._count_tick)
        self._clip_timer = QTimer(self)
        self._clip_timer.setSingleShot(True)
        self._clip_timer.setInterval(2500)
        self._clip_timer.timeout.connect(self.clip_label.clear)

        # AudioService outlives this page (it belongs to the main window): only bound methods of
        # QObjects here, so Qt drops the connections when the page is destroyed (no lambdas!).
        self.audio.level.connect(self.meter.set_level)
        self.audio.clipped.connect(self._clipped)
        self.audio.recording_changed.connect(self._rec_changed)
        self.audio.take_ready.connect(self._take_ready)
        self.audio.input_state.connect(self._input_state_changed)
        self.audio.devices_changed.connect(self._update_mic_label)
        input_lost = getattr(self.audio, "input_lost", None)
        if input_lost is not None:
            input_lost.connect(self._input_lost)
        ctl.phraseChanged.connect(self._phrase_changed)
        ctl.projectChanged.connect(self._refresh_text)

        self.goto(self._first_pid())
        self._update_mic_label()

    # ------------------------------------------------------------------ navigation
    def _phrases(self):
        return self.ctl.project.catalog.phrases

    def _wanted(self, pid) -> bool:
        return not self.cb_missing.isChecked() or self.ctl.project.status(pid) != "recorded"

    def _first_pid(self):
        for p in self._phrases():
            if self._wanted(p.id):
                return p.id
        return self._phrases()[0].id if self._phrases() else None

    def _next_pid(self, pid):
        ids = [p.id for p in self._phrases()]
        i = ids.index(pid) if pid in ids else -1
        for q in ids[i + 1:]:
            if self._wanted(q):
                return q
        return None

    def _option_toggled(self, v):
        key = self.sender().property("settings_key") if self.sender() is not None else None
        if key:
            st.put(key, v)

    def goto(self, pid, push_history=False):
        if pid is None:
            return
        self.panel.flush_pending()   # a trim nudge in flight belongs to the take it was made on
        self._stop_keep_for_navigation()
        if push_history and self.pid and self.pid != pid:
            self.history.append(self.pid)
        self.pid = pid
        self.state = "idle"
        self.panel.wave.select_handle("")
        self.panel.show_phrase(pid)
        self._refresh_text()
        self._update_state_ui()

    def open_phrase(self, pid):
        """Start recording session on a specific phrase (from the take panel's button)."""
        self.goto(pid, push_history=True)

    def _refresh_text(self):
        if self.pid is None:
            return
        proj = self.ctl.project
        ph = proj.catalog.get(self.pid)
        self.big.setText(proj.spoken_text(self.pid))
        hint = getattr(ph, "hint", None)
        self.hint.setText(f"How to say it: {hint}" if hint else "")
        self.hint.setVisible(bool(hint))
        sysnote = "  ·  only one take is used in-game" if is_single_take(ph) else ""
        self.meta.setText(f"{ph.category.upper()}  ·  {ph.file_stem}.ogg{sysnote}")
        st_ = proj.status(self.pid)
        n = len(self.ctl.takes(self.pid))
        col = theme.STATUS_COLORS.get(st_, theme.MUTED)
        label = theme.STATUS_LABELS.get(st_, st_)
        self.status_badge.setText(f"<span style='color:{col}'>● {label}</span>"
                                  f"<span style='color:{theme.MUTED}'>  ·  {n} take{'s' if n != 1 else ''}</span>")
        nxt = self._next_pid(self.pid)
        self.next_label.setText(f"next:  {proj.spoken_text(nxt)}" if nxt else "next:  — last one! —")
        ids = [p.id for p in self._phrases()]
        done, total = self.ctl.progress()
        self.pos_label.setText(f"Phrase {ids.index(self.pid) + 1} of {total}   ·   {total - done} still missing")

    def _stop_keep_for_navigation(self):
        """Leaving a phrase mid-take: a countdown is cancelled, a running take is stopped and KEPT
        on the phrase it was recorded for (take_ready is emitted synchronously), without autoplay."""
        if self.state == "count":
            self.cancel()
        elif self.state == "rec":
            self._advance_after_take = False
            self._quiet_take = True
            try:
                self.audio.stop_recording()
            finally:
                self._quiet_take = False

    def go_prev(self):
        self._stop_keep_for_navigation()
        if self.history:
            pid = self.history.pop()
        else:
            ids = [p.id for p in self._phrases()]
            i = ids.index(self.pid) if self.pid in ids else 0
            if i == 0:
                return
            pid = ids[i - 1]
        self.goto(pid)
        self.setFocus()

    def skip(self):
        nxt = self._next_pid(self.pid)
        if nxt is None:
            self.ctl.status.emit("That was the last phrase.")
            return
        self.goto(nxt, push_history=True)
        self.setFocus()

    def keep_next(self):
        if self.state == "count":
            self.cancel()
        if self.state == "rec":
            self._advance_after_take = True
            self.audio.stop_recording()
            return
        nxt = self._next_pid(self.pid)
        if nxt is None:
            done, total = self.ctl.progress()
            self.ctl.status.emit(f"End of the list. {done} / {total} recorded.")
            self.state = "idle"
            self._update_state_ui()
            return
        self.goto(nxt, push_history=True)
        self.setFocus()
        if self.cb_hands.isChecked():
            QTimer.singleShot(250, self.start_recording)

    def retake(self):
        if self.read_only:
            return
        self.panel.flush_pending()
        if self.state == "count":
            self.cancel()
            self.start_recording()
            return
        if self.state == "rec":
            self.audio.cancel_recording()
            self.start_recording()
            return
        lst = self.session_takes.get(self.pid) or []
        if lst:
            idx = lst.pop()
            try:
                if idx < len(self.ctl.takes(self.pid)):
                    self.ctl.project.delete_take(self.pid, idx)
            except Exception as e:  # noqa: BLE001
                log.warning("retake delete failed: %s", e)
            self.ctl.notify(self.pid)
            self.panel.show_phrase(self.pid)
        self.start_recording()

    def set_read_only(self, ro: bool):
        self.read_only = bool(ro)
        if self.read_only and self.state in ("rec", "count"):
            self.cancel()
        for b in (self.btn_rec, self.btn_retake):
            b.setEnabled(not self.read_only)
        self.panel.set_read_only(self.read_only)
        self._update_state_ui()

    def _missing_toggled(self, _):
        self._refresh_text()

    # ------------------------------------------------------------------ recording
    def toggle_record(self):
        if self.state == "rec":
            self.audio.stop_recording()
        elif self.state == "count":
            self.cancel()
        else:
            self.start_recording()

    def start_recording(self):
        if self.pid is None or self.state in ("rec", "count") or not self.isVisible() or self.read_only:
            return
        self.setFocus()
        self.audio.stop_playback()
        if self.cb_count.isChecked():
            self.state = "count"
            self._count = 3
            self._update_state_ui()
            self._count_timer.start()
            return
        self._begin()

    def _count_tick(self):
        self._count -= 1
        if self._count <= 0:
            self._count_timer.stop()
            self._begin()
        else:
            self._update_state_ui()

    def _begin(self):
        sens = float(getattr(self.ctl.project, "trim_sensitivity", 0.5))
        self.state = "rec"
        if not self.audio.start_recording(handsfree=self.cb_hands.isChecked(), sensitivity=sens):
            self.state = "idle"
        self._update_state_ui()

    def cancel(self):
        self._count_timer.stop()
        if self.state == "rec":
            self.audio.cancel_recording()
        self.state = "review" if self.session_takes.get(self.pid) else "idle"
        self._update_state_ui()

    def _rec_changed(self, on):
        if on:
            self._pulse.start()
        else:
            self._pulse.stop()
            self.btn_rec.setProperty("pulse", False)
        self._update_state_ui()

    def _take_ready(self, x, sr, automatic):
        if self.pid is None:
            return
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        advance, self._advance_after_take = self._advance_after_take, False
        if len(x) < 0.15 * sr:
            self.state = "idle"
            self._update_state_ui()
            self.state_label.setText("Too short — press Space and try again")
            return
        peak = float(np.max(np.abs(x))) if len(x) else 0.0
        try:
            self.ctl.project.add_take(self.pid, x, sr)
        except Exception as e:  # noqa: BLE001
            log.exception("add_take failed")
            self.state = "idle"
            self._update_state_ui()
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Couldn't save take", str(e))
            return
        idx = len(self.ctl.takes(self.pid)) - 1
        self.session_takes.setdefault(self.pid, []).append(idx)
        self.state = "review"
        self.panel.show_phrase(self.pid, idx)
        self.ctl.notify(self.pid)
        self._refresh_text()
        self._update_state_ui()
        if peak < 0.02:
            self.state_label.setText("That was very quiet — is the right microphone selected?")
        if advance:
            self.keep_next()
            return
        dropout = bool(getattr(self.audio, "dropout_message", None))
        if self._quiet_take:
            return
        if (self.cb_autoplay.isChecked() or self.cb_hands.isChecked()) and self.isVisible() and not dropout:
            self.panel.play_trimmed()
        if self.isVisible():
            self.setFocus()

    def _phrase_changed(self, pid):
        if pid == self.pid:
            self._refresh_text()

    # ------------------------------------------------------------------ visuals
    def _do_pulse(self):
        self.btn_rec.setProperty("pulse", not self.btn_rec.property("pulse"))
        restyle(self.btn_rec)

    def _update_state_ui(self):
        s = self.state
        if s == "rec":
            self.btn_rec.setText("■")
            self.btn_rec.setProperty("state", "rec")
            hf = "  — stops when you stop talking" if self.cb_hands.isChecked() else ""
            self.state_label.setText(f"<span style='color:{theme.RED}'>● Recording…</span>  "
                                     f"<span style='color:{theme.MUTED}'>Space to stop{hf}</span>")
        elif s == "count":
            self.btn_rec.setText(str(self._count))
            self.btn_rec.setProperty("state", "count")
            self.state_label.setText(f"<span style='color:{theme.ACCENT}'>Get ready… {self._count}</span>")
        else:
            self.btn_rec.setText("●")
            self.btn_rec.setProperty("state", "")
            if s == "review":
                self.state_label.setText("Got it.  <span style='color:%s'>Enter keep &amp; next · R retake · "
                                         "Space another take</span>" % theme.MUTED)
            elif self.read_only:
                self.state_label.setText(f"<span style='color:{theme.AMBER}'>Read-only:</span> this project is "
                                         "open in another Co-Driver Studio window, recording is off here.")
            else:
                self.state_label.setText("Press <b>Space</b> and read the line")
        restyle(self.btn_rec)
        has_session = bool(self.session_takes.get(self.pid))
        self.btn_retake.setEnabled((s in ("rec", "review") or has_session) and not self.read_only)
        self.btn_rec.setEnabled(not self.read_only)

    def _clipped(self):
        self.meter.mark_clip()
        if self.isVisible():
            self.clip_label.setText("Too loud! Back off the mic or lower its gain")
            self._clip_timer.start()

    def _input_state_changed(self, _open):
        self._update_mic_label()

    def _input_lost(self, msg):
        """The mic stream died (unplugged / Bluetooth dropped). AudioService already stopped and
        delivered whatever was captured (if long enough) through take_ready."""
        self._count_timer.stop()
        if self.state in ("rec", "count"):
            self.state = "review" if self.session_takes.get(self.pid) else "idle"
        self._advance_after_take = False
        self._update_state_ui()
        self.state_label.setText(f"<span style='color:{theme.RED}'>Microphone disconnected.</span>  "
                                 f"<span style='color:{theme.MUTED}'>{msg or ''}</span>")
        self._update_mic_label()

    def _update_mic_label(self):
        name = self.audio.device_name("input")
        state = "" if self.audio.input_open else "  (closed)"
        self.mic_label.setText(f"Mic: {name}{state}")

    def showEvent(self, e):
        super().showEvent(e)
        self.audio.set_monitoring(True)
        self._refresh_text()
        self._update_mic_label()
        QTimer.singleShot(0, self.setFocus)

    def hideEvent(self, e):
        super().hideEvent(e)
        if e.spontaneous() or self.isVisible():
            # the WINDOW was minimized / unmapped (we're still the current page): never touch a
            # take in progress. Only an idle meter stream is closed.
            if self.state not in ("rec", "count"):
                self.audio.set_monitoring(False)
            return
        # real page switch: a countdown is just cancelled, a running take is stopped AND kept
        if self.state == "count":
            self.cancel()
        elif self.state == "rec":
            self._advance_after_take = False
            self.audio.stop_recording()
        self.audio.set_monitoring(False)
