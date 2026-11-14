"""Qt-side audio service.

audio_io callbacks run on PortAudio threads. They only push numpy blocks into a deque;
a GUI-thread QTimer drains it, updates meters, runs the hands-free silence detector and
tracks playback position. No widget is ever touched from a non-GUI thread.
"""
from __future__ import annotations

import collections
import inspect
import time

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from .. import audio_io, dsp
from . import settings as st
from .errors import log

AudioDeviceError = getattr(audio_io, "AudioDeviceError", RuntimeError)

PREROLL_S = 0.12         # audio kept from just before Space was pressed
MAX_TAKE_S = 30.0        # safety auto-stop
MONITOR_RESTART_S = 45.0  # legacy Recorder buffers everything; recycle it while idle to bound memory
RING_S = MAX_TAKE_S + 5.0  # bounded Recorder buffer (when audio_io supports it): no recycling needed
MIN_DROPOUT_KEEP_S = 0.3  # mic died mid-take: keep what was captured if at least this long
CLIP_LEVEL = 0.985
DROPOUT_MSG = ("The microphone stopped delivering audio (unplugged, Bluetooth dropped or taken by another "
               "app). Reconnect it, check Audio settings, then press Space to try again.")


def _accepts(fn, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def ring_buffer_kwargs(recorder_cls) -> tuple[dict, dict]:
    """(init_kwargs, start_kwargs) that put audio_io.Recorder in bounded ring-buffer mode, or two
    empty dicts if this audio_io doesn't support it (then the idle stream is recycled instead)."""
    if _accepts(recorder_cls.__init__, "max_seconds"):
        return {"max_seconds": RING_S}, {}
    if _accepts(getattr(recorder_cls, "start", None), "keep_last_s"):
        return {}, {"keep_last_s": RING_S}
    return {}, {}


def recorder_failure(rec) -> str | None:
    """Error text if the recorder's stream died (audio_io `Recorder.error` / `Recorder.is_alive()`),
    else None. Tolerant of recorders that have neither."""
    err = getattr(rec, "error", None)
    if callable(err):
        try:
            err = err()
        except Exception:  # noqa: BLE001
            err = None
    if err:
        return str(err)
    alive = getattr(rec, "is_alive", None)
    if alive is not None:
        try:
            ok = alive() if callable(alive) else alive
        except Exception as e:  # noqa: BLE001
            return str(e) or "stream check failed"
        if not ok:
            return "input stream stopped"
    return None


def friendly_device_error(exc: BaseException, what: str) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    if what == "input":
        return ("Couldn't open the microphone.\n\n" + msg +
                "\n\nCheck that a mic is plugged in and allowed in Windows Settings › Privacy › Microphone, "
                "then pick it under Audio settings.")
    return ("Couldn't play audio.\n\n" + msg +
            "\n\nCheck your speakers/headphones and pick an output under Audio settings.")


def match_device_name(devs, name):
    """Index of the device remembered as `name`: exact match first, then a prefix match either way
    (older builds saved MME's 31-char truncated names, e.g. "LG TV (NVIDIA High Definition A")."""
    if not name:
        return None
    name = str(name)
    for idx, n in devs:
        if n == name:
            return idx
    low = name.strip().lower()
    cands = [(idx, n) for idx, n in devs
             if n.lower().startswith(low) or (len(n) >= 8 and low.startswith(n.strip().lower()))]
    if cands:
        return min(cands, key=lambda c: abs(len(c[1]) - len(name)))[0]
    return None


def _devices(fn) -> list[tuple[int, str]]:
    try:
        return list(fn())
    except Exception as e:  # noqa: BLE001
        log.warning("device listing failed: %s", e)
        return []


class AudioService(QObject):
    level = Signal(float)                 # input peak dBFS (-120 = silent / closed)
    clipped = Signal()
    input_state = Signal(bool)            # input stream open?
    recording_changed = Signal(bool)
    take_ready = Signal(object, int, bool)  # audio, samplerate, stopped_automatically
    playback_started = Signal(str)
    playback_position = Signal(str, float)
    playback_finished = Signal(str)
    error = Signal(str)                   # friendly message for a dialog
    devices_changed = Signal()
    input_lost = Signal(str)              # mic stream died (emitted after any salvaged take_ready)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.input_device = self._restore_device("audio/input", audio_io.list_input_devices, default=True)
        self.output_device = self._restore_device("audio/output", audio_io.list_output_devices)
        self._rec = None
        self._rec_opened_at = 0.0
        self._queue: collections.deque = collections.deque()
        self._blocks_seen = False
        self._monitor_wanted = False
        self._recording = False
        self._take: list[np.ndarray] = []
        self._take_len = 0
        self._preroll: collections.deque = collections.deque()
        self._preroll_len = 0
        self._detector = None
        self._input_failed = False
        self._ring = False            # current Recorder is bounded (no idle recycling)
        self.dropout_message: str | None = None   # set while/after the mic dropped out, cleared on reopen

        self._player = None
        self._player_dev = object()
        self._play_token: str | None = None
        self._play_len = 0.0
        self._play_t0 = 0.0
        self._last_level = -120.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    # ------------------------------------------------------------------ devices
    def _restore_device(self, key, lister, default=False):
        name = st.get(key + "_name")
        devs = _devices(lister)
        idx = match_device_name(devs, name)
        if idx is not None:
            full = self._name_of(devs, idx)
            if full and full != name:
                st.put(key + "_name", full)   # upgrade a truncated saved name
            return idx
        if default:
            try:
                return audio_io.default_input_device()
            except Exception:  # noqa: BLE001
                return None
        return None

    def input_devices(self):
        return _devices(audio_io.list_input_devices)

    def output_devices(self):
        return _devices(audio_io.list_output_devices)

    def device_name(self, which: str) -> str:
        dev = self.input_device if which == "input" else self.output_device
        devs = self.input_devices() if which == "input" else self.output_devices()
        for idx, n in devs:
            if idx == dev:
                return n
        return "System default"

    def set_input_device(self, idx):
        if idx == self.input_device:
            return
        self.input_device = idx
        st.put("audio/input_name", self._name_of(self.input_devices(), idx))
        self._input_failed = False
        if self._rec is not None and not self._recording:
            self._close_input()
            self._sync_input()
        self.devices_changed.emit()

    def set_output_device(self, idx):
        self.output_device = idx
        st.put("audio/output_name", self._name_of(self.output_devices(), idx))
        self.stop_playback()
        self._player = None
        self.devices_changed.emit()

    @staticmethod
    def _name_of(devs, idx):
        for i, n in devs:
            if i == idx:
                return n
        return None

    # ------------------------------------------------------------------ input
    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def input_open(self) -> bool:
        return self._rec is not None

    @property
    def samplerate(self) -> int:
        return int(getattr(self._rec, "samplerate", 48000) or 48000)

    def set_monitoring(self, on: bool):
        """Keep the mic open (for the level meter) while idle."""
        self._monitor_wanted = on
        if on:
            self._input_failed = False
        self._sync_input()

    def _sync_input(self):
        want = self._monitor_wanted or self._recording
        if want and self._rec is None and not self._input_failed:
            self._open_input(report=False)
        elif not want and self._rec is not None:
            self._close_input()

    def _on_block(self, block):  # PortAudio thread: only enqueue
        self._queue.append(block)

    def _open_input(self, report=True) -> bool:
        init_kw, start_kw = ring_buffer_kwargs(audio_io.Recorder)
        try:
            rec = audio_io.Recorder(device=self.input_device, **init_kw)
            rec.start(on_block=self._on_block, **start_kw)
        except Exception as e:  # noqa: BLE001 - AudioDeviceError, PortAudioError, ...
            log.warning("input open failed: %s", e)
            self._input_failed = True
            self._rec = None
            if report:
                self.error.emit(friendly_device_error(e, "input"))
            self.input_state.emit(False)
            return False
        self._rec = rec
        self._ring = bool(init_kw or start_kw)
        self.dropout_message = None
        self._rec_opened_at = time.monotonic()
        self._blocks_seen = False
        self._queue.clear()
        self._preroll.clear()
        self._preroll_len = 0
        self.input_state.emit(True)
        return True

    def _close_input(self):
        rec, self._rec = self._rec, None
        if rec is not None:
            try:
                rec.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("recorder stop failed: %s", e)
        self._queue.clear()
        self.level.emit(-120.0)
        self.input_state.emit(False)

    def start_recording(self, handsfree=False, sensitivity=0.5, silence_ms=900) -> bool:
        if self._recording:
            return True
        self.stop_playback()
        if self._rec is not None and not self._blocks_seen:
            # recorder doesn't deliver blocks (or none yet): use a fresh one so stop() == the take
            self._close_input()
        if self._rec is None and not self._open_input(report=True):
            return False
        self._recording = True
        self._take = list(self._preroll) if self._blocks_seen else []
        self._take_len = sum(len(b) for b in self._take)
        self._rec_started = time.monotonic()
        self._detector = None
        if handsfree:
            try:
                self._detector = dsp.LiveSilenceDetector(self.samplerate, silence_ms=silence_ms,
                                                         sensitivity=sensitivity)
            except Exception as e:  # noqa: BLE001
                log.warning("LiveSilenceDetector failed: %s", e)
        self.recording_changed.emit(True)
        return True

    def stop_recording(self, automatic=False):
        if not self._recording:
            return
        self._drain()
        self._recording = False
        sr = self.samplerate
        if self._blocks_seen and self._take:
            x = np.concatenate(self._take).astype(np.float32)
            keep_open = self._monitor_wanted
            if not keep_open:
                self._close_input()
        else:
            rec, self._rec = self._rec, None
            try:
                x = rec.stop() if rec is not None else np.zeros(0, np.float32)
            except Exception as e:  # noqa: BLE001
                self.error.emit(friendly_device_error(e, "input"))
                x = np.zeros(0, np.float32)
            self.input_state.emit(False)
            self._sync_input()
        self._take = []
        self._detector = None
        self.recording_changed.emit(False)
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        self.take_ready.emit(x, sr, automatic)

    def cancel_recording(self):
        if not self._recording:
            return
        self._recording = False
        self._take = []
        self._detector = None
        if not self._monitor_wanted:
            self._close_input()
        self.recording_changed.emit(False)

    def _drain(self):
        q = self._queue
        peak = 0.0
        while q:
            b = np.asarray(q.popleft(), dtype=np.float32).reshape(-1)
            if not len(b):
                continue
            self._blocks_seen = True
            peak = max(peak, float(np.max(np.abs(b))))
            if self._recording:
                self._take.append(b)
                self._take_len += len(b)
                if self._detector is not None:
                    try:
                        if self._detector.feed(b):
                            self._detector = None
                            self._auto_stop_pending = True
                    except Exception as e:  # noqa: BLE001
                        log.warning("detector failed: %s", e)
                        self._detector = None
            else:
                self._preroll.append(b)
                self._preroll_len += len(b)
                limit = PREROLL_S * self.samplerate
                while self._preroll and self._preroll_len - len(self._preroll[0]) >= limit:
                    self._preroll_len -= len(self._preroll.popleft())
        return peak

    # ------------------------------------------------------------------ output
    @property
    def is_playing(self) -> bool:
        return self._play_token is not None

    @property
    def play_token(self):
        return self._play_token

    def play(self, x, sr, token: str = "") -> bool:
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if not len(x):
            return False
        if self._recording:
            return False
        try:
            if self._player is None or self._player_dev != self.output_device:
                self._player = audio_io.Player(device=self.output_device)
                self._player_dev = self.output_device
            self._player.play(x, int(sr))
        except Exception as e:  # noqa: BLE001
            log.warning("playback failed: %s", e)
            self._player = None
            self.error.emit(friendly_device_error(e, "output"))
            return False
        old = self._play_token
        if old is not None and old != token:
            self.playback_finished.emit(old)
        self._play_token = token
        self._play_len = len(x) / sr
        self._play_t0 = time.monotonic()
        self.playback_started.emit(token)
        return True

    def stop_playback(self):
        if self._player is not None:
            try:
                self._player.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("player stop failed: %s", e)
        tok, self._play_token = self._play_token, None
        if tok is not None:
            self.playback_finished.emit(tok)

    def playback_pos(self) -> float:
        if self._player is None or self._play_token is None:
            return 0.0
        try:
            return float(self._player.position_s())
        except Exception:  # noqa: BLE001
            return time.monotonic() - self._play_t0

    # ------------------------------------------------------------------ poll
    _auto_stop_pending = False

    def _handle_dropout(self, why: str):
        """Mic stream died: stop, keep what was captured (if long enough), report once."""
        log.warning("input stream lost: %s", why)
        self.dropout_message = DROPOUT_MSG
        was_recording = self._recording
        self._drain()
        x = None
        if was_recording:
            self._recording = False
            if self._blocks_seen and self._take:
                x = np.concatenate(self._take).astype(np.float32)
            self._take = []
            self._detector = None
            self._auto_stop_pending = False
        rec, self._rec = self._rec, None
        if rec is not None:
            try:
                rest = rec.stop()
                if was_recording and x is None and not self._blocks_seen:
                    x = np.asarray(rest, dtype=np.float32).reshape(-1)
            except Exception as e:  # noqa: BLE001 - a dead stream may not stop cleanly
                log.warning("recorder stop after dropout failed: %s", e)
        self._queue.clear()
        self._input_failed = True   # don't reopen in a tight loop; set_monitoring(True)/start retry
        sr = self.samplerate if rec is None else int(getattr(rec, "samplerate", 48000) or 48000)
        self.level.emit(-120.0)
        self.input_state.emit(False)
        if was_recording:
            self.recording_changed.emit(False)
            if x is not None and len(x) >= MIN_DROPOUT_KEEP_S * sr:
                self.take_ready.emit(x, sr, True)
        self.input_lost.emit(DROPOUT_MSG)

    def _poll(self):
        if self._rec is not None:
            why = recorder_failure(self._rec)
            if why:
                self._handle_dropout(why)
        if self._rec is not None:
            peak = self._drain()
            try:
                lvl = float(self._rec.level_db())
            except Exception:  # noqa: BLE001
                lvl = 20 * np.log10(max(peak, 1e-6))
            self._last_level = lvl
            self.level.emit(lvl)
            if peak >= CLIP_LEVEL:
                self.clipped.emit()
            if self._recording:
                if self._auto_stop_pending:
                    self._auto_stop_pending = False
                    self.stop_recording(automatic=True)
                elif time.monotonic() - self._rec_started > MAX_TAKE_S:
                    self.stop_recording(automatic=True)
            elif (not self._ring and self._blocks_seen
                  and time.monotonic() - self._rec_opened_at > MONITOR_RESTART_S):
                # Legacy unbounded Recorder only: reopening is audible on Bluetooth headsets (profile
                # switch), so with a ring-buffer Recorder (audio_io max_seconds/keep_last_s) we never do it.
                self._close_input()
                self._sync_input()
        if self._play_token is not None:
            playing = True
            try:
                playing = bool(self._player.is_playing)
            except Exception:  # noqa: BLE001
                playing = time.monotonic() - self._play_t0 < self._play_len
            # grace period: some backends report not-playing for a moment right after play()
            if not playing and time.monotonic() - self._play_t0 > 0.15:
                tok, self._play_token = self._play_token, None
                self.playback_finished.emit(tok)
            else:
                self.playback_position.emit(self._play_token, self.playback_pos())

    def shutdown(self):
        self._timer.stop()
        self.cancel_recording()
        self._monitor_wanted = False
        self._close_input()
        self.stop_playback()
