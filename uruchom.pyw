# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Start aplikacji niezalezny od katalogu roboczego.

`pythonw -m ucho` wymaga, zeby biezacym katalogiem byl katalog projektu.
Pasek zadan uruchamia przypiety kafelek bez takiej gwarancji, wiec skroty i
polecenie przypiecia wskazuja ten plik: jego katalog trafia na poczatek
`sys.path`, wiec pakiet `ucho` jest widoczny zawsze.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ucho.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
