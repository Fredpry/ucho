# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Ikona w zasobniku systemowym i globalny skrot klawiszowy.

Oba mechanizmy sa opcjonalne - jesli pystray albo pynput nie wystartuja
(np. przez polityke systemowa), aplikacja dziala dalej, tylko bez nich.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import ikona as ikona_mod
from .i18n import t

log = logging.getLogger(__name__)


class Zasobnik:
    """Cienka nakladka na pystray. Wszystkie metody sa bezpieczne przy braku biblioteki."""

    def __init__(self, tytul: str, on_przelacz: Callable[[], None],
                 on_pokaz: Callable[[], None], on_zakoncz: Callable[[], None]):
        self._tytul = tytul
        self._on_przelacz = on_przelacz
        self._on_pokaz = on_pokaz
        self._on_zakoncz = on_zakoncz
        self._ikona = None
        self._nagrywa = False

    def start(self) -> bool:
        try:
            import pystray
        except ImportError:
            log.warning("Brak pystray - zasobnik wylaczony")
            return False

        menu = pystray.Menu(
            pystray.MenuItem(t("Pokaż okno"), lambda: self._on_pokaz(), default=True),
            pystray.MenuItem(
                lambda _: t("Zatrzymaj nagrywanie") if self._nagrywa else t("Rozpocznij nagrywanie"),
                lambda: self._on_przelacz(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("Zakończ"), lambda: self._on_zakoncz()),
        )
        try:
            self._ikona = pystray.Icon(
                "ucho", ikona_mod.rysuj(64, nagrywa=False), self._tytul, menu
            )
            self._ikona.run_detached()
            log.info("Ikona w zasobniku uruchomiona")
            return True
        except Exception as blad:
            log.warning("Nie udalo sie uruchomic zasobnika: %s", blad)
            self._ikona = None
            return False

    def ustaw_stan(self, nagrywa: bool, opis: str | None = None) -> None:
        self._nagrywa = nagrywa
        if self._ikona is None:
            return
        try:
            self._ikona.icon = ikona_mod.rysuj(64, nagrywa=nagrywa)
            self._ikona.title = opis or self._tytul
            self._ikona.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        if self._ikona is None:
            return
        try:
            self._ikona.stop()
        except Exception:
            pass
        self._ikona = None


# Kody wirtualne klawiszy - niezalezne od ukladu klawiatury i od tego,
# czy Windows przetlumaczyl klawisz na znak.
_VK_LITER = {litera: 0x41 + i for i, litera in enumerate("abcdefghijklmnopqrstuvwxyz")}
_VK_FUNKCYJNE = {f"f{n}": 0x6F + n for n in range(1, 13)}

_ODSTEP_POWTORZEN_S = 0.6


class GlobalnySkrot:
    """Globalny hotkey dzialajacy takze gdy okno jest schowane.

    Nie uzywamy `keyboard.GlobalHotKeys`, bo przy wcisnietym Alt Windows nie
    tlumaczy litery na znak i pynput zglasza surowy kod (np. <82> zamiast 'r') -
    wbudowane dopasowanie wtedy nigdy nie trafia. Sledzimy wiec modyfikatory
    sami i porownujemy kod wirtualny klawisza.
    """

    def __init__(self, kombinacja: str, akcja: Callable[[], None]):
        self._kombinacja = kombinacja
        self._akcja = akcja
        self._sluchacz = None
        self._wcisniete: set[str] = set()
        self._ostatnie_wywolanie = 0.0
        self._wymagane, self._vk = self._rozbierz(kombinacja)

    @staticmethod
    def _rozbierz(kombinacja: str) -> tuple[set[str], int | None]:
        wymagane: set[str] = set()
        vk: int | None = None
        for token in kombinacja.lower().replace(" ", "").split("+"):
            token = token.strip("<>")
            if token in ("ctrl", "alt", "shift"):
                wymagane.add(token)
            elif token in _VK_LITER:
                vk = _VK_LITER[token]
            elif token in _VK_FUNKCYJNE:
                vk = _VK_FUNKCYJNE[token]
            elif token.isdigit():
                vk = int(token)
        return wymagane, vk

    @staticmethod
    def _modyfikator(key) -> str | None:
        nazwa = getattr(key, "name", "") or ""
        if nazwa.startswith("ctrl"):
            return "ctrl"
        if nazwa.startswith("alt"):
            return "alt"
        if nazwa.startswith("shift"):
            return "shift"
        return None

    def start(self) -> bool:
        if self._vk is None:
            log.warning("Nie rozpoznano skrotu %s", self._kombinacja)
            return False
        try:
            from pynput import keyboard
        except ImportError:
            log.warning("Brak pynput - globalny skrot wylaczony")
            return False
        try:
            self._sluchacz = keyboard.Listener(
                on_press=self._na_wcisniecie, on_release=self._na_zwolnienie
            )
            self._sluchacz.daemon = True
            self._sluchacz.start()
            log.info("Skrot globalny %s aktywny (vk=%s)", self._kombinacja, self._vk)
            return True
        except Exception as blad:
            log.warning("Nie udalo sie zarejestrowac skrotu %s: %s", self._kombinacja, blad)
            self._sluchacz = None
            return False

    def _na_wcisniecie(self, key) -> None:
        mod = self._modyfikator(key)
        if mod:
            self._wcisniete.add(mod)
            return
        if getattr(key, "vk", None) != self._vk:
            return
        if not self._wymagane.issubset(self._wcisniete):
            return
        # Autorepeat przy przytrzymaniu klawisza nie moze wywolac akcji wielokrotnie.
        teraz = time.monotonic()
        if teraz - self._ostatnie_wywolanie < _ODSTEP_POWTORZEN_S:
            return
        self._ostatnie_wywolanie = teraz
        self._wywolaj()

    def _na_zwolnienie(self, key) -> None:
        mod = self._modyfikator(key)
        if mod:
            self._wcisniete.discard(mod)

    def _wywolaj(self) -> None:
        # Akcja moze podniesc okno, wiec nie blokujemy watku nasluchujacego klawiatury.
        threading.Thread(target=self._akcja, daemon=True).start()

    def stop(self) -> None:
        if self._sluchacz is None:
            return
        try:
            self._sluchacz.stop()
        except Exception:
            pass
        self._sluchacz = None
