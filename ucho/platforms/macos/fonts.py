# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS UI font stacks, UI font files, and bundled Fluent icon glyphs."""

from __future__ import annotations

from pathlib import Path

from ... import paths

ICON_FONT = "FluentSystemIcons-Ucho.ttf"

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
    True: (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ),
    False: (
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ),
}


class MacFonts:
    """macOS `Fonts` implementation."""

    # Tk on macOS does not list "SF Pro" or "SF Mono" in font.families() (M3a spike);
    # ".AppleSystemUIFont" works by name even though it is absent from font.families().
    sans = (".AppleSystemUIFont", "Helvetica Neue", "Helvetica")
    display = ("New York", "Georgia", "Times")
    mono = ("Menlo", "Monaco", "Courier")
    numeric = (".AppleSystemUIFont", "Helvetica Neue", "Helvetica")

    def __init__(self) -> None:
        # Tk on macOS does not expose separate semibold font families.
        self.strong: dict[str, tuple[str, ...]] = {}

    def icon_font(self) -> str | None:
        path = paths.app_dir() / "design" / "icons" / ICON_FONT
        return str(path) if path.exists() else None

    def glyphs(self) -> dict[str, str]:
        return dict(GLYPHS)

    def ui_font_file(self, bold: bool) -> str | None:
        return next((p for p in _UI_FONTS[bool(bold)] if Path(p).exists()), None)


def create() -> MacFonts:
    return MacFonts()
