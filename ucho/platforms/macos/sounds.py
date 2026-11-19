# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS dictation feedback sounds via NSSound or afplay."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..base import SoundEvent

log = logging.getLogger(__name__)

SOUND_NAMES = {"start": "Tink", "stop": "Pop", "cancel": "Basso"}
SOUNDS_DIR = "/System/Library/Sounds"


class MacSounds:
    """macOS `Sounds` implementation."""

    def __init__(self, popen: Callable[..., Any] | None = None) -> None:
        self._popen = subprocess.Popen if popen is None else popen

    def play(self, event: SoundEvent) -> None:
        name = SOUND_NAMES.get(event, "Tink")
        try:
            from AppKit import NSSound

            sound = NSSound.soundNamed_(name)
            if sound is not None:
                sound.play()
                return
        except ImportError:
            pass
        except Exception:
            log.debug("NSSound playback failed for %s", name, exc_info=True)
            return

        try:
            self._popen(
                ["/usr/bin/afplay", f"{SOUNDS_DIR}/{name}.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            log.debug("afplay fallback failed for %s", name, exc_info=True)


def create() -> MacSounds:
    return MacSounds()
