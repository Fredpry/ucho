# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Parsing of the hotkey settings ("<ctrl>+<alt>+r") for RegisterHotKey."""
import pytest

from ucho import config, hotkeys


@pytest.mark.parametrize("combo, expected", [
    ("<ctrl>+<alt>+r", (hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, 0x52)),
    ("<ctrl>+<shift>+space", (hotkeys.MOD_CONTROL | hotkeys.MOD_SHIFT, 0x20)),
    ("Ctrl + Alt + T", (hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, 0x54)),
    ("<win>+<shift>+f9", (hotkeys.MOD_WIN | hotkeys.MOD_SHIFT, 0x78)),
    ("<ctrl>+<alt>+5", (hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, 0x35)),
])
def test_parse(combo, expected):
    assert hotkeys.parse(combo) == expected


@pytest.mark.parametrize("combo", ["", "r", "<ctrl>", "<ctrl>+<alt>+r+t", "<ctrl>+<hyper>+r"])
def test_parse_rejects(combo):
    assert hotkeys.parse(combo) is None


def test_defaults_parse_and_labels():
    assert hotkeys.parse(config.DEFAULTS["hotkey"]) is not None
    assert hotkeys.parse(config.DEFAULTS["dictation_hotkey"]) is not None
    assert hotkeys.label(config.DEFAULTS["hotkey"]) == "Ctrl + Alt + R"
    assert hotkeys.label(config.DEFAULTS["dictation_hotkey"]) == "Ctrl + Shift + Space"
