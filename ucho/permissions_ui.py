# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Permissions status panel and system-audio warning hook (customtkinter widgets)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import customtkinter as ctk

from . import platform_text, theme, ui
from .i18n import t
from .platform_text import PermissionRow
from .theme import C

log = logging.getLogger(__name__)


class PermissionsPanel(ctk.CTkFrame):
    """Card listing OS permissions with live status and Allow / Open settings actions."""

    def __init__(self, master, on_update: Callable[[list[PermissionRow]], None] | None = None):
        super().__init__(master, fg_color="transparent")
        self._on_update = on_update
        self._signature: list[tuple[str, str, bool, bool]] | None = None
        self._requesting: set[str] = set()
        self.grid_columnconfigure(0, weight=1)
        self.card = ui.card(self)
        self.card.grid(row=0, column=0, sticky="ew")
        self.card.grid_columnconfigure(0, weight=1)
        self.refresh()
        self._schedule()

    def refresh(self) -> None:
        """Recomputes permission rows, rebuilds widgets if changed, and invokes `on_update`."""
        rows = platform_text.permission_rows()
        signature = [(r.kind, r.state, r.allow, r.open_settings) for r in rows]
        if signature != self._signature:
            self._signature = signature
            for child in self.card.winfo_children():
                child.destroy()
            for index, row in enumerate(rows):
                self._build_row(index, row, last=(index == len(rows) - 1))
        if self._on_update is not None:
            self._on_update(rows)

    def _build_row(self, index: int, row: PermissionRow, last: bool) -> None:
        holder = ctk.CTkFrame(self.card, fg_color="transparent")
        holder.grid(
            row=index,
            column=0,
            sticky="ew",
            padx=18,
            pady=(14 if index == 0 else 8, 14 if last else 8),
        )
        holder.grid_columnconfigure(0, weight=1)

        left = ctk.CTkFrame(holder, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        left.grid_columnconfigure(0, weight=1)
        ui.label(left, row.title, theme.SM, "bold", anchor="w").grid(row=0, column=0, sticky="w")
        ui.muted(left, row.hint, theme.XS + 1, anchor="w", justify="left", wraplength=360).grid(
            row=1, column=0, sticky="w", pady=(2, 0)
        )

        right = ctk.CTkFrame(holder, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e")
        if row.state == "granted":
            color = C.success
        elif row.state == "denied":
            color = C.warning
        else:
            color = C.text_muted
        ui.label(
            right,
            platform_text.state_text(row.state),
            theme.SM - 1,
            "bold" if row.state == "granted" else "normal",
            color=color,
        ).pack(side="left")

        if row.allow:
            ui.secondary(
                right,
                t("Allow"),
                lambda k=row.kind: self._allow(k),
                width=10,
                height=30,
            ).pack(side="left", padx=(10, 0))
        if row.open_settings:
            ui.secondary(
                right,
                t("Open settings"),
                lambda k=row.kind: platform_text.open_permission_settings(k),
                width=10,
                height=30,
            ).pack(side="left", padx=(10, 0))

    def _allow(self, kind: str) -> None:
        # A request may block for seconds (macOS probes system audio with a short tap), so it
        # runs off the Tk thread; the 1 s timer and the refresh below pick up the new state.
        if kind in self._requesting:
            return
        self._requesting.add(kind)

        def run() -> None:
            try:
                platform_text.request_permission(kind)
            finally:
                self._requesting.discard(kind)

        threading.Thread(target=run, name=f"permission-{kind}", daemon=True).start()
        try:
            if self.winfo_exists():
                self.after(400, self._refresh_if_exists)
        except Exception:
            log.debug("Could not schedule post-request refresh", exc_info=True)

    def _refresh_if_exists(self) -> None:
        try:
            if self.winfo_exists():
                self.refresh()
        except Exception:
            log.debug("PermissionsPanel refresh failed", exc_info=True)

    def _schedule(self) -> None:
        try:
            if self.winfo_exists():
                self.after(1000, self._tick)
        except Exception:
            log.debug("Could not schedule PermissionsPanel timer", exc_info=True)

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
            if self.winfo_ismapped():
                self.refresh()
        except Exception:
            log.debug("PermissionsPanel timer tick failed", exc_info=True)
        self._schedule()


def show_system_audio_unavailable(app) -> None:
    """Shows a warning toast when system audio capture is unavailable."""
    try:
        app.toast(platform_text.system_audio_unavailable_text(), "warning", 8)
    except Exception:
        log.debug("Could not show system audio unavailable toast", exc_info=True)
