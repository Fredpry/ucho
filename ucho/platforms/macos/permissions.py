# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS privacy permissions (TCC): microphone, system audio and Accessibility.

Used by the first-run wizard and Settings (M9), which refresh `state()`
every second while the permissions are on screen, so `state()` stays cheap:
one AVFoundation call, one `AXIsProcessTrusted` and one TCC preflight.

System audio has no public API for its state. `TCCAccessPreflight` (private,
from TCC.framework) answers 0 granted, 1 denied, 2 not decided; on the CI
runners it changed from 2 to 0 after a TCC.db entry (docs/macos-spike.md).
Being private SPI it is only a hint: here it can only say "denied". Only a
test tap that delivered non-zero samples says "granted" (`check_system_audio`,
on demand, never in the refresh loop); zeros prove nothing, since nothing may
be playing. The test tap is also what makes macOS show its prompt.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import audio_tap

if TYPE_CHECKING:
    from ..base import Permission

log = logging.getLogger(__name__)

KINDS = ("microphone", "system_audio", "accessibility")

_PANE = "x-apple.systempreferences:com.apple.preference.security?"
# System audio sits in "Screen & System Audio Recording" (macOS 15: list
# "System Audio Recording Only"); checked by hand on a Mac (plan 7b).
SETTINGS_URLS = {
    "microphone": _PANE + "Privacy_Microphone",
    "system_audio": _PANE + "Privacy_ScreenCapture",
    "accessibility": _PANE + "Privacy_Accessibility",
}

_TCC_PATH = "/System/Library/PrivateFrameworks/TCC.framework/TCC"
_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UTF8 = 0x08000100

_tcc_lock = threading.Lock()
_tcc: tuple[Any, Any] | None = None  # (TCCAccessPreflight, CoreFoundation)
_tcc_failed = False

_probe_lock = threading.Lock()
_probe_result: Permission | None = None
_probe_thread: threading.Thread | None = None


def _tcc_functions() -> tuple[Any, Any] | None:
    global _tcc, _tcc_failed
    with _tcc_lock:
        if _tcc is not None or _tcc_failed:
            return _tcc
        try:
            cf = ctypes.CDLL(_CF_PATH)
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            cf.CFRelease.restype = None
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            preflight = ctypes.CDLL(_TCC_PATH).TCCAccessPreflight
            preflight.restype = ctypes.c_int
            preflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        except (OSError, AttributeError) as error:
            _tcc_failed = True
            log.info("TCC preflight is not available: %s", error)
            return None
        _tcc = (preflight, cf)
        return _tcc


def tcc_preflight(service: str = "kTCCServiceAudioCapture") -> int | None:
    """Raw `TCCAccessPreflight(service)` (0 granted, 1 denied, 2 not decided); None when missing."""
    functions = _tcc_functions()
    if functions is None:
        return None
    preflight, cf = functions
    name = cf.CFStringCreateWithCString(None, service.encode("ascii"), _UTF8)
    if not name:
        return None
    try:
        return int(preflight(name, None))
    except Exception as error:
        log.debug("TCC preflight failed: %s", error)
        return None
    finally:
        cf.CFRelease(name)


def preflight_hint(code: int | None) -> Permission:
    """The only thing the private preflight may decide: an explicit "denied"."""
    return "denied" if code == 1 else "unknown"


def system_audio_state() -> Permission:
    """"denied" from the preflight, "granted" after a test tap heard sound, else "unknown"."""
    if not audio_tap.supported():
        return "unsupported"
    if preflight_hint(tcc_preflight()) == "denied":
        return "denied"
    with _probe_lock:
        return "granted" if _probe_result == "granted" else "unknown"


def probe_result() -> Permission | None:
    """The last test tap's answer, None before the first one."""
    with _probe_lock:
        return _probe_result


def check_system_audio(on_done: Callable[[Permission], None] | None = None) -> bool:
    """Starts a 1 s test tap on a worker thread; False when one is already running.

    `on_done(result)` runs on that worker thread (the UI posts it to Tk).
    A "granted" stays until the app quits; "unknown" never replaces it.
    """
    global _probe_thread
    if not audio_tap.supported():
        return False

    def run() -> None:
        global _probe_result
        try:
            result = audio_tap.probe()
        except Exception:
            log.exception("System audio check failed")
            result = "unknown"
        with _probe_lock:
            if _probe_result != "granted":
                _probe_result = result
            final = _probe_result
        log.info("System audio check: %s", result)
        if on_done is not None:
            try:
                on_done(final)
            except Exception:
                log.exception("System audio check callback failed")

    with _probe_lock:
        if _probe_thread is not None and _probe_thread.is_alive():
            return False
        _probe_thread = threading.Thread(target=run, name="audio-tap-check", daemon=True)
        _probe_thread.start()
    return True


def accessibility_state() -> Permission:
    try:
        from ApplicationServices import AXIsProcessTrusted

        return "granted" if AXIsProcessTrusted() else "denied"
    except Exception:
        return "unknown"


def request_accessibility() -> None:
    """Shows the system Accessibility prompt (it points the user to System Settings)."""
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception as error:
        log.debug("Could not request Accessibility permission: %s", error)


class MacPermissions:
    """`Permissions` for macOS (see the module docstring for what each state means)."""

    def state(self) -> dict[str, Permission]:
        from .audio import microphone_permission

        return {
            "microphone": microphone_permission(),
            "system_audio": system_audio_state(),
            "accessibility": accessibility_state(),
        }

    def request(self, kind: str) -> None:
        if kind == "microphone":
            from .audio import request_microphone

            request_microphone()
        elif kind == "system_audio":
            check_system_audio()
        elif kind == "accessibility":
            request_accessibility()
        else:
            log.debug("Unknown permission kind: %s", kind)

    def settings_url(self, kind: str) -> str | None:
        return SETTINGS_URLS.get(kind)

    def check_system_audio(self, on_done: Callable[[Permission], None] | None = None) -> bool:
        """The "Check" button: a test tap, see `check_system_audio`."""
        return check_system_audio(on_done)


def create() -> MacPermissions:
    return MacPermissions()
