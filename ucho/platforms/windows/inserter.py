# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Windows text insertion and target-window inspection via SendInput and User32."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import TYPE_CHECKING

from .winapi import (
    INPUT_KEYBOARD,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_CONTROL,
    VK_MENU,
    VK_RETURN,
    VK_SHIFT,
    VK_SPACE,
    VK_V,
    _INPUT,
    _KEYBDINPUT,
    _user32,
)

if TYPE_CHECKING:
    from ..base import Permission, WindowRef


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


def activate(window: int) -> bool:
    """One attempt to bring the window back; Windows focus rules may refuse it."""
    _user32.SetForegroundWindow(window)
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        if foreground_window() == window:
            return True
        time.sleep(0.02)
    return False


class WindowsInserter:
    """Win32 `TextInserter` implementation."""

    def foreground(self) -> WindowRef | None:
        return foreground_window()

    def process_of(self, window: WindowRef | None) -> int | None:
        return window_process(window)

    def is_ours(self, window: WindowRef | None) -> bool:
        return window_is_ours(window)

    def is_shell(self, window: WindowRef | None) -> bool:
        return is_shell_window(window)

    def exists(self, window: WindowRef) -> bool:
        return window_exists(window)

    def activate(self, window: WindowRef) -> bool:
        return activate(window)

    def paste(self) -> None:
        press_ctrl_v()

    def press_enter(self) -> None:
        press_enter()

    def type_text(self, text: str) -> None:
        type_text(text)

    def wait_keys_released(self, limit_s: float = 1.5) -> None:
        wait_for_keys_released(limit_s)

    def paste_label(self) -> str:
        return "Ctrl+V"

    def permission(self) -> Permission:
        return "granted"

    def request_permission(self) -> None:
        pass


def create() -> WindowsInserter:
    return WindowsInserter()
