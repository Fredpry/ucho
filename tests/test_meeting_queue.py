# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Transcription queue: one session at a time, FIFO, no duplicates, survives failures.

The engine is a fake that records the jobs it gets; the test finishes them by
hand. `app.post` collects callbacks, and `pump()` runs them like the UI thread.
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucho import meeting as meeting_mod
from ucho.merge import Segment


class FakeEngine:
    def __init__(self):
        self.jobs: list[SimpleNamespace] = []

    def _job(self, kind, **kwargs):
        job = SimpleNamespace(kind=kind, cancel=threading.Event(), **kwargs)
        self.jobs.append(job)
        return job

    def transcribe(self, files, model, device, language, progress=None, status=None,
                   on_done=None, on_error=None, report=None):
        return self._job("transcribe", files=files, on_done=on_done, on_error=on_error, report=report)

    def exclusive(self, fn, name, on_done=None, on_error=None, reload=True):
        return self._job(name, on_done=on_done, on_error=on_error)


class FakeSettings(dict):
    def load(self):
        pass


class FakeApp:
    def __init__(self, summary: bool):
        self.engine = FakeEngine()
        self.settings = FakeSettings(model="M", device="CPU", language="auto",
                                     summary_enabled=summary, summary_model="LLM",
                                     rename_folder=False, auto_title=False)
        self.posted: list = []

    def post(self, fn):
        self.posted.append(fn)

    def pump(self):
        while self.posted:
            self.posted.pop(0)()


@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.setattr(meeting_mod.transcribe, "model_downloaded", lambda _m: True)
    saved: list = []
    monkeypatch.setattr(meeting_mod, "save_results", lambda folder, segments, meta: saved.append(meta))

    def factory(summary: bool = False):
        app = FakeApp(summary)
        app.saved = saved
        controller = meeting_mod.MeetingController(app)
        events: list = []
        controller.listen(lambda event, data: events.append((event, data)))
        return app, controller, events

    return factory


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    return folder


def _start(controller, folder):
    return controller.transcribe(folder, {"microphone": folder / "microphone.flac"}, {"name": folder.name})


def _transcribed(job):
    job.on_done(([Segment(0.0, 1.0, "text", "me")], "CPU"))


def _files(app):
    return [job.files["microphone"].parent.name for job in app.engine.jobs if job.kind == "transcribe"]


def test_second_session_is_queued_not_rejected(make, tmp_path):
    app, m, events = make()
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    assert _start(m, a) and _start(m, b)
    app.pump()
    assert _files(app) == ["a"]
    assert m.queued == 1 and m.queue_position(b) == 1 and m.is_active(a)
    assert not [e for e in events if e[0] == "error"]


def test_queue_runs_in_order(make, tmp_path):
    app, m, _events = make()
    folders = [_folder(tmp_path, name) for name in "abc"]
    for folder in folders:
        _start(m, folder)
    assert [m.queue_position(f) for f in folders] == [0, 1, 2]
    for expected in ("a", "b", "c"):
        app.pump()
        assert _files(app)[-1] == expected
        _transcribed(app.engine.jobs[-1])
    app.pump()
    assert _files(app) == ["a", "b", "c"]
    assert not m.busy and m.queued == 0


def test_no_duplicate_entries(make, tmp_path):
    app, m, _events = make()
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    _start(m, a)
    _start(m, b)
    assert _start(m, b) and _start(m, a)  # queued / running already
    assert m.queued == 1
    _transcribed(app.engine.jobs[-1])
    app.pump()
    _transcribed(app.engine.jobs[-1])
    app.pump()
    assert _files(app) == ["a", "b"]


def test_queue_continues_after_a_failure(make, tmp_path):
    app, m, events = make()
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    _start(m, a)
    _start(m, b)
    app.engine.jobs[-1].on_error(RuntimeError("decoder broke"))
    app.pump()
    assert _files(app) == ["a", "b"] and m.is_active(b)
    assert any(e[0] == "error" for e in events)


def test_next_waits_for_the_summary(make, tmp_path):
    app, m, _events = make(summary=True)
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    _start(m, a)
    _start(m, b)
    _transcribed(app.engine.jobs[-1])
    app.pump()
    assert [j.kind for j in app.engine.jobs] == ["transcribe", "summary"]
    assert m.summarizing and m.is_active(a) and m.queue_position(b) == 1
    app.engine.jobs[-1].on_error(RuntimeError("LLM failed"))
    app.pump()
    assert [j.kind for j in app.engine.jobs] == ["transcribe", "summary", "transcribe"]
    assert m.is_active(b)


def test_transcribe_again_goes_through_the_queue(make, tmp_path, monkeypatch):
    app, m, _events = make()
    monkeypatch.setattr(meeting_mod.session, "audio_tracks",
                        lambda folder: {"microphone": folder / "microphone.flac"})
    monkeypatch.setattr(meeting_mod.session, "read_meta", lambda _folder: {"segments": []})
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    _start(m, a)
    assert m.transcribe_again(b)
    assert m.queue_position(b) == 1 and _files(app) == ["a"]


def test_deleted_queued_session_is_skipped(make, tmp_path):
    app, m, _events = make()
    a, b, c = (_folder(tmp_path, name) for name in "abc")
    for folder in (a, b, c):
        _start(m, folder)
    b.rmdir()
    _transcribed(app.engine.jobs[-1])
    app.pump()
    assert _files(app) == ["a", "c"]


def test_quit_logs_and_drops_the_queue(make, tmp_path, caplog):
    app, m, _events = make()
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    _start(m, a)
    _start(m, b)
    with caplog.at_level("INFO", logger="ucho.meeting"):
        m.quit()
    assert m.queued == 0 and app.engine.jobs[0].cancel.is_set()
    assert "1 queued transcription" in caplog.text


def test_in_use_covers_recording_running_and_queued(make, tmp_path):
    """Deletion and audio retention must leave these sessions alone."""
    app, m, _events = make()
    a, b, c, idle = (_folder(tmp_path, n) for n in ("a", "b", "c", "idle"))
    assert _start(m, a) and _start(m, b)
    app.pump()
    m.recording, m.recording_dir = True, c
    assert m.in_use(a) and m.in_use(b) and m.in_use(c)
    assert not m.in_use(idle) and not m.in_use(None)
    assert m.dequeue(b) and not m.in_use(b)
    m.recording, m.recording_dir = False, None
    assert not m.in_use(c)


def test_queue_lookups_compare_resolved_paths(make, tmp_path):
    app, m, _events = make()
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    _start(m, a)
    _start(m, b)
    other_spelling = tmp_path / "x" / ".." / "b"
    assert m.queue_position(other_spelling) == 1
    assert _start(m, other_spelling) and m.queued == 1  # not queued twice
    assert m.is_active(tmp_path / "." / "a")
    assert m.dequeue(other_spelling) and m.queued == 0


def _report_run(make, tmp_path, **report):
    app, m, _events = make()
    a = _folder(tmp_path, "a")
    _start(m, a)
    job = app.engine.jobs[-1]
    job.report.update(report)
    _transcribed(job)
    return app.saved[-1]


def test_complete_run_is_marked_complete(make, tmp_path):
    meta = _report_run(make, tmp_path)
    assert meta["transcript_complete"] is True
    assert "transcription_cancelled" not in meta and "transcription_errors" not in meta


def test_cancelled_run_is_marked(make, tmp_path):
    meta = _report_run(make, tmp_path, cancelled=True)
    assert meta["transcript_complete"] is False and meta["transcription_cancelled"] is True


def test_run_with_failed_chunks_is_not_complete(make, tmp_path):
    meta = _report_run(make, tmp_path, errors=2)
    assert meta["transcript_complete"] is False and meta["transcription_errors"] == 2


def test_transcribe_again_replaces_old_completion_keys(make, tmp_path):
    app, m, _events = make()
    a = _folder(tmp_path, "a")
    m.transcribe(a, {"microphone": a / "microphone.flac"},
                 {"name": "a", "transcript_complete": False, "transcription_cancelled": True})
    _transcribed(app.engine.jobs[-1])
    assert app.saved[-1]["transcript_complete"] is True
    assert "transcription_cancelled" not in app.saved[-1]


def test_error_before_the_summary_job_does_not_block_the_queue(make, tmp_path, monkeypatch, caplog):
    app, m, events = make(summary=True)
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    _start(m, a)
    _start(m, b)

    def broken(*_args):
        raise RuntimeError("summary module broken")

    monkeypatch.setattr(m, "summarize", broken)
    with caplog.at_level("ERROR", logger="ucho.meeting"):
        _transcribed(app.engine.jobs[-1])
    assert not m.summarizing and not m.transcribing
    assert "Finishing the transcription failed" in caplog.text
    app.pump()
    assert _files(app) == ["a", "b"] and m.is_active(b)
