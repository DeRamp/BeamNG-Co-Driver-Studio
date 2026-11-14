import threading
import time
import numpy as np


class AudioDeviceError(RuntimeError):
    pass


def list_input_devices():
    return [(0, "Fake Microphone (USB Headset)"), (1, "Fake Line In")]


def list_output_devices():
    return [(2, "Fake Speakers"), (3, "Fake Headphones")]


def default_input_device():
    return 0


class Recorder:
    def __init__(self, device=None, samplerate=None):
        self.samplerate = samplerate or 48000
        self.is_recording = False
        self._blocks, self._lvl, self._t = [], -120.0, None

    def start(self, on_block=None):
        self.is_recording = True
        self._on_block = on_block

        def run():
            rng = np.random.default_rng(1)
            k = 0
            while self.is_recording:
                b = rng.normal(0, 0.003, 1024).astype(np.float32)
                if 10 < k % 100 < 40:
                    b += 0.3 * np.sin(np.arange(1024) * 0.05).astype(np.float32)
                k += 1
                self._blocks.append(b)
                self._lvl = 20 * np.log10(max(1e-6, float(np.max(np.abs(b)))))
                if self._on_block:
                    self._on_block(b)
                time.sleep(1024 / self.samplerate)
        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()

    def stop(self):
        self.is_recording = False
        if self._t:
            self._t.join()
        return np.concatenate(self._blocks) if self._blocks else np.zeros(0, np.float32)

    def level_db(self):
        return self._lvl if self.is_recording else -120.0


class Player:
    def __init__(self, device=None):
        self.is_playing = False
        self._t0, self._dur, self._gen = 0.0, 0.0, 0

    def play(self, x, sr, on_done=None):
        self._gen += 1
        gen = self._gen
        self._t0, self._dur, self.is_playing = time.monotonic(), len(x) / sr, True

        def run():
            time.sleep(self._dur)
            if gen == self._gen:
                self.is_playing = False
                if on_done:
                    on_done()
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._gen += 1
        self.is_playing = False

    def position_s(self):
        return min(self._dur, time.monotonic() - self._t0) if self.is_playing else 0.0
