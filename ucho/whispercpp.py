# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS speech-to-text backend: Whisper via whisper.cpp (`pywhispercpp`).

Exposes the same surface as `transcribe.Engine` (load, release, generate,
device, requested_device) and returns `Result` shaped like an OpenVINO
Whisper result so chunking, silence skipping, hallucination filtering and
live transcription work unchanged.
"""

from __future__ import annotations

import gc
import importlib.util
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config, parakeet
from .i18n import t

log = logging.getLogger(__name__)

BACKEND = "whispercpp"
REPO = "ggerganov/whisper.cpp"
MODEL_LARGE = f"{REPO}:ggml-large-v3-q5_0.bin"
MODEL_TURBO = f"{REPO}:ggml-large-v3-turbo-q5_0.bin"
MODEL_TINY = f"{REPO}:ggml-tiny-q5_1.bin"
MODEL_BASE = f"{REPO}:ggml-base-q5_1.bin"
SAMPLE_RATE = 16_000


def available() -> bool:
    """True when `pywhispercpp` is installed (checked without importing it)."""
    try:
        return importlib.util.find_spec("pywhispercpp") is not None
    except Exception:
        return False


@dataclass
class Result(parakeet.Result):
    languages: list[str] = field(default_factory=list)


def _normalize_language(language: str | None) -> str | None:
    if not language:
        return None
    cleaned = language.strip()
    if not cleaned or cleaned.lower() == "auto":
        return None
    if cleaned.startswith("<|") and cleaned.endswith("|>"):
        cleaned = cleaned[2:-2]
    return cleaned.lower() or None


def _allowed_languages() -> list[str]:
    return [code for code, token in config.LANGUAGES.items() if token is not None]


class Engine:
    """whisper.cpp recognizer with the interface of `transcribe.Engine`."""

    def __init__(self, repo_id: str = MODEL_LARGE, device: str = "METAL",
                 threads: int | None = None):
        self.repo_id = repo_id
        self.requested_device = "METAL"
        self.device = "METAL"
        self.threads = threads or os.cpu_count() or 4
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        from . import transcribe

        folder = transcribe.model_path(self.repo_id, download=False)
        _, filename = config.split_model_id(self.repo_id)
        if filename is None:
            raise FileNotFoundError(self.repo_id)
        path = Path(os.path.abspath(Path(folder) / filename))
        if not path.is_file():
            raise FileNotFoundError(path)

        try:
            from pywhispercpp.model import Model
        except ImportError as error:
            raise RuntimeError(
                t("Speech recognition with whisper.cpp needs the pywhispercpp package.")
            ) from error

        log_path = config.LOG_FILE.with_name("whispercpp.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            offset = log_path.stat().st_size
        except OSError:
            offset = 0

        start = time.monotonic()
        self._model = Model(
            str(path),
            n_threads=self.threads,
            print_progress=False,
            print_realtime=False,
            redirect_whispercpp_logs_to=str(log_path),
        )

        appended = ""
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                appended = handle.read()
        except OSError:
            pass
        lower = appended.lower()
        if "metal" in lower or "mtl" in lower:
            self.device = "METAL"
        else:
            self.device = "CPU"
            log.info("whisper.cpp did not report Metal; falling back to CPU")
        log.info("whisper.cpp ready on %s in %.1fs (%d threads)",
                 self.device, time.monotonic() - start, self.threads)

    def release(self) -> None:
        if self._model is None:
            return
        self._model = None
        gc.collect()
        log.info("whisper.cpp model released")

    def _detect_language(self, audio: np.ndarray) -> str:
        allowed = _allowed_languages()
        found = self._model.auto_detect_language(audio)
        probs = found if isinstance(found, dict) else found[1]
        candidates = [(float(probs[code]), code) for code in allowed if code in probs]
        if candidates:
            _, code = max(candidates, key=lambda item: item[0])
            return code
        log.warning("whisper.cpp detected none of %s; falling back to %s", allowed, allowed[0])
        return allowed[0]

    def generate(self, audio: np.ndarray, language: str | None = None,
                 prompt: str | None = None) -> Result:
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if audio.size == 0:
            return Result()
        self.load()

        code = _normalize_language(language)
        if code is None:
            code = self._detect_language(audio)

        kwargs: dict = {"language": code}
        if prompt:
            kwargs["initial_prompt"] = prompt
        segments = self._model.transcribe(audio, **kwargs)

        chunks: list[parakeet.Chunk] = []
        for segment in segments:
            text = str(getattr(segment, "text", "")).strip()
            if not text:
                continue
            chunks.append(
                parakeet.Chunk(
                    start_ts=segment.t0 / 100.0,
                    end_ts=segment.t1 / 100.0,
                    text=text,
                )
            )
        joined = " ".join(chunk.text for chunk in chunks)
        return Result(
            texts=[joined] if joined else [],
            chunks=chunks,
            languages=[f"<|{code}|>"],
        )
