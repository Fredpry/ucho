# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS system and hardware probes via sysctl, vm_stat, and defaults."""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...hardware import GraphicsAdapter

log = logging.getLogger(__name__)

TIMEOUT_S = 2.0
_GIB = 1024**3
_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes", re.IGNORECASE)


def run_command(args: list[str]) -> str:
    """Runs `args` with a timeout and returns stdout."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=True,
    ).stdout


class MacSystemInfo:
    """macOS `SystemInfo` implementation."""

    def __init__(self, run: Callable[[list[str]], str] | None = None) -> None:
        self._run = run

    def _call(self, args: list[str]) -> str:
        runner = self._run if self._run is not None else run_command
        return runner(args)

    def ram_gb(self) -> tuple[float, float, float | None]:
        try:
            mem_bytes = int(self._call(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip())
            if mem_bytes <= 0:
                return (0.0, 0.0, None)
            total = mem_bytes / _GIB
        except Exception:
            log.debug("Could not read hw.memsize", exc_info=True)
            return (0.0, 0.0, None)

        try:
            vm_out = self._call(["/usr/bin/vm_stat"])
            lines = vm_out.splitlines()
            if not lines:
                return (total, 0.0, total)
            match = _PAGE_SIZE_RE.search(lines[0])
            if not match:
                return (total, 0.0, total)
            page_size = int(match.group(1))
            if page_size <= 0:
                return (total, 0.0, total)
            pages: dict[str, int] = {}
            for line in lines[1:]:
                key, sep, rest = line.partition(":")
                if not sep:
                    continue
                value_text = rest.strip().rstrip(".")
                if value_text.isdigit():
                    pages[key.strip().lower()] = int(value_text)
            if "pages free" not in pages and "pages inactive" not in pages:
                return (total, 0.0, total)
            free_pages = pages.get("pages free", 0) + pages.get("pages inactive", 0)
            available = (free_pages * page_size) / _GIB
            return (total, available, total)
        except Exception:
            log.debug("Could not parse vm_stat output", exc_info=True)
            return (total, 0.0, total)

    def cpu_name(self) -> str:
        try:
            return self._call(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        except Exception:
            log.debug("Could not read machdep.cpu.brand_string", exc_info=True)
            return ""

    def physical_cores(self) -> int | None:
        try:
            cores = int(self._call(["/usr/sbin/sysctl", "-n", "hw.physicalcpu"]).strip())
            return cores if cores > 0 else None
        except Exception:
            log.debug("Could not read hw.physicalcpu", exc_info=True)
            return None

    def cpu_features(self) -> dict[str, bool]:
        arm64 = False
        try:
            arm64 = self._call(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"]).strip() == "1"
        except Exception:
            log.debug("Could not read hw.optional.arm64", exc_info=True)

        avx2 = False
        avx512 = False
        try:
            tokens = {
                tok.upper()
                for tok in self._call(["/usr/sbin/sysctl", "-n", "machdep.cpu.leaf7_features"]).split()
            }
            avx2 = "AVX2" in tokens
            avx512 = "AVX512F" in tokens
        except Exception:
            log.debug("Could not read machdep.cpu.leaf7_features", exc_info=True)

        return {"arm64": arm64, "avx2": avx2, "avx512": avx512}

    def adapters(self) -> list[GraphicsAdapter]:
        try:
            from ...hardware import GraphicsAdapter

            if self.cpu_features().get("arm64", False):
                return [GraphicsAdapter("Apple GPU (Metal)")]
        except Exception:
            log.debug("Could not probe macOS graphics adapters", exc_info=True)
        return []

    def ui_language(self) -> str | None:
        try:
            raw = self._call(["/usr/bin/defaults", "read", "-g", "AppleLanguages"]).strip()
            if not raw:
                return None
            inner = raw.strip()
            if inner.startswith("(") and inner.endswith(")"):
                inner = inner[1:-1]
            for part in inner.splitlines():
                token = part.strip().rstrip(",").strip().strip('"').strip("'").strip()
                if not token:
                    continue
                lang = re.split(r"[-_]", token, maxsplit=1)[0].strip().lower()
                return lang or None
            return None
        except Exception:
            log.debug("Could not read AppleLanguages", exc_info=True)
            return None

    def reduce_motion(self) -> bool:
        try:
            out = self._call(["/usr/bin/defaults", "read", "com.apple.universalaccess", "reduceMotion"])
            return out.strip() == "1"
        except Exception:
            log.debug("Could not read reduceMotion", exc_info=True)
            return False


def create() -> MacSystemInfo:
    return MacSystemInfo()
