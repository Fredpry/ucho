# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Rownolegle nagrywanie dwoch niezaleznych sciezek audio.

Sciezka "mikrofon" to zwykle wejscie WASAPI, sciezka "system" to WASAPI
loopback, czyli to co slychac w glosnikach. Standardowe PortAudio nie
udostepnia loopbacku na Windows, dlatego uzywamy forku PyAudioWPatch.

Zalozenia projektowe:
  * callback PortAudio NIE dotyka dysku - wrzuca surowe bajty do kolejki,
    a osobny watek zapisuje. Blokujace I/O w callbacku powoduje przerwy w audio.
  * WASAPI zwraca smieci przy zadaniu jednego kanalu, wiec czytamy stereo
    i miksujemy do mono sami.
  * kazda sciezka jest wyrownywana do zegara sciennego przez dopisywanie ciszy.
    Bez tego dwa niezalezne zegary urzadzen rozjezdzaja sie na dlugim nagraniu
    i znaczniki czasu obu sciezek przestaja do siebie pasowac.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .i18n import t

try:
    import soxr
except ImportError:  # pragma: no cover - resampling jest wymagany
    soxr = None

import pyaudiowpatch as pyaudio

log = logging.getLogger(__name__)

CZESTOTLIWOSC_DOCELOWA = 16_000
BLOK = 2048
# Ponizej tej roznicy nie wyrownujemy - drobne wahania sa normalne.
TOLERANCJA_DRIFTU_S = 0.3
# Brak danych przez tyle sekund oznacza, ze urzadzenie zniknelo.
TIMEOUT_URZADZENIA_S = 4.0


@dataclass(frozen=True)
class InfoUrzadzenia:
    index: int
    nazwa: str
    kanaly: int
    czestotliwosc: int
    loopback: bool

    @property
    def etykieta(self) -> str:
        przyrostek = " [dźwięk systemowy]" if self.loopback else ""
        return f"{self.nazwa}{przyrostek}"


def _z_info(info: dict, loopback: bool) -> InfoUrzadzenia:
    return InfoUrzadzenia(
        index=int(info["index"]),
        nazwa=str(info["name"]),
        kanaly=max(1, int(info.get("maxInputChannels", 2))),
        czestotliwosc=int(round(float(info.get("defaultSampleRate", 48000)))),
        loopback=loopback,
    )


def _mapa_urzadzen() -> tuple[list[InfoUrzadzenia], list[InfoUrzadzenia],
                             InfoUrzadzenia | None, InfoUrzadzenia | None]:
    """Zwraca (mikrofony, wyjscia_loopback, domyslny_mikrofon, domyslne_wyjscie).

    PortAudio buforuje liste urzadzen przy inicjalizacji, dlatego za kazdym
    razem tworzymy swiezy obiekt PyAudio i od razu go zamykamy.
    """
    mikrofony: list[InfoUrzadzenia] = []
    loopbacki: list[InfoUrzadzenia] = []
    dom_mik: InfoUrzadzenia | None = None
    dom_sys: InfoUrzadzenia | None = None

    p = pyaudio.PyAudio()
    try:
        indeksy_loopback = set()
        try:
            for info in p.get_loopback_device_info_generator():
                urz = _z_info(info, loopback=True)
                loopbacki.append(urz)
                indeksy_loopback.add(urz.index)
        except Exception as blad:  # brak WASAPI albo brak urzadzen wyjsciowych
            log.warning("Nie udalo sie wyliczyc urzadzen loopback: %s", blad)

        for i in range(p.get_device_count()):
            try:
                info = p.get_device_info_by_index(i)
            except Exception:
                continue
            if int(info.get("maxInputChannels", 0)) <= 0:
                continue
            if i in indeksy_loopback:
                continue
            mikrofony.append(_z_info(info, loopback=False))

        try:
            dom_mik = _z_info(p.get_default_input_device_info(), loopback=False)
        except Exception as blad:
            log.warning("Brak domyslnego mikrofonu: %s", blad)

        try:
            dom_sys = _z_info(p.get_default_wasapi_loopback(), loopback=True)
        except Exception as blad:
            log.warning("Brak domyslnego urzadzenia loopback: %s", blad)
    finally:
        p.terminate()

    return mikrofony, loopbacki, dom_mik, dom_sys


def lista_urzadzen() -> dict:
    mikrofony, loopbacki, dom_mik, dom_sys = _mapa_urzadzen()
    return {
        "mikrofony": mikrofony,
        "system": loopbacki,
        "domyslny_mikrofon": dom_mik,
        "domyslny_system": dom_sys,
    }


def _znajdz(index: int | None, kandydaci: list[InfoUrzadzenia],
            domyslne: InfoUrzadzenia | None) -> InfoUrzadzenia | None:
    if index is not None:
        for urz in kandydaci:
            if urz.index == index:
                return urz
    return domyslne


class _Sciezka:
    """Pojedyncza sciezka: strumien PortAudio -> kolejka -> plik FLAC 16 kHz mono."""

    def __init__(self, etykieta: str, plik: Path, urzadzenie: InfoUrzadzenia):
        self.etykieta = etykieta
        self.plik = plik
        self.urzadzenie = urzadzenie
        self.poziom = 0.0
        self.blad: str | None = None

        self._kolejka: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)
        self._stream = None
        self._plik_wy: sf.SoundFile | None = None
        self._resampler = None
        self._watek: threading.Thread | None = None
        self._stop = threading.Event()
        self._probki_zapisane = 0
        self._start_monotoniczny = 0.0
        self._ostatnie_dane = 0.0
        self._szerokosc = 2  # paInt16
        self._kanaly = urzadzenie.kanaly
        self._czestotliwosc = urzadzenie.czestotliwosc

    # --- cykl zycia -----------------------------------------------------

    def otworz_plik(self) -> None:
        self.plik.parent.mkdir(parents=True, exist_ok=True)
        # Zapis przyrostowy: nagranie przetrwa nawet twarde ubicie procesu.
        self._plik_wy = sf.SoundFile(
            str(self.plik), mode="w", samplerate=CZESTOTLIWOSC_DOCELOWA,
            channels=1, subtype="PCM_16", format="FLAC",
        )

    def start(self, p: "pyaudio.PyAudio", urzadzenie: InfoUrzadzenia | None = None) -> None:
        if urzadzenie is not None:
            self.urzadzenie = urzadzenie
            self._kanaly = urzadzenie.kanaly
            self._czestotliwosc = urzadzenie.czestotliwosc

        self._zbuduj_resampler()
        self._start_monotoniczny = self._start_monotoniczny or time.monotonic()
        self._ostatnie_dane = time.monotonic()

        self._stream = p.open(
            format=pyaudio.paInt16,
            channels=self._kanaly,
            rate=self._czestotliwosc,
            input=True,
            input_device_index=self.urzadzenie.index,
            frames_per_buffer=BLOK,
            stream_callback=self._callback,
        )
        if self._watek is None or not self._watek.is_alive():
            self._watek = threading.Thread(
                target=self._petla_zapisu, name=f"zapis-{self.etykieta}", daemon=True
            )
            self._watek.start()
        self._stream.start_stream()

    def _zbuduj_resampler(self) -> None:
        if self._czestotliwosc == CZESTOTLIWOSC_DOCELOWA:
            self._resampler = None
            return
        if soxr is None:
            raise RuntimeError(t("Brak biblioteki soxr — uruchom ponownie instaluj.ps1"))
        self._resampler = soxr.ResampleStream(
            self._czestotliwosc, CZESTOTLIWOSC_DOCELOWA, 1,
            dtype="float32", quality="HQ",
        )

    def zatrzymaj_strumien(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop_stream()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def zakoncz(self) -> None:
        self.zatrzymaj_strumien()
        # Bez sentinela: watek zapisu ma dokonczyc to, co zostalo w kolejce.
        self._stop.set()
        if self._watek is not None:
            self._watek.join(timeout=10.0)
        if self._plik_wy is not None:
            try:
                self._plik_wy.close()
            except Exception:
                pass
            self._plik_wy = None

    # --- przetwarzanie --------------------------------------------------

    def _callback(self, in_data, frame_count, time_info, status):  # noqa: ARG002
        try:
            self._kolejka.put_nowait(in_data)
        except queue.Full:
            # Wolniejszy dysk niz strumien - gubimy blok zamiast blokowac audio.
            log.warning("Kolejka %s pelna, pominieto blok", self.etykieta)
        return (None, pyaudio.paContinue)

    def _petla_zapisu(self) -> None:
        while True:
            try:
                dane = self._kolejka.get(timeout=0.25)
            except queue.Empty:
                # Konczymy dopiero gdy kolejka jest pusta - inaczej zgubilibysmy
                # ostatnie bloki, ktore czekaja jeszcze na zapis.
                if self._stop.is_set():
                    break
                continue
            if dane is None:
                break
            try:
                self._zapisz_blok(dane)
            except Exception as blad:  # nie ubijamy nagrania przez jeden blok
                log.exception("Blad zapisu sciezki %s: %s", self.etykieta, blad)
                self.blad = str(blad)

        # Domknij resampler, zeby nie zgubic ogona bufora.
        if self._resampler is not None and self._plik_wy is not None:
            try:
                ogon = self._resampler.resample_chunk(
                    np.zeros(0, dtype=np.float32), last=True
                )
                if ogon is not None and len(ogon):
                    self._plik_wy.write(np.asarray(ogon, dtype=np.float32))
            except Exception:
                pass

    def _zapisz_blok(self, dane: bytes) -> None:
        if self._plik_wy is None:
            return
        probki = np.frombuffer(dane, dtype=np.int16).astype(np.float32) / 32768.0
        if self._kanaly > 1:
            uzyteczne = (len(probki) // self._kanaly) * self._kanaly
            probki = probki[:uzyteczne].reshape(-1, self._kanaly).mean(axis=1)

        if probki.size == 0:
            return

        self._ostatnie_dane = time.monotonic()
        rms = float(np.sqrt(np.mean(np.square(probki))))
        # Skala decybelowa 0..1: -60 dBFS to cisza, 0 dBFS to maksimum.
        db = 20.0 * np.log10(rms + 1e-9)
        self.poziom = float(min(1.0, max(0.0, (db + 60.0) / 60.0)))

        if self._resampler is not None:
            wyjscie = self._resampler.resample_chunk(probki)
            if wyjscie is None or len(wyjscie) == 0:
                return
            wyjscie = np.asarray(wyjscie, dtype=np.float32)
        else:
            wyjscie = probki

        self._wyrownaj_do_zegara()
        self._plik_wy.write(wyjscie)
        self._probki_zapisane += len(wyjscie)

    def _wyrownaj_do_zegara(self) -> None:
        """Dopisuje cisze, jesli sciezka zostala w tyle za czasem rzeczywistym.

        Nigdy nie ucina - lepiej miec o pol sekundy za duzo niz rozjechane
        znaczniki czasu miedzy sciezkami.
        """
        if self._plik_wy is None or self._start_monotoniczny == 0.0:
            return
        oczekiwane = (time.monotonic() - self._start_monotoniczny) * CZESTOTLIWOSC_DOCELOWA
        brakuje = int(oczekiwane - self._probki_zapisane)
        if brakuje > TOLERANCJA_DRIFTU_S * CZESTOTLIWOSC_DOCELOWA:
            self._plik_wy.write(np.zeros(brakuje, dtype=np.float32))
            self._probki_zapisane += brakuje

    @property
    def martwa(self) -> bool:
        return (time.monotonic() - self._ostatnie_dane) > TIMEOUT_URZADZENIA_S

    @property
    def trwanie_s(self) -> float:
        return self._probki_zapisane / CZESTOTLIWOSC_DOCELOWA


class Nagrywarka:
    """Zarzadza obiema sciezkami i pilnuje, zeby przezyly zmiane urzadzenia."""

    def __init__(self, tryb: str = "obie", mikrofon_index: int | None = None,
                 system_index: int | None = None):
        self.tryb = tryb
        self._mikrofon_index = mikrofon_index
        self._system_index = system_index
        self._pa: "pyaudio.PyAudio | None" = None
        self._sciezki: dict[str, _Sciezka] = {}
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        self._blokada = threading.Lock()
        self.katalog: Path | None = None
        self.start_ts = 0.0
        self.ostrzezenia: list[str] = []

    # --- API ------------------------------------------------------------

    def start(self, katalog: Path) -> None:
        self.katalog = katalog
        katalog.mkdir(parents=True, exist_ok=True)
        self.ostrzezenia.clear()

        urzadzenia = lista_urzadzen()
        wybrane: list[tuple[str, str, InfoUrzadzenia]] = []

        if self.tryb in ("obie", "mikrofon"):
            mik = _znajdz(self._mikrofon_index, urzadzenia["mikrofony"],
                          urzadzenia["domyslny_mikrofon"])
            if mik is None:
                self.ostrzezenia.append(t("Nie znaleziono mikrofonu."))
            else:
                wybrane.append(("mikrofon", "mikrofon.flac", mik))

        if self.tryb in ("obie", "system"):
            sysurz = _znajdz(self._system_index, urzadzenia["system"],
                             urzadzenia["domyslny_system"])
            if sysurz is None:
                self.ostrzezenia.append(
                    t("Nie znaleziono urządzenia loopback — dźwięk systemowy nie będzie nagrany.")
                )
            else:
                wybrane.append(("system", "spotkanie.flac", sysurz))

        if not wybrane:
            raise RuntimeError(t("Brak dostępnych urządzeń audio do nagrywania."))

        self._pa = pyaudio.PyAudio()
        self.start_ts = time.time()

        for nazwa, plik, urz in wybrane:
            sciezka = _Sciezka(nazwa, katalog / plik, urz)
            sciezka.otworz_plik()
            try:
                sciezka.start(self._pa)
            except Exception as blad:
                sciezka.zakoncz()
                self.ostrzezenia.append(t("Nie udało się otworzyć „{nazwa}”: {blad}", nazwa=urz.nazwa, blad=blad))
                continue
            self._sciezki[nazwa] = sciezka

        if not self._sciezki:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(t("Nie udało się uruchomić żadnej ścieżki nagrywania."))

        self._stop.clear()
        self._watchdog = threading.Thread(target=self._petla_watchdoga,
                                          name="watchdog-audio", daemon=True)
        self._watchdog.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=3.0)
            self._watchdog = None

        with self._blokada:
            for sciezka in self._sciezki.values():
                sciezka.zakoncz()
            if self._pa is not None:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None

        wynik = {
            "katalog": self.katalog,
            "trwanie_s": max((s.trwanie_s for s in self._sciezki.values()), default=0.0),
            "sciezki": {
                nazwa: {
                    "plik": s.plik,
                    "urzadzenie": s.urzadzenie.nazwa,
                    "trwanie_s": s.trwanie_s,
                }
                for nazwa, s in self._sciezki.items()
                if s.plik.exists() and s.plik.stat().st_size > 0
            },
            "ostrzezenia": list(self.ostrzezenia),
        }
        self._sciezki.clear()
        return wynik

    @property
    def poziomy(self) -> dict[str, float]:
        return {nazwa: s.poziom for nazwa, s in self._sciezki.items()}

    @property
    def trwanie_s(self) -> float:
        if not self.start_ts:
            return 0.0
        return time.time() - self.start_ts

    # --- odpornosc na zmiane urzadzenia ---------------------------------

    def _petla_watchdoga(self) -> None:
        while not self._stop.wait(2.0):
            martwe = [n for n, s in self._sciezki.items() if s.martwa]
            if martwe:
                log.warning("Sciezki bez danych: %s - restartuje strumienie", martwe)
                self._restartuj_strumienie()

    def _restartuj_strumienie(self) -> None:
        """Pelny restart PortAudio - inaczej nie zobaczy nowego urzadzenia domyslnego."""
        with self._blokada:
            if self._pa is None:
                return
            for sciezka in self._sciezki.values():
                sciezka.zatrzymaj_strumien()
            try:
                self._pa.terminate()
            except Exception:
                pass

            urzadzenia = lista_urzadzen()
            self._pa = pyaudio.PyAudio()
            for nazwa, sciezka in self._sciezki.items():
                if nazwa == "mikrofon":
                    nowe = _znajdz(self._mikrofon_index, urzadzenia["mikrofony"],
                                   urzadzenia["domyslny_mikrofon"])
                else:
                    nowe = _znajdz(self._system_index, urzadzenia["system"],
                                   urzadzenia["domyslny_system"])
                if nowe is None:
                    continue
                try:
                    sciezka.start(self._pa, nowe)
                    log.info("Sciezka %s wznowiona na '%s'", nazwa, nowe.nazwa)
                except Exception as blad:
                    log.error("Nie udalo sie wznowic sciezki %s: %s", nazwa, blad)
