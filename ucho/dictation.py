# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dictation mode: you speak, the text lands where the cursor is.

Ctrl+Shift+Space starts recording, a second press ends it, Esc cancels. The
recording goes through the shared engine ahead of any meeting transcription
(ucho/engine.py) and the result is pasted into the active window: a terminal,
a browser, anything.

A few decisions that are not obvious:
  * Esc is registered only while recording; otherwise we do not take it
    away from other programs (ucho/hotkeys.py).
  * The model can stay loaded ("Keep the dictation model loaded"): loading
    takes seconds, and dictation has to start at once.
  * When the active window belongs to Ucho itself, the text only goes to the
    clipboard: pasting into our own window would land in the wrong place.
  * The window that was active when the dictation started is the target. If
    another window is active at paste time and the target cannot be brought
    back, the text stays on the clipboard instead of landing in the wrong
    window.
  * A dictation started on the desktop or the taskbar, or with only the
    desktop or the taskbar active at paste time, stays on the clipboard:
    Ctrl+V there goes nowhere.
  * Paste mode borrows the clipboard: the dictated text is marked so that
    clipboard history (Win+V) and cloud clipboard sync skip it, and is handed
    over with delayed rendering. The previous text is put back only after the
    target window's process has taken the dictation (see `DelayedClipboard`);
    when in doubt the dictation stays on the clipboard instead.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import c_void_p, wintypes
from typing import Callable

import numpy as np

from . import config, live, transcribe
from .i18n import t

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
# People press the hotkey just as they finish the last word; without this
# tail the end of the utterance is sometimes cut off.
TAIL_S = 0.25
LIVE_TICK_MS = 250


# --- WinAPI: clipboard and keyboard -------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.SetClipboardData.argtypes = [wintypes.UINT, c_void_p]
_user32.SetClipboardData.restype = c_void_p
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = c_void_p
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.GetClipboardSequenceNumber.argtypes = []
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = c_void_p
_kernel32.GlobalLock.argtypes = [c_void_p]
_kernel32.GlobalLock.restype = c_void_p
_kernel32.GlobalUnlock.argtypes = [c_void_p]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [c_void_p]
_kernel32.GlobalFree.restype = c_void_p
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE

# The hidden window that owns the clipboard while a dictation is pasted
# (`DelayedClipboard`). Handles and LRESULT are pointer-sized on 64-bit Python.
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HICON), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


_user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                 wintypes.UINT, wintypes.UINT]
_user32.PeekMessageW.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.TranslateMessage.restype = wintypes.BOOL
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, c_void_p, wintypes.BOOL,
                                              wintypes.DWORD, wintypes.DWORD]
_user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
_user32.GetOpenClipboardWindow.argtypes = []
_user32.GetOpenClipboardWindow.restype = wintypes.HWND
_user32.GetClipboardOwner.argtypes = []
_user32.GetClipboardOwner.restype = wintypes.HWND

VK_SPACE = 0x20
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_V = 0x56
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
WM_RENDERFORMAT = 0x0305
WM_RENDERALLFORMATS = 0x0306
WM_DESTROYCLIPBOARD = 0x0307
HWND_MESSAGE = -3
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
ERROR_CLASS_ALREADY_EXISTS = 1410


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    # Needed only so that the union has the right size.
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    event = _INPUT(type=INPUT_KEYBOARD)
    event.u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return event


def _send(events: list[_INPUT]) -> None:
    array = (_INPUT * len(events))(*events)
    _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))


def wait_for_keys_released(limit_s: float = 1.5) -> None:
    """Ctrl+V while Shift is still held means something else in many programs."""
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        if not any(_user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_SPACE)):
            return
        time.sleep(0.02)


# Registered formats that keep the text out of clipboard history (Win+V),
# cloud clipboard sync and clipboard monitors ("Cloud Clipboard and Clipboard
# History Formats" in the Windows docs). The first counts by its presence,
# the other two need a DWORD 0.
PRIVATE_FORMATS = {
    "ExcludeClipboardContentFromMonitorProcessing": 0,
    "CanIncludeInClipboardHistory": 0,
    "CanUploadToCloudClipboard": 0,
}
# How long model_key() trusts its last look at the model folder.
MODEL_CHECK_S = 5.0
# How long the target window has to ask for the pasted text after Ctrl+V.
# A busy Electron app or a remote desktop session can take seconds; without
# a request in this time the text stays on the clipboard and nothing is restored.
PASTE_TIMEOUT_S = 5.0
# After the target has taken the text, before the previous text goes back.
# Some programs read the clipboard more than once for one paste; the later
# reads get the cached text only while the clipboard is still ours.
RESTORE_GRACE_S = 1.0
# Enter mode: between the target taking the text and Enter (a terminal must
# digest the paste first, otherwise Enter overtakes it).
ENTER_DELAY_S = 0.25


def _open_clipboard(owner: int | None = None) -> None:
    for _ in range(20):
        if _user32.OpenClipboard(owner):
            return
        time.sleep(0.05)  # the clipboard is sometimes briefly held by another program
    raise RuntimeError(t("The clipboard is in use by another program"))


def _put(format_: int, data: bytes) -> None:
    """SetClipboardData with a moveable copy of `data`; the clipboard must be open."""
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not handle:
        raise MemoryError("GlobalAlloc")
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        _kernel32.GlobalFree(handle)
        raise MemoryError("GlobalLock")
    ctypes.memmove(pointer, data, len(data))
    _kernel32.GlobalUnlock(handle)
    # On success the system owns the memory; on failure it is still ours.
    if not _user32.SetClipboardData(format_, handle):
        _kernel32.GlobalFree(handle)
        raise RuntimeError("SetClipboardData failed")


def set_clipboard(text: str | None, private: bool = True) -> int:
    """Puts `text` on the clipboard (None empties it). Returns the clipboard sequence number.

    `private` adds the formats that keep the text out of clipboard history and
    cloud sync. Everything Ucho puts on the clipboard is a dictation or a
    transcript, so that is the default.
    """
    _open_clipboard()
    try:
        _user32.EmptyClipboard()
        if text is not None:
            _put(CF_UNICODETEXT, _utf16(text))
            if private:
                _put_private_formats()
    finally:
        _user32.CloseClipboard()
    return clipboard_sequence()


def _utf16(text: str) -> bytes:
    return (text + "\0").encode("utf-16-le")


def _put_private_formats() -> None:
    """The formats that keep the clipboard out of history and cloud sync; the clipboard must be open."""
    for name, value in PRIVATE_FORMATS.items():
        format_ = _user32.RegisterClipboardFormatW(name)
        try:
            if not format_:
                raise RuntimeError("RegisterClipboardFormatW failed")
            _put(format_, value.to_bytes(4, "little"))
        except (MemoryError, RuntimeError):
            log.warning("Could not set the clipboard format %s", name)


def get_clipboard_text() -> str | None:
    """The clipboard text (CF_UNICODETEXT), or None when the clipboard holds no text."""
    if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    _open_clipboard()
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def clipboard_sequence() -> int:
    return int(_user32.GetClipboardSequenceNumber())


# --- delayed rendering: the previous clipboard comes back only after the paste ---------
#
# Restoring the previous clipboard a fixed time after Ctrl+V was not safe: a
# slow target (remote desktop, Citrix, a busy Electron app, a CPU busy with a
# transcription) reads the clipboard later and pastes the previous text, maybe
# a password, and in Enter mode sends it. So the dictated text is put on the
# clipboard with delayed rendering: a hidden window of ours owns the clipboard,
# Windows asks it for the text (WM_RENDERFORMAT) when a program pastes, and
# only then do we know the paste happened and who took it. The previous text
# comes back only when the target window's own process took the dictation and
# nobody has written to the clipboard since; in every other case (no request
# in PASTE_TIMEOUT_S, another process asked, the clipboard changed) the
# dictated text stays and the previous text is not restored.

_CLASS_NAME = "UchoClipboardOwner"
_owners: dict[int, "DelayedClipboard"] = {}  # hidden window -> its clipboard offer
_class_lock = threading.Lock()
_class_registered = False


@WNDPROC
def _wndproc(hwnd, message, wparam, lparam):
    # Kept alive as a module global: Windows calls it for as long as the class exists.
    owner = _owners.get(hwnd or 0)
    if owner is not None and message in (WM_RENDERFORMAT, WM_RENDERALLFORMATS, WM_DESTROYCLIPBOARD):
        try:
            if message == WM_RENDERFORMAT:
                owner._render(int(wparam))
            elif message == WM_RENDERALLFORMATS:
                owner._render_all()
            else:
                owner.lost = True
        except Exception:
            log.warning("Clipboard message %#x failed", message, exc_info=True)
        return 0
    return _user32.DefWindowProcW(hwnd, message, wparam, lparam)


def _register_class() -> None:
    global _class_registered
    with _class_lock:
        if _class_registered:
            return
        wc = _WNDCLASSW()
        wc.lpfnWndProc = _wndproc
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.lpszClassName = _CLASS_NAME
        if not _user32.RegisterClassW(ctypes.byref(wc)) and \
                ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
        _class_registered = True


class DelayedClipboard:
    """The dictated text on the clipboard, handed over only when a program asks for it.

    Everything runs on the calling thread, which owns the hidden window: it
    must call `pump` while it waits, so that Windows can deliver the requests.
    `close` always follows (try/finally): when nobody has asked for the text
    yet, it is rendered into the clipboard so it stays there for Ctrl+V.
    """

    def __init__(self, text: str, consumer_pid: int | None):
        self.text = text
        self.consumer_pid = consumer_pid  # the process expected to paste
        self.hwnd: int | None = None
        self.rendered = False   # a program asked for the text
        self.confirmed = False  # ... and it was the consumer's process
        self.lost = False       # another program emptied the clipboard

    def offer(self) -> None:
        """Takes the clipboard: the text in delayed rendering, plus the private formats."""
        _register_class()
        hwnd = _user32.CreateWindowExW(0, _CLASS_NAME, None, 0, 0, 0, 0, 0, HWND_MESSAGE, None,
                                       _kernel32.GetModuleHandleW(None), None)
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        self.hwnd = hwnd
        _owners[hwnd] = self
        _open_clipboard(hwnd)
        try:
            if not _user32.EmptyClipboard():  # makes our window the owner
                raise OSError(ctypes.get_last_error(), "EmptyClipboard failed")
            # NULL data: Windows sends WM_RENDERFORMAT when a program asks.
            _user32.SetClipboardData(CF_UNICODETEXT, None)
            _put_private_formats()
        finally:
            _user32.CloseClipboard()
        if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            raise OSError("The delayed clipboard format was not registered")

    def _render(self, format_: int) -> None:
        """WM_RENDERFORMAT: the requester has the clipboard open; just set the data."""
        if format_ != CF_UNICODETEXT or self.rendered:
            return
        opener = _user32.GetOpenClipboardWindow()
        _put(CF_UNICODETEXT, _utf16(self.text))
        self.rendered = True
        # A program that opened the clipboard without a window cannot be
        # identified: not confirmed, so the previous text is not restored.
        reader = window_process(opener) if opener else None
        self.confirmed = self.consumer_pid is not None and reader == self.consumer_pid

    def _render_all(self) -> None:
        """WM_RENDERALLFORMATS (window closing) or `close`: leave the text on the clipboard."""
        if self.rendered or self.lost or not self.hwnd:
            return
        if not _user32.OpenClipboard(self.hwnd):
            return
        try:
            if _user32.GetClipboardOwner() == self.hwnd:
                _put(CF_UNICODETEXT, _utf16(self.text))
                self.rendered = True  # not `confirmed`: nobody pasted it
        finally:
            _user32.CloseClipboard()

    def pump(self, seconds: float, until: Callable[[], bool] | None = None) -> bool:
        """Delivers window messages for up to `seconds`; True as soon as `until()` holds."""
        message = wintypes.MSG()
        end = time.monotonic() + seconds
        while True:
            while _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                _user32.TranslateMessage(ctypes.byref(message))
                _user32.DispatchMessageW(ctypes.byref(message))
            if until is not None and until():
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            _user32.MsgWaitForMultipleObjects(0, None, False, max(1, min(50, int(left * 1000))),
                                              QS_ALLINPUT)

    def restore(self, previous: str | None) -> bool:
        """Puts `previous` back (None empties) if the clipboard is still ours. True when done.

        The ownership check and the write happen with the clipboard open, so
        a copy made in between cannot be overwritten.
        """
        if self.lost or not self.hwnd or not _user32.OpenClipboard(self.hwnd):
            return False
        try:
            if _user32.GetClipboardOwner() != self.hwnd:
                return False
            _user32.EmptyClipboard()
            if previous is not None:
                _put(CF_UNICODETEXT, _utf16(previous))
                _put_private_formats()
            return True
        finally:
            _user32.CloseClipboard()

    def close(self) -> None:
        hwnd = self.hwnd
        if not hwnd:
            return
        try:
            self._render_all()
        except Exception:
            log.warning("Could not leave the dictation on the clipboard", exc_info=True)
        try:
            # If the text is still not rendered, DestroyWindow sends
            # WM_RENDERALLFORMATS once more, while `_owners` still has the window.
            _user32.DestroyWindow(hwnd)
        finally:
            self.hwnd = None
            _owners.pop(hwnd, None)


def press_ctrl_v() -> None:
    _send([_key(VK_CONTROL), _key(VK_V),
           _key(VK_V, flags=KEYEVENTF_KEYUP),
           _key(VK_CONTROL, flags=KEYEVENTF_KEYUP)])


def press_enter() -> None:
    _send([_key(VK_RETURN), _key(VK_RETURN, flags=KEYEVENTF_KEYUP)])


def type_text(text: str) -> None:
    """Types the text as Unicode characters: no clipboard, any keyboard layout."""
    data = text.encode("utf-16-le")
    events: list[_INPUT] = []
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        events.append(_key(scan=unit, flags=KEYEVENTF_UNICODE))
        events.append(_key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    # In portions: a very long burst of events can lose characters in a terminal.
    for i in range(0, len(events), 200):
        _send(events[i:i + 200])
        time.sleep(0.01)


def foreground_window() -> int | None:
    return _user32.GetForegroundWindow() or None


def window_process(window: int | None) -> int | None:
    """Process id of a window; None for no window."""
    if not window:
        return None
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    return pid.value or None


def window_is_ours(window: int | None) -> bool:
    """True when the window belongs to this process (any Ucho window)."""
    return window_process(window) == os.getpid()


def foreground_is_ours() -> bool:
    """True when the active window belongs to this process (any Ucho window)."""
    return window_is_ours(foreground_window())


def window_exists(window: int) -> bool:
    return bool(_user32.IsWindow(window))


# The taskbar and the desktop are in front after a click on them, but nobody
# dictates into them: a dictation started there has no target window, and
# Ctrl+V (or typing) into them goes nowhere.
_SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"}


def is_shell_window(window: int | None) -> bool:
    """True for the taskbar or the desktop."""
    if not window:
        return False
    name = ctypes.create_unicode_buffer(64)
    return bool(_user32.GetClassNameW(window, name, len(name))) and name.value in _SHELL_CLASSES


def dictation_target() -> int | None:
    """The foreground window as the target of a dictation; None for the taskbar or desktop."""
    window = foreground_window()
    if not window or is_shell_window(window):
        return None
    return window


def activate(window: int) -> bool:
    """One attempt to bring the window back; Windows focus rules may refuse it."""
    _user32.SetForegroundWindow(window)
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        if foreground_window() == window:
            return True
        time.sleep(0.02)
    return False


# Where the dictated text goes, decided by `paste_target` and `insert_text`.
PASTE = "paste"            # pasted into the foreground window, which is the right one
OWN_WINDOW = "own_window"  # a Ucho window is active: clipboard only
FOCUS_LOST = "focus_lost"  # the target window is gone or did not come back: clipboard only
NO_WINDOW = "no_window"    # only the desktop or the taskbar is active: clipboard only
NOT_TAKEN = "not_taken"    # Ctrl+V sent, but the window did not take the text in time: clipboard only

# Results that leave the text on the clipboard for the user to paste by hand.
CLIPBOARD_ONLY = (FOCUS_LOST, NO_WINDOW, NOT_TAKEN)


def paste_target(target: int | None, foreground: Callable[[], int | None],
                 is_ours: Callable[[int | None], bool], exists: Callable[[int], bool],
                 activate_window: Callable[[int], bool],
                 is_shell: Callable[[int | None], bool] = lambda _w: False) -> str:
    """PASTE, OWN_WINDOW, NO_WINDOW or FOCUS_LOST for a dictation that started with `target` in front.

    A target that belongs to Ucho (the dictation started from its own button
    or tray menu) counts as no target: the text goes to whatever window is
    active, unless that is Ucho itself or the desktop/taskbar. Otherwise the
    text is pasted only into the target, which gets one attempt to come back
    to the foreground.
    """
    current = foreground()
    if not target or is_ours(target) or current == target:
        if is_ours(current):
            return OWN_WINDOW
        if not current or is_shell(current):
            return NO_WINDOW
        return PASTE
    if not exists(target):
        return FOCUS_LOST
    if activate_window(target) and foreground() == target:
        return PASTE
    return FOCUS_LOST


# Two dictations in a row must not interleave: the second would save the
# first one's text as "previous" and put it back afterwards.
_insert_lock = threading.Lock()


def insert_text(text: str, mode: str, enter: bool, target: int | None = None,
                delivered: Callable[[str], None] | None = None) -> str:
    """Inserts text into the target window; returns PASTE, OWN_WINDOW or one of CLIPBOARD_ONLY.

    With anything but PASTE the text is only on the clipboard, and it stays
    there for the user to paste. `delivered(result)` is called as soon as the
    outcome is known, before the previous clipboard comes back.
    """
    def report(where: str) -> str:
        if delivered is not None:
            delivered(where)
        return where

    with _insert_lock:
        wait_for_keys_released()
        where = paste_target(target, foreground_window, window_is_ours, window_exists, activate,
                             is_shell_window)
        if where != PASTE:
            if where in (FOCUS_LOST, NO_WINDOW):
                log.info("No window to paste into (%s) - the text stays on the clipboard", where)
            set_clipboard(text)
            return report(where)
        if mode == "type":
            type_text(text)
            if enter:
                time.sleep(ENTER_DELAY_S)
                press_enter()
            return report(PASTE)
        return _paste(text, enter, report)


def _paste(text: str, enter: bool, report: Callable[[str], str]) -> str:
    """Ctrl+V with the clipboard borrowed; the previous text comes back only after a confirmed paste.

    Only text (CF_UNICODETEXT) is saved and restored. When the previous
    content had no text (an image, copied files), it cannot be put back
    without the risk of corrupting it; the clipboard is cleared instead.
    Rich formats next to the text (HTML or RTF of a copy from Word) are lost
    the same way; their plain text comes back.
    """
    previous = get_clipboard_text()
    offer = DelayedClipboard(text, window_process(foreground_window()))
    try:
        try:
            offer.offer()
        except Exception:
            # Without delayed rendering there is no way to know when the
            # target has read the text, so the previous text is not restored.
            log.warning("Delayed clipboard unavailable - the previous clipboard will not be restored",
                        exc_info=True)
            offer.close()
            set_clipboard(text)
            press_ctrl_v()
            if enter:
                time.sleep(ENTER_DELAY_S)
                press_enter()
            return report(PASTE)
        press_ctrl_v()
        if not offer.pump(PASTE_TIMEOUT_S, until=lambda: offer.rendered):
            # Not taken (yet): no Enter, nothing restored. `close` renders the
            # text, so a late paste still gets the dictation, never the previous text.
            log.info("The target did not take the text within %.0f s - it stays on the clipboard",
                     PASTE_TIMEOUT_S)
            return report(NOT_TAKEN)
        if enter:
            offer.pump(ENTER_DELAY_S)
            press_enter()
        report(PASTE)
        if not offer.confirmed:
            log.info("The text was taken by another process - the previous clipboard is not restored")
            return PASTE
        offer.pump(RESTORE_GRACE_S)
        try:
            if not offer.restore(previous):
                log.info("The clipboard changed meanwhile - the previous text is not restored")
        except Exception:
            # The text is already pasted; only the old clipboard is not back.
            log.warning("Could not restore the previous clipboard", exc_info=True)
        return PASTE
    finally:
        offer.close()


def beep(frequency: int, ms: int = 70) -> None:
    def _play():
        try:
            import winsound

            winsound.Beep(frequency, ms)
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


# --- microphone ------------------------------------------------------------------------


class Microphone:
    """Records into memory. Dictation lasts seconds, no file is needed."""

    def __init__(self, name: str | None, legacy_index: int | None = None):
        # The chosen microphone by name (None = the Windows default); the index
        # is a choice saved by an earlier version, see audio.find_device().
        self._name = name
        self._legacy_index = legacy_index
        self._reported_missing: str | int | None = None
        self._pa = None
        self._stream = None
        self._blocks: list[bytes] = []
        self._channels = 1
        self._rate = SAMPLE_RATE
        self.level = 0.0
        self.name = ""
        # Set at the first audio block. Opening the stream takes about 0.35 s,
        # so the "speak" signal is given only then; otherwise the first word is lost.
        self.ready = threading.Event()
        self._reset_take()

    def _reset_take(self) -> None:
        self._taken_blocks = 0
        self._resampler = None
        self._taken_parts: list[np.ndarray] = []
        self._taken_samples = 0
        self._take_used = False

    @property
    def taken_samples(self) -> int:
        return self._taken_samples

    def choose(self, name: str | None, legacy_index: int | None = None) -> None:
        self._name, self._legacy_index = name, legacy_index

    def prepare(self) -> None:
        """Initializes PortAudio up front; at the first recording it would cost another 0.5 s."""
        from . import audio

        if self._pa is None:
            self._pa = audio.new_pyaudio()

    def _device(self, p) -> tuple[object, bool]:
        """(device, found): `found` is False when the chosen microphone is not in `p`'s list."""
        from . import audio

        mics, default, names = audio.microphones(p)
        chosen = bool(self._name) or self._legacy_index is not None
        device = audio.find_device(self._name, mics, None, self._legacy_index, names)
        found = device is not None or not chosen
        device = device or default
        if device is None:
            raise RuntimeError(t("No microphone found."))
        return device, found

    def _close_pa(self) -> None:
        from . import audio

        audio.close_pyaudio(self._pa)
        self._pa = None

    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        from . import audio

        # PortAudio lists the devices (and the Windows default) present when it
        # starts, so every dictation gets a fresh one: it then sees a headset
        # plugged in since and a default changed in Windows. Only the first start
        # in the process is slow (about 0.25 s, the DLL loads; prepare() pays it
        # up front), later ones take about 15 ms.
        self._close_pa()
        for attempt in range(2):
            try:
                if self._pa is None:
                    self._pa = audio.new_pyaudio()
                device, found = self._device(self._pa)
                choice = self._name or self._legacy_index
                if found:
                    self._reported_missing = None
                elif self._reported_missing != choice:
                    self._reported_missing = choice
                    log.warning("Chosen microphone %r is not connected - using the default", choice)
                self._channels, self._rate = device.channels, device.rate
                self._blocks = []
                self._reset_take()
                self.level = 0.0
                self.ready.clear()
                self._stream = self._pa.open(
                    format=pyaudio.paInt16, channels=self._channels,
                    rate=self._rate, input=True,
                    input_device_index=device.index, frames_per_buffer=1024,
                    stream_callback=self._callback,
                )
                self._stream.start_stream()
                self.name = device.name
                return
            except Exception:
                self._stream = None
                self._close_pa()
                if attempt:
                    raise
                log.warning("Opening the microphone failed - retrying", exc_info=True)

    def _callback(self, in_data, frame_count, time_info, status):  # noqa: ARG002
        import pyaudiowpatch as pyaudio

        self._blocks.append(in_data)
        self.ready.set()
        try:
            self.level = float(np.abs(np.frombuffer(in_data, dtype=np.int16)).max()) / 32768.0
        except ValueError:
            pass
        return (None, pyaudio.paContinue)

    def _blocks_to_mono(self, blocks: list[bytes]) -> np.ndarray:
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        data = b"".join(blocks)
        if not data:
            return np.zeros(0, dtype=np.float32)
        signal = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if self._channels > 1:
            n = len(signal) // self._channels * self._channels
            signal = signal[:n].reshape(-1, self._channels).mean(axis=1)
        return np.ascontiguousarray(signal, dtype=np.float32)

    def take(self) -> np.ndarray:
        """Returns 16 kHz mono float32 audio recorded since the previous take()."""
        self._take_used = True
        end = len(self._blocks)
        if end <= self._taken_blocks:
            return np.zeros(0, dtype=np.float32)
        new_blocks = self._blocks[self._taken_blocks:end]
        self._taken_blocks = end
        signal = self._blocks_to_mono(new_blocks)
        if self._rate != SAMPLE_RATE and signal.size:
            if self._resampler is None:
                import soxr

                self._resampler = soxr.ResampleStream(
                    self._rate, SAMPLE_RATE, 1, dtype="float32", quality="HQ")
            signal = np.ascontiguousarray(
                self._resampler.resample_chunk(signal, last=False), dtype=np.float32)
        if signal.size:
            self._taken_parts.append(signal)
            self._taken_samples += signal.size
        return signal

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            for method in (stream.stop_stream, stream.close):
                try:
                    method()
                except Exception:
                    pass
        if not self._take_used:
            data, self._blocks = b"".join(self._blocks), []
            signal = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if self._channels > 1:
                n = len(signal) // self._channels * self._channels
                signal = signal[:n].reshape(-1, self._channels).mean(axis=1)
            if self._rate != SAMPLE_RATE and signal.size:
                import soxr

                signal = soxr.resample(signal, self._rate, SAMPLE_RATE, quality="HQ")
            self._reset_take()
            return np.ascontiguousarray(signal, dtype=np.float32)

        rest_blocks = self._blocks[self._taken_blocks:]
        self._blocks = []
        rest = self._blocks_to_mono(rest_blocks)
        if self._rate != SAMPLE_RATE and (rest.size or self._resampler is not None):
            if self._resampler is None:
                import soxr

                self._resampler = soxr.ResampleStream(
                    self._rate, SAMPLE_RATE, 1, dtype="float32", quality="HQ")
            rest = np.ascontiguousarray(
                self._resampler.resample_chunk(rest, last=True), dtype=np.float32)
        parts = [*self._taken_parts, rest]
        self._reset_take()
        non_empty = [p for p in parts if p.size]
        if not non_empty:
            return np.zeros(0, dtype=np.float32)
        return np.ascontiguousarray(np.concatenate(non_empty), dtype=np.float32)

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def close(self) -> None:
        if self._stream is not None:
            self.stop()
        self._close_pa()


# --- controller ---------------------------------------------------------------------------


class DictationController:
    """State of dictation for the Dictation screen, the small window and the tray."""

    def __init__(self, app):
        self.app = app
        self.microphone = Microphone(app.settings["microphone_name"], app.settings["microphone_index"])
        self._downloaded: dict[str, tuple[bool, float]] = {}
        self._reported_missing: str | None = None
        self.recording = False
        self.finishing = False
        self._finish_token = 0
        self.recording_start = 0.0
        self.queued = 0
        self.model_state = "idle"  # idle / loading / ready / error
        self.model_error = ""
        self.model_device = ""
        self.last_text = ""
        self.live_text = ""  # text recognized so far in the current live dictation
        self.last_seconds = 0.0
        self.target_window: int | None = None  # foreground window when the dictation started
        self._live: live.LiveDictation | None = None
        self._shown: live.LiveDictation | None = None
        self._message: tuple[str, str] | None = None
        self._message_until = 0.0
        self._listeners: list[Callable[[str, object], None]] = []

    # --- events -------------------------------------------------------------

    def listen(self, callback: Callable[[str, object], None]) -> None:
        self._listeners.append(callback)

    def clear_listeners(self) -> None:
        self._listeners.clear()

    def _emit(self, event: str, data=None) -> None:
        def deliver():
            for callback in list(self._listeners):
                try:
                    callback(event, data)
                except Exception:
                    log.exception("Listener for %s failed", event)
        self.app.post(deliver)

    def show_message(self, text: str, style: str = "ready", seconds: float = 3.0) -> None:
        self._message = (text, style)
        self._message_until = time.monotonic() + seconds

    @property
    def settings(self) -> config.Settings:
        return self.app.settings

    def model_key(self) -> tuple[str, str]:
        """(model, device) dictation uses; the transcription model when its own is missing."""
        model = self.settings.dictation_model()
        if model != self.settings["model"] and not self._model_downloaded(model):
            if self._reported_missing != model:
                self._reported_missing = model
                log.warning("Dictation model %s is not downloaded - using %s", model,
                            self.settings["model"])
            model = self.settings["model"]
        return model, self.settings["device"]

    def _model_downloaded(self, model: str) -> bool:
        """transcribe.model_downloaded, checked on disk at most every MODEL_CHECK_S.

        The Dictation screen asks for model_key() on every refresh (many times a second).
        """
        now = time.monotonic()
        cached = self._downloaded.get(model)
        if cached is None or now - cached[1] > MODEL_CHECK_S:
            cached = (transcribe.model_downloaded(model), now)
            self._downloaded[model] = cached
        return cached[0]

    def _engine_call(self, name: str) -> None:
        fn = getattr(self.app.engine, name, None)
        if fn is None:
            return
        try:
            fn()
        except Exception:
            log.exception("Engine %s failed", name)

    def _waiting_text(self) -> str | None:
        fn = getattr(self.app.engine, "waiting_behind", None)
        if fn is None:
            return None
        try:
            name = fn()
        except Exception:
            log.debug("waiting_behind failed", exc_info=True)
            return None
        if name is None:
            return None
        if name in ("transcribe", "live-chunk"):
            return t("Waiting for the transcription…")
        if name == "summary":
            return t("Waiting for the summary…")
        return t("Waiting for the model…")

    def status(self) -> tuple[str, str, str]:
        """(headline, pill style, hint) for the UI."""
        hotkey = self.app.dictation_hotkey_label()
        hint = t("{shortcut} — speak, press again — insert text", shortcut=hotkey)
        error = self.app.hotkey_errors.get("dictate")
        if self.recording:
            elapsed = time.monotonic() - self.recording_start
            waiting = self._waiting_text()
            return (t("Recording  {time}", time=f"{int(elapsed) // 60}:{int(elapsed) % 60:02d}"),
                    "recording", waiting or t("{shortcut} finishes, Esc cancels", shortcut=hotkey))
        if self.queued:
            return self._waiting_text() or t("Recognizing…"), "working", hint
        if self._message and time.monotonic() < self._message_until:
            return self._message[0], self._message[1], hint
        if error:
            return t("Shortcut unavailable"), "warning", error
        if self.model_state == "error":
            return t("The model failed to load"), "warning", self.model_error[:120]
        if self.model_state == "loading":
            return t("Loading model…"), "idle", t("You can already speak, the text will appear once it loads")
        return t("Ready"), "ready", hint

    def level(self) -> float:
        return min(1.0, self.microphone.level * 3) if self.recording else 0.0

    # --- model -------------------------------------------------------------------

    def preload(self) -> None:
        """Loads the dictation model in the background, if the setting says so."""
        # Settings may have changed (or a model was downloaded): look at the disk again.
        self._downloaded.clear()
        self._reported_missing = None
        key = self.model_key()
        preload = bool(self.settings["dictation_preload"])
        if not transcribe.model_downloaded(key[0]):
            self.model_state, self.model_error = "error", t("Model {model} is not downloaded.",
                                                             model=key[0].split("/")[-1])
            self.app.engine.configure(None, False)
            return
        self.model_state = "loading" if preload else "idle"

        def done(device):
            self.model_state = "ready" if device or not preload else "idle"
            self.model_device = device or ""
            self._emit("state")

        def failed(error):
            self.model_state, self.model_error = "error", str(error)
            self._emit("state")

        self.app.engine.configure(key, preload, on_done=done, on_error=failed)

    # --- recording ---------------------------------------------------------------

    def toggle(self) -> None:
        if self.finishing:
            return
        if self.recording:
            self.stop()
        else:
            self.start()

    def start(self) -> bool:
        if self.recording:
            return False
        # Before anything of ours can take the focus: this is where the text goes.
        try:
            self.target_window = dictation_target()
        except Exception:
            self.target_window = None
        self.microphone.choose(self.settings["microphone_name"], self.settings["microphone_index"])
        try:
            self.microphone.start()
        except Exception as error:
            log.exception("Could not open the microphone")
            self.show_message(t("Microphone unavailable"), "warning", 6)
            self.last_text = str(error)[:200]
            self._emit("state")
            return False
        log.info("Dictation from: %s", self.microphone.name)
        self.recording = True
        self._engine_call("pause_for_dictation")
        self.recording_start = time.monotonic()
        self.live_text = ""
        self._live = self._shown = None
        if self.settings["dictation_live"]:
            try:
                self._start_live()
            except Exception:
                log.exception("Live dictation could not start - recognizing after the stop")
                self._live = self._shown = None
        self.app.hotkeys.escape(True)
        if self.settings["dictation_sounds"]:
            ready = self.microphone.ready
            threading.Thread(target=lambda: ready.wait(2.0) and beep(1046), daemon=True).start()
        self._emit("state")
        return True

    def _start_live(self) -> None:
        model, device = self.model_key()
        session = live.LiveDictation(
            self.app.engine, model, device, self.settings["dictation_language"],
            self.settings["dictation_dictionary"],
            on_text=lambda text: self.app.post(lambda: self._on_live_text(session, text)))
        # `_live` is fed until the stop; `_shown` keeps showing its text until
        # the result arrives (its last pieces may finish after the stop).
        self._live = self._shown = session
        self._schedule_live_tick(session)

    def _on_live_text(self, session: live.LiveDictation, text: str) -> None:
        if session is self._shown and (self.recording or self.queued):
            self.live_text = text

    def _schedule_live_tick(self, session: live.LiveDictation) -> None:
        # Bound to its session: a tick still pending from a dictation that just
        # ended must not start a second polling loop for the next one.
        self.app.after(LIVE_TICK_MS, lambda: self._live_tick(session))

    def _live_tick(self, session: live.LiveDictation) -> None:
        """Feeds the audio recorded since the last tick to the live session (UI thread)."""
        if not self.recording or session is not self._live:
            return
        try:
            session.feed(self.microphone.take())
        except Exception:
            # This dictation then finishes the old way, from the whole recording.
            log.exception("Live dictation failed - falling back to full recognition")
            session.cancel()
            self._live = None
            self.live_text = ""
            return
        self._schedule_live_tick(session)

    def stop(self) -> None:
        if not self.recording or self.finishing:
            return
        self.finishing = True
        token = self._finish_token
        self.app.after(int(TAIL_S * 1000), lambda: self._finish(token))

    def _finish(self, token: int | None = None) -> None:
        # A cancel (Esc) inside the tail window invalidates the scheduled finish.
        if token is not None and token != self._finish_token:
            return
        taken = self.microphone.taken_samples
        recording = self.microphone.stop()
        self.recording = self.finishing = False
        self.app.hotkeys.escape(False)
        if self.settings["dictation_sounds"]:
            beep(659)
        self.queued += 1
        model, device = self.model_key()
        language = self.settings["dictation_language"]
        mode, enter = self.settings["dictation_mode"], bool(self.settings["dictation_enter"])
        target = self.target_window
        submitted = time.monotonic()

        def done(result):
            text, seconds, used = result
            waited = time.monotonic() - submitted
            # last_text_from_log() parses this line; keep its format.
            log.info("Recognized %.1f s of audio in %.1f s: %r", recording.size / SAMPLE_RATE,
                     waited, text)
            self.model_state, self.model_device = "ready", used

            def insert():
                reported: list[str | None] = []

                def report(where: str | None) -> None:
                    if not reported:
                        reported.append(where)
                        self.app.post(lambda: self._recognized(text, waited, where))

                if text:
                    try:
                        # Trailing space: two dictations in a row do not glue into one word.
                        insert_text(text if enter else text + " ", mode, enter, target, report)
                    except Exception:
                        log.exception("Could not insert the text")
                report(None)

            self._insert_thread = threading.Thread(target=insert, name="insert-text", daemon=True)
            self._insert_thread.start()

        def failed(error):
            self.app.post(lambda: self._failed(str(error)))

        session, self._live = self._live, None
        try:
            if session is not None:
                try:
                    session.feed(recording[taken:])
                    pieces = session.submitted
                    session.finish(recording, on_done=done, on_error=failed)
                    log.info("Live dictation finished (%d piece(s) submitted)", pieces)
                except Exception:
                    log.exception("Live dictation finish failed - falling back to full recognition")
                    session.cancel()
                    self.app.engine.dictate(recording, model, device, language,
                                            self.settings["dictation_dictionary"],
                                            on_done=done, on_error=failed)
            else:
                self.app.engine.dictate(recording, model, device, language,
                                        self.settings["dictation_dictionary"],
                                        on_done=done, on_error=failed)
        finally:
            self._engine_call("resume_after_dictation")
        self._emit("state")

    def _recognized(self, text: str, seconds: float, where: str | None) -> None:
        """`where`: PASTE, OWN_WINDOW, one of CLIPBOARD_ONLY, or None when inserting failed."""
        self.queued = max(0, self.queued - 1)
        self.live_text = ""
        if text:
            self.last_text, self.last_seconds = text, seconds
            if where == NOT_TAKEN:
                # The Ctrl+V is still queued in a slow window, which may paste on its own.
                self.show_message(t("The window has not taken the text yet. If it does not "
                                    "appear, press Ctrl+V."), "warning", 8)
            elif where in CLIPBOARD_ONLY:
                # The window you dictated into is gone, did not come back or
                # did not take the text, or only the desktop was active: better
                # on the clipboard than pasted into the wrong window or nowhere.
                self.show_message(t("Text is on the clipboard, press Ctrl+V"), "warning", 8)
            elif where is None:
                self.show_message(t("Clipboard busy, try again"), "warning", 6)
            else:
                label = t("Inserted") if where == PASTE else t("Copied to clipboard")
                self.show_message(f"{label} ({seconds:.1f} s)")
        else:
            self.show_message(t("I didn't hear anything"), "idle")
        self._emit("text", text)

    def _failed(self, error: str) -> None:
        self.queued = max(0, self.queued - 1)
        self.live_text = ""
        self.show_message(t("Recognition error"), "warning", 6)
        self.last_text = error[:200]
        self._emit("state")

    def cancel(self) -> None:
        if not self.recording:
            return
        if self._live is not None:
            self._live.cancel()
        self._live = self._shown = None
        self.live_text = ""
        self._finish_token += 1
        self.microphone.stop()
        self.recording = self.finishing = False
        self._engine_call("resume_after_dictation")
        self.app.hotkeys.escape(False)
        if self.settings["dictation_sounds"]:
            beep(330, 120)
        self.show_message(t("Cancelled"), "idle")
        self._emit("state")

    def copy_last(self) -> bool:
        if not self.last_text:
            return False
        try:
            set_clipboard(self.last_text)
        except Exception:
            log.exception("Could not copy to the clipboard")
            self.show_message(t("Clipboard busy, try again"), "warning", 4)
            return False
        self.show_message(t("Copied ({n} characters)", n=len(self.last_text)))
        return True

    def check_limit(self) -> None:
        """Stops a recording that runs past the maximum length (called from the UI tick)."""
        if (self.recording and not self.finishing and
                time.monotonic() - self.recording_start > float(self.settings["dictation_max_s"])):
            self.stop()

    def quit(self) -> None:
        # A paste may still be waiting for the target (up to PASTE_TIMEOUT_S): let it
        # finish, so the dictation is rendered onto the clipboard before we exit.
        thread = getattr(self, "_insert_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(PASTE_TIMEOUT_S + 1.0)
        if self.recording:
            if self._live is not None:
                self._live.cancel()
                self._live = None
            self.microphone.stop()
            self.recording = False
            self._engine_call("resume_after_dictation")
        self.microphone.close()
