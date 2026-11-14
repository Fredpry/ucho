# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""The UI theme follows design/tokens.json and keeps the WCAG contrast of the design system."""
import json

import pytest

from ucho import theme


def test_fallback_matches_tokens_json():
    tokens = json.loads(theme.TOKENS_FILE.read_text(encoding="utf-8"))
    assert tokens["color"]["semantic"] == theme.FALLBACK
    assert tokens["color"]["status"] == theme.FALLBACK_STATUS
    assert (tokens["color"]["blush"]["600"], tokens["color"]["blush"]["300"]) == theme.FALLBACK_FOCUS


def test_palette_without_tokens_file_equals_palette_with_it():
    assert theme.palette({}) == theme.palette()


@pytest.mark.parametrize("mode", [0, 1])
@pytest.mark.parametrize("foreground, background", [
    ("text", "bg"), ("text", "surface"), ("text_muted", "bg"), ("text_muted", "surface"),
    ("on_action", "action"), ("action", "bg"), ("recording_ink", "recording_soft"),
    ("success_ink", "success_soft"), ("info_ink", "info_soft"), ("warning_ink", "warning_soft"),
    ("text", "selected"), ("text", "info_soft"), ("success", "surface"), ("warning", "surface"),
    ("text_muted", "surface_muted"),
])
def test_text_contrast_is_at_least_aa(mode, foreground, background):
    c = theme.C
    ratio = theme.contrast(getattr(c, foreground)[mode], getattr(c, background)[mode])
    assert ratio >= 4.5, f"{foreground} on {background} ({'dark' if mode else 'light'}): {ratio:.2f}"


def test_mix_and_contrast_helpers():
    assert theme.mix("#FFFFFF", "#000000", 0.5) == "#808080"
    assert round(theme.contrast("#000000", "#FFFFFF"), 1) == 21.0
