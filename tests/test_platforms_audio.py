# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tests for platform audio, sysinfo, and fonts backends (M1c)."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

try:
    import pyaudiowpatch  # noqa: F401
except ImportError:  # the CI test job does not install the audio stack
    stub = types.ModuleType("pyaudiowpatch")
    stub.paWASAPI, stub.paInt16, stub.paContinue = 13, 8, 0
    stub.PyAudio = object
    sys.modules["pyaudiowpatch"] = stub

from ucho import audio, dictation, platforms, theme
from ucho.platforms import base

ROOT = Path(__file__).resolve().parent.parent


def _check_contract(protocol: type, impl: object) -> None:
    assert isinstance(impl, protocol), f"{type(impl).__name__} does not satisfy {protocol.__name__}"
    for name, member in protocol.__dict__.items():
        if name.startswith("_") or not callable(member):
            continue
        proto_sig = inspect.signature(member)
        attr = getattr(impl, name)
        assert callable(attr)
        proto_params = list(proto_sig.parameters.values())[1:]
        impl_params = list(inspect.signature(attr).parameters.values())
        assert len(impl_params) == len(proto_params), f"{type(impl).__name__}.{name} param count"
        assert [p.name for p in impl_params] == [p.name for p in proto_params], (
            f"{type(impl).__name__}.{name} param names"
        )
        assert [p.default is not inspect.Parameter.empty for p in impl_params] == [
            p.default is not inspect.Parameter.empty for p in proto_params
        ], f"{type(impl).__name__}.{name} defaults"


def test_import_audio_without_pyaudiowpatch_and_null_backend():
    code = (
        "import sys; sys.modules['pyaudiowpatch'] = None; "
        "import ucho.audio as audio; from ucho import platforms\n"
        "with platforms.override('null'):\n"
        "    assert audio.list_devices() == {\n"
        "        'microphones': [], 'system': [],\n"
        "        'default_microphone': None, 'default_system': None,\n"
        "        'names_by_index': {},\n"
        "    }\n"
        "    assert audio.new_pyaudio() is None\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_track_with_fake_audio_backend(monkeypatch, tmp_path):
    pytest.importorskip("soxr")  # 48 kHz -> 16 kHz; the CI test job has no soxr
    rng = np.random.default_rng(0)
    blocks = [
        rng.integers(-4000, 4000, size=audio.BLOCK * 2, dtype=np.int16).tobytes()
        for _ in range(50)
    ]
    events: list[str] = []
    hosts: list[object] = []

    class FakeStream:
        def __init__(self, on_data):
            self._on_data = on_data

        def start(self):
            events.append("start")
            for block in blocks:
                self._on_data(block)

        def stop(self):
            events.append("stop")

        def close(self):
            events.append("close")

    class FakeBackend:
        name = "fake"

        def open_input(self, device, channels, rate, frames, on_data, host=None):
            hosts.append(host)
            return FakeStream(on_data)

    monkeypatch.setattr(platforms, "audio", lambda: FakeBackend())

    device = audio.DeviceInfo(0, "Fake Mic", 2, 48_000, False)
    flac_path = tmp_path / "microphone.flac"
    track = audio._Track("microphone", flac_path, device)
    track.open_file()
    track.start(None)
    track.finish()

    info = sf.info(str(flac_path))
    samples, rate = sf.read(str(flac_path), dtype="float32")
    assert rate == 16_000
    assert info.channels == 1
    assert abs(len(samples) - (50 * audio.BLOCK // 3)) <= audio.BLOCK
    assert hosts == [None]
    assert events == ["start", "stop", "close"]


def test_dictation_microphone_with_fake_backend_under_null_override(monkeypatch):
    pytest.importorskip("soxr")  # 48 kHz -> 16 kHz; the CI test job has no soxr
    rng = np.random.default_rng(1)
    blocks = [
        rng.integers(-3000, 3000, size=1024 * 2, dtype=np.int16).tobytes()
        for _ in range(20)
    ]
    device = audio.DeviceInfo(1, "Fake Mic", 2, 48_000, False)
    hosts: list[object] = []

    class FakeStream:
        def __init__(self, on_data):
            self._on_data = on_data

        def start(self):
            for block in blocks:
                self._on_data(block)

        def stop(self):
            pass

        def close(self):
            pass

    class FakeBackend:
        name = "fake"

        def list_devices(self):
            return {
                "microphones": [device],
                "system": [],
                "default_microphone": device,
                "default_system": None,
                "names_by_index": {},
            }

        def open_input(self, dev, channels, rate, frames, on_data, host=None):
            hosts.append(host)
            return FakeStream(on_data)

    with platforms.override("null"):
        monkeypatch.setattr(platforms, "audio", lambda: FakeBackend())
        mic = dictation.Microphone(None)
        mic.start()
        signal = mic.stop()

    expected = round(20 * 1024 * 16_000 / 48_000)
    assert signal.dtype == np.float32
    assert abs(signal.size - expected) <= 32
    assert hosts == [None]


def test_recorder_null_override_warning_and_restart_streams(monkeypatch, tmp_path):
    device = audio.DeviceInfo(1, "Fake Mic", 1, 16_000, False)
    opened: list[int] = []

    class FakeStream:
        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    class FakeBackend:
        name = "fake"

        def list_devices(self):
            return {
                "microphones": [device],
                "system": [],
                "default_microphone": device,
                "default_system": None,
                "names_by_index": {},
            }

        def open_input(self, dev, channels, rate, frames, on_data, host=None):
            opened.append(dev.index)
            return FakeStream()

    with platforms.override("null"):
        monkeypatch.setattr(platforms, "audio", lambda: FakeBackend())
        rec = audio.Recorder()
        rec.start(tmp_path)
        try:
            assert any("No system audio source found" in w for w in rec.warnings)
            assert len(opened) == 1
            rec._restart_streams()
            assert len(opened) == 2
        finally:
            rec.stop()
        rec._restart_streams()
        assert len(opened) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only backends")
def test_windows_fonts_audio_and_protocol_contracts():
    from ucho.platforms.windows import audio as win_audio
    from ucho.platforms.windows import fonts as win_fonts
    from ucho.platforms.windows import sysinfo as win_sysinfo

    fonts_impl = win_fonts.create()
    assert fonts_impl.sans == ("Segoe UI Variable Text", "Segoe UI", "Arial")
    assert fonts_impl.display == ("Sitka Heading", "Georgia", "Times New Roman")
    assert fonts_impl.mono == ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New")
    assert fonts_impl.numeric == ("Segoe UI Variable Display", "Segoe UI Light", "Segoe UI", "Arial")
    assert fonts_impl.strong == {
        "sans": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
        "display": ("Sitka Heading Semibold",),
        "numeric": ("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
    }
    assert theme.SANS == fonts_impl.sans

    calls: list[dict] = []
    stream_events: list[str] = []

    class FakeRawStream:
        def start_stream(self):
            stream_events.append("start_stream")

        def stop_stream(self):
            stream_events.append("stop_stream")

        def close(self):
            stream_events.append("close")

    class FakePa:
        def open(self, **kwargs):
            calls.append(kwargs)
            return FakeRawStream()

    received: list[bytes] = []
    dev = audio.DeviceInfo(7, "Mic", 2, 48_000, False)
    backend = win_audio.create()
    stream = backend.open_input(dev, 2, 48_000, 1024, received.append, host=FakePa())

    assert len(calls) == 1
    kwargs = calls[0]
    assert set(kwargs) == {
        "format",
        "channels",
        "rate",
        "input",
        "input_device_index",
        "frames_per_buffer",
        "stream_callback",
    }
    assert kwargs["format"] == win_audio.pyaudio.paInt16
    assert kwargs["channels"] == 2
    assert kwargs["rate"] == 48_000
    assert kwargs["input"] is True
    assert kwargs["input_device_index"] == 7
    assert kwargs["frames_per_buffer"] == 1024

    cb_result = kwargs["stream_callback"](b"abcd", 1, {}, 0)
    assert cb_result == (None, win_audio.pyaudio.paContinue)
    assert received == [b"abcd"]

    stream.start()
    stream.stop()
    stream.close()
    assert stream_events == ["start_stream", "stop_stream", "close"]

    for protocol, impl in (
        (base.AudioBackend, backend),
        (base.InputStream, stream),
        (base.SystemInfo, win_sysinfo.create()),
        (base.Fonts, fonts_impl),
    ):
        _check_contract(protocol, impl)
