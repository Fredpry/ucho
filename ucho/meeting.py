# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Meeting mode: two-track recording, transcription and the optional summary.

The logic behind the "Meeting" screen, without widgets. Model work goes
through the shared engine (ucho/engine.py); results come back to the UI
thread through `app.post`, and screens listen to the events below.

One session is transcribed (and summarized) at a time. A session that stops
while another one is still being worked on waits in a FIFO queue and starts
when the current one has finished or failed, summary included. The queue
lives here, not in the engine: the engine gets one meeting job at a time, so
a dictation still overtakes it between chunks, and a queued transcription
never runs between another session's transcription and its summary. The
queue is not saved: sessions still queued at exit stay without a transcript
and can be transcribed later.

Events (name, data):
    state          -
    progress       (fraction, new_text)
    status         text
    transcribed    (folder, segments, meta, summary_expected)
    summary        (folder, result)
    titled         (old_folder, new_folder, name)
    renamed        (folder, name)
    speakers       folder (speaker names updated)
    error          text
    live           segments recognized so far in the recording (live transcription)

Automatic titles (ucho/titles.py, setting "auto_title"): sessions the user did
not name and that have no summary get a short title from the chosen analysis
model after the transcription queue is empty. Title jobs run through
`engine.exclusive` and yield immediately (and retry later) when a recording,
dictation, transcription or summary starts, so naming never delays user work.

Live transcription (ucho/live_meeting.py, setting "meeting_live"): while a
meeting records, the chunks the transcription after the stop will cut are
recognized as soon as they are final, and the text so far is saved in the
session folder. After the stop the normal transcription runs and takes every
chunk it finds in the live cache with the same range and samples, so only the
rest (usually the last chunk) goes to the model and the transcript is the same
as without live transcription.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from . import config, downloads, llm, session, titles, transcribe
from .events import Emitter
from .i18n import t
from .merge import (META_FILE, TRANSCRIPT_COMPLETE, TRANSCRIPT_PARTIAL, TRANSCRIPTION_CANCELLED,
                    TRANSCRIPTION_ERRORS, Segment, clean_speaker_name, save_results, slug,
                    speaker_label, split_speaker)

log = logging.getLogger(__name__)

TITLE_RETRY_S = 30.0


class MeetingController(Emitter):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.recorder = None
        self.recording = False
        self.recording_start = 0.0
        self.session_name = ""
        self.session_name_source: str | None = None
        self.session_dir: Path | None = None  # the session being transcribed or summarized
        self.recording_dir: Path | None = None  # the session being recorded right now
        self.last_stopped: Path | None = None  # folder of the last finished recording
        self.transcribing = False
        self.summarizing = False
        self.progress = 0.0
        self.status_text = ""
        self.message: tuple[str, str] | None = None  # (text, style) for the meeting screen
        self.live_text: list[str] = []
        self.last_segments: list[Segment] = []
        self.last_meta: dict = {}
        self._job = None
        self._queue: deque[tuple[Path, dict, dict]] = deque()  # (folder, files, meta)
        self._titles: deque[Path] = deque()
        self._titling: Path | None = None
        self._title_job = None
        self._title_timer: threading.Timer | None = None
        self.live = None  # live_meeting.LiveMeeting of the recording, when on
        self.live_segments: list[Segment] = []
        self._live_meta: dict = {}
        self._caches: dict[str, object] = {}  # folder key -> live_meeting.ChunkCache

    @property
    def settings(self) -> config.Settings:
        return self.app.settings

    @property
    def busy(self) -> bool:
        return self.transcribing or self.summarizing

    @property
    def shown_status(self) -> str:
        if self.transcribing:
            try:
                paused = bool(getattr(self.app.engine, "dictation_active", False))
            except Exception:
                paused = False
            if paused:
                return t("Paused for a dictation")
        return self.status_text

    @property
    def queued(self) -> int:
        """Sessions waiting for their transcription."""
        return len(self._queue)

    def queue_position(self, folder: Path | None) -> int:
        """1 for the next session to be transcribed, 2 for the one after it; 0 when not queued."""
        if folder is None:
            return 0
        for position, (queued, _files, _meta) in enumerate(list(self._queue), start=1):
            if same_folder(queued, folder):
                return position
        return 0

    def is_active(self, folder: Path | None) -> bool:
        """True while this session is being transcribed or summarized."""
        return self.busy and same_folder(folder, self.session_dir)

    def dequeue(self, folder: Path) -> bool:
        """Takes a session out of the queue (for example before it is deleted)."""
        self._drop_title(folder)
        for entry in list(self._queue):
            if same_folder(entry[0], folder):
                self._queue.remove(entry)
                self._caches.pop(_folder_key(entry[0]), None)
                self._emit("state")
                return True
        return False

    def _drop_title(self, folder: Path) -> None:
        for queued in list(self._titles):
            if same_folder(queued, folder):
                self._titles.remove(queued)

    def in_use(self, folder: Path | None) -> bool:
        """Recorded, transcribed, summarized or waiting in the queue: not to be deleted or trimmed."""
        if folder is None:
            return False
        # Unresolvable paths count as the same here: cannot tell, so in use.
        if self.recording and same_folder(self.recording_dir, folder, unknown=True):
            return True
        if self.busy and same_folder(self.session_dir, folder, unknown=True):
            return True
        if self._titling is not None and same_folder(self._titling, folder, unknown=True):
            return True
        return any(same_folder(queued, folder, unknown=True) for queued, _files, _meta in list(self._queue))

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
        # No speech model on the disk (still downloading, or missing): nothing could
        # transcribe the recording, so it does not start (#28).
        blocked = downloads.recording_blocked(self.app)
        if blocked:
            self.app.refuse_recording(blocked)
            return False
        from . import audio

        typed = name.strip()
        name_source = "user" if typed else None
        name = typed or t("Meeting")
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d_%H-%M")
        folder = self.settings.recordings_dir / f"{stamp}_{slug(name) or 'Meeting'}"
        live = self._new_live(folder, name, now, name_source=name_source)
        recorder = audio.Recorder(
            tracks=self.settings["tracks"],
            microphone=self.settings["microphone_name"],
            system=self.settings["system_name"],
            microphone_index=self.settings["microphone_index"],
            system_index=self.settings["system_index"],
            listener=live.feed if live is not None else None,
        )
        try:
            recorder.start(folder)
        except Exception as error:
            log.exception("Could not start recording")
            if live is not None:
                live.stop()
            self.message = (str(error), "warning")
            self._emit("error", str(error))
            return False
        self.live = live
        self.live_segments = []
        if live is not None:
            live.start()
        self.recorder = recorder
        self.recording = True
        self.recording_dir = folder
        # Not session_dir: that one belongs to a transcription that may still run.
        self.session_name = name
        self.session_name_source = name_source
        self.recording_start = time.time()
        self.message = (("; ".join(recorder.warnings)), "warning") if recorder.warnings else None
        log.info("Recording started")
        self._emit("state")
        return True

    def stop(self) -> None:
        if not self.recording or self.recorder is None:
            return
        self.recording = False
        self.recording_dir = None
        # Live work first: no more chunks are started and nothing more is
        # saved by the live thread. A chunk already in the engine finishes
        # before the transcription below starts and lands in the cache.
        live, self.live = self.live, None
        cache = live.stop() if live is not None else None
        try:
            result = self.recorder.stop()
        finally:
            self.recorder = None
        files = {n: d["file"] for n, d in result["tracks"].items()}
        self.last_stopped = result["folder"]
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
        if self.session_name_source == "user":
            meta["name_source"] = "user"
        if live is not None:
            log.info("Live transcription while recording: %d chunks done, %d failed%s",
                     live.done, live.errors, ", stopped early (too slow)" if live.gave_up else "")
        self.message = None
        if self.live_segments:
            # Keeps the live text on disk until the transcription replaces it.
            self._save_partial(result["folder"], self.live_segments, meta)
        else:
            try:
                config.write_json(result["folder"] / "meta.json", meta)
            except OSError as error:
                log.warning("Could not write meta.json: %s", type(error).__name__)
                self.message = (t("Could not save the recording details (meta.json)."), "warning")
        self._emit("state")
        if self.settings["transcribe_after_stop"]:
            self.transcribe(result["folder"], files, meta, cache=cache)
        else:
            self._emit("saved", result["folder"])

    # --- live transcription while recording ---------------------------------------

    def live_wanted(self) -> bool:
        """Live transcription runs only when the recording is transcribed after the stop anyway."""
        return bool(self.settings["transcribe_after_stop"]) and self.settings.meeting_live()

    def _new_live(self, folder: Path, name: str, started: datetime,
                  name_source: str | None = None):
        """A LiveMeeting for a new recording, or None when off or the model is missing."""
        if not self.live_wanted():
            return None
        model, device, language = (self.settings["model"], self.settings["device"],
                                   self.settings["language"])
        if not transcribe.model_downloaded(model):
            return None
        from .live_meeting import LiveMeeting

        self._live_meta = {
            "name": name,
            "date": started.strftime("%Y-%m-%d %H:%M"),
            "model": model,
            "language": language,
        }
        if name_source == "user":
            self._live_meta["name_source"] = "user"
        dictation = getattr(self.app, "dictation", None)

        def dictating() -> bool:
            # No new meeting chunk starts while a dictation is being spoken:
            # the dictation would wait for the whole chunk (up to 5 minutes of
            # audio with a forced language, see #12).
            return bool(getattr(dictation, "recording", False))

        return LiveMeeting(self.app.engine, model, device, language,
                           on_update=lambda segments: self._live_update(folder, segments),
                           should_wait=dictating if dictation is not None else None)

    def _live_update(self, folder: Path, segments: list[Segment]) -> None:
        """New live text (live thread): saved at once, so a crash keeps it."""
        self.live_segments = segments
        meta = dict(self._live_meta, duration_s=round(self.elapsed(), 1))
        self._save_partial(folder, segments, meta)
        self._emit("live", segments)

    @staticmethod
    def _save_partial(folder: Path, segments: list[Segment], meta: dict) -> None:
        partial = dict(meta)
        partial[TRANSCRIPT_COMPLETE] = False
        partial[TRANSCRIPT_PARTIAL] = True
        try:
            save_results(folder, segments, partial)
        except OSError as error:
            log.warning("Could not save the live transcript: %s", type(error).__name__)

    # --- transcription ----------------------------------------------------------

    def _speaker_pass(self):
        if not self.settings.get("speaker_detection"):
            return None
        from . import diarize

        if diarize.ready():
            return diarize.track_pass(diarize.Diarizer())
        if not diarize.available():
            log.info("Speaker detection skipped: sherpa-onnx is not installed")
        else:
            log.info("Speaker detection skipped: speaker models are not downloaded")
        return None

    def transcribe_again(self, folder: Path) -> bool:
        files = session.audio_tracks(folder)
        if not files:
            self._emit("error", t("No audio files in this session."))
            return False
        meta = session.read_meta(folder)
        meta.pop("segments", None)
        # A new diarization run may number the voices differently, so a name
        # given to "meeting:2" could land on another person.
        names = {k: v for k, v in session.speaker_names(meta).items()
                 if split_speaker(k)[1] is None}
        if names:
            meta["speaker_names"] = names
        else:
            meta.pop("speaker_names", None)
        meta.setdefault("name", folder.name)
        return self.transcribe(folder, files, meta)

    def rename_speaker(self, folder: Path, code: str, name: str) -> bool:
        """Sets or clears the user-given name for `code` in `folder` and rewrites its files."""
        if self.in_use(folder):
            return False
        meta = session.read_meta(folder)
        segments = session.segments(meta)
        if not segments or code not in session.speaker_codes(segments):
            return False
        names = session.speaker_names(meta)
        cleaned = clean_speaker_name(name)
        if not cleaned or cleaned in (speaker_label(code), speaker_label(code, translate=False)):
            names.pop(code, None)
        else:
            names[code] = cleaned
        meta.pop("segments", None)
        if names:
            meta["speaker_names"] = names
        else:
            meta.pop("speaker_names", None)
        try:
            save_results(folder, segments, meta)
        except OSError as error:
            log.warning("Could not save speaker name: %s", type(error).__name__)
            self._emit("error", t("Could not save the speaker name: {error}", error=error))
            return False
        if same_folder(folder, self.session_dir):
            self.last_meta = dict(meta)
        self._emit("speakers", folder)
        return True

    def transcribe(self, folder: Path, files: dict, meta: dict, cache=None) -> bool:
        """Transcribes the session now, or queues it while another one is being worked on.

        True when the session is running or waiting in the queue (also when it
        already was: a session is never queued twice). `cache`: chunks
        recognized while recording (live_meeting.ChunkCache).
        """
        if self.is_active(folder) or self.queue_position(folder):
            log.info("Transcription already running or queued")
            return True
        if self.busy or self._queue:
            self._queue.append((folder, files, meta))
            if cache is not None:
                self._caches[_folder_key(folder)] = cache
            log.info("Transcription queued (%d waiting)", len(self._queue))
            self._emit("state")
            return True
        return self._start(folder, files, meta, cache)

    def _advance(self) -> None:
        """Starts the next queued session when nothing is running (UI thread)."""
        while self._queue and not self.busy:
            folder, files, meta = self._queue.popleft()
            cache = self._caches.pop(_folder_key(folder), None)
            if not folder.is_dir():
                log.warning("Queued session no longer exists - skipped")
                continue
            if self._start(folder, files, meta, cache):
                return
        self._emit("state")
        self._next_title()

    def _finished(self) -> None:
        """The current session is done (transcript, and summary when expected) or failed."""
        self.app.post(self._advance)

    def _start(self, folder: Path, files: dict, meta: dict, cache=None) -> bool:
        # Settings are read once at start and the app can sit in the tray for
        # weeks; every UI change goes to disk at once, so the file is at least
        # as fresh as memory.
        self.settings.load()
        model, device = self.settings["model"], self.settings["device"]
        if not transcribe.model_downloaded(model):
            log.warning("Transcription skipped: model %r is not downloaded", model)
            self._emit("error", t("The model is not downloaded — open Settings."))
            return False
        # A queued session may start after the settings changed: record what is used.
        meta["model"], meta["language"] = model, self.settings["language"]
        if cache is not None and not cache.matches(model, device, self.settings["language"]):
            log.info("Live results not used: model, device or language changed since the recording")
            cache = None

        self.transcribing = True
        self.session_dir = folder
        self.progress = 0.0
        self.live_text = []
        self.last_segments, self.last_meta = [], dict(meta)
        self.status_text = t("Loading model...")
        self._emit("state")
        started = time.monotonic()
        report = transcribe.new_report()

        def progress(fraction: float, text: str) -> None:
            self.progress = max(0.0, min(1.0, fraction))
            if text:
                self.live_text.append(text.strip())
            self._emit("progress", (self.progress, text.strip() if text else ""))

        def status(text: str) -> None:
            self.status_text = text
            self._emit("status", text)

        def done(result) -> None:
            # Runs on the engine thread, whose callback wrapper only logs
            # exceptions: without this, an error between here and the summary
            # job (settings, imports) would leave `busy` set and the queue stuck.
            try:
                finish(result)
            except Exception:
                log.exception("Finishing the transcription failed")
                self.transcribing = False
                self.summarizing = False
                self._emit("state")
                self._finished()

        def finish(result) -> None:
            segments, used_device = result
            full = dict(meta)
            full["device"] = used_device
            full["transcription_time_s"] = round(time.monotonic() - started, 1)
            # Stop during the transcription ends the job normally with the
            # segments so far. Audio retention deletes audio only when this
            # says the transcript is complete (retention.transcript_complete).
            for key in (TRANSCRIPT_COMPLETE, TRANSCRIPTION_CANCELLED, TRANSCRIPTION_ERRORS,
                        TRANSCRIPT_PARTIAL):
                full.pop(key, None)
            if cache is not None:
                log.info("Chunks recognized while recording: %d used, %d recognized now",
                         cache.hits, cache.misses)
            full[TRANSCRIPT_COMPLETE] = not report["cancelled"] and not report["errors"]
            if report["cancelled"]:
                full[TRANSCRIPTION_CANCELLED] = True
            if report["errors"]:
                full[TRANSCRIPTION_ERRORS] = report["errors"]
            try:
                save_results(folder, segments, full)
            except Exception as error:
                failed(RuntimeError(t("Saving failed: {error}", error=error)))
                return
            expected = self._summary_expected(segments)
            if (not expected and segments and self.titles_enabled()
                    and titles.wants_title(full, folder.name)
                    and not same_folder(self._titling, folder)
                    and not any(same_folder(queued, folder) for queued in self._titles)):
                self._titles.append(folder)
            # Summarizing first: `busy` must not drop between the two steps,
            # or a queued session would start in between.
            # The results are set before `transcribing` drops: the UI thread
            # reads them as soon as it sees the job is no longer running.
            self.progress = 1.0
            self.last_segments, self.last_meta = segments, full
            self.status_text = t("Done in {time} s  ·  {device}  ·  {n} utterances",
                                 time=f"{full['transcription_time_s']:.0f}", device=used_device,
                                 n=len(segments))
            self.summarizing = expected
            self.transcribing = False
            self._emit("transcribed", (folder, segments, full, expected))
            if expected:
                self.summarize(folder, segments, full)
            else:
                self._emit("state")
                self._finished()

        def failed(error: BaseException) -> None:
            self.transcribing = False
            text = str(error) or type(error).__name__
            self.status_text = t("Error: {error}", error=text)
            self._emit("error", self.status_text)
            self._emit("state")
            self._finished()

        try:
            speakers = self._speaker_pass()
        except Exception:
            log.exception("Speaker detection could not start")
            speakers = None
        self._job = self.app.engine.transcribe(
            files, model, device, self.settings["language"],
            progress=progress, status=status, on_done=done, on_error=failed, report=report,
            **({"cache": cache} if cache is not None else {}),
            **({"speakers": speakers} if speakers is not None else {}),
        )
        return True

    def cancel(self) -> None:
        if self._job is not None and self.busy:
            self._job.cancel.set()
            self.status_text = t("Stopping...")
            self._emit("status", self.status_text)

    # --- summary ---------------------------------------------------------------

    def _summary_expected(self, segments: list[Segment]) -> bool:
        return bool(segments and self.settings["summary_enabled"]
                    and llm.available(llm.chosen(self.settings)))

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

        model = llm.chosen(self.settings)
        if not llm.available(model):
            self.summarizing = False
            self._emit("summary_error", t("No language model is available — choose one in Settings."))
            self._emit("state")
            self._finished()
            return
        self.summarizing = True
        self.status_text = t("Summarizing...")
        self._emit("state")

        def work(job):
            return summary_mod.summarize(
                segments, model, self.settings["device"],
                status=lambda text: (setattr(self, "status_text", text), self._emit("status", text)),
                cancel=job.cancel, speaker_names=session.speaker_names(meta))

        def done(result) -> None:
            # `summarizing` stays set until the summary is saved and the folder
            # renamed, so the next queued session cannot start in between.
            try:
                if self._job is not None and self._job.cancel.is_set():
                    self._emit("summary_error", t("stopped"))
                elif not result:
                    self._emit("summary_error", t("The model returned no summary."))
                else:
                    self._save_summary(folder, segments, meta, result)
            finally:
                self.summarizing = False
                self._emit("state")
                self._finished()

        def failed(error: BaseException) -> None:
            self.summarizing = False
            log.warning("Summary failed: %s", error)
            self._emit("summary_error", str(error))
            self._emit("state")
            self._finished()

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

    # --- automatic titles and user rename -------------------------------------

    def titles_enabled(self) -> bool:
        value = self.settings.get("auto_title")
        if value is False:
            return False
        return llm.available(llm.chosen(self.settings))

    def _title_should_yield(self) -> bool:
        dictating = bool(getattr(getattr(self.app, "dictation", None), "recording", False))
        return bool(self.recording or self.transcribing or self.summarizing
                    or self._queue or dictating)

    def _next_title(self) -> None:
        if self._titling is not None or not self._titles:
            return
        if not self.titles_enabled():
            self._titles.clear()
            return
        if self._title_should_yield():
            self._retry_titles()
            return
        chosen = None
        while self._titles:
            candidate = self._titles.popleft()
            if not candidate.is_dir():
                continue
            meta = session.read_meta(candidate)
            segments = session.segments(meta)
            if segments and titles.wants_title(meta, candidate.name):
                chosen = (candidate, meta, segments)
                break
        if chosen is None:
            return
        folder, meta, segments = chosen
        model = llm.chosen(self.settings)
        self._titling = folder

        def work(job):
            cancel = SimpleNamespace(is_set=lambda: job.cancel.is_set() or self._title_should_yield())
            if cancel.is_set():
                return (True, {})
            result = titles.generate_title(
                segments, model, self.settings["device"],
                cancel=cancel, language=meta.get("language"))
            return (cancel.is_set(), result)

        def done(value) -> None:
            self.app.post(lambda: self._title_finished(folder, *value))

        def failed(error: BaseException) -> None:
            log.warning("Title generation failed: %s", type(error).__name__)
            self.app.post(lambda: self._title_finished(folder, False, {}))

        self._title_job = self.app.engine.exclusive(work, "title", on_done=done, on_error=failed)

    def _retry_titles(self) -> None:
        if self._title_timer is not None and self._title_timer.is_alive():
            return
        timer = threading.Timer(TITLE_RETRY_S, lambda: self.app.post(self._next_title))
        timer.daemon = True
        timer.start()
        self._title_timer = timer

    def _title_finished(self, folder: Path, interrupted: bool, result: dict) -> None:
        self._titling = None
        self._title_job = None
        if interrupted:
            if not any(same_folder(queued, folder) for queued in self._titles):
                self._titles.appendleft(folder)
            self._retry_titles()
            return
        try:
            if result and folder.is_dir():
                meta = session.read_meta(folder)
                if titles.wants_title(meta, folder.name):
                    meta["name"] = result["name"]
                    meta["name_source"] = "auto"
                    for key in ("title_model", "title_device", "title_time_s"):
                        if key in result:
                            meta[key] = result[key]
                    try:
                        save_results(folder, session.segments(meta), meta)
                    except OSError as error:
                        log.warning("Could not save automatic title: %s", type(error).__name__)
                        return
                    new = (rename_session(folder, {"topic": result["name"], "participants": []})
                           if self.settings["rename_folder"] else folder)
                    self._emit("titled", (folder, new, result["name"]))
        finally:
            self._next_title()

    def rename(self, folder: Path, name: str) -> bool:
        name = " ".join(name.split())[:120]
        if not name:
            return False
        if (self.is_active(folder) or self.queue_position(folder)
                or (self.recording and same_folder(self.recording_dir, folder))):
            self._emit("error", t("This recording is busy. Rename it when it has finished."))
            return False
        meta = session.read_meta(folder)
        meta["name"] = name
        meta["name_source"] = "user"
        for key in ("title_model", "title_device", "title_time_s"):
            meta.pop(key, None)
        segments = session.segments(meta)
        try:
            if segments:
                save_results(folder, segments, meta)
            else:
                config.write_json(folder / META_FILE, meta)
        except OSError as error:
            self._emit("error", t("Could not rename: {error}", error=error))
            return False
        self._drop_title(folder)
        self._emit("renamed", (folder, name))
        return True

    def quit(self) -> None:
        if self.recording:
            # Do not lose a recording in progress: close the files properly.
            self.stop()
        if self._title_timer is not None:
            self._title_timer.cancel()
            self._title_timer = None
        self._titles.clear()
        if self._title_job is not None:
            self._title_job.cancel.set()
        if self._queue:
            # Not saved: these sessions keep their audio and can be
            # transcribed later with "Transcribe again".
            log.info("Quit with %d queued transcription(s) not started: %s", len(self._queue),
                     ", ".join(folder.name for folder, _f, _m in self._queue))
            self._queue.clear()
        self._caches.clear()
        if self._job is not None:
            self._job.cancel.set()


def _folder_key(folder: Path) -> str:
    try:
        return str(Path(folder).resolve()).lower()
    except (OSError, RuntimeError):
        return str(folder).lower()


def same_folder(a: Path | None, b: Path | None, unknown: bool = False) -> bool:
    """True when both paths name the same folder (resolved: case, `..`, relative spellings).

    `unknown` is the answer when a path cannot be resolved.
    """
    if a is None or b is None:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return unknown


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
        log.warning("Could not rename a session folder: %s", type(error).__name__)
        return folder
    log.info("Session folder renamed")
    return target

