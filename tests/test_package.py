# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Release package, installer scripts and the one version number."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from ucho import config
from ucho_mcp import server

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["install.ps1", "uninstall.ps1", "install.cmd", "uninstall.cmd"]

_SPEC = importlib.util.spec_from_file_location("build_package", ROOT / "tools" / "build_package.py")
build_package = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_package)


def test_package_ships_installer_and_uninstaller():
    shipped = {p.relative_to(ROOT).as_posix() for p in build_package.contents()}
    assert set(SCRIPTS) <= shipped
    assert "CHANGELOG.md" in shipped


def test_package_layout_matches_readme():
    # Extracted to C:\Users\<you> the app lands in C:\Users\<you>\Ucho\app,
    # the path both READMEs use for the MCP command.
    assert build_package.PACKAGE_DIR == "Ucho/app"
    for readme, you in (("README.md", "<you>"), ("README.pl.md", "<Ty>")):
        text = (ROOT / readme).read_text(encoding="utf-8")
        app = rf"C:\Users\{you}\Ucho\app"
        assert f'claude mcp add -s user ucho -- "{app}\\.venv\\Scripts\\python.exe" "{app}\\mcp_server.py"' in text
        assert "install.cmd" in text and "uninstall.cmd" in text
        # The Cursor / Claude Desktop and Codex snippets parse and use the same paths.
        mcp = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))["mcpServers"]["ucho"]
        codex = tomllib.loads(re.search(r"```toml\n(.*?)```", text, re.S).group(1))["mcp_servers"]["ucho"]
        for entry in (mcp, codex):
            assert entry["command"] == rf"{app}\.venv\Scripts\python.exe"
            assert entry["args"] == [rf"{app}\mcp_server.py"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_ascii_with_crlf(name):
    # PowerShell 5.1 reads a file without a BOM as ANSI; cmd.exe mis-parses
    # labels in a batch file with bare LF line endings.
    data = (ROOT / name).read_bytes()
    assert data.isascii(), name
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), name


def test_install_cmd_runs_the_installer_with_pause():
    text = (ROOT / "install.cmd").read_text(encoding="ascii")
    assert '-ExecutionPolicy Bypass -File "%~dp0install.ps1" -Pause %*' in text


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell not available")
@pytest.mark.parametrize("name", ["install.ps1", "uninstall.ps1"])
def test_powershell_scripts_parse(name):
    command = (
        "$e = $null; "
        f"$null = [System.Management.Automation.Language.Parser]::ParseFile('{ROOT / name}', [ref]$null, [ref]$e); "
        "$e | ForEach-Object { $_.ToString() }; exit $e.Count"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_one_version_everywhere():
    assert re.fullmatch(r"\d+\.\d+\.\d+", config.VERSION)
    assert server.VERSION == config.VERSION
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## v{config.VERSION} " in changelog
