"""Effects page: preset + per-parameter sliders, A/B preview on any recorded phrase."""
from __future__ import annotations

import copy
import dataclasses
import json

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget)

from ... import dsp
from .. import theme
from ..errors import log
from ..widgets import hint_label, page_header
from ..workers import run_async

TOKEN = "effects-preview"


def _rng(lo, hi, step):
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 6) for i in range(n + 1)]


def _hz(v):
    return f"{v / 1000:.1f} kHz" if v >= 1000 else f"{v:.0f} Hz"


# field -> (label, values, formatter, help, group)
SPECS = {
    "highpass_hz": ("Low cut", [0.0] + _rng(40, 1200, 10), lambda v: "Off" if not v else _hz(v),
                    "Removes boom and rumble. Radios ≈ 300–600 Hz.", "Tone"),
    "lowpass_hz": ("High cut", _rng(1500, 12000, 100) + [0.0], lambda v: "Off" if not v else _hz(v),
                   "Removes the top end. Radios ≈ 2.5–4 kHz.", "Tone"),
    "filter_order": ("Filter steepness", [2, 4, 6, 8], lambda v: f"{int(v) * 6} dB/oct",
                     "How sharply the cuts above kick in.", "Tone"),
    "drive": ("Drive", _rng(0, 1, 0.01), lambda v: "Off" if not v else f"{v * 100:.0f}%",
              "Saturation / grit of an overdriven radio.", "Character"),
    "bitcrush_bits": ("Bit crush", [0] + list(range(16, 1, -1)), lambda v: "Off" if not v else f"{int(v)} bit",
                      "Lo-fi digital crunch. Fewer bits = harsher.", "Character"),
    "compression": ("Compression", _rng(0, 1, 0.01), lambda v: "Off" if not v else f"{v * 100:.0f}%",
                    "Evens out loud and quiet words so every call cuts through engine noise.", "Dynamics"),
    "reverb": ("Helmet / room", _rng(0, 1, 0.01), lambda v: "Off" if not v else f"{v * 100:.0f}%",
               "Small boxy reflections, like speaking inside a helmet.", "Character"),
    "noise_db": ("Background hiss", [None] + _rng(-70, -20, 1), lambda v: "Off" if v is None else f"{v:.0f} dB",
                 "Constant radio hiss under the voice.", "Radio"),
    "squelch_db": ("Squelch level", _rng(-40, -6, 1), lambda v: f"{v:.0f} dB",
                   "Loudness of the squelch burst.", "Radio"),
    "gain_db": ("Extra gain", _rng(-12, 12, 0.5), lambda v: f"{v:+.1f} dB",
                "Added after everything else (the final safety limiter still prevents clipping).", "Output"),
    "target_rms_db": ("Loudness (RMS dB)", _rng(-30, -16, 0.5), lambda v: f"{v:.1f} dB",
                      "Every take is levelled to this average loudness so all calls sound equally loud.", "Output"),
    "peak_ceiling_db": ("Peak ceiling", _rng(-12, -0.5, 0.5), lambda v: f"{v:.1f} dBFS",
                        "Loud peaks are held below this level.", "Output"),
    "normalize_db": ("Peak level", [None] + _rng(-12, 0, 0.5), lambda v: "Off" if v is None else f"{v:.1f} dBFS",
                     "Every take is levelled to this peak so all calls are equally loud.", "Output"),
}
GROUP_ORDER = ["Tone", "Character", "Dynamics", "Radio", "Output"]
CHECKS = {"squelch": ("Squelch clicks at start and end", "Radio"),
          "normalize": ("Level every take to the same loudness", "Output")}


def _same(a, b) -> bool:
    try:
        return json.dumps(a.to_dict(), sort_keys=True) == json.dumps(b.to_dict(), sort_keys=True)
    except Exception:  # noqa: BLE001
        return a == b


class ValueSlider(QWidget):
    def __init__(self, values, fmt, on_change, parent=None):
        super().__init__(parent)
        self.values, self.fmt, self.on_change = values, fmt, on_change
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, len(values) - 1)
        self.slider.setPageStep(max(1, len(values) // 10))
        self.label = QLabel("")
        # room for the widest value ("-12.0 dBFS") so the sliders don't jump while dragging
        self.label.setMinimumWidth(self.label.fontMetrics().horizontalAdvance("-12.0 dBFS") + 6)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.label)
        self.slider.valueChanged.connect(self._changed)

    def value(self):
        return self.values[self.slider.value()]

    def set_value(self, v):
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            idx = next((i for i, x in enumerate(self.values) if x is None or x == 0.0), 0) \
                if None in self.values else 0
        else:
            cands = [(abs(float(x) - float(v)), i) for i, x in enumerate(self.values) if x is not None
                     and not (x == 0 and v != 0 and 0 in self.values[:1] + self.values[-1:] and len(self.values) > 2)]
            idx = min(cands)[1] if cands else 0
        self.slider.blockSignals(True)
        self.slider.setValue(idx)
        self.slider.blockSignals(False)
        self.label.setText(self.fmt(self.values[idx]))

    def _changed(self, i):
        self.label.setText(self.fmt(self.values[i]))
        self.on_change()


class EffectsPage(QWidget):
    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self._building = False
        self._sliders: dict[str, ValueSlider] = {}
        self._checks: dict[str, QCheckBox] = {}
        self._cache: dict = {}
        self._want_play = False
        self._ro = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)
        outer.addWidget(page_header("Effects", "Make your voice sound like it's coming through a rally intercom. "
                                    "Effects are applied when playing and exporting. Your raw recordings never change."))

        # preset row
        row = QHBoxLayout()
        self.master = QCheckBox("Effects on")
        self.master.toggled.connect(self._changed)
        self.preset = QComboBox()
        self.preset.addItems(list(dsp.PRESETS.keys()) + ["Custom"])
        self.preset.setMinimumWidth(self.preset.fontMetrics().horizontalAdvance("M") * 11)
        self.preset.activated.connect(self._preset_chosen)
        lab = QLabel("Preset")
        lab.setProperty("muted", True)
        row.addWidget(self.master)
        row.addSpacing(16)
        row.addWidget(lab)
        row.addWidget(self.preset)
        row.addStretch(1)
        outer.addLayout(row)

        # preview row
        prev = QGroupBox("Preview")
        pl = QHBoxLayout(prev)
        pl.setSpacing(8)
        pl.addWidget(QLabel("Preview on:"))
        self.phrase_combo = QComboBox()
        self.phrase_combo.setMinimumWidth(self.phrase_combo.fontMetrics().horizontalAdvance("M") * 10)
        self.phrase_combo.currentIndexChanged.connect(self._play_if_playing)
        pl.addWidget(self.phrase_combo, 1)
        self.btn_a = QPushButton("A  ·  Dry")
        self.btn_b = QPushButton("B  ·  With effects")
        self.btn_a.setToolTip("Your voice without effects (just levelled)")
        for b in (self.btn_a, self.btn_b):
            b.setCheckable(True)
        self.btn_b.setChecked(True)
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        grp.addButton(self.btn_a)
        grp.addButton(self.btn_b)
        self.btn_a.toggled.connect(self._ab_toggled)
        self.btn_b.toggled.connect(self._ab_toggled)
        self.btn_play = QPushButton("▶  Play")
        self.btn_play.setProperty("primary", True)
        self.btn_play.clicked.connect(self.play)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.clicked.connect(self.ctl.audio.stop_playback)
        for w in (self.btn_a, self.btn_b, self.btn_play, self.btn_stop):
            pl.addWidget(w)
        outer.addWidget(prev)
        self.no_takes = hint_label("Record a few phrases first to hear the effects on your own voice.")
        outer.addWidget(self.no_takes)

        # parameter groups
        fields = {f.name for f in dataclasses.fields(dsp.EffectSettings)}
        groups: dict[str, QGridLayout] = {}
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 4, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        boxes = {}
        for gname in GROUP_ORDER:
            box = QGroupBox(gname)
            gl = QGridLayout(box)
            gl.setHorizontalSpacing(12)
            gl.setVerticalSpacing(4)
            gl.setColumnStretch(1, 1)
            groups[gname] = gl
            boxes[gname] = box
        for name, (label, values, fmt, help_, gname) in SPECS.items():
            if name not in fields:
                continue
            gl = groups[gname]
            r = gl.rowCount()
            lab = QLabel(label)
            lab.setMinimumWidth(lab.fontMetrics().horizontalAdvance("Filter steepness") + 4)
            lab.setToolTip(help_)
            s = ValueSlider(values, fmt, self._changed)
            s.setToolTip(help_)
            gl.addWidget(lab, r, 0)
            gl.addWidget(s, r, 1)
            h = hint_label(help_)
            gl.addWidget(h, r + 1, 1)
            self._sliders[name] = s
        for name, (label, gname) in CHECKS.items():
            if name not in fields:
                continue
            cb = QCheckBox(label)
            cb.toggled.connect(self._changed)
            groups[gname].addWidget(cb, groups[gname].rowCount(), 0, 1, 2)
            self._checks[name] = cb
        # two columns of group boxes
        left = ["Tone", "Character"]
        right = ["Dynamics", "Radio", "Output"]
        for col, names in enumerate((left, right)):
            v = QVBoxLayout()
            v.setSpacing(10)
            for n in names:
                if groups[n].count():
                    v.addWidget(boxes[n])
            v.addStretch(1)
            grid.addLayout(v, 0, col)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._commit)

        # AudioService outlives this page: bound methods only (Qt disconnects them on destruction)
        ctl.audio.playback_finished.connect(self._playback_finished)
        ctl.phraseChanged.connect(self._phrase_changed)
        self.load_from_project()

    def _playback_finished(self, token):
        if token == TOKEN:
            self._set_playing(False)

    def _phrase_changed(self, _pid):
        self._cache.clear()

    def _ab_toggled(self, on):
        if on:
            self._switch_ab()

    # ------------------------------------------------------------------ state <-> widgets
    def load_from_project(self):
        self._building = True
        eff = self.ctl.project.effects
        self.master.setChecked(bool(getattr(eff, "enabled", True)))
        for name, s in self._sliders.items():
            s.set_value(getattr(eff, name, None))
        for name, cb in self._checks.items():
            cb.setChecked(bool(getattr(eff, name, False)))
        self._sync_preset_combo()
        self._update_enabled()
        self._building = False

    def current_settings(self):
        eff = copy.deepcopy(self.ctl.project.effects)
        eff.enabled = self.master.isChecked()
        for name, s in self._sliders.items():
            v = s.value()
            if name in ("filter_order", "bitcrush_bits") and v is not None:
                v = int(v)
            setattr(eff, name, v)
        for name, cb in self._checks.items():
            setattr(eff, name, cb.isChecked())
        return eff

    def _sync_preset_combo(self):
        eff = self.ctl.project.effects
        cmp = copy.deepcopy(eff)
        cmp.enabled = True
        for i, (name, pre) in enumerate(dsp.PRESETS.items()):
            if _same(cmp, pre):
                self.preset.setCurrentIndex(i)
                return
        self.preset.setCurrentIndex(self.preset.count() - 1)

    def set_read_only(self, ro: bool):
        self._ro = bool(ro)
        self.master.setEnabled(not self._ro)
        self._update_enabled()

    def _update_enabled(self):
        on = self.master.isChecked() and not self._ro
        for s in self._sliders.values():
            s.setEnabled(on)
        for cb in self._checks.values():
            cb.setEnabled(on)
        self.preset.setEnabled(on)
        if "squelch_db" in self._sliders and "squelch" in self._checks:
            self._sliders["squelch_db"].setEnabled(on and self._checks["squelch"].isChecked())
        if "normalize" in self._checks:
            for n in ("target_rms_db", "peak_ceiling_db"):
                if n in self._sliders:
                    self._sliders[n].setEnabled(not self._ro and self._checks["normalize"].isChecked())

    def _preset_chosen(self, i):
        names = list(dsp.PRESETS.keys())
        if i >= len(names):
            return
        new = copy.deepcopy(dsp.PRESETS[names[i]])
        new.enabled = True
        self.ctl.project.effects = new
        self.load_from_project()
        self._debounce.start()

    def _changed(self, *_):
        if self._building:
            return
        self._update_enabled()
        self.ctl.project.effects = self.current_settings()
        self._sync_preset_combo()
        self._debounce.start()

    def _commit(self):
        self.ctl.save()
        self._play_if_playing()

    # ------------------------------------------------------------------ preview
    def refresh_phrases(self):
        cur = self.phrase_combo.currentData()
        self.phrase_combo.blockSignals(True)
        self.phrase_combo.clear()
        proj = self.ctl.project
        for p in proj.catalog.phrases:
            if self.ctl.first_enabled(p.id) is not None:
                self.phrase_combo.addItem(proj.spoken_text(p.id), p.id)
        if cur is not None:
            i = self.phrase_combo.findData(cur)
            if i >= 0:
                self.phrase_combo.setCurrentIndex(i)
        else:
            # prefer a longer, more interesting phrase to show off effects
            best, best_len = 0, -1
            for i in range(self.phrase_combo.count()):
                n = len(self.phrase_combo.itemText(i))
                if n > best_len and not proj.catalog.get(self.phrase_combo.itemData(i)).is_system:
                    best, best_len = i, n
            if self.phrase_combo.count():
                self.phrase_combo.setCurrentIndex(best)
        self.phrase_combo.blockSignals(False)
        has = self.phrase_combo.count() > 0
        for w in (self.phrase_combo, self.btn_play, self.btn_a, self.btn_b):
            w.setEnabled(has)
        self.no_takes.setVisible(not has)

    def _render(self, wet: bool, on_done):
        pid = self.phrase_combo.currentData()
        if pid is None:
            return
        idx = self.ctl.first_enabled(pid)
        if idx is None:
            return
        proj = self.ctl.project
        t = self.ctl.takes(pid)[idx]
        settings = copy.deepcopy(proj.effects) if wet else copy.deepcopy(dsp.PRESETS["Clean"])
        key = (pid, t.file, t.trim_start, t.trim_end, json.dumps(settings.to_dict(), sort_keys=True, default=str))
        if key in self._cache:
            on_done(*self._cache[key])
            return
        out_sr = int(proj.pack.export_sample_rate)
        seed = proj.take_seed(pid, idx) if hasattr(proj, "take_seed") else 0
        ts, te = t.trim_start, t.trim_end
        file = t.file

        def work():
            # resolve the take by file at render time: a concurrent delete can't swap it for another
            lock = getattr(proj, "lock", None)
            if lock is not None:
                lock.acquire()
            try:
                cur = next((i for i, tk in enumerate(proj.takes(pid)) if tk.file == file), None)
                if cur is None:
                    raise LookupError(f"the take {file} was deleted")
                x, sr = proj.load_take_audio(pid, cur)
            finally:
                if lock is not None:
                    lock.release()
            y = dsp.render_take(x, sr, ts, te, settings, out_sr=out_sr, seed=seed)
            return np.asarray(y, np.float32), out_sr

        def done(res):
            if len(self._cache) > 40:
                self._cache.clear()
            self._cache[key] = res
            on_done(*res)
        run_async(work, done, self._preview_failed, owner=self)

    def _preview_failed(self, e, _tb):
        self.ctl.status.emit(f"Preview failed: {e}")

    def play(self, from_pos: float = 0.0):
        wet = self.btn_b.isChecked()

        def go(y, sr):
            start = int(max(0.0, from_pos) * sr)
            if start >= len(y):
                start = 0
            if self.ctl.audio.play(y[start:], sr, TOKEN):
                self._set_playing(True)
        self._render(wet, go)

    def _set_playing(self, on):
        self._want_play = on

    def _switch_ab(self):
        if self.ctl.audio.play_token == TOKEN:
            self.play(self.ctl.audio.playback_pos())

    def _play_if_playing(self, *_):
        if self.ctl.audio.play_token == TOKEN:
            self.play(0.0)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_phrases()
