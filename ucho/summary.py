# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Lokalne podsumowanie transkryptu malym modelem jezykowym.

Ten sam runtime co Whisper - OpenVINO GenAI na tej samej zintegrowanej karcie -
wiec nie ma tu Ollamy, serwera HTTP ani procesu w tle. Model laduje sie dopiero
po zakonczonej transkrypcji i jest zwalniany natychmiast po wygenerowaniu
podsumowania (`SilnikLLM.zwolnij`), zeby nie trzymac ~2 GB pamieci przez cale
zycie aplikacji. Sieci nie dotykamy: model czytamy z `modele\\hub` przez
`transcribe.sciezka_modelu(..., pobierz=False)`.

Model zwraca JSON wymuszony schematem (`StructuredOutputConfig`), a nie zwykly
tekst do sparsowania - dzieki temu nazwa folderu i podsumowanie nie zaleza od
tego, czy maly model akurat trafil w format.
"""

from __future__ import annotations

import difflib
import gc
import json
import logging
import re
import threading
import time
import unicodedata
from typing import Callable

from . import config, transcribe
from .i18n import t
from .merge import Segment, czas_hms, jako_tekst, slug

log = logging.getLogger(__name__)

# Ile znakow transkryptu wchodzi w jedno przejscie modelu. Godzinne spotkanie to
# ok. 50-80 kB tekstu, czyli znacznie wiecej niz warto wpychac naraz w model 4B
# na iGPU - dlatego dluzsze transkrypty lecą map-reduce.
#
# Prog dobrany tak, zeby typowe spotkanie (do ~40 min, ok. 24 kB) zmiescilo sie
# w JEDNYM przejsciu. Map-reduce gubi ustalenia negocjowane przez cala rozmowe:
# w tescie model wziął pierwszy zaproponowany termin ("wtorek 29") i nie zobaczyl,
# ze pol godziny pozniej przesuniety zostal na srode. Streszczenie streszczen
# nie ma z czego tego odtworzyc - fragment o wtorku i fragment o srodzie sa
# osobno. 24 kB to ok. 8 tys. tokenow, czyli wciaz z zapasem w oknie Qwena.
MAKS_ZNAKOW_FRAGMENTU = 24_000
MAKS_FRAGMENTOW = 8

# Fragment zwraca teraz trzy listy zamiast dwoch (doszly ustalenia), wiec 320
# tokenow potrafilo urwac odpowiedz w polowie ostatniego ustalenia.
MAKS_TOKENOW_FRAGMENT = 560
MAKS_TOKENOW_FINAL = 700

MAKS_UCZESTNIKOW = 6
# Prog podobienstwa imienia do slowa z transkryptu - patrz `_pada_w_tekscie`.
PODOBIENSTWO_IMIENIA = 0.8
# Powyzej tego dwa punkty uznajemy za to samo zdanie - patrz `_zdania`.
PODOBIENSTWO_ZDANIA = 0.75
MAKS_PUNKTOW = 5
MAKS_USTALEN = 6
MAKS_ZNAKOW_TEMATU = 60

# Etykiety sciezek audio - model lubi je brac za imiona, wiec odsiewamy je twardo.
NIE_IMIONA = {
    "ja", "spotkanie", "uczestnik", "uczestnicy", "rozmowca", "rozmowcy",
    "mowca", "prowadzacy", "nieznany", "brak", "wszyscy", "gosc", "user",
    # Funkcje i role - model podaje je zamiast imienia, gdy imie nie padlo.
    "dyrektor", "dyrektorka", "prezes", "kierownik", "kierowniczka", "szef",
    "szefowa", "klient", "klientka", "zespol", "zespół", "zarzad", "zarząd",
    "administrator", "moderator", "asystent", "asystentka",
}

# Bez tego ta sama osoba trafia na liste dwa razy: raz jako "Pan Marek",
# raz jako "Marek".
_GRZECZNOSCIOWE = re.compile(
    r"^(pan|pani|panie|pana|panu|panią|pania|dr|prof|inż|inz|mgr)\.?\s+",
    re.IGNORECASE,
)

StatusFn = Callable[[str], None]


SCHEMAT_FRAGMENTU = {
    "type": "object",
    "properties": {
        "punkty": {"type": "array", "items": {"type": "string"}},
        "ustalenia": {"type": "array", "items": {"type": "string"}},
        "uczestnicy": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["punkty", "ustalenia", "uczestnicy"],
    "additionalProperties": False,
}

SCHEMAT_PODSUMOWANIA = {
    "type": "object",
    "properties": {
        "temat": {"type": "string"},
        "uczestnicy": {"type": "array", "items": {"type": "string"}},
        "tldr": {"type": "string"},
        "punkty": {"type": "array", "items": {"type": "string"}},
        "ustalenia": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["temat", "uczestnicy", "tldr", "punkty", "ustalenia"],
    "additionalProperties": False,
}

SYSTEM = (
    "Jesteś asystentem, który robi notatki ze spotkań biznesowych po polsku. "
    "Czytasz automatyczny transkrypt rozmowy i wyciągasz z niego to, do czego "
    "ktoś wróci za tydzień: co ustalono, kto co robi i na kiedy.\n"
    "Zasady, których trzymasz się bezwzględnie:\n"
    "1. KONKRETY SĄ NAJWAŻNIEJSZE. Daty, dni tygodnia, godziny, liczby, kwoty, "
    "nazwy firm, systemów i stanowisk przepisujesz dokładnie tak, jak padły. "
    "Nigdy nie zamieniasz ich na ogólniki typu „ustalono termin”. Ale też niczego "
    "do nich nie dokładasz: jeśli padł sam dzień tygodnia i numer dnia, piszesz "
    "tylko tyle. Nie dopisujesz miesiąca ani roku, których nikt nie wymówił.\n"
    "2. Opierasz się wyłącznie na transkrypcie — niczego nie zmyślasz i nie "
    "dopowiadasz. Czego nie ma w tekście, tego nie ma w notatce.\n"
    "3. Pomijasz small talk sprzed i po spotkaniu: testy mikrofonu, „czy mnie "
    "słychać”, problemy z kamerą, pogodę, dygresje prywatne.\n"
    "4. Transkrypt jest automatyczny, więc bywa przekręcony. Fragmenty, których "
    "nie rozumiesz, po prostu pomijasz — nie zgadujesz, co mogły znaczyć.\n"
    "5. Ustalenia bywają negocjowane: pada jeden termin, potem drugi. Liczy się "
    "WERSJA OSTATECZNA, ta z końca rozmowy — nie pierwsza propozycja.\n"
    "Odpowiadasz zawsze samym poprawnym JSON-em, bez komentarza."
)

_NOTA_ETYKIET = (
    'Etykiety "Ja:" i "Spotkanie:" oznaczają źródło dźwięku (mikrofon i głośniki), '
    "a NIE imiona — nigdy nie traktuj ich jako uczestników."
)

PROMPT_FRAGMENTU = """Fragment {numer} z {razem} transkryptu spotkania. {nota}

Zwróć JSON o polach:
- "punkty": do 3 najważniejszych rzeczy merytorycznych z tego fragmentu, każda jako jedno krótkie zdanie po polsku. Pomiń small talk i sprawy techniczne połączenia.
- "ustalenia": wszystko, co w tym fragmencie zostało UZGODNIONE lub komuś zlecone — spotkania i terminy (dzień tygodnia, data, godzina, czas trwania), zadania (kto i co robi), zobowiązania („podeślemy”, „przygotujemy”, „odezwę się”). Przepisz daty, godziny i liczby dokładnie tak, jak padły. Jeśli w tym fragmencie nic nie ustalono — pusta lista.
- "uczestnicy": imiona, które PADŁY w tym fragmencie (powitanie, zwrot do kogoś, przedstawienie się). Jeśli żadne imię nie padło — pusta lista. Nie zgaduj.

Fragment:
---
{material}
---"""

PROMPT_PODSUMOWANIA = """Poniżej {zrodlo} spotkania. {nota}

Zwróć JSON o polach:
- "temat": 2-5 słów nazywających to spotkanie, po polsku, bez daty. Ma opisywać po co się spotkano, a nie firmę wspomnianą po drodze.
- "uczestnicy": imiona osób, które PADŁY w rozmowie. Jeśli żadne imię nie padło albo nie masz pewności — pusta lista. Nie zgaduj i nie wymyślaj.
- "tldr": 2-3 zdania streszczenia całości. Pierwsze zdanie mówi, po co było to spotkanie, ostatnie — czym się skończyło.
- "punkty": do 5 najważniejszych tematów, każdy jako jedno krótkie zdanie. Każdy o czymś INNYM — nie powtarzaj tej samej myśli w kilku punktach. Bez small talku i spraw technicznych połączenia.
- "ustalenia": konkretne decyzje, terminy i zadania (kto, co, na kiedy). Zachowaj daty, dni tygodnia, godziny i liczby dokładnie tak, jak padły — to najważniejsza część notatki. Jeśli nic nie ustalono — pusta lista.

{material}"""


# --- model ----------------------------------------------------------------


class SilnikLLM:
    """Leniwie budowany LLMPipeline z automatycznym zejsciem na CPU.

    Swiadomie lustrzany wobec `transcribe.Silnik` - ta sama kolejnosc prob
    (zadane urzadzenie, potem CPU; z cache kompilacji, potem bez).
    """

    def __init__(self, repo_id: str, device: str = "GPU"):
        self.repo_id = repo_id
        self.zadany_device = device
        self.device = device
        self._pipe = None

    def zaladuj(self) -> None:
        if self._pipe is not None:
            return

        import openvino_genai as ov_genai

        katalog = str(transcribe.sciezka_modelu(self.repo_id, pobierz=False))
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
                    self._pipe = ov_genai.LLMPipeline(katalog, device, **kwargs)
                    self.device = device
                    log.info(
                        "LLM gotowy na %s w %.1fs (%s, cache=%s)",
                        device, time.monotonic() - start, self.repo_id, bool(kwargs),
                    )
                    return
                except Exception as blad:
                    ostatni_blad = blad
                    log.warning("Nie udalo sie zbudowac LLM na %s: %s", device, blad)

        raise RuntimeError(f"Nie udalo sie zaladowac modelu jezykowego: {ostatni_blad}")

    def zwolnij(self) -> None:
        """Oddaje pamiec zajeta przez model - to jest to 'wylaczenie po uzyciu'."""
        if self._pipe is None:
            return
        self._pipe = None
        gc.collect()
        log.info("Pipeline LLM zwolniony")

    def _prompt(self, uzytkownik: str) -> str:
        """Sklada prompt w formacie oczekiwanym przez model.

        Qwen3 domyslnie wlacza tryb rozumowania i marnuje setki tokenow na blok
        <think>. Wylaczamy go przez `extra_context`, a gdyby ta wersja szablonu
        go nie znala - skladamy ChatML recznie z pustym blokiem <think>, dokladnie
        tak, jak robi to szablon Qwen3 przy enable_thinking=False.
        """
        historia = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": uzytkownik},
        ]
        tokenizer = self._pipe.get_tokenizer()
        for kwargs in ({"extra_context": {"enable_thinking": False}}, {}):
            try:
                prompt = tokenizer.apply_chat_template(historia, True, **kwargs)
                if prompt:
                    return prompt
            except Exception as blad:
                log.debug("apply_chat_template(%s) nie zadzialal: %s", kwargs, blad)

        prompt = (
            f"<|im_start|>system\n{SYSTEM}<|im_end|>\n"
            f"<|im_start|>user\n{uzytkownik}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        if "qwen3" in self.repo_id.lower():
            prompt += "<think>\n\n</think>\n\n"
        return prompt

    def generuj_json(self, uzytkownik: str, schemat: dict, maks_tokenow: int,
                     anuluj: threading.Event | None = None) -> dict:
        """Jedno przejscie modelu z wymuszonym schematem JSON.

        `anuluj` sprawdzamy przez streamer, czyli po kazdym tokenie - inaczej
        przycisk "Przerwij" nie robilby nic przez kilkanascie sekund generacji.
        """
        import openvino_genai as ov_genai

        prompt = self._prompt(uzytkownik)

        streamer = None
        if anuluj is not None:
            def streamer(_fragment: str):
                return (ov_genai.StreamingStatus.CANCEL if anuluj.is_set()
                        else ov_genai.StreamingStatus.RUNNING)

        for wymuszaj in (True, False):
            konfiguracja = ov_genai.GenerationConfig()
            konfiguracja.max_new_tokens = maks_tokenow
            konfiguracja.do_sample = False
            if wymuszaj:
                try:
                    konfiguracja.structured_output_config = ov_genai.StructuredOutputConfig(
                        json_schema=json.dumps(schemat)
                    )
                except Exception as blad:
                    log.warning("Wymuszony JSON niedostepny: %s", blad)
                    continue
            try:
                wynik = self._pipe.generate(prompt, konfiguracja, streamer)
            except Exception as blad:
                # Grammar-constrained decoding potrafi nie wystartowac na GPU -
                # wtedy druga iteracja leci zwyklym dekodowaniem.
                log.warning("Generacja (wymuszony JSON=%s) nie powiodla sie: %s",
                            wymuszaj, blad)
                continue

            if anuluj is not None and anuluj.is_set():
                return {}

            dane = _wczytaj_json(_bez_rozumowania(str(wynik)))
            if dane is not None:
                return dane
            log.warning("Model nie zwrocil poprawnego JSON (wymuszony=%s)", wymuszaj)

        return {}


# --- tekst ----------------------------------------------------------------


def _bez_rozumowania(tekst: str) -> str:
    """Usuwa bloki <think>...</think>, takze niedomkniete."""
    tekst = re.sub(r"<think>.*?</think>", " ", tekst, flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in tekst:
        tekst = tekst.rsplit("</think>", 1)[1]
    return tekst.replace("<think>", " ").strip()


def _wczytaj_json(tekst: str) -> dict | None:
    """Parsuje odpowiedz modelu; przy nadmiarowym tekscie wycina pierwszy obiekt."""
    tekst = tekst.strip()
    if tekst.startswith("```"):
        tekst = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", tekst).strip()
    try:
        dane = json.loads(tekst)
    except ValueError:
        dopasowanie = re.search(r"\{.*\}", tekst, re.DOTALL)
        if not dopasowanie:
            return None
        try:
            dane = json.loads(dopasowanie.group(0))
        except ValueError:
            return None
    return dane if isinstance(dane, dict) else None


def _fragmenty(tekst: str) -> list[str]:
    """Tnie transkrypt na kawalki po calych wypowiedziach.

    Przy bardzo dlugich spotkaniach bierzemy rownomierna probke fragmentow -
    lepiej streszczic osiem kawalkow z calego spotkania niz przemielic wszystko
    i kazac uzytkownikowi czekac kilkanascie minut.
    """
    kawalki: list[str] = []
    biezacy: list[str] = []
    dlugosc = 0

    for linia in tekst.splitlines():
        if biezacy and dlugosc + len(linia) > MAKS_ZNAKOW_FRAGMENTU:
            kawalki.append("\n".join(biezacy))
            biezacy, dlugosc = [], 0
        biezacy.append(linia)
        dlugosc += len(linia) + 1

    if biezacy:
        kawalki.append("\n".join(biezacy))
    if not kawalki:
        return []
    if len(kawalki) <= MAKS_FRAGMENTOW:
        return kawalki

    krok = len(kawalki) / MAKS_FRAGMENTOW
    return [kawalki[min(int(i * krok), len(kawalki) - 1)] for i in range(MAKS_FRAGMENTOW)]


# --- porzadkowanie odpowiedzi ---------------------------------------------


def _zdania(wartosc, limit: int) -> list[str]:
    """Porzadkuje liste zdan od modelu, odsiewajac powtorzenia.

    Nie tylko identyczne: maly model potrafi wypelnic liste piecioma wariantami
    tego samego zdania ("Firma chce zarejestrować wizytę, by zaprezentować
    rozwiązanie" / "...by omówić zastosowanie"). Lepiej oddac trzy rozne punkty
    niz piec razy jeden.
    """
    wynik: list[str] = []
    if isinstance(wartosc, str):
        wartosc = [wartosc]
    if not isinstance(wartosc, (list, tuple)):
        return wynik
    for element in wartosc:
        tekst = " ".join(str(element).split()).strip(" -•*")
        if not tekst:
            continue
        if any(difflib.SequenceMatcher(None, tekst.lower(), juz.lower()).ratio()
               >= PODOBIENSTWO_ZDANIA for juz in wynik):
            continue
        wynik.append(tekst)
        if len(wynik) >= limit:
            break
    return wynik


def _uproszczone(tekst: str) -> str:
    """Male litery bez ogonkow - do porownywania imion z trescia transkryptu."""
    return unicodedata.normalize("NFKD", tekst.lower()).encode("ascii", "ignore").decode()


def _slowa(tekst: str) -> set[str]:
    """Zbior slow transkryptu w postaci porownywalnej z imionami."""
    return set(re.findall(r"[a-z]+", _uproszczone(tekst)))


def _slowa_dokladne(tekst: str) -> set[str]:
    """Jak `_slowa`, ale z zachowanymi ogonkami.

    Do nazw miesiecy ogonki sa konieczne: bez nich "mają" zlewa sie z "maja",
    czyli kazde spotkanie, na ktorym ktos czegos "ma", potwierdzaloby maj.
    """
    return set(re.findall(r"[a-ząćęłńóśźż]+", tekst.lower()))


# Model lubi dopowiedziec miesiac tam, gdzie padl sam numer dnia - w tescie
# z "wtorek 29" zrobil "29 lutego", a potem "29 marca". Zadna instrukcja
# w promptcie tego nie zatrzymala, wiec wycinamy je po fakcie: miesiac zostaje
# tylko wtedy, gdy naprawde padl w rozmowie.
MIESIACE = {
    "styczeń": ("styczeń", "stycznia", "styczniu"),
    "luty": ("luty", "lutego", "lutym"),
    "marzec": ("marzec", "marca", "marcu"),
    "kwiecień": ("kwiecień", "kwietnia", "kwietniu"),
    "maj": ("maj", "maja", "maju"),
    "czerwiec": ("czerwiec", "czerwca", "czerwcu"),
    "lipiec": ("lipiec", "lipca", "lipcu"),
    "sierpień": ("sierpień", "sierpnia", "sierpniu"),
    "wrzesień": ("wrzesień", "września", "wrześniu"),
    "październik": ("październik", "października", "październiku"),
    "listopad": ("listopad", "listopada", "listopadzie"),
    "grudzień": ("grudzień", "grudnia", "grudniu"),
}


# Data liczbowa z miesiacem: "29.02", "29.02.2026", "29/02". Drugi czlon musi
# byc poprawnym miesiacem (01-12), zeby nie pomylic daty z godzina - "12.00"
# ma tam "00", a "13.30" ma "30", wiec obie zostaja nietkniete.
_DATA_LICZBOWA = re.compile(r"\b(\d{1,2})[./](0?[1-9]|1[0-2])(?:[./](\d{2,4}))?\b")


def _bez_zmyslonych_dat(zdanie: str, slowa: set[str], transkrypt: str) -> str:
    """Usuwa ze zdania czesci daty, ktore nie padly w transkrypcie.

    Nazwy miesiecy znikaja w calosci, a z dat liczbowych zostaje sam dzien.
    Zaden prompt tego nie zalatwil: 4B robil z "wtorek 29" najpierw "29 lutego",
    potem "29 marca", a 8B - "29.02". Lepiej oddac niepelna date niz zmyslona.
    """
    obecne = set(re.findall(r"[a-ząćęłńóśźż]+", zdanie.lower()))
    for formy in MIESIACE.values():
        if not (obecne & set(formy)) or (slowa & set(formy)):
            continue
        wzor = re.compile(r"\s*\b(?:%s)\b" % "|".join(formy), re.IGNORECASE)
        zdanie = wzor.sub("", zdanie)
        log.info("Usuwam zmyslony miesiac %r", formy[0])

    def sam_dzien(dopasowanie: re.Match) -> str:
        if dopasowanie.group(0) in transkrypt:
            return dopasowanie.group(0)
        log.info("Zmyslona data liczbowa %r - zostawiam sam dzien",
                 dopasowanie.group(0))
        return dopasowanie.group(1)

    zdanie = _DATA_LICZBOWA.sub(sam_dzien, zdanie)
    return " ".join(zdanie.split())


def _pada_w_tekscie(imie: str, slowa: set[str]) -> bool:
    """Czy imie faktycznie pada w transkrypcie (z tolerancja na odmiane).

    Model potrafi dopisac osobe, ktorej nie bylo - w tescie wymyslil "Teresę"
    z przekreconego "Teros?". Sprawdzamy wiec, czy w transkrypcie stoi slowo
    dostatecznie podobne do imienia.

    Porownujemy CALE slowa, nie podlancuchy: "Teresa" siedzi w srodku
    "zainteresowani", a "Robert" w "robimy". I z tolerancja na odmiane, bo
    polskie imiona maja jeszcze e ruchome - "Paweł" pada w nagraniu wylacznie
    jako "Pawle" i "Pawła", nigdy w mianowniku. Prog 0,8 zmierzony na
    transkrypcie spotkania: 6/6 imion obecnych trafionych, 0/8 nieobecnych
    przepuszczonych.
    """
    rdzen = _uproszczone(imie).split()[0]
    if len(rdzen) < 3:
        return False
    kandydaci = [slowo for slowo in slowa if slowo[:3] == rdzen[:3]]
    return bool(difflib.get_close_matches(rdzen, kandydaci, n=1,
                                          cutoff=PODOBIENSTWO_IMIENIA))


def _imiona(wartosc, transkrypt: str | None = None) -> list[str]:
    """Zostawia tylko to, co wyglada na imie - reszte modelu odrzucamy.

    Gdy podasz `transkrypt`, imiona sa dodatkowo sprawdzane pod katem tego, czy
    w ogole w nim padly.
    """
    wynik: list[str] = []
    widziane: set[str] = set()
    if not isinstance(wartosc, (list, tuple)):
        return wynik

    porownywalny = _slowa(transkrypt) if transkrypt else None

    for element in wartosc:
        imie = " ".join(str(element).split()).strip(" -,.")
        imie = _GRZECZNOSCIOWE.sub("", imie).strip()
        if not (2 <= len(imie) <= 24):
            continue
        if not re.fullmatch(r"[^\W\d_][\w'’-]*(?: [^\W\d_][\w'’-]*)?", imie, re.UNICODE):
            continue
        # Pierwszy czlon tez, inaczej "Kierownik produkcji" czy "Dyrektor
        # finansowy" przechodza obok filtru jednowyrazowego.
        if imie.lower() in widziane:
            continue
        if {imie.lower(), imie.lower().split()[0]} & NIE_IMIONA:
            continue
        if porownywalny is not None and not _pada_w_tekscie(imie, porownywalny):
            log.info("Odrzucam imie %r - nie pada w transkrypcie", imie)
            continue
        widziane.add(imie.lower())
        wynik.append(imie[:1].upper() + imie[1:])
        if len(wynik) >= MAKS_UCZESTNIKOW:
            break
    return wynik


def _normalizuj(dane: dict, transkrypt: str | None = None) -> dict:
    temat = " ".join(str(dane.get("temat", "")).split()).strip(" .-\"'")
    tldr = " ".join(str(dane.get("tldr", "")).split())
    punkty = _zdania(dane.get("punkty"), MAKS_PUNKTOW)
    ustalenia = _zdania(dane.get("ustalenia"), MAKS_USTALEN)

    if transkrypt:
        slowa = _slowa_dokladne(transkrypt)
        tldr = _bez_zmyslonych_dat(tldr, slowa, transkrypt)
        punkty = [_bez_zmyslonych_dat(p, slowa, transkrypt) for p in punkty]
        ustalenia = [_bez_zmyslonych_dat(u, slowa, transkrypt) for u in ustalenia]

    return {
        "temat": temat[:MAKS_ZNAKOW_TEMATU],
        "uczestnicy": _imiona(dane.get("uczestnicy"), transkrypt),
        "tldr": tldr,
        "punkty": punkty,
        "ustalenia": ustalenia,
    }


# --- glowna funkcja -------------------------------------------------------


def podsumuj(
    segmenty: list[Segment],
    model: str,
    device: str = "GPU",
    status: StatusFn | None = None,
    anuluj: threading.Event | None = None,
) -> dict:
    """Zwraca {temat, uczestnicy, tldr, punkty, ustalenia, model_llm, device_llm}.

    Pusty slownik oznacza, ze nie bylo czego podsumowac albo model nie zwrocil
    nic sensownego - wywolujacy ma wtedy zostawic wszystko tak, jak bylo.
    """
    tekst = jako_tekst(segmenty).strip() if segmenty else ""
    if not tekst:
        return {}

    silnik = SilnikLLM(model, device)
    start = time.monotonic()
    try:
        if status:
            status(t("Ładuję model podsumowania..."))
        silnik.zaladuj()

        fragmenty = _fragmenty(tekst)
        imiona: list[str] = []

        if len(fragmenty) > 1:
            notatki: list[str] = []
            ustalenia: list[str] = []
            for i, fragment in enumerate(fragmenty, start=1):
                if anuluj is not None and anuluj.is_set():
                    break
                if status:
                    status(t("Podsumowuję fragment {i}/{n}...", i=i, n=len(fragmenty)))
                czesc = silnik.generuj_json(
                    PROMPT_FRAGMENTU.format(
                        numer=i, razem=len(fragmenty),
                        nota=_NOTA_ETYKIET, material=fragment,
                    ),
                    SCHEMAT_FRAGMENTU,
                    MAKS_TOKENOW_FRAGMENT,
                    anuluj,
                )
                notatki.extend(_zdania(czesc.get("punkty"), 3))
                ustalenia.extend(_zdania(czesc.get("ustalenia"), MAKS_USTALEN))
                imiona.extend(_imiona(czesc.get("uczestnicy"), tekst))

            if not notatki and not ustalenia:
                return {}
            material = "Notatki z kolejnych fragmentów:\n" + "\n".join(
                f"- {punkt}" for punkt in notatki
            )
            zrodlo = "notatki z transkryptu"
            # Ustalenia przekazujemy osobno, bo to one najczesciej gubily sie
            # przy sklejaniu notatek - model traktowal termin jak jeden z wielu
            # punktow i wycinal go przy limicie piatki.
            if ustalenia:
                material += ("\n\nUstalenia i terminy wychwycone w fragmentach "
                             "(zachowaj daty i godziny co do słowa):\n"
                             + "\n".join(f"- {u}" for u in dict.fromkeys(ustalenia)))
            if imiona:
                material += ("\n\nImiona wychwycone w rozmowie: "
                             + ", ".join(dict.fromkeys(imiona)))
        else:
            material = f"Transkrypt:\n---\n{fragmenty[0]}\n---"
            zrodlo = "transkrypt"

        if anuluj is not None and anuluj.is_set():
            return {}
        if status:
            status(t("Składam podsumowanie..."))

        wynik = _normalizuj(
            silnik.generuj_json(
                PROMPT_PODSUMOWANIA.format(
                    zrodlo=zrodlo, nota=_NOTA_ETYKIET, material=material
                ),
                SCHEMAT_PODSUMOWANIA,
                MAKS_TOKENOW_FINAL,
                anuluj,
            ),
            tekst,
        )
        if anuluj is not None and anuluj.is_set():
            return {}
        if not wynik["uczestnicy"] and imiona:
            wynik["uczestnicy"] = _imiona(imiona, tekst)
        if not wynik["temat"] and not wynik["tldr"]:
            return {}

        wynik["model_llm"] = model
        wynik["device_llm"] = silnik.device
        wynik["czas_podsumowania_s"] = round(time.monotonic() - start, 1)
        log.info("Podsumowanie gotowe w %.1fs: %s",
                 wynik["czas_podsumowania_s"], wynik["temat"])
        return wynik
    finally:
        silnik.zwolnij()


# --- zapis i nazwa katalogu -----------------------------------------------


def jako_markdown(wynik: dict, meta: dict) -> str:
    linie = [f"# {wynik.get('temat') or meta.get('nazwa', 'Spotkanie')}", ""]

    if meta.get("data"):
        linie.append(f"- **Data:** {meta['data']}")
    if meta.get("trwanie_s"):
        linie.append(f"- **Czas trwania:** {czas_hms(meta['trwanie_s'])}")
    if wynik.get("uczestnicy"):
        linie.append(f"- **Uczestnicy:** {', '.join(wynik['uczestnicy'])}")
    linie += ["", "## W skrócie", "", wynik.get("tldr") or "_Brak._", ""]

    if wynik.get("punkty"):
        linie += ["## Najważniejsze punkty", ""]
        linie += [f"- {punkt}" for punkt in wynik["punkty"]]
        linie.append("")

    if wynik.get("ustalenia"):
        linie += ["## Ustalenia", ""]
        linie += [f"- {ustalenie}" for ustalenie in wynik["ustalenia"]]
        linie.append("")

    linie += [
        "---",
        "",
        f"_Wygenerowane lokalnie modelem `{wynik.get('model_llm', '')}` "
        f"na {wynik.get('device_llm', '?')} w {wynik.get('czas_podsumowania_s', 0):.0f}s. "
        "Nic nie opuściło tego komputera._",
    ]
    return "\n".join(linie) + "\n"


def znacznik_z_nazwy(nazwa_katalogu: str) -> str | None:
    """'2026-09-08_09-15_Standup' -> '2026-09-08_09-15'. None, gdy inny format."""
    czesci = nazwa_katalogu.split("_")
    if len(czesci) < 2:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", czesci[0]):
        return None
    if not re.fullmatch(r"\d{2}-\d{2}", czesci[1]):
        return None
    return f"{czesci[0]}_{czesci[1]}"


def _czlon_tematu(temat: str, maks: int = 40) -> str:
    """Temat -> czlon nazwy katalogu, uciety na granicy slowa.

    `slug` tnie po znakach, wiec dlugi temat konczyl sie polowa wyrazu
    ('architektura-dan'). Ostatnie slowo odrzucamy tylko wtedy, gdy ciecie
    naprawde wypadlo w jego srodku - inaczej 'Plan na Q4' straciloby 'Q4'.
    """
    pelny = slug(temat, maks=200)
    if len(pelny) <= maks:
        return pelny
    if pelny[maks] == "-":          # ciecie trafilo dokladnie miedzy slowa
        return pelny[:maks]
    return pelny[:maks].rpartition("-")[0].strip("-") or pelny[:maks]


def nazwa_katalogu(stara_nazwa: str, wynik: dict) -> str | None:
    """Sklada '2026-09-08_09-15_Temat_Anna-Marek'. None = nie ma czego zmieniac.

    Znacznik czasu bierzemy ze starej nazwy, nie z zegara - inaczej ponowna
    transkrypcja archiwalnego nagrania przestawilaby jego date.
    """
    znacznik = znacznik_z_nazwy(stara_nazwa)
    temat = _czlon_tematu(wynik.get("temat", ""))
    if not znacznik or not temat:
        return None

    imiona = slug("-".join(imie.split()[0] for imie in wynik.get("uczestnicy", [])[:3]),
                  maks=30)
    nowa = f"{znacznik}_{temat}"
    if imiona:
        nowa += f"_{imiona}"
    return nowa if nowa != stara_nazwa else None
