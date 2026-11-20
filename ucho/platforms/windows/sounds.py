# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Windows dictation feedback tones via winsound."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..base import SoundEvent

TONES = {"start": (1046, 70), "stop": (659, 70), "cancel": (330, 120)}


class WindowsSounds:
    """Win32 `Sounds` implementation."""

    def play(self, event: SoundEvent) -> None:
        frequency, ms = TONES.get(event, TONES["start"])

        def _play():
            try:
                import winsound

                winsound.Beep(frequency, ms)
            except Exception:
                pass

        threading.Thread(target=_play, daemon=True).start()


def create() -> WindowsSounds:
    return WindowsSounds()
