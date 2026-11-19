# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS audio backend via sounddevice (CoreAudio microphone input)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from ...audio import DeviceInfo, _from_info

if TYPE_CHECKING:
    from ..base import Permission

log = logging.getLogger(__name__)

_OVERFLOW_LOG_INTERVAL_S = 10.0


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
    raw = sd.default.device
    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        idx = raw
    else:
        idx = raw[0] if len(raw) > 0 else None
    if idx is None:
        return None
    val = int(idx)
    return val if val >= 0 else None


class _MacStream:
    """`InputStream` wrapper around a `sounddevice.RawInputStream`."""

    def __init__(self, stream: Any) -> None:
        self._stream: Any = stream

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()


class MacAudio:
    """CoreAudio `AudioBackend` implementation via `sounddevice`."""

    name = "coreaudio"

    def list_devices(self) -> dict:
        try:
            sd = _sd()
            raw_devices = sd.query_devices()
            default_idx = _default_input_index(sd)
        except Exception as error:
            log.warning("Could not query audio devices: %s", error)
            return _empty_devices()

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

        return {
            "microphones": mics,
            "system": [],
            "default_microphone": default_mic,
            "default_system": None,
            "names_by_index": names,
        }

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

        stream = sd.RawInputStream(
            samplerate=rate,
            blocksize=frames,
            device=device.index,
            channels=channels,
            dtype="int16",
            callback=callback,
        )
        return _MacStream(stream)

    def permission(self, kind: Literal["microphone", "system_audio"]) -> Permission:
        if kind == "system_audio":
            return "unsupported"
        if kind != "microphone":
            return "unknown"
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

    def request_permission(self, kind: Literal["microphone", "system_audio"]) -> None:
        if kind != "microphone":
            return
        try:
            from AVFoundation import AVCaptureDevice

            AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                "soun", lambda granted: None
            )
        except Exception as error:
            log.debug("Could not request microphone permission: %s", error)


def create() -> MacAudio:
    return MacAudio()
