# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Kazdy tekst interfejsu ma tlumaczenie z tymi samymi polami {..}."""
import ast
import json
import string
from pathlib import Path

from ucho import config, i18n

KATALOG = Path(__file__).resolve().parent.parent / "ucho"
EN = json.loads((KATALOG / "locale" / "en.json").read_text(encoding="utf-8"))


def _klucze_z_kodu() -> set[str]:
    klucze = set()
    for plik in KATALOG.glob("*.py"):
        for wezel in ast.walk(ast.parse(plik.read_text(encoding="utf-8"))):
            if (isinstance(wezel, ast.Call) and isinstance(wezel.func, ast.Name)
                    and wezel.func.id == "t" and wezel.args
                    and isinstance(wezel.args[0], ast.Constant)
                    and isinstance(wezel.args[0].value, str)):
                klucze.add(wezel.args[0].value)
    return klucze


def _klucze_z_konfiguracji() -> set[str]:
    klucze = {"Ja", "Spotkanie"}
    klucze |= set(config.ETYKIETY_SCIEZEK.values())
    klucze |= {config.ETYKIETY_INTERFEJSU["auto"]}
    klucze |= {m["opis"] for m in config.MODELE.values()}
    klucze |= {m["opis"] for m in config.MODELE_LLM.values()}
    return klucze


def _pola(tekst: str) -> set[str]:
    return {pole for _, pole, _, _ in string.Formatter().parse(tekst) if pole}


def test_kazdy_tekst_ma_tlumaczenie():
    brakujace = sorted((_klucze_z_kodu() | _klucze_z_konfiguracji()) - set(EN))
    assert not brakujace, f"Brak w en.json: {brakujace}"


def test_tlumaczenia_maja_te_same_pola():
    for klucz, wartosc in EN.items():
        if klucz.startswith("_"):
            continue
        assert _pola(klucz) == _pola(wartosc), klucz


def test_przelaczanie_jezyka():
    assert i18n.ustaw_jezyk("en") == "en"
    assert i18n.t("Ustawienia") == "Settings"
    assert i18n.t("Model pobrany ({rozmiar})", rozmiar="1 GB") == "Model downloaded (1 GB)"
    assert i18n.ustaw_jezyk("pl") == "pl"
    assert i18n.t("Ustawienia") == "Ustawienia"
    assert i18n.t("Tekst bez tłumaczenia") == "Tekst bez tłumaczenia"
