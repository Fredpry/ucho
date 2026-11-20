# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""One Ucho per user on macOS: a lock file plus a Unix socket for commands.

The first process holds `fcntl.flock` on `<lock_dir>/ucho.lock` and serves a
Unix socket next to it. A second process (`python -m ucho --dictate`,
`Ucho.app/Contents/MacOS/Ucho --dictate`, `open -n`) finds the lock taken,
connects to the socket, sends one ASCII line from `COMMANDS` ("show",
"dictate", "record") and exits once the first process answered "ok". The
Dock, Finder and `open -a` start no second process for a running app: they
activate it and send a reopen event (`::tk::mac::ReopenApplication` in app.py).

Why flock: the kernel releases it when the process dies, even after
`kill -9`, so a crashed Ucho never blocks the next start. The socket file
outlives such a crash; whoever holds the lock knows nobody else serves it and
removes it before binding.

The socket is bound in `acquire()`, not in `listen()`: the app builds its
window between the two (seconds), and a second start in that time would
otherwise find no socket. Its connection waits in the backlog and is
answered as soon as `listen()` starts the accept thread.

Nothing here is reachable from the network: an `AF_UNIX` socket with mode
0600 in the user's own data folder (or the per-user `$TMPDIR`), accepting
only the words in `COMMANDS`. `fcntl` and `AF_UNIX` exist only on POSIX, so
they are used inside functions (this module imports on Windows for tests).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from ... import paths
from ...instance import COMMANDS

log = logging.getLogger(__name__)

STEM = "ucho"
# macOS `sockaddr_un.sun_path` holds 104 bytes with the closing NUL; longer
# folders (a deep UCHO_DATA_DIR, pytest's tmp_path) fall back to $TMPDIR.
MAX_SOCKET_PATH = 100
MAX_LINE = 64
SIGNAL_TIMEOUT_S = 5.0
CONNECT_RETRY_S = 0.1
ACCEPT_POLL_S = 0.5
READ_TIMEOUT_S = 1.0
REPLY_OK = b"ok\n"
REPLY_ERROR = b"error\n"


def stem_for(name: str | None) -> str:
    """File name stem for an instance name; None is the app's own `ucho`."""
    if not name:
        return STEM
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or STEM


def socket_path(folder: str | os.PathLike, stem: str, tmpdir: str | os.PathLike, uid: int) -> str:
    """`<folder>/<stem>.sock`, or `<tmpdir>/<stem>-<uid>-<hash>.sock` when that is too long.

    The hash of the folder keeps two data folders (tests, `UCHO_DATA_DIR`) on
    separate sockets in the shared fallback folder.
    """
    folder_text = str(folder)
    primary = str(PurePosixPath(folder_text) / f"{stem}.sock")
    if len(primary.encode("utf-8")) <= MAX_SOCKET_PATH:
        return primary
    digest = hashlib.sha256(folder_text.encode("utf-8")).hexdigest()[:10]
    return str(PurePosixPath(str(tmpdir)) / f"{stem}-{uid}-{digest}.sock")


def parse_command(data: bytes) -> str | None:
    """One request line -> a command from `COMMANDS`, or None."""
    line = data.split(b"\n", 1)[0].strip()
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if text in COMMANDS else None


def read_line(conn: socket.socket, limit: int = MAX_LINE) -> bytes:
    """Reads up to one newline (or `limit` bytes, or the peer closing)."""
    data = b""
    while b"\n" not in data and len(data) < limit:
        chunk = conn.recv(limit - len(data))
        if not chunk:
            break
        data += chunk
    return data


def serve_connection(conn: socket.socket, on_command: Callable[[str], None]) -> str | None:
    """Handles one client: reads its command, runs `on_command`, answers ok/error."""
    try:
        conn.settimeout(READ_TIMEOUT_S)
        command = parse_command(read_line(conn))
        if command is None:
            log.warning("Ignored an unknown request on the instance socket")
            conn.sendall(REPLY_ERROR)
            return None
        log.info("Request from a second start: %s", command)
        try:
            on_command(command)
        except Exception:
            log.exception("Handling %s failed", command)
        conn.sendall(REPLY_OK)
        return command
    except OSError:
        # The client gave up (timeout, closed): the command, if read, ran anyway.
        log.debug("Instance socket client went away", exc_info=True)
        return None


def exchange(conn: socket.socket, command: str) -> bool:
    """Client side: sends the command line and waits for "ok"."""
    conn.sendall(command.encode("ascii") + b"\n")
    return read_line(conn).strip() == REPLY_OK.strip()


class MacInstance:
    """macOS `SingleInstance`: flock on a lock file, commands over a Unix socket."""

    def __init__(self, name: str | None = None, folder: str | os.PathLike | None = None):
        self._stem = stem_for(name)
        self._folder = Path(folder) if folder is not None else Path(paths.lock_dir())
        self._lock_fd: int | None = None
        self._server: socket.socket | None = None
        self._socket_path: str | None = None
        self._socket_inode: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def lock_file(self) -> Path:
        return self._folder / f"{self._stem}.lock"

    @property
    def socket_file(self) -> str:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
        return socket_path(self._folder, self._stem, tmpdir, uid)

    def acquire(self) -> bool:
        """True for the first instance; False when Ucho already runs."""
        import fcntl

        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            # Without a lock file the app still has to start (null semantics).
            log.warning("Single-instance lock file unavailable - not guarding", exc_info=True)
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError:
            os.close(fd)
            log.warning("Single-instance lock failed - not guarding", exc_info=True)
            return True
        self._lock_fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass
        self._bind()
        return True

    def _bind(self) -> None:
        path = self.socket_file
        try:
            # We hold the lock, so a socket file left here belongs to a dead Ucho.
            if os.path.lexists(path):
                os.unlink(path)
                log.info("Removed a stale instance socket")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(path)
                os.chmod(path, 0o600)
                server.listen(8)
                server.settimeout(ACCEPT_POLL_S)
            except OSError:
                server.close()
                raise
        except OSError:
            log.warning("Instance socket unavailable - a second start cannot hand over its command",
                        exc_info=True)
            return
        self._server = server
        self._socket_path = path
        try:
            self._socket_inode = os.stat(path).st_ino
        except OSError:
            self._socket_inode = None

    def listen(self, on_command: Callable[[str], None]) -> None:
        """First instance: calls `on_command(name)` (on a background thread) for each request."""
        server = self._server
        if server is None or self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if not self._stop.is_set():
                        log.warning("Instance socket stopped accepting", exc_info=True)
                    return
                with conn:
                    serve_connection(conn, on_command)

        self._thread = threading.Thread(target=loop, name="single-instance", daemon=True)
        self._thread.start()

    def signal(self, command: str) -> bool:
        """Second instance: asks the running Ucho to run `command`."""
        if command not in COMMANDS:
            return False
        path = self.socket_file
        deadline = time.monotonic() + SIGNAL_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("The running Ucho did not answer %s", command)
                return False
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                conn.settimeout(remaining)
                conn.connect(path)
                return exchange(conn, command)
            except (FileNotFoundError, ConnectionRefusedError):
                # The first instance holds the lock but has not bound yet, or is quitting.
                time.sleep(min(CONNECT_RETRY_S, max(0.0, remaining)))
            except OSError:
                log.warning("Handing %s to the running Ucho failed", command, exc_info=True)
                return False
            finally:
                conn.close()

    def close(self) -> None:
        """Stops the listener, removes our socket and releases the lock."""
        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(ACCEPT_POLL_S * 2 + 0.5)
        path, self._socket_path = self._socket_path, None
        if path is not None:
            try:
                # Only our own socket: a newer Ucho may already have bound a new one.
                if self._socket_inode is None or os.stat(path).st_ino == self._socket_inode:
                    os.unlink(path)
            except OSError:
                pass
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


def create(name: str | None = None) -> MacInstance:
    return MacInstance(name)
