import threading
import time

import numpy as np
import pytest

from codriver_studio import audio_io


def test_device_listing_does_not_crash():
    ins = audio_io.list_input_devices()
    outs = audio_io.list_output_devices()
    assert isinstance(ins, list) and isinstance(outs, list)
    for idx, name in ins + outs:
        assert isinstance(idx, int) and isinstance(name, str)
    d = audio_io.default_input_device()
    assert d is None or isinstance(d, int)


def test_bad_device_raises_friendly_error():
    with pytest.raises(audio_io.AudioDeviceError):
        audio_io.Recorder(device=99999)
    with pytest.raises(audio_io.AudioDeviceError):
        audio_io.Player(device=99999)


def _recorder():
    try:
        return audio_io.Recorder()
    except audio_io.AudioDeviceError as e:
        pytest.skip(f"no input device: {e}")


def _player():
    try:
        return audio_io.Player()
    except audio_io.AudioDeviceError as e:
        pytest.skip(f"no output device: {e}")


def test_record_short_take():
    rec = _recorder()
    blocks = []
    try:
        rec.start(on_block=lambda b: blocks.append(len(b)))
    except audio_io.AudioDeviceError as e:
        pytest.skip(f"input device unusable: {e}")
    assert rec.is_recording
    time.sleep(0.3)
    lvl = rec.level_db()
    take = rec.stop()
    assert not rec.is_recording
    assert take.dtype == np.float32 and take.ndim == 1
    assert 0.15 * rec.samplerate <= len(take) <= 0.8 * rec.samplerate
    assert blocks and sum(blocks) == len(take)
    assert -120.0 <= lvl <= 0.0 + 1e-6
    assert rec.level_db() == -120.0
    again = rec.stop()                       # second stop is harmless
    assert len(again) == len(take)


def test_play_silence_on_done_once_and_restart():
    pl = _player()
    calls = []
    done = threading.Event()

    def on_done():
        calls.append(1)
        pl.stop()                            # stop() from on_done must not deadlock
        done.set()

    try:
        pl.play(np.zeros(4800, np.float32), 48000, on_done=on_done)
    except audio_io.AudioDeviceError as e:
        pytest.skip(f"output device unusable: {e}")
    assert done.wait(3.0), "on_done not called"
    time.sleep(0.1)
    assert calls == [1]
    assert not pl.is_playing
    assert pl.position_s() == pytest.approx(0.1, abs=0.02) or pl.position_s() == 0.0

    # play() while playing stops the previous one (its on_done fires once), stop() twice is fine
    first, second = [], threading.Event()
    pl.play(np.zeros(48000, np.float32), 48000, on_done=lambda: first.append(1))
    time.sleep(0.1)
    assert pl.is_playing
    assert 0.0 <= pl.position_s() < 1.0
    pl.play(np.zeros(2400, np.float32), 24000, on_done=second.set)
    assert second.wait(3.0)
    time.sleep(0.1)
    assert first == [1]
    pl.stop()
    pl.stop()
    pl.play(np.zeros(0, np.float32), 48000)  # empty buffer
    time.sleep(0.2)
    assert not pl.is_playing
