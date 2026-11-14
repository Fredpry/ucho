# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Buduje dist/Ucho-Windows.zip i dist/SHA256SUMS.txt.

Do paczki trafia tylko to, czego potrzebuje uzytkownik: kod aplikacji, serwer
MCP, instalator i dokumenty. Bez testow, narzedzi, modeli i danych.
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
KATALOG_PACZKI = "Ucho"
PLIKI = [
    "instaluj.ps1", "uruchom.pyw", "dyktuj.pyw", "mcp_serwer.py", "requirements.txt",
    "dyktuj.example.json", "LICENSE", "NOTICE", "README.md", "README.pl.md",
    "PRIVACY.md", "THIRD_PARTY.md",
]
KATALOGI = ["ucho", "ucho_mcp"]
POMIJANE = {"__pycache__"}


def zawartosc() -> list[Path]:
    wynik = [KORZEN / p for p in PLIKI]
    for katalog in KATALOGI:
        wynik += [p for p in (KORZEN / katalog).rglob("*")
                  if p.is_file() and not POMIJANE & set(p.parts) and p.suffix != ".pyc"]
    brakujace = [p for p in wynik if not p.exists()]
    if brakujace:
        raise SystemExit(f"Brak plikow: {brakujace}")
    return sorted(wynik)


def main() -> int:
    dist = KORZEN / "dist"
    dist.mkdir(exist_ok=True)
    paczka = dist / "Ucho-Windows.zip"
    with zipfile.ZipFile(paczka, "w", compression=zipfile.ZIP_DEFLATED) as zip_:
        for plik in zawartosc():
            zip_.write(plik, f"{KATALOG_PACZKI}/{plik.relative_to(KORZEN).as_posix()}")
    skrot = hashlib.sha256(paczka.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{skrot}  {paczka.name}\n", encoding="utf-8")
    print(f"{paczka} ({paczka.stat().st_size / 1e6:.1f} MB)\nSHA-256 {skrot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
