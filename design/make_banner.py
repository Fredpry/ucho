# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Builds the animated README banners in design/assets/ (PL/EN, light/dark).

The headline is set in Bricolage Grotesque and converted to outlines, because
GitHub shows SVG files as images, and an image cannot load a web font. The
transcript uses the viewer's monospace font. Animation is plain CSS inside the
SVG (GitHub keeps it), with a still frame for prefers-reduced-motion.

Usage (design tool, not part of the app; needs fonttools and uharfbuzz):

    python design/make_banner.py path/to/BricolageGrotesque[opsz,wdth,wght].ttf

The font comes from github.com/google/fonts (ofl/bricolagegrotesque, SIL OFL 1.1).
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

OUT = Path(__file__).resolve().parent / "assets"
W, H, BAND_Y, R = 1280, 700, 330, 28
AXES = {"wght": 650, "wdth": 90, "opsz": 96}
HEADLINE_SIZE = 112
TRACKING = -0.025  # em, same as the website
CYCLE = 15.0       # seconds for one round of the transcript

THEMES = {
    "light": {"bg": "#FDF7F4", "text": "#2A211E", "accent": "#2B5C47", "band": "#163327"},
    "dark": {"bg": "#1D1816", "text": "#F4EAE6", "accent": "#9CCDB0", "band": "#11261D"},
}
BAND_TEXT, BAND_MUTED, ME, THEM = "#F4EAE6", "#A9B8AF", "#EBBBAA", "#9CCDB0"

COPY = {
    "pl": {
        "lines": ("Ucho słucha.", "Nikomu nie powtarza."),
        "title": "Ucho słucha. Nikomu nie powtarza.",
        "desc": "Prywatna transkrypcja spotkań i dyktowania, offline na Twoim laptopie.",
        "me": "Ja", "them": "Spotkanie",
        "transcript": (
            ("me", "00:00:04", "Zaczynamy od budżetu na Q1."),
            ("them", "00:00:11", "Dostawca podniósł cenę o 10%."),
            ("me", "00:00:24", "Potrzebujemy drugiej oferty."),
            ("them", "00:00:31", "I'll take it, by Friday."),
        ),
    },
    "en": {
        "lines": ("Ucho listens.", "It never tells."),
        "title": "Ucho listens. It never tells.",
        "desc": "Private meeting and dictation transcripts, offline on your laptop.",
        "me": "Me", "them": "Meeting",
        "transcript": (
            ("me", "00:00:04", "Let's start with the Q1 budget."),
            ("them", "00:00:11", "The supplier raised prices 10%."),
            ("me", "00:00:24", "We need a second quote."),
            ("them", "00:00:31", "Biorę to, do piątku."),
        ),
    },
}

EAR_OUTER = "M 92 70 C 92 44 112 28 134 28 C 160 28 178 48 178 74 C 178 96 164 104 158 118 C 152 132 154 150 136 160 C 124 166 110 160 108 150"
EAR_INNER = "M 116 76 C 116 62 126 54 136 54 C 148 54 156 64 156 74 C 156 86 144 90 138 98 C 132 106 136 116 126 120"
WAVES = ("M 52 70 A 58 58 0 0 0 52 130", "M 68 80 A 38 38 0 0 0 68 120", "M 82 89 A 18 18 0 0 0 82 111")
BARS = (0.25, 0.45, 0.7, 0.95, 0.6, 1.0, 0.75, 0.9, 0.5, 0.8, 0.4, 0.2)


class Setter:
    """Shapes a line with HarfBuzz and returns it as one SVG path."""

    def __init__(self, font_path: Path):
        self.font = TTFont(font_path)
        self.upem = self.font["head"].unitsPerEm
        self.glyphs = self.font.getGlyphSet(location=AXES)
        self.order = self.font.getGlyphOrder()
        blob = hb.Blob.from_file_path(str(font_path))
        self.hb_font = hb.Font(hb.Face(blob))
        self.hb_font.set_variations(AXES)

    def path(self, text: str, x: float, baseline: float, size: float) -> tuple[str, float]:
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb_font, buf, {"kern": True, "liga": True})
        scale = size / self.upem
        track = TRACKING * self.upem
        pen_x, parts = 0.0, []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions, strict=True):
            pen = SVGPathPen(self.glyphs)
            self.glyphs[self.order[info.codepoint]].draw(pen)
            d = pen.getCommands()
            if d:
                gx = x + (pen_x + pos.x_offset) * scale
                parts.append(f'<path transform="translate({gx:.2f} {baseline:.2f}) scale({scale:.5f} {-scale:.5f})" d="{d}"/>')
            pen_x += pos.x_advance + track
        return "".join(parts), pen_x * scale


def keyframes(name: str, stops: list[tuple[float, str]]) -> str:
    body = " ".join(f"{p:.2f}% {{ {css} }}" for p, css in stops)
    return f"@keyframes {name} {{ {body} }}"


def build(setter: Setter, lang: str, theme: str) -> str:
    c, t = COPY[lang], THEMES[theme]
    line1, _ = setter.path(c["lines"][0], 72, 152, HEADLINE_SIZE)
    line2, _ = setter.path(c["lines"][1], 72, 262, HEADLINE_SIZE)

    # Transcript timing: each line is "spoken" for a while, then typed.
    fs, char_w = 20, 0.62 * 20
    x_ts, x_who, x_text = 440, 556, 690
    css, rows, speak = [], [], []
    for i, (who, ts, text) in enumerate(c["transcript"]):
        y = 438 + i * 58
        start = 0.6 + i * 3.1
        type_from, type_to = start + 0.8, start + 0.8 + 0.045 * len(text)
        p = [100 * v / CYCLE for v in (start, type_from, type_to)]
        width = len(text) * char_w + 12
        css.append(keyframes(f"show{i}", [(0, "opacity: 0"), (p[0], "opacity: 0"), (p[0] + 0.01, "opacity: 1"), (93, "opacity: 1"), (97, "opacity: 0"), (100, "opacity: 0")]))
        css.append(keyframes(f"type{i}", [
            (0, "transform: translateX(0)"),
            (p[1], f"transform: translateX(0); animation-timing-function: steps({len(text)}, end)"),
            (p[2], f"transform: translateX({width:.0f}px)"),
            (100, f"transform: translateX({width:.0f}px)"),
        ]))
        css.append(f".l{i} {{ animation: show{i} {CYCLE}s linear infinite; }} .c{i} {{ animation: type{i} {CYCLE}s linear infinite; }}")
        speak.append((p[0], p[2]))
        colour = ME if who == "me" else THEM
        rows.append(
            f'<g class="l{i}">'
            f'<text x="{x_ts}" y="{y}" class="mono" fill="{BAND_MUTED}">{ts}</text>'
            f'<text x="{x_who}" y="{y}" class="mono b" fill="{colour}">{escape(c[who])}</text>'
            f'<text x="{x_text}" y="{y}" class="mono" fill="{BAND_TEXT}">{escape(text)}</text>'
            f'<rect class="c{i}" x="{x_text - 2}" y="{y - fs}" width="{width:.0f}" height="{fs + 10}" fill="{t["band"]}"/>'
            f"</g>"
        )

    # Voice bars: loud while a line is spoken, quiet in between.
    stops = [(0.0, "opacity: .25")]
    for a, b in speak:
        stops += [(a, "opacity: .25"), (a + 0.5, "opacity: 1"), (b, "opacity: 1"), (b + 0.5, "opacity: .25")]
    stops.append((100.0, "opacity: .25"))
    css.append(keyframes("speak", stops))

    bars = []
    bar_w, gap, x0, mid, height = 7, 4, 72, 520, 120
    for i, amp in enumerate(BARS):
        bx = x0 + i * (bar_w + gap)
        bars.append(f'<rect class="bar" style="--a:{amp};animation-delay:-{(i * 0.37) % 0.85:.2f}s" '
                    f'x="{bx}" y="{mid - height / 2}" width="{bar_w}" height="{height}" rx="3"/>')

    style = f"""
    .mono {{ font: 400 {fs}px "Cascadia Mono", "SF Mono", Menlo, Consolas, "Liberation Mono", monospace; }}
    .b {{ font-weight: 700; }}
    .bar {{ fill: {ME}; transform-box: fill-box; transform-origin: center; animation: talk .85s ease-in-out infinite alternate; }}
    .voice {{ animation: speak {CYCLE}s linear infinite; }}
    .wave {{ fill: none; stroke: {ME}; stroke-width: 8; stroke-linecap: round; opacity: 0; animation: wave 1.8s cubic-bezier(.22,1,.36,1) infinite; }}
    .w2 {{ animation-delay: .25s; }} .w3 {{ animation-delay: .5s; }}
    @keyframes talk {{ from {{ transform: scaleY(.08); }} to {{ transform: scaleY(var(--a)); }} }}
    @keyframes wave {{ 0% {{ opacity: 0; }} 25% {{ opacity: .9; }} 70% {{ opacity: 0; }} 100% {{ opacity: 0; }} }}
    {" ".join(css)}
    @media (prefers-reduced-motion: reduce) {{
      .bar, .voice, .wave, [class^="l"], [class^="c"] {{ animation: none !important; }}
      .bar {{ transform: scaleY(var(--a)); }} .wave {{ opacity: .7; }} [class^="c"] {{ opacity: 0; }}
    }}"""

    waves = "".join(f'<path class="wave w{i + 1}" d="{d}"/>' for i, d in enumerate(WAVES))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="t d">
<title id="t">{escape(c["title"])}</title>
<desc id="d">{escape(c["desc"])}</desc>
<style>{style}
</style>
<defs><clipPath id="r"><rect width="{W}" height="{H}" rx="{R}"/></clipPath></defs>
<g clip-path="url(#r)">
<rect width="{W}" height="{H}" fill="{t["bg"]}"/>
<g fill="{t["text"]}">{line1}</g>
<g fill="{t["accent"]}">{line2}</g>
<rect y="{BAND_Y}" width="{W}" height="{H - BAND_Y}" fill="{t["band"]}"/>
<g class="voice">{"".join(bars)}</g>
<g transform="translate(176 418) scale(1.12)">{waves}<path d="{EAR_OUTER}" fill="none" stroke="{ME}" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/><path d="{EAR_INNER}" fill="none" stroke="#DF9C86" stroke-width="11" stroke-linecap="round"/></g>
{"".join(rows)}
<text x="{W - 72}" y="{H - 40}" text-anchor="end" class="mono" font-size="16" fill="{BAND_MUTED}">ucho.app</text>
</g>
</svg>
"""


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    setter = Setter(Path(argv[0]))
    for lang in COPY:
        for theme in THEMES:
            target = OUT / f"banner-{lang}-{theme}.svg"
            target.write_text(build(setter, lang, theme), encoding="utf-8")
            print(target.relative_to(OUT.parent.parent), f"{target.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
