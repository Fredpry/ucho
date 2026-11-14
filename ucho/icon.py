# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Drawing the app icon - for the window, the tray and the .lnk shortcuts.

The icon is drawn in code instead of shipped as a binary file: nothing to
download, and the "recording" variant is just one color change.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (24, 26, 32, 255)
ACCENT = (94, 164, 255, 255)
ACCENT_REC = (239, 68, 68, 255)


def draw(size: int = 256, recording: bool = False) -> Image.Image:
    """A microphone on a rounded square. While recording the capsule turns red."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    s = size / 256.0
    color = ACCENT_REC if recording else ACCENT

    pen.rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(56 * s), fill=BG
    )

    # Microphone capsule.
    pen.rounded_rectangle(
        [int(100 * s), int(56 * s), int(156 * s), int(150 * s)],
        radius=int(28 * s), fill=color,
    )

    # Holder: an arc around the capsule.
    pen.arc(
        [int(76 * s), int(92 * s), int(180 * s), int(184 * s)],
        start=0, end=180, fill=color, width=max(2, int(12 * s)),
    )

    # Stem and base.
    pen.rounded_rectangle(
        [int(122 * s), int(176 * s), int(134 * s), int(204 * s)],
        radius=int(6 * s), fill=color,
    )
    pen.rounded_rectangle(
        [int(92 * s), int(198 * s), int(164 * s), int(210 * s)],
        radius=int(6 * s), fill=color,
    )
    return image


def save_ico(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base = draw(256)
    base.save(path, format="ICO", sizes=[(n, n) for n in sizes])
    return path


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "assets" / "icon.ico"
    print(save_ico(target))
