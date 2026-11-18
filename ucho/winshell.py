# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Windows taskbar integration: pinning, icon, Jump List, progress, autostart.

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
        # The relaunch properties go first and the id last: the shell reads
        # the relaunch data when the id changes.
        _set_text(store, _PID_RELAUNCH_COMMAND, command)
        _set_text(store, _PID_RELAUNCH_NAME, name)
        _set_text(store, _PID_RELAUNCH_ICON, icon)
        if app_id:
            _set_text(store, _PID_ID, app_id)
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


def shortcut_arguments(path: Path) -> str | None:
    """Command-line arguments stored in a .lnk file (None when it cannot be read)."""
    if sys.platform != "win32" or not path.exists():
        return None
    link = persist_file = c_void_p()
    try:
        _com_start()
        link = _create(_CLSID_SHELL_LINK, _IID_SHELL_LINK_W)
        _method(link, _IDX_QUERY_INTERFACE, POINTER(_GUID), POINTER(c_void_p))(
            link, byref(_guid(_IID_PERSIST_FILE)), byref(persist_file))
        _method(persist_file, _IDX_PF_LOAD, wintypes.LPCWSTR, wintypes.DWORD)(
            persist_file, str(path), 0)
        buffer = ctypes.create_unicode_buffer(1024)
        _method(link, _IDX_SL_GET_ARGUMENTS, wintypes.LPWSTR, ctypes.c_int)(link, buffer, 1024)
        return buffer.value
    except Exception:
        log.debug("Could not read %s", path, exc_info=True)
        return None
    finally:
        for pointer in (persist_file, link):
            if pointer:
                _release(pointer)


SHORTCUT_NAMES = ("Ucho.lnk", "Ucho Dictate.lnk")


def shortcut_paths() -> list[Path]:
    """Shortcuts created by the installer: Desktop and Start menu, "Ucho" and "Ucho Dictate"."""
    import os

    folders: list[Path] = []
    profile = os.environ.get("USERPROFILE")
    if profile:
        folders.append(Path(profile) / "Desktop")
    appdata = os.environ.get("APPDATA")
    if appdata:
        folders.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [folder / name for folder in folders for name in SHORTCUT_NAMES]


def _set_property(store: c_void_p, fmtid: str, pid: int, value: str) -> None:
    key = _PROPERTYKEY(_guid(fmtid), pid)
    variant = _propvariant_text(value)
    try:
        _method(store, _IDX_SET_VALUE, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT))(
            store, byref(key), byref(variant)
        )
    finally:
        ctypes.windll.ole32.PropVariantClear(byref(variant))


# --- Jump List -----------------------------------------------------------------

_CLSID_DESTINATION_LIST = "{77F10CF0-3DB5-4966-B520-B7C54FD35ED6}"
_IID_CUSTOM_DESTINATION_LIST = "{6332DEBF-87B5-4670-90C0-5E57B408A49E}"
_CLSID_ENUMERABLE_OBJECT_COLLECTION = "{2D3468C1-36A7-43B6-AC24-D3F02FD9607A}"
_IID_OBJECT_COLLECTION = "{5632B1A4-E38A-400A-928A-D4CD63230295}"
_IID_OBJECT_ARRAY = "{92CA9DCD-5622-4BBA-A805-5E9F541BD8C9}"
_IID_SHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_FMTID_TITLE = "{F29F85E0-4FF9-1068-AB91-08002B27B3D9}"  # PKEY_Title
_PID_TITLE = 2

# ICustomDestinationList (after IUnknown).
_IDX_DL_SET_APP_ID = 3
_IDX_DL_BEGIN_LIST = 4
_IDX_DL_ADD_USER_TASKS = 7
_IDX_DL_COMMIT_LIST = 8
_IDX_DL_DELETE_LIST = 10
# IObjectCollection.
_IDX_OC_ADD_OBJECT = 5
# IShellLinkW.
_IDX_SL_SET_DESCRIPTION = 7
_IDX_SL_GET_ARGUMENTS = 10
_IDX_SL_SET_WORKING_DIRECTORY = 9
_IDX_SL_SET_ARGUMENTS = 11
_IDX_SL_SET_ICON_LOCATION = 17
_IDX_SL_SET_PATH = 20


def _create(clsid: str, iid: str) -> c_void_p:
    pointer = c_void_p()
    ctypes.windll.ole32.CoCreateInstance(
        byref(_guid(clsid)), None, _CLSCTX_INPROC_SERVER, byref(_guid(iid)), byref(pointer)
    )
    if not pointer:
        raise OSError(f"CoCreateInstance {clsid} failed")
    return pointer


def _task_link(title: str, program: str, arguments: str, icon: str, description: str,
               working_dir: str) -> c_void_p:
    """IShellLinkW with a title (PKEY_Title), as the Jump List needs it."""
    link = _create(_CLSID_SHELL_LINK, _IID_SHELL_LINK_W)
    store = c_void_p()
    try:
        _method(link, _IDX_SL_SET_PATH, wintypes.LPCWSTR)(link, program)
        _method(link, _IDX_SL_SET_ARGUMENTS, wintypes.LPCWSTR)(link, arguments)
        _method(link, _IDX_SL_SET_WORKING_DIRECTORY, wintypes.LPCWSTR)(link, working_dir)
        _method(link, _IDX_SL_SET_DESCRIPTION, wintypes.LPCWSTR)(link, description)
        if icon:
            path, _, index = icon.rpartition(",")
            _method(link, _IDX_SL_SET_ICON_LOCATION, wintypes.LPCWSTR, ctypes.c_int)(
                link, path or icon, int(index) if index.lstrip("-").isdigit() else 0)
        _method(link, _IDX_QUERY_INTERFACE, POINTER(_GUID), POINTER(c_void_p))(
            link, byref(_guid(_IID_PROPERTY_STORE)), byref(store))
        _set_property(store, _FMTID_TITLE, _PID_TITLE, title)
        _method(store, _IDX_COMMIT)(store)
        return link
    except Exception:
        _release(link)
        raise
    finally:
        if store:
            _release(store)


def set_jump_list(app_id: str, tasks: list[dict]) -> bool:
    """Tasks in the taskbar Jump List (right click on the Ucho button).

    `tasks`: dicts with title, program, arguments, icon ("path,index"),
    description and working_dir. Windows keeps the list per AppUserModelID,
    so it stays after a restart, also for the pinned tile.
    """
    if sys.platform != "win32":
        return False
    destinations = collection = c_void_p()
    links: list[c_void_p] = []
    try:
        _com_start()
        destinations = _create(_CLSID_DESTINATION_LIST, _IID_CUSTOM_DESTINATION_LIST)
        _method(destinations, _IDX_DL_SET_APP_ID, wintypes.LPCWSTR)(destinations, app_id)
        slots = wintypes.UINT(0)
        removed = c_void_p()
        _method(destinations, _IDX_DL_BEGIN_LIST, POINTER(wintypes.UINT), POINTER(_GUID),
                POINTER(c_void_p))(destinations, byref(slots), byref(_guid(_IID_OBJECT_ARRAY)),
                                   byref(removed))
        if removed:
            _release(removed)
        collection = _create(_CLSID_ENUMERABLE_OBJECT_COLLECTION, _IID_OBJECT_COLLECTION)
        for task in tasks:
            link = _task_link(task["title"], task["program"], task.get("arguments", ""),
                              task.get("icon", ""), task.get("description", task["title"]),
                              task.get("working_dir", ""))
            links.append(link)
            _method(collection, _IDX_OC_ADD_OBJECT, c_void_p)(collection, link)
        # IObjectCollection derives from IObjectArray, so it can be passed as is.
        _method(destinations, _IDX_DL_ADD_USER_TASKS, c_void_p)(destinations, collection)
        _method(destinations, _IDX_DL_COMMIT_LIST)(destinations)
        return True
    except Exception:
        log.debug("Could not set the Jump List", exc_info=True)
        return False
    finally:
        for pointer in links + [collection, destinations]:
            if pointer:
                _release(pointer)


# --- start with Windows ---------------------------------------------------------

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "Ucho"


def autostart_command() -> str | None:
    """The command Windows runs at sign-in for Ucho, None when not registered."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, _RUN_VALUE)[0])
    except OSError:
        return None


def set_autostart(enabled: bool, command: str) -> bool:
    """Adds or removes Ucho in HKCU\\...\\Run (per user, no admin rights)."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        log.warning("Could not change the autostart entry", exc_info=True)
        return False


# --- taskbar button progress ------------------------------------------------------


class TaskbarProgress:
    """Progress or state on the taskbar button (ITaskbarList3).

    Red (ERROR) is used as "recording", a moving bar as "working".
    """

    NONE = 0x0
    INDETERMINATE = 0x1
    NORMAL = 0x2
    ERROR = 0x4

    _CLSID = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
    _IID = "{ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf}"

    def __init__(self, hwnd: int):
        self._hwnd = hwnd
        self._obj = c_void_p()
        self._last: tuple | None = None
        if sys.platform != "win32":
            return
        try:
            _com_start()
            self._obj = _create(self._CLSID, self._IID)
            _method(self._obj, 3)(self._obj)  # HrInit
        except Exception:
            log.debug("ITaskbarList3 not available", exc_info=True)
            self._obj = c_void_p()

    def set(self, state: int, value: int | None = None) -> None:
        if not self._obj or (state, value) == self._last:
            return
        self._last = (state, value)
        try:
            if value is not None:
                _method(self._obj, 9, wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong)(
                    self._obj, self._hwnd, value, 100)
            _method(self._obj, 10, wintypes.HWND, ctypes.c_int)(self._obj, self._hwnd, state)
        except Exception:
            log.debug("Could not set the taskbar progress", exc_info=True)
