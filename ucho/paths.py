# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Application and user data directory paths.

Pure standard-library path computation with no internal imports and no disk
writes at import time.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

_ONEDRIVE_ENV_VARS = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")


def _resolve_context(
    system: str | None,
    home: PurePath | None,
    env: Mapping[str, str] | None,
) -> tuple[str, PurePath, Mapping[str, str]]:
    return (
        sys.platform if system is None else system,
        Path.home() if home is None else home,
        os.environ if env is None else env,
    )


def app_dir() -> Path:
    """Return the directory containing application code and bundled assets."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def data_dir(
    system: str | None = None,
    home: PurePath | None = None,
    env: Mapping[str, str] | None = None,
) -> PurePath:
    """Return the writable data directory for models, settings, and logs."""
    system, home, env = _resolve_context(system, home, env)
    override = env.get("UCHO_DATA_DIR")
    if override:
        return type(home)(override)
    if system == "win32":
        return app_dir()
    if system == "darwin":
        return home / "Library" / "Application Support" / "Ucho"
    xdg = env.get("XDG_DATA_HOME")
    base = type(home)(xdg) if xdg else home / ".local" / "share"
    return base / "ucho"


def log_file(
    system: str | None = None,
    home: PurePath | None = None,
    env: Mapping[str, str] | None = None,
) -> PurePath:
    """Return the path to the main application log file."""
    system, home, env = _resolve_context(system, home, env)
    if env.get("UCHO_DATA_DIR"):
        return data_dir(system=system, home=home, env=env) / "ucho.log"
    if system == "darwin":
        return home / "Library" / "Logs" / "Ucho" / "ucho.log"
    return data_dir(system=system, home=home, env=env) / "ucho.log"


def models_dir(
    system: str | None = None,
    home: PurePath | None = None,
    env: Mapping[str, str] | None = None,
) -> PurePath:
    """Return the directory where speech and LLM models are stored."""
    return data_dir(system=system, home=home, env=env) / "models"


def lock_dir(
    system: str | None = None,
    home: PurePath | None = None,
    env: Mapping[str, str] | None = None,
) -> PurePath:
    """Return the directory for the single-instance lock file and socket (macOS)."""
    return data_dir(system=system, home=home, env=env)


def legacy_models_dir(
    system: str | None = None,
    home: PurePath | None = None,
    env: Mapping[str, str] | None = None,
) -> PurePath:
    """Return the v0.1 models directory (`modele/`) for migration."""
    return data_dir(system=system, home=home, env=env) / "modele"


def settings_file(
    system: str | None = None,
    home: PurePath | None = None,
    env: Mapping[str, str] | None = None,
) -> PurePath:
    """Return the path to `settings.json`."""
    return data_dir(system=system, home=home, env=env) / "settings.json"


def cloud_roots(
    system: str | None = None,
    home: PurePath | None = None,
    env: Mapping[str, str] | None = None,
) -> list[PurePath]:
    """Return cloud-synced root prefixes for the given platform."""
    system, home, env = _resolve_context(system, home, env)
    if system == "win32":
        roots: list[PurePath] = []
        for variable in _ONEDRIVE_ENV_VARS:
            root = env.get(variable)
            if root:
                roots.append(PureWindowsPath(root))
        return roots
    if system == "darwin":
        return [
            home / "Library" / "CloudStorage",
            home / "Library" / "Mobile Documents",
            home / "Documents",
            home / "Desktop",
            home / "Dropbox",
        ]
    return []


def _to_posix_parts(path: PurePath | str) -> tuple[str, ...]:
    posix = PurePosixPath(path.as_posix() if isinstance(path, PurePath) else path)
    return tuple(part.lower() for part in posix.parts)


def is_cloud_path(
    path: PurePath | str,
    system: str | None = None,
    home: PurePath | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """True when `path` lies inside a known cloud-synced folder."""
    system, home, env = _resolve_context(system, home, env)
    if system == "win32":
        text = str(path).lower()
        for variable in _ONEDRIVE_ENV_VARS:
            root = env.get(variable)
            if root and text.startswith(root.lower().rstrip("\\/")):
                return True
        return "onedrive" in text
    if system == "darwin":
        target = _to_posix_parts(path)
        for root in cloud_roots(system=system, home=home, env=env):
            prefix = _to_posix_parts(root)
            if len(target) >= len(prefix) and target[: len(prefix)] == prefix:
                return True
        return False
    return False
