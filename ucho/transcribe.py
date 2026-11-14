# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Local Whisper transcription through OpenVINO GenAI (Arc iGPU / CPU / NPU).

We use ready-made, pre-converted models from the OpenVINO repository on
Hugging Face, so neither PyTorch nor `optimum-intel` is needed.

We cut long recordings ourselves into chunks of a few minutes (at silences),
even though the pipeline handles long-form audio on its own. Reason: OpenVINO
does not report progress, and without it the user stares at a frozen bar for
many minutes. As a bonus, an error in one chunk does not ruin the whole file.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from . import config
from .i18n import t
from .merge import SPEAKER_ME, SPEAKER_MEETING, Segment, merge_tracks

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
ANALYSIS_FRAME_S = 0.1

# Forced language: long chunks, because full context improves quality.
CHUNK_S = 300.0
SILENCE_SEARCH_WINDOW_S = 15.0

# Auto-detection: short windows, because Whisper detects the language ONCE per
# generate call. With a long window a mixed PL/EN meeting ends with the second
# language translated into the first or dropped entirely. Measured on an
# alternating PL/EN recording: whole file at once - 0 of 3 EN parts, 30 s
# windows - 2 of 3 but translated into Polish, 12 s windows - 3 of 3 correctly
# in English.
CHUNK_AUTO_S = 12.0

# The price of short windows: on "mhm", a cough or noise the detector has
# nothing to listen to and picks at random from all ~100 Whisper languages -
# in a Polish meeting Icelandic, Korean or Portuguese pops up any moment. So we
# narrow the choice to the languages in config.LANGUAGES by passing a reduced
# `lang_to_id`.
#
# `suppress_tokens` does NOT help here (checked): language detection is a
# separate pass over `lang_to_id` that runs before generation starts, so token
# suppression does not touch it. Measured on 8 bad chunks of the last meeting:
# suppress_tokens - 1 of 8 fixed, reduced lang_to_id - 8 of 8.
AUTO_LANGUAGES = tuple(token for token in config.LANGUAGES.values() if token)
VAD_FRAME_S = 0.05
RELATIVE_SILENCE_THRESHOLD = 0.06
MIN_SILENCE_S = 0.35

# Below this peak RMS a chunk counts as silence and is not sent to the model at
# all. Whisper hallucinates on silence - it can produce a sentence in Icelandic
# or Russian in a Polish recording. The microphone track is silent for most of
# the meeting when you only listen, so this also saves a lot of time. Silence
# measures below 0.001, speech from about 0.01 up.
SPEECH_RMS_THRESHOLD = 0.003

ProgressFn = Callable[[float, str], None]


# --- model ---------------------------------------------------------------


def model_path(repo_id: str, download: bool = False) -> Path:
    """Returns the local model folder. Without `download` it never touches the network.

    We always download the pinned revision from `config.REVISIONS`. A local
    check also accepts an older copy downloaded earlier (e.g. before pinning),
    so nobody has to download several GB again.
    """
    from huggingface_hub import snapshot_download

    revision = config.REVISIONS.get(repo_id)
    if download:
        return Path(snapshot_download(repo_id, revision=revision))
    try:
        return Path(snapshot_download(repo_id, revision=revision, local_files_only=True))
    except Exception:
        if revision is None:
            raise
        return Path(snapshot_download(repo_id, local_files_only=True))


def model_downloaded(repo_id: str) -> bool:
    try:
        model_path(repo_id, download=False)
        return True
    except Exception as error:
        # A missing model is LocalEntryNotFoundError. Any other exception (e.g. a
        # broken .pyc in the standard library when importing huggingface_hub)
        # also ends as "model not downloaded", so without this entry the cause is lost.
        if type(error).__name__ != "LocalEntryNotFoundError":
            log.warning("Checking model %r failed: %r", repo_id, error)
        return False


def available_devices() -> list[str]:
    """OpenVINO devices, e.g. ['CPU', 'GPU', 'NPU']."""
    try:
        import openvino as ov

        return list(ov.Core().available_devices)
    except Exception as error:
        log.warning("Could not query OpenVINO for devices: %s", error)
        return ["CPU"]


def _pipeline_class():
    import openvino_genai as ov_genai

    # The class name changed between OpenVINO GenAI releases.
    cls = getattr(ov_genai, "WhisperPipeline", None)
    if cls is None:
        cls = getattr(ov_genai, "ASRPipeline", None)
    if cls is None:
        raise RuntimeError(
            "This openvino-genai version provides neither WhisperPipeline nor ASRPipeline."
        )
    return cls


def genai_version() -> str:
    try:
        import openvino_genai

        return str(getattr(openvino_genai, "__version__", "?"))
    except Exception:
        return "?"


class Engine:
    """Lazily built pipeline with automatic fallback to the CPU."""

    def __init__(self, repo_id: str, device: str = "GPU"):
        self.repo_id = repo_id
        self.requested_device = device
        self.device = device
        self._pipe = None
        self._lang_to_id: dict[str, int] | None = None

    def load(self) -> None:
        if self._pipe is not None:
            return

        folder = str(model_path(self.repo_id, download=False))
        cls = _pipeline_class()
        cache = str(config.OV_CACHE_DIR)
        config.OV_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        candidates = [self.requested_device]
        if self.requested_device != "CPU":
            candidates.append("CPU")

        last_error: Exception | None = None
        for device in candidates:
            for kwargs in ({"CACHE_DIR": cache}, {}):
                try:
                    start = time.monotonic()
                    self._pipe = cls(folder, device, **kwargs)
                    self.device = device
                    self._lang_to_id = self._narrowed_languages()
                    log.info(
                        "Pipeline ready on %s in %.1fs (cache=%s)",
                        device, time.monotonic() - start, bool(kwargs),
                    )
                    return
                except Exception as error:
                    last_error = error
                    log.warning("Could not build the pipeline on %s: %s", device, error)

        raise RuntimeError(
            f"Could not load the model on any device: {last_error}"
        )

    def _narrowed_languages(self) -> dict[str, int] | None:
        """The model's `lang_to_id` limited to AUTO_LANGUAGES.

        None when it cannot be read - then auto-detection runs over all
        languages, i.e. the behavior from before this change.
        """
        try:
            full = dict(self._pipe.get_generation_config().lang_to_id)
        except Exception as error:
            log.warning("Could not read lang_to_id: %s", error)
            return None

        narrowed = {token: full[token] for token in AUTO_LANGUAGES if token in full}
        if len(narrowed) < 2:
            log.warning(
                "The model knows only %d of %d auto-detection languages - not narrowing.",
                len(narrowed), len(AUTO_LANGUAGES),
            )
            return None

        log.info("Language auto-detection narrowed to: %s", ", ".join(narrowed))
        return narrowed

    def release(self) -> None:
        """Frees the GPU memory held by the model.

        After transcription the language-model summary may start - there is no
        point keeping Whisper on the same integrated GPU at that time.
        """
        if self._pipe is None:
            return
        self._pipe = None
        gc.collect()
        log.info("Whisper pipeline released")

    def generate(self, audio: np.ndarray, language: str | None):
        kwargs = {"task": "transcribe", "return_timestamps": True}
        if language:
            kwargs["language"] = language
        elif self._lang_to_id:
            kwargs["lang_to_id"] = self._lang_to_id

        try:
            return self._call(audio, kwargs)
        except TypeError:
            if "lang_to_id" not in kwargs:
                raise
            # An older GenAI release does not know this parameter - carry on
            # without narrowing instead of failing the whole transcription.
            log.warning("openvino-genai does not accept lang_to_id - auto-detection without narrowing")
            self._lang_to_id = None
            del kwargs["lang_to_id"]
            return self._call(audio, kwargs)

    def _call(self, audio: np.ndarray, kwargs: dict):
        # Newer releases accept an ndarray, older ones need a list of floats.
        try:
            return self._pipe.generate(audio, **kwargs)
        except TypeError:
            return self._pipe.generate(audio.tolist(), **kwargs)


# --- cutting audio --------------------------------------------------------


def _quietest_point(audio: np.ndarray, start: int, stop: int) -> int:
    """Index of the quietest spot in the given range - we cut there."""
    start = max(0, start)
    stop = min(len(audio), stop)
    if stop - start < SAMPLE_RATE:
        return stop

    frame = int(ANALYSIS_FRAME_S * SAMPLE_RATE)
    chunk = audio[start:stop]
    count = len(chunk) // frame
    if count < 5:
        return (start + stop) // 2

    energy = np.square(chunk[: count * frame].reshape(count, frame)).mean(axis=1)
    # Smoothing with a 0.5 s window - we look for a real pause, not a single frame.
    kernel = np.ones(5, dtype=np.float32) / 5.0
    smooth = np.convolve(energy, kernel, mode="same")
    return start + int(np.argmin(smooth)) * frame + frame // 2


def peak_rms(chunk: np.ndarray) -> float:
    """The loudest 100 ms window in the chunk.

    Peak, not mean - otherwise one short sentence in a long, quiet chunk
    would count as silence and be skipped.
    """
    if chunk.size == 0:
        return 0.0
    frame = int(ANALYSIS_FRAME_S * SAMPLE_RATE)
    count = chunk.size // frame
    if count < 1:
        return float(np.sqrt(np.mean(np.square(chunk))))
    windows = np.sqrt(
        np.square(chunk[: count * frame].reshape(count, frame)).mean(axis=1)
    )
    return float(windows.max())


def _silence_boundaries(audio: np.ndarray) -> list[int]:
    """Indices of the middles of long enough pauses - candidates for a cut."""
    frame = int(VAD_FRAME_S * SAMPLE_RATE)
    count = len(audio) // frame
    if count < 2:
        return []

    energy = np.sqrt(
        np.square(audio[: count * frame].reshape(count, frame)).mean(axis=1)
    )
    threshold = max(float(energy.max()) * RELATIVE_SILENCE_THRESHOLD, 1e-4)
    quiet = energy < threshold
    min_frames = max(1, int(MIN_SILENCE_S / VAD_FRAME_S))

    boundaries: list[int] = []
    i = 0
    while i < count:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < count and quiet[j]:
            j += 1
        if j - i >= min_frames:
            boundaries.append((i + j) // 2 * frame)
        i = j
    return boundaries


def shorten_pauses(audio: np.ndarray, max_pause_s: float = 0.6,
                   min_pause_s: float = 1.0) -> np.ndarray:
    """Cuts silence at the start and end and shortens pauses longer than `min_pause_s`.

    For dictation, where the whole recording goes to the model in one call.
    On longer silence Whisper invents text (in Polish usually "Dziekuje." or
    "Dziekuje za uwage." at the end), and after a pause it can drop or garble
    the start of the next utterance. Measured on speech with 10-second pauses:
    without shortening "Dziekuje." appeared at the end and "Jasne" and
    "time-line" vanished; after shortening the text was complete with no additions.
    """
    frame = int(VAD_FRAME_S * SAMPLE_RATE)
    count = len(audio) // frame
    if count < 4:
        return audio

    energy = np.sqrt(
        np.square(audio[: count * frame].reshape(count, frame)).mean(axis=1)
    )
    # Threshold relative to loud speech (95th percentile), not the maximum - a
    # single cough or knock then does not lift it above quiet speech.
    threshold = max(float(np.percentile(energy, 95)) * RELATIVE_SILENCE_THRESHOLD, 1e-4)
    quiet = energy < threshold
    min_frames = int(min_pause_s / VAD_FRAME_S)
    keep_frames = int(max_pause_s / VAD_FRAME_S)

    keep = np.ones(count, dtype=bool)
    i = 0
    while i < count:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < count and quiet[j]:
            j += 1
        edge = i == 0 or j == count
        if edge or j - i >= min_frames:
            kept = 0 if edge else keep_frames
            half = kept // 2
            keep[i + half: j - (kept - half)] = False
        i = j

    if keep.all():
        return audio
    speech = audio[: count * frame][np.repeat(keep, frame)]
    if speech.size == 0:
        return speech
    # A short margin so the first and last sound are not cut off.
    margin = np.zeros(int(0.2 * SAMPLE_RATE), dtype=np.float32)
    return np.ascontiguousarray(np.concatenate([margin, speech, margin]),
                                dtype=np.float32)


def split_at_silence(audio: np.ndarray, max_s: float) -> list[tuple[int, int]]:
    """Cuts into chunks no longer than `max_s`, sticking to boundaries in silence."""
    max_len = int(max_s * SAMPLE_RATE)
    ranges: list[tuple[int, int]] = []
    start = 0

    for boundary in _silence_boundaries(audio) + [len(audio)]:
        if boundary <= start:
            continue
        # A too short piece is joined to the next one - otherwise we would chop
        # utterances to shreds and the model would lose the sentence context.
        if boundary - start < max_len * 0.4 and boundary != len(audio):
            continue
        while boundary - start > max_len:
            ranges.append((start, start + max_len))
            start += max_len
        if boundary > start:
            ranges.append((start, boundary))
        start = boundary

    return [(a, b) for a, b in ranges if b - a > SAMPLE_RATE // 4]


def split_into_chunks(audio: np.ndarray, auto_detect: bool = False
                      ) -> list[tuple[int, int]]:
    """Picks the cutting strategy for the language mode."""
    if auto_detect:
        ranges = split_at_silence(audio, CHUNK_AUTO_S)
        return ranges or [(0, len(audio))]

    target_len = int(CHUNK_S * SAMPLE_RATE)
    window = int(SILENCE_SEARCH_WINDOW_S * SAMPLE_RATE)

    ranges = []
    begin = 0
    while begin < len(audio):
        target = begin + target_len
        if target >= len(audio) - window:
            ranges.append((begin, len(audio)))
            break
        end = _quietest_point(audio, target - window, target + window)
        if end <= begin:
            end = min(len(audio), target)
        ranges.append((begin, end))
        begin = end
    return ranges


def _chunks(result) -> list:
    """Normalizes the pipeline result - `chunks` is a list or a list of lists."""
    chunks = getattr(result, "chunks", None)
    if not chunks:
        return []
    first = chunks[0]
    if isinstance(first, (list, tuple)):
        return list(first)
    return list(chunks)


# --- main function --------------------------------------------------------


def transcribe_file(
    file: Path,
    engine: Engine,
    speaker: str,
    language: str | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    weight: tuple[float, float] = (0.0, 1.0),
) -> list[Segment]:
    """Transcribes one 16 kHz mono WAV/FLAC file.

    `weight` is the (from, to) range of the global progress bar this file
    takes - so two tracks can report one shared 0..1 progress.
    """
    audio, sr = sf.read(str(file), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        import soxr

        audio = soxr.resample(audio, sr, SAMPLE_RATE, quality="HQ")
    audio = np.ascontiguousarray(audio, dtype=np.float32)

    if audio.size < SAMPLE_RATE // 2:
        return []

    # The whole track is silence (e.g. the microphone when you only listened) -
    # there is nothing to transcribe, and on silence the model only hallucinates.
    if peak_rms(audio) < SPEECH_RMS_THRESHOLD:
        log.info("Track %s skipped - silence only", file.name)
        if progress:
            progress(weight[1], "")
        return []

    engine.load()
    ranges = split_into_chunks(audio, auto_detect=language is None)
    weight_from, weight_to = weight
    span = max(0.0, weight_to - weight_from)

    segments: list[Segment] = []
    for i, (begin, end) in enumerate(ranges):
        if cancel is not None and cancel.is_set():
            break

        offset = begin / SAMPLE_RATE
        chunk = audio[begin:end]

        if peak_rms(chunk) < SPEECH_RMS_THRESHOLD:
            if progress:
                progress(weight_from + span * (i + 1) / len(ranges), "")
            continue

        try:
            result = engine.generate(chunk, language)
        except Exception as error:
            log.exception("Transcription error in chunk %d of file %s", i, file.name)
            if progress:
                progress(weight_from + span * (i + 1) / len(ranges),
                         f"[segment {i + 1} error: {error}]")
            continue

        end_s = end / SAMPLE_RATE
        chunks = _chunks(result)
        if chunks:
            last_end = offset
            for item in chunks:
                start = float(getattr(item, "start_ts", -1.0))
                stop = float(getattr(item, "end_ts", -1.0))
                text = str(getattr(item, "text", "")).strip()
                if not text:
                    continue
                # Whisper sometimes returns -1 when it does not know the timestamp.
                start = last_end if start < 0 else offset + start
                stop = start + 2.0 if stop < 0 else offset + stop
                # On silence the model can return a timestamp outside the
                # processed chunk - clamp it to the chunk boundaries.
                start = min(max(start, offset), end_s)
                stop = min(max(stop, start), end_s)
                last_end = stop
                segments.append(Segment(start, stop, text, speaker))
        else:
            text = str(result).strip()
            if text:
                segments.append(
                    Segment(offset, end / SAMPLE_RATE, text, speaker)
                )

        if progress:
            last_text = segments[-1].text if segments else ""
            progress(weight_from + span * (i + 1) / len(ranges), last_text)

    return segments


def transcribe_session(
    files: dict[str, Path],
    model: str,
    device: str,
    language_code: str = "auto",
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    status: Callable[[str], None] | None = None,
) -> tuple[list[Segment], str]:
    """Transcribes all tracks of a session. Returns (segments, used_device)."""
    language = config.LANGUAGES.get(language_code)
    engine = Engine(model, device)

    speakers = {"microphone": SPEAKER_ME, "system": SPEAKER_MEETING}
    to_do = [(n, p) for n, p in files.items() if p and Path(p).exists()]
    if not to_do:
        return [], device

    # The first load on the GPU can take tens of seconds (model compilation),
    # so we say so before the first chunk starts.
    if status:
        status(t("Loading model on {device}...", device=device))
    engine.load()
    if status:
        status(t("Transcribing on {device}...", device=engine.device))

    step = 1.0 / len(to_do)
    results: list[list[Segment]] = []
    try:
        for i, (name, file) in enumerate(to_do):
            if cancel is not None and cancel.is_set():
                break
            results.append(
                transcribe_file(
                    Path(file), engine, speakers.get(name, name), language,
                    progress=progress, cancel=cancel,
                    weight=(i * step, (i + 1) * step),
                )
            )
    finally:
        used_device = engine.device
        engine.release()

    return merge_tracks(*results), used_device
