# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
r"""Builds dist/Ucho-Windows.zip, optionally dist/Ucho-Setup.exe, and dist/SHA256SUMS.txt.

The package holds only what the user needs: the app code, the MCP server,
the installer and uninstaller and the documents. No tests, tools, models or
data.

Everything is staged once into `dist/stage` and both outputs are built from
the same staged tree:
- `dist/Ucho-Windows.zip` places everything under `Ucho/app/` (right-click,
  Extract All to `C:\Users\<you>` -> `C:\Users\<you>\Ucho\app`).
- `dist/Ucho-Setup.exe` (with `--installer`, built with Inno Setup 6.7.3)
  installs per-user without admin rights and runs `install.ps1`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
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


def version() -> str:
    """Read VERSION from ucho/config.py without importing it (imports set env vars)."""
    text = (ROOT / "ucho" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit("VERSION not found in ucho/config.py")
    return match.group(1)


def stage(dest: Path) -> Path:
    """Copy every packaged file into a clean staging directory."""
    if dest.exists():
        shutil.rmtree(dest)
    for file in contents():
        target = dest / file.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)
    return dest


def find_iscc() -> Path | None:
    """Locate the Inno Setup 6 compiler (ISCC.exe)."""
    if env := os.environ.get("ISCC"):
        candidate = Path(env)
        if candidate.is_file():
            return candidate
    if which := shutil.which("iscc"):
        return Path(which)
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        if base := os.environ.get(var):
            candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
            if candidate.is_file():
                return candidate
    return None


def build_installer(staged: Path, dist: Path) -> Path:
    """Compile dist/Ucho-Setup.exe from staged files using Inno Setup 6."""
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit(
            "Inno Setup 6 compiler (ISCC.exe) not found. "
            "Install Inno Setup 6 or set the ISCC environment variable."
        )
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from ucho.icon import save_ico

    ico = save_ico(dist / "build" / "ucho-setup.ico")
    subprocess.run(
        [
            str(iscc),
            "/Qp",
            f"/DAppVersion={version()}",
            f"/DSourceDir={staged}",
            f"/DOutputDir={dist}",
            f"/DSetupIcon={ico}",
            str(ROOT / "installer" / "ucho.iss"),
        ],
        check=True,
    )
    exe = dist / "Ucho-Setup.exe"
    if not exe.is_file():
        raise SystemExit(f"Installer was not produced: {exe}")
    return exe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Ucho Windows release package.")
    parser.add_argument(
        "--installer",
        action="store_true",
        help="Also build Ucho-Setup.exe with Inno Setup 6.",
    )
    args = parser.parse_args(argv)

    dist = DIST
    dist.mkdir(parents=True, exist_ok=True)
    # Remove any leftover installer so SHA256SUMS.txt only lists outputs from this run.
    stale_exe = dist / "Ucho-Setup.exe"
    if stale_exe.exists():
        stale_exe.unlink()

    staged = stage(dist / "stage")
    package = dist / "Ucho-Windows.zip"
    staged_files = sorted(p for p in staged.rglob("*") if p.is_file())
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as zip_:
        for file in staged_files:
            zip_.write(file, f"{PACKAGE_DIR}/{file.relative_to(staged).as_posix()}")

    artifacts = [package]
    if args.installer:
        artifacts.append(build_installer(staged, dist))
    artifacts.sort(key=lambda p: p.name)

    lines = []
    for artifact in artifacts:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}\n")
        print(f"{artifact} ({artifact.stat().st_size / 1e6:.1f} MB)\nSHA-256 {digest}")
    (dist / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
