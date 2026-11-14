# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
r"""Builds dist/Ucho-Windows.zip and dist/SHA256SUMS.txt.

The package holds only what the user needs: the app code, the MCP server,
the installer and uninstaller and the documents. No tests, tools, models or
data.

Everything sits under `Ucho/app/`. The README says: right click the zip,
Extract All, destination `C:\Users\<you>`. The app then lands in
`C:\Users\<you>\Ucho\app`, next to the default recordings folder
`C:\Users\<you>\Ucho\recordings` and outside OneDrive, which is the path the
README uses for the MCP command. Keeping the default destination also works
(`Downloads\Ucho-Windows\Ucho\app`); the installer prints the real paths.
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = "Ucho/app"
FILES = [
    "install.cmd", "install.ps1", "uninstall.cmd", "uninstall.ps1",
    "ucho.pyw", "dictate.pyw", "mcp_server.py", "requirements.txt",
    "requirements-parakeet.txt",
    "design/tokens.json",
    "LICENSE", "NOTICE", "README.md", "README.pl.md", "CHANGELOG.md",
    "PRIVACY.md", "THIRD_PARTY.md",
]
DIRS = ["ucho", "ucho_mcp"]
SKIPPED = {"__pycache__"}


def contents() -> list[Path]:
    result = [ROOT / p for p in FILES]
    for folder in DIRS:
        result += [p for p in (ROOT / folder).rglob("*")
                   if p.is_file() and not SKIPPED & set(p.parts) and p.suffix != ".pyc"]
    missing = [p for p in result if not p.exists()]
    if missing:
        raise SystemExit(f"Missing files: {missing}")
    return sorted(result)


def main() -> int:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    package = dist / "Ucho-Windows.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as zip_:
        for file in contents():
            zip_.write(file, f"{PACKAGE_DIR}/{file.relative_to(ROOT).as_posix()}")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{digest}  {package.name}\n", encoding="utf-8")
    print(f"{package} ({package.stat().st_size / 1e6:.1f} MB)\nSHA-256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
