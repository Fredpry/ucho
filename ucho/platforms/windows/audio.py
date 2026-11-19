# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Windows audio backend via PyAudioWPatch (WASAPI input and loopback)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import pyaudiowpatch as pyaudio

from ... import audio as _core
from ...audio import DeviceInfo, _from_info

if TYPE_CHECKING:
    from ..base import Permission

log = logging.getLogger(__name__)


def new_pyaudio():
    return _core.new_pyaudio()


def close_pyaudio(p) -> None:
    _core.close_pyaudio(p)


def _input_api(p) -> int:
    """The PortAudio host API microphones are listed from.

    PortAudio lists every microphone once per Windows audio API: MME (names cut
    to 31 characters, plus the "Microsoft Sound Mapper"), DirectSound (plus the
    "Primary Sound Capture Driver") and WASAPI. We list WASAPI only, the API the
    system-audio loopback uses too, so each device appears once with its full
    name. Without WASAPI we fall back to the default API.
    """
    try:
        return int(p.get_host_api_info_by_type(pyaudio.paWASAPI)["index"])
    except Exception:
        return int(p.get_default_host_api_info()["index"])


def microphones(p) -> tuple[list[DeviceInfo], DeviceInfo | None, dict[int, str]]:
    """(microphones, default microphone, names of all input devices by index).

    The names by index resolve a choice saved by earlier versions, which
    stored a PortAudio index (see `find_device`).
    """
    api = _input_api(p)
    found: list[DeviceInfo] = []
    names: dict[int, str] = {}
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
        except Exception:
            continue
        if int(info.get("maxInputChannels", 0)) <= 0:
            continue
        names[i] = str(info["name"])
        if info.get("isLoopbackDevice") or int(info.get("hostApi", -1)) != api:
            continue
        found.append(_from_info(info, loopback=False))

    default: DeviceInfo | None = None
    try:
        index = int(p.get_host_api_info_by_index(api)["defaultInputDevice"])
        default = next((d for d in found if d.index == index), None)
    except Exception:
        pass
    if default is None:
        try:
            default = _from_info(p.get_default_input_device_info(), loopback=False)
        except Exception as error:
            log.warning("No default microphone: %s", error)
    return found, default, names


def _device_map() -> tuple[list[DeviceInfo], list[DeviceInfo],
                           DeviceInfo | None, DeviceInfo | None, dict[int, str]]:
    """Returns (microphones, loopback_outputs, default_microphone, default_output, names).

    PortAudio caches the device list at initialization, so each time we create
    a fresh PyAudio object and close it right away.
    """
    loopbacks: list[DeviceInfo] = []
    default_sys: DeviceInfo | None = None

    p = new_pyaudio()
    try:
        try:
            for info in p.get_loopback_device_info_generator():
                loopbacks.append(_from_info(info, loopback=True))
        except Exception as error:  # no WASAPI or no output devices
            log.warning("Could not list loopback devices: %s", error)

        mics, default_mic, names = microphones(p)
        # A loopback is never offered as a microphone.
        loopback_indices = {d.index for d in loopbacks}
        mics = [d for d in mics if d.index not in loopback_indices]

        try:
            default_sys = _from_info(p.get_default_wasapi_loopback(), loopback=True)
        except Exception as error:
            log.warning("No default loopback device: %s", error)
    finally:
        close_pyaudio(p)

    return mics, loopbacks, default_mic, default_sys, names


class _PortAudioStream:
    """`InputStream` wrapper around a PyAudioWPatch stream."""

    def __init__(self, stream, owned_pa=None) -> None:
        self._stream = stream
        self._owned_pa = owned_pa

    def start(self) -> None:
        self._stream.start_stream()

    def stop(self) -> None:
        self._stream.stop_stream()

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            if self._owned_pa is not None:
                owned, self._owned_pa = self._owned_pa, None
                close_pyaudio(owned)


class WindowsAudio:
    """Win32 WASAPI `AudioBackend` implementation via PyAudioWPatch."""

    name = "wasapi"

    def list_devices(self) -> dict:
        mics, loopbacks, default_mic, default_sys, names = _device_map()
        return {
            "microphones": mics,
            "system": loopbacks,
            "default_microphone": default_mic,
            "default_system": default_sys,
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
    ) -> _PortAudioStream:
        owned_pa = None
        p: Any = host
        if p is None:
            p = new_pyaudio()
            owned_pa = p

        def callback(in_data, frame_count, time_info, status):
            on_data(in_data)
            return (None, pyaudio.paContinue)

        try:
            stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device.index,
                frames_per_buffer=frames,
                stream_callback=callback,
            )
        except BaseException:
            if owned_pa is not None:
                close_pyaudio(owned_pa)
            raise
        return _PortAudioStream(stream, owned_pa=owned_pa)

    def permission(self, kind: Literal["microphone", "system_audio"]) -> Permission:
        return "unsupported"

    def request_permission(self, kind: Literal["microphone", "system_audio"]) -> None:
        pass


def create() -> WindowsAudio:
    return WindowsAudio()
