"""Pack & Export page: pack info, timing gaps, export zip / install to BeamNG."""
from __future__ import annotations

import inspect
import os
import pathlib
import re
import subprocess
import sys
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressDialog, QPushButton,
                               QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from ... import exporter
from .. import settings as st
from .. import theme
from ..errors import log
from ..widgets import ElidedLabel, FlowLayout, hint_label, page_header, wrappable_path
from ..workers import run_async

try:
    from ...project import BUILTIN_DIALECTS, BUILTIN_LANGUAGES, dialects_for, language_id
except ImportError:   # pragma: no cover - only with very old/fake project modules
    BUILTIN_LANGUAGES = {"english": "English"}
    BUILTIN_DIALECTS = {"en-US": ("english", "English (US)"), "en-GB": ("english", "English (UK)")}

    def language_id(text):
        t = (text or "").strip()
        return "english" if t.lower() in ("english",) else t

    def dialects_for(language):
        return [(c, l) for c, (o, l) in BUILTIN_DIALECTS.items() if o == language_id(language)]
RATES = [24000, 44100, 48000]
GAPS = [
    ("link_word_gap_ms", "After “into” / “and”", -200, 200,
     "Gap after a link word. Negative values overlap the words slightly for a fast, natural flow."),
    ("corner_gap_ms", "Inside a corner call", -200, 200,
     "Between the parts of one corner, e.g. “three left” → “long” → “tightens”."),
    ("phrase_gap_ms", "Between other words", -200, 200,
     "Between all other words in one call."),
    ("pacenote_gap_ms", "Between calls", -100, 400,
     "Pause between two separate calls."),
]
RESTART_NOTE = ("Restart BeamNG (or press <b>Ctrl+L</b> in game) to load the new pack. "
                "Then select it in a rally mission's start screen → <b>Co-driver</b>.")


def open_in_explorer(path) -> None:
    path = pathlib.Path(path)
    try:
        if sys.platform == "win32":
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except Exception as e:  # noqa: BLE001
        log.warning("open folder failed: %s", e)


def _slug(text):
    s = re.sub(r"[^a-z0-9]", "_", str(text).lower())
    return re.sub(r"_+", "_", s).strip("_") or "codriver"


class ExportPage(QWidget):
    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self.interactive = True      # tests switch dialogs off
        self.last_report = None
        self.last_error = None
        self._busy = False
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)
        outer.addWidget(page_header("Pack & Export", "Name your co-driver, fine-tune the timing and build the mod "
                                    "for BeamNG.drive."))

        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 6, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)

        # ---- pack info
        info = QGroupBox("Pack")
        f = QFormLayout(info)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(8)
        self.persona = QLineEdit()
        self.persona.setPlaceholderText("Shown in the game's co-driver list")
        self.persona.editingFinished.connect(self._persona_changed)
        self.pack_id = ElidedLabel("", mode=Qt.ElideMiddle)
        self.pack_id.setProperty("hint", True)
        self.author = QLineEdit()
        self.author.setPlaceholderText("Your name (optional)")
        self.author.editingFinished.connect(lambda: self._set("author", self.author.text().strip()))
        # Language: builtin ones map to the game's own translations; or type any name ("Русский").
        self.language = QComboBox()
        self.language.setEditable(True)
        self.language.setInsertPolicy(QComboBox.NoInsert)
        for key, label in BUILTIN_LANGUAGES.items():
            self.language.addItem(label, key)
        self.language.lineEdit().setPlaceholderText("Pick one or type your own")
        self.language.setToolTip("Shown in BeamNG's co-driver list. Pick a language or type any name, "
                                 "e.g. Deutsch or Русский.")
        self.language.activated.connect(self._language_changed)
        self.language.lineEdit().editingFinished.connect(self._language_changed)
        # Dialect: suggestions follow the language; free text allowed; empty = none
        self.dialect = QComboBox()
        self.dialect.setEditable(True)
        self.dialect.setInsertPolicy(QComboBox.NoInsert)
        self.dialect.lineEdit().setPlaceholderText("Optional, e.g. de-AT")
        self.dialect.activated.connect(self._dialect_changed)
        self.dialect.lineEdit().editingFinished.connect(self._dialect_changed)
        self.rate = QComboBox()
        for r in RATES:
            self.rate.addItem(f"{r / 1000:g} kHz" + ("  (recommended)" if r == 48000 else ""), r)
        self.rate.currentIndexChanged.connect(lambda _: self._set("export_sample_rate", self.rate.currentData()))
        self.casual = QCheckBox("Also include a “casual” style")
        self.casual.setToolTip("A second, less detailed style that reuses the same recordings")
        self.casual.toggled.connect(lambda v: self._set("include_casual", v))
        f.addRow("Co-driver name", self.persona)
        f.addRow("", self.pack_id)
        f.addRow("Author", self.author)
        f.addRow("Language", self.language)
        f.addRow("Dialect", self.dialect)
        f.addRow("", hint_label("Shown in BeamNG's co-driver list. You can record the calls in any language; "
                                "the script on the Record page stays in English, so translate as you read."))
        f.addRow("Sample rate", self.rate)
        f.addRow("", self.casual)
        f.addRow("", hint_label("The casual style uses fewer words per call. Same voice, no extra recording."))
        self._info_box = info

        # ---- gaps
        gaps = QGroupBox("Timing")
        g = QGridLayout(gaps)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(2)
        g.setColumnStretch(2, 1)
        self.gap_spins: dict[str, QSpinBox] = {}
        r = 0
        for field, label, lo, hi, help_ in GAPS:
            lab = QLabel(label)
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(10)
            sp.setSuffix(" ms")
            sp.setToolTip(help_)
            sp.valueChanged.connect(lambda v, k=field: self._set(k, int(v)))
            g.addWidget(lab, r, 0)
            g.addWidget(sp, r, 1)
            # the explanation goes under the control (full width), not in a squeezed third column
            g.addWidget(hint_label(help_), r + 1, 0, 1, 3)
            self.gap_spins[field] = sp
            g.setRowMinimumHeight(r + 2, 8)
            r += 3
        g.addWidget(hint_label("Tip: try changes on the Sample Calls page. They apply instantly."), r, 0, 1, 3)
        self._gaps_box = gaps

        # ---- export
        ex = QGroupBox("Build the mod")
        ev = QVBoxLayout(ex)
        ev.setSpacing(8)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        ev.addWidget(self.summary)
        row = FlowLayout(spacing=8, vspacing=6)
        self.btn_install = QPushButton("Install to BeamNG")
        self.btn_install.setProperty("primary", True)
        self.btn_install.clicked.connect(self.install)
        self.btn_export = QPushButton("Export zip…")
        self.btn_export.clicked.connect(self.export_dialog)
        self.btn_mods = QPushButton("Choose mods folder…")
        self.btn_mods.setToolTip("If you moved your BeamNG user folder, point this at its 'mods' folder")
        self.btn_mods.clicked.connect(self.choose_mods_dir)
        row.addWidget(self.btn_install)
        row.addWidget(self.btn_export)
        row.addWidget(self.btn_mods)
        ev.addLayout(row)
        self.mods_label = QLabel("")
        self.mods_label.setProperty("hint", True)
        self.mods_label.setWordWrap(True)
        ev.addWidget(self.mods_label)

        # result card
        self.result = QFrame()
        self.result.setObjectName("Card")
        self.result.setStyleSheet(f"#Card {{ border-color: {theme.GREEN}; }}")
        rl = QVBoxLayout(self.result)
        rl.setContentsMargins(16, 12, 16, 12)
        self.result_title = QLabel("")
        self.result_title.setStyleSheet(f"font-size: 13pt; font-weight: 600; color: {theme.GREEN};")
        self.result_text = QLabel("")
        self.result_text.setWordWrap(True)
        self.result_text.setTextFormat(Qt.RichText)
        self.result_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_open = QPushButton("Open folder")
        self.btn_open.clicked.connect(lambda: self.last_report and open_in_explorer(self.last_report.zip_path))
        self.result_title.setWordWrap(True)
        rr = QHBoxLayout()
        rr.addWidget(self.result_title, 1)
        rr.addWidget(self.btn_open, 0, Qt.AlignTop)
        rl.addLayout(rr)
        rl.addWidget(self.result_text)
        self.result.hide()
        ev.addWidget(self.result)
        self._build_box = ex
        self._grid = grid
        self._narrow = None
        self._arrange(narrow=False)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        ctl.progressChanged.connect(self._progress_changed)
        self.load_from_project()

    def _arrange(self, narrow: bool):
        """Pack and Timing side by side, or stacked when the page is too narrow for both."""
        if narrow == self._narrow:
            return
        self._narrow = narrow
        g = self._grid
        for w in (self._info_box, self._gaps_box, self._build_box):
            g.removeWidget(w)
        if narrow:
            g.addWidget(self._info_box, 0, 0)
            g.addWidget(self._gaps_box, 1, 0)
            g.addWidget(self._build_box, 2, 0)
            g.setColumnStretch(1, 0)
        else:
            g.addWidget(self._info_box, 0, 0)
            g.addWidget(self._gaps_box, 0, 1)
            g.addWidget(self._build_box, 1, 0, 1, 2)
            g.setColumnStretch(1, 1)
        g.setColumnStretch(0, 1)
        for r in range(4):
            g.setRowStretch(r, 0)
        g.setRowStretch(3 if narrow else 2, 1)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # side by side needs both boxes at a comfortable width (font based, not pixels)
        need = 2 * self.fontMetrics().horizontalAdvance("M") * 30
        self._arrange(narrow=self.width() < need)

    def set_read_only(self, ro: bool):
        for w in (self.persona, self.author, self.language, self.dialect, self.rate, self.casual,
                  *self.gap_spins.values()):
            w.setEnabled(not ro)

    def _progress_changed(self, *_):
        self._update_summary()

    # ------------------------------------------------------------------ pack fields
    def load_from_project(self):
        self._loading = True
        pk = self.ctl.project.pack
        self.persona.setText(pk.persona)
        self.author.setText(getattr(pk, "author", ""))
        self._show_language(getattr(pk, "language", "english"), getattr(pk, "dialect", "en-US"))
        rate = int(getattr(pk, "export_sample_rate", 48000))
        i = self.rate.findData(rate)
        if i < 0:
            self.rate.addItem(f"{rate / 1000:g} kHz", rate)
            i = self.rate.count() - 1
        self.rate.setCurrentIndex(i)
        self.casual.setChecked(bool(getattr(pk, "include_casual", True)))
        for k, sp in self.gap_spins.items():
            sp.setValue(int(getattr(pk, k, 0)))
        self._loading = False
        self._update_pack_id()
        self._update_summary()
        self._update_mods()

    def pack_dirname(self) -> str:
        pk = self.ctl.project.pack
        d = getattr(pk, "dirname", None)
        if d:
            return str(d)
        return _slug(getattr(pk, "dirname_slug", "") or pk.persona)

    def _update_pack_id(self):
        self.pack_id.setText(f"Pack ID: {self.pack_dirname()}")
        self.pack_id.setToolTip(f"{self.pack_dirname()}\n\nInternal folder name of the pack in the game. Fixed "
                                "once created so re-exports replace the old version.")

    def _set(self, key, value):
        if self._loading:
            return
        pk = self.ctl.project.pack
        if getattr(pk, key, None) == value:
            return
        setattr(pk, key, value)
        self.ctl.save()

    # ------------------------------------------------------------------ language / dialect
    def _show_language(self, language: str, dialect: str):
        """Put the pack's language + dialect into the two combos without saving anything."""
        was, self._loading = self._loading, True
        try:
            lang = language_id(language)
            i = self.language.findData(lang)
            if i >= 0:
                self.language.setCurrentIndex(i)
            else:
                self.language.setCurrentIndex(-1)
                self.language.setEditText(lang)
            self.dialect.clear()
            for code, label in dialects_for(lang):
                self.dialect.addItem(f"{label}  ·  {code}", code)
            j = self.dialect.findData(dialect)
            if j >= 0:
                self.dialect.setCurrentIndex(j)
            else:
                self.dialect.setCurrentIndex(-1)
                self.dialect.setEditText(dialect or "")
        finally:
            self._loading = was

    def _combo_value(self, combo: QComboBox) -> str:
        """Data of the chosen item, or the typed text when it doesn't match an item's label."""
        text = combo.currentText().strip()
        i = combo.currentIndex()
        if i >= 0 and combo.itemText(i) == combo.currentText():
            return combo.itemData(i)
        j = combo.findText(text, Qt.MatchFixedString)
        return combo.itemData(j) if j >= 0 else text

    def _language_changed(self, *_):
        if self._loading:
            return
        pk = self.ctl.project.pack
        lang = language_id(self._combo_value(self.language))
        if not lang:                                  # emptied: put the current value back
            self._show_language(pk.language, pk.dialect)
            return
        if lang == pk.language:
            return
        # keep the dialect only if it still fits the new language
        options = [c for c, _ in dialects_for(lang)]
        dialect = pk.dialect if (pk.dialect in options or (not options and pk.dialect not in BUILTIN_DIALECTS)) \
            else (options[0] if options else "")
        pk.language, pk.dialect = lang, dialect
        self.ctl.save()
        self._show_language(lang, dialect)

    def _dialect_changed(self, *_):
        if self._loading:
            return
        self._set("dialect", self._combo_value(self.dialect))

    def _persona_changed(self):
        name = self.persona.text().strip()
        if not name:
            self.persona.setText(self.ctl.project.pack.persona)
            return
        pk = self.ctl.project.pack
        if name == pk.persona:
            return
        if hasattr(pk, "set_persona"):
            pk.set_persona(name)
        else:
            pk.persona = name
        self.ctl.save()
        self._update_pack_id()
        self.ctl.projectChanged.emit()

    # ------------------------------------------------------------------ summary / mods dir
    def _update_summary(self):
        done, total = self.ctl.progress()
        if done == total:
            self.summary.setText(f"<span style='color:{theme.GREEN}'>All {total} phrases recorded.</span> "
                                 "Ready to go!")
        elif done == 0:
            self.summary.setText(f"<span style='color:{theme.AMBER}'>Nothing recorded yet.</span> "
                                 "Record some phrases first.")
        else:
            self.summary.setText(f"{done} / {total} phrases recorded. <span style='color:{theme.AMBER}'>"
                                 f"{total - done} missing</span> phrases will be silent in the game.")
        self.btn_export.setEnabled(done > 0 and not self._busy)
        self._update_mods()

    def mods_dir(self):
        custom = st.get("export/mods_dir")
        if custom and pathlib.Path(custom).is_dir():
            return pathlib.Path(custom)
        try:
            return exporter.beamng_mods_dir()
        except Exception:  # noqa: BLE001
            return None

    def _update_mods(self):
        d = self.mods_dir()
        done, _ = self.ctl.progress()
        if d is None:
            self.btn_install.setEnabled(False)
            self.btn_install.setToolTip("BeamNG's mods folder wasn't found. Use “Choose mods folder…” "
                                        "or export a zip and copy it yourself.")
            self.mods_label.setText("BeamNG mods folder not found. Is the game installed? You can still export "
                                    "a zip, or choose the folder yourself.")
        else:
            self.btn_install.setEnabled(done > 0 and not self._busy)
            self.btn_install.setToolTip(f"Write {self.zip_name()} straight into {d}")
            self.mods_label.setText(f"Mods folder: {d}")

    def choose_mods_dir(self):
        start = str(self.mods_dir() or st.documents_dir())
        d = QFileDialog.getExistingDirectory(self, "BeamNG mods folder", start)
        if d:
            st.put("export/mods_dir", d)
            self._update_mods()

    def zip_name(self) -> str:
        pk = self.ctl.project.pack
        n = getattr(pk, "zip_name", None)
        return str(n) if n else f"codriver_{_slug(pk.persona)}.zip"

    # ------------------------------------------------------------------ export flow
    def export_dialog(self):
        last = st.get("export/last_dir") or st.desktop_dir()
        path, _ = QFileDialog.getSaveFileName(self, "Export voice pack", str(pathlib.Path(last) / self.zip_name()),
                                              "BeamNG mod (*.zip)")
        if path:
            st.put("export/last_dir", str(pathlib.Path(path).parent))
            self.export_to(path)

    def install(self):
        d = self.mods_dir()
        if d is None:
            return
        running = False
        try:
            running = bool(getattr(exporter, "beamng_running", lambda: False)())
        except Exception:  # noqa: BLE001
            pass
        if running and self.interactive:
            r = QMessageBox.warning(self, "BeamNG is running",
                                    "BeamNG.drive is running. The game may lock the old mod file so it can't "
                                    "be replaced.\n\nClose the game first, or try anyway?",
                                    QMessageBox.Retry | QMessageBox.Ignore | QMessageBox.Cancel, QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Retry:
                return self.install()
        self.export_to(d / self.zip_name(), installed=True)

    def _confirm_missing(self) -> bool:
        try:
            missing = list(exporter.missing_phrases(self.ctl.project))
        except Exception as e:  # noqa: BLE001
            log.warning("missing_phrases failed: %s", e)
            return True
        if not missing or not self.interactive:
            return True
        cat = self.ctl.project.catalog
        names = []
        for pid in missing[:15]:
            try:
                names.append("•  " + self.ctl.project.spoken_text(pid))
            except KeyError:
                names.append("•  " + pid)
        more = f"\n…and {len(missing) - 15} more" if len(missing) > 15 else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Some phrases are missing")
        box.setText(f"{len(missing)} of {len(cat.phrases)} phrases have no take yet.\n"
                    "The co-driver will stay silent for those in the game.")
        box.setInformativeText("\n".join(names) + more)
        go = box.addButton("Export anyway", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is go

    def export_to(self, path, installed=False):
        if self._busy:
            return None
        if not self._confirm_missing():
            return None
        path = pathlib.Path(path)
        proj = self.ctl.project
        cancel = threading.Event()
        try:
            params = inspect.signature(exporter.export_mod).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs = {"allow_missing": True}
        if "cancel" in params:
            kwargs["cancel"] = cancel

        dlg = QProgressDialog("Rendering and encoding your takes…", "Cancel" if "cancel" in params else "", 0, 100, self)
        dlg.setWindowTitle("Exporting")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        if "cancel" not in params:
            dlg.setCancelButton(None)
        else:
            dlg.canceled.connect(cancel.set)
        dlg.setValue(0)
        if self.interactive:
            dlg.show()
        self._busy = True
        self._update_summary()
        self.ctl.audio.stop_playback()

        def work(progress):
            def cb(*a):
                progress(a)
            return exporter.export_mod(proj, path, progress_cb=cb, **kwargs)

        def on_progress(a):
            try:
                if len(a) >= 2 and a[1]:
                    dlg.setMaximum(int(a[1]))
                    dlg.setValue(int(a[0]))
                elif len(a) == 1 and isinstance(a[0], float) and a[0] <= 1.0:
                    dlg.setValue(int(a[0] * 100))
                for x in a:
                    if isinstance(x, str):
                        dlg.setLabelText(x)
            except Exception:  # noqa: BLE001
                pass

        def finished():
            self._busy = False
            dlg.close()
            dlg.deleteLater()
            self._update_summary()

        def done(report):
            finished()
            self.last_report = report
            self.last_error = None
            self._show_result(report, installed)

        def failed(e, tb):
            finished()
            self.last_error = e
            if e.__class__.__name__ == "ExportCancelled":
                self.ctl.status.emit("Export cancelled.")
                return
            if self.interactive:
                QMessageBox.critical(self, "Export failed", f"The pack couldn't be exported:\n\n{e}")

        return run_async(work, done, failed, on_progress, owner=self)

    def _show_result(self, report, installed):
        zp = pathlib.Path(report.zip_path)
        n = getattr(report, "files_written", 0)
        missing = list(getattr(report, "missing", []) or [])
        warnings = list(getattr(report, "warnings", []) or [])
        self.result_title.setText(("Installed to BeamNG" if installed else "Exported") + f"  ·  {zp.name}")
        # zero-width spaces after the separators let a long path wrap instead of widening the page
        path_html = wrappable_path(zp)
        parts = [f"{n} audio files written to <code>{path_html}</code>."]
        if missing:
            parts.append(f"<span style='color:{theme.AMBER}'>{len(missing)} phrases have no audio and will be "
                         f"silent.</span>")
        parts.append(f"<br><span style='font-size:12pt'>{RESTART_NOTE}</span>"
                     if installed else
                     f"<br>Copy the zip into BeamNG's <code>mods</code> folder. {RESTART_NOTE}")
        if warnings:
            items = "".join(f"<li>{w}</li>" for w in warnings[:12])
            more = f"<li>…and {len(warnings) - 12} more</li>" if len(warnings) > 12 else ""
            parts.append(f"<br><span style='color:{theme.AMBER}'><b>Warnings</b></span><ul style='margin:2px'>"
                         f"{items}{more}</ul>")
        self.result_text.setText("<br>".join(parts))
        self.result.show()
        if self.interactive:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Done!")
            box.setTextFormat(Qt.RichText)
            box.setText(f"<b>{'Installed' if installed else 'Exported'} {zp.name}</b><br><br>{RESTART_NOTE}")
            if warnings:
                box.setInformativeText(f"{len(warnings)} warning(s), see the Pack & Export page.")
            ob = box.addButton("Open folder", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Ok)
            box.exec()
            if box.clickedButton() is ob:
                open_in_explorer(zp)

    def showEvent(self, e):
        super().showEvent(e)
        self.load_from_project()
