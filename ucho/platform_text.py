# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Platform-dependent user-facing texts and permission row helpers (no Tk)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from . import config, platforms
from .i18n import t

log = logging.getLogger(__name__)

PERMISSION_KINDS = ("microphone", "system_audio", "accessibility")
_KNOWN_STATES = ("granted", "denied", "unknown", "unsupported")


@dataclass(frozen=True)
class PermissionRow:
    kind: str
    title: str
    hint: str
    state: str
    allow: bool
    open_settings: bool


def _permission_titles() -> dict[str, str]:
    return {
        "microphone": t("Microphone"),
        "system_audio": t("System audio"),
        "accessibility": t("Accessibility"),
    }


def _permission_hints() -> dict[str, str]:
    return {
        "microphone": t("Needed to record your voice and to dictate."),
        "system_audio": t(
            "Needed to record the other participants of a meeting. "
            "Without it Ucho records the microphone only."
        ),
        "accessibility": t("Needed to paste dictated text into other windows and to type it."),
    }


def _has_settings_url(kind: str) -> bool:
    try:
        url = platforms.permissions().settings_url(kind)
    except Exception:
        return False
    return isinstance(url, str) and bool(url)


def permission_rows(state: Mapping[str, str] | None = None) -> list[PermissionRow]:
    """Builds the ordered list of permission rows from the current platform state."""
    if state is None:
        try:
            raw: Mapping[str, str] = platforms.permissions().state()
        except Exception as error:
            log.debug("Could not read permissions state: %s", type(error).__name__)
            raw = {}
    else:
        raw = state
    titles = _permission_titles()
    hints = _permission_hints()
    rows: list[PermissionRow] = []
    for kind in PERMISSION_KINDS:
        value = raw.get(kind, "unknown")
        normalized = value if value in _KNOWN_STATES else "unknown"
        allow = normalized == "unknown"
        open_settings = normalized == "denied" and _has_settings_url(kind)
        rows.append(
            PermissionRow(
                kind=kind,
                title=titles[kind],
                hint=hints[kind],
                state=normalized,
                allow=allow,
                open_settings=open_settings,
            )
        )
    return rows


def all_granted(rows: Iterable[PermissionRow]) -> bool:
    """True when every permission row is granted."""
    return all(row.state == "granted" for row in rows)


def state_text(state: str) -> str:
    """Human-readable label for a permission state."""
    if state == "granted":
        return t("Allowed")
    if state == "denied":
        return t("Denied")
    if state == "unsupported":
        return t("Not available")
    return t("Not asked yet")


def request_permission(kind: str) -> None:
    """Asks the platform to prompt for `kind`, logging failures without raising."""
    try:
        platforms.permissions().request(kind)
    except Exception as error:
        log.warning("Could not request permission %s: %s", kind, type(error).__name__)


def open_permission_settings(kind: str) -> bool:
    """Opens the OS settings pane for `kind` if a URL is available."""
    try:
        url = platforms.permissions().settings_url(kind)
        if not url:
            return False
        platforms.shell().open_path(url)
        return True
    except Exception as error:
        log.warning("Could not open permission settings for %s: %s", kind, type(error).__name__)
        return False


def wizard_steps(family: str | None = None) -> list[str]:
    """Ordered first-run wizard step names for `family` (or the active platform)."""
    fam = platforms.family() if family is None else family
    if fam == "macos":
        return ["language", "folder", "hardware", "analysis", "permissions", "consent"]
    return ["language", "folder", "hardware", "analysis", "consent"]


def system_theme_label() -> str:
    """Label for the 'follow OS theme' option."""
    if platforms.family() == "macos":
        return t("Same as the system")
    return t("Same as Windows")


def dictation_mode_labels() -> dict[str, str]:
    """Dictation insertion mode labels keyed by `config.DICTATION_MODE_LABELS` keys."""
    macos = platforms.family() == "macos"
    labels: dict[str, str] = {}
    for key, text in config.DICTATION_MODE_LABELS.items():
        if macos and key == "paste":
            labels[key] = t("Paste (clipboard + {keys})", keys=platforms.inserter().paste_label())
        else:
            labels[key] = t(text)
    return labels


def cloud_folder_note(example: object = None) -> str:
    """Warning shown when the recordings folder is inside a cloud-synced directory."""
    if platforms.family() == "macos":
        if example:
            return t(
                "This folder is synced to the cloud (iCloud Drive, Dropbox or similar): recordings "
                "and transcripts would go to the cloud. Choose a folder outside it, for example {folder}.",
                folder=example,
            )
        return t(
            "This folder is synced to the cloud (iCloud Drive, Dropbox or similar): recordings "
            "and transcripts would go to the cloud. Choose a folder outside it."
        )
    if example:
        return t(
            "This folder is synced by OneDrive: recordings and transcripts would go to "
            "the cloud. Choose a folder outside OneDrive, for example {folder}.",
            folder=example,
        )
    return t(
        "This folder is synced by OneDrive: recordings and transcripts "
        "would go to the cloud. Choose a folder outside OneDrive."
    )


def cloud_lock_hint() -> str:
    """Diagnostics hint when a file error happens in a cloud-synced folder."""
    if platforms.family() == "macos":
        return t(
            "The file is in a folder synced to the cloud, which can lock files while it syncs. "
            "Wait for the sync to finish or choose a folder outside it."
        )
    return t(
        "The file is in a OneDrive folder, which can lock files while they sync. "
        "Wait for OneDrive to finish or choose a folder outside OneDrive."
    )


def access_denied_message() -> str:
    """Diagnostics message for OS permission errors."""
    if platforms.family() == "macos":
        return t("macOS did not let Ucho read or write a file or folder.")
    return t("Windows did not let Ucho read or write a file or folder.")


def audio_device_hint() -> str:
    """Diagnostics hint for audio device failures."""
    if platforms.family() == "macos":
        return t(
            "Check the device in the macOS Sound settings, close other programs that use it and "
            "choose it again in Settings."
        )
    return t(
        "Check the device in Windows sound settings, close other programs that use it and "
        "choose it again in Settings."
    )


def system_info_description() -> str:
    """Short description of `system.txt` in the diagnostics package preview."""
    if platforms.family() == "macos":
        return t("Ucho version, macOS, processor, memory and graphics")
    return t("Ucho version, Windows, processor, memory and graphics")


def model_tail(key: str) -> str:
    """Short display name for a speech model key."""
    _, filename = config.split_model_id(key)
    if filename is not None:
        return filename.removesuffix(".bin")
    return key.split("/")[-1]


def system_audio_unavailable_text() -> str:
    """Warning text when system audio capture is unavailable."""
    return t("System audio is unavailable, recording the microphone only")
