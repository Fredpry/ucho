# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Lokalna transkrypcja Whisper przez OpenVINO GenAI (iGPU Arc / CPU / NPU).

Uzywamy gotowych, prekonwertowanych modeli z repozytorium OpenVINO na
Hugging Face, dzieki czemu nie potrzeba PyTorcha ani `optimum-intel`.

Dlugie nagrania tniemy sami na kilkuminutowe segmenty (w miejscu ciszy),
mimo ze pipeline radzi sobie z long-form samodzielnie. Powod: OpenVINO nie
raportuje postepu, a bez tego uzytkownik przez kilkanascie minut patrzy
na zamrozony pasek. Przy okazji blad w jednym segmencie nie przekresla calosci.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from . import config
from .i18n import t
from .merge import MOWCA_JA, MOWCA_SPOTKANIE, Segment, scal

log = logging.getLogger(__name__)

CZESTOTLIWOSC = 16_000
RAMKA_ANALIZY_S = 0.1

# Jezyk wymuszony: dlugie segmenty, bo pelny kontekst poprawia jakosc.
DLUGOSC_SEGMENTU_S = 300.0
OKNO_SZUKANIA_CISZY_S = 15.0

# Autodetekcja: krotkie okna, bo Whisper wykrywa jezyk RAZ na wywolanie generate.
# Przy dlugim oknie mieszane spotkanie PL/EN konczy sie tym, ze drugi jezyk jest
# tlumaczony na pierwszy albo gubiony calkowicie. Zmierzone na naprzemiennym
# nagraniu PL/EN: caly plik naraz - 0 z 3 fragmentow EN, okna 30 s - 2 z 3 ale
# przetlumaczone na polski, okna 12 s - 3 z 3 poprawnie po angielsku.
DLUGOSC_SEGMENTU_AUTO_S = 12.0

# Cena krotkich okien: na "mhm", kaszlnieciu czy szumie detektor nie ma czego
# sluchac i wybiera losowo z calej setki jezykow Whispera - w polskim spotkaniu
# lada chwila wyskakuje islandzki, koreanski albo portugalski. Zawezamy wiec
# pule wyboru do jezykow z config.JEZYKI, przekazujac okrojone `lang_to_id`.
#
# `suppress_tokens` tu NIE pomaga (sprawdzone): detekcja jezyka to osobny
# przebieg po `lang_to_id`, wykonywany zanim ruszy generacja, wiec tlumienie
# tokenow jej nie dotyka. Zmierzone na 8 feralnych fragmentach ostatniego
# spotkania: suppress_tokens - 1 z 8 poprawionych, okrojone lang_to_id - 8 z 8.
JEZYKI_AUTO = tuple(token for token in config.JEZYKI.values() if token)
RAMKA_VAD_S = 0.05
PROG_CISZY_WZGLEDNY = 0.06
MIN_CISZA_S = 0.35

# Ponizej tego szczytowego RMS fragment uznajemy za cisze i w ogole nie wysylamy
# go do modelu. Whisper na ciszy halucynuje - potrafi wyprodukowac zdanie po
# islandzku albo rosyjsku w polskim nagraniu. Sciezka mikrofonu, gdy tylko
# sluchasz, jest cisza przez wieksza czesc spotkania, wiec to takze duza
# oszczednosc czasu. Cisza mierzy sie ponizej 0.001, mowa od okolo 0.01 wzwyz.
PROG_MOWY_RMS = 0.003

PostepFn = Callable[[float, str], None]


# --- model ---------------------------------------------------------------


def sciezka_modelu(repo_id: str, pobierz: bool = False) -> Path:
    """Zwraca lokalny katalog modelu. Bez `pobierz` nie dotyka sieci.

    Pobieramy zawsze przypieta rewizje z `config.REWIZJE`. Przy sprawdzaniu
    lokalnym akceptujemy tez starsza, wczesniej pobrana kopie (np. sprzed
    przypiecia), zeby nikt nie musial pobierac kilku GB od nowa.
    """
    from huggingface_hub import snapshot_download

    rewizja = config.REWIZJE.get(repo_id)
    if pobierz:
        return Path(snapshot_download(repo_id, revision=rewizja))
    try:
        return Path(snapshot_download(repo_id, revision=rewizja, local_files_only=True))
    except Exception:
        if rewizja is None:
            raise
        return Path(snapshot_download(repo_id, local_files_only=True))


def model_pobrany(repo_id: str) -> bool:
    try:
        sciezka_modelu(repo_id, pobierz=False)
        return True
    except Exception as blad:
        # Brak modelu to LocalEntryNotFoundError. Kazdy inny wyjatek (np. uszkodzony
        # .pyc w bibliotece standardowej przy imporcie huggingface_hub) tez konczy
        # sie "model nie jest pobrany", wiec bez tego wpisu przyczyna przepada.
        if type(blad).__name__ != "LocalEntryNotFoundError":
            log.warning("Sprawdzenie modelu %r nie powiodlo sie: %r", repo_id, blad)
        return False


def dostepne_urzadzenia() -> list[str]:
    """Lista urzadzen OpenVINO, np. ['CPU', 'GPU', 'NPU']."""
    try:
        import openvino as ov

        return list(ov.Core().available_devices)
    except Exception as blad:
        log.warning("Nie udalo sie odpytac OpenVINO o urzadzenia: %s", blad)
        return ["CPU"]


def _klasa_pipeline():
    import openvino_genai as ov_genai

    # Nazwa klasy zmieniala sie miedzy wydaniami OpenVINO GenAI.
    klasa = getattr(ov_genai, "WhisperPipeline", None)
    if klasa is None:
        klasa = getattr(ov_genai, "ASRPipeline", None)
    if klasa is None:
        raise RuntimeError(
            "Ta wersja openvino-genai nie udostepnia ani WhisperPipeline, ani ASRPipeline."
        )
    return klasa


def wersja_genai() -> str:
    try:
        import openvino_genai

        return str(getattr(openvino_genai, "__version__", "?"))
    except Exception:
        return "?"


class Silnik:
    """Leniwie budowany pipeline z automatycznym zejsciem na CPU."""

    def __init__(self, repo_id: str, device: str = "GPU"):
        self.repo_id = repo_id
        self.zadany_device = device
        self.device = device
        self._pipe = None
        self._lang_to_id: dict[str, int] | None = None

    def zaladuj(self) -> None:
        if self._pipe is not None:
            return

        katalog = str(sciezka_modelu(self.repo_id, pobierz=False))
        klasa = _klasa_pipeline()
        cache = str(config.KATALOG_CACHE_OV)
        config.KATALOG_CACHE_OV.mkdir(parents=True, exist_ok=True)

        kandydaci = [self.zadany_device]
        if self.zadany_device != "CPU":
            kandydaci.append("CPU")

        ostatni_blad: Exception | None = None
        for device in kandydaci:
            for kwargs in ({"CACHE_DIR": cache}, {}):
                try:
                    start = time.monotonic()
                    self._pipe = klasa(katalog, device, **kwargs)
                    self.device = device
                    self._lang_to_id = self._zawezone_jezyki()
                    log.info(
                        "Pipeline gotowy na %s w %.1fs (cache=%s)",
                        device, time.monotonic() - start, bool(kwargs),
                    )
                    return
                except Exception as blad:
                    ostatni_blad = blad
                    log.warning("Nie udalo sie zbudowac pipeline na %s: %s", device, blad)

        raise RuntimeError(
            f"Nie udalo sie zaladowac modelu na zadnym urzadzeniu: {ostatni_blad}"
        )

    def _zawezone_jezyki(self) -> dict[str, int] | None:
        """`lang_to_id` modelu ograniczone do JEZYKI_AUTO.

        None, gdy nie da sie tego odczytac - wtedy zostaje autodetekcja po
        wszystkich jezykach, czyli zachowanie sprzed tej zmiany.
        """
        try:
            pelne = dict(self._pipe.get_generation_config().lang_to_id)
        except Exception as blad:
            log.warning("Nie udalo sie odczytac lang_to_id: %s", blad)
            return None

        zawezone = {token: pelne[token] for token in JEZYKI_AUTO if token in pelne}
        if len(zawezone) < 2:
            log.warning(
                "Model zna tylko %d z %d jezykow autodetekcji - nie zawezam.",
                len(zawezone), len(JEZYKI_AUTO),
            )
            return None

        log.info("Autodetekcja jezyka zawezona do: %s", ", ".join(zawezone))
        return zawezone

    def zwolnij(self) -> None:
        """Oddaje pamiec GPU zajeta przez model.

        Po transkrypcji ruszaja podsumowania na modelu jezykowym - nie ma sensu
        trzymac wtedy jeszcze Whispera na tej samej zintegrowanej karcie.
        """
        if self._pipe is None:
            return
        self._pipe = None
        gc.collect()
        log.info("Pipeline Whisper zwolniony")

    def generuj(self, audio: np.ndarray, jezyk: str | None):
        kwargs = {"task": "transcribe", "return_timestamps": True}
        if jezyk:
            kwargs["language"] = jezyk
        elif self._lang_to_id:
            kwargs["lang_to_id"] = self._lang_to_id

        try:
            return self._wywolaj(audio, kwargs)
        except TypeError:
            if "lang_to_id" not in kwargs:
                raise
            # Starsze wydanie GenAI nie zna tego parametru - lecimy dalej bez
            # zawezenia, zamiast wywracac cala transkrypcje.
            log.warning("openvino-genai nie przyjmuje lang_to_id - autodetekcja bez zawezenia")
            self._lang_to_id = None
            del kwargs["lang_to_id"]
            return self._wywolaj(audio, kwargs)

    def _wywolaj(self, audio: np.ndarray, kwargs: dict):
        # Nowsze wydania przyjmuja ndarray, starsze wymagaja listy floatow.
        try:
            return self._pipe.generate(audio, **kwargs)
        except TypeError:
            return self._pipe.generate(audio.tolist(), **kwargs)


# --- ciecie audio ---------------------------------------------------------


def _najcichszy_punkt(audio: np.ndarray, od: int, do: int) -> int:
    """Indeks najcichszego miejsca w zadanym zakresie - tam tniemy."""
    od = max(0, od)
    do = min(len(audio), do)
    if do - od < CZESTOTLIWOSC:
        return do

    ramka = int(RAMKA_ANALIZY_S * CZESTOTLIWOSC)
    fragment = audio[od:do]
    liczba = len(fragment) // ramka
    if liczba < 5:
        return (od + do) // 2

    energia = np.square(fragment[: liczba * ramka].reshape(liczba, ramka)).mean(axis=1)
    # Wygladzenie oknem 0,5 s - szukamy realnej pauzy, nie pojedynczej ramki.
    jadro = np.ones(5, dtype=np.float32) / 5.0
    gladka = np.convolve(energia, jadro, mode="same")
    return od + int(np.argmin(gladka)) * ramka + ramka // 2


def szczytowe_rms(fragment: np.ndarray) -> float:
    """Najglosniejsze okno 100 ms we fragmencie.

    Szczyt, a nie srednia - inaczej jedno krotkie zdanie w dlugim, cichym
    fragmencie zostaloby uznane za cisze i pominiete.
    """
    if fragment.size == 0:
        return 0.0
    ramka = int(RAMKA_ANALIZY_S * CZESTOTLIWOSC)
    liczba = fragment.size // ramka
    if liczba < 1:
        return float(np.sqrt(np.mean(np.square(fragment))))
    okna = np.sqrt(
        np.square(fragment[: liczba * ramka].reshape(liczba, ramka)).mean(axis=1)
    )
    return float(okna.max())


def _granice_ciszy(audio: np.ndarray) -> list[int]:
    """Indeksy srodkow dostatecznie dlugich pauz - kandydaci na ciecie."""
    ramka = int(RAMKA_VAD_S * CZESTOTLIWOSC)
    liczba = len(audio) // ramka
    if liczba < 2:
        return []

    energia = np.sqrt(
        np.square(audio[: liczba * ramka].reshape(liczba, ramka)).mean(axis=1)
    )
    prog = max(float(energia.max()) * PROG_CISZY_WZGLEDNY, 1e-4)
    cicha = energia < prog
    min_ramek = max(1, int(MIN_CISZA_S / RAMKA_VAD_S))

    granice: list[int] = []
    i = 0
    while i < liczba:
        if not cicha[i]:
            i += 1
            continue
        j = i
        while j < liczba and cicha[j]:
            j += 1
        if j - i >= min_ramek:
            granice.append((i + j) // 2 * ramka)
        i = j
    return granice


def skroc_pauzy(audio: np.ndarray, maks_pauza_s: float = 0.6,
                min_pauza_s: float = 1.0) -> np.ndarray:
    """Wycina cisze z poczatku i konca, a pauzy dluzsze niz `min_pauza_s` skraca.

    Do dyktowania, gdzie cale nagranie idzie do modelu jednym wywolaniem.
    Whisper na dluzszej ciszy zmysla tekst (w polskim zwykle "Dziekuje." albo
    "Dziekuje za uwage." na koncu), a po pauzie potrafi zgubic albo przekrecic
    poczatek kolejnej wypowiedzi. Zmierzone na mowie z 10-sekundowymi pauzami:
    bez skracania na koncu wyskakiwalo "Dziekuje.", a "Jasne" i "time-line"
    znikaly; po skroceniu tekst byl kompletny i bez dopiskow.
    """
    ramka = int(RAMKA_VAD_S * CZESTOTLIWOSC)
    liczba = len(audio) // ramka
    if liczba < 4:
        return audio

    energia = np.sqrt(
        np.square(audio[: liczba * ramka].reshape(liczba, ramka)).mean(axis=1)
    )
    # Prog wzgledem glosnej mowy (95. percentyl), a nie maksimum - pojedyncze
    # kaszlniecie albo stukniecie nie podnosi go wtedy ponad cicha mowe.
    prog = max(float(np.percentile(energia, 95)) * PROG_CISZY_WZGLEDNY, 1e-4)
    cicha = energia < prog
    min_ramek = int(min_pauza_s / RAMKA_VAD_S)
    zostaw_ramek = int(maks_pauza_s / RAMKA_VAD_S)

    zostaw = np.ones(liczba, dtype=bool)
    i = 0
    while i < liczba:
        if not cicha[i]:
            i += 1
            continue
        j = i
        while j < liczba and cicha[j]:
            j += 1
        brzeg = i == 0 or j == liczba
        if brzeg or j - i >= min_ramek:
            zachowaj = 0 if brzeg else zostaw_ramek
            polowa = zachowaj // 2
            zostaw[i + polowa: j - (zachowaj - polowa)] = False
        i = j

    if zostaw.all():
        return audio
    mowa = audio[: liczba * ramka][np.repeat(zostaw, ramka)]
    if mowa.size == 0:
        return mowa
    # Krotki margines, zeby nie uciac pierwszej ani ostatniej gloski.
    margines = np.zeros(int(0.2 * CZESTOTLIWOSC), dtype=np.float32)
    return np.ascontiguousarray(np.concatenate([margines, mowa, margines]),
                                dtype=np.float32)


def podziel_po_ciszy(audio: np.ndarray, maks_s: float) -> list[tuple[int, int]]:
    """Tnie na fragmenty nie dluzsze niz `maks_s`, trzymajac sie granic w ciszy."""
    maks = int(maks_s * CZESTOTLIWOSC)
    zakresy: list[tuple[int, int]] = []
    start = 0

    for granica in _granice_ciszy(audio) + [len(audio)]:
        if granica <= start:
            continue
        # Zbyt krotki kawalek doklejamy do nastepnego - inaczej rozdrobnilibysmy
        # wypowiedzi na strzepy i model stracilby kontekst zdania.
        if granica - start < maks * 0.4 and granica != len(audio):
            continue
        while granica - start > maks:
            zakresy.append((start, start + maks))
            start += maks
        if granica > start:
            zakresy.append((start, granica))
        start = granica

    return [(a, b) for a, b in zakresy if b - a > CZESTOTLIWOSC // 4]


def podziel_na_segmenty(audio: np.ndarray, autodetekcja: bool = False
                        ) -> list[tuple[int, int]]:
    """Dobiera strategie ciecia do trybu jezykowego."""
    if autodetekcja:
        zakresy = podziel_po_ciszy(audio, DLUGOSC_SEGMENTU_AUTO_S)
        return zakresy or [(0, len(audio))]

    dlugosc_docelowa = int(DLUGOSC_SEGMENTU_S * CZESTOTLIWOSC)
    okno = int(OKNO_SZUKANIA_CISZY_S * CZESTOTLIWOSC)

    zakresy = []
    poczatek = 0
    while poczatek < len(audio):
        cel = poczatek + dlugosc_docelowa
        if cel >= len(audio) - okno:
            zakresy.append((poczatek, len(audio)))
            break
        koniec = _najcichszy_punkt(audio, cel - okno, cel + okno)
        if koniec <= poczatek:
            koniec = min(len(audio), cel)
        zakresy.append((poczatek, koniec))
        poczatek = koniec
    return zakresy


def _chunki(wynik) -> list:
    """Normalizuje wynik pipeline - `chunks` bywa lista albo lista list."""
    chunki = getattr(wynik, "chunks", None)
    if not chunki:
        return []
    pierwszy = chunki[0]
    if isinstance(pierwszy, (list, tuple)):
        return list(pierwszy)
    return list(chunki)


# --- glowna funkcja -------------------------------------------------------


def transkrybuj_plik(
    plik: Path,
    silnik: Silnik,
    mowca: str,
    jezyk: str | None = None,
    postep: PostepFn | None = None,
    anuluj: threading.Event | None = None,
    waga: tuple[float, float] = (0.0, 1.0),
) -> list[Segment]:
    """Transkrybuje jeden plik WAV/FLAC 16 kHz mono.

    `waga` to zakres (od, do) globalnego paska postepu, jaki zajmuje ten plik -
    dzieki temu dwie sciezki moga raportowac wspolny postep 0..1.
    """
    audio, sr = sf.read(str(plik), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != CZESTOTLIWOSC:
        import soxr

        audio = soxr.resample(audio, sr, CZESTOTLIWOSC, quality="HQ")
    audio = np.ascontiguousarray(audio, dtype=np.float32)

    if audio.size < CZESTOTLIWOSC // 2:
        return []

    # Cala sciezka to cisza (np. mikrofon, gdy tylko sluchales) - nie ma czego
    # transkrybowac, a model na ciszy tylko halucynuje.
    if szczytowe_rms(audio) < PROG_MOWY_RMS:
        log.info("Sciezka %s pominieta - sama cisza", plik.name)
        if postep:
            postep(waga[1], "")
        return []

    silnik.zaladuj()
    zakresy = podziel_na_segmenty(audio, autodetekcja=jezyk is None)
    od_wagi, do_wagi = waga
    rozpietosc = max(0.0, do_wagi - od_wagi)

    segmenty: list[Segment] = []
    for i, (poczatek, koniec) in enumerate(zakresy):
        if anuluj is not None and anuluj.is_set():
            break

        offset = poczatek / CZESTOTLIWOSC
        fragment = audio[poczatek:koniec]

        if szczytowe_rms(fragment) < PROG_MOWY_RMS:
            if postep:
                postep(od_wagi + rozpietosc * (i + 1) / len(zakresy), "")
            continue

        try:
            wynik = silnik.generuj(fragment, jezyk)
        except Exception as blad:
            log.exception("Blad transkrypcji segmentu %d pliku %s", i, plik.name)
            if postep:
                postep(od_wagi + rozpietosc * (i + 1) / len(zakresy),
                       f"[blad segmentu {i + 1}: {blad}]")
            continue

        koniec_s = koniec / CZESTOTLIWOSC
        chunki = _chunki(wynik)
        if chunki:
            ostatni_koniec = offset
            for chunk in chunki:
                start = float(getattr(chunk, "start_ts", -1.0))
                kon = float(getattr(chunk, "end_ts", -1.0))
                tekst = str(getattr(chunk, "text", "")).strip()
                if not tekst:
                    continue
                # Whisper bywa zwraca -1, gdy nie zna znacznika czasu.
                start = ostatni_koniec if start < 0 else offset + start
                kon = start + 2.0 if kon < 0 else offset + kon
                # Na ciszy model potrafi zwrocic znacznik wykraczajacy poza
                # przetwarzany fragment - przycinamy do jego granic.
                start = min(max(start, offset), koniec_s)
                kon = min(max(kon, start), koniec_s)
                ostatni_koniec = kon
                segmenty.append(Segment(start, kon, tekst, mowca))
        else:
            tekst = str(wynik).strip()
            if tekst:
                segmenty.append(
                    Segment(offset, koniec / CZESTOTLIWOSC, tekst, mowca)
                )

        if postep:
            ostatni_tekst = segmenty[-1].tekst if segmenty else ""
            postep(od_wagi + rozpietosc * (i + 1) / len(zakresy), ostatni_tekst)

    return segmenty


def transkrybuj_sesje(
    pliki: dict[str, Path],
    model: str,
    device: str,
    jezyk_kod: str = "auto",
    postep: PostepFn | None = None,
    anuluj: threading.Event | None = None,
    status: Callable[[str], None] | None = None,
) -> tuple[list[Segment], str]:
    """Transkrybuje wszystkie sciezki sesji. Zwraca (segmenty, uzyty_device)."""
    jezyk = config.JEZYKI.get(jezyk_kod)
    silnik = Silnik(model, device)

    etykiety = {"mikrofon": t(MOWCA_JA), "system": t(MOWCA_SPOTKANIE)}
    do_zrobienia = [(n, p) for n, p in pliki.items() if p and Path(p).exists()]
    if not do_zrobienia:
        return [], device

    # Pierwsze zaladowanie na GPU potrafi trwac kilkadziesiat sekund (kompilacja
    # modelu), wiec informujemy o tym zanim ruszy pierwszy segment.
    if status:
        status(t("Ładuję model na {device}...", device=device))
    silnik.zaladuj()
    if status:
        status(t("Transkrybuję na {device}...", device=silnik.device))

    krok = 1.0 / len(do_zrobienia)
    wyniki: list[list[Segment]] = []
    try:
        for i, (nazwa, plik) in enumerate(do_zrobienia):
            if anuluj is not None and anuluj.is_set():
                break
            wyniki.append(
                transkrybuj_plik(
                    Path(plik), silnik, etykiety.get(nazwa, nazwa), jezyk,
                    postep=postep, anuluj=anuluj,
                    waga=(i * krok, (i + 1) * krok),
                )
            )
    finally:
        uzyty_device = silnik.device
        silnik.zwolnij()

    return scal(*wyniki), uzyty_device
