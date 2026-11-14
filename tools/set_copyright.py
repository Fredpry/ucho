# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Adds or replaces the SPDX header in all source files.

Usage:
    python tools/set_copyright.py                      # default holder
    python tools/set_copyright.py "New holder"         # change the copyright holder
    python tools/set_copyright.py --check              # check only (CI)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOLDER = "Fryderyk Pryjma"
YEAR = "2026"
PATTERNS = ["ucho/**/*.py", "ucho_mcp/**/*.py", "tests/**/*.py", "tools/**/*.py", "*.py", "*.pyw"]
COPYRIGHT_LINE = re.compile(r"^# SPDX-FileCopyrightText: .*$", re.MULTILINE)
LICENSE_LINE = "# SPDX-License-Identifier: Apache-2.0"


def files() -> list[Path]:
    result: set[Path] = set()
    for pattern in PATTERNS:
        result.update(p for p in ROOT.glob(pattern) if ".venv" not in p.parts)
    return sorted(result)


def header(holder: str) -> str:
    return f"# SPDX-FileCopyrightText: {YEAR} {holder}\n{LICENSE_LINE}\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if not a.startswith("--")]
    holder = args[0] if args else HOLDER
    pending = []
    for file in files():
        text = file.read_text(encoding="utf-8")
        if COPYRIGHT_LINE.search(text) and LICENSE_LINE in text:
            new = COPYRIGHT_LINE.sub(f"# SPDX-FileCopyrightText: {YEAR} {holder}", text, count=1)
        else:
            new = header(holder) + text
        if new != text:
            pending.append(file.relative_to(ROOT))
            if not check:
                file.write_text(new, encoding="utf-8")
    if check and pending:
        print("Missing or different SPDX header:", *pending, sep="\n  ")
        return 1
    print(f"{'To change' if check else 'Changed'}: {len(pending)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
