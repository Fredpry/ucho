# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Punkt wejscia: `pythonw -m ucho`.

Uruchamiamy pod pythonw (bez konsoli), wiec kazdy nieobsluzony wyjatek musi
trafic do pliku logu i do okienka - inaczej aplikacja znika bez sladu.
"""

from __future__ import annotations

import sys
import traceback

from . import config
from .i18n import t


def _pokaz_blad(tresc: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        korzen = tk.Tk()
        korzen.withdraw()
        messagebox.showerror(t("{app} — błąd uruchomienia", app=config.APLIKACJA), tresc)
        korzen.destroy()
    except Exception:
        pass


def main() -> int:
    try:
        from .app import uruchom

        uruchom()
        return 0
    except Exception:
        slad = traceback.format_exc()
        try:
            with open(config.PLIK_LOGU, "a", encoding="utf-8") as plik:
                plik.write("\n=== Nieobsluzony wyjatek ===\n" + slad)
        except OSError:
            pass
        _pokaz_blad(
            t("Aplikacja nie wystartowała.") + "\n\n"
            f"{slad.strip().splitlines()[-1]}\n\n"
            + t("Pełny ślad: {plik}", plik=config.PLIK_LOGU)
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
