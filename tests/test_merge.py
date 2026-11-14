# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
from ucho.merge import Segment, czas_hms, jako_markdown, jako_srt, oczysc, scal, slug


def test_oczysc_usuwa_halucynacje_whispera():
    segmenty = [
        Segment(0.0, 2.0, "Napisy stworzone przez społeczność Amara.org", "Spotkanie"),
        Segment(2.0, 4.0, "Dziękuję za uwagę.", "Spotkanie"),
        Segment(4.0, 6.0, "Thank you.", "Spotkanie"),
        Segment(6.0, 9.0, "Zaczynamy od budżetu na czwarty kwartał.", "Ja"),
    ]
    wynik = oczysc(segmenty)
    assert [s.tekst for s in wynik] == ["Zaczynamy od budżetu na czwarty kwartał."]


def test_oczysc_odrzuca_zbyt_krotkie_i_powtorzenia():
    segmenty = [
        Segment(0.0, 0.05, "Za krótko", "Ja"),
        Segment(1.0, 2.0, "To samo zdanie", "Ja"),
        Segment(2.0, 3.0, "To samo zdanie", "Ja"),
    ]
    wynik = oczysc(segmenty)
    assert len(wynik) == 1
    assert wynik[0].koniec == 3.0


def test_scal_porzadkuje_sciezki_po_czasie_i_skleja_bliskie_wypowiedzi():
    ja = [Segment(0.0, 2.0, "Cześć wszystkim.", "Ja"), Segment(2.5, 4.0, "Zaczynamy?", "Ja")]
    spotkanie = [Segment(5.0, 7.0, "Tak, zaczynajmy.", "Spotkanie")]
    wynik = scal(ja, spotkanie)
    assert [(s.mowca, s.tekst) for s in wynik] == [
        ("Ja", "Cześć wszystkim. Zaczynamy?"),
        ("Spotkanie", "Tak, zaczynajmy."),
    ]


def test_slug_bez_polskich_znakow_i_podkreslen():
    assert slug("Budżet Q4_zespół") == "Budzet-Q4-zespol"
    assert len(slug("a" * 100)) == 48


def test_czas_hms():
    assert czas_hms(3725.9) == "01:02:05"
    assert czas_hms(-5) == "00:00:00"


def test_jako_srt_ma_numeracje_i_minimalny_czas():
    srt = jako_srt([Segment(1.0, 1.1, "Krótko", "Ja")])
    assert srt.splitlines()[:3] == ["1", "00:00:01,000 --> 00:00:01,500", "Ja: Krótko"]


def test_jako_markdown_bez_mowy():
    tekst = jako_markdown([], {"nazwa": "Test", "trwanie_s": 61})
    assert "# Transkrypt: Test" in tekst
    assert "00:01:01" in tekst
    assert "Nie rozpoznano żadnej mowy." in tekst
