# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""System tray icon and the global keyboard shortcut.

Both are optional: if pystray or pynput fail to start (e.g. because of a
system policy), the app keeps working without them.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import icon as icon_mod
from .i18n import t

log = logging.getLogger(__name__)


class TrayIcon:
    """Thin wrapper over pystray. Every method is safe when the library is missing."""

    def __init__(self, title: str, on_toggle: Callable[[], None],
                 on_show: Callable[[], None], on_quit: Callable[[], None]):
        self._title = title
        self._on_toggle = on_toggle
        self._on_show = on_show
        self._on_quit = on_quit
        self._icon = None
        self._recording = False

    def start(self) -> bool:
        try:
            import pystray
        except ImportError:
            log.warning("pystray missing - tray icon disabled")
            return False

        menu = pystray.Menu(
            pystray.MenuItem(t("Show window"), lambda: self._on_show(), default=True),
            pystray.MenuItem(
                lambda _: t("Stop recording") if self._recording else t("Start recording"),
                lambda: self._on_toggle(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("Quit"), lambda: self._on_quit()),
        )
        try:
            self._icon = pystray.Icon(
                "ucho", icon_mod.draw(64, recording=False), self._title, menu
            )
            self._icon.run_detached()
            log.info("Tray icon started")
            return True
        except Exception as error:
            log.warning("Could not start the tray icon: %s", error)
            self._icon = None
            return False

    def set_state(self, recording: bool, description: str | None = None) -> None:
        self._recording = recording
        if self._icon is None:
            return
        try:
            self._icon.icon = icon_mod.draw(64, recording=recording)
            self._icon.title = description or self._title
            self._icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            pass
        self._icon = None


# Virtual key codes: independent of the keyboard layout and of whether Windows
# translated the key into a character.
_VK_LETTERS = {letter: 0x41 + i for i, letter in enumerate("abcdefghijklmnopqrstuvwxyz")}
_VK_FUNCTION = {f"f{n}": 0x6F + n for n in range(1, 13)}

_REPEAT_INTERVAL_S = 0.6


class GlobalHotkey:
    """Global hotkey that works also while the window is hidden.

    We do not use `keyboard.GlobalHotKeys`: with Alt held down Windows does not
    translate the letter into a character and pynput reports a raw code (e.g.
    <82> instead of 'r'), so the built-in matching never fires. We track the
    modifiers ourselves and compare the virtual key code.
    """

    def __init__(self, combo: str, action: Callable[[], None]):
        self._combo = combo
        self._action = action
        self._listener = None
        self._pressed: set[str] = set()
        self._last_call = 0.0
        self._required, self._vk = self._parse(combo)

    @staticmethod
    def _parse(combo: str) -> tuple[set[str], int | None]:
        required: set[str] = set()
        vk: int | None = None
        for token in combo.lower().replace(" ", "").split("+"):
            token = token.strip("<>")
            if token in ("ctrl", "alt", "shift"):
                required.add(token)
            elif token in _VK_LETTERS:
                vk = _VK_LETTERS[token]
            elif token in _VK_FUNCTION:
                vk = _VK_FUNCTION[token]
            elif token.isdigit():
                vk = int(token)
        return required, vk

    @staticmethod
    def _modifier(key) -> str | None:
        name = getattr(key, "name", "") or ""
        if name.startswith("ctrl"):
            return "ctrl"
        if name.startswith("alt"):
            return "alt"
        if name.startswith("shift"):
            return "shift"
        return None

    def start(self) -> bool:
        if self._vk is None:
            log.warning("Unrecognized shortcut %s", self._combo)
            return False
        try:
            from pynput import keyboard
        except ImportError:
            log.warning("pynput missing - global shortcut disabled")
            return False
        try:
            self._listener = keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release
            )
            self._listener.daemon = True
            self._listener.start()
            log.info("Global shortcut %s active (vk=%s)", self._combo, self._vk)
            return True
        except Exception as error:
            log.warning("Could not register shortcut %s: %s", self._combo, error)
            self._listener = None
            return False

    def _on_press(self, key) -> None:
        mod = self._modifier(key)
        if mod:
            self._pressed.add(mod)
            return
        if getattr(key, "vk", None) != self._vk:
            return
        if not self._required.issubset(self._pressed):
            return
        # Autorepeat while the key is held must not fire the action many times.
        now = time.monotonic()
        if now - self._last_call < _REPEAT_INTERVAL_S:
            return
        self._last_call = now
        self._fire()

    def _on_release(self, key) -> None:
        mod = self._modifier(key)
        if mod:
            self._pressed.discard(mod)

    def _fire(self) -> None:
        # The action may raise the window, so we do not block the keyboard listener thread.
        threading.Thread(target=self._action, daemon=True).start()

    def stop(self) -> None:
        if self._listener is None:
            return
        try:
            self._listener.stop()
        except Exception:
            pass
        self._listener = None
