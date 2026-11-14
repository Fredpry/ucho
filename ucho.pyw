# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""App start that does not depend on the working directory.

`pythonw -m ucho` needs the project folder as the current directory. The
taskbar starts a pinned tile without that guarantee, so the shortcuts and the
pin command point to this file: its folder goes first on `sys.path`, so the
`ucho` package is always found.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ucho.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
