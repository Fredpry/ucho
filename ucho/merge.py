# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Scalanie transkrypcji dwoch sciezek w jeden transkrypt z podzialem na mowcow.

Mikrofon to "Ja", dzwiek systemowy to "Spotkanie". Poniewaz obie sciezki
sa nagrywane rownolegle i wyrownane do zegara sciennego, wystarczy posortowac
segmenty po czasie startu, aby odtworzyc przebieg rozmowy.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .i18n import t

MOWCA_JA = "Ja"
MOWCA_SPOTKANIE = "Spotkanie"

# Whisper na ciszy i szumie potrafi wygenerowac stale formulki z napisow filmowych.
# Odsiewamy najczestsze, zeby nie zasmiecaly transkryptu.
_HALUCYNACJE = [
    r"^napisy (stworzone przez|robione przez|wykonane przez)",
    r"amara\.?org",
    r"^zapraszam(y)? (do subskrypcji|na kolejny)",
    r"^subskrybuj",
    # "dzi[eę]kuj": wczesniej bylo samo "dziekuj", wiec prawdziwe "Dziękuję"
    # z polskim "ę" nigdy nie pasowalo (wylapal to test).
    r"^dzi[eę]kuj[eę] za (uwag[eę]|obejrzenie|ogl[aą]danie)",
    r"^subtitles? (by|created by|provided by)",
    r"^transcri(pt|ption) by",
    r"^thanks? for watching",
    r"^please subscribe",
    # Whisper wstawia te formulki w miejsce ciszy, takze w nagraniu polskim.
    r"^(thank you|thanks|you|bye)[.!]?$",
    # Samodzielne wtracenie bez zdania wokol to niemal zawsze artefakt na szumie.
    r"^(so|um|uh+|hmm+|mm+|ah+|oh+|eh+|yeah|no|tak|no tak)[.,!?]?$",
    r"^napisy:",
    r"^\W*$",
]
_HALUCYNACJE_RE = [re.compile(w, re.IGNORECASE) for w in _HALUCYNACJE]

# Minimalny czas trwania segmentu - krotsze to zwykle artefakty.
_MIN_TRWANIE = 0.15
# Przerwa, powyzej ktorej nie sklejamy dwoch wypowiedzi tego samego mowcy.
_MAX_PRZERWA_SKLEJANIA = 1.5


@dataclass
class Segment:
    start: float
    koniec: float
    tekst: str
    mowca: str


def _jest_halucynacja(tekst: str) -> bool:
    czysty = tekst.strip()
    if not czysty:
        return True
    return any(wzor.search(czysty) for wzor in _HALUCYNACJE_RE)


def oczysc(segmenty: list[Segment]) -> list[Segment]:
    """Usuwa puste segmenty, znane halucynacje i bezposrednie powtorzenia."""
    wynik: list[Segment] = []
    for seg in segmenty:
        tekst = " ".join(seg.tekst.split())
        if not tekst or _jest_halucynacja(tekst):
            continue
        if seg.koniec - seg.start < _MIN_TRWANIE:
            continue
        if wynik and wynik[-1].mowca == seg.mowca and wynik[-1].tekst == tekst:
            # Whisper potrafi zapetlic to samo zdanie - bierzemy je raz.
            wynik[-1].koniec = seg.koniec
            continue
        wynik.append(Segment(seg.start, seg.koniec, tekst, seg.mowca))
    return wynik


def scal(*listy: list[Segment]) -> list[Segment]:
    """Laczy segmenty ze wszystkich sciezek w chronologiczna rozmowe."""
    wszystkie: list[Segment] = []
    for lista in listy:
        wszystkie.extend(oczysc(lista))
    wszystkie.sort(key=lambda s: (s.start, s.mowca))

    sklejone: list[Segment] = []
    for seg in wszystkie:
        poprzedni = sklejone[-1] if sklejone else None
        if (
            poprzedni is not None
            and poprzedni.mowca == seg.mowca
            and seg.start - poprzedni.koniec <= _MAX_PRZERWA_SKLEJANIA
        ):
            poprzedni.tekst = f"{poprzedni.tekst} {seg.tekst}".strip()
            poprzedni.koniec = max(poprzedni.koniec, seg.koniec)
        else:
            sklejone.append(Segment(seg.start, seg.koniec, seg.tekst, seg.mowca))
    return sklejone


def slug(tekst: str, maks: int = 48) -> str:
    """Dowolny tekst -> bezpieczny fragment nazwy katalogu.

    Podkreslenie tez zamieniamy na mysinik: nazwa katalogu sesji dzieli sie na
    czlony wlasnie po podkresleniu (data_godzina_temat_imiona).
    """
    # "ł" nie rozklada sie w NFKD i bez tego znikalo ("zespół" -> "zespo").
    tekst = tekst.replace("ł", "l").replace("Ł", "L")
    tekst = unicodedata.normalize("NFKD", tekst).encode("ascii", "ignore").decode()
    tekst = re.sub(r"[^\w\s-]", "", tekst).strip()
    tekst = re.sub(r"[\s_-]+", "-", tekst)
    return tekst[:maks].strip("-")


def czas_hms(sekundy: float) -> str:
    sekundy = max(0.0, sekundy)
    g, reszta = divmod(int(sekundy), 3600)
    m, s = divmod(reszta, 60)
    return f"{g:02d}:{m:02d}:{s:02d}"


def _czas_srt(sekundy: float) -> str:
    sekundy = max(0.0, sekundy)
    g, reszta = divmod(int(sekundy), 3600)
    m, s = divmod(reszta, 60)
    ms = int(round((sekundy - int(sekundy)) * 1000))
    return f"{g:02d}:{m:02d}:{s:02d},{ms:03d}"


def jako_markdown(segmenty: list[Segment], meta: dict) -> str:
    naglowek = [
        "# " + t("Transkrypt: {tytul}", tytul=meta.get("temat") or meta.get("nazwa") or t("Spotkanie")),
        "",
        f"- **{t('Data')}:** {meta.get('data', '')}",
        f"- **{t('Czas trwania')}:** {czas_hms(meta.get('trwanie_s', 0))}",
        f"- **Model:** `{meta.get('model', '')}`",
        f"- **{t('Obliczenia')}:** {meta.get('device', '')}",
        f"- **{t('Język')}:** {meta.get('jezyk', 'auto')}",
    ]
    if meta.get("urzadzenie_mikrofon"):
        naglowek.append(f"- **{t('Mikrofon')}:** {meta['urzadzenie_mikrofon']}")
    if meta.get("urzadzenie_system"):
        naglowek.append(f"- **{t('Dźwięk systemowy')}:** {meta['urzadzenie_system']}")
    if meta.get("uczestnicy"):
        naglowek.append(f"- **{t('Uczestnicy')}:** {', '.join(meta['uczestnicy'])}")
    naglowek += ["", "---", ""]

    # Podsumowanie na gorze, zeby nie trzeba bylo czytac calosci - pelna wersja
    # z ustaleniami siedzi w podsumowanie.md.
    if meta.get("tldr"):
        naglowek += ["## " + t("W skrócie"), "", meta["tldr"], ""]
        if meta.get("punkty"):
            naglowek += [f"- {punkt}" for punkt in meta["punkty"]] + [""]
        naglowek += ["---", ""]

    if not segmenty:
        naglowek.append("_" + t("Nie rozpoznano żadnej mowy.") + "_")
        return "\n".join(naglowek) + "\n"

    for seg in segmenty:
        naglowek.append(f"**[{czas_hms(seg.start)}] {seg.mowca}:** {seg.tekst}")
        naglowek.append("")
    return "\n".join(naglowek)


def jako_srt(segmenty: list[Segment]) -> str:
    linie: list[str] = []
    for i, seg in enumerate(segmenty, start=1):
        koniec = max(seg.koniec, seg.start + 0.5)
        linie.append(str(i))
        linie.append(f"{_czas_srt(seg.start)} --> {_czas_srt(koniec)}")
        linie.append(f"{seg.mowca}: {seg.tekst}")
        linie.append("")
    return "\n".join(linie)


def jako_tekst(segmenty: list[Segment]) -> str:
    return "\n".join(f"{seg.mowca}: {seg.tekst}" for seg in segmenty) + "\n"


def zapisz_wyniki(katalog: Path, segmenty: list[Segment], meta: dict) -> dict[str, Path]:
    """Zapisuje transkrypt w trzech formatach oraz metadane sesji."""
    katalog.mkdir(parents=True, exist_ok=True)
    pliki = {
        "md": katalog / "transkrypt.md",
        "srt": katalog / "transkrypt.srt",
        "txt": katalog / "transkrypt.txt",
        "meta": katalog / "meta.json",
    }
    pliki["md"].write_text(jako_markdown(segmenty, meta), encoding="utf-8")
    pliki["srt"].write_text(jako_srt(segmenty), encoding="utf-8")
    pliki["txt"].write_text(jako_tekst(segmenty), encoding="utf-8")

    pelne_meta = dict(meta)
    pelne_meta["zapisano"] = datetime.now().isoformat(timespec="seconds")
    pelne_meta["liczba_segmentow"] = len(segmenty)
    pelne_meta["segmenty"] = [asdict(s) for s in segmenty]
    pliki["meta"].write_text(
        json.dumps(pelne_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return pliki
