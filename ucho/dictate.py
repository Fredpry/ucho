# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dictate: you speak into the microphone, the text lands where the cursor is.

Ctrl+Shift+Space starts recording, a second press ends it. The recording
goes through the same Whisper as Ucho (OpenVINO, locally), and the result is
pasted into the active window: a terminal, a browser, anything.
Esc while recording cancels.

A few decisions that are not obvious:
  * The hotkey is registered with `RegisterHotKey`, not pynput. pynput only
    listens, so Ctrl+Shift+Space would still reach the terminal (Windows
    Terminal opens its new-tab menu on it). RegisterHotKey captures the
    combination and nobody else gets it.
  * Esc is registered only while recording; otherwise we do not take it
    away from other programs.
  * The model stays in memory for the whole life of the app. Loading takes
    a few seconds, and dictation has to start immediately.
  * All model calls go through one worker thread: the OpenVINO pipeline is
    not safe to call from several threads.
  * The recording state shows on the taskbar button: a red bar = recording,
    a flowing green bar = recognizing.
"""

from __future__ import annotations

import ctypes
import json
import logging
import queue
import re
import sys
import threading
import time
import traceback
from ctypes import byref, c_void_p, wintypes
from pathlib import Path

import numpy as np

from . import config, i18n
from .i18n import t

log = logging.getLogger(__name__)

# The window title is also used to find a running instance (FindWindowW), so
# it is set after the language is chosen, in main().
APP_NAME = "Ucho Dictate"
APP_ID = "Ucho.Dictate"
SETTINGS_FILE = config.PROJECT_DIR / "dictate.json"
# v0.1 file name; migrated to dictate.json on first start (and kept).
LEGACY_SETTINGS_FILE = config.PROJECT_DIR / "dyktuj.json"
LOG_FILE = config.PROJECT_DIR / "dictate.log"
LEGACY_LOG_FILE = config.PROJECT_DIR / "dyktuj.log"
ICON_FILE = Path(__file__).resolve().parent / "assets" / "dictate.ico"
STARTER = config.PROJECT_DIR / "dictate.pyw"

SAMPLE_RATE = 16_000
# People press the hotkey just as they finish the last word; without this
# tail the end of the utterance is sometimes cut off.
TAIL_S = 0.25
MIN_RECORDING_S = 0.3

DEFAULTS = {
    "language": "auto",     # auto (PL/EN) / pl / en
    "enter": False,         # Enter after pasting, i.e. "send" right away
    "mode": "paste",        # paste (clipboard + Ctrl+V) / type (simulated keyboard)
    "sounds": True,
    "max_recording_s": 300,
    # Dictation is short recordings, so we can afford full FP16 precision
    # (~3 GB, about 25% slower than INT8). When the model is not downloaded,
    # the one from the main Ucho settings is used.
    "model": "OpenVINO/whisper-large-v3-fp16-ov",
    # Names that Whisper keeps getting wrong. Key = what it recognized
    # (case-insensitive, whole words), value = the correct form.
    # Example; your own corrections go into dictate.json.
    "dictionary": {
        "Cloud Code": "Claude Code",
        "Cloud Codem": "Claude Code'em",
    },
}

# v0.1 key -> current key ("enter" and "model" did not change).
LEGACY_KEYS = {
    "jezyk": "language",
    "tryb": "mode",
    "dzwieki": "sounds",
    "maks_nagranie_s": "max_recording_s",
    "slownik": "dictionary",
}
LEGACY_MODES = {"wklej": "paste", "pisz": "type"}

BACKGROUND = "#15171d"
PANEL = "#1e2129"
ACCENT = "#a78bfa"
RED = "#ef4444"
TEXT = "#e6e9ef"
TEXT_MUTED = "#8b93a5"
GREEN = "#34d399"


# --- settings ---------------------------------------------------------------


def migrate_legacy_dictate_settings(old: dict) -> dict:
    """v0.1 dyktuj.json content -> current keys and values. Unknown keys are dropped."""
    new: dict = {}
    for key, value in old.items():
        key = LEGACY_KEYS.get(key, key)
        if key not in DEFAULTS:
            continue
        if key == "mode" and isinstance(value, str):
            value = LEGACY_MODES.get(value, value)
        new[key] = value
    return new


def load_settings(file: Path | None = None, legacy_file: Path | None = None) -> dict:
    file = file or SETTINGS_FILE
    legacy_file = legacy_file or file.with_name(LEGACY_SETTINGS_FILE.name)
    config.migrate_file(file, legacy_file, migrate_legacy_dictate_settings)
    data = dict(DEFAULTS)
    try:
        stored = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


# Log line of a finished dictation, in the current and the v0.1 wording.
_RECOGNIZED_LINE = re.compile(
    r"(?:Recognized [\d.]+ s of audio in [\d.]+ s|Rozpoznano [\d.]+ s audio w [\d.]+ s): (.+)$"
)


def last_text_from_log(file: Path | None = None) -> str:
    """The last non-empty dictation from the log, so it survives an app restart."""
    import ast

    if file is None:
        file = LOG_FILE if LOG_FILE.exists() else LEGACY_LOG_FILE
    try:
        with open(file, "rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 256_000))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        match = _RECOGNIZED_LINE.search(line)
        if not match:
            continue
        try:
            text = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            continue
        if isinstance(text, str) and text.strip():
            return text
    return ""


def save_settings(data: dict, file: Path | None = None) -> None:
    file = file or SETTINGS_FILE
    try:
        file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.warning("Could not save %s", file)


# --- icon -------------------------------------------------------------------


def draw_icon(size: int = 256):
    """Microphone with sound waves, violet so it is not confused with the main Ucho window."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    r = ImageDraw.Draw(image)
    s = size / 256.0
    color = (167, 139, 250, 255)

    def p(*xs):
        return [int(x * s) for x in xs]

    r.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(56 * s),
                        fill=(24, 26, 32, 255))
    r.rounded_rectangle(p(104, 52, 152, 140), radius=int(24 * s), fill=color)
    r.arc(p(82, 86, 174, 172), start=0, end=180, fill=color, width=max(2, int(11 * s)))
    r.rounded_rectangle(p(122, 168, 134, 196), radius=int(6 * s), fill=color)
    r.rounded_rectangle(p(96, 190, 160, 202), radius=int(6 * s), fill=color)
    # Waves on both sides of the capsule.
    for radius in (62, 86):
        bbox = p(128 - radius, 96 - radius, 128 + radius, 96 + radius)
        r.arc(bbox, start=-35, end=35, fill=color, width=max(2, int(9 * s)))
        r.arc(bbox, start=145, end=215, fill=color, width=max(2, int(9 * s)))
    return image


def save_icon(path: Path = ICON_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    draw_icon(256).save(path, format="ICO",
                        sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    return path


# --- WinAPI -----------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                 wintypes.UINT, wintypes.UINT, wintypes.UINT]
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.SetClipboardData.argtypes = [wintypes.UINT, c_void_p]
_user32.SetClipboardData.restype = c_void_p
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = c_void_p
_kernel32.GlobalLock.argtypes = [c_void_p]
_kernel32.GlobalLock.restype = c_void_p
_kernel32.GlobalUnlock.argtypes = [c_void_p]
_kernel32.GlobalFree.argtypes = [c_void_p]
_kernel32.CreateMutexW.argtypes = [c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE

MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_V = 0x56
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_TOGGLE_ESC = 0x8001  # WM_APP + 1
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
ERROR_ALREADY_EXISTS = 183

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


def set_clipboard(text: str, hwnd: int | None) -> None:
    buffer = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buffer)
    for _ in range(20):
        if _user32.OpenClipboard(hwnd or None):
            break
        time.sleep(0.05)  # the clipboard is sometimes briefly held by another program
    else:
        raise RuntimeError(t("The clipboard is in use by another program"))
    try:
        _user32.EmptyClipboard()
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise MemoryError("GlobalAlloc")
        pointer = _kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, buffer, size)
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
            raise RuntimeError("SetClipboardData failed")
    finally:
        _user32.CloseClipboard()


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


def insert_text(text: str, mode: str, enter: bool, own_window: int) -> bool:
    """Inserts text into the active window. False = clipboard only (our window was active)."""
    wait_for_keys_released()
    active = _user32.GetForegroundWindow() or 0
    if mode != "type" or active == own_window:
        set_clipboard(text, own_window)
    if active == own_window:
        return False
    if mode == "type":
        type_text(text)
    else:
        press_ctrl_v()
    if enter:
        # The terminal must digest the paste first, otherwise Enter overtakes it.
        time.sleep(0.25)
        press_enter()
    return True


class SystemHotkey:
    """Ctrl+Shift+Space via RegisterHotKey, plus Esc only while recording.

    System hotkeys belong to the thread that registered them, so this thread
    runs its own message loop. Registering Esc is requested with a message.
    """

    TOGGLE = 1
    CANCEL = 2

    def __init__(self, action):
        self._action = action
        self._thread_id = 0
        self._ready = threading.Event()
        self.error: str | None = None

    def start(self) -> bool:
        threading.Thread(target=self._loop, name="hotkey", daemon=True).start()
        self._ready.wait(3.0)
        return self.error is None

    def _loop(self) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Forces the message queue to exist before anyone posts to it.
        _user32.PeekMessageW(byref(msg), None, 0, 0, 0)
        if not _user32.RegisterHotKey(None, self.TOGGLE,
                                      MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE):
            code = ctypes.get_last_error()
            self.error = t("Ctrl+Shift+Space is taken by another program (code {code}).",
                           code=code)
            log.warning(self.error)
            self._ready.set()
            return
        log.info("Hotkey Ctrl+Shift+Space registered")
        self._ready.set()
        try:
            while _user32.GetMessageW(byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._action(int(msg.wParam))
                elif msg.message == WM_TOGGLE_ESC:
                    if msg.wParam:
                        _user32.RegisterHotKey(None, self.CANCEL, MOD_NOREPEAT, VK_ESCAPE)
                    else:
                        _user32.UnregisterHotKey(None, self.CANCEL)
        finally:
            _user32.UnregisterHotKey(None, self.TOGGLE)
            _user32.UnregisterHotKey(None, self.CANCEL)

    def esc(self, enable: bool) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_TOGGLE_ESC, int(enable), 0)

    def stop(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


class TaskbarProgress:
    """Progress bar on the taskbar button (ITaskbarList3)."""

    NONE = 0x0
    INDETERMINATE = 0x1
    ERROR = 0x4  # red; we use it as "recording"

    _CLSID = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
    _IID = "{ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf}"

    def __init__(self, hwnd: int):
        from . import winshell

        self._ws = winshell
        self._hwnd = hwnd
        self._obj = c_void_p()
        try:
            winshell._com_start()
            ctypes.windll.ole32.CoCreateInstance(
                byref(winshell._guid(self._CLSID)), None, 1,
                byref(winshell._guid(self._IID)), byref(self._obj),
            )
            if self._obj:
                winshell._method(self._obj, 3)(self._obj)  # HrInit
        except Exception:
            log.debug("ITaskbarList3 not available", exc_info=True)
            self._obj = c_void_p()

    def set(self, state: int, value: int | None = None) -> None:
        if not self._obj:
            return
        try:
            if value is not None:
                self._ws._method(self._obj, 9, wintypes.HWND, ctypes.c_ulonglong,
                                 ctypes.c_ulonglong)(self._obj, self._hwnd, value, 100)
            self._ws._method(self._obj, 10, wintypes.HWND, ctypes.c_int)(
                self._obj, self._hwnd, state)
        except Exception:
            log.debug("Could not set the taskbar progress", exc_info=True)


def beep(frequency: int, ms: int = 70) -> None:
    def _play():
        try:
            import winsound

            winsound.Beep(frequency, ms)
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


# --- microphone -------------------------------------------------------------


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

    def prepare(self) -> None:
        """Initializes PortAudio up front; at the first recording it would cost another 0.5 s."""
        import pyaudiowpatch as pyaudio

        if self._pa is None:
            self._pa = pyaudio.PyAudio()

    def _device(self, p):
        from . import audio

        if self._index is not None:
            try:
                return audio._from_info(p.get_device_info_by_index(self._index), loopback=False)
            except Exception:
                log.warning("Microphone with index %s unavailable - using the default",
                            self._index)
        return audio._from_info(p.get_default_input_device_info(), loopback=False)

    def _close_pa(self) -> None:
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
        self._pa = None

    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        # We keep the PyAudio instance, because creating it takes time and would
        # cut off the start of the utterance. When the device disappeared
        # (headphones changed), it is rebuilt.
        for attempt in range(2):
            try:
                if self._pa is None:
                    self._pa = pyaudio.PyAudio()
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


# --- recognition ------------------------------------------------------------


def apply_dictionary(text: str, dictionary: dict) -> str:
    for wrong, right in (dictionary or {}).items():
        if wrong and isinstance(right, str):
            text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text,
                          flags=re.IGNORECASE)
    return text


def recognize(engine, recording: np.ndarray, language_code: str,
              dictionary: dict | None = None) -> str:
    from . import transcribe
    from .merge import is_hallucination

    if recording.size < MIN_RECORDING_S * SAMPLE_RATE:
        return ""
    # On silence Whisper invents sentences; better to insert nothing.
    if transcribe.peak_rms(recording) < transcribe.SPEECH_RMS_THRESHOLD:
        log.info("Recording skipped - silence only")
        return ""

    # Pauses for thinking are the main source of invented "Dziękuję." and of
    # lost fragments; see transcribe.shorten_pauses.
    before = recording.size
    recording = transcribe.shorten_pauses(recording)
    if recording.size < before:
        log.info("Pauses shortened: %.1f s -> %.1f s",
                 before / SAMPLE_RATE, recording.size / SAMPLE_RATE)
    if recording.size < MIN_RECORDING_S * SAMPLE_RATE:
        return ""

    engine.load()
    result = engine.generate(recording, config.LANGUAGES.get(language_code))

    texts = getattr(result, "texts", None)
    if texts:
        text = str(texts[0])
    else:
        chunks = transcribe._chunks(result)
        text = " ".join(str(getattr(c, "text", "")) for c in chunks) if chunks else str(result)

    text = re.sub(r"\s+", " ", text).strip()
    if is_hallucination(text):
        return ""
    return apply_dictionary(text, dictionary)


# --- window -----------------------------------------------------------------


def _run_window():
    import customtkinter as ctk

    from . import transcribe, winshell

    class Dictate(ctk.CTk):
        def __init__(self):
            super().__init__()
            main_settings = config.Settings()
            self.settings = load_settings()
            model = self.settings.get("model") or main_settings["model"]
            if model != main_settings["model"] and not transcribe.model_downloaded(model):
                log.warning("Model %s is not downloaded - using %s", model, main_settings["model"])
                model = main_settings["model"]
            log.info("Dictation model: %s", model)
            self.engine = transcribe.Engine(model, main_settings["device"])
            self.microphone = Microphone(main_settings["microphone_index"])

            self._events: "queue.Queue[tuple]" = queue.Queue()
            self._jobs: "queue.Queue[tuple | None]" = queue.Queue()
            self.model_ready = False
            self.model_error: str | None = None
            self.recording = False
            self.finishing = False
            self.recording_start = 0.0
            self.queued = 0
            self._hwnd = 0
            self._taskbar: TaskbarProgress | None = None
            self._message: tuple[str, str] | None = None
            self._message_until = 0.0
            self.last_text = last_text_from_log()

            self._build()

            self.hotkey = SystemHotkey(lambda code: self._events.put(("hotkey", code)))
            self.hotkey.start()

            threading.Thread(target=self._work_loop, name="whisper", daemon=True).start()
            self._jobs.put(("load",))

            self.protocol("WM_DELETE_WINDOW", self.quit_app)
            self.after(60, self._tick)
            # The window must exist in the system before we describe it for the taskbar.
            self.after(600, self._after_start)

        # --- interface ---------------------------------------------------

        def _build(self) -> None:
            ctk.set_appearance_mode("dark")
            self.title(APP_NAME)
            self.geometry("400x250")
            self.resizable(False, False)
            self.configure(fg_color=BACKGROUND)
            self._set_icon()
            self.after(400, self._set_icon)

            frame = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
            frame.pack(fill="both", expand=True, padx=12, pady=12)

            top = ctk.CTkFrame(frame, fg_color="transparent")
            top.pack(fill="x", padx=14, pady=(12, 0))
            # Rescue when the paste missed or the clipboard was overwritten:
            # copies the full last text (the label below shows only its start).
            self.b_copy = ctk.CTkButton(top, text=t("Copy"), width=70, height=26,
                                        fg_color=ACCENT, hover_color="#8b5cf6",
                                        text_color=BACKGROUND,
                                        font=ctk.CTkFont(size=12, weight="bold"),
                                        command=self.copy_last)
            self.b_copy.pack(side="right")
            self.l_status = ctk.CTkLabel(top, text="", font=ctk.CTkFont(size=17, weight="bold"),
                                         text_color=TEXT, anchor="w")
            self.l_status.pack(side="left", fill="x", expand=True)
            self.l_hint = ctk.CTkLabel(frame, text="", text_color=TEXT_MUTED,
                                       anchor="w", font=ctk.CTkFont(size=12))
            self.l_hint.pack(fill="x", padx=14)

            self.level = ctk.CTkProgressBar(frame, height=6, progress_color=RED)
            self.level.set(0)
            self.level.pack(fill="x", padx=14, pady=(8, 6))

            self.l_text = ctk.CTkLabel(frame, text="", text_color=TEXT_MUTED,
                                       anchor="nw", justify="left", wraplength=340,
                                       height=54, font=ctk.CTkFont(size=12))
            self.l_text.pack(fill="x", padx=14)
            if self.last_text:
                self.l_text.configure(text=self.last_text)
            else:
                self.b_copy.configure(state="disabled")

            bottom = ctk.CTkFrame(frame, fg_color="transparent")
            bottom.pack(fill="x", padx=12, pady=(4, 10), side="bottom")
            labels = {"pl": "PL", "en": "EN", "auto": "Auto"}
            self._languages = {v: k for k, v in labels.items()}
            self.w_language = ctk.CTkSegmentedButton(
                bottom, values=list(labels.values()), command=self._change_language,
                selected_color=ACCENT, selected_hover_color=ACCENT, width=150,
            )
            self.w_language.set(labels.get(self.settings["language"], "PL"))
            self.w_language.pack(side="left")
            self.v_enter = ctk.BooleanVar(value=bool(self.settings["enter"]))
            ctk.CTkCheckBox(bottom, text=t("Enter after pasting"), variable=self.v_enter,
                            command=self._change_enter, fg_color=ACCENT,
                            hover_color=ACCENT, checkbox_width=18, checkbox_height=18,
                            font=ctk.CTkFont(size=12)).pack(side="right")

        def _set_icon(self) -> None:
            try:
                if not ICON_FILE.exists():
                    save_icon()
                self.iconbitmap(default=str(ICON_FILE))
            except Exception:
                log.debug("Could not set the icon", exc_info=True)

        def _after_start(self) -> None:
            try:
                self._hwnd = int(self.wm_frame(), 16)
            except Exception:
                self._hwnd = self.winfo_id()
            command = f'"{_pythonw()}" "{STARTER}"'
            winshell.describe_window(self._hwnd, command, APP_NAME, f"{ICON_FILE},0", APP_ID)
            self._taskbar = TaskbarProgress(self._hwnd)
            self._update_taskbar()

        def _change_language(self, label: str) -> None:
            self.settings["language"] = self._languages.get(label, "pl")
            save_settings(self.settings)

        def _change_enter(self) -> None:
            self.settings["enter"] = bool(self.v_enter.get())
            save_settings(self.settings)

        def copy_last(self) -> None:
            if not self.last_text:
                return
            try:
                set_clipboard(self.last_text, self._hwnd or None)
            except Exception:
                log.exception("Could not copy to the clipboard")
                self._show_message(t("Clipboard busy, try again"), RED, 4)
                return
            self._show_message(t("Copied ({n} characters)", n=len(self.last_text)))

        def _show_message(self, text: str, color: str = GREEN, s: float = 3.0) -> None:
            self._message = (text, color)
            self._message_until = time.monotonic() + s

        def _refresh(self) -> None:
            hint = t("Ctrl+Shift+Space — speak, press again — insert text")
            if self.hotkey.error:
                status, color, hint = t("Shortcut unavailable"), RED, self.hotkey.error
            elif self.recording:
                elapsed = time.monotonic() - self.recording_start
                status = t("●  Recording  {time}",
                           time=f"{int(elapsed) // 60}:{int(elapsed) % 60:02d}")
                color, hint = RED, t("Ctrl+Shift+Space finishes, Esc cancels")
            elif self.queued:
                status, color = t("Recognizing…"), ACCENT
            elif self._message and time.monotonic() < self._message_until:
                status, color = self._message
            elif self.model_error:
                status, color, hint = t("The model failed to load"), RED, self.model_error[:90]
            elif not self.model_ready:
                status, color = t("Loading model…"), TEXT_MUTED
                hint = t("You can already speak, the text will appear once it loads")
            else:
                status, color = t("Ready"), GREEN
            self.l_status.configure(text=status, text_color=color)
            self.l_hint.configure(text=hint)
            self.level.set(min(1.0, self.microphone.level * 3) if self.recording else 0)

        def _update_taskbar(self) -> None:
            if self._taskbar is None:
                return
            if self.recording:
                self._taskbar.set(TaskbarProgress.ERROR, 100)
            elif self.queued or not (self.model_ready or self.model_error):
                self._taskbar.set(TaskbarProgress.INDETERMINATE)
            else:
                self._taskbar.set(TaskbarProgress.NONE)

        def _tick(self) -> None:
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                self._handle(event)
            if (self.recording and not self.finishing and
                    time.monotonic() - self.recording_start
                    > float(self.settings["max_recording_s"])):
                self.stop()
            self._refresh()
            self.after(80, self._tick)

        def _handle(self, event: tuple) -> None:
            kind = event[0]
            if kind == "hotkey":
                if event[1] == SystemHotkey.TOGGLE:
                    self.toggle()
                elif event[1] == SystemHotkey.CANCEL and self.recording:
                    self.cancel()
            elif kind == "model":
                _, ok, description = event
                self.model_ready, self.model_error = ok, (None if ok else description)
                if ok:
                    self._show_message(t("Ready ({description})", description=description))
            elif kind == "text":
                _, text, elapsed, inserted = event
                self.queued -= 1
                if text:
                    self.last_text = text
                    self.b_copy.configure(state="normal")
                    self.l_text.configure(text=text, text_color=TEXT)
                    where = t("Inserted") if inserted else t("Copied to clipboard")
                    self._show_message(f"{where} ({elapsed:.1f} s)")
                else:
                    self._show_message(t("I didn't hear anything"), TEXT_MUTED)
            elif kind == "error":
                self.queued -= 1
                self._show_message(t("Recognition error"), RED, 6)
                self.l_text.configure(text=event[1][:200], text_color=RED)
            self._update_taskbar()

        # --- recording ---------------------------------------------------

        def toggle(self) -> None:
            if self.finishing:
                return
            if self.recording:
                self.stop()
            else:
                self.start()

        def start(self) -> None:
            try:
                self.microphone.start()
            except Exception as error:
                log.exception("Could not open the microphone")
                self._show_message(t("Microphone unavailable"), RED, 6)
                self.l_text.configure(text=str(error)[:200], text_color=RED)
                return
            log.info("Recording from: %s", self.microphone.name)
            self.recording = True
            self.recording_start = time.monotonic()
            self.hotkey.esc(True)
            if self.settings["sounds"]:
                ready = self.microphone.ready
                threading.Thread(target=lambda: ready.wait(2.0) and beep(1046),
                                 daemon=True).start()
            self._update_taskbar()

        def stop(self) -> None:
            self.finishing = True
            self.after(int(TAIL_S * 1000), self._finish)

        def _finish(self) -> None:
            recording = self.microphone.stop()
            self.recording = self.finishing = False
            self.hotkey.esc(False)
            if self.settings["sounds"]:
                beep(659)
            self.queued += 1
            self._jobs.put(("recognize", recording, self.settings["language"],
                            bool(self.settings["enter"]), self.settings["mode"]))
            self._update_taskbar()

        def cancel(self) -> None:
            self.microphone.stop()
            self.recording = self.finishing = False
            self.hotkey.esc(False)
            if self.settings["sounds"]:
                beep(330, 120)
            self._show_message(t("Cancelled"), TEXT_MUTED)
            self._update_taskbar()

        # --- worker thread -----------------------------------------------

        def _work_loop(self) -> None:
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                if job[0] == "load":
                    try:
                        self.microphone.prepare()
                    except Exception:
                        log.warning("PortAudio did not start up front", exc_info=True)
                    try:
                        start = time.monotonic()
                        self.engine.load()
                        self._events.put(("model", True,
                                          f"{self.engine.device}, {time.monotonic() - start:.0f} s"))
                    except Exception as error:
                        log.exception("Could not load the model")
                        self._events.put(("model", False, str(error)))
                    continue

                _, recording, language, enter, mode = job
                try:
                    start = time.monotonic()
                    text = recognize(self.engine, recording, language,
                                     self.settings.get("dictionary"))
                    elapsed = time.monotonic() - start
                    # last_text_from_log() parses this line; keep its format.
                    log.info("Recognized %.1f s of audio in %.1f s: %r",
                             recording.size / SAMPLE_RATE, elapsed, text)
                    inserted = False
                    if text:
                        # Trailing space: two dictations in a row do not glue into one word.
                        inserted = insert_text(text if enter else text + " ",
                                               mode, enter, self._hwnd)
                    self._events.put(("text", text, elapsed, inserted))
                except Exception as error:
                    log.exception("Recognition error")
                    self._events.put(("error", str(error)))

        def quit_app(self) -> None:
            self.hotkey.stop()
            self.microphone.close()
            self._jobs.put(None)
            self.destroy()

    Dictate().mainloop()


# --- start ------------------------------------------------------------------

_mutex = None


def _pythonw() -> Path:
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    return windowless if windowless.exists() else interpreter


def _single_instance() -> bool:
    """A second click on the tile should show the running window, not start a copy."""
    global _mutex
    _mutex = _kernel32.CreateMutexW(None, False, "Local\\Ucho.Dictate")
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return True
    window = _user32.FindWindowW(None, APP_NAME)
    if window:
        _user32.ShowWindow(window, 9)  # SW_RESTORE
        _user32.SetForegroundWindow(window)
    return False


def create_shortcuts() -> list[Path]:
    """Shortcuts in the Start menu and on the Desktop, with our AppUserModelID."""
    import os
    import subprocess

    from . import winshell

    if not ICON_FILE.exists():
        save_icon()
    targets = [
        Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        / "Ucho Dictate.lnk",
        Path(os.environ["USERPROFILE"]) / "Desktop" / "Ucho Dictate.lnk",
    ]
    for target in targets:
        script = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
            "$s.TargetPath=$env:LNK_PY;$s.Arguments=$env:LNK_ARG;"
            "$s.WorkingDirectory=$env:LNK_DIR;$s.IconLocation=$env:LNK_ICON;"
            "$s.Description='Voice dictation into any window (local Whisper)';$s.Save()"
        )
        env = dict(os.environ, LNK=str(target), LNK_PY=str(_pythonw()), LNK_ARG=f'"{STARTER}"',
                   LNK_DIR=str(config.PROJECT_DIR), LNK_ICON=str(ICON_FILE))
        subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env, check=True)
        winshell.set_shortcut_app_id(target, APP_ID)
    return targets


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        filename=str(LOG_FILE), filemode="a", encoding="utf-8",
    )
    if "--shortcuts" in sys.argv:
        for target in create_shortcuts():
            print(target)
        return 0
    global APP_NAME
    i18n.set_language(config.Settings()["ui_language"])
    APP_NAME = t("Ucho Dictate")
    if not _single_instance():
        return 0
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
    log.info("Start %s", APP_NAME)
    try:
        _run_window()
        return 0
    except Exception:
        trace = traceback.format_exc()
        log.error("Unhandled exception:\n%s", trace)
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(t("{app} — error", app=APP_NAME),
                                 f"{trace.strip().splitlines()[-1]}\n\n"
                                 + t("Details: {file}", file=LOG_FILE))
            root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
