"""Layout guard: every page and dialog at small *logical* screens must not squish, overlap or clip.

The user's 1920×1080 display at 150 % Windows scaling leaves only ~1280×690 logical pixels for a
maximized window, so this renders every page with QT_SCALE_FACTOR=1.5 (and other common setups)
and walks all visible widgets:

  (a) squished   - a widget smaller than the minimum its layout should guarantee
                   (minimumSizeHint / explicit minimum / heightForWidth of wrapped text),
  (b) overlap    - two visible sibling widgets managed by the same layout intersect,
  (c) clipped    - a non-wrapping, non-eliding label whose text is wider/taller than the label.

Each configuration runs in its own process (QT_SCALE_FACTOR is read when the QApplication is
created). By hand:  python tests/test_ui_layout.py SCALE WIDTH HEIGHT [--shots DIR] [--json FILE]
(WIDTH/HEIGHT 0 = the window's minimum size).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent

# (scale, logical width, logical height); 0×0 = the main window's minimum size
CONFIGS = [
    (1.5, 1280, 690),     # 1920×1080 @ 150 %, maximized (the user's setup)
    (1.0, 1280, 690),
    (1.25, 1280, 672),    # 1600×900-ish / 1080p @ 125 %
    (1.0, 1366, 728),     # 1366×768 laptop @ 100 %
    (1.5, 0, 0),          # the minimum window size
    (1.0, 1920, 1040),    # big window: nothing regressed
]


def _cfg_id(c):
    s, w, h = c
    return f"{int(s * 100)}pct-{'min' if not w else f'{w}x{h}'}"


@pytest.mark.parametrize("cfg", CONFIGS, ids=[_cfg_id(c) for c in CONFIGS])
def test_layout_no_squish_overlap_or_clipping(cfg, tmp_path):
    scale, w, h = cfg
    out = tmp_path / "problems.json"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=str(scale),
               CDS_SETTINGS_DIR=str(tmp_path / "settings"))
    env.pop("QT_SCREEN_SCALE_FACTORS", None)
    r = subprocess.run([sys.executable, str(pathlib.Path(__file__)), str(scale), str(w), str(h),
                        "--json", str(out), "--work", str(tmp_path / "work")],
                       cwd=str(HERE.parent), env=env, capture_output=True, text=True, timeout=240)
    assert r.returncode == 0 and out.exists(), (r.stdout[-3000:], r.stderr[-3000:])
    problems = json.loads(out.read_text("utf-8"))
    assert problems == {}, "\n".join(f"[{page}] {p}" for page, ps in problems.items() for p in ps)


# ====================================================================== checker (in the child process)
def _is_elided(w) -> bool:
    return bool(w.property("elide"))


def _layout_widgets(lay):
    """All widgets placed (directly or through nested layouts) by ``lay``."""
    out = []
    for i in range(lay.count()):
        it = lay.itemAt(i)
        if it.widget() is not None:
            out.append(it.widget())
        elif it.layout() is not None:
            out.extend(_layout_widgets(it.layout()))
    return out


def _name(w) -> str:
    from PySide6.QtWidgets import QAbstractButton, QLabel
    bits = [type(w).__name__]
    if w.objectName() and not w.objectName().startswith("qt_"):
        bits.append(f"#{w.objectName()}")
    txt = ""
    if isinstance(w, (QLabel, QAbstractButton)):
        txt = w.text()
    elif hasattr(w, "title") and callable(w.title):
        try:
            txt = w.title()
        except TypeError:
            txt = ""
    if txt:
        import re
        txt = re.sub(r"<[^>]+>", "", str(txt)).replace("\n", " ")
        bits.append(repr(txt[:40]))
    p = w.parentWidget()
    chain = []
    while p is not None and len(chain) < 3:
        if p.objectName() and not p.objectName().startswith("qt_"):
            chain.append(p.objectName())
        else:
            chain.append(type(p).__name__)
        p = p.parentWidget()
    return " ".join(bits) + f" in {'/'.join(reversed(chain))}"


def _effective_min(w):
    """Minimum size the parent layout should give ``w`` (Qt's qSmartMinSize)."""
    from PySide6.QtWidgets import QSizePolicy
    pol = w.sizePolicy()
    msh, sh = w.minimumSizeHint(), w.sizeHint()
    mw, mh = w.minimumWidth(), w.minimumHeight()
    if mw <= 0:
        if pol.horizontalPolicy() == QSizePolicy.Ignored:
            mw = 0
        elif pol.horizontalPolicy().value & QSizePolicy.ShrinkFlag.value:
            mw = msh.width()
        else:
            mw = max(sh.width(), msh.width())
    if mh <= 0:
        if pol.verticalPolicy() == QSizePolicy.Ignored:
            mh = 0
        elif pol.verticalPolicy().value & QSizePolicy.ShrinkFlag.value:
            mh = msh.height()
        else:
            mh = max(sh.height(), msh.height())
    return max(0, mw), max(0, mh)


def check(root) -> list[str]:
    """Problems of all visible widgets below ``root`` (a top-level window)."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import (QAbstractScrollArea, QLabel, QScrollBar, QSplitterHandle, QWidget)
    problems: list[str] = []
    widgets = [w for w in root.findChildren(QWidget) if w.isVisible() and w.window() is root]
    for w in widgets:
        if w.objectName().startswith("qt_") or isinstance(w, (QScrollBar, QSplitterHandle)):
            continue
        par = w.parentWidget()
        # scroll-area internals (viewport) are sized by the scroll area, not by a layout
        if isinstance(par, QAbstractScrollArea) and w is not getattr(par, "widget", lambda: None)():
            continue
        # (a) squished below the guaranteed minimum
        managed = par is not None and par.layout() is not None and w in _layout_widgets(par.layout())
        if managed:
            mw, mh = _effective_min(w)
            if w.hasHeightForWidth() and w.width() > 0:
                lay = w.layout()
                # a container needs its layout's *minimum* height for this width; a leaf
                # (word-wrapped label) needs its full height-for-width or text is cut off
                need = lay.minimumHeightForWidth(w.width()) if lay is not None else w.heightForWidth(w.width())
                mh = max(mh, need)
            if w.height() < mh - 1:
                problems.append(f"squished vertically: {_name(w)} is {w.height()}px, needs {mh}px")
            if w.width() < mw - 1:
                problems.append(f"squished horizontally: {_name(w)} is {w.width()}px, needs {mw}px")
        # (c) label text clipped
        if isinstance(w, QLabel) and w.text() and not _is_elided(w) and w.pixmap().isNull():
            cr = w.contentsRect()
            if w.wordWrap():
                need_h = w.heightForWidth(w.width()) if w.hasHeightForWidth() else w.sizeHint().height()
                if need_h > w.height() + 1:
                    problems.append(f"wrapped text clipped: {_name(w)} {w.height()}px tall, needs {need_h}px")
            else:
                fmt = w.textFormat()
                if fmt == Qt.RichText or (fmt == Qt.AutoText and "<" in w.text()):
                    # rich text: lay it out like QLabel does
                    doc = QTextDocument()
                    doc.setDefaultFont(w.font())
                    doc.setDocumentMargin(0)
                    doc.setHtml(w.text())
                    need_w = doc.idealWidth()
                    need_h = doc.size().height()
                else:
                    fm = w.fontMetrics()
                    lines = w.text().split("\n")
                    need_w = max(fm.horizontalAdvance(t) for t in lines)
                    need_h = fm.height() * len(lines)
                if need_w > cr.width() + 2:
                    problems.append(f"text clipped horizontally: {_name(w)} {cr.width()}px wide, "
                                    f"text {int(need_w)}px")
                if need_h > cr.height() + 2:
                    problems.append(f"text clipped vertically: {_name(w)} {cr.height()}px tall, "
                                    f"text {int(need_h)}px")
    # content that only fits by scrolling sideways (a too-wide row / unbreakable label)
    from PySide6.QtWidgets import QListView, QScrollArea
    for w in widgets:
        if isinstance(w, (QScrollArea, QListView)) and w.horizontalScrollBar().isVisible():
            problems.append(f"scrolls sideways: {_name(w)} ({w.viewport().width()}px wide)")
    # (b) overlapping siblings of one layout
    seen = set()
    for w in [root] + widgets:
        lay = w.layout()
        if lay is None or id(lay) in seen:
            continue
        seen.add(id(lay))
        kids = [k for k in _layout_widgets(lay) if k.isVisible()]
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                inter = QRect(a.geometry()).intersected(b.geometry())
                if inter.width() > 1 and inter.height() > 1:
                    problems.append(f"overlap: {_name(a)} {a.geometry().getRect()} and "
                                    f"{_name(b)} {b.geometry().getRect()}")
    # the window itself must fit its content
    if root.layout() is not None:
        need = root.layout().totalMinimumSize()
        if root.width() < need.width() - 1 or root.height() < need.height() - 1:
            problems.append(f"window {root.width()}x{root.height()} smaller than its layout minimum "
                            f"{need.width()}x{need.height()}")
    return problems


# ====================================================================== scenario (child process)
def _main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("scale", type=float)
    ap.add_argument("width", type=int)
    ap.add_argument("height", type=int)
    ap.add_argument("--shots")
    ap.add_argument("--json")
    ap.add_argument("--work")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.environ["QT_SCALE_FACTOR"] = str(a.scale)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(HERE))
    import tempfile
    import test_ui_smoke as smoke        # installs fakes / fonts / temp settings dir
    from PySide6.QtWidgets import QMessageBox
    from codriver_studio import project as project_mod
    from codriver_studio.ui import workers
    from codriver_studio.ui.app import make_app
    from codriver_studio.ui.audio_settings import AudioSettingsDialog
    from codriver_studio.ui.main_window import MainWindow
    from codriver_studio.ui.welcome import NewProjectDialog

    app = make_app(["layout"])
    for n in ("warning", "critical", "information"):
        setattr(QMessageBox, n, staticmethod(lambda *x, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *x, **k: QMessageBox.Yes)
    pump = smoke.pump
    work = pathlib.Path(a.work or tempfile.mkdtemp(prefix="cds_layout_"))
    work.mkdir(parents=True, exist_ok=True)
    tag = _cfg_id((a.scale, a.width, a.height))
    problems: dict[str, list[str]] = {}

    win = MainWindow()

    def size_win():
        if a.width and a.height:
            win.resize(a.width, a.height)
        else:
            win.resize(win.minimumSize())
        pump(60)

    win.show()
    size_win()

    def state(name, widget=None):
        widget = widget or win
        pump(150)
        ps = check(widget)
        # at the user's size (1080p @ 150 %, maximized) and above, pages fit without scrolling
        ws_ = win.workspace
        if widget is win and ws_ is not None and a.width >= 1280 and a.height >= 670:
            sa = ws_.stack.currentWidget()
            for bar, what in ((getattr(sa, "verticalScrollBar", None), "vertically"),
                              (getattr(sa, "horizontalScrollBar", None), "horizontally")):
                if bar is not None and bar().isVisible():
                    ps.append(f"page has to scroll {what} at {a.width}x{a.height}")
        if ps:
            problems[name] = ps
        if a.shots:
            os.makedirs(a.shots, exist_ok=True)
            widget.grab().save(os.path.join(a.shots, f"{tag}_{name}.png"))

    state("welcome")
    dlg = NewProjectDialog(win)
    dlg.show()
    state("new_project_dialog", dlg)
    dlg.reject()

    proj = win.create_project(work / "proj", "Layout Tester With A Fairly Long Name")
    size_win()
    ws, ctl = win.workspace, win.ctl
    cat = proj.catalog
    pids = [p for p in ["three_left", "into", "hard_right", "dont_cut", "150", "100", "long", "caution"]
            if p in {ph.id for ph in cat.phrases}]
    for k, pid in enumerate(pids):
        proj.add_take(pid, smoke.speechish(words=2, seed=k), 48000)
        if k < 2:
            proj.add_take(pid, smoke.speechish(words=2, seed=k + 50), 48000)
            proj.add_take(pid, smoke.speechish(words=2, seed=k + 70), 48000)
        ctl.notify(pid)

    ws.show_page(0)
    ws.phrases.select_pid(pids[0])
    state("phrases")

    rec = ws.record
    for cb in (rec.cb_autoplay, rec.cb_count, rec.cb_hands):
        cb.setChecked(False)
    rec.cb_missing.setChecked(False)
    ws.show_page(1)
    missing = next(p.id for p in cat.phrases if not proj.takes(p.id) and not p.is_system)
    rec.goto(missing)
    state("record_idle")
    rec.goto(pids[0])
    ctl.audio.take_ready.emit(smoke.speechish(seed=3), 48000, False)
    state("record_review")
    long_sys = max((p for p in cat.phrases if p.is_system), key=lambda p: len(proj.spoken_text(p.id)))
    rec.goto(long_sys.id)
    ctl.audio.take_ready.emit(smoke.speechish(seed=4), 48000, False)
    state("record_long_line")
    ctl.audio.stop_playback()

    ws.show_page(2)
    state("effects")

    ws.show_page(3)
    sc = ws.samples
    sc.edit.setPlainText("150 three left into hard right don't cut blorp caution long tightens")
    sc.reparse()
    state("sample_calls")

    ws.show_page(4)
    ex = ws.export
    ex.interactive = False
    ex.export_to(work / "out" / "pack.zip")
    smoke.wait_until(lambda: ex.last_report is not None or ex.last_error is not None, 120)
    state("export_result")

    dlg = AudioSettingsDialog(win.audio, win)
    dlg.show()
    state("audio_settings_dialog", dlg)
    dlg.done(0)

    box = QMessageBox(QMessageBox.Question, "Project already open",
                      f"{proj.folder}\n\nis already open in another Co-Driver Studio window (PID 12345).\n\n"
                      "Two windows editing the same project could overwrite each other's work. Open it "
                      "read-only here (browse, listen, export; no recording or editing)?",
                      QMessageBox.Yes | QMessageBox.Cancel, win)
    box.show()
    state("lock_dialog", box)
    box.done(0)

    folder = proj.folder
    workers.wait_idle()
    win.close_project()
    size_win()
    state("welcome_recent")

    ro = project_mod.Project.open(folder, read_only=True)
    win._set_project(ro)
    size_win()
    win.workspace.show_page(1)
    state("readonly_record")
    win.workspace.show_page(0)
    win.workspace.phrases.select_pid(pids[0])
    state("readonly_phrases")

    workers.wait_idle()
    win.close()
    pump(50)
    for name, ps in problems.items():
        print(f"[{tag}] {name}: {len(ps)} problem(s)")
        for p in ps:
            print("    ", p)
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(problems, indent=1), "utf-8")
    sys.stdout.flush()
    win.deleteLater()
    pump(50)


if __name__ == "__main__":
    _main(sys.argv[1:])
