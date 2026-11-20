# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Screens of the main window: Meeting, Dictation, Recordings, Transcript.

Screens hold widgets only. State lives in the controllers (ucho/meeting.py,
ucho/dictation.py); a screen reads it in `tick()` (about 15 times a second
while visible) and listens to controller events for content changes.
"""

from __future__ import annotations

import logging
import threading
import tkinter
from pathlib import Path

import customtkinter as ctk

from . import config, platform_text, platforms, retention, session, theme, ui
from .archive import Match, fold, query_words
from .hotkeys import label as hotkey_label
from .i18n import t
from .meeting import same_folder
from .merge import (TRANSCRIPT_MD, TRANSCRIPT_PARTIAL, display_title, format_hms, speaker_label,
                    speaker_track)
from .theme import C

log = logging.getLogger(__name__)

PAD = 28  # outer padding of a screen
# Below this screen height (logical px) the big buttons shrink and the meeting
# screen lists two recent recordings instead of three. 1366 x 768 at 125 %
# leaves about 535.
COMPACT_HEIGHT = 600
# Utterances shown in the live transcript on the Meeting screen.
LIVE_LINES = 60


class Screen(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self.app = app

    def on_show(self, **kwargs) -> None:
        pass

    def tick(self) -> None:
        pass


def header(master, title: str, subtitle: str | None = None) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
    """Title row; returns (row, right-hand slot)."""
    row = ctk.CTkFrame(master, fg_color="transparent")
    row.grid_columnconfigure(0, weight=1)
    left = ctk.CTkFrame(row, fg_color="transparent")
    left.grid(row=0, column=0, sticky="w")
    ui.heading(left, title).pack(anchor="w")
    if subtitle:
        ui.muted(left, subtitle, theme.SM - 1).pack(anchor="w", pady=(0, 0))
    # width/height 1: an empty CTkFrame would otherwise keep its default 200 x 200.
    slot = ctk.CTkFrame(row, fg_color="transparent", width=1, height=1)
    slot.grid(row=0, column=1, sticky="e")
    return row, slot


def open_path(path: Path) -> None:
    try:
        platforms.shell().open_path(path)  # the user's own folder in the system file manager
    except Exception as error:
        log.warning("Could not open a folder in Explorer: %s", type(error).__name__)


def describe(meta: dict, folder: Path) -> tuple[str, str]:
    """(title, "date · duration") of a session."""
    title = display_title(meta) or session_title(folder.name)
    parts = [meta.get("date") or session.date_from_name(folder.name)]
    if meta.get("duration_s"):
        parts.append(format_hms(meta["duration_s"]))
    return title, "  ·  ".join(p for p in parts if p)


def _busy_with(app, folder: Path) -> bool:
    """Recorded, transcribed or summarized right now (a queued session can be deleted)."""
    return app.meeting.in_use(folder) and not app.meeting.queue_position(folder)


def delete_recording(app, folder: Path, parent) -> bool:
    """Asks, then deletes a whole session folder. True when it was deleted.

    Refused while the session is being recorded, transcribed or summarized.
    The search index needs no extra step: it rebuilds when the list of
    sessions changes (archive.Archive.index).
    """
    if _busy_with(app, folder):
        app.toast(t("This recording is being recorded, transcribed or summarized. "
                    "Delete it when that has finished."), "warning", 6)
        return False
    meta = session.read_meta(folder)
    title, _line = describe(meta, folder)
    date = meta.get("date") or session.date_from_name(folder.name)
    if date:
        message = t("The recording “{title}” from {date} will be deleted permanently: audio, "
                    "transcript and summary. This cannot be undone.", title=title, date=date)
    else:
        message = t("The recording “{title}” will be deleted permanently: audio, transcript and "
                    "summary. This cannot be undone.", title=title)
    if not ui.confirm(parent, t("Delete recording?"), message, t("Delete permanently"),
                      destructive=True):
        return False
    # The dialog was modal but the app kept running: check again.
    if _busy_with(app, folder):
        app.toast(t("This recording is being recorded, transcribed or summarized. "
                    "Delete it when that has finished."), "warning", 6)
        return False
    app.meeting.dequeue(folder)  # waiting for a transcription: drop it from the queue first
    try:
        retention.delete_session(app.settings.recordings_dir, folder)
    except retention.PartialDeleteError as error:
        # The recording is out of the list, but some files are still on the disk.
        app.show("recordings")
        app.toast(t("The recording was only partly deleted. The rest is in the folder {folder}. "
                    "Ucho deletes it at the next start; you can also close the programs that use it "
                    "and delete the folder yourself.", folder=str(error.rest)), "warning", 15)
        return True
    except retention.DeleteError as error:
        log.warning("Recording not deleted: %s", error)
        app.toast(t("The recording could not be deleted. Close programs that use its files "
                    "and try again."), "warning", 6)
        return False
    app.show("recordings")
    app.toast(t("Recording deleted"))
    return True


def transcription_status(meeting, folder: Path) -> str:
    """Short status for a recordings card: running, queued, ready or missing."""
    if meeting.recording and same_folder(meeting.recording_dir, folder):
        return t("recording…")
    if meeting.is_active(folder):
        return t("transcribing…") if meeting.transcribing else t("summarizing…")
    position = meeting.queue_position(folder)
    if position:
        return t("Queued, #{n}", n=position)
    if not session.find_file(folder, TRANSCRIPT_MD):
        return t("no transcript")
    # Live text left by a recording that was never transcribed after the stop.
    return t("partial transcript") if session.read_meta(folder).get(TRANSCRIPT_PARTIAL) \
        else t("transcript ready")


def session_title(folder_name: str) -> str:
    """'2026-09-08_09-15_Budget-Q4_Anna' -> 'Budget Q4 Anna'."""
    parts = folder_name.split("_")
    if len(parts) >= 3 and parts[1].count("-") == 1:
        return " ".join(parts[2:]).replace("-", " ")
    return folder_name


# --- Meeting ----------------------------------------------------------------------


class MeetingScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.meeting = app.meeting
        self.grid_columnconfigure(0, weight=5, uniform="cols")
        self.grid_columnconfigure(1, weight=4, uniform="cols")
        self.grid_rowconfigure(1, weight=1)

        top, slot = header(self, t("Record a meeting"))
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=PAD, pady=(PAD - 4, 14))
        self._top = top
        self.pill = ui.StatusPill(slot)
        self.pill.pack()

        # Recorder card.
        recorder = ui.card(self, corner_radius=theme.RADIUS_LG)
        recorder.grid(row=1, column=0, sticky="nsew", padx=(PAD, 10), pady=(0, PAD - 6))
        recorder.grid_columnconfigure(0, weight=1)
        # Rows 2 and 6 take the spare height, so the button group sits in the
        # middle and the level meters at the bottom, at any window height.
        recorder.grid_rowconfigure((2, 6), weight=1)
        ui.muted(recorder, t("Meeting name"), anchor="w").grid(
            row=0, column=0, sticky="ew", padx=22, pady=(18, 4))
        self.name = ui.entry(recorder, t("e.g. Team standup"))
        self.name.grid(row=1, column=0, sticky="ew", padx=22)
        compact = app.compact
        self.button = ui.RecordButton(recorder, self._toggle, diameter=104 if compact else 136,
                                      idle_text="REC", active_text="STOP")
        self.button.grid(row=3, column=0, pady=(4, 0))
        self.timer = ui.label(recorder, "00:00:00", theme.XXL + (4 if compact else 8),
                              family="numeric")
        self.timer.grid(row=4, column=0, pady=(0, 0))
        self.hint = ui.muted(recorder, "")
        self.hint.grid(row=5, column=0)
        meters = ctk.CTkFrame(recorder, fg_color="transparent")
        meters.grid(row=7, column=0, sticky="ew", padx=22, pady=(10, 0))
        meters.grid_columnconfigure(0, weight=1)
        self.meters = {
            "microphone": ui.LevelMeter(meters, t("Microphone")),
            "system": ui.LevelMeter(meters, t("System audio")),
        }
        for i, meter in enumerate(self.meters.values()):
            meter.grid(row=i, column=0, sticky="ew", pady=(0, 6))
        self.notice = ui.label(recorder, "", theme.XS + 1, color=C.warning, wraplength=360,
                               justify="left", anchor="w")
        self.notice.grid(row=8, column=0, sticky="ew", padx=22, pady=(0, 14))
        recorder.bind("<Configure>", lambda e: self.notice.configure(
            wraplength=max(200, ui.logical(self, e.width) - 50)), add="+")

        # Side column.
        side = ctk.CTkFrame(self, fg_color="transparent")
        side.grid(row=1, column=1, sticky="nsew", padx=(10, PAD), pady=(0, PAD - 6))
        side.grid_columnconfigure(0, weight=1)
        recent_row = ctk.CTkFrame(side, fg_color="transparent")
        recent_row.grid(row=0, column=0, sticky="ew")
        self._recent_row = recent_row
        recent_row.grid_columnconfigure(0, weight=1)
        ui.label(recent_row, t("Recent recordings"), theme.SM + 1, "bold", anchor="w").grid(
            row=0, column=0, sticky="w")
        ui.ghost(recent_row, t("All recordings"), lambda: app.show("recordings"), width=10).grid(
            row=0, column=1, sticky="e")
        self.recent = ctk.CTkFrame(side, fg_color="transparent")
        self.recent.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.recent.grid_columnconfigure(0, weight=1)

        # Live transcript while recording (ucho/live_meeting.py): takes the place
        # of the recent recordings until the stop.
        self._side = side
        self.live_box = ctk.CTkFrame(side, fg_color="transparent")
        self.live_box.grid_columnconfigure(0, weight=1)
        self.live_box.grid_rowconfigure(2, weight=1)
        ui.label(self.live_box, t("Live transcript"), theme.SM + 1, "bold", anchor="w").grid(
            row=0, column=0, sticky="w")
        self.live_note = ui.muted(self.live_box, "", anchor="w", justify="left", wraplength=280)
        self.live_note.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.live_text = ui.TranscriptText(self.live_box, height=120)
        self.live_text.grid(row=2, column=0, sticky="nsew")
        self._live_shown = False
        self._live_shown_lines: list | None = None

        language = ctk.CTkFrame(side, fg_color="transparent")
        language.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ui.muted(language, t("Transcription language"), anchor="w").pack(fill="x", pady=(0, 4))
        self.language = ui.segmented(language, list(config.LANGUAGE_LABELS.values()),
                                     config.LANGUAGE_LABELS.get(app.settings["language"], "Auto (PL/EN)"),
                                     self._language)
        self.language.pack(fill="x")

        note = ctk.CTkFrame(side, fg_color=C.info_soft, corner_radius=theme.RADIUS_MD)
        note.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        note.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(note, text="", image=ui.icon("info", 16, C.info)).grid(
            row=0, column=0, sticky="n", padx=(12, 6), pady=12)
        self.consent = ui.label(note, t("Tell participants that the meeting is being recorded. "
                                        "Use headphones, so the microphone does not pick up the others."),
                                theme.XS + 1, color=C.text, justify="left", anchor="w", wraplength=280)
        self.consent.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=10)
        side.bind("<Configure>", self._wrap, add="+")

        self.meeting.listen(self._event)
        self._recent_shown: list | None = None
        self._compact: bool | None = None
        self.bind("<Configure>", self._resize, add="+")

    def _resize(self, event) -> None:
        compact = ui.logical(self, event.height) < COMPACT_HEIGHT
        if compact != self._compact:
            self._compact = compact
            self.button.set_diameter(104 if compact else 136)
            self._top.grid_configure(pady=(16, 10) if compact else (PAD - 4, 14))
            self.timer.configure(font=theme.font(theme.XXL + (2 if compact else 8), family="numeric"))
            self._recent_shown = None
            self.refresh_recent()

    def _wrap(self, event) -> None:
        self.consent.configure(wraplength=max(160, ui.logical(self, event.width) - 56))
        self.live_note.configure(wraplength=max(160, ui.logical(self, event.width) - 8))

    def _toggle(self) -> None:
        self.meeting.toggle(self.name.get())

    def _language(self, value: str) -> None:
        for code, text in config.LANGUAGE_LABELS.items():
            if text == value:
                self.app.settings["language"] = code
                self.app.settings.save()

    def _event(self, event: str, data) -> None:
        if event in ("state", "saved", "summary", "transcribed", "titled", "renamed"):
            self.refresh_recent()
        elif event == "live":
            self._show_live_text(data)

    def _show_live_text(self, segments) -> None:
        # The last utterances are enough here; the whole text is in the session folder.
        shown = segments[-LIVE_LINES:]
        signature = [(seg.start, seg.speaker, seg.text) for seg in shown]
        if signature == self._live_shown_lines:
            return
        self._live_shown_lines = signature
        self.live_text.clear()
        for seg in shown:
            self.live_text.line(format_hms(seg.start), seg.speaker, speaker_label(seg.speaker), seg.text)
        self.live_text.see("end")

    def _update_live(self) -> None:
        """Shows the live transcript instead of the recent recordings while it runs."""
        m = self.meeting
        live = m.live if m.recording else None
        if (live is not None) != self._live_shown:
            self._live_shown = live is not None
            if self._live_shown:
                self._recent_row.grid_remove()
                self.recent.grid_remove()
                self.live_box.grid(row=0, column=0, rowspan=2, sticky="nsew")
                self._side.grid_rowconfigure(1, weight=1)
                self._live_shown_lines = None
                self._show_live_text(list(m.live_segments))
            else:
                self.live_box.grid_remove()
                self._side.grid_rowconfigure(1, weight=0)
                self._recent_row.grid()
                self.recent.grid()
        if live is None:
            return
        if live.gave_up:
            note = t("Stopped for this recording: the computer is too slow for it right now. "
                     "The transcript is made after the stop.")
        elif not m.live_segments:
            note = t("In progress. Text appears here a little while after it is said.")
        else:
            note = t("In progress, a little behind the speech. The final transcript is made "
                     "after the stop.")
        if self.live_note.cget("text") != note:
            self.live_note.configure(text=note)

    def on_show(self, **kwargs) -> None:
        self.refresh_recent()
        if kwargs.get("focus"):
            ui.focus(self.button)

    def refresh_recent(self) -> None:
        sessions = self.app.archive.sessions()[:2 if self._compact else 3]
        signature = [(s.name, s.stat().st_mtime_ns if s.exists() else 0) for s in sessions]
        if signature == self._recent_shown:
            return
        self._recent_shown = signature
        for child in self.recent.winfo_children():
            child.destroy()
        if not sessions:
            ui.muted(self.recent, t("No recordings yet."), anchor="w").grid(
                row=0, column=0, sticky="w", pady=6)
            return
        for i, folder in enumerate(sessions):
            meta = session.read_meta(folder)
            title, line = describe(meta, folder)
            card = RecordingCard(self.recent, self.app, folder, title, line, meta.get("tldr") or "",
                                 compact=True)
            card.grid(row=i, column=0, sticky="ew", pady=(0, 8))

    def tick(self) -> None:
        m = self.meeting
        # No speech model yet: the button is off and the notice says why (#28).
        blocked = None if m.recording else self.app.recording_blocked(False)
        self.button.set_state(m.recording, enabled=blocked is None)
        self.button.render()
        self.timer.configure(text=format_hms(m.elapsed()))
        levels = m.levels()
        for key, meter in self.meters.items():
            meter.set(levels.get(key, 0.0))
        self.pill.show(*self.app.status_pill())
        error = self.app.hotkey_errors.get("record")
        shortcut = hotkey_label(self.app.settings["hotkey"])
        self.hint.configure(text=error or t("Shortcut: {shortcut}", shortcut=shortcut))
        self.name.configure(state="disabled" if m.recording else "normal")
        message = blocked or (m.message[0] if m.message else "")
        if not self.app.audio_ok and not m.recording:
            message = message or t("No audio devices found. Connect a microphone or headphones.")
        if self.notice.cget("text") != message:
            self.notice.configure(text=message)
        self._update_live()


# --- Recording card (used in the list and as "recent") -------------------------------


class RecordingCard(ctk.CTkFrame):
    """Clickable card: title, date and duration, one-line summary or search hits."""

    def __init__(self, master, app, folder: Path, title: str, line: str, summary: str = "",
                 hits: list | None = None, compact: bool = False):
        super().__init__(master, fg_color=C.surface, corner_radius=theme.RADIUS_MD,
                         border_width=1, border_color=C.border)
        self.app, self.folder = app, folder
        self.grid_columnconfigure(0, weight=1)
        pad = 12 if compact else 16
        last = ui.label(self, title, theme.SM if compact else theme.SM + 1, "bold", anchor="w")
        last.grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad - 2, 0))
        last = ui.muted(self, line, theme.XS, anchor="w")
        last.grid(row=1, column=0, sticky="ew", padx=pad)
        row = 2
        self.summary = None
        self._excerpts: list = []
        if summary:
            limit = 84 if compact else 240
            text = summary if len(summary) <= limit else summary[:limit - 1].rstrip() + "…"
            self.summary = last = ui.label(self, text, theme.XS + 1,
                                           color=C.text_muted if compact else C.text,
                                           anchor="w", justify="left", wraplength=480)
            last.grid(row=row, column=0, sticky="ew", padx=pad, pady=(4, 0))
            row += 1
        for hit in hits or []:
            hit_row = ctk.CTkFrame(self, fg_color="transparent")
            hit_row.grid(row=row, column=0, sticky="ew", padx=pad, pady=(4, 0))
            hit_row.grid_columnconfigure(2, weight=1)
            ui.label(hit_row, format_hms(hit.start), theme.XS, color=C.text_muted,
                     family="mono").grid(row=0, column=0, sticky="nw", padx=(0, 8))
            code = hit.speaker
            ui.label(hit_row, hit.name or speaker_label(code), theme.XS + 1, "bold",
                     color=C.action if speaker_track(code) == "me" else C.info).grid(
                row=0, column=1, sticky="nw", padx=(0, 6))
            excerpt = hit.text if len(hit.text) < 160 else hit.text[:157] + "…"
            excerpt_label = ui.label(hit_row, excerpt, theme.XS + 1, anchor="w", justify="left",
                                     wraplength=440)
            excerpt_label.grid(row=0, column=2, sticky="ew")
            self._excerpts.append(excerpt_label)
            last = hit_row
            row += 1
        # Bottom padding on the last row, not a spacer frame: a spacer would
        # paint over the card's rounded bottom border.
        info = last.grid_info()
        top = info.get("pady", 0)
        top = top[0] if isinstance(top, tuple) else top
        last.grid_configure(pady=(top, pad - 2))
        self.bind("<Configure>", self._wrap, add="+")
        self._bind_all(self)
        ui.focusable(self, self.open)
        # Delete on a focused card: the same confirmation as the button on the transcript screen.
        tkinter.Misc.bind(self, "<Delete>", self._delete_key, add="+")

    def _delete_key(self, _event=None) -> str:
        delete_recording(self.app, self.folder, self)
        return "break"

    def _bind_all(self, widget) -> None:
        for sequence, handler in (("<Button-1>", lambda e=None: e is not None and self.open()),
                                  ("<Enter>", lambda _e=None: self._hover(True)),
                                  ("<Leave>", lambda _e=None: self._hover(False))):
            widget.bind(sequence, handler, add="+")
        try:
            widget.configure(cursor="hand2")
        except (ValueError, Exception):
            pass
        for child in widget.winfo_children():
            self._bind_all(child)

    def _wrap(self, event) -> None:
        width = ui.logical(self, event.width)
        if self.summary is not None:
            self.summary.configure(wraplength=max(180, width - 36))
        for excerpt in self._excerpts:
            excerpt.configure(wraplength=max(160, width - 190))

    def _hover(self, inside: bool) -> None:
        # customtkinter can deliver Enter/Leave without an event, also while the
        # list is being rebuilt; a card that is already gone has nothing to repaint.
        if self.winfo_exists():
            self.configure(fg_color=C.hover if inside else C.surface)

    def open(self) -> None:
        self.app.show("transcript", folder=self.folder, query=self.app.last_query)


# --- Recordings ---------------------------------------------------------------------


class RecordingsScreen(Screen):
    LIMIT = 60

    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        top, slot = header(self, t("Recordings"))
        top.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD - 4, 12))
        self.count = ui.muted(slot, "")
        self.count.pack(side="left", padx=(0, 8))
        ui.secondary(slot, t("Open folder"), lambda: open_path(app.settings.recordings_dir),
                     "folder", width=10).pack(side="left")

        search = ctk.CTkFrame(self, fg_color="transparent")
        search.grid(row=1, column=0, sticky="ew", padx=PAD, pady=(0, 10))
        search.grid_columnconfigure(0, weight=1)
        self.query = ui.entry(search, t("Search titles and transcripts…"))
        self.query.configure(height=40)
        self.query.grid(row=0, column=0, sticky="ew")
        self.query.bind("<KeyRelease>", lambda _e: self._schedule(), add=True)
        self.query.bind("<Escape>", lambda _e: (self.query.delete(0, "end"), self._schedule()), add=True)

        self.list = ui.scrollable(self)
        self.list.grid(row=2, column=0, sticky="nsew", padx=(PAD - 6, PAD - 12), pady=(0, PAD - 10))
        self.list.grid_columnconfigure(0, weight=1)
        self._after = None
        self._generation = 0
        # "state" too: the status of queued and running transcriptions changes with it.
        app.meeting.listen(lambda event, _d: event in (
            "state", "saved", "summary", "transcribed", "titled", "renamed"
        ) and self.winfo_ismapped() and self.search())

    def on_show(self, **kwargs) -> None:
        if "query" in kwargs:
            self.query.delete(0, "end")
            if kwargs["query"]:
                self.query.insert(0, kwargs["query"])
        self.search()
        self.query.focus_set()

    def _schedule(self) -> None:
        if self._after is not None:
            self.after_cancel(self._after)
        self._after = self.after(250, self.search)

    def search(self) -> None:
        self._after = None
        query = self.query.get().strip()
        self.app.last_query = query
        self._generation += 1
        generation = self._generation
        archive = self.app.archive

        def work():
            try:
                matches = archive.find(query, limit=self.LIMIT)
                metas = [(m, session.read_meta(m.session)) for m in matches]
                total = len(archive.sessions())
            except Exception:
                log.exception("Search failed")
                metas, total = [], 0
            # A layout switch destroys the old screens while a search may still run.
            self.app.post(lambda: generation == self._generation and self.winfo_exists()
                          and self._show(query, metas, total))

        threading.Thread(target=work, name="search", daemon=True).start()

    def _show(self, query: str, results: list[tuple[Match, dict]], total: int) -> None:
        for child in self.list.winfo_children():
            child.destroy()
        if query:
            self.count.configure(text=t("{n} found", n=len(results)))
        else:
            self.count.configure(text=t("{n} recordings", n=total))
        if not results:
            empty = ctk.CTkFrame(self.list, fg_color="transparent")
            empty.grid(row=0, column=0, pady=40)
            ui.label(empty, t("Nothing found for “{query}”.", query=query) if query
                     else t("No recordings yet."), theme.SM + 1, "bold").pack()
            ui.muted(empty, t("Search looks in titles, summaries and every transcript, "
                              "with or without Polish letters.") if query else
                     t("Start one on the Meeting screen or with the shortcut.")).pack(pady=(4, 0))
            return
        words = [fold(w) for w in query_words(query)]
        for i, (match, meta) in enumerate(results):
            title, line = describe(meta, match.session)
            status = transcription_status(self.app.meeting, match.session)
            if retention.audio_removed(meta):
                status += "  ·  " + t("audio deleted")
            line = f"{line}  ·  {status}" if line else status
            if meta.get("participants"):
                line += "  ·  " + ", ".join(meta["participants"])
            card = RecordingCard(self.list, self.app, match.session, title, line,
                                 meta.get("tldr") or "", hits=match.hits if words else None)
            card.grid(row=i, column=0, sticky="ew", pady=(0, 10), padx=(6, 6))


# --- Transcript ---------------------------------------------------------------------


class TranscriptScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.meeting = app.meeting
        self.folder: Path | None = None
        self.segments: list = []
        self.names: dict[str, str] = {}
        self.live = False
        self.query = ""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(6, weight=1)

        back = ui.ghost(self, t("Recordings"), lambda: app.show("recordings"), "back",
                        color=C.text_muted, width=10)
        back.grid(row=0, column=0, sticky="w", padx=PAD - 10, pady=(PAD - 12, 0))
        self.title = ui.heading(self, "", theme.XL + 2)
        self.title.grid(row=1, column=0, sticky="ew", padx=PAD)
        self.subtitle = ui.muted(self, "", theme.SM - 1, anchor="w")
        self.subtitle.grid(row=2, column=0, sticky="ew", padx=PAD)

        # One row used to hold the buttons, the progress bar and its text, packed
        # left and right; in a narrow window pack squeezed the text under the
        # Delete button and cut both its ends, the percentage too (#14). Now the
        # progress has its own row and Delete wraps under the buttons when the
        # row is too narrow for both.
        self.actions = ctk.CTkFrame(self, fg_color="transparent")
        self.actions.grid(row=3, column=0, sticky="ew", padx=PAD, pady=(12, 12))
        self.actions.grid_columnconfigure(0, weight=1)
        self.buttons = ctk.CTkFrame(self.actions, fg_color="transparent")
        self.buttons.grid(row=0, column=0, sticky="w")
        self.copy = ui.secondary(self.buttons, t("Copy all"), self._copy, "copy", width=10)
        self.copy.pack(side="left", padx=(0, 8))
        self.open = ui.secondary(self.buttons, t("Open folder"), self._open, "folder", width=10)
        self.open.pack(side="left", padx=(0, 8))
        self.rename = ui.secondary(self.buttons, t("Rename"), self._rename, "edit", width=10)
        self.again = ui.secondary(self.buttons, t("Transcribe again"), self._again, "refresh",
                                  width=10)
        self.again.pack(side="left", padx=(0, 8))
        self.summarize = ui.secondary(self.buttons, t("Summarize"), self._summarize, "summary",
                                      width=10)
        self.stop = ui.secondary(self.buttons, t("Stop"), self.meeting.cancel, "stop", width=10)
        # Right-aligned, away from the everyday actions.
        self.delete = ui.danger(self.actions, t("Delete recording"), self._delete, "delete",
                                width=10)
        self._delete_wrapped: bool | None = None
        self.actions.bind("<Configure>", self._fit_actions, add="+")
        self.buttons.bind("<Configure>", self._fit_actions, add="+")

        self.progress_row = ctk.CTkFrame(self, fg_color="transparent")
        self.progress = ctk.CTkProgressBar(self.progress_row, width=160, height=8)
        self.progress.pack(side="left", padx=(0, 12))
        self.progress.set(0)
        self.progress_label = ui.muted(self.progress_row, "", anchor="w", justify="left")
        self.progress_label.pack(side="left", fill="x", expand=True)
        self.progress_row.bind("<Configure>", self._wrap_progress, add="+")

        self.speakers_row = ctk.CTkFrame(self, fg_color="transparent")

        self.text = ui.TranscriptText(self)
        self.text.grid(row=6, column=0, sticky="nsew", padx=PAD, pady=(0, PAD - 8))
        self.meeting.listen(self._event)

    # --- showing ------------------------------------------------------------

    def on_show(self, folder: Path | None = None, live: bool = False, query: str = "", **_) -> None:
        self.query = query or ""
        if live or (folder is not None and self.meeting.busy and folder == self.meeting.session_dir):
            self._show_live()
        elif folder is not None:
            self._show_session(folder)
        self._update_actions()

    def _show_live(self) -> None:
        m = self.meeting
        self.live = True
        self.folder = m.session_dir
        self.segments = []
        self.names = {}
        self.title.configure(text=m.last_meta.get("name") or (self.folder.name if self.folder else ""))
        model = platform_text.model_tail(self.app.settings["model"])
        self.subtitle.configure(text=f"{self.app.settings['device']}  ·  {model}")
        self.text.clear()
        for line in m.live_text:
            self.text.write((line + "\n", "body"))

    def _show_session(self, folder: Path) -> None:
        self.live = False
        self.folder = folder
        meta = session.read_meta(folder)
        title, line = describe(meta, folder)
        details = [line] if line else []
        if meta.get("device"):
            details.append(str(meta["device"]))
        if meta.get("model"):
            details.append(platform_text.model_tail(str(meta["model"])))
        if retention.audio_removed(meta):
            details.append(t("audio deleted"))
        self.title.configure(text=title)
        self.subtitle.configure(text="  ·  ".join(details))
        self.segments = session.segments(meta)
        self.names = session.speaker_names(meta)
        self._render(meta)

    def _can_rename_speakers(self) -> bool:
        return (not self.live and self.folder is not None and bool(self.segments)
                and not self.meeting.in_use(self.folder))

    def _update_speakers(self) -> None:
        for child in self.speakers_row.winfo_children():
            child.destroy()
        if not self._can_rename_speakers():
            self.speakers_row.grid_remove()
            return
        codes = session.speaker_codes(self.segments)
        if not codes:
            self.speakers_row.grid_remove()
            return
        ui.muted(self.speakers_row, t("Speakers:"), theme.XS + 1, anchor="w").pack(
            side="left", padx=(0, 8))
        for code in codes[:8]:
            ui.secondary(
                self.speakers_row,
                speaker_label(code, self.names),
                lambda c=code: self._rename(c),
                height=28,
                width=10,
            ).pack(side="left", padx=(0, 6))
        if len(codes) > 8:
            ui.muted(self.speakers_row, f"+{len(codes) - 8}", theme.XS + 1, anchor="w").pack(
                side="left", padx=(2, 0))
        self.speakers_row.grid(row=5, column=0, sticky="ew", padx=PAD, pady=(0, 10))

    def _render(self, meta: dict) -> None:
        self._update_speakers()
        text = self.text
        text.clear()
        if meta.get("tldr") or meta.get("key_points") or meta.get("decisions"):
            text.write((t("Summary") + "\n", "title"))
            if meta.get("participants"):
                text.write((t("Participants: {list}", list=", ".join(meta["participants"])) + "\n",
                            "muted"))
            if meta.get("tldr"):
                text.write((meta["tldr"] + "\n", "summary"))
            for point in meta.get("key_points") or []:
                text.write(("•  " + point + "\n", "summary"))
            if meta.get("decisions"):
                text.write((t("Decisions:") + "\n", "muted"))
                for decision in meta["decisions"]:
                    text.write(("→  " + decision + "\n", "summary"))
            text.write(("\n", "body"), (t("Transcript") + "\n", "title"))
        on_click = self._rename if self._can_rename_speakers() else None
        if self.segments:
            for seg in self.segments:
                text.line(format_hms(seg.start), seg.speaker,
                          speaker_label(seg.speaker, self.names), seg.text, on_click=on_click)
        elif self.folder is not None:
            file = session.find_file(self.folder, TRANSCRIPT_MD)
            if file is not None:
                try:
                    text.write((file.read_text(encoding="utf-8"), "body"))
                except OSError:
                    pass
            elif self.meeting.queue_position(self.folder):
                text.write((t("Waiting in the queue: the transcription starts when the one before "
                              "it finishes.") + "\n", "muted"))
            elif session.audio_tracks(self.folder):
                text.write((t("No transcript yet. Use “Transcribe again”.") + "\n", "muted"))
            else:
                text.write((t("No transcript, and the audio files are gone, so this recording "
                              "cannot be transcribed.") + "\n", "muted"))
        if self.query:
            text.highlight(query_words(self.query))
        else:
            text.see("1.0")

    def _update_actions(self) -> None:
        busy = self.meeting.is_active(self.folder)
        position = self.meeting.queue_position(self.folder)
        has_audio = bool(self.folder and session.audio_tracks(self.folder))
        has_summary = bool(self.folder and session.find_file(self.folder, session.SUMMARY_MD))
        self.rename.pack_forget()
        self.again.pack_forget()
        self.summarize.pack_forget()
        self.stop.pack_forget()
        self.progress_row.grid_remove()
        if busy:
            self.stop.pack(side="left", padx=(0, 8))
            self.progress_row.grid(row=4, column=0, sticky="ew", padx=PAD, pady=(0, 12))
        elif position:
            self.progress.set(0)
            self.progress_label.configure(text=t("Queued, #{n}", n=position))
            self.progress_row.grid(row=4, column=0, sticky="ew", padx=PAD, pady=(0, 12))
        else:
            if self.folder is not None:
                self.rename.pack(side="left", padx=(0, 8))
            if has_audio:
                self.again.pack(side="left", padx=(0, 8))
            if self.segments and not has_summary:
                self.summarize.pack(side="left", padx=(0, 8))
        self._update_speakers()
        self.after_idle(self._fit_actions)

    def _fit_actions(self, _event=None) -> None:
        """Delete next to the buttons when both fit in one row, under them otherwise."""
        if not self.winfo_exists():  # an after_idle call that outlived the window
            return
        width = self.actions.winfo_width()
        if width <= 1:  # not laid out yet; the <Configure> event comes back
            return
        gap = round(16 * ctk.ScalingTracker.get_widget_scaling(self))
        wrapped = self.buttons.winfo_reqwidth() + gap + self.delete.winfo_reqwidth() > width
        if wrapped == self._delete_wrapped:
            return
        self._delete_wrapped = wrapped
        if wrapped:
            self.delete.grid(row=1, column=0, sticky="e", pady=(8, 0))
        else:
            self.delete.grid(row=0, column=1, sticky="e")

    def _wrap_progress(self, event) -> None:
        # Long status texts (an error, a summary step) wrap instead of running off the edge.
        width = ui.logical(self.progress_row, event.width) - self.progress.cget("width") - 12
        if width > 50:
            self.progress_label.configure(wraplength=width)

    # --- events ----------------------------------------------------------------

    def _event(self, event: str, data) -> None:
        if event == "titled":
            old, new, _name = data
            if self.folder is not None and same_folder(self.folder, old) and not self.live:
                self._show_session(new)
                self._update_actions()
            return
        if event == "renamed":
            folder, _name = data
            if self.folder is not None and same_folder(self.folder, folder) and not self.live:
                self._show_session(folder)
                self._update_actions()
            return
        m = self.meeting
        if not self.live:
            if event == "state" and self.folder is not None:
                if m.transcribing and m.is_active(self.folder):
                    # This session waited in the queue and has just started.
                    self._show_live()
                self._update_actions()
            elif event == "speakers" and same_folder(data, self.folder):
                self._show_session(self.folder)
                self._update_actions()
            return
        if event == "state" and not m.is_active(self.folder):
            # Finished or failed; what comes next belongs to another session.
            self.live = False
        if event == "progress":
            fraction, text = data
            if text and m.is_active(self.folder):
                self.text.write((text + "\n", "body"))
                self.text.see("end")
        elif event == "transcribed":
            folder, segments, meta, _expected = data
            self.folder, self.segments = folder, segments
            self.names = session.speaker_names(meta)
            self.live = bool(_expected)
            self.title.configure(text=meta.get("name") or folder.name)
            self._render(meta)
            self._update_actions()
        elif event == "summary":
            folder, _result = data
            self.live = False
            self._show_session(folder)
            self._update_actions()
        elif event in ("state", "error", "summary_error"):
            self._update_actions()

    def tick(self) -> None:
        m = self.meeting
        if self.live and m.busy:
            self.progress.set(m.progress if m.transcribing else 1.0)
            label = f"{int(m.progress * 100)}%  ·  {m.shown_status}" if m.transcribing else m.status_text
            if self.progress_label.cget("text") != label:
                self.progress_label.configure(text=label)

    # --- actions ---------------------------------------------------------------

    def _rename(self, code: str) -> None:
        if self.folder is None:
            return
        if self.meeting.in_use(self.folder):
            self.app.toast(t("This recording is busy. Try again when it is finished."), "warning")
            return
        current = speaker_label(code, self.names)
        value = ui.ask_text(
            self,
            t("Rename speaker"),
            t("Name for “{label}” in this recording. Leave it empty to go back to “{default}”.",
              label=current, default=speaker_label(code)),
            self.names.get(code, ""),
            t("Save"),
        )
        if value is None or self.folder is None:
            return
        if self.meeting.rename_speaker(self.folder, code, value):
            self._show_session(self.folder)
            self._update_actions()
            self.app.toast(t("Speaker renamed"))
        else:
            self.app.toast(t("Could not rename the speaker."), "warning")

    def _copy(self) -> None:
        from . import dictation

        if self.segments:
            text = "\n".join(f"[{format_hms(s.start)}] {speaker_label(s.speaker, self.names)}: {s.text}"
                             for s in self.segments)
        else:
            text = self.text.all_text()
        try:
            dictation.set_clipboard(text)
            self.app.toast(t("Copied ({n} characters)", n=len(text)))
        except Exception:
            log.exception("Copy failed")
            self.app.toast(t("Clipboard busy, try again"), "warning")

    def _open(self) -> None:
        if self.folder is not None:
            open_path(self.folder)

    def _rename(self) -> None:
        if self.folder is None:
            return
        dialog = ctk.CTkInputDialog(text=t("New name for this recording:"), title=t("Rename"))
        value = dialog.get_input()
        if value and value.strip() and self.meeting.rename(self.folder, value):
            self._show_session(self.folder)
            self._update_actions()

    def _again(self) -> None:
        if self.folder is not None and self.meeting.transcribe_again(self.folder):
            if self.meeting.is_active(self.folder):
                self.on_show(live=True)
            else:
                # Queued behind another session; it turns live when it starts.
                self._show_session(self.folder)
                self._update_actions()

    def _summarize(self) -> None:
        if self.folder is not None and self.meeting.summarize_again(self.folder):
            self.live = True
            self._update_actions()

    def _delete(self) -> None:
        if self.folder is not None and delete_recording(self.app, self.folder, self):
            self.folder, self.segments, self.names, self.live = None, [], {}, False


# --- Dictation --------------------------------------------------------------------------

DICTATION_LANGUAGES = {"auto": "Auto", "pl": "PL", "en": "EN"}


class DictationPanel(ctk.CTkFrame):
    """Dictation controls; full size on the Dictation screen, compact in the small window."""

    def __init__(self, master, app, compact: bool = False):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.dictation = app.dictation
        self.compact = compact
        self.grid_columnconfigure(0, weight=1)
        if compact:
            self._build_compact()
        else:
            self._build_full()
        self.dictation.listen(self._event)
        self._shown_text = None

    def _build_full(self) -> None:
        """Two columns: the controls on the left, the last text on the right.

        Side by side rather than stacked, so it fits a 1366 x 768 screen at
        125 % (about 1093 x 535 logical pixels for the window content).
        """
        app = self.app
        self.grid_columnconfigure(0, weight=5, uniform="dictation")
        self.grid_columnconfigure(1, weight=4, uniform="dictation")
        self.grid_rowconfigure(0, weight=1)
        card = ui.card(self, corner_radius=theme.RADIUS_LG)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure((0, 5), weight=1)
        self.button = ui.RecordButton(card, self.dictation.toggle, diameter=120,
                                      idle_text="", active_text="", glyph="mic")
        self.button.grid(row=1, column=0, pady=(10, 0))
        self.headline = ui.label(card, "", theme.LG, "bold")
        self.headline.grid(row=2, column=0)
        self.hint = ui.muted(card, "", wraplength=360, justify="center")
        self.hint.grid(row=3, column=0, padx=20, pady=(2, 10))
        self.meter = ui.LevelMeter(card, t("Microphone"))
        self.meter.grid(row=4, column=0, sticky="ew", padx=28, pady=(0, 6))
        options = ctk.CTkFrame(card, fg_color="transparent")
        options.grid(row=6, column=0, sticky="ew", padx=28, pady=(8, 18))
        options.grid_columnconfigure(1, weight=1)
        self.language = ui.segmented(options, list(DICTATION_LANGUAGES.values()),
                                     DICTATION_LANGUAGES.get(app.settings["dictation_language"], "Auto"),
                                     self._language, width=160)
        self.language.grid(row=0, column=0, sticky="w")
        self.enter = ui.switch(options, t("Enter after pasting"), app.settings["dictation_enter"],
                               self._enter)
        self.enter.grid(row=0, column=2, sticky="e")
        card.bind("<Configure>", lambda e: self.hint.configure(
            wraplength=max(200, ui.logical(self, e.width) - 48)), add="+")

        side = ctk.CTkFrame(self, fg_color="transparent")
        side.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(0, weight=1)
        last = ui.card(side)
        last.grid(row=0, column=0, sticky="nsew")
        last.grid_columnconfigure(0, weight=1)
        last.grid_rowconfigure(1, weight=1)
        ui.muted(last, t("Last dictation"), anchor="w").grid(row=0, column=0, sticky="w",
                                                              padx=16, pady=(12, 0))
        self.copy = ui.secondary(last, t("Copy"), self._copy, "copy", width=10, height=30)
        self.copy.grid(row=0, column=1, sticky="e", padx=12, pady=(10, 0))
        self.text = ui.label(last, "", theme.SM, anchor="nw", justify="left", wraplength=320)
        self.text.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=16, pady=(6, 14))
        last.bind("<Configure>", lambda e: self.text.configure(
            wraplength=max(180, ui.logical(self, e.width) - 36)), add="+")
        self.model = ui.muted(side, "", theme.XS, anchor="w", justify="left", wraplength=320)
        self.model.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        side.bind("<Configure>", lambda e: self.model.configure(
            wraplength=max(180, ui.logical(self, e.width) - 8)), add="+")

    def set_compact(self, compact: bool) -> None:
        """Smaller button on short windows (1366 x 768 at 125 %)."""
        if not self.compact:
            self.button.set_diameter(96 if compact else 120)

    def _build_compact(self) -> None:
        app = self.app
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        self.button = ui.RecordButton(top, self.dictation.toggle, diameter=56, idle_text="",
                                      active_text="", glyph="mic")
        self.button.grid(row=0, column=0, rowspan=2, padx=(0, 6))
        self.headline = ui.label(top, "", theme.LG - 1, "bold", anchor="w")
        self.headline.grid(row=0, column=1, sticky="sw")
        self.hint = ui.muted(top, "", theme.XS, anchor="w", justify="left", wraplength=250)
        self.hint.grid(row=1, column=1, sticky="nw")
        self.meter = ui.LevelMeter(self, t("Microphone"))
        self.meter.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        last = ui.card(self)
        last.grid(row=2, column=0, sticky="nsew")
        last.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        ui.muted(last, t("Last dictation"), theme.XS, anchor="w").grid(
            row=0, column=0, sticky="w", padx=12, pady=(8, 0))
        self.copy = ui.secondary(last, t("Copy"), self._copy, "copy", width=10, height=26)
        self.copy.grid(row=0, column=1, sticky="e", padx=(0, 8), pady=(6, 0))
        self.text = ui.label(last, "", theme.XS + 1, anchor="nw", justify="left", wraplength=330)
        self.text.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=12, pady=(2, 10))
        last.bind("<Configure>", lambda e: self.text.configure(
            wraplength=max(200, ui.logical(self, e.width) - 28)), add="+")
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.language = ui.segmented(bottom, list(DICTATION_LANGUAGES.values()),
                                     DICTATION_LANGUAGES.get(app.settings["dictation_language"], "Auto"),
                                     self._language, width=150)
        self.language.pack(side="left")
        self.enter = ui.switch(bottom, t("Enter after pasting"), app.settings["dictation_enter"],
                               self._enter)
        self.enter.pack(side="right")
        self.model = None

    def _language(self, value: str) -> None:
        for code, text in DICTATION_LANGUAGES.items():
            if text == value:
                self.app.settings["dictation_language"] = code
                self.app.settings.save()
                self.app.sync_dictation_widgets()

    def _enter(self, value: bool) -> None:
        self.app.settings["dictation_enter"] = value
        self.app.settings.save()
        self.app.sync_dictation_widgets()

    def sync(self) -> None:
        """Mirrors settings changed in another dictation panel."""
        self.language.set(DICTATION_LANGUAGES.get(self.app.settings["dictation_language"], "Auto"))
        if bool(self.enter.get()) != bool(self.app.settings["dictation_enter"]):
            self.enter.toggle()

    def _copy(self) -> None:
        self.dictation.copy_last()

    def _event(self, event: str, data) -> None:
        pass

    def tick(self) -> None:
        d = self.dictation
        headline, style, hint = d.status()
        color = {"recording": C.recording, "warning": C.warning, "working": C.info}.get(style, C.text)
        if self.headline.cget("text") != headline:
            self.headline.configure(text=headline, text_color=color)
        if self.hint.cget("text") != hint:
            self.hint.configure(text=hint)
        blocked = not d.recording and self.app.recording_blocked(False) is not None
        self.button.set_state(d.recording, enabled=not blocked)
        self.button.render()
        self.meter.set(d.level())
        showing_live = bool((d.recording or d.queued) and d.live_text)
        active_text = d.live_text if showing_live else d.last_text
        text = active_text or t("Nothing dictated yet. Put the cursor where the text should go "
                                "and press the shortcut.")
        if (text, showing_live) != self._shown_text:
            self._shown_text = (text, showing_live)
            if len(text) < 600:
                shown = text
            elif showing_live:
                shown = "…" + text[-597:]
            else:
                shown = text[:597] + "…"
            self.text.configure(text=shown, text_color=C.text if active_text else C.text_muted)
            self.copy.configure(state="normal" if d.last_text else "disabled")
        if self.model is not None:
            model, device = d.model_key()
            shared = model == self.app.settings["model"]
            state = {"ready": t("loaded"), "loading": t("loading"), "error": t("not loaded"),
                     "idle": t("loads on first use")}.get(d.model_state, "")
            from .settings_screen import short_model

            line = t("Model: {model} on {device} ({state})", model=short_model(model),
                     device=d.model_device or device, state=state)
            if shared:
                line += "  ·  " + t("shared with transcription")
            if self.model.cget("text") != line:
                self.model.configure(text=line)


class DictationScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        top, slot = header(self, t("Dictation"))
        top.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD - 4, 14))
        self.pill = ui.StatusPill(slot)
        self.pill.pack()
        self.panel = DictationPanel(self, app)
        self.panel.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=(0, PAD - 6))
        self.bind("<Configure>", lambda e: self.panel.set_compact(
            ui.logical(self, e.height) < COMPACT_HEIGHT), add="+")

    def tick(self) -> None:
        self.pill.show(*self.app.status_pill())
        self.panel.tick()

    def on_show(self, **kwargs) -> None:
        if kwargs.get("focus"):
            ui.focus(self.panel.button)


# macOS: a window of a background app that becomes key activates the app and
# takes the keyboard from the window you dictate into. `noActivates` keeps the
# window from becoming key, `nonActivating` (an NSPanel style) keeps a click on
# it from activating Ucho. Both must be set before the window is first mapped.
MAC_DICTATION_STYLE = ("utility", ("noActivates", "nonActivating"))


def float_without_activating(window) -> bool:
    """Gives a Tk toplevel the non-activating panel style on macOS (no-op elsewhere). True when set."""
    try:
        if window.tk.call("tk", "windowingsystem") != "aqua":
            return False
    except tkinter.TclError:
        return False
    style, attributes = MAC_DICTATION_STYLE
    for attrs in (attributes, attributes[:1]):  # older Tk may not know nonActivating
        try:
            window.tk.call("::tk::unsupported::MacWindowStyle", "style", window._w, style, attrs)
            return True
        except tkinter.TclError:
            log.debug("MacWindowStyle %s %s refused", style, attrs, exc_info=True)
    log.warning("The dictation window may take the focus: its window style could not be set")
    return False


class DictationWindow(ctk.CTkToplevel):
    """Variant B: a small dictation window opened from the tray, the hotkey or the Jump List."""

    def __init__(self, app):
        super().__init__(app)
        float_without_activating(self)
        self.app = app
        self.title(t("Ucho Dictate"))
        self.geometry("440x320")
        self.minsize(400, 300)
        self.resizable(True, False)
        self.configure(fg_color=C.bg)
        self.panel = DictationPanel(self, app, compact=True)
        self.panel.pack(fill="both", expand=True, padx=16, pady=16)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.after(250, self._icon)

    def _icon(self) -> None:
        if not self.winfo_exists():
            return
        platforms.shell().set_window_icon(self, dictation=True)
        self.app.describe_window(self, dictation=True)

    def show(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def tick(self) -> None:
        if self.winfo_viewable():
            self.panel.tick()
