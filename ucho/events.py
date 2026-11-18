# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Listener list shared by the meeting and dictation controllers."""

from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

Listener = Callable[[str, object], None]


class Emitter:
    """Events delivered on the UI thread. The subclass has `self.app` with `post(fn)`."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def listen(self, callback: Listener) -> None:
        self._listeners.append(callback)

    def clear_listeners(self) -> None:
        self._listeners.clear()

    def _emit(self, event: str, data=None) -> None:
        """Delivers an event on the UI thread."""
        def deliver() -> None:
            for callback in list(self._listeners):
                try:
                    callback(event, data)
                except Exception:
                    log.exception("Listener for %s failed", event)
        self.app.post(deliver)  # type: ignore[attr-defined]
