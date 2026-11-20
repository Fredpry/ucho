# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS text insertion and target-app inspection via Quartz events and AppKit.

A `WindowRef` on macOS is the pid of the frontmost application: keyboard
events go to the active app, not to a window, so the app is the target.
PyObjC (`AppKit`, `Quartz`, `ApplicationServices`) and the Carbon library are
loaded inside the functions, never at import time, so this module imports on
every system (tests run it on Windows with fakes).

Posting key events (`CGEventPost`) needs the Accessibility permission; without
it macOS drops the events silently, so `insert_text` checks `input_blocked()`
first and leaves the text on the clipboard instead.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..base import Permission, WindowRef

log = logging.getLogger(__name__)

# Virtual key codes (HIToolbox Events.h). The V key is its ANSI position: on a
# layout that moves V (Dvorak) ⌘ plus this key is another shortcut.
KVK_ANSI_V = 0x09
KVK_RETURN = 0x24
KVK_SPACE = 0x31
# CGEventKeyboardSetUnicodeString takes at most 20 UTF-16 units per event.
TYPE_CHUNK = 20
# CGEventFlags (CGEventTypes.h); the numbers keep the pure helpers free of PyObjC.
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_OPTION = 0x00080000
FLAG_COMMAND = 0x00100000
MODIFIER_FLAGS = FLAG_SHIFT | FLAG_CONTROL | FLAG_OPTION | FLAG_COMMAND
ACCESSIBILITY_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

# Results of `input_blocked()`.
NO_PERMISSION = "no_permission"
SECURE_INPUT = "secure_input"

FINDER = "com.apple.finder"
# Apps that are in front after a click on the Dock, the desktop or the menu
# bar, but that nobody dictates into (the macOS counterpart of the taskbar).
SHELL_BUNDLES = frozenset({
    "com.apple.dock",
    "com.apple.loginwindow",
    "com.apple.WindowManager",
    "com.apple.controlcenter",
    "com.apple.notificationcenterui",
})


# --- pure helpers (tested on every system) -------------------------------------------


def utf16_chunks(text: str, size: int = TYPE_CHUNK) -> list[str]:
    """Splits `text` into pieces of at most `size` UTF-16 units, never inside a surrogate pair."""
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if current and units + width > size:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def is_shell_app(bundle_id: str | None, has_windows: bool) -> bool:
    """True for the Dock, the login window and similar, and for Finder with no window open (the desktop)."""
    if bundle_id in SHELL_BUNDLES:
        return True
    return bundle_id == FINDER and not has_windows


def keys_held(flags: int, space_down: bool) -> bool:
    """True while a modifier (⇧⌃⌥⌘) or Space of the dictation hotkey is still pressed."""
    return bool(flags & MODIFIER_FLAGS) or space_down


# --- system calls ------------------------------------------------------------------


def settle(seconds: float) -> None:
    """Waits `seconds` while running the calling thread's run loop.

    AppKit state such as `NSWorkspace.frontmostApplication` and pasteboard
    data requests arrive through a run loop. In the app Tk runs the main one
    while dictation works on its own thread, whose run loop has no sources
    (it returns at once and this is a plain sleep); a caller on the main
    thread (tests, scripts) gets the main run loop serviced here.
    """
    if seconds <= 0:
        return
    start = time.monotonic()
    try:
        import objc
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop

        with objc.autorelease_pool():
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(seconds))
    except ImportError:
        pass
    left = seconds - (time.monotonic() - start)
    if left > 0.002:
        time.sleep(left)  # no input sources: the run loop returned at once


def frontmost_pid() -> int | None:
    """Pid of the active application, or None."""
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    pid = int(app.processIdentifier())
    return pid if pid > 0 else None


def _running_app(pid: int):
    from AppKit import NSRunningApplication

    return NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)


def has_normal_windows(pid: int) -> bool:
    """True when the process has an on-screen window at the normal level (not the desktop icons).

    Owner pid and layer are readable without the Screen Recording permission
    (only window titles need it).
    """
    import Quartz

    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    for window in windows:
        if int(window.get("kCGWindowOwnerPID", -1)) == pid and int(window.get("kCGWindowLayer", -1)) == 0:
            return True
    return False


def _post_key(code: int, flags: int = 0, text: str | None = None) -> None:
    """Key down and up for `code` with exactly `flags` (held keys do not leak in)."""
    import Quartz

    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(source, code, down)
        Quartz.CGEventSetFlags(event, flags)
        if text is not None:
            Quartz.CGEventKeyboardSetUnicodeString(event, utf16_length(text), text)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def paste_shortcut() -> None:
    _post_key(KVK_ANSI_V, FLAG_COMMAND)


def press_enter() -> None:
    _post_key(KVK_RETURN)


def type_text(text: str) -> None:
    """Types the text as Unicode characters: no clipboard, any keyboard layout."""
    for chunk in utf16_chunks(text):
        _post_key(0, 0, chunk)
        # In portions with a pause: a long burst can lose characters in a terminal.
        time.sleep(0.01)


def wait_for_keys_released(limit_s: float = 1.5) -> None:
    """⌘V while ⌃⇧ of the hotkey is still held would be another shortcut in many apps."""
    try:
        import Quartz
    except ImportError:
        return
    state = Quartz.kCGEventSourceStateHIDSystemState
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        try:
            flags = int(Quartz.CGEventSourceFlagsState(state))
            space = bool(Quartz.CGEventSourceKeyState(state, KVK_SPACE))
        except Exception:
            log.debug("Could not read the keyboard state", exc_info=True)
            return
        if not keys_held(flags, space):
            return
        time.sleep(0.02)


def accessibility_trusted() -> bool:
    from ApplicationServices import AXIsProcessTrusted

    return bool(AXIsProcessTrusted())


_carbon = None
_carbon_lock = threading.Lock()


def secure_input_enabled() -> bool:
    """True while a password field or Terminal's "Secure Keyboard Entry" holds the keyboard.

    macOS then keeps synthetic key events away from the focused field, so a
    paste or typing would be lost. `IsSecureEventInputEnabled` (Carbon) has
    no PyObjC wrapper; ctypes is enough for a function without arguments.
    """
    global _carbon
    with _carbon_lock:
        if _carbon is None:
            _carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
            _carbon.IsSecureEventInputEnabled.restype = ctypes.c_ubyte
            _carbon.IsSecureEventInputEnabled.argtypes = []
        library = _carbon
    return bool(library.IsSecureEventInputEnabled())


class MacInserter:
    """macOS `TextInserter` implementation (Quartz events, AppKit, Accessibility)."""

    def foreground(self) -> WindowRef | None:
        try:
            return frontmost_pid()
        except Exception:
            log.debug("Could not read the frontmost application", exc_info=True)
            return None

    def process_of(self, window: WindowRef | None) -> int | None:
        return int(window) if window else None

    def is_ours(self, window: WindowRef | None) -> bool:
        return bool(window) and window == os.getpid()

    def is_shell(self, window: WindowRef | None) -> bool:
        if not window:
            return False
        try:
            app = _running_app(window)
            if app is None:
                return False
            bundle_id = app.bundleIdentifier()
            bundle_id = str(bundle_id) if bundle_id is not None else None
            if bundle_id == FINDER:
                return is_shell_app(bundle_id, has_normal_windows(window))
            return is_shell_app(bundle_id, True)
        except Exception:
            log.debug("Could not inspect the application %s", window, exc_info=True)
            return False

    def exists(self, window: WindowRef) -> bool:
        if not window:
            return False
        try:
            app = _running_app(window)
            if app is not None:
                return not app.isTerminated()
        except Exception:
            log.debug("Could not look up the application %s", window, exc_info=True)
        # Not a registered app (a command-line process) or no AppKit: ask the kernel.
        if sys.platform == "win32":
            return False  # signal 0 is CTRL_C_EVENT there; this module only runs there in tests
        try:
            os.kill(window, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def activate(self, window: WindowRef) -> bool:
        """One attempt to bring the app back; macOS 14+ may refuse it (cooperative activation)."""
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps

            app = _running_app(window)
            if app is None:
                return False
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        except Exception:
            log.debug("Could not activate the application %s", window, exc_info=True)
            return False
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            if self.foreground() == window:
                return True
            settle(0.02)
        return self.foreground() == window

    def paste(self) -> None:
        paste_shortcut()

    def press_enter(self) -> None:
        press_enter()

    def type_text(self, text: str) -> None:
        type_text(text)

    def wait_keys_released(self, limit_s: float = 1.5) -> None:
        wait_for_keys_released(limit_s)

    def paste_label(self) -> str:
        return "⌘V"

    def permission(self) -> Permission:
        try:
            return "granted" if accessibility_trusted() else "denied"
        except Exception:
            log.debug("Could not read the Accessibility permission", exc_info=True)
            return "unknown"

    def request_permission(self) -> None:
        """Shows the system prompt that leads to Accessibility in System Settings."""
        try:
            from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        except Exception:
            log.warning("Could not ask for the Accessibility permission", exc_info=True)

    def input_blocked(self) -> str | None:
        """NO_PERMISSION, SECURE_INPUT or None: whether posted keys would be lost now.

        Not part of `base.TextInserter`: `dictation.input_blocked()` looks it
        up with getattr, other platforms never block.
        """
        if self.permission() == "denied":
            return NO_PERMISSION
        try:
            if secure_input_enabled():
                return SECURE_INPUT
        except Exception:
            log.debug("Could not check secure keyboard entry", exc_info=True)
        return None


def create() -> MacInserter:
    return MacInserter()
