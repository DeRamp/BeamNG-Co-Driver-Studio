"""Run blocking work (rendering, export) on QThreadPool; results come back as GUI-thread signals."""
from __future__ import annotations

import sys
import traceback
from typing import Callable

import shiboken6
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .errors import log


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object, str)      # exception, formatted traceback
    progress = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn, signals, pass_progress):
        super().__init__()
        self.fn, self.signals, self.pass_progress = fn, signals, pass_progress
        self.setAutoDelete(True)

    @Slot()
    def run(self):
        try:
            if self.pass_progress:
                res = self.fn(self.signals.progress.emit)
            else:
                res = self.fn()
        except BaseException as e:  # noqa: BLE001 - reported to GUI
            tb = traceback.format_exc()
            log.error("Background job failed:\n%s", tb)
            self.signals.failed.emit(e, tb)
        else:
            self.signals.done.emit(res)


_live: set = set()   # keep signal objects alive until their job finished


def alive(*objs) -> bool:
    """True if every given QObject still exists on the C++ side (None entries are ignored)."""
    for o in objs:
        if o is not None and isinstance(o, QObject) and not shiboken6.isValid(o):
            return False
    return True


def _guard(cb, owner):
    if cb is None or owner is None:
        return cb

    def guarded(*a):
        if alive(owner):
            cb(*a)
    return guarded


def run_async(fn: Callable, on_done: Callable | None = None, on_error: Callable | None = None,
              on_progress: Callable | None = None, pool: QThreadPool | None = None,
              owner: QObject | None = None) -> _Signals:
    """Run fn() (or fn(progress_emit) when on_progress is given) on a worker thread.

    Callbacks are invoked on the GUI thread (signals are queued across threads).
    on_error(exc, traceback_str); default: re-raise via the global exception hook.
    owner: callbacks are skipped once this QObject was destroyed (e.g. project closed mid-job).
    """
    on_done, on_error, on_progress = (_guard(c, owner) for c in (on_done, on_error, on_progress))
    sig = _Signals()
    _live.add(sig)

    def finish(*_):
        _live.discard(sig)

    if on_done:
        sig.done.connect(on_done)
    if on_progress:
        sig.progress.connect(on_progress)
    if on_error:
        sig.failed.connect(on_error)
    else:
        sig.failed.connect(lambda e, tb: sys.excepthook(type(e), e, e.__traceback__))
    sig.done.connect(finish)
    sig.failed.connect(finish)
    (pool or QThreadPool.globalInstance()).start(_Job(fn, sig, on_progress is not None))
    return sig


def wait_idle(timeout_ms: int = 10000) -> bool:
    """Testing helper: wait for all background jobs."""
    return QThreadPool.globalInstance().waitForDone(timeout_ms)
