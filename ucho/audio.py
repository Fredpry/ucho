# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Parallel recording of two independent audio tracks.

The "microphone" track is a normal WASAPI input, the "system" track is WASAPI
loopback, i.e. whatever comes out of the speakers. Standard PortAudio does not
expose loopback on Windows, which is why we use the PyAudioWPatch fork.

Design assumptions:
  * the PortAudio callback does NOT touch the disk - it puts raw bytes in a
    queue and a separate thread writes. Blocking I/O in the callback causes
    gaps in the audio.
  * WASAPI returns garbage when asked for one channel, so we read stereo and
    mix down to mono ourselves.
  * each track is aligned to the wall clock by padding with silence. Without
    it two independent device clocks drift apart on a long recording and the
    timestamps of the two tracks stop matching.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .i18n import t
from .session import MEETING_FILE, MICROPHONE_FILE

try:
    import soxr
except ImportError:  # pragma: no cover - resampling is required
    soxr = None

import pyaudiowpatch as pyaudio

log = logging.getLogger(__name__)

# PortAudio's initialization and termination are not thread-safe. Since v0.2
# the meeting recorder, the device list and dictation live in one process,
# and two threads creating PyAudio at the same moment crashed it with an
# access violation (seen at startup: device listing + dictation warm-up).
PORTAUDIO_LOCK = threading.RLock()


def new_pyaudio() -> "pyaudio.PyAudio":
    with PORTAUDIO_LOCK:
        return pyaudio.PyAudio()


def close_pyaudio(p) -> None:
    if p is None:
        return
    with PORTAUDIO_LOCK:
        try:
            p.terminate()
        except Exception:
            pass


TARGET_RATE = 16_000
BLOCK = 2048
# Below this difference we do not align - small fluctuations are normal.
DRIFT_TOLERANCE_S = 0.3
# No data for this many seconds means the device disappeared.
DEVICE_TIMEOUT_S = 4.0


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    channels: int
    rate: int
    loopback: bool

    @property
    def label(self) -> str:
        suffix = " [" + t("system audio") + "]" if self.loopback else ""
        return f"{self.name}{suffix}"


def _from_info(info: dict, loopback: bool) -> DeviceInfo:
    return DeviceInfo(
        index=int(info["index"]),
        name=str(info["name"]),
        channels=max(1, int(info.get("maxInputChannels", 2))),
        rate=int(round(float(info.get("defaultSampleRate", 48000)))),
        loopback=loopback,
    )


def _device_map() -> tuple[list[DeviceInfo], list[DeviceInfo],
                           DeviceInfo | None, DeviceInfo | None]:
    """Returns (microphones, loopback_outputs, default_microphone, default_output).

    PortAudio caches the device list at initialization, so each time we create
    a fresh PyAudio object and close it right away.
    """
    microphones: list[DeviceInfo] = []
    loopbacks: list[DeviceInfo] = []
    default_mic: DeviceInfo | None = None
    default_sys: DeviceInfo | None = None

    p = new_pyaudio()
    try:
        loopback_indices = set()
        try:
            for info in p.get_loopback_device_info_generator():
                dev = _from_info(info, loopback=True)
                loopbacks.append(dev)
                loopback_indices.add(dev.index)
        except Exception as error:  # no WASAPI or no output devices
            log.warning("Could not list loopback devices: %s", error)

        for i in range(p.get_device_count()):
            try:
                info = p.get_device_info_by_index(i)
            except Exception:
                continue
            if int(info.get("maxInputChannels", 0)) <= 0:
                continue
            if i in loopback_indices:
                continue
            microphones.append(_from_info(info, loopback=False))

        try:
            default_mic = _from_info(p.get_default_input_device_info(), loopback=False)
        except Exception as error:
            log.warning("No default microphone: %s", error)

        try:
            default_sys = _from_info(p.get_default_wasapi_loopback(), loopback=True)
        except Exception as error:
            log.warning("No default loopback device: %s", error)
    finally:
        close_pyaudio(p)

    return microphones, loopbacks, default_mic, default_sys


def list_devices() -> dict:
    microphones, loopbacks, default_mic, default_sys = _device_map()
    return {
        "microphones": microphones,
        "system": loopbacks,
        "default_microphone": default_mic,
        "default_system": default_sys,
    }


def find_device(index: int | None, candidates: list[DeviceInfo],
                default: DeviceInfo | None) -> DeviceInfo | None:
    if index is not None:
        for dev in candidates:
            if dev.index == index:
                return dev
    return default


class _Track:
    """A single track: PortAudio stream -> queue -> 16 kHz mono FLAC file."""

    def __init__(self, name: str, file: Path, device: DeviceInfo):
        self.name = name
        self.file = file
        self.device = device
        self.level = 0.0
        self.error: str | None = None

        self._queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)
        self._stream = None
        self._out: sf.SoundFile | None = None
        self._resampler = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._samples_written = 0
        self._start_monotonic = 0.0
        self._last_data = 0.0
        self._width = 2  # paInt16
        self._channels = device.channels
        self._rate = device.rate

    # --- life cycle -----------------------------------------------------

    def open_file(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        # Incremental writing: the recording survives even a hard kill of the process.
        self._out = sf.SoundFile(
            str(self.file), mode="w", samplerate=TARGET_RATE,
            channels=1, subtype="PCM_16", format="FLAC",
        )

    def start(self, p: "pyaudio.PyAudio", device: DeviceInfo | None = None) -> None:
        if device is not None:
            self.device = device
            self._channels = device.channels
            self._rate = device.rate

        self._build_resampler()
        self._start_monotonic = self._start_monotonic or time.monotonic()
        self._last_data = time.monotonic()

        self._stream = p.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._rate,
            input=True,
            input_device_index=self.device.index,
            frames_per_buffer=BLOCK,
            stream_callback=self._callback,
        )
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._write_loop, name=f"write-{self.name}", daemon=True
            )
            self._thread.start()
        self._stream.start_stream()

    def _build_resampler(self) -> None:
        if self._rate == TARGET_RATE:
            self._resampler = None
            return
        if soxr is None:
            raise RuntimeError(t("The soxr library is missing — run install.ps1 again"))
        self._resampler = soxr.ResampleStream(
            self._rate, TARGET_RATE, 1,
            dtype="float32", quality="HQ",
        )

    def stop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop_stream()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def finish(self) -> None:
        self.stop_stream()
        # No sentinel: the write thread must finish what is left in the queue.
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        if self._out is not None:
            try:
                self._out.close()
            except Exception:
                pass
            self._out = None

    # --- processing -----------------------------------------------------

    def _callback(self, in_data, frame_count, time_info, status):  # noqa: ARG002
        try:
            self._queue.put_nowait(in_data)
        except queue.Full:
            # Disk slower than the stream - drop a block instead of blocking audio.
            log.warning("Queue %s full, block dropped", self.name)
        return (None, pyaudio.paContinue)

    def _write_loop(self) -> None:
        while True:
            try:
                data = self._queue.get(timeout=0.25)
            except queue.Empty:
                # We stop only when the queue is empty - otherwise we would lose
                # the last blocks still waiting to be written.
                if self._stop.is_set():
                    break
                continue
            if data is None:
                break
            try:
                self._write_block(data)
            except Exception as error:  # one block must not kill the recording
                log.exception("Error writing track %s: %s", self.name, error)
                self.error = str(error)

        # Flush the resampler so the tail of the buffer is not lost.
        if self._resampler is not None and self._out is not None:
            try:
                tail = self._resampler.resample_chunk(
                    np.zeros(0, dtype=np.float32), last=True
                )
                if tail is not None and len(tail):
                    self._out.write(np.asarray(tail, dtype=np.float32))
            except Exception:
                pass

    def _write_block(self, data: bytes) -> None:
        if self._out is None:
            return
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if self._channels > 1:
            usable = (len(samples) // self._channels) * self._channels
            samples = samples[:usable].reshape(-1, self._channels).mean(axis=1)

        if samples.size == 0:
            return

        self._last_data = time.monotonic()
        rms = float(np.sqrt(np.mean(np.square(samples))))
        # Decibel scale 0..1: -60 dBFS is silence, 0 dBFS is the maximum.
        db = 20.0 * np.log10(rms + 1e-9)
        self.level = float(min(1.0, max(0.0, (db + 60.0) / 60.0)))

        if self._resampler is not None:
            out = self._resampler.resample_chunk(samples)
            if out is None or len(out) == 0:
                return
            out = np.asarray(out, dtype=np.float32)
        else:
            out = samples

        self._align_to_clock()
        self._out.write(out)
        self._samples_written += len(out)

    def _align_to_clock(self) -> None:
        """Pads with silence when the track fell behind real time.

        Never cuts - better half a second too much than timestamps that drift
        apart between the tracks.
        """
        if self._out is None or self._start_monotonic == 0.0:
            return
        expected = (time.monotonic() - self._start_monotonic) * TARGET_RATE
        missing = int(expected - self._samples_written)
        if missing > DRIFT_TOLERANCE_S * TARGET_RATE:
            self._out.write(np.zeros(missing, dtype=np.float32))
            self._samples_written += missing

    @property
    def dead(self) -> bool:
        return (time.monotonic() - self._last_data) > DEVICE_TIMEOUT_S

    @property
    def duration_s(self) -> float:
        return self._samples_written / TARGET_RATE


class Recorder:
    """Manages both tracks and makes sure they survive a device change."""

    def __init__(self, tracks: str = "both", microphone_index: int | None = None,
                 system_index: int | None = None):
        self.tracks = tracks
        self._microphone_index = microphone_index
        self._system_index = system_index
        self._pa: "pyaudio.PyAudio | None" = None
        self._tracks: dict[str, _Track] = {}
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.folder: Path | None = None
        self.start_ts = 0.0
        self.warnings: list[str] = []

    # --- API ------------------------------------------------------------

    def start(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.warnings.clear()

        devices = list_devices()
        chosen: list[tuple[str, str, DeviceInfo]] = []

        if self.tracks in ("both", "microphone"):
            mic = find_device(self._microphone_index, devices["microphones"],
                              devices["default_microphone"])
            if mic is None:
                self.warnings.append(t("No microphone found."))
            else:
                chosen.append(("microphone", MICROPHONE_FILE, mic))

        if self.tracks in ("both", "system"):
            sys_dev = find_device(self._system_index, devices["system"],
                                  devices["default_system"])
            if sys_dev is None:
                self.warnings.append(
                    t("No loopback device found — system audio will not be recorded.")
                )
            else:
                chosen.append(("system", MEETING_FILE, sys_dev))

        if not chosen:
            raise RuntimeError(t("No audio devices available for recording."))

        self._pa = new_pyaudio()
        self.start_ts = time.time()

        for name, file, dev in chosen:
            track = _Track(name, folder / file, dev)
            track.open_file()
            try:
                track.start(self._pa)
            except Exception as error:
                track.finish()
                self.warnings.append(t("Could not open “{name}”: {error}", name=dev.name, error=error))
                continue
            self._tracks[name] = track

        if not self._tracks:
            close_pyaudio(self._pa)
            self._pa = None
            raise RuntimeError(t("Could not start any recording track."))

        self._stop.clear()
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                          name="audio-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=3.0)
            self._watchdog = None

        with self._lock:
            for track in self._tracks.values():
                track.finish()
            close_pyaudio(self._pa)
            self._pa = None

        result = {
            "folder": self.folder,
            "duration_s": max((s.duration_s for s in self._tracks.values()), default=0.0),
            "tracks": {
                name: {
                    "file": s.file,
                    "device": s.device.name,
                    "duration_s": s.duration_s,
                }
                for name, s in self._tracks.items()
                if s.file.exists() and s.file.stat().st_size > 0
            },
            "warnings": list(self.warnings),
        }
        self._tracks.clear()
        return result

    @property
    def levels(self) -> dict[str, float]:
        return {name: s.level for name, s in self._tracks.items()}

    @property
    def duration_s(self) -> float:
        if not self.start_ts:
            return 0.0
        return time.time() - self.start_ts

    # --- surviving a device change --------------------------------------

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(2.0):
            dead = [n for n, s in self._tracks.items() if s.dead]
            if dead:
                log.warning("Tracks without data: %s - restarting streams", dead)
                self._restart_streams()

    def _restart_streams(self) -> None:
        """Full PortAudio restart - otherwise it does not see a new default device."""
        with self._lock:
            if self._pa is None:
                return
            for track in self._tracks.values():
                track.stop_stream()
            close_pyaudio(self._pa)

            devices = list_devices()
            self._pa = new_pyaudio()
            for name, track in self._tracks.items():
                if name == "microphone":
                    new = find_device(self._microphone_index, devices["microphones"],
                                      devices["default_microphone"])
                else:
                    new = find_device(self._system_index, devices["system"],
                                      devices["default_system"])
                if new is None:
                    continue
                try:
                    track.start(self._pa, new)
                    log.info("Track %s resumed on '%s'", name, new.name)
                except Exception as error:
                    log.error("Could not resume track %s: %s", name, error)
