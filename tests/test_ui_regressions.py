"""Regression tests for verified UI bugs (offscreen, fake audio_io like test_ui_smoke)."""
from __future__ import annotations

import pathlib
import sys
import threading
import time

import numpy as np
import pytest

# importing the smoke module installs the fakes + offscreen platform exactly once
from test_ui_smoke import app, pump, speechish, wait_until  # noqa: F401  (app is a fixture)

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QHideEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QLineEdit, QPlainTextEdit,  # noqa: E402
                               QTextEdit)

from codriver_studio.ui import audio as ui_audio  # noqa: E402
from codriver_studio.ui import workers  # noqa: E402
from codriver_studio.ui.main_window import MainWindow  # noqa: E402

SR = 48000


@pytest.fixture()
def win(app):  # noqa: F811
    w = MainWindow()
    w.resize(1360, 880)
    w.show()
    pump()
    yield w
    workers.wait_idle()
    w.audio.cancel_recording()
    w.close()
    pump()
    w.deleteLater()
    pump()


@pytest.fixture(autouse=True)
def errors(monkeypatch):
    """Uncaught exceptions in slots (they'd be 'Something went wrong' dialogs in the app).
    Captured instead of reaching the app hook (whose modal dialog would hang offscreen);
    any of them fails the test."""
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: seen.append(f"{t.__name__}: {e}"))
    monkeypatch.setattr(threading, "excepthook",
                        lambda a: seen.append(f"{a.exc_type.__name__}: {a.exc_value}"))
    yield seen
    assert seen == [], seen


def _record_ws(win, tmp_path, name="proj"):
    proj = win.create_project(tmp_path / name, "Reg")
    ws = win.workspace
    rec = ws.record
    rec.cb_missing.setChecked(False)
    rec.cb_autoplay.setChecked(False)
    rec.cb_count.setChecked(False)
    rec.cb_hands.setChecked(False)
    return proj, ws, rec


def _trim(t):
    return round(t.trim_start, 6), round(t.trim_end, 6), t.auto_trimmed


# ---------------------------------------------------------------- 1. pending trim commit lands on the right take
def test_nudge_then_enter_lands_on_nudged_take(win, tmp_path):
    proj, ws, rec = _record_ws(win, tmp_path)
    ids = [p.id for p in proj.catalog.phrases]
    a, b = ids[20], ids[21]
    proj.add_take(a, speechish(words=3, seed=1), SR)
    proj.add_take(b, speechish(words=1, seed=2), SR)
    b_before = _trim(proj.takes(b)[0])
    a_end = proj.takes(a)[0].trim_end
    ws.show_page(1)
    pump(80)
    rec.goto(a)
    pump(30)
    wave = rec.panel.wave
    wave.setFocus()
    pump(20)
    wave.select_handle("end")
    QTest.keyClick(wave, Qt.Key_Right)    # nudge +10 ms, commit pending (350 ms)
    QTest.keyClick(wave, Qt.Key_Return)   # keep & next right away
    pump(600)
    assert rec.pid == b
    assert _trim(proj.takes(b)[0]) == b_before, "nudge leaked onto the next phrase's take"
    ta = proj.takes(a)[0]
    assert ta.trim_end == pytest.approx(a_end + 0.010, abs=1e-4)
    assert ta.auto_trimmed is False


def test_nudge_then_retake_does_not_touch_older_take(win, tmp_path):
    proj, ws, rec = _record_ws(win, tmp_path)
    a = proj.catalog.phrases[25].id
    proj.add_take(a, speechish(words=1, seed=1), SR)
    proj.set_trim(a, 0, 0.40, 1.00)
    keeper = _trim(proj.takes(a)[0])
    ws.show_page(1)
    pump(80)
    rec.goto(a)
    pump(30)
    rec._take_ready(speechish(words=3, seed=2), SR, False)   # session take #2 in the editor
    pump(30)
    assert rec.panel.idx == 1
    wave = rec.panel.wave
    wave.setFocus()
    pump(20)
    wave.select_handle("end")
    QTest.keyClick(wave, Qt.Key_Right)
    QTest.keyClick(wave, Qt.Key_R)        # retake: deletes take #2, starts recording
    pump(500)
    rec.cancel()
    pump(30)
    assert len(proj.takes(a)) == 1
    assert _trim(proj.takes(a)[0]) == keeper, "nudge of the retaken take landed on the older take"


def test_commit_uses_take_identity_captured_at_edit_start(win, tmp_path):
    """Even if something moves the panel without flushing, the debounced commit hits the
    take the edit was made on."""
    proj, ws, _rec = _record_ws(win, tmp_path)
    ids = [p.id for p in proj.catalog.phrases]
    a, b = ids[30], ids[31]
    proj.add_take(a, speechish(words=2, seed=3), SR)
    proj.add_take(b, speechish(words=2, seed=4), SR)
    b_before = _trim(proj.takes(b)[0])
    ws.show_page(0)
    panel = ws.phrases.panel
    panel.show_phrase(a, 0)
    pump(30)
    s0 = panel.wave.start
    panel.wave.select_handle("start")
    panel.wave.nudge(0.02)
    panel.pid, panel.idx = b, 0          # simulate a path that forgets to flush
    pump(600)                             # debounce timer fires
    assert _trim(proj.takes(b)[0]) == b_before
    assert proj.takes(a)[0].trim_start == pytest.approx(s0 + 0.02, abs=1e-4)


def test_commit_for_deleted_take_is_dropped(win, tmp_path):
    proj, ws, _rec = _record_ws(win, tmp_path)
    a = proj.catalog.phrases[40].id
    proj.add_take(a, speechish(words=2, seed=5), SR)
    proj.add_take(a, speechish(words=3, seed=6), SR)
    second = _trim(proj.takes(a)[1])
    ws.show_page(0)
    panel = ws.phrases.panel
    panel.show_phrase(a, 0)
    pump(30)
    panel.wave.select_handle("end")
    panel.wave.nudge(-0.03)
    proj.delete_take(a, 0)               # take #1 vanishes under the pending commit
    pump(600)
    assert len(proj.takes(a)) == 1
    assert _trim(proj.takes(a)[0]) == second, "commit for a deleted take hit its sibling"


def test_row_change_flushes_onto_previous_take(win, tmp_path):
    proj, ws, _rec = _record_ws(win, tmp_path)
    a = proj.catalog.phrases[41].id
    proj.add_take(a, speechish(words=2, seed=7), SR)
    proj.add_take(a, speechish(words=2, seed=8), SR)
    second = _trim(proj.takes(a)[1])
    ws.show_page(0)
    panel = ws.phrases.panel
    panel.show_phrase(a, 0)
    pump(30)
    e0 = panel.wave.end
    panel.wave.select_handle("end")
    panel.wave.nudge(-0.02)
    panel.list.setCurrentRow(1)           # click take 2 within 350 ms
    pump(500)
    assert proj.takes(a)[0].trim_end == pytest.approx(e0 - 0.02, abs=1e-4)
    assert _trim(proj.takes(a)[1]) == second


# ---------------------------------------------------------------- 2. app-lifetime signals vs deleted workspaces
def test_close_and_open_project_no_deleted_widget_errors(win, tmp_path, errors):
    from codriver_studio.ui.pages import effects as eff
    p1 = win.create_project(tmp_path / "one", "One")
    pump(100)
    p1.add_take(p1.catalog.phrases[30].id, speechish(seed=3), SR)
    win.ctl.notify(p1.catalog.phrases[30].id)
    win.workspace.show_page(0)
    pump(50)
    win.close_project()
    pump(50)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    pump(50)
    win.create_project(tmp_path / "two", "Two")
    pump(150)
    win.workspace.show_page(0)
    pump(50)
    win.workspace.show_page(1)
    pump(100)
    a = win.audio
    a.input_state.emit(True)
    a.input_state.emit(False)
    a.devices_changed.emit()
    a.playback_finished.emit(eff.TOKEN)
    a.playback_position.emit("x", 0.1)
    a.level.emit(-20.0)
    a.clipped.emit()
    a.recording_changed.emit(False)
    pump(100)
    assert errors == []


def test_no_lambda_connections_to_audio_service_in_ui_source():
    """Static guard: AudioService / Controller signals must not be connected to lambdas/partials."""
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "codriver_studio" / "ui"
    bad = []
    pat = re.compile(r"(audio|ctl)\.\w+\.connect\(\s*(lambda|partial|functools\.partial)")
    for f in root.rglob("*.py"):
        for n, line in enumerate(f.read_text("utf-8").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{f.name}:{n}: {line.strip()}")
    assert bad == []


# ---------------------------------------------------------------- 3. keyboard flow on the Record page
def test_record_page_child_widgets_dont_steal_keys(win, tmp_path):
    proj, ws, rec = _record_ws(win, tmp_path)
    ids = [p.id for p in proj.catalog.phrases]
    proj.add_take(ids[20], speechish(seed=1), SR)
    ws.show_page(1)
    pump(80)
    rec.goto(ids[20])
    pump(30)
    win.activateWindow()
    rec.setFocus()
    pump(30)
    panel = rec.panel
    for w in (panel.list, panel.sens, panel.btn_play, panel.btn_raw, panel.btn_auto, panel.btn_delete):
        assert w.focusPolicy() == Qt.NoFocus, w
    # clicking the take list / slider must not move keyboard focus there
    r = panel.list.visualItemRect(panel.list.item(0))
    QTest.mouseClick(panel.list.viewport(), Qt.LeftButton, Qt.NoModifier, r.center())
    QTest.mouseClick(panel.sens, Qt.LeftButton, Qt.NoModifier, QPoint(5, panel.sens.height() // 2))
    pump(30)
    assert QApplication.focusWidget() not in (panel.list, panel.sens)
    v0 = panel.sens.value()
    check0 = panel.list.item(0).checkState()
    QTest.keyClick(QApplication.focusWidget() or rec, Qt.Key_Right)
    pump(30)
    assert rec.pid == ids[21], "Right should skip to the next phrase"
    assert panel.sens.value() == v0
    QTest.keyClick(QApplication.focusWidget() or rec, Qt.Key_Left)
    pump(30)
    assert rec.pid == ids[20]
    QTest.keyClick(QApplication.focusWidget() or rec, Qt.Key_Space)
    assert wait_until(lambda: rec.state == "rec", 3)
    assert panel.list.item(0).checkState() == check0
    rec.cancel()


def test_typing_in_text_fields_never_drives_the_recorder(win, tmp_path):
    proj, ws, rec = _record_ws(win, tmp_path)
    pid = proj.catalog.phrases[5].id
    proj.add_take(pid, speechish(seed=2), SR)
    win.ctl.notify(pid)
    ws.show_page(1)
    pump(50)
    rec.goto(pid)
    rec._take_ready(speechish(seed=3), SR, False)    # a session take R could delete
    pump(30)
    n_takes = len(proj.takes(pid))
    state0 = rec.state
    fields = []
    for i, page in enumerate(ws.pages):
        for cls in (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox):
            fields += [(i, f) for f in page.findChildren(cls)]
    assert len(fields) >= 3
    for i, f in fields:
        if not f.isEnabled():
            continue
        ws.show_page(i)
        pump(20)
        if not f.isVisible():
            continue
        f.setFocus()
        pump(10)
        for k in (Qt.Key_R, Qt.Key_Space, Qt.Key_P, Qt.Key_Left, Qt.Key_Right, Qt.Key_Backspace):
            QTest.keyClick(f, k)
        pump(20)
        assert rec.state == state0 and rec.pid == pid, f
        assert len(proj.takes(pid)) == n_takes, f
        assert not win.audio.is_recording, f
    # the Return key in a text field elsewhere neither
    ws.show_page(4)
    pump(20)
    ws.export.persona.setFocus()
    QTest.keyClick(ws.export.persona, Qt.Key_Return)
    pump(20)
    assert rec.pid == pid


# ---------------------------------------------------------------- 4./5. folder names + Documents location
@pytest.mark.parametrize("name,bad_stem", [
    ("Nul", "nul"), ("CON", "con"), ("prn", "prn"), ("Aux", "aux"), ("COM1", "com1"), ("com9", "com9"),
    ("LPT1", "lpt1"), ("lpt9", "lpt9"), ("nul.txt", "nul"), ("CON.pack", "con"), ("Nul.", "nul"),
    ("aux ", "aux"),
])
def test_safe_folder_name_avoids_reserved_device_names(name, bad_stem):
    from codriver_studio.ui.welcome import _safe_folder_name
    out = _safe_folder_name(name)
    stem = out.split(".", 1)[0].rstrip(" ").lower()
    assert stem != bad_stem, out
    assert out and not out.endswith((".", " ")), out


@pytest.mark.parametrize("name,expected", [
    ("My Pack...", "My Pack"), ("Pack . . ", "Pack"), ("  Rally  ", "Rally"), ("a:b", "a_b"),
    ("Console", "Console"), ("COM10", "COM10"), ("Nully", "Nully"), ("...", "My Co-Driver"),
])
def test_safe_folder_name_trailing_and_normal(name, expected):
    from codriver_studio.ui.welcome import _safe_folder_name
    assert _safe_folder_name(name) == expected


def test_documents_location_follows_qstandardpaths(win, monkeypatch, tmp_path):
    from PySide6.QtCore import QStandardPaths
    from PySide6.QtWidgets import QFileDialog
    from codriver_studio.ui import settings as st
    from codriver_studio.ui import welcome
    fake_docs = str(tmp_path / "OneDrive" / "Documents")

    def wl(loc):
        if loc == QStandardPaths.DocumentsLocation:
            return fake_docs
        if loc == QStandardPaths.DesktopLocation:
            return str(tmp_path / "OneDrive" / "Desktop")
        return ""
    monkeypatch.setattr(QStandardPaths, "writableLocation", staticmethod(wl))
    assert welcome.default_projects_root() == pathlib.Path(fake_docs) / "Co-Driver Studio"
    st.put("last_project", None)
    starts = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: starts.append(a[2]) or ""))
    win.open_project_dialog()
    assert starts == [fake_docs]


# ---------------------------------------------------------------- 6. minimize vs page switch while recording
def _start_take(win, tmp_path):
    proj, ws, rec = _record_ws(win, tmp_path)
    pid = proj.catalog.phrases[12].id
    ws.show_page(1)
    pump(60)
    rec.goto(pid)
    rec.start_recording()
    assert wait_until(lambda: rec.state == "rec", 3)
    pump(500)
    return proj, ws, rec, pid


def test_window_hide_or_minimize_keeps_recording(win, tmp_path):
    proj, ws, rec, pid = _start_take(win, tmp_path)
    # what Qt delivers when the window is minimized: a hide event while the page is still current
    QApplication.sendEvent(rec, QHideEvent())
    pump(30)
    assert rec.state == "rec" and win.audio.is_recording
    win.showMinimized()
    pump(100)
    assert rec.state == "rec" and win.audio.is_recording
    win.showNormal()
    pump(100)
    assert rec.state == "rec"
    rec.toggle_record()
    assert wait_until(lambda: rec.state == "review", 3)
    assert len(proj.takes(pid)) == 1


def test_page_switch_stops_and_keeps_the_take(win, tmp_path):
    proj, ws, rec, pid = _start_take(win, tmp_path)
    ws.show_page(0)
    pump(100)
    assert not win.audio.is_recording
    assert rec.state != "rec"
    assert len(proj.takes(pid)) == 1, "page switch discarded the take in progress"
    assert not win.audio.is_playing   # no autoplay on a hidden page


# ---------------------------------------------------------------- 7. mic dropout
class _DyingRecorder:
    """Fake Recorder whose stream dies after die_after_s (error attribute or is_alive())."""
    die_after_s = 0.6
    mode = "error"

    def __init__(self, device=None, samplerate=None):
        self.samplerate = samplerate or SR
        self.is_recording = False
        self.error = None
        self._alive = True
        self._t = None

    def start(self, on_block=None):
        self.is_recording = True
        t0 = time.monotonic()

        def run():
            rng = np.random.default_rng(3)
            while self.is_recording:
                if time.monotonic() - t0 > self.die_after_s:
                    if self.mode == "error":
                        self.error = "Device unavailable [PaErrorCode -9985]"
                    else:
                        self._alive = False
                    return
                b = (0.2 * np.sin(np.arange(1024) * 0.05) + rng.normal(0, 0.003, 1024)).astype(np.float32)
                if on_block:
                    on_block(b)
                time.sleep(1024 / self.samplerate)
        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()

    def is_alive(self):
        return self._alive

    def stop(self):
        self.is_recording = False
        if self._t:
            self._t.join()
        if self.error:
            raise RuntimeError(self.error)
        return np.zeros(0, np.float32)

    def level_db(self):
        return -20.0


@pytest.mark.parametrize("mode", ["error", "is_alive"])
def test_mic_dropout_keeps_captured_audio_and_reports(win, tmp_path, monkeypatch, mode):
    cls = type("Dying", (_DyingRecorder,), {"mode": mode, "die_after_s": 0.8})
    monkeypatch.setattr(ui_audio.audio_io, "Recorder", cls)
    proj, ws, rec = _record_ws(win, tmp_path)
    win.audio.set_monitoring(False)
    win.audio._close_input()
    pid = proj.catalog.phrases[14].id
    ws.show_page(1)
    pump(30)
    rec.goto(pid)
    win.audio._close_input()       # fresh recorder: the dying clock starts with the take
    rec.start_recording()
    assert wait_until(lambda: rec.state == "rec", 3)
    assert wait_until(lambda: rec.state != "rec", 4), "still 'Recording' after the mic died"
    assert not win.audio.is_recording
    takes = proj.takes(pid)
    assert len(takes) == 1 and takes[0].duration >= 0.3
    assert "disconnected" in rec.state_label.text().lower()
    assert win.audio.input_open is False


def test_mic_dropout_too_short_discards_quietly(win, tmp_path, monkeypatch):
    cls = type("DyingFast", (_DyingRecorder,), {"die_after_s": 0.05})
    monkeypatch.setattr(ui_audio.audio_io, "Recorder", cls)
    proj, ws, rec = _record_ws(win, tmp_path)
    pid = proj.catalog.phrases[15].id
    ws.show_page(1)
    pump(30)
    rec.goto(pid)
    win.audio._close_input()
    rec.start_recording()
    assert wait_until(lambda: rec.state != "rec", 3)
    assert proj.takes(pid) == []
    assert rec.state == "idle"
    assert "disconnected" in rec.state_label.text().lower()


# ---------------------------------------------------------------- 8. idle mic recycling vs ring buffer
@pytest.mark.parametrize("api", ["start_keep_last_s", "init_max_seconds"])
def test_ring_buffer_recorder_is_never_recycled(win, monkeypatch, api):
    base = ui_audio.audio_io.Recorder

    if api == "start_keep_last_s":       # the real audio_io API: start(on_block, keep_last_s)
        class RingRecorder(base):
            max_seconds = None

            def start(self, on_block=None, keep_last_s=None):
                self.max_seconds = keep_last_s
                super().start(on_block=on_block)
    else:
        class RingRecorder(base):
            def __init__(self, device=None, samplerate=None, max_seconds=None):
                super().__init__(device=device, samplerate=samplerate)
                self.max_seconds = max_seconds
    monkeypatch.setattr(ui_audio.audio_io, "Recorder", RingRecorder)
    a = win.audio
    a.set_monitoring(False)
    a.set_monitoring(True)
    assert wait_until(lambda: a._blocks_seen, 3)
    first = a._rec
    assert first.max_seconds and first.max_seconds >= ui_audio.MAX_TAKE_S
    a._rec_opened_at -= ui_audio.MONITOR_RESTART_S + 10
    pump(200)
    assert a._rec is first, "ring-buffer recorder was closed/reopened (audible on Bluetooth)"
    a.set_monitoring(False)


def test_legacy_recorder_still_recycled_when_idle(win):
    a = win.audio
    a.set_monitoring(False)
    a.set_monitoring(True)
    assert wait_until(lambda: a._blocks_seen, 3)
    first = a._rec
    a._rec_opened_at -= ui_audio.MONITOR_RESTART_S + 10
    pump(200)
    assert a._rec is not None and a._rec is not first
    a.set_monitoring(False)


# ---------------------------------------------------------------- 9. render resolves the take by file
def test_render_async_resolves_take_by_file(win, tmp_path, monkeypatch):
    proj = win.create_project(tmp_path / "r", "R")
    ctl = win.ctl
    pid = proj.catalog.phrases[50].id
    for k in range(3):
        proj.add_take(pid, speechish(words=1 + k, seed=20 + k), SR)
    files = [t.file for t in proj.takes(pid)]
    rendered = []
    orig = type(proj).render_take

    def spy(self, phrase_id, take_index, out_sr=None):
        with self.lock:
            rendered.append(self.takes(phrase_id)[take_index].file)
        return orig(self, phrase_id, take_index, out_sr)
    monkeypatch.setattr(type(proj), "render_take", spy)
    got = []
    with proj.lock:                      # worker can't start rendering until we release
        ctl.render_async(pid, 1, lambda y, sr: got.append(len(y)))
        proj.delete_take(pid, 0)         # concurrent delete shifts the indices
    assert workers.wait_idle(10000)
    assert wait_until(lambda: bool(got), 5)
    assert rendered == [files[1]], rendered


# ---------------------------------------------------------------- project lock / read-only / device names
def test_second_window_gets_read_only_and_lock_is_released(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from codriver_studio import project as project_mod
    proj = win.create_project(tmp_path / "shared", "Shared")
    assert proj.has_lock
    pid = proj.catalog.phrases[3].id
    proj.add_take(pid, speechish(seed=9), SR)
    w2 = MainWindow()
    w2.show()
    pump()
    try:
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Cancel))
        assert w2.open_project(proj.folder) is None and w2.ctl is None
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        p2 = w2.open_project(proj.folder)
        assert p2 is not None and p2.read_only and w2.ctl.read_only
        ws2 = w2.workspace
        assert ws2.ro_badge.isVisibleTo(w2)
        ws2.show_page(1)
        pump(50)
        rec2 = ws2.record
        rec2.goto(pid)
        rec2.start_recording()
        pump(50)
        assert rec2.state == "idle" and not w2.audio.is_recording
        assert not rec2.btn_rec.isEnabled()
        assert not ws2.effects.master.isEnabled()
        assert not ws2.export.persona.isEnabled()
        ws2.show_page(0)
        ws2.phrases.panel.show_phrase(pid, 0)
        pump(30)
        assert not ws2.phrases.panel.btn_delete.isEnabled()
        assert not ws2.phrases.panel.wave.isEnabled()
        w2.ctl.save()          # silently refused, no dialog / exception
    finally:
        w2.close()
        pump()
        w2.deleteLater()
    # closing the writer releases the lock: someone else can take it
    win.close_project()
    p3 = project_mod.Project.open(proj.folder)
    assert p3.acquire_lock()
    p3.release_lock()


def test_saved_truncated_device_name_matches_full_name():
    devs = [(3, "Speakers (Realtek(R) Audio)"), (7, "LG TV (NVIDIA High Definition Audio)"),
            (9, "LG TV (NVIDIA High Definition Audio) #2")]
    assert ui_audio.match_device_name(devs, "LG TV (NVIDIA High Definition A") == 7
    assert ui_audio.match_device_name(devs, "LG TV (NVIDIA High Definition Audio) #2") == 9
    assert ui_audio.match_device_name(devs, "Speakers (Realtek(R) Audio)") == 3
    assert ui_audio.match_device_name(devs, "USB Mic") is None
    assert ui_audio.match_device_name(devs, None) is None


def test_merge_from_other_instance_refreshes_lists(win, tmp_path):
    proj = win.create_project(tmp_path / "m", "M")
    seen = []
    win.ctl.projectChanged.connect(lambda: seen.append(1))
    pid = proj.catalog.phrases[2].id
    proj.last_merge_added = 2          # as if the last save merged two foreign takes
    win.ctl.notify(pid)
    assert seen, "a merge must trigger a full refresh"
    assert proj.last_merge_added == 0


@pytest.mark.parametrize("key", ["skip", "go_prev"])
def test_phrase_navigation_mid_take_keeps_it_on_the_recorded_phrase(win, tmp_path, key):
    proj, ws, rec, pid = _start_take(win, tmp_path)
    if key == "go_prev":
        rec.history.append(pid)          # make sure "previous" has somewhere to go
        rec.history.insert(0, rec._next_pid(pid))
    getattr(rec, key)()
    pump(100)
    assert not win.audio.is_recording
    assert len(proj.takes(pid)) == 1, "navigating away discarded the take in progress"
    assert rec.pid != pid or key == "go_prev"
    assert not win.audio.is_playing      # navigation never autoplays the stopped take


# ---------------------------------------------------------------- pack language on the export page
def test_export_page_language_choice(win, tmp_path):
    proj = win.create_project(tmp_path / "lang", "Lang")
    ws = win.workspace
    ws.show_page(4)
    pump(20)
    page = ws.export
    assert page.language.currentText() == "English" and page.dialect.currentData() == "en-US"

    # builtin: Spanish → dialect list follows, defaults to es-ES
    page.language.setCurrentIndex(page.language.findData("spanish"))
    page.language.activated.emit(page.language.currentIndex())
    pump(20)
    assert proj.pack.language == "spanish" and proj.pack.dialect == "es-ES"
    assert [page.dialect.itemData(i) for i in range(page.dialect.count())] == ["es-ES"]

    # custom: typed name is kept literally, builtin dialect is dropped, free-text dialect allowed
    page.language.setEditText("Русский")
    page.language.lineEdit().editingFinished.emit()
    pump(20)
    assert proj.pack.language == "Русский" and proj.pack.dialect == ""
    assert page.dialect.count() == 0
    page.dialect.setEditText("ru-RU")
    page.dialect.lineEdit().editingFinished.emit()
    pump(20)
    assert proj.pack.dialect == "ru-RU"

    # typing a builtin's name maps to its id; saved and shown again after reopening
    page.language.setEditText("english")
    page.language.lineEdit().editingFinished.emit()
    pump(20)
    assert proj.pack.language == "english" and proj.pack.dialect == "en-US"
    from codriver_studio.project import Project
    assert Project.open(proj.folder, read_only=True).pack.language == "english"
    page.load_from_project()
    assert page.language.currentText() == "English"


def test_window_title_shows_version(win, tmp_path):
    from codriver_studio import __version__
    assert win.windowTitle() == f"Co-Driver Studio v{__version__}"
    win.create_project(tmp_path / "ver", "Title Test")
    pump(20)
    assert win.windowTitle() == f"Title Test · Co-Driver Studio v{__version__}"
    assert __version__ == "1.2.0"


# ---------------------------------------------------------------- timing on the Sample Calls page
def test_sample_calls_timing_syncs_with_export_and_is_exported(win, tmp_path):
    import json as _json
    from codriver_studio import exporter
    from codriver_studio.project import Project
    proj = win.create_project(tmp_path / "timing", "Timing")
    ws = win.workspace
    sc, ex = ws.samples, ws.export
    for pid in ("three_left", "hard_right"):
        proj.add_take(pid, speechish(SR, 2, 1), SR)

    # changing a gap on Sample Calls updates the pack, the export page and the saved project
    ws.show_page(3)
    pump(20)
    sc.timing.spins["phrase_gap_ms"].setValue(120)
    sc.timing.spins["pacenote_gap_ms"].setValue(300)
    pump(20)
    assert proj.pack.phrase_gap_ms == 120 and proj.pack.pacenote_gap_ms == 300
    assert ex.gap_spins["phrase_gap_ms"].value() == 120 and ex.gap_spins["pacenote_gap_ms"].value() == 300
    saved = _json.loads((proj.folder / "project.json").read_text("utf-8"))["pack"]
    assert saved["phrase_gap_ms"] == 120 and saved["pacenote_gap_ms"] == 300

    # and the other way round
    ex.gap_spins["link_word_gap_ms"].setValue(-80)
    pump(20)
    assert sc.timing.spins["link_word_gap_ms"].value() == -80 and proj.pack.link_word_gap_ms == -80

    # the exported voicepack.json carries the values set on the Sample Calls page
    ent, _ = exporter.build_voicepack_infos(Project.open(proj.folder, read_only=True))
    assert ent["phraseGapMs"] == 120 and ent["pacenoteGapMs"] == 300 and ent["linkWordGapMs"] == -80


def test_sample_calls_timing_change_replays_last_call_with_new_gap(win, tmp_path):
    proj = win.create_project(tmp_path / "replay", "Replay")
    ws = win.workspace
    sc = ws.samples
    for pid in ("three_left", "hard_right"):
        proj.add_take(pid, speechish(SR, 2, 2), SR)
    ws.show_page(3)
    pump(20)
    sc.cb_vary.setChecked(False)
    sc.edit.setPlainText("three left hard right")
    sc.play()
    assert wait_until(lambda: sc.btn_play.isEnabled() and getattr(sc, "last_audio", None) is not None, 10)
    n0 = len(sc.last_audio[0])
    win.audio.stop_playback()
    sc.last_audio = None
    sc.timing.spins["corner_gap_ms"].setValue(200)     # both phrases are corner parts → corner gap
    assert wait_until(lambda: getattr(sc, "last_audio", None) is not None, 10), "no replay after a timing change"
    n1 = len(sc.last_audio[0])
    assert n1 - n0 == pytest.approx(0.2 * sc.last_audio[1], abs=0.02 * sc.last_audio[1])
    win.audio.stop_playback()


def test_sample_calls_timing_disabled_when_read_only(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    proj = win.create_project(tmp_path / "shared_t", "SharedT")
    w2 = MainWindow()
    w2.show()
    pump()
    try:
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        p2 = w2.open_project(proj.folder)
        assert p2 is not None and p2.read_only
        spins = w2.workspace.samples.timing.spins
        assert spins and not any(sp.isEnabled() for sp in spins.values())
    finally:
        w2.close()
        pump()
        w2.deleteLater()
        pump()
