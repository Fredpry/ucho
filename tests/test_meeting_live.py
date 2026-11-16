# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Live meeting transcription in the meeting controller: stop, queue, cache, partial save.

No audio devices and no models: the recorder, the live transcription and the
engine are fakes; the chunk logic itself is tested in test_live_meeting.py.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucho import config
from ucho import meeting as meeting_mod
from ucho.live_meeting import ChunkCache
from ucho.merge import TRANSCRIPT_COMPLETE, TRANSCRIPT_PARTIAL, Segment


class FakeEngine:
    def __init__(self):
        self.jobs: list[SimpleNamespace] = []

    def transcribe(self, files, model, device, language, progress=None, status=None,
                   on_done=None, on_error=None, report=None, cache=None):
        job = SimpleNamespace(kind="transcribe", files=files, cache=cache, on_done=on_done,
                              on_error=on_error, report=report, cancel=threading.Event())
        self.jobs.append(job)
        return job


class FakeSettings(dict):
    def load(self):
        pass

    def meeting_live(self):
        return True


class FakeApp:
    def __init__(self):
        self.engine = FakeEngine()
        self.settings = FakeSettings(model="M", device="GPU", language="auto", summary_enabled=False,
                                     summary_model="LLM", rename_folder=False,
                                     transcribe_after_stop=True, auto_title=False)
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
        return {"folder": self.folder, "duration_s": 12.0, "warnings": [],
                "tracks": {"system": {"file": file, "device": "Speakers", "duration_s": 12.0}}}


class FakeLive:
    def __init__(self, cache):
        self.cache = cache
        self.stopped = 0
        self.done, self.errors, self.gave_up = 3, 0, False

    def stop(self):
        self.stopped += 1
        return self.cache


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(meeting_mod.transcribe, "model_downloaded", lambda _m: True)
    monkeypatch.setattr(meeting_mod.transcribe, "genai_version", lambda: "test")

    def factory():
        app = FakeApp()
        controller = meeting_mod.MeetingController(app)
        events: list = []
        controller.listen(lambda event, data: events.append((event, data)))
        return app, controller, events

    return factory


def _recording(m, folder: Path, cache=None, segments=None):
    """Puts the controller in the state of a running recording with live transcription."""
    folder.mkdir(exist_ok=True)
    m.recording, m.recording_dir, m.recording_start = True, folder, 0.0
    m.recorder = FakeRecorder(folder)
    m.live = FakeLive(cache if cache is not None else ChunkCache("M", "GPU", "auto"))
    m.live_segments = list(segments or [])
    return m.live


def test_stop_hands_the_live_cache_to_the_transcription(make, tmp_path):
    app, m, _events = make()
    cache = ChunkCache("M", "GPU", "auto")
    live = _recording(m, tmp_path / "a", cache)
    m.stop()
    assert live.stopped == 1 and m.live is None
    assert len(app.engine.jobs) == 1 and app.engine.jobs[0].cache is cache


def test_stop_keeps_the_live_text_on_disk_until_the_transcription(make, tmp_path):
    app, m, _events = make()
    folder = tmp_path / "a"
    _recording(m, folder, segments=[Segment(1.0, 2.0, "hello", "meeting")])
    m.stop()
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta[TRANSCRIPT_PARTIAL] is True and meta[TRANSCRIPT_COMPLETE] is False
    assert [s["text"] for s in meta["segments"]] == ["hello"]
    assert "hello" in (folder / "transcript.md").read_text(encoding="utf-8")

    # The transcription after the stop replaces it: no duplicated segments, no partial flag.
    app.engine.jobs[0].on_done(([Segment(1.0, 2.0, "hello", "meeting")], "GPU"))
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert TRANSCRIPT_PARTIAL not in meta and meta[TRANSCRIPT_COMPLETE] is True
    assert [s["text"] for s in meta["segments"]] == ["hello"]


def test_stop_without_live_text_writes_plain_meta(make, tmp_path):
    _app, m, _events = make()
    folder = tmp_path / "a"
    _recording(m, folder)
    m.stop()
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert TRANSCRIPT_PARTIAL not in meta and "segments" not in meta


def test_a_queued_recording_keeps_its_cache_until_it_starts(make, tmp_path):
    app, m, _events = make()
    busy = tmp_path / "busy"
    busy.mkdir()
    m.transcribe(busy, {"system": busy / "meeting.flac"}, {"name": "busy"})
    cache = ChunkCache("M", "GPU", "auto")
    folder = tmp_path / "a"
    _recording(m, folder, cache)
    m.stop()
    assert m.queue_position(folder) == 1 and len(app.engine.jobs) == 1
    app.engine.jobs[0].on_done(([], "GPU"))
    app.pump()
    assert len(app.engine.jobs) == 2 and app.engine.jobs[1].cache is cache


def test_a_dequeued_recording_drops_its_cache(make, tmp_path):
    app, m, _events = make()
    busy = tmp_path / "busy"
    busy.mkdir()
    m.transcribe(busy, {"system": busy / "meeting.flac"}, {"name": "busy"})
    folder = tmp_path / "a"
    _recording(m, folder)
    m.stop()
    assert m.dequeue(folder)
    assert m._caches == {}


def test_cache_from_other_settings_is_not_used(make, tmp_path):
    app, m, _events = make()
    folder = tmp_path / "a"
    _recording(m, folder, ChunkCache("M", "GPU", "pl"))  # recorded with Polish, now Auto
    m.stop()
    assert app.engine.jobs[0].cache is None


def test_transcribe_again_never_uses_a_cache(make, tmp_path):
    app, m, _events = make()
    folder = tmp_path / "a"
    folder.mkdir()
    (folder / "meeting.flac").write_bytes(b"x")
    (folder / "meta.json").write_text(json.dumps({"name": "a", TRANSCRIPT_PARTIAL: True,
                                                  "segments": [{"start": 0, "end": 1, "text": "x",
                                                                "speaker": "meeting"}]}),
                                      encoding="utf-8")
    assert m.transcribe_again(folder)
    job = app.engine.jobs[0]
    assert job.cache is None
    job.on_done(([Segment(0.0, 1.0, "y", "meeting")], "GPU"))
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert TRANSCRIPT_PARTIAL not in meta and [s["text"] for s in meta["segments"]] == ["y"]


def test_no_live_without_transcription_after_stop(make):
    app, m, _events = make()
    app.settings["transcribe_after_stop"] = False
    assert not m.live_wanted()


def test_live_update_saves_and_emits(make, tmp_path):
    app, m, events = make()
    folder = tmp_path / "a"
    folder.mkdir()
    m._live_meta = {"name": "a", "date": "2026-01-01 10:00", "model": "M", "language": "auto"}
    m._live_update(folder, [Segment(0.0, 1.0, "live words", "me")])
    app.pump()
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta[TRANSCRIPT_PARTIAL] is True
    assert ("live", m.live_segments) in events


@pytest.mark.parametrize("stored, device, language, expected", [
    (None, "GPU", "auto", True), (None, "GPU.1", "auto", True), (None, "NPU", "auto", True),
    (None, "CPU", "auto", False), (None, "GPU", "pl", False), (None, "GPU", "en", False),
    (True, "CPU", "pl", True), (False, "GPU", "auto", False),
])
def test_meeting_live_setting(tmp_path, stored, device, language, expected):
    settings = config.Settings(tmp_path / "settings.json")
    settings["meeting_live"] = stored
    settings["device"] = device
    settings["language"] = language
    assert settings.meeting_live() is expected


def test_meeting_live_default_is_automatic():
    assert config.DEFAULTS["meeting_live"] is None


def test_recorder_track_passes_written_samples_to_the_listener(tmp_path, monkeypatch):
    import sys
    import types

    import numpy as np
    import soundfile as sf

    try:
        import pyaudiowpatch  # noqa: F401
    except ImportError:
        stub = types.ModuleType("pyaudiowpatch")
        stub.paInt16, stub.paContinue, stub.paWASAPI = 8, 0, 13
        monkeypatch.setitem(sys.modules, "pyaudiowpatch", stub)
    from ucho import audio

    got: list = []

    def listener(name, samples):
        got.append((name, np.array(samples)))
        if len(got) == 1:
            raise RuntimeError("a broken listener must not stop the recording")

    device = audio.DeviceInfo(0, "Mic", 1, 16_000, False)
    track = audio._Track("microphone", tmp_path / "microphone.flac", device, listener)
    track.open_file()
    blocks = [(np.sin(np.arange(n) / 7.0) * 8000).astype(np.int16) for n in (1600, 2048, 512)]
    for block in blocks:
        track._write_block(block.tobytes())
    track._out.close()
    track._out = None
    written, _rate = sf.read(str(tmp_path / "microphone.flac"), dtype="float32")
    assert [name for name, _ in got] == ["microphone"] * 3
    tapped = np.concatenate([samples for _name, samples in got])
    from ucho import live_meeting

    # What the listener got, quantized like the file, is exactly what was written.
    assert np.array_equal(live_meeting.quantize_pcm16(tapped), written)
