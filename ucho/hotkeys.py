# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Global hotkeys: shortcut format parsing, UI labels, and platform facade.

Shortcuts use the `<ctrl>+<alt>+r` format (`parse`, `label`). The platform
implementation lives in `ucho/platforms/<family>/hotkeys.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import platforms
from .i18n import t

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_ESCAPE = 0x1B

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
    """Named global hotkeys; `action(name)` runs on the platform's hotkey thread."""

    ESCAPE = "escape"

    def __init__(self, action: Callable[[str], None]):
        self._impl = platforms.hotkeys(action)
        self.errors: dict[str, str] = {}

    def start(self, bindings: dict[str, str]) -> dict[str, str]:
        # The same dict as the platform's: the Windows hotkey thread may still add to it
        # after start() returned (it waits at most 3 s for the registration).
        self.errors = self._impl.start(bindings)
        return self.errors

    def escape(self, enable: bool) -> None:
        self._impl.escape(enable)

    def stop(self) -> None:
        self._impl.stop()
