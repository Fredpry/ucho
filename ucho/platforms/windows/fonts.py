# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Windows UI font stacks, UI font files, and Segoe icon glyphs."""

from __future__ import annotations

import os
from pathlib import Path

_ICON_FONTS = ("SegoeIcons.ttf", "segmdl2.ttf")
GLYPHS = {
    "meeting": "\uE7C8",     # Record
    "mic": "\uE720",         # Microphone
    "list": "\uE8FD",        # BulletedList
    "settings": "\uE713",
    "search": "\uE721",
    "copy": "\uE8C8",
    "folder": "\uE838",      # OpenLocal
    "back": "\uE72B",
    "refresh": "\uE72C",
    "stop": "\uE71A",
    "info": "\uE946",
    "warning": "\uE7BA",
    "check": "\uE73E",
    "window": "\uE8A7",      # OpenInNewWindow
    "summary": "\uE8BD",     # Message
    "speed": "\uEC4A",       # Speed high
    "close": "\uE711",
    "delete": "\uE74D",
    "edit": "\uE70F",
}

_UI_FONTS = {
    True: ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf"),
    False: ("segoeui.ttf", "arial.ttf"),
}


def _fonts_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


class WindowsFonts:
    """Win32 `Fonts` implementation."""

    sans = ("Segoe UI Variable Text", "Segoe UI", "Arial")
    display = ("Sitka Heading", "Georgia", "Times New Roman")  # Iowan/Charter are rare on Windows
    mono = ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New")
    # Large numbers (the timer): the display cut of Segoe UI, tabular digits.
    numeric = ("Segoe UI Variable Display", "Segoe UI Light", "Segoe UI", "Arial")

    def __init__(self) -> None:
        # Windows ships semibold cuts as separate families; they read calmer than bold.
        self.strong: dict[str, tuple[str, ...]] = {
            "sans": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
            "display": ("Sitka Heading Semibold",),
            "numeric": ("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
        }

    def icon_font(self) -> str | None:
        fonts_dir = _fonts_dir()
        return next((str(fonts_dir / f) for f in _ICON_FONTS if (fonts_dir / f).exists()), None)

    def glyphs(self) -> dict[str, str]:
        return dict(GLYPHS)

    def ui_font_file(self, bold: bool) -> str | None:
        fonts_dir = _fonts_dir()
        return next((str(fonts_dir / f) for f in _UI_FONTS[bool(bold)] if (fonts_dir / f).exists()), None)


def create() -> WindowsFonts:
    return WindowsFonts()
