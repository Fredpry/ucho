# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS global hotkeys: key and modifier mapping, labels, known system shortcuts,
the Carbon-backed listener with a stand-in binding (every system), and the real
Carbon registration with synthetic key events (macOS only)."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ucho import config, hotkeys, i18n, platforms
from ucho.platforms import base
from ucho.platforms.macos import hotkeys as mac

ROOT = Path(__file__).resolve().parent.parent
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only: Carbon hotkeys")


# --- pure functions -------------------------------------------------------------------------


def test_defaults_map_to_carbon_codes():
    assert mac.to_carbon(config.DEFAULTS["hotkey"]) == (0x0F, mac.CONTROL_KEY | mac.OPTION_KEY)
    assert mac.to_carbon(config.DEFAULTS["dictation_hotkey"]) == (0x31, mac.CONTROL_KEY | mac.SHIFT_KEY)


@pytest.mark.parametrize("combo, expected", [
    ("<cmd>+<shift>+f9", (0x65, mac.CMD_KEY | mac.SHIFT_KEY)),
    ("<win>+<alt>+5", (0x17, mac.CMD_KEY | mac.OPTION_KEY)),
    ("<ctrl>+<alt>+0", (0x1D, mac.CONTROL_KEY | mac.OPTION_KEY)),
    ("<ctrl>+enter", (0x24, mac.CONTROL_KEY)),
    ("<ctrl>+tab", (0x30, mac.CONTROL_KEY)),
    ("<ctrl>+<alt>+j", (0x26, mac.CONTROL_KEY | mac.OPTION_KEY)),
    ("<ctrl>+f1", (0x7A, mac.CONTROL_KEY)),
    ("<ctrl>+f12", (0x6F, mac.CONTROL_KEY)),
])
def test_to_carbon(combo, expected):
    assert mac.to_carbon(combo) == expected


def test_every_parsable_key_has_a_carbon_code():
    codes = {vk: mac.carbon_key(vk) for vk in set(hotkeys._KEYS.values())}
    assert all(code is not None for code in codes.values()), codes
    # Two Windows keys never land on the same Mac key.
    assert len(set(codes.values())) == len(codes)
    assert mac.to_carbon("r") is None
    assert mac.to_carbon("<ctrl>+<hyper>+r") is None
    assert mac.carbon_key(0x2C) is None  # no such key in hotkeys._KEYS


def test_carbon_modifiers():
    assert mac.carbon_modifiers(0) == 0
    assert mac.carbon_modifiers(hotkeys.MOD_WIN) == mac.CMD_KEY
    everything = hotkeys.MOD_CONTROL | hotkeys.MOD_ALT | hotkeys.MOD_SHIFT | hotkeys.MOD_WIN
    assert mac.carbon_modifiers(everything | hotkeys.MOD_NOREPEAT) == (
        mac.CONTROL_KEY | mac.OPTION_KEY | mac.SHIFT_KEY | mac.CMD_KEY)


def test_mac_labels_use_symbols_in_apple_order():
    assert hotkeys.label("<ctrl>+<alt>+r", "macos") == "⌃⌥R"
    assert hotkeys.label("<ctrl>+<shift>+space", "macos") == "⌃⇧Space"
    assert hotkeys.label("<cmd>+<shift>+f9", "macos") == "⇧⌘F9"
    assert hotkeys.label("<shift>+<win>+<control>+<alt>+a", "macos") == "⌃⌥⇧⌘A"
    assert hotkeys.label("<ctrl>+enter", "macos") == "⌃Return"
    assert hotkeys.label("", "macos") == ""
    with platforms.override("macos"):
        assert hotkeys.label(config.DEFAULTS["hotkey"]) == "⌃⌥R"
    with platforms.override("windows"):
        assert hotkeys.label(config.DEFAULTS["hotkey"]) == "Ctrl + Alt + R"


def test_mac_label_is_translated():
    i18n.set_language("pl")
    assert hotkeys.label("<ctrl>+<shift>+space", "macos") == "⌃⇧Spacja"


@pytest.mark.parametrize("combo, purpose", [
    ("<cmd>+space", "Spotlight"),
    ("<ctrl>+space", "switching the input source"),
    ("<ctrl>+<alt>+space", "switching the input source"),
    ("<ctrl>+<cmd>+space", "emoji and symbols"),
    ("<cmd>+<alt>+space", "Finder search"),
    ("<cmd>+tab", "switching apps"),
    ("<cmd>+<alt>+esc", "force quitting apps"),
    ("<ctrl>+<cmd>+q", "locking the screen"),
    ("<cmd>+<shift>+4", "screenshots"),
    ("<ctrl>+<cmd>+<shift>+4", "screenshots"),
    ("<cmd>+c", "app menu shortcuts"),
    ("<cmd>+<shift>+n", "app menu shortcuts"),
])
def test_known_macos_conflicts(combo, purpose):
    assert hotkeys.macos_conflict(combo) == purpose


@pytest.mark.parametrize("combo", ["<ctrl>+<alt>+r", "<ctrl>+<shift>+space", "<cmd>+<ctrl>+<alt>+d",
                                   "<ctrl>+f5", "nonsense"])
def test_no_conflict_for_defaults_and_free_combos(combo):
    assert hotkeys.macos_conflict(combo) is None


def check_contract(protocol: type, impl: object) -> None:
    """Method names, parameter names and defaults as in `base` (like tests/test_platforms.py)."""
    assert isinstance(impl, protocol)
    for name, member in protocol.__dict__.items():
        if name.startswith("_") or not callable(member):
            continue
        expected = list(inspect.signature(member).parameters.values())[1:]
        actual = list(inspect.signature(getattr(impl, name)).parameters.values())
        assert [p.name for p in actual] == [p.name for p in expected], name
        assert [p.default for p in actual] == [p.default for p in expected], name


# --- listener with a stand-in Carbon binding ---------------------------------------------------


class FakeCarbon:
    """Records what MacHotkeys asks of Carbon; `busy` holds (key, modifiers) taken elsewhere."""

    def __init__(self, busy=(), install_status=0):
        self.busy = set(busy)
        self.install_status = install_status
        self.handler = None
        self.removed = 0
        self.registered: dict[object, tuple[int, int, int, int]] = {}
        self.unregistered: list[tuple[int, int, int, int]] = []

    def install(self, on_hotkey):
        if self.install_status:
            return self.install_status, None
        self.handler = on_hotkey
        return 0, ("token", on_hotkey)

    def remove(self, token):
        assert token[1] is self.handler
        self.handler = None
        self.removed += 1
        return 0

    def register(self, key, modifiers, number, options):
        taken = {(k, m) for k, m, _n, _o in self.registered.values()} | self.busy
        if (key, modifiers) in taken:
            return mac.EVENT_HOTKEY_EXISTS, None
        ref = object()
        self.registered[ref] = (key, modifiers, number, options)
        return 0, ref

    def unregister(self, ref):
        self.unregistered.append(self.registered.pop(ref))
        return 0

    def press(self, key, modifiers=0, signature=mac.SIGNATURE):
        for k, m, number, _o in self.registered.values():
            if (k, m) == (key, modifiers):
                return self.handler(signature, number)
        return None


DEFAULT_BINDINGS = {"record": config.DEFAULTS["hotkey"], "dictate": config.DEFAULTS["dictation_hotkey"]}
R, SPACE = 0x0F, 0x31
CTRL_ALT, CTRL_SHIFT = mac.CONTROL_KEY | mac.OPTION_KEY, mac.CONTROL_KEY | mac.SHIFT_KEY


def test_contract():
    check_contract(base.GlobalHotkeys, mac.MacHotkeys(lambda _n: None, carbon=FakeCarbon()))
    check_contract(base.GlobalHotkeys, mac.create(lambda _n: None))


def test_start_registers_exclusive_hotkeys_and_dispatches():
    carbon, fired = FakeCarbon(), []
    keys = mac.MacHotkeys(fired.append, carbon=carbon)
    assert keys.start(DEFAULT_BINDINGS) == {}
    assert sorted((k, m, o) for k, m, _n, o in carbon.registered.values()) == [
        (R, CTRL_ALT, mac.HOTKEY_EXCLUSIVE), (SPACE, CTRL_SHIFT, mac.HOTKEY_EXCLUSIVE)]
    assert carbon.press(R, CTRL_ALT) == mac.NO_ERR
    assert carbon.press(SPACE, CTRL_SHIFT) == mac.NO_ERR
    assert fired == ["record", "dictate"]
    # Another program's hotkey (other signature) or an unknown id is passed on.
    assert carbon.press(R, CTRL_ALT, signature=mac.fourcc("ABCD")) == mac.EVENT_NOT_HANDLED
    assert keys._dispatch(mac.SIGNATURE, 999_999) == mac.EVENT_NOT_HANDLED
    assert fired == ["record", "dictate"]


def test_busy_unrecognized_and_conflicting_shortcuts_are_reported(caplog):
    carbon = FakeCarbon(busy={(R, CTRL_ALT)})
    keys = mac.MacHotkeys(lambda _n: None, carbon=carbon)
    errors = keys.start({"record": "<ctrl>+<alt>+r", "dictate": "<ctrl>+<hyper>+x",
                         "spot": "<cmd>+space"})
    assert errors["record"] == "⌃⌥R is taken by another program (code -9878)."
    assert errors["dictate"] == "Unrecognized shortcut <ctrl>+<hyper>+x."
    assert errors["spot"] == "⌘Space is also a macOS shortcut (Spotlight) and may not work."
    # The system shortcut is still registered: with one input source Ctrl+Space would work.
    assert [(k, m) for k, m, _n, _o in carbon.registered.values()] == [(SPACE, mac.CMD_KEY)]
    assert keys.errors is errors
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_escape_is_borrowed_only_while_enabled():
    carbon, fired = FakeCarbon(), []
    keys = mac.MacHotkeys(fired.append, carbon=carbon)
    keys.start(DEFAULT_BINDINGS)
    assert carbon.press(mac.KVK_ESCAPE) is None
    keys.escape(True)
    keys.escape(True)  # twice: still one registration
    escapes = [v for v in carbon.registered.values() if v[0] == mac.KVK_ESCAPE]
    assert len(escapes) == 1 and escapes[0][1] == 0 and escapes[0][3] == 0  # not exclusive
    assert carbon.press(mac.KVK_ESCAPE) == mac.NO_ERR
    assert fired == [hotkeys.Hotkeys.ESCAPE]
    keys.escape(False)
    assert carbon.press(mac.KVK_ESCAPE) is None
    assert [v[0] for v in carbon.unregistered] == [mac.KVK_ESCAPE]
    keys.escape(False)
    assert len(carbon.registered) == 2


def test_stop_and_restart_release_everything():
    carbon = FakeCarbon()
    keys = mac.MacHotkeys(lambda _n: None, carbon=carbon)
    keys.start(DEFAULT_BINDINGS)
    keys.escape(True)
    # A restart (new bindings) frees the old keys first, or they would be "busy".
    assert keys.start({"record": "<ctrl>+<alt>+r"}) == {}
    assert carbon.removed == 1
    assert len(carbon.unregistered) == 3
    assert [(k, m) for k, m, _n, _o in carbon.registered.values()] == [(R, CTRL_ALT)]
    keys.stop()
    assert carbon.registered == {}
    assert carbon.handler is None and carbon.removed == 2
    keys.stop()  # safe twice
    keys.escape(True)  # after stop: nothing to borrow Esc for
    assert carbon.registered == {}


def test_failing_action_is_logged_not_raised(caplog):
    carbon = FakeCarbon()

    def action(_name):
        raise RuntimeError("boom")

    keys = mac.MacHotkeys(action, carbon=carbon)
    keys.start(DEFAULT_BINDINGS)
    assert carbon.press(R, CTRL_ALT) == mac.NO_ERR
    assert "Hotkey action record failed" in caplog.text


def test_without_carbon_every_shortcut_is_unsupported(monkeypatch):
    def broken():
        raise OSError("no Carbon here")

    monkeypatch.setattr(mac, "CarbonHotkeys", broken)
    keys = mac.MacHotkeys(lambda _n: None)
    errors = keys.start(DEFAULT_BINDINGS)
    assert errors == {name: "Global hotkeys are not supported on this system." for name in DEFAULT_BINDINGS}
    keys.escape(True)
    keys.stop()

    failing = mac.MacHotkeys(lambda _n: None, carbon=FakeCarbon(install_status=-50))
    assert set(failing.start(DEFAULT_BINDINGS)) == set(DEFAULT_BINDINGS)


def test_calls_off_the_main_thread_go_through_on_main():
    carbon, scheduled = FakeCarbon(), []
    keys = mac.MacHotkeys(lambda _n: None, carbon=carbon, on_main=scheduled.append)
    errors = keys.start(DEFAULT_BINDINGS)
    assert errors == {} and carbon.registered == {}  # nothing touched Carbon yet
    for fn in scheduled:
        fn()
    assert len(carbon.registered) == 2


def test_default_on_main_runs_inline_on_the_main_thread_and_skips_without_tk(monkeypatch, caplog):
    ran = []
    mac._on_main_thread(lambda: ran.append("main"))
    assert ran == ["main"]
    import tkinter

    monkeypatch.setattr(tkinter, "_default_root", None, raising=False)
    worker = threading.Thread(target=lambda: mac._on_main_thread(lambda: ran.append("worker")))
    worker.start()
    worker.join()
    assert ran == ["main"]
    assert "off the main thread" in caplog.text


def test_platform_accessor_and_unique_ids():
    with platforms.override("macos"):
        first = platforms.hotkeys(lambda _n: None)
        assert isinstance(first, mac.MacHotkeys)
    a, b = mac._next_id(), mac._next_id()
    assert b > a


# --- real Carbon (macOS only) ---------------------------------------------------------------

# CGEventFlags (CGEventTypes.h).
FLAG_SHIFT, FLAG_CONTROL, FLAG_OPTION = 0x20000, 0x40000, 0x80000


def _post_key(key: int, flags: int) -> None:
    """Synthetic key press and release through CGEventPost (allowed on the CI runner)."""
    import ctypes

    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    for down in (True, False):
        event = cg.CGEventCreateKeyboardEvent(None, key, down)
        cg.CGEventSetFlags(event, flags)
        cg.CGEventPost(0, event)  # kCGHIDEventTap
        cf.CFRelease(event)


def _tk_root():
    import tkinter

    root = tkinter.Tk()
    root.title("Ucho hotkey test")
    root.geometry("240x80+80+80")
    root.update()
    return root


@darwin_only
def test_real_carbon_hotkey_fires_on_the_tk_thread():
    """Criteria 4 (same process) and 5: register, a synthetic Ctrl+Option+R runs the action
    on the main thread, Esc only while enabled, a duplicate is reported, stop frees the keys."""
    root = _tk_root()
    fired: list[tuple[str, bool]] = []
    keys = mac.MacHotkeys(lambda name: fired.append((name, threading.current_thread() is threading.main_thread())))
    other = mac.MacHotkeys(lambda _n: None)
    again = mac.MacHotkeys(lambda _n: None)
    try:
        assert keys.start(DEFAULT_BINDINGS) == {}
        duplicate = other.start({"record": "<ctrl>+<alt>+r"})
        assert "taken by another program (code -9878)" in duplicate["record"]

        steps = {"record": False, "escape": False}
        deadline = time.monotonic() + 15

        def check():
            names = [name for name, _main in fired]
            if not steps["record"] and "record" in names:
                steps["record"] = True
                keys.escape(True)
                root.after(300, lambda: _post_key(mac.KVK_ESCAPE, 0))
            if not steps["escape"] and "escape" in names:
                steps["escape"] = True
                keys.escape(False)
                root.after(300, lambda: _post_key(mac.KVK_ESCAPE, 0))  # must not fire now
                root.after(1200, root.quit)
                return
            if time.monotonic() > deadline:
                root.quit()
                return
            root.after(50, check)

        root.after(800, lambda: _post_key(R, FLAG_CONTROL | FLAG_OPTION))
        root.after(50, check)
        root.mainloop()
        assert fired == [("record", True), ("escape", True)], fired
    finally:
        keys.stop()
        other.stop()
        root.update()
    try:
        assert again.start({"record": "<ctrl>+<alt>+r"}) == {}  # released by stop()
    finally:
        again.stop()
        root.destroy()


CHILD = r"""
import json, sys, tkinter
from ucho import i18n
from ucho.platforms.macos.hotkeys import MacHotkeys
i18n.set_language("en")
root = tkinter.Tk()
root.update()
keys = MacHotkeys(lambda _n: None)
print(json.dumps(keys.start({"record": sys.argv[1]})), flush=True)
keys.stop()
root.destroy()
"""


@darwin_only
def test_real_carbon_reports_a_hotkey_held_by_another_process():
    """Criterion 4: a second process (like a second Ucho) gets the "taken" message."""
    root = _tk_root()
    keys = mac.MacHotkeys(lambda _n: None)
    try:
        assert keys.start({"record": "<ctrl>+<alt>+r"}) == {}
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        proc = subprocess.run([sys.executable, "-c", CHILD, "<ctrl>+<alt>+r"], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        errors = json.loads(proc.stdout.strip().splitlines()[-1])
        assert errors["record"] == "⌃⌥R is taken by another program (code -9878)."
    finally:
        keys.stop()
        root.destroy()
