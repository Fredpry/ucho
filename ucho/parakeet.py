# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Optional speech-to-text backend: NVIDIA Parakeet TDT 0.6B v3 via sherpa-onnx.

Whisper large-v3 on OpenVINO stays the default. Parakeet is an alternative
for laptops without a usable Intel GPU: on the CPU it is several times faster
than Whisper (numbers in docs/benchmarks.md). It is not installed by default;
`install.ps1 -WithParakeet` adds the `sherpa-onnx` package, and the model
(fp32 ONNX export of NVIDIA's weights, 2.5 GB) is downloaded on demand from
Hugging Face like every other model, at the revision pinned in
`config.REVISIONS`.

This class has the same surface as `transcribe.Engine` (load, release,
generate, device), and `generate` returns an object shaped like an OpenVINO
Whisper result (`.texts`, `.chunks` with `start_ts`/`end_ts`/`text`). So the
chunking, silence skipping, hallucination filter, `merge_tracks`, the
transcript formats and the MCP server work unchanged.

Differences from Whisper that matter here:

- The PyPI wheel of sherpa-onnx runs on the CPU only (no DirectML, no
  OpenVINO execution provider), so `device` is always "CPU".
- The model has no language switch. It recognizes 25 European languages
  (Polish and English among them) and picks per utterance; the `language`
  argument is ignored.
- Long audio must be split at silences into short pieces, or quality and
  memory degrade badly (model README). Ucho cuts up to 5-minute chunks in
  forced-language mode, so `generate` splits again into pieces of at most
  `MAX_PIECE_S`.
- The model README warns that very quiet input decodes badly (sherpa-onnx
  issue #4024). Measured: the 5-minute synthetic meeting at -30 dB scored
  3.4 % WER without any gain and 3.8 % when scaled up first, so there is no
  gain step here.
- It does not invent text on silence or noise (measured on 30 s of silence,
  white noise, 50 Hz hum and speech-shaped noise, also with the silence gate
  of transcribe.py off).
"""

from __future__ import annotations

import gc
import logging
import os
import re
import time
from dataclasses import dataclass, field

import numpy as np

from . import config
from .i18n import t

log = logging.getLogger(__name__)

# Two ONNX exports of the same NVIDIA weights, both by the sherpa-onnx project.
# The app offers fp32 only: on synthetic Polish speech int8 lost word endings
# ("salę" -> "sal", "rusza" -> "rus") and scored 7.6 % WER on a 5-minute
# meeting against 3.6 % for fp32 and 2.7 % for Whisper (docs/benchmarks.md).
# int8 stays here for `tools/benchmark_wer.py --model`.
REPO_ID = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3"
REPO_ID_INT8 = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
BACKEND = "parakeet"

# encoder, decoder, joiner, tokens per export. The fp32 encoder keeps its
# weights in `encoder.weights` next to it (loaded by ONNX Runtime).
FILES = {
    REPO_ID: ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"),
    REPO_ID_INT8: ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"),
}

SAMPLE_RATE = 16_000

# The model README: "split at silences into 15-20 s chunks". Ucho's auto mode
# already cuts ~12 s windows; forced-language chunks (up to 300 s) are cut again.
MAX_PIECE_S = 20.0

# A segment ends after one of these, so the transcript gets sentence-sized
# lines like Whisper's instead of one line per 20 s piece.
_SENTENCE_END = re.compile(r"[.!?…]$")


available = config.sherpa_available

def default_threads() -> int:
    """Threads for the ONNX Runtime CPU session.

    Measured on a Core Ultra 7 258V (4 P-cores + 4 LP E-cores, 8 logical CPUs)
    on a 5-minute meeting: 2 threads were fastest, 4 threads about a quarter
    slower and 8 threads 3x slower than 2 (the E-cores hold the P-cores back).
    Other CPUs were not measured, so 2 is used wherever there are 4 or more
    logical CPUs.
    """
    return 2 if (os.cpu_count() or 1) >= 4 else 1


@dataclass
class Chunk:
    """One recognized sentence, like an OpenVINO Whisper chunk (seconds within the input)."""

    start_ts: float
    end_ts: float
    text: str


@dataclass
class Result:
    texts: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)

    def __str__(self) -> str:
        return self.texts[0] if self.texts else ""


class Engine:
    """Parakeet recognizer with the interface of `transcribe.Engine`."""

    def __init__(self, repo_id: str = REPO_ID, device: str = "CPU", threads: int | None = None,
                 recognizer_factory=None):
        self.repo_id = repo_id
        # Only the CPU is available in the sherpa-onnx wheel (see module docstring),
        # whatever device the settings name; the status line then says "CPU".
        self.requested_device = "CPU"
        self.device = "CPU"
        self.threads = threads or default_threads()
        self._factory = recognizer_factory or _recognizer
        self._pipe = None

    def load(self) -> None:
        if self._pipe is not None:
            return
        from .transcribe import model_path

        folder = model_path(self.repo_id, download=False)
        start = time.monotonic()
        self._pipe = self._factory(folder, self.threads, FILES.get(self.repo_id, FILES[REPO_ID]))
        log.info("Parakeet ready on CPU in %.1fs (%d threads)", time.monotonic() - start,
                 self.threads)

    def release(self) -> None:
        if self._pipe is None:
            return
        self._pipe = None
        gc.collect()
        log.info("Parakeet released")

    def generate(self, audio: np.ndarray, language: str | None = None,
                 prompt: str | None = None) -> Result:
        """Recognizes `audio` (16 kHz mono float32). `language` (see module docstring) and `prompt` are ignored."""
        from .transcribe import split_at_silence

        self.load()
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if audio.size == 0:
            return Result()
        if audio.size <= MAX_PIECE_S * SAMPLE_RATE:
            ranges = [(0, audio.size)]
        else:
            ranges = split_at_silence(audio, MAX_PIECE_S) or [(0, audio.size)]

        result = Result()
        for begin, end in ranges:
            piece = np.ascontiguousarray(audio[begin:end])
            stream = self._pipe.create_stream()
            stream.accept_waveform(SAMPLE_RATE, piece)
            self._pipe.decode_stream(stream)
            chunks = sentences(stream.result, begin / SAMPLE_RATE, (end - begin) / SAMPLE_RATE)
            result.chunks.extend(chunks)
        text = " ".join(c.text for c in result.chunks)
        result.texts = [text] if text else []
        return result


def _recognizer(folder, threads: int, files: tuple[str, str, str, str]):
    try:
        import sherpa_onnx
    except ImportError as error:
        raise RuntimeError(t("Parakeet needs the optional sherpa-onnx package. "
                             "Run the installer again with -WithParakeet.")) from error

    paths = [os.path.join(str(folder), name) for name in files]
    missing = [n for n, p in zip(files, paths, strict=True) if not os.path.exists(p)]
    if missing:
        raise RuntimeError(f"Parakeet model files missing: {', '.join(missing)}")
    encoder, decoder, joiner, tokens = paths
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=encoder,
        decoder=decoder,
        joiner=joiner,
        tokens=tokens,
        model_type="nemo_transducer",
        num_threads=threads,
        provider="cpu",
    )


def sentences(result, offset: float, length: float) -> list[Chunk]:
    """Token-level result -> sentence chunks with times relative to the whole input.

    `result` has `tokens` (BPE pieces, a leading space starts a word),
    `timestamps` (start of each token in seconds) and, for TDT models,
    `durations` (length of each token in seconds). A sentence ends at ., !, ?
    before a new word, or at the end of the piece. Its end time is the end of
    its last token, so pauses stay visible: `merge_tracks` starts a new
    paragraph after a pause longer than 1.5 s, as with Whisper. Without
    durations a sentence ends where the next one starts (the last one at the
    end of the piece).
    """
    tokens = list(getattr(result, "tokens", []) or [])
    times = list(getattr(result, "timestamps", []) or [])
    if not tokens or len(times) != len(tokens):
        text = str(getattr(result, "text", "") or "").strip()
        return [Chunk(offset, offset + length, text)] if text else []
    durations = list(getattr(result, "durations", []) or [])
    if len(durations) != len(tokens):
        durations = []

    chunks: list[Chunk] = []
    words: list[str] = []
    start: float | None = None
    for i, (token, at) in enumerate(zip(tokens, times, strict=True)):
        if start is None:
            start = float(at)
        words.append(token)
        last = i == len(tokens) - 1
        # Only before a new word: "10.00" is one word, not two sentences.
        if last or (_SENTENCE_END.search(token.strip() or " ")
                    and tokens[i + 1].startswith(" ")):
            if durations:
                stop = min(float(at) + float(durations[i]), length)
            else:
                stop = length if last else float(times[i + 1])
            text = re.sub(r"\s+", " ", "".join(words)).strip()
            if text:
                chunks.append(Chunk(offset + start, offset + max(stop, start), text))
            words, start = [], None
    return chunks
