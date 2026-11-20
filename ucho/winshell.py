# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Compatibility alias for external scripts (install.ps1): the code lives in ucho/platforms/windows/shell.py."""

from __future__ import annotations

from .platforms.windows.shell import (
    SHORTCUT_NAMES,
    TaskbarProgress,
    autostart_command,
    describe_window,
    set_autostart,
    set_jump_list,
    set_shortcut_app_id,
    shortcut_arguments,
    shortcut_paths,
)

__all__ = [
    "SHORTCUT_NAMES",
    "TaskbarProgress",
    "autostart_command",
    "describe_window",
    "set_autostart",
    "set_jump_list",
    "set_shortcut_app_id",
    "shortcut_arguments",
    "shortcut_paths",
]
