# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""One Ucho per user session: a second start hands its command to the first one.

The platform implementation lives in `ucho/platforms/<family>/instance.py`.
"""

from __future__ import annotations

from collections.abc import Callable

from . import platforms

# The Windows named mutex; the installer (installer/ucho.iss, AppMutex) uses the same name.
MUTEX_NAME = "Local\\Ucho.App"
COMMANDS = ("show", "dictate", "record")


class SingleInstance:
    """Single-instance guard delegating to the active platform backend."""

    def __init__(self, name: str | None = None):
        self._impl = platforms.instance() if name is None else platforms.instance(name)

    def acquire(self) -> bool:
        """First instance -> True; False when Ucho already runs."""
        return self._impl.acquire()

    def listen(self, on_command: Callable[[str], None]) -> None:
        """First instance: calls `on_command(name)` for each request."""
        self._impl.listen(on_command)

    def signal(self, command: str) -> bool:
        """Second instance hands its command over to the running Ucho."""
        return self._impl.signal(command)

    def close(self) -> None:
        """Releases the instance guard resources."""
        self._impl.close()
