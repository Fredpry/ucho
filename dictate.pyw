# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Starts the Dictate app independently of the working directory (see ucho.pyw)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ucho.dictate import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
