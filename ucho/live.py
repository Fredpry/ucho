# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Live (piecewise) dictation: cut at pauses and recognize while speaking.

Instead of waiting until the end of a dictation to transcribe the whole audio in
one call, `PieceCutter` splits the incoming microphone stream at pauses (with
a minimum piece length to keep sentence context and a hard maximum cap cut at
the quietest nearby spot so continuous speech still gets recognized in chunks).

Each finished piece is submitted to the shared Whisper engine (`ucho/engine.py`)
as a `DICTATION`-priority job. `LiveDictation` passes the trailing words of the
previous pieces (`PROMPT_CHARS`) as `initial_prompt` for continuity, pins the
language once `"auto"` detects one on a non-empty piece so short follow-up
pieces do not drift, and runs outro stripping, hallucination filtering and the
user dictionary only once on the joined text at the end (filtering per piece
would drop a real standalone "Tak." or clip an intermediate piece ending).

Measured on 62.8 s of synthetic Polish speech on an Arc GPU: the text is ready
0.8 s after the stop instead of 7.9 s, WER 1.9 % live vs 2.7 % whole
(docs/benchmarks.md, "Live dictation").
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import numpy as np

from . import engine as engine_mod
from . import transcribe

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
PAUSE_S = 0.6        # a quiet run this long ends a piece
MIN_PIECE_S = 2.0    # a piece shorter than this waits for the next pause (keeps context)
MAX_PIECE_S = 20.0   # hard cap without a pause
CAP_SEARCH_S = 5.0   # the cap cut goes to the quietest point of the last CAP_SEARCH_S before the cap
PROMPT_CHARS = 200   # end of the previous text passed as Whisper's prompt


class PieceCutter:
    """Incremental pause-and-cap cutter for 16 kHz mono float32 audio."""

    def __init__(
        self,
        pause_s: float = PAUSE_S,
        min_piece_s: float = MIN_PIECE_S,
        max_piece_s: float = MAX_PIECE_S,
        cap_search_s: float = CAP_SEARCH_S,
    ):
        self._frame_samples = round(transcribe.VAD_FRAME_S * SAMPLE_RATE)
        self._pause_frames = max(1, round(pause_s / transcribe.VAD_FRAME_S))
        self._min_piece_samples = round(min_piece_s * SAMPLE_RATE)
        self._max_piece_samples = max(self._frame_samples, round(max_piece_s * SAMPLE_RATE))
        self._cap_search_samples = round(cap_search_s * SAMPLE_RATE)

        self._pending = np.zeros(0, dtype=np.float32)
        self._unframed = np.zeros(0, dtype=np.float32)
        self._pending_start = 0
        self._stream_len = 0
        self._energies: list[float] = []
        self._threshold = 1e-4
        self._scan_frame = 0
        self._quiet_run_start: int | None = None
        self._pieces = 0

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        """Appends 16 kHz mono float32 samples and returns finished pieces in order."""
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return []

        self._pending = (
            np.concatenate([self._pending, arr])
            if self._pending.size
            else np.ascontiguousarray(arr)
        )
        self._unframed = (
            np.concatenate([self._unframed, arr])
            if self._unframed.size
            else np.ascontiguousarray(arr)
        )
        self._stream_len += arr.size

        n_new = self._unframed.size // self._frame_samples
        if n_new > 0:
            used = n_new * self._frame_samples
            frames = self._unframed[:used].reshape(n_new, self._frame_samples)
            rms = np.sqrt(np.square(frames).mean(axis=1))
            self._energies.extend(rms.tolist())
            self._unframed = np.ascontiguousarray(self._unframed[used:], dtype=np.float32)
            p95 = float(np.percentile(self._energies, 95))
            self._threshold = max(p95 * transcribe.RELATIVE_SILENCE_THRESHOLD, 1e-4)

        n_frames = len(self._energies)
        pieces: list[np.ndarray] = []
        while True:
            cap_at = self._pending_start + self._max_piece_samples
            if self._scan_frame < n_frames and (self._scan_frame + 1) * self._frame_samples <= cap_at:
                f = self._scan_frame
                self._scan_frame += 1
                cut = self._check_frame_pause(f)
                if cut is not None:
                    pieces.append(self._take_pending(cut))
                continue
            if self._stream_len >= cap_at:
                cut = self._cap_cut(cap_at)
                pieces.append(self._take_pending(cut))
                # Restart quiet-run tracking at the frame containing the cap cut.
                self._quiet_run_start = None
                self._scan_frame = cut // self._frame_samples
                continue
            break
        return pieces

    def _check_frame_pause(self, f: int) -> int | None:
        if self._energies[f] >= self._threshold:
            self._quiet_run_start = None
            return None
        if self._quiet_run_start is None:
            self._quiet_run_start = f
        run_len = f - self._quiet_run_start + 1
        if run_len != self._pause_frames:
            return None
        run_start_sample = self._quiet_run_start * self._frame_samples
        cut = run_start_sample + (self._pause_frames // 2) * self._frame_samples
        if cut - self._pending_start < self._min_piece_samples:
            return None
        speech_floor = max(self._threshold, transcribe.SPEECH_RMS_THRESHOLD)
        first_frame = self._pending_start // self._frame_samples
        if not any(self._energies[j] >= speech_floor for j in range(first_frame, self._quiet_run_start)):
            return None
        return cut

    def _cap_cut(self, cap_at: int) -> int:
        rel_stop = cap_at - self._pending_start
        rel_start = max(0, rel_stop - self._cap_search_samples)
        q = transcribe._quietest_point(self._pending, rel_start, rel_stop)
        if q <= 0 or q > rel_stop:
            return cap_at
        return self._pending_start + q

    def _take_pending(self, cut: int) -> np.ndarray:
        rel = cut - self._pending_start
        piece = np.ascontiguousarray(self._pending[:rel], dtype=np.float32)
        self._pending = np.ascontiguousarray(self._pending[rel:], dtype=np.float32)
        self._pending_start = cut
        self._pieces += 1
        return piece

    def tail(self) -> np.ndarray:
        """Returns all unreturned samples and empties the pending buffer."""
        out = np.ascontiguousarray(self._pending, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._unframed = np.zeros(0, dtype=np.float32)
        self._pending_start = self._stream_len
        self._quiet_run_start = None
        self._scan_frame = len(self._energies)
        return out

    @property
    def pending_s(self) -> float:
        return self._pending.size / SAMPLE_RATE

    @property
    def pieces(self) -> int:
        return self._pieces


def join_texts(texts: list[str]) -> str:
    """Collapses inner whitespace of each non-empty piece and joins with one space."""
    parts: list[str] = []
    for text in texts:
        clean = " ".join(text.split())
        if clean:
            parts.append(clean)
    return " ".join(parts)


def prompt_from(texts: list[str], limit: int = PROMPT_CHARS) -> str | None:
    """Returns the last <= `limit` chars of joined `texts` on a word boundary, or None."""
    joined = join_texts(texts)
    if not joined or limit <= 0:
        return None
    if len(joined) <= limit:
        return joined
    tail = joined[-limit:]
    if joined[-limit - 1] != " ":
        space = tail.find(" ")
        if space == -1:
            return None
        tail = tail[space + 1:]
    else:
        tail = tail.lstrip()
    return tail or None


class LiveDictation:
    """Coordinates incremental cutting and Whisper recognition for one dictation."""

    def __init__(
        self,
        engine,
        model: str,
        device: str,
        language: str,
        dictionary: dict | None = None,
        *,
        on_text: Callable[[str], None] | None = None,
        use_prompt: bool = True,
        cutter: PieceCutter | None = None,
        recognize=None,
        finish_text=None,
    ):
        self._engine = engine
        self._model = model
        self._device = device
        self._setting_language = language
        self._pinned_language: str | None = None
        self._dictionary = dictionary
        self._on_text = on_text
        self._use_prompt = use_prompt
        self._cutter = cutter if cutter is not None else PieceCutter()
        self._recognize = recognize
        self._finish_text = finish_text

        self._lock = threading.Lock()
        self._texts: list[str] = []
        self._jobs: list[engine_mod.Job] = []
        self._failed = False
        self._cancelled = False
        self._finished = False

    def _resolve_recognize(self):
        if self._recognize is not None:
            return self._recognize
        from . import transcribe as tr

        return tr.recognize_piece

    def _resolve_finish_text(self):
        if self._finish_text is not None:
            return self._finish_text
        from . import transcribe as tr

        return tr.finish_text

    @property
    def language(self) -> str:
        with self._lock:
            return self._pinned_language or self._setting_language

    @property
    def text(self) -> str:
        with self._lock:
            return join_texts(self._texts)

    @property
    def submitted(self) -> int:
        with self._lock:
            return len(self._jobs)

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    def feed(self, samples: np.ndarray) -> int:
        """Feeds audio samples to the cutter and submits any finished pieces."""
        with self._lock:
            if self._finished:
                raise RuntimeError("LiveDictation already finished")
            if self._cancelled:
                return 0
        pieces = self._cutter.feed(samples)
        for piece in pieces:
            self._submit_piece(piece)
        return len(pieces)

    def _submit_piece(self, piece: np.ndarray) -> None:
        with self._lock:
            if self._cancelled or self._finished:
                return
            idx = len(self._texts)
            self._texts.append("")

        def work(job: engine_mod.Job):
            with self._lock:
                if self._cancelled or job.cancel.is_set():
                    return
                earlier = list(self._texts[:idx])
                language = self._pinned_language or self._setting_language
            prompt = prompt_from(earlier) if self._use_prompt else None
            pipe = self._engine.pipeline(self._model, self._device)
            recognize = self._resolve_recognize()
            t0 = time.monotonic()
            piece_text, detected = recognize(pipe, piece, language, prompt)
            elapsed = time.monotonic() - t0

            with self._lock:
                if self._cancelled or job.cancel.is_set():
                    return
                self._texts[idx] = piece_text
                if (
                    self._setting_language == "auto"
                    and self._pinned_language is None
                    and piece_text
                    and detected is not None
                ):
                    self._pinned_language = detected
                joined = join_texts(self._texts[: idx + 1])

            log.info("Live piece %d: %.1f s audio in %.2f s",
                     idx + 1, piece.size / SAMPLE_RATE, elapsed)
            if self._on_text is not None:
                try:
                    self._on_text(joined)
                except Exception:
                    log.exception("Live dictation on_text callback failed")

        def on_error(_error: BaseException) -> None:
            with self._lock:
                if not self._cancelled:
                    self._failed = True

        job = self._engine.submit(
            work, engine_mod.DICTATION, "dictate-piece", on_error=on_error
        )
        with self._lock:
            self._jobs.append(job)
            if self._cancelled:
                job.cancel.set()

    def finish(
        self,
        recording: np.ndarray | None = None,
        on_done: Callable | None = None,
        on_error: Callable | None = None,
    ) -> engine_mod.Job:
        """Recognizes the remaining tail and finishes with one filtered text result."""
        with self._lock:
            if self._cancelled:
                raise RuntimeError("LiveDictation was cancelled")
            if self._finished:
                raise RuntimeError("LiveDictation already finished")
            self._finished = True
        tail_audio = self._cutter.tail()

        def work(_job: engine_mod.Job):
            started = time.monotonic()
            pipe = self._engine.pipeline(self._model, self._device)
            recognize = self._resolve_recognize()
            finish_text = self._resolve_finish_text()

            with self._lock:
                failed = self._failed
                earlier = list(self._texts)
                language = self._pinned_language or self._setting_language

            if failed and recording is not None:
                raw, _detected = recognize(pipe, recording, self._setting_language, None)
                final_text = finish_text(raw, self._dictionary)
            else:
                prompt = prompt_from(earlier) if self._use_prompt else None
                tail_text, _detected = recognize(pipe, tail_audio, language, prompt)
                with self._lock:
                    self._texts.append(tail_text)
                    joined = join_texts(self._texts)
                final_text = finish_text(joined, self._dictionary)

            return final_text, time.monotonic() - started, getattr(pipe, "device", self._device)

        job = self._engine.submit(
            work, engine_mod.DICTATION, "dictate", on_done=on_done, on_error=on_error
        )
        with self._lock:
            self._jobs.append(job)
        return job

    def cancel(self) -> None:
        """Marks the session cancelled and cancels any pending engine jobs."""
        with self._lock:
            self._cancelled = True
            jobs = list(self._jobs)
        for job in jobs:
            if not job.done.is_set():
                job.cancel.set()
