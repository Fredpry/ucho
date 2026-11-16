# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Release package, installer scripts and the one version number."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pytest

from ucho import config, instance
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


def test_build_package_version():
    assert build_package.version() == config.VERSION


def test_stage_copies_contents_and_removes_stale_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "stage"
        dest.mkdir()
        stale = dest / "stale.txt"
        stale.write_text("stale", encoding="utf-8")

        result = build_package.stage(dest)

        assert result == dest
        assert not stale.exists()
        expected = {p.relative_to(ROOT).as_posix() for p in build_package.contents()}
        actual = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
        assert actual == expected


def test_find_iscc_from_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_iscc = Path(tmpdir) / "ISCC.exe"
        fake_iscc.write_bytes(b"")
        monkeypatch.setenv("ISCC", str(fake_iscc))
        assert build_package.find_iscc() == fake_iscc

        monkeypatch.setenv("ISCC", str(Path(tmpdir) / "missing.exe"))
        monkeypatch.setattr(build_package.shutil, "which", lambda _name: None)
        for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            monkeypatch.delenv(var, raising=False)
        assert build_package.find_iscc() is None


def test_inno_setup_script_metadata_and_format():
    data = (ROOT / "installer" / "ucho.iss").read_bytes()
    # Inno Setup 6 reads a UTF-8 file as Unicode only with a BOM; without it the
    # Polish messages would turn into mojibake.
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")

    text = data.decode("utf-8-sig")
    assert "AppId={{8E0B6C2A-5A7D-4F1E-9C3B-2D6A1F4E7B90}" in text
    assert "PrivilegesRequired=lowest" in text
    assert re.search(rf"^AppMutex={re.escape(instance.MUTEX_NAME)}\r?$", text, re.MULTILINE)
    assert r"DefaultDirName={%USERPROFILE}\Ucho\app" in text
    assert "OutputBaseFilename=Ucho-Setup" in text
    assert r'Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"' in text
    assert r'Name: "en"; MessagesFile: "compiler:Default.isl"' in text
    assert "install.ps1" in text and "-ExecutionPolicy Bypass" in text
    assert "uninstall.ps1" in text


def test_main_builds_zip_and_sha256sums_without_exe(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        dist = Path(tmpdir) / "dist"
        dist.mkdir()
        (dist / "Ucho-Setup.exe").write_bytes(b"stale")
        monkeypatch.setattr(build_package, "DIST", dist)

        assert build_package.main([]) == 0

        zip_path = dist / "Ucho-Windows.zip"
        sums_path = dist / "SHA256SUMS.txt"
        assert zip_path.is_file()
        assert not (dist / "Ucho-Setup.exe").exists()
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        assert sums_path.read_text(encoding="utf-8") == f"{digest}  Ucho-Windows.zip\n"
