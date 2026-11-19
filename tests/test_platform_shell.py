# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tests for refactored platform hotkeys, single instance, and shell integration."""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucho import config, diarize, hotkeys, instance, platforms

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(sys.platform != "win32", reason="WindowsShell is Windows-only")
def test_windows_shell_commands_and_labels():
    from ucho.platforms.windows import shell as win_shell

    sh = win_shell.WindowsShell()
    assert sh.start_command() == [str(win_shell._pythonw()), str(win_shell.STARTER)]
    worker_cmd = sh.worker_command("ucho.diarize", ["--worker"])
    assert worker_cmd == [diarize.worker_python(), "-m", "ucho.diarize", "--worker"]
    assert sh.autostart_label() == "Start with Windows"


@pytest.mark.skipif(sys.platform != "win32", reason="WindowsShell is Windows-only")
def test_windows_shell_autostart_set_and_sync(monkeypatch):
    from ucho.platforms.windows import shell as win_shell

    sh = win_shell.WindowsShell()
    set_calls: list[tuple[bool, str]] = []
    wanted = win_shell.minimized_command()

    monkeypatch.setattr(win_shell, "set_autostart", lambda en, cmd: set_calls.append((en, cmd)) or True)
    assert sh.autostart_set(True) is True
    assert set_calls == [(True, wanted)]

    # 1. enabled=True + different entry -> set_autostart(True, wanted)
    set_calls.clear()
    monkeypatch.setattr(win_shell, "autostart_command", lambda: r'"C:\old\ucho.pyw" --minimized')
    sh.autostart_sync(True)
    assert set_calls == [(True, wanted)]

    # 2. enabled=True + matching entry -> no-op
    set_calls.clear()
    monkeypatch.setattr(win_shell, "autostart_command", lambda: wanted)
    sh.autostart_sync(True)
    assert set_calls == []

    # 3. enabled=False + entry pointing to ucho.pyw -> set_autostart(False, wanted)
    set_calls.clear()
    monkeypatch.setattr(win_shell, "autostart_command", lambda: r'"C:\Python\pythonw.exe" "C:\Ucho\UCHO.PYW" --minimized')
    sh.autostart_sync(False)
    assert set_calls == [(False, wanted)]

    # 4. enabled=False + hand-made entry -> untouched
    set_calls.clear()
    monkeypatch.setattr(win_shell, "autostart_command", lambda: r'"C:\Tools\custom.exe"')
    sh.autostart_sync(False)
    assert set_calls == []

    # 5. enabled=False + None -> untouched
    set_calls.clear()
    monkeypatch.setattr(win_shell, "autostart_command", lambda: None)
    sh.autostart_sync(False)
    assert set_calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="WindowsShell is Windows-only")
def test_windows_shell_after_start_and_describe_window(monkeypatch):
    from ucho.platforms.windows import shell as win_shell

    sh = win_shell.WindowsShell()
    jump_calls: list[tuple[str, list[dict]]] = []
    stamped: list[bool] = []
    described: list[tuple[int, str, str, str, str | None]] = []

    monkeypatch.setattr(win_shell, "set_jump_list", lambda app_id, tasks: jump_calls.append((app_id, tasks)) or True)
    monkeypatch.setattr(win_shell, "_stamp_shortcuts", lambda: stamped.append(True))
    monkeypatch.setattr(
        win_shell,
        "describe_window",
        lambda hwnd, command, name, icon, app_id=None: described.append((hwnd, command, name, icon, app_id)) or True,
    )

    tasks = [
        ("--record", "Record a meeting", "Start recording a meeting with Ucho"),
        ("--dictate", "Dictate", "Open Ucho dictation"),
    ]
    sh.after_start(config.APP_ID, tasks)
    assert stamped == [True]
    assert len(jump_calls) == 1
    app_id, task_dicts = jump_calls[0]
    assert app_id == config.APP_ID
    assert task_dicts == [
        {
            "title": "Record a meeting",
            "arguments": f'"{win_shell.STARTER}" --record',
            "description": "Start recording a meeting with Ucho",
            "icon": f"{win_shell.ICON_FILE},0",
            "program": str(win_shell._pythonw()),
            "working_dir": str(config.PROJECT_DIR),
        },
        {
            "title": "Dictate",
            "arguments": f'"{win_shell.STARTER}" --dictate',
            "description": "Open Ucho dictation",
            "icon": f"{win_shell.DICTATE_ICON_FILE},0",
            "program": str(win_shell._pythonw()),
            "working_dir": str(config.PROJECT_DIR),
        },
    ]

    fake_win = SimpleNamespace(wm_frame=lambda: "0x10")
    assert sh.describe_window(fake_win, dictation=True) is True
    assert described == [
        (
            16,
            win_shell.start_command() + " --dictate",
            config.APP_NAME,
            f"{win_shell.DICTATE_ICON_FILE},0",
            config.APP_ID,
        )
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="WindowsShell is Windows-only")
def test_windows_shell_progress_and_work_area(monkeypatch):
    from ucho.platforms.windows import shell as win_shell

    sh = win_shell.WindowsShell()
    recorded: list[tuple[int, int, int | None]] = []

    class _FakeTaskbarProgress:
        NONE = 0x0
        INDETERMINATE = 0x1
        NORMAL = 0x2
        ERROR = 0x4

        def __init__(self, hwnd: int) -> None:
            self.hwnd = hwnd

        def set(self, state: int, value: int | None = None) -> None:
            recorded.append((self.hwnd, state, value))

    monkeypatch.setattr(win_shell, "TaskbarProgress", _FakeTaskbarProgress)
    fake_win = SimpleNamespace(wm_frame=lambda: "0x10")
    bar = sh.progress(fake_win)
    bar.set("none")
    bar.set("normal", 42)
    bar.set("indeterminate")
    bar.set("paused", 50)
    bar.set("error", 100)
    assert recorded == [
        (16, 0x0, None),
        (16, 0x2, 42),
        (16, 0x1, None),
        (16, 0x8, 50),
        (16, 0x4, 100),
    ]

    monkeypatch.setattr(win_shell, "_work_area", lambda: (0, 0))
    assert sh.work_area() is None
    monkeypatch.setattr(win_shell, "_work_area", lambda: (1920, 1040))
    assert sh.work_area() == (1920, 1040)


def test_hotkeys_facade_delegates(monkeypatch):
    calls: list[tuple] = []

    class _FakeHotkeys:
        def __init__(self, action):
            calls.append(("init", action))

        def start(self, bindings):
            calls.append(("start", bindings))
            return {"record": "busy"}

        def escape(self, enable):
            calls.append(("escape", enable))

        def stop(self):
            calls.append(("stop",))

    monkeypatch.setattr(platforms, "hotkeys", _FakeHotkeys)

    def action(_name):
        return None

    hk = hotkeys.Hotkeys(action)
    assert hotkeys.Hotkeys.ESCAPE == "escape"
    assert hk.start({"record": "<ctrl>+<alt>+r"}) == {"record": "busy"}
    assert hk.errors == {"record": "busy"}
    hk.escape(True)
    hk.stop()
    assert calls == [
        ("init", action),
        ("start", {"record": "<ctrl>+<alt>+r"}),
        ("escape", True),
        ("stop",),
    ]


def test_instance_facade_delegates(monkeypatch):
    calls: list[tuple] = []

    class _FakeInstance:
        def __init__(self, *args):
            calls.append(("init", args))

        def acquire(self):
            calls.append(("acquire",))
            return True

        def listen(self, on_command):
            calls.append(("listen", on_command))

        def signal(self, command):
            calls.append(("signal", command))
            return True

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(platforms, "instance", _FakeInstance)
    inst_default = instance.SingleInstance()
    inst_named = instance.SingleInstance("x")

    def cb(_cmd):
        return None

    assert inst_default.acquire() is True
    inst_named.listen(cb)
    assert inst_named.signal("show") is True
    inst_named.close()
    assert calls == [
        ("init", ()),
        ("init", ("x",)),
        ("acquire",),
        ("listen", cb),
        ("signal", "show"),
        ("close",),
    ]


def test_null_hotkeys_warns_and_null_instance_acquires(caplog):
    with platforms.override("null"):
        with caplog.at_level(logging.WARNING):
            hk = hotkeys.Hotkeys(lambda _n: None)
            errors = hk.start({"record": "<ctrl>+<alt>+r"})
        assert errors == {"record": "Global hotkeys are not supported on this system."}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        errors_logged = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(warnings) == 1
        assert "not supported" in warnings[0].getMessage()
        assert not errors_logged

        inst = instance.SingleInstance()
        assert inst.acquire() is True


def test_diarize_run_worker_uses_platform_shell(monkeypatch, tmp_path: Path):
    track = tmp_path / "meeting.flac"
    seen_cmd: list[list[str]] = []
    shell_calls: list[tuple[str, list[str]]] = []

    class _FakeShell:
        def worker_command(self, module: str, args: list[str]) -> list[str]:
            shell_calls.append((module, list(args)))
            return ["custom-python", "-m", module, *args]

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return (json.dumps({"turns": [[0.5, 2.0, 0]]}) + "\n").encode("utf-8"), b""

    def fake_popen(cmd, **_kwargs):
        seen_cmd.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(platforms, "shell", lambda: _FakeShell())
    turns = diarize.run_worker(track, 0.5, 2, popen=fake_popen)
    assert turns == [diarize.Turn(0.5, 2.0, 0)]
    assert shell_calls == [
        ("ucho.diarize", ["--worker", "--threshold", repr(0.5), "--threads", "2", str(track)])
    ]
    assert seen_cmd == [
        ["custom-python", "-m", "ucho.diarize", "--worker", "--threshold", repr(0.5), "--threads", "2", str(track)]
    ]


def test_no_winshell_imports_outside_compat_module():
    offenders: list[str] = []
    for path in sorted((ROOT / "ucho").glob("*.py")):
        if path.name == "winshell.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "winshell" or alias.name.startswith("winshell."):
                        offenders.append(f"{path.name}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "winshell" or mod.startswith("winshell.") or (
                    node.level > 0 and mod == "" and any(a.name == "winshell" for a in node.names)
                ):
                    offenders.append(f"{path.name}:{node.lineno}: from {mod} import ...")
    assert not offenders, "Unexpected winshell imports:\n" + "\n".join(offenders)


def test_import_on_darwin_subprocess():
    pytest.importorskip("customtkinter")
    code = (
        "import sys; import numpy, soundfile, customtkinter; sys.platform = 'darwin'; "
        "import ucho.hotkeys, ucho.instance, ucho.screens"
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


def test_app_after_start_uses_platform_shell(monkeypatch):
    pytest.importorskip("customtkinter")
    from ucho import app as app_mod

    calls: list[tuple] = []
    fake_bar = object()

    class _RecordingShell:
        def describe_window(self, tk_window, dictation: bool = False) -> bool:
            calls.append(("describe_window", tk_window, dictation))
            return True

        def progress(self, tk_window):
            calls.append(("progress", tk_window))
            return fake_bar

        def after_start(self, app_id: str, tasks: list[tuple[str, str, str]]) -> None:
            calls.append(("after_start", app_id, tasks))

        def autostart_sync(self, enabled: bool) -> None:
            calls.append(("autostart_sync", enabled))

    monkeypatch.setattr(platforms, "shell", lambda: _RecordingShell())
    threads: list[tuple] = []
    fake_self = SimpleNamespace(
        smoke=False,
        options=app_mod.Options(),
        settings={"start_with_windows": True},
        _taskbar=None,
        _start_thread=lambda target, name: threads.append((target, name)),
    )
    fake_self.describe_window = lambda: app_mod.App.describe_window(fake_self)

    app_mod.App._after_start(fake_self)
    assert fake_self._taskbar is fake_bar
    assert calls[:2] == [
        ("describe_window", fake_self, False),
        ("progress", fake_self),
    ]
    assert len(threads) == 1
    target, name = threads[0]
    assert name == "taskbar"
    target()
    assert calls[2:] == [
        (
            "after_start",
            config.APP_ID,
            [
                ("--record", "Record a meeting", "Start recording a meeting with Ucho"),
                ("--dictate", "Dictate", "Open Ucho dictation"),
            ],
        ),
        ("autostart_sync", True),
    ]
