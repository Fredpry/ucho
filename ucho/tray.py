# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""System tray icon: one for the whole app (meeting recording and dictation).

Optional: if pystray fails to start (e.g. because of a system policy), the
app keeps working without it. The global hotkeys live in ucho/hotkeys.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from collections.abc import Callable

from . import icon as icon_mod

log = logging.getLogger(__name__)


@dataclass
class Item:
    """One menu entry. `text` is called each time the menu opens, so it can change."""

    text: Callable[[], str]
    action: Callable[[], None]
    default: bool = False
    visible: Callable[[], bool] = field(default=lambda: True)


class TrayIcon:
    """Thin wrapper over pystray. Every method is safe when the library is missing.

    `items`: menu entries; None is a separator.
    """

    def __init__(self, title: str, items: list[Item | None]):
        self._title = title
        self._items = items
        self._icon = None
        self._active: bool | None = None

    def start(self) -> bool:
        try:
            import pystray
        except ImportError:
            log.warning("pystray missing - tray icon disabled")
            return False

        def entry(item: Item):
            return pystray.MenuItem(lambda _: item.text(), lambda: item.action(),
                                    default=item.default, visible=lambda _: item.visible())

        menu = pystray.Menu(*[pystray.Menu.SEPARATOR if item is None else entry(item)
                              for item in self._items])
        try:
            self._icon = pystray.Icon("ucho", icon_mod.draw(64), self._title, menu)
            self._icon.run_detached()
            log.info("Tray icon started")
            return True
        except Exception as error:
            log.warning("Could not start the tray icon: %s", error)
            self._icon = None
            return False

    @property
    def running(self) -> bool:
        return self._icon is not None

    def set_state(self, active: bool, description: str | None = None) -> None:
        """Red dot on the icon while recording or dictating."""
        if self._icon is None or active == self._active:
            return
        self._active = active
        try:
            self._icon.icon = icon_mod.draw(64, recording=active)
            self._icon.title = description or self._title
            self._icon.update_menu()
        except Exception:
            log.debug("Could not update the tray icon", exc_info=True)

    def refresh(self) -> None:
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            pass
        self._icon = None
