# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dopisuje albo podmienia naglowek SPDX we wszystkich plikach zrodlowych.

Uzycie:
    python tools/set_copyright.py                      # posiadacz domyslny
    python tools/set_copyright.py "Nowy posiadacz"     # zmiana posiadacza praw
    python tools/set_copyright.py --check              # tylko sprawdz (CI)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
POSIADACZ = "Fryderyk Pryjma"
ROK = "2026"
WZORCE = ["ucho/**/*.py", "ucho_mcp/**/*.py", "tests/**/*.py", "tools/**/*.py", "*.py", "*.pyw"]
LINIA_PRAW = re.compile(r"^# SPDX-FileCopyrightText: .*$", re.MULTILINE)
LINIA_LICENCJI = "# SPDX-License-Identifier: Apache-2.0"


def pliki() -> list[Path]:
    wynik: set[Path] = set()
    for wzorzec in WZORCE:
        wynik.update(p for p in KORZEN.glob(wzorzec) if ".venv" not in p.parts)
    return sorted(wynik)


def naglowek(posiadacz: str) -> str:
    return f"# SPDX-FileCopyrightText: {ROK} {posiadacz}\n{LINIA_LICENCJI}\n"


def main(argv: list[str]) -> int:
    sprawdz = "--check" in argv
    argumenty = [a for a in argv if not a.startswith("--")]
    posiadacz = argumenty[0] if argumenty else POSIADACZ
    braki = []
    for plik in pliki():
        tekst = plik.read_text(encoding="utf-8")
        if LINIA_PRAW.search(tekst) and LINIA_LICENCJI in tekst:
            nowy = LINIA_PRAW.sub(f"# SPDX-FileCopyrightText: {ROK} {posiadacz}", tekst, count=1)
        else:
            nowy = naglowek(posiadacz) + tekst
        if nowy != tekst:
            braki.append(plik.relative_to(KORZEN))
            if not sprawdz:
                plik.write_text(nowy, encoding="utf-8")
    if sprawdz and braki:
        print("Brak albo inny naglowek SPDX:", *braki, sep="\n  ")
        return 1
    print(f"{'Do zmiany' if sprawdz else 'Zmieniono'}: {len(braki)} plikow")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
