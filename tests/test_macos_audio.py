# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tests for the macOS sounddevice audio backend (`ucho/platforms/macos/audio.py`)."""

from __future__ import annotations

import inspect
import logging
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ucho import audio, platforms
from ucho.platforms import base
from ucho.platforms.macos import audio as mac_audio
from ucho.platforms.macos.audio import MacAudio


class _FakePortAudioError(Exception):
    pass


def _make_fake_sd(devices=None, default_device=(0, 2), stream_cls=None):
    fake = types.ModuleType("sounddevice")
    fake.PortAudioError = _FakePortAudioError
    fake.default = SimpleNamespace(device=default_device)
    if callable(devices):
        fake.query_devices = devices
    else:
        data = list(devices or [])
        fake.query_devices = lambda: list(data)
    if stream_cls is not None:
        fake.RawInputStream = stream_cls
    return fake


def _sample_devices() -> list[dict]:
    return [
        {
            "index": 0,
            "name": "MacBook Pro Microphone",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
        },
        {
            "index": 1,
            "name": "External USB Mic",
            "hostapi": 0,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 44100.0,
        },
        {
            "index": 2,
            "name": "MacBook Pro Speakers",
            "hostapi": 0,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
        {
            "index": 3,
            "name": "  ucho Aggregate ",
            "hostapi": 0,
            "max_input_channels": 2,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]


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


def test_list_devices_filters_outputs_and_aggregates(monkeypatch):
    fake = _make_fake_sd(_sample_devices(), default_device=(1, 2))
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    result = MacAudio().list_devices()
    assert set(result) == {
        "microphones",
        "system",
        "default_microphone",
        "default_system",
        "names_by_index",
    }
    mics = result["microphones"]
    assert [m.name for m in mics] == ["MacBook Pro Microphone", "External USB Mic"]
    assert [m.index for m in mics] == [0, 1]
    assert [m.channels for m in mics] == [1, 2]
    assert [m.rate for m in mics] == [48000, 44100]
    assert all(m.loopback is False for m in mics)
    assert result["default_microphone"] == mics[1]
    assert result["system"] == []
    assert result["default_system"] is None
    assert result["names_by_index"] == {
        0: "MacBook Pro Microphone",
        1: "External USB Mic",
        3: "  ucho Aggregate ",
    }


def test_list_devices_default_fallbacks_and_missing_index(monkeypatch):
    raw = [
        {"name": "Built-in Mic", "max_input_channels": 1, "default_samplerate": 48000.0},
        {"name": "Second Mic", "max_input_channels": 2, "default_samplerate": 48000.0},
        {"name": "Ucho Aggregate", "max_input_channels": 2, "default_samplerate": 48000.0},
        {"max_input_channels": 2, "default_samplerate": 48000.0},  # broken entry without name
    ]

    # Default -1 falls back to first microphone.
    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sd(raw, default_device=(-1, 0)))
    res_neg = MacAudio().list_devices()
    assert [m.index for m in res_neg["microphones"]] == [0, 1]
    assert res_neg["default_microphone"] == res_neg["microphones"][0]
    assert res_neg["names_by_index"] == {0: "Built-in Mic", 1: "Second Mic", 2: "Ucho Aggregate"}

    # Default pointing to Ucho aggregate falls back to first microphone.
    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sd(raw, default_device=(2, 0)))
    res_agg = MacAudio().list_devices()
    assert res_agg["default_microphone"] == res_agg["microphones"][0]

    # Integer default_device and None default_device.
    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sd(raw, default_device=1))
    assert MacAudio().list_devices()["default_microphone"].index == 1

    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sd(raw, default_device=(None, 0)))
    assert MacAudio().list_devices()["default_microphone"].index == 0


class _InputOutputPair:
    """Shape of `sounddevice._InputOutputPair`: indexable, but without `len()`."""

    def __init__(self, pair):
        self._pair = pair

    def __getitem__(self, index):
        return self._pair[index]


def test_list_devices_reads_real_sounddevice_default_pair(monkeypatch):
    fake = _make_fake_sd(_sample_devices(), default_device=_InputOutputPair((1, 2)))
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    result = MacAudio().list_devices()
    assert [m.index for m in result["microphones"]] == [0, 1]
    assert result["default_microphone"].index == 1


def test_list_devices_keeps_devices_when_default_is_unreadable(monkeypatch):
    class BrokenDefault:
        @property
        def device(self):
            raise RuntimeError("no default")

    fake = _make_fake_sd(_sample_devices())
    fake.default = BrokenDefault()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    result = MacAudio().list_devices()
    assert [m.index for m in result["microphones"]] == [0, 1]
    assert result["default_microphone"].index == 0


def test_list_devices_handles_errors_and_missing_sounddevice(monkeypatch, caplog):
    empty = {
        "microphones": [],
        "system": [],
        "default_microphone": None,
        "default_system": None,
        "names_by_index": {},
    }

    def _raise_pa():
        raise _FakePortAudioError("PortAudio not initialized")

    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sd(_raise_pa))
    with caplog.at_level(logging.WARNING, logger="ucho.platforms.macos.audio"):
        assert MacAudio().list_devices() == empty
    assert any("PortAudio not initialized" in r.message for r in caplog.records)

    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert MacAudio().list_devices() == empty


def test_open_input_arguments_callback_and_stream_lifecycle(monkeypatch):
    created_kwargs: list[dict] = []
    events: list[str] = []

    class FakeRawInputStream:
        def __init__(self, **kwargs):
            created_kwargs.append(kwargs)

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

        def close(self):
            events.append("close")

    fake = _make_fake_sd(stream_cls=FakeRawInputStream)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    received: list[bytes] = []
    dev = audio.DeviceInfo(4, "USB Mic", 2, 48_000, False)
    backend = mac_audio.create()
    assert backend.name == "coreaudio"

    stream = backend.open_input(dev, 2, 48_000, 1024, received.append, host="ignored")
    assert events == []
    assert len(created_kwargs) == 1
    kwargs = created_kwargs[0]
    assert kwargs["samplerate"] == 48_000
    assert kwargs["blocksize"] == 1024
    assert kwargs["device"] == 4
    assert kwargs["channels"] == 2
    assert kwargs["dtype"] == "int16"
    assert callable(kwargs["callback"])

    payload = np.array([100, -100, 200, -200], dtype=np.int16).tobytes()
    kwargs["callback"](memoryview(payload), 2, None, None)
    assert received == [payload]
    assert isinstance(received[0], bytes)

    stream.start()
    stream.stop()
    stream.close()
    stream.close()
    assert events == ["start", "stop", "close"]

    _check_contract(base.AudioBackend, backend)
    _check_contract(base.InputStream, stream)


def test_stream_close_safe_after_error_and_propagates(monkeypatch):
    close_calls = 0

    class FailingStream:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("stop boom")

        def close(self):
            nonlocal close_calls
            close_calls += 1
            raise RuntimeError("close boom")

    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sd(stream_cls=FailingStream))
    dev = audio.DeviceInfo(0, "Mic", 1, 16_000, False)
    stream = MacAudio().open_input(dev, 1, 16_000, 512, lambda _b: None)

    with pytest.raises(RuntimeError, match="stop boom"):
        stream.stop()
    with pytest.raises(RuntimeError, match="close boom"):
        stream.close()
    # Second close after error is a safe no-op.
    stream.close()
    assert close_calls == 1


def test_overflow_logging_throttled_and_callback_error_caught(monkeypatch, caplog):
    captured_cb = {}

    class FakeRawInputStream:
        def __init__(self, **kwargs):
            captured_cb["fn"] = kwargs["callback"]

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    clock = {"now": 100.0}
    monkeypatch.setattr(mac_audio.time, "monotonic", lambda: clock["now"])
    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sd(stream_cls=FakeRawInputStream))

    should_fail = {"flag": False}
    received: list[bytes] = []

    def on_data(data: bytes) -> None:
        if should_fail["flag"]:
            raise ValueError("consumer error")
        received.append(data)

    dev = audio.DeviceInfo(0, "Mic", 1, 16_000, False)
    MacAudio().open_input(dev, 1, 16_000, 512, on_data)
    cb = captured_cb["fn"]

    overflow = SimpleNamespace(input_overflow=True)
    with caplog.at_level(logging.WARNING, logger="ucho.platforms.macos.audio"):
        cb(b"\x01\x00", 1, None, overflow)
        clock["now"] = 105.0
        cb(b"\x02\x00", 1, None, overflow)
        clock["now"] = 109.9
        cb(b"\x03\x00", 1, None, overflow)
        overflow_logs = [r for r in caplog.records if r.message == "Audio input overflow"]
        assert len(overflow_logs) == 1

        clock["now"] = 110.0
        cb(b"\x04\x00", 1, None, overflow)
        overflow_logs = [r for r in caplog.records if r.message == "Audio input overflow"]
        assert len(overflow_logs) == 2

        should_fail["flag"] = True
        cb(b"\x05\x00", 1, None, None)
        cb(b"\x06\x00", 1, None, None)

    assert len(received) == 4


def test_permissions_with_and_without_avfoundation(monkeypatch):
    backend = MacAudio()
    assert backend.permission("system_audio") == "unsupported"
    backend.request_permission("system_audio")

    monkeypatch.setitem(sys.modules, "AVFoundation", None)
    assert backend.permission("microphone") == "unknown"
    backend.request_permission("microphone")

    status_holder = {"value": 3}
    requested: list[str] = []

    class FakeAVCaptureDevice:
        @staticmethod
        def authorizationStatusForMediaType_(media_type: str) -> int:
            assert media_type == "soun"
            return status_holder["value"]

        @staticmethod
        def requestAccessForMediaType_completionHandler_(media_type: str, handler) -> None:
            requested.append(media_type)
            handler(True)

    avf = types.ModuleType("AVFoundation")
    avf.AVCaptureDevice = FakeAVCaptureDevice
    monkeypatch.setitem(sys.modules, "AVFoundation", avf)

    cases = ((3, "granted"), (2, "denied"), (1, "denied"), (0, "unknown"), (9, "unknown"))
    for code, expected in cases:
        status_holder["value"] = code
        assert backend.permission("microphone") == expected

    backend.request_permission("microphone")
    assert requested == ["soun"]


def test_track_with_mac_audio_and_fake_sounddevice(monkeypatch):
    rng = np.random.default_rng(7)
    mono = [rng.integers(-4000, 4000, size=audio.BLOCK, dtype=np.int16) for _ in range(10)]
    blocks = [np.repeat(m, 2).tobytes() for m in mono]

    class FakeRawInputStream:
        def __init__(self, **kwargs):
            self._callback = kwargs["callback"]
            assert kwargs["samplerate"] == audio.TARGET_RATE
            assert kwargs["channels"] == 2
            assert kwargs["blocksize"] == audio.BLOCK
            assert kwargs["dtype"] == "int16"

        def start(self):
            for block in blocks:
                self._callback(memoryview(block), audio.BLOCK, None, None)

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sd(stream_cls=FakeRawInputStream))
    monkeypatch.setattr(platforms, "audio", lambda: MacAudio())

    device = audio.DeviceInfo(0, "Mac Mic", 2, audio.TARGET_RATE, False)
    with tempfile.TemporaryDirectory() as tmp_dir:
        flac_path = Path(tmp_dir) / "microphone.flac"
        track = audio._Track("microphone", flac_path, device)
        track.open_file()
        track.start(None)
        track.finish()

        samples, rate = sf.read(str(flac_path), dtype="int16")
    assert rate == audio.TARGET_RATE and samples.ndim == 1
    assert np.array_equal(samples, np.concatenate(mono))


@pytest.mark.skipif(sys.platform != "darwin", reason="real sounddevice and AVFoundation only on macOS")
def test_real_backend_on_macos_does_not_raise():
    pytest.importorskip("sounddevice")
    backend = MacAudio()
    result = backend.list_devices()  # the CI runner may have zero or more devices
    assert result["system"] == [] and result["default_system"] is None
    assert all(not m.name.strip().lower().startswith("ucho") for m in result["microphones"])
    if result["microphones"]:
        assert result["default_microphone"] in result["microphones"]
    assert backend.permission("microphone") in ("granted", "denied", "unknown", "unsupported")
