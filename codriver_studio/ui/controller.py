"""Shared state for one open project: the Project, the AudioService, change signals, render cache."""
from __future__ import annotations

import collections
import contextlib
import json

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from .errors import log
from .workers import run_async

RENDER_CACHE = 48


class TakeGone(LookupError):
    """The take a render was requested for no longer exists."""


class Controller(QObject):
    phraseChanged = Signal(str)      # takes / trims / enable state of one phrase changed
    projectChanged = Signal()        # effects / pack info / many phrases changed
    progressChanged = Signal(int, int)
    openPhraseRequested = Signal(str, int)   # (phrase_id, take_index or -1) → Phrases page take panel
    recordRequested = Signal(str)            # → Record page on that phrase
    status = Signal(str)                     # short status-bar message

    def __init__(self, project, audio, parent=None):
        super().__init__(parent)
        self.project = project
        self.audio = audio
        self._cache: collections.OrderedDict = collections.OrderedDict()
        self.audio.error.connect(self._audio_error)

    # ------------------------------------------------------------------ notifications
    @property
    def read_only(self) -> bool:
        return bool(getattr(self.project, "read_only", False))

    def notify(self, phrase_id: str | None = None):
        merged = int(getattr(self.project, "last_merge_added", 0) or 0)
        if phrase_id is None:
            self.projectChanged.emit()
        else:
            self.phraseChanged.emit(phrase_id)
        if merged > 0 and phrase_id is not None:
            # the last save merged takes another instance had written: every list may be stale
            self.project.last_merge_added = 0
            self._cache.clear()
            self.projectChanged.emit()
            self.status.emit(f"Merged {merged} take{'s' if merged != 1 else ''} saved by another "
                             "Co-Driver Studio window.")
        self.progressChanged.emit(*self.progress())

    def progress(self):
        try:
            return tuple(self.project.progress())
        except Exception as e:  # noqa: BLE001
            log.warning("progress failed: %s", e)
            return 0, len(self.project.catalog.phrases)

    def save(self):
        if self.read_only:
            self.status.emit("Read-only project: changes are not saved.")
            return
        try:
            self.project.save()
        except Exception as e:  # noqa: BLE001
            log.exception("save failed")
            QMessageBox.warning(None, "Couldn't save", f"Saving the project failed:\n{e}")

    def _audio_error(self, msg):
        QMessageBox.warning(None, "Audio device", msg)

    # ------------------------------------------------------------------ helpers
    def phrase(self, pid):
        return self.project.catalog.get(pid)

    def takes(self, pid):
        try:
            return list(self.project.takes(pid))
        except Exception:  # noqa: BLE001
            return []

    def first_enabled(self, pid) -> int | None:
        for i, t in enumerate(self.takes(pid)):
            if t.enabled and not getattr(t, "missing", False):
                return i
        return None

    def effects_key(self) -> str:
        try:
            return json.dumps(self.project.effects.to_dict(), sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return repr(self.project.effects)

    def _take_key(self, pid, idx, out_sr):
        t = self.takes(pid)[idx]
        return (pid, t.file, round(t.trim_start, 5), round(t.trim_end, 5), self.effects_key(), out_sr)

    def render_async(self, pid, idx, on_done, out_sr=None, on_error=None, owner=None):
        """Render a take (trim + effects) off the GUI thread; on_done(audio, sr) on GUI thread.

        The take is identified by its FILE at request time; the worker looks that file up again
        (under project.lock) so a take deleted/reordered meanwhile can never render the wrong one.
        owner: a QObject whose destruction cancels the callbacks."""
        try:
            key = self._take_key(pid, idx, out_sr)
        except IndexError:
            return
        if key in self._cache:
            self._cache.move_to_end(key)
            y, sr = self._cache[key]
            on_done(y, sr)
            return
        proj = self.project
        file = key[1]

        def work():
            lock = getattr(proj, "lock", None)
            with (lock if lock is not None else contextlib.nullcontext()):
                # held for the whole render (RLock, re-entrant): the index stays valid throughout
                cur = next((i for i, t in enumerate(proj.takes(pid)) if t.file == file), None)
                if cur is None:
                    raise TakeGone(f"the take {file} was deleted")
                return proj.render_take(pid, cur, out_sr) if out_sr else proj.render_take(pid, cur)

        def done(res):
            y, sr = res
            self._cache[key] = (y, sr)
            while len(self._cache) > RENDER_CACHE:
                self._cache.popitem(last=False)
            on_done(y, sr)

        def failed(e, tb):
            if isinstance(e, TakeGone):
                return   # take deleted while rendering: nothing to play, not an error
            msg = f"Couldn't render “{pid}”: {e}"
            self.status.emit(msg)
            if on_error:
                on_error(e)
            else:
                QMessageBox.warning(None, "Playback", msg)

        run_async(work, _owned(done, owner), _owned(failed, owner), owner=self)

    def play_phrase(self, pid, token="phrase"):
        """Play the first enabled take of a phrase, rendered with effects."""
        idx = self.first_enabled(pid)
        if idx is None:
            self.status.emit("Nothing to play yet: record this phrase first.")
            return False
        self.render_async(pid, idx, lambda y, sr: self.audio.play(y, sr, token))
        return True


def _owned(cb, owner):
    if owner is None:
        return cb
    from .workers import alive

    def guarded(*a):
        if alive(owner):
            cb(*a)
    return guarded
