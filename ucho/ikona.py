# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Generowanie ikony aplikacji - dla okna, zasobnika i skrotow .lnk.

Ikona jest rysowana kodem zamiast dolaczana jako plik binarny: nie ma czego
pobierac, a wariant "nagrywa" powstaje przez zmiane jednego koloru.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

TLO = (24, 26, 32, 255)
AKCENT = (94, 164, 255, 255)
AKCENT_REC = (239, 68, 68, 255)


def rysuj(rozmiar: int = 256, nagrywa: bool = False) -> Image.Image:
    """Mikrofon na zaokraglonym kwadracie. Podczas nagrywania kapsula czerwienieje."""
    obraz = Image.new("RGBA", (rozmiar, rozmiar), (0, 0, 0, 0))
    rysownik = ImageDraw.Draw(obraz)
    s = rozmiar / 256.0
    kolor = AKCENT_REC if nagrywa else AKCENT

    rysownik.rounded_rectangle(
        [0, 0, rozmiar - 1, rozmiar - 1], radius=int(56 * s), fill=TLO
    )

    # Kapsula mikrofonu.
    rysownik.rounded_rectangle(
        [int(100 * s), int(56 * s), int(156 * s), int(150 * s)],
        radius=int(28 * s), fill=kolor,
    )

    # Kablak - luk obejmujacy kapsule.
    rysownik.arc(
        [int(76 * s), int(92 * s), int(180 * s), int(184 * s)],
        start=0, end=180, fill=kolor, width=max(2, int(12 * s)),
    )

    # Nozka i podstawka.
    rysownik.rounded_rectangle(
        [int(122 * s), int(176 * s), int(134 * s), int(204 * s)],
        radius=int(6 * s), fill=kolor,
    )
    rysownik.rounded_rectangle(
        [int(92 * s), int(198 * s), int(164 * s), int(210 * s)],
        radius=int(6 * s), fill=kolor,
    )
    return obraz


def zapisz_ico(sciezka: Path) -> Path:
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    rozmiary = [16, 24, 32, 48, 64, 128, 256]
    bazowa = rysuj(256)
    bazowa.save(sciezka, format="ICO", sizes=[(r, r) for r in rozmiary])
    return sciezka


if __name__ == "__main__":
    cel = Path(__file__).resolve().parent / "assets" / "ikona.ico"
    print(zapisz_ico(cel))
