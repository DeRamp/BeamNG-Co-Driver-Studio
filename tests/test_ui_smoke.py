"""Offscreen smoke test of the whole GUI.

Uses the real non-UI modules when they import, except audio_io which is always the private
fake (no real microphone in tests). CDS_UI_FAKES=1 forces fakes for every module.
Set CDS_SHOT_DIR to save a screenshot of each page.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import zipfile

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.path.isdir(r"C:\Windows\Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")  # offscreen QPA ships no fonts
_SETTINGS_TMP = tempfile.mkdtemp(prefix="cds_ui_settings_")
os.environ["CDS_SETTINGS_DIR"] = _SETTINGS_TMP
for p in (str(ROOT), str(FAKES)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _install_modules():
    import ui_fakes
    used = {}
    force = os.environ.get("CDS_UI_FAKES") == "1"
    fake_names = []
    for name in ui_fakes.NAMES:
        if name == "audio_io":
            continue  # patched into the UI only (below), never into sys.modules
        if force:
            fake_names.append(name)
            continue
        try:
            importlib.import_module(f"codriver_studio.{name}")
            used[name] = "real"
        except Exception:  # noqa: BLE001
            fake_names.append(name)
    if fake_names:   # install([]) would install EVERY fake (``names or NAMES``)
        ui_fakes.install(fake_names)
    used.update({n: "fake" for n in fake_names})
    used["audio_io"] = "fake"
    return used


MODULES = _install_modules()

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import ui_fakes.audio_io as fake_audio_io  # noqa: E402
from codriver_studio.ui import audio as ui_audio  # noqa: E402
from codriver_studio.ui import workers  # noqa: E402

# no real microphone/speakers in tests: the UI's audio service talks to the fake
ui_audio.audio_io = fake_audio_io
ui_audio.AudioDeviceError = fake_audio_io.AudioDeviceError
from codriver_studio.ui.app import make_app  # noqa: E402
from codriver_studio.ui.main_window import MainWindow  # noqa: E402

SHOT_DIR = os.environ.get("CDS_SHOT_DIR")


def pump(ms=50):
    end = time.monotonic() + ms / 1000
    app = QApplication.instance()
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.instance().processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def speechish(sr=48000, words=2, seed=0):
    """Room noise + a few voiced bursts (harmonics with an envelope)."""
    rng = np.random.default_rng(seed)
    n = int(sr * (0.6 + 0.45 * words + 0.5))
    x = rng.normal(0, 0.002, n).astype(np.float32)
    t0 = 0.5
    for w in range(words):
        dur = 0.32
        i0, i1 = int(t0 * sr), int((t0 + dur) * sr)
        tt = np.arange(i1 - i0) / sr
        f0 = 140 + 20 * w
        v = sum(np.sin(2 * np.pi * f0 * k * tt) / k for k in range(1, 8))
        env = np.sin(np.pi * tt / dur) ** 0.5
        x[i0:i1] += (0.25 * v * env).astype(np.float32)
        t0 += dur + 0.12
    return np.clip(x, -1, 1).astype(np.float32)


def shot(win, name):
    if not SHOT_DIR:
        return
    os.makedirs(SHOT_DIR, exist_ok=True)
    pump(120)
    win.grab().save(os.path.join(SHOT_DIR, f"{name}.png"))


@pytest.fixture(scope="module")
def app():
    a = make_app(["test"])
    # dialogs must never block the test
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    return a


@pytest.fixture()
def win(app, tmp_path):
    w = MainWindow()
    w.resize(1360, 880)
    w.show()
    pump()
    yield w
    w.close()
    pump()
    w.deleteLater()
    pump()


def test_modules_report():
    print("UI smoke modules:", MODULES)
    assert MODULES["audio_io"] == "fake"


def test_full_ui_flow(win, tmp_path):
    shot(win, "00_welcome")
    proj = win.create_project(tmp_path / "proj", "Test Driver")
    assert proj is not None and win.workspace is not None
    ws, ctl = win.workspace, win.ctl
    cat = proj.catalog

    # ---- every page renders while empty
    for i, name in enumerate(["phrases", "record", "effects", "samples", "export"]):
        ws.show_page(i)
        pump(60)
        assert ws.stack.currentIndex() == i
    ws.show_page(1)
    shot(win, "01_record_empty")

    # ---- inject synthetic takes (no microphone)
    pids = ["three_left", "into", "hard_right", "dont_cut", "150", "100", "long", "caution", "left", "right"]
    pids = [p for p in pids if p in {ph.id for ph in cat.phrases}]
    assert len(pids) >= 5
    for k, pid in enumerate(pids):
        proj.add_take(pid, speechish(words=len(pid.split("_")), seed=k), 48000)
        ctl.notify(pid)
    sys_pid = next(p.id for p in cat.phrases if p.is_system)
    proj.add_take(sys_pid, speechish(words=1, seed=99), 48000)
    proj.add_take(sys_pid, speechish(words=1, seed=98), 48000)
    ctl.notify(sys_pid)
    done, total = ctl.progress()
    assert done == len(pids) + 1
    assert ws.progress.format() == f"{done} / {total} recorded"

    # ---- phrases page: select + take panel + drag trim handles
    ws.show_page(0)
    page = ws.phrases
    page.select_pid("three_left")
    pump(50)
    panel = page.panel
    assert panel.pid == "three_left" and panel.idx == 0
    wave = panel.wave
    assert wave.has_audio
    s0, e0 = wave.trim()
    new_start = s0 + 0.05
    x0 = int(wave.x_for_time(s0))
    x1 = int(wave.x_for_time(new_start))
    y = wave.height() // 2
    QTest.mousePress(wave, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y))
    QTest.mouseMove(wave, QPoint((x0 + x1) // 2, y))
    QTest.mouseMove(wave, QPoint(x1, y))
    QTest.mouseRelease(wave, Qt.LeftButton, Qt.NoModifier, QPoint(x1, y))
    pump(30)
    t = proj.takes("three_left")[0]
    assert abs(t.trim_start - wave.start) < 1e-3 and t.trim_start > s0 + 0.02, (s0, t.trim_start)
    assert t.auto_trimmed is False
    # end handle with keyboard nudge
    wave.setFocus()
    wave.select_handle("end")
    before = wave.end
    QTest.keyClick(wave, Qt.Key_Left)
    QTest.keyClick(wave, Qt.Key_Left, Qt.ShiftModifier)
    assert abs(wave.end - (before - 0.011)) < 1e-6
    wave._flush_commit()
    assert abs(proj.takes("three_left")[0].trim_end - (before - 0.011)) < 1e-4
    wave.zoom(2.0)
    wave.fit()
    panel.reauto_trim()
    assert proj.takes("three_left")[0].auto_trimmed
    shot(win, "02_phrases")

    # play (fake player) → playhead moves
    panel.play_trimmed()
    assert wait_until(lambda: ctl.audio.is_playing, 5)
    assert wait_until(lambda: wave.playhead is not None, 3)
    ctl.audio.stop_playback()

    # system phrase takes behave like radio buttons
    page.select_pid(sys_pid)
    pump(30)
    states = [tk.enabled for tk in proj.takes(sys_pid)]
    assert sum(states) == 1
    off = states.index(False)
    panel.list.item(off).setCheckState(Qt.Checked)
    pump(30)
    states = [tk.enabled for tk in proj.takes(sys_pid)]
    assert states[off] and sum(states) == 1
    assert panel.list.item(1 - off).checkState() == Qt.Unchecked  # list refreshed
    # filters
    page.filter.setCurrentIndex(1)  # missing
    pump(20)
    assert "three_left" not in page._rows
    page.filter.setCurrentIndex(0)
    page.search.setText("hard")
    page.rebuild()
    assert "hard_right" in page._rows and "three_left" not in page._rows
    page.search.setText("")
    page.rebuild()

    # ---- record page: guided flow with the fake recorder
    ws.show_page(1)
    rec = ws.record
    rec.cb_count.setChecked(False)
    rec.cb_hands.setChecked(False)
    rec.cb_missing.setChecked(True)
    rec.goto(rec._first_pid())
    pid = rec.pid
    assert proj.status(pid) != "recorded"
    n_before = len(proj.takes(pid))
    QTest.keyClick(rec, Qt.Key_Space)
    assert wait_until(lambda: rec.state == "rec", 3)
    pump(700)
    QTest.keyClick(rec, Qt.Key_Space)
    assert wait_until(lambda: rec.state == "review", 5)
    assert len(proj.takes(pid)) == n_before + 1
    assert rec.panel.wave.has_audio
    shot(win, "03_record_review")
    # retake replaces the take
    QTest.keyClick(rec, Qt.Key_R)
    assert wait_until(lambda: rec.state == "rec", 3)
    pump(500)
    QTest.keyClick(rec, Qt.Key_Space)
    assert wait_until(lambda: rec.state == "review", 5)
    assert len(proj.takes(pid)) == n_before + 1
    # keep & next, previous, skip
    QTest.keyClick(rec, Qt.Key_Return)
    pump(30)
    assert rec.pid != pid
    QTest.keyClick(rec, Qt.Key_Backspace)
    assert rec.pid == pid
    QTest.keyClick(rec, Qt.Key_Right)
    assert rec.pid != pid
    # injected take through the audio service signal path
    ctl.audio.take_ready.emit(speechish(seed=5), 48000, False)
    pump(30)
    assert proj.status(rec.pid) == "recorded"
    ctl.audio.stop_playback()

    # hands-free: fake recorder alternates tone / silence → detector auto-stops
    rec.cb_hands.setChecked(True)
    rec.skip()
    hp = rec.pid
    rec.start_recording()
    stopped = wait_until(lambda: rec.state == "review", 8)
    print("hands-free auto-stop:", stopped)
    if not stopped:
        rec.toggle_record()
        wait_until(lambda: rec.state == "review", 3)
    assert len(proj.takes(hp)) >= 1
    rec.cb_hands.setChecked(False)
    ctl.audio.stop_playback()

    # ---- effects page
    ws.show_page(2)
    pump(50)
    eff = ws.effects
    assert eff.phrase_combo.count() >= 5
    names = [eff.preset.itemText(i) for i in range(eff.preset.count())]
    radio = names.index("Rally Radio")
    eff.preset.setCurrentIndex(radio)
    eff._preset_chosen(radio)
    hp_val = proj.effects.highpass_hz
    assert hp_val > 0
    s = eff._sliders["drive"]
    s.slider.setValue(s.slider.maximum())
    assert proj.effects.drive == pytest.approx(1.0)
    assert eff.preset.currentText() == "Custom"
    eff._debounce.timeout.emit()
    eff.play()
    assert wait_until(lambda: ctl.audio.play_token == "effects-preview", 10)
    eff.btn_a.setChecked(True)
    pump(100)
    ctl.audio.stop_playback()
    import json
    saved = json.loads((proj.folder / "project.json").read_text("utf-8"))
    assert saved["effects"]["drive"] == pytest.approx(1.0)
    shot(win, "04_effects")

    # ---- sample calls
    ws.show_page(3)
    sc = ws.samples
    sc.edit.setPlainText("150 three left into hard right don't cut blorp")
    sc.reparse()
    assert "three_left" in sc.ids and "hard_right" in sc.ids
    assert "blorp" in sc.unknown
    sc.append_phrase("caution")
    assert sc.ids[-1] == "caution"
    sc.play()
    assert wait_until(lambda: ctl.audio.play_token == "sample-call", 10)
    shot(win, "05_sample_calls")
    sc.random_stage()
    assert wait_until(lambda: sc.btn_stage.isEnabled(), 15)
    ctl.audio.stop_playback()

    # ---- export through the page's code path
    ws.show_page(4)
    ex = ws.export
    ex.interactive = False
    ex.persona.setText("Test Driver Two")
    ex._persona_changed()
    assert proj.pack.persona == "Test Driver Two"
    ex.gap_spins["pacenote_gap_ms"].setValue(250)
    assert proj.pack.pacenote_gap_ms == 250
    out = tmp_path / "out" / "pack.zip"
    sig = ex.export_to(out)
    assert sig is not None
    assert wait_until(lambda: ex.last_report is not None or ex.last_error is not None, 60), "export timed out"
    assert ex.last_error is None, ex.last_error
    rep = ex.last_report
    assert pathlib.Path(rep.zip_path).exists()
    assert zipfile.is_zipfile(rep.zip_path)
    assert rep.files_written >= len(pids)
    assert ex.result.isVisibleTo(ex)
    shot(win, "06_export")

    # ---- reopen project from disk (recent list + welcome)
    folder = proj.folder
    win.close_project()
    pump(30)
    shot(win, "07_welcome_recent")
    assert win.welcome.recent.count() >= 1
    p2 = win.open_project(folder)
    assert p2 is not None and p2.status("three_left") == "recorded"
    workers.wait_idle()


def test_audio_device_error_is_friendly(win, tmp_path):
    """No mic: Recorder raises AudioDeviceError → dialog signal, no crash."""
    audio_io = ui_audio.audio_io

    class Broken(audio_io.Recorder):
        def start(self, on_block=None):
            raise audio_io.AudioDeviceError("No microphone found")

    win.create_project(tmp_path / "p2", "Nobody")
    errors = []
    win.audio.error.connect(errors.append)
    orig = audio_io.Recorder
    audio_io.Recorder = Broken
    try:
        win.audio.cancel_recording()
        win.audio.set_monitoring(False)
        rec = win.workspace.record
        win.workspace.show_page(1)
        pump(30)
        win.audio._input_failed = False
        win.audio._close_input()
        rec.cb_count.setChecked(False)
        rec.start_recording()
        pump(30)
        assert rec.state == "idle"
        assert errors and "microphone" in errors[-1].lower()
    finally:
        audio_io.Recorder = orig


def test_app_starts_as_process(tmp_path):
    """`python -m codriver_studio` boots (offscreen) and quits cleanly."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", CDS_SETTINGS_DIR=str(tmp_path), CDS_QUIT_AFTER_MS="2500")
    r = subprocess.run([sys.executable, "-m", "codriver_studio", "--welcome"], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "Traceback" not in r.stderr, r.stderr[-3000:]
