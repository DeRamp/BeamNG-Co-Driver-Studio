"""Waveform view with draggable trim handles, zoom, playhead and keyboard nudge."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import dsp
from . import theme

HANDLE_GRAB_PX = 8
MIN_LEN_S = 0.03
RULER_H = 18
MIN_HEIGHT = 110       # logical px: still usable for trimming on a 1080p @ 150 % screen


class WaveformView(QWidget):
    trimDragging = Signal(float, float)   # live while dragging
    trimEdited = Signal(float, float)     # committed (mouse release / nudge idle)
    # same commit, plus the context (take identity) that was current when the edit STARTED.
    # Owners should use this one: a pending nudge commit may fire after the view moved on.
    trimCommitted = Signal(object, float, float)
    handleSelected = Signal(str)          # "start" | "end" | ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(MIN_HEIGHT)
        self.setMinimumWidth(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.x = np.zeros(0, np.float32)
        self.sr = 48000
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.view0 = 0.0
        self.view1 = 1.0
        self.selected = ""
        self.playhead: float | None = None
        self._drag: str | None = None
        self._pan_from: tuple[float, float, float] | None = None
        self._peaks_key = None
        self._peaks = None
        self._placeholder = "No take selected"
        self._commit = QTimer(self)
        self._commit.setSingleShot(True)
        self._commit.setInterval(350)
        self._commit.timeout.connect(self._emit_commit)
        self.context = None          # opaque take identity set by the owner with set_audio()
        self._edit_context = None    # context captured when the pending edit started

    preferred_height = 170   # owners on tight pages lower this so other widgets get the space first

    def sizeHint(self):
        return QSize(480, max(MIN_HEIGHT, self.preferred_height))

    def minimumSizeHint(self):
        return QSize(160, MIN_HEIGHT)

    # ------------------------------------------------------------------ data
    def set_placeholder(self, text: str):
        self._placeholder = text
        self.update()

    def set_audio(self, x, sr, start=None, end=None, context=None):
        self._flush_commit()
        self.context = context
        self.x = np.asarray(x, dtype=np.float32).reshape(-1)
        self.sr = int(sr)
        self.duration = len(self.x) / self.sr if self.sr else 0.0
        self.start = 0.0 if start is None else float(start)
        self.end = self.duration if end is None else float(end)
        self._clamp()
        self.playhead = None
        self._peaks_key = None
        self.fit()

    def clear(self, placeholder: str | None = None):
        self._flush_commit()
        self.context = None
        if placeholder is not None:
            self._placeholder = placeholder
        self.x = np.zeros(0, np.float32)
        self.duration = 0.0
        self.start = self.end = 0.0
        self.playhead = None
        self.selected = ""
        self._peaks_key = None
        self.update()

    @property
    def has_audio(self) -> bool:
        return len(self.x) > 0

    def set_trim(self, start, end, emit=False):
        self.start, self.end = float(start), float(end)
        self._clamp()
        self.update()
        if emit:
            self._commit.stop()
            self._edit_context = self.context
            self._emit_commit()

    def trim(self):
        return self.start, self.end

    def set_playhead(self, t):
        self.playhead = t
        if t is not None and self.has_audio and not (self.view0 <= t <= self.view1):
            span = self.view1 - self.view0
            self.view0 = max(0.0, min(t - span * 0.1, self.duration - span))
            self.view1 = self.view0 + span
            self._peaks_key = None
        self.update()

    def _clamp(self):
        d = self.duration
        self.start = max(0.0, min(self.start, d))
        self.end = max(0.0, min(self.end, d))
        if self.end - self.start < MIN_LEN_S:
            if self.end >= d:
                self.start = max(0.0, self.end - MIN_LEN_S)
            else:
                self.end = min(d, self.start + MIN_LEN_S)

    def _begin_edit(self):
        """Remember which take this edit belongs to (only at the start of an edit burst)."""
        if not self._commit.isActive() and self._drag is None:
            self._edit_context = self.context

    def _emit_commit(self):
        ctx, self._edit_context = self._edit_context, None
        self.trimCommitted.emit(ctx, self.start, self.end)
        self.trimEdited.emit(self.start, self.end)

    def _flush_commit(self):
        """Commit a pending (debounced) nudge right now, for the take it was made on."""
        if self._commit.isActive():
            self._commit.stop()
            self._emit_commit()

    flush_pending = _flush_commit

    @property
    def has_pending_commit(self) -> bool:
        return self._commit.isActive()

    # ------------------------------------------------------------------ view
    def fit(self):
        self.view0, self.view1 = 0.0, max(self.duration, 0.05)
        self._peaks_key = None
        self.update()

    def zoom(self, factor: float, center_t: float | None = None):
        if not self.has_audio:
            return
        span = self.view1 - self.view0
        c = (self.view0 + self.view1) / 2 if center_t is None else center_t
        new = max(0.02, min(self.duration, span / factor))
        rel = (c - self.view0) / span if span else 0.5
        v0 = c - rel * new
        v0 = max(0.0, min(v0, self.duration - new))
        self.view0, self.view1 = v0, v0 + new
        self._peaks_key = None
        self.update()

    def _wave_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(1, 1, -1, -1 - RULER_H)

    def x_for_time(self, t: float) -> float:
        r = self._wave_rect()
        span = (self.view1 - self.view0) or 1.0
        return r.left() + (t - self.view0) / span * r.width()

    def time_for_x(self, x: float) -> float:
        r = self._wave_rect()
        span = self.view1 - self.view0
        return self.view0 + (x - r.left()) / max(1.0, r.width()) * span

    # ------------------------------------------------------------------ editing
    def select_handle(self, which: str):
        if which != self.selected:
            self.selected = which
            self.handleSelected.emit(which)
            self.update()

    def nudge(self, dt: float, which: str | None = None):
        which = which or self.selected
        if not which or not self.has_audio:
            return
        self._begin_edit()
        if which == "start":
            self.start = max(0.0, min(self.start + dt, self.end - MIN_LEN_S))
        else:
            self.end = min(self.duration, max(self.end + dt, self.start + MIN_LEN_S))
        self.update()
        self.trimDragging.emit(self.start, self.end)
        self._commit.start()

    def _handle_at(self, px: float) -> str:
        ds = abs(px - self.x_for_time(self.start))
        de = abs(px - self.x_for_time(self.end))
        if min(ds, de) <= HANDLE_GRAB_PX:
            return "start" if ds <= de else "end"
        return ""

    def _move_handle(self, which, t):
        if which == "start":
            self.start = max(0.0, min(t, self.end - MIN_LEN_S))
        else:
            self.end = min(self.duration, max(t, self.start + MIN_LEN_S))

    def mousePressEvent(self, e):
        if not self.has_audio:
            return
        self.setFocus(Qt.MouseFocusReason)
        px = e.position().x()
        if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and e.modifiers() & Qt.AltModifier):
            self._pan_from = (px, self.view0, self.view1)
            self.setCursor(Qt.ClosedHandCursor)
            return
        if e.button() != Qt.LeftButton:
            return
        self._flush_commit()   # a pending nudge belongs to the edit before this one
        self._begin_edit()
        h = self._handle_at(px)
        if not h:
            # click elsewhere: move the nearest handle there
            t = self.time_for_x(px)
            h = "start" if abs(t - self.start) <= abs(t - self.end) else "end"
            self._move_handle(h, t)
            self.trimDragging.emit(self.start, self.end)
        self.select_handle(h)
        self._drag = h
        self.update()

    def mouseMoveEvent(self, e):
        px = e.position().x()
        if self._pan_from is not None:
            x0, v0, v1 = self._pan_from
            dt = (x0 - px) / max(1.0, self._wave_rect().width()) * (v1 - v0)
            span = v1 - v0
            nv0 = max(0.0, min(v0 + dt, self.duration - span))
            self.view0, self.view1 = nv0, nv0 + span
            self._peaks_key = None
            self.update()
            return
        if self._drag:
            self._move_handle(self._drag, self.time_for_x(px))
            self.trimDragging.emit(self.start, self.end)
            self.update()
            return
        self.setCursor(Qt.SizeHorCursor if self.has_audio and self._handle_at(px) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self._pan_from is not None:
            self._pan_from = None
            self.setCursor(Qt.ArrowCursor)
            return
        if self._drag:
            self._drag = None
            self._commit.stop()
            self._emit_commit()

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def wheelEvent(self, e):
        if not self.has_audio:
            return
        steps = e.angleDelta().y() / 120.0 or e.angleDelta().x() / 120.0
        if e.modifiers() & Qt.ShiftModifier:
            span = self.view1 - self.view0
            nv0 = max(0.0, min(self.view0 - steps * span * 0.15, self.duration - span))
            self.view0, self.view1 = nv0, nv0 + span
            self._peaks_key = None
            self.update()
        else:
            self.zoom(1.25 ** steps, self.time_for_x(e.position().x()))
        e.accept()

    def _is_nudge_key(self, e) -> bool:
        return e.key() in (Qt.Key_Left, Qt.Key_Right) and bool(self.selected) and self.has_audio

    def event(self, e):
        # claim arrow keys before page-level shortcuts while a handle is selected
        if e.type() == QEvent.ShortcutOverride and (self._is_nudge_key(e) or
                                                     (e.key() == Qt.Key_Escape and self.selected)):
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        k = e.key()
        if self._is_nudge_key(e):
            step = 0.001 if e.modifiers() & Qt.ShiftModifier else 0.010
            self.nudge(-step if k == Qt.Key_Left else step)
            return
        if k == Qt.Key_BracketLeft:
            self.select_handle("start")
            return
        if k == Qt.Key_BracketRight:
            self.select_handle("end")
            return
        if k == Qt.Key_Escape and self.selected:
            self.select_handle("")
            return
        if k in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom(1.5)
            return
        if k == Qt.Key_Minus:
            self.zoom(1 / 1.5)
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e):
        self._flush_commit()
        super().focusOutEvent(e)

    # ------------------------------------------------------------------ paint
    def _visible_peaks(self, n):
        key = (self.view0, self.view1, n, len(self.x), id(self.x))
        if key != self._peaks_key:
            i0 = max(0, int(self.view0 * self.sr))
            i1 = min(len(self.x), max(i0 + 1, int(math.ceil(self.view1 * self.sr))))
            try:
                mins, maxs = dsp.peaks_for_display(self.x[i0:i1], n)
            except Exception:  # noqa: BLE001 - never let painting crash
                mins = maxs = np.zeros(n, np.float32)
            self._peaks = (np.asarray(mins, np.float32), np.asarray(maxs, np.float32))
            self._peaks_key = key
        return self._peaks

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        full = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(theme.ACCENT if self.hasFocus() and self.has_audio else theme.BORDER), 1))
        p.setBrush(QColor("#121418"))
        p.drawRoundedRect(full, 6, 6)
        r = self._wave_rect()
        if not self.has_audio:
            p.setPen(QColor(theme.DIM))
            p.drawText(self.rect(), Qt.AlignCenter, self._placeholder)
            p.end()
            return
        mid = r.center().y()
        half = r.height() / 2 - 4
        # center line
        p.setPen(QColor(255, 255, 255, 25))
        p.drawLine(QPointF(r.left(), mid), QPointF(r.right(), mid))

        n = max(1, int(r.width()))
        mins, maxs = self._visible_peaks(n)
        xs0, xs1 = self.x_for_time(self.start), self.x_for_time(self.end)
        wave_in = QColor(theme.WAVE)
        wave_out = QColor("#3d5a73")
        npk = len(maxs)
        for i in range(npk):
            px = r.left() + i * r.width() / npk
            top = mid - float(maxs[i]) * half
            bot = mid - float(mins[i]) * half
            if bot - top < 1:
                bot = top + 1
            p.setPen(wave_in if xs0 <= px <= xs1 else wave_out)
            p.drawLine(QPointF(px, top), QPointF(px, bot))

        # shade outside trim
        shade = QColor(0, 0, 0, 120)
        if xs0 > r.left():
            p.fillRect(QRectF(r.left(), r.top(), xs0 - r.left(), r.height()), shade)
        if xs1 < r.right():
            p.fillRect(QRectF(xs1, r.top(), r.right() - xs1, r.height()), shade)

        p.setRenderHint(QPainter.Antialiasing, True)
        for which, hx in (("start", xs0), ("end", xs1)):
            if hx < r.left() - 10 or hx > r.right() + 10:
                continue
            sel = which == self.selected
            col = QColor(theme.ACCENT_HI if sel else theme.ACCENT)
            p.setPen(QPen(col, 3 if sel else 2))
            p.drawLine(QPointF(hx, r.top()), QPointF(hx, r.bottom()))
            # grab tab
            w = 11 if sel else 9
            d = -1 if which == "start" else 1
            tri = QPolygonF([QPointF(hx, r.top()), QPointF(hx + d * w, r.top()),
                             QPointF(hx + d * w, r.top() + 14), QPointF(hx, r.top() + 20)])
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawPolygon(tri)
            tri2 = QPolygonF([QPointF(hx, r.bottom()), QPointF(hx + d * w, r.bottom()),
                              QPointF(hx + d * w, r.bottom() - 14), QPointF(hx, r.bottom() - 20)])
            p.drawPolygon(tri2)

        if self.playhead is not None:
            phx = self.x_for_time(self.playhead)
            if r.left() <= phx <= r.right():
                p.setPen(QPen(QColor("white"), 1.5))
                p.drawLine(QPointF(phx, r.top()), QPointF(phx, r.bottom()))

        # ruler
        p.setRenderHint(QPainter.Antialiasing, False)
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        span = self.view1 - self.view0
        step = _nice_step(span / max(1.0, r.width() / 70))
        t = math.ceil(self.view0 / step) * step
        ry = r.bottom() + 1
        p.setPen(QColor(theme.DIM))
        while t <= self.view1 + 1e-9:
            tx = self.x_for_time(t)
            p.drawLine(QPointF(tx, ry), QPointF(tx, ry + 4))
            label = f"{t:.2f}s" if step < 0.1 else f"{t:.1f}s"
            p.drawText(QRectF(tx + 3, ry, 60, RULER_H - 1), Qt.AlignLeft | Qt.AlignVCenter, label)
            t += step
        # trimmed length tag
        tag = f"{self.end - self.start:.2f} s kept"
        tw = p.fontMetrics().horizontalAdvance(tag) + 12
        tr = QRectF(r.right() - tw - 2, ry + 1, tw, RULER_H - 3)
        p.fillRect(tr, QColor("#121418"))
        p.setPen(QColor(theme.ACCENT))
        p.drawText(tr, Qt.AlignCenter, tag)
        p.end()


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag
