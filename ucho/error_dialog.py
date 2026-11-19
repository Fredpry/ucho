# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Error dialog and the "Save diagnostics package" dialog.

Public API (callable from any thread):

- `show_error(app, error, context=None)`: shows a handled error, e.g. a
  failed model download (`context=diagnostics.MODEL_DOWNLOAD`).
- `install(app)`: unhandled exceptions in Tk callbacks, threads and the main
  thread are logged with their traceback and end in this dialog.
- `open_diagnostics(app)`: the package dialog on its own (Settings → About).

Dialogs are opened on the UI thread through `app.post`, and
`diagnostics.Throttle` keeps a loop of errors from opening many windows.
Recognition and the package itself live in ucho/diagnostics.py (no Tk).
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import customtkinter as ctk

from . import config, diagnostics, theme, ui
from .i18n import t
from .theme import C

log = logging.getLogger(__name__)

_reporter: diagnostics.Reporter | None = None
_hooks_installed = False


def _file_descriptions() -> dict[str, str]:
    return {
        "system.txt": t("Ucho version, Windows, processor, memory and graphics"),
        "settings.json": t("settings without personal paths and names"),
        "models.txt": t("downloaded models and their sizes"),
        "error.txt": t("this error with its technical details"),
        "ucho.log": t("the app log, with your text removed"),
        "ucho.log.1": t("the older part of the log, with your text removed"),
    }


def _reporter_for(app) -> diagnostics.Reporter:
    global _reporter
    if _reporter is None or _reporter.owner is not app:

        def show(error: BaseException, context: str | None, on_close) -> ErrorDialog:
            if not app.winfo_exists():
                raise RuntimeError("the app window is gone")
            return ErrorDialog(app, error, context, on_close=on_close)

        _reporter = diagnostics.Reporter(app.post, show, owner=app)
    return _reporter


def show_error(app, error: BaseException, context: str | None = None) -> None:
    """Shows `error` in the error dialog (any thread). Not throttled by repeats:
    the user asked for the action again, so a second failure is shown again
    (only one dialog is open at a time)."""
    _reporter_for(app).report(error, context, force=True)


def report(app, error: BaseException, context: str | None = None) -> None:
    """An unhandled error: shown unless the throttle holds it back."""
    _reporter_for(app).report(error, context)


def install(app) -> None:
    """Routes unhandled exceptions of Tk callbacks, threads and the main thread here.

    Every hook logs the full traceback first (the previous hook does that for
    threads, see app._install_excepthooks), then shows the dialog.
    """
    global _hooks_installed
    _reporter_for(app)
    previous_callback = app.report_callback_exception

    def callback_hook(exc, value, trace):
        previous_callback(exc, value, trace)
        if value is not None and _reporter is not None:
            _reporter.report(value)

    app.report_callback_exception = callback_hook
    if _hooks_installed:
        return
    _hooks_installed = True
    previous_thread = threading.excepthook
    previous_main = sys.excepthook

    def thread_hook(args):
        previous_thread(args)
        if args.exc_value is not None and _reporter is not None:
            _reporter.report(args.exc_value)

    def main_hook(exc, value, trace):
        log.error("Unhandled exception", exc_info=(exc, value, trace))
        if previous_main is not sys.__excepthook__:
            previous_main(exc, value, trace)
        if value is not None and _reporter is not None:
            _reporter.report(value)

    threading.excepthook = thread_hook
    sys.excepthook = main_hook


def _place(dialog: ctk.CTkToplevel, app) -> None:
    """Over the app window when it is visible; on top on its own otherwise
    (a dialog transient to a window hidden in the tray would stay hidden)."""
    try:
        visible = app.state() not in ("withdrawn", "iconic")
    except Exception:
        visible = False
    if visible:
        dialog.transient(app)
    dialog.lift()
    dialog.attributes("-topmost", True)
    dialog.after(400, lambda: dialog.winfo_exists() and dialog.attributes("-topmost", False))


def _readonly_text(master, height: int) -> ctk.CTkTextbox:
    box = ctk.CTkTextbox(master, height=height, wrap="none", font=theme.font(theme.XS, family="mono"),
                         fg_color=C.surface_muted, border_width=1, border_color=C.border)
    box.configure(state="disabled")
    return box


def _set_text(box: ctk.CTkTextbox, text: str) -> None:
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("1.0", text)
    box.configure(state="disabled")


class ErrorDialog(ctk.CTkToplevel):
    """Title, message and hint for a recognised error, details and a package button."""

    def __init__(self, app, error: BaseException, context: str | None = None, on_close=None):
        super().__init__(app)
        self.app, self.error, self.context = app, error, context
        self.info = diagnostics.classify(error, context)
        self._on_close = on_close
        self.title(f"{config.APP_NAME} — {self.info.title}")
        self.configure(fg_color=C.surface)
        width = 460
        ui.label(self, self.info.title, theme.LG, "bold", anchor="w", justify="left",
                 wraplength=width).pack(fill="x", padx=24, pady=(20, 6))
        ui.label(self, self.info.message, theme.SM - 1, anchor="w", justify="left",
                 wraplength=width).pack(fill="x", padx=24)
        ui.muted(self, self.info.hint, theme.SM - 1, anchor="w", justify="left",
                 wraplength=width).pack(fill="x", padx=24, pady=(8, 0))
        # Packed only when more errors arrive while the dialog is open.
        self.suppressed = ui.muted(self, "", anchor="w", justify="left", wraplength=width)

        self.details_button = ui.ghost(self, t("Show details"), self._toggle_details, width=10)
        self.details_button.pack(anchor="w", padx=18, pady=(10, 0))
        self.details = _readonly_text(self, 180)
        _set_text(self.details, diagnostics.format_details(error, _recordings(app)))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", side="bottom", padx=24, pady=(14, 20))
        self.save_button = ui.primary(buttons, t("Save diagnostics package"), self._save, width=10)
        self.save_button.pack(side="right")
        close = ui.secondary(buttons, t("Close"), self.close, width=10, height=38)
        close.pack(side="right", padx=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        _place(self, app)
        self.after(50, close.focus_set)
        log.info("Error dialog shown (%s)", self.info.kind)

    def _toggle_details(self) -> None:
        if self.details.winfo_ismapped():
            self.details.pack_forget()
            self.details_button.configure(text=t("Show details"))
        else:
            self.details.pack(fill="both", expand=True, padx=24, pady=(6, 0))
            self.details_button.configure(text=t("Hide details"))

    def update_suppressed(self, count: int) -> None:
        if self.winfo_exists() and count:
            self.suppressed.configure(text=t("{count} more errors happened meanwhile (in the log).",
                                             count=count))
            if not self.suppressed.winfo_ismapped():
                self.suppressed.pack(fill="x", padx=24, pady=(6, 0), before=self.details_button)

    def _save(self) -> None:
        DiagnosticsDialog(self.app, self.error, self.info, parent=self)

    def close(self) -> None:
        callback, self._on_close = self._on_close, None
        try:
            self.destroy()
        finally:
            if callback is not None:
                callback()


def _recordings(app) -> list[str]:
    try:
        return [str(app.settings.recordings_dir)]
    except Exception:
        return []


class DiagnosticsDialog(ctk.CTkToplevel):
    """Shows what goes into the package, then saves it, shows it in Explorer and
    opens an e-mail draft. Nothing is sent by Ucho."""

    def __init__(self, app, error: BaseException | None = None,
                 info: diagnostics.ErrorInfo | None = None, parent=None):
        super().__init__(parent or app)
        self.app, self.error, self.info = app, error, info
        self.settings = getattr(app, "settings", None)
        self.path: Path | None = None
        self._generation = 0
        self.title(t("Diagnostics package"))
        self.configure(fg_color=C.surface)
        width = 520
        ui.label(self, t("Diagnostics package"), theme.LG, "bold", anchor="w").pack(
            fill="x", padx=24, pady=(20, 6))
        ui.label(self, t("A ZIP file that helps the author find the cause of a problem. Ucho does "
                         "not send it: you attach it to an e-mail yourself."),
                 theme.SM - 1, anchor="w", justify="left", wraplength=width).pack(fill="x", padx=24)
        self.files = ui.label(self, "", theme.SM - 1, anchor="w", justify="left", wraplength=width)
        self.files.pack(fill="x", padx=24, pady=(10, 0))
        ui.muted(self, t("Never included: recordings, transcripts and summaries."), anchor="w",
                 justify="left", wraplength=width).pack(fill="x", padx=24, pady=(4, 0))

        self.include = ctk.BooleanVar(value=False)
        self.checkbox = ctk.CTkCheckBox(self, text=t("Include dictated text"), variable=self.include,
                                        command=self._refresh, font=theme.font(theme.SM - 1))
        ui.focusable(self.checkbox, self.checkbox.toggle)
        self.checkbox.pack(anchor="w", padx=24, pady=(12, 0))
        self.counts = ui.muted(self, "", anchor="w", justify="left", wraplength=width)
        self.counts.pack(fill="x", padx=24, pady=(4, 0))
        self.excerpt_label = ui.label(self, t("End of the log, as it will be saved:"),
                                      theme.SM - 1, "bold", anchor="w")
        self.excerpt_label.pack(fill="x", padx=24, pady=(10, 4))
        self.excerpt = _readonly_text(self, 120)
        self.excerpt.pack(fill="both", expand=True, padx=24)
        self.status = ui.label(self, "", theme.SM - 1, anchor="w", justify="left",
                               wraplength=width)
        self.status.pack(fill="x", padx=24, pady=(8, 0))

        self.buttons = ctk.CTkFrame(self, fg_color="transparent")
        self.buttons.pack(fill="x", padx=24, pady=(12, 20))
        self.save_button = ui.primary(self.buttons, t("Save"), self._save, width=10)
        self.save_button.pack(side="right")
        self.cancel = ui.secondary(self.buttons, t("Cancel"), self.destroy, width=10, height=38)
        self.cancel.pack(side="right", padx=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _e: self.destroy())
        _place(self, app)
        self.after(50, self.cancel.focus_set)
        self._refresh()

    def _post(self, fn) -> None:
        self.app.post(lambda: self.winfo_exists() and fn())

    def _refresh(self) -> None:
        """Preview off the UI thread: the log can be a few MB."""
        self._generation += 1
        generation, include = self._generation, bool(self.include.get())
        _set_text(self.excerpt, t("Preparing…"))

        def work():
            try:
                result = diagnostics.preview(include, self.settings, self.error)
            except Exception as error:
                log.warning("Diagnostics preview failed", exc_info=True)
                name = type(error).__name__
                self._post(lambda: _set_text(self.excerpt, name))
                return
            self._post(lambda: generation == self._generation and self._show_preview(result))

        threading.Thread(target=work, name="diagnostics-preview", daemon=True).start()

    def _show_preview(self, result: diagnostics.Preview) -> None:
        descriptions = _file_descriptions()
        # One paragraph, not a list: the dialog must fit a 1366 x 768 screen at 125 %.
        self.files.configure(text=t("What is inside:") + " " + "; ".join(
            f"{name} ({descriptions[name]})" for name in result.files
            if name in descriptions) + ".")
        self.counts.configure(text=t("Removed from the log: {dictations} dictations and "
                                     "{fragments} other fragments (meeting titles, names).",
                                     dictations=result.dictations, fragments=result.fragments)
                              if not self.include.get() else
                              t("The dictated text stays in the log. Meeting titles and names "
                                "are still removed."))
        _set_text(self.excerpt, result.log_excerpt or t("(the log is empty)"))
        self.excerpt.see("end")
        self.excerpt.xview_moveto(0)

    def _save(self) -> None:
        self.save_button.configure(state="disabled")
        self.checkbox.configure(state="disabled")
        self.status.configure(text=t("Saving…"), text_color=C.text)
        include = bool(self.include.get())

        def work():
            try:
                path = diagnostics.save_package(include, settings=self.settings, error=self.error,
                                                info=self.info,
                                                profile=getattr(self.app, "hardware_profile", None))
            except Exception as error:
                log.warning("Saving the diagnostics package failed", exc_info=True)
                failure = error
                self._post(lambda: self._failed(failure))
                return
            self._post(lambda: self._saved(path))

        def start():
            threading.Thread(target=work, name="diagnostics-save", daemon=True).start()

        # The hardware profile imports OpenVINO; the app probes it once and keeps it.
        load = getattr(self.app, "load_hardware", None)
        if load is not None:
            load(lambda: self.winfo_exists() and start())
        else:
            start()

    def _failed(self, error: BaseException) -> None:
        info = diagnostics.classify(error)
        self.status.configure(text=t("Could not save the package: {error}", error=info.title),
                              text_color=C.warning)
        self.save_button.configure(state="normal")
        self.checkbox.configure(state="normal")

    def _saved(self, path: Path) -> None:
        self.path = path
        diagnostics.reveal(path)
        mailed = diagnostics.open_mail(diagnostics.mail_url(path, self.info))
        text = t("Saved: {path}", path=path)
        if mailed:
            text += "\n" + t("An e-mail draft is open: attach this file to it and send it.")
        else:
            text += "\n" + t("No mail program opened. Send the file to {email} yourself.",
                             email=config.SUPPORT_EMAIL)
        self.status.configure(text=text, text_color=C.text)
        for widget in (self.checkbox, self.counts, self.excerpt_label, self.excerpt):
            widget.pack_forget()
        for child in self.buttons.winfo_children():
            child.destroy()
        close = ui.primary(self.buttons, t("Close"), self.destroy, width=10)
        close.pack(side="right")
        ui.secondary(self.buttons, t("Write e-mail"),
                     lambda: diagnostics.open_mail(diagnostics.mail_url(path, self.info)),
                     width=10, height=38).pack(side="right", padx=(0, 8))
        ui.secondary(self.buttons, t("Show in folder"), lambda: diagnostics.reveal(path),
                     "folder", width=10, height=38).pack(side="right", padx=(0, 8))
        close.focus_set()


def open_diagnostics(app) -> DiagnosticsDialog:
    """The package dialog without an error (Settings → About)."""
    return DiagnosticsDialog(app)
