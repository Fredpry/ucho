# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""System audio on macOS: a Core Audio Process Tap of all apps (catap).

The tap records the mix every app plays (except Ucho itself, so its beeps
stay off the "Meeting" track), under the "System Audio Recording Only"
permission instead of "Screen Recording" (macOS 14.2 or later).

Why the start and stop run on worker threads with a time limit: on the GitHub
macOS runners `RecordingSession.start()` blocked forever inside Core Audio's
`AudioDeviceCreateIOProcID` in every variant tried (worker thread, main
thread with a run loop, with and without a TCC entry; docs/macos-spike.md).
Whether a real Mac without the permission blocks too, or delivers zeros, is
not known. A blocked call cannot be interrupted, so the thread is abandoned:
after `TAP_TIMEOUT_S` the stream reports a failure, the recording goes on with
the microphone alone, and no new tap is started until the blocked one returns
(each attempt would only leave another stuck thread behind). catap calls Core
Audio through ctypes, which releases the GIL, so a stuck call does not stall
the other threads (checked on the runner by tests/test_macos_tap.py).

Nothing here imports catap or PyObjC at module level (`ucho/platforms`
rule); catap is imported on the start thread, it takes about 0.6 s.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import platform
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from ...audio import DeviceInfo
from ...i18n import t

if TYPE_CHECKING:
    from ..base import Permission

log = logging.getLogger(__name__)

# How long a tap may take to start (or stop) before it is given up.
TAP_TIMEOUT_S = 10.0
# A tap that started but has not delivered a buffer within this time is
# treated as started anyway: from then on the recorder's "no data" watchdog
# decides (it restarts the stream like after sleep).
FIRST_BUFFER_S = 4.0
# The one system source. Index -1 is not a PortAudio index; the name is what
# Settings saves as `system_name`, so it is never translated.
SYSTEM_SOURCE_INDEX = -1
SYSTEM_SOURCE_NAME = "System audio (all apps)"
SYSTEM_SOURCE_RATE = 48_000
MIN_MACOS = (14, 2)

SessionFactory = Callable[[Callable[[Any], None]], Any]

_hung_lock = threading.Lock()
_hung_starts = 0  # tap starts past the time limit that have not returned yet


def system_source() -> DeviceInfo:
    """The system audio source for `list_devices()["system"]` (mono; the tap mixes down)."""
    return DeviceInfo(index=SYSTEM_SOURCE_INDEX, name=SYSTEM_SOURCE_NAME, channels=1,
                      rate=SYSTEM_SOURCE_RATE, loopback=True)


def is_system_source(device: DeviceInfo) -> bool:
    return bool(device.loopback) and device.index == SYSTEM_SOURCE_INDEX


def macos_version(release: str | None = None) -> tuple[int, ...]:
    """(14, 4) from "14.4"; () when unknown."""
    if release is None:
        try:
            release = platform.mac_ver()[0]
        except Exception:
            return ()
    try:
        return tuple(int(part) for part in release.split(".") if part)
    except ValueError:
        return ()


def supported(release: str | None = None) -> bool:
    """True when a process tap can exist here: macOS 14.2+ with catap installed.

    Cheap (no import of catap), so `list_devices()` may call it.
    """
    if sys.platform != "darwin":
        return False
    version = macos_version(release)
    if not version or version < MIN_MACOS:
        return False
    try:
        return importlib.util.find_spec("catap") is not None
    except (ImportError, ValueError):
        return False


def hung() -> bool:
    """True while an earlier tap start is stuck past its time limit."""
    with _hung_lock:
        return _hung_starts > 0


def _hung_add(delta: int) -> None:
    global _hung_starts
    with _hung_lock:
        _hung_starts = max(0, _hung_starts + delta)


# --- sample conversion (pure, tested on every system) -----------------------------------


def to_mono_float(data: bytes, channels: int, bits: int, is_float: bool) -> np.ndarray:
    """Interleaved native PCM (what catap delivers) as mono float32 in -1..1.

    catap allows float32 and signed 16/24/32-bit integers, little endian.
    A partial frame at the end is dropped; NaN and infinity become 0.
    """
    channels = max(1, int(channels))
    width = bits // 8
    if width <= 0 or bits % 8:
        raise ValueError(f"Unsupported sample width: {bits} bits")
    usable = (len(data) // (width * channels)) * width * channels
    raw = memoryview(data)[:usable]
    if is_float:
        if bits != 32:
            raise ValueError(f"Unsupported float width: {bits} bits")
        samples = np.frombuffer(raw, dtype="<f4").astype(np.float32)
        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    elif bits == 16:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif bits == 32:
        samples = (np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
    elif bits == 24:
        triples = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        value = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        value = np.where(value >= 1 << 23, value - (1 << 24), value)
        samples = value.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"Unsupported integer width: {bits} bits")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    return samples


def float_to_int16(samples: np.ndarray) -> bytes:
    """float32 -1..1 as int16 bytes, clipped (the inverse of `ucho.audio`'s / 32768)."""
    scaled = np.rint(np.asarray(samples, dtype=np.float32) * 32768.0)
    return np.clip(scaled, -32768, 32767).astype("<i2").tobytes()


# --- the stream ----------------------------------------------------------------------------


def _catap_session(on_buffer: Callable[[Any], None]) -> Any:
    """A catap system-audio session without Ucho's own process (runs on the start thread)."""
    import catap

    exclude = []
    try:
        own = catap.find_process_by_pid(os.getpid())
    except Exception as error:
        log.debug("Could not look up Ucho's own audio process: %s", error)
        own = None
    if own is not None:
        exclude.append(own)
    else:
        # Ucho has no Core Audio process object until it plays a sound, so its
        # beeps may reach the Meeting track (checked by hand, docs/macos-plan.md 7b).
        log.info("Ucho is not an audio process yet - its own sounds are not excluded from the tap")
    return catap.RecordingSession.from_system_audio(exclude=exclude, on_buffer=on_buffer)


class TapStream:
    """`InputStream` over a system-audio tap; never blocks the calling thread.

    `start()` returns at once. Until the tap runs and delivers, `starting` is
    True (the recorder does not count the track as dead). When the start fails
    or takes longer than `timeout_s`, `failure` holds the reason, the
    `on_failure` callback runs (on a worker thread) and the stream stays dead.
    Buffers are mixed to mono, resampled to `rate` when the tap runs at
    another rate, repeated to `channels` and passed on as int16 bytes.
    """

    def __init__(self, channels: int, rate: int, on_data: Callable[[bytes], None], *,
                 timeout_s: float | None = None, session_factory: SessionFactory | None = None,
                 on_failure: Callable[[str], None] | None = None) -> None:
        self._channels = max(1, int(channels))
        self._rate = int(rate)
        self._on_data = on_data
        self._timeout_s = TAP_TIMEOUT_S if timeout_s is None else timeout_s
        self._factory = session_factory or _catap_session
        self._on_failure = on_failure
        self._lock = threading.Lock()
        self._state = "new"  # new, starting, running, failed, closed
        self._session: Any = None
        self._timed_out = False
        self._running_since: float | None = None
        self._resampler: Any = None
        self._resampler_rate: float | None = None
        self._buffer_error_logged = False
        self.failure: str | None = None
        self.buffers = 0
        self.nonzero = False  # any non-zero sample: the permission evidently works
        self.started = threading.Event()  # set when running or failed
        self.stop_thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None

    # --- state ----------------------------------------------------------------

    @property
    def starting(self) -> bool:
        with self._lock:
            state = self._state
            since = self._running_since
        if state == "starting":
            return True
        if state == "running" and self.buffers == 0 and since is not None:
            return time.monotonic() - since < FIRST_BUFFER_S
        return False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state == "running"

    # --- InputStream ------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                return
            if hung():
                self._state = "failed"
                self.failure = t("System audio capture is not responding. Restart Ucho to record it again.")
            else:
                self._state = "starting"
        if self.failure is not None:
            self.started.set()
            raise OSError(self.failure)
        self._timer = threading.Timer(self._timeout_s, self._start_timeout)
        self._timer.daemon = True
        self._timer.name = "audio-tap-timer"
        self._timer.start()
        threading.Thread(target=self._start_worker, name="audio-tap-start", daemon=True).start()

    def stop(self) -> None:
        self._shutdown()

    def close(self) -> None:
        self._shutdown()

    # --- start and stop on worker threads ------------------------------------------

    def _start_worker(self) -> None:
        started_at = time.monotonic()
        session: Any = None
        error: Exception | None = None
        try:
            session = self._factory(self._on_buffer)
            session.start()
        except Exception as exc:  # OSError, catap's UnsupportedTapFormatError, ImportError...
            error = exc
        took = time.monotonic() - started_at
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            timed_out = self._timed_out
            keep = error is None and self._state == "starting"
            if keep:
                self._session = session
                self._state = "running"
                self._running_since = time.monotonic()
            elif error is not None and self._state == "starting":
                self._state = "failed"
                self.failure = f"{type(error).__name__}: {error}"
        if timed_out:
            _hung_add(-1)
            log.info("A system audio tap returned %.1f s after it was given up (%s)", took,
                     "failed" if error is not None else "closing it")
        if keep:
            log.info("System audio tap started in %.1f s", took)
            self.started.set()
            return
        if error is not None:
            if not timed_out:
                log.warning("System audio capture could not start: %s: %s", type(error).__name__, error)
                self.started.set()
                self._report_failure()
            return
        # Started after a timeout or after close(): nobody reads it any more.
        self._close_session(session)

    def _start_timeout(self) -> None:
        with self._lock:
            if self._state != "starting":
                return
            self._state = "failed"
            self._timed_out = True
            self.failure = f"the system audio tap did not start within {self._timeout_s:.0f} s"
        _hung_add(1)
        log.warning("System audio capture did not start within %.0f s - recording without it",
                    self._timeout_s)
        self.started.set()
        self._report_failure()

    def _report_failure(self) -> None:
        if self._on_failure is None or self.failure is None:
            return
        try:
            self._on_failure(self.failure)
        except Exception:
            log.exception("System audio failure callback failed")

    def _shutdown(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
            self._state = "closed"
            session, self._session = self._session, None
        self.started.set()
        if session is None:
            return  # never started, failed, or still starting (the start thread closes it)
        self.stop_thread = threading.Thread(target=self._close_session, args=(session,),
                                            name="audio-tap-stop", daemon=True)
        self.stop_thread.start()

    def _close_session(self, session: Any) -> None:
        """Stops a tap on the current (worker) thread; a watchdog timer logs a stuck stop."""
        done = threading.Event()

        def overdue() -> None:
            if not done.is_set():
                log.warning("Stopping the system audio tap takes longer than %.0f s - leaving it",
                            self._timeout_s)

        timer = threading.Timer(self._timeout_s, overdue)
        timer.daemon = True
        timer.start()
        try:
            session.close()
        except Exception as error:
            log.warning("Could not stop the system audio tap: %s: %s", type(error).__name__, error)
        finally:
            done.set()
            timer.cancel()

    # --- data -------------------------------------------------------------------------

    def _on_buffer(self, buffer: Any) -> None:
        """catap's worker thread: native PCM -> int16 at our rate and channel count."""
        with self._lock:
            if self._state not in ("starting", "running"):
                return
        try:
            fmt = buffer.format
            mono = to_mono_float(buffer.data, int(fmt.num_channels), int(fmt.bits_per_sample),
                                 bool(fmt.is_float))
            if not self.nonzero and mono.size and np.any(mono):
                self.nonzero = True
            mono = self._resample(mono, float(fmt.sample_rate))
            self.buffers += 1
            if mono.size == 0:
                return
            if self._channels > 1:
                mono = np.repeat(mono, self._channels)
            self._on_data(float_to_int16(mono))
        except Exception as error:
            if not self._buffer_error_logged:
                self._buffer_error_logged = True
                log.warning("System audio buffer dropped: %s: %s", type(error).__name__, error)

    def _resample(self, samples: np.ndarray, rate: float) -> np.ndarray:
        if not rate or round(rate) == self._rate:
            return samples
        if self._resampler is None or self._resampler_rate != rate:
            import soxr

            self._resampler = soxr.ResampleStream(rate, self._rate, 1, dtype="float32", quality="HQ")
            self._resampler_rate = rate
            log.info("System audio tap runs at %.0f Hz - resampling to %d Hz", rate, self._rate)
        return np.asarray(self._resampler.resample_chunk(samples), dtype=np.float32)


def probe(seconds: float = 1.0, timeout_s: float | None = None,
          session_factory: SessionFactory | None = None) -> Permission:
    """A short test tap: "granted" when it delivered non-zero samples, else "unknown".

    Blocks for up to `timeout_s + seconds`, so it runs on a worker thread
    (`MacPermissions.check_system_audio`). Zeros prove nothing (nothing may be
    playing), and a tap that does not start is "unknown" too.
    """
    if hung():
        return "unknown"
    timeout_s = TAP_TIMEOUT_S if timeout_s is None else timeout_s
    stream = TapStream(1, SYSTEM_SOURCE_RATE, lambda _data: None, timeout_s=timeout_s,
                       session_factory=session_factory)
    try:
        stream.start()
    except OSError:
        return "unknown"
    try:
        stream.started.wait(timeout_s + 1.0)
        if stream.running:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not stream.nonzero:
                time.sleep(0.05)
    finally:
        stream.close()
    return "granted" if stream.nonzero else "unknown"
