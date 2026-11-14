# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Integracja z paskiem zadan Windows - przypinanie i ikona.

Aplikacja startuje przez `pythonw.exe`, wiec bez dodatkowych danych Windows
traktuje okno jak "jakiegos Pythona": przypiecie laduje ikone interpretera,
a klikniecie przypietego kafelka odpala pythonw bez argumentow.

Zalatwiaja to dwie rzeczy:
  * wlasny AppUserModelID (ustawiany w `app.uruchom`) - tozsamosc aplikacji,
  * wlasciwosci powloki: na oknie (`System.AppUserModel.Relaunch*`) i na
    skrotach .lnk (`System.AppUserModel.ID`), zeby przypiety kafelek wskazywal
    nasza ikone i nasze polecenie, a przypiecie sklejalo sie z dzialajacym oknem.

Wszystko idzie przez ctypes - nie ciagniemy pywin32 dla kilku wywolan COM.
Kazda funkcja jest "best effort": na bledzie zwraca False i tylko loguje.
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
    # vt + 3 pola rezerwowe + unia. Uzywamy tylko wariantu tekstowego (VT_LPWSTR),
    # wiec unie opisujemy jako wskaznik na napis plus dopelnienie do 16 bajtow.
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("pwszVal", c_void_p),
        ("dopelnienie", c_void_p),
    ]


def _guid(tekst: str) -> _GUID:
    wynik = _GUID()
    ctypes.windll.ole32.CLSIDFromString(tekst, byref(wynik))
    return wynik


# Grupa wlasciwosci AppUserModel - identyfikatory z propkey.h.
_FMTID_AUM = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
_PID_RELAUNCH_COMMAND = 2
_PID_RELAUNCH_ICON = 3
_PID_RELAUNCH_NAZWA = 4
_PID_ID = 5

_IID_PROPERTY_STORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
_IID_PERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"

# Indeksy metod w tablicach wirtualnych (po trzech metodach IUnknown).
_IDX_SET_VALUE = 6      # IPropertyStore::SetValue
_IDX_COMMIT = 7         # IPropertyStore::Commit
_IDX_QUERY_INTERFACE = 0
_IDX_RELEASE = 2
_IDX_PF_LOAD = 5        # IPersistFile::Load
_IDX_PF_SAVE = 6        # IPersistFile::Save


def _metoda(wskaznik: c_void_p, indeks: int, *typy_arg):
    """Zwraca wywolywalna metode COM spod danego indeksu tablicy wirtualnej."""
    vtbl = ctypes.cast(wskaznik, POINTER(POINTER(c_void_p)))[0]
    prototyp = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *typy_arg)
    return prototyp(vtbl[indeks])


def _zwolnij(wskaznik: c_void_p) -> None:
    try:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(
            ctypes.cast(wskaznik, POINTER(POINTER(c_void_p)))[0][_IDX_RELEASE]
        )(wskaznik)
    except Exception:
        pass


def _propvariant_tekst(wartosc: str) -> _PROPVARIANT:
    """PROPVARIANT typu VT_LPWSTR.

    `InitPropVariantFromString` jest funkcja inline z naglowka, a nie eksportem
    propsys.dll - napis alokujemy sami. Pamiec zwalnia pozniej PropVariantClear,
    dlatego musi pochodzic z CoTaskMemAlloc.
    """
    ctypes.windll.ole32.CoTaskMemAlloc.restype = c_void_p
    bajty = (len(wartosc) + 1) * ctypes.sizeof(ctypes.c_wchar)
    bufor = ctypes.windll.ole32.CoTaskMemAlloc(bajty)
    if not bufor:
        raise MemoryError("CoTaskMemAlloc")
    ctypes.memmove(bufor, ctypes.create_unicode_buffer(wartosc), bajty)
    wariant = _PROPVARIANT()
    wariant.vt = _VT_LPWSTR
    wariant.pwszVal = bufor
    return wariant


def _ustaw_tekst(magazyn: c_void_p, pid: int, wartosc: str) -> None:
    klucz = _PROPERTYKEY(_guid(_FMTID_AUM), pid)
    wariant = _propvariant_tekst(wartosc)
    try:
        _metoda(magazyn, _IDX_SET_VALUE, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT))(
            magazyn, byref(klucz), byref(wariant)
        )
    finally:
        ctypes.windll.ole32.PropVariantClear(byref(wariant))


def _com_start() -> None:
    # RPC_E_CHANGED_MODE oznacza, ze COM juz dziala w innym trybie - to nam nie
    # przeszkadza, korzystamy z istniejacej inicjalizacji watku.
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except OSError:
        pass


def opisz_okno(hwnd: int, polecenie: str, nazwa: str, ikona: str,
               id_aplikacji: str | None = None) -> bool:
    """Nadaje oknu dane do przypiecia: polecenie startowe, nazwe i ikone.

    Dzieki temu "Przypnij do paska zadan" na dzialajacym oknie tworzy kafelek
    naszej aplikacji, a nie skrot do pythonw.exe.
    """
    if sys.platform != "win32":
        return False
    magazyn = c_void_p()
    try:
        _com_start()
        ctypes.windll.shell32.SHGetPropertyStoreForWindow(
            wintypes.HWND(hwnd), byref(_guid(_IID_PROPERTY_STORE)), byref(magazyn)
        )
        if not magazyn:
            return False
        if id_aplikacji:
            _ustaw_tekst(magazyn, _PID_ID, id_aplikacji)
        _ustaw_tekst(magazyn, _PID_RELAUNCH_COMMAND, polecenie)
        _ustaw_tekst(magazyn, _PID_RELAUNCH_NAZWA, nazwa)
        _ustaw_tekst(magazyn, _PID_RELAUNCH_ICON, ikona)
        _metoda(magazyn, _IDX_COMMIT)(magazyn)
        return True
    except Exception:
        log.debug("Nie udalo sie opisac okna dla paska zadan", exc_info=True)
        return False
    finally:
        if magazyn:
            _zwolnij(magazyn)


def ustaw_id_skrotu(sciezka: Path, id_aplikacji: str) -> bool:
    """Zapisuje AppUserModelID w pliku .lnk.

    Skrot z tym samym identyfikatorem co proces sklei sie z dzialajacym oknem -
    przypiety kafelek i uruchomiona aplikacja to jeden przycisk na pasku.
    """
    if sys.platform != "win32" or not sciezka.exists():
        return False
    link = c_void_p()
    plik = c_void_p()
    magazyn = c_void_p()
    try:
        _com_start()
        ctypes.windll.ole32.CoCreateInstance(
            byref(_guid(_CLSID_SHELL_LINK)), None, _CLSCTX_INPROC_SERVER,
            byref(_guid(_IID_PERSIST_FILE)), byref(plik),
        )
        if not plik:
            return False
        _metoda(plik, _IDX_PF_LOAD, wintypes.LPCWSTR, wintypes.DWORD)(
            plik, str(sciezka), _STGM_READWRITE
        )
        _metoda(plik, _IDX_QUERY_INTERFACE, POINTER(_GUID), POINTER(c_void_p))(
            plik, byref(_guid(_IID_PROPERTY_STORE)), byref(magazyn)
        )
        if not magazyn:
            return False
        _ustaw_tekst(magazyn, _PID_ID, id_aplikacji)
        _metoda(magazyn, _IDX_COMMIT)(magazyn)
        # Zapis musi isc przez IPersistFile - sam Commit zostaje w pamieci.
        _metoda(plik, _IDX_PF_SAVE, wintypes.LPCWSTR, wintypes.BOOL)(plik, None, True)
        return True
    except Exception:
        log.debug("Nie udalo sie ustawic ID skrotu %s", sciezka, exc_info=True)
        return False
    finally:
        for wskaznik in (magazyn, plik, link):
            if wskaznik:
                _zwolnij(wskaznik)


def sciezki_skrotow() -> list[Path]:
    """Skroty tworzone przez instalator: pulpit i Menu Start."""
    import os

    wynik: list[Path] = []
    pulpit = os.environ.get("USERPROFILE")
    if pulpit:
        wynik.append(Path(pulpit) / "Desktop" / "Ucho.lnk")
    appdata = os.environ.get("APPDATA")
    if appdata:
        wynik.append(
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Ucho.lnk"
        )
    return wynik
