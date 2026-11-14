"""Audio settings dialog: input / output device pickers with a live mic test."""
from __future__ import annotations

import numpy as np
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)

from .widgets import LevelMeter, hint_label

DEFAULT = "System default"


class AudioSettingsDialog(QDialog):
    def __init__(self, audio, parent=None):
        super().__init__(parent)
        self.audio = audio
        self.setWindowTitle("Audio settings")
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance("M") * 34)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.inp = QComboBox()
        self.out = QComboBox()
        self._fill()
        self.inp.currentIndexChanged.connect(lambda _: self.audio.set_input_device(self.inp.currentData()))
        self.out.currentIndexChanged.connect(lambda _: self.audio.set_output_device(self.out.currentData()))
        form.addRow("Microphone", self.inp)
        self.meter = LevelMeter()
        form.addRow("Level", self.meter)
        form.addRow("", hint_label("Speak at normal co-driver volume: the bar should reach the yellow zone "
                                   "but never light up CLIP."))
        form.addRow("Playback", self.out)
        lay.addLayout(form)
        row = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh devices")
        self.btn_refresh.clicked.connect(self._fill)
        self.btn_beep = QPushButton("▶  Test playback")
        self.btn_beep.clicked.connect(self._beep)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_beep)
        row.addStretch(1)
        lay.addLayout(row)
        self.state = QLabel("")
        self.state.setProperty("hint", True)
        lay.addWidget(self.state)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        audio.level.connect(self.meter.set_level)
        audio.clipped.connect(self.meter.mark_clip)
        audio.input_state.connect(self._input_state)
        self._was_monitoring = audio._monitor_wanted
        audio.set_monitoring(True)
        self._input_state(audio.input_open)

    def _fill(self):
        for combo, devs, cur in ((self.inp, self.audio.input_devices(), self.audio.input_device),
                                 (self.out, self.audio.output_devices(), self.audio.output_device)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(DEFAULT, None)
            for idx, name in devs:
                combo.addItem(name, idx)
            i = combo.findData(cur)
            combo.setCurrentIndex(max(0, i))
            combo.blockSignals(False)
        if not self.audio.input_devices():
            self.inp.setToolTip("No microphone found")

    def _input_state(self, open_):
        self.state.setText("Microphone is live." if open_ else
                           "Microphone isn't open. Check that it's plugged in and allowed in Windows privacy settings.")

    def _beep(self):
        sr = 48000
        t = np.arange(int(sr * 0.35)) / sr
        y = (0.25 * np.sin(2 * np.pi * 660 * t) * np.minimum(1, (0.35 - t) * 30) * np.minimum(1, t * 60))
        self.audio.play(y.astype(np.float32), sr, "settings-beep")

    def done(self, r):
        self.audio.set_monitoring(self._was_monitoring)
        try:
            self.audio.level.disconnect(self.meter.set_level)
            self.audio.clipped.disconnect(self.meter.mark_clip)
            self.audio.input_state.disconnect(self._input_state)
        except (RuntimeError, TypeError):
            pass
        super().done(r)
