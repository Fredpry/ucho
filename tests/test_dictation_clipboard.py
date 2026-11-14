# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dictation paste: target window check and restoring the previous clipboard.

The Win32 calls are replaced with fakes; nothing touches the real clipboard
or sends keys.
"""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictation uses WinAPI")

TARGET, OTHER, OURS = 101, 202, 303


@pytest.fixture
def dictation():
    from ucho import dictation

    return dictation


def _target(dictation, target, foreground, exists=True, activates=True):
    """paste_target with a fake desktop; returns (result, activation attempts)."""
    state = {"front": foreground, "attempts": 0}

    def activate(window):
        state["attempts"] += 1
        if activates:
            state["front"] = window
        return activates

    result = dictation.paste_target(target, lambda: state["front"], lambda w: w == OURS,
                                    lambda _w: exists, activate)
    return result, state["attempts"]


def test_paste_when_the_target_is_still_in_front(dictation):
    assert _target(dictation, TARGET, TARGET) == (dictation.PASTE, 0)


def test_no_target_pastes_into_the_active_window(dictation):
    assert _target(dictation, None, OTHER) == (dictation.PASTE, 0)
    assert _target(dictation, None, OURS) == (dictation.OWN_WINDOW, 0)


def test_started_from_ucho_counts_as_no_target(dictation):
    assert _target(dictation, OURS, OTHER) == (dictation.PASTE, 0)
    assert _target(dictation, OURS, OURS) == (dictation.OWN_WINDOW, 0)


def test_target_brought_back_once(dictation):
    assert _target(dictation, TARGET, OTHER) == (dictation.PASTE, 1)
    assert _target(dictation, TARGET, OURS) == (dictation.PASTE, 1)


def test_focus_refused_keeps_the_text_on_the_clipboard(dictation):
    assert _target(dictation, TARGET, OTHER, activates=False) == (dictation.FOCUS_LOST, 1)


def test_closed_target_is_not_activated(dictation):
    assert _target(dictation, TARGET, OTHER, exists=False) == (dictation.FOCUS_LOST, 0)


def test_restore_only_when_the_clipboard_is_still_ours(dictation):
    assert dictation.restore_action("old", 7, 7) == dictation.RESTORE
    assert dictation.restore_action(None, 7, 7) == dictation.CLEAR
    assert dictation.restore_action("old", 7, 8) == dictation.KEEP
    assert dictation.restore_action(None, 7, 9) == dictation.KEEP


class FakeDesktop:
    """Clipboard, windows and keyboard for insert_text."""

    def __init__(self, front, clipboard="previous", activates=True):
        self.front = front
        self.clipboard = clipboard
        self.sequence = 1
        self.activates = activates
        self.events: list = []

    def install(self, monkeypatch, dictation):
        def set_clipboard(text, private=True):
            self.clipboard = text
            self.sequence += 1
            self.events.append(("set", text, private))
            return self.sequence

        def activate(window):
            if self.activates:
                self.front = window
            return self.activates

        patches = {
            "wait_for_keys_released": lambda: None,
            "foreground_window": lambda: self.front,
            "window_is_ours": lambda w: w == OURS,
            "window_exists": lambda _w: True,
            "activate": activate,
            "set_clipboard": set_clipboard,
            "get_clipboard_text": lambda: self.clipboard,
            "clipboard_sequence": lambda: self.sequence,
            "press_ctrl_v": lambda: self.events.append(("paste", self.front, self.clipboard)),
            "press_enter": lambda: self.events.append(("enter", self.front)),
            "type_text": lambda text: self.events.append(("type", self.front, text)),
        }
        for name, value in patches.items():
            monkeypatch.setattr(dictation, name, value)
        monkeypatch.setattr(dictation.time, "sleep", lambda _s: None)


def test_paste_restores_the_previous_text(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET)
    desk.install(monkeypatch, dictation)
    delivered = []
    assert dictation.insert_text("hello ", "paste", False, TARGET, delivered.append) == dictation.PASTE
    assert delivered == [dictation.PASTE]
    assert desk.events == [("set", "hello ", True), ("paste", TARGET, "hello "),
                           ("set", "previous", True)]
    assert desk.clipboard == "previous"


def test_paste_does_not_overwrite_a_newer_copy(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET)
    desk.install(monkeypatch, dictation)
    original = dictation.press_ctrl_v

    def paste_then_user_copies():
        original()
        desk.clipboard, desk.sequence = "copied meanwhile", desk.sequence + 1

    monkeypatch.setattr(dictation, "press_ctrl_v", paste_then_user_copies)
    dictation.insert_text("hello", "paste", False, TARGET)
    assert desk.clipboard == "copied meanwhile"


def test_paste_clears_when_there_was_no_text_before(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET, clipboard=None)
    desk.install(monkeypatch, dictation)
    dictation.insert_text("hello", "paste", False, TARGET)
    assert desk.events[-1] == ("set", None, True)
    assert desk.clipboard is None


def test_lost_focus_leaves_the_text_and_does_not_paste(monkeypatch, dictation):
    desk = FakeDesktop(front=OTHER, activates=False)
    desk.install(monkeypatch, dictation)
    delivered = []
    result = dictation.insert_text("secret", "paste", True, TARGET, delivered.append)
    assert result == delivered[0] == dictation.FOCUS_LOST
    assert desk.events == [("set", "secret", True)]
    assert desk.clipboard == "secret"


def test_type_mode_does_not_touch_the_clipboard(monkeypatch, dictation):
    desk = FakeDesktop(front=OTHER)
    desk.install(monkeypatch, dictation)
    assert dictation.insert_text("hi", "type", True, TARGET) == dictation.PASTE
    assert desk.events == [("type", TARGET, "hi"), ("enter", TARGET)]
    assert desk.clipboard == "previous"
