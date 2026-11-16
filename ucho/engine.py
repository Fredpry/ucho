# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""One Whisper engine for the whole app: a worker thread with a priority queue.

Before v0.2 the meeting recorder and the dictation window were two processes,
each with its own model in memory. Now every model call goes through this
single worker thread (an OpenVINO pipeline must not be called from two
threads at once), and jobs are ordered by priority:

    DICTATION   a dictation the user is waiting for, pasted into a window
    NORMAL      meeting transcription, summary, benchmark
    BACKGROUND  preloading the dictation model

A long meeting transcription is a *step* job: a generator that yields after
every chunk (~12 s of audio in "auto" mode, about 1 s of work on an Arc GPU).
After each step the worker picks the highest-priority job again, so a
dictation submitted in the middle of an hour-long transcription waits for at
most one chunk. With a forced language the chunks are up to 5 minutes long
(see transcribe.CHUNK_S), so the wait can be longer there.

Models ("pipelines") are cached by (model, device). When dictation uses the
transcription model ("shared", the low-memory option) both jobs use the same
loaded pipeline. A summary needs the language model on the same integrated
GPU, so every Whisper pipeline is released before it runs (see AGENTS.md).
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

DICTATION = 0
NORMAL = 1
BACKGROUND = 2

# Without preloading, the dictation model is released after this idle time.
IDLE_RELEASE_S = 120.0

Key = tuple[str, str]  # (model repo id, device)


@dataclass
class Job:
    """A unit of work for the engine thread.

    `work(job)` returns the result, or a generator: then the worker runs one
    step per turn and the generator's return value is the result.
    """

    work: Callable[["Job"], Any]
    priority: int = NORMAL
    name: str = ""
    on_done: Callable[[Any], None] | None = None
    on_error: Callable[[BaseException], None] | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    submitted: float = field(default_factory=time.monotonic)
    started: float | None = None
    finished: float | None = None
    pipeline_key: Key | None = None  # pipeline a step job keeps using between steps
    _steps: Any = None

    def wait(self, timeout: float | None = None) -> Any:
        """Blocks until the job ends (for tools and tests; never on the UI thread)."""
        if not self.done.wait(timeout):
            raise TimeoutError(self.name)
        if self.error is not None:
            raise self.error
        return self.result


class Engine:
    """Scheduler plus a cache of loaded Whisper pipelines."""

    def __init__(self, pipeline_factory: Callable[[str, str], Any] | None = None):
        if pipeline_factory is None:
            from . import transcribe

            # Whisper, or the optional Parakeet backend for its model id.
            pipeline_factory = transcribe.create_engine
        self._factory = pipeline_factory
        self._pipelines: dict[Key, Any] = {}
        self._keep: set[Key] = set()  # pipelines to keep loaded between jobs
        # Pipelines held by a live meeting transcription (ucho/live_meeting.py)
        # while it records: not released when idle or after another job.
        # exclusive() still releases them (memory order for the summary).
        self._held: dict[Key, int] = {}
        self._idle_since: dict[Key, float] = {}
        self._heap: list[tuple[int, int, Job]] = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.current: Job | None = None

    # --- life cycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="whisper-engine", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._stopping = True
            for _, _, job in self._heap:
                job.cancel.set()
            self._cv.notify_all()
        if self.current is not None:
            self.current.cancel.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def on_worker(self) -> bool:
        return threading.current_thread() is self._thread

    def pending(self) -> int:
        with self._cv:
            return len(self._heap) + (1 if self.current is not None else 0)

    # --- jobs -----------------------------------------------------------------

    def submit(self, work: Callable[[Job], Any], priority: int = NORMAL, name: str = "",
               on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None) -> Job:
        job = Job(work, priority, name or getattr(work, "__name__", "job"), on_done, on_error)
        self._push(job, next(self._seq))
        return job

    def _push(self, job: Job, seq: int) -> None:
        with self._cv:
            if self._stopping:
                job.cancel.set()
            heapq.heappush(self._heap, (job.priority, seq, job))
            self._cv.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._heap and not self._stopping:
                    self._cv.wait(timeout=10.0)
                    self._release_idle()
                if self._stopping and not self._heap:
                    break
                priority, seq, job = heapq.heappop(self._heap)
                self.current = job
            try:
                more = self._step(job)
            finally:
                self.current = None
            if more:
                # Same sequence number: the job keeps its place among equals,
                # but anything more urgent submitted meanwhile goes first.
                self._push(job, seq)
        self._release(set())

    def _step(self, job: Job) -> bool:
        """Runs one step of `job`. True when the job has more steps."""
        if job.started is None:
            job.started = time.monotonic()
        try:
            if job.cancel.is_set() and job._steps is None:
                raise Cancelled(job.name)
            if job._steps is None:
                value = job.work(job)
                if not _is_generator(value):
                    self._finish(job, value)
                    return False
                job._steps = value
            try:
                next(job._steps)
                return True
            except StopIteration as stop:
                self._finish(job, stop.value)
                return False
        except BaseException as error:  # noqa: BLE001 - reported to the caller
            if not isinstance(error, Cancelled):
                log.exception("Engine job %s failed", job.name)
            job.error = error
            job.finished = time.monotonic()
            job.done.set()
            if job.on_error is not None:
                _call(job.on_error, error)
            return False

    @staticmethod
    def _finish(job: Job, value: Any) -> None:
        job.result = value
        job.finished = time.monotonic()
        job.done.set()
        if job.on_done is not None:
            _call(job.on_done, value)

    # --- pipelines (engine thread only) ---------------------------------------

    def pipeline(self, model: str, device: str) -> Any:
        """The cached pipeline for (model, device), created on first use (not loaded)."""
        key = (model, device)
        pipe = self._pipelines.get(key)
        if pipe is None:
            pipe = self._factory(model, device)
            self._pipelines[key] = pipe
        self._idle_since[key] = time.monotonic()
        return pipe

    def hold(self, key: Key) -> None:
        """Keeps `key` loaded between jobs until `unhold` (any thread)."""
        with self._cv:
            self._held[key] = self._held.get(key, 0) + 1

    def unhold(self, key: Key) -> None:
        with self._cv:
            count = self._held.get(key, 0) - 1
            if count > 0:
                self._held[key] = count
            else:
                self._held.pop(key, None)

    def _kept(self) -> set[Key]:
        """Pipelines to keep between jobs: the dictation model and held ones."""
        with self._cv:
            return set(self._keep) | set(self._held)

    def loaded(self) -> list[Key]:
        return [k for k, p in self._pipelines.items() if getattr(p, "_pipe", True) is not None]

    def _release(self, keep: set[Key]) -> None:
        for key in list(self._pipelines):
            if key not in keep:
                try:
                    self._pipelines.pop(key).release()
                except Exception:
                    log.debug("Release of %s failed", key, exc_info=True)
                self._idle_since.pop(key, None)

    def _release_idle(self) -> None:
        now = time.monotonic()
        for key in list(self._pipelines):
            if key not in self._kept() and now - self._idle_since.get(key, now) > IDLE_RELEASE_S:
                log.info("Releasing idle model %s on %s", *key)
                self._release({k for k in self._pipelines if k != key})

    # --- what the app runs ------------------------------------------------------

    def configure(self, dictation: Key | None, preload: bool, on_done=None, on_error=None) -> Job:
        """Which pipeline stays loaded for instant dictation; loads it in the background."""
        def work(job: Job):
            self._keep = {dictation} if (dictation and preload) else set()
            busy = self._busy_keys()
            self._release(self._kept() | busy)
            if dictation and preload:
                started = time.monotonic()
                pipe = self.pipeline(*dictation)
                pipe.load()
                log.info("Dictation model ready: %s on %s (%.1f s)", dictation[0],
                         getattr(pipe, "device", dictation[1]), time.monotonic() - started)
                return getattr(pipe, "device", dictation[1])
            return None

        return self.submit(work, BACKGROUND, "configure", on_done, on_error)

    def _busy_keys(self) -> set[Key]:
        """Pipelines used by step jobs that are still running."""
        keys = set()
        with self._cv:
            jobs = [j for _, _, j in self._heap]
        for job in jobs:
            if job.pipeline_key and job._steps is not None:
                keys.add(job.pipeline_key)
        return keys

    def transcribe(self, files: dict, model: str, device: str, language: str,
                   progress=None, status=None, on_done=None, on_error=None,
                   report: dict | None = None, cache=None, speakers=None) -> Job:
        """Meeting transcription in steps. Result: (segments, used_device).

        `report` (transcribe.new_report) is filled in on the engine thread
        before `on_done` runs: cancelled or not, failed chunks. `cache`
        (live_meeting.ChunkCache): chunks recognized while recording.
        `speakers` (diarize.track_pass): optional post-transcription speaker pass.
        """
        from . import transcribe as tr

        def work(job: Job):
            pipe = self.pipeline(model, device)
            segments = yield from tr.iter_transcribe_session(
                files, pipe, language, progress=progress, cancel=job.cancel, status=status,
                report=report, cache=cache, speakers=speakers)
            if report is not None and job.cancel.is_set():
                # Stop pressed after the last chunk check: treated as cancelled too.
                report["cancelled"] = True
            used = pipe.device
            kept = self._kept()
            if (model, device) not in kept:
                self._release(kept | (self._busy_keys() - {(model, device)}))
            return segments, used

        job = Job(work, NORMAL, "transcribe", on_done, on_error, pipeline_key=(model, device))
        self._push(job, next(self._seq))
        return job

    def dictate(self, audio, model: str, device: str, language: str,
                dictionary: dict | None = None, on_done=None, on_error=None) -> Job:
        """Dictation, ahead of everything else. Result: (text, compute seconds, device)."""
        from . import transcribe as tr

        def work(job: Job):
            started = time.monotonic()
            pipe = self.pipeline(model, device)
            text = tr.recognize_dictation(pipe, audio, language, dictionary)
            return text, time.monotonic() - started, getattr(pipe, "device", device)

        return self.submit(work, DICTATION, "dictate", on_done, on_error)

    def exclusive(self, fn: Callable[[Job], Any], name: str, on_done=None, on_error=None,
                  reload: bool = True) -> Job:
        """Runs `fn` with every Whisper pipeline released (summary, benchmark).

        A summary loads a language model on the same integrated GPU; Whisper
        must not sit there at the same time. Afterwards the dictation model is
        loaded again in the background.
        """
        def work(job: Job):
            keep = set(self._keep)
            self._release(self._busy_keys())
            try:
                return fn(job)
            finally:
                if reload:
                    for key in keep:
                        self.submit(lambda _j, k=key: self.pipeline(*k).load(),
                                    BACKGROUND, "reload")

        return self.submit(work, NORMAL, name, on_done, on_error)


class Cancelled(Exception):
    """The job was cancelled before it started."""


def _is_generator(value: Any) -> bool:
    return hasattr(value, "__next__") and hasattr(value, "send")


def _call(callback: Callable, value: Any) -> None:
    try:
        callback(value)
    except Exception:
        log.exception("Engine callback failed")
