# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Shared Whisper engine: priorities, dictation between transcription chunks, model reuse.

Uses a fake pipeline, so no OpenVINO or model is needed.
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ucho import engine as engine_mod

RATE = 16_000


def speech(seconds: float, pause_every: float = 4.0) -> np.ndarray:
    """A tone with a 0.6 s pause every few seconds, so auto mode cuts it into chunks."""
    t = np.arange(int(seconds * RATE)) / RATE
    audio = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    period = int(pause_every * RATE)
    for start in range(period, audio.size, period):
        audio[start:start + int(0.6 * RATE)] = 0.0
    return audio


class FakePipe:
    def __init__(self, calls: list, model: str, device: str, on_generate=None):
        self.calls = calls
        self.repo_id = model
        self.requested_device = device
        self.device = device
        self._pipe = None
        self.loads = 0
        self.on_generate = on_generate

    def load(self):
        if self._pipe is None:
            self._pipe = object()
            self.loads += 1
            self.calls.append(("load", self.repo_id))

    def release(self):
        if self._pipe is not None:
            self.calls.append(("release", self.repo_id))
        self._pipe = None

    def generate(self, audio, language):
        seconds = audio.size / RATE
        kind = "dictate" if seconds < 3 else "chunk"
        self.calls.append((kind, self.repo_id))
        if self.on_generate:
            self.on_generate(kind)
        chunk = SimpleNamespace(start_ts=0.0, end_ts=min(seconds, 2.0), text=f"{kind} text")
        return SimpleNamespace(chunks=[chunk], texts=[f"{kind} text"])


@pytest.fixture
def calls():
    return []


def make_engine(calls, on_generate=None):
    pipes = []

    def factory(model, device):
        pipe = FakePipe(calls, model, device, on_generate)
        pipes.append(pipe)
        return pipe

    eng = engine_mod.Engine(factory)
    eng.start()
    return eng, pipes


@pytest.fixture
def meeting(tmp_path: Path) -> dict:
    file = tmp_path / "meeting.flac"
    sf.write(str(file), speech(40.0), RATE, format="FLAC")
    return {"system": file}


def test_dictation_runs_between_transcription_chunks(calls, meeting):
    eng = None
    submitted = threading.Event()

    def on_generate(kind):
        # On the second meeting chunk the user dictates; it must run next,
        # before the remaining chunks of the meeting.
        if kind == "chunk" and sum(1 for c in calls if c[0] == "chunk") == 2 and not submitted.is_set():
            submitted.set()
            eng.dictate(speech(1.5, pause_every=99), "M-int8", "GPU", "pl")

    eng, pipes = make_engine(calls, on_generate)
    job = eng.transcribe(meeting, "M-int8", "GPU", "auto")
    segments, device = job.wait(30)
    eng.stop()

    kinds = [c[0] for c in calls if c[0] in ("chunk", "dictate")]
    assert kinds.count("chunk") >= 4
    assert kinds.index("dictate") == 2, kinds
    assert device == "GPU" and segments
    # Shared model: one pipeline object, loaded once.
    assert len(pipes) == 1 and pipes[0].loads == 1


def test_separate_dictation_model_and_preload(calls, meeting):
    eng, pipes = make_engine(calls)
    eng.configure(("M-fp16", "GPU"), preload=True).wait(5)
    assert ("load", "M-fp16") in calls
    eng.transcribe(meeting, "M-int8", "GPU", "auto").wait(30)
    text, seconds, device = eng.dictate(speech(1.5, 99), "M-fp16", "GPU", "pl").wait(5)
    eng.stop()
    assert text == "dictate text" and device == "GPU"
    # The transcription model is released after the meeting; the kept
    # dictation model is loaded once and stays until shutdown.
    assert calls.index(("release", "M-int8")) < len(calls) - 1
    assert [c for c in calls if c == ("load", "M-fp16")] == [("load", "M-fp16")]


def test_shared_model_is_kept_after_transcription(calls, meeting):
    eng, pipes = make_engine(calls)
    eng.configure(("M-int8", "GPU"), preload=True).wait(5)
    eng.transcribe(meeting, "M-int8", "GPU", "auto").wait(30)
    assert ("release", "M-int8") not in calls
    eng.stop()
    assert calls[-1] == ("release", "M-int8")


def test_exclusive_releases_whisper_and_reloads_dictation_model(calls):
    eng, pipes = make_engine(calls)
    eng.configure(("M-fp16", "GPU"), preload=True).wait(5)
    seen = []

    def summary(job):
        seen.append([k for k in eng.loaded()])
        return "summary"

    assert eng.exclusive(summary, "summary").wait(5) == "summary"
    eng.submit(lambda job: None, engine_mod.BACKGROUND).wait(5)  # after the reload job
    eng.stop()
    assert seen == [[]]
    assert [c for c in calls if c[0] == "load"] == [("load", "M-fp16"), ("load", "M-fp16")]


def test_priorities_order_waiting_jobs(calls):
    eng = engine_mod.Engine(lambda m, d: FakePipe(calls, m, d))
    order = []
    gate = threading.Event()
    eng.submit(lambda job: gate.wait(5), engine_mod.NORMAL, "blocker")
    eng.submit(lambda job: order.append("background"), engine_mod.BACKGROUND)
    eng.submit(lambda job: order.append("normal"), engine_mod.NORMAL)
    last = eng.submit(lambda job: order.append("dictation"), engine_mod.DICTATION)
    eng.start()
    gate.set()
    last.wait(5)
    eng.submit(lambda job: None, engine_mod.BACKGROUND).wait(5)
    eng.stop()
    assert order == ["dictation", "normal", "background"]


def test_errors_reach_the_caller_and_the_engine_keeps_working(calls):
    eng, _ = make_engine(calls)
    errors = []

    def broken(job):
        raise RuntimeError("model not downloaded")

    job = eng.submit(broken, on_error=errors.append)
    with pytest.raises(RuntimeError):
        job.wait(5)
    assert eng.submit(lambda job: 42).wait(5) == 42
    eng.stop()
    assert len(errors) == 1


def test_cancel_stops_a_transcription_between_chunks(calls, meeting):
    eng = None
    job_box = {}

    def on_generate(kind):
        if sum(1 for c in calls if c[0] == "chunk") == 1:
            job_box["job"].cancel.set()

    eng, _ = make_engine(calls, on_generate)
    job_box["job"] = eng.transcribe(meeting, "M", "CPU", "auto")
    job_box["job"].wait(30)
    eng.stop()
    assert sum(1 for c in calls if c[0] == "chunk") == 1
