# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dictation paste: target window check and restoring the previous clipboard.

The Win32 calls (and the delayed-rendering clipboard owner) are replaced with
fakes; nothing touches the real clipboard or sends keys. The real
`DelayedClipboard` is tested in test_dictation_delayed_clipboard.py.
"""
from __future__ import annotations

import pytest

TARGET, OTHER, OURS, DESKTOP = 101, 202, 303, 404
PIDS = {TARGET: 11, OTHER: 22, OURS: 33, DESKTOP: 44}


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
                                    lambda _w: exists, activate, lambda w: w == DESKTOP)
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


def test_desktop_or_taskbar_in_front_means_clipboard_only(dictation):
    # Started on the desktop (no target) and the desktop is still in front.
    assert _target(dictation, None, DESKTOP) == (dictation.NO_WINDOW, 0)
    assert _target(dictation, None, None) == (dictation.NO_WINDOW, 0)
    # Started from Ucho, then the desktop was clicked.
    assert _target(dictation, OURS, DESKTOP) == (dictation.NO_WINDOW, 0)


class FakeDesktop:
    """Clipboard, windows, keyboard and the delayed-rendering owner for insert_text.

    `reader` is the process that asks for the text after Ctrl+V: the target's
    (TARGET's pid), another one, or None for a window that ignores Ctrl+V or
    does not get to it within the timeout.
    """

    def __init__(self, front, clipboard="previous", activates=True, reader=PIDS[TARGET]):
        self.front = front
        self.clipboard = clipboard
        self.activates = activates
        self.reader = reader
        self.offer_fails = False
        self.copy_after_paste = False
        self.owner = None
        self.pasted = False
        self.taken: list = []
        self.events: list = []

    def install(self, monkeypatch, dictation):
        desk = self

        def set_clipboard(text, private=True):
            desk.clipboard, desk.owner = text, None
            desk.events.append(("set", text, private))
            return 1

        def activate(window):
            if desk.activates:
                desk.front = window
            return desk.activates

        def press_ctrl_v():
            desk.events.append(("paste", desk.front))
            desk.pasted = True
            if desk.owner is None:  # no delayed rendering: the target reads at once
                desk.taken.append(desk.clipboard)

        class FakeOffer:
            def __init__(self, text, consumer_pid):
                self.text, self.consumer_pid = text, consumer_pid
                self.rendered = self.confirmed = self.lost = False

            def offer(self):
                if desk.offer_fails:
                    raise OSError("no window")
                desk.owner, desk.clipboard = self, ("delayed", self.text)
                desk.events.append(("offer", self.text))

            def pump(self, seconds, until=None):
                if desk.pasted and desk.reader and not self.rendered and desk.owner is self:
                    self.rendered = True
                    self.confirmed = desk.reader == self.consumer_pid
                    desk.clipboard = self.text
                    desk.taken.append(self.text)
                    if desk.copy_after_paste:
                        desk.owner, desk.clipboard, self.lost = None, "copied meanwhile", True
                return bool(until and until())

            def restore(self, previous):
                if desk.owner is not self:
                    return False
                desk.owner, desk.clipboard = None, previous
                desk.events.append(("restore", previous))
                return True

            def close(self):
                if desk.owner is self and not self.rendered:
                    desk.clipboard, self.rendered = self.text, True
                desk.events.append(("close",))

        patches = {
            "wait_for_keys_released": lambda: None,
            "foreground_window": lambda: desk.front,
            "window_is_ours": lambda w: w == OURS,
            "is_shell_window": lambda w: w == DESKTOP,
            "window_exists": lambda _w: True,
            "window_process": lambda w: PIDS.get(w),
            "activate": activate,
            "set_clipboard": set_clipboard,
            "get_clipboard_text": lambda: desk.clipboard,
            "press_ctrl_v": press_ctrl_v,
            "press_enter": lambda: desk.events.append(("enter", desk.front, list(desk.taken))),
            "type_text": lambda text: desk.events.append(("type", desk.front, text)),
            "DelayedClipboard": FakeOffer,
        }
        for name, value in patches.items():
            monkeypatch.setattr(dictation, name, value)
        monkeypatch.setattr(dictation.time, "sleep", lambda _s: None)
        return self


def test_confirmed_paste_restores_the_previous_text(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET).install(monkeypatch, dictation)
    delivered = []
    assert dictation.insert_text("hello ", "paste", False, TARGET, delivered.append) == dictation.PASTE
    assert delivered == [dictation.PASTE]
    assert desk.taken == ["hello "]
    assert desk.events == [("offer", "hello "), ("paste", TARGET), ("restore", "previous"), ("close",)]
    assert desk.clipboard == "previous"


def test_slow_target_never_gets_the_previous_text(monkeypatch, dictation):
    # The target does not ask for the text within the timeout (remote desktop,
    # a busy app): no Enter, nothing restored, the dictation stays for Ctrl+V.
    desk = FakeDesktop(front=TARGET, reader=None).install(monkeypatch, dictation)
    delivered = []
    result = dictation.insert_text("hello", "paste", True, TARGET, delivered.append)
    assert result == delivered[0] == dictation.NOT_TAKEN
    assert not [e for e in desk.events if e[0] in ("enter", "restore")]
    assert desk.clipboard == "hello"
    # A very late paste gets the dictation, never the previous clipboard.
    assert "previous" not in desk.taken


def test_enter_only_after_the_target_took_the_text(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET).install(monkeypatch, dictation)
    dictation.insert_text("hello team", "paste", True, TARGET)
    assert desk.events == [("offer", "hello team"), ("paste", TARGET), ("enter", TARGET, ["hello team"]),
                           ("restore", "previous"), ("close",)]


def test_text_taken_by_another_process_is_not_followed_by_a_restore(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET, reader=PIDS[OTHER]).install(monkeypatch, dictation)
    assert dictation.insert_text("hello", "paste", False, TARGET) == dictation.PASTE
    assert ("restore", "previous") not in desk.events
    assert desk.clipboard == "hello"


def test_paste_does_not_overwrite_a_newer_copy(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET).install(monkeypatch, dictation)
    desk.copy_after_paste = True
    dictation.insert_text("hello", "paste", False, TARGET)
    assert desk.clipboard == "copied meanwhile"


def test_paste_clears_when_there_was_no_text_before(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET, clipboard=None).install(monkeypatch, dictation)
    dictation.insert_text("hello", "paste", False, TARGET)
    assert ("restore", None) in desk.events
    assert desk.clipboard is None


def test_without_delayed_rendering_the_previous_text_is_not_restored(monkeypatch, dictation):
    desk = FakeDesktop(front=TARGET).install(monkeypatch, dictation)
    desk.offer_fails = True
    assert dictation.insert_text("hello", "paste", True, TARGET) == dictation.PASTE
    assert desk.taken == ["hello"]
    assert [e for e in desk.events if e != ("close",)] == [
        ("set", "hello", True), ("paste", TARGET), ("enter", TARGET, ["hello"])]
    assert desk.clipboard == "hello"


def test_lost_focus_leaves_the_text_and_does_not_paste(monkeypatch, dictation):
    desk = FakeDesktop(front=OTHER, activates=False).install(monkeypatch, dictation)
    delivered = []
    result = dictation.insert_text("secret", "paste", True, TARGET, delivered.append)
    assert result == delivered[0] == dictation.FOCUS_LOST
    assert desk.events == [("set", "secret", True)]
    assert desk.clipboard == "secret"


@pytest.mark.parametrize("mode", ["paste", "type"])
def test_dictation_on_the_desktop_stays_on_the_clipboard(monkeypatch, dictation, mode):
    desk = FakeDesktop(front=DESKTOP).install(monkeypatch, dictation)
    delivered = []
    result = dictation.insert_text("dictated words ", mode, True, None, delivered.append)
    assert result == delivered[0] == dictation.NO_WINDOW
    assert desk.events == [("set", "dictated words ", True)]
    assert desk.clipboard == "dictated words "


def test_clipboard_only_results_show_the_paste_hint(dictation):
    assert set(dictation.CLIPBOARD_ONLY) == {dictation.FOCUS_LOST, dictation.NO_WINDOW,
                                             dictation.NOT_TAKEN}


def test_type_mode_does_not_touch_the_clipboard(monkeypatch, dictation):
    desk = FakeDesktop(front=OTHER).install(monkeypatch, dictation)
    assert dictation.insert_text("hi", "type", True, TARGET) == dictation.PASTE
    assert desk.events == [("type", TARGET, "hi"), ("enter", TARGET, [])]
    assert desk.clipboard == "previous"
