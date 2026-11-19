# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""The Ucho app: one process for meeting recording and dictation.

One window with a sidebar (Meeting, Dictation, Recordings, Settings) or, in
the "tray" layout, the tray icon as the hub with the main window and a small
dictation window. Both layouts share everything below: one Whisper engine
(ucho/engine.py), one tray icon, both global hotkeys, one AppUserModelID,
the Jump List and the single-instance guard.

`python -m ucho --smoke-test` builds the whole UI without audio capture and
without loading a model, walks through every screen and both layouts, and
exits with 1 when anything logged an error (CI runs it on windows-latest).
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import logging.handlers
import queue
import sys
import tempfile
import threading
import time
import tkinter
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

import customtkinter as ctk

from . import config, downloads, hardware, i18n, retention, theme, ui, winshell
from .archive import Archive
from .dictation import DictationController
from .engine import Engine
from . import diagnostics, error_dialog
from .hotkeys import Hotkeys
from .hotkeys import label as hotkey_label
from .i18n import t
from .instance import SingleInstance
from .meeting import MeetingController
from .theme import C
from .tray import Item, TrayIcon

log = logging.getLogger(__name__)

ICON_FILE = Path(__file__).resolve().parent / "assets" / "icon.ico"
DICTATE_ICON_FILE = Path(__file__).resolve().parent / "assets" / "dictate.ico"
TICK_MS = 66
SIDEBAR_WIDTH = 212
# The speech model is looked for on the disk again this often while it is
# missing (it may arrive from the installer or another Ucho), and once more
# after a download.
MODEL_CHECK_S = 10.0
# Audio retention runs 20 s after start (the window and the dictation preload
# come first), then about once a day. The hourly check only compares two
# numbers; a single 24 h timer could drift a lot across sleep and hibernation.
RETENTION_FIRST_MS = 20_000
RETENTION_CHECK_MS = 3_600_000
RETENTION_INTERVAL_S = 23.5 * 3600


@dataclass
class Options:
    dictate: bool = False      # open dictation (shortcut "Ucho Dictate", Jump List)
    record: bool = False       # start recording a meeting (Jump List)
    minimized: bool = False    # start in the tray (start with Windows)
    smoke_test: float | None = None  # seconds; CI start test without audio and models


def parse_args(argv: list[str] | None) -> Options:
    parser = argparse.ArgumentParser(prog="python -m ucho", description="Ucho (offline).")
    parser.add_argument("--dictate", action="store_true", help="open dictation")
    parser.add_argument("--record", action="store_true", help="start recording a meeting")
    parser.add_argument("--minimized", action="store_true", help="start in the tray")
    parser.add_argument("--smoke-test", nargs="?", const=3.0, type=float, metavar="SECONDS",
                        help="start the full UI without audio capture or models, exit after "
                             "SECONDS (default 3); exit code 1 when an error was logged")
    args, _unknown = parser.parse_known_args(argv)
    return Options(args.dictate, args.record, args.minimized, args.smoke_test)


class App(ctk.CTk):
    def __init__(self, settings: config.Settings, options: Options | None = None):
        self.settings = settings
        self.options = options or Options()
        self.smoke = self.options.smoke_test is not None
        # The smoke test must not touch OpenVINO devices (CI); the screenshot tool turns it on.
        self.probe_hardware = not self.smoke
        theme.apply(settings["theme"])
        super().__init__()
        theme.apply_fonts()

        self._posted: queue.Queue[Callable[[], None]] = queue.Queue()
        self.engine = Engine()
        self.engine.start()
        self.archive = Archive(settings.recordings_dir)
        self.meeting = MeetingController(self)
        self.dictation = DictationController(self)
        self.hotkeys = Hotkeys(self._on_hotkey)
        self.hotkey_errors: dict[str, str] = {}
        self.hardware_profile: hardware.HardwareProfile | None = None
        self.hardware_rec: hardware.Recommendation | None = None
        self.hardware_measured = None
        self._hardware_waiting: list[Callable[[], None]] = []
        self.devices: dict | None = None
        self._device_callbacks: list[Callable[[dict], None]] = []
        self.audio_ok = True
        self.last_query = ""
        self.compact = False
        self.dictation_window = None
        self._body: ctk.CTkFrame | None = None
        self._screens: dict = {}
        self._current = ""
        self._nav: dict[str, ctk.CTkButton] = {}
        self._toast = None
        self._toast_until = 0.0
        self._taskbar = None
        self._threads: list[threading.Thread] = []
        # Model downloads (wizard, Settings, the main window) and the speech
        # model check that blocks recording while no model is on the disk.
        # Download threads are not joined at quit: a download can take minutes.
        self.downloads = downloads.Downloads(self.post)
        self.downloads.listen(self._download_finished)
        self._speech_downloaded: frozenset[str] | None = None  # speech models on the disk
        self._speech_checked_at = 0.0
        self._speech_generation = 0
        self._speech_checking = False
        self._blocked_shown: bool | None = None
        self._models_shown = None

        self._setup_window()
        self.tray = TrayIcon(config.APP_NAME, [
            Item(lambda: t("Show Ucho"), lambda: self.post(self.show_main), default=True),
            Item(lambda: t("Stop recording") if self.meeting.recording else t("Record a meeting"),
                 lambda: self.post(self.toggle_recording),
                 enabled=lambda: self.meeting.recording or not self.recording_blocked(False)),
            Item(lambda: t("Stop dictation") if self.dictation.recording else t("Dictate"),
                 lambda: self.post(self.dictation.toggle),
                 enabled=lambda: self.dictation.recording or not self.recording_blocked(False)),
            Item(lambda: t("Dictation window"), lambda: self.post(self.show_dictation),
                 visible=lambda: self.settings["layout"] == "tray"),
            None,
            Item(lambda: t("Quit"), lambda: self.post(self.quit_app)),
        ])

        try:
            self.dictation.last_text = "" if self.smoke else _last_dictation()
        except Exception:
            log.debug("No last dictation", exc_info=True)
        self.build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._on_callback_error
        # Unhandled errors (Tk callbacks, threads) end in the error dialog (#29).
        error_dialog.install(self)

        self.hotkey_errors = self.hotkeys.start({"record": self.settings["hotkey"],
                                                 "dictate": self.settings["dictation_hotkey"]})
        if self.settings["tray"] or self.settings["layout"] == "tray":
            self.tray.start()
        self._start_thread(self._load_devices, "devices")
        self._check_speech_async()
        if not self.smoke:
            # Describe the window for the taskbar before anything heavy starts:
            # loading an OpenVINO model holds the GIL for 10+ s, and a window
            # pinned during that time would become a pythonw.exe tile.
            self.update_idletasks()
            self.describe_window()
            self._start_thread(self.dictation_prepare, "portaudio")
            # Let the window draw itself first; the preload freezes the UI briefly.
            self.after(1500, self.dictation.preload)
        self.after(TICK_MS, self._tick)
        # The window must exist in the system before it is described for the taskbar.
        self.after(600, self._after_start)
        self._retention_last: float | None = None  # time.monotonic() of the last run
        self.after(RETENTION_FIRST_MS, self._retention_tick)

    # --- window -------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.title(config.APP_NAME)
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        work_w, work_h = _work_area()
        if not work_w:
            work_w, work_h = self.winfo_screenwidth(), self.winfo_screenheight() - 48
        # Logical sizes (customtkinter scales them). A 1366x768 screen at 125 %
        # leaves about 1093 x 566 logical pixels; the layout must fit there.
        avail_w, avail_h = work_w / scaling, work_h / scaling - 34
        width, height = int(min(1040, avail_w - 40)), int(min(700, avail_h - 10))
        self.compact = height < 620
        self.geometry(f"{width}x{height}")
        self.minsize(min(820, width), min(500, height))
        self.configure(fg_color=C.bg)
        self._set_icon()
        # customtkinter repaints the title bar after ~200 ms (dark mode) and
        # Windows can lose the icon on the way; set it again afterwards.
        self.after(400, self._set_icon)

    def _set_icon(self) -> None:
        if not ICON_FILE.exists():
            return
        try:
            self.iconbitmap(default=str(ICON_FILE))
            self.iconbitmap(str(ICON_FILE))
        except Exception:
            pass

    def _on_callback_error(self, exc, value, trace) -> None:
        log.error("Unhandled exception in the UI", exc_info=(exc, value, trace))

    def window_handle(self, window=None) -> int:
        """Handle of the top-level window the taskbar sees (wm_frame, not winfo_id)."""
        window = window or self
        try:
            return int(window.wm_frame(), 16)
        except Exception:
            return window.winfo_id()

    def describe_window(self, window=None, dictation: bool = False) -> None:
        if sys.platform != "win32" or self.smoke:
            return
        command = start_command() + (" --dictate" if dictation else "")
        icon_file = DICTATE_ICON_FILE if dictation else ICON_FILE
        ok = winshell.describe_window(self.window_handle(window), command, config.APP_NAME,
                                      f"{icon_file},0", config.APP_ID)
        log.info("Taskbar description of the %s window: %s", "dictation" if dictation else "main", ok)

    def _after_start(self) -> None:
        if self.smoke:
            return
        self.describe_window()
        self._taskbar = winshell.TaskbarProgress(self.window_handle())
        tasks = [
            {"title": t("Record a meeting"), "arguments": f'"{STARTER}" --record',
             "description": t("Start recording a meeting with Ucho"), "icon": f"{ICON_FILE},0"},
            {"title": t("Dictate"), "arguments": f'"{STARTER}" --dictate',
             "description": t("Open Ucho dictation"), "icon": f"{DICTATE_ICON_FILE},0"},
        ]
        for task in tasks:
            task.update(program=str(_pythonw()), working_dir=str(config.PROJECT_DIR))

        def taskbar_setup():
            log.info("Jump List: %s", winshell.set_jump_list(config.APP_ID, tasks))
            _stamp_shortcuts()
            from .settings_screen import sync_autostart

            sync_autostart(self.settings)

        self._start_thread(taskbar_setup, "taskbar")
        if self.options.record:
            self.toggle_recording()
        elif self.options.dictate:
            self.show_dictation()

    # --- building -------------------------------------------------------------------

    def build(self) -> None:
        """(Re)builds the window content: wizard on first start, else the main layout."""
        if self._body is not None:
            self._body.destroy()
        self._reset_listeners()
        self._screens, self._nav, self._current = {}, {}, ""
        self._toast = None
        if self.dictation_window is not None:
            # customtkinter schedules title bar and icon updates on a new
            # Toplevel (~200 ms); destroying it before they run makes Tk raise.
            old, self.dictation_window = self.dictation_window, None
            old.withdraw()
            self.after(1500, lambda: old.winfo_exists() and old.destroy())
        self._body = ctk.CTkFrame(self, fg_color=C.bg, corner_radius=0)
        self._body.pack(fill="both", expand=True)
        if not self.settings["setup_complete"]:
            self._build_wizard(0, None)
            return
        self._build_main()
        if self.options.minimized and not self.smoke:
            self.options.minimized = False
            self.withdraw()

    def _reset_listeners(self) -> None:
        """Screens are rebuilt; only the app's own listener survives."""
        self.meeting.clear_listeners()
        self.dictation.clear_listeners()
        self.meeting.listen(self._meeting_event)

    def _meeting_event(self, event: str, data) -> None:
        if event == "error":
            self.toast(str(data), "warning", 6)
        elif event == "notice":
            self.toast(str(data), "ready", 6)
        elif event == "summary_error":
            log.warning("Summary skipped: %s", data)
            self.toast(t("No summary: {reason}", reason=data), "warning", 6)

    def _build_wizard(self, step: int, state: dict | None) -> None:
        from .wizard import Wizard

        Wizard(self._body, self, step, state).pack(fill="both", expand=True)

    def show_wizard(self, step: int = 0, state: dict | None = None) -> None:
        if self.settings["setup_complete"] and self._screens:
            # "Run the setup again" from Settings.
            self.settings["setup_complete"] = False
        if self._body is not None:
            self._body.destroy()
        self._screens, self._nav = {}, {}
        self._reset_listeners()
        self._body = ctk.CTkFrame(self, fg_color=C.bg, corner_radius=0)
        self._body.pack(fill="both", expand=True)
        self._build_wizard(step, state)
        self.show_main()

    def wizard(self):
        """The wizard frame while it is shown, else None."""
        from .wizard import Wizard

        children = self._body.winfo_children() if self._body is not None else []
        return next((c for c in children if isinstance(c, Wizard)), None)

    def finish_wizard(self) -> None:
        self.recordings_dir_changed()
        self.settings_changed()
        self.build()
        self.show("meeting")

    def _build_main(self) -> None:
        root = self._body
        root.grid_columnconfigure(2, weight=1)
        root.grid_rowconfigure(0, weight=1)
        sidebar = ctk.CTkFrame(root, fg_color=C.surface, corner_radius=0, width=SIDEBAR_WIDTH)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        ctk.CTkFrame(root, fg_color=C.border, corner_radius=0, width=1).grid(row=0, column=1, sticky="ns")
        self._content = ctk.CTkFrame(root, fg_color=C.bg, corner_radius=0)
        self._content.grid(row=0, column=2, sticky="nsew")

        logo = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo.grid(row=0, column=0, sticky="ew", padx=18, pady=(22, 22))
        ctk.CTkLabel(logo, text="", image=ui.mark(34)).pack(side="left")
        ui.label(logo, "Ucho", theme.XL + 4, "bold", family="display").pack(side="left", padx=(8, 0))

        items = [("meeting", t("Meeting"), "meeting")]
        if self.settings["layout"] == "single":
            items.append(("dictation", t("Dictation"), "mic"))
        items += [("recordings", t("Recordings"), "list"), ("settings", t("Settings"), "settings")]
        for i, (key, text, glyph) in enumerate(items):
            button = ctk.CTkButton(sidebar, text=text, anchor="w", height=40,
                                   corner_radius=theme.RADIUS_SM + 2, fg_color="transparent",
                                   hover_color=C.hover, text_color=C.text_muted,
                                   font=theme.font(theme.SM + 1), border_color=C.focus,
                                   image=ui.icon(glyph, 18, C.text_muted), compound="left",
                                   command=lambda k=key: self.show(k))
            button.grid(row=1 + i, column=0, sticky="ew", padx=12, pady=2)
            ui.focusable(button, button.invoke)
            self._nav[key] = button
        sidebar.grid_rowconfigure(10, weight=1)
        if self.settings["layout"] == "tray":
            ui.ghost(sidebar, t("Dictation window"), self.show_dictation, "window",
                     color=C.text_muted, anchor="w").grid(row=11, column=0, sticky="ew", padx=12)

        self._build_models_card(sidebar)

        self._activity = ctk.CTkFrame(sidebar, fg_color=C.info_soft, corner_radius=theme.RADIUS_MD)
        self._activity.grid_columnconfigure(0, weight=1)
        self._activity_label = ui.label(self._activity, "", theme.XS + 1, "bold", color=C.info,
                                        anchor="w")
        self._activity_label.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        self._activity_bar = ctk.CTkProgressBar(self._activity, height=6, progress_color=C.info)
        self._activity_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        for widget in (self._activity, self._activity_label, self._activity_bar):
            widget.bind("<Button-1>", lambda _e: self.show("transcript", live=True), add="+")
        self._activity_shown = False

        privacy = ctk.CTkFrame(sidebar, fg_color="transparent")
        privacy.grid(row=13, column=0, sticky="ew", padx=18, pady=(10, 18))
        ctk.CTkLabel(privacy, text="", image=ui.icon("check", 14, C.success)).pack(side="left")
        ui.muted(privacy, t("Offline. Nothing leaves this computer."), theme.XS, justify="left",
                 wraplength=150, anchor="w").pack(side="left", padx=(6, 0))
        self.show("meeting")

    # --- navigation -------------------------------------------------------------------

    def _screen(self, name: str):
        if name not in self._screens:
            from . import screens
            from .settings_screen import SettingsScreen

            factory = {
                "meeting": screens.MeetingScreen, "dictation": screens.DictationScreen,
                "recordings": screens.RecordingsScreen, "transcript": screens.TranscriptScreen,
                "settings": SettingsScreen,
            }[name]
            self._screens[name] = factory(self._content, self)
        return self._screens[name]

    def show(self, name: str, **kwargs) -> None:
        if not self.settings["setup_complete"]:
            return
        if name == "dictation" and self.settings["layout"] == "tray":
            self.show_dictation()
            return
        screen = self._screen(name)
        for key, other in self._screens.items():
            if key != name:
                other.pack_forget()
        screen.pack(fill="both", expand=True)
        self._current = name
        active = "recordings" if name == "transcript" else name
        for key, button in self._nav.items():
            selected = key == active
            glyph = {"meeting": "meeting", "dictation": "mic", "recordings": "list",
                     "settings": "settings"}[key]
            button.configure(fg_color=C.selected if selected else "transparent",
                             text_color=C.text if selected else C.text_muted,
                             font=theme.font(theme.SM + 1, "bold" if selected else "normal"),
                             image=ui.icon(glyph, 18, C.action if selected else C.text_muted))
        screen.on_show(**kwargs)

    def show_main(self, screen: str | None = None) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()
        if screen:
            self.show(screen, focus=True)

    def show_dictation(self) -> None:
        if not self.settings["setup_complete"]:
            self.show_main()
            return
        if self.settings["layout"] == "single":
            self.show_main("dictation")
            return
        if self.dictation_window is None or not self.dictation_window.winfo_exists():
            from .screens import DictationWindow

            self.dictation_window = DictationWindow(self)
        self.dictation_window.show()

    def sync_dictation_widgets(self) -> None:
        panels = []
        if "dictation" in self._screens:
            panels.append(self._screens["dictation"].panel)
        if self.dictation_window is not None and self.dictation_window.winfo_exists():
            panels.append(self.dictation_window.panel)
        for panel in panels:
            panel.sync()

    # --- actions ----------------------------------------------------------------------

    def toggle_recording(self) -> None:
        name = ""
        if "meeting" in self._screens:
            name = self._screens["meeting"].name.get()
        was_recording = self.meeting.recording
        self.meeting.toggle(name)
        if self.state() != "withdrawn" and self.settings["setup_complete"]:
            stopped = self.meeting.last_stopped
            if was_recording and self.meeting.transcribing and self.meeting.is_active(stopped):
                self.show("transcript", live=True)
            elif was_recording and self.meeting.queue_position(stopped):
                # Another session is still being transcribed: show this one as queued.
                self.show("transcript", folder=stopped)
            elif not was_recording:
                self.show("meeting")

    def _on_hotkey(self, name: str) -> None:
        if name == "record":
            self.post(self.toggle_recording)
        elif name == "dictate":
            self.post(self._dictate_hotkey)
        elif name == Hotkeys.ESCAPE:
            self.post(self.dictation.cancel)

    def _dictate_hotkey(self) -> None:
        self.dictation.toggle()
        # In the tray layout the small window appears without taking the focus,
        # so the text still goes to the window you are typing in.
        if (self.settings["layout"] == "tray" and self.dictation.recording
                and self.settings["setup_complete"]):
            if self.dictation_window is None or not self.dictation_window.winfo_exists():
                from .screens import DictationWindow

                self.dictation_window = DictationWindow(self)
                self.dictation_window.withdraw()
            _show_without_focus(self.window_handle(self.dictation_window))

    def on_command(self, command: str) -> None:
        """A second start of Ucho (shortcut, pinned tile, Jump List)."""
        if command == "record":
            self.show_main()
            if not self.meeting.recording:
                self.toggle_recording()
        elif command == "dictate":
            self.show_dictation()
        else:
            if self.settings["layout"] == "tray" and self.state() == "withdrawn" and \
                    self.dictation_window is not None and self.dictation_window.winfo_viewable():
                self.dictation_window.show()
            else:
                self.show_main()

    def status_pill(self) -> tuple[str, str]:
        m, d = self.meeting, self.dictation
        if m.recording:
            return t("Recording"), "recording"
        if d.recording:
            return t("Dictating"), "recording"
        # The number waiting; cards and the transcript screen show a position instead.
        queued = f"  ·  {t('{n} waiting', n=m.queued)}" if m.queued else ""
        if m.transcribing:
            return t("Transcribing {percent}%", percent=int(m.progress * 100)) + queued, "working"
        if m.summarizing:
            return t("Summarizing") + queued, "working"
        if d.queued:
            return t("Recognizing"), "working"
        readiness = self.speech_readiness(False)
        if readiness.running:
            fraction = readiness.progress.fraction
            percent = f" {int(fraction * 100)}%" if fraction is not None else ""
            return t("Downloading the model") + percent, "working"
        if readiness.blocks:
            return t("No speech model"), "warning"
        return t("Ready"), "ready"

    def dictation_hotkey_label(self) -> str:
        return hotkey_label(self.settings["dictation_hotkey"])

    def toast(self, text: str, style: str = "ready", seconds: float = 3.0) -> None:
        """Short message at the bottom of the window."""
        target = getattr(self, "_content", None) or self._body
        if target is None or not target.winfo_exists():
            return
        if self._toast is None or not self._toast.winfo_exists():
            self._toast = ctk.CTkLabel(target, text="", corner_radius=999, height=34, padx=16,
                                       font=theme.font(theme.SM - 1, "bold"))
        color = {"warning": C.warning}.get(style, C.text)
        self._toast.configure(text=text, fg_color=color, text_color=C.bg)
        self._toast.place(relx=0.5, rely=1.0, y=-24, anchor="s")
        self._toast.lift()
        self._toast_until = time.monotonic() + seconds

    # --- settings changes ------------------------------------------------------------

    def settings_changed(self) -> None:
        """Model, device or dictation settings changed: reload what dictation keeps in memory."""
        self._check_speech_model()
        if not self.smoke:
            self.dictation.preload()

    def recordings_dir_changed(self) -> None:
        self.archive = Archive(self.settings.recordings_dir)
        try:
            self.settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # --- models: downloads and whether recording can start ---------------------------

    def speech_readiness(self, check: bool = True) -> downloads.Readiness:
        """State of the transcription model chosen in Settings, with the fallback model.

        `check`: look at the disk now when it was not checked yet (a start of a
        recording); the UI loop passes False and treats "not checked" as ready.
        """
        model = self.settings["model"]
        downloaded = self._speech_downloaded
        if downloaded is None and check:
            downloaded = self._check_speech_model()
        if downloaded is None:
            return downloads.speech_readiness(model, None, self.downloads.get(model))
        return downloads.speech_readiness(model, model in downloaded, self.downloads.get(model),
                                          self._fallback(model, downloaded))

    def _fallback(self, preferred: str, downloaded: frozenset[str]) -> str | None:
        running = [item.key for item in self.downloads.running()]
        return downloads.fallback_speech_model(
            preferred, downloaded, running,
            prefer=(self.settings["model"], self.settings.dictation_model()))

    def speech_model_for(self, preferred: str, check: bool = True) -> str | None:
        """The speech model to use instead of `preferred`: itself when ready, else a fallback.

        None when no speech model is on the disk. Not checked yet (and
        `check` False): `preferred`, as before the check existed.
        """
        downloaded = self._speech_downloaded
        if downloaded is None and check:
            downloaded = self._check_speech_model()
        if downloaded is None:
            return preferred
        return self._fallback(preferred, downloaded)

    def recording_blocked(self, check: bool = True) -> str | None:
        """Why a meeting or a dictation cannot start now (no speech model at all), or None."""
        return downloads.block_reason(self.speech_readiness(check))

    def refuse_recording(self, reason: str) -> None:
        """A meeting or dictation was asked for without a speech model: say why."""
        log.info("Recording not started: speech model %s", self.speech_readiness(False).state)
        self.toast(reason, "warning", 6)
        if self.state() in ("withdrawn", "iconic"):
            # The hotkey or the tray while the window is hidden: the toast would be unseen.
            self.tray.notify(reason)

    def _speech_candidates(self) -> list[str]:
        from . import transcribe

        models = transcribe.offered_models()
        for model in (self.settings["model"], self.settings.dictation_model()):
            if model not in models:
                models.append(model)
        return models

    def _check_speech_model(self) -> frozenset[str]:
        """Looks for the speech models on the disk (UI thread, fast after the first import)."""
        from . import transcribe

        downloaded = frozenset(m for m in self._speech_candidates() if transcribe.model_downloaded(m))
        self._speech_generation += 1
        self._apply_speech_check(downloaded)
        return downloaded

    def _apply_speech_check(self, downloaded: frozenset[str]) -> None:
        self._speech_downloaded = downloaded
        self._speech_checked_at = time.monotonic()
        for model in downloaded:
            self.downloads.forget(model)

    def _check_speech_async(self) -> None:
        """The same check off the UI thread: the first one imports huggingface_hub."""
        if self._speech_checking:
            return
        self._speech_checking = True
        self._speech_checked_at = time.monotonic()
        candidates, generation = self._speech_candidates(), self._speech_generation

        def work() -> None:
            from . import transcribe

            downloaded = frozenset(m for m in candidates if transcribe.model_downloaded(m))

            def apply() -> None:
                self._speech_checking = False
                self._speech_checked_at = time.monotonic()
                # A check on the UI thread (settings change, finished download) is newer.
                if generation == self._speech_generation:
                    self._apply_speech_check(downloaded)
            self.post(apply)

        threading.Thread(target=work, name="model check", daemon=True).start()

    def download_model(self, repo: str, kind: str = downloads.SPEECH,
                       on_done: Callable[[str | None], None] | None = None) -> bool:
        """Downloads a model from Hugging Face in the background, with progress in the window."""
        from . import transcribe

        return self.downloads.start(
            repo, downloads.model_name(repo), kind,
            lambda bar: transcribe.model_path(repo, download=True, tqdm_class=bar), on_done)

    def download_speech_model(self) -> None:
        self.download_model(self.settings["model"])

    def download_speaker_models(self, on_done: Callable[[str | None], None] | None = None) -> bool:
        from . import diarize

        return self.downloads.start(downloads.SPEAKER_MODELS, t("Speaker detection models"),
                                    downloads.SPEAKERS, diarize.download_models, on_done)

    def _download_finished(self, item: downloads.Download) -> None:
        if item.error is not None:
            if self.state() in ("withdrawn", "iconic"):
                self.tray.notify(t("Model download failed: {error}", error=item.error))
            if item.exception is not None:
                # The cause in plain words, a hint and the diagnostics package (#29).
                error_dialog.show_error(self, item.exception, context=diagnostics.MODEL_DOWNLOAD)
            else:
                self.toast(t("Model download failed: {error}", error=item.error), "warning", 8)
        if item.kind == downloads.SPEECH:
            self._check_speech_model()
            if item.error is None and item.key in (self.settings["model"],
                                                   self.settings.dictation_model()):
                self.settings_changed()
        self.tray.refresh()

    def _build_models_card(self, sidebar) -> None:
        """Sidebar card: a running download, or the missing speech model with a button."""
        self._models = ctk.CTkFrame(sidebar, fg_color=C.info_soft, corner_radius=theme.RADIUS_MD)
        self._models.grid_columnconfigure(0, weight=1)
        self._models_title = ui.label(self._models, "", theme.XS + 1, "bold", anchor="w",
                                      justify="left", wraplength=164)
        self._models_title.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        self._models_text = ui.label(self._models, "", theme.XS, anchor="w", justify="left",
                                     wraplength=164)
        self._models_text.grid(row=1, column=0, sticky="ew", padx=12)
        self._models_bar = ctk.CTkProgressBar(self._models, height=6, progress_color=C.info)
        self._models_button = ui.secondary(self._models, "", self.download_speech_model,
                                           width=10, height=30)
        self._models_shown = None

    def models_card_content(self) -> tuple | None:
        """(style, title, text, fraction or None, button text or None) for the card, or None."""
        readiness = self.speech_readiness(False)
        name = downloads.model_name(readiness.model)
        size = downloads.model_size(readiness.model)
        if readiness.state == downloads.FALLBACK:
            meanwhile = t("Recording uses {model} meanwhile.",
                          model=downloads.model_name(readiness.fallback or ""))
            if readiness.running:
                return ("info", t("Downloading the speech model"),
                        f"{name}\n{readiness.progress.describe()}\n{meanwhile}",
                        readiness.progress.fraction or 0.0, None)
            if readiness.error:
                return ("warning", t("Download failed"),
                        t("{model}: {error}", model=name, error=readiness.error) + f"\n{meanwhile}",
                        None, t("Retry"))
            return ("info", t("Speech model not downloaded"), f"{name}\n{meanwhile}", None,
                    t("Download ({size})", size=size))
        if readiness.state == downloads.DOWNLOADING:
            return ("info", t("Downloading the speech model"),
                    f"{name}\n{readiness.progress.describe()}\n"
                    + t("Recording works when it is done."),
                    readiness.progress.fraction or 0.0, None)
        if readiness.state == downloads.MISSING:
            return ("warning", t("No speech model"),
                    t("Recording needs the speech model {model}. It is downloaded once from "
                      "Hugging Face.", model=name),
                    None, t("Download ({size})", size=size))
        if readiness.state == downloads.FAILED:
            return ("warning", t("Download failed"),
                    t("{model}: {error}", model=name, error=readiness.error or ""),
                    None, t("Retry"))
        running = self.downloads.running()
        if running:
            item = running[0]
            return ("info", t("Downloading a model"), f"{item.name}\n{item.progress.describe()}",
                    item.progress.fraction or 0.0, None)
        return None

    def _update_models(self) -> None:
        readiness = self.speech_readiness(False)
        blocked = readiness.blocks
        if blocked != self._blocked_shown:
            self._blocked_shown = blocked
            self.tray.refresh()
        # Not there yet: the model may arrive from the installer or another Ucho.
        waiting = readiness.state in (downloads.MISSING, downloads.FAILED, downloads.UNKNOWN) or (
            readiness.state == downloads.FALLBACK and not readiness.running)
        if waiting and time.monotonic() - self._speech_checked_at > MODEL_CHECK_S:
            self._check_speech_async()
        if not self._screens or not hasattr(self, "_models") or not self._models.winfo_exists():
            return
        content = self.models_card_content()
        # Progress in whole percents: no reconfiguring 15 times a second.
        key = None if content is None else (content[:3], None if content[3] is None
                                            else round(content[3], 2), content[4])
        if key == self._models_shown:
            return
        self._models_shown = key
        if content is None:
            self._models.grid_forget()
            return
        style, title, text, fraction, button = content
        color, soft = (C.info, C.info_soft) if style == "info" else (C.warning_ink, C.warning_soft)
        self._models.configure(fg_color=soft)
        self._models_title.configure(text=title, text_color=color)
        self._models_text.configure(text=text)
        if fraction is not None:
            self._models_bar.set(fraction)
            self._models_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 10))
        else:
            self._models_bar.grid_forget()
        if button is not None:
            self._models_button.configure(text=button)
            self._models_button.grid(row=3, column=0, sticky="w", padx=12, pady=(8, 10))
        else:
            self._models_button.grid_forget()
        self._models_text.grid_configure(pady=(0, 10) if fraction is None and button is None else 0)
        self._models.grid(row=6, column=0, sticky="ew", padx=12, pady=(14, 0))

    # --- audio retention -------------------------------------------------------------

    def _retention_tick(self) -> None:
        last = self._retention_last
        if last is None or time.monotonic() - last >= RETENTION_INTERVAL_S:
            self.run_retention()
        self.after(RETENTION_CHECK_MS, self._retention_tick)

    def run_retention(self) -> None:
        """Removes expired audio (Settings: "Delete audio after") and leftovers of failed deletions.

        The folders are selected on a worker thread (it reads every meta.json);
        the files are deleted on the UI thread, where each session is checked
        again against the meeting controller, so a transcription started in the
        meantime is never touched. Leftovers (tombstone folders, see
        retention.delete_session) are not sessions any more and go on the worker.
        """
        self._retention_last = time.monotonic()
        days = retention.retention_days(self.settings["audio_retention_days"])
        folder = self.settings.recordings_dir

        def work():
            try:
                retention.remove_leftovers(folder)
            except Exception as error:
                log.error("Removing leftovers of deleted recordings failed (%s)", type(error).__name__)
            if not days:
                return
            try:
                selected = retention.select_expired(folder, days)
            except Exception as error:
                # The type only: messages of file errors contain paths, i.e. meeting titles.
                log.error("Audio retention: selection failed (%s)", type(error).__name__)
                return
            if selected:
                self.post(lambda: self._apply_retention(folder, days, selected))

        self._start_thread(work, "retention")

    def _apply_retention(self, folder: Path, days: int, selected: list[Path]) -> None:
        if folder != self.settings.recordings_dir:
            return  # the folder was changed in the meantime; the next run uses the new one
        try:
            sessions, _files = retention.apply_retention(folder, days, active=self.meeting.in_use,
                                                         folders=selected)
        except Exception as error:
            log.error("Audio retention failed (%s)", type(error).__name__)
            return
        if sessions and self._current == "recordings":
            self._screens["recordings"].search()

    def set_theme(self, value: str) -> None:
        self.settings["theme"] = value
        self.settings.save()
        # customtkinter re-colors the title bar by hiding and showing the window
        # and then gives the focus back to the widget that had it; if a rebuild
        # destroyed that widget meanwhile, Tk raises. Park the focus on the root.
        tkinter.Misc.focus_set(self)
        ctk.set_appearance_mode(theme.appearance_mode(value))

    def set_layout(self, value: str) -> None:
        if value == self.settings["layout"]:
            return
        self.settings["layout"] = value
        self.settings.save()
        if value == "tray" and not self.tray.running:
            self.tray.start()
        current = self._current or "settings"
        self.build()
        self.show("meeting" if current in ("dictation", "transcript") else current)
        self.tray.refresh()

    def apply_language(self) -> None:
        used = i18n.set_language(self.settings["ui_language"])
        log.info("Interface language: %s", used)

    def set_ui_language(self, value: str) -> None:
        self.settings["ui_language"] = value
        self.settings.save()
        self.apply_language()
        self.build()
        self.show("settings")

    def set_autostart(self, enabled: bool) -> None:
        from .settings_screen import autostart_command

        self.settings["start_with_windows"] = enabled
        self.settings.save()
        if not self.smoke and not winshell.set_autostart(enabled, autostart_command()):
            self.toast(t("Could not change the autostart entry."), "warning")

    # --- hardware ------------------------------------------------------------------------

    def load_hardware(self, callback: Callable[[], None]) -> None:
        """Hardware profile and recommendation (OpenVINO import takes a few seconds)."""
        if self.hardware_profile is not None:
            callback()
            return
        self._hardware_waiting.append(callback)
        if len(self._hardware_waiting) > 1:
            return

        def work():
            try:
                # The smoke test must not touch OpenVINO devices: an empty
                # profile goes through the same rules.
                profile = hardware.profile() if self.probe_hardware else hardware.HardwareProfile()
                rec = hardware.recommend(profile)
            except Exception:
                log.exception("Hardware profile failed")
                profile, rec = hardware.HardwareProfile(), None
            if rec is None:
                rec = hardware.recommend(profile)

            def deliver():
                self.hardware_profile, self.hardware_rec = profile, rec
                waiting, self._hardware_waiting = self._hardware_waiting, []
                for cb in waiting:
                    try:
                        cb()
                    except Exception:
                        log.exception("Hardware callback failed")
            self.post(deliver)

        self._start_thread(work, "hardware")

    def run_benchmark(self, rec, on_done: Callable, on_error: Callable) -> None:
        """hardware.benchmark through the engine (all Whisper models released meanwhile)."""
        profile = self.hardware_profile

        def work(job):
            measured = hardware.benchmark(rec.model, rec.device)
            new_rec = hardware.after_benchmark(copy.deepcopy(rec), profile, measured.speed)
            if measured.device != rec.device:
                new_rec.warnings.append(t("The model did not start on {device}; the measurement is for {used}.",
                                          device=rec.device, used=measured.device))
            return measured, new_rec

        self.engine.exclusive(work, "benchmark",
                              on_done=lambda result: self.post(lambda: on_done(result)),
                              on_error=lambda error: self.post(lambda: on_error(error)))

    # --- audio devices -------------------------------------------------------------------

    def _load_devices(self) -> None:
        try:
            from . import audio

            devices = audio.list_devices()
        except Exception as error:
            # No PortAudio devices (e.g. a CI runner) must not stop the app.
            log.warning("Could not enumerate audio devices: %s", error)
            devices = {"microphones": [], "system": [], "error": str(error)}
        self.post(lambda: self._apply_devices(devices))

    def _apply_devices(self, devices: dict) -> None:
        self.devices = devices
        try:
            from . import audio

            if audio.migrate_device_choices(self.settings, devices):
                self.settings.save()
                log.info("Audio device choice saved by name: microphone %r, system %r",
                         self.settings["microphone_name"], self.settings["system_name"])
        except Exception:
            log.exception("Could not convert the saved audio device choice")
        self.audio_ok = bool(devices.get("microphones") or devices.get("system"))
        if not self.audio_ok:
            log.warning("No audio devices found")
        for callback in list(self._device_callbacks):
            try:
                callback(devices)
            except Exception:
                log.exception("Device callback failed")

    def on_devices(self, callback: Callable[[dict], None]) -> None:
        self._device_callbacks.append(callback)
        if self.devices is not None:
            callback(self.devices)

    def dictation_prepare(self) -> None:
        try:
            self.dictation.microphone.prepare()
        except Exception:
            log.warning("PortAudio did not start up front", exc_info=True)

    # --- loop ------------------------------------------------------------------------------

    def post(self, fn: Callable[[], None]) -> None:
        """Runs `fn` on the UI thread (safe to call from any thread)."""
        self._posted.put(fn)

    def _start_thread(self, target: Callable, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _tick(self) -> None:
        for _ in range(200):
            try:
                fn = self._posted.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as error:
                log.exception("UI callback failed")
                error_dialog.report(self, error)
        try:
            self._update()
        except Exception:
            log.exception("UI update failed")
        self.after(TICK_MS, self._tick)

    def _update(self) -> None:
        self.dictation.check_limit()
        screen = self._screens.get(self._current)
        if screen is not None and self.state() != "withdrawn":
            screen.tick()
        if self.dictation_window is not None and self.dictation_window.winfo_exists():
            self.dictation_window.tick()
        if self._toast is not None and self._toast.winfo_exists() and time.monotonic() > self._toast_until:
            self._toast.place_forget()
        self._update_activity()
        self._update_models()
        active = self.meeting.recording or self.dictation.recording
        self.tray.set_state(active, t("{app} - recording", app=config.APP_NAME) if active else None)
        if self._taskbar is not None:
            m = self.meeting
            if active:
                self._taskbar.set(winshell.TaskbarProgress.ERROR, 100)
            elif m.transcribing:
                self._taskbar.set(winshell.TaskbarProgress.NORMAL, max(1, int(m.progress * 100)))
            elif m.summarizing or self.dictation.queued:
                self._taskbar.set(winshell.TaskbarProgress.INDETERMINATE)
            elif running := self.downloads.running():
                fraction = running[0].progress.fraction
                if fraction is None:
                    self._taskbar.set(winshell.TaskbarProgress.INDETERMINATE)
                else:
                    self._taskbar.set(winshell.TaskbarProgress.NORMAL, max(1, int(fraction * 100)))
            else:
                self._taskbar.set(winshell.TaskbarProgress.NONE)

    def _update_activity(self) -> None:
        if not self._screens or not hasattr(self, "_activity") or not self._activity.winfo_exists():
            return
        m = self.meeting
        busy = m.busy and self._current != "transcript"
        if busy:
            text, _style = self.status_pill()
            self._activity_label.configure(text=text)
            if m.transcribing:
                self._activity_bar.configure(mode="determinate")
                self._activity_bar.set(m.progress)
            if not self._activity_shown:
                self._activity.grid(row=12, column=0, sticky="ew", padx=12, pady=(8, 0))
                self._activity_shown = True
        elif self._activity_shown:
            self._activity.grid_forget()
            self._activity_shown = False

    # --- closing -------------------------------------------------------------------------

    def _on_close(self) -> None:
        if self.tray.running and (self.settings["tray"] or self.settings["layout"] == "tray"):
            self.withdraw()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        log.info("Quit")
        self.shutdown()
        self.quit()

    def shutdown(self) -> None:
        """Stops recording, hotkeys, tray and engine. Safe to call twice."""
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        for step in (self.meeting.quit, self.dictation.quit, self.hotkeys.stop, self.tray.stop,
                     lambda: self.engine.stop(timeout=5.0), self.settings.save):
            try:
                step()
            except Exception:
                log.exception("Shutdown step failed")
        for thread in self._threads:
            thread.join(timeout=5.0)

    def close(self) -> None:
        """Destroys the window after mainloop returned, without stray Tcl callbacks."""
        try:
            for pending in self.tk.call("after", "info"):
                self.after_cancel(pending)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


# --- helpers --------------------------------------------------------------------------------

STARTER = config.PROJECT_DIR / "ucho.pyw"


def _pythonw() -> Path:
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    return windowless if windowless.exists() else interpreter


def start_command() -> str:
    """Command the taskbar uses to start the app again from a pinned tile.

    Deliberately `ucho.pyw`, not `-m ucho`: the tile starts without the
    project working directory, and the script adds it to `sys.path` itself.
    """
    return f'"{_pythonw()}" "{STARTER}"'


def _last_dictation() -> str:
    from .dictate import last_text_from_log

    return last_text_from_log()


def _stamp_shortcuts() -> None:
    """Our shortcuts get the AppUserModelID, so a pinned tile and the window are one button.

    Only shortcuts that start this copy of Ucho: another installation (or an
    older Ucho Dictate) keeps its own id.
    """
    for shortcut in winshell.shortcut_paths():
        arguments = winshell.shortcut_arguments(shortcut) if shortcut.exists() else None
        if arguments and str(STARTER).lower() in arguments.lower():
            log.info("Shortcut %s id: %s", shortcut.name,
                     winshell.set_shortcut_app_id(shortcut, config.APP_ID))


def _work_area() -> tuple[int, int]:
    """Size of the primary monitor without the taskbar, in physical pixels."""
    if sys.platform != "win32":
        return 0, 0
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        return rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        return 0, 0


def _show_without_focus(hwnd: int) -> None:
    if sys.platform == "win32" and hwnd:
        import ctypes

        ctypes.windll.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE


def _set_app_id() -> None:
    """Our own AppUserModelID; otherwise the taskbar shows the pythonw.exe icon.

    Must be called before the first window is created.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(config.APP_ID)
    except Exception:
        log.debug("Could not set AppUserModelID", exc_info=True)


class _ErrorCounter(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _install_excepthooks() -> None:
    def thread_hook(args):
        log.error("Unhandled exception in thread %s", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_hook


def run(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    if options.smoke_test is not None:
        return smoke_test(options)

    try:
        config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # Capped: about 2 MB plus one older file (ucho.log.1); the log holds dictated text.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(
            str(config.LOG_FILE), maxBytes=2_000_000, backupCount=1, encoding="utf-8")],
    )
    _install_excepthooks()
    command = "record" if options.record else "dictate" if options.dictate else "show"
    instance = SingleInstance()
    if not instance.acquire():
        log.info("Ucho already runs; passing on: %s", command)
        instance.signal(command)
        return 0
    settings = config.Settings()
    used = i18n.set_language(settings["ui_language"])
    log.info("Start %s %s (UI: %s, layout: %s)", config.APP_NAME, config.VERSION, used,
             settings["layout"])
    _set_app_id()
    config.prepare_dirs(settings)
    app = App(settings, options)
    instance.listen(lambda c: app.post(lambda: app.on_command(c)))
    try:
        app.mainloop()
    finally:
        app.shutdown()
        instance.close()
        app.close()
    return 0


def smoke_test(options: Options) -> int:
    """Builds the full UI with a temporary settings file and recordings folder.

    No audio capture (devices are only listed) and no model load. Walks
    through the wizard, every screen, both layouts and both themes, then
    exits: 0 when nothing logged an error, 1 otherwise.
    """
    seconds = max(1.0, options.smoke_test or 3.0)
    counter = _ErrorCounter()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().addHandler(counter)
    _install_excepthooks()
    with tempfile.TemporaryDirectory(prefix="ucho-smoke-") as folder:
        root = Path(folder)
        recordings = root / "recordings"
        _synthetic_session(recordings)
        (root / "settings.json").write_text(json.dumps({"recordings_dir": str(recordings),
                                                        "ui_language": "en"}), encoding="utf-8")
        settings = config.Settings(root / "settings.json")
        i18n.set_language(settings["ui_language"])
        app = App(settings, options)
        session_dir = next(recordings.iterdir())
        steps: list[Callable[[], None]] = [
            lambda: app.show_wizard(1), lambda: app.show_wizard(2),
            lambda: app.show_wizard(3), lambda: app.show_wizard(4),
            lambda: app.wizard().finish(),
            lambda: app.show("dictation"), lambda: app.show("recordings"),
            lambda: app.show("transcript", folder=session_dir, query="budget"),
            lambda: app.show("settings"),
            lambda: app.set_theme("dark"), lambda: app.show("meeting"),
            lambda: app.set_layout("tray"), lambda: app.show_dictation(),
            lambda: app.set_theme("light"), lambda: app.set_layout("single"),
        ]
        delay = int(seconds * 1000 / (len(steps) + 1))
        for i, step in enumerate(steps, start=1):
            app.after(delay * i, step)
        app.after(int(seconds * 1000) + 200, app.quit_app)
        started = time.monotonic()
        try:
            app.mainloop()
        finally:
            app.shutdown()
            app.close()
        log.info("Smoke test finished in %.1f s", time.monotonic() - started)
    errors = [r for r in counter.records if r.levelno >= logging.ERROR]
    for record in errors:
        print(f"SMOKE TEST ERROR: {record.getMessage()}", file=sys.stderr)
    print("Smoke test: " + ("FAILED" if errors else "OK"), file=sys.stderr)
    return 1 if errors else 0


def _synthetic_session(recordings: Path) -> None:
    """One made-up meeting for the smoke test (no real data)."""
    folder = recordings / "2026-01-05_10-00_Budget-review"
    folder.mkdir(parents=True)
    meta = {
        "name": "Budget review", "date": "2026-01-05 10:00", "duration_s": 754,
        "tldr": "The team agreed on the Q1 budget and a second supplier quote.",
        "segments": [
            {"start": 5, "end": 9, "text": "Let us start with the budget for the first quarter.",
             "speaker": "me"},
            {"start": 12, "end": 18, "text": "The supplier raised the price by ten percent.",
             "speaker": "meeting"},
        ],
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


__all__ = ["App", "Options", "run", "smoke_test", "start_command"]
