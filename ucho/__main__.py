# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Entry point: `pythonw -m ucho`.

We run under pythonw (no console), so every unhandled exception must reach
the log file and a message box - otherwise the app vanishes without a trace.
"""

from __future__ import annotations

import sys
import traceback

from . import config
from .i18n import t


def _show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(t("{app} — startup error", app=config.APP_NAME), message)
        root.destroy()
    except Exception:
        pass


def main() -> int:
    try:
        from .app import run

        run()
        return 0
    except Exception:
        trace = traceback.format_exc()
        try:
            with open(config.LOG_FILE, "a", encoding="utf-8") as file:
                file.write("\n=== Unhandled exception ===\n" + trace)
        except OSError:
            pass
        _show_error(
            t("The application failed to start.") + "\n\n"
            f"{trace.strip().splitlines()[-1]}\n\n"
            + t("Full trace: {file}", file=config.LOG_FILE)
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
