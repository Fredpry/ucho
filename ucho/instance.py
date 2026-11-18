# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""One Ucho per user session: a second start hands its command to the first one.

The first process owns a named mutex and waits on three named events
("show", "dictate", "record"). A second start (a shortcut, the pinned tile,
a Jump List task) finds the mutex, sets the event for its command and exits;
the first process shows its window, opens dictation or starts recording.

Named kernel objects in the `Local\\` namespace exist only in this Windows
session: nothing is reachable from the network or from other users.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from collections.abc import Callable

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\Ucho.App"
COMMANDS = ("show", "dictate", "record")
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
ASFW_ANY = 0xFFFFFFFF


class SingleInstance:
    def __init__(self, name: str = MUTEX_NAME):
        self._name = name
        self._mutex = None
        self._events: list = []
        self._stop = threading.Event()

    def acquire(self) -> bool:
        """True for the first instance; False when Ucho already runs."""
        if sys.platform != "win32":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._mutex = kernel32.CreateMutexW(None, False, self._name)
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    def listen(self, on_command: Callable[[str], None]) -> None:
        """First instance: calls `on_command(name)` (on a background thread) for each request."""
        if sys.platform != "win32":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        self._events = [kernel32.CreateEventW(None, False, False, self._event_name(c))
                        for c in COMMANDS]
        handles = (wintypes.HANDLE * len(self._events))(*self._events)

        def loop():
            while not self._stop.is_set():
                result = kernel32.WaitForMultipleObjects(len(self._events), handles, False, 500)
                if WAIT_OBJECT_0 <= result < WAIT_OBJECT_0 + len(COMMANDS):
                    command = COMMANDS[result - WAIT_OBJECT_0]
                    log.info("Request from a second start: %s", command)
                    try:
                        on_command(command)
                    except Exception:
                        log.exception("Handling %s failed", command)

        threading.Thread(target=loop, name="single-instance", daemon=True).start()

    def _event_name(self, command: str) -> str:
        return f"{self._name}.Command.{command}"

    def signal(self, command: str) -> bool:
        """Second instance: asks the running Ucho to run `command`."""
        if sys.platform != "win32" or command not in COMMANDS:
            return False
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenEventW.restype = wintypes.HANDLE
        # The running instance may bring its window to the front: Windows
        # allows that only when the process the user just started permits it.
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
        event = None
        # The first instance creates its events a moment after the mutex.
        for _ in range(30):
            event = kernel32.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False,
                                        self._event_name(command))
            if event:
                break
            time.sleep(0.1)
        if not event:
            return False
        try:
            return bool(kernel32.SetEvent(event))
        finally:
            kernel32.CloseHandle(event)

    def close(self) -> None:
        self._stop.set()
        if sys.platform != "win32":
            return
        for handle in self._events + ([self._mutex] if self._mutex else []):
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass
        self._events, self._mutex = [], None
