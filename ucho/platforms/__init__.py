# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Platform abstraction layer: lazy per-OS backend loading and test overrides.

Rule for keeping cross-platform imports clean: `ucho/platforms/__init__.py`,
`base.py`, `null.py` and `macos/*` import nothing system-specific at module
level (only inside functions or `create()`). `windows/*` modules may use
`ctypes.wintypes` at module level, because they are imported only by the
accessors when `family() == "windows"`.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from . import base

log = logging.getLogger(__name__)

Family = Literal["windows", "macos", "null"]
FAMILIES = ("windows", "macos", "null")

_override_family: Family | None = None
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()
_logged_missing: set[tuple[str, str]] = set()
_missing_lock = threading.Lock()


def family() -> Family:
    """Active platform family (`"windows"`, `"macos"`, or `"null"`)."""
    if _override_family is not None:
        return _override_family
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "null"


@contextlib.contextmanager
def override(fam: str) -> Iterator[None]:
    """Forces `family()` to return `fam` for the duration of the block (tests only)."""
    if fam not in FAMILIES:
        raise ValueError(f"Unknown platform family: {fam!r}")
    global _override_family
    previous = _override_family
    _override_family = fam  # type: ignore[assignment]
    with _cache_lock:
        _cache.clear()
    try:
        yield
    finally:
        _override_family = previous
        with _cache_lock:
            _cache.clear()


def _load(name: str) -> Callable[..., Any]:
    from . import null

    fam = family()
    if fam == "null":
        return null.FACTORIES[name]
    module_name = f"ucho.platforms.{fam}.{name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Missing is either the module itself or the whole family package
        # (no `macos/` folder yet); a missing dependency inside it is a real error.
        if exc.name not in (module_name, f"ucho.platforms.{fam}"):
            raise
        with _missing_lock:
            key = (fam, name)
            if key not in _logged_missing:
                _logged_missing.add(key)
                log.info("No %s implementation for %s - using the null one", name, fam)
        return null.FACTORIES[name]
    return module.create


def _singleton(name: str) -> Any:
    with _cache_lock:
        instance_obj = _cache.get(name)
        if instance_obj is None:
            instance_obj = _load(name)()
            _cache[name] = instance_obj
        return instance_obj


def clipboard() -> base.Clipboard:
    """Cached platform clipboard implementation."""
    return _singleton("clipboard")


def inserter() -> base.TextInserter:
    """Cached platform text inserter implementation."""
    return _singleton("inserter")


def sounds() -> base.Sounds:
    """Cached platform sound player implementation."""
    return _singleton("sounds")


def shell() -> base.Shell:
    """Cached platform desktop/shell integration implementation."""
    return _singleton("shell")


def audio() -> base.AudioBackend:
    """Cached platform audio backend implementation."""
    return _singleton("audio")


def sysinfo() -> base.SystemInfo:
    """Cached platform hardware/system info implementation."""
    return _singleton("sysinfo")


def fonts() -> base.Fonts:
    """Cached platform fonts and icon glyphs implementation."""
    return _singleton("fonts")


def permissions() -> base.Permissions:
    """Cached platform permissions implementation."""
    return _singleton("permissions")


def hotkeys(action: Callable[[str], None]) -> base.GlobalHotkeys:
    """Creates a new global hotkey listener for `action(name)`."""
    return _load("hotkeys")(action)


def instance(*args: Any, **kwargs: Any) -> base.SingleInstance:
    """Creates a new single-instance guard."""
    return _load("instance")(*args, **kwargs)
