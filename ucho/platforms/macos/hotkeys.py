# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS global hotkeys through Carbon `RegisterEventHotKey` (ctypes, no PyObjC).

Why Carbon and not an event tap or pynput: a hotkey needs no TCC permission
(event taps need Input Monitoring or Accessibility), and like Windows'
`RegisterHotKey` the system then hands the combination only to us. With
`kEventHotKeyExclusive` a combination already held exclusively by another
process (a second Ucho, other apps that register the same way) fails with
`eventHotKeyExistsErr`, so a busy shortcut is reported, not silently shared.
Shortcuts of macOS itself (Cmd+Space, Ctrl+Space) register without an error
(M3a spike), so `hotkeys.macos_conflict` adds a warning for known ones.

Threading: Carbon belongs to the main thread. The handler is installed on
`GetApplicationEventTarget()`, so `kEventHotKeyPressed` arrives inside Tk's
event loop on the Tk (main) thread; `action(name)` runs there. Calls from
another thread are passed to Tk with `after(0, ...)`.

Key codes: Carbon takes virtual key codes of the ANSI layout (`kVK_*`, the
physical key position), not characters; on QWERTY layouts (also Polish Pro)
the letters match.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from typing import Any

from ...hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, VK_ESCAPE, label, macos_conflict, parse
from ...i18n import t

log = logging.getLogger(__name__)

CARBON_PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"

# Carbon modifier bits (Events.h).
CMD_KEY = 1 << 8
SHIFT_KEY = 1 << 9
OPTION_KEY = 1 << 11
CONTROL_KEY = 1 << 12
HOTKEY_EXCLUSIVE = 1 << 0  # kEventHotKeyExclusive
NO_ERR = 0
EVENT_NOT_HANDLED = -9874  # eventNotHandledErr: let the next handler see it
EVENT_HOTKEY_EXISTS = -9878  # eventHotKeyExistsErr


def fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


SIGNATURE = fourcc("UCHO")
EVENT_CLASS_KEYBOARD = fourcc("keyb")
EVENT_HOTKEY_PRESSED = 5
PARAM_DIRECT_OBJECT = fourcc("----")
TYPE_HOTKEY_ID = fourcc("hkid")

KVK_ESCAPE = 0x35

_MODIFIER_MAP = ((MOD_CONTROL, CONTROL_KEY), (MOD_ALT, OPTION_KEY), (MOD_SHIFT, SHIFT_KEY),
                 (MOD_WIN, CMD_KEY))

# Windows virtual key (hotkeys._KEYS) -> macOS kVK_* (HIToolbox/Events.h).
_LETTERS = {"A": 0x00, "S": 0x01, "D": 0x02, "F": 0x03, "H": 0x04, "G": 0x05, "Z": 0x06,
            "X": 0x07, "C": 0x08, "V": 0x09, "B": 0x0B, "Q": 0x0C, "W": 0x0D, "E": 0x0E,
            "R": 0x0F, "Y": 0x10, "T": 0x11, "O": 0x1F, "U": 0x20, "I": 0x22, "P": 0x23,
            "L": 0x25, "J": 0x26, "K": 0x28, "N": 0x2D, "M": 0x2E}
_DIGITS = {1: 0x12, 2: 0x13, 3: 0x14, 4: 0x15, 5: 0x17, 6: 0x16, 7: 0x1A, 8: 0x1C, 9: 0x19,
           0: 0x1D}
_FUNCTION = {1: 0x7A, 2: 0x78, 3: 0x63, 4: 0x76, 5: 0x60, 6: 0x61, 7: 0x62, 8: 0x64, 9: 0x65,
             10: 0x6D, 11: 0x67, 12: 0x6F}
VK_TO_KVK: dict[int, int] = {
    0x20: 0x31,  # space
    0x0D: 0x24,  # enter -> return
    0x09: 0x30,  # tab
    VK_ESCAPE: KVK_ESCAPE,
    **{ord(letter): code for letter, code in _LETTERS.items()},
    **{0x30 + digit: code for digit, code in _DIGITS.items()},
    **{0x6F + number: code for number, code in _FUNCTION.items()},
}

_ids = itertools.count(1)
_ids_lock = threading.Lock()


def _next_id() -> int:
    """Hotkey ids unique in the process, so two listeners never mix up their keys."""
    with _ids_lock:
        return next(_ids)


def carbon_modifiers(modifiers: int) -> int:
    """`MOD_*` bits (Windows) -> Carbon `controlKey/optionKey/shiftKey/cmdKey` bits."""
    result = 0
    for windows, carbon in _MODIFIER_MAP:
        if modifiers & windows:
            result |= carbon
    return result


def carbon_key(vk: int) -> int | None:
    """Windows virtual key -> macOS `kVK_*` code, None for keys without one."""
    return VK_TO_KVK.get(vk)


def to_carbon(combo: str) -> tuple[int, int] | None:
    """'<ctrl>+<alt>+r' -> (kVK_ANSI_R, controlKey | optionKey); None when not understood."""
    parsed = parse(combo)
    if parsed is None:
        return None
    modifiers, vk = parsed
    key = carbon_key(vk)
    if key is None:
        return None
    return key, carbon_modifiers(modifiers)


class CarbonHotkeys:
    """Minimal ctypes binding of the Carbon hotkey calls (loaded on first use)."""

    def __init__(self) -> None:
        import ctypes

        self._ct = ctypes
        lib = ctypes.CDLL(CARBON_PATH)

        class EventHotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        class EventTypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

        self._hotkey_id, self._type_spec = EventHotKeyID, EventTypeSpec
        self._handler_type = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p)
        lib.GetApplicationEventTarget.argtypes = []
        lib.GetApplicationEventTarget.restype = ctypes.c_void_p
        lib.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID,
                                            ctypes.c_void_p, ctypes.c_uint32,
                                            ctypes.POINTER(ctypes.c_void_p)]
        lib.RegisterEventHotKey.restype = ctypes.c_int32
        lib.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        lib.UnregisterEventHotKey.restype = ctypes.c_int32
        # ItemCount and ByteCount are `unsigned long` (64-bit on arm64).
        lib.InstallEventHandler.argtypes = [ctypes.c_void_p, self._handler_type, ctypes.c_size_t,
                                            ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
        lib.InstallEventHandler.restype = ctypes.c_int32
        lib.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        lib.RemoveEventHandler.restype = ctypes.c_int32
        lib.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                          ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                          ctypes.c_void_p]
        lib.GetEventParameter.restype = ctypes.c_int32
        self._lib = lib
        self._target = lib.GetApplicationEventTarget()

    def install(self, on_hotkey: Callable[[int, int], int]) -> tuple[int, Any]:
        """Installs a kEventHotKeyPressed handler; `on_hotkey(signature, id)` returns an OSStatus.

        Returns (status, token); the token keeps the C callback alive and goes to `remove`.
        """
        ct = self._ct

        def handler(_call_ref, event, _user_data):
            hotkey = self._hotkey_id()
            status = self._lib.GetEventParameter(event, PARAM_DIRECT_OBJECT, TYPE_HOTKEY_ID, None,
                                                 ct.sizeof(hotkey), None, ct.byref(hotkey))
            if status != NO_ERR:
                return EVENT_NOT_HANDLED
            return on_hotkey(int(hotkey.signature), int(hotkey.id))

        callback = self._handler_type(handler)
        spec = self._type_spec(EVENT_CLASS_KEYBOARD, EVENT_HOTKEY_PRESSED)
        ref = ct.c_void_p()
        status = self._lib.InstallEventHandler(self._target, callback, 1, ct.byref(spec), None,
                                               ct.byref(ref))
        return status, (callback, ref)

    def remove(self, token: Any) -> int:
        _callback, ref = token
        return self._lib.RemoveEventHandler(ref)

    def register(self, key: int, modifiers: int, number: int, options: int) -> tuple[int, Any]:
        ref = self._ct.c_void_p()
        status = self._lib.RegisterEventHotKey(key, modifiers, self._hotkey_id(SIGNATURE, number),
                                               self._target, options, self._ct.byref(ref))
        return status, ref

    def unregister(self, ref: Any) -> int:
        return self._lib.UnregisterEventHotKey(ref)


def _on_main_thread(fn: Callable[[], None]) -> None:
    """Runs `fn` now on the main thread, else through Tk's `after` (Carbon is main-thread only)."""
    if threading.current_thread() is threading.main_thread():
        fn()
        return
    import tkinter

    root = getattr(tkinter, "_default_root", None)
    if root is None:
        log.warning("Hotkey change off the main thread without a Tk window - skipped")
        return
    root.after(0, fn)


class MacHotkeys:
    """macOS `GlobalHotkeys`: `action(name)` runs on the Tk (main) thread."""

    ESCAPE = "escape"

    def __init__(self, action: Callable[[str], None],
                 carbon: CarbonHotkeys | None = None,
                 on_main: Callable[[Callable[[], None]], None] | None = None) -> None:
        self._action = action
        self._carbon = carbon
        self._on_main = on_main or _on_main_thread
        self._handler: Any = None
        self._refs: dict[int, Any] = {}  # hotkey id -> EventHotKeyRef
        self._names: dict[int, str] = {}  # hotkey id -> name
        self._escape_id: int | None = None
        self.errors: dict[str, str] = {}

    def start(self, bindings: dict[str, str]) -> dict[str, str]:
        """Registers {name: combo}. Returns the errors (empty when all worked).

        Off the main thread the registration runs later on Tk; the returned
        dict is filled then (the facade keeps the same object).
        """
        self.stop()
        self.errors = {}
        errors = self.errors
        self._on_main(lambda: self._register_all(dict(bindings), errors))
        return errors

    def _binding(self) -> CarbonHotkeys | None:
        if self._carbon is None:
            try:
                self._carbon = CarbonHotkeys()
            except (OSError, AttributeError):
                log.warning("Carbon hotkeys are unavailable", exc_info=True)
                return None
        return self._carbon

    def _register_all(self, bindings: dict[str, str], errors: dict[str, str]) -> None:
        carbon = self._binding()
        if carbon is None:
            message = t("Global hotkeys are not supported on this system.")
            errors.update({name: message for name in bindings})
            return
        if self._handler is None:
            status, token = carbon.install(self._dispatch)
            if status != NO_ERR:
                log.warning("Hotkey handler not installed (code %s)", status)
                message = t("Global hotkeys are not supported on this system.")
                errors.update({name: message for name in bindings})
                return
            self._handler = token
        for name, combo in bindings.items():
            converted = to_carbon(combo)
            if converted is None:
                errors[name] = t("Unrecognized shortcut {shortcut}.", shortcut=combo)
                log.warning("Unrecognized shortcut %s for %s", combo, name)
                continue
            key, modifiers = converted
            number = _next_id()
            status, ref = carbon.register(key, modifiers, number, HOTKEY_EXCLUSIVE)
            if status != NO_ERR:
                errors[name] = t("{shortcut} is taken by another program (code {code}).",
                                 shortcut=label(combo, "macos"), code=status)
                # A busy hotkey is expected (e.g. an older Ucho still running), not an error.
                log.warning("Hotkey %s for %s not registered (code %s)", combo, name, status)
                continue
            self._refs[number] = ref
            self._names[number] = name
            log.info("Hotkey %s registered for %s", combo, name)
            purpose = macos_conflict(combo)
            if purpose:
                errors[name] = t("{shortcut} is also a macOS shortcut ({purpose}) and may not work.",
                                 shortcut=label(combo, "macos"), purpose=purpose)
                log.warning("Hotkey %s for %s is also a macOS shortcut", combo, name)

    def _dispatch(self, signature: int, number: int) -> int:
        """Carbon handler body: our hotkey -> action(name); anything else -> not handled."""
        name = self._names.get(number) if signature == SIGNATURE else None
        if name is None:
            return EVENT_NOT_HANDLED
        try:
            self._action(name)
        except Exception:
            log.exception("Hotkey action %s failed", name)
        return NO_ERR

    def escape(self, enable: bool) -> None:
        """Esc on (while dictating) or off."""
        self._on_main(lambda: self._set_escape(enable))

    def _set_escape(self, enable: bool) -> None:
        carbon = self._carbon
        if carbon is None or self._handler is None:
            return
        if enable and self._escape_id is None:
            number = _next_id()
            # Not exclusive: Esc is everybody's key, we only borrow it while dictating.
            status, ref = carbon.register(KVK_ESCAPE, 0, number, 0)
            if status != NO_ERR:
                log.warning("Esc hotkey not registered (code %s)", status)
                return
            self._refs[number] = ref
            self._names[number] = self.ESCAPE
            self._escape_id = number
        elif not enable and self._escape_id is not None:
            number, self._escape_id = self._escape_id, None
            self._release(number)

    def _release(self, number: int) -> None:
        ref = self._refs.pop(number, None)
        self._names.pop(number, None)
        if ref is not None and self._carbon is not None:
            status = self._carbon.unregister(ref)
            if status != NO_ERR:
                log.debug("UnregisterEventHotKey returned %s", status)

    def stop(self) -> None:
        self._on_main(self._stop_now)

    def _stop_now(self) -> None:
        for number in list(self._refs):
            self._release(number)
        self._escape_id = None
        handler, self._handler = self._handler, None
        if handler is not None and self._carbon is not None:
            self._carbon.remove(handler)


def create(action: Callable[[str], None]) -> MacHotkeys:
    return MacHotkeys(action)
