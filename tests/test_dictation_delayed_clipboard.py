# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""The real delayed-rendering clipboard code, run in a private window station.

A window station has its own clipboard. The child process below switches to
a non-interactive window station of its own and checks by name that it is no
longer in the interactive one (WinSta0) before anything is written, so the
real clipboard of the person running the tests is never read or changed. A second thread plays the target
program: it opens the clipboard and asks for the text, which is what a
program does when it handles Ctrl+V. No keys are sent.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only: Win32 clipboard")

ISOLATED = 3  # exit code of the child when it could not isolate itself

CHILD = r'''
import ctypes, json, os, sys, threading
from ctypes import wintypes

u = ctypes.WinDLL("user32", use_last_error=True)
u.GetClipboardSequenceNumber.restype = wintypes.DWORD
u.CountClipboardFormats.restype = ctypes.c_int
u.CreateWindowStationW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
u.CreateWindowStationW.restype = wintypes.HANDLE
u.SetProcessWindowStation.argtypes = [wintypes.HANDLE]
u.GetProcessWindowStation.restype = wintypes.HANDLE
u.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, ctypes.c_void_p]
u.CreateDesktopW.restype = wintypes.HANDLE
u.SetThreadDesktop.argtypes = [wintypes.HANDLE]
u.OpenClipboard.argtypes = [wintypes.HWND]
u.GetClipboardData.argtypes = [wintypes.UINT]
u.GetClipboardData.restype = ctypes.c_void_p
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
u.CreateWindowExW.restype = wintypes.HWND
u.DestroyWindow.argtypes = [wintypes.HWND]
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.GlobalLock.argtypes = [ctypes.c_void_p]
k.GlobalLock.restype = ctypes.c_void_p
k.GlobalUnlock.argtypes = [ctypes.c_void_p]

u.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]


def station_name():
    buffer = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD(0)
    if not u.GetUserObjectInformationW(u.GetProcessWindowStation(), 2, buffer, ctypes.sizeof(buffer),
                                       ctypes.byref(needed)):  # UOI_NAME
        return ""
    return buffer.value


interactive = station_name()
# Unnamed: Windows names it after the logon session ("Service-0x...$"). A
# normal user may not create a named one. It outlives this process, so later
# runs find the clipboard of earlier runs in it; it is emptied below, after
# checking by name that this process really is in it.
station = u.CreateWindowStationW(None, 0, 0x37F, None)
if not station or not u.SetProcessWindowStation(station) or u.GetProcessWindowStation() != station:
    sys.exit(3)
private = station_name()
if not interactive or not private or private == interactive or private.lower() == "winsta0":
    sys.exit(3)
desktop = u.CreateDesktopW("ucho-test", None, None, 0, 0x01FF, None)
if not desktop or not u.SetThreadDesktop(desktop):
    sys.exit(3)
if not u.OpenClipboard(None):
    sys.exit(3)
u.EmptyClipboard()
u.CloseClipboard()

sys.path.insert(0, sys.argv[1])
from ucho import dictation as d

CF_UNICODETEXT = 13


def read(owner_window=True):
    """What a target program does on Ctrl+V, on its own thread and window."""
    box = {}

    def run():
        u.SetThreadDesktop(desktop)
        hwnd = u.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, -3, None, None, None) \
            if owner_window else None
        box["hwnd"] = hwnd
        if u.OpenClipboard(hwnd):
            try:
                handle = u.GetClipboardData(CF_UNICODETEXT)
                pointer = k.GlobalLock(handle) if handle else None
                box["text"] = ctypes.wstring_at(pointer) if pointer else None
                if pointer:
                    k.GlobalUnlock(handle)
            finally:
                u.CloseClipboard()
        if hwnd:
            u.DestroyWindow(hwnd)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def clipboard():
    box = {}

    def run():
        u.SetThreadDesktop(desktop)
        box["text"] = d.get_clipboard_text()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return box["text"]


results = {}

# 1. The target's process takes the text: confirmed, then the previous text comes back.
d.set_clipboard("previous")
offer = d.DelayedClipboard("dictated", os.getpid())
offer.offer()
results["pending_before_paste"] = not offer.rendered
thread, box = read()
taken = offer.pump(5, until=lambda: offer.rendered)
thread.join()
restored = offer.restore("previous")
offer.close()
results["confirmed"] = [taken, offer.confirmed, box.get("text"), restored, clipboard()]

# 2. Another process asks for the text: not confirmed.
offer = d.DelayedClipboard("dictated", 1)
offer.offer()
thread, box = read()
offer.pump(5, until=lambda: offer.rendered)
thread.join()
offer.close()
results["other_process"] = [offer.rendered, offer.confirmed, box.get("text"), clipboard()]

# 3. A reader without a window cannot be identified: not confirmed.
offer = d.DelayedClipboard("dictated", os.getpid())
offer.offer()
thread, box = read(owner_window=False)
offer.pump(5, until=lambda: offer.rendered)
thread.join()
offer.close()
results["anonymous_reader"] = [offer.rendered, offer.confirmed, box.get("text")]

# 4. Nobody pastes: closing leaves the dictation on the clipboard.
d.set_clipboard("previous")
offer = d.DelayedClipboard("dictated", os.getpid())
offer.offer()
results["nobody"] = [offer.pump(0.3, until=lambda: offer.rendered)]
offer.close()
results["nobody"].append(clipboard())

# 5. A copy made after the paste is never overwritten by the restore.
offer = d.DelayedClipboard("dictated", os.getpid())
offer.offer()
thread, box = read()
offer.pump(5, until=lambda: offer.rendered)
thread.join()
copier = threading.Thread(target=lambda: (u.SetThreadDesktop(desktop), d.set_clipboard("copied")))
copier.start()
copier.join()
offer.pump(0.1)
results["newer_copy"] = [offer.confirmed, offer.lost, offer.restore("previous"), clipboard()]
offer.close()

# 6. insert_text end to end, Enter mode: Enter only after the text was taken.
d.set_clipboard("previous")
events = []
target = {}


def ctrl_v():
    thread, box = read()
    target["thread"], target["box"] = thread, box
    events.append("ctrl+v")


d.wait_for_keys_released = lambda *a: None
d.foreground_window = lambda: 4242
d.window_is_ours = lambda w: False
d.is_shell_window = lambda w: False
d.window_process = lambda w: os.getpid()
d.press_ctrl_v = ctrl_v
d.press_enter = lambda: events.append(("enter", target["box"].get("text")))
d.RESTORE_GRACE_S = 0.1
delivered = []
where = d.insert_text("dictated", "paste", True, 4242, delivered.append)
target["thread"].join()
results["insert_text"] = [where, delivered, list(events), target["box"].get("text"), clipboard()]

# 7. insert_text when the target never asks: Ctrl+V sent, no Enter, the text stays.
d.set_clipboard("previous")
events.clear()
d.press_ctrl_v = lambda: events.append("ctrl+v ignored")
d.PASTE_TIMEOUT_S = 0.3
where = d.insert_text("dictated", "paste", True, 4242)
results["ignored"] = [where, events, clipboard()]

print(json.dumps(results))
'''


def test_delayed_rendering_in_a_private_window_station(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    root = Path(__file__).resolve().parent.parent
    run = subprocess.run([sys.executable, str(script), str(root)], capture_output=True, text=True,
                         timeout=120)
    if run.returncode == ISOLATED:
        pytest.skip("could not create a private window station here")
    assert run.returncode == 0, run.stderr
    results = json.loads(run.stdout.strip().splitlines()[-1])
    assert results["pending_before_paste"]
    assert results["confirmed"] == [True, True, "dictated", True, "previous"]
    assert results["other_process"] == [True, False, "dictated", "dictated"]
    assert results["anonymous_reader"] == [True, False, "dictated"]
    assert results["nobody"] == [False, "dictated"]
    assert results["newer_copy"] == [True, True, False, "copied"]
    assert results["insert_text"] == ["paste", ["paste"], ["ctrl+v", ["enter", "dictated"]],
                                      "dictated", "previous"]
    assert results["ignored"] == ["not_taken", ["ctrl+v ignored"], "dictated"]
