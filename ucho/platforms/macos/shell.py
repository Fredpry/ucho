# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS shell integration: LaunchAgent autostart, app bundle commands, and icons."""

from __future__ import annotations

import logging
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import paths
from ...i18n import t

if TYPE_CHECKING:
    from ..base import ProgressBar

log = logging.getLogger(__name__)

LAUNCH_AGENT_LABEL = "app.ucho"


def app_bundle() -> Path | None:
    """Outermost `.app` bundle path when running frozen, or None."""
    if not getattr(sys, "frozen", False):
        return None
    for parent in Path(sys.executable).parents:
        if parent.suffix == ".app":
            return parent
    return None


class MacShell:
    """macOS desktop and shell implementation of `Shell`."""

    def __init__(
        self,
        home: Path | None = None,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self._home = Path.home() if home is None else Path(home)
        self._popen = subprocess.Popen if popen is None else popen

    def _plist_path(self) -> Path:
        return self._home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    def _autostart_args(self) -> list[str]:
        bundle = app_bundle()
        cmd = self.start_command()
        if bundle is not None:
            return [*cmd, "--args", "--minimized"]
        return [*cmd, "--minimized"]

    def set_app_identity(self) -> None:
        pass

    def window_handle(self, tk_window) -> int:
        try:
            return int(tk_window.winfo_id())
        except Exception:
            return 0

    def describe_window(self, tk_window, dictation: bool = False) -> bool:
        return False

    def after_start(self, app_id: str, tasks: list[tuple[str, str, str]]) -> None:
        pass

    def progress(self, tk_window) -> ProgressBar | None:
        return None

    def start_command(self) -> list[str]:
        bundle = app_bundle()
        if bundle is not None:
            return ["/usr/bin/open", "-a", str(bundle)]
        return [sys.executable, "-m", "ucho"]

    def worker_command(self, module: str, args: list[str]) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--worker", module, *args]
        return [sys.executable, "-m", module, *args]

    def autostart_get(self) -> str | None:
        plist_file = self._plist_path()
        try:
            data = plistlib.loads(plist_file.read_bytes())
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        prog_args = data.get("ProgramArguments")
        if (
            not isinstance(prog_args, list)
            or not prog_args
            or not all(isinstance(x, str) for x in prog_args)
        ):
            return None
        return shlex.join(prog_args)

    def autostart_set(self, enabled: bool) -> bool:
        plist_file = self._plist_path()
        if not enabled:
            try:
                plist_file.unlink(missing_ok=True)
                return True
            except OSError:
                log.warning("Could not remove LaunchAgent plist %s", plist_file, exc_info=True)
                return False

        bundle = app_bundle()
        if bundle is not None:
            posix = bundle.as_posix()
            if posix.startswith("/Volumes/") or "/AppTranslocation/" in posix:
                log.warning(
                    "Cannot enable autostart from %s; move Ucho to Applications first",
                    posix,
                )
                return False

        payload: dict[str, Any] = {
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": self._autostart_args(),
            "RunAtLoad": True,
            "ProcessType": "Interactive",
        }
        if bundle is None:
            payload["WorkingDirectory"] = str(paths.app_dir())

        try:
            plist_file.parent.mkdir(parents=True, exist_ok=True)
            plist_file.write_bytes(plistlib.dumps(payload))
            return True
        except OSError:
            log.warning("Could not write LaunchAgent plist %s", plist_file, exc_info=True)
            return False

    def autostart_sync(self, enabled: bool) -> None:
        if enabled:
            expected = shlex.join(self._autostart_args())
            if self.autostart_get() != expected:
                self.autostart_set(True)
        elif self._plist_path().exists():
            self.autostart_set(False)

    def open_path(self, path) -> None:
        self._popen(["/usr/bin/open", str(path)])

    def work_area(self) -> tuple[int, int] | None:
        return None

    def show_without_focus(self, tk_window) -> None:
        tk_window.deiconify()

    def set_window_icon(self, tk_window, dictation: bool = False) -> None:
        try:
            from PIL import ImageTk

            from ... import icon

            image = icon.draw_dictate(256) if dictation else icon.draw(256)
            photo = ImageTk.PhotoImage(image, master=tk_window)
            tk_window.iconphoto(not dictation, photo)
            tk_window._ucho_icon = photo
        except Exception:
            log.debug("Could not set the macOS window icon", exc_info=True)

    def autostart_label(self) -> str:
        return t("Open at login")


def create() -> MacShell:
    return MacShell()
