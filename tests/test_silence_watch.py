# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""`audio.SilenceWatch` and the recorder's handling of a system source that fails or stays silent."""

from __future__ import annotations

import numpy as np

from ucho import audio, platforms

RATE = 48_000
BLOCK = 4_800  # 0.1 s


def _zeros(seconds: float) -> list[np.ndarray]:
    return [np.zeros(BLOCK, dtype=np.float32)] * int(round(seconds * RATE / BLOCK))


def test_zeros_for_21_s_fire_once():
    watch = audio.SilenceWatch()
    fired = [watch.feed(block, RATE) for block in _zeros(21)]
    assert fired.count(True) == 1
    # It fires on the block that completes 20 s, not earlier.
    assert fired.index(True) == int(20 * RATE / BLOCK) - 1


def test_noise_never_fires():
    rng = np.random.default_rng(1)
    watch = audio.SilenceWatch()
    for _ in range(300):
        assert not watch.feed((rng.standard_normal(BLOCK) * 1e-4).astype(np.float32), RATE)
    assert watch.heard and not watch.fired


def test_zeros_after_sound_never_fire():
    watch = audio.SilenceWatch()
    block = np.zeros(BLOCK, dtype=np.float32)
    block[17] = 1 / 32768  # one least significant bit is enough
    assert not watch.feed(block, RATE)
    assert not any(watch.feed(z, RATE) for z in _zeros(25))


def test_short_silence_and_empty_blocks():
    watch = audio.SilenceWatch(limit_s=1.0)
    assert not any(watch.feed(z, RATE) for z in _zeros(0.9))
    assert not watch.feed(np.zeros(0, dtype=np.float32), RATE)
    assert not watch.feed(np.zeros(BLOCK, dtype=np.float32), 0)
    assert watch.feed(np.zeros(BLOCK, dtype=np.float32), RATE)
    assert not watch.feed(np.zeros(BLOCK, dtype=np.float32), RATE)


# --- the recorder ----------------------------------------------------------------------


MIC = audio.DeviceInfo(1, "Fake Mic", 1, 16_000, False)
SYSTEM = audio.DeviceInfo(-1, "Fake System", 1, 16_000, True)


class FakeStream:
    def __init__(self, on_data):
        self.on_data = on_data
        self.events: list[str] = []
        self.failure = None
        self.starting = False

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")

    def close(self):
        self.events.append("close")


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.streams: dict[int, list[FakeStream]] = {}

    def list_devices(self):
        return {"microphones": [MIC], "system": [SYSTEM], "default_microphone": MIC,
                "default_system": SYSTEM, "names_by_index": {}}

    def open_input(self, dev, channels, rate, frames, on_data, host=None):
        stream = FakeStream(on_data)
        self.streams.setdefault(dev.index, []).append(stream)
        return stream


def _recorder(monkeypatch):
    backend = FakeBackend()
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(platforms, "audio", lambda: backend)
    rec = audio.Recorder(on_notice=lambda code, message: notices.append((code, message)))
    return backend, rec, notices


def _write_zeros(track: audio._Track, seconds: float) -> None:
    for _ in range(int(seconds * 10)):
        track._write_block(np.zeros(1_600, dtype=np.int16).tobytes())


def test_recorder_warns_once_when_the_macos_system_track_is_all_zeros(monkeypatch, tmp_path):
    with platforms.override("macos"):
        backend, rec, notices = _recorder(monkeypatch)
        rec.start(tmp_path)
        try:
            _write_zeros(rec._tracks["microphone"], 25)  # the microphone is never watched
            assert notices == []
            _write_zeros(rec._tracks["system"], 25)
        finally:
            result = rec.stop()
    message = "System audio is silent. Ucho may not have permission to record it."
    assert notices == [(audio.NOTICE_SYSTEM_SILENT, message)]
    assert result["warnings"].count(message) == 1
    assert set(result["tracks"]) == {"microphone", "system"}


def test_recorder_does_not_watch_silence_off_macos(monkeypatch, tmp_path):
    with platforms.override("null"):
        backend, rec, notices = _recorder(monkeypatch)
        rec.start(tmp_path)
        try:
            _write_zeros(rec._tracks["system"], 25)
        finally:
            rec.stop()
    assert notices == []


def test_failed_source_is_dropped_and_the_recording_goes_on(monkeypatch, tmp_path):
    with platforms.override("macos"):
        backend, rec, notices = _recorder(monkeypatch)
        rec.start(tmp_path)
        try:
            system_stream = backend.streams[-1][0]
            system = rec._tracks["system"]
            system_stream.starting = True
            system._last_data = 0.0  # long ago: it would be dead if it were not starting
            assert not system.dead
            system_stream.starting = False
            system_stream.failure = "the system audio tap did not start within 10 s"
            rec._drop_failed()
            rec._drop_failed()  # once only
            assert system.abandoned
            assert system_stream.events == ["start", "stop", "close"]
            assert notices == [(audio.NOTICE_SYSTEM_FAILED,
                                "System audio could not be recorded — recording the microphone only.")]
            # The restart after a dead microphone leaves the abandoned track alone.
            rec._tracks["microphone"]._last_data = 0.0
            rec._restart_streams()
            assert len(backend.streams[-1]) == 1
            assert len(backend.streams[1]) == 2
            _write_zeros(rec._tracks["microphone"], 1)
        finally:
            result = rec.stop()
    assert set(result["tracks"]) == {"microphone"}
    assert not (tmp_path / "meeting.flac").exists()
    assert (tmp_path / "microphone.flac").exists()
    assert notices[0][1] in result["warnings"]


def test_failed_source_without_a_microphone_track_says_so(monkeypatch, tmp_path):
    with platforms.override("macos"):
        backend = FakeBackend()
        notices: list[tuple[str, str]] = []
        monkeypatch.setattr(platforms, "audio", lambda: backend)
        rec = audio.Recorder(tracks="system",
                             on_notice=lambda code, message: notices.append((code, message)))
        rec.start(tmp_path)
        try:
            backend.streams[-1][0].failure = "gone"
            rec._drop_failed()
        finally:
            rec.stop()
    assert notices == [(audio.NOTICE_SYSTEM_FAILED, "System audio could not be recorded.")]


def test_failed_source_keeps_what_it_recorded(monkeypatch, tmp_path):
    with platforms.override("macos"):
        backend, rec, notices = _recorder(monkeypatch)
        rec.start(tmp_path)
        try:
            _write_zeros(rec._tracks["microphone"], 1)
            noise = (np.arange(1_600, dtype=np.int16) % 50 - 25).tobytes()
            for _ in range(10):
                rec._tracks["system"]._write_block(noise)
            backend.streams[-1][0].failure = "gone"
            rec._drop_failed()
        finally:
            result = rec.stop()
    assert set(result["tracks"]) == {"microphone", "system"}
    assert result["tracks"]["system"]["duration_s"] > 0.9


def test_streams_without_the_new_attributes_behave_as_before(monkeypatch, tmp_path):
    class PlainStream:
        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    class PlainBackend(FakeBackend):
        def open_input(self, dev, channels, rate, frames, on_data, host=None):
            return PlainStream()

    with platforms.override("null"):
        monkeypatch.setattr(platforms, "audio", lambda: PlainBackend())
        rec = audio.Recorder()
        rec.start(tmp_path)
        try:
            track = rec._tracks["microphone"]
            assert not track.starting and track.failure is None
            track._last_data = 0.0
            assert track.dead
            rec._drop_failed()
            assert not track.abandoned
        finally:
            rec.stop()
