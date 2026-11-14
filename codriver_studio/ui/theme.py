"""Dark Fusion theme with a rally-orange accent."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

BG = "#17191d"
PANEL = "#1f2227"
RAISED = "#282c33"
RAISED_HI = "#323741"
BORDER = "#3a4049"
TEXT = "#e8eaed"
MUTED = "#9aa3ae"
DIM = "#6b737e"
ACCENT = "#ff7a1a"
ACCENT_HI = "#ff9442"
ACCENT_LO = "#b85410"
GREEN = "#4cc26a"
AMBER = "#f0b429"
RED = "#e5484d"
WAVE = "#5fb3ff"

STATUS_COLORS = {"missing": DIM, "recorded": GREEN, "disabled": AMBER}
STATUS_LABELS = {"missing": "Missing", "recorded": "Recorded", "disabled": "Disabled"}

QSS = f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {RAISED_HI}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}

#NavList {{ background: {PANEL}; border: none; border-right: 1px solid {BORDER}; outline: 0;
            padding-top: 8px; font-size: 11pt; }}
#NavList::item {{ padding: 11px 14px 11px 16px; border-left: 3px solid transparent; color: {MUTED}; }}
#NavList::item:hover {{ background: {RAISED}; color: {TEXT}; }}
#NavList::item:selected {{ background: {RAISED}; color: {TEXT}; border-left: 3px solid {ACCENT}; }}

#TopBar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}
#ProjectTitle {{ font-size: 13pt; font-weight: 600; }}
#Brand {{ color: {ACCENT}; font-weight: 700; font-size: 10pt; letter-spacing: 1px; }}
#PageTitle {{ font-size: 17pt; font-weight: 600; }}
#PageSubtitle, .muted, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel[hint="true"] {{ color: {MUTED}; font-size: 9pt; }}
QLabel[warn="true"] {{ color: {AMBER}; }}
QLabel[error="true"] {{ color: {RED}; font-weight: 600; }}

QPushButton, QToolButton {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 5px;
    padding: 6px 12px; min-height: 18px; }}
QPushButton:hover, QToolButton:hover {{ background: {RAISED_HI}; border-color: #4a515c; }}
QPushButton:pressed, QToolButton:pressed {{ background: {BORDER}; }}
QPushButton:checked, QToolButton:checked {{ background: {ACCENT_LO}; border-color: {ACCENT}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {DIM}; background: {PANEL}; border-color: {RAISED_HI}; }}
QPushButton[primary="true"] {{ background: {ACCENT}; border-color: {ACCENT}; color: #1a1006; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {ACCENT_HI}; }}
QPushButton[primary="true"]:disabled {{ background: {RAISED}; color: {DIM}; border-color: {BORDER}; }}
QPushButton[flat="true"] {{ background: transparent; border-color: transparent; }}
QPushButton[flat="true"]:hover {{ background: {RAISED}; }}
QPushButton[chip="true"] {{ border-radius: 12px; padding: 3px 10px; min-height: 16px; }}
QPushButton[chip="true"][missing="true"] {{ color: {MUTED}; border-style: dashed; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 5px; padding: 5px 7px;
    selection-background-color: {ACCENT_LO}; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox:disabled, QLineEdit:disabled {{ color: {DIM}; }}
QAbstractSpinBox {{ padding-right: 20px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ subcontrol-origin: border; width: 18px;
    background: {RAISED}; border-left: 1px solid {BORDER}; }}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 5px; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 5px; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {RAISED_HI}; }}
QAbstractSpinBox::up-arrow {{ image: url(cdsimg:up.png); width: 9px; height: 9px; }}
QAbstractSpinBox::down-arrow {{ image: url(cdsimg:down.png); width: 9px; height: 9px; }}
QComboBox QAbstractItemView {{ background: {RAISED}; border: 1px solid {BORDER}; selection-background-color: {ACCENT_LO}; }}

QTreeView, QListWidget, QTableView {{ background: {PANEL}; alternate-background-color: #22262c;
    border: 1px solid {BORDER}; border-radius: 6px; outline: 0; }}
QTreeView::item, QListWidget::item {{ padding: 4px 2px; }}
QTreeView::item:selected, QListWidget::item:selected {{ background: {ACCENT_LO}; color: white; }}
QTreeView::item:hover:!selected, QListWidget::item:hover:!selected {{ background: {RAISED}; }}
QHeaderView::section {{ background: {RAISED}; color: {MUTED}; border: none; border-right: 1px solid {BORDER};
    padding: 6px 8px; font-weight: 600; }}

QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding: 12px 10px 10px 10px;
    background: {PANEL}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {ACCENT}; font-weight: 600; }}

QProgressBar {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 6px; text-align: center;
    min-height: 18px; color: {TEXT}; font-weight: 600; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::sub-page:horizontal:disabled {{ background: {DIM}; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover {{ background: white; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QListWidget::indicator {{ width: 16px; height: 16px; border: 1px solid #59616c;
    border-radius: 3px; background: {PANEL}; }}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT};
    image: url(:/cds/check.png); }}
QCheckBox:disabled {{ color: {DIM}; }}

QSplitter::handle {{ background: {BG}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 30px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QMenuBar {{ background: {PANEL}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {ACCENT_LO}; }}
QMenu {{ background: {RAISED}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 5px 20px; }}

#Card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
#BigLine {{ font-weight: 600; }}   /* size is fitted to the space at run time */
#Countdown {{ font-size: 72pt; font-weight: 700; color: {ACCENT}; }}
"""


def _check_pixmap() -> QPixmap:
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#1a1006"), 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([QPointF_(7, 17), QPointF_(13, 23), QPointF_(25, 9)])
    p.end()
    return pm


def QPointF_(x, y):
    from PySide6.QtCore import QPointF
    return QPointF(x, y)


_RESOURCE_DIR = None


def _register_check_image() -> None:
    """QSS needs a url for the check mark; write it to a temp file and alias it via search path."""
    global _RESOURCE_DIR
    import tempfile
    from PySide6.QtCore import QDir
    d = tempfile.mkdtemp(prefix="cds_ui_")
    _check_pixmap().save(f"{d}/check.png")
    for name, pts in (("up", [(4, 20), (14, 9), (24, 20)]), ("down", [(4, 9), (14, 20), (24, 9)])):
        pm = QPixmap(28, 28)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(MUTED), 3.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline([QPointF_(x, y) for x, y in pts])
        p.end()
        pm.save(f"{d}/{name}.png")
    QDir.addSearchPath("cdsimg", d)
    _RESOURCE_DIR = d


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    c = QColor
    pal.setColor(QPalette.Window, c(BG))
    pal.setColor(QPalette.WindowText, c(TEXT))
    pal.setColor(QPalette.Base, c(PANEL))
    pal.setColor(QPalette.AlternateBase, c("#22262c"))
    pal.setColor(QPalette.ToolTipBase, c(RAISED_HI))
    pal.setColor(QPalette.ToolTipText, c(TEXT))
    pal.setColor(QPalette.Text, c(TEXT))
    pal.setColor(QPalette.Button, c(RAISED))
    pal.setColor(QPalette.ButtonText, c(TEXT))
    pal.setColor(QPalette.BrightText, c(RED))
    pal.setColor(QPalette.Highlight, c(ACCENT_LO))
    pal.setColor(QPalette.HighlightedText, c("white"))
    pal.setColor(QPalette.Link, c(ACCENT))
    pal.setColor(QPalette.PlaceholderText, c(DIM))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, c(DIM))
    app.setPalette(pal)
    f = QFont("Segoe UI", 10)
    app.setFont(f)
    _register_check_image()
    app.setStyleSheet(QSS.replace("url(:/cds/check.png)", "url(cdsimg:check.png)"))


def status_icon(status: str, size: int = 14) -> QIcon:
    """Small painted icon: green check (recorded), amber pause bars (disabled), grey ring (missing)."""
    key = (status, size)
    cache = status_icon.__dict__.setdefault("_cache", {})
    if key in cache:
        return cache[key]
    scale = 2
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size * scale
    col = QColor(STATUS_COLORS.get(status, DIM))
    if status == "recorded":
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(1, 1, s - 2, s - 2)
        p.setPen(QPen(QColor(BG), s * 0.13, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline([QPointF_(s * 0.28, s * 0.52), QPointF_(s * 0.44, s * 0.68), QPointF_(s * 0.72, s * 0.34)])
    elif status == "disabled":
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(1, 1, s - 2, s - 2)
        p.setBrush(QColor(BG))
        p.drawRect(int(s * 0.3), int(s * 0.46), int(s * 0.4), int(s * 0.1))
    else:
        p.setPen(QPen(col, s * 0.1))
        p.setBrush(Qt.NoBrush)
        m = s * 0.12
        p.drawEllipse(QPointF_(s / 2, s / 2), s / 2 - m, s / 2 - m)
    p.end()
    pm.setDevicePixelRatio(scale)
    icon = QIcon(pm)
    cache[key] = icon
    return icon


def app_icon() -> QIcon:
    """Painted app icon: orange rounded square with a white chevron (pacenote arrow)."""
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    p.drawRoundedRect(8, 8, 240, 240, 48, 48)
    p.setPen(QPen(QColor("white"), 30, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline([QPointF_(80, 190), QPointF_(80, 110), QPointF_(150, 110)])
    p.drawPolyline([QPointF_(125, 70), QPointF_(170, 110), QPointF_(125, 150)])
    p.end()
    return QIcon(pm)
