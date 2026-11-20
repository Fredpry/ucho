# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS single instance: socket path choice, the one-line command protocol (every
system, over a socket pair), and flock plus a Unix socket between real processes
(macOS only), and the Dock reopen and Cmd+Q handlers of the app."""

from __future__ import annotations

import inspect
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from ucho import paths, platforms
from ucho.instance import SingleInstance
from ucho.platforms import base
from ucho.platforms.macos import instance as mac

ROOT = Path(__file__).resolve().parent.parent
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only: flock and Unix sockets")


def check_contract(protocol: type, impl: object) -> None:
    """Method names, parameter names and defaults as in `base` (like tests/test_platforms.py)."""
    assert isinstance(impl, protocol)
    for name, member in protocol.__dict__.items():
        if name.startswith("_") or not callable(member):
            continue
        expected = list(inspect.signature(member).parameters.values())[1:]
        actual = list(inspect.signature(getattr(impl, name)).parameters.values())
        assert [p.name for p in actual] == [p.name for p in expected], name
        assert [p.default for p in actual] == [p.default for p in expected], name


# --- pure parts -------------------------------------------------------------------------------


def test_lock_dir_is_the_data_dir():
    home = PurePosixPath("/Users/tester")
    assert paths.lock_dir(system="darwin", home=home, env={}) == \
        home / "Library" / "Application Support" / "Ucho"
    assert paths.lock_dir(system="darwin", home=home, env={"UCHO_DATA_DIR": "/tmp/u"}) == \
        PurePosixPath("/tmp/u")


def test_stem_for():
    assert mac.stem_for(None) == "ucho"
    assert mac.stem_for("") == "ucho"
    assert mac.stem_for("Local\\Ucho.Test.ab12") == "Local_Ucho.Test.ab12"
    assert mac.stem_for("../..") == "ucho"


def test_socket_path_in_the_folder_or_tmpdir_when_too_long():
    folder = "/Users/tester/Library/Application Support/Ucho"
    assert mac.socket_path(folder, "ucho", "/var/folders/T", 501) == f"{folder}/ucho.sock"
    deep = "/Users/tester/" + "x" * 90
    fallback = mac.socket_path(deep, "ucho", "/var/folders/ab/T", 501)
    assert fallback.startswith("/var/folders/ab/T/ucho-501-") and fallback.endswith(".sock")
    assert len(fallback.encode()) <= mac.MAX_SOCKET_PATH
    # Two data folders never share a fallback socket.
    assert fallback != mac.socket_path(deep + "y", "ucho", "/var/folders/ab/T", 501)
    # The limit counts bytes (sun_path), not characters.
    accented = "/Users/" + "ą" * 45
    assert len(f"{accented}/ucho.sock") <= mac.MAX_SOCKET_PATH
    assert mac.socket_path(accented, "ucho", "/tmp", 501).startswith("/tmp/ucho-501-")


@pytest.mark.parametrize("data, expected", [
    (b"show\n", "show"), (b"dictate", "dictate"), (b" record \r\n", "record"),
    (b"show\nrecord\n", "show"), (b"quit\n", None), (b"rm -rf /\n", None), (b"\xff\xfe\n", None),
    (b"", None), (b"SHOW\n", None),
])
def test_parse_command(data, expected):
    assert mac.parse_command(data) == expected


def _serve(command: bytes, on_command) -> tuple[str | None, bytes]:
    server, client = socket.socketpair()
    with server, client:
        client.sendall(command)
        handled = mac.serve_connection(server, on_command)
        return handled, client.recv(16)


def test_serve_connection_runs_known_commands_and_answers():
    got: list[str] = []
    assert _serve(b"record\n", got.append) == ("record", mac.REPLY_OK)
    assert _serve(b"unknown\n", got.append) == (None, mac.REPLY_ERROR)
    assert _serve(b"x" * 500, got.append) == (None, mac.REPLY_ERROR)
    assert got == ["record"]


def test_serve_connection_survives_a_failing_handler_and_a_silent_client(caplog, monkeypatch):
    def broken(_command):
        raise RuntimeError("boom")

    assert _serve(b"show\n", broken) == ("show", mac.REPLY_OK)
    assert "Handling show failed" in caplog.text
    monkeypatch.setattr(mac, "READ_TIMEOUT_S", 0.2)
    server, client = socket.socketpair()
    with server, client:
        assert mac.serve_connection(server, lambda _c: None) is None  # nothing sent: timeout


def test_exchange_round_trip():
    server, client = socket.socketpair()
    got: list[str] = []
    with server, client:
        worker = threading.Thread(target=lambda: mac.serve_connection(server, got.append))
        worker.start()
        assert mac.exchange(client, "dictate")
        worker.join(5)
    assert got == ["dictate"]


def test_contract_factory_and_unknown_command(tmp_path):
    impl = mac.MacInstance(folder=tmp_path)
    check_contract(base.SingleInstance, impl)
    assert mac.create().lock_file.name == "ucho.lock"
    assert mac.create("Local\\Ucho.Test").lock_file.name == "Local_Ucho.Test.lock"
    assert not impl.signal("format-disk")  # refused before any socket
    impl.close()  # nothing acquired: safe
    with platforms.override("macos"):
        assert isinstance(SingleInstance()._impl, mac.MacInstance)


# --- real processes (macOS only) ----------------------------------------------------------

CHILD = r"""
import sys, time
from ucho.platforms.macos.instance import MacInstance
folder, mode = sys.argv[1], sys.argv[2]
inst = MacInstance(folder=folder)
if mode == "acquire":
    print("acquired" if inst.acquire() else "busy", flush=True)
elif mode == "signal":
    print(f"sent {time.time():.6f}", flush=True)
    print("ok" if inst.signal(sys.argv[3]) else "failed", flush=True)
elif mode == "serve":
    assert inst.acquire()
    inst.listen(lambda c: print("got", c, flush=True))
    print("ready", flush=True)
    time.sleep(120)
inst.close()
"""


def _child(folder: Path, *args: str, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-c", CHILD, str(folder), *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=60, check=False, **kwargs)


@pytest.fixture(params=["short", "long"])
def folder(request, tmp_path):
    """A short folder (socket inside it) and pytest's long tmp_path (socket in $TMPDIR)."""
    if request.param == "long":
        path = tmp_path / ("deep-" + "d" * 60)
        path.mkdir()
        yield path
        return
    short = Path(tempfile.mkdtemp(prefix="uc-", dir="/tmp"))
    yield short
    for child in short.iterdir():
        child.unlink()
    short.rmdir()


@darwin_only
def test_second_acquire_fails_and_signal_reaches_the_first(folder):
    """Criteria 1 and 2: a second process cannot acquire; its signal arrives in < 1 s."""
    first = mac.MacInstance(folder=folder)
    got: list[tuple[str, float]] = []
    arrived = threading.Event()
    try:
        assert first.acquire()
        sock = first.socket_file
        in_folder = sock.startswith(str(folder))
        assert in_folder == (len(f"{folder}/ucho.sock".encode()) <= mac.MAX_SOCKET_PATH)
        assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
        first.listen(lambda c: (got.append((c, time.time())), arrived.set()))

        proc = _child(folder, "acquire")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["busy"]

        proc = _child(folder, "signal", "show")
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert lines[-1] == "ok"
        assert arrived.wait(5)
        sent = float(lines[0].split()[1])
        assert got[0][0] == "show"
        assert got[0][1] - sent < 1.0
    finally:
        first.close()
    assert not os.path.lexists(sock)
    assert (folder / "ucho.lock").exists()  # the lock file stays; only the flock goes
    again = mac.MacInstance(folder=folder)
    try:
        assert again.acquire()
    finally:
        again.close()


@darwin_only
def test_a_killed_instance_does_not_block_the_next_start(folder):
    """Criterion 3: after kill -9 the stale socket is removed and commands work again."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    holder = subprocess.Popen([sys.executable, "-c", CHILD, str(folder), "serve"], cwd=str(ROOT),
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = mac.MacInstance(folder=folder)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        assert not second.acquire()
        assert second.signal("record")
        assert holder.stdout.readline().strip() == "got record"
        holder.send_signal(signal.SIGKILL)
        holder.wait(10)
        assert os.path.lexists(second.socket_file)  # left behind by the dead process

        got: list[str] = []
        arrived = threading.Event()
        assert second.acquire()
        second.listen(lambda c: (got.append(c), arrived.set()))
        proc = _child(folder, "signal", "dictate")
        assert proc.stdout.splitlines()[-1] == "ok", proc.stderr
        assert arrived.wait(5) and got == ["dictate"]
    finally:
        second.close()
        if holder.poll() is None:
            holder.kill()
            holder.wait(10)


@darwin_only
def test_signal_waits_for_an_instance_that_is_still_starting(folder):
    """A second start during the first one's window setup: bound in acquire(), answered by listen()."""
    first = mac.MacInstance(folder=folder)
    result: list[bool] = []
    got: list[str] = []
    try:
        assert first.acquire()
        sender = threading.Thread(target=lambda: result.append(mac.MacInstance(folder=folder).signal("show")))
        sender.start()
        time.sleep(0.5)
        assert not result  # connected, waiting for the answer
        first.listen(got.append)
        sender.join(10)
        assert result == [True] and got == ["show"]
    finally:
        first.close()


@darwin_only
def test_app_handles_dock_reopen_and_cmd_q():
    import tkinter

    from ucho.app import App

    root = tkinter.Tk()
    root.withdraw()
    calls: list[str] = []
    fake = SimpleNamespace(createcommand=root.createcommand, on_command=calls.append,
                           quit_app=lambda: calls.append("quit"))
    try:
        App._install_macos_handlers(fake)
        root.tk.call("::tk::mac::ReopenApplication")
        root.tk.call("::tk::mac::Quit")
    finally:
        root.destroy()
    assert calls == ["show", "quit"]
