# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Ustawienia aplikacji oraz sciezki katalogow.

Ten modul jest importowany jako pierwszy i ustawia zmienne srodowiskowe
Hugging Face ZANIM ktokolwiek zaimportuje `huggingface_hub` - dzieki temu
modele laduja sie do katalogu programu, a nie do profilu uzytkownika,
a telemetria Hugging Face jest wylaczona.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

KATALOG_PROJEKTU = Path(__file__).resolve().parent.parent
KATALOG_MODELI = KATALOG_PROJEKTU / "modele"
KATALOG_CACHE_OV = KATALOG_MODELI / "ovcache"
PLIK_USTAWIEN = KATALOG_PROJEKTU / "ustawienia.json"
PLIK_LOGU = KATALOG_PROJEKTU / "ucho.log"

# Nagrania celowo NIE trafiaja do Dokumentow: na wielu firmowych komputerach
# Dokumenty sa przekierowane do OneDrive, wiec nagrania i transkrypty
# wyjechalyby do chmury, a obietnica "nic nie wychodzi z komputera" bylaby
# nieprawdziwa. Katalog domowy OneDrive domyslnie nie synchronizuje.
KATALOG_NAGRAN = Path.home() / "Ucho" / "nagrania"

# Musi byc przed importem huggingface_hub w innych modulach.
os.environ.setdefault("HF_HOME", str(KATALOG_MODELI))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

APLIKACJA = "Ucho"
WERSJA = "0.1.0"

# AppUserModelID - Windows grupuje po nim okna na pasku zadan. Bez wlasnego
# identyfikatora okno dziedziczy ikone pythonw.exe zamiast naszej.
ID_APLIKACJI = "Ucho.Transkrypcja"

# Gotowe, prekonwertowane modele OpenVINO - nie wymagaja PyTorcha ani konwersji.
# Turbo to model destylowany (4 warstwy dekodera zamiast 32) - szybki, ale
# slabo rozpoznaje jezyk na krotkich wypowiedziach. Przy "auto" na spotkaniu
# PL/EN sypal wtracenia po islandzku, rosyjsku czy arabsku, wiec domyslnym
# modelem jest pelny large-v3. Opisy sa po polsku i tlumaczone w interfejsie.
MODELE = {
    "OpenVINO/whisper-large-v3-int8-ov": {
        "opis": "Large-v3 INT8 — najlepsze rozpoznanie języka (zalecany)",
        "rozmiar": "~1,6 GB",
    },
    "OpenVINO/whisper-large-v3-fp16-ov": {
        "opis": "Large-v3 FP16 — pełna precyzja, ok. 25% wolniej (domyślny w Dyktuj)",
        "rozmiar": "~3,1 GB",
    },
    "OpenVINO/whisper-large-v3-turbo-int8-ov": {
        "opis": "Turbo INT8 — szybszy, gorszy przy mieszanych językach",
        "rozmiar": "~790 MB",
    },
    "OpenVINO/whisper-large-v3-turbo-int4-ov": {
        "opis": "Turbo INT4 — najszybszy, do długich nagrań",
        "rozmiar": "~500 MB",
    },
}

MODEL_DOMYSLNY = "OpenVINO/whisper-large-v3-int8-ov"

# Opcjonalne modele jezykowe do podsumowania i nazwania folderu. Ten sam runtime
# co Whisper (OpenVINO GenAI), ten sam katalog modeli - bez Ollamy i bez serwera
# w tle. Model laduje sie dopiero po transkrypcji i jest zwalniany od razu.
# Podsumowania sa domyslnie wylaczone: rdzeniem Ucho jest transkrypcja.
MODELE_LLM = {
    "OpenVINO/Qwen3-8B-int4-ov": {
        "opis": "Qwen3 8B INT4 — najlepsze podsumowania, wolniejszy",
        "rozmiar": "~5,0 GB",
    },
    "OpenVINO/Qwen3-4B-int4-ov": {
        "opis": "Qwen3 4B INT4 — szybki, dobry polski",
        "rozmiar": "~2,3 GB",
    },
    "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov": {
        "opis": "Qwen2.5 1.5B INT4 — najszybszy, najmniejszy",
        "rozmiar": "~0,95 GB",
    },
}

MODEL_LLM_DOMYSLNY = "OpenVINO/Qwen3-8B-int4-ov"

# Przypiete rewizje modeli (commit na Hugging Face, sprawdzone 14.11.2026).
# Pobieramy dokladnie te wersje, wiec podmiana modelu w repozytorium zrodlowym
# nie trafi do uzytkownikow bez zmiany tego slownika.
REWIZJE = {
    "OpenVINO/whisper-large-v3-int8-ov": "5603442ef51120fc1d5bb6c8749d01592b14acb5",
    "OpenVINO/whisper-large-v3-fp16-ov": "220761e60602a5ca694c409d5f424563b75d6820",
    "OpenVINO/whisper-large-v3-turbo-int8-ov": "b568445dd5dc8c695bde596f8acbb4694fd6ba64",
    "OpenVINO/whisper-large-v3-turbo-int4-ov": "ae50b4d9a9dbaf16f2df59c23f3984e42f864dfc",
    "OpenVINO/Qwen3-8B-int4-ov": "5c47abf4b8e12ebe8e99745bb0c1ec17e0c0abcc",
    "OpenVINO/Qwen3-4B-int4-ov": "b467368d16b75df14055562fe927ae8e1f15f7ef",
    "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov": "4d14c299e35d8b74e3471f9e92bd1377fae50736",
}

# Whisper oczekuje tokenu jezyka w tej postaci. None = autodetekcja.
# Autodetekcja wybiera wylacznie sposrod pozostalych pozycji tego slownika
# (patrz transcribe.py, zawezona mapa lang_to_id).
JEZYKI = {
    "auto": None,
    "pl": "<|pl|>",
    "en": "<|en|>",
}

ETYKIETY_JEZYKOW = {
    "auto": "Auto (PL/EN)",
    "pl": "Polski",
    "en": "English",
}

ETYKIETY_SCIEZEK = {
    "obie": "Mikrofon + dźwięk systemowy",
    "mikrofon": "Tylko mikrofon",
    "system": "Tylko dźwięk systemowy",
}

ETYKIETY_INTERFEJSU = {
    "auto": "Auto (jak system)",
    "pl": "Polski",
    "en": "English",
}

DOMYSLNE = {
    "katalog_nagran": str(KATALOG_NAGRAN),
    "model": MODEL_DOMYSLNY,
    "device": "GPU",
    "jezyk": "auto",
    "jezyk_interfejsu": "auto",
    "sciezki": "obie",
    "mikrofon_index": None,
    "system_index": None,
    "hotkey": "<ctrl>+<alt>+r",
    "tray": True,
    "transkrybuj_po_stopie": True,
    "podsumowanie": False,
    "model_llm": MODEL_LLM_DOMYSLNY,
    "zmiana_nazwy": False,
}


class Ustawienia:
    """Cienka warstwa nad plikiem JSON. Braki uzupelnia wartosciami domyslnymi."""

    def __init__(self, plik: Path = PLIK_USTAWIEN):
        self._plik = plik
        self._dane = dict(DOMYSLNE)
        self.wczytaj()

    def wczytaj(self) -> None:
        try:
            zapisane = json.loads(self._plik.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if isinstance(zapisane, dict):
            for klucz, wartosc in zapisane.items():
                if klucz in DOMYSLNE:
                    self._dane[klucz] = wartosc

    def zapisz(self) -> None:
        try:
            self._plik.parent.mkdir(parents=True, exist_ok=True)
            self._plik.write_text(
                json.dumps(self._dane, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def __getitem__(self, klucz: str):
        return self._dane.get(klucz, DOMYSLNE.get(klucz))

    def __setitem__(self, klucz: str, wartosc) -> None:
        self._dane[klucz] = wartosc

    def get(self, klucz: str, domyslna=None):
        return self._dane.get(klucz, domyslna)

    @property
    def katalog_nagran(self) -> Path:
        return Path(self._dane["katalog_nagran"])


def przygotuj_katalogi(ustawienia: "Ustawienia | None" = None) -> None:
    KATALOG_MODELI.mkdir(parents=True, exist_ok=True)
    KATALOG_CACHE_OV.mkdir(parents=True, exist_ok=True)
    katalog = ustawienia.katalog_nagran if ustawienia else KATALOG_NAGRAN
    katalog.mkdir(parents=True, exist_ok=True)
