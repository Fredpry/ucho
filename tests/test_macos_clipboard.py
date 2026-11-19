# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS clipboard, paste and focus (`ucho/platforms/macos/{clipboard,inserter}.py`).

The first part runs everywhere: pure helpers, the pasteboard promise logic
on a fake pasteboard and `dictation.insert_text` under `override("macos")`.
The second part (macOS only, job macos-tests) uses the real NSPasteboard,
`pbpaste`/`pbcopy`, and a small Tk app in a child process as the paste
target; the runner has the Accessibility permission and allows CGEventPost.
"""

from __future__ import annotations

import inspect
import json
import os
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ucho import dictation, platforms
from ucho.platforms import base
from ucho.platforms.macos import clipboard as mac_clipboard
from ucho.platforms.macos import inserter as mac_inserter
from ucho.platforms.macos.clipboard import MacClipboard, MacDelayedOffer
from ucho.platforms.macos.inserter import MacInserter

MACOS_ONLY = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only: real NSPasteboard and Quartz")


def _check_contract(protocol: type, impl: object) -> None:
    assert isinstance(impl, protocol), f"{type(impl).__name__} does not satisfy {protocol.__name__}"
    for name, member in protocol.__dict__.items():
        if name.startswith("_") or not callable(member):
            continue
        proto = list(inspect.signature(member).parameters.values())[1:]
        actual = list(inspect.signature(getattr(impl, name)).parameters.values())
        assert [p.name for p in actual] == [p.name for p in proto], f"{type(impl).__name__}.{name}"
        assert [p.default is inspect.Parameter.empty for p in actual] == [
            p.default is inspect.Parameter.empty for p in proto], f"{type(impl).__name__}.{name} defaults"


class FakePasteboard:
    """NSPasteboard stand-in: a change counter, text, markers and a promise."""

    def __init__(self, text: str | None = "previous") -> None:
        self.count = 1
        self.text = text
        self.private = False
        self.provide = None
        self.writes: list[tuple[str | None, bool]] = []

    def change_count(self) -> int:
        return self.count

    def read_text(self) -> str | None:
        if self.provide is not None:
            self.text, self.provide = self.provide(), None  # AppKit caches what the provider gave
        return self.text

    def write_text(self, text: str | None, private: bool) -> int:
        self.count += 1
        self.text, self.private, self.provide = text, private, None
        self.writes.append((text, private))
        return self.count

    def promise_text(self, provide, private: bool) -> int:
        self.count += 1
        self.text, self.private, self.provide = None, private, provide
        return self.count

    def run_loop(self, seconds: float) -> None:
        time.sleep(min(seconds, 0.001))

    def copy_by_other_app(self, text: str) -> None:
        self.count += 1
        self.text, self.provide = text, None


# --- everywhere: pure helpers ----------------------------------------------------------


def test_contracts():
    _check_contract(base.Clipboard, MacClipboard(FakePasteboard()))
    _check_contract(base.DelayedOffer, MacDelayedOffer(FakePasteboard(), "x", None))
    _check_contract(base.TextInserter, MacInserter())


def test_platform_accessors_pick_the_macos_modules_without_pyobjc():
    with platforms.override("macos"):
        assert isinstance(platforms.inserter(), MacInserter)
        assert isinstance(platforms.clipboard(), MacClipboard)
        assert platforms.inserter().paste_label() == "⌘V"


def test_utf16_chunks_keep_surrogate_pairs_whole():
    text = "zażółć gęślą jaźń " * 3 + "🙂" * 15 + "abc"
    chunks = mac_inserter.utf16_chunks(text)
    assert "".join(chunks) == text
    assert all(mac_inserter.utf16_length(c) <= mac_inserter.TYPE_CHUNK for c in chunks)
    assert all(not ("\ud800" <= c[-1] <= "\udbff") for c in chunks)
    assert mac_inserter.utf16_chunks("a" * 45) == ["a" * 20, "a" * 20, "a" * 5]
    assert mac_inserter.utf16_chunks("a" * 19 + "🙂") == ["a" * 19, "🙂"]
    assert mac_inserter.utf16_chunks("") == []


def test_shell_apps_and_held_keys():
    assert mac_inserter.is_shell_app("com.apple.dock", True)
    assert mac_inserter.is_shell_app("com.apple.finder", has_windows=False)
    assert not mac_inserter.is_shell_app("com.apple.finder", has_windows=True)
    assert not mac_inserter.is_shell_app("com.apple.TextEdit", has_windows=False)
    assert not mac_inserter.is_shell_app(None, False)
    assert mac_inserter.keys_held(mac_inserter.FLAG_CONTROL | mac_inserter.FLAG_SHIFT, False)
    assert mac_inserter.keys_held(0, True)
    assert not mac_inserter.keys_held(0x100, False)  # a device-specific bit, no modifier


def test_window_refs_are_pids():
    ins = MacInserter()
    assert ins.process_of(None) is None and ins.process_of(0) is None
    assert ins.process_of(4242) == 4242
    assert ins.is_ours(os.getpid()) and not ins.is_ours(None) and not ins.is_ours(os.getpid() + 1)
    assert ins.is_shell(None) is False


@pytest.mark.parametrize(("trusted", "secure", "expected"), [
    (True, False, None),
    (False, False, mac_inserter.NO_PERMISSION),
    (False, True, mac_inserter.NO_PERMISSION),
    (True, True, mac_inserter.SECURE_INPUT),
])
def test_input_blocked(monkeypatch, trusted, secure, expected):
    monkeypatch.setattr(mac_inserter, "accessibility_trusted", lambda: trusted)
    monkeypatch.setattr(mac_inserter, "secure_input_enabled", lambda: secure)
    assert MacInserter().input_blocked() == expected
    assert MacInserter().permission() == ("granted" if trusted else "denied")


def test_permission_unknown_and_not_blocked_without_pyobjc(monkeypatch):
    def missing():
        raise ImportError("no PyObjC")

    def failing():
        raise OSError("no Carbon")

    monkeypatch.setattr(mac_inserter, "accessibility_trusted", missing)
    monkeypatch.setattr(mac_inserter, "secure_input_enabled", failing)
    assert MacInserter().permission() == "unknown"
    assert MacInserter().input_blocked() is None


def test_dictation_input_blocked_is_optional(monkeypatch):
    monkeypatch.setattr(platforms, "inserter", lambda: SimpleNamespace())
    assert dictation.input_blocked() is None

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(platforms, "inserter", lambda: SimpleNamespace(input_blocked=broken))
    assert dictation.input_blocked() is None
    monkeypatch.setattr(platforms, "inserter", lambda: SimpleNamespace(input_blocked=lambda: "secure_input"))
    assert dictation.input_blocked() == dictation.SECURE_INPUT
    assert set(dictation.INPUT_BLOCKED).isdisjoint(dictation.CLIPBOARD_ONLY)
    assert dictation.NO_PERMISSION == mac_inserter.NO_PERMISSION
    assert dictation.SECURE_INPUT == mac_inserter.SECURE_INPUT


# --- everywhere: the pasteboard promise --------------------------------------------------


def test_offer_is_a_private_promise_until_read():
    pb = FakePasteboard()
    clip = MacClipboard(pb, frontmost=lambda: 11)
    offer = clip.offer_delayed("dictated", 11)
    assert pb.text == "previous" and not offer.rendered  # not offered yet
    offer.offer()
    assert pb.provide is not None and pb.private and not offer.rendered
    assert offer.pump(0.01, until=lambda: offer.rendered) is False
    assert pb.read_text() == "dictated"
    assert offer.rendered and offer.confirmed and not offer.lost
    assert offer.pump(0.0, until=lambda: offer.rendered) is True
    assert offer.restore("previous") is True
    assert pb.text == "previous" and pb.private
    offer.close()
    assert pb.text == "previous"


def test_read_while_another_app_is_in_front_is_not_confirmed():
    pb = FakePasteboard()
    offer = MacDelayedOffer(pb, "dictated", 11, frontmost=lambda: 22)
    offer.offer()
    assert pb.read_text() == "dictated"
    assert offer.rendered and not offer.confirmed
    assert MacDelayedOffer(pb, "x", None, frontmost=lambda: None)._provide() == "x"


def test_restore_never_overwrites_a_newer_copy():
    pb = FakePasteboard()
    offer = MacDelayedOffer(pb, "dictated", 11, frontmost=lambda: 11)
    offer.offer()
    pb.read_text()
    pb.copy_by_other_app("copied meanwhile")
    assert offer.restore("previous") is False
    assert offer.lost and pb.text == "copied meanwhile"
    offer.close()
    assert pb.text == "copied meanwhile"


def test_close_leaves_an_unread_dictation_as_text():
    pb = FakePasteboard()
    offer = MacDelayedOffer(pb, "dictated", 11)
    offer.offer()
    offer.close()
    assert pb.provide is None and pb.text == "dictated" and pb.private
    assert offer.rendered and not offer.confirmed
    writes = len(pb.writes)
    offer.close()
    assert len(pb.writes) == writes


def test_close_after_another_copy_keeps_it():
    pb = FakePasteboard()
    offer = MacDelayedOffer(pb, "dictated", 11)
    offer.offer()
    pb.copy_by_other_app("theirs")
    offer.close()
    assert pb.text == "theirs" and offer.lost


def test_restore_none_empties_and_set_text_reports_the_count():
    pb = FakePasteboard()
    clip = MacClipboard(pb)
    assert clip.set_text("a", private=False) == clip.sequence() == pb.count
    assert pb.private is False
    offer = clip.offer_delayed("b", None)
    offer.offer()
    assert offer.restore(None) is True
    assert clip.get_text() is None


# --- everywhere: insert_text with the macOS clipboard logic --------------------------------

TARGET, OTHER, OURS, DOCK = 1011, 1022, 1033, 1044


class FakeMac:
    """Frontmost app, keys and readers around a real `MacClipboard` on a `FakePasteboard`."""

    def __init__(self, front=TARGET, reader=TARGET, blocked=None, activates=True):
        self.front = front
        self.reader = reader  # the app that reads the pasteboard after ⌘V (None: nobody)
        self.blocked = blocked
        self.activates = activates
        self.events: list = []
        self.asked = 0
        self.pb = FakePasteboard()
        self.clipboard = MacClipboard(self.pb, frontmost=lambda: self.reader_front)
        self.reader_front = None
        fake = self

        class Inserter:
            def foreground(self):
                return fake.front

            def process_of(self, window):
                return window or None

            def is_ours(self, window):
                return window == OURS

            def is_shell(self, window):
                return window == DOCK

            def exists(self, window):
                return True

            def activate(self, window):
                fake.events.append(("activate", window))
                if fake.activates:
                    fake.front = window
                return fake.activates

            def paste(self):
                fake.events.append(("paste", fake.front))
                if fake.reader is not None:
                    fake.reader_front = fake.front if fake.reader == fake.front else fake.reader
                    fake.events.append(("read", fake.pb.read_text()))

            def press_enter(self):
                fake.events.append(("enter", fake.front))

            def type_text(self, text):
                fake.events.append(("type", fake.front, text))

            def wait_keys_released(self, limit_s=1.5):
                pass

            def paste_label(self):
                return "⌘V"

            def permission(self):
                return "denied" if fake.blocked == dictation.NO_PERMISSION else "granted"

            def request_permission(self):
                fake.asked += 1

            def input_blocked(self):
                return fake.blocked

        self.inserter = Inserter()

    def install(self, monkeypatch):
        monkeypatch.setattr(platforms, "clipboard", lambda: self.clipboard)
        monkeypatch.setattr(platforms, "inserter", lambda: self.inserter)
        monkeypatch.setattr(dictation, "PASTE_TIMEOUT_S", 0.05)
        monkeypatch.setattr(dictation, "RESTORE_GRACE_S", 0.01)
        monkeypatch.setattr(dictation, "ENTER_DELAY_S", 0.0)
        return self


@pytest.fixture
def mac_family():
    with platforms.override("macos"):
        yield


def test_mac_paste_restores_after_the_target_read(monkeypatch, mac_family):
    mac = FakeMac().install(monkeypatch)
    delivered = []
    assert dictation.insert_text("hello ", "paste", False, TARGET, delivered.append) == dictation.PASTE
    assert delivered == [dictation.PASTE]
    assert mac.events == [("paste", TARGET), ("read", "hello ")]
    assert mac.pb.text == "previous"


def test_mac_enter_follows_the_read(monkeypatch, mac_family):
    mac = FakeMac().install(monkeypatch)
    assert dictation.insert_text("hello", "paste", True, TARGET) == dictation.PASTE
    assert mac.events == [("paste", TARGET), ("read", "hello"), ("enter", TARGET)]
    assert mac.pb.text == "previous"


def test_mac_slow_target_keeps_the_dictation(monkeypatch, mac_family):
    mac = FakeMac(reader=None).install(monkeypatch)
    assert dictation.insert_text("hello", "paste", True, TARGET) == dictation.NOT_TAKEN
    assert mac.events == [("paste", TARGET)]
    assert mac.pb.text == "hello" and mac.pb.provide is None


def test_mac_read_by_another_app_is_not_restored(monkeypatch, mac_family):
    mac = FakeMac(reader=OTHER).install(monkeypatch)
    assert dictation.insert_text("hello", "paste", False, TARGET) == dictation.PASTE
    assert mac.pb.text == "hello"


def test_mac_target_brought_back_and_refused(monkeypatch, mac_family):
    mac = FakeMac(front=OTHER).install(monkeypatch)
    assert dictation.insert_text("hi", "paste", False, TARGET) == dictation.PASTE
    assert mac.events[0] == ("activate", TARGET)
    mac = FakeMac(front=OTHER, activates=False).install(monkeypatch)
    assert dictation.insert_text("hi", "paste", False, TARGET) == dictation.FOCUS_LOST
    assert mac.pb.text == "hi"


@pytest.mark.parametrize("mode", ["paste", "type"])
@pytest.mark.parametrize("blocked", [dictation.NO_PERMISSION, dictation.SECURE_INPUT])
def test_mac_blocked_input_sends_no_keys(monkeypatch, mac_family, mode, blocked):
    mac = FakeMac(blocked=blocked).install(monkeypatch)
    delivered = []
    assert dictation.insert_text("secret", mode, True, TARGET, delivered.append) == blocked
    assert delivered == [blocked]
    assert mac.events == []
    assert mac.pb.text == "secret" and mac.pb.private


def test_mac_type_mode_and_shell_apps(monkeypatch, mac_family):
    mac = FakeMac().install(monkeypatch)
    assert dictation.insert_text("typed", "type", True, TARGET) == dictation.PASTE
    assert mac.events == [("type", TARGET, "typed"), ("enter", TARGET)]
    assert mac.pb.text == "previous"
    mac = FakeMac(front=DOCK).install(monkeypatch)
    assert dictation.dictation_target() is None
    assert dictation.insert_text("x", "paste", False, None) == dictation.NO_WINDOW
    mac = FakeMac(front=OURS).install(monkeypatch)
    assert dictation.insert_text("x", "paste", False, None) == dictation.OWN_WINDOW


def test_blocked_messages_and_one_permission_prompt(monkeypatch):
    shown: list[tuple[str, str, float]] = []
    asked: list[int] = []

    class Controller:
        queued = 1
        live_text = ""
        last_text = ""
        last_seconds = 0.0

        def show_message(self, text, style="ready", seconds=3.0):
            shown.append((text, style, seconds))

        def _emit(self, *_args):
            pass

    fake = SimpleNamespace(paste_label=lambda: "⌘V", request_permission=lambda: asked.append(1))
    monkeypatch.setattr(platforms, "inserter", lambda: fake)
    monkeypatch.setattr(dictation, "_permission_asked", False)
    for _ in range(2):
        dictation.DictationController._recognized(Controller(), "hi", 1.0, dictation.NO_PERMISSION)
    dictation.DictationController._recognized(Controller(), "hi", 1.0, dictation.SECURE_INPUT)
    assert asked == [1]
    assert shown[0] == ("Ucho needs the Accessibility permission to paste. Text is on the clipboard, "
                        "press ⌘V.", "warning", 10)
    assert shown[2] == ("Secure keyboard entry is on (a password field or a terminal). Text is on the "
                        "clipboard, press ⌘V.", "warning", 8)


def test_dictation_window_style_only_on_aqua():
    screens = pytest.importorskip("ucho.screens")
    calls: list[tuple] = []

    class Tk:
        def __init__(self, system, refuse=0):
            self.system, self.refuse = system, refuse

        def call(self, *args):
            calls.append(args)
            if args[:2] == ("tk", "windowingsystem"):
                return self.system
            if self.refuse:
                self.refuse -= 1
                raise screens.tkinter.TclError("bad attribute")
            return ""

    assert screens.float_without_activating(SimpleNamespace(tk=Tk("win32"), _w=".w")) is False
    assert len(calls) == 1
    calls.clear()
    assert screens.float_without_activating(SimpleNamespace(tk=Tk("aqua"), _w=".w")) is True
    assert calls[-1] == ("::tk::unsupported::MacWindowStyle", "style", ".w", "utility",
                         ("noActivates", "nonActivating"))
    calls.clear()
    assert screens.float_without_activating(SimpleNamespace(tk=Tk("aqua", refuse=1), _w=".w")) is True
    assert calls[-1][-1] == ("noActivates",)


# --- macOS only: the real pasteboard ------------------------------------------------------


@pytest.fixture
def real_clipboard():
    clip = MacClipboard()
    saved = clip.get_text()
    yield clip
    clip.set_text(saved)


def _pbpaste_async() -> subprocess.Popen:
    return subprocess.Popen(["/usr/bin/pbpaste"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@MACOS_ONLY
def test_real_set_get_sequence_and_markers(real_clipboard):
    from AppKit import NSPasteboard

    before = real_clipboard.sequence()
    count = real_clipboard.set_text("ucho test żółw")
    assert count == real_clipboard.sequence() > before
    assert real_clipboard.get_text() == "ucho test żółw"
    types = {str(t) for t in NSPasteboard.generalPasteboard().types()}
    assert set(mac_clipboard.PRIVATE_TYPES) <= types
    out = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, timeout=10).stdout.decode("utf-8")
    assert out == "ucho test żółw"
    real_clipboard.set_text("public", private=False)
    types = {str(t) for t in NSPasteboard.generalPasteboard().types()}
    assert not set(mac_clipboard.PRIVATE_TYPES) & types
    real_clipboard.set_text(None)
    assert real_clipboard.get_text() is None


@MACOS_ONLY
def test_real_promise_is_read_by_another_process(real_clipboard):
    real_clipboard.set_text("before")
    offer = real_clipboard.offer_delayed("promised ąę", None)
    offer.offer()
    try:
        assert not offer.rendered
        reader = _pbpaste_async()
        assert offer.pump(8.0, until=lambda: offer.rendered), "pbpaste did not ask for the promised text"
        out = reader.communicate(timeout=10)[0].decode("utf-8")
        assert out == "promised ąę"
        assert not offer.confirmed  # no consumer
        assert offer.restore("before") is True
        assert real_clipboard.get_text() == "before"
    finally:
        offer.close()


@MACOS_ONLY
def test_real_restore_refused_after_another_copy(real_clipboard):
    real_clipboard.set_text("before")
    offer = real_clipboard.offer_delayed("dictated", None)
    offer.offer()
    try:
        subprocess.run(["/usr/bin/pbcopy"], input=b"copied by pbcopy", timeout=10, check=True)
        assert offer.restore("before") is False
        assert offer.lost
        assert real_clipboard.get_text() == "copied by pbcopy"
    finally:
        offer.close()
    assert real_clipboard.get_text() == "copied by pbcopy"


@MACOS_ONLY
def test_real_unread_promise_stays_as_text_after_close(real_clipboard):
    offer = real_clipboard.offer_delayed("left for ⌘V", None)
    offer.offer()
    offer.close()
    assert offer.rendered and not offer.confirmed
    out = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, timeout=10).stdout.decode("utf-8")
    assert out == "left for ⌘V"


@MACOS_ONLY
def test_real_permission_and_secure_input():
    ins = MacInserter()
    assert ins.permission() == "granted"  # the runner image grants Accessibility
    assert isinstance(mac_inserter.secure_input_enabled(), bool)
    assert ins.input_blocked() is None
    ins.wait_keys_released(0.2)
    assert ins.exists(os.getpid()) and not ins.exists(999_999)


# --- macOS only: a Tk app in a child process as the paste target --------------------------

TARGET_APP = r"""
import json, sys, threading, tkinter
root = tkinter.Tk()
root.title("Ucho paste target")
root.geometry("420x120+200+200")
entry = tkinter.Entry(root, width=40)
entry.pack(padx=10, pady=10)
enters = []
entry.bind("<Return>", lambda _e: enters.append(entry.get()))
commands = []

def read_stdin():
    for line in sys.stdin:
        commands.append(line.strip())

def activate():
    try:
        from AppKit import NSApplication, NSRunningApplication
        NSRunningApplication.currentApplication().activateWithOptions_(3)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception as exc:
        print("activate failed:", exc, file=sys.stderr, flush=True)
    root.deiconify()
    root.lift()
    root.focus_force()
    entry.focus_set()

def poll():
    while commands:
        command = commands.pop(0)
        if command == "activate":
            activate()
        elif command == "clear":
            entry.delete(0, "end")
            enters.clear()
        elif command == "dump":
            print(json.dumps({"text": entry.get(), "enters": enters}), flush=True)
        elif command == "quit":
            root.destroy()
            return
    root.after(30, poll)

threading.Thread(target=read_stdin, daemon=True).start()
root.after(100, activate)
root.after(300, lambda: print("ready", flush=True))
root.after(30, poll)
root.mainloop()
"""


class TargetApp:
    def __init__(self, script_path) -> None:
        self.proc = subprocess.Popen([sys.executable, str(script_path)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8")
        self.pid = self.proc.pid
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def line(self, timeout: float = 10.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                return self.lines.get_nowait()
            except queue.Empty:
                mac_inserter.settle(0.05)
        raise AssertionError("the target app did not answer")

    def send(self, command: str) -> None:
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def dump(self) -> dict:
        mac_inserter.settle(0.5)  # let Tk handle the posted keys
        self.send("dump")
        return json.loads(self.line())

    def close(self) -> str:
        try:
            self.send("quit")
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=10)
        return self.proc.stderr.read() if self.proc.stderr else ""


def _frontmost_description() -> str:
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return "none" if app is None else f"{app.bundleIdentifier()} pid {app.processIdentifier()}"


@pytest.fixture(scope="module")
def target_app(tmp_path_factory):
    script = tmp_path_factory.mktemp("paste_target") / "target_app.py"
    script.write_text(TARGET_APP, encoding="utf-8")
    app = TargetApp(script)
    try:
        assert app.line(30) == "ready"
        ins = MacInserter()
        end = time.monotonic() + 20
        while ins.foreground() != app.pid and time.monotonic() < end:
            app.send("activate")
            ins.activate(app.pid)
            mac_inserter.settle(0.5)
        assert ins.foreground() == app.pid, f"target app not in front: {_frontmost_description()}"
        yield app
    finally:
        stderr = app.close()
        if stderr.strip():
            print("target app stderr:", stderr)


@MACOS_ONLY
def test_real_foreground_is_the_target_app(target_app):
    ins = MacInserter()
    assert ins.foreground() == target_app.pid
    assert ins.process_of(target_app.pid) == target_app.pid
    assert not ins.is_ours(target_app.pid)
    assert ins.is_ours(os.getpid())
    assert ins.exists(target_app.pid)
    assert not ins.is_shell(target_app.pid)
    assert mac_inserter.has_normal_windows(target_app.pid)
    assert dictation.dictation_target() == target_app.pid


@MACOS_ONLY
def test_real_paste_with_enter_restores_the_clipboard(target_app, real_clipboard):
    target_app.send("clear")
    real_clipboard.set_text("the previous clipboard")
    delivered: list[str] = []
    result = dictation.insert_text("hello from Ucho", "paste", True, target_app.pid, delivered.append)
    assert result == delivered[0] == dictation.PASTE
    state = target_app.dump()
    assert state["text"] == "hello from Ucho"
    assert state["enters"] == ["hello from Ucho"]
    assert real_clipboard.get_text() == "the previous clipboard"


@MACOS_ONLY
def test_real_offer_confirmed_by_the_target(target_app, real_clipboard):
    target_app.send("clear")
    offer = real_clipboard.offer_delayed("confirmed text", target_app.pid)
    offer.offer()
    try:
        MacInserter().paste()
        assert offer.pump(8.0, until=lambda: offer.rendered)
        assert offer.confirmed
    finally:
        offer.close()
    assert target_app.dump()["text"] == "confirmed text"


@MACOS_ONLY
def test_real_type_mode(target_app, real_clipboard):
    target_app.send("clear")
    real_clipboard.set_text("untouched")
    text = "zażółć gęślą jaźń, typed by Ucho in pieces"
    assert dictation.insert_text(text, "type", False, target_app.pid) == dictation.PASTE
    assert target_app.dump()["text"] == text
    assert real_clipboard.get_text() == "untouched"


# --- macOS only: Tk in this process (last: it makes this process a GUI app) ----------------


@MACOS_ONLY
def test_real_promise_served_while_tk_runs_the_main_loop(real_clipboard):
    """The app's threads: Tk on the main thread, the offer on a worker, the reader outside."""
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    result: dict = {}

    def worker() -> None:
        offer = real_clipboard.offer_delayed("from the insert thread", None)
        try:
            offer.offer()
            reader = _pbpaste_async()
            result["taken"] = offer.pump(8.0, until=lambda: offer.rendered)
            result["out"] = reader.communicate(timeout=10)[0].decode("utf-8")
        except Exception as exc:
            result["error"] = repr(exc)
        finally:
            offer.close()
            result["done"] = True

    def poll() -> None:
        if result.get("done") or time.monotonic() > deadline:
            root.quit()
        else:
            root.after(50, poll)

    deadline = time.monotonic() + 25
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    root.after(50, poll)
    root.mainloop()
    root.destroy()
    thread.join(5)
    assert result.get("taken") is True, result
    assert result["out"] == "from the insert thread"


@MACOS_ONLY
def test_real_dictation_window_style():
    import tkinter

    from ucho import screens

    root = tkinter.Tk()
    try:
        window = tkinter.Toplevel(root)
        assert screens.float_without_activating(window) is True
        style = root.tk.call("::tk::unsupported::MacWindowStyle", "style", window._w)
        assert "utility" in str(style) and "noActivates" in str(style)
    finally:
        root.destroy()
