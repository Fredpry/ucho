# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Automatic meeting titles and user rename in MeetingController."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucho import config
from ucho import meeting as meeting_mod
from ucho.merge import Segment


class FakeEngine:
    def __init__(self):
        self.jobs: list[SimpleNamespace] = []

    def transcribe(self, files, model, device, language, progress=None, status=None,
                   on_done=None, on_error=None, report=None, cache=None):
        job = SimpleNamespace(kind="transcribe", name="transcribe", files=files,
                              on_done=on_done, on_error=on_error, report=report,
                              cancel=threading.Event())
        self.jobs.append(job)
        return job

    def exclusive(self, fn, name, on_done=None, on_error=None, reload=True):
        job = SimpleNamespace(kind=name, name=name, fn=fn, on_done=on_done,
                              on_error=on_error, reload=reload, cancel=threading.Event())
        self.jobs.append(job)
        return job


class FakeSettings(dict):
    def load(self):
        pass


class FakeApp:
    def __init__(self, **overrides):
        self.engine = FakeEngine()
        defaults = dict(
            model="M",
            device="GPU",
            language="auto",
            summary_enabled=False,
            summary_model="LLM",
            rename_folder=False,
            transcribe_after_stop=True,
            auto_title=None,
        )
        defaults.update(overrides)
        self.settings = FakeSettings(defaults)
        self.dictation = SimpleNamespace(recording=False)
        self.posted: list = []

    def post(self, fn):
        self.posted.append(fn)

    def pump(self):
        while self.posted:
            self.posted.pop(0)()


class FakeRecorder:
    def __init__(self, folder: Path):
        self.folder = folder

    def stop(self):
        file = self.folder / "meeting.flac"
        file.write_bytes(b"x")
        return {
            "folder": self.folder,
            "duration_s": 12.0,
            "warnings": [],
            "tracks": {"system": {"file": file, "device": "Speakers", "duration_s": 12.0}},
        }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(meeting_mod, "TITLE_RETRY_S", 3600.0)
    downloaded = {"M": True, "LLM": True, config.TITLE_MODEL: True}
    monkeypatch.setattr(meeting_mod.transcribe, "model_downloaded",
                        lambda model: downloaded.get(model, False))
    monkeypatch.setattr(meeting_mod.transcribe, "genai_version", lambda: "test")

    calls: list[dict] = []
    title_output = {
        "name": "Plan wdrożenia systemu",
        "title_model": config.TITLE_MODEL,
        "title_device": "GPU",
        "title_time_s": 0.8,
    }

    def fake_generate(segments, model, device="GPU", cancel=None, language=None):
        calls.append({
            "segments": segments,
            "model": model,
            "device": device,
            "cancel": cancel,
            "language": language,
        })
        return dict(title_output)

    monkeypatch.setattr(meeting_mod.titles, "generate_title", fake_generate)

    controllers: list[meeting_mod.MeetingController] = []

    def make(**settings_overrides):
        app = FakeApp(**settings_overrides)
        controller = meeting_mod.MeetingController(app)
        controllers.append(controller)
        events: list = []
        controller.listen(lambda event, data: events.append((event, data)))
        return app, controller, events

    yield SimpleNamespace(
        make=make,
        downloaded=downloaded,
        calls=calls,
        title_output=title_output,
    )

    for controller in controllers:
        controller.quit()


def _session(tmp_path: Path, name: str = "2026-11-16_10-00_Meeting") -> Path:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "microphone.flac").write_bytes(b"x")
    return folder


def _start(m: meeting_mod.MeetingController, folder: Path, meta: dict | None = None) -> bool:
    base = {"name": "Meeting", "date": "2026-11-16 10:00", "duration_s": 30.0}
    if meta:
        base.update(meta)
    return m.transcribe(folder, {"microphone": folder / "microphone.flac"}, base)


def _finish_transcription(job, text: str = "Omawiamy wdrożenie systemu.") -> None:
    job.on_done(([Segment(0.0, 2.0, text, "me")], "GPU"))


def _run_title_job(job) -> None:
    value = job.fn(job)
    job.on_done(value)


def test_title_job_runs_after_unnamed_transcription_and_saves_auto_title(env, tmp_path):
    app, m, events = env.make()
    folder = _session(tmp_path)
    assert _start(m, folder)
    _finish_transcription(app.engine.jobs[-1])
    app.pump()

    assert [j.name for j in app.engine.jobs] == ["transcribe", "title"]
    assert m.in_use(folder)

    _run_title_job(app.engine.jobs[-1])
    app.pump()

    assert len(env.calls) == 1
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "Plan wdrożenia systemu"
    assert meta["name_source"] == "auto"
    assert meta["title_model"] == config.TITLE_MODEL
    assert meta["title_device"] == "GPU"
    assert meta["title_time_s"] == 0.8
    assert "# Transcript: Plan wdrożenia systemu" in (folder / "transcript.md").read_text(encoding="utf-8")
    assert ("titled", (folder, folder, "Plan wdrożenia systemu")) in events
    assert not m.in_use(folder)


def test_title_skipped_when_summary_or_user_name_or_disabled_or_missing_model(env, tmp_path):
    # Summary expected -> summary names the session instead.
    app, m, _events = env.make(summary_enabled=True)
    f1 = _session(tmp_path, "2026-11-16_10-00_One")
    _start(m, f1)
    _finish_transcription(app.engine.jobs[-1])
    app.pump()
    assert [j.name for j in app.engine.jobs] == ["transcribe", "summary"]

    # User-typed name -> never replaced.
    app, m, _events = env.make()
    f2 = _session(tmp_path, "2026-11-16_10-05_Custom")
    _start(m, f2, {"name": "Spotkanie zarządu", "name_source": "user"})
    _finish_transcription(app.engine.jobs[-1])
    app.pump()
    assert [j.name for j in app.engine.jobs] == ["transcribe"]

    # auto_title is False.
    app, m, _events = env.make(auto_title=False)
    f3 = _session(tmp_path, "2026-11-16_10-10_Meeting")
    _start(m, f3)
    _finish_transcription(app.engine.jobs[-1])
    app.pump()
    assert [j.name for j in app.engine.jobs] == ["transcribe"]

    # Title model not downloaded.
    env.downloaded[config.TITLE_MODEL] = False
    app, m, _events = env.make(auto_title=True)
    f4 = _session(tmp_path, "2026-11-16_10-15_Meeting")
    _start(m, f4)
    _finish_transcription(app.engine.jobs[-1])
    app.pump()
    assert [j.name for j in app.engine.jobs] == ["transcribe"]


def test_title_waits_until_transcription_queue_is_empty(env, tmp_path):
    app, m, _events = env.make()
    a = _session(tmp_path, "2026-11-16_10-00_Meeting")
    b = _session(tmp_path, "2026-11-16_10-30_Meeting")
    _start(m, a)
    _start(m, b)

    _finish_transcription(app.engine.jobs[-1], "Pierwsze spotkanie.")
    app.pump()
    # Second transcription starts before any title job.
    assert [j.name for j in app.engine.jobs] == ["transcribe", "transcribe"]

    _finish_transcription(app.engine.jobs[-1], "Drugie spotkanie.")
    app.pump()
    assert [j.name for j in app.engine.jobs] == ["transcribe", "transcribe", "title"]

    _run_title_job(app.engine.jobs[-1])
    app.pump()
    assert [j.name for j in app.engine.jobs] == ["transcribe", "transcribe", "title", "title"]


def test_user_rename_during_title_job_is_not_overwritten(env, tmp_path):
    app, m, events = env.make()
    folder = _session(tmp_path)
    _start(m, folder)
    _finish_transcription(app.engine.jobs[-1])
    app.pump()

    title_job = app.engine.jobs[-1]
    assert title_job.name == "title"
    assert m.rename(folder, "Moja własna nazwa")
    app.pump()

    _run_title_job(title_job)
    app.pump()

    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "Moja własna nazwa"
    assert meta["name_source"] == "user"
    assert "title_model" not in meta
    assert not [e for e in events if e[0] == "titled"]


def test_rename_folder_option_renames_directory_on_title(env, tmp_path):
    app, m, events = env.make(rename_folder=True)
    folder = _session(tmp_path, "2026-11-16_10-00_Meeting")
    _start(m, folder)
    _finish_transcription(app.engine.jobs[-1])
    app.pump()

    _run_title_job(app.engine.jobs[-1])
    app.pump()

    titled = [data for event, data in events if event == "titled"]
    assert len(titled) == 1
    old, new, title = titled[0]
    assert old == folder
    assert new != folder and new.is_dir() and not folder.exists()
    assert "Plan-wdrozenia-systemu" in new.name
    assert title == "Plan wdrożenia systemu"


def test_interrupted_title_job_requeues_at_front_and_starts_retry_timer(env, tmp_path, monkeypatch):
    app, m, events = env.make()
    a = _session(tmp_path, "2026-11-16_10-00_Meeting")
    b = _session(tmp_path, "2026-11-16_10-30_Meeting")
    _start(m, a)
    _start(m, b)
    _finish_transcription(app.engine.jobs[-1], "Pierwsze.")
    app.pump()
    _finish_transcription(app.engine.jobs[-1], "Drugie.")
    app.pump()

    title_job = app.engine.jobs[-1]
    assert title_job.name == "title"

    def interrupt_during_call(segments, model, device="GPU", cancel=None, language=None):
        app.dictation.recording = True
        assert cancel is not None and cancel.is_set()
        return {}

    monkeypatch.setattr(meeting_mod.titles, "generate_title", interrupt_during_call)
    _run_title_job(title_job)
    app.pump()

    assert list(m._titles) == [a, b]
    assert m._title_timer is not None and m._title_timer.is_alive()
    assert not [e for e in events if e[0] == "titled"]


def test_unusable_title_result_changes_nothing_and_advances(env, tmp_path, monkeypatch):
    app, m, events = env.make()
    a = _session(tmp_path, "2026-11-16_10-00_Meeting")
    b = _session(tmp_path, "2026-11-16_10-30_Meeting")
    _start(m, a)
    _start(m, b)
    _finish_transcription(app.engine.jobs[-1], "Pierwsze.")
    app.pump()
    _finish_transcription(app.engine.jobs[-1], "Drugie.")
    app.pump()

    results = [{}, dict(env.title_output)]
    monkeypatch.setattr(meeting_mod.titles, "generate_title",
                        lambda *_a, **_kw: results.pop(0))

    _run_title_job(app.engine.jobs[-1])
    app.pump()
    meta_a = json.loads((a / "meta.json").read_text(encoding="utf-8"))
    assert meta_a["name"] == "Meeting" and "name_source" not in meta_a

    # Second folder's title job was started right after the first returned {}.
    assert len(app.engine.jobs) == 4 and app.engine.jobs[-1].name == "title"
    _run_title_job(app.engine.jobs[-1])
    app.pump()
    meta_b = json.loads((b / "meta.json").read_text(encoding="utf-8"))
    assert meta_b["name"] == "Plan wdrożenia systemu" and meta_b["name_source"] == "auto"
    assert [d[0] for e, d in events if e == "titled"] == [b]


def test_rename_updates_meta_refuses_busy_and_removes_from_title_queue(env, tmp_path):
    app, m, events = env.make()
    a = _session(tmp_path, "2026-11-16_10-00_Meeting")
    b = _session(tmp_path, "2026-11-16_10-30_Meeting")
    _start(m, a)
    _start(m, b)

    # Active and queued sessions cannot be renamed.
    assert not m.rename(a, "Nowa nazwa")
    assert not m.rename(b, "Nowa nazwa")
    m.recording, m.recording_dir = True, b
    assert not m.rename(b, "Nowa nazwa")
    m.recording, m.recording_dir = False, None

    _finish_transcription(app.engine.jobs[-1])
    app.pump()
    # `a` is now in `m._titles` while `b` transcribes; renaming `a` drops it from `_titles`.
    assert a in m._titles
    assert m.rename(a, "  Nazwa   użytkownika  ")
    assert a not in m._titles
    app.pump()

    meta_a = json.loads((a / "meta.json").read_text(encoding="utf-8"))
    assert meta_a["name"] == "Nazwa użytkownika"
    assert meta_a["name_source"] == "user"
    assert ("renamed", (a, "Nazwa użytkownika")) in events


def test_quit_cancels_timer_clears_queue_and_cancels_title_job(env, tmp_path):
    app, m, _events = env.make()
    a = _session(tmp_path, "2026-11-16_10-00_Meeting")
    b = _session(tmp_path, "2026-11-16_10-30_Meeting")
    _start(m, a)
    _start(m, b)
    _finish_transcription(app.engine.jobs[-1])
    app.pump()
    _finish_transcription(app.engine.jobs[-1])
    app.pump()

    title_job = app.engine.jobs[-1]
    m._retry_titles()
    assert m._title_timer is not None
    m.quit()

    assert m._title_timer is None
    assert not m._titles
    assert title_job.cancel.is_set()


def test_stop_writes_name_source_user_only_when_typed(env, tmp_path):
    _app, m, _events = env.make(transcribe_after_stop=False)

    typed_folder = tmp_path / "2026-11-16_10-00_Typed"
    typed_folder.mkdir()
    m.recording, m.recording_dir, m.recording_start = True, typed_folder, 0.0
    m.session_name = "Budżet Q4"
    m.session_name_source = "user"
    m.recorder = FakeRecorder(typed_folder)
    m.stop()
    meta_typed = json.loads((typed_folder / "meta.json").read_text(encoding="utf-8"))
    assert meta_typed["name"] == "Budżet Q4"
    assert meta_typed.get("name_source") == "user"

    default_folder = tmp_path / "2026-11-16_10-30_Meeting"
    default_folder.mkdir()
    m.recording, m.recording_dir, m.recording_start = True, default_folder, 0.0
    m.session_name = "Meeting"
    m.session_name_source = None
    m.recorder = FakeRecorder(default_folder)
    m.stop()
    meta_default = json.loads((default_folder / "meta.json").read_text(encoding="utf-8"))
    assert meta_default["name"] == "Meeting"
    assert "name_source" not in meta_default
