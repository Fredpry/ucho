# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Global hotkeys through RegisterHotKey: record (Ctrl+Alt+R), dictate (Ctrl+Shift+Space), Esc.

Why RegisterHotKey and not a keyboard listener (pynput, used before v0.2):
the system then delivers the combination only to us, so Ctrl+Shift+Space no
longer also opens the new-tab menu of Windows Terminal, and we learn at once
when another program already owns the combination ("hotkey busy") instead of
both programs reacting.

Hotkeys belong to the thread that registered them, so one thread here runs
its own message loop. Esc is registered only while a dictation is recording;
otherwise we do not take it away from other programs.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import byref, wintypes
from typing import Callable

from .i18n import t

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_ESCAPE = 0x1B
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP_ESC = 0x8001

_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
              "shift": MOD_SHIFT, "win": MOD_WIN, "cmd": MOD_WIN}
# Virtual key codes: letters are their upper-case ASCII code.
_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": VK_ESCAPE, "escape": VK_ESCAPE,
         **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},
         **{str(d): 0x30 + d for d in range(10)},
         **{f"f{n}": 0x6F + n for n in range(1, 13)}}


def parse(combo: str) -> tuple[int, int] | None:
    """'<ctrl>+<alt>+r' -> (MOD_CONTROL | MOD_ALT, VK_R); None when not understood."""
    modifiers, key = 0, None
    for token in (combo or "").lower().replace(" ", "").split("+"):
        token = token.strip("<>")
        if not token:
            continue
        if token in _MODIFIERS:
            modifiers |= _MODIFIERS[token]
        elif token in _KEYS and key is None:
            key = _KEYS[token]
        else:
            return None
    if key is None or not modifiers:
        return None
    return modifiers, key


def label(combo: str) -> str:
    """'<ctrl>+<shift>+space' -> 'Ctrl + Shift + Space' for the UI."""
    names = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win",
             "space": t("Space"), "esc": "Esc", "enter": "Enter"}
    parts = []
    for token in (combo or "").lower().replace(" ", "").split("+"):
        token = token.strip("<>")
        if token:
            parts.append(names.get(token, token.upper()))
    return " + ".join(parts)


class Hotkeys:
    """Registers named hotkeys and calls `action(name)` on the hotkey thread.

    `errors` maps a name to a user-facing message when the combination could
    not be registered (usually: another program holds it). The app keeps
    working without it; the button in the window does the same thing.
    """

    ESCAPE = "escape"

    def __init__(self, action: Callable[[str], None]):
        self._action = action
        self._bindings: dict[str, str] = {}
        self._ids: dict[int, str] = {}
        self._thread_id = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.errors: dict[str, str] = {}

    def start(self, bindings: dict[str, str]) -> dict[str, str]:
        """Registers {name: combo}. Returns the errors (empty when all worked)."""
        self.stop()
        self._bindings = dict(bindings)
        self.errors = {}
        if sys.platform != "win32":
            return self.errors
        self._ready.clear()
        self._thread = threading.Thread(target=self._loop, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return self.errors

    def _loop(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self._thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Forces the message queue to exist before anyone posts to it.
        user32.PeekMessageW(byref(msg), None, 0, 0, 0)

        registered: list[int] = []
        for number, (name, combo) in enumerate(self._bindings.items(), start=1):
            parsed = parse(combo)
            if parsed is None:
                self.errors[name] = t("Unrecognized shortcut {shortcut}.", shortcut=combo)
                log.warning("Unrecognized shortcut %s for %s", combo, name)
                continue
            modifiers, key = parsed
            if user32.RegisterHotKey(None, number, modifiers | MOD_NOREPEAT, key):
                registered.append(number)
                self._ids[number] = name
                log.info("Hotkey %s registered for %s", combo, name)
            else:
                code = ctypes.get_last_error()
                self.errors[name] = t("{shortcut} is taken by another program (code {code}).",
                                      shortcut=label(combo), code=code)
                # A busy hotkey is expected (e.g. an older Ucho still running), not an error.
                log.warning("Hotkey %s for %s not registered (code %s)", combo, name, code)
        escape_id = len(self._bindings) + 1
        self._ids[escape_id] = self.ESCAPE
        self._ready.set()
        try:
            while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    name = self._ids.get(int(msg.wParam))
                    if name:
                        try:
                            self._action(name)
                        except Exception:
                            log.exception("Hotkey action %s failed", name)
                elif msg.message == WM_APP_ESC:
                    if msg.wParam:
                        user32.RegisterHotKey(None, escape_id, MOD_NOREPEAT, VK_ESCAPE)
                    else:
                        user32.UnregisterHotKey(None, escape_id)
        finally:
            for number in registered + [escape_id]:
                user32.UnregisterHotKey(None, number)
            self._thread_id = 0

    def escape(self, enable: bool) -> None:
        """Esc on (while dictating) or off."""
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_APP_ESC, int(enable), 0)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        # Wait until the old thread unregistered everything, or a restart with
        # new bindings would find our own hotkeys "busy".
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2.0)
        self._thread = None
