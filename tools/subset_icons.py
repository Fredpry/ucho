# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Builds the macOS icon font `design/icons/FluentSystemIcons-Ucho.ttf`.

On Windows the UI draws its icons from Segoe Fluent Icons in %WINDIR%\\Fonts.
macOS has no such font, so the app ships a small subset of Microsoft's Fluent
UI System Icons (MIT, the same visual style). The subset keeps only the icons
the UI uses and puts each one at the Segoe code point of the same icon, so
`ucho/platforms/macos/fonts.py` and the Windows backend share one glyph table.

Source: the "Regular" font and its name map from
https://github.com/microsoft/fluentui-system-icons at tag `SOURCE_TAG`
(commit `SOURCE_COMMIT`). Download both files yourself (they are about 3 MB
and do not belong in the repository):

    https://raw.githubusercontent.com/microsoft/fluentui-system-icons/<commit>/fonts/FluentSystemIcons-Regular.ttf
    https://raw.githubusercontent.com/microsoft/fluentui-system-icons/<commit>/fonts/FluentSystemIcons-Regular.json

Usage (developer machine; `fonttools` is not an app dependency):
    python -m pip install fonttools
    python tools/subset_icons.py <FluentSystemIcons-Regular.ttf> <FluentSystemIcons-Regular.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "design" / "icons" / "FluentSystemIcons-Ucho.ttf"

SOURCE_TAG = "1.1.347"
SOURCE_COMMIT = "6c2c9075cf8952ef48369b2475233b6e9761efed"
SOURCE_SHA256 = "dcbc53cef37733e7b872c9909aec4d09dab7583879ccebc6c8574236280f1efb"
MAX_BYTES = 100_000

# UI icon name (keys of the Windows GLYPHS table) -> Fluent icon name.
FLUENT_NAMES = {
    "meeting": "ic_fluent_record_24_regular",
    "mic": "ic_fluent_mic_24_regular",
    "list": "ic_fluent_text_bullet_list_ltr_24_regular",
    "settings": "ic_fluent_settings_24_regular",
    "search": "ic_fluent_search_24_regular",
    "copy": "ic_fluent_copy_24_regular",
    "folder": "ic_fluent_folder_open_24_regular",
    "back": "ic_fluent_arrow_left_24_regular",
    "refresh": "ic_fluent_arrow_clockwise_24_regular",
    "stop": "ic_fluent_stop_24_regular",
    "info": "ic_fluent_info_24_regular",
    "warning": "ic_fluent_warning_24_regular",
    "check": "ic_fluent_checkmark_24_regular",
    "window": "ic_fluent_open_24_regular",
    "summary": "ic_fluent_chat_24_regular",
    "speed": "ic_fluent_top_speed_24_regular",
    "close": "ic_fluent_dismiss_24_regular",
    "delete": "ic_fluent_delete_24_regular",
    "edit": "ic_fluent_edit_24_regular",
}


def _segoe_glyphs() -> dict[str, str]:
    sys.path.insert(0, str(ROOT))
    from ucho.platforms.windows.fonts import GLYPHS

    return dict(GLYPHS)


def build(source: Path, names_file: Path, output: Path = OUTPUT) -> Path:
    import hashlib

    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._c_m_a_p import cmap_format_4

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != SOURCE_SHA256:
        raise SystemExit(f"{source.name}: sha256 {digest} is not the pinned source {SOURCE_SHA256}")
    segoe = _segoe_glyphs()
    if set(segoe) != set(FLUENT_NAMES):
        raise SystemExit(f"Icon names differ from the Windows table: {sorted(set(segoe) ^ set(FLUENT_NAMES))}")
    codepoints = json.loads(names_file.read_text(encoding="utf-8"))

    font = TTFont(str(source))
    best = font.getBestCmap()
    glyph_names = {}
    for name, fluent in FLUENT_NAMES.items():
        if fluent not in codepoints or codepoints[fluent] not in best:
            raise SystemExit(f"{fluent} is not in the source font")
        glyph_names[name] = best[codepoints[fluent]]

    options = subset.Options()
    options.layout_features = []
    options.name_IDs = ["*"]
    options.notdef_outline = True
    options.drop_tables += ["GSUB", "GPOS", "GDEF"]
    subsetter = subset.Subsetter(options)
    subsetter.populate(glyphs=sorted(set(glyph_names.values())))
    subsetter.subset(font)

    # One Windows Unicode BMP table: each icon at its Segoe code point.
    table = cmap_format_4(4)
    table.platformID, table.platEncID, table.language = 3, 1, 0
    table.cmap = {ord(segoe[name]): glyph for name, glyph in glyph_names.items()}
    font["cmap"].tables = [table]

    output.parent.mkdir(parents=True, exist_ok=True)
    font.save(str(output))
    size = output.stat().st_size
    if size >= MAX_BYTES:
        raise SystemExit(f"{output.name} is {size} bytes, over the {MAX_BYTES} limit")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="FluentSystemIcons-Regular.ttf")
    parser.add_argument("names", type=Path, help="FluentSystemIcons-Regular.json")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    path = build(args.source, args.names, args.output)
    sys.stdout.write(f"{path} ({path.stat().st_size} bytes, {len(FLUENT_NAMES)} icons)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
