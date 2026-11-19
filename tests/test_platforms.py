# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tests for the platform abstraction layer (`ucho/platforms/`)."""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucho import dictation, platforms
from ucho.platforms import base, null

ROOT = Path(__file__).resolve().parent.parent

# Legacy Windows-only modules whose module-level imports are removed in later tasks.
<<<<<<< HEAD
_M1B_EXCEPTIONS: set[str] = set()  # emptied by task M1b
_M1C_EXCEPTIONS = {"audio.py"}  # task M1c removes it
=======
_M1B_EXCEPTIONS = {"hotkeys.py", "instance.py", "winshell.py"}  # task M1b removes them
_M1C_EXCEPTIONS: set[str] = set()  # task M1c removed audio.py
>>>>>>> macos/m1c
MODULE_IMPORT_EXCEPTIONS = _M1B_EXCEPTIONS | _M1C_EXCEPTIONS

FORBIDDEN_MODULES = {
    "winreg",
    "winsound",
    "msvcrt",
    "pyaudiowpatch",
    "objc",
    "Foundation",
    "AppKit",
    "Cocoa",
    "Quartz",
    "ApplicationServices",
    "CoreAudio",
    "AVFoundation",
    "PyObjCTools",
    "sounddevice",
    "catap",
}


def test_family_and_override(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert platforms.family() == "windows"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert platforms.family() == "macos"
    monkeypatch.setattr(sys, "platform", "linux")
    assert platforms.family() == "null"

    with pytest.raises(ValueError):
        with platforms.override("unknown"):
            pass

    with platforms.override("macos"):
        assert platforms.family() == "macos"
        with platforms.override("null"):
            assert platforms.family() == "null"
        assert platforms.family() == "macos"
    assert platforms.family() == "null"


def test_platform_base_and_null_import_everywhere():
    assert hasattr(base, "Clipboard")
    assert hasattr(null, "NullClipboard")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only WinAPI modules")
def test_windows_platform_modules_import_on_windows():
    from ucho.platforms.windows import clipboard, hotkeys, inserter, instance, shell, sounds

    assert callable(clipboard.create)
    assert callable(hotkeys.create)
    assert callable(inserter.create)
    assert callable(instance.create)
    assert callable(shell.create)
    assert callable(sounds.create)


def test_missing_modules_fall_back_to_null_and_cache():
    singletons = (
        platforms.clipboard,
        platforms.inserter,
        platforms.sounds,
        platforms.shell,
        platforms.audio,
        platforms.sysinfo,
        platforms.fonts,
        platforms.permissions,
    )
    for fam in ("macos", "null"):
        with platforms.override(fam):
            for accessor in singletons:
                first = accessor()
                second = accessor()
                assert first is second
                assert type(first).__module__ == "ucho.platforms.null"

            h1 = platforms.hotkeys(lambda _n: None)
            h2 = platforms.hotkeys(lambda _n: None)
            assert h1 is not h2
            assert type(h1) is null.NullHotkeys

            i1 = platforms.instance()
            i2 = platforms.instance()
            assert i1 is not i2
            assert type(i1) is null.NullInstance


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _scan_expr(expr: ast.AST | None, file: Path, errors: list[str]) -> None:
    if expr is None:
        return
    stack: list[ast.AST] = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Attribute) and node.attr in ("WinDLL", "windll"):
            if isinstance(node.value, ast.Name) and node.value.id == "ctypes":
                errors.append(f"{file.relative_to(ROOT)}:{node.lineno}: ctypes.{node.attr}")
        stack.extend(ast.iter_child_nodes(node))


def _scan_statements(stmts: list[ast.stmt], file: Path, errors: list[str]) -> None:
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                top = alias.name.split(".")[0]
                if (alias.name == "ctypes.wintypes" or alias.name.startswith("ctypes.wintypes.")
                        or top in FORBIDDEN_MODULES):
                    errors.append(f"{file.relative_to(ROOT)}:{stmt.lineno}: import {alias.name}")
        elif isinstance(stmt, ast.ImportFrom):
            mod = stmt.module or ""
            top = mod.split(".")[0] if mod else ""
            if mod == "ctypes" and any(a.name == "wintypes" for a in stmt.names):
                errors.append(f"{file.relative_to(ROOT)}:{stmt.lineno}: from ctypes import wintypes")
            elif (mod == "ctypes.wintypes" or mod.startswith("ctypes.wintypes.")
                    or top in FORBIDDEN_MODULES):
                errors.append(f"{file.relative_to(ROOT)}:{stmt.lineno}: from {mod} import ...")
        elif isinstance(stmt, ast.If):
            _scan_expr(stmt.test, file, errors)
            if not _is_type_checking(stmt.test):
                _scan_statements(stmt.body, file, errors)
            _scan_statements(stmt.orelse, file, errors)
        elif isinstance(stmt, ast.ClassDef):
            for expr in (*stmt.bases, *stmt.decorator_list):
                _scan_expr(expr, file, errors)
            for kw in stmt.keywords:
                _scan_expr(kw.value, file, errors)
            _scan_statements(stmt.body, file, errors)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                _scan_expr(item.context_expr, file, errors)
            _scan_statements(stmt.body, file, errors)
        elif isinstance(stmt, ast.Try) or (hasattr(ast, "TryStar") and isinstance(stmt, ast.TryStar)):
            _scan_statements(stmt.body, file, errors)
            for handler in stmt.handlers:
                _scan_expr(handler.type, file, errors)
                _scan_statements(handler.body, file, errors)
            _scan_statements(stmt.orelse, file, errors)
            _scan_statements(stmt.finalbody, file, errors)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            _scan_expr(stmt.iter, file, errors)
            _scan_statements(stmt.body, file, errors)
            _scan_statements(stmt.orelse, file, errors)
        elif isinstance(stmt, ast.While):
            _scan_expr(stmt.test, file, errors)
            _scan_statements(stmt.body, file, errors)
            _scan_statements(stmt.orelse, file, errors)
        else:
            for child in ast.iter_child_nodes(stmt):
                _scan_expr(child, file, errors)


def test_no_forbidden_module_level_imports():
    for name in sorted(MODULE_IMPORT_EXCEPTIONS):
        assert (ROOT / "ucho" / name).is_file(), f"Stale exception entry: {name}"

    files: list[Path] = []
    for path in sorted((ROOT / "ucho").glob("*.py")):
        if path.name not in MODULE_IMPORT_EXCEPTIONS:
            files.append(path)
    files.extend(sorted((ROOT / "ucho_mcp").glob("*.py")))
    files.extend([
        ROOT / "ucho" / "platforms" / "__init__.py",
        ROOT / "ucho" / "platforms" / "base.py",
        ROOT / "ucho" / "platforms" / "null.py",
    ])
    macos_dir = ROOT / "ucho" / "platforms" / "macos"
    if macos_dir.is_dir():
        files.extend(sorted(macos_dir.glob("*.py")))

    errors: list[str] = []
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        _scan_statements(tree.body, file, errors)
    assert not errors, "Forbidden module-level imports/calls:\n" + "\n".join(errors)


def test_import_dictation_on_linux_subprocess():
    # numpy and soundfile are imported first, under the real platform: on
    # Windows their imports break when sys.platform says "linux" (os.uname(),
    # libsndfile.so). Ucho's own modules are imported only after the switch.
    code = (
        "import sys; import numpy, soundfile; sys.platform = 'linux'; "
        "import ucho.dictation; from ucho import platforms; "
        "print(platforms.family())"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "null"


def _protocol_methods(protocol: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in protocol.__dict__.items()
        if not name.startswith("_") and callable(member)
    }


def _check_contract(protocol: type, impl: object) -> None:
    assert isinstance(impl, protocol), f"{type(impl).__name__} does not satisfy {protocol.__name__}"
    for name, proto_sig in _protocol_methods(protocol).items():
        attr = getattr(impl, name)
        assert callable(attr)
        proto_params = list(proto_sig.parameters.values())[1:]
        impl_params = list(inspect.signature(attr).parameters.values())
        assert len(impl_params) == len(proto_params), f"{type(impl).__name__}.{name} param count"
        assert [p.name for p in impl_params] == [p.name for p in proto_params], (
            f"{type(impl).__name__}.{name} param names"
        )
        assert [p.default is not inspect.Parameter.empty for p in impl_params] == [
            p.default is not inspect.Parameter.empty for p in proto_params
        ], f"{type(impl).__name__}.{name} defaults"


def test_protocol_contracts():
    pairs: list[tuple[type, object]] = [
        (base.Clipboard, null.NullClipboard()),
        (base.DelayedOffer, null.NullDelayedOffer("x", None)),
        (base.TextInserter, null.NullInserter()),
        (base.Sounds, null.NullSounds()),
        (base.GlobalHotkeys, null.NullHotkeys(lambda _n: None)),
        (base.SingleInstance, null.NullInstance()),
        (base.Shell, null.NullShell()),
        (base.AudioBackend, null.NullAudio()),
        (base.SystemInfo, null.NullSystemInfo()),
        (base.Fonts, null.NullFonts()),
        (base.Permissions, null.NullPermissions()),
    ]
    if sys.platform == "win32":
        from ucho.platforms.windows import clipboard, hotkeys, inserter, instance, shell, sounds

        pairs.extend([
            (base.Clipboard, clipboard.WindowsClipboard()),
            (base.DelayedOffer, clipboard.DelayedClipboard("x", None)),
            (base.TextInserter, inserter.WindowsInserter()),
            (base.Sounds, sounds.WindowsSounds()),
            (base.GlobalHotkeys, hotkeys.Hotkeys(lambda _n: None)),
            (base.SingleInstance, instance.SingleInstance("Local\\Ucho.Test.Contract")),
            (base.Shell, shell.WindowsShell()),
        ])

    for protocol, impl in pairs:
        _check_contract(protocol, impl)


def test_null_insert_text_and_beep():
    with platforms.override("null"):
        assert dictation.insert_text("abc", "paste", False) in dictation.CLIPBOARD_ONLY
        assert dictation.insert_text("abc", "type", False) in dictation.CLIPBOARD_ONLY
        assert dictation.beep(1046) is None


def test_beep_maps_frequencies_to_sound_events(monkeypatch):
    events: list[str] = []
    fake = SimpleNamespace(play=events.append)
    monkeypatch.setattr(platforms, "sounds", lambda: fake)

    dictation.beep(1046)
    dictation.beep(659)
    dictation.beep(330, 120)
    dictation.beep(440)

    assert events == ["start", "stop", "cancel", "start"]


def test_null_semantics():
    with platforms.override("null"):
        assert platforms.hotkeys(lambda _n: None).start({"a": "x"}) == {
            "a": "Global hotkeys are not supported on this system.",
        }
        inst = platforms.instance()
        assert inst.acquire() is True
        assert inst.signal("show") is False
        assert platforms.shell().start_command() == [sys.executable, "-m", "ucho"]
        assert platforms.permissions().state() == {
            "microphone": "unsupported",
            "system_audio": "unsupported",
            "accessibility": "unsupported",
        }


def test_recognized_message_uses_platform_paste_label(monkeypatch):
    shown: list[tuple[str, str, float]] = []

    class _FakeController:
        queued = 1
        live_text = ""
        last_text = ""
        last_seconds = 0.0

        def show_message(self, text: str, style: str = "ready", seconds: float = 3.0) -> None:
            shown.append((text, style, seconds))

        def _emit(self, *_args) -> None:
            pass

    monkeypatch.setattr(platforms, "inserter", lambda: SimpleNamespace(paste_label=lambda: "⌘V"))
    dictation.DictationController._recognized(_FakeController(), "hello", 1.0, dictation.FOCUS_LOST)
    assert shown == [("Text is on the clipboard, press ⌘V", "warning", 8)]
