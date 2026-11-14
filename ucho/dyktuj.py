# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dyktuj: mowisz do mikrofonu, tekst laduje tam, gdzie stoi kursor.

Ctrl+Shift+Spacja zaczyna nagrywanie, drugie wcisniecie je konczy. Nagranie
idzie przez tego samego Whispera co Ucho (OpenVINO, lokalnie), a wynik
jest wklejany do aktywnego okna - terminala, przegladarki, czegokolwiek.
Esc w trakcie nagrywania anuluje.

Kilka decyzji, ktore nie sa oczywiste:
  * Skrot rejestrujemy przez `RegisterHotKey`, a nie przez pynput. pynput
    tylko podsluchuje, wiec Ctrl+Shift+Spacja trafialaby dalej do terminala
    (Windows Terminal otwiera na niej menu nowej karty). RegisterHotKey
    przechwytuje kombinacje i nikt poza nami jej nie dostaje.
  * Esc jest rejestrowany tylko na czas nagrywania - poza nim nie zabieramy
    go innym programom.
  * Model siedzi w pamieci przez caly czas dzialania aplikacji. Ladowanie to
    kilka sekund, a dyktowanie ma ruszac od razu.
  * Wszystkie operacje na modelu ida przez jeden watek roboczy - pipeline
    OpenVINO nie jest bezpieczny przy wywolaniach z kilku watkow.
  * Stan nagrywania widac na przycisku w pasku zadan: czerwony pasek
    = nagrywam, przelewajacy sie zielony = rozpoznaje.
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

# Nazwa okna sluzy tez do odnalezienia dzialajacej instancji (FindWindowW),
# wiec ustawiamy ja po wyborze jezyka, w main().
APLIKACJA = "Ucho Dyktuj"
ID_APLIKACJI = "Ucho.Dyktuj"
PLIK_USTAWIEN = config.KATALOG_PROJEKTU / "dyktuj.json"
PLIK_LOGU = config.KATALOG_PROJEKTU / "dyktuj.log"
PLIK_IKONY = Path(__file__).resolve().parent / "assets" / "dyktuj.ico"
STARTER = config.KATALOG_PROJEKTU / "dyktuj.pyw"

CZESTOTLIWOSC = 16_000
# Ludzie wciskaja skrot w chwili, gdy koncza ostatnie slowo - bez tego ogona
# koncowka wypowiedzi bywa ucieta.
OGON_S = 0.25
MIN_NAGRANIE_S = 0.3

DOMYSLNE = {
    "jezyk": "auto",        # auto (PL/EN) / pl / en
    "enter": False,         # Enter po wklejeniu, czyli od razu "wyslij"
    "tryb": "wklej",        # wklej (schowek + Ctrl+V) / pisz (symulacja klawiatury)
    "dzwieki": True,
    "maks_nagranie_s": 300,
    # Dyktowanie to krotkie nagrania, wiec stac nas na pelna precyzje FP16
    # (~3 GB, ok. 25% wolniej niz INT8). Gdy model nie jest pobrany, bierzemy
    # ten z ustawien glownej aplikacji Ucho.
    "model": "OpenVINO/whisper-large-v3-fp16-ov",
    # Nazwy, ktore Whisper uparcie przekreca. Klucz = to, co rozpoznal
    # (bez wzgledu na wielkosc liter, cale slowa), wartosc = poprawna forma.
    # Przyklad; wlasne poprawki dopisuje sie w dyktuj.json.
    "slownik": {
        "Cloud Code": "Claude Code",
        "Cloud Codem": "Claude Code'em",
    },
}

TLO = "#15171d"
PANEL = "#1e2129"
AKCENT = "#a78bfa"
CZERWIEN = "#ef4444"
TEKST = "#e6e9ef"
TEKST_PRZYGASZONY = "#8b93a5"
ZIELEN = "#34d399"


# --- ustawienia -------------------------------------------------------------


def wczytaj_ustawienia() -> dict:
    dane = dict(DOMYSLNE)
    try:
        zapisane = json.loads(PLIK_USTAWIEN.read_text(encoding="utf-8"))
        if isinstance(zapisane, dict):
            dane.update({k: v for k, v in zapisane.items() if k in DOMYSLNE})
    except (OSError, ValueError):
        pass
    return dane


def ostatni_tekst_z_logu() -> str:
    """Ostatnie niepuste dyktowanie z logu - zeby przetrwalo restart aplikacji."""
    import ast

    try:
        with open(PLIK_LOGU, "rb") as plik:
            plik.seek(0, 2)
            plik.seek(max(0, plik.tell() - 256_000))
            linie = plik.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    wzor = re.compile(r"Rozpoznano [\d.]+ s audio w [\d.]+ s: (.+)$")
    for linia in reversed(linie):
        trafienie = wzor.search(linia)
        if not trafienie:
            continue
        try:
            tekst = ast.literal_eval(trafienie.group(1))
        except (ValueError, SyntaxError):
            continue
        if isinstance(tekst, str) and tekst.strip():
            return tekst
    return ""


def zapisz_ustawienia(dane: dict) -> None:
    try:
        PLIK_USTAWIEN.write_text(json.dumps(dane, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    except OSError:
        log.warning("Nie udalo sie zapisac %s", PLIK_USTAWIEN)


# --- ikona ------------------------------------------------------------------


def rysuj_ikone(rozmiar: int = 256):
    """Mikrofon z falami dzwieku - fiolet, zeby nie mylil sie z glownym oknem Ucho."""
    from PIL import Image, ImageDraw

    obraz = Image.new("RGBA", (rozmiar, rozmiar), (0, 0, 0, 0))
    r = ImageDraw.Draw(obraz)
    s = rozmiar / 256.0
    kolor = (167, 139, 250, 255)

    def p(*xs):
        return [int(x * s) for x in xs]

    r.rounded_rectangle([0, 0, rozmiar - 1, rozmiar - 1], radius=int(56 * s),
                        fill=(24, 26, 32, 255))
    r.rounded_rectangle(p(104, 52, 152, 140), radius=int(24 * s), fill=kolor)
    r.arc(p(82, 86, 174, 172), start=0, end=180, fill=kolor, width=max(2, int(11 * s)))
    r.rounded_rectangle(p(122, 168, 134, 196), radius=int(6 * s), fill=kolor)
    r.rounded_rectangle(p(96, 190, 160, 202), radius=int(6 * s), fill=kolor)
    # Fale po bokach kapsuly.
    for promien in (62, 86):
        bbox = p(128 - promien, 96 - promien, 128 + promien, 96 + promien)
        r.arc(bbox, start=-35, end=35, fill=kolor, width=max(2, int(9 * s)))
        r.arc(bbox, start=145, end=215, fill=kolor, width=max(2, int(9 * s)))
    return obraz


def zapisz_ikone(sciezka: Path = PLIK_IKONY) -> Path:
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    rysuj_ikone(256).save(sciezka, format="ICO",
                          sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    return sciezka


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
WM_PRZELACZ_ESC = 0x8001  # WM_APP + 1
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
    # Potrzebny tylko po to, zeby unia miala wlasciwy rozmiar.
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _UNIA(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNIA)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def _klawisz(vk: int = 0, skan: int = 0, flagi: int = 0) -> _INPUT:
    zdarzenie = _INPUT(type=INPUT_KEYBOARD)
    zdarzenie.u.ki = _KEYBDINPUT(vk, skan, flagi, 0, 0)
    return zdarzenie


def _wyslij(zdarzenia: list[_INPUT]) -> None:
    tablica = (_INPUT * len(zdarzenia))(*zdarzenia)
    _user32.SendInput(len(zdarzenia), tablica, ctypes.sizeof(_INPUT))


def czekaj_na_puszczenie_klawiszy(limit_s: float = 1.5) -> None:
    """Ctrl+V przy wcisnietym jeszcze Shifcie to w wielu programach co innego."""
    koniec = time.monotonic() + limit_s
    while time.monotonic() < koniec:
        if not any(_user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_SPACE)):
            return
        time.sleep(0.02)


def ustaw_schowek(tekst: str, hwnd: int | None) -> None:
    bufor = ctypes.create_unicode_buffer(tekst)
    rozmiar = ctypes.sizeof(bufor)
    for _ in range(20):
        if _user32.OpenClipboard(hwnd or None):
            break
        time.sleep(0.05)  # schowek bywa chwilowo zajety przez inny program
    else:
        raise RuntimeError(t("Schowek jest zajęty przez inny program"))
    try:
        _user32.EmptyClipboard()
        uchwyt = _kernel32.GlobalAlloc(GMEM_MOVEABLE, rozmiar)
        if not uchwyt:
            raise MemoryError("GlobalAlloc")
        wskaznik = _kernel32.GlobalLock(uchwyt)
        ctypes.memmove(wskaznik, bufor, rozmiar)
        _kernel32.GlobalUnlock(uchwyt)
        if not _user32.SetClipboardData(CF_UNICODETEXT, uchwyt):
            _kernel32.GlobalFree(uchwyt)
            raise RuntimeError("SetClipboardData nie powiodlo sie")
    finally:
        _user32.CloseClipboard()


def wcisnij_ctrl_v() -> None:
    _wyslij([_klawisz(VK_CONTROL), _klawisz(VK_V),
             _klawisz(VK_V, flagi=KEYEVENTF_KEYUP),
             _klawisz(VK_CONTROL, flagi=KEYEVENTF_KEYUP)])


def wcisnij_enter() -> None:
    _wyslij([_klawisz(VK_RETURN), _klawisz(VK_RETURN, flagi=KEYEVENTF_KEYUP)])


def napisz(tekst: str) -> None:
    """Wpisuje tekst jako znaki Unicode - bez schowka, niezaleznie od ukladu klawiatury."""
    dane = tekst.encode("utf-16-le")
    zdarzenia: list[_INPUT] = []
    for i in range(0, len(dane), 2):
        jednostka = int.from_bytes(dane[i:i + 2], "little")
        zdarzenia.append(_klawisz(skan=jednostka, flagi=KEYEVENTF_UNICODE))
        zdarzenia.append(_klawisz(skan=jednostka, flagi=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    # Porcjami - bardzo dlugi ciag zdarzen naraz potrafi zgubic znaki w terminalu.
    for i in range(0, len(zdarzenia), 200):
        _wyslij(zdarzenia[i:i + 200])
        time.sleep(0.01)


def wstaw_tekst(tekst: str, tryb: str, enter: bool, wlasne_okno: int) -> bool:
    """Wstawia tekst do aktywnego okna. False = tylko schowek (aktywne bylo nasze okno)."""
    czekaj_na_puszczenie_klawiszy()
    aktywne = _user32.GetForegroundWindow() or 0
    if tryb != "pisz" or aktywne == wlasne_okno:
        ustaw_schowek(tekst, wlasne_okno)
    if aktywne == wlasne_okno:
        return False
    if tryb == "pisz":
        napisz(tekst)
    else:
        wcisnij_ctrl_v()
    if enter:
        # Terminal musi najpierw przetrawic wklejenie, inaczej Enter go wyprzedzi.
        time.sleep(0.25)
        wcisnij_enter()
    return True


class SkrotSystemowy:
    """Ctrl+Shift+Spacja przez RegisterHotKey, plus Esc tylko na czas nagrywania.

    Skroty systemowe sa przypisane do watku, ktory je zarejestrowal, wiec ten
    watek ma wlasna petle komunikatow. Rejestracje Esc zlecamy mu komunikatem.
    """

    PRZELACZ = 1
    ANULUJ = 2

    def __init__(self, akcja):
        self._akcja = akcja
        self._id_watku = 0
        self._gotowe = threading.Event()
        self.blad: str | None = None

    def start(self) -> bool:
        threading.Thread(target=self._petla, name="skrot", daemon=True).start()
        self._gotowe.wait(3.0)
        return self.blad is None

    def _petla(self) -> None:
        self._id_watku = _kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Wymusza utworzenie kolejki komunikatow, zanim ktos cos do niej wysle.
        _user32.PeekMessageW(byref(msg), None, 0, 0, 0)
        if not _user32.RegisterHotKey(None, self.PRZELACZ,
                                      MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE):
            kod = ctypes.get_last_error()
            self.blad = t("Ctrl+Shift+Spacja jest zajęta przez inny program (kod {kod}).",
                          kod=kod)
            log.warning(self.blad)
            self._gotowe.set()
            return
        log.info("Skrot Ctrl+Shift+Spacja zarejestrowany")
        self._gotowe.set()
        try:
            while _user32.GetMessageW(byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._akcja(int(msg.wParam))
                elif msg.message == WM_PRZELACZ_ESC:
                    if msg.wParam:
                        _user32.RegisterHotKey(None, self.ANULUJ, MOD_NOREPEAT, VK_ESCAPE)
                    else:
                        _user32.UnregisterHotKey(None, self.ANULUJ)
        finally:
            _user32.UnregisterHotKey(None, self.PRZELACZ)
            _user32.UnregisterHotKey(None, self.ANULUJ)

    def esc(self, wlacz: bool) -> None:
        if self._id_watku:
            _user32.PostThreadMessageW(self._id_watku, WM_PRZELACZ_ESC, int(wlacz), 0)

    def stop(self) -> None:
        if self._id_watku:
            _user32.PostThreadMessageW(self._id_watku, WM_QUIT, 0, 0)


class PasekZadan:
    """Pasek postepu na przycisku w pasku zadan (ITaskbarList3)."""

    BRAK = 0x0
    NIEOKRESLONY = 0x1
    BLAD = 0x4  # czerwony - uzywamy go jako "nagrywam"

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
                winshell._metoda(self._obj, 3)(self._obj)  # HrInit
        except Exception:
            log.debug("ITaskbarList3 niedostepny", exc_info=True)
            self._obj = c_void_p()

    def ustaw(self, stan: int, wartosc: int | None = None) -> None:
        if not self._obj:
            return
        try:
            if wartosc is not None:
                self._ws._metoda(self._obj, 9, wintypes.HWND, ctypes.c_ulonglong,
                                 ctypes.c_ulonglong)(self._obj, self._hwnd, wartosc, 100)
            self._ws._metoda(self._obj, 10, wintypes.HWND, ctypes.c_int)(
                self._obj, self._hwnd, stan)
        except Exception:
            log.debug("Nie udalo sie ustawic paska postepu", exc_info=True)


def piknij(czestotliwosc: int, ms: int = 70) -> None:
    def _graj():
        try:
            import winsound

            winsound.Beep(czestotliwosc, ms)
        except Exception:
            pass

    threading.Thread(target=_graj, daemon=True).start()


# --- mikrofon ---------------------------------------------------------------


class Mikrofon:
    """Nagrywanie do pamieci. Dyktowanie trwa sekundy, plik nie jest potrzebny."""

    def __init__(self, index: int | None):
        self._index = index
        self._pa = None
        self._stream = None
        self._bloki: list[bytes] = []
        self._kanaly = 1
        self._czestotliwosc = CZESTOTLIWOSC
        self.poziom = 0.0
        self.nazwa = ""
        # Ustawiane przy pierwszym bloku audio. Otwarcie strumienia trwa ok. 0,35 s,
        # wiec sygnal "mow" dajemy dopiero wtedy - inaczej pierwsze slowo przepada.
        self.gotowy = threading.Event()

    def przygotuj(self) -> None:
        """Inicjalizacja PortAudio z gory - przy pierwszym nagraniu kosztowalaby kolejne 0,5 s."""
        import pyaudiowpatch as pyaudio

        if self._pa is None:
            self._pa = pyaudio.PyAudio()

    def _urzadzenie(self, p):
        from . import audio

        if self._index is not None:
            try:
                return audio._z_info(p.get_device_info_by_index(self._index), loopback=False)
            except Exception:
                log.warning("Mikrofon o indeksie %s niedostepny - biore domyslny",
                            self._index)
        return audio._z_info(p.get_default_input_device_info(), loopback=False)

    def _zamknij_pa(self) -> None:
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
        self._pa = None

    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        # Instancje PyAudio trzymamy, bo jej utworzenie trwa i ucinaloby poczatek
        # wypowiedzi. Gdy urzadzenie zniknelo (zmiana sluchawek), budujemy ja od nowa.
        for proba in range(2):
            try:
                if self._pa is None:
                    self._pa = pyaudio.PyAudio()
                urz = self._urzadzenie(self._pa)
                self._kanaly, self._czestotliwosc = urz.kanaly, urz.czestotliwosc
                self._bloki = []
                self.poziom = 0.0
                self.gotowy.clear()
                self._stream = self._pa.open(
                    format=pyaudio.paInt16, channels=self._kanaly,
                    rate=self._czestotliwosc, input=True,
                    input_device_index=urz.index, frames_per_buffer=1024,
                    stream_callback=self._callback,
                )
                self._stream.start_stream()
                self.nazwa = urz.nazwa
                return
            except Exception:
                self._stream = None
                self._zamknij_pa()
                if proba:
                    raise
                log.warning("Otwarcie mikrofonu nie powiodlo sie - ponawiam", exc_info=True)

    def _callback(self, in_data, frame_count, time_info, status):  # noqa: ARG002
        import pyaudiowpatch as pyaudio

        self._bloki.append(in_data)
        self.gotowy.set()
        try:
            self.poziom = float(np.abs(np.frombuffer(in_data, dtype=np.int16)).max()) / 32768.0
        except ValueError:
            pass
        return (None, pyaudio.paContinue)

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            for metoda in (stream.stop_stream, stream.close):
                try:
                    metoda()
                except Exception:
                    pass
        dane, self._bloki = b"".join(self._bloki), []
        sygnal = np.frombuffer(dane, dtype=np.int16).astype(np.float32) / 32768.0
        if self._kanaly > 1:
            n = len(sygnal) // self._kanaly * self._kanaly
            sygnal = sygnal[:n].reshape(-1, self._kanaly).mean(axis=1)
        if self._czestotliwosc != CZESTOTLIWOSC and sygnal.size:
            import soxr

            sygnal = soxr.resample(sygnal, self._czestotliwosc, CZESTOTLIWOSC, quality="HQ")
        return np.ascontiguousarray(sygnal, dtype=np.float32)

    @property
    def nagrywa(self) -> bool:
        return self._stream is not None

    def zamknij(self) -> None:
        if self._stream is not None:
            self.stop()
        self._zamknij_pa()


# --- rozpoznawanie ----------------------------------------------------------


def popraw_slownikiem(tekst: str, slownik: dict) -> str:
    for zle, dobrze in (slownik or {}).items():
        if zle and isinstance(dobrze, str):
            tekst = re.sub(rf"(?<!\w){re.escape(zle)}(?!\w)", dobrze, tekst,
                           flags=re.IGNORECASE)
    return tekst


def rozpoznaj(silnik, nagranie: np.ndarray, jezyk_kod: str,
              slownik: dict | None = None) -> str:
    from . import transcribe
    from .merge import _jest_halucynacja

    if nagranie.size < MIN_NAGRANIE_S * CZESTOTLIWOSC:
        return ""
    # Na ciszy Whisper zmysla zdania - lepiej nie wstawic nic.
    if transcribe.szczytowe_rms(nagranie) < transcribe.PROG_MOWY_RMS:
        log.info("Nagranie pominiete - sama cisza")
        return ""

    # Pauzy na zastanowienie sie to glowne zrodlo zmyslonych "Dziekuje."
    # i zgubionych fragmentow - patrz transcribe.skroc_pauzy.
    przed = nagranie.size
    nagranie = transcribe.skroc_pauzy(nagranie)
    if nagranie.size < przed:
        log.info("Pauzy skrocone: %.1f s -> %.1f s",
                 przed / CZESTOTLIWOSC, nagranie.size / CZESTOTLIWOSC)
    if nagranie.size < MIN_NAGRANIE_S * CZESTOTLIWOSC:
        return ""

    silnik.zaladuj()
    wynik = silnik.generuj(nagranie, config.JEZYKI.get(jezyk_kod))

    teksty = getattr(wynik, "texts", None)
    if teksty:
        tekst = str(teksty[0])
    else:
        chunki = transcribe._chunki(wynik)
        tekst = " ".join(str(getattr(c, "text", "")) for c in chunki) if chunki else str(wynik)

    tekst = re.sub(r"\s+", " ", tekst).strip()
    if _jest_halucynacja(tekst):
        return ""
    return popraw_slownikiem(tekst, slownik)


# --- okno -------------------------------------------------------------------


def _uruchom_okno():
    import customtkinter as ctk

    from . import transcribe, winshell

    class Dyktuj(ctk.CTk):
        def __init__(self):
            super().__init__()
            glowne = config.Ustawienia()
            self.ust = wczytaj_ustawienia()
            model = self.ust.get("model") or glowne["model"]
            if model != glowne["model"] and not transcribe.model_pobrany(model):
                log.warning("Model %s nie jest pobrany - biore %s", model, glowne["model"])
                model = glowne["model"]
            log.info("Model dyktowania: %s", model)
            self.silnik = transcribe.Silnik(model, glowne["device"])
            self.mikrofon = Mikrofon(glowne["mikrofon_index"])

            self._zdarzenia: "queue.Queue[tuple]" = queue.Queue()
            self._zadania: "queue.Queue[tuple | None]" = queue.Queue()
            self.model_gotowy = False
            self.blad_modelu: str | None = None
            self.nagrywa = False
            self.konczy = False
            self.start_nagrania = 0.0
            self.w_kolejce = 0
            self._hwnd = 0
            self._pasek: PasekZadan | None = None
            self._komunikat: tuple[str, str] | None = None
            self._komunikat_do = 0.0
            self.ostatni_tekst = ostatni_tekst_z_logu()

            self._zbuduj()

            self.skrot = SkrotSystemowy(lambda kod: self._zdarzenia.put(("skrot", kod)))
            self.skrot.start()

            threading.Thread(target=self._petla_pracy, name="whisper", daemon=True).start()
            self._zadania.put(("zaladuj",))

            self.protocol("WM_DELETE_WINDOW", self.zakoncz)
            self.after(60, self._tick)
            # Okno musi juz istniec w systemie, zanim opiszemy je dla paska zadan.
            self.after(600, self._po_starcie)

        # --- interfejs ---------------------------------------------------

        def _zbuduj(self) -> None:
            ctk.set_appearance_mode("dark")
            self.title(APLIKACJA)
            self.geometry("400x250")
            self.resizable(False, False)
            self.configure(fg_color=TLO)
            self._ustaw_ikone()
            self.after(400, self._ustaw_ikone)

            ramka = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
            ramka.pack(fill="both", expand=True, padx=12, pady=12)

            gora = ctk.CTkFrame(ramka, fg_color="transparent")
            gora.pack(fill="x", padx=14, pady=(12, 0))
            # Ratunek, gdy wklejenie nie trafilo albo schowek zostal nadpisany:
            # kopiuje pelny ostatni tekst (etykieta nizej pokazuje tylko poczatek).
            self.b_kopiuj = ctk.CTkButton(gora, text=t("Kopiuj"), width=70, height=26,
                                          fg_color=AKCENT, hover_color="#8b5cf6",
                                          text_color=TLO, font=ctk.CTkFont(size=12, weight="bold"),
                                          command=self.kopiuj_ostatni)
            self.b_kopiuj.pack(side="right")
            self.l_status = ctk.CTkLabel(gora, text="", font=ctk.CTkFont(size=17, weight="bold"),
                                         text_color=TEKST, anchor="w")
            self.l_status.pack(side="left", fill="x", expand=True)
            self.l_podpowiedz = ctk.CTkLabel(ramka, text="", text_color=TEKST_PRZYGASZONY,
                                             anchor="w", font=ctk.CTkFont(size=12))
            self.l_podpowiedz.pack(fill="x", padx=14)

            self.poziom = ctk.CTkProgressBar(ramka, height=6, progress_color=CZERWIEN)
            self.poziom.set(0)
            self.poziom.pack(fill="x", padx=14, pady=(8, 6))

            self.l_tekst = ctk.CTkLabel(ramka, text="", text_color=TEKST_PRZYGASZONY,
                                        anchor="nw", justify="left", wraplength=340,
                                        height=54, font=ctk.CTkFont(size=12))
            self.l_tekst.pack(fill="x", padx=14)
            if self.ostatni_tekst:
                self.l_tekst.configure(text=self.ostatni_tekst)
            else:
                self.b_kopiuj.configure(state="disabled")

            dol = ctk.CTkFrame(ramka, fg_color="transparent")
            dol.pack(fill="x", padx=12, pady=(4, 10), side="bottom")
            etykiety = {"pl": "PL", "en": "EN", "auto": "Auto"}
            self._jezyki = {v: k for k, v in etykiety.items()}
            self.w_jezyk = ctk.CTkSegmentedButton(
                dol, values=list(etykiety.values()), command=self._zmien_jezyk,
                selected_color=AKCENT, selected_hover_color=AKCENT, width=150,
            )
            self.w_jezyk.set(etykiety.get(self.ust["jezyk"], "PL"))
            self.w_jezyk.pack(side="left")
            self.v_enter = ctk.BooleanVar(value=bool(self.ust["enter"]))
            ctk.CTkCheckBox(dol, text=t("Enter po wklejeniu"), variable=self.v_enter,
                            command=self._zmien_enter, fg_color=AKCENT,
                            hover_color=AKCENT, checkbox_width=18, checkbox_height=18,
                            font=ctk.CTkFont(size=12)).pack(side="right")

        def _ustaw_ikone(self) -> None:
            try:
                if not PLIK_IKONY.exists():
                    zapisz_ikone()
                self.iconbitmap(default=str(PLIK_IKONY))
            except Exception:
                log.debug("Nie udalo sie ustawic ikony", exc_info=True)

        def _po_starcie(self) -> None:
            try:
                self._hwnd = int(self.wm_frame(), 16)
            except Exception:
                self._hwnd = self.winfo_id()
            polecenie = f'"{_pythonw()}" "{STARTER}"'
            winshell.opisz_okno(self._hwnd, polecenie, APLIKACJA, f"{PLIK_IKONY},0", ID_APLIKACJI)
            self._pasek = PasekZadan(self._hwnd)
            self._ustaw_pasek()

        def _zmien_jezyk(self, etykieta: str) -> None:
            self.ust["jezyk"] = self._jezyki.get(etykieta, "pl")
            zapisz_ustawienia(self.ust)

        def _zmien_enter(self) -> None:
            self.ust["enter"] = bool(self.v_enter.get())
            zapisz_ustawienia(self.ust)

        def kopiuj_ostatni(self) -> None:
            if not self.ostatni_tekst:
                return
            try:
                ustaw_schowek(self.ostatni_tekst, self._hwnd or None)
            except Exception:
                log.exception("Nie udalo sie skopiowac do schowka")
                self._pokaz_komunikat(t("Schowek zajęty, spróbuj jeszcze raz"), CZERWIEN, 4)
                return
            self._pokaz_komunikat(t("Skopiowano ({n} znaków)", n=len(self.ostatni_tekst)))

        def _pokaz_komunikat(self, tekst: str, kolor: str = ZIELEN, s: float = 3.0) -> None:
            self._komunikat = (tekst, kolor)
            self._komunikat_do = time.monotonic() + s

        def _odswiez(self) -> None:
            podp = t("Ctrl+Shift+Spacja — mów, drugi raz — wstaw tekst")
            if self.skrot.blad:
                status, kolor, podp = t("Skrót niedostępny"), CZERWIEN, self.skrot.blad
            elif self.nagrywa:
                czas = time.monotonic() - self.start_nagrania
                status = t("●  Nagrywam  {czas}", czas=f"{int(czas) // 60}:{int(czas) % 60:02d}")
                kolor, podp = CZERWIEN, t("Ctrl+Shift+Spacja kończy, Esc anuluje")
            elif self.w_kolejce:
                status, kolor = t("Rozpoznaję…"), AKCENT
            elif self._komunikat and time.monotonic() < self._komunikat_do:
                status, kolor = self._komunikat
            elif self.blad_modelu:
                status, kolor, podp = t("Model się nie załadował"), CZERWIEN, self.blad_modelu[:90]
            elif not self.model_gotowy:
                status, kolor = t("Ładuję model…"), TEKST_PRZYGASZONY
                podp = t("Możesz już mówić, tekst pojawi się po załadowaniu")
            else:
                status, kolor = t("Gotowe"), ZIELEN
            self.l_status.configure(text=status, text_color=kolor)
            self.l_podpowiedz.configure(text=podp)
            self.poziom.set(min(1.0, self.mikrofon.poziom * 3) if self.nagrywa else 0)

        def _ustaw_pasek(self) -> None:
            if self._pasek is None:
                return
            if self.nagrywa:
                self._pasek.ustaw(PasekZadan.BLAD, 100)
            elif self.w_kolejce or not (self.model_gotowy or self.blad_modelu):
                self._pasek.ustaw(PasekZadan.NIEOKRESLONY)
            else:
                self._pasek.ustaw(PasekZadan.BRAK)

        def _tick(self) -> None:
            while True:
                try:
                    zdarzenie = self._zdarzenia.get_nowait()
                except queue.Empty:
                    break
                self._obsluz(zdarzenie)
            if (self.nagrywa and not self.konczy and
                    time.monotonic() - self.start_nagrania > float(self.ust["maks_nagranie_s"])):
                self.zatrzymaj()
            self._odswiez()
            self.after(80, self._tick)

        def _obsluz(self, zdarzenie: tuple) -> None:
            rodzaj = zdarzenie[0]
            if rodzaj == "skrot":
                if zdarzenie[1] == SkrotSystemowy.PRZELACZ:
                    self.przelacz()
                elif zdarzenie[1] == SkrotSystemowy.ANULUJ and self.nagrywa:
                    self.anuluj()
            elif rodzaj == "model":
                _, ok, opis = zdarzenie
                self.model_gotowy, self.blad_modelu = ok, (None if ok else opis)
                if ok:
                    self._pokaz_komunikat(t("Gotowe ({opis})", opis=opis))
            elif rodzaj == "tekst":
                _, tekst, czas, wstawiono = zdarzenie
                self.w_kolejce -= 1
                if tekst:
                    self.ostatni_tekst = tekst
                    self.b_kopiuj.configure(state="normal")
                    self.l_tekst.configure(text=tekst, text_color=TEKST)
                    gdzie = t("Wstawiono") if wstawiono else t("Skopiowano do schowka")
                    self._pokaz_komunikat(f"{gdzie} ({czas:.1f} s)")
                else:
                    self._pokaz_komunikat(t("Nic nie usłyszałem"), TEKST_PRZYGASZONY)
            elif rodzaj == "blad":
                self.w_kolejce -= 1
                self._pokaz_komunikat(t("Błąd rozpoznawania"), CZERWIEN, 6)
                self.l_tekst.configure(text=zdarzenie[1][:200], text_color=CZERWIEN)
            self._ustaw_pasek()

        # --- nagrywanie --------------------------------------------------

        def przelacz(self) -> None:
            if self.konczy:
                return
            if self.nagrywa:
                self.zatrzymaj()
            else:
                self.zacznij()

        def zacznij(self) -> None:
            try:
                self.mikrofon.start()
            except Exception as blad:
                log.exception("Nie udalo sie otworzyc mikrofonu")
                self._pokaz_komunikat(t("Mikrofon niedostępny"), CZERWIEN, 6)
                self.l_tekst.configure(text=str(blad)[:200], text_color=CZERWIEN)
                return
            log.info("Nagrywanie z: %s", self.mikrofon.nazwa)
            self.nagrywa = True
            self.start_nagrania = time.monotonic()
            self.skrot.esc(True)
            if self.ust["dzwieki"]:
                gotowy = self.mikrofon.gotowy
                threading.Thread(target=lambda: gotowy.wait(2.0) and piknij(1046),
                                 daemon=True).start()
            self._ustaw_pasek()

        def zatrzymaj(self) -> None:
            self.konczy = True
            self.after(int(OGON_S * 1000), self._dokoncz)

        def _dokoncz(self) -> None:
            nagranie = self.mikrofon.stop()
            self.nagrywa = self.konczy = False
            self.skrot.esc(False)
            if self.ust["dzwieki"]:
                piknij(659)
            self.w_kolejce += 1
            self._zadania.put(("rozpoznaj", nagranie, self.ust["jezyk"],
                               bool(self.ust["enter"]), self.ust["tryb"]))
            self._ustaw_pasek()

        def anuluj(self) -> None:
            self.mikrofon.stop()
            self.nagrywa = self.konczy = False
            self.skrot.esc(False)
            if self.ust["dzwieki"]:
                piknij(330, 120)
            self._pokaz_komunikat(t("Anulowano"), TEKST_PRZYGASZONY)
            self._ustaw_pasek()

        # --- watek roboczy -----------------------------------------------

        def _petla_pracy(self) -> None:
            while True:
                zadanie = self._zadania.get()
                if zadanie is None:
                    return
                if zadanie[0] == "zaladuj":
                    try:
                        self.mikrofon.przygotuj()
                    except Exception:
                        log.warning("PortAudio nie wystartowalo z gory", exc_info=True)
                    try:
                        start = time.monotonic()
                        self.silnik.zaladuj()
                        self._zdarzenia.put(("model", True,
                                             f"{self.silnik.device}, {time.monotonic() - start:.0f} s"))
                    except Exception as blad:
                        log.exception("Nie udalo sie zaladowac modelu")
                        self._zdarzenia.put(("model", False, str(blad)))
                    continue

                _, nagranie, jezyk, enter, tryb = zadanie
                try:
                    start = time.monotonic()
                    tekst = rozpoznaj(self.silnik, nagranie, jezyk, self.ust.get("slownik"))
                    czas = time.monotonic() - start
                    log.info("Rozpoznano %.1f s audio w %.1f s: %r",
                             nagranie.size / CZESTOTLIWOSC, czas, tekst)
                    wstawiono = False
                    if tekst:
                        # Spacja na koncu: dwa dyktowania z rzedu nie skleja sie w jedno slowo.
                        wstawiono = wstaw_tekst(tekst if enter else tekst + " ",
                                                tryb, enter, self._hwnd)
                    self._zdarzenia.put(("tekst", tekst, czas, wstawiono))
                except Exception as blad:
                    log.exception("Blad rozpoznawania")
                    self._zdarzenia.put(("blad", str(blad)))

        def zakoncz(self) -> None:
            self.skrot.stop()
            self.mikrofon.zamknij()
            self._zadania.put(None)
            self.destroy()

    Dyktuj().mainloop()


# --- start ------------------------------------------------------------------

_mutex = None


def _pythonw() -> Path:
    interpreter = Path(sys.executable)
    bez_konsoli = interpreter.with_name("pythonw.exe")
    return bez_konsoli if bez_konsoli.exists() else interpreter


def _jedyna_instancja() -> bool:
    """Drugie klikniecie kafelka ma pokazac dzialajace okno, nie startowac kopii."""
    global _mutex
    _mutex = _kernel32.CreateMutexW(None, False, "Local\\Ucho.Dyktuj")
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return True
    okno = _user32.FindWindowW(None, APLIKACJA)
    if okno:
        _user32.ShowWindow(okno, 9)  # SW_RESTORE
        _user32.SetForegroundWindow(okno)
    return False


def utworz_skroty() -> list[Path]:
    """Skroty w Menu Start i na Pulpicie, z naszym AppUserModelID."""
    import os
    import subprocess

    from . import winshell

    if not PLIK_IKONY.exists():
        zapisz_ikone()
    cele = [
        Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Ucho Dyktuj.lnk",
        Path(os.environ["USERPROFILE"]) / "Desktop" / "Ucho Dyktuj.lnk",
    ]
    for cel in cele:
        skrypt = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CEL);"
            "$s.TargetPath=$env:CEL_PY;$s.Arguments=$env:CEL_ARG;"
            "$s.WorkingDirectory=$env:CEL_KAT;$s.IconLocation=$env:CEL_IKONA;"
            "$s.Description='Dyktowanie glosem do dowolnego okna (lokalny Whisper)';$s.Save()"
        )
        env = dict(os.environ, CEL=str(cel), CEL_PY=str(_pythonw()), CEL_ARG=f'"{STARTER}"',
                   CEL_KAT=str(config.KATALOG_PROJEKTU), CEL_IKONA=str(PLIK_IKONY))
        subprocess.run(["powershell", "-NoProfile", "-Command", skrypt], env=env, check=True)
        winshell.ustaw_id_skrotu(cel, ID_APLIKACJI)
    return cele


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        filename=str(PLIK_LOGU), filemode="a", encoding="utf-8",
    )
    if "--skroty" in sys.argv:
        for cel in utworz_skroty():
            print(cel)
        return 0
    global APLIKACJA
    i18n.ustaw_jezyk(config.Ustawienia()["jezyk_interfejsu"])
    APLIKACJA = t("Ucho Dyktuj")
    if not _jedyna_instancja():
        return 0
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ID_APLIKACJI)
    except Exception:
        pass
    log.info("Start %s", APLIKACJA)
    try:
        _uruchom_okno()
        return 0
    except Exception:
        slad = traceback.format_exc()
        log.error("Nieobsluzony wyjatek:\n%s", slad)
        try:
            import tkinter as tk
            from tkinter import messagebox

            korzen = tk.Tk()
            korzen.withdraw()
            messagebox.showerror(t("{app} — błąd", app=APLIKACJA),
                                 f"{slad.strip().splitlines()[-1]}\n\n"
                                 + t("Szczegóły: {plik}", plik=PLIK_LOGU))
            korzen.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
