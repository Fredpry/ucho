# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Meeting mode: two-track recording, transcription and the optional summary.

The logic behind the "Meeting" screen, without widgets. Model work goes
through the shared engine (ucho/engine.py); results come back to the UI
thread through `app.post`, and screens listen to the events below.

Events (name, data):
    state          -
    progress       (fraction, new_text)
    status         text
    transcribed    (folder, segments, meta, summary_expected)
    summary        (folder, result)
    error          text
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config, session, transcribe
from .i18n import t
from .merge import Segment, save_results, slug

log = logging.getLogger(__name__)


class MeetingController:
    def __init__(self, app):
        self.app = app
        self.recorder = None
        self.recording = False
        self.recording_start = 0.0
        self.session_name = ""
        self.session_dir: Path | None = None
        self.transcribing = False
        self.summarizing = False
        self.progress = 0.0
        self.status_text = ""
        self.message: tuple[str, str] | None = None  # (text, style) for the meeting screen
        self.live_text: list[str] = []
        self.last_segments: list[Segment] = []
        self.last_meta: dict = {}
        self._job = None
        self._listeners: list[Callable[[str, object], None]] = []

    # --- events -------------------------------------------------------------

    def listen(self, callback: Callable[[str, object], None]) -> None:
        self._listeners.append(callback)

    def clear_listeners(self) -> None:
        self._listeners.clear()

    def _emit(self, event: str, data=None) -> None:
        """Delivers an event on the UI thread."""
        def deliver():
            for callback in list(self._listeners):
                try:
                    callback(event, data)
                except Exception:
                    log.exception("Listener for %s failed", event)
        self.app.post(deliver)

    @property
    def settings(self) -> config.Settings:
        return self.app.settings

    @property
    def busy(self) -> bool:
        return self.transcribing or self.summarizing

    def levels(self) -> dict[str, float]:
        if self.recording and self.recorder is not None:
            return self.recorder.levels
        return {}

    def elapsed(self) -> float:
        return time.time() - self.recording_start if self.recording else 0.0

    # --- recording ------------------------------------------------------------

    def toggle(self, name: str = "") -> None:
        if self.recording:
            self.stop()
        else:
            self.start(name)

    def start(self, name: str = "") -> bool:
        if self.recording:
            return False
        from . import audio

        name = name.strip() or t("Meeting")
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        folder = self.settings.recordings_dir / f"{stamp}_{slug(name) or 'Meeting'}"
        recorder = audio.Recorder(
            tracks=self.settings["tracks"],
            microphone_index=self.settings["microphone_index"],
            system_index=self.settings["system_index"],
        )
        try:
            recorder.start(folder)
        except Exception as error:
            log.exception("Could not start recording")
            self.message = (str(error), "warning")
            self._emit("error", str(error))
            return False
        self.recorder = recorder
        self.recording = True
        self.session_name = name
        self.session_dir = folder
        self.recording_start = time.time()
        self.message = (("; ".join(recorder.warnings)), "warning") if recorder.warnings else None
        log.info("Recording started: %s", folder.name)
        self._emit("state")
        return True

    def stop(self) -> None:
        if not self.recording or self.recorder is None:
            return
        self.recording = False
        result = self.recorder.stop()
        self.recorder = None
        files = {n: d["file"] for n, d in result["tracks"].items()}
        log.info("Recording stopped: %.0f s", result["duration_s"])
        if not files:
            self.message = (t("Nothing was recorded."), "warning")
            self._emit("state")
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
        self.message = None
        self._emit("state")
        if self.settings["transcribe_after_stop"]:
            self.transcribe(result["folder"], files, meta)
        else:
            self._emit("saved", result["folder"])

    # --- transcription ----------------------------------------------------------

    def transcribe_again(self, folder: Path) -> bool:
        files = session.audio_tracks(folder)
        if not files:
            self._emit("error", t("No audio files in this session."))
            return False
        meta = session.read_meta(folder)
        meta.pop("segments", None)
        meta.setdefault("name", folder.name)
        meta["model"] = self.settings["model"]
        meta["language"] = self.settings["language"]
        return self.transcribe(folder, files, meta)

    def transcribe(self, folder: Path, files: dict, meta: dict) -> bool:
        if self.busy:
            self._emit("error", t("Another transcription is running. Wait until it finishes."))
            return False
        # Settings are read once at start and the app can sit in the tray for
        # weeks; every UI change goes to disk at once, so the file is at least
        # as fresh as memory.
        self.settings.load()
        model, device = self.settings["model"], self.settings["device"]
        if not transcribe.model_downloaded(model):
            log.warning("Transcription skipped: model %r is not downloaded", model)
            self._emit("error", t("The model is not downloaded — open Settings."))
            return False

        self.transcribing = True
        self.session_dir = folder
        self.progress = 0.0
        self.live_text = []
        self.last_segments, self.last_meta = [], dict(meta)
        self.status_text = t("Loading model...")
        self._emit("state")
        started = time.monotonic()

        def progress(fraction: float, text: str) -> None:
            self.progress = max(0.0, min(1.0, fraction))
            if text:
                self.live_text.append(text.strip())
            self._emit("progress", (self.progress, text.strip() if text else ""))

        def status(text: str) -> None:
            self.status_text = text
            self._emit("status", text)

        def done(result) -> None:
            segments, used_device = result
            full = dict(meta)
            full["device"] = used_device
            full["transcription_time_s"] = round(time.monotonic() - started, 1)
            try:
                save_results(folder, segments, full)
            except Exception as error:
                failed(RuntimeError(t("Saving failed: {error}", error=error)))
                return
            expected = self._summary_expected(segments)
            self.transcribing = False
            self.progress = 1.0
            self.last_segments, self.last_meta = segments, full
            self.status_text = t("Done in {time} s  ·  {device}  ·  {n} utterances",
                                 time=f"{full['transcription_time_s']:.0f}", device=used_device,
                                 n=len(segments))
            self._emit("transcribed", (folder, segments, full, expected))
            if expected:
                self.summarize(folder, segments, full)
            else:
                self._emit("state")

        def failed(error: BaseException) -> None:
            self.transcribing = False
            text = str(error) or type(error).__name__
            self.status_text = t("Error: {error}", error=text)
            self._emit("error", self.status_text)
            self._emit("state")

        self._job = self.app.engine.transcribe(files, model, device, self.settings["language"],
                                               progress=progress, status=status,
                                               on_done=done, on_error=failed)
        return True

    def cancel(self) -> None:
        if self._job is not None and self.busy:
            self._job.cancel.set()
            self.status_text = t("Stopping...")
            self._emit("status", self.status_text)

    # --- summary ---------------------------------------------------------------

    def _summary_expected(self, segments: list[Segment]) -> bool:
        return bool(segments and self.settings["summary_enabled"]
                    and transcribe.model_downloaded(self.settings["summary_model"]))

    def summarize_again(self, folder: Path) -> bool:
        """Summary on its own, without running the audio through Whisper again."""
        if self.busy:
            return False
        meta = session.read_meta(folder)
        segments = session.segments(meta)
        if not segments:
            self._emit("error", t("No segments in meta.json — use “Transcribe again”."))
            return False
        self.session_dir = folder
        self.last_segments, self.last_meta = segments, meta
        self.summarize(folder, segments, meta)
        return True

    def summarize(self, folder: Path, segments: list[Segment], meta: dict) -> None:
        """Runs the local language model with every Whisper model released.

        No error in this step may spoil a finished transcript: at worst the
        session stays without a summary and with its old name.
        """
        from . import summary as summary_mod

        model = self.settings["summary_model"]
        if not transcribe.model_downloaded(model):
            self._emit("summary_error", t("The summary model is not downloaded — see Settings."))
            return
        self.summarizing = True
        self.status_text = t("Summarizing...")
        self._emit("state")

        def work(job):
            return summary_mod.summarize(
                segments, model, self.settings["device"],
                status=lambda text: (setattr(self, "status_text", text), self._emit("status", text)),
                cancel=job.cancel)

        def done(result) -> None:
            self.summarizing = False
            if self._job is not None and self._job.cancel.is_set():
                self._emit("summary_error", t("stopped"))
            elif not result:
                self._emit("summary_error", t("The model returned no summary."))
            else:
                self._save_summary(folder, segments, meta, result)
            self._emit("state")

        def failed(error: BaseException) -> None:
            self.summarizing = False
            log.warning("Summary failed: %s", error)
            self._emit("summary_error", str(error))
            self._emit("state")

        self._job = self.app.engine.exclusive(work, "summary", on_done=done, on_error=failed)

    def _save_summary(self, folder: Path, segments: list[Segment], meta: dict, result: dict) -> None:
        from . import summary as summary_mod

        try:
            (folder / session.SUMMARY_MD).write_text(
                summary_mod.as_markdown(result, meta), encoding="utf-8")
            meta.update(result)
            # Second save pass: transcript.md gets the summary block, meta.json
            # the new fields. save_results is idempotent.
            save_results(folder, segments, meta)
        except OSError as error:
            log.exception("Could not save the summary")
            self._emit("summary_error", t("Saving the summary: {error}", error=error))
            return
        if self.settings["rename_folder"]:
            folder = rename_session(folder, result)
        self.session_dir = folder
        self.last_meta = meta
        self._emit("summary", (folder, result))

    def quit(self) -> None:
        if self.recording:
            # Do not lose a recording in progress: close the files properly.
            self.stop()
        if self._job is not None:
            self._job.cancel.set()


def rename_session(folder: Path, result: dict) -> Path:
    """Replaces the name part with the topic and names. Returns the current path."""
    from . import summary as summary_mod

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
        log.warning("Could not rename %s -> %s: %s", folder.name, new_name, error)
        return folder
    log.info("Session renamed: %s -> %s", folder.name, target.name)
    return target

