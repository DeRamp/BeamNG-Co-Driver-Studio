"""Shared helpers for the project/exporter/sample_calls tests."""
import numpy as np

SR = 48000


def tone(freq=440.0, dur=0.4, lead=0.2, tail=0.3, sr=SR, amp=0.5):
    """Silence – sine burst – silence, like a short spoken phrase."""
    t = np.arange(int(dur * sr)) / sr
    burst = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.concatenate([np.zeros(int(lead * sr), np.float32), burst,
                           np.zeros(int(tail * sr), np.float32)])
