"""Microphone recording and playback via sounddevice (PortAudio). No Qt.

All callbacks given by the caller run on non-GUI threads: ``Recorder.start(on_block)`` runs in the
PortAudio callback thread (keep it cheap), ``Player.play(on_done)`` runs on a short-lived helper
thread so it may safely call ``Player.stop()`` / ``Player.play()``.

Device loss (USB mic unplugged, output device disabled) never raises on the audio thread:
``Recorder.error`` / ``Recorder.is_alive()`` report it, and ``Player`` still fires ``on_done``
(with ``Player.error`` set) instead of hanging.
"""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

__all__ = [
    "AudioDeviceError", "list_input_devices", "list_output_devices", "default_input_device",
    "default_output_device", "display_name", "Recorder", "Player",
]

_WATCHDOG_S = 1.0          # no callbacks for this long while running = stream is dead
_MME_NAME_MAX = 31         # MME (WAVEINCAPS/WAVEOUTCAPS szPname) truncates names to 31 chars


class AudioDeviceError(RuntimeError):
    """Raised with a user-readable message when an audio device is missing, busy or unusable."""


def _sd():
    try:
        import sounddevice as sd  # noqa: PLC0415  (lazy: PortAudio may be missing)
        return sd
    except Exception as e:  # OSError when the PortAudio DLL is missing
        raise AudioDeviceError(f"Audio system unavailable (PortAudio could not be loaded): {e}") from e


def _friendly(e: Exception, what: str) -> AudioDeviceError:
    msg = str(e)
    low = msg.lower()
    if "unanticipated host error" in low or "device unavailable" in low or "busy" in low or "-9985" in low:
        hint = f"The {what} is busy or unavailable. Close other programs using it, or pick another device."
    elif "invalid sample rate" in low or "-9997" in low:
        hint = f"The {what} does not support this sample rate."
    elif "invalid device" in low or "-9996" in low or "no default" in low:
        hint = f"No usable {what} found. Check that one is connected and enabled in Windows sound settings."
    elif "invalid number of channels" in low or "-9998" in low:
        hint = f"The {what} does not support the requested channel count."
    else:
        hint = f"Could not open the {what}."
    return AudioDeviceError(f"{hint} ({msg})")


def _default_hostapi(sd) -> int | None:
    try:
        return int(sd.default.hostapi)
    except Exception:
        return None


def _full_names(sd, devs, kind: str) -> list[str]:
    """Device names of the WASAPI and DirectSound host APIs (not truncated like MME's)."""
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        apis = sd.query_hostapis()
    except Exception:
        return []
    want = {i for i, h in enumerate(apis) if any(w in str(h.get("name", "")) for w in ("WASAPI", "DirectSound"))}
    return [str(d["name"]) for d in devs if d.get("hostapi") in want and d.get(key, 0) > 0]


def display_name(name: str, candidates) -> str:
    """Better display name for a (possibly truncated) MME device name: if exactly one distinct
    longer candidate starts with it, that one; otherwise ``name`` unchanged."""
    if len(name) < _MME_NAME_MAX:
        return name
    longer = {c for c in candidates if len(c) > len(name) and c.startswith(name)}
    return longer.pop() if len(longer) == 1 else name


def _list(kind: str) -> list[tuple[int, str]]:
    """Devices of the default host API (avoids the 4x duplicates Windows exposes via MME/DS/WASAPI/WDM-KS).

    Indices are those of the default host API (MME on Windows - what gets opened); names MME
    truncated to 31 chars are replaced by the full WASAPI/DirectSound name when unambiguous."""
    try:
        sd = _sd()
        devs = sd.query_devices()
    except Exception:
        return []
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    api = _default_hostapi(sd)
    out = [(i, d["name"]) for i, d in enumerate(devs) if d.get(key, 0) > 0 and (api is None or d.get("hostapi") == api)]
    if not out:
        out = [(i, d["name"]) for i, d in enumerate(devs) if d.get(key, 0) > 0]
    if any(len(n) >= _MME_NAME_MAX for _, n in out):
        full = _full_names(sd, devs, kind)
        out = [(i, display_name(n, full)) for i, n in out]
    return out


def list_input_devices() -> list[tuple[int, str]]:
    return _list("input")


def list_output_devices() -> list[tuple[int, str]]:
    return _list("output")


def _default_device(kind: str) -> int | None:
    try:
        sd = _sd()
        idx = sd.default.device[0 if kind == "input" else 1]
        if idx is None or int(idx) < 0:
            return None
        d = sd.query_devices(int(idx))
        key = "max_input_channels" if kind == "input" else "max_output_channels"
        return int(idx) if d.get(key, 0) > 0 else None
    except Exception:
        return None


def default_input_device() -> int | None:
    return _default_device("input")


def default_output_device() -> int | None:
    return _default_device("output")


def _resolve(device, kind: str):
    """Return (device_index, info dict) or raise AudioDeviceError."""
    sd = _sd()
    what = "microphone" if kind == "input" else "audio output"
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        if device is None:
            device = _default_device(kind)
            if device is None:
                avail = _list(kind)
                if not avail:
                    raise AudioDeviceError(f"No {what} found. Connect one and check Windows sound settings.")
                device = avail[0][0]
        info = sd.query_devices(device, kind)
    except AudioDeviceError:
        raise
    except Exception as e:
        raise _friendly(e, what) from e
    if info.get(key, 0) <= 0:
        raise AudioDeviceError(f"The selected device '{info.get('name', device)}' has no {kind} channels.")
    idx = info.get("index", device)
    return (int(idx) if isinstance(idx, (int, np.integer)) else device), info


def _close_bounded(stream, timeout: float = 1.0):
    """Close (aborting if needed) a PortAudio stream without letting a dead driver hang us."""
    if stream is None:
        return

    def run():
        try:
            stream.abort()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    t = threading.Thread(target=run, name="audio-close", daemon=True)
    t.start()
    t.join(timeout)


class Recorder:
    """Records mono float32 from an input device. ``stop()`` returns the whole take.

    ``start(keep_last_s=N)`` turns the buffer into a ring that keeps only the last N seconds
    (bounded memory, for an always-open monitoring stream); ``snapshot(seconds)`` and
    ``clear()`` work while recording. All buffer access is thread safe. Stream death is never
    raised on the audio thread: it sets ``error`` and ``is_alive()`` turns False.
    """

    def __init__(self, device=None, samplerate=None):
        self.device, info = _resolve(device, "input")
        self.device_name = info.get("name", "")
        self.samplerate = int(samplerate or round(info.get("default_samplerate", 48000)) or 48000)
        self._max_ch = int(info.get("max_input_channels", 1))
        self._lock = threading.Lock()
        self._blocks: deque = deque()          # np.float32 blocks, oldest first
        self._n = 0                            # samples held in _blocks
        self._keep: int | None = None          # ring size in samples (None = keep everything)
        self._peaks: deque = deque()          # (monotonic time, peak) of recent blocks
        self._stream = None
        self._on_block = None
        self._last_take = np.zeros(0, dtype=np.float32)
        self._last_cb = 0.0                    # monotonic time of the last callback (or of start)
        self._stopping = False
        self.error: str | None = None          # why the stream died; None while healthy
        self.status_flags = 0                  # callbacks that carried any PortAudio status flag
        self.overflow_count = 0                # input overflows (PortAudio dropped samples)

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def _fail(self, msg: str):
        if self.error is None:
            self.error = msg

    def _callback(self, indata, frames, time_info, status):
        # PortAudio thread: must never raise. One allocation per block (the mono copy) plus
        # the deque/tuple bookkeeping; the ring trims old blocks so memory stays bounded.
        try:
            now = time.monotonic()
            self._last_cb = now
            if status:
                self.status_flags += 1
                if getattr(status, "input_overflow", False):
                    self.overflow_count += 1
            if indata.shape[1] == 1:
                mono = indata[:, 0].copy()
            else:
                mono = np.mean(indata, axis=1, dtype=np.float32)
            pk = float(max(mono.max(), -mono.min())) if mono.size else 0.0
            with self._lock:
                self._blocks.append(mono)
                self._n += mono.size
                keep = self._keep
                if keep is not None:
                    while self._blocks and self._n - self._blocks[0].size >= keep:
                        self._n -= self._blocks.popleft().size
                self._peaks.append((now, pk))
                while self._peaks and now - self._peaks[0][0] > 0.15:
                    self._peaks.popleft()
            cb = self._on_block
            if cb is not None:
                try:
                    cb(mono)
                except Exception:
                    pass                        # never kill the audio thread
        except Exception as e:
            self._fail(f"Microphone processing failed: {e}")

    def _on_finished(self):
        # PortAudio finished the stream; if we did not ask for that, the device went away.
        if not self._stopping:
            self._fail("The microphone stopped delivering audio (unplugged or disabled?).")

    def start(self, on_block=None, keep_last_s=None):
        """Open the input stream. ``keep_last_s``: retain only the last N seconds (ring buffer)."""
        if self._stream is not None:
            return
        sd = _sd()
        with self._lock:
            self._blocks.clear()
            self._n = 0
            self._peaks.clear()
            self._keep = (max(1, int(round(float(keep_last_s) * self.samplerate)))
                          if keep_last_s is not None else None)
        self._on_block = on_block
        self.error = None
        self._stopping = False
        self.status_flags = 0
        self.overflow_count = 0
        last_err = None
        for ch in dict.fromkeys([1, min(2, self._max_ch)]):
            stream = None
            try:
                stream = sd.InputStream(device=self.device, samplerate=self.samplerate, channels=ch,
                                        dtype="float32", blocksize=int(self.samplerate * 0.02),
                                        callback=self._callback, finished_callback=self._on_finished)
                self._last_cb = time.monotonic()
                self._stream = stream
                stream.start()
                self._last_cb = time.monotonic()
                return
            except Exception as e:
                last_err = e
                self._stream = None
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
        raise _friendly(last_err, "microphone") from last_err

    def is_alive(self) -> bool:
        """True while recording on a healthy stream. False (and ``error`` set) when PortAudio
        stopped the stream, the callback failed, or no callback arrived for > 1 s."""
        stream = self._stream
        if stream is None or self.error is not None:
            return False
        try:
            active = bool(stream.active)
        except Exception as e:
            self._fail(f"The microphone stream failed: {e}")
            return False
        if not active:
            self._fail("The microphone stream stopped (device unplugged or disabled?).")
            return False
        if time.monotonic() - self._last_cb > _WATCHDOG_S:
            self._fail("No audio from the microphone for over a second (unplugged or in use by another program?).")
            return False
        return True

    def _tail(self, seconds) -> np.ndarray:
        """The whole buffer (``seconds=None``) or its last ``seconds``. Caller holds ``_lock``."""
        if not self._blocks:
            return np.zeros(0, dtype=np.float32)
        if seconds is None:
            return np.concatenate(list(self._blocks))
        want = max(0, int(round(float(seconds) * self.samplerate)))
        if want == 0:
            return np.zeros(0, dtype=np.float32)
        parts, got = [], 0
        for b in reversed(self._blocks):
            parts.append(b)
            got += b.size
            if got >= want:
                break
        out = np.concatenate(parts[::-1])
        return out[-want:] if out.size > want else out

    def snapshot(self, seconds) -> np.ndarray:
        """Copy of the last ``seconds`` of audio (less if not that much is buffered); keeps recording."""
        with self._lock:
            return self._tail(seconds).astype(np.float32, copy=True)

    def clear(self):
        """Drop everything buffered so far (recording continues)."""
        with self._lock:
            self._blocks.clear()
            self._n = 0

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is None:
            return self._last_take.copy()
        self._stopping = True
        if self.error is not None:
            _close_bounded(stream)             # dead device: never hang in stop()
        else:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        with self._lock:
            take = self._tail(None).astype(np.float32, copy=True)
            self._blocks.clear()
            self._n = 0
            self._peaks.clear()
        self._on_block = None
        self._last_take = take
        return take.copy()

    def level_db(self) -> float:
        """Peak dBFS over the last ~150 ms; -120 when silent / not recording."""
        now = time.monotonic()
        with self._lock:
            if self._stream is None:
                return -120.0
            pk = max((p for t, p in self._peaks if now - t <= 0.15), default=0.0)
        return float(max(-120.0, 20.0 * np.log10(pk))) if pk > 1e-6 else -120.0

    def __del__(self):
        try:
            if self._stream is not None:
                self._stopping = True
                self._stream.abort()
                self._stream.close()
        except Exception:
            pass


class _Session:
    __slots__ = ("data", "pos", "sr_src", "ratio", "on_done", "done_fired", "stream", "finished", "last_cb")

    def __init__(self, data, sr_src, ratio, on_done):
        self.data = data
        self.pos = 0
        self.sr_src = sr_src
        self.ratio = ratio            # device frames per source frame
        self.on_done = on_done
        self.done_fired = False
        self.stream = None
        self.finished = False
        self.last_cb = time.monotonic()


class Player:
    """Non-blocking mono playback. ``on_done`` is called exactly once per ``play()`` - when playback
    finishes or is stopped (by ``stop()`` or a newer ``play()``) - from a helper thread.

    If the output device disappears mid-playback (the stream stops or stalls for > 1 s) the
    session is finished anyway, ``on_done`` still fires and ``error`` describes what happened."""

    def __init__(self, device=None):
        self.device, info = _resolve(device, "output")
        self.device_name = info.get("name", "")
        self._default_sr = int(round(info.get("default_samplerate", 48000)) or 48000)
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self.error: str | None = None

    @property
    def is_playing(self) -> bool:
        s = self._session
        return s is not None and not s.finished

    def position_s(self) -> float:
        s = self._session
        if s is None:
            return 0.0
        return min(s.pos, s.data.size) / s.ratio / s.sr_src

    def _make_callback(self, sess: _Session):
        sd = _sd()
        stop_exc = sd.CallbackStop

        def cb(outdata, frames, time_info, status):
            sess.last_cb = time.monotonic()
            try:
                d = sess.data
                p = sess.pos
                chunk = d[p:p + frames]
                n = chunk.size
                outdata[:n, 0] = chunk
                if n < frames:
                    outdata[n:, 0] = 0.0
                if outdata.shape[1] > 1:
                    outdata[:, 1:] = outdata[:, :1]
                sess.pos = p + n
                done = sess.pos >= d.size
            except Exception:
                done = True
            if done:
                raise stop_exc

        return cb

    def _watch(self, sess: _Session):
        """Watchdog: finish the session if the stream stops or stalls (device gone)."""
        while not sess.finished:
            time.sleep(0.05)
            if sess.finished:
                return
            stream = sess.stream
            reason = None
            try:
                if stream is not None and not stream.active and sess.pos < sess.data.size:
                    reason = "The audio output stopped unexpectedly (device unplugged or disabled?)."
            except Exception as e:
                reason = f"The audio output failed: {e}"
            if reason is None and time.monotonic() - sess.last_cb > _WATCHDOG_S:
                if sess.pos < sess.data.size:
                    reason = "The audio output stopped responding (device unplugged or disabled?)."
                else:
                    reason = ""              # all data handed over, driver never reported finished
            if reason is not None:
                if reason:
                    self.error = reason
                self._finish(sess)
                return

    def _finish(self, sess: _Session):
        """Mark a session finished and fire its on_done once (on a helper thread)."""
        with self._lock:
            if sess.done_fired:
                return
            sess.done_fired = True
            sess.finished = True
            if self._session is sess:
                self._session = None
        stream = sess.stream
        sess.stream = None

        def run():
            _close_bounded(stream)
            if sess.on_done is not None:
                try:
                    sess.on_done()
                except Exception:
                    pass

        threading.Thread(target=run, name="player-done", daemon=True).start()

    def play(self, x, sr, on_done=None):
        self.stop()
        self.error = None
        sd = _sd()
        a = np.nan_to_num(np.asarray(x, dtype=np.float32).reshape(-1))
        a = np.clip(a, -1.0, 1.0)
        sr = max(1, int(sr))
        if a.size == 0:                       # nothing to play: just report done
            s = _Session(a, sr, 1.0, on_done)
            self._finish(s)
            return
        sess = None
        last_err = None
        for dev_sr in dict.fromkeys([sr, self._default_sr]):
            if dev_sr == sr:
                data = np.ascontiguousarray(a)
            else:
                from . import dsp  # noqa: PLC0415
                data = dsp.resample(a, sr, dev_sr)
            s = _Session(data, sr, dev_sr / sr, on_done)
            for ch in (1, 2):
                stream = None
                try:
                    stream = sd.OutputStream(device=self.device, samplerate=dev_sr, channels=ch, dtype="float32",
                                             callback=self._make_callback(s),
                                             finished_callback=lambda s=s: self._finish(s))
                    s.stream = stream
                    with self._lock:
                        self._session = s
                    s.last_cb = time.monotonic()
                    stream.start()
                    s.last_cb = time.monotonic()
                    sess = s
                    break
                except Exception as e:
                    last_err = e
                    with self._lock:
                        if self._session is s:
                            self._session = None
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                    s.stream = None
            if sess is not None:
                break
        if sess is None:
            raise _friendly(last_err, "audio output") from last_err
        threading.Thread(target=self._watch, args=(sess,), name="player-watchdog", daemon=True).start()

    def stop(self):
        with self._lock:
            sess = self._session
        if sess is None:
            return
        stream = sess.stream
        if stream is not None:
            try:
                stream.abort()   # finished_callback -> _finish (idempotent)
            except Exception:
                pass
        self._finish(sess)

    def __del__(self):
        try:
            s = self._session
            if s is not None and s.stream is not None:
                s.stream.abort()
                s.stream.close()
        except Exception:
            pass
