# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tlumaczenia interfejsu: polski (tekst zrodlowy) i angielski.

Kluczem jest polski tekst wpisany w kodzie, a `locale/en.json` mapuje go na
angielski. Brak tlumaczenia oznacza polski tekst, wiec nic sie nie wysypie,
a test `tests/test_i18n.py` pilnuje, zeby kazde `t("...")` mialo wpis w en.json.

Jezyk wybiera ustawienie `jezyk_interfejsu` ("auto", "pl", "en"). "auto" bierze
jezyk interfejsu Windows: polski dla polskiego systemu, angielski dla reszty.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

JEZYKI_INTERFEJSU = ("auto", "pl", "en")
_KATALOG = Path(__file__).resolve().parent / "locale"

_jezyk = "pl"
_tlumaczenia: dict[str, str] = {}


def jezyk_systemu() -> str:
    """'pl' dla polskiego interfejsu Windows, 'en' dla kazdego innego."""
    if sys.platform == "win32":
        try:
            import ctypes

            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            # Mlodsze 10 bitow to jezyk glowny; 0x15 = polski.
            return "pl" if (langid & 0x3FF) == 0x15 else "en"
        except Exception:
            log.debug("Nie udalo sie odczytac jezyka systemu", exc_info=True)
    import locale

    kod = (locale.getlocale()[0] or "").lower()
    return "pl" if kod.startswith(("pl", "polish")) else "en"


def wczytaj_tlumaczenia(jezyk: str) -> dict[str, str]:
    if jezyk == "pl":
        return {}
    try:
        dane = json.loads((_KATALOG / f"{jezyk}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Brak tlumaczen dla jezyka %s", jezyk)
        return {}
    return {k: v for k, v in dane.items() if isinstance(v, str) and not k.startswith("_")}


def ustaw_jezyk(jezyk: str | None) -> str:
    """Ustawia jezyk interfejsu dla calego procesu. Zwraca faktycznie uzyty kod."""
    global _jezyk, _tlumaczenia
    if jezyk not in ("pl", "en"):
        jezyk = jezyk_systemu()
    _jezyk = jezyk
    _tlumaczenia = wczytaj_tlumaczenia(jezyk)
    return _jezyk


def jezyk() -> str:
    return _jezyk


def t(tekst: str, **pola) -> str:
    """Tekst w biezacym jezyku; pola wstawiane przez str.format."""
    wynik = _tlumaczenia.get(tekst, tekst)
    return wynik.format(**pola) if pola else wynik
