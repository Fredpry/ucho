# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Speaker codes, user-given speaker names, transcript rendering and MeetingController."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucho import diarize, i18n, meeting as meeting_mod, session
from ucho.merge import (
    MAX_SPEAKER_NAME,
    Segment,
    clean_speaker_name,
    clean_speaker_names,
    diarized_code,
    save_results,
    speaker_label,
    speaker_track,
    split_speaker,
)


@pytest.fixture(autouse=True)
def _reset_language():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def test_split_speaker_and_speaker_track():
    assert split_speaker("meeting:3") == ("meeting", 3)
    assert split_speaker("me:1") == ("me", 1)
    assert split_speaker("me") == ("me", None)
    assert split_speaker("meeting") == ("meeting", None)
    for invalid in ("meeting:0", "meeting:-1", "meeting:x", "meeting:", "other:2", "Anna", ""):
        assert split_speaker(invalid) == (invalid, None)
    assert speaker_track("meeting:2") == "meeting"
    assert speaker_track("me:4") == "me"
    assert speaker_track("me") == "me"
    assert speaker_track("Anna") == "Anna"
    assert diarized_code("meeting", 2) == "meeting:2"


def test_clean_speaker_name_and_names_dict():
    assert clean_speaker_name("  Anna \n\t  Kowalska\r\x07 ") == "Anna Kowalska"
    assert clean_speaker_name(None) == ""
    assert clean_speaker_name("   \n\t  ") == ""
    long_name = "A" * 60
    assert len(clean_speaker_name(long_name)) == MAX_SPEAKER_NAME
    assert clean_speaker_name("A" * 39 + "   B") == "A" * 39

    assert clean_speaker_names(None) == {}
    assert clean_speaker_names(["not", "a", "dict"]) == {}
    cleaned = clean_speaker_names({
        "Spotkanie": "  Zespół  ",
        "Ja": "Jan",
        "meeting:1": "Anna\nNowak",
        "meeting:2": "   ",
        "": "Ignored",
    })
    assert list(cleaned.items()) == [
        ("meeting", "Zespół"),
        ("me", "Jan"),
        ("meeting:1", "Anna Nowak"),
    ]


def test_speaker_label_with_names_diarized_codes_and_languages():
    names = {"meeting:1": "Anna", "me": "Jan"}
    assert speaker_label("meeting:1", names) == "Anna"
    assert speaker_label("me", names) == "Jan"
    assert speaker_label("meeting:2", names) == "Speaker 2"
    assert speaker_label("me:3", names) == "Microphone 3"
    assert speaker_label("me") == "Me"
    assert speaker_label("meeting") == "Meeting"
    assert speaker_label("Custom") == "Custom"

    i18n.set_language("pl")
    assert speaker_label("meeting:1", names) == "Anna"
    assert speaker_label("meeting:2") == "Mówca 2"
    assert speaker_label("me:3") == "Mikrofon 3"
    assert speaker_label("meeting:2", translate=False) == "Speaker 2"
    assert speaker_label("me:3", translate=False) == "Microphone 3"
    assert speaker_label("me") == "Ja"
    assert speaker_label("me", translate=False) == "Me"


def test_save_results_renders_speaker_names_and_keeps_codes_in_segments(tmp_path: Path):
    segments = [
        Segment(0.0, 2.0, "Hello.", "me"),
        Segment(3.0, 5.0, "Hi Jan.", "meeting:1"),
        Segment(6.0, 8.0, "Any updates?", "meeting:2"),
    ]
    meta = {
        "name": "Sync",
        "duration_s": 8,
        "speaker_names": {"meeting:1": "Anna", "me": "Fryderyk-test"},
    }
    files = save_results(tmp_path, segments, meta)
    md = files["md"].read_text(encoding="utf-8")
    srt = files["srt"].read_text(encoding="utf-8")
    txt = files["txt"].read_text(encoding="utf-8")
    saved_meta = json.loads(files["meta"].read_text(encoding="utf-8"))

    assert "**[00:00:00] Fryderyk-test:** Hello." in md
    assert "**[00:00:03] Anna:** Hi Jan." in md
    assert "**[00:00:06] Speaker 2:** Any updates?" in md
    assert "Fryderyk-test: Hello." in srt and "Anna: Hi Jan." in srt and "Speaker 2: Any updates?" in srt
    assert txt.splitlines() == [
        "Fryderyk-test: Hello.",
        "Anna: Hi Jan.",
        "Speaker 2: Any updates?",
    ]
    assert [s["speaker"] for s in saved_meta["segments"]] == ["me", "meeting:1", "meeting:2"]
    assert saved_meta["speaker_names"] == {"meeting:1": "Anna", "me": "Fryderyk-test"}

    # Empty speaker_names dict is omitted from meta.json.
    save_results(tmp_path, segments, {"name": "Sync", "speaker_names": {"meeting:1": "  "}})
    saved_empty = json.loads(files["meta"].read_text(encoding="utf-8"))
    assert "speaker_names" not in saved_empty


def test_session_normalizes_speaker_names_and_lists_codes():
    meta = session.normalize_meta({
        "speaker_names": {"Spotkanie": " Goście ", "meeting:1": "Anna", "me": ""},
        "segments": [
            {"start": 0, "end": 1, "text": "A", "speaker": "me"},
            {"start": 1, "end": 2, "text": "B", "speaker": "meeting:2"},
            {"start": 2, "end": 3, "text": "C", "speaker": "me"},
            {"start": 3, "end": 4, "text": "D", "speaker": "meeting:1"},
        ],
    })
    assert meta["speaker_names"] == {"meeting": "Goście", "meeting:1": "Anna"}
    assert session.speaker_names(meta) == {"meeting": "Goście", "meeting:1": "Anna"}
    assert session.speaker_codes(meta["segments"]) == ["me", "meeting:2", "meeting:1"]
    assert session.speaker_codes(session.segments(meta)) == ["me", "meeting:2", "meeting:1"]


class _FakeEngine:
    def __init__(self):
        self.jobs: list[SimpleNamespace] = []

    def transcribe(self, files, model, device, language, **kwargs):
        job = SimpleNamespace(
            kind="transcribe", files=files, model=model, device=device,
            language=language, cancel=threading.Event(), **kwargs,
        )
        self.jobs.append(job)
        return job


class _FakeSettings(dict):
    def load(self):
        pass


class _FakeApp:
    def __init__(self):
        self.engine = _FakeEngine()
        self.settings = _FakeSettings(
            model="M", device="CPU", language="auto",
            speaker_detection=False, summary_enabled=False,
            summary_model="LLM", rename_folder=False,
        )

    def post(self, fn):
        fn()


def test_rename_speaker_updates_files_and_validates_state(tmp_path: Path, monkeypatch):
    app = _FakeApp()
    controller = meeting_mod.MeetingController(app)
    events: list[tuple[str, object]] = []
    controller.listen(lambda event, data: events.append((event, data)))

    folder = tmp_path / "2026-11-16_10-00_Test"
    segments = [
        Segment(0.0, 2.0, "Good morning.", "me"),
        Segment(3.0, 5.0, "Hello.", "meeting:1"),
    ]
    save_results(folder, segments, {"name": "Test", "duration_s": 5})

    assert controller.rename_speaker(folder, "meeting:1", "  Anna  ") is True
    assert ("speakers", folder) in events
    assert "Anna: Hello." in (folder / "transcript.txt").read_text(encoding="utf-8")
    meta = session.read_meta(folder)
    assert meta["speaker_names"] == {"meeting:1": "Anna"}
    assert session.segments(meta) == segments

    # Clearing with default label (translated or English) or empty string removes the mapping.
    i18n.set_language("pl")
    assert controller.rename_speaker(folder, "meeting:1", "Mówca 1") is True
    assert "speaker_names" not in json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    assert controller.rename_speaker(folder, "meeting:1", "Anna") is True
    assert controller.rename_speaker(folder, "meeting:1", "Speaker 1") is True
    assert "speaker_names" not in json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    assert controller.rename_speaker(folder, "me", "Jan") is True
    assert controller.rename_speaker(folder, "me", "   ") is True
    assert "speaker_names" not in json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    # Unknown speaker code is refused.
    assert controller.rename_speaker(folder, "meeting:9", "Ghost") is False

    # Refused while session is in use.
    monkeypatch.setattr(controller, "in_use", lambda _f: True)
    assert controller.rename_speaker(folder, "me", "Jan") is False
    monkeypatch.setattr(controller, "in_use", lambda _f: False)

    # Refused when the session has no segments.
    empty_folder = tmp_path / "empty"
    empty_folder.mkdir()
    (empty_folder / "meta.json").write_text(json.dumps({"name": "Empty", "segments": []}), encoding="utf-8")
    assert controller.rename_speaker(empty_folder, "me", "Jan") is False


def test_transcribe_again_keeps_plain_speaker_names_and_drops_diarized(tmp_path: Path, monkeypatch):
    app = _FakeApp()
    controller = meeting_mod.MeetingController(app)
    folder = tmp_path / "session"
    folder.mkdir()
    (folder / "microphone.flac").write_bytes(b"x")
    save_results(
        folder,
        [Segment(0.0, 2.0, "Hi.", "me"), Segment(3.0, 5.0, "Hello.", "meeting:2")],
        {"name": "Sync", "speaker_names": {"me": "Jan", "meeting:2": "Anna"}},
    )
    seen_meta: list[dict] = []
    monkeypatch.setattr(controller, "transcribe", lambda f, files, meta, cache=None: seen_meta.append(meta) or True)

    assert controller.transcribe_again(folder) is True
    assert seen_meta[0].get("speaker_names") == {"me": "Jan"}

    # When only diarized names existed, the key is removed completely.
    save_results(
        folder,
        [Segment(3.0, 5.0, "Hello.", "meeting:2")],
        {"name": "Sync", "speaker_names": {"meeting:2": "Anna"}},
    )
    seen_meta.clear()
    assert controller.transcribe_again(folder) is True
    assert "speaker_names" not in seen_meta[0]


def test_start_passes_speakers_hook_only_when_enabled_and_ready(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(meeting_mod.transcribe, "model_downloaded", lambda _m: True)
    app = _FakeApp()
    controller = meeting_mod.MeetingController(app)
    folder = tmp_path / "s"
    folder.mkdir()
    files = {"microphone": folder / "microphone.flac"}

    # Setting off: no speakers kwarg passed.
    monkeypatch.setattr(diarize, "ready", lambda: True)
    assert controller._start(folder, files, {"name": "s"}) is True
    assert "speakers" not in app.engine.jobs[-1].__dict__

    # Setting on, but diarize.ready() is False: no speakers kwarg passed.
    controller.transcribing = False
    app.settings["speaker_detection"] = True
    monkeypatch.setattr(diarize, "ready", lambda: False)
    assert controller._start(folder, files, {"name": "s"}) is True
    assert "speakers" not in app.engine.jobs[-1].__dict__

    # Setting on and diarize.ready() is True: speakers callable is passed.
    controller.transcribing = False
    monkeypatch.setattr(diarize, "ready", lambda: True)
    assert controller._start(folder, files, {"name": "s"}) is True
    assert callable(getattr(app.engine.jobs[-1], "speakers", None))
