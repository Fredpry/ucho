# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Model downloads with visible progress, and whether recording can start.

huggingface_hub reports download progress only through tqdm bars.
`snapshot_download(tqdm_class=...)` builds three bars from the class it gets
(huggingface_hub 1.30): "Downloading bytes" (bytes from the network, with a
guessed total that grows), "Reconstructing ..." (bytes written to disk; each
file adds its size to `total` when it starts) and "Fetching N files" (a file
counter). `progress_bar_class` returns a small stand-in for tqdm that forwards
both byte counts to a thread-safe `ByteCounter`; the UI reads it on its own
thread. It needs no tqdm import, so the tests run without it.

Xet downloads write to disk in large steps: in a real download of the speaker
models the disk count stood at 0 % for seconds and then jumped to 100 %. The
network count moves smoothly but may stay below the file size (compression,
deduplication). So "done" is the larger of the two, capped at the total.

The total grows while the files start (8 at a time), so the percentage can
step back a little at the beginning of a download; the largest files of the
OpenVINO repositories start first, so it settles within seconds.

Readiness: a meeting transcription and a dictation (as its fallback) both use
the transcription model chosen in Settings, so recording waits until that
model is on disk. `speech_readiness` turns the disk check and the download
state into one of five states; `block_reason` is the text shown instead of
starting a recording.
"""

from __future__ import annotations

import errno
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from . import config
from .i18n import t

log = logging.getLogger(__name__)

# Download kinds: what the model is for (the main window names it).
SPEECH = "speech"
SPEAKERS = "speakers"
LANGUAGE = "language"

# Readiness states of the speech model.
READY = "ready"
UNKNOWN = "unknown"          # not checked yet (the first check runs off the UI thread)
DOWNLOADING = "downloading"
MISSING = "missing"
FAILED = "failed"

# The network-bytes bar of snapshot_download: its total is a guess that grows
# by 25 % steps, so only its bytes are used, never its total.
TRANSFER_BAR = "Downloading bytes"
MB = 1024 * 1024
# Download key of the speaker detection models (two repos, one progress).
SPEAKER_MODELS = "speaker-models"


# --- progress -------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    done: int = 0
    total: int = 0   # 0: not known yet

    @property
    def fraction(self) -> float | None:
        if self.total <= 0:
            return None
        return max(0.0, min(1.0, self.done / self.total))

    def describe(self) -> str:
        """"42% · 640 / 1520 MB", or "640 MB" while the total is not known."""
        done = round(self.done / MB)
        fraction = self.fraction
        if fraction is None:
            return t("{done} MB", done=done)
        return t("{percent}% · {done} / {total} MB", percent=int(fraction * 100), done=done,
                 total=max(done, round(self.total / MB)))


class ByteCounter:
    """Bytes done and expected, updated from download threads, read by the UI.

    `advance` counts bytes written to disk, `advance_transfer` bytes received.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = 0
        self._transfer = 0
        self._total = 0

    def add_total(self, n: int | float) -> None:
        with self._lock:
            self._total = max(0, self._total + int(n))

    def advance(self, n: int | float) -> None:
        with self._lock:
            # Negative after a retry that starts a file again (huggingface_hub rolls back).
            self._done = max(0, self._done + int(n))

    def advance_transfer(self, n: int | float) -> None:
        with self._lock:
            self._transfer = max(0, self._transfer + int(n))

    def progress(self) -> Progress:
        with self._lock:
            total, done = self._total, self._done
            # A file without a known size still counts as done; never show more than 100 %.
            if total and done > total:
                total = done
            transfer = min(self._transfer, total) if total else self._transfer
            return Progress(max(done, transfer), total)


def progress_bar_class(counter: ByteCounter) -> type:
    """A tqdm stand-in for `snapshot_download(tqdm_class=...)` that feeds `counter`.

    Byte bars count; the network-transfer bar only with its bytes, not with
    its guessed total. huggingface_hub uses a class that is not a tqdm
    subclass as-is: no `name`/`disable` arguments, no shared lock.
    """

    class DownloadBar:
        def __init__(self, iterable=None, *_args, **kwargs):
            self.iterable = iterable
            self.desc = str(kwargs.get("desc") or "")
            self.disable = False
            self.n = 0
            self._total = 0
            self._transfer = self.desc.startswith(TRANSFER_BAR)
            self._counts = kwargs.get("unit") == "B" and not self._transfer
            self.total = kwargs.get("total")
            initial = kwargs.get("initial") or 0
            if initial:
                self.update(initial)

        @property
        def total(self) -> int:
            return self._total

        @total.setter
        def total(self, value) -> None:
            value = int(value or 0)
            if self._counts:
                counter.add_total(value - self._total)
            self._total = value

        @property
        def format_dict(self) -> dict:
            return {"n": self.n, "total": self._total, "rate": None, "elapsed": 0}

        def update(self, n: int | float | None = 1) -> None:
            n = int(n or 0)
            self.n += n
            if self._counts and n:
                counter.advance(n)
            elif self._transfer and n:
                counter.advance_transfer(n)

        def __iter__(self):
            for item in self.iterable or ():
                yield item
                self.update(1)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            self.close()
            return False

        def set_description(self, desc: str | None = None, refresh: bool = True) -> None:
            self.desc = desc or ""

        def set_description_str(self, desc: str | None = None, refresh: bool = True) -> None:
            self.desc = desc or ""

        def set_postfix(self, *_args, **_kwargs) -> None:
            pass

        def set_postfix_str(self, *_args, **_kwargs) -> None:
            pass

        def refresh(self, *_args, **_kwargs) -> None:
            pass

        def clear(self, *_args, **_kwargs) -> None:
            pass

        def close(self) -> None:
            pass

    return DownloadBar


def describe_error(error: BaseException) -> str:
    """A short, user-facing reason for a failed download (the full one goes to the log)."""
    names = {cls.__name__ for cls in type(error).__mro__}
    cause = error.__cause__ or error.__context__
    if cause is not None:
        names |= {cls.__name__ for cls in type(cause).__mro__}
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return t("Not enough free disk space.")
    offline = {"ConnectError", "ConnectTimeout", "TimeoutException", "ReadTimeout",
               "OfflineModeIsEnabled", "LocalEntryNotFoundError", "ConnectionError",
               "NameResolutionError", "ProxyError"}
    if names & offline:
        return t("No connection to Hugging Face. Check the internet connection.")
    text = (str(error).strip().splitlines() or [type(error).__name__])[0]
    return text[:160]


# --- downloads ------------------------------------------------------------------


@dataclass(eq=False)
class Download:
    key: str
    name: str        # shown to the user
    kind: str        # SPEECH / SPEAKERS / LANGUAGE
    counter: ByteCounter = field(default_factory=ByteCounter)
    running: bool = True
    error: str | None = None
    callbacks: list[Callable[[str | None], None]] = field(default_factory=list)

    @property
    def progress(self) -> Progress:
        return self.counter.progress()


def _thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


class Downloads:
    """Model downloads in background threads; their state changes only on the UI thread.

    `post` runs a function on the UI thread (App.post); `spawn` starts the
    worker (injected, so the tests run it synchronously). A key downloads at
    most once at a time; a failure stays visible until the next attempt.
    """

    def __init__(self, post: Callable[[Callable[[], None]], None],
                 spawn: Callable[[Callable[[], None], str], None] | None = None):
        self._post = post
        self._spawn = spawn or _thread
        self._items: dict[str, Download] = {}
        self._listeners: list[Callable[[Download], None]] = []

    def listen(self, callback: Callable[[Download], None]) -> None:
        """`callback(download)` on the UI thread after every finished or failed download."""
        self._listeners.append(callback)

    def start(self, key: str, name: str, kind: str, work: Callable[[type], None],
              on_done: Callable[[str | None], None] | None = None) -> bool:
        """Runs `work(bar_class)` in the background; False when `key` is already downloading.

        `on_done(error)` runs on the UI thread; `error` is None on success.
        """
        current = self._items.get(key)
        if current is not None and current.running:
            if on_done is not None:
                current.callbacks.append(on_done)
            return False
        item = Download(key, name, kind)
        if on_done is not None:
            item.callbacks.append(on_done)
        self._items[key] = item
        bar_class = progress_bar_class(item.counter)
        log.info("Model download started: %s", key)

        def run() -> None:
            error: str | None = None
            try:
                work(bar_class)
            except Exception as exc:
                log.exception("Model download failed: %s", key)
                error = describe_error(exc)
            self._post(lambda: self._finish(item, error))

        self._spawn(run, f"download {key}")
        return True

    def _finish(self, item: Download, error: str | None) -> None:
        item.running, item.error = False, error
        if error is None:
            log.info("Model download finished: %s", item.key)
            if self._items.get(item.key) is item:
                del self._items[item.key]
        for callback in item.callbacks:
            try:
                callback(error)
            except Exception:
                log.exception("Download callback failed")
        for listener in list(self._listeners):
            try:
                listener(item)
            except Exception:
                log.exception("Download listener failed")

    def get(self, key: str) -> Download | None:
        """The running or failed download of `key` (a finished one is forgotten)."""
        return self._items.get(key)

    def running(self) -> list[Download]:
        return [item for item in self._items.values() if item.running]

    def is_running(self, key: str) -> bool:
        item = self._items.get(key)
        return item is not None and item.running

    def forget(self, key: str) -> None:
        """Drops a failure (e.g. the model is on disk after all)."""
        item = self._items.get(key)
        if item is not None and not item.running:
            del self._items[key]


# --- readiness ------------------------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    state: str
    model: str
    progress: Progress = Progress()
    error: str | None = None

    @property
    def blocks(self) -> bool:
        """True when recording must not start. Not yet checked does not block."""
        return self.state in (DOWNLOADING, MISSING, FAILED)


def speech_readiness(model: str, on_disk: bool | None, download: Download | None) -> Readiness:
    """State of the transcription model `model`.

    `on_disk`: the last disk check (None: not checked yet). `download`: its
    running or failed download, if any. A running download wins over the
    disk: its files are still being written.
    """
    if download is not None and download.running:
        return Readiness(DOWNLOADING, model, download.progress)
    if on_disk:
        return Readiness(READY, model)
    if on_disk is None:
        return Readiness(UNKNOWN, model)
    if download is not None and download.error:
        return Readiness(FAILED, model, error=download.error)
    return Readiness(MISSING, model)


def recording_blocked(app, check: bool = True) -> str | None:
    """`app.recording_blocked(check)` for the controllers; None for an app without it (tests)."""
    blocked = getattr(app, "recording_blocked", None)
    reason = blocked(check) if callable(blocked) else None
    return reason if isinstance(reason, str) else None


def model_name(repo: str) -> str:
    """Short model name for messages: "whisper-large-v3-int8"."""
    return repo.split("/")[-1].removesuffix("-ov")


def model_size(repo: str) -> str:
    info = config.MODELS.get(repo) or config.LLM_MODELS.get(repo) or {}
    return str(info.get("size", "?"))


def block_reason(readiness: Readiness) -> str | None:
    """Why a meeting or a dictation cannot start now, or None."""
    if readiness.state == DOWNLOADING:
        return t("Downloading the speech model ({progress}). Recording works when it is done.",
                 progress=readiness.progress.describe())
    if readiness.state == MISSING:
        return t("The speech model {model} is not downloaded. Download it to record.",
                 model=model_name(readiness.model))
    if readiness.state == FAILED:
        return t("The speech model download failed: {error}", error=readiness.error or "")
    return None


__all__ = [
    "SPEECH", "SPEAKERS", "LANGUAGE", "SPEAKER_MODELS", "READY", "UNKNOWN", "DOWNLOADING", "MISSING", "FAILED",
    "Progress", "ByteCounter", "progress_bar_class", "describe_error", "Download", "Downloads",
    "Readiness", "speech_readiness", "recording_blocked", "model_name", "model_size", "block_reason",
]
