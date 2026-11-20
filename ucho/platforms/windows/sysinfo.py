# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Windows system and hardware probes via WinAPI and registry."""

from __future__ import annotations

import logging

from ...hardware import GIB, GraphicsAdapter

log = logging.getLogger(__name__)

PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
PF_AVX512F_INSTRUCTIONS_AVAILABLE = 41


def _ram() -> tuple[float, float, float | None]:
    """(visible, free, installed) in GiB via GlobalMemoryStatusEx."""
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    kernel32 = ctypes.windll.kernel32
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")

    installed: float | None = None
    kb = ctypes.c_ulonglong(0)
    try:
        if kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb)) and kb.value:
            installed = kb.value / 1024**2
    except Exception:
        log.debug("GetPhysicallyInstalledSystemMemory unavailable", exc_info=True)
    return status.ullTotalPhys / GIB, status.ullAvailPhys / GIB, installed


def _cpu_name() -> str:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    ) as key:
        return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()


def _physical_cores() -> int | None:
    """Number of RelationProcessorCore records from GetLogicalProcessorInformationEx."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    relation_processor_core = 0
    length = wintypes.DWORD(0)
    kernel32.GetLogicalProcessorInformationEx(relation_processor_core, None, ctypes.byref(length))
    if not length.value:
        return None
    buffer = ctypes.create_string_buffer(length.value)
    if not kernel32.GetLogicalProcessorInformationEx(
        relation_processor_core, buffer, ctypes.byref(length)
    ):
        return None
    # Records have variable length: DWORD Relationship, DWORD Size, then data.
    count, offset = 0, 0
    while offset + 8 <= length.value:
        size = int.from_bytes(buffer.raw[offset + 4: offset + 8], "little")
        if size <= 0:
            break
        count += 1
        offset += size
    return count or None


def _cpu_feature(feature: int) -> bool:
    import ctypes

    return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(feature))


def _adapters() -> list[GraphicsAdapter]:
    """Display adapters from the registry (device class "Display")."""
    import winreg

    path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    result: list[GraphicsAdapter] = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as device_class:
        i = 0
        while True:
            try:
                subkey = winreg.EnumKey(device_class, i)
            except OSError:
                break
            i += 1
            if not subkey.isdigit():
                continue  # "Properties" and similar are not adapters
            try:
                with winreg.OpenKey(device_class, subkey) as adapter:
                    name = str(winreg.QueryValueEx(adapter, "DriverDesc")[0])
                    try:
                        version = str(winreg.QueryValueEx(adapter, "DriverVersion")[0])
                    except OSError:
                        version = ""
            except OSError:
                continue
            if all(a.name != name for a in result):
                result.append(GraphicsAdapter(name, version))
    return result


class WindowsSystemInfo:
    """Win32 `SystemInfo` implementation."""

    def ram_gb(self) -> tuple[float, float, float | None]:
        return _ram()

    def cpu_name(self) -> str:
        return _cpu_name()

    def physical_cores(self) -> int | None:
        return _physical_cores()

    def cpu_features(self) -> dict[str, bool]:
        return {
            "avx2": _cpu_feature(PF_AVX2_INSTRUCTIONS_AVAILABLE),
            "avx512": _cpu_feature(PF_AVX512F_INSTRUCTIONS_AVAILABLE),
        }

    def adapters(self) -> list[GraphicsAdapter]:
        return _adapters()

    def ui_language(self) -> str | None:
        import ctypes

        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        # The lower 10 bits are the primary language; 0x15 = Polish.
        return "pl" if (langid & 0x3FF) == 0x15 else "en"

    def reduce_motion(self) -> bool:
        try:
            import ctypes

            enabled = ctypes.c_int(1)
            spi_getclientareaanimation = 0x1042
            ctypes.windll.user32.SystemParametersInfoW(spi_getclientareaanimation, 0,
                                                       ctypes.byref(enabled), 0)
            return not enabled.value
        except Exception:
            return False


def create() -> WindowsSystemInfo:
    return WindowsSystemInfo()
