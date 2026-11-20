# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS audio backend: microphones via sounddevice, system audio via a process tap.

Microphones go through PortAudio (sounddevice). The one system source,
"System audio (all apps)", is a Core Audio Process Tap (`audio_tap.py`).

PortAudio reads the device list once, when it initializes (on the first
`import sounddevice`), so a headset plugged in later would never show up and
a restart after a device change could open a stale index. `list_devices()`
therefore re-initializes PortAudio, but only while none of our PortAudio
streams is open: `Pa_Terminate` invalidates every open stream. Opening,
closing and the restart share `ucho.audio.PORTAUDIO_LOCK`, so no stream can
be created in the middle of it.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from ...audio import PORTAUDIO_LOCK, DeviceInfo, _from_info
from . import audio_tap

if TYPE_CHECKING:
    from ..base import Permission

log = logging.getLogger(__name__)

_OVERFLOW_LOG_INTERVAL_S = 10.0
# Re-initializing PortAudio costs some tens of ms; the list rarely changes faster.
_REFRESH_INTERVAL_S = 1.0


def _sd() -> Any:
    import sounddevice

    return sounddevice


def _empty_devices() -> dict:
    return {
        "microphones": [],
        "system": [],
        "default_microphone": None,
        "default_system": None,
        "names_by_index": {},
    }


def _default_input_index(sd: Any) -> int | None:
    """PortAudio index of the default input, or None.

    `sd.default.device` is a `sounddevice._InputOutputPair`: it supports
    indexing but not `len()`, so it is read with `[0]` only. Any failure here
    must not cost us the device list, so it only drops the default.
    """
    try:
        raw = sd.default.device
        if raw is None:
            return None
        if isinstance(raw, int) and not isinstance(raw, bool):
            idx = raw
        else:
            idx = raw[0]
        if idx is None:
            return None
        val = int(idx)
    except Exception as error:
        log.debug("Could not read the default input device: %s", error)
        return None
    return val if val >= 0 else None


def microphone_permission() -> Permission:
    """Microphone access from AVFoundation (also used by `permissions.py`)."""
    try:
        from AVFoundation import AVCaptureDevice

        status = int(AVCaptureDevice.authorizationStatusForMediaType_("soun"))
    except Exception:
        return "unknown"
    if status == 3:
        return "granted"
    if status in (1, 2):
        return "denied"
    return "unknown"


def _has_usage_description(key: str) -> bool:
    """False in a frozen app whose Info.plist lacks `key`.

    macOS kills a bundled app that asks for a protected resource without the
    matching usage text. Run from sources, the terminal (or Python.app) is the
    app macOS asks for, so the request is safe there.
    """
    if not getattr(sys, "frozen", False):
        return True
    try:
        from Foundation import NSBundle

        return NSBundle.mainBundle().objectForInfoDictionaryKey_(key) is not None
    except Exception:
        return False


def request_microphone() -> None:
    """Shows the system microphone prompt (once; later calls only return the answer)."""
    if not _has_usage_description("NSMicrophoneUsageDescription"):
        log.warning("Not asking for the microphone: NSMicrophoneUsageDescription is missing")
        return
    try:
        from AVFoundation import AVCaptureDevice

        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            "soun", lambda granted: None
        )
    except Exception as error:
        log.debug("Could not request microphone permission: %s", error)


class _MacStream:
    """`InputStream` wrapper around a `sounddevice.RawInputStream`."""

    def __init__(self, stream: Any, on_close: Callable[[], None] | None = None) -> None:
        self._stream: Any = stream
        self._on_close = on_close

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            with PORTAUDIO_LOCK:
                stream.close()
        finally:
            on_close, self._on_close = self._on_close, None
            if on_close is not None:
                on_close()


class MacAudio:
    """CoreAudio `AudioBackend` implementation via `sounddevice`."""

    name = "coreaudio"

    def __init__(self) -> None:
        self._open_streams = 0  # PortAudio streams created and not closed yet
        self._last_refresh = 0.0
        self._count_lock = threading.Lock()

    def _stream_closed(self) -> None:
        with self._count_lock:
            self._open_streams = max(0, self._open_streams - 1)

    def _refresh(self, sd: Any) -> bool:
        """Re-initializes PortAudio so it sees devices plugged in since; only with no open stream.

        Called with `PORTAUDIO_LOCK` held. Returns True when it re-initialized.
        """
        with self._count_lock:
            if self._open_streams:
                return False
        now = time.monotonic()
        if now - self._last_refresh < _REFRESH_INTERVAL_S:
            return False
        terminate = getattr(sd, "_terminate", None)
        initialize = getattr(sd, "_initialize", None)
        if not callable(terminate) or not callable(initialize):
            return False
        self._last_refresh = now
        try:
            terminate()
        except Exception as error:
            log.debug("Could not terminate PortAudio for a device refresh: %s", error)
            return False
        try:
            initialize()
        except Exception as error:
            # PortAudio stays down until the next successful refresh.
            log.warning("Could not restart PortAudio: %s", error)
            self._last_refresh = 0.0
            return False
        return True

    def list_devices(self) -> dict:
        try:
            with PORTAUDIO_LOCK:
                fresh_import = "sounddevice" not in sys.modules
                sd = _sd()
                if fresh_import:
                    self._last_refresh = time.monotonic()  # importing initialized it just now
                else:
                    self._refresh(sd)
                raw_devices = sd.query_devices()
                default_idx = _default_input_index(sd)
        except Exception as error:
            log.warning("Could not query audio devices: %s", error)
            return self._with_system(_empty_devices())

        mics: list[DeviceInfo] = []
        names: dict[int, str] = {}
        for pos, item in enumerate(raw_devices):
            try:
                info = item if isinstance(item, dict) else dict(item)
                max_in = int(info.get("max_input_channels", 0))
                if max_in <= 0:
                    continue
                raw_idx = info.get("index", pos)
                idx = pos if raw_idx is None else int(raw_idx)
                raw_name = info["name"]
                if raw_name is None:
                    continue
                name = str(raw_name)
                rate = info.get("default_samplerate", 48000)
                dev = _from_info(
                    {
                        "index": idx,
                        "name": name,
                        "maxInputChannels": max_in,
                        "defaultSampleRate": 48000 if rate is None else rate,
                    },
                    loopback=False,
                )
            except Exception:
                continue

            names[idx] = name
            if name.strip().lower().startswith("ucho"):
                continue
            mics.append(dev)

        default_mic: DeviceInfo | None = None
        if default_idx is not None:
            default_mic = next((d for d in mics if d.index == default_idx), None)
        if default_mic is None and mics:
            default_mic = mics[0]

        return self._with_system({
            "microphones": mics,
            "system": [],
            "default_microphone": default_mic,
            "default_system": None,
            "names_by_index": names,
        })

    @staticmethod
    def _with_system(devices: dict) -> dict:
        """Adds the process tap as the one system source where macOS supports it."""
        if audio_tap.supported():
            source = audio_tap.system_source()
            devices["system"] = [source]
            devices["default_system"] = source
        return devices

    def open_input(
        self,
        device: DeviceInfo,
        channels: int,
        rate: int,
        frames: int,
        on_data: Callable[[bytes], None],
        host: object | None = None,
    ) -> _MacStream:
        del host
        if audio_tap.is_system_source(device):
            return audio_tap.TapStream(channels, rate, on_data)
        sd = _sd()
        last_overflow: float | None = None
        last_error: float | None = None

        def callback(indata: Any, frame_count: int, time_info: Any, status: Any) -> None:
            nonlocal last_overflow, last_error
            del frame_count, time_info
            if status and getattr(status, "input_overflow", False):
                now = time.monotonic()
                if last_overflow is None or (now - last_overflow) >= _OVERFLOW_LOG_INTERVAL_S:
                    last_overflow = now
                    log.warning("Audio input overflow")
            try:
                on_data(bytes(indata))
            except Exception as error:
                now = time.monotonic()
                if last_error is None or (now - last_error) >= _OVERFLOW_LOG_INTERVAL_S:
                    last_error = now
                    log.warning("Audio input callback error: %s", error)

        with PORTAUDIO_LOCK:
            stream = sd.RawInputStream(
                samplerate=rate,
                blocksize=frames,
                device=device.index,
                channels=channels,
                dtype="int16",
                callback=callback,
            )
            with self._count_lock:
                self._open_streams += 1
        return _MacStream(stream, self._stream_closed)

    def permission(self, kind: Literal["microphone", "system_audio"]) -> Permission:
        if kind == "system_audio":
            if not audio_tap.supported():
                return "unsupported"
            from . import permissions

            return permissions.system_audio_state()
        if kind != "microphone":
            return "unknown"
        return microphone_permission()

    def request_permission(self, kind: Literal["microphone", "system_audio"]) -> None:
        if kind == "system_audio":
            if audio_tap.supported():
                from .. import permissions as accessor

                accessor().request("system_audio")
            return
        if kind == "microphone":
            request_microphone()


def create() -> MacAudio:
    return MacAudio()
