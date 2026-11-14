# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Windows taskbar integration - pinning and the icon.

The app starts through `pythonw.exe`, so without extra data Windows treats the
window as "some Python": pinning picks up the interpreter icon, and clicking
the pinned tile starts pythonw with no arguments.

Two things fix that:
  * our own AppUserModelID (set in `app.run`) - the identity of the app,
  * shell properties: on the window (`System.AppUserModel.Relaunch*`) and on
    the .lnk shortcuts (`System.AppUserModel.ID`), so the pinned tile points to
    our icon and our command, and pinning merges with the running window.

Everything goes through ctypes - we do not pull in pywin32 for a few COM calls.
Every function is "best effort": on error it returns False and only logs.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import POINTER, byref, c_void_p, wintypes
from pathlib import Path

log = logging.getLogger(__name__)

_CLSCTX_INPROC_SERVER = 0x1
_STGM_READWRITE = 0x00000002
_VT_LPWSTR = 31


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    # vt + 3 reserved fields + a union. We only use the string variant
    # (VT_LPWSTR), so the union is described as a string pointer plus padding
    # up to 16 bytes.
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("pwszVal", c_void_p),
        ("padding", c_void_p),
    ]


def _guid(text: str) -> _GUID:
    result = _GUID()
    ctypes.windll.ole32.CLSIDFromString(text, byref(result))
    return result


# AppUserModel property group - identifiers from propkey.h.
_FMTID_AUM = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
_PID_RELAUNCH_COMMAND = 2
_PID_RELAUNCH_ICON = 3
_PID_RELAUNCH_NAME = 4
_PID_ID = 5

_IID_PROPERTY_STORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
_IID_PERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"

# Method indices in the virtual tables (after the three IUnknown methods).
_IDX_SET_VALUE = 6      # IPropertyStore::SetValue
_IDX_COMMIT = 7         # IPropertyStore::Commit
_IDX_QUERY_INTERFACE = 0
_IDX_RELEASE = 2
_IDX_PF_LOAD = 5        # IPersistFile::Load
_IDX_PF_SAVE = 6        # IPersistFile::Save


def _method(pointer: c_void_p, index: int, *arg_types):
    """Returns a callable COM method at the given virtual table index."""
    vtbl = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *arg_types)
    return prototype(vtbl[index])


def _release(pointer: c_void_p) -> None:
    try:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(
            ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0][_IDX_RELEASE]
        )(pointer)
    except Exception:
        pass


def _propvariant_text(value: str) -> _PROPVARIANT:
    """A PROPVARIANT of type VT_LPWSTR.

    `InitPropVariantFromString` is an inline function from a header, not a
    propsys.dll export, so we allocate the string ourselves. PropVariantClear
    frees it later, so it must come from CoTaskMemAlloc.
    """
    ctypes.windll.ole32.CoTaskMemAlloc.restype = c_void_p
    size = (len(value) + 1) * ctypes.sizeof(ctypes.c_wchar)
    buffer = ctypes.windll.ole32.CoTaskMemAlloc(size)
    if not buffer:
        raise MemoryError("CoTaskMemAlloc")
    ctypes.memmove(buffer, ctypes.create_unicode_buffer(value), size)
    variant = _PROPVARIANT()
    variant.vt = _VT_LPWSTR
    variant.pwszVal = buffer
    return variant


def _set_text(store: c_void_p, pid: int, value: str) -> None:
    key = _PROPERTYKEY(_guid(_FMTID_AUM), pid)
    variant = _propvariant_text(value)
    try:
        _method(store, _IDX_SET_VALUE, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT))(
            store, byref(key), byref(variant)
        )
    finally:
        ctypes.windll.ole32.PropVariantClear(byref(variant))


def _com_start() -> None:
    # RPC_E_CHANGED_MODE means COM already runs in another mode - that is fine,
    # we use the thread's existing initialization.
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except OSError:
        pass


def describe_window(hwnd: int, command: str, name: str, icon: str,
                    app_id: str | None = None) -> bool:
    """Gives the window its pinning data: start command, name and icon.

    This way "Pin to taskbar" on the running window creates a tile for our
    app instead of a shortcut to pythonw.exe.
    """
    if sys.platform != "win32":
        return False
    store = c_void_p()
    try:
        _com_start()
        ctypes.windll.shell32.SHGetPropertyStoreForWindow(
            wintypes.HWND(hwnd), byref(_guid(_IID_PROPERTY_STORE)), byref(store)
        )
        if not store:
            return False
        if app_id:
            _set_text(store, _PID_ID, app_id)
        _set_text(store, _PID_RELAUNCH_COMMAND, command)
        _set_text(store, _PID_RELAUNCH_NAME, name)
        _set_text(store, _PID_RELAUNCH_ICON, icon)
        _method(store, _IDX_COMMIT)(store)
        return True
    except Exception:
        log.debug("Could not describe the window for the taskbar", exc_info=True)
        return False
    finally:
        if store:
            _release(store)


def set_shortcut_app_id(path: Path, app_id: str) -> bool:
    """Writes the AppUserModelID into a .lnk file.

    A shortcut with the same id as the process merges with the running window:
    the pinned tile and the running app are one taskbar button.
    """
    if sys.platform != "win32" or not path.exists():
        return False
    link = c_void_p()
    persist_file = c_void_p()
    store = c_void_p()
    try:
        _com_start()
        ctypes.windll.ole32.CoCreateInstance(
            byref(_guid(_CLSID_SHELL_LINK)), None, _CLSCTX_INPROC_SERVER,
            byref(_guid(_IID_PERSIST_FILE)), byref(persist_file),
        )
        if not persist_file:
            return False
        _method(persist_file, _IDX_PF_LOAD, wintypes.LPCWSTR, wintypes.DWORD)(
            persist_file, str(path), _STGM_READWRITE
        )
        _method(persist_file, _IDX_QUERY_INTERFACE, POINTER(_GUID), POINTER(c_void_p))(
            persist_file, byref(_guid(_IID_PROPERTY_STORE)), byref(store)
        )
        if not store:
            return False
        _set_text(store, _PID_ID, app_id)
        _method(store, _IDX_COMMIT)(store)
        # The save must go through IPersistFile - Commit alone stays in memory.
        _method(persist_file, _IDX_PF_SAVE, wintypes.LPCWSTR, wintypes.BOOL)(
            persist_file, None, True)
        return True
    except Exception:
        log.debug("Could not set the shortcut id for %s", path, exc_info=True)
        return False
    finally:
        for pointer in (store, persist_file, link):
            if pointer:
                _release(pointer)


def shortcut_paths() -> list[Path]:
    """Shortcuts created by the installer: Desktop and Start menu."""
    import os

    result: list[Path] = []
    profile = os.environ.get("USERPROFILE")
    if profile:
        result.append(Path(profile) / "Desktop" / "Ucho.lnk")
    appdata = os.environ.get("APPDATA")
    if appdata:
        result.append(
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Ucho.lnk"
        )
    return result
