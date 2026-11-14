"""Small reusable widgets: level meter, flow layout, page header, helpers."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import theme

FLOOR_DB = -60.0


class LevelMeter(QWidget):
    """Horizontal peak meter (-60..0 dBFS) with peak hold and clip indicator."""

    def __init__(self, parent=None, compact=False):
        super().__init__(parent)
        self.compact = compact
        self._db = -120.0
        self._hold = -120.0
        self._hold_t = 0.0
        self._clip_t = 0.0
        self.setMinimumHeight(10 if compact else 26)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip("Microphone level. Aim for the green/yellow zone; red = too loud (clipping).")
        self._decay = QTimer(self)
        self._decay.setInterval(50)
        self._decay.timeout.connect(self.update)
        self._decay.start()

    def sizeHint(self):
        return QSize(160, 12 if self.compact else 30)

    def set_level(self, db: float):
        now = time.monotonic()
        db = max(-120.0, float(db))
        # fast attack, smooth release
        self._db = db if db > self._db else max(db, self._db - 1.5)
        if db >= self._hold or now - self._hold_t > 1.2:
            self._hold, self._hold_t = db, now
        if db > -0.3:
            self._clip_t = now

    def mark_clip(self):
        self._clip_t = time.monotonic()

    @property
    def clipping(self) -> bool:
        return time.monotonic() - self._clip_t < 1.5

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)
        clip_w = 0 if self.compact else 30
        bar = QRect(r.left(), r.top(), r.width() - clip_w - (6 if clip_w else 0), r.height())
        p.setPen(QColor(theme.BORDER))
        p.setBrush(QColor(theme.PANEL))
        p.drawRoundedRect(bar, 4, 4)

        def x_of(db):
            f = (max(FLOOR_DB, min(0.0, db)) - FLOOR_DB) / -FLOOR_DB
            return bar.left() + 1 + f * (bar.width() - 2)

        g = QLinearGradient(bar.left(), 0, bar.right(), 0)
        g.setColorAt(0.0, QColor(theme.GREEN))
        g.setColorAt(0.70, QColor(theme.GREEN))
        g.setColorAt(0.86, QColor(theme.AMBER))
        g.setColorAt(0.97, QColor(theme.RED))
        inner = bar.adjusted(1, 1, -1, -1)
        w = x_of(self._db) - inner.left()
        if w > 0:
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawRoundedRect(QRect(inner.left(), inner.top(), int(w), inner.height()), 3, 3)
        if self._hold > FLOOR_DB:
            hx = int(x_of(self._hold))
            p.setPen(QColor(theme.TEXT))
            p.drawLine(hx, inner.top(), hx, inner.bottom())
        if not self.compact:
            # tick marks
            p.setPen(QColor(0, 0, 0, 90))
            for db in (-48, -36, -24, -12, -6):
                tx = int(x_of(db))
                p.drawLine(tx, inner.top() + 2, tx, inner.bottom() - 2)
            cr = QRect(bar.right() + 6, r.top(), clip_w, r.height())
            on = self.clipping
            p.setPen(QColor(theme.RED if on else theme.BORDER))
            p.setBrush(QColor(theme.RED if on else theme.PANEL))
            p.drawRoundedRect(cr, 4, 4)
            p.setPen(QColor("white" if on else theme.DIM))
            f = p.font()
            f.setPointSizeF(7.5)
            f.setBold(True)
            p.setFont(f)
            p.drawText(cr, Qt.AlignCenter, "CLIP")
        p.end()


class FlowLayout(QLayout):
    """Wrapping layout (chips, tool-button groups): items flow left to right and wrap onto the
    next row when the width runs out. Height-for-width aware, so parents give it enough rows."""

    def __init__(self, parent=None, spacing=6, vspacing=None):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self._vsp = spacing if vspacing is None else vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        m = self.contentsMargins()
        return self._do(QRect(0, 0, w, 0), True) + m.top() + m.bottom()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        m = self.contentsMargins()
        self._do(rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom()), False)

    def _visible(self):
        return [it for it in self._items if not (it.widget() is not None and it.widget().isHidden())]

    def sizeHint(self):
        """One row with everything."""
        vis = self._visible()
        m = self.contentsMargins()
        if not vis:
            return QSize(0, 0)
        w = sum(it.sizeHint().width() for it in vis) + self._sp * (len(vis) - 1)
        h = max(it.sizeHint().height() for it in vis)
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self):
        """As narrow as the widest item (everything wrapped)."""
        s = QSize()
        for it in self._visible():
            s = s.expandedTo(it.minimumSize()).expandedTo(QSize(it.sizeHint().width(), 0))
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect, test):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._visible():
            hint = it.sizeHint()
            nx = x + hint.width() + self._sp
            if nx - self._sp > rect.right() + 1 and line_h > 0:
                x, y = rect.x(), y + line_h + self._vsp
                nx = x + hint.width() + self._sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()

    def clear(self):
        while self._items:
            it = self._items.pop()
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()


class PageScrollArea(QScrollArea):
    """Scroll area around a whole page: the page fills the viewport, and only when the viewport is
    smaller than the page's *minimum* size does it scroll (instead of overlapping widgets).

    QScrollArea's own widgetResizable mode sizes height-for-width pages (anything with word-wrapped
    labels or flow layouts) to their *preferred* height and so scrolls much too early."""

    def __init__(self, page: QWidget, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWidget(page)
        page.installEventFilter(self)

    def page(self) -> QWidget:
        return self.widget()

    def _min_h(self, w: int) -> int:
        page = self.widget()
        lay = page.layout()
        if lay is None:
            return page.minimumSizeHint().height()
        if not lay.hasHeightForWidth():
            return max(lay.totalMinimumSize().height(), page.minimumHeight())
        if isinstance(lay, QVBoxLayout):
            # QBoxLayout.setGeometry() never squeezes a height-for-width item below its
            # heightForWidth(), but minimumHeightForWidth() assumes it can: sum it up the way
            # setGeometry() will actually lay the page out
            m = lay.contentsMargins()
            iw = w - m.left() - m.right()
            total, n = m.top() + m.bottom(), 0
            for i in range(lay.count()):
                it = lay.itemAt(i)
                if it.spacerItem() is not None:
                    total += it.minimumSize().height()
                    continue
                if it.isEmpty():
                    continue
                total += it.heightForWidth(iw) if it.hasHeightForWidth() else it.minimumSize().height()
                n += 1
            return max(total + max(0, n - 1) * max(0, lay.spacing()), lay.minimumHeightForWidth(w),
                       page.minimumHeight())
        return max(lay.minimumHeightForWidth(w), page.minimumHeight())

    def fit_page(self):
        page = self.widget()
        if page is None:
            return
        vp = self.maximumViewportSize()          # viewport size without scroll bars
        lay = page.layout()
        min_w = max(page.minimumWidth(), lay.totalMinimumSize().width() if lay is not None else 0)
        avail_w, avail_h = vp.width(), vp.height()
        sb_w = self.verticalScrollBar().sizeHint().width()
        sb_h = self.horizontalScrollBar().sizeHint().height()
        need_h = avail_w < min_w
        if need_h:
            avail_h -= sb_h
        w = max(avail_w, min_w)
        if self._min_h(w) > avail_h:             # needs a vertical bar: it takes some width
            avail_w -= sb_w
            w = max(avail_w, min_w)
            if not need_h and avail_w < min_w:
                avail_h -= sb_h
        h = max(avail_h, self._min_h(w))
        if page.size() != QSize(w, h):
            page.resize(w, h)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit_page()

    def eventFilter(self, obj, e):
        if obj is self.widget() and e.type() == QEvent.LayoutRequest:
            QTimer.singleShot(0, self.fit_page)
        return super().eventFilter(obj, e)

    def showEvent(self, e):
        super().showEvent(e)
        self.fit_page()


def wrappable_path(path) -> str:
    """The path with zero-width spaces after its separators, so word-wrapping labels / message
    boxes break a long path at a folder boundary instead of widening the window or breaking "C:"."""
    return str(path).replace("\\", "\\​").replace("/", "/​")


class ElidedLabel(QLabel):
    """Single-line plain-text label that elides (…) instead of forcing its full width on the layout.

    ``text()`` still returns the full text; the full text becomes the tooltip while elided
    (unless a tooltip was set explicitly)."""

    def __init__(self, text: str = "", parent=None, mode=Qt.ElideRight, min_chars: int = 6):
        super().__init__(parent)
        self._mode = mode
        self._min_chars = min_chars
        self._own_tip = False
        self.setProperty("elide", True)          # tells the layout guard test this is deliberate
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setText(text)

    def setToolTip(self, tip):
        self._own_tip = bool(tip)
        super().setToolTip(tip)

    def setText(self, text):
        super().setText(text)
        self._update_tip()

    def minimumSizeHint(self):
        fm = self.fontMetrics()
        t = self.text()
        w = fm.horizontalAdvance(t[: self._min_chars] + "…") if len(t) > self._min_chars else fm.horizontalAdvance(t)
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), super().minimumSizeHint().height())

    def _elided(self) -> str:
        return self.fontMetrics().elidedText(self.text(), self._mode, max(0, self.contentsRect().width()))

    def _update_tip(self):
        if self._own_tip:
            return
        QLabel.setToolTip(self, self.text() if self._elided() != self.text() else "")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_tip()

    def paintEvent(self, e):
        QFrame.paintEvent(self, e)
        p = QPainter(self)
        self.style().drawItemText(p, self.contentsRect(), int(self.alignment()), self.palette(), self.isEnabled(),
                                  self._elided(), self.foregroundRole())
        p.end()


class FitTextLabel(QWidget):
    """Big word-wrapped text that picks the largest point size (``max_pt`` … ``min_pt``) that fits
    its rectangle; if even ``min_pt`` doesn't fit, the last visible line is elided."""

    def __init__(self, parent=None, max_pt: float = 40.0, min_pt: float = 15.0, weight=QFont.DemiBold):
        super().__init__(parent)
        self._text = ""
        self.max_pt, self.min_pt = max_pt, min_pt
        self._weight = weight
        self._fit_cache = None
        self.setProperty("elide", True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def text(self) -> str:
        return self._text

    def setText(self, text: str):
        self._text = str(text or "")
        self._fit_cache = None
        self.setToolTip(self._text)
        self.update()

    def _font(self, pt: float) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(pt)
        f.setWeight(self._weight)
        return f

    def _text_height(self, pt: float, width: int) -> int:
        fm = QFontMetrics(self._font(pt))
        return fm.boundingRect(QRect(0, 0, max(1, width), 100000), int(Qt.TextWordWrap), self._text).height()

    def fitted_point_size(self) -> float:
        r = self.contentsRect()
        key = (r.width(), r.height(), self._text, self.font().key())
        if self._fit_cache and self._fit_cache[0] == key:
            return self._fit_cache[1]
        pt = self.min_pt
        hi, lo = self.max_pt, self.min_pt
        if self._text and self._text_height(hi, r.width()) <= r.height():
            pt = hi
        elif self._text:
            while hi - lo > 0.5:                   # binary search on the point size
                mid = (hi + lo) / 2
                if self._text_height(mid, r.width()) <= r.height():
                    lo = mid
                else:
                    hi = mid
            pt = lo
        self._fit_cache = (key, pt)
        return pt

    def minimumSizeHint(self):
        fm = QFontMetrics(self._font(self.min_pt))
        return QSize(fm.horizontalAdvance("M" * 8), fm.lineSpacing() + 4)

    def sizeHint(self):
        fm = QFontMetrics(self._font(self.max_pt))
        return QSize(fm.horizontalAdvance("M" * 16), fm.lineSpacing() * 2)

    def paintEvent(self, _e):
        from PySide6.QtGui import QTextLayout
        r = self.contentsRect()
        pt = self.fitted_point_size()
        font = self._font(pt)
        fm = QFontMetrics(font)
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(font)
        p.setPen(self.palette().color(self.foregroundRole()))
        need = self._text_height(pt, r.width())
        if need <= r.height():
            p.drawText(r, int(Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap), self._text)
            p.end()
            return
        # doesn't fit even at min_pt: draw the lines that fit, elide the last one
        layout = QTextLayout(self._text, font)
        layout.beginLayout()
        lines = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(r.width())
            lines.append((line.textStart(), line.textLength()))
        layout.endLayout()
        n = max(1, r.height() // fm.lineSpacing())
        y = r.top() + max(0, (r.height() - n * fm.lineSpacing()) // 2) + fm.ascent()
        for i, (s, ln) in enumerate(lines[:n]):
            t = self._text[s:s + ln].rstrip()
            if i == n - 1 and len(lines) > n:
                t = fm.elidedText(self._text[s:].strip(), Qt.ElideRight, r.width())
            p.drawText(r.left(), y, t)
            y += fm.lineSpacing()
        p.end()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update()


def page_header(title: str, subtitle: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 6)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("PageSubtitle")
        s.setWordWrap(True)
        lay.addWidget(s)
    return w


def hint_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("hint", True)
    lab.setWordWrap(True)
    return lab


def primary_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("primary", True)
    return b


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color: {theme.BORDER}; background: {theme.BORDER}; max-height: 1px;")
    return f


def restyle(w: QWidget):
    """Re-apply QSS after a dynamic property change."""
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def fmt_secs(s: float) -> str:
    return f"{s:.2f} s"


def hbox(*widgets, spacing=8, margins=(0, 0, 0, 0), stretch_at=None) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for i, w in enumerate(widgets):
        if stretch_at == i:
            lay.addStretch(1)
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, QLayout):
            lay.addLayout(w)
        else:
            lay.addWidget(w)
    return lay
