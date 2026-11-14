"""Pack timing (gap) settings shared by the Pack & Export and Sample Calls pages.

Both pages edit the same ``project.pack`` fields, which the exporter writes into voicepack.json.
Every change is saved and announced through ``Controller.packChanged(field)`` so the other page
updates its controls immediately.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QWidget

from .widgets import FlowLayout

try:
    from ..project import clamp_gap_ms
except ImportError:  # pragma: no cover - fake project modules
    def clamp_gap_ms(name, value):
        lo, hi = (-100, 400) if name == "pacenote_gap_ms" else (-200, 200)
        return max(lo, min(hi, int(round(float(value)))))

# (pack field, label, min ms, max ms, explanation)  — ranges match the game's audioTiming.lua clamps
GAPS = [
    ("link_word_gap_ms", "After “into” / “and”", -200, 200,
     "Gap after a link word. Negative values overlap the words slightly for a fast, natural flow."),
    ("corner_gap_ms", "Inside a corner call", -200, 200,
     "Between the parts of one corner, e.g. “three left” → “long” → “tightens”."),
    ("phrase_gap_ms", "Between other words", -200, 200,
     "Between all other words in one call."),
    ("pacenote_gap_ms", "Between calls", -100, 400,
     "Pause between two separate calls."),
]
GAP_FIELDS = tuple(g[0] for g in GAPS)


def make_gap_spin(field: str, lo: int, hi: int, help_: str) -> QSpinBox:
    sp = QSpinBox()
    sp.setRange(lo, hi)
    sp.setSingleStep(10)
    sp.setSuffix(" ms")
    sp.setToolTip(help_)
    sp.setKeyboardTracking(False)       # typing "120" changes once, not three times
    sp.setProperty("gap_field", field)
    return sp


def set_gap(ctl, field: str, value) -> bool:
    """Write one gap to the pack (clamped like the game), save, and broadcast. True if it changed."""
    pk = ctl.project.pack
    value = clamp_gap_ms(field, value)
    if getattr(pk, field, None) == value:
        return False
    setattr(pk, field, value)
    ctl.save()
    sig = getattr(ctl, "packChanged", None)
    if sig is not None:
        sig.emit(field)
    return True


class TimingRow(QWidget):
    """Compact, wrapping row of the four gap spin boxes (Sample Calls page)."""

    changed = Signal(str)               # a gap was changed from THIS row (field name)

    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self.spins: dict[str, QSpinBox] = {}
        flow = FlowLayout(self, spacing=14, vspacing=6)
        flow.setContentsMargins(0, 0, 0, 0)
        for field, label, lo, hi, help_ in GAPS:
            cell = QWidget()
            h = QHBoxLayout(cell)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            lab = QLabel(label)
            lab.setToolTip(help_)
            sp = make_gap_spin(field, lo, hi, help_)
            sp.valueChanged.connect(self._spin_changed)
            h.addWidget(lab)
            h.addWidget(sp)
            flow.addWidget(cell)
            self.spins[field] = sp
        if getattr(ctl, "packChanged", None) is not None:
            ctl.packChanged.connect(self._pack_changed)
        self.load()

    def load(self):
        pk = self.ctl.project.pack
        for field, sp in self.spins.items():
            sp.blockSignals(True)
            sp.setValue(int(getattr(pk, field, 0)))
            sp.blockSignals(False)

    def set_read_only(self, ro: bool):
        for sp in self.spins.values():
            sp.setEnabled(not ro)

    def _spin_changed(self, value):
        field = self.sender().property("gap_field") if self.sender() is not None else None
        if field and set_gap(self.ctl, field, value):
            self.changed.emit(field)

    def _pack_changed(self, field):
        if field in self.spins:
            self.load()
