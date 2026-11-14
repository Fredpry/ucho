# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""App window: recording, transcription, recordings list, settings."""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from . import (audio, config, i18n, icon as icon_mod, session, summary as summary_mod,
               transcribe, winshell)
from .i18n import t
from .merge import TRANSCRIPT_MD, Segment, format_hms, save_results, slug, speaker_label
from .tray import GlobalHotkey, TrayIcon

log = logging.getLogger(__name__)

BG = "#15171d"
PANEL = "#1e2129"
PANEL_LIGHT = "#272b35"
ACCENT = "#5ea4ff"
ACCENT_HOVER = "#7ab4ff"
RED = "#ef4444"
RED_HOVER = "#f26565"
TEXT = "#e6e9ef"
TEXT_MUTED = "#8b93a5"
GREEN = "#34d399"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.settings = config.Settings()
        config.prepare_dirs(self.settings)

        self.recorder: audio.Recorder | None = None
        self.recording = False
        self.session_dir: Path | None = None
        self.session_name = ""
        self.recording_start = 0.0
        self.cancel_transcription = threading.Event()
        self.transcribing = False
        self.summarizing = False
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._devices = {"microphones": [], "system": [],
                         "default_microphone": None, "default_system": None}

        self._build_window()
        self._build_header()
        self._build_views()
        self._show_view("record")

        self._tray = TrayIcon(
            config.APP_NAME,
            on_toggle=lambda: self.after(0, self.toggle_recording),
            on_show=lambda: self.after(0, self._show_window),
            on_quit=lambda: self.after(0, self.quit_app),
        )
        self._hotkey = GlobalHotkey(
            self.settings["hotkey"], lambda: self.after(0, self.toggle_recording)
        )
        if self.settings["tray"]:
            self._tray.start()
        self._hotkey.start()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        threading.Thread(target=self._load_devices, daemon=True).start()
        self.after(100, self._tick)
        # The window must already exist in the system before we describe it
        # for the taskbar.
        self.after(600, self._describe_for_taskbar)

    # --- building the UI --------------------------------------------------

    def _build_window(self) -> None:
        ctk.set_appearance_mode("dark")
        self.title(t("{app} - local recording and transcription", app=config.APP_NAME))
        # Fit to the screen: on a high-DPI laptop the work area can be short,
        # and a fixed 560x770 would cut off the bottom of the view.
        width = min(560, self.winfo_screenwidth() - 60)
        height = min(720, self.winfo_screenheight() - 110)
        self.geometry(f"{width}x{height}")
        self.minsize(500, min(600, height))
        self.configure(fg_color=BG)

        self._icon_file = Path(__file__).resolve().parent / "assets" / "icon.ico"
        if not self._icon_file.exists():
            try:
                icon_mod.save_ico(self._icon_file)
            except Exception:
                pass
        self._set_window_icon()
        # After ~200 ms CustomTkinter hides and shows the window to repaint the
        # title bar dark, and Windows can lose the icon along the way. We set
        # it again after that.
        self.after(400, self._set_window_icon)

    def _window_hwnd(self) -> int:
        """Handle of the window the taskbar sees.

        `winfo_id()` returns the Tk child window; taskbar properties must go
        to the parent window, which `wm_frame()` gives.
        """
        try:
            return int(self.wm_frame(), 16)
        except Exception:
            return self.winfo_id()

    def _describe_for_taskbar(self) -> None:
        """Taskbar data: what we are and how to start us again."""
        if sys.platform != "win32":
            return
        try:
            ok = winshell.describe_window(
                self._window_hwnd(),
                start_command(),
                config.APP_NAME,
                f"{self._icon_file},0",
                config.APP_ID,
            )
            log.info("Taskbar window description: %s", ok)
        except Exception:
            log.debug("Could not describe the window", exc_info=True)
        # Shortcuts get the same id, so a pinned tile and the running window
        # are one button. Done in the background: COM can take a moment.
        threading.Thread(target=_stamp_shortcuts, daemon=True).start()

    def _set_window_icon(self) -> None:
        if not self._icon_file.exists():
            return
        try:
            # `default` also covers child windows (dialogs).
            self.iconbitmap(default=str(self._icon_file))
            self.iconbitmap(str(self._icon_file))
        except Exception:
            pass

    def _build_header(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=56)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        ctk.CTkLabel(
            bar, text=f"  {config.APP_NAME}", text_color=TEXT,
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(side="left", padx=(14, 20))

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        for key, label in (("record", t("Record")),
                           ("recordings", t("Recordings")),
                           ("settings", t("Settings"))):
            button = ctk.CTkButton(
                bar, text=label, width=96, height=32, corner_radius=8,
                fg_color="transparent", hover_color=PANEL_LIGHT,
                text_color=TEXT_MUTED,
                font=ctk.CTkFont(size=13),
                command=lambda k=key: self._show_view(k),
            )
            button.pack(side="left", padx=2)
            self._nav_buttons[key] = button

    def _build_views(self) -> None:
        self._container = ctk.CTkFrame(self, fg_color=BG)
        self._container.pack(fill="both", expand=True)

        self._views = {
            "record": self._record_view(),
            "transcript": self._transcript_view(),
            "recordings": self._recordings_view(),
            "settings": self._settings_view(),
        }

    def _show_view(self, name: str) -> None:
        for view in self._views.values():
            view.pack_forget()
        self._views[name].pack(fill="both", expand=True, padx=16, pady=16)

        active_tab = "record" if name == "transcript" else name
        for key, button in self._nav_buttons.items():
            selected = key == active_tab
            button.configure(
                fg_color=PANEL_LIGHT if selected else "transparent",
                text_color=TEXT if selected else TEXT_MUTED,
            )
        if name == "recordings":
            self._refresh_recordings()

    # --- view: record -----------------------------------------------------

    def _record_view(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self._container, fg_color=BG)

        ctk.CTkLabel(frame, text=t("Meeting name"), text_color=TEXT_MUTED,
                     font=ctk.CTkFont(size=12)).pack(anchor="w")
        self._name_entry = ctk.CTkEntry(
            frame, height=36, corner_radius=10, fg_color=PANEL,
            border_color=PANEL_LIGHT, text_color=TEXT,
            placeholder_text=t("e.g. Team standup"),
        )
        self._name_entry.pack(fill="x", pady=(4, 12))

        card = ctk.CTkFrame(frame, fg_color=PANEL, corner_radius=16)
        card.pack(fill="x", pady=(0, 12))

        self._time_label = ctk.CTkLabel(
            card, text="00:00:00", text_color=TEXT,
            font=ctk.CTkFont(family="Consolas", size=34, weight="bold"),
        )
        self._time_label.pack(pady=(16, 0))

        self._status_label = ctk.CTkLabel(
            card, text=t("Ready to record"), text_color=TEXT_MUTED,
            font=ctk.CTkFont(size=12), wraplength=460,
        )
        self._status_label.pack(pady=(0, 12))

        # The short label matters: a longer text stretches CTkButton
        # horizontally and the circle turns into an oval.
        self._rec_button = ctk.CTkButton(
            card, text="REC", width=148, height=148, corner_radius=74,
            fg_color=RED, hover_color=RED_HOVER, text_color="#ffffff",
            font=ctk.CTkFont(size=22, weight="bold"),
            command=self.toggle_recording,
        )
        self._rec_button.pack(pady=(0, 6))

        ctk.CTkLabel(
            card, text=t("global shortcut: {shortcut}", shortcut=self._shortcut_text()),
            text_color=TEXT_MUTED, font=ctk.CTkFont(size=11),
        ).pack(pady=(0, 14))

        self._level_bars = {}
        for key, label in (("microphone", t("Microphone")), ("system", t("System audio"))):
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=label, text_color=TEXT_MUTED, width=130,
                         anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
            bar = ctk.CTkProgressBar(row, height=10, corner_radius=5,
                                     fg_color=PANEL, progress_color=GREEN)
            bar.pack(side="left", fill="x", expand=True)
            bar.set(0)
            self._level_bars[key] = bar

        language_row = ctk.CTkFrame(frame, fg_color="transparent")
        language_row.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(language_row, text=t("Transcription language"), text_color=TEXT_MUTED,
                     font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 6))
        self._language_picker = ctk.CTkSegmentedButton(
            language_row, values=list(config.LANGUAGE_LABELS.values()),
            fg_color=PANEL, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=PANEL, unselected_hover_color=PANEL_LIGHT,
            text_color=TEXT, command=self._on_language_change,
        )
        self._language_picker.pack(fill="x")
        self._language_picker.set(config.LANGUAGE_LABELS[self.settings["language"]])

        self._notice = ctk.CTkLabel(
            frame,
            text=t("Tell participants that the meeting is being recorded.\n"
                   "Use headphones — on speakers the microphone also picks up the other people."),
            text_color=TEXT_MUTED, font=ctk.CTkFont(size=11), justify="left",
        )
        self._notice.pack(anchor="w", pady=(14, 0))
        return frame

    def _shortcut_text(self) -> str:
        return (self.settings["hotkey"].replace("<", "").replace(">", "")
                .replace("+", " + ").upper())

    def _on_language_change(self, value: str) -> None:
        for code, label in config.LANGUAGE_LABELS.items():
            if label == value:
                self.settings["language"] = code
                self.settings.save()
                return

    # --- view: transcript -------------------------------------------------

    def _transcript_view(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self._container, fg_color=BG)

        self._transcript_title = ctk.CTkLabel(
            frame, text=t("Transcription"), text_color=TEXT,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self._transcript_title.pack(anchor="w")

        self._transcript_subtitle = ctk.CTkLabel(
            frame, text="", text_color=TEXT_MUTED, font=ctk.CTkFont(size=12)
        )
        self._transcript_subtitle.pack(anchor="w", pady=(2, 12))

        self._progress_bar = ctk.CTkProgressBar(
            frame, height=12, corner_radius=6, fg_color=PANEL, progress_color=ACCENT
        )
        self._progress_bar.pack(fill="x")
        self._progress_bar.set(0)

        self._progress_label = ctk.CTkLabel(
            frame, text="0%", text_color=TEXT_MUTED, font=ctk.CTkFont(size=12)
        )
        self._progress_label.pack(anchor="w", pady=(6, 12))

        # Summary card: inserted above the transcript once the model finishes.
        self._summary_card = ctk.CTkFrame(frame, fg_color=PANEL, corner_radius=12)
        self._participants_label = ctk.CTkLabel(
            self._summary_card, text="", text_color=ACCENT, anchor="w",
            justify="left", font=ctk.CTkFont(size=12, weight="bold"), wraplength=460,
        )
        self._participants_label.pack(fill="x", padx=14, pady=(10, 0))
        self._summary_label = ctk.CTkLabel(
            self._summary_card, text="", text_color=TEXT, anchor="w",
            justify="left", font=ctk.CTkFont(size=12), wraplength=460,
        )
        self._summary_label.pack(fill="x", padx=14, pady=(4, 10))

        self._transcript_box = ctk.CTkTextbox(
            frame, fg_color=PANEL, text_color=TEXT, corner_radius=12,
            font=ctk.CTkFont(size=12), wrap="word",
        )
        self._transcript_box.pack(fill="both", expand=True)

        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.pack(fill="x", pady=(12, 0))

        self._folder_button = ctk.CTkButton(
            buttons, text=t("Open folder"), height=36, corner_radius=10,
            fg_color=PANEL_LIGHT, hover_color=PANEL, text_color=TEXT,
            command=self._open_session_folder,
        )
        self._folder_button.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self._cancel_button = ctk.CTkButton(
            buttons, text=t("Stop"), height=36, corner_radius=10,
            fg_color=PANEL_LIGHT, hover_color=RED, text_color=TEXT,
            command=self._cancel_transcription,
        )
        self._cancel_button.pack(side="left", expand=True, fill="x", padx=(6, 0))
        return frame

    # --- view: recordings -------------------------------------------------

    def _recordings_view(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self._container, fg_color=BG)
        top = ctk.CTkFrame(frame, fg_color="transparent")
        top.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(top, text=t("Recordings"), text_color=TEXT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkButton(top, text=t("Refresh"), width=88, height=30, corner_radius=8,
                      fg_color=PANEL_LIGHT, hover_color=PANEL, text_color=TEXT,
                      command=self._refresh_recordings).pack(side="right")

        self._recordings_list = ctk.CTkScrollableFrame(frame, fg_color=BG)
        self._recordings_list.pack(fill="both", expand=True)
        return frame

    def _refresh_recordings(self) -> None:
        for child in self._recordings_list.winfo_children():
            child.destroy()

        folder = self.settings.recordings_dir
        if not folder.exists():
            return
        sessions = session.list_sessions(folder)
        if not sessions:
            ctk.CTkLabel(self._recordings_list, text=t("No recordings yet."),
                         text_color=TEXT_MUTED).pack(pady=20)
            return

        for session_dir in sessions[:60]:
            self._session_card(session_dir)

    @staticmethod
    def _describe_folder_name(folder_name: str) -> tuple[str, str]:
        """'2026-09-08_09-15_Budzet-Q4_Anna-Marek' -> ('Budzet Q4 Anna Marek', ...).

        Since the model names the folder, there can be two parts after the
        timestamp (topic and names), so we can no longer assume exactly three.
        """
        parts = folder_name.split("_")
        if len(parts) >= 3 and parts[1].count("-") == 1:
            date = f"{parts[0]} {parts[1].replace('-', ':')}"
            return " ".join(parts[2:]).replace("-", " "), date
        return folder_name, ""

    def _session_card(self, session_dir: Path) -> None:
        meta = session.read_meta(session_dir)
        name, date = self._describe_folder_name(session_dir.name)

        card = ctk.CTkFrame(self._recordings_list, fg_color=PANEL, corner_radius=12)
        card.pack(fill="x", pady=4)

        ctk.CTkLabel(card, text=meta.get("topic") or meta.get("name") or name,
                     text_color=TEXT, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            fill="x", padx=14, pady=(10, 0))

        description = meta.get("date") or date
        if meta.get("duration_s"):
            description += (f"  ·  {format_hms(meta['duration_s'])}"
                            f"  ·  {meta.get('device', '?')}")
        transcript_file = session.find_file(session_dir, TRANSCRIPT_MD)
        description += "  ·  " + (t("transcript ready") if transcript_file else t("no transcript"))
        if meta.get("participants"):
            description += "  ·  " + ", ".join(meta["participants"])
        ctk.CTkLabel(card, text=description, text_color=TEXT_MUTED, anchor="w",
                     font=ctk.CTkFont(size=11)).pack(fill="x", padx=14, pady=(2, 4))

        # The summary is visible right in the list: that is what it is for.
        summary_file = session.find_file(session_dir, session.SUMMARY_MD)
        if meta.get("tldr"):
            ctk.CTkLabel(card, text=meta["tldr"], text_color=TEXT, anchor="w",
                         justify="left", wraplength=430,
                         font=ctk.CTkFont(size=11)).pack(fill="x", padx=14, pady=(0, 6))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(buttons, text=t("Folder"), width=68, height=28, corner_radius=8,
                      fg_color=PANEL_LIGHT, hover_color=BG, text_color=TEXT,
                      font=ctk.CTkFont(size=11),
                      command=lambda s=session_dir: self._open(s)).pack(side="left", padx=2)
        if transcript_file:
            ctk.CTkButton(buttons, text=t("Transcript"), width=84, height=28, corner_radius=8,
                          fg_color=PANEL_LIGHT, hover_color=BG, text_color=TEXT,
                          font=ctk.CTkFont(size=11),
                          command=lambda f=transcript_file: self._open(f)
                          ).pack(side="left", padx=2)
        if summary_file:
            ctk.CTkButton(buttons, text=t("Summary"), width=108, height=28,
                          corner_radius=8, fg_color=PANEL_LIGHT, hover_color=BG,
                          text_color=TEXT, font=ctk.CTkFont(size=11),
                          command=lambda f=summary_file: self._open(f)
                          ).pack(side="left", padx=2)
        elif meta.get("segments"):
            ctk.CTkButton(buttons, text=t("Summarize"), width=86, height=28,
                          corner_radius=8, fg_color=PANEL_LIGHT, hover_color=ACCENT,
                          text_color=TEXT, font=ctk.CTkFont(size=11),
                          command=lambda s=session_dir: self._summarize_again(s)
                          ).pack(side="left", padx=2)
        ctk.CTkButton(buttons, text=t("Transcribe again"), width=140, height=28,
                      corner_radius=8, fg_color=PANEL_LIGHT, hover_color=ACCENT,
                      text_color=TEXT, font=ctk.CTkFont(size=11),
                      command=lambda s=session_dir: self._transcribe_again(s)
                      ).pack(side="left", padx=2)

    # --- view: settings ---------------------------------------------------

    def _settings_view(self) -> ctk.CTkFrame:
        frame = ctk.CTkScrollableFrame(self._container, fg_color=BG)

        def section(title: str) -> ctk.CTkFrame:
            ctk.CTkLabel(frame, text=title, text_color=TEXT, anchor="w",
                         font=ctk.CTkFont(size=14, weight="bold")).pack(
                fill="x", pady=(14, 6))
            box = ctk.CTkFrame(frame, fg_color=PANEL, corner_radius=12)
            box.pack(fill="x")
            return box

        def field(parent, label: str) -> ctk.CTkFrame:
            ctk.CTkLabel(parent, text=label, text_color=TEXT_MUTED, anchor="w",
                         font=ctk.CTkFont(size=11)).pack(fill="x", padx=12, pady=(10, 2))
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(0, 8))
            return row

        # Audio devices
        box = section(t("Audio devices"))
        self._microphone_menu = ctk.CTkOptionMenu(
            field(box, t("Microphone")), values=[t("(default)")], fg_color=PANEL_LIGHT,
            button_color=PANEL_LIGHT, button_hover_color=BG, text_color=TEXT,
            dynamic_resizing=False, command=lambda _: self._save_devices(),
        )
        self._microphone_menu.pack(fill="x")
        self._system_menu = ctk.CTkOptionMenu(
            field(box, t("System audio (loopback)")), values=[t("(default)")],
            fg_color=PANEL_LIGHT, button_color=PANEL_LIGHT, button_hover_color=BG,
            text_color=TEXT, dynamic_resizing=False,
            command=lambda _: self._save_devices(),
        )
        self._system_menu.pack(fill="x")
        self._tracks_menu = ctk.CTkOptionMenu(
            field(box, t("What to record")),
            values=[t(v) for v in config.TRACK_LABELS.values()],
            fg_color=PANEL_LIGHT, button_color=PANEL_LIGHT, button_hover_color=BG,
            text_color=TEXT, dynamic_resizing=False, command=self._save_tracks,
        )
        self._tracks_menu.set(t(config.TRACK_LABELS[self.settings["tracks"]]))
        self._tracks_menu.pack(fill="x")

        # Transcription
        box = section(t("Transcription"))
        model_labels = [f"{k.split('/')[-1]} - {t(v['description'])}"
                        for k, v in config.MODELS.items()]
        self._model_map = dict(zip(model_labels, config.MODELS.keys(), strict=True))
        self._model_menu = ctk.CTkOptionMenu(
            field(box, t("Model")), values=model_labels, fg_color=PANEL_LIGHT,
            button_color=PANEL_LIGHT, button_hover_color=BG, text_color=TEXT,
            dynamic_resizing=False, command=self._save_model,
        )
        for label, repo in self._model_map.items():
            if repo == self.settings["model"]:
                self._model_menu.set(label)
        self._model_menu.pack(fill="x")

        self._model_label = ctk.CTkLabel(
            box, text="", text_color=TEXT_MUTED, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self._model_label.pack(fill="x", padx=12, pady=(0, 6))

        self._download_button = ctk.CTkButton(
            box, text=t("Download model"), height=32, corner_radius=8, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color="#0b1220",
            command=self._download_model,
        )
        self._download_button.pack(fill="x", padx=12, pady=(0, 10))

        self._device_menu = ctk.CTkOptionMenu(
            field(box, t("Compute device")), values=["GPU", "CPU"],
            fg_color=PANEL_LIGHT, button_color=PANEL_LIGHT, button_hover_color=BG,
            text_color=TEXT, dynamic_resizing=False, command=self._save_device,
        )
        self._device_menu.pack(fill="x")

        self._device_label = ctk.CTkLabel(
            box, text="", text_color=TEXT_MUTED, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self._device_label.pack(fill="x", padx=12, pady=(0, 10))

        self._auto_switch = ctk.CTkSwitch(
            box, text=t("Transcribe automatically after stopping"),
            text_color=TEXT, progress_color=ACCENT, font=ctk.CTkFont(size=12),
            command=self._save_auto,
        )
        self._auto_switch.pack(anchor="w", padx=12, pady=(0, 12))
        if self.settings["transcribe_after_stop"]:
            self._auto_switch.select()

        # AI summary
        box = section(t("AI summary (optional)"))
        self._summary_switch = ctk.CTkSwitch(
            box, text=t("Summarize after transcription"), text_color=TEXT,
            progress_color=ACCENT, font=ctk.CTkFont(size=12),
            command=self._save_summary_enabled,
        )
        self._summary_switch.pack(anchor="w", padx=12, pady=(12, 4))
        if self.settings["summary_enabled"]:
            self._summary_switch.select()

        self._rename_switch = ctk.CTkSwitch(
            box, text=t("Name the folder after the topic and names"), text_color=TEXT,
            progress_color=ACCENT, font=ctk.CTkFont(size=12),
            command=self._save_rename_folder,
        )
        self._rename_switch.pack(anchor="w", padx=12, pady=(0, 8))
        if self.settings["rename_folder"]:
            self._rename_switch.select()

        llm_labels = [f"{k.split('/')[-1]} - {t(v['description'])}"
                      for k, v in config.LLM_MODELS.items()]
        self._llm_map = dict(zip(llm_labels, config.LLM_MODELS.keys(), strict=True))
        self._llm_menu = ctk.CTkOptionMenu(
            field(box, t("Language model")), values=llm_labels, fg_color=PANEL_LIGHT,
            button_color=PANEL_LIGHT, button_hover_color=BG, text_color=TEXT,
            dynamic_resizing=False, command=self._save_llm_model,
        )
        for label, repo in self._llm_map.items():
            if repo == self.settings["summary_model"]:
                self._llm_menu.set(label)
        self._llm_menu.pack(fill="x")

        self._llm_label = ctk.CTkLabel(
            box, text="", text_color=TEXT_MUTED, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self._llm_label.pack(fill="x", padx=12, pady=(0, 6))

        self._download_llm_button = ctk.CTkButton(
            box, text=t("Download model"), height=32, corner_radius=8, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color="#0b1220",
            command=self._download_llm_model,
        )
        self._download_llm_button.pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkLabel(
            box,
            text=t("The model starts only after transcription and is released right after it —\n"
                   "it does not stay in memory or in the background. Runs locally, offline."),
            text_color=TEXT_MUTED, anchor="w", justify="left",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=12, pady=(0, 12))

        # Other
        box = section(t("Other"))
        row = field(box, t("Recordings folder"))
        self._folder_entry = ctk.CTkEntry(
            row, height=32, fg_color=PANEL_LIGHT, border_color=PANEL_LIGHT,
            text_color=TEXT,
        )
        self._folder_entry.insert(0, str(self.settings.recordings_dir))
        self._folder_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text=t("Save"), width=72, height=32, corner_radius=8,
                      fg_color=PANEL_LIGHT, hover_color=ACCENT, text_color=TEXT,
                      command=self._save_folder).pack(side="left", padx=(6, 0))

        ui_labels = [t(v) for v in config.UI_LANGUAGE_LABELS.values()]
        self._ui_map = dict(zip(ui_labels, config.UI_LANGUAGE_LABELS.keys(), strict=True))
        self._ui_menu = ctk.CTkOptionMenu(
            field(box, t("Interface language (after restart)")),
            values=ui_labels, fg_color=PANEL_LIGHT, button_color=PANEL_LIGHT,
            button_hover_color=BG, text_color=TEXT, dynamic_resizing=False,
            command=self._save_ui_language,
        )
        self._ui_menu.set(t(config.UI_LANGUAGE_LABELS.get(
            self.settings["ui_language"], config.UI_LANGUAGE_LABELS["auto"])))
        self._ui_menu.pack(fill="x")

        ctk.CTkLabel(
            box,
            text=t("Global shortcut: {shortcut}", shortcut=self._shortcut_text()) + "\n"
                 f"OpenVINO GenAI: {transcribe.genai_version()}  ·  {config.APP_NAME} {config.VERSION}",
            text_color=TEXT_MUTED, anchor="w", justify="left",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=12, pady=(4, 12))

        self.after(300, self._refresh_model_status)
        self.after(300, self._refresh_llm_status)
        return frame

    # --- settings: handlers -----------------------------------------------

    def _load_devices(self) -> None:
        try:
            devices = audio.list_devices()
        except Exception as error:
            log.exception("Could not enumerate audio devices")
            self._queue.put(("error", t("Audio device error: {error}", error=error)))
            return
        self._queue.put(("devices", devices))

    def _apply_devices(self, devices: dict) -> None:
        self._devices = devices

        def fill(menu, device_list, selected_index):
            labels = [t("(default)")] + [d.label for d in device_list]
            menu.configure(values=labels)
            selected = t("(default)")
            for device in device_list:
                if device.index == selected_index:
                    selected = device.label
            menu.set(selected)

        fill(self._microphone_menu, devices["microphones"],
             self.settings["microphone_index"])
        fill(self._system_menu, devices["system"],
             self.settings["system_index"])

        if not devices["system"]:
            self._status_label.configure(
                text=t("Warning: no loopback device — system audio unavailable"),
                text_color=RED,
            )

    def _save_devices(self) -> None:
        def index_for(menu, device_list):
            selected = menu.get()
            if selected == t("(default)"):
                return None
            for device in device_list:
                if device.label == selected:
                    return device.index
            return None

        self.settings["microphone_index"] = index_for(
            self._microphone_menu, self._devices["microphones"])
        self.settings["system_index"] = index_for(
            self._system_menu, self._devices["system"])
        self.settings.save()

    def _save_tracks(self, value: str) -> None:
        for code, label in config.TRACK_LABELS.items():
            if t(label) == value:
                self.settings["tracks"] = code
        self.settings.save()

    def _save_model(self, value: str) -> None:
        self.settings["model"] = self._model_map.get(value, config.DEFAULT_MODEL)
        self.settings.save()
        self._refresh_model_status()

    def _save_device(self, value: str) -> None:
        self.settings["device"] = value
        self.settings.save()

    def _save_auto(self) -> None:
        self.settings["transcribe_after_stop"] = bool(self._auto_switch.get())
        self.settings.save()

    def _save_summary_enabled(self) -> None:
        self.settings["summary_enabled"] = bool(self._summary_switch.get())
        self.settings.save()

    def _save_rename_folder(self) -> None:
        self.settings["rename_folder"] = bool(self._rename_switch.get())
        self.settings.save()

    def _save_llm_model(self, value: str) -> None:
        self.settings["summary_model"] = self._llm_map.get(
            value, config.DEFAULT_LLM_MODEL)
        self.settings.save()
        self._refresh_llm_status()

    def _save_ui_language(self, value: str) -> None:
        self.settings["ui_language"] = self._ui_map.get(value, "auto")
        self.settings.save()

    def _save_folder(self) -> None:
        path = Path(self._folder_entry.get().strip())
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._status_label.configure(text=t("Invalid folder: {error}", error=error),
                                         text_color=RED)
            return
        self.settings["recordings_dir"] = str(path)
        self.settings.save()

    def _refresh_model_status(self) -> None:
        model = self.settings["model"]
        info = config.MODELS.get(model, {})
        if transcribe.model_downloaded(model):
            self._model_label.configure(
                text=t("Model downloaded ({size})", size=info.get('size', '')), text_color=GREEN)
            # Greyed out, so it does not look like a button waiting for a click.
            self._download_button.configure(
                state="disabled", text=t("Model ready"),
                fg_color=PANEL_LIGHT, text_color_disabled=TEXT_MUTED)
        else:
            self._model_label.configure(
                text=t("Model not downloaded ({size})", size=info.get('size', '')),
                text_color=RED)
            self._download_button.configure(
                state="normal", text=t("Download model ({size})", size=info.get('size', '')),
                fg_color=ACCENT, text_color="#0b1220")

        # Importing openvino takes a few seconds; we do not block the UI with it.
        self._device_label.configure(text=t("Checking available devices..."))
        threading.Thread(
            target=lambda: self._queue.put(
                ("ov_devices", transcribe.available_devices())),
            daemon=True,
        ).start()

    def _refresh_llm_status(self) -> None:
        model = self.settings["summary_model"]
        info = config.LLM_MODELS.get(model, {})
        if transcribe.model_downloaded(model):
            self._llm_label.configure(
                text=t("Model downloaded ({size})", size=info.get('size', '')), text_color=GREEN)
            self._download_llm_button.configure(
                state="disabled", text=t("Model ready"),
                fg_color=PANEL_LIGHT, text_color_disabled=TEXT_MUTED)
        else:
            self._llm_label.configure(
                text=t("Model not downloaded ({size}) — transcription works without it, "
                       "just with no summary", size=info.get('size', '')),
                text_color=RED)
            self._download_llm_button.configure(
                state="normal", text=t("Download model ({size})", size=info.get('size', '')),
                fg_color=ACCENT, text_color="#0b1220")

    def _download_model(self) -> None:
        model = self.settings["model"]
        self._download_button.configure(state="disabled", text=t("Downloading..."))

        def job():
            try:
                transcribe.model_path(model, download=True)
                self._queue.put(("model_downloaded", model))
            except Exception as error:
                self._queue.put(("error", t("Model download failed: {error}", error=error)))
                self._queue.put(("model_downloaded", model))

        threading.Thread(target=job, daemon=True).start()

    def _download_llm_model(self) -> None:
        model = self.settings["summary_model"]
        self._download_llm_button.configure(state="disabled", text=t("Downloading..."))

        def job():
            try:
                transcribe.model_path(model, download=True)
            except Exception as error:
                self._queue.put(
                    ("error", t("Language model download failed: {error}", error=error)))
            self._queue.put(("llm_downloaded", model))

        threading.Thread(target=job, daemon=True).start()

    # --- recording --------------------------------------------------------

    def toggle_recording(self) -> None:
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if self.recording or self.transcribing:
            return

        name = self._name_entry.get().strip() or t("Meeting")
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        folder = self.settings.recordings_dir / f"{stamp}_{slug(name) or 'Meeting'}"

        self.recorder = audio.Recorder(
            tracks=self.settings["tracks"],
            microphone_index=self.settings["microphone_index"],
            system_index=self.settings["system_index"],
        )
        try:
            self.recorder.start(folder)
        except Exception as error:
            log.exception("Could not start recording")
            self._status_label.configure(text=str(error), text_color=RED)
            self.recorder = None
            return

        self.recording = True
        self.session_name = name
        self.session_dir = folder
        self.recording_start = time.time()

        self._rec_button.configure(text="STOP", fg_color=PANEL_LIGHT, hover_color=RED)
        status = t("Recording...")
        if self.recorder.warnings:
            status += "  (" + "; ".join(self.recorder.warnings) + ")"
        self._status_label.configure(text=status, text_color=RED)
        self._name_entry.configure(state="disabled")
        self._tray.set_state(True, t("{app} - recording", app=config.APP_NAME))
        self._show_view("record")

    def stop_recording(self) -> None:
        if not self.recording or self.recorder is None:
            return

        self.recording = False
        result = self.recorder.stop()
        self.recorder = None

        self._rec_button.configure(text="REC", fg_color=RED, hover_color=RED_HOVER)
        self._status_label.configure(text=t("Stopped"), text_color=TEXT_MUTED)
        self._name_entry.configure(state="normal")
        self._tray.set_state(False)
        for bar in self._level_bars.values():
            bar.set(0)

        files = {n: d["file"] for n, d in result["tracks"].items()}
        if not files:
            self._status_label.configure(text=t("Nothing was recorded."), text_color=RED)
            return

        meta = {
            "name": self.session_name,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "duration_s": result["duration_s"],
            "model": self.settings["model"],
            "language": self.settings["language"],
            "microphone_device": result["tracks"].get("microphone", {}).get("device"),
            "system_device": result["tracks"].get("system", {}).get("device"),
            "genai": transcribe.genai_version(),
        }
        (result["folder"] / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        if self.settings["transcribe_after_stop"]:
            self._start_transcription(result["folder"], files, meta)
        else:
            self._show_view("recordings")

    # --- transcription ----------------------------------------------------

    def _transcribe_again(self, session_dir: Path) -> None:
        files = session.audio_tracks(session_dir)
        if not files:
            self._status_label.configure(text=t("No audio files in this session."),
                                         text_color=RED)
            return

        meta = session.read_meta(session_dir)
        meta.pop("segments", None)
        meta.setdefault("name", session_dir.name)
        meta["model"] = self.settings["model"]
        meta["language"] = self.settings["language"]
        self._start_transcription(session_dir, files, meta)

    def _start_transcription(self, folder: Path, files: dict, meta: dict) -> None:
        if self.transcribing:
            return

        # Settings are read once, at start, and the app can sit in the tray for
        # weeks. If the file changed in the meantime (by hand or from a second
        # instance), after a recording we would reach for a stale model,
        # including one no longer on disk. Every UI change goes to disk at
        # once, so the file is always at least as fresh as memory.
        self.settings.load()

        if not transcribe.model_downloaded(self.settings["model"]):
            # Without this log line there is no trace that transcription was
            # supposed to start at all, and the window message is lost while
            # the app sits in the tray.
            log.warning("Transcription skipped: model %r is not downloaded",
                        self.settings["model"])
            self._status_label.configure(
                text=t("The model is not downloaded — open Settings."), text_color=RED)
            self._show_view("settings")
            return

        self.transcribing = True
        self.session_dir = folder
        self.cancel_transcription.clear()

        self._transcript_title.configure(text=meta.get("name", folder.name))
        self._transcript_subtitle.configure(
            text=f"{self.settings['device']}  ·  {self.settings['model'].split('/')[-1]}")
        self._transcript_box.delete("1.0", "end")
        self._summary_card.pack_forget()
        self._progress_bar.set(0)
        self._progress_label.configure(text=t("Loading model..."))
        self._cancel_button.configure(state="normal", text=t("Stop"))
        self._show_view("transcript")

        model = self.settings["model"]
        device = self.settings["device"]
        language = self.settings["language"]

        def job():
            start = time.monotonic()
            try:
                segments, used_device = transcribe.transcribe_session(
                    files, model, device, language,
                    progress=lambda fraction, text: self._queue.put(
                        ("progress", fraction, text)),
                    cancel=self.cancel_transcription,
                    status=lambda text: self._queue.put(("transcription_status", text)),
                )
            except Exception as error:
                log.exception("Transcription failed")
                self._queue.put(("transcription_error", str(error)))
                return

            full = dict(meta)
            full["device"] = used_device
            full["transcription_time_s"] = round(time.monotonic() - start, 1)
            try:
                save_results(folder, segments, full)
            except Exception as error:
                self._queue.put(("transcription_error", t("Saving failed: {error}", error=error)))
                return
            # The transcript is shown right away; the summary arrives in a
            # separate message, because the language model needs another half
            # minute. The flag tells the view whether to keep "Stop" active.
            self._queue.put(
                ("transcription_done", segments, full, self._summary_expected(segments)))
            self._summarize(folder, segments, full)

        threading.Thread(target=job, daemon=True).start()

    # --- summary ----------------------------------------------------------

    def _summary_expected(self, segments: list[Segment]) -> bool:
        return bool(
            segments
            and self.settings["summary_enabled"]
            and transcribe.model_downloaded(self.settings["summary_model"])
        )

    def _summarize(self, folder: Path, segments: list[Segment], meta: dict,
                   force: bool = False) -> None:
        """Runs the local language model and tidies the session. Call from a thread!

        No error in this step may spoil a finished transcript: at worst the
        session stays without a summary and with its old name.
        """
        if not segments or not (force or self.settings["summary_enabled"]):
            return
        self.summarizing = True
        try:
            self._summarize_now(folder, segments, meta)
        finally:
            self.summarizing = False

    def _summarize_now(self, folder: Path, segments: list[Segment], meta: dict) -> None:
        model = self.settings["summary_model"]
        if not transcribe.model_downloaded(model):
            self._queue.put(
                ("summary_error", t("The summary model is not downloaded — see Settings.")))
            return

        try:
            result = summary_mod.summarize(
                segments, model, self.settings["device"],
                status=lambda text: self._queue.put(("transcription_status", text)),
                cancel=self.cancel_transcription,
            )
        except Exception as error:
            log.exception("Summary failed")
            self._queue.put(("summary_error", str(error)))
            return

        if self.cancel_transcription.is_set():
            self._queue.put(("summary_error", t("stopped")))
            return
        if not result:
            self._queue.put(("summary_error", t("The model returned no summary.")))
            return

        try:
            (folder / session.SUMMARY_MD).write_text(
                summary_mod.as_markdown(result, meta), encoding="utf-8")
            meta.update(result)
            # Second save pass: transcript.md gets the summary block, meta.json
            # the new fields. save_results is idempotent.
            save_results(folder, segments, meta)
        except OSError as error:
            log.exception("Could not save the summary")
            self._queue.put(("summary_error", t("Saving the summary: {error}", error=error)))
            return

        if self.settings["rename_folder"]:
            folder = self._rename_session(folder, result)

        self._queue.put(("summary", folder, result))

    @staticmethod
    def _rename_session(folder: Path, result: dict) -> Path:
        """Replaces the name part with the topic and names. Returns the current path."""
        new_name = summary_mod.folder_name(folder.name, result)
        if not new_name:
            return folder

        target = folder.parent / new_name
        counter = 2
        while target.exists():
            target = folder.parent / f"{new_name}-{counter}"
            counter += 1

        try:
            folder.rename(target)
        except OSError as error:
            # Most common reason: the folder is open in Explorer. That is not a
            # transcription error: the files are in place, only the name stays.
            log.warning("Could not rename %s -> %s: %s",
                        folder.name, new_name, error)
            return folder

        log.info("Session renamed: %s -> %s", folder.name, target.name)
        return target

    def _summarize_again(self, session_dir: Path) -> None:
        """Summary on its own, without running the audio through Whisper again."""
        if self.transcribing or self.summarizing:
            return
        meta = session.read_meta(session_dir)
        segments = session.segments(meta)
        if not segments:
            self._status_label.configure(
                text=t("No segments in meta.json — use “Transcribe again”."),
                text_color=RED)
            self._show_view("recordings")
            return

        self.summarizing = True
        self.session_dir = session_dir
        self.cancel_transcription.clear()
        self._transcript_title.configure(text=meta.get("name", session_dir.name))
        self._transcript_subtitle.configure(
            text=self.settings["summary_model"].split("/")[-1])
        self._progress_bar.set(1.0)
        self._progress_label.configure(text=t("Summarizing..."))
        self._summary_card.pack_forget()
        self._cancel_button.configure(state="normal", text=t("Stop"))
        self._show_transcript(segments)
        self._show_view("transcript")

        threading.Thread(
            target=lambda: self._summarize(session_dir, segments, meta, force=True),
            daemon=True,
        ).start()

    def _cancel_transcription(self) -> None:
        if self.transcribing or self.summarizing:
            self.cancel_transcription.set()
            self._progress_label.configure(text=t("Stopping..."))
            self._cancel_button.configure(state="disabled")

    # --- UI loop ----------------------------------------------------------

    def _tick(self) -> None:
        if self.recording and self.recorder is not None:
            self._time_label.configure(
                text=format_hms(time.time() - self.recording_start))
            levels = self.recorder.levels
            for key, bar in self._level_bars.items():
                bar.set(levels.get(key, 0.0))

        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle_message(message)

        self.after(100, self._tick)

    def _handle_message(self, message: tuple) -> None:
        kind = message[0]

        if kind == "devices":
            self._apply_devices(message[1])

        elif kind == "transcription_status":
            self._progress_label.configure(text=message[1])

        elif kind == "ov_devices":
            devices = message[1]
            self._device_menu.configure(values=devices)
            chosen = self.settings["device"]
            self._device_menu.set(chosen if chosen in devices else devices[0])
            self._device_label.configure(
                text=t("Detected by OpenVINO: {list}", list=", ".join(devices)))

        elif kind == "progress":
            _, fraction, text = message
            self._progress_bar.set(max(0.0, min(1.0, fraction)))
            self._progress_label.configure(text=f"{int(fraction * 100)}%")
            if text:
                self._transcript_box.insert("end", text.strip() + "\n\n")
                self._transcript_box.see("end")

        elif kind == "transcription_done":
            _, segments, meta, summary_expected = message
            self.transcribing = False
            self._progress_bar.set(1.0)
            elapsed = meta.get("transcription_time_s", 0)
            self._progress_label.configure(
                text=t("Done in {time} s  ·  {device}  ·  {n} utterances",
                       time=f"{elapsed:.0f}", device=meta.get('device', '?'),
                       n=len(segments)))
            self._show_transcript(segments)
            # With a summary coming, the button stays active; otherwise there
            # would be no way to stop the language model that is just starting.
            if not summary_expected:
                self._cancel_button.configure(state="disabled", text=t("Finished"))

        elif kind == "transcription_error":
            self.transcribing = False
            self._progress_label.configure(text=t("Error: {error}", error=message[1]))
            self._cancel_button.configure(state="disabled", text=t("Error"))

        elif kind == "summary":
            _, folder, result = message
            # The folder may have been renamed; otherwise "Open folder" would
            # point to a path that no longer exists.
            self.session_dir = folder
            self._show_summary(result)
            self._cancel_button.configure(state="disabled", text=t("Finished"))

        elif kind == "summary_error":
            log.warning("Summary skipped: %s", message[1])
            self._progress_label.configure(
                text=t("{text}  ·  no summary ({reason})",
                       text=self._progress_label.cget('text'), reason=message[1]))
            self._cancel_button.configure(state="disabled", text=t("Finished"))

        elif kind == "model_downloaded":
            self._refresh_model_status()

        elif kind == "llm_downloaded":
            self._refresh_llm_status()

        elif kind == "error":
            self._status_label.configure(text=message[1], text_color=RED)

    def _show_summary(self, result: dict) -> None:
        if result.get("topic"):
            self._transcript_title.configure(text=result["topic"])

        header = t("Participants: {list}", list=(", ".join(result["participants"])
                                                 if result.get("participants")
                                                 else t("no names recognized")))
        self._participants_label.configure(text=header)

        lines = [result.get("tldr", "")]
        if result.get("key_points"):
            lines += [""] + [f"•  {point}" for point in result["key_points"]]
        if result.get("decisions"):
            lines += ["", t("Decisions:")] + [f"→  {d}" for d in result["decisions"]]
        self._summary_label.configure(text="\n".join(lines).strip())

        self._summary_card.pack(fill="x", pady=(0, 10), before=self._transcript_box)

    def _show_transcript(self, segments: list[Segment]) -> None:
        self._transcript_box.delete("1.0", "end")
        if not segments:
            self._transcript_box.insert("end", t("No speech was recognized.") + "\n")
            return
        for seg in segments:
            self._transcript_box.insert(
                "end", f"[{format_hms(seg.start)}] {speaker_label(seg.speaker)}: {seg.text}\n\n")
        self._transcript_box.see("1.0")

    # --- helpers ----------------------------------------------------------

    def _open(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except Exception as error:
            log.warning("Could not open %s: %s", path, error)

    def _open_session_folder(self) -> None:
        if self.session_dir is not None:
            self._open(self.session_dir)

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_close(self) -> None:
        if self.settings["tray"]:
            self.withdraw()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        if self.recording:
            # Do not lose a recording in progress: close the files properly.
            self.stop_recording()
        self.cancel_transcription.set()
        self._hotkey.stop()
        self._tray.stop()
        self.settings.save()
        self.destroy()


def start_command() -> str:
    """Command the taskbar uses to bring the app back from a pinned tile.

    Deliberately `ucho.pyw`, not `-m ucho`: the tile starts without the project
    working directory, and the script adds it to `sys.path` itself.
    """
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.exists():
        interpreter = windowless
    starter = config.PROJECT_DIR / "ucho.pyw"
    return f'"{interpreter}" "{starter}"'


def _stamp_shortcuts() -> None:
    for shortcut in winshell.shortcut_paths():
        if shortcut.exists():
            log.info("Shortcut %s id: %s", shortcut.name,
                     winshell.set_shortcut_app_id(shortcut, config.APP_ID))


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


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        filename=str(config.LOG_FILE),
        filemode="a",
        encoding="utf-8",
    )
    used = i18n.set_language(config.Settings()["ui_language"])
    log.info("Start %s %s (UI: %s)", config.APP_NAME, config.VERSION, used)
    _set_app_id()
    App().mainloop()
