# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Drawing the app icon (the ear mark from design/assets/ucho-mark.svg).

The icon is drawn in code instead of shipped only as a binary file: nothing to
download, and the "recording" variant is one extra dot. The paths below are
those of the SVG mark (viewBox 200 x 200, its translate already applied),
sampled into polylines, drawn at 4x size and scaled down for smooth edges.
Below 32 px the sound waves are dropped and the strokes get thicker, as the
design system says.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

# Colors from design/tokens.json.
BLUSH_100 = (250, 235, 229, 255)
BLUSH_300 = (235, 187, 170, 255)
BLUSH_400 = (223, 156, 134, 255)
BLUSH_600 = (176, 100, 78, 255)
BLUSH_700 = (142, 78, 61, 255)
BLUSH_800 = (107, 59, 47, 255)
CLAY = (180, 69, 47, 255)
WHITE = (255, 255, 255, 255)

_OUTER = [((76, 72), (76, 46), (96, 30), (118, 30)), ((118, 30), (144, 30), (162, 50), (162, 76)),
          ((162, 76), (162, 98), (148, 106), (142, 120)), ((142, 120), (136, 134), (138, 152), (120, 162)),
          ((120, 162), (108, 168), (94, 162), (92, 152))]
_INNER = [((100, 78), (100, 64), (110, 56), (120, 56)), ((120, 56), (132, 56), (140, 66), (140, 76)),
          ((140, 76), (140, 88), (128, 92), (122, 100)), ((122, 100), (116, 108), (120, 118), (110, 122))]
# Sound waves: arcs bulging to the left, (center x, center y, radius, half chord).
_WAVES = [(85.6, 102, 58, 30), (82.2, 102, 38, 20), (77.4, 102, 18, 11)]


def _bezier(points, steps: int = 24) -> list[tuple[float, float]]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = points
    result = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        result.append((u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                       u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3))
    return result


def _stroke(pen: ImageDraw.ImageDraw, points, width: float, color) -> None:
    # A disc at every sample plus straight pieces between them: round joins
    # and caps without the seams PIL's polyline joints leave on tight curves.
    r = width / 2
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        pen.line([(x0, y0), (x1, y1)], fill=color, width=max(1, round(width)))
    for x, y in points:
        pen.ellipse([x - r, y - r, x + r, y + r], fill=color)


def draw_mark(size: int, outer=BLUSH_600, inner=BLUSH_800, waves=BLUSH_400,
              background=BLUSH_100, radius: float = 0.22, recording: bool = False,
              with_waves: bool | None = None) -> Image.Image:
    """The ear mark on a rounded square (`background=None`: transparent), `size` px."""
    scale = 4
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    if background is not None:
        pen.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * radius), fill=background)

    if with_waves is None:
        with_waves = size >= 32
    # With waves the mark spans x 28..169, y 23..174; without them x 70..169.
    span = 150 if with_waves else 120
    fit = big * (0.74 if background is not None else 0.92) / span
    center_x = 98 if with_waves else 118
    offset_x = big / 2 - center_x * fit
    offset_y = big / 2 - 98 * fit
    thick = 1.0 if with_waves else 1.3

    def tr(points):
        return [(offset_x + x * fit, offset_y + y * fit) for x, y in points]

    if with_waves:
        for cx, cy, r, half in _WAVES:
            start = math.asin(half / r)
            points = [(cx + r * math.cos(math.pi - start + 2 * start * i / 24),
                       cy - r * math.sin(math.pi - start + 2 * start * i / 24)) for i in range(25)]
            _stroke(pen, tr(points), 8 * fit, waves)
    _stroke(pen, tr([p for curve in _OUTER for p in _bezier(curve)]), 13 * fit * thick, outer)
    _stroke(pen, tr([p for curve in _INNER for p in _bezier(curve)]), 11 * fit * thick, inner)

    if recording:
        r = big * 0.17
        cx = cy = big - r - big * 0.04
        ring = big * 0.035
        pen.ellipse([cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring], fill=WHITE)
        pen.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CLAY)
    return image.resize((size, size), Image.LANCZOS)


def draw(size: int = 256, recording: bool = False) -> Image.Image:
    """App icon. While recording a clay-red dot appears in the corner."""
    return draw_mark(size, recording=recording)


def draw_dictate(size: int = 256) -> Image.Image:
    """Icon of the "Ucho Dictate" shortcut: the same mark, inverted colors."""
    return draw_mark(size, outer=BLUSH_100, inner=BLUSH_300, waves=BLUSH_300, background=BLUSH_700)


ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def save_ico(path: Path, painter=draw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    images = [painter(n) for n in ICO_SIZES]
    images[-1].save(path, format="ICO", sizes=[(n, n) for n in ICO_SIZES],
                    append_images=images[:-1])
    return path


if __name__ == "__main__":
    assets = Path(__file__).resolve().parent / "assets"
    print(save_ico(assets / "icon.ico"))
    print(save_ico(assets / "dictate.ico", draw_dictate))
