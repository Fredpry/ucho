# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS system audio (process tap), PortAudio refresh and permissions.

The first part runs everywhere with stand-ins for catap and sounddevice. The
tests marked darwin run in the macos-tests job against the real catap,
PyObjC and PortAudio. On the GitHub runners the tap never starts (it blocks
in Core Audio, docs/macos-spike.md), so they check the time limit: the
recording goes on with the microphone, nothing blocks, the process exits.
Real tap data needs a real Mac (docs/macos-plan.md 7b-10).
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ucho import audio, platforms
from ucho.platforms import base
from ucho.platforms.macos import audio as mac_audio
from ucho.platforms.macos import audio_tap, permissions

ROOT = Path(__file__).resolve().parent.parent
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only: real Core Audio")


@pytest.fixture(autouse=True)
def _no_hung_taps(monkeypatch):
    monkeypatch.setattr(audio_tap, "_hung_starts", 0)
    monkeypatch.setattr(permissions, "_probe_result", None)
    monkeypatch.setattr(permissions, "_probe_thread", None)


def _wait(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# --- sample conversion ------------------------------------------------------------------


def _fmt(channels=2, bits=32, is_float=True, rate=48_000.0):
    return SimpleNamespace(num_channels=channels, bits_per_sample=bits, is_float=is_float,
                           sample_rate=rate)


def test_float32_stereo_to_mono():
    frames = np.array([[0.5, 0.25], [-1.0, 1.0], [0.1, 0.3]], dtype="<f4")
    mono = audio_tap.to_mono_float(frames.tobytes() + b"\x00\x00", 2, 32, True)
    assert mono.dtype == np.float32
    np.testing.assert_allclose(mono, [0.375, 0.0, 0.2], atol=1e-6)


def test_integer_formats_to_float():
    i16 = np.array([16384, -32768], dtype="<i2").tobytes()
    np.testing.assert_allclose(audio_tap.to_mono_float(i16, 1, 16, False), [0.5, -1.0])
    i32 = np.array([2**30, -(2**31)], dtype="<i4").tobytes()
    np.testing.assert_allclose(audio_tap.to_mono_float(i32, 1, 32, False), [0.5, -1.0])
    # 24-bit little endian: 0x400000 = 0.5, 0x800000 = -1.0, 0xFFFFFF = -1 LSB
    i24 = bytes([0x00, 0x00, 0x40, 0x00, 0x00, 0x80, 0xFF, 0xFF, 0xFF])
    np.testing.assert_allclose(audio_tap.to_mono_float(i24, 1, 24, False), [0.5, -1.0, -1 / 2**23])
    with pytest.raises(ValueError):
        audio_tap.to_mono_float(b"\x00" * 8, 1, 64, True)
    with pytest.raises(ValueError):
        audio_tap.to_mono_float(b"\x00" * 8, 1, 12, False)


def test_nan_and_infinity_become_safe():
    data = np.array([np.nan, np.inf, -np.inf, 0.5], dtype="<f4").tobytes()
    np.testing.assert_allclose(audio_tap.to_mono_float(data, 1, 32, True), [0.0, 1.0, -1.0, 0.5])


def test_float_to_int16_clips():
    out = np.frombuffer(audio_tap.float_to_int16(np.array([2.0, -2.0, 0.5, -1.0, 1.0, 0.0])),
                        dtype="<i2")
    assert out.tolist() == [32767, -32768, 16384, -32768, 32767, 0]
    # Round trip with ucho.audio's int16 -> float conversion.
    samples = np.linspace(-0.9, 0.9, 101, dtype=np.float32)
    back = np.frombuffer(audio_tap.float_to_int16(samples), dtype="<i2").astype(np.float32) / 32768
    np.testing.assert_allclose(back, samples, atol=1 / 32768)


def test_supported_needs_macos_14_2_and_catap(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert not audio_tap.supported("15.0")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(audio_tap.importlib.util, "find_spec", lambda name: object())
    assert not audio_tap.supported("14.1")
    assert not audio_tap.supported("")
    assert audio_tap.supported("14.2")
    assert audio_tap.supported("26.1")
    monkeypatch.setattr(audio_tap.importlib.util, "find_spec", lambda name: None)
    assert not audio_tap.supported("15.0")
    assert audio_tap.macos_version("15.7.1") == (15, 7, 1)
    assert audio_tap.macos_version("x") == ()


# --- the tap stream with a stand-in catap session ----------------------------------------


class FakeSession:
    """Stands in for catap.RecordingSession; `block` keeps start() stuck like on the runners."""

    def __init__(self, on_buffer, block: threading.Event | None = None, error: Exception | None = None):
        self.on_buffer = on_buffer
        self.block = block
        self.error = error
        self.started = threading.Event()
        self.closed = threading.Event()

    def start(self):
        if self.block is not None:
            self.block.wait()
        if self.error is not None:
            raise self.error
        self.started.set()

    def close(self):
        self.closed.set()

    def deliver(self, samples, fmt):
        self.on_buffer(SimpleNamespace(data=np.asarray(samples, dtype="<f4").tobytes(),
                                       frame_count=len(samples), format=fmt))


def _factory(sessions: list, **kwargs):
    def make(on_buffer):
        session = FakeSession(on_buffer, **kwargs)
        sessions.append(session)
        return session
    return make


def test_tap_stream_converts_buffers(monkeypatch):
    sessions: list[FakeSession] = []
    received: list[bytes] = []
    stream = audio_tap.TapStream(2, 48_000, received.append, session_factory=_factory(sessions))
    assert not stream.starting
    stream.start()
    assert stream.started.wait(2)
    assert stream.running and stream.failure is None
    assert stream.starting  # running, but no buffer yet
    session = sessions[0]
    session.deliver([0.0, 0.0, 0.0, 0.0], _fmt(channels=2))
    assert not stream.nonzero and not stream.starting
    session.deliver([0.5, 0.5, -0.25, -0.25], _fmt(channels=2))
    assert stream.nonzero
    assert np.frombuffer(received[1], dtype="<i2").tolist() == [16384, 16384, -8192, -8192]
    stream.stop()
    stream.close()
    assert session.closed.wait(2)
    session.deliver([0.5, 0.5], _fmt(channels=2))  # after stop: dropped
    assert len(received) == 2


def test_tap_stream_resamples_another_rate():
    pytest.importorskip("soxr", reason="soxr is installed with the app; the Windows test job has no soxr")
    sessions: list[FakeSession] = []
    received: list[bytes] = []
    stream = audio_tap.TapStream(1, 48_000, received.append, session_factory=_factory(sessions))
    stream.start()
    assert stream.started.wait(2)
    for _ in range(20):
        sessions[0].deliver(np.full(441 * 2, 0.25, dtype=np.float32), _fmt(channels=2, rate=44_100.0))
    total = sum(len(chunk) for chunk in received) // 2
    # 0.2 s at 48 kHz, minus what soxr keeps back as its filter delay (about 700 samples).
    assert 20 * 480 - 1_500 < total <= 20 * 480
    stream.close()


def test_tap_stream_start_error_is_a_failure_not_a_hang():
    sessions: list[FakeSession] = []
    failures: list[str] = []
    stream = audio_tap.TapStream(1, 48_000, lambda _b: None, on_failure=failures.append,
                                 session_factory=_factory(sessions, error=OSError("no tap")))
    stream.start()
    assert stream.started.wait(2)
    assert stream.failure == "OSError: no tap"
    assert failures == [stream.failure]
    assert not audio_tap.hung()
    assert not stream.starting


def test_tap_stream_start_that_hangs_times_out_without_blocking():
    block = threading.Event()
    sessions: list[FakeSession] = []
    failures: list[str] = []
    stream = audio_tap.TapStream(1, 48_000, lambda _b: None, timeout_s=0.3,
                                 on_failure=failures.append,
                                 session_factory=_factory(sessions, block=block))
    began = time.monotonic()
    stream.start()
    assert time.monotonic() - began < 0.2  # start() itself never waits
    assert stream.starting
    assert stream.started.wait(3)
    assert "did not start within" in stream.failure
    assert failures == [stream.failure]
    assert audio_tap.hung()

    # While the stuck start has not returned, no new tap is started.
    second = audio_tap.TapStream(1, 48_000, lambda _b: None, session_factory=_factory(sessions))
    with pytest.raises(OSError, match="not responding"):
        second.start()
    assert len(sessions) == 1
    assert second.failure is not None
    assert audio_tap.probe(session_factory=_factory(sessions)) == "unknown"

    # The stuck call returns late: the abandoned session is closed, new taps are allowed.
    block.set()
    assert sessions[0].closed.wait(2)
    assert _wait(lambda: not audio_tap.hung())
    stream.close()


def test_close_while_starting_closes_the_late_session():
    block = threading.Event()
    sessions: list[FakeSession] = []
    stream = audio_tap.TapStream(1, 48_000, lambda _b: None, timeout_s=5,
                                 session_factory=_factory(sessions, block=block))
    stream.start()
    stream.close()
    assert not stream.starting
    block.set()
    assert sessions[0].closed.wait(2)
    assert not audio_tap.hung()


def test_probe_answers(monkeypatch):
    def factory_with(samples):
        def make(on_buffer):
            session = FakeSession(on_buffer)
            original = session.start

            def start():
                original()
                session.deliver(samples, _fmt(channels=1))
            session.start = start
            return session
        return make

    assert audio_tap.probe(0.2, session_factory=factory_with([0.0] * 64)) == "unknown"
    assert audio_tap.probe(0.2, session_factory=factory_with([0.0, 0.1] * 32)) == "granted"
    block = threading.Event()
    began = time.monotonic()
    assert audio_tap.probe(0.2, timeout_s=0.3, session_factory=_factory([], block=block)) == "unknown"
    assert time.monotonic() - began < 3
    block.set()
    assert _wait(lambda: not audio_tap.hung())


# --- the backend: system source and PortAudio refresh -------------------------------------


class FakeRawInputStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def _fake_sd(devices: list[dict]):
    fake = types.ModuleType("sounddevice")
    fake.calls = []
    fake.default = SimpleNamespace(device=(0, 1))
    fake.query_devices = lambda: list(devices)
    fake.RawInputStream = FakeRawInputStream
    fake._terminate = lambda: fake.calls.append("terminate")
    fake._initialize = lambda: fake.calls.append("initialize")
    return fake


MIC = {"index": 0, "name": "Built-in Microphone", "max_input_channels": 1,
       "default_samplerate": 48_000.0}
HEADSET = {"index": 1, "name": "USB Headset", "max_input_channels": 1, "default_samplerate": 16_000.0}


def test_list_devices_has_the_system_source_where_supported(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd([MIC]))
    monkeypatch.setattr(audio_tap, "supported", lambda release=None: True)
    devices = mac_audio.MacAudio().list_devices()
    source = audio_tap.system_source()
    assert devices["system"] == [source]
    assert devices["default_system"] == source
    assert source == audio.DeviceInfo(-1, "System audio (all apps)", 1, 48_000, True)
    assert audio_tap.is_system_source(source)
    assert not audio_tap.is_system_source(audio.DeviceInfo(-1, "Mic", 1, 48_000, False))

    monkeypatch.setitem(sys.modules, "sounddevice", None)  # PortAudio missing: still the tap
    assert mac_audio.MacAudio().list_devices()["system"] == [source]

    monkeypatch.setattr(audio_tap, "supported", lambda release=None: False)
    assert mac_audio.MacAudio().list_devices()["system"] == []


def test_open_input_on_the_system_source_is_a_tap(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd([MIC]))
    stream = mac_audio.MacAudio().open_input(audio_tap.system_source(), 1, 48_000, 2048,
                                             lambda _b: None)
    assert isinstance(stream, audio_tap.TapStream)
    assert isinstance(stream, base.InputStream)
    assert not stream.running  # not started by open_input
    stream.close()


def test_portaudio_refresh_only_without_open_streams(monkeypatch):
    devices = [MIC]
    fake = _fake_sd(devices)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(mac_audio, "_REFRESH_INTERVAL_S", 0.0)
    backend = mac_audio.MacAudio()

    devices.append(HEADSET)  # plugged in: PortAudio sees it only after a restart
    names = [d.name for d in backend.list_devices()["microphones"]]
    assert fake.calls == ["terminate", "initialize"]
    assert names == ["Built-in Microphone", "USB Headset"]

    fake.calls.clear()
    mic = audio.DeviceInfo(0, "Built-in Microphone", 1, 48_000, False)
    first = backend.open_input(mic, 1, 48_000, 1024, lambda _b: None)
    second = backend.open_input(mic, 1, 48_000, 1024, lambda _b: None)
    backend.list_devices()
    first.close()
    first.close()  # counted once
    backend.list_devices()
    assert fake.calls == []  # a stream is still open: a restart would invalidate it
    second.close()
    backend.list_devices()
    assert fake.calls == ["terminate", "initialize"]

    # A tap is not a PortAudio stream and does not hold the refresh back.
    fake.calls.clear()
    tap = backend.open_input(audio_tap.system_source(), 1, 48_000, 2048, lambda _b: None)
    backend.list_devices()
    assert fake.calls == ["terminate", "initialize"]
    tap.close()


def test_portaudio_refresh_is_throttled_and_survives_errors(monkeypatch, caplog):
    fake = _fake_sd([MIC])
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    backend = mac_audio.MacAudio()
    backend.list_devices()
    backend.list_devices()
    assert fake.calls == ["terminate", "initialize"]  # the second one within 1 s is skipped

    monkeypatch.setattr(mac_audio, "_REFRESH_INTERVAL_S", 0.0)

    def broken():
        raise RuntimeError("PortAudio down")
    fake._initialize = broken
    backend.list_devices()  # logged, the list is still read
    assert "Could not restart PortAudio" in caplog.text
    fake._terminate = broken
    assert [d.name for d in backend.list_devices()["microphones"]] == ["Built-in Microphone"]

    del fake._terminate  # an old sounddevice without the private helpers
    assert backend._refresh(fake) is False


def test_recorder_records_the_microphone_when_the_tap_hangs(monkeypatch, tmp_path):
    """The whole path on any system: Recorder -> MacAudio -> TapStream stuck in start()."""
    block = threading.Event()
    sessions: list[FakeSession] = []

    class Stream(FakeRawInputStream):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            streams.append(self)

    streams: list[Stream] = []
    fake = _fake_sd([dict(MIC, default_samplerate=16_000.0)])  # no resampling: no soxr needed
    fake.RawInputStream = Stream
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(audio_tap, "supported", lambda release=None: True)
    monkeypatch.setattr(audio_tap, "_catap_session", _factory(sessions, block=block))
    monkeypatch.setattr(audio_tap, "TAP_TIMEOUT_S", 0.3)
    monkeypatch.setattr(audio_tap, "SYSTEM_SOURCE_RATE", 16_000)
    notices: list[tuple[str, str]] = []
    with platforms.override("macos"):
        rec = audio.Recorder(on_notice=lambda code, message: notices.append((code, message)))
        began = time.monotonic()
        rec.start(tmp_path)
        assert time.monotonic() - began < 1.0
        try:
            assert set(rec._tracks) == {"microphone", "system"}, rec.warnings
            assert _wait(lambda: rec._tracks["system"].failure is not None, 3)
            rec._drop_failed()
            callback = streams[0].kwargs["callback"]
            for _ in range(20):
                callback(np.full(4_800, 300, dtype=np.int16).tobytes(), 4_800, None, None)
            assert _wait(lambda: rec._tracks["microphone"].duration_s > 1.5)
        finally:
            began = time.monotonic()
            result = rec.stop()
            assert time.monotonic() - began < 2.0
    assert set(result["tracks"]) == {"microphone"}
    assert [code for code, _ in notices] == [audio.NOTICE_SYSTEM_FAILED]
    block.set()


# --- permissions ---------------------------------------------------------------------------


def test_permissions_contract_and_settings_urls():
    perms = permissions.create()
    assert isinstance(perms, base.Permissions)
    for kind, anchor in (("microphone", "Privacy_Microphone"),
                         ("system_audio", "Privacy_ScreenCapture"),
                         ("accessibility", "Privacy_Accessibility")):
        url = perms.settings_url(kind)
        assert url.startswith("x-apple.systempreferences:com.apple.preference.security?")
        assert url.endswith(anchor)
    assert perms.settings_url("camera") is None
    with platforms.override("macos"):
        assert type(platforms.permissions()).__name__ == "MacPermissions"


def test_permission_state_from_the_system_apis(monkeypatch):
    monkeypatch.setattr(audio_tap, "supported", lambda release=None: True)
    monkeypatch.setattr(permissions, "tcc_preflight", lambda service="": 2)
    avf = types.ModuleType("AVFoundation")
    avf.AVCaptureDevice = SimpleNamespace(authorizationStatusForMediaType_=lambda media: 3)
    monkeypatch.setitem(sys.modules, "AVFoundation", avf)
    trusted = {"value": False}
    app_services = types.ModuleType("ApplicationServices")
    app_services.AXIsProcessTrusted = lambda: trusted["value"]
    monkeypatch.setitem(sys.modules, "ApplicationServices", app_services)

    perms = permissions.MacPermissions()
    assert perms.state() == {"microphone": "granted", "system_audio": "unknown",
                             "accessibility": "denied"}
    trusted["value"] = True
    monkeypatch.setattr(permissions, "_probe_result", "granted")
    assert perms.state() == {"microphone": "granted", "system_audio": "granted",
                             "accessibility": "granted"}
    # An explicit "denied" from TCC wins over an earlier test tap.
    monkeypatch.setattr(permissions, "tcc_preflight", lambda service="": 1)
    assert perms.state()["system_audio"] == "denied"
    assert mac_audio.MacAudio().permission("system_audio") == "denied"

    monkeypatch.setitem(sys.modules, "ApplicationServices", None)
    assert perms.state()["accessibility"] == "unknown"
    monkeypatch.setattr(audio_tap, "supported", lambda release=None: False)
    assert perms.state()["system_audio"] == "unsupported"
    assert mac_audio.MacAudio().permission("system_audio") == "unsupported"


def test_preflight_is_only_a_hint():
    assert permissions.preflight_hint(1) == "denied"
    for code in (0, 2, None, 7):
        assert permissions.preflight_hint(code) == "unknown"


def test_tcc_preflight_missing_framework(monkeypatch):
    if sys.platform == "darwin":
        pytest.skip("Windows only: the TCC framework exists on macOS")
    monkeypatch.setattr(permissions, "_tcc", None)
    monkeypatch.setattr(permissions, "_tcc_failed", False)
    assert permissions.tcc_preflight() is None
    assert permissions._tcc_failed


def test_check_system_audio_runs_once_and_keeps_granted(monkeypatch):
    monkeypatch.setattr(audio_tap, "supported", lambda release=None: True)
    monkeypatch.setattr(permissions, "tcc_preflight", lambda service="": 2)
    answers = iter(["granted", "unknown"])
    gate = threading.Event()

    def fake_probe():
        gate.wait(2)
        return next(answers)

    monkeypatch.setattr(audio_tap, "probe", fake_probe)
    done: list[str] = []
    perms = permissions.MacPermissions()
    assert perms.check_system_audio(done.append)
    assert not perms.check_system_audio(done.append)  # one at a time
    gate.set()
    assert _wait(lambda: done == ["granted"])
    assert permissions.system_audio_state() == "granted"
    permissions._probe_thread.join(2)
    perms.request("system_audio")  # the "Allow" button runs the same check
    assert _wait(lambda: not permissions._probe_thread.is_alive())
    assert permissions.probe_result() == "granted"  # "unknown" does not replace it

    monkeypatch.setattr(audio_tap, "supported", lambda release=None: False)
    assert not perms.check_system_audio()


def test_requests_go_to_the_system_prompts(monkeypatch):
    calls: list[object] = []
    avf = types.ModuleType("AVFoundation")
    avf.AVCaptureDevice = SimpleNamespace(
        requestAccessForMediaType_completionHandler_=lambda media, handler: calls.append(media))
    monkeypatch.setitem(sys.modules, "AVFoundation", avf)
    app_services = types.ModuleType("ApplicationServices")
    app_services.kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"
    app_services.AXIsProcessTrustedWithOptions = lambda options: calls.append(options)
    monkeypatch.setitem(sys.modules, "ApplicationServices", app_services)

    perms = permissions.MacPermissions()
    perms.request("microphone")
    perms.request("accessibility")
    perms.request("camera")
    assert calls == ["soun", {"AXTrustedCheckOptionPrompt": True}]

    # A frozen app without the usage text would be killed by macOS: no prompt.
    calls.clear()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    foundation = types.ModuleType("Foundation")
    foundation.NSBundle = SimpleNamespace(mainBundle=lambda: SimpleNamespace(
        objectForInfoDictionaryKey_=lambda key: None))
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    perms.request("microphone")
    assert calls == []
    foundation.NSBundle = SimpleNamespace(mainBundle=lambda: SimpleNamespace(
        objectForInfoDictionaryKey_=lambda key: "Ucho records your voice."))
    perms.request("microphone")
    assert calls == ["soun"]


# --- macOS only: the real frameworks on the runner ------------------------------------


@darwin_only
def test_catap_and_the_process_tap_api_exist():
    import catap

    assert audio_tap.supported()
    assert hasattr(catap.RecordingSession, "from_system_audio")
    core_audio = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    assert hasattr(core_audio, "AudioHardwareCreateProcessTap")
    assert hasattr(core_audio, "AudioHardwareDestroyProcessTap")
    native = Path(catap.__file__).parent / "native" / "libcatap_coreaudio.dylib"
    assert native.is_file()  # M10 must bundle it
    devices = mac_audio.MacAudio().list_devices()
    assert devices["default_system"] == audio_tap.system_source()


@darwin_only
def test_real_permission_state_and_preflight():
    state = permissions.MacPermissions().state()
    assert set(state) == {"microphone", "system_audio", "accessibility"}
    assert set(state.values()) <= {"granted", "denied", "unknown"}
    assert permissions.tcc_preflight() in (0, 1, 2)
    assert permissions.tcc_preflight("kTCCServiceMicrophone") in (0, 1, 2)


@darwin_only
def test_real_portaudio_refresh(monkeypatch):
    sd = pytest.importorskip("sounddevice", reason="macos-tests installs sounddevice")
    monkeypatch.setattr(mac_audio, "_REFRESH_INTERVAL_S", 0.0)
    calls: list[str] = []
    terminate, initialize = sd._terminate, sd._initialize
    monkeypatch.setattr(sd, "_terminate", lambda: (calls.append("terminate"), terminate())[1])
    monkeypatch.setattr(sd, "_initialize", lambda: (calls.append("initialize"), initialize())[1])
    backend = mac_audio.MacAudio()
    before = [d.name for d in backend.list_devices()["microphones"]]
    after = backend.list_devices()
    assert [d.name for d in after["microphones"]] == before
    assert calls == ["terminate", "initialize"] * 2
    mic = after["default_microphone"]
    if mic is None:
        return  # a runner without an input device
    calls.clear()
    stream = backend.open_input(mic, 1, mic.rate, 1024, lambda _b: None)
    try:
        stream.start()
        backend.list_devices()
        assert calls == []
        stream.stop()
    finally:
        stream.close()
    backend.list_devices()
    assert calls == ["terminate", "initialize"]


_CHILD = textwrap.dedent("""
    import json, sys, threading, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from ucho import audio, platforms

    out = {"notices": []}
    ticks = []
    stop = threading.Event()

    def ticker():  # stands in for the Tk loop: must keep running
        while not stop.is_set():
            ticks.append(time.monotonic())
            time.sleep(0.05)

    threading.Thread(target=ticker, daemon=True).start()
    rec = audio.Recorder(tracks=sys.argv[3],
                         on_notice=lambda code, message: out["notices"].append(code))
    began = time.monotonic()
    rec.start(Path(sys.argv[2]))
    out["start_s"] = time.monotonic() - began
    out["tracks"] = sorted(rec._tracks)
    deadline = time.monotonic() + 16
    while time.monotonic() < deadline:
        system = rec._tracks.get("system")
        if system is None or system.abandoned or system.duration_s > 1:
            break
        time.sleep(0.2)
    system = rec._tracks.get("system")
    out["system_abandoned"] = bool(system is not None and system.abandoned)
    out["system_s"] = system.duration_s if system is not None else None
    began = time.monotonic()
    result = rec.stop()
    out["stop_s"] = time.monotonic() - began
    out["result_tracks"] = sorted(result["tracks"])
    stop.set()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    out["max_gap_s"] = max(gaps) if gaps else None
    print("RESULT " + json.dumps(out), flush=True)
""")


def _run_child(tmp_path, tracks: str) -> dict:
    began = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(ROOT), str(tmp_path / "rec"), tracks],
                          capture_output=True, text=True, timeout=90)
    took = time.monotonic() - began
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stderr[-3000:]
    out = json.loads(lines[-1].removeprefix("RESULT "))
    out["process_s"] = took
    print(out)
    return out


@darwin_only
def test_real_tap_never_blocks_the_app(tmp_path):
    """System audio only: on the runner the tap hangs; the app must not.

    The child process records with the real catap and exits on its own; a
    stuck start would show up as a timeout of the child (it would not exit)
    or as a long gap in the ticking thread.
    """
    out = _run_child(tmp_path, "system")
    assert out["start_s"] < 2.0
    assert out["stop_s"] < 3.0
    assert out["max_gap_s"] is not None and out["max_gap_s"] < 1.0
    assert out["tracks"] == ["system"]
    if out["system_abandoned"]:  # the runners: start blocks in Core Audio
        assert out["notices"] == [audio.NOTICE_SYSTEM_FAILED]
        assert out["result_tracks"] == []
    else:  # a tap that starts (a real Mac, or a future runner image)
        assert out["system_s"] > 1
        assert out["result_tracks"] == ["system"]
    assert out["process_s"] < 60


@darwin_only
def test_real_recording_goes_on_with_the_microphone(tmp_path):
    out = _run_child(tmp_path, "both")
    if "microphone" not in out["tracks"]:
        pytest.fail(f"no microphone on the runner: {out}")
    assert out["start_s"] < 3.0
    assert out["stop_s"] < 3.0
    assert out["max_gap_s"] < 1.0
    assert "microphone" in out["result_tracks"]
    if out["system_abandoned"]:
        assert out["result_tracks"] == ["microphone"]
        assert audio.NOTICE_SYSTEM_FAILED in out["notices"]
