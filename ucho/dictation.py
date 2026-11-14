# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dictation mode: you speak, the text lands where the cursor is.

Ctrl+Shift+Space starts recording, a second press ends it, Esc cancels. The
recording goes through the shared engine ahead of any meeting transcription
(ucho/engine.py) and the result is pasted into the active window: a terminal,
a browser, anything.

A few decisions that are not obvious:
  * Esc is registered only while recording; otherwise we do not take it
    away from other programs (ucho/hotkeys.py).
  * The model can stay loaded ("Keep the dictation model loaded"): loading
    takes seconds, and dictation has to start at once.
  * When the active window belongs to Ucho itself, the text only goes to the
    clipboard: pasting into our own window would land in the wrong place.
  * The window that was active when the dictation started is the target. If
    another window is active at paste time and the target cannot be brought
    back, the text stays on the clipboard instead of landing in the wrong
    window.
  * Paste mode borrows the clipboard: the dictated text is marked so that
    clipboard history (Win+V) and cloud clipboard sync skip it, and the
    previous text is put back once the paste has been delivered.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import c_void_p, wintypes
from typing import Callable

import numpy as np

from . import config, transcribe
from .i18n import t

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
# People press the hotkey just as they finish the last word; without this
# tail the end of the utterance is sometimes cut off.
TAIL_S = 0.25


# --- WinAPI: clipboard and keyboard -------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.SetClipboardData.argtypes = [wintypes.UINT, c_void_p]
_user32.SetClipboardData.restype = c_void_p
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = c_void_p
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.GetClipboardSequenceNumber.argtypes = []
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = c_void_p
_kernel32.GlobalLock.argtypes = [c_void_p]
_kernel32.GlobalLock.restype = c_void_p
_kernel32.GlobalUnlock.argtypes = [c_void_p]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [c_void_p]
_kernel32.GlobalFree.restype = c_void_p

VK_SPACE = 0x20
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_V = 0x56
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    # Needed only so that the union has the right size.
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    event = _INPUT(type=INPUT_KEYBOARD)
    event.u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return event


def _send(events: list[_INPUT]) -> None:
    array = (_INPUT * len(events))(*events)
    _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))


def wait_for_keys_released(limit_s: float = 1.5) -> None:
    """Ctrl+V while Shift is still held means something else in many programs."""
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        if not any(_user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_SPACE)):
            return
        time.sleep(0.02)


# Registered formats that keep the text out of clipboard history (Win+V),
# cloud clipboard sync and clipboard monitors ("Cloud Clipboard and Clipboard
# History Formats" in the Windows docs). The first counts by its presence,
# the other two need a DWORD 0.
PRIVATE_FORMATS = {
    "ExcludeClipboardContentFromMonitorProcessing": 0,
    "CanIncludeInClipboardHistory": 0,
    "CanUploadToCloudClipboard": 0,
}
# Time for the target program to read the clipboard after Ctrl+V before the
# previous text goes back: the target handles the keystroke asynchronously.
RESTORE_DELAY_S = 0.4


def _open_clipboard() -> None:
    for _ in range(20):
        if _user32.OpenClipboard(None):
            return
        time.sleep(0.05)  # the clipboard is sometimes briefly held by another program
    raise RuntimeError(t("The clipboard is in use by another program"))


def _put(format_: int, data: bytes) -> None:
    """SetClipboardData with a moveable copy of `data`; the clipboard must be open."""
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not handle:
        raise MemoryError("GlobalAlloc")
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        _kernel32.GlobalFree(handle)
        raise MemoryError("GlobalLock")
    ctypes.memmove(pointer, data, len(data))
    _kernel32.GlobalUnlock(handle)
    # On success the system owns the memory; on failure it is still ours.
    if not _user32.SetClipboardData(format_, handle):
        _kernel32.GlobalFree(handle)
        raise RuntimeError("SetClipboardData failed")


def set_clipboard(text: str | None, private: bool = True) -> int:
    """Puts `text` on the clipboard (None empties it). Returns the clipboard sequence number.

    `private` adds the formats that keep the text out of clipboard history and
    cloud sync. Everything Ucho puts on the clipboard is a dictation or a
    transcript, so that is the default.
    """
    _open_clipboard()
    try:
        _user32.EmptyClipboard()
        if text is not None:
            _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
            if private:
                for name, value in PRIVATE_FORMATS.items():
                    format_ = _user32.RegisterClipboardFormatW(name)
                    try:
                        if not format_:
                            raise RuntimeError("RegisterClipboardFormatW failed")
                        _put(format_, value.to_bytes(4, "little"))
                    except (MemoryError, RuntimeError):
                        log.warning("Could not set the clipboard format %s", name)
    finally:
        _user32.CloseClipboard()
    return clipboard_sequence()


def get_clipboard_text() -> str | None:
    """The clipboard text (CF_UNICODETEXT), or None when the clipboard holds no text."""
    if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    _open_clipboard()
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def clipboard_sequence() -> int:
    return int(_user32.GetClipboardSequenceNumber())


# What happens to the clipboard after a paste, decided by `restore_action`.
RESTORE = "restore"  # put the previous text back
CLEAR = "clear"      # there was no text before: do not leave the dictation there
KEEP = "keep"        # someone else wrote to the clipboard meanwhile: leave it alone


def restore_action(previous: str | None, ours: int, now: int) -> str:
    """RESTORE, CLEAR or KEEP, from the sequence numbers after our write (`ours`) and now.

    Only text (CF_UNICODETEXT) is saved and restored. When the previous
    content had no text (an image, copied files), it cannot be put back
    without the risk of corrupting it; the clipboard is cleared instead, and
    that old content is lost, as it was before restoring existed. Rich formats
    next to the text (HTML or RTF of a copy from Word) are lost the same way;
    their plain text comes back.
    """
    if now != ours:
        return KEEP
    return RESTORE if previous is not None else CLEAR


def press_ctrl_v() -> None:
    _send([_key(VK_CONTROL), _key(VK_V),
           _key(VK_V, flags=KEYEVENTF_KEYUP),
           _key(VK_CONTROL, flags=KEYEVENTF_KEYUP)])


def press_enter() -> None:
    _send([_key(VK_RETURN), _key(VK_RETURN, flags=KEYEVENTF_KEYUP)])


def type_text(text: str) -> None:
    """Types the text as Unicode characters: no clipboard, any keyboard layout."""
    data = text.encode("utf-16-le")
    events: list[_INPUT] = []
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        events.append(_key(scan=unit, flags=KEYEVENTF_UNICODE))
        events.append(_key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    # In portions: a very long burst of events can lose characters in a terminal.
    for i in range(0, len(events), 200):
        _send(events[i:i + 200])
        time.sleep(0.01)


def foreground_window() -> int | None:
    return _user32.GetForegroundWindow() or None


def window_is_ours(window: int | None) -> bool:
    """True when the window belongs to this process (any Ucho window)."""
    if not window:
        return False
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    return pid.value == os.getpid()


def foreground_is_ours() -> bool:
    """True when the active window belongs to this process (any Ucho window)."""
    return window_is_ours(foreground_window())


def window_exists(window: int) -> bool:
    return bool(_user32.IsWindow(window))


# The taskbar and the desktop are in front after a click on them, but nobody
# dictates into them: a dictation started there has no target window.
_SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"}


def dictation_target() -> int | None:
    """The foreground window as the target of a dictation; None for the taskbar or desktop."""
    window = foreground_window()
    if not window:
        return None
    name = ctypes.create_unicode_buffer(64)
    if _user32.GetClassNameW(window, name, len(name)) and name.value in _SHELL_CLASSES:
        return None
    return window


def activate(window: int) -> bool:
    """One attempt to bring the window back; Windows focus rules may refuse it."""
    _user32.SetForegroundWindow(window)
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        if foreground_window() == window:
            return True
        time.sleep(0.02)
    return False


# Where the dictated text goes, decided by `paste_target`.
PASTE = "paste"            # into the foreground window, which is the right one
OWN_WINDOW = "own_window"  # a Ucho window is active: clipboard only
FOCUS_LOST = "focus_lost"  # the target window is gone or did not come back: clipboard only


def paste_target(target: int | None, foreground: Callable[[], int | None],
                 is_ours: Callable[[int | None], bool], exists: Callable[[int], bool],
                 activate_window: Callable[[int], bool]) -> str:
    """PASTE, OWN_WINDOW or FOCUS_LOST for a dictation that started with `target` in front.

    A target that belongs to Ucho (the dictation started from its own button
    or tray menu) counts as no target: the text goes to whatever window is
    active, as before. Otherwise the text is pasted only into the target,
    which gets one attempt to come back to the foreground.
    """
    current = foreground()
    if not target or is_ours(target) or current == target:
        return OWN_WINDOW if is_ours(current) else PASTE
    if not exists(target):
        return FOCUS_LOST
    if activate_window(target) and foreground() == target:
        return PASTE
    return FOCUS_LOST


# Two dictations in a row must not interleave: the second would save the
# first one's text as "previous" and put it back afterwards.
_insert_lock = threading.Lock()


def insert_text(text: str, mode: str, enter: bool, target: int | None = None,
                delivered: Callable[[str], None] | None = None) -> str:
    """Inserts text into the target window; returns PASTE, OWN_WINDOW or FOCUS_LOST.

    With OWN_WINDOW and FOCUS_LOST the text is only on the clipboard, and it
    stays there for the user to paste. `delivered(result)` is called as soon
    as the text is in place, before the previous clipboard comes back.
    """
    def report(where: str) -> str:
        if delivered is not None:
            delivered(where)
        return where

    with _insert_lock:
        wait_for_keys_released()
        where = paste_target(target, foreground_window, window_is_ours, window_exists, activate)
        if where != PASTE:
            if where == FOCUS_LOST:
                log.info("The dictation target window is not active - the text stays on the clipboard")
            set_clipboard(text)
            return report(where)
        previous, ours = None, None
        if mode == "type":
            type_text(text)
        else:
            previous = get_clipboard_text()
            ours = set_clipboard(text)
            press_ctrl_v()
        if enter:
            # The terminal must digest the paste first, otherwise Enter overtakes it.
            time.sleep(0.25)
            press_enter()
        report(PASTE)
        if ours is not None:
            time.sleep(RESTORE_DELAY_S)
            try:
                if restore_action(previous, ours, clipboard_sequence()) != KEEP:
                    set_clipboard(previous)
            except Exception:
                # The text is already pasted; only the old clipboard is not back.
                log.warning("Could not restore the previous clipboard", exc_info=True)
        return where


def beep(frequency: int, ms: int = 70) -> None:
    def _play():
        try:
            import winsound

            winsound.Beep(frequency, ms)
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


# --- microphone ------------------------------------------------------------------------


class Microphone:
    """Records into memory. Dictation lasts seconds, no file is needed."""

    def __init__(self, index: int | None):
        self._index = index
        self._pa = None
        self._stream = None
        self._blocks: list[bytes] = []
        self._channels = 1
        self._rate = SAMPLE_RATE
        self.level = 0.0
        self.name = ""
        # Set at the first audio block. Opening the stream takes about 0.35 s,
        # so the "speak" signal is given only then; otherwise the first word is lost.
        self.ready = threading.Event()

    def set_index(self, index: int | None) -> None:
        self._index = index

    def prepare(self) -> None:
        """Initializes PortAudio up front; at the first recording it would cost another 0.5 s."""
        from . import audio

        if self._pa is None:
            self._pa = audio.new_pyaudio()

    def _device(self, p):
        from . import audio

        if self._index is not None:
            try:
                return audio._from_info(p.get_device_info_by_index(self._index), loopback=False)
            except Exception:
                log.warning("Microphone with index %s unavailable - using the default", self._index)
        return audio._from_info(p.get_default_input_device_info(), loopback=False)

    def _close_pa(self) -> None:
        from . import audio

        audio.close_pyaudio(self._pa)
        self._pa = None

    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        from . import audio

        # We keep the PyAudio instance, because creating it takes time and would
        # cut off the start of the utterance. When the device disappeared
        # (headphones changed), it is rebuilt.
        for attempt in range(2):
            try:
                if self._pa is None:
                    self._pa = audio.new_pyaudio()
                device = self._device(self._pa)
                self._channels, self._rate = device.channels, device.rate
                self._blocks = []
                self.level = 0.0
                self.ready.clear()
                self._stream = self._pa.open(
                    format=pyaudio.paInt16, channels=self._channels,
                    rate=self._rate, input=True,
                    input_device_index=device.index, frames_per_buffer=1024,
                    stream_callback=self._callback,
                )
                self._stream.start_stream()
                self.name = device.name
                return
            except Exception:
                self._stream = None
                self._close_pa()
                if attempt:
                    raise
                log.warning("Opening the microphone failed - retrying", exc_info=True)

    def _callback(self, in_data, frame_count, time_info, status):  # noqa: ARG002
        import pyaudiowpatch as pyaudio

        self._blocks.append(in_data)
        self.ready.set()
        try:
            self.level = float(np.abs(np.frombuffer(in_data, dtype=np.int16)).max()) / 32768.0
        except ValueError:
            pass
        return (None, pyaudio.paContinue)

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            for method in (stream.stop_stream, stream.close):
                try:
                    method()
                except Exception:
                    pass
        data, self._blocks = b"".join(self._blocks), []
        signal = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if self._channels > 1:
            n = len(signal) // self._channels * self._channels
            signal = signal[:n].reshape(-1, self._channels).mean(axis=1)
        if self._rate != SAMPLE_RATE and signal.size:
            import soxr

            signal = soxr.resample(signal, self._rate, SAMPLE_RATE, quality="HQ")
        return np.ascontiguousarray(signal, dtype=np.float32)

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def close(self) -> None:
        if self._stream is not None:
            self.stop()
        self._close_pa()


# --- controller ---------------------------------------------------------------------------


class DictationController:
    """State of dictation for the Dictation screen, the small window and the tray."""

    def __init__(self, app):
        self.app = app
        self.microphone = Microphone(app.settings["microphone_index"])
        self.recording = False
        self.finishing = False
        self.recording_start = 0.0
        self.queued = 0
        self.model_state = "idle"  # idle / loading / ready / error
        self.model_error = ""
        self.model_device = ""
        self.last_text = ""
        self.last_seconds = 0.0
        self.target_window: int | None = None  # foreground window when the dictation started
        self._message: tuple[str, str] | None = None
        self._message_until = 0.0
        self._listeners: list[Callable[[str, object], None]] = []

    # --- events -------------------------------------------------------------

    def listen(self, callback: Callable[[str, object], None]) -> None:
        self._listeners.append(callback)

    def clear_listeners(self) -> None:
        self._listeners.clear()

    def _emit(self, event: str, data=None) -> None:
        def deliver():
            for callback in list(self._listeners):
                try:
                    callback(event, data)
                except Exception:
                    log.exception("Listener for %s failed", event)
        self.app.post(deliver)

    def show_message(self, text: str, style: str = "ready", seconds: float = 3.0) -> None:
        self._message = (text, style)
        self._message_until = time.monotonic() + seconds

    @property
    def settings(self) -> config.Settings:
        return self.app.settings

    def model_key(self) -> tuple[str, str]:
        """(model, device) dictation uses; the transcription model when its own is missing."""
        model = self.settings.dictation_model()
        if model != self.settings["model"] and not transcribe.model_downloaded(model):
            log.warning("Dictation model %s is not downloaded - using %s", model, self.settings["model"])
            model = self.settings["model"]
        return model, self.settings["device"]

    def status(self) -> tuple[str, str, str]:
        """(headline, pill style, hint) for the UI."""
        hotkey = self.app.dictation_hotkey_label()
        hint = t("{shortcut} — speak, press again — insert text", shortcut=hotkey)
        error = self.app.hotkey_errors.get("dictate")
        if self.recording:
            elapsed = time.monotonic() - self.recording_start
            return (t("Recording  {time}", time=f"{int(elapsed) // 60}:{int(elapsed) % 60:02d}"),
                    "recording", t("{shortcut} finishes, Esc cancels", shortcut=hotkey))
        if self.queued:
            return t("Recognizing…"), "working", hint
        if self._message and time.monotonic() < self._message_until:
            return self._message[0], self._message[1], hint
        if error:
            return t("Shortcut unavailable"), "warning", error
        if self.model_state == "error":
            return t("The model failed to load"), "warning", self.model_error[:120]
        if self.model_state == "loading":
            return t("Loading model…"), "idle", t("You can already speak, the text will appear once it loads")
        return t("Ready"), "ready", hint

    def level(self) -> float:
        return min(1.0, self.microphone.level * 3) if self.recording else 0.0

    # --- model -------------------------------------------------------------------

    def preload(self) -> None:
        """Loads the dictation model in the background, if the setting says so."""
        key = self.model_key()
        preload = bool(self.settings["dictation_preload"])
        if not transcribe.model_downloaded(key[0]):
            self.model_state, self.model_error = "error", t("Model {model} is not downloaded.",
                                                             model=key[0].split("/")[-1])
            self.app.engine.configure(None, False)
            return
        self.model_state = "loading" if preload else "idle"

        def done(device):
            self.model_state = "ready" if device or not preload else "idle"
            self.model_device = device or ""
            self._emit("state")

        def failed(error):
            self.model_state, self.model_error = "error", str(error)
            self._emit("state")

        self.app.engine.configure(key, preload, on_done=done, on_error=failed)

    # --- recording ---------------------------------------------------------------

    def toggle(self) -> None:
        if self.finishing:
            return
        if self.recording:
            self.stop()
        else:
            self.start()

    def start(self) -> bool:
        if self.recording:
            return False
        # Before anything of ours can take the focus: this is where the text goes.
        try:
            self.target_window = dictation_target()
        except Exception:
            self.target_window = None
        self.microphone.set_index(self.settings["microphone_index"])
        try:
            self.microphone.start()
        except Exception as error:
            log.exception("Could not open the microphone")
            self.show_message(t("Microphone unavailable"), "warning", 6)
            self.last_text = str(error)[:200]
            self._emit("state")
            return False
        log.info("Dictation from: %s", self.microphone.name)
        self.recording = True
        self.recording_start = time.monotonic()
        self.app.hotkeys.escape(True)
        if self.settings["dictation_sounds"]:
            ready = self.microphone.ready
            threading.Thread(target=lambda: ready.wait(2.0) and beep(1046), daemon=True).start()
        self._emit("state")
        return True

    def stop(self) -> None:
        if not self.recording or self.finishing:
            return
        self.finishing = True
        self.app.after(int(TAIL_S * 1000), self._finish)

    def _finish(self) -> None:
        recording = self.microphone.stop()
        self.recording = self.finishing = False
        self.app.hotkeys.escape(False)
        if self.settings["dictation_sounds"]:
            beep(659)
        self.queued += 1
        model, device = self.model_key()
        language = self.settings["dictation_language"]
        mode, enter = self.settings["dictation_mode"], bool(self.settings["dictation_enter"])
        target = self.target_window
        submitted = time.monotonic()

        def done(result):
            text, seconds, used = result
            waited = time.monotonic() - submitted
            # last_text_from_log() parses this line; keep its format.
            log.info("Recognized %.1f s of audio in %.1f s: %r", recording.size / SAMPLE_RATE,
                     waited, text)
            self.model_state, self.model_device = "ready", used

            def insert():
                reported: list[str | None] = []

                def report(where: str | None) -> None:
                    if not reported:
                        reported.append(where)
                        self.app.post(lambda: self._recognized(text, waited, where))

                if text:
                    try:
                        # Trailing space: two dictations in a row do not glue into one word.
                        insert_text(text if enter else text + " ", mode, enter, target, report)
                    except Exception:
                        log.exception("Could not insert the text")
                report(None)

            threading.Thread(target=insert, name="insert-text", daemon=True).start()

        def failed(error):
            self.app.post(lambda: self._failed(str(error)))

        self.app.engine.dictate(recording, model, device, language,
                                self.settings["dictation_dictionary"], on_done=done, on_error=failed)
        self._emit("state")

    def _recognized(self, text: str, seconds: float, where: str | None) -> None:
        """`where`: PASTE, OWN_WINDOW, FOCUS_LOST, or None when inserting failed."""
        self.queued = max(0, self.queued - 1)
        if text:
            self.last_text, self.last_seconds = text, seconds
            if where == FOCUS_LOST:
                # The window you dictated into is gone or did not come back:
                # better on the clipboard than pasted into the wrong window.
                self.show_message(t("Text is on the clipboard, press Ctrl+V"), "warning", 8)
            elif where is None:
                self.show_message(t("Clipboard busy, try again"), "warning", 6)
            else:
                label = t("Inserted") if where == PASTE else t("Copied to clipboard")
                self.show_message(f"{label} ({seconds:.1f} s)")
        else:
            self.show_message(t("I didn't hear anything"), "idle")
        self._emit("text", text)

    def _failed(self, error: str) -> None:
        self.queued = max(0, self.queued - 1)
        self.show_message(t("Recognition error"), "warning", 6)
        self.last_text = error[:200]
        self._emit("state")

    def cancel(self) -> None:
        if not self.recording:
            return
        self.microphone.stop()
        self.recording = self.finishing = False
        self.app.hotkeys.escape(False)
        if self.settings["dictation_sounds"]:
            beep(330, 120)
        self.show_message(t("Cancelled"), "idle")
        self._emit("state")

    def copy_last(self) -> bool:
        if not self.last_text:
            return False
        try:
            set_clipboard(self.last_text)
        except Exception:
            log.exception("Could not copy to the clipboard")
            self.show_message(t("Clipboard busy, try again"), "warning", 4)
            return False
        self.show_message(t("Copied ({n} characters)", n=len(self.last_text)))
        return True

    def check_limit(self) -> None:
        """Stops a recording that runs past the maximum length (called from the UI tick)."""
        if (self.recording and not self.finishing and
                time.monotonic() - self.recording_start > float(self.settings["dictation_max_s"])):
            self.stop()

    def quit(self) -> None:
        if self.recording:
            self.microphone.stop()
            self.recording = False
        self.microphone.close()
