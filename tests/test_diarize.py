# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Speaker detection (diarization) pass: overlap assignment, step yielding, session hook."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ucho import config, diarize, i18n, transcribe
from ucho.diarize import Diarizer, Turn, assign_speakers, iter_diarize_track, track_pass
from ucho.merge import Segment

RATE = diarize.SAMPLE_RATE


@pytest.fixture(autouse=True)
def _english_ui():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def _speech(seconds: float = 5.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE), dtype=np.float32) / RATE
    return (0.25 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_assign_speakers_numbers_by_first_appearance_without_mutating_inputs():
    original = [
        Segment(1.0, 3.0, "Hello everyone.", "meeting"),
        Segment(4.0, 6.0, "Good morning.", "meeting"),
        Segment(7.0, 9.0, "Let us begin.", "meeting"),
    ]
    turns = [
        Turn(0.5, 3.5, speaker=5),
        Turn(3.8, 6.2, speaker=2),
        Turn(6.8, 9.5, speaker=5),
    ]
    out = assign_speakers(original, turns, "meeting")
    assert [s.speaker for s in out] == ["meeting:1", "meeting:2", "meeting:1"]
    assert [(s.start, s.end, s.text) for s in out] == [(s.start, s.end, s.text) for s in original]
    assert [s.speaker for s in original] == ["meeting", "meeting", "meeting"]
    assert all(a is not b for a, b in zip(out, original, strict=True))


def test_assign_speakers_overlap_ties_and_unassigned():
    segments = [
        Segment(0.0, 4.0, "Mostly second speaker.", "meeting"),
        Segment(5.0, 7.0, "Exact tie goes to earlier turn.", "meeting"),
        Segment(10.0, 12.0, "No turn here.", "meeting"),
        Segment(15.0, 15.0, "Zero length inside turn.", "meeting"),
    ]
    turns = [
        Turn(0.0, 1.0, speaker=0),
        Turn(1.0, 4.0, speaker=1),
        Turn(5.0, 6.0, speaker=0),
        Turn(6.0, 7.0, speaker=1),
        Turn(14.0, 16.0, speaker=1),
    ]
    out = assign_speakers(segments, turns, "meeting")
    # Speaker 1 appears at segment 0.0..4.0 -> meeting:1; speaker 0 at 5.0..7.0 -> meeting:2.
    assert [s.speaker for s in out] == ["meeting:1", "meeting:2", "meeting", "meeting:1"]


def test_assign_speakers_keeps_input_when_fewer_than_two_speakers_or_no_turns():
    segments = [
        Segment(0.0, 2.0, "First.", "meeting"),
        Segment(3.0, 5.0, "Second.", "meeting"),
    ]
    assert [s.speaker for s in assign_speakers(segments, [], "meeting")] == ["meeting", "meeting"]
    single = [Turn(0.0, 6.0, speaker=0)]
    assert [s.speaker for s in assign_speakers(segments, single, "meeting")] == ["meeting", "meeting"]
    assert assign_speakers([], single, "meeting") == []


def test_assign_speakers_on_microphone_track():
    segments = [
        Segment(0.0, 2.0, "First voice.", "me"),
        Segment(3.0, 5.0, "Second voice.", "me"),
    ]
    turns = [Turn(0.0, 2.0, speaker=1), Turn(3.0, 5.0, speaker=0)]
    assert [s.speaker for s in assign_speakers(segments, turns, "me")] == ["me:1", "me:2"]


def test_diarizer_sorts_drops_invalid_turns_and_builds_per_call():
    calls: list[tuple[float, int]] = []
    raw_items = [
        SimpleNamespace(start=4.0, end=6.0, speaker=1),
        SimpleNamespace(start=2.0, end=2.0, speaker=0),
        SimpleNamespace(start=3.0, end=1.0, speaker=0),
        SimpleNamespace(start=0.5, end=2.5, speaker=0),
    ]

    class WithSortMethod:
        def process(self, samples, callback=None):
            assert samples.dtype == np.float32 and samples.ndim == 1
            if callback is not None:
                assert callback(1, 2) == 0
            return SimpleNamespace(sort_by_start_time=lambda: list(raw_items))

    class PlainList:
        def process(self, samples, callback=None):
            return list(raw_items)

    def factory(threshold: float, threads: int):
        calls.append((threshold, threads))
        return WithSortMethod() if len(calls) == 1 else PlainList()

    d = Diarizer(threshold=0.55, threads=2, factory=factory)
    t1 = d.diarize(_speech(3.0))
    t2 = d.diarize(_speech(3.0))
    assert len(calls) == 2
    assert calls[0] == (0.55, 2)
    assert t1 == [Turn(0.5, 2.5, 0), Turn(4.0, 6.0, 1)]
    assert t2 == t1


def test_diarizer_skips_short_audio_and_honors_cancel():
    called = []
    cancel = threading.Event()

    class CancellingSD:
        def __init__(self):
            self.cb_return = None

        def process(self, samples, callback=None):
            cancel.set()
            self.cb_return = callback(1, 4)
            return [SimpleNamespace(start=0.0, end=2.0, speaker=0)]

    sd = CancellingSD()
    d = Diarizer(factory=lambda th, n: called.append((th, n)) or sd)
    assert d.diarize(_speech(1.0)) == []
    assert not called

    turns = d.diarize(_speech(3.0), cancel=cancel)
    assert turns == []
    assert sd.cb_return == 1
    assert len(called) == 1


def test_iter_diarize_track_yields_while_helper_thread_works(tmp_path: Path):
    flac = tmp_path / "meeting.flac"
    sf.write(str(flac), _speech(5.0), RATE)
    segments = [
        Segment(0.2, 2.0, "One.", "meeting"),
        Segment(2.5, 4.5, "Two.", "meeting"),
    ]
    unblock = threading.Event()

    class BlockingDiarizer:
        def diarize(self, audio, cancel=None):
            unblock.wait(timeout=5.0)
            return [Turn(0.0, 2.2, 0), Turn(2.3, 4.8, 1)]

    gen = iter_diarize_track(flac, segments, "meeting", BlockingDiarizer())
    t0 = time.monotonic()
    assert next(gen) is None
    assert time.monotonic() - t0 < 1.0
    unblock.set()

    result = None
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            result = stop.value
            break
    assert [s.speaker for s in result] == ["meeting:1", "meeting:2"]


def test_iter_diarize_track_handles_empty_errors_and_cancel(tmp_path: Path):
    missing = tmp_path / "does_not_exist.flac"
    # Empty segments: never touches the file and never yields.
    gen = iter_diarize_track(missing, [], "meeting", Diarizer(factory=lambda *_: None))
    with pytest.raises(StopIteration) as stop:
        next(gen)
    assert stop.value.value == []

    flac = tmp_path / "meeting.flac"
    sf.write(str(flac), _speech(5.0), RATE)
    segs = [Segment(0.0, 2.0, "Hello.", "meeting"), Segment(2.5, 4.5, "World.", "meeting")]

    class BrokenDiarizer:
        def diarize(self, audio, cancel=None):
            raise RuntimeError("onnx failed")

    out = transcribe.drain(iter_diarize_track(flac, segs, "meeting", BrokenDiarizer()))
    assert [s.speaker for s in out] == ["meeting", "meeting"]

    cancel = threading.Event()
    cancel.set()
    out_cancel = transcribe.drain(
        iter_diarize_track(flac, segs, "meeting", Diarizer(factory=lambda *_: None), cancel=cancel)
    )
    assert [s.speaker for s in out_cancel] == ["meeting", "meeting"]


class _FakePipe:
    def __init__(self, chunks_per_call: list[list[SimpleNamespace]]):
        self.requested_device = "CPU"
        self.device = "CPU"
        self._calls = list(chunks_per_call)

    def load(self):
        pass

    def generate(self, audio, language, prompt=None):
        chunks = self._calls.pop(0) if self._calls else []
        return SimpleNamespace(chunks=chunks)


def test_iter_transcribe_session_runs_speakers_hook_before_merge(tmp_path: Path):
    mic = tmp_path / "microphone.flac"
    sys_file = tmp_path / "meeting.flac"
    # Silence on microphone, speech on system track.
    sf.write(str(mic), np.zeros(int(3.0 * RATE), dtype=np.float32), RATE)
    sf.write(str(sys_file), _speech(5.0), RATE)

    def make_pipe():
        return _FakePipe([[
            SimpleNamespace(start_ts=0.2, end_ts=1.8, text="First speaker here."),
            SimpleNamespace(start_ts=2.5, end_ts=4.2, text="Second speaker replies."),
        ]])

    seen_calls: list[tuple[str, Path, str, int]] = []
    statuses: list[str] = []

    def fake_speakers(name: str, file: Path, segments: list[Segment], speaker_code: str, cancel):
        seen_calls.append((name, file, speaker_code, len(segments)))
        yield
        return [
            Segment(segments[0].start, segments[0].end, segments[0].text, f"{speaker_code}:1"),
            Segment(segments[1].start, segments[1].end, segments[1].text, f"{speaker_code}:2"),
        ]

    files = {"microphone": mic, "system": sys_file}
    merged = transcribe.drain(
        transcribe.iter_transcribe_session(
            files, make_pipe(), "pl", status=statuses.append, speakers=fake_speakers,
        )
    )
    assert seen_calls == [("system", sys_file, "meeting", 2)]
    assert "Detecting speakers..." in statuses
    assert [s.speaker for s in merged] == ["meeting:1", "meeting:2"]

    # Without the hook the two close utterances of "meeting" are joined by merge_tracks.
    plain = transcribe.drain(transcribe.iter_transcribe_session(files, make_pipe(), "pl"))
    assert [s.speaker for s in plain] == ["meeting"]

    # With cancel set before the speaker pass, the hook is not called.
    cancel = threading.Event()
    hook_called = []

    def cancel_on_status(msg: str):
        if msg.startswith("Transcribing"):
            cancel.set()

    transcribe.drain(
        transcribe.iter_transcribe_session(
            files, make_pipe(), "pl", cancel=cancel, status=cancel_on_status,
            speakers=lambda *a: hook_called.append(a),
        )
    )
    assert not hook_called


def test_track_pass_connects_diarizer_to_session(tmp_path: Path):
    sys_file = tmp_path / "meeting.flac"
    sf.write(str(sys_file), _speech(5.0), RATE)
    d = Diarizer(
        factory=lambda th, n: SimpleNamespace(
            process=lambda audio, callback=None: [
                SimpleNamespace(start=0.0, end=2.0, speaker=0),
                SimpleNamespace(start=2.0, end=4.8, speaker=1),
            ]
        )
    )
    segs = [
        Segment(0.2, 1.8, "Hello.", "meeting"),
        Segment(2.2, 4.0, "Hi there.", "meeting"),
    ]
    out = transcribe.drain(track_pass(d)("system", sys_file, segs, "meeting", None))
    assert [s.speaker for s in out] == ["meeting:1", "meeting:2"]


def test_available_and_models_downloaded_do_not_raise(monkeypatch, tmp_path: Path):
    assert isinstance(diarize.available(), bool)
    monkeypatch.setattr(transcribe, "model_downloaded", lambda _repo: False)
    assert diarize.models_downloaded() is False
    assert diarize.ready() is False

    for repo in config.SPEAKER_MODELS:
        assert len(config.REVISIONS[repo]) == 40
        assert config.SPEAKER_MODELS[repo]["file"] in config.DOWNLOAD_PATTERNS[repo]
