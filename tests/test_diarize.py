# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Speaker detection (diarization) pass: overlap assignment, step yielding, session hook."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ucho import config, diarize, i18n, transcribe
from ucho.diarize import (
    Diarizer,
    Turn,
    assign_speakers,
    iter_diarize_track,
    run_worker,
    track_pass,
    worker_main,
    worker_python,
)
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
        def diarize_file(self, file: Path, cancel=None):
            assert file == flac
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
        def diarize_file(self, file: Path, cancel=None):
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


class _FakeProc:
    def __init__(
        self,
        cmd: list[str],
        kwargs: dict,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        timeouts: int = 2,
        on_timeout=None,
    ):
        self.cmd = list(cmd)
        self.kwargs = dict(kwargs)
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._remaining_timeouts = timeouts
        self._on_timeout = on_timeout
        self.communicate_calls: list[float | None] = []
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.communicate_calls.append(timeout)
        if not self.killed and self._remaining_timeouts > 0:
            self._remaining_timeouts -= 1
            if self._on_timeout is not None:
                self._on_timeout(len(self.communicate_calls))
            raise subprocess.TimeoutExpired(self.cmd, timeout or 0.0)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


def test_run_worker_parses_turns_retries_and_handles_errors_and_cancel(tmp_path: Path):
    track = tmp_path / "meeting.flac"
    created: list[_FakeProc] = []

    def make_popen(stdout: bytes, stderr: bytes = b"", returncode: int = 0, timeouts: int = 2, on_timeout=None):
        def popen(cmd, **kwargs):
            proc = _FakeProc(
                cmd, kwargs, stdout=stdout, stderr=stderr,
                returncode=returncode, timeouts=timeouts, on_timeout=on_timeout,
            )
            created.append(proc)
            return proc
        return popen

    payload = {"turns": [[4.0, 6.0, 1], [2.0, 2.0, 0], [3.0, 1.0, 0], [0.5, 2.5, 0]]}
    stdout_bytes = (f"noise line\n{json.dumps(payload)}\n\n").encode("utf-8")
    turns = run_worker(track, 0.55, 3, popen=make_popen(stdout_bytes))
    assert turns == [Turn(0.5, 2.5, 0), Turn(4.0, 6.0, 1)]
    proc = created[-1]
    assert "-m" in proc.cmd and "ucho.diarize" in proc.cmd and "--worker" in proc.cmd
    assert repr(0.55) in proc.cmd and "3" in proc.cmd and str(track) in proc.cmd
    assert proc.kwargs["cwd"] == str(config.PROJECT_DIR)
    assert proc.kwargs["stdin"] == subprocess.DEVNULL
    assert proc.kwargs["stdout"] == subprocess.PIPE
    assert proc.kwargs["stderr"] == subprocess.PIPE
    assert len(proc.communicate_calls) == 3

    # Non-zero exit -> RuntimeError with the last non-empty stderr line.
    with pytest.raises(RuntimeError, match="RuntimeError: boom"):
        run_worker(
            track, 0.5, 2,
            popen=make_popen(b"", stderr=b"warning\nRuntimeError: boom\n\n", returncode=1, timeouts=0),
        )

    # Bad JSON or missing turns list -> RuntimeError.
    with pytest.raises(RuntimeError, match="returned no result"):
        run_worker(track, 0.5, 2, popen=make_popen(b"not-json\n", timeouts=0))
    with pytest.raises(RuntimeError, match="returned no result"):
        run_worker(track, 0.5, 2, popen=make_popen(b'{"other": 1}\n', timeouts=0))

    # Cancel set while running -> kill() called and [] returned.
    cancel = threading.Event()
    out_cancel = run_worker(
        track, 0.5, 2, cancel=cancel,
        popen=make_popen(stdout_bytes, timeouts=2, on_timeout=lambda _n: cancel.set()),
    )
    assert out_cancel == []
    assert created[-1].killed is True

    # Large stdout (20 000 turns) works without truncating.
    big_turns = [[float(i), float(i) + 0.5, i % 4] for i in range(20_000)]
    big_stdout = (json.dumps({"turns": big_turns}) + "\n").encode("utf-8")
    parsed_big = run_worker(track, 0.5, 2, popen=make_popen(big_stdout, timeouts=1))
    assert len(parsed_big) == 20_000
    assert parsed_big[0] == Turn(0.0, 0.5, 0)
    assert parsed_big[-1] == Turn(19999.0, 19999.5, 3)


def test_worker_python_prefers_console_interpreter_next_to_pythonw(monkeypatch, tmp_path: Path):
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(pythonw))
    # Without python.exe next to it -> unchanged.
    assert worker_python() == str(pythonw)

    # With python.exe next to pythonw.exe -> uses python.exe.
    python_exe = tmp_path / "python.exe"
    python_exe.write_bytes(b"")
    assert worker_python() == str(python_exe)

    # Already python.exe -> unchanged.
    monkeypatch.setattr(sys, "executable", str(python_exe))
    assert worker_python() == str(python_exe)


def test_worker_main_cli_and_error_handling(monkeypatch, tmp_path: Path, capsys):
    flac = tmp_path / "track.flac"
    sf.write(str(flac), _speech(3.0), RATE)

    # Without --worker -> exit code 2 and usage on stderr.
    assert worker_main([str(flac)]) == 2
    err = capsys.readouterr().err
    assert "Usage:" in err

    seen: list[tuple[float, int]] = []

    def fake_factory(threshold: float, threads: int):
        seen.append((threshold, threads))
        return SimpleNamespace(
            process=lambda audio, callback=None: [
                SimpleNamespace(start=0.2, end=1.4, speaker=0),
                SimpleNamespace(start=1.6, end=2.8, speaker=1),
            ]
        )

    monkeypatch.setattr(diarize, "_sherpa_diarization", fake_factory)
    rc = worker_main(["--worker", "--threshold", "0.6", "--threads", "2", str(flac)])
    captured = capsys.readouterr()
    assert rc == 0
    assert seen == [(0.6, 2)]
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"turns": [[0.2, 1.4, 0], [1.6, 2.8, 1]]}

    # Factory raising -> returns 1 and stderr contains the error type.
    def raising_factory(threshold: float, threads: int):
        raise ValueError("broken model")

    monkeypatch.setattr(diarize, "_sherpa_diarization", raising_factory)
    rc_err = worker_main(["--worker", str(flac)])
    captured_err = capsys.readouterr()
    assert rc_err == 1
    assert captured_err.out == ""
    assert "ValueError" in captured_err.err


def test_diarizer_diarize_file_in_process_vs_worker(monkeypatch, tmp_path: Path):
    flac = tmp_path / "meeting.flac"
    sf.write(str(flac), _speech(3.0), RATE)

    in_proc = Diarizer(
        threshold=0.45,
        threads=2,
        factory=lambda th, n: SimpleNamespace(
            process=lambda audio, callback=None: [SimpleNamespace(start=0.5, end=2.0, speaker=0)]
        ),
    )
    assert in_proc.in_process is True
    assert in_proc.diarize_file(flac) == [Turn(0.5, 2.0, 0)]

    worker_calls: list[tuple[Path, float, int, threading.Event | None]] = []

    def fake_run_worker(file: Path, threshold: float, threads: int, cancel=None):
        worker_calls.append((file, threshold, threads, cancel))
        return [Turn(1.0, 2.5, 1)]

    monkeypatch.setattr(diarize, "run_worker", fake_run_worker)
    out_of_proc = Diarizer(threshold=0.65, threads=4)
    assert out_of_proc.in_process is False
    cancel = threading.Event()
    assert out_of_proc.diarize_file(flac, cancel=cancel) == [Turn(1.0, 2.5, 1)]
    assert worker_calls == [(flac, 0.65, 4, cancel)]
