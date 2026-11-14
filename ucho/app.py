# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Okno aplikacji: nagrywanie, transkrypcja, lista nagran, ustawienia."""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from . import (audio, config, i18n, ikona as ikona_mod, podsumowanie as podsum,
               transcribe, winshell)
from .i18n import t
from .merge import Segment, czas_hms, slug, zapisz_wyniki
from .tray import GlobalnySkrot, Zasobnik

log = logging.getLogger(__name__)

TLO = "#15171d"
PANEL = "#1e2129"
PANEL_JASNY = "#272b35"
AKCENT = "#5ea4ff"
AKCENT_HOVER = "#7ab4ff"
CZERWIEN = "#ef4444"
CZERWIEN_HOVER = "#f26565"
TEKST = "#e6e9ef"
TEKST_PRZYGASZONY = "#8b93a5"
ZIELEN = "#34d399"


class Aplikacja(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.ustawienia = config.Ustawienia()
        config.przygotuj_katalogi(self.ustawienia)

        self.nagrywarka: audio.Nagrywarka | None = None
        self.nagrywa = False
        self.katalog_sesji: Path | None = None
        self.nazwa_sesji = ""
        self.start_nagrania = 0.0
        self.anuluj_transkrypcje = threading.Event()
        self.transkrybuje = False
        self.podsumowuje = False
        self._kolejka: "queue.Queue[tuple]" = queue.Queue()
        self._urzadzenia = {"mikrofony": [], "system": [],
                            "domyslny_mikrofon": None, "domyslny_system": None}

        self._zbuduj_okno()
        self._zbuduj_naglowek()
        self._zbuduj_widoki()
        self._pokaz_widok("nagrywanie")

        self._zasobnik = Zasobnik(
            config.APLIKACJA,
            on_przelacz=lambda: self.after(0, self.przelacz_nagrywanie),
            on_pokaz=lambda: self.after(0, self._pokaz_okno),
            on_zakoncz=lambda: self.after(0, self.zakoncz),
        )
        self._skrot = GlobalnySkrot(
            self.ustawienia["hotkey"], lambda: self.after(0, self.przelacz_nagrywanie)
        )
        if self.ustawienia["tray"]:
            self._zasobnik.start()
        self._skrot.start()

        self.protocol("WM_DELETE_WINDOW", self._na_zamkniecie)
        threading.Thread(target=self._wczytaj_urzadzenia, daemon=True).start()
        self.after(100, self._tick)
        # Okno musi juz istniec w systemie, zanim opiszemy je dla paska zadan.
        self.after(600, self._opisz_dla_paska)

    # --- budowa interfejsu ----------------------------------------------

    def _zbuduj_okno(self) -> None:
        ctk.set_appearance_mode("dark")
        self.title(t("{app} - lokalne nagrywanie i transkrypcja", app=config.APLIKACJA))
        # Dopasowanie do ekranu: na laptopie z wysokim DPI przestrzen robocza
        # bywa niska, a sztywne 560x770 uciecoby dolna czesc widoku.
        szerokosc = min(560, self.winfo_screenwidth() - 60)
        wysokosc = min(720, self.winfo_screenheight() - 110)
        self.geometry(f"{szerokosc}x{wysokosc}")
        self.minsize(500, min(600, wysokosc))
        self.configure(fg_color=TLO)

        self._plik_ikony = Path(__file__).resolve().parent / "assets" / "ikona.ico"
        if not self._plik_ikony.exists():
            try:
                ikona_mod.zapisz_ico(self._plik_ikony)
            except Exception:
                pass
        self._ustaw_ikone_okna()
        # CustomTkinter po ~200 ms chowa i pokazuje okno, zeby przemalowac pasek
        # tytulu na ciemny - przy okazji Windows potrafi zgubic ikone. Ustawiamy
        # ja ponownie po tej operacji.
        self.after(400, self._ustaw_ikone_okna)

    def _hwnd_okna(self) -> int:
        """Uchwyt okna widzianego przez pasek zadan.

        `winfo_id()` zwraca okno potomne Tk - wlasciwosci dla paska musza trafic
        na okno nadrzedne, ktore podaje `wm_frame()`.
        """
        try:
            return int(self.wm_frame(), 16)
        except Exception:
            return self.winfo_id()

    def _opisz_dla_paska(self) -> None:
        """Dane dla paska zadan: czym jestesmy i czym sie uruchamiamy ponownie."""
        if sys.platform != "win32":
            return
        try:
            udalo_sie = winshell.opisz_okno(
                self._hwnd_okna(),
                polecenie_startowe(),
                config.APLIKACJA,
                f"{self._plik_ikony},0",
                config.ID_APLIKACJI,
            )
            log.info("Opis okna dla paska zadan: %s", udalo_sie)
        except Exception:
            log.debug("Nie udalo sie opisac okna", exc_info=True)
        # Skroty dostaja to samo ID, wiec przypiety kafelek i dzialajace okno to
        # jeden przycisk. Robimy to w tle - COM potrafi chwile pomyslec.
        threading.Thread(target=_oznacz_skroty, daemon=True).start()

    def _ustaw_ikone_okna(self) -> None:
        if not self._plik_ikony.exists():
            return
        try:
            # `default` obejmuje takze okna podrzedne (dialogi).
            self.iconbitmap(default=str(self._plik_ikony))
            self.iconbitmap(str(self._plik_ikony))
        except Exception:
            pass

    def _zbuduj_naglowek(self) -> None:
        pasek = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=56)
        pasek.pack(fill="x", side="top")
        pasek.pack_propagate(False)

        ctk.CTkLabel(
            pasek, text=f"  {config.APLIKACJA}", text_color=TEKST,
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(side="left", padx=(14, 20))

        self._przyciski_nawigacji: dict[str, ctk.CTkButton] = {}
        for klucz, etykieta in (("nagrywanie", t("Nagrywanie")),
                                ("nagrania", t("Nagrania")),
                                ("ustawienia", t("Ustawienia"))):
            przycisk = ctk.CTkButton(
                pasek, text=etykieta, width=96, height=32, corner_radius=8,
                fg_color="transparent", hover_color=PANEL_JASNY,
                text_color=TEKST_PRZYGASZONY,
                font=ctk.CTkFont(size=13),
                command=lambda k=klucz: self._pokaz_widok(k),
            )
            przycisk.pack(side="left", padx=2)
            self._przyciski_nawigacji[klucz] = przycisk

    def _zbuduj_widoki(self) -> None:
        self._kontener = ctk.CTkFrame(self, fg_color=TLO)
        self._kontener.pack(fill="both", expand=True)

        self._widoki = {
            "nagrywanie": self._widok_nagrywania(),
            "transkrypcja": self._widok_transkrypcji(),
            "nagrania": self._widok_nagran(),
            "ustawienia": self._widok_ustawien(),
        }

    def _pokaz_widok(self, nazwa: str) -> None:
        for widok in self._widoki.values():
            widok.pack_forget()
        self._widoki[nazwa].pack(fill="both", expand=True, padx=16, pady=16)

        aktywna_zakladka = "nagrywanie" if nazwa == "transkrypcja" else nazwa
        for klucz, przycisk in self._przyciski_nawigacji.items():
            wybrany = klucz == aktywna_zakladka
            przycisk.configure(
                fg_color=PANEL_JASNY if wybrany else "transparent",
                text_color=TEKST if wybrany else TEKST_PRZYGASZONY,
            )
        if nazwa == "nagrania":
            self._odswiez_liste_nagran()

    # --- widok: nagrywanie ----------------------------------------------

    def _widok_nagrywania(self) -> ctk.CTkFrame:
        ramka = ctk.CTkFrame(self._kontener, fg_color=TLO)

        ctk.CTkLabel(ramka, text=t("Nazwa spotkania"), text_color=TEKST_PRZYGASZONY,
                     font=ctk.CTkFont(size=12)).pack(anchor="w")
        self._pole_nazwy = ctk.CTkEntry(
            ramka, height=36, corner_radius=10, fg_color=PANEL,
            border_color=PANEL_JASNY, text_color=TEKST,
            placeholder_text=t("np. Standup zespołu"),
        )
        self._pole_nazwy.pack(fill="x", pady=(4, 12))

        karta = ctk.CTkFrame(ramka, fg_color=PANEL, corner_radius=16)
        karta.pack(fill="x", pady=(0, 12))

        self._etykieta_czasu = ctk.CTkLabel(
            karta, text="00:00:00", text_color=TEKST,
            font=ctk.CTkFont(family="Consolas", size=34, weight="bold"),
        )
        self._etykieta_czasu.pack(pady=(16, 0))

        self._etykieta_stanu = ctk.CTkLabel(
            karta, text=t("Gotowy do nagrywania"), text_color=TEKST_PRZYGASZONY,
            font=ctk.CTkFont(size=12), wraplength=460,
        )
        self._etykieta_stanu.pack(pady=(0, 12))

        # Krotka etykieta jest tu istotna: dluzszy tekst rozpycha CTkButton na
        # szerokosc i zamiast kola wychodzi owal.
        self._przycisk_rec = ctk.CTkButton(
            karta, text="REC", width=148, height=148, corner_radius=74,
            fg_color=CZERWIEN, hover_color=CZERWIEN_HOVER, text_color="#ffffff",
            font=ctk.CTkFont(size=22, weight="bold"),
            command=self.przelacz_nagrywanie,
        )
        self._przycisk_rec.pack(pady=(0, 6))

        ctk.CTkLabel(
            karta, text=t("skrót globalny: {skrot}", skrot=self._opis_skrotu()),
            text_color=TEKST_PRZYGASZONY, font=ctk.CTkFont(size=11),
        ).pack(pady=(0, 14))

        self._poziomy_ui = {}
        for klucz, etykieta in (("mikrofon", t("Mikrofon")), ("system", t("Dźwięk systemowy"))):
            wiersz = ctk.CTkFrame(ramka, fg_color="transparent")
            wiersz.pack(fill="x", pady=3)
            ctk.CTkLabel(wiersz, text=etykieta, text_color=TEKST_PRZYGASZONY, width=130,
                         anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
            pasek = ctk.CTkProgressBar(wiersz, height=10, corner_radius=5,
                                       fg_color=PANEL, progress_color=ZIELEN)
            pasek.pack(side="left", fill="x", expand=True)
            pasek.set(0)
            self._poziomy_ui[klucz] = pasek

        wiersz_jezyka = ctk.CTkFrame(ramka, fg_color="transparent")
        wiersz_jezyka.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(wiersz_jezyka, text=t("Język transkrypcji"), text_color=TEKST_PRZYGASZONY,
                     font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 6))
        self._wybor_jezyka = ctk.CTkSegmentedButton(
            wiersz_jezyka, values=list(config.ETYKIETY_JEZYKOW.values()),
            fg_color=PANEL, selected_color=AKCENT, selected_hover_color=AKCENT_HOVER,
            unselected_color=PANEL, unselected_hover_color=PANEL_JASNY,
            text_color=TEKST, command=self._na_zmiane_jezyka,
        )
        self._wybor_jezyka.pack(fill="x")
        self._wybor_jezyka.set(config.ETYKIETY_JEZYKOW[self.ustawienia["jezyk"]])

        self._notka = ctk.CTkLabel(
            ramka,
            text=t("Poinformuj uczestników, że spotkanie jest nagrywane.\n"
                   "Używaj słuchawek — na głośnikach mikrofon złapie także rozmówców."),
            text_color=TEKST_PRZYGASZONY, font=ctk.CTkFont(size=11), justify="left",
        )
        self._notka.pack(anchor="w", pady=(14, 0))
        return ramka

    def _opis_skrotu(self) -> str:
        return (self.ustawienia["hotkey"].replace("<", "").replace(">", "")
                .replace("+", " + ").upper())

    def _na_zmiane_jezyka(self, wartosc: str) -> None:
        for kod, etykieta in config.ETYKIETY_JEZYKOW.items():
            if etykieta == wartosc:
                self.ustawienia["jezyk"] = kod
                self.ustawienia.zapisz()
                return

    # --- widok: transkrypcja --------------------------------------------

    def _widok_transkrypcji(self) -> ctk.CTkFrame:
        ramka = ctk.CTkFrame(self._kontener, fg_color=TLO)

        self._tytul_transkrypcji = ctk.CTkLabel(
            ramka, text=t("Transkrypcja"), text_color=TEKST,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self._tytul_transkrypcji.pack(anchor="w")

        self._podtytul_transkrypcji = ctk.CTkLabel(
            ramka, text="", text_color=TEKST_PRZYGASZONY, font=ctk.CTkFont(size=12)
        )
        self._podtytul_transkrypcji.pack(anchor="w", pady=(2, 12))

        self._pasek_postepu = ctk.CTkProgressBar(
            ramka, height=12, corner_radius=6, fg_color=PANEL, progress_color=AKCENT
        )
        self._pasek_postepu.pack(fill="x")
        self._pasek_postepu.set(0)

        self._etykieta_postepu = ctk.CTkLabel(
            ramka, text="0%", text_color=TEKST_PRZYGASZONY, font=ctk.CTkFont(size=12)
        )
        self._etykieta_postepu.pack(anchor="w", pady=(6, 12))

        # Karta podsumowania - doklejana nad transkryptem, gdy model skonczy.
        self._karta_podsumowania = ctk.CTkFrame(ramka, fg_color=PANEL, corner_radius=12)
        self._etykieta_uczestnikow = ctk.CTkLabel(
            self._karta_podsumowania, text="", text_color=AKCENT, anchor="w",
            justify="left", font=ctk.CTkFont(size=12, weight="bold"), wraplength=460,
        )
        self._etykieta_uczestnikow.pack(fill="x", padx=14, pady=(10, 0))
        self._etykieta_podsumowania = ctk.CTkLabel(
            self._karta_podsumowania, text="", text_color=TEKST, anchor="w",
            justify="left", font=ctk.CTkFont(size=12), wraplength=460,
        )
        self._etykieta_podsumowania.pack(fill="x", padx=14, pady=(4, 10))

        self._pole_transkrypcji = ctk.CTkTextbox(
            ramka, fg_color=PANEL, text_color=TEKST, corner_radius=12,
            font=ctk.CTkFont(size=12), wrap="word",
        )
        self._pole_transkrypcji.pack(fill="both", expand=True)

        przyciski = ctk.CTkFrame(ramka, fg_color="transparent")
        przyciski.pack(fill="x", pady=(12, 0))

        self._przycisk_folder = ctk.CTkButton(
            przyciski, text=t("Otwórz folder"), height=36, corner_radius=10,
            fg_color=PANEL_JASNY, hover_color=PANEL, text_color=TEKST,
            command=self._otworz_folder_sesji,
        )
        self._przycisk_folder.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self._przycisk_anuluj = ctk.CTkButton(
            przyciski, text=t("Przerwij"), height=36, corner_radius=10,
            fg_color=PANEL_JASNY, hover_color=CZERWIEN, text_color=TEKST,
            command=self._przerwij_transkrypcje,
        )
        self._przycisk_anuluj.pack(side="left", expand=True, fill="x", padx=(6, 0))
        return ramka

    # --- widok: nagrania -------------------------------------------------

    def _widok_nagran(self) -> ctk.CTkFrame:
        ramka = ctk.CTkFrame(self._kontener, fg_color=TLO)
        gora = ctk.CTkFrame(ramka, fg_color="transparent")
        gora.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(gora, text=t("Nagrania"), text_color=TEKST,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkButton(gora, text=t("Odśwież"), width=88, height=30, corner_radius=8,
                      fg_color=PANEL_JASNY, hover_color=PANEL, text_color=TEKST,
                      command=self._odswiez_liste_nagran).pack(side="right")

        self._lista_nagran = ctk.CTkScrollableFrame(ramka, fg_color=TLO)
        self._lista_nagran.pack(fill="both", expand=True)
        return ramka

    def _odswiez_liste_nagran(self) -> None:
        for dziecko in self._lista_nagran.winfo_children():
            dziecko.destroy()

        katalog = self.ustawienia.katalog_nagran
        if not katalog.exists():
            return
        sesje = sorted((s for s in katalog.iterdir() if s.is_dir()), reverse=True)
        if not sesje:
            ctk.CTkLabel(self._lista_nagran, text=t("Brak nagrań."),
                         text_color=TEKST_PRZYGASZONY).pack(pady=20)
            return

        for sesja in sesje[:60]:
            self._karta_sesji(sesja)

    @staticmethod
    def _opis_z_nazwy_katalogu(nazwa_katalogu: str) -> tuple[str, str]:
        """'2026-09-08_09-15_Budzet-Q4_Anna-Marek' -> ('Budzet Q4 Anna Marek', ...).

        Od kiedy nazwe nadaje model, po znaczniku czasu moga byc dwa czlony
        (temat i imiona), wiec nie mozemy juz zakladac dokladnie trzech czesci.
        """
        czesci = nazwa_katalogu.split("_")
        if len(czesci) >= 3 and czesci[1].count("-") == 1:
            data = f"{czesci[0]} {czesci[1].replace('-', ':')}"
            return " ".join(czesci[2:]).replace("-", " "), data
        return nazwa_katalogu, ""

    def _karta_sesji(self, sesja: Path) -> None:
        meta = {}
        plik_meta = sesja / "meta.json"
        if plik_meta.exists():
            try:
                meta = json.loads(plik_meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}

        nazwa, data = self._opis_z_nazwy_katalogu(sesja.name)

        karta = ctk.CTkFrame(self._lista_nagran, fg_color=PANEL, corner_radius=12)
        karta.pack(fill="x", pady=4)

        ctk.CTkLabel(karta, text=meta.get("temat") or meta.get("nazwa") or nazwa,
                     text_color=TEKST, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            fill="x", padx=14, pady=(10, 0))

        opis = meta.get("data") or data
        if meta.get("trwanie_s"):
            opis += f"  ·  {czas_hms(meta['trwanie_s'])}  ·  {meta.get('device', '?')}"
        ma_transkrypt = (sesja / "transkrypt.md").exists()
        opis += "  ·  " + (t("transkrypt gotowy") if ma_transkrypt else t("bez transkryptu"))
        if meta.get("uczestnicy"):
            opis += "  ·  " + ", ".join(meta["uczestnicy"])
        ctk.CTkLabel(karta, text=opis, text_color=TEKST_PRZYGASZONY, anchor="w",
                     font=ctk.CTkFont(size=11)).pack(fill="x", padx=14, pady=(2, 4))

        # Podsumowanie widoczne wprost na liscie - po to bylo je robic.
        ma_podsumowanie = (sesja / "podsumowanie.md").exists()
        if meta.get("tldr"):
            ctk.CTkLabel(karta, text=meta["tldr"], text_color=TEKST, anchor="w",
                         justify="left", wraplength=430,
                         font=ctk.CTkFont(size=11)).pack(fill="x", padx=14, pady=(0, 6))

        przyciski = ctk.CTkFrame(karta, fg_color="transparent")
        przyciski.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(przyciski, text=t("Folder"), width=68, height=28, corner_radius=8,
                      fg_color=PANEL_JASNY, hover_color=TLO, text_color=TEKST,
                      font=ctk.CTkFont(size=11),
                      command=lambda s=sesja: self._otworz(s)).pack(side="left", padx=2)
        if ma_transkrypt:
            ctk.CTkButton(przyciski, text=t("Transkrypt"), width=84, height=28, corner_radius=8,
                          fg_color=PANEL_JASNY, hover_color=TLO, text_color=TEKST,
                          font=ctk.CTkFont(size=11),
                          command=lambda s=sesja: self._otworz(s / "transkrypt.md")
                          ).pack(side="left", padx=2)
        if ma_podsumowanie:
            ctk.CTkButton(przyciski, text=t("Podsumowanie"), width=108, height=28,
                          corner_radius=8, fg_color=PANEL_JASNY, hover_color=TLO,
                          text_color=TEKST, font=ctk.CTkFont(size=11),
                          command=lambda s=sesja: self._otworz(s / "podsumowanie.md")
                          ).pack(side="left", padx=2)
        elif meta.get("segmenty"):
            ctk.CTkButton(przyciski, text=t("Podsumuj"), width=86, height=28,
                          corner_radius=8, fg_color=PANEL_JASNY, hover_color=AKCENT,
                          text_color=TEKST, font=ctk.CTkFont(size=11),
                          command=lambda s=sesja: self._podsumuj_ponownie(s)
                          ).pack(side="left", padx=2)
        ctk.CTkButton(przyciski, text=t("Transkrybuj ponownie"), width=140, height=28,
                      corner_radius=8, fg_color=PANEL_JASNY, hover_color=AKCENT,
                      text_color=TEKST, font=ctk.CTkFont(size=11),
                      command=lambda s=sesja: self._transkrybuj_ponownie(s)
                      ).pack(side="left", padx=2)

    # --- widok: ustawienia -----------------------------------------------

    def _widok_ustawien(self) -> ctk.CTkFrame:
        ramka = ctk.CTkScrollableFrame(self._kontener, fg_color=TLO)

        def sekcja(tytul: str) -> ctk.CTkFrame:
            ctk.CTkLabel(ramka, text=tytul, text_color=TEKST, anchor="w",
                         font=ctk.CTkFont(size=14, weight="bold")).pack(
                fill="x", pady=(14, 6))
            pudlo = ctk.CTkFrame(ramka, fg_color=PANEL, corner_radius=12)
            pudlo.pack(fill="x")
            return pudlo

        def pole(rodzic, etykieta: str) -> ctk.CTkFrame:
            ctk.CTkLabel(rodzic, text=etykieta, text_color=TEKST_PRZYGASZONY, anchor="w",
                         font=ctk.CTkFont(size=11)).pack(fill="x", padx=12, pady=(10, 2))
            wiersz = ctk.CTkFrame(rodzic, fg_color="transparent")
            wiersz.pack(fill="x", padx=12, pady=(0, 8))
            return wiersz

        # Urzadzenia audio
        pudlo = sekcja(t("Urządzenia audio"))
        self._menu_mikrofon = ctk.CTkOptionMenu(
            pole(pudlo, t("Mikrofon")), values=[t("(domyślny)")], fg_color=PANEL_JASNY,
            button_color=PANEL_JASNY, button_hover_color=TLO, text_color=TEKST,
            dynamic_resizing=False, command=lambda _: self._zapisz_urzadzenia(),
        )
        self._menu_mikrofon.pack(fill="x")
        self._menu_system = ctk.CTkOptionMenu(
            pole(pudlo, t("Dźwięk systemowy (loopback)")), values=[t("(domyślny)")],
            fg_color=PANEL_JASNY, button_color=PANEL_JASNY, button_hover_color=TLO,
            text_color=TEKST, dynamic_resizing=False,
            command=lambda _: self._zapisz_urzadzenia(),
        )
        self._menu_system.pack(fill="x")
        self._menu_sciezki = ctk.CTkOptionMenu(
            pole(pudlo, t("Co nagrywać")),
            values=[t(v) for v in config.ETYKIETY_SCIEZEK.values()],
            fg_color=PANEL_JASNY, button_color=PANEL_JASNY, button_hover_color=TLO,
            text_color=TEKST, dynamic_resizing=False, command=self._zapisz_sciezki,
        )
        self._menu_sciezki.set(t(config.ETYKIETY_SCIEZEK[self.ustawienia["sciezki"]]))
        self._menu_sciezki.pack(fill="x")

        # Transkrypcja
        pudlo = sekcja(t("Transkrypcja"))
        etykiety_modeli = [f"{k.split('/')[-1]} - {t(v['opis'])}"
                           for k, v in config.MODELE.items()]
        self._mapa_modeli = dict(zip(etykiety_modeli, config.MODELE.keys(), strict=True))
        self._menu_modelu = ctk.CTkOptionMenu(
            pole(pudlo, t("Model")), values=etykiety_modeli, fg_color=PANEL_JASNY,
            button_color=PANEL_JASNY, button_hover_color=TLO, text_color=TEKST,
            dynamic_resizing=False, command=self._zapisz_model,
        )
        for etykieta, repo in self._mapa_modeli.items():
            if repo == self.ustawienia["model"]:
                self._menu_modelu.set(etykieta)
        self._menu_modelu.pack(fill="x")

        self._etykieta_modelu = ctk.CTkLabel(
            pudlo, text="", text_color=TEKST_PRZYGASZONY, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self._etykieta_modelu.pack(fill="x", padx=12, pady=(0, 6))

        self._przycisk_pobierz = ctk.CTkButton(
            pudlo, text=t("Pobierz model"), height=32, corner_radius=8, fg_color=AKCENT,
            hover_color=AKCENT_HOVER, text_color="#0b1220",
            command=self._pobierz_model,
        )
        self._przycisk_pobierz.pack(fill="x", padx=12, pady=(0, 10))

        self._menu_device = ctk.CTkOptionMenu(
            pole(pudlo, t("Urządzenie obliczeniowe")), values=["GPU", "CPU"],
            fg_color=PANEL_JASNY, button_color=PANEL_JASNY, button_hover_color=TLO,
            text_color=TEKST, dynamic_resizing=False, command=self._zapisz_device,
        )
        self._menu_device.pack(fill="x")

        self._etykieta_device = ctk.CTkLabel(
            pudlo, text="", text_color=TEKST_PRZYGASZONY, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self._etykieta_device.pack(fill="x", padx=12, pady=(0, 10))

        self._przelacznik_auto = ctk.CTkSwitch(
            pudlo, text=t("Transkrybuj automatycznie po zatrzymaniu"),
            text_color=TEKST, progress_color=AKCENT, font=ctk.CTkFont(size=12),
            command=self._zapisz_auto,
        )
        self._przelacznik_auto.pack(anchor="w", padx=12, pady=(0, 12))
        if self.ustawienia["transkrybuj_po_stopie"]:
            self._przelacznik_auto.select()

        # Podsumowanie AI
        pudlo = sekcja(t("Podsumowanie AI (opcjonalne)"))
        self._przelacznik_podsumowania = ctk.CTkSwitch(
            pudlo, text=t("Podsumowuj po transkrypcji"), text_color=TEKST,
            progress_color=AKCENT, font=ctk.CTkFont(size=12),
            command=self._zapisz_podsumowanie,
        )
        self._przelacznik_podsumowania.pack(anchor="w", padx=12, pady=(12, 4))
        if self.ustawienia["podsumowanie"]:
            self._przelacznik_podsumowania.select()

        self._przelacznik_nazwy = ctk.CTkSwitch(
            pudlo, text=t("Nazywaj folder tematem i imionami"), text_color=TEKST,
            progress_color=AKCENT, font=ctk.CTkFont(size=12),
            command=self._zapisz_zmiane_nazwy,
        )
        self._przelacznik_nazwy.pack(anchor="w", padx=12, pady=(0, 8))
        if self.ustawienia["zmiana_nazwy"]:
            self._przelacznik_nazwy.select()

        etykiety_llm = [f"{k.split('/')[-1]} - {t(v['opis'])}"
                        for k, v in config.MODELE_LLM.items()]
        self._mapa_llm = dict(zip(etykiety_llm, config.MODELE_LLM.keys(), strict=True))
        self._menu_llm = ctk.CTkOptionMenu(
            pole(pudlo, t("Model językowy")), values=etykiety_llm, fg_color=PANEL_JASNY,
            button_color=PANEL_JASNY, button_hover_color=TLO, text_color=TEKST,
            dynamic_resizing=False, command=self._zapisz_model_llm,
        )
        for etykieta, repo in self._mapa_llm.items():
            if repo == self.ustawienia["model_llm"]:
                self._menu_llm.set(etykieta)
        self._menu_llm.pack(fill="x")

        self._etykieta_llm = ctk.CTkLabel(
            pudlo, text="", text_color=TEKST_PRZYGASZONY, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self._etykieta_llm.pack(fill="x", padx=12, pady=(0, 6))

        self._przycisk_pobierz_llm = ctk.CTkButton(
            pudlo, text=t("Pobierz model"), height=32, corner_radius=8, fg_color=AKCENT,
            hover_color=AKCENT_HOVER, text_color="#0b1220",
            command=self._pobierz_model_llm,
        )
        self._przycisk_pobierz_llm.pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkLabel(
            pudlo,
            text=t("Model rusza dopiero po transkrypcji i jest zwalniany zaraz po niej —\n"
                   "nie zostaje w pamięci ani w tle. Działa lokalnie, bez sieci."),
            text_color=TEKST_PRZYGASZONY, anchor="w", justify="left",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=12, pady=(0, 12))

        # Pozostale
        pudlo = sekcja(t("Pozostałe"))
        wiersz = pole(pudlo, t("Katalog nagrań"))
        self._pole_katalogu = ctk.CTkEntry(
            wiersz, height=32, fg_color=PANEL_JASNY, border_color=PANEL_JASNY,
            text_color=TEKST,
        )
        self._pole_katalogu.insert(0, str(self.ustawienia.katalog_nagran))
        self._pole_katalogu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(wiersz, text=t("Zapisz"), width=72, height=32, corner_radius=8,
                      fg_color=PANEL_JASNY, hover_color=AKCENT, text_color=TEKST,
                      command=self._zapisz_katalog).pack(side="left", padx=(6, 0))

        etykiety_ui = [t(v) for v in config.ETYKIETY_INTERFEJSU.values()]
        self._mapa_ui = dict(zip(etykiety_ui, config.ETYKIETY_INTERFEJSU.keys(), strict=True))
        self._menu_ui = ctk.CTkOptionMenu(
            pole(pudlo, t("Język interfejsu (po ponownym uruchomieniu)")),
            values=etykiety_ui, fg_color=PANEL_JASNY, button_color=PANEL_JASNY,
            button_hover_color=TLO, text_color=TEKST, dynamic_resizing=False,
            command=self._zapisz_jezyk_interfejsu,
        )
        self._menu_ui.set(t(config.ETYKIETY_INTERFEJSU.get(
            self.ustawienia["jezyk_interfejsu"], config.ETYKIETY_INTERFEJSU["auto"])))
        self._menu_ui.pack(fill="x")

        ctk.CTkLabel(
            pudlo,
            text=t("Skrót globalny: {skrot}", skrot=self._opis_skrotu()) + "\n"
                 f"OpenVINO GenAI: {transcribe.wersja_genai()}  ·  {config.APLIKACJA} {config.WERSJA}",
            text_color=TEKST_PRZYGASZONY, anchor="w", justify="left",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=12, pady=(4, 12))

        self.after(300, self._odswiez_status_modelu)
        self.after(300, self._odswiez_status_llm)
        return ramka

    # --- ustawienia: obsluga ---------------------------------------------

    def _wczytaj_urzadzenia(self) -> None:
        try:
            urzadzenia = audio.lista_urzadzen()
        except Exception as blad:
            log.exception("Nie udalo sie wyliczyc urzadzen audio")
            self._kolejka.put(("blad", t("Błąd urządzeń audio: {blad}", blad=blad)))
            return
        self._kolejka.put(("urzadzenia", urzadzenia))

    def _zastosuj_urzadzenia(self, urzadzenia: dict) -> None:
        self._urzadzenia = urzadzenia

        def wypelnij(menu, lista, wybrany_index):
            etykiety = [t("(domyślny)")] + [u.etykieta for u in lista]
            menu.configure(values=etykiety)
            wybrana = t("(domyślny)")
            for urz in lista:
                if urz.index == wybrany_index:
                    wybrana = urz.etykieta
            menu.set(wybrana)

        wypelnij(self._menu_mikrofon, urzadzenia["mikrofony"],
                 self.ustawienia["mikrofon_index"])
        wypelnij(self._menu_system, urzadzenia["system"],
                 self.ustawienia["system_index"])

        if not urzadzenia["system"]:
            self._etykieta_stanu.configure(
                text=t("Uwaga: brak urządzenia loopback — dźwięk systemowy niedostępny"),
                text_color=CZERWIEN,
            )

    def _zapisz_urzadzenia(self) -> None:
        def index_dla(menu, lista):
            wybrana = menu.get()
            if wybrana == t("(domyślny)"):
                return None
            for urz in lista:
                if urz.etykieta == wybrana:
                    return urz.index
            return None

        self.ustawienia["mikrofon_index"] = index_dla(
            self._menu_mikrofon, self._urzadzenia["mikrofony"])
        self.ustawienia["system_index"] = index_dla(
            self._menu_system, self._urzadzenia["system"])
        self.ustawienia.zapisz()

    def _zapisz_sciezki(self, wartosc: str) -> None:
        for kod, etykieta in config.ETYKIETY_SCIEZEK.items():
            if t(etykieta) == wartosc:
                self.ustawienia["sciezki"] = kod
        self.ustawienia.zapisz()

    def _zapisz_model(self, wartosc: str) -> None:
        self.ustawienia["model"] = self._mapa_modeli.get(wartosc, config.MODEL_DOMYSLNY)
        self.ustawienia.zapisz()
        self._odswiez_status_modelu()

    def _zapisz_device(self, wartosc: str) -> None:
        self.ustawienia["device"] = wartosc
        self.ustawienia.zapisz()

    def _zapisz_auto(self) -> None:
        self.ustawienia["transkrybuj_po_stopie"] = bool(self._przelacznik_auto.get())
        self.ustawienia.zapisz()

    def _zapisz_podsumowanie(self) -> None:
        self.ustawienia["podsumowanie"] = bool(self._przelacznik_podsumowania.get())
        self.ustawienia.zapisz()

    def _zapisz_zmiane_nazwy(self) -> None:
        self.ustawienia["zmiana_nazwy"] = bool(self._przelacznik_nazwy.get())
        self.ustawienia.zapisz()

    def _zapisz_model_llm(self, wartosc: str) -> None:
        self.ustawienia["model_llm"] = self._mapa_llm.get(
            wartosc, config.MODEL_LLM_DOMYSLNY)
        self.ustawienia.zapisz()
        self._odswiez_status_llm()

    def _zapisz_jezyk_interfejsu(self, wartosc: str) -> None:
        self.ustawienia["jezyk_interfejsu"] = self._mapa_ui.get(wartosc, "auto")
        self.ustawienia.zapisz()

    def _zapisz_katalog(self) -> None:
        sciezka = Path(self._pole_katalogu.get().strip())
        try:
            sciezka.mkdir(parents=True, exist_ok=True)
        except OSError as blad:
            self._etykieta_stanu.configure(text=t("Zły katalog: {blad}", blad=blad), text_color=CZERWIEN)
            return
        self.ustawienia["katalog_nagran"] = str(sciezka)
        self.ustawienia.zapisz()

    def _odswiez_status_modelu(self) -> None:
        model = self.ustawienia["model"]
        info = config.MODELE.get(model, {})
        if transcribe.model_pobrany(model):
            self._etykieta_modelu.configure(
                text=t("Model pobrany ({rozmiar})", rozmiar=info.get('rozmiar', '')), text_color=ZIELEN)
            # Wyszarzony, zeby nie wygladal na przycisk czekajacy na klikniecie.
            self._przycisk_pobierz.configure(
                state="disabled", text=t("Model gotowy"),
                fg_color=PANEL_JASNY, text_color_disabled=TEKST_PRZYGASZONY)
        else:
            self._etykieta_modelu.configure(
                text=t("Model niepobrany ({rozmiar})", rozmiar=info.get('rozmiar', '')),
                text_color=CZERWIEN)
            self._przycisk_pobierz.configure(
                state="normal", text=t("Pobierz model ({rozmiar})", rozmiar=info.get('rozmiar', '')),
                fg_color=AKCENT, text_color="#0b1220")

        # Import openvino trwa kilka sekund - nie blokujemy nim interfejsu.
        self._etykieta_device.configure(text=t("Sprawdzam dostępne urządzenia..."))
        threading.Thread(
            target=lambda: self._kolejka.put(
                ("urzadzenia_ov", transcribe.dostepne_urzadzenia())),
            daemon=True,
        ).start()

    def _odswiez_status_llm(self) -> None:
        model = self.ustawienia["model_llm"]
        info = config.MODELE_LLM.get(model, {})
        if transcribe.model_pobrany(model):
            self._etykieta_llm.configure(
                text=t("Model pobrany ({rozmiar})", rozmiar=info.get('rozmiar', '')), text_color=ZIELEN)
            self._przycisk_pobierz_llm.configure(
                state="disabled", text=t("Model gotowy"),
                fg_color=PANEL_JASNY, text_color_disabled=TEKST_PRZYGASZONY)
        else:
            self._etykieta_llm.configure(
                text=t("Model niepobrany ({rozmiar}) — bez niego transkrypcja działa, "
                       "tylko bez podsumowania", rozmiar=info.get('rozmiar', '')),
                text_color=CZERWIEN)
            self._przycisk_pobierz_llm.configure(
                state="normal", text=t("Pobierz model ({rozmiar})", rozmiar=info.get('rozmiar', '')),
                fg_color=AKCENT, text_color="#0b1220")

    def _pobierz_model(self) -> None:
        model = self.ustawienia["model"]
        self._przycisk_pobierz.configure(state="disabled", text=t("Pobieram..."))

        def zadanie():
            try:
                transcribe.sciezka_modelu(model, pobierz=True)
                self._kolejka.put(("model_pobrany", model))
            except Exception as blad:
                self._kolejka.put(("blad", t("Pobieranie modelu nie powiodło się: {blad}", blad=blad)))
                self._kolejka.put(("model_pobrany", model))

        threading.Thread(target=zadanie, daemon=True).start()

    def _pobierz_model_llm(self) -> None:
        model = self.ustawienia["model_llm"]
        self._przycisk_pobierz_llm.configure(state="disabled", text=t("Pobieram..."))

        def zadanie():
            try:
                transcribe.sciezka_modelu(model, pobierz=True)
            except Exception as blad:
                self._kolejka.put(
                    ("blad", t("Pobieranie modelu językowego nie powiodło się: {blad}", blad=blad)))
            self._kolejka.put(("model_llm_pobrany", model))

        threading.Thread(target=zadanie, daemon=True).start()

    # --- nagrywanie -------------------------------------------------------

    def przelacz_nagrywanie(self) -> None:
        if self.nagrywa:
            self.zatrzymaj_nagrywanie()
        else:
            self.rozpocznij_nagrywanie()

    def rozpocznij_nagrywanie(self) -> None:
        if self.nagrywa or self.transkrybuje:
            return

        nazwa = self._pole_nazwy.get().strip() or t("Spotkanie")
        znacznik = datetime.now().strftime("%Y-%m-%d_%H-%M")
        katalog = self.ustawienia.katalog_nagran / f"{znacznik}_{slug(nazwa) or 'Spotkanie'}"

        self.nagrywarka = audio.Nagrywarka(
            tryb=self.ustawienia["sciezki"],
            mikrofon_index=self.ustawienia["mikrofon_index"],
            system_index=self.ustawienia["system_index"],
        )
        try:
            self.nagrywarka.start(katalog)
        except Exception as blad:
            log.exception("Nie udalo sie rozpoczac nagrywania")
            self._etykieta_stanu.configure(text=str(blad), text_color=CZERWIEN)
            self.nagrywarka = None
            return

        self.nagrywa = True
        self.nazwa_sesji = nazwa
        self.katalog_sesji = katalog
        self.start_nagrania = time.time()

        self._przycisk_rec.configure(text="STOP", fg_color=PANEL_JASNY,
                                     hover_color=CZERWIEN)
        stan = t("Nagrywanie...")
        if self.nagrywarka.ostrzezenia:
            stan += "  (" + "; ".join(self.nagrywarka.ostrzezenia) + ")"
        self._etykieta_stanu.configure(text=stan, text_color=CZERWIEN)
        self._pole_nazwy.configure(state="disabled")
        self._zasobnik.ustaw_stan(True, t("{app} - nagrywanie", app=config.APLIKACJA))
        self._pokaz_widok("nagrywanie")

    def zatrzymaj_nagrywanie(self) -> None:
        if not self.nagrywa or self.nagrywarka is None:
            return

        self.nagrywa = False
        wynik = self.nagrywarka.stop()
        self.nagrywarka = None

        self._przycisk_rec.configure(text="REC", fg_color=CZERWIEN,
                                     hover_color=CZERWIEN_HOVER)
        self._etykieta_stanu.configure(text=t("Zatrzymano"), text_color=TEKST_PRZYGASZONY)
        self._pole_nazwy.configure(state="normal")
        self._zasobnik.ustaw_stan(False)
        for pasek in self._poziomy_ui.values():
            pasek.set(0)

        pliki = {n: d["plik"] for n, d in wynik["sciezki"].items()}
        if not pliki:
            self._etykieta_stanu.configure(text=t("Nic nie nagrano."), text_color=CZERWIEN)
            return

        meta = {
            "nazwa": self.nazwa_sesji,
            "data": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "trwanie_s": wynik["trwanie_s"],
            "model": self.ustawienia["model"],
            "jezyk": self.ustawienia["jezyk"],
            "urzadzenie_mikrofon": wynik["sciezki"].get("mikrofon", {}).get("urzadzenie"),
            "urzadzenie_system": wynik["sciezki"].get("system", {}).get("urzadzenie"),
            "genai": transcribe.wersja_genai(),
        }
        (wynik["katalog"] / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        if self.ustawienia["transkrybuj_po_stopie"]:
            self._uruchom_transkrypcje(wynik["katalog"], pliki, meta)
        else:
            self._pokaz_widok("nagrania")

    # --- transkrypcja -----------------------------------------------------

    def _transkrybuj_ponownie(self, sesja: Path) -> None:
        pliki = {}
        for nazwa, plik in (("mikrofon", "mikrofon.flac"), ("system", "spotkanie.flac")):
            if (sesja / plik).exists():
                pliki[nazwa] = sesja / plik
        if not pliki:
            self._etykieta_stanu.configure(text=t("Brak plików audio w tej sesji."),
                                           text_color=CZERWIEN)
            return

        meta = {}
        if (sesja / "meta.json").exists():
            try:
                meta = json.loads((sesja / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        meta.pop("segmenty", None)
        meta.setdefault("nazwa", sesja.name)
        meta["model"] = self.ustawienia["model"]
        meta["jezyk"] = self.ustawienia["jezyk"]
        self._uruchom_transkrypcje(sesja, pliki, meta)

    def _uruchom_transkrypcje(self, katalog: Path, pliki: dict, meta: dict) -> None:
        if self.transkrybuje:
            return

        # Ustawienia wczytujemy raz, przy starcie, a aplikacja potrafi chodzic
        # w zasobniku tygodniami. Jesli plik zmienil sie w miedzyczasie (recznie
        # albo z drugiej instancji), po nagraniu siegnelibysmy po nieaktualny
        # model - lacznie z takim, ktorego juz nie ma na dysku. Kazda zmiana
        # z UI ladunie na dysk od razu, wiec plik jest zawsze co najmniej tak
        # swiezy jak pamiec.
        self.ustawienia.wczytaj()

        if not transcribe.model_pobrany(self.ustawienia["model"]):
            # Bez tego wpisu w logu nie ma sladu, ze transkrypcja w ogole miala
            # ruszyc - a komunikat w oknie przepada, gdy aplikacja siedzi w tacce.
            log.warning("Transkrypcja pominieta: model %r nie jest pobrany",
                        self.ustawienia["model"])
            self._etykieta_stanu.configure(
                text=t("Model nie jest pobrany — wejdź w Ustawienia."), text_color=CZERWIEN)
            self._pokaz_widok("ustawienia")
            return

        self.transkrybuje = True
        self.katalog_sesji = katalog
        self.anuluj_transkrypcje.clear()

        self._tytul_transkrypcji.configure(text=meta.get("nazwa", katalog.name))
        self._podtytul_transkrypcji.configure(
            text=f"{self.ustawienia['device']}  ·  {self.ustawienia['model'].split('/')[-1]}")
        self._pole_transkrypcji.delete("1.0", "end")
        self._karta_podsumowania.pack_forget()
        self._pasek_postepu.set(0)
        self._etykieta_postepu.configure(text=t("Ładuję model..."))
        self._przycisk_anuluj.configure(state="normal", text=t("Przerwij"))
        self._pokaz_widok("transkrypcja")

        model = self.ustawienia["model"]
        device = self.ustawienia["device"]
        jezyk = self.ustawienia["jezyk"]

        def zadanie():
            start = time.monotonic()
            try:
                segmenty, uzyty_device = transcribe.transkrybuj_sesje(
                    pliki, model, device, jezyk,
                    postep=lambda frakcja, tekst: self._kolejka.put(
                        ("postep", frakcja, tekst)),
                    anuluj=self.anuluj_transkrypcje,
                    status=lambda tekst: self._kolejka.put(("status_transkrypcji", tekst)),
                )
            except Exception as blad:
                log.exception("Transkrypcja nie powiodla sie")
                self._kolejka.put(("transkrypcja_blad", str(blad)))
                return

            pelne = dict(meta)
            pelne["device"] = uzyty_device
            pelne["czas_transkrypcji_s"] = round(time.monotonic() - start, 1)
            try:
                zapisz_wyniki(katalog, segmenty, pelne)
            except Exception as blad:
                self._kolejka.put(("transkrypcja_blad", t("Zapis nie powiódł się: {blad}", blad=blad)))
                return
            # Transkrypt pokazujemy od razu; podsumowanie dochodzi osobnym
            # komunikatem, bo model jezykowy potrzebuje jeszcze pol minuty.
            # Flaga mowi widokowi, czy zostawic czynny przycisk "Przerwij".
            self._kolejka.put(
                ("transkrypcja_koniec", segmenty, pelne, self._bedzie_podsumowanie(segmenty)))
            self._podsumuj(katalog, segmenty, pelne)

        threading.Thread(target=zadanie, daemon=True).start()

    # --- podsumowanie -----------------------------------------------------

    def _bedzie_podsumowanie(self, segmenty: list[Segment]) -> bool:
        return bool(
            segmenty
            and self.ustawienia["podsumowanie"]
            and transcribe.model_pobrany(self.ustawienia["model_llm"])
        )

    def _podsumuj(self, katalog: Path, segmenty: list[Segment], meta: dict,
                  wymus: bool = False) -> None:
        """Uruchamia lokalny model jezykowy i porzadkuje sesje. Wolac z watku!

        Zaden blad tego kroku nie moze przekreslic gotowego transkryptu -
        w najgorszym razie sesja zostaje bez podsumowania i ze stara nazwa.
        """
        if not segmenty or not (wymus or self.ustawienia["podsumowanie"]):
            return
        self.podsumowuje = True
        try:
            self._podsumuj_teraz(katalog, segmenty, meta)
        finally:
            self.podsumowuje = False

    def _podsumuj_teraz(self, katalog: Path, segmenty: list[Segment], meta: dict) -> None:
        model = self.ustawienia["model_llm"]
        if not transcribe.model_pobrany(model):
            self._kolejka.put(
                ("podsumowanie_blad", t("Model podsumowania nie jest pobrany — Ustawienia.")))
            return

        try:
            wynik = podsum.podsumuj(
                segmenty, model, self.ustawienia["device"],
                status=lambda tekst: self._kolejka.put(("status_transkrypcji", tekst)),
                anuluj=self.anuluj_transkrypcje,
            )
        except Exception as blad:
            log.exception("Podsumowanie nie powiodlo sie")
            self._kolejka.put(("podsumowanie_blad", str(blad)))
            return

        if self.anuluj_transkrypcje.is_set():
            self._kolejka.put(("podsumowanie_blad", t("przerwane")))
            return
        if not wynik:
            self._kolejka.put(("podsumowanie_blad", t("Model nie zwrócił podsumowania.")))
            return

        try:
            (katalog / "podsumowanie.md").write_text(
                podsum.jako_markdown(wynik, meta), encoding="utf-8")
            meta.update(wynik)
            # Drugi przebieg zapisu - transkrypt.md dostaje blok podsumowania,
            # meta.json nowe pola. zapisz_wyniki jest idempotentne.
            zapisz_wyniki(katalog, segmenty, meta)
        except OSError as blad:
            log.exception("Nie udalo sie zapisac podsumowania")
            self._kolejka.put(("podsumowanie_blad", t("Zapis podsumowania: {blad}", blad=blad)))
            return

        if self.ustawienia["zmiana_nazwy"]:
            katalog = self._zmien_nazwe_sesji(katalog, wynik)

        self._kolejka.put(("podsumowanie", katalog, wynik))

    @staticmethod
    def _zmien_nazwe_sesji(katalog: Path, wynik: dict) -> Path:
        """Podmienia czlon nazwy na temat i imiona. Zwraca aktualna sciezke."""
        nowa_nazwa = podsum.nazwa_katalogu(katalog.name, wynik)
        if not nowa_nazwa:
            return katalog

        cel = katalog.parent / nowa_nazwa
        licznik = 2
        while cel.exists():
            cel = katalog.parent / f"{nowa_nazwa}-{licznik}"
            licznik += 1

        try:
            katalog.rename(cel)
        except OSError as blad:
            # Najczestszy powod: folder jest otwarty w Eksploratorze. Nie jest to
            # blad transkrypcji - pliki sa na miejscu, tylko nazwa zostaje stara.
            log.warning("Nie udalo sie zmienic nazwy %s -> %s: %s",
                        katalog.name, nowa_nazwa, blad)
            return katalog

        log.info("Sesja przemianowana: %s -> %s", katalog.name, cel.name)
        return cel

    def _podsumuj_ponownie(self, sesja: Path) -> None:
        """Podsumowanie samo w sobie - bez ponownego mielenia audio Whisperem."""
        if self.transkrybuje or self.podsumowuje:
            return
        try:
            meta = json.loads((sesja / "meta.json").read_text(encoding="utf-8"))
            segmenty = [Segment(**s) for s in meta.get("segmenty", [])]
        except (OSError, ValueError, TypeError):
            log.exception("Nie udalo sie wczytac segmentow z %s", sesja)
            segmenty, meta = [], {}
        if not segmenty:
            self._etykieta_stanu.configure(
                text=t("Brak segmentów w meta.json — użyj „Transkrybuj ponownie”."),
                text_color=CZERWIEN)
            self._pokaz_widok("nagrania")
            return

        self.podsumowuje = True
        self.katalog_sesji = sesja
        self.anuluj_transkrypcje.clear()
        self._tytul_transkrypcji.configure(text=meta.get("nazwa", sesja.name))
        self._podtytul_transkrypcji.configure(
            text=self.ustawienia["model_llm"].split("/")[-1])
        self._pasek_postepu.set(1.0)
        self._etykieta_postepu.configure(text=t("Podsumowuję..."))
        self._karta_podsumowania.pack_forget()
        self._przycisk_anuluj.configure(state="normal", text=t("Przerwij"))
        self._pokaz_transkrypt(segmenty)
        self._pokaz_widok("transkrypcja")

        threading.Thread(
            target=lambda: self._podsumuj(sesja, segmenty, meta, wymus=True),
            daemon=True,
        ).start()

    def _przerwij_transkrypcje(self) -> None:
        if self.transkrybuje or self.podsumowuje:
            self.anuluj_transkrypcje.set()
            self._etykieta_postepu.configure(text=t("Przerywam..."))
            self._przycisk_anuluj.configure(state="disabled")

    # --- petla UI ---------------------------------------------------------

    def _tick(self) -> None:
        if self.nagrywa and self.nagrywarka is not None:
            self._etykieta_czasu.configure(
                text=czas_hms(time.time() - self.start_nagrania))
            poziomy = self.nagrywarka.poziomy
            for klucz, pasek in self._poziomy_ui.items():
                pasek.set(poziomy.get(klucz, 0.0))

        while True:
            try:
                komunikat = self._kolejka.get_nowait()
            except queue.Empty:
                break
            self._obsluz_komunikat(komunikat)

        self.after(100, self._tick)

    def _obsluz_komunikat(self, komunikat: tuple) -> None:
        rodzaj = komunikat[0]

        if rodzaj == "urzadzenia":
            self._zastosuj_urzadzenia(komunikat[1])

        elif rodzaj == "status_transkrypcji":
            self._etykieta_postepu.configure(text=komunikat[1])

        elif rodzaj == "urzadzenia_ov":
            urzadzenia = komunikat[1]
            self._menu_device.configure(values=urzadzenia)
            wybrane = self.ustawienia["device"]
            self._menu_device.set(wybrane if wybrane in urzadzenia else urzadzenia[0])
            self._etykieta_device.configure(
                text=t("Wykryte przez OpenVINO: {lista}", lista=", ".join(urzadzenia)))

        elif rodzaj == "postep":
            _, frakcja, tekst = komunikat
            self._pasek_postepu.set(max(0.0, min(1.0, frakcja)))
            self._etykieta_postepu.configure(text=f"{int(frakcja * 100)}%")
            if tekst:
                self._pole_transkrypcji.insert("end", tekst.strip() + "\n\n")
                self._pole_transkrypcji.see("end")

        elif rodzaj == "transkrypcja_koniec":
            _, segmenty, meta, bedzie_podsumowanie = komunikat
            self.transkrybuje = False
            self._pasek_postepu.set(1.0)
            czas = meta.get("czas_transkrypcji_s", 0)
            self._etykieta_postepu.configure(
                text=t("Gotowe w {czas} s  ·  {device}  ·  {n} wypowiedzi",
                       czas=f"{czas:.0f}", device=meta.get('device', '?'),
                       n=len(segmenty)))
            self._pokaz_transkrypt(segmenty)
            # Przy podsumowaniu przycisk zostaje czynny - inaczej nie dalo by sie
            # przerwac modelu jezykowego, ktory dopiero rusza.
            if not bedzie_podsumowanie:
                self._przycisk_anuluj.configure(state="disabled", text=t("Zakończono"))

        elif rodzaj == "transkrypcja_blad":
            self.transkrybuje = False
            self._etykieta_postepu.configure(text=t("Błąd: {blad}", blad=komunikat[1]))
            self._przycisk_anuluj.configure(state="disabled", text=t("Błąd"))

        elif rodzaj == "podsumowanie":
            _, katalog, wynik = komunikat
            # Katalog mogl zmienic nazwe - inaczej "Otworz folder" wskazywalby
            # na sciezke, ktora juz nie istnieje.
            self.katalog_sesji = katalog
            self._pokaz_podsumowanie(wynik)
            self._przycisk_anuluj.configure(state="disabled", text=t("Zakończono"))

        elif rodzaj == "podsumowanie_blad":
            log.warning("Podsumowanie pominiete: %s", komunikat[1])
            self._etykieta_postepu.configure(
                text=t("{tekst}  ·  bez podsumowania ({powod})",
                       tekst=self._etykieta_postepu.cget('text'), powod=komunikat[1]))
            self._przycisk_anuluj.configure(state="disabled", text=t("Zakończono"))

        elif rodzaj == "model_pobrany":
            self._odswiez_status_modelu()

        elif rodzaj == "model_llm_pobrany":
            self._odswiez_status_llm()

        elif rodzaj == "blad":
            self._etykieta_stanu.configure(text=komunikat[1], text_color=CZERWIEN)

    def _pokaz_podsumowanie(self, wynik: dict) -> None:
        if wynik.get("temat"):
            self._tytul_transkrypcji.configure(text=wynik["temat"])

        naglowek = t("Uczestnicy: {lista}", lista=(", ".join(wynik["uczestnicy"])
                                                if wynik.get("uczestnicy")
                                                else t("nie rozpoznano imion")))
        self._etykieta_uczestnikow.configure(text=naglowek)

        linie = [wynik.get("tldr", "")]
        if wynik.get("punkty"):
            linie += [""] + [f"•  {punkt}" for punkt in wynik["punkty"]]
        if wynik.get("ustalenia"):
            linie += ["", t("Ustalenia:")] + [f"→  {u}" for u in wynik["ustalenia"]]
        self._etykieta_podsumowania.configure(text="\n".join(linie).strip())

        self._karta_podsumowania.pack(fill="x", pady=(0, 10), before=self._pole_transkrypcji)

    def _pokaz_transkrypt(self, segmenty: list[Segment]) -> None:
        self._pole_transkrypcji.delete("1.0", "end")
        if not segmenty:
            self._pole_transkrypcji.insert("end", t("Nie rozpoznano żadnej mowy.") + "\n")
            return
        for seg in segmenty:
            self._pole_transkrypcji.insert(
                "end", f"[{czas_hms(seg.start)}] {seg.mowca}: {seg.tekst}\n\n")
        self._pole_transkrypcji.see("1.0")

    # --- pomocnicze -------------------------------------------------------

    def _otworz(self, sciezka: Path) -> None:
        try:
            os.startfile(str(sciezka))
        except Exception as blad:
            log.warning("Nie udalo sie otworzyc %s: %s", sciezka, blad)

    def _otworz_folder_sesji(self) -> None:
        if self.katalog_sesji is not None:
            self._otworz(self.katalog_sesji)

    def _pokaz_okno(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _na_zamkniecie(self) -> None:
        if self.ustawienia["tray"]:
            self.withdraw()
        else:
            self.zakoncz()

    def zakoncz(self) -> None:
        if self.nagrywa:
            # Nie gubimy trwajacego nagrania - domykamy pliki poprawnie.
            self.zatrzymaj_nagrywanie()
        self.anuluj_transkrypcje.set()
        self._skrot.stop()
        self._zasobnik.stop()
        self.ustawienia.zapisz()
        self.destroy()


def polecenie_startowe() -> str:
    """Polecenie, ktorym pasek zadan ma wskrzesic aplikacje z przypietego kafelka.

    Celowo `uruchom.pyw`, a nie `-m ucho`: kafelek startuje bez katalogu
    roboczego projektu, a skrypt sam dokłada go do `sys.path`.
    """
    interpreter = Path(sys.executable)
    bez_konsoli = interpreter.with_name("pythonw.exe")
    if bez_konsoli.exists():
        interpreter = bez_konsoli
    starter = config.KATALOG_PROJEKTU / "uruchom.pyw"
    return f'"{interpreter}" "{starter}"'


def _oznacz_skroty() -> None:
    for skrot in winshell.sciezki_skrotow():
        if skrot.exists():
            log.info("ID w skrocie %s: %s", skrot.name,
                     winshell.ustaw_id_skrotu(skrot, config.ID_APLIKACJI))


def _ustaw_id_aplikacji() -> None:
    """Wlasny AppUserModelID - inaczej pasek zadan pokazuje ikone pythonw.exe.

    Musi byc wywolane zanim powstanie pierwsze okno.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            config.ID_APLIKACJI
        )
    except Exception:
        log.debug("Nie udalo sie ustawic AppUserModelID", exc_info=True)


def uruchom() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        filename=str(config.PLIK_LOGU),
        filemode="a",
        encoding="utf-8",
    )
    uzyty = i18n.ustaw_jezyk(config.Ustawienia()["jezyk_interfejsu"])
    log.info("Start %s %s (interfejs: %s)", config.APLIKACJA, config.WERSJA, uzyty)
    _ustaw_id_aplikacji()
    Aplikacja().mainloop()
