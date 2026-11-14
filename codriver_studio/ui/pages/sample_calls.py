"""Sample Calls page: type or click a pacenote, hear it composed from your takes."""
from __future__ import annotations

import random
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ... import sample_calls
from .. import theme
from ..widgets import ElidedLabel, FlowLayout, page_header
from ..workers import run_async

TOKEN = "sample-call"
STAGE_CALLS = 8
EXAMPLE = "150 three left into hard right don't cut"


def _norm(word: str) -> str:
    return word.lower().replace("'", "").replace("’", "")


class UnknownHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.unknown: set[str] = set()
        self.fmt = QTextCharFormat()
        self.fmt.setForeground(QColor(theme.RED))
        self.fmt.setUnderlineStyle(QTextCharFormat.WaveUnderline)
        self.fmt.setUnderlineColor(QColor(theme.RED))

    def highlightBlock(self, text):
        if not self.unknown:
            return
        for m in re.finditer(r"[A-Za-z0-9'’]+", text):
            if _norm(m.group(0)) in self.unknown:
                self.setFormat(m.start(), m.end() - m.start(), self.fmt)


class CallEdit(QPlainTextEdit):
    def __init__(self, on_enter, parent=None):
        super().__init__(parent)
        self.on_enter = on_enter

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers() & Qt.ShiftModifier:
            self.on_enter()
            return
        super().keyPressEvent(e)


class SampleCallsPage(QWidget):
    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self.ids: list[str] = []
        self.unknown: list[str] = []
        self.rng = random.Random()
        self._chips: dict[str, QPushButton] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)
        outer.addWidget(page_header("Sample Calls", "Hear how your voice sounds as a real pacenote, stitched "
                                    "together with your pack's gap settings, just like in the game."))

        self.edit = CallEdit(self.play)
        self.edit.setPlaceholderText(f"Type a call, e.g. “{EXAMPLE}”, then press Enter")
        self.edit.setStyleSheet("font-size: 14pt;")
        self.edit.ensurePolished()
        # two lines of the 14 pt call text + the editor's padding/frame, whatever the DPI
        fm = QFontMetrics(self.edit.font())
        self.edit.setFixedHeight(int(2 * fm.lineSpacing() + 2 * self.edit.document().documentMargin()
                                     + 2 * self.edit.frameWidth() + 12))
        self.hl = UnknownHighlighter(self.edit.document())
        self.edit.textChanged.connect(lambda: self._parse_timer.start())
        outer.addWidget(self.edit)

        # parsed tokens
        self.parsed = QWidget()
        self.parsed_flow = FlowLayout(self.parsed, spacing=6)
        self.parsed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        outer.addWidget(self.parsed)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_play = QPushButton("▶  Play")
        self.btn_play.setProperty("primary", True)
        self.btn_play.clicked.connect(self.play)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.clicked.connect(ctl.audio.stop_playback)
        self.btn_random = QPushButton("Random call")
        self.btn_random.clicked.connect(self.random_call)
        self.btn_stage = QPushButton(f"Random stage  ({STAGE_CALLS} calls)")
        self.btn_stage.setToolTip("Plays several random calls back to back, separated by the pacenote gap")
        self.btn_stage.clicked.connect(self.random_stage)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.edit.clear)
        self.cb_vary = QCheckBox("Vary takes")
        self.cb_vary.setToolTip("Pick a random ticked take for each phrase, like the game does")
        self.cb_vary.setChecked(True)
        for w in (self.btn_play, self.btn_stop, self.btn_random, self.btn_stage, self.btn_clear):
            row.addWidget(w)
        row.addSpacing(10)
        row.addWidget(self.cb_vary)
        row.addStretch(1)
        outer.addLayout(row)

        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        outer.addWidget(self.info)

        # phrase palette
        pal = QGroupBox("Click to add")
        pv = QVBoxLayout(pal)
        pv.setContentsMargins(8, 8, 8, 8)
        # single line (elided), not word-wrapped: a wrapped label would make the whole box
        # height-for-width, and Qt then never shrinks it below the chip area's preferred height
        legend = ElidedLabel("Solid = recorded, dashed = not recorded yet (will be skipped).")
        legend.setProperty("hint", True)
        pv.addWidget(legend)
        holder = QWidget()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(0, 0, 6, 0)
        hv.setSpacing(6)
        cat = ctl.project.catalog
        for c in cat.categories:
            if c == "system":
                continue
            phrases = cat.by_category(c)
            if not phrases:
                continue
            lab = QLabel(c.capitalize())
            lab.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 600; margin-top: 4px;")
            hv.addWidget(lab)
            w = QWidget()
            fl = FlowLayout(w, spacing=5)
            for p in phrases:
                b = QPushButton(p.text)
                b.setProperty("chip", True)
                b.setFocusPolicy(Qt.NoFocus)
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, pid=p.id: self.append_phrase(pid))
                fl.addWidget(b)
                self._chips[p.id] = b
            hv.addWidget(w)
        hv.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        pv.addWidget(scroll)
        outer.addWidget(pal, 1)

        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(120)
        self._parse_timer.timeout.connect(self.reparse)
        ctl.phraseChanged.connect(self._phrase_changed)
        self.reparse()

    # ------------------------------------------------------------------ parsing
    def text(self) -> str:
        return self.edit.toPlainText()

    def reparse(self):
        txt = self.text().strip()
        if txt:
            self.ids, self.unknown = sample_calls.parse_call(self.ctl.project.catalog, txt)
        else:
            self.ids, self.unknown = [], []
        self.hl.unknown = {_norm(u) for u in self.unknown}
        self.hl.rehighlight()
        self._show_tokens()
        self._update_info()

    def _show_tokens(self):
        self.parsed_flow.clear()
        proj = self.ctl.project
        for pid in self.ids:
            rec = self.ctl.first_enabled(pid) is not None
            lab = QLabel(proj.catalog.get(pid).text)
            col = theme.GREEN if rec else theme.AMBER
            lab.setStyleSheet(f"border: 1px {'solid' if rec else 'dashed'} {col}; border-radius: 10px; "
                              f"padding: 2px 9px; color: {col if not rec else theme.TEXT};")
            lab.setToolTip(f"{pid}  ·  {'recorded' if rec else 'not recorded yet'}")
            self.parsed_flow.addWidget(lab)
            lab.show()
        for u in self.unknown:
            lab = QLabel(u)
            lab.setStyleSheet(f"border: 1px solid {theme.RED}; border-radius: 10px; padding: 2px 9px; "
                              f"color: {theme.RED}; text-decoration: line-through;")
            lab.setToolTip("Not a word the co-driver knows. It is skipped.")
            self.parsed_flow.addWidget(lab)
            lab.show()
        self.parsed.setVisible(bool(self.ids or self.unknown))
        self._fit_parsed()

    def _fit_parsed(self):
        w = max(200, self.parsed.width() or self.width() - 40)
        self.parsed.setFixedHeight(max(0, self.parsed_flow.heightForWidth(w)))
        self.parsed_flow.setGeometry(self.parsed.rect())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_parsed()

    def _update_info(self, extra_missing=None):
        proj = self.ctl.project
        missing = [pid for pid in self.ids if self.ctl.first_enabled(pid) is None]
        if extra_missing:
            missing = list(dict.fromkeys(list(extra_missing) + missing))
        bits = []
        if self.unknown:
            bits.append(f"<span style='color:{theme.RED}'>Unknown: {', '.join(self.unknown)}</span>")
        if missing:
            names = ", ".join(self._phrase_text(p) for p in missing[:12])
            more = f" (+{len(missing) - 12} more)" if len(missing) > 12 else ""
            bits.append(f"<span style='color:{theme.AMBER}'>Not recorded yet: {names}{more}</span>")
        self.info.setText("  &nbsp;·&nbsp;  ".join(bits))
        self.info.setVisible(bool(bits))

    def _phrase_text(self, pid):
        try:
            return self.ctl.project.catalog.get(pid).text
        except KeyError:
            return pid

    def _phrase_changed(self, pid):
        b = self._chips.get(pid)
        if b is not None:
            self._style_chip(pid, b)
        if pid in self.ids:
            self._show_tokens()
            self._update_info()

    def _style_chip(self, pid, b):
        missing = self.ctl.first_enabled(pid) is None
        if bool(b.property("missing")) != missing:
            b.setProperty("missing", missing)
            b.style().unpolish(b)
            b.style().polish(b)

    def refresh_chips(self):
        for pid, b in self._chips.items():
            self._style_chip(pid, b)

    def append_phrase(self, pid):
        t = self.text().rstrip()
        word = self.ctl.project.catalog.get(pid).text
        self.edit.setPlainText(f"{t} {word}".strip())
        c = self.edit.textCursor()
        c.movePosition(c.MoveOperation.End)
        self.edit.setTextCursor(c)
        self.reparse()

    # ------------------------------------------------------------------ playing
    def _out_sr(self):
        return int(getattr(self.ctl.project.pack, "export_sample_rate", 48000) or 48000)

    def play(self):
        self.reparse()
        if not self.ids:
            self.ctl.status.emit("Type a call or click some phrases first.")
            return
        self._compose([list(self.ids)])

    def _compose(self, notes, transcript=None):
        proj = self.ctl.project
        out_sr = self._out_sr()
        rng = random.Random(self.rng.random()) if self.cb_vary.isChecked() else None
        self.btn_play.setEnabled(False)
        self.btn_stage.setEnabled(False)

        def work():
            if len(notes) == 1:
                return sample_calls.compose(proj, notes[0], out_sr=out_sr, rng=rng)
            if hasattr(sample_calls, "compose_stage"):
                return sample_calls.compose_stage(proj, notes, out_sr=out_sr, rng=rng)
            import numpy as np
            parts, miss, sr = [], [], out_sr
            gap = np.zeros(int(max(0, proj.pack.pacenote_gap_ms) * out_sr / 1000), np.float32)
            for n in notes:
                y, sr, m = sample_calls.compose(proj, n, out_sr=out_sr, rng=rng)
                parts += [y, gap]
                miss += m
            return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), sr, miss

        def done(res):
            y, sr, missing = res
            self.btn_play.setEnabled(True)
            self.btn_stage.setEnabled(True)
            self.last_audio = (y, sr)
            self._update_info(missing)
            if transcript:
                self.info.setText(transcript + ("<br>" + self.info.text() if self.info.text() else ""))
                self.info.setVisible(True)
            if len(y):
                self.ctl.audio.play(y, sr, TOKEN)
            else:
                self.ctl.status.emit("Nothing recorded for this call yet.")

        def failed(e, tb):
            self.btn_play.setEnabled(True)
            self.btn_stage.setEnabled(True)
            self.ctl.status.emit(f"Couldn't build the call: {e}")
        run_async(work, done, failed, owner=self)

    def _random_note(self):
        """A random call, preferring phrases that are recorded (a few tries)."""
        cat = self.ctl.project.catalog
        best = None
        for _ in range(12):
            ids = sample_calls.random_call(cat, self.rng)
            score = sum(self.ctl.first_enabled(p) is not None for p in ids) / max(1, len(ids))
            if best is None or score > best[0]:
                best = (score, ids)
            if score == 1.0:
                break
        return best[1]

    def random_call(self):
        ids = self._random_note()
        cat = self.ctl.project.catalog
        self.edit.setPlainText(" ".join(cat.get(p).text for p in ids))
        self.reparse()
        self.ids = list(ids)
        self._compose([list(ids)])

    def random_stage(self):
        cat = self.ctl.project.catalog
        notes = [self._random_note() for _ in range(STAGE_CALLS)]
        lines = [" ".join(cat.get(p).text for p in n) for n in notes]
        transcript = "<b>Stage:</b> " + "  ›  ".join(lines)
        self.edit.setPlainText(lines[0])
        self.reparse()
        self._compose(notes, transcript)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_chips()
        self.reparse()
