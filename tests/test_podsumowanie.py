# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Filtry, ktore pilnuja, zeby podsumowanie nie zmyslalo imion i dat.

Przypadki pochodza z opisu w README: kazdy z nich byl kiedys prawdziwym bledem.
"""
from ucho.podsumowanie import (_bez_zmyslonych_dat, _pada_w_tekscie, _slowa,
                               _wczytaj_json, nazwa_katalogu)

TRANSKRYPT = (
    "Pawle, czy możesz przygotować budżet? Jesteśmy zainteresowani ofertą. "
    "Robimy to we wtorek 29, spotkanie o 12.00 albo 13.30. Dziękuję, Pawła poproszę o notatkę."
)


def test_imie_w_odmianie_przechodzi():
    assert _pada_w_tekscie("Paweł", _slowa(TRANSKRYPT))


def test_imie_ukryte_w_innym_slowie_nie_przechodzi():
    slowa = _slowa(TRANSKRYPT)
    assert not _pada_w_tekscie("Teresa", slowa)  # "zainteresowani"
    assert not _pada_w_tekscie("Robert", slowa)  # "robimy"


def test_niewymowiony_miesiac_znika():
    slowa = _slowa(TRANSKRYPT)
    wynik = _bez_zmyslonych_dat("Spotkanie we wtorek 29 lutego.", slowa, TRANSKRYPT)
    assert "lutego" not in wynik
    assert "29" in wynik


def test_godziny_zostaja():
    slowa = _slowa(TRANSKRYPT)
    wynik = _bez_zmyslonych_dat("Spotkanie o 12.00 albo 13.30.", slowa, TRANSKRYPT)
    assert "12.00" in wynik and "13.30" in wynik


def test_wczytaj_json_z_blokiem_kodu_i_smieciami():
    assert _wczytaj_json('```json\n{"temat": "Budżet"}\n```') == {"temat": "Budżet"}
    assert _wczytaj_json('Oto wynik: {"temat": "X"} i koniec') == {"temat": "X"}
    assert _wczytaj_json("brak obiektu") is None


def test_nazwa_katalogu_zachowuje_znacznik_czasu():
    nowa = nazwa_katalogu("2026-09-08_09-15_Spotkanie",
                          {"temat": "Budżet Q4", "uczestnicy": ["Anna Nowak", "Marek"]})
    assert nowa is not None
    assert nowa.startswith("2026-09-08_09-15_")
    assert "Anna" in nowa and "Marek" in nowa
    assert nazwa_katalogu("bez-znacznika", {"temat": "X"}) is None
