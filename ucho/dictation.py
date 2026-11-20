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
  * A dictation started on the desktop or the taskbar, or with only the
    desktop or the taskbar active at paste time, stays on the clipboard:
    Ctrl+V there goes nowhere.
  * Paste mode borrows the clipboard: the dictated text is marked so that
    clipboard history (Win+V) and cloud clipboard sync skip it, and is handed
    over with delayed rendering. The previous text is put back only after the
    target window's process has taken the dictation (see `DelayedClipboard`);
    when in doubt the dictation stays on the clipboard instead.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import numpy as np

from . import config, downloads, live, platform_text, platforms, transcribe
from .events import Emitter
from .i18n import t

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
# People press the hotkey just as they finish the last word; without this
# tail the end of the utterance is sometimes cut off.
TAIL_S = 0.25
LIVE_TICK_MS = 250
# How long model_key() trusts its last look at the model folder.
MODEL_CHECK_S = 5.0
# How long the target window has to ask for the pasted text after Ctrl+V.
# A busy Electron app or a remote desktop session can take seconds; without
# a request in this time the text stays on the clipboard and nothing is restored.
PASTE_TIMEOUT_S = 5.0
# After the target has taken the text, before the previous text goes back.
# Some programs read the clipboard more than once for one paste; the later
# reads get the cached text only while the clipboard is still ours.
RESTORE_GRACE_S = 1.0
# Enter mode: between the target taking the text and Enter (a terminal must
# digest the paste first, otherwise Enter overtakes it).
ENTER_DELAY_S = 0.25


# --- platform primitives (ucho/platforms) ---


def set_clipboard(text: str | None, private: bool = True) -> int:
    """Puts `text` on the clipboard (None empties it). Returns the clipboard sequence number."""
    return platforms.clipboard().set_text(text, private)


def get_clipboard_text() -> str | None:
    """The clipboard text, or None when the clipboard holds no text."""
    return platforms.clipboard().get_text()


def clipboard_sequence() -> int:
    """Current platform clipboard change counter."""
    return platforms.clipboard().sequence()


def DelayedClipboard(text: str, consumer_pid: int | None):
    """Creates a delayed-rendering clipboard offer (not yet offered)."""
    return platforms.clipboard().offer_delayed(text, consumer_pid)


def press_ctrl_v() -> None:
    """Sends the platform paste shortcut."""
    platforms.inserter().paste()


def press_enter() -> None:
    """Sends Enter to the foreground window."""
    platforms.inserter().press_enter()


def type_text(text: str) -> None:
    """Types `text` directly as simulated keyboard input."""
    platforms.inserter().type_text(text)


def wait_for_keys_released(limit_s: float = 1.5) -> None:
    """Waits briefly for modifier keys to be released before pasting."""
    platforms.inserter().wait_keys_released(limit_s)


def foreground_window() -> int | None:
    """Handle/reference of the current foreground window, or None."""
    return platforms.inserter().foreground()


def window_process(window: int | None) -> int | None:
    """Process id of a window; None for no window."""
    return platforms.inserter().process_of(window)


def window_is_ours(window: int | None) -> bool:
    """True when the window belongs to this process (any Ucho window)."""
    return platforms.inserter().is_ours(window)


def window_exists(window: int) -> bool:
    """True when `window` still exists."""
    return platforms.inserter().exists(window)


def is_shell_window(window: int | None) -> bool:
    """True for the desktop or taskbar/shell window."""
    return platforms.inserter().is_shell(window)


def dictation_target() -> int | None:
    """The foreground window as the target of a dictation; None for the taskbar or desktop."""
    window = foreground_window()
    if not window or is_shell_window(window):
        return None
    return window


def activate(window: int) -> bool:
    """Attempts to bring `window` back to the foreground."""
    return platforms.inserter().activate(window)


def input_blocked() -> str | None:
    """NO_PERMISSION or SECURE_INPUT when simulated keys would be lost now, else None.

    Only macOS can block: key events need the Accessibility permission, and a
    password field or Terminal's "Secure Keyboard Entry" keeps them away. The
    check is an optional `input_blocked()` of the platform inserter (not part
    of `base.TextInserter`); other platforms and errors mean "not blocked".
    """
    check = getattr(platforms.inserter(), "input_blocked", None)
    if check is None:
        return None
    try:
        return check()
    except Exception:
        log.debug("Could not check whether input is blocked", exc_info=True)
        return None


# Where the dictated text goes, decided by `paste_target` and `insert_text`.
PASTE = "paste"            # pasted into the foreground window, which is the right one
OWN_WINDOW = "own_window"  # a Ucho window is active: clipboard only
FOCUS_LOST = "focus_lost"  # the target window is gone or did not come back: clipboard only
NO_WINDOW = "no_window"    # only the desktop or the taskbar is active: clipboard only
NOT_TAKEN = "not_taken"    # Ctrl+V sent, but the window did not take the text in time: clipboard only
# macOS: no keys sent at all, clipboard only.
NO_PERMISSION = "no_permission"  # Ucho has no Accessibility permission
SECURE_INPUT = "secure_input"    # a password field or secure keyboard entry is active

# Results that leave the text on the clipboard for the user to paste by hand.
CLIPBOARD_ONLY = (FOCUS_LOST, NO_WINDOW, NOT_TAKEN)
# The same, with their own message (the cause is fixed outside the window).
INPUT_BLOCKED = (NO_PERMISSION, SECURE_INPUT)


def paste_target(target: int | None, foreground: Callable[[], int | None],
                 is_ours: Callable[[int | None], bool], exists: Callable[[int], bool],
                 activate_window: Callable[[int], bool],
                 is_shell: Callable[[int | None], bool] = lambda _w: False) -> str:
    """PASTE, OWN_WINDOW, NO_WINDOW or FOCUS_LOST for a dictation that started with `target` in front.

    A target that belongs to Ucho (the dictation started from its own button
    or tray menu) counts as no target: the text goes to whatever window is
    active, unless that is Ucho itself or the desktop/taskbar. Otherwise the
    text is pasted only into the target, which gets one attempt to come back
    to the foreground.
    """
    current = foreground()
    if not target or is_ours(target) or current == target:
        if is_ours(current):
            return OWN_WINDOW
        if not current or is_shell(current):
            return NO_WINDOW
        return PASTE
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
    """Inserts text into the target window; returns PASTE, OWN_WINDOW, one of CLIPBOARD_ONLY or INPUT_BLOCKED.

    With anything but PASTE the text is only on the clipboard, and it stays
    there for the user to paste. `delivered(result)` is called as soon as the
    outcome is known, before the previous clipboard comes back.
    """
    def report(where: str) -> str:
        if delivered is not None:
            delivered(where)
        return where

    with _insert_lock:
        wait_for_keys_released()
        where = paste_target(target, foreground_window, window_is_ours, window_exists, activate,
                             is_shell_window)
        if where != PASTE:
            if where in (FOCUS_LOST, NO_WINDOW):
                log.info("No window to paste into (%s) - the text stays on the clipboard", where)
            set_clipboard(text)
            return report(where)
        blocked = input_blocked()
        if blocked is not None:
            log.info("Keys cannot be sent (%s) - the text stays on the clipboard", blocked)
            set_clipboard(text)
            return report(blocked)
        if mode == "type":
            type_text(text)
            if enter:
                time.sleep(ENTER_DELAY_S)
                press_enter()
            return report(PASTE)
        return _paste(text, enter, report)


def _paste(text: str, enter: bool, report: Callable[[str], str]) -> str:
    """Ctrl+V with the clipboard borrowed; the previous text comes back only after a confirmed paste.

    Only text (CF_UNICODETEXT) is saved and restored. When the previous
    content had no text (an image, copied files), it cannot be put back
    without the risk of corrupting it; the clipboard is cleared instead.
    Rich formats next to the text (HTML or RTF of a copy from Word) are lost
    the same way; their plain text comes back.
    """
    previous = get_clipboard_text()
    offer = DelayedClipboard(text, window_process(foreground_window()))
    try:
        try:
            offer.offer()
        except Exception:
            # Without delayed rendering there is no way to know when the
            # target has read the text, so the previous text is not restored.
            log.warning("Delayed clipboard unavailable - the previous clipboard will not be restored",
                        exc_info=True)
            offer.close()
            set_clipboard(text)
            press_ctrl_v()
            if enter:
                time.sleep(ENTER_DELAY_S)
                press_enter()
            return report(PASTE)
        press_ctrl_v()
        if not offer.pump(PASTE_TIMEOUT_S, until=lambda: offer.rendered):
            # Not taken (yet): no Enter, nothing restored. `close` renders the
            # text, so a late paste still gets the dictation, never the previous text.
            log.info("The target did not take the text within %.0f s - it stays on the clipboard",
                     PASTE_TIMEOUT_S)
            return report(NOT_TAKEN)
        if enter:
            offer.pump(ENTER_DELAY_S)
            press_enter()
        report(PASTE)
        if not offer.confirmed:
            log.info("The text was taken by another process - the previous clipboard is not restored")
            return PASTE
        offer.pump(RESTORE_GRACE_S)
        try:
            if not offer.restore(previous):
                log.info("The clipboard changed meanwhile - the previous text is not restored")
        except Exception:
            # The text is already pasted; only the old clipboard is not back.
            log.warning("Could not restore the previous clipboard", exc_info=True)
        return PASTE
    finally:
        offer.close()


_permission_asked = False


def _ask_for_input_permission() -> None:
    """The system prompt for Accessibility, once per run (it leads to System Settings)."""
    global _permission_asked
    if _permission_asked:
        return
    _permission_asked = True
    try:
        platforms.inserter().request_permission()
    except Exception:
        log.warning("Could not ask for the input permission", exc_info=True)


_BEEP_EVENTS = {1046: "start", 659: "stop", 330: "cancel"}


def beep(frequency: int, ms: int = 70) -> None:
    """Plays a short dictation tone (duration comes from the platform's table; `ms` is kept for callers)."""
    event = _BEEP_EVENTS.get(frequency, "start")
    try:
        platforms.sounds().play(event)
    except Exception:
        log.debug("Could not play the %s sound", event, exc_info=True)


# --- microphone ------------------------------------------------------------------------


class Microphone:
    """Records into memory. Dictation lasts seconds, no file is needed."""

    def __init__(self, name: str | None, legacy_index: int | None = None):
        # The chosen microphone by name (None = the Windows default); the index
        # is a choice saved by an earlier version, see audio.find_device().
        self._name = name
        self._legacy_index = legacy_index
        self._reported_missing: str | int | None = None
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
        self._reset_take()

    def _reset_take(self) -> None:
        self._taken_blocks = 0
        self._resampler = None
        self._taken_parts: list[np.ndarray] = []
        self._taken_samples = 0
        self._take_used = False

    @property
    def taken_samples(self) -> int:
        return self._taken_samples

    def choose(self, name: str | None, legacy_index: int | None = None) -> None:
        self._name, self._legacy_index = name, legacy_index

    def prepare(self) -> None:
        """Initializes PortAudio up front; at the first recording it would cost another 0.5 s."""
        from . import audio

        if self._pa is None:
            self._pa = audio.new_pyaudio()

    def _device(self, p) -> tuple[object, bool]:
        """(device, found): `found` is False when the chosen microphone is not in `p`'s list."""
        from . import audio

        mics, default, names = audio.microphones(p)
        chosen = bool(self._name) or self._legacy_index is not None
        device = audio.find_device(self._name, mics, None, self._legacy_index, names)
        found = device is not None or not chosen
        device = device or default
        if device is None:
            raise RuntimeError(t("No microphone found."))
        return device, found

    def _close_pa(self) -> None:
        from . import audio

        audio.close_pyaudio(self._pa)
        self._pa = None

    def start(self) -> None:
        from . import audio

        # PortAudio lists the devices (and the Windows default) present when it
        # starts, so every dictation gets a fresh one: it then sees a headset
        # plugged in since and a default changed in Windows. Only the first start
        # in the process is slow (about 0.25 s, the DLL loads; prepare() pays it
        # up front), later ones take about 15 ms.
        self._close_pa()
        for attempt in range(2):
            try:
                if self._pa is None:
                    self._pa = audio.new_pyaudio()
                device, found = self._device(self._pa)
                choice = self._name or self._legacy_index
                if found:
                    self._reported_missing = None
                elif self._reported_missing != choice:
                    self._reported_missing = choice
                    log.warning("Chosen microphone %r is not connected - using the default", choice)
                self._channels, self._rate = device.channels, device.rate
                self._blocks = []
                self._reset_take()
                self.level = 0.0
                self.ready.clear()
                self._stream = platforms.audio().open_input(
                    device, self._channels, self._rate, 1024, self._callback, host=self._pa,
                )
                self._stream.start()
                self.name = device.name
                return
            except Exception:
                self._stream = None
                self._close_pa()
                if attempt:
                    raise
                log.warning("Opening the microphone failed - retrying", exc_info=True)

    def _callback(self, in_data: bytes) -> None:
        self._blocks.append(in_data)
        self.ready.set()
        try:
            self.level = float(np.abs(np.frombuffer(in_data, dtype=np.int16)).max()) / 32768.0
        except ValueError:
            pass

    def _blocks_to_mono(self, blocks: list[bytes]) -> np.ndarray:
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        data = b"".join(blocks)
        if not data:
            return np.zeros(0, dtype=np.float32)
        signal = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if self._channels > 1:
            n = len(signal) // self._channels * self._channels
            signal = signal[:n].reshape(-1, self._channels).mean(axis=1)
        return np.ascontiguousarray(signal, dtype=np.float32)

    def take(self) -> np.ndarray:
        """Returns 16 kHz mono float32 audio recorded since the previous take()."""
        self._take_used = True
        end = len(self._blocks)
        if end <= self._taken_blocks:
            return np.zeros(0, dtype=np.float32)
        new_blocks = self._blocks[self._taken_blocks:end]
        self._taken_blocks = end
        signal = self._blocks_to_mono(new_blocks)
        if self._rate != SAMPLE_RATE and signal.size:
            if self._resampler is None:
                import soxr

                self._resampler = soxr.ResampleStream(
                    self._rate, SAMPLE_RATE, 1, dtype="float32", quality="HQ")
            signal = np.ascontiguousarray(
                self._resampler.resample_chunk(signal, last=False), dtype=np.float32)
        if signal.size:
            self._taken_parts.append(signal)
            self._taken_samples += signal.size
        return signal

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            for method in (stream.stop, stream.close):
                try:
                    method()
                except Exception:
                    pass
        if not self._take_used:
            data, self._blocks = b"".join(self._blocks), []
            signal = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if self._channels > 1:
                n = len(signal) // self._channels * self._channels
                signal = signal[:n].reshape(-1, self._channels).mean(axis=1)
            if self._rate != SAMPLE_RATE and signal.size:
                import soxr

                signal = soxr.resample(signal, self._rate, SAMPLE_RATE, quality="HQ")
            self._reset_take()
            return np.ascontiguousarray(signal, dtype=np.float32)

        rest_blocks = self._blocks[self._taken_blocks:]
        self._blocks = []
        rest = self._blocks_to_mono(rest_blocks)
        if self._rate != SAMPLE_RATE and (rest.size or self._resampler is not None):
            if self._resampler is None:
                import soxr

                self._resampler = soxr.ResampleStream(
                    self._rate, SAMPLE_RATE, 1, dtype="float32", quality="HQ")
            rest = np.ascontiguousarray(
                self._resampler.resample_chunk(rest, last=True), dtype=np.float32)
        parts = [*self._taken_parts, rest]
        self._reset_take()
        non_empty = [p for p in parts if p.size]
        if not non_empty:
            return np.zeros(0, dtype=np.float32)
        return np.ascontiguousarray(np.concatenate(non_empty), dtype=np.float32)

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def close(self) -> None:
        if self._stream is not None:
            self.stop()
        self._close_pa()


# --- controller ---------------------------------------------------------------------------


class DictationController(Emitter):
    """State of dictation for the Dictation screen, the small window and the tray."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.microphone = Microphone(app.settings["microphone_name"], app.settings["microphone_index"])
        self._downloaded: dict[str, tuple[bool, float]] = {}
        self._reported_missing: str | None = None
        self.recording = False
        self.finishing = False
        self._finish_token = 0
        self.recording_start = 0.0
        self.queued = 0
        self.model_state = "idle"  # idle / loading / ready / error
        self.model_error = ""
        self.model_device = ""
        self.last_text = ""
        self.live_text = ""  # text recognized so far in the current live dictation
        self.last_seconds = 0.0
        self.target_window: int | None = None  # foreground window when the dictation started
        self._live: live.LiveDictation | None = None
        self._shown: live.LiveDictation | None = None
        self._message: tuple[str, str] | None = None
        self._message_until = 0.0

    def show_message(self, text: str, style: str = "ready", seconds: float = 3.0) -> None:
        self._message = (text, style)
        self._message_until = time.monotonic() + seconds

    @property
    def settings(self) -> config.Settings:
        return self.app.settings

    def model_key(self) -> tuple[str, str]:
        """(model, device) dictation uses; another downloaded speech model when its own is missing.

        With the app's model check (#28) the fallback is any speech model on the
        disk, preferring the transcription model; without it (tests), the
        transcription model.
        """
        model = self.settings.dictation_model()
        picked = (downloads.speech_model_for(self.app, model)
                  if callable(getattr(self.app, "speech_model_for", None)) else None)
        if picked is not None:
            if picked != model and self._reported_missing != model:
                self._reported_missing = model
                log.warning("Dictation model %s is not ready - using %s", model, picked)
            return picked, self.settings["device"]
        if model != self.settings["model"] and not self._model_downloaded(model):
            if self._reported_missing != model:
                self._reported_missing = model
                log.warning("Dictation model %s is not downloaded - using %s", model,
                            self.settings["model"])
            model = self.settings["model"]
        return model, self.settings["device"]

    def _model_downloaded(self, model: str) -> bool:
        """transcribe.model_downloaded, checked on disk at most every MODEL_CHECK_S.

        The Dictation screen asks for model_key() on every refresh (many times a second).
        """
        now = time.monotonic()
        cached = self._downloaded.get(model)
        if cached is None or now - cached[1] > MODEL_CHECK_S:
            cached = (transcribe.model_downloaded(model), now)
            self._downloaded[model] = cached
        return cached[0]

    def _engine_call(self, name: str) -> None:
        fn = getattr(self.app.engine, name, None)
        if fn is None:
            return
        try:
            fn()
        except Exception:
            log.exception("Engine %s failed", name)

    def _waiting_text(self) -> str | None:
        fn = getattr(self.app.engine, "waiting_behind", None)
        if fn is None:
            return None
        try:
            name = fn()
        except Exception:
            log.debug("waiting_behind failed", exc_info=True)
            return None
        if name is None:
            return None
        if name in ("transcribe", "live-chunk"):
            return t("Waiting for the transcription…")
        if name == "summary":
            return t("Waiting for the summary…")
        return t("Waiting for the model…")

    def status(self) -> tuple[str, str, str]:
        """(headline, pill style, hint) for the UI."""
        hotkey = self.app.dictation_hotkey_label()
        hint = t("{shortcut} — speak, press again — insert text", shortcut=hotkey)
        error = self.app.hotkey_errors.get("dictate")
        if self.recording:
            elapsed = time.monotonic() - self.recording_start
            waiting = self._waiting_text()
            return (t("Recording  {time}", time=f"{int(elapsed) // 60}:{int(elapsed) % 60:02d}"),
                    "recording", waiting or t("{shortcut} finishes, Esc cancels", shortcut=hotkey))
        if self.queued:
            return self._waiting_text() or t("Recognizing…"), "working", hint
        if self._message and time.monotonic() < self._message_until:
            return self._message[0], self._message[1], hint
        blocked = downloads.recording_blocked(self.app, check=False)
        if blocked:
            return t("Speech model not ready"), "warning", blocked
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
        # Settings may have changed (or a model was downloaded): look at the disk again.
        self._downloaded.clear()
        self._reported_missing = None
        key = self.model_key()
        preload = bool(self.settings["dictation_preload"])
        if not transcribe.model_downloaded(key[0]):
            self.model_state, self.model_error = "error", t("Model {model} is not downloaded.",
                                                             model=platform_text.model_tail(key[0]))
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
        blocked = downloads.recording_blocked(self.app)
        if blocked:
            # No speech model on the disk: nothing could recognize the dictation (#28).
            self.app.refuse_recording(blocked)
            self._emit("state")
            return False
        # Before anything of ours can take the focus: this is where the text goes.
        try:
            self.target_window = dictation_target()
        except Exception:
            self.target_window = None
        self.microphone.choose(self.settings["microphone_name"], self.settings["microphone_index"])
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
        self._engine_call("pause_for_dictation")
        self.recording_start = time.monotonic()
        self.live_text = ""
        self._live = self._shown = None
        if self.settings["dictation_live"]:
            try:
                self._start_live()
            except Exception:
                log.exception("Live dictation could not start - recognizing after the stop")
                self._live = self._shown = None
        self.app.hotkeys.escape(True)
        if self.settings["dictation_sounds"]:
            ready = self.microphone.ready
            threading.Thread(target=lambda: ready.wait(2.0) and beep(1046), daemon=True).start()
        self._emit("state")
        return True

    def _start_live(self) -> None:
        model, device = self.model_key()
        session = live.LiveDictation(
            self.app.engine, model, device, self.settings["dictation_language"],
            self.settings["dictation_dictionary"],
            on_text=lambda text: self.app.post(lambda: self._on_live_text(session, text)))
        # `_live` is fed until the stop; `_shown` keeps showing its text until
        # the result arrives (its last pieces may finish after the stop).
        self._live = self._shown = session
        self._schedule_live_tick(session)

    def _on_live_text(self, session: live.LiveDictation, text: str) -> None:
        if session is self._shown and (self.recording or self.queued):
            self.live_text = text

    def _schedule_live_tick(self, session: live.LiveDictation) -> None:
        # Bound to its session: a tick still pending from a dictation that just
        # ended must not start a second polling loop for the next one.
        self.app.after(LIVE_TICK_MS, lambda: self._live_tick(session))

    def _live_tick(self, session: live.LiveDictation) -> None:
        """Feeds the audio recorded since the last tick to the live session (UI thread)."""
        if not self.recording or session is not self._live:
            return
        try:
            session.feed(self.microphone.take())
        except Exception:
            # This dictation then finishes the old way, from the whole recording.
            log.exception("Live dictation failed - falling back to full recognition")
            session.cancel()
            self._live = None
            self.live_text = ""
            return
        self._schedule_live_tick(session)

    def stop(self) -> None:
        if not self.recording or self.finishing:
            return
        self.finishing = True
        token = self._finish_token
        self.app.after(int(TAIL_S * 1000), lambda: self._finish(token))

    def _finish(self, token: int | None = None) -> None:
        # A cancel (Esc) inside the tail window invalidates the scheduled finish.
        if token is not None and token != self._finish_token:
            return
        taken = self.microphone.taken_samples
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

            self._insert_thread = threading.Thread(target=insert, name="insert-text", daemon=True)
            self._insert_thread.start()

        def failed(error):
            self.app.post(lambda: self._failed(str(error)))

        session, self._live = self._live, None
        try:
            if session is not None:
                try:
                    session.feed(recording[taken:])
                    pieces = session.submitted
                    session.finish(recording, on_done=done, on_error=failed)
                    log.info("Live dictation finished (%d piece(s) submitted)", pieces)
                except Exception:
                    log.exception("Live dictation finish failed - falling back to full recognition")
                    session.cancel()
                    self.app.engine.dictate(recording, model, device, language,
                                            self.settings["dictation_dictionary"],
                                            on_done=done, on_error=failed)
            else:
                self.app.engine.dictate(recording, model, device, language,
                                        self.settings["dictation_dictionary"],
                                        on_done=done, on_error=failed)
        finally:
            self._engine_call("resume_after_dictation")
        self._emit("state")

    def _recognized(self, text: str, seconds: float, where: str | None) -> None:
        """`where`: PASTE, OWN_WINDOW, one of CLIPBOARD_ONLY or INPUT_BLOCKED, or None when inserting failed."""
        self.queued = max(0, self.queued - 1)
        self.live_text = ""
        if text:
            self.last_text, self.last_seconds = text, seconds
            if where == NOT_TAKEN:
                # The Ctrl+V is still queued in a slow window, which may paste on its own.
                self.show_message(t("The window has not taken the text yet. If it does not "
                                    "appear, press {keys}.",
                                    keys=platforms.inserter().paste_label()), "warning", 8)
            elif where == NO_PERMISSION:
                self.show_message(t("Ucho needs the Accessibility permission to paste. Text is on "
                                    "the clipboard, press {keys}.",
                                    keys=platforms.inserter().paste_label()), "warning", 10)
                _ask_for_input_permission()
            elif where == SECURE_INPUT:
                self.show_message(t("Secure keyboard entry is on (a password field or a terminal). "
                                    "Text is on the clipboard, press {keys}.",
                                    keys=platforms.inserter().paste_label()), "warning", 8)
            elif where in CLIPBOARD_ONLY:
                # The window you dictated into is gone, did not come back or
                # did not take the text, or only the desktop was active: better
                # on the clipboard than pasted into the wrong window or nowhere.
                self.show_message(t("Text is on the clipboard, press {keys}",
                                    keys=platforms.inserter().paste_label()), "warning", 8)
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
        self.live_text = ""
        self.show_message(t("Recognition error"), "warning", 6)
        self.last_text = error[:200]
        self._emit("state")

    def cancel(self) -> None:
        if not self.recording:
            return
        if self._live is not None:
            self._live.cancel()
        self._live = self._shown = None
        self.live_text = ""
        self._finish_token += 1
        self.microphone.stop()
        self.recording = self.finishing = False
        self._engine_call("resume_after_dictation")
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
        # A paste may still be waiting for the target (up to PASTE_TIMEOUT_S): let it
        # finish, so the dictation is rendered onto the clipboard before we exit.
        thread = getattr(self, "_insert_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(PASTE_TIMEOUT_S + 1.0)
        if self.recording:
            if self._live is not None:
                self._live.cancel()
                self._live = None
            self.microphone.stop()
            self.recording = False
            self._engine_call("resume_after_dictation")
        self.microphone.close()
