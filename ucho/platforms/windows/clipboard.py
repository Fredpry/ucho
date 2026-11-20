# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Windows clipboard access, private formats and delayed rendering."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from ...i18n import t
from .inserter import window_process
from .winapi import (
    CF_UNICODETEXT,
    ERROR_CLASS_ALREADY_EXISTS,
    GMEM_MOVEABLE,
    HWND_MESSAGE,
    PM_REMOVE,
    QS_ALLINPUT,
    WM_DESTROYCLIPBOARD,
    WM_RENDERALLFORMATS,
    WM_RENDERFORMAT,
    WNDPROC,
    _WNDCLASSW,
    _kernel32,
    _user32,
)

log = logging.getLogger(__name__)

# Registered formats that keep the text out of clipboard history (Win+V),
# cloud clipboard sync and clipboard monitors ("Cloud Clipboard and Clipboard
# History Formats" in the Windows docs). The first counts by its presence,
# the other two need a DWORD 0.
PRIVATE_FORMATS = {
    "ExcludeClipboardContentFromMonitorProcessing": 0,
    "CanIncludeInClipboardHistory": 0,
    "CanUploadToCloudClipboard": 0,
}


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
_owners: dict[int, DelayedClipboard] = {}  # hidden window -> its clipboard offer
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


class WindowsClipboard:
    """Win32 `Clipboard` implementation."""

    def get_text(self) -> str | None:
        return get_clipboard_text()

    def set_text(self, text: str | None, private: bool = True) -> int:
        return set_clipboard(text, private)

    def sequence(self) -> int:
        return clipboard_sequence()

    def offer_delayed(self, text: str, consumer_pid: int | None) -> DelayedClipboard:
        return DelayedClipboard(text, consumer_pid)


def create() -> WindowsClipboard:
    return WindowsClipboard()
