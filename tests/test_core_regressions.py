"""Regression tests for reviewer-verified core bugs (project locking / merge-safe save / orphan
recovery / single-take restore / audio_io robustness). Every test here failed before the fix."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from cds_helpers import SR, tone
from codriver_studio import audio_io, dsp
from codriver_studio import project as projmod
from codriver_studio.project import Project, ProjectError


def read_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def folder(tmp_path):
    p = Project.create(tmp_path / "proj", "Lock Test")
    return p.folder


# ============================================================================ 1a. project lock
def test_lock_second_instance_refused_until_released(folder):
    a = Project.open(folder)
    b = Project.open(folder)
    assert a.lock_holder() is None
    assert a.acquire_lock() is True
    assert (folder / ".codriver.lock").is_file()
    info = read_json(folder / ".codriver.lock")
    assert info["pid"] == os.getpid() and info["host"] and info["time"] > 0
    assert a.acquire_lock() is True            # re-acquiring our own lock is fine
    assert a.lock_holder() is None             # ours: not "another holder"
    assert b.acquire_lock() is False
    holder = b.lock_holder()
    assert holder and str(os.getpid()) in holder
    b.release_lock()                           # not ours: must not remove a's lock
    assert (folder / ".codriver.lock").is_file()
    a.release_lock()
    assert not (folder / ".codriver.lock").exists()
    assert b.lock_holder() is None
    assert b.acquire_lock() is True
    b.release_lock()


def _write_lock(folder, **info):
    (Path(folder) / ".codriver.lock").write_text(json.dumps(info), encoding="utf-8")


def test_lock_stale_dead_pid_is_taken_over(folder):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _write_lock(folder, pid=proc.pid, host=projmod._hostname(), time=time.time())
    p = Project.open(folder)
    assert p.lock_holder() is None
    assert p.acquire_lock() is True
    assert read_json(folder / ".codriver.lock")["pid"] == os.getpid()
    p.release_lock()


def test_lock_live_pid_other_process_blocks(folder):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(folder, pid=proc.pid, host=projmod._hostname(), time=time.time())
        p = Project.open(folder)
        assert p.acquire_lock() is False
        assert str(proc.pid) in p.lock_holder()
    finally:
        proc.kill()
        proc.wait()
    assert p.acquire_lock() is True            # process gone → stale
    p.release_lock()


def test_lock_other_host_trusted_unless_old(folder):
    _write_lock(folder, pid=1234, host="some-other-pc", time=time.time() - 60)
    p = Project.open(folder)
    assert p.acquire_lock() is False
    assert "some-other-pc" in p.lock_holder()
    _write_lock(folder, pid=1234, host="some-other-pc", time=time.time() - 13 * 3600)
    assert p.lock_holder() is None
    assert p.acquire_lock() is True
    p.release_lock()


def test_pid_alive():
    assert projmod._pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not projmod._pid_alive(proc.pid)
    assert not projmod._pid_alive(0)


def test_read_only_open_never_writes(folder):
    before = (folder / "project.json").read_bytes()
    p = Project.open(folder, read_only=True)
    assert p.read_only
    with pytest.raises(ProjectError):
        p.save()
    with pytest.raises(ProjectError):
        p.add_take("hard_left", tone(), SR)
    assert (folder / "project.json").read_bytes() == before
    assert not (folder / "takes" / "hard_left").exists()


# ============================================================================ 1b. merge-safe save
def test_revision_and_writer_id_written(folder):
    p = Project.open(folder)
    r0 = read_json(folder / "project.json")["revision"]
    p.save()
    d = read_json(folder / "project.json")
    assert d["revision"] == r0 + 1 and d["writer_id"] == p.writer_id
    q = Project.open(folder)
    assert q.writer_id != p.writer_id
    q.save()
    assert read_json(folder / "project.json")["revision"] == r0 + 2


def test_two_instances_do_not_lose_each_others_takes(folder):
    a = Project.open(folder)
    b = Project.open(folder)
    ta = a.add_take("hard_left", tone(440), SR)
    tb = b.add_take("hard_left", tone(660), SR)
    assert ta.file != tb.file                   # numbering never reuses a file on disk
    files = {t["file"] for t in read_json(folder / "project.json")["takes"]["hard_left"]}
    assert files == {ta.file, tb.file}          # before the fix b's save overwrote a's take
    assert b.last_merge_added == 1
    b.add_take("hard_right", tone(), SR)
    # a edits after b saved: a merges b's takes instead of dropping them
    a.set_trim("hard_left", 0, 0.1, 0.5)
    assert a.last_merge_added == 2
    d = read_json(folder / "project.json")
    assert {t["file"] for t in d["takes"]["hard_left"]} == {ta.file, tb.file}
    assert len(d["takes"]["hard_right"]) == 1
    entry = next(t for t in d["takes"]["hard_left"] if t["file"] == ta.file)
    assert entry["trim_start"] == pytest.approx(0.1) and entry["auto_trimmed"] is False
    c = Project.open(folder)
    assert len(c.takes("hard_left")) == 2 and len(c.takes("hard_right")) == 1
    assert not any("Recovered" in w for w in c.load_warnings)


def test_merge_conflicting_fields_keep_ours(folder):
    p = Project.open(folder)
    p.add_take("hard_left", tone(), SR)
    a = Project.open(folder)
    b = Project.open(folder)
    a.set_trim("hard_left", 0, 0.05, 0.3)
    b.set_take_enabled("hard_left", 0, False)   # b's version of the take wins (ours)
    t = read_json(folder / "project.json")["takes"]["hard_left"]
    assert len(t) == 1 and t[0]["enabled"] is False


def test_merge_does_not_resurrect_deleted_takes(folder):
    a = Project.open(folder)
    a.add_take("hard_left", tone(), SR)
    a.add_take("hard_left", tone(500), SR)
    b = Project.open(folder)
    a.delete_take("hard_left", 0)               # a deletes take 1 (file removed)
    b.set_translation("hard_left", "links hart")  # b still has take 1 in memory
    d = read_json(folder / "project.json")
    assert [t["file"] for t in d["takes"]["hard_left"]] == ["takes/hard_left/take_002.wav"]
    assert d["translations"]["hard_left"] == "links hart"
    a.set_trim("hard_left", 0, 0.0, 0.2)        # and a does not get it back from b either
    assert len(read_json(folder / "project.json")["takes"]["hard_left"]) == 1


def test_merge_single_take_phrase_keeps_one_enabled(folder):
    pid = "system_countdown3_1"
    a = Project.open(folder)
    b = Project.open(folder)
    a.add_take(pid, tone(), SR)
    b.add_take(pid, tone(600), SR)
    d = read_json(folder / "project.json")["takes"][pid]
    assert len(d) == 2 and sum(t["enabled"] for t in d) == 1
    assert len(Project.open(folder).exportable_takes(pid)) == 1


# ============================================================================ 1c. orphan recovery
def test_orphan_wavs_recovered_as_disabled_takes(folder):
    p = Project.open(folder)
    p.add_take("hard_left", tone(), SR)
    d = folder / "takes" / "hard_left"
    dsp.write_wav(d / "take_002.wav", tone(550, lead=0.3), SR)
    (folder / "takes" / "hard_right").mkdir(parents=True)
    dsp.write_wav(folder / "takes" / "hard_right" / "take_007.wav", tone(), SR)
    (folder / "takes" / "no_such_phrase").mkdir(parents=True)
    dsp.write_wav(folder / "takes" / "no_such_phrase" / "take_001.wav", tone(), SR)
    (d / "take_009.wav").write_bytes(b"")       # empty (e.g. a crashed reservation): not a recording

    q = Project.open(folder)
    assert any(w.startswith("Recovered 2 recordings that were not in the project file") for w in q.load_warnings)
    assert any("no_such_phrase" in w for w in q.load_warnings)
    hl = q.takes("hard_left")
    assert [t.file for t in hl] == ["takes/hard_left/take_001.wav", "takes/hard_left/take_002.wav"]
    rec = hl[1]
    assert rec.enabled is False and rec.auto_trimmed is True and not rec.missing
    assert rec.duration == pytest.approx(len(tone(550, lead=0.3)) / SR, abs=1e-4)
    x, sr = dsp.read_audio(d / "take_002.wav")
    exp = dsp.auto_trim(x, sr, sensitivity=q.trim_sensitivity)
    assert (rec.trim_start, rec.trim_end) == (pytest.approx(exp[0], abs=1e-5), pytest.approx(exp[1], abs=1e-5))
    assert 0.0 <= rec.trim_start < rec.trim_end <= rec.duration
    assert q.status("hard_right") == "disabled"
    assert "no_such_phrase" not in q._takes
    # numbering never reuses an existing filename on disk (009 exists, even empty)
    t = q.add_take("hard_left", tone(), SR)
    assert t.file == "takes/hard_left/take_010.wav"
    # persisted on the next save: reopening recovers nothing new
    again = Project.open(folder)
    assert not any("Recovered" in w for w in again.load_warnings)
    assert len(again.takes("hard_left")) == 3


def test_locked_delete_is_not_recovered_later(folder, monkeypatch):
    p = Project.open(folder)
    p.add_take("hard_left", tone(), SR)
    p.add_take("hard_left", tone(), SR)
    victim = p.abs_path(p.takes("hard_left")[0].file)
    real_unlink = Path.unlink

    def locked(self, *a, **k):
        if Path(self) == victim:
            raise PermissionError("in use")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", locked)
    p.delete_take("hard_left", 0)
    assert victim.exists()
    q = Project.open(folder)
    assert len(q.takes("hard_left")) == 1 and not any("Recovered" in w for w in q.load_warnings)
    t = q.add_take("hard_left", tone(), SR)
    assert t.file.endswith("take_003.wav")
    monkeypatch.setattr(Path, "unlink", real_unlink)
    Project.open(folder)                        # retried on open
    assert not victim.exists()


def test_concurrent_numbering_reserves_file(folder):
    """Two instances picking a number at the same moment never share a file."""
    a = Project.open(folder)
    b = Project.open(folder)
    with a.lock, b.lock:
        r1 = a._next_take_file("hard_left")
        r2 = b._next_take_file("hard_left")
    assert r1 != r2


# ============================================================================ 2. single-take retake
def test_single_take_retake_delete_restores_users_choice(folder):
    p = Project.open(folder)
    pid = "over_crest_corner"
    p.add_take(pid, tone(), SR)
    p.add_take(pid, tone(600), SR)
    p.set_take_enabled(pid, 0, True)            # user picks take 1 of 2
    assert [t.enabled for t in p.takes(pid)] == [True, False]
    p.add_take(pid, tone(700), SR)              # retake auto-enables itself
    assert [t.enabled for t in p.takes(pid)] == [False, False, True]
    p.delete_take(pid, 2)                       # R: throw the retake away
    assert [t.enabled for t in p.takes(pid)] == [True, False]


def test_single_take_restore_falls_back_to_newest_if_previous_gone(folder):
    p = Project.open(folder)
    pid = "system_countdown3_1"
    for f in (400, 500, 600):
        p.add_take(pid, tone(f), SR)
    p.set_take_enabled(pid, 0, True)
    p.add_take(pid, tone(700), SR)              # displaces take 0
    p.delete_take(pid, 0)                       # the remembered take is gone (not enabled → no change)
    assert [t.enabled for t in p.takes(pid)] == [False, False, True]
    p.delete_take(pid, 2)
    assert [t.enabled for t in p.takes(pid)] == [False, True]


# ============================================================================ 3. audio_io (fake PortAudio)
class _Flags:
    def __init__(self, input_overflow=False):
        self.input_overflow = input_overflow

    def __bool__(self):
        return self.input_overflow


class _FakeStream:
    def __init__(self, callback=None, finished_callback=None, channels=1, hang=0.0, **kw):
        self.callback = callback
        self.finished_callback = finished_callback
        self.channels = channels
        self.active = False
        self.hang = hang
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def abort(self):
        if self.hang:
            time.sleep(self.hang)
        self.active = False

    def close(self):
        if self.hang:
            time.sleep(self.hang)
        self.closed = True


class _FakeSD:
    class CallbackStop(Exception):
        pass

    class default:
        hostapi = 0
        device = (0, 1)

    def __init__(self, devices=None, hostapis=None, hang=0.0):
        self.devices = devices or [
            {"name": "Mic", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0,
             "default_samplerate": 1000.0, "index": 0},
            {"name": "Out", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2,
             "default_samplerate": 1000.0, "index": 1},
        ]
        self.hostapis = hostapis or [{"name": "MME"}]
        self.streams = []
        self.hang = hang

    def query_devices(self, device=None, kind=None):
        if device is None:
            return self.devices
        return self.devices[int(device)]

    def query_hostapis(self):
        return self.hostapis

    def InputStream(self, **kw):
        s = _FakeStream(**kw)
        self.streams.append(s)
        return s

    def OutputStream(self, **kw):
        s = _FakeStream(hang=self.hang, **kw)
        self.streams.append(s)
        return s


@pytest.fixture
def fake_sd(monkeypatch):
    sd = _FakeSD()
    monkeypatch.setattr(audio_io, "_sd", lambda: sd)
    return sd


def _feed(rec, stream, values, block=20, status=None):
    for v in values:
        stream.callback(np.full((block, stream.channels), v, np.float32), block, None, status or _Flags())


def test_recorder_ring_buffer_bounded_and_snapshot(fake_sd):
    rec = audio_io.Recorder()
    assert rec.samplerate == 1000
    rec.start(keep_last_s=0.5)
    st = fake_sd.streams[-1]
    _feed(rec, st, np.arange(100, dtype=np.float32) / 100)       # 2 s of 20-sample blocks
    assert rec._n <= 500 + 20                                    # bounded memory
    snap = rec.snapshot(0.1)
    assert snap.size == 100 and snap.dtype == np.float32
    assert np.allclose(snap[:20], 0.95) and np.allclose(snap[-20:], 0.99)
    big = rec.snapshot(10)
    assert 500 <= big.size <= 520                               # only what the ring holds
    assert rec.is_recording                                      # snapshot does not stop
    snap[:] = 0
    assert rec.snapshot(0.02)[0] == pytest.approx(0.99)          # snapshot is a copy
    rec.clear()
    assert rec.snapshot(1.0).size == 0
    _feed(rec, st, [0.5])
    take = rec.stop()
    assert take.size == 20 and np.allclose(take, 0.5)


def test_recorder_unbounded_by_default(fake_sd):
    rec = audio_io.Recorder()
    rec.start()
    _feed(rec, fake_sd.streams[-1], [0.1] * 100)
    assert rec.snapshot(0.5).size == 500
    assert rec.stop().size == 2000


def test_recorder_ring_thread_safe(fake_sd):
    rec = audio_io.Recorder()
    rec.start(keep_last_s=0.3)
    st = fake_sd.streams[-1]
    stop = threading.Event()
    errs = []

    def producer():
        while not stop.is_set():
            _feed(rec, st, [0.25] * 10)

    th = threading.Thread(target=producer)
    th.start()
    try:
        for _ in range(300):
            s = rec.snapshot(0.2)
            if s.size > 200 or (s.size and not np.allclose(s, 0.25)):
                errs.append(s.size)
            if _ % 50 == 0:
                rec.clear()
    finally:
        stop.set()
        th.join()
    assert not errs
    assert rec.stop().size <= 320


def test_recorder_overflow_counted_and_callback_never_raises(fake_sd):
    rec = audio_io.Recorder()
    rec.start()
    st = fake_sd.streams[-1]
    _feed(rec, st, [0.1, 0.1], status=_Flags(input_overflow=True))
    assert rec.overflow_count == 2 and rec.status_flags == 2
    assert rec.is_alive() and rec.error is None
    st.callback(object(), 20, None, _Flags())                   # garbage → no exception escapes
    assert rec.error and not rec.is_alive()


def test_recorder_detects_dead_stream(fake_sd):
    rec = audio_io.Recorder()
    rec.start()
    st = fake_sd.streams[-1]
    _feed(rec, st, [0.1])
    assert rec.is_alive()
    st.active = False                                           # PortAudio stopped the stream
    assert not rec.is_alive() and "microphone" in rec.error.lower()
    assert rec.stop().size == 20


def test_recorder_finished_callback_sets_error(fake_sd):
    rec = audio_io.Recorder()
    rec.start()
    st = fake_sd.streams[-1]
    st.finished_callback()                                      # stream ended without stop()
    assert rec.error and not rec.is_alive()
    rec.stop()
    rec.start()
    assert rec.error is None                                    # cleared on restart
    rec.stop()
    fake_sd.streams[-1].finished_callback()                     # finish caused by our stop(): fine
    assert rec.error is None


def test_recorder_watchdog_no_callbacks(fake_sd, monkeypatch):
    monkeypatch.setattr(audio_io, "_WATCHDOG_S", 0.15)
    rec = audio_io.Recorder()
    rec.start()
    assert rec.is_alive()
    time.sleep(0.25)                                           # mic unplugged: no callbacks
    assert not rec.is_alive()
    assert "no audio" in rec.error.lower()
    assert not audio_io.Recorder().is_alive()                  # not recording → not alive


def test_player_on_done_fires_when_device_vanishes(fake_sd, monkeypatch):
    monkeypatch.setattr(audio_io, "_WATCHDOG_S", 0.2)
    pl = audio_io.Player()
    done = threading.Event()
    pl.play(np.zeros(5000, np.float32), 1000, on_done=done.set)  # callbacks never come
    assert pl.is_playing
    assert done.wait(2.0), "on_done must fire even if the device died"
    assert not pl.is_playing and pl.error


def test_player_on_done_fires_when_stream_stops_early(fake_sd):
    pl = audio_io.Player()
    done = threading.Event()
    pl.play(np.zeros(5000, np.float32), 1000, on_done=done.set)
    st = fake_sd.streams[-1]
    st.callback(np.zeros((100, st.channels), np.float32), 100, None, None)
    st.active = False                                           # device removed mid-playback
    assert done.wait(2.0)
    assert pl.error


def test_player_hanging_close_does_not_block_on_done(monkeypatch):
    sd = _FakeSD(hang=5.0)
    monkeypatch.setattr(audio_io, "_sd", lambda: sd)
    monkeypatch.setattr(audio_io, "_WATCHDOG_S", 0.2)
    pl = audio_io.Player()
    done = threading.Event()
    t0 = time.monotonic()
    pl.play(np.zeros(5000, np.float32), 1000, on_done=done.set)
    assert done.wait(3.0)
    assert time.monotonic() - t0 < 3.0


# ============================================================================ 3c. device names
def test_display_name_rule():
    trunc = "LG TV (NVIDIA High Definition A"
    assert len(trunc) == 31
    assert audio_io.display_name(trunc, ["LG TV (NVIDIA High Definition Audio)", "Other"]) == \
        "LG TV (NVIDIA High Definition Audio)"
    assert audio_io.display_name(trunc, [trunc + "udio)", trunc + "udio) 2"]) == trunc   # ambiguous
    assert audio_io.display_name(trunc, [trunc + "udio)", trunc + "udio)"]) == trunc + "udio)"  # dupes
    assert audio_io.display_name("Short (Name", ["Short (Name) longer"]) == "Short (Name"     # not truncated
    assert audio_io.display_name(trunc, []) == trunc


def test_listing_uses_full_names_keeps_mme_indices(monkeypatch):
    devs = [
        {"name": "Microphone (2- Razer Kiyo Pro)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "LG TV (NVIDIA High Definition A", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Digital Output (DESKTOP_ABC1234", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "LG TV (NVIDIA High Definition Audio)", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Digital Output (DESKTOP_ABC1234)", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Microphone (2- Razer Kiyo Pro)", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 0},
    ]
    sd = _FakeSD(devices=devs, hostapis=[{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}])
    monkeypatch.setattr(audio_io, "_sd", lambda: sd)
    assert audio_io.list_output_devices() == [(1, "LG TV (NVIDIA High Definition Audio)"),
                                              (2, "Digital Output (DESKTOP_ABC1234)")]
    assert audio_io.list_input_devices() == [(0, "Microphone (2- Razer Kiyo Pro)")]


@pytest.mark.skipif(sys.platform != "win32", reason="MME truncation is Windows-specific")
def test_real_devices_not_truncated_when_full_name_known():
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        apis = sd.query_hostapis()
    except Exception as e:
        pytest.skip(f"no PortAudio: {e}")
    full_ids = {i for i, h in enumerate(apis) if "WASAPI" in h["name"] or "DirectSound" in h["name"]}
    for kind, listing in (("input", audio_io.list_input_devices()), ("output", audio_io.list_output_devices())):
        key = f"max_{kind}_channels"
        full = {d["name"] for d in devs if d["hostapi"] in full_ids and d[key] > 0}
        for idx, name in listing:
            raw = devs[idx]["name"]
            assert devs[idx][key] > 0                              # index still the MME device
            matches = {f for f in full if len(f) > len(raw) and f.startswith(raw)}
            if len(raw) >= 31 and len(matches) == 1:
                assert name == matches.pop()
            else:
                assert name == raw
