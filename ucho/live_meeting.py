# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Live meeting transcription: incrementally chunk and recognize while recording.

While a meeting is being recorded to two 16 kHz mono PCM_16 FLAC tracks
("microphone" and "system"), `TrackChunker` cuts each growing track at the same
boundaries that `transcribe.split_into_chunks` will pick after stop, and
`LiveMeeting` sends settled non-silent chunks to the shared Whisper engine
(`ucho/engine.py`) one at a time.

Recognized chunks are stored in `ChunkCache` keyed by (track, begin, end) with
a BLAKE2b digest of the PCM_16-quantized samples. After recording stops, the
normal offline `transcribe.iter_transcribe_session` pass still runs on the final
FLAC files and reuses cached results only when both the sample range and the
samples match bit-for-bit, so the final transcript is always identical to an
offline transcription.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Callable

import numpy as np

from . import config, transcribe
from . import engine as engine_mod
from .merge import SPEAKER_ME, SPEAKER_MEETING, Segment, merge_tracks

log = logging.getLogger(__name__)

SAMPLE_RATE = transcribe.SAMPLE_RATE
POLL_S = 1.0
MAX_BEHIND_S = 120.0
MAX_ERRORS = 3

_SPEAKERS = {
    "microphone": SPEAKER_ME,
    "system": SPEAKER_MEETING,
}


def quantize_pcm16(samples: np.ndarray) -> np.ndarray:
    """Returns float32 1-D samples matching a PCM_16 FLAC write-and-read round trip."""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    # Adding +0.0 normalizes IEEE-754 -0.0 to +0.0 so sample bytes match libsndfile.
    q = np.clip(np.rint(x * np.float32(32768.0)), -32768, 32767) + np.float32(0.0)
    return (q / np.float32(32768.0)).astype(np.float32)


def chunk_digest(chunk: np.ndarray) -> bytes:
    """Returns a 16-byte BLAKE2b digest of float32 samples."""
    data = np.ascontiguousarray(chunk, dtype=np.float32)
    return hashlib.blake2b(data.tobytes(), digest_size=16).digest()


def quiet_run_boundaries(energy: np.ndarray, threshold: float) -> list[int]:
    """Vectorized equivalent of `transcribe.boundaries_from_energy`."""
    arr = np.asarray(energy)
    if arr.size == 0:
        return []
    frame = int(transcribe.VAD_FRAME_S * SAMPLE_RATE)
    min_frames = max(1, int(transcribe.MIN_SILENCE_S / transcribe.VAD_FRAME_S))
    quiet = np.asarray(arr < threshold, dtype=bool)
    padded = np.empty(quiet.size + 2, dtype=bool)
    padded[0] = False
    padded[1:-1] = quiet
    padded[-1] = False

    starts = np.flatnonzero(~padded[:-1] & padded[1:])
    ends = np.flatnonzero(padded[:-1] & ~padded[1:])
    keep = (ends - starts) >= min_frames
    if not np.any(keep):
        return []
    mids = ((starts[keep] + ends[keep]) // 2) * frame
    return [int(x) for x in mids.tolist()]


def trailing_quiet_start(energy: np.ndarray, threshold: float) -> int:
    """Index of the first frame in the trailing run of quiet frames."""
    arr = np.asarray(energy)
    n = len(arr)
    if n == 0:
        return 0
    not_quiet = np.flatnonzero(~(arr < threshold))
    if not_quiet.size == 0:
        return 0
    return int(not_quiet[-1]) + 1


def _copy_segments(segments: list[Segment]) -> list[Segment]:
    return [Segment(s.start, s.end, s.text, s.speaker) for s in segments]


def _track_sort_key(track: str) -> tuple[int, str]:
    if track == "microphone":
        return (0, track)
    if track == "system":
        return (1, track)
    return (2, track)


class ChunkCache:
    """Thread-safe cache of live-transcribed chunks keyed by range and sample digest."""

    def __init__(self, model: str, device: str, language: str) -> None:
        self.model = model
        self.device = device
        self.language = language
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, int, int], tuple[bytes, list[Segment]]] = {}
        self._hits = 0
        self._misses = 0

    def matches(self, model: str, device: str, language: str) -> bool:
        return self.model == model and self.device == device and self.language == language

    def put(
        self,
        track: str,
        begin: int,
        end: int,
        chunk: np.ndarray,
        segments: list[Segment],
    ) -> None:
        digest = chunk_digest(chunk)
        copied = _copy_segments(segments)
        with self._lock:
            self._entries[(track, begin, end)] = (digest, copied)

    def get(
        self,
        track: str,
        begin: int,
        end: int,
        chunk: np.ndarray,
    ) -> list[Segment] | None:
        digest = chunk_digest(chunk)
        with self._lock:
            entry = self._entries.get((track, begin, end))
            if entry is None or entry[0] != digest:
                self._misses += 1
                return None
            self._hits += 1
            return _copy_segments(entry[1])

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def segments(self) -> dict[str, list[Segment]]:
        with self._lock:
            by_track: dict[str, list[tuple[int, int, list[Segment]]]] = {}
            for (track, begin, end), (_digest, segs) in self._entries.items():
                by_track.setdefault(track, []).append((begin, end, segs))
            result: dict[str, list[Segment]] = {}
            for track, items in by_track.items():
                items.sort(key=lambda item: (item[0], item[1]))
                result[track] = [
                    Segment(s.start, s.end, s.text, s.speaker)
                    for _b, _e, segs in items
                    for s in segs
                ]
            return result


class TrackChunker:
    """Incremental chunker for one 16 kHz mono float32 track."""

    def __init__(self, auto_detect: bool) -> None:
        self._auto_detect = auto_detect
        self._length = 0
        self._sent = 0

        # Active samples covering global indices [self._sent, self._sent + self._buf_len).
        self._buf = np.empty(0, dtype=np.float32)
        self._buf_len = 0

        # Forced-mode constants.
        self._forced_threshold = (
            int(transcribe.CHUNK_S * SAMPLE_RATE)
            + int(transcribe.SILENCE_SEARCH_WINDOW_S * SAMPLE_RATE)
        )

        # Auto-mode state: frame-aligned remainder and amortized float32 energy history.
        self._frame = int(transcribe.VAD_FRAME_S * SAMPLE_RATE)
        self._unframed = np.empty(0, dtype=np.float32)
        self._energy_buf = np.empty(256, dtype=np.float32)
        self._energy_len = 0

    @property
    def length(self) -> int:
        return self._length

    def _append_samples(self, arr: np.ndarray) -> None:
        needed = self._buf_len + arr.size
        if needed > self._buf.size:
            new_cap = max(needed, self._buf.size * 2, 4096)
            grown = np.empty(new_cap, dtype=np.float32)
            if self._buf_len:
                grown[:self._buf_len] = self._buf[:self._buf_len]
            self._buf = grown
        self._buf[self._buf_len:needed] = arr
        self._buf_len = needed
        self._length += arr.size

    def _take_settled(self, ranges: list[tuple[int, int]]) -> list[tuple[int, int, np.ndarray]]:
        if not ranges:
            return []
        base = self._sent
        view = self._buf[:self._buf_len]
        out = [
            (begin, end, np.array(view[begin - base:end - base], dtype=np.float32, copy=True))
            for begin, end in ranges
        ]
        new_sent = ranges[-1][1]
        drop = new_sent - base
        remaining = self._buf_len - drop
        if remaining == 0:
            self._buf_len = 0
            if self._buf.size > 16 * SAMPLE_RATE:
                self._buf = np.empty(0, dtype=np.float32)
        elif self._buf.size > max(32 * SAMPLE_RATE, remaining * 4):
            shrunk = np.empty(max(remaining * 2, 4096), dtype=np.float32)
            shrunk[:remaining] = self._buf[drop:self._buf_len]
            self._buf = shrunk
            self._buf_len = remaining
        else:
            self._buf[:remaining] = self._buf[drop:self._buf_len].copy()
            self._buf_len = remaining
        self._sent = new_sent
        return out

    def _append_energy(self, new_energy: np.ndarray) -> None:
        needed = self._energy_len + new_energy.size
        if needed > self._energy_buf.size:
            new_cap = max(needed, self._energy_buf.size * 2)
            grown = np.empty(new_cap, dtype=np.float32)
            if self._energy_len:
                grown[:self._energy_len] = self._energy_buf[:self._energy_len]
            self._energy_buf = grown
        self._energy_buf[self._energy_len:needed] = new_energy
        self._energy_len = needed

    def feed(self, samples: np.ndarray) -> list[tuple[int, int, np.ndarray]]:
        """Appends already-quantized float32 samples and returns newly settled chunks."""
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return []
        self._append_samples(arr)

        if not self._auto_detect:
            if self._length - self._sent <= self._forced_threshold:
                return []
            ranges = transcribe.split_into_chunks(self._buf[:self._buf_len], auto_detect=False)
            if len(ranges) <= 1:
                return []
            base = self._sent
            settled = [(base + b, base + e) for b, e in ranges[:-1]]
            return self._take_settled(settled)

        combined = np.concatenate((self._unframed, arr)) if self._unframed.size else arr
        n_new = combined.size // self._frame
        if n_new == 0:
            self._unframed = np.ascontiguousarray(combined, dtype=np.float32)
            return []

        used = n_new * self._frame
        new_energy = transcribe.frame_energy(combined[:used])
        self._append_energy(new_energy)
        self._unframed = np.ascontiguousarray(combined[used:], dtype=np.float32)

        if self._energy_len < 2:
            return []

        energy = self._energy_buf[:self._energy_len]
        threshold = transcribe.silence_threshold(energy)
        safe = trailing_quiet_start(energy, threshold) * self._frame
        closed = [b for b in quiet_run_boundaries(energy, threshold) if b < safe]
        ranges = transcribe.ranges_at_boundaries(closed, safe, transcribe.CHUNK_AUTO_S)
        settled = [(b, e) for b, e in ranges if e < safe and b >= self._sent]
        return self._take_settled(settled)


class LiveMeeting:
    """Coordinates live chunking and recognition of growing meeting tracks."""

    def __init__(
        self,
        engine,
        model: str,
        device: str,
        language_code: str,
        *,
        on_update: Callable[[list[Segment]], None] | None = None,
        should_wait: Callable[[], bool] | None = None,
        poll_s: float = POLL_S,
        max_behind_s: float = MAX_BEHIND_S,
        recognize: Callable | None = None,
    ) -> None:
        self._engine = engine
        self._model = model
        self._device = device
        self._language_code = language_code
        self._language = config.LANGUAGES.get(language_code)
        self._auto_detect = self._language is None
        self.cache = ChunkCache(model, device, language_code)

        self._on_update = on_update
        self._should_wait = should_wait
        self._poll_s = poll_s
        self._max_behind_s = max_behind_s
        self._recognize = recognize if recognize is not None else transcribe.transcribe_chunk

        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._started = False
        self._held = False
        self._stopped = False
        self._gave_up = False

        self._incoming: dict[str, list[np.ndarray]] = {}
        self._chunkers: dict[str, TrackChunker] = {}
        self._todo: list[tuple[str, int, int, np.ndarray]] = []
        self._in_flight = False
        self._in_flight_samples = 0
        self._in_flight_start: tuple[str, int] | None = None  # (track, begin) of the job in flight
        self._lengths: dict[str, int] = {}  # samples chunked so far per track
        self._update_pending = False
        self._job_finished = False

        self._submitted = 0
        self._done = 0
        self._errors = 0

    @property
    def gave_up(self) -> bool:
        with self._cv:
            return self._gave_up

    @property
    def behind_s(self) -> float:
        with self._cv:
            return self._behind_samples_locked() / SAMPLE_RATE

    @property
    def submitted(self) -> int:
        with self._cv:
            return self._submitted

    @property
    def done(self) -> int:
        with self._cv:
            return self._done

    @property
    def errors(self) -> int:
        with self._cv:
            return self._errors

    @property
    def stopped(self) -> bool:
        with self._cv:
            return self._stopped

    @property
    def auto_detect(self) -> bool:
        return self._auto_detect

    def _behind_samples_locked(self) -> int:
        """How far live work lags behind the recording: per track, from the begin of
        the oldest chunk not yet recognized to the end of what was recorded.

        Measured per track, not summed: with a forced language both tracks
        settle a 5-minute chunk at the same moment, which is not falling behind.
        """
        oldest: dict[str, int] = {}
        pending = [(t, b) for t, b, _e, _a in self._todo]
        if self._in_flight_start is not None:
            pending.append(self._in_flight_start)
        for track, begin in pending:
            oldest[track] = min(oldest.get(track, begin), begin)
        return max((self._lengths.get(track, begin) - begin for track, begin in oldest.items()),
                   default=0)

    def _give_up_locked(self) -> None:
        self._gave_up = True
        self._todo.clear()
        self._incoming.clear()

    def feed(self, track: str, samples: np.ndarray) -> None:
        """Queues a copy of a recorded audio block for live chunking."""
        arr = np.array(samples, dtype=np.float32, copy=True).reshape(-1)
        if arr.size == 0:
            return
        with self._cv:
            if self._stopped or self._gave_up:
                return
            self._incoming.setdefault(track, []).append(arr)

    def start(self) -> None:
        """Holds the Whisper pipeline on the engine and starts the worker thread."""
        with self._cv:
            if self._started or self._stopped:
                return
            self._started = True
        self._engine.hold((self._model, self._device))
        with self._cv:
            if self._stopped:
                unhold_now = True
            else:
                unhold_now = False
                self._held = True
                self._thread = threading.Thread(
                    target=self._loop, name="live-meeting", daemon=True
                )
                self._thread.start()
        if unhold_now:
            self._engine.unhold((self._model, self._device))

    def stop(self, timeout: float = 5.0) -> ChunkCache:
        """Stops the worker thread without cancelling any in-flight job and returns the cache."""
        with self._cv:
            if not self._stopped:
                self._stopped = True
                self._incoming.clear()
                self._todo.clear()
                self._update_pending = False
                self._cv.notify_all()
            thread = self._thread

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

        with self._cv:
            self._incoming.clear()
            self._todo.clear()
            if thread is None or not thread.is_alive():
                self._chunkers.clear()
            should_unhold = self._held
            self._held = False

        if should_unhold:
            self._engine.unhold((self._model, self._device))
        return self.cache

    def segments(self) -> list[Segment]:
        """Returns the merged transcript of all chunks currently in the cache."""
        by_track = self.cache.segments()
        ordered = [by_track[name] for name in sorted(by_track, key=_track_sort_key)]
        return merge_tracks(*ordered)

    def _loop(self) -> None:
        limit_s = self._max_behind_s + (
            transcribe.CHUNK_AUTO_S
            if self._auto_detect
            else transcribe.CHUNK_S + transcribe.SILENCE_SEARCH_WINDOW_S
        )
        try:
            while True:
                if not self._flush_update():
                    break
                if not self._ingest_batches(limit_s):
                    break
                if not self._maybe_submit():
                    break
                with self._cv:
                    if self._stopped:
                        break
                    if not self._update_pending and not self._job_finished:
                        self._cv.wait(timeout=self._poll_s)
                    self._job_finished = False
        finally:
            with self._cv:
                self._chunkers.clear()

    def _flush_update(self) -> bool:
        with self._cv:
            if self._stopped:
                return False
            notify = self._update_pending
            self._update_pending = False
        if notify and self._on_update is not None:
            try:
                self._on_update(self.segments())
            except Exception:
                log.exception("Live meeting update callback failed")
        with self._cv:
            return not self._stopped

    def _ingest_batches(self, limit_s: float) -> bool:
        with self._cv:
            if self._stopped:
                return False
            if self._gave_up:
                self._incoming.clear()
                return True
            batches = self._incoming
            self._incoming = {}

        new_items: list[tuple[str, int, int, np.ndarray]] = []
        for track, blocks in batches.items():
            if not blocks:
                continue
            raw = blocks[0] if len(blocks) == 1 else np.concatenate(blocks)
            quantized = quantize_pcm16(raw)
            chunker = self._chunkers.get(track)
            if chunker is None:
                chunker = TrackChunker(self._auto_detect)
                self._chunkers[track] = chunker
            settled = chunker.feed(quantized)
            with self._cv:
                self._lengths[track] = chunker.length
            for begin, end, audio in settled:
                if transcribe.peak_rms(audio) >= transcribe.SPEECH_RMS_THRESHOLD:
                    new_items.append((track, begin, end, audio))

        with self._cv:
            if self._stopped:
                return False
            if not self._gave_up:
                if new_items:
                    self._todo.extend(new_items)
                behind_s = self._behind_samples_locked() / SAMPLE_RATE
                if behind_s > limit_s:
                    self._give_up_locked()
                    log.warning(
                        "Live meeting gave up: %.1f s behind (limit %.1f s)",
                        behind_s,
                        limit_s,
                    )
            return True

    def _maybe_submit(self) -> bool:
        with self._cv:
            can_submit = (
                not self._stopped
                and not self._gave_up
                and not self._in_flight
                and bool(self._todo)
            )
        if not can_submit:
            with self._cv:
                return not self._stopped

        waiting = False
        if self._should_wait is not None:
            try:
                waiting = bool(self._should_wait())
            except Exception:
                log.exception("Live meeting should_wait callback failed")
                waiting = False
        if waiting:
            with self._cv:
                return not self._stopped

        with self._cv:
            if self._stopped or self._gave_up or self._in_flight or not self._todo:
                return not self._stopped
            idx = min(
                range(len(self._todo)),
                key=lambda i: (self._todo[i][1], _track_sort_key(self._todo[i][0])),
            )
            track, begin, end, audio = self._todo.pop(idx)
            self._in_flight = True
            self._in_flight_samples = end - begin
            self._in_flight_start = (track, begin)
            self._submitted += 1

        speaker = _SPEAKERS.get(track, track)
        model = self._model
        device = self._device
        language = self._language
        recognize = self._recognize

        def work(job: engine_mod.Job):
            if job.cancel.is_set():
                return None
            pipe = self._engine.pipeline(model, device)
            segs = recognize(pipe, audio, begin, end, language, speaker)
            self.cache.put(track, begin, end, audio, segs)
            return segs

        try:
            self._engine.submit(
                work,
                engine_mod.NORMAL,
                "live-chunk",
                on_done=self._on_job_done,
                on_error=self._on_job_error,
            )
        except Exception:
            log.exception("Failed to submit live meeting chunk")
            with self._cv:
                self._in_flight = False
                self._in_flight_samples = 0
                self._in_flight_start = None
                self._errors += 1
                if self._errors >= MAX_ERRORS and not self._gave_up:
                    self._give_up_locked()
                    log.warning("Live meeting gave up after %d errors", self._errors)
        return True

    def _on_job_done(self, _result) -> None:
        with self._cv:
            self._in_flight = False
            self._in_flight_samples = 0
            self._in_flight_start = None
            self._done += 1
            self._job_finished = True
            if not self._stopped:
                self._update_pending = True
            self._cv.notify_all()

    def _on_job_error(self, error: BaseException) -> None:
        with self._cv:
            self._in_flight = False
            self._in_flight_samples = 0
            self._in_flight_start = None
            self._job_finished = True
            if not isinstance(error, engine_mod.Cancelled):
                self._errors += 1
                if self._errors >= MAX_ERRORS and not self._gave_up:
                    self._give_up_locked()
                    log.warning("Live meeting gave up after %d errors", self._errors)
            self._cv.notify_all()
