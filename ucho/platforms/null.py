# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Null platform implementations so the app works without system features."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..audio import DeviceInfo
    from ..hardware import GraphicsAdapter
    from .base import Permission, SoundEvent, WindowRef


class NullDelayedOffer:
    """No-op delayed clipboard offer for platforms without Win32 delayed rendering."""

    def __init__(self, text: str, consumer_pid: int | None) -> None:
        self.text = text
        self.consumer_pid = consumer_pid
        self.rendered = False
        self.confirmed = False
        self.lost = False

    def offer(self) -> None:
        pass

    def pump(self, seconds: float, until: Callable[[], bool] | None = None) -> bool:
        return bool(until is not None and until())

    def restore(self, previous: str | None) -> bool:
        return False

    def close(self) -> None:
        pass


class NullClipboard:
    """No-op clipboard implementation."""

    def get_text(self) -> str | None:
        return None

    def set_text(self, text: str | None, private: bool = True) -> int:
        return 0

    def sequence(self) -> int:
        return 0

    def offer_delayed(self, text: str, consumer_pid: int | None) -> NullDelayedOffer:
        return NullDelayedOffer(text, consumer_pid)


class NullInserter:
    """No-op text inserter implementation."""

    def foreground(self) -> WindowRef | None:
        return None

    def process_of(self, window: WindowRef | None) -> int | None:
        return None

    def is_ours(self, window: WindowRef | None) -> bool:
        return False

    def is_shell(self, window: WindowRef | None) -> bool:
        return False

    def exists(self, window: WindowRef) -> bool:
        return False

    def activate(self, window: WindowRef) -> bool:
        return False

    def paste(self) -> None:
        pass

    def press_enter(self) -> None:
        pass

    def type_text(self, text: str) -> None:
        pass

    def wait_keys_released(self, limit_s: float = 1.5) -> None:
        pass

    def paste_label(self) -> str:
        return "Ctrl+V"

    def permission(self) -> Permission:
        return "unsupported"

    def request_permission(self) -> None:
        pass


class NullSounds:
    """No-op sound player implementation."""

    def play(self, event: SoundEvent) -> None:
        pass


class NullHotkeys:
    """Global hotkeys stub reporting hotkeys as unsupported."""

    def __init__(self, action: Callable[[str], None]) -> None:
        self._action = action

    def start(self, bindings: dict[str, str]) -> dict[str, str]:
        from ..i18n import t

        message = t("Global hotkeys are not supported on this system.")
        return {name: message for name in bindings}

    def escape(self, enable: bool) -> None:
        pass

    def stop(self) -> None:
        pass


class NullInstance:
    """Single-instance stub that always allows starting."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def acquire(self) -> bool:
        return True

    def listen(self, handler: Callable[[str], None]) -> None:
        pass

    def signal(self, command: str) -> bool:
        return False

    def close(self) -> None:
        pass


class NullShell:
    """No-op desktop shell integration."""

    def set_app_identity(self) -> None:
        pass

    def window_handle(self, tk_window) -> int:
        return 0

    def describe_window(self, tk_window) -> bool:
        return False

    def after_start(self, app_id: str, tasks: list[tuple[str, str]]) -> None:
        pass

    def progress(self, tk_window) -> None:
        return None

    def start_command(self) -> list[str]:
        return [sys.executable, "-m", "ucho"]

    def worker_command(self, module: str, args: list[str]) -> list[str]:
        return [sys.executable, "-m", module, *args]

    def autostart_get(self) -> str | None:
        return None

    def autostart_set(self, enabled: bool) -> bool:
        return False

    def open_path(self, path) -> None:
        pass

    def work_area(self) -> tuple[int, int, int, int] | None:
        return None

    def show_without_focus(self, tk_window) -> None:
        tk_window.deiconify()

    def set_window_icon(self, tk_window) -> None:
        pass

    def autostart_label(self) -> str:
        from ..i18n import t

        return t("Start at login")


class NullAudio:
    """Audio backend stub when no capture backend is available."""

    name = "null"

    def list_devices(self) -> dict:
        return {
            "microphones": [],
            "system": [],
            "default_microphone": None,
            "default_system": None,
            "names_by_index": {},
        }

    def open_input(self, device: DeviceInfo, channels: int, rate: int, frames: int,
                   on_data: Callable[[bytes], None]) -> Any:
        raise OSError("Audio input is not available on this system")

    def permission(self, kind: Literal["microphone", "system_audio"]) -> Permission:
        return "unsupported"

    def request_permission(self, kind: Literal["microphone", "system_audio"]) -> None:
        pass


class NullSystemInfo:
    """Empty hardware/system info stub."""

    def ram_gb(self) -> tuple[float, float, float | None]:
        return (0.0, 0.0, None)

    def cpu_name(self) -> str:
        return ""

    def physical_cores(self) -> int | None:
        return None

    def cpu_features(self) -> dict[str, bool]:
        return {}

    def adapters(self) -> list[GraphicsAdapter]:
        return []

    def ui_language(self) -> str | None:
        return None

    def reduce_motion(self) -> bool:
        return False


class NullFonts:
    """Fallback Tk font families and empty icon glyph set."""

    sans = ("Helvetica",)
    display = ("Times",)
    mono = ("Courier",)
    numeric = ("Helvetica",)

    def __init__(self) -> None:
        self.strong: dict[str, tuple[str, ...]] = {}

    def icon_font(self) -> str | None:
        return None

    def glyphs(self) -> dict[str, str]:
        return {}

    def ui_font_file(self, bold: bool) -> str | None:
        return None


class NullPermissions:
    """Permissions stub for systems without TCC-style runtime permissions."""

    def state(self) -> dict[str, Permission]:
        return {
            "microphone": "unsupported",
            "system_audio": "unsupported",
            "accessibility": "unsupported",
        }

    def request(self, kind: str) -> None:
        pass

    def settings_url(self, kind: str) -> str | None:
        return None


FACTORIES: dict[str, Callable[..., Any]] = {
    "clipboard": NullClipboard,
    "inserter": NullInserter,
    "sounds": NullSounds,
    "shell": NullShell,
    "audio": NullAudio,
    "sysinfo": NullSystemInfo,
    "fonts": NullFonts,
    "permissions": NullPermissions,
    "hotkeys": NullHotkeys,
    "instance": NullInstance,
}
