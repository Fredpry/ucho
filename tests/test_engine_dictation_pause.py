# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tests for engine dictation pause, queue priority, and controller waiting statuses."""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ucho import engine as engine_mod
from ucho import meeting as meeting_mod
from ucho.i18n import t

RATE = 16_000


def _speech(seconds: float, pause_every: float = 4.0) -> np.ndarray:
    t_arr = np.arange(int(seconds * RATE)) / RATE
    audio = 0.3 * np.sin(2 * np.pi * 220 * t_arr).astype(np.float32)
    period = int(pause_every * RATE)
    for start in range(period, audio.size, period):
        audio[start:start + int(0.6 * RATE)] = 0.0
    return audio


class _FakePipe:
    def __init__(self, calls: list, model: str, device: str, on_generate=None):
        self.calls = calls
        self.repo_id = model
        self.requested_device = device
        self.device = device
        self._pipe = None
        self.on_generate = on_generate

    def load(self):
        if self._pipe is None:
            self._pipe = object()
            self.calls.append(("load", self.repo_id))

    def release(self):
        if self._pipe is not None:
            self.calls.append(("release", self.repo_id))
        self._pipe = None

    def generate(self, audio, language, prompt=None):
        seconds = audio.size / RATE
        kind = "dictate" if seconds < 3 else "chunk"
        self.calls.append((kind, self.repo_id))
        if self.on_generate:
            self.on_generate(kind)
        chunk = SimpleNamespace(start_ts=0.0, end_ts=min(seconds, 2.0), text=f"{kind} text")
        return SimpleNamespace(chunks=[chunk], texts=[f"{kind} text"], languages=["<|pl|>"])


def _make_engine(calls: list | None = None, on_generate=None) -> engine_mod.Engine:
    call_log = calls if calls is not None else []
    eng = engine_mod.Engine(lambda m, d: _FakePipe(call_log, m, d, on_generate))
    eng.start()
    return eng


def test_step_job_finishes_current_step_then_pauses_until_resume():
    eng = _make_engine()
    steps: list[int] = []
    ran: list[str] = []
    step1_started = threading.Event()
    step1_release = threading.Event()
    step2_started = threading.Event()

    def step_work(_job):
        steps.append(1)
        step1_started.set()
        step1_release.wait(2.0)
        yield
        steps.append(2)
        step2_started.set()
        yield
        steps.append(3)
        return "done"

    try:
        step_job = eng.submit(step_work, engine_mod.NORMAL, "transcribe")
        assert step1_started.wait(2.0)

        eng.pause_for_dictation()
        assert eng.paused is True
        assert eng.dictation_active is True

        d1 = eng.submit(lambda _j: ran.append("d1"), engine_mod.DICTATION, "dictate-piece")
        assert eng.waiting_behind() == "transcribe"

        step1_release.set()
        d1.wait(2.0)
        assert not step2_started.wait(0.08)
        assert steps == [1]

        normal = eng.submit(lambda _j: ran.append("normal"), engine_mod.NORMAL, "normal")
        bg = eng.submit(lambda _j: ran.append("bg"), engine_mod.BACKGROUND, "bg")
        d2 = eng.submit(lambda _j: ran.append("d2"), engine_mod.DICTATION, "dictate")
        d2.wait(2.0)

        assert not step2_started.wait(0.08)
        assert steps == [1]
        assert ran == ["d1", "d2"]
        assert not normal.done.is_set()
        assert not bg.done.is_set()

        eng.resume_after_dictation()
        assert step_job.wait(2.0) == "done"
        normal.wait(2.0)
        bg.wait(2.0)
        assert steps == [1, 2, 3]
        assert ran == ["d1", "d2", "normal", "bg"]
        assert eng.paused is False
        assert eng.dictation_active is False
        assert eng.waiting_behind() is None
    finally:
        step1_release.set()
        eng.stop()


def test_pause_expiry_lets_jobs_run_without_resume(caplog):
    eng = _make_engine()
    started = threading.Event()
    release = threading.Event()
    try:
        with caplog.at_level(logging.WARNING, logger="ucho.engine"):
            eng.submit(lambda _j: (started.set(), release.wait(2.0)), engine_mod.NORMAL, "blocker")
            assert started.wait(2.0)
            eng.pause_for_dictation(max_s=0.1)
            normal = eng.submit(lambda _j: "ok", engine_mod.NORMAL, "after")
            release.set()
            assert normal.wait(2.0) == "ok"
            assert eng.paused is False
            assert eng.dictation_active is False
        expired = [r for r in caplog.records if "Dictation pause expired" in r.message]
        assert len(expired) == 1
    finally:
        release.set()
        eng.stop()


def test_resume_without_pause_and_double_pause_resume_logging(caplog):
    eng = _make_engine()
    try:
        with caplog.at_level(logging.INFO, logger="ucho.engine"):
            eng.resume_after_dictation()
            eng.pause_for_dictation()
            eng.pause_for_dictation()
            assert eng.paused is True
            eng.resume_after_dictation()
            eng.resume_after_dictation()
            assert eng.paused is False
        paused_logs = [r for r in caplog.records if "Engine paused for a dictation" in r.message]
        resumed_logs = [r for r in caplog.records if "Engine resumed after a dictation pause" in r.message]
        assert len(paused_logs) == 1
        assert len(resumed_logs) == 1
    finally:
        eng.stop()


def test_paused_dictation_active_and_waiting_behind_states():
    eng = _make_engine()
    try:
        assert eng.paused is False
        assert eng.dictation_active is False
        assert eng.waiting_behind() is None

        # Pause while idle
        eng.pause_for_dictation()
        assert eng.paused is True
        assert eng.dictation_active is True
        assert eng.waiting_behind() is None
        eng.resume_after_dictation()

        # Running DICTATION job: dictation_active is True, waiting_behind is None
        d_started = threading.Event()
        d_release = threading.Event()
        d_job = eng.submit(lambda _j: (d_started.set(), d_release.wait(2.0)),
                           engine_mod.DICTATION, "dictate")
        assert d_started.wait(2.0)
        assert eng.dictation_active is True
        assert eng.waiting_behind() is None
        d2_job = eng.submit(lambda _j: None, engine_mod.DICTATION, "dictate-2")
        assert eng.waiting_behind() is None
        d_release.set()
        d_job.wait(2.0)
        d2_job.wait(2.0)
        assert eng.dictation_active is False

        # Running NORMAL job with and without a queued DICTATION job
        n_started = threading.Event()
        n_release = threading.Event()
        n_job = eng.submit(lambda _j: (n_started.set(), n_release.wait(2.0)),
                           engine_mod.NORMAL, "transcribe")
        assert n_started.wait(2.0)
        assert eng.dictation_active is False
        assert eng.waiting_behind() is None

        waiting_d = eng.submit(lambda _j: None, engine_mod.DICTATION, "dictate")
        assert eng.dictation_active is True
        assert eng.waiting_behind() == "transcribe"

        n_release.set()
        waiting_d.wait(2.0)
        n_job.wait(2.0)
        assert eng.dictation_active is False
        assert eng.waiting_behind() is None
    finally:
        eng.stop()


def test_stop_while_paused_drains_and_cancels_queued_jobs():
    eng = _make_engine()
    ran: list[str] = []
    eng.pause_for_dictation()
    queued = eng.submit(lambda _j: ran.append("ran"), engine_mod.NORMAL, "queued")
    eng.stop(timeout=2.0)

    assert eng._thread is not None and not eng._thread.is_alive()
    assert queued.done.is_set()
    assert isinstance(queued.error, engine_mod.Cancelled)
    assert ran == []


def test_transcribe_identical_with_and_without_pause_and_dictations(tmp_path: Path):
    file = tmp_path / "meeting.flac"
    sf.write(str(file), _speech(30.0), RATE, format="FLAC")
    files = {"system": file}

    calls_ref: list = []
    eng_ref = _make_engine(calls_ref)
    try:
        ref_segments, ref_dev = eng_ref.transcribe(files, "M-int8", "GPU", "auto").wait(15.0)
    finally:
        eng_ref.stop()

    calls_paused: list = []
    triggered = threading.Event()
    eng_box: dict[str, engine_mod.Engine] = {}

    def on_generate(kind: str) -> None:
        if kind == "chunk" and not triggered.is_set():
            triggered.set()
            e = eng_box["eng"]
            e.pause_for_dictation()
            e.dictate(_speech(1.2, pause_every=99), "M-int8", "GPU", "pl")
            e.dictate(_speech(1.2, pause_every=99), "M-int8", "GPU", "pl")
            e.resume_after_dictation()

    eng_paused = _make_engine(calls_paused, on_generate)
    eng_box["eng"] = eng_paused
    try:
        paused_segments, paused_dev = eng_paused.transcribe(files, "M-int8", "GPU", "auto").wait(15.0)
    finally:
        eng_paused.stop()

    assert triggered.is_set()
    assert paused_dev == ref_dev
    assert [(s.start, s.end, s.speaker, s.text) for s in paused_segments] == [
        (s.start, s.end, s.speaker, s.text) for s in ref_segments
    ]
    kinds = [c[0] for c in calls_paused if c[0] in ("chunk", "dictate")]
    assert kinds.count("dictate") == 2
    assert kinds.index("dictate") == 1


# --- DictationController ----------------------------------------------------


class _FakeMic:
    def __init__(self, stream: np.ndarray | None = None, *, fail_start: bool = False):
        self.stream = stream if stream is not None else np.zeros(RATE, dtype=np.float32)
        self.fail_start = fail_start
        self.taken_samples = 0
        self.level = 0.0
        self.name = "fake-mic"
        self.ready = threading.Event()

    def choose(self, *_args):
        pass

    def start(self):
        if self.fail_start:
            raise RuntimeError("no mic")
        self.taken_samples = 0

    def take(self):
        return np.zeros(0, dtype=np.float32)

    def stop(self):
        return self.stream.copy()

    def close(self):
        pass


class _FakeDictApp:
    def __init__(self, engine, live_on: bool = True):
        self.engine = engine
        self.settings = {
            "microphone_name": None,
            "microphone_index": None,
            "dictation_live": live_on,
            "dictation_language": "auto",
            "dictation_dictionary": {},
            "dictation_sounds": False,
            "dictation_mode": "paste",
            "dictation_enter": False,
            "dictation_hotkey": "ctrl+shift+space",
        }
        self.hotkey_errors: dict[str, str] = {}
        self.hotkeys = SimpleNamespace(escape=lambda _on: None)

    def post(self, fn):
        fn()

    def after(self, _ms, _fn):
        pass

    def dictation_hotkey_label(self) -> str:
        return "Ctrl+Shift+Space"


@pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictation uses WinAPI")
def test_dictation_controller_pause_cancel_quit_and_mic_failure(monkeypatch):
    from ucho import dictation

    monkeypatch.setattr(dictation, "dictation_target", lambda: None)
    monkeypatch.setattr(dictation, "insert_text",
                        lambda _t, _m, _e, _w, report: report(dictation.PASTE))
    monkeypatch.setattr(dictation.DictationController, "model_key", lambda self: ("M", "GPU"))

    eng = _make_engine()
    try:
        app = _FakeDictApp(eng, live_on=False)
        ctrl = dictation.DictationController(app)

        # Failed mic start must not leave the engine paused.
        ctrl.microphone = _FakeMic(fail_start=True)
        assert ctrl.start() is False
        assert eng.paused is False

        # Successful start pauses; cancel resumes.
        ctrl.microphone = _FakeMic()
        assert ctrl.start() is True
        assert eng.paused is True
        ctrl.cancel()
        assert eng.paused is False

        # Quit while recording resumes.
        assert ctrl.start() is True
        assert eng.paused is True
        ctrl.quit()
        assert eng.paused is False
    finally:
        eng.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictation uses WinAPI")
@pytest.mark.parametrize("live_on", [True, False])
def test_dictation_controller_finish_queues_final_job_before_resume(monkeypatch, live_on):
    from ucho import dictation

    monkeypatch.setattr(dictation, "dictation_target", lambda: None)
    monkeypatch.setattr(dictation, "insert_text",
                        lambda _t, _m, _e, _w, report: report(dictation.PASTE))
    monkeypatch.setattr(dictation.DictationController, "model_key", lambda self: ("M", "GPU"))

    eng = _make_engine()
    blocker_started = threading.Event()
    blocker_release = threading.Event()
    observed: list[tuple[bool, str | None]] = []
    orig_resume = eng.resume_after_dictation

    def spy_resume() -> None:
        with eng._cv:
            has_dict = any(j.priority == engine_mod.DICTATION and j.name == "dictate"
                           for _, _, j in eng._heap)
        observed.append((has_dict, eng.waiting_behind()))
        orig_resume()

    monkeypatch.setattr(eng, "resume_after_dictation", spy_resume)

    try:
        eng.submit(lambda _j: (blocker_started.set(), blocker_release.wait(2.0)),
                   engine_mod.NORMAL, "transcribe")
        assert blocker_started.wait(2.0)

        app = _FakeDictApp(eng, live_on=live_on)
        ctrl = dictation.DictationController(app)
        ctrl.microphone = _FakeMic(_speech(1.2, pause_every=99))
        assert ctrl.start() is True
        assert eng.paused is True

        ctrl._finish()
        assert observed == [(True, "transcribe")]
        assert eng.paused is False
        assert eng.dictation_active is True

        blocker_release.set()
        eng.submit(lambda _j: None, engine_mod.BACKGROUND).wait(2.0)
        thread = getattr(ctrl, "_insert_thread", None)
        if thread is not None:
            thread.join(2.0)
    finally:
        blocker_release.set()
        eng.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictation uses WinAPI")
def test_dictation_controller_status_waiting_text(monkeypatch):
    from ucho import dictation

    monkeypatch.setattr(dictation.DictationController, "model_key", lambda self: ("M", "GPU"))

    class _FakeStatusEngine:
        def __init__(self):
            self.behind: str | None = None

        def waiting_behind(self):
            if isinstance(self.behind, BaseException):
                raise self.behind
            return self.behind

    fake_eng = _FakeStatusEngine()
    app = _FakeDictApp(fake_eng)
    ctrl = dictation.DictationController(app)

    # Recording: hint shows waiting text when waiting_behind returns a job name.
    ctrl.recording = True
    ctrl.recording_start = 0.0
    fake_eng.behind = "transcribe"
    _headline, style, hint = ctrl.status()
    assert style == "recording" and hint == t("Waiting for the transcription…")

    fake_eng.behind = "live-chunk"
    assert ctrl.status()[2] == t("Waiting for the transcription…")

    fake_eng.behind = None
    assert ctrl.status()[2] == t("{shortcut} finishes, Esc cancels", shortcut="Ctrl+Shift+Space")

    # Queued after stop: headline shows waiting text with style "working".
    ctrl.recording = False
    ctrl.queued = 1

    fake_eng.behind = "transcribe"
    assert ctrl.status()[:2] == (t("Waiting for the transcription…"), "working")

    fake_eng.behind = "summary"
    assert ctrl.status()[:2] == (t("Waiting for the summary…"), "working")

    fake_eng.behind = "benchmark"
    assert ctrl.status()[:2] == (t("Waiting for the model…"), "working")

    fake_eng.behind = None
    assert ctrl.status()[:2] == (t("Recognizing…"), "working")

    # Defensive against errors or missing method.
    fake_eng.behind = RuntimeError("boom")
    assert ctrl.status()[:2] == (t("Recognizing…"), "working")

    app.engine = SimpleNamespace()
    assert ctrl.status()[:2] == (t("Recognizing…"), "working")


# --- MeetingController.shown_status -----------------------------------------


def test_meeting_controller_shown_status():
    class _RaisingEngine:
        @property
        def dictation_active(self):
            raise RuntimeError("broken")

    def make_ctrl(engine) -> meeting_mod.MeetingController:
        app = SimpleNamespace(
            engine=engine,
            settings={"microphone_name": None, "microphone_index": None},
            post=lambda fn: fn(),
        )
        ctrl = meeting_mod.MeetingController(app)
        ctrl.status_text = "Transcribing on GPU..."
        return ctrl

    m_active = make_ctrl(SimpleNamespace(dictation_active=True))
    m_active.transcribing = True
    assert m_active.shown_status == t("Paused for a dictation")

    m_active.transcribing = False
    assert m_active.shown_status == "Transcribing on GPU..."

    m_inactive = make_ctrl(SimpleNamespace(dictation_active=False))
    m_inactive.transcribing = True
    assert m_inactive.shown_status == "Transcribing on GPU..."

    m_no_attr = make_ctrl(SimpleNamespace())
    m_no_attr.transcribing = True
    assert m_no_attr.shown_status == "Transcribing on GPU..."

    m_raises = make_ctrl(_RaisingEngine())
    m_raises.transcribing = True
    assert m_raises.shown_status == "Transcribing on GPU..."
