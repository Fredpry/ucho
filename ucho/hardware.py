# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Hardware detection and automatic choice of the Whisper model and device.

Why rules and a measurement instead of a small "decision model": an ML model
would need training data from hundreds of configurations we do not have, and
its choice could not be explained. Here the decision has three explicit steps:

1. `profile()` reads facts from the system (RAM, CPU, OpenVINO devices),
2. `recommend()` applies the rule table `RULES` (pure function, unit tested),
3. `benchmark()` measures the real-time factor on a synthetic signal and
   `after_benchmark()` steps one row down when the machine is too slow.

The result carries human-readable reasons, so the user sees *why* a model was
picked and can override it. `python -m ucho.hardware` prints the profile and
the recommendation for bug reports; `--save` writes it to the settings.
Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from . import config
from .i18n import language, t

log = logging.getLogger(__name__)

GIB = 1024**3

# Model ids are keys of config.MODELS.
LARGE_INT8 = "OpenVINO/whisper-large-v3-int8-ov"
LARGE_FP16 = "OpenVINO/whisper-large-v3-fp16-ov"
TURBO_INT8 = "OpenVINO/whisper-large-v3-turbo-int8-ov"
TURBO_INT4 = "OpenVINO/whisper-large-v3-turbo-int4-ov"

# Language models for meeting names and summaries. The recommendation never
# changes their device: they use the transcription device, with CPU fallback.
LLM_LARGE = "OpenVINO/Qwen3-8B-int4-ov"
LLM_MEDIUM = "OpenVINO/Qwen3-4B-int4-ov"
LLM_SMALL = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"

# The rule table accepts its choice when the measurement is at least this many
# times faster than the recording. Below that a long meeting takes noticeably
# longer to transcribe than it lasted, and the margin covers a busy machine.
MIN_SPEED = 1.5

# Windows reports RAM without the part reserved by firmware and the iGPU, so a
# "16 GB" laptop shows 15.3-15.8 GB. GetPhysicallyInstalledSystemMemory gives
# the nominal amount; when it is missing we add this tolerance instead.
RAM_TOLERANCE_GB = 1.0

# Intel GPU micro-architecture (GPU_UARCH_VERSION) from which the GPU rows
# apply: 12 = Xe-LP (Iris Xe, 11th gen and newer), 20 = Xe2 (Arc 140V in
# Lunar Lake). Gen9/Gen11 (HD/UHD 6xx, Iris Plus) are too slow for large-v3.
MIN_GPU_UARCH = 12

# Physical cores from which a CPU without a usable GPU still gets large-v3.
# Physical, not logical: SMT adds little to matrix-heavy inference, and a 4-core
# laptop with 8 threads is clearly too slow for large-v3 on the CPU.
STRONG_CPU_CORES = 8

PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
PF_AVX512F_INSTRUCTIONS_AVAILABLE = 41


# --- profile -----------------------------------------------------------------


@dataclass
class OvDevice:
    """One OpenVINO device. Empty fields mean the property was not available."""

    id: str
    name: str = ""
    type: str = ""  # "integrated" / "discrete"
    memory_gb: float | None = None
    uarch: str = ""  # GPU_UARCH_VERSION, e.g. "20.4.4"
    driver: str = ""

    @property
    def uarch_major(self) -> int | None:
        try:
            return int(self.uarch.split(".")[0])
        except (ValueError, IndexError):
            return None


@dataclass
class GraphicsAdapter:
    """Display adapter as Windows sees it (also ones OpenVINO cannot use)."""

    name: str
    driver_version: str = ""


@dataclass
class HardwareProfile:
    ram_total_gb: float = 0.0  # visible to Windows
    ram_free_gb: float = 0.0
    ram_installed_gb: float | None = None  # nominal, from SMBIOS
    cpu_name: str = ""
    logical_cores: int = 0
    physical_cores: int | None = None
    avx2: bool = False
    avx512: bool = False
    devices: list[OvDevice] = field(default_factory=list)
    adapters: list[GraphicsAdapter] = field(default_factory=list)
    openvino_version: str = ""

    @property
    def ram_gb(self) -> float:
        """RAM used by the rules: nominal when known, otherwise visible + tolerance."""
        if self.ram_installed_gb:
            return self.ram_installed_gb
        return self.ram_total_gb + RAM_TOLERANCE_GB

    @property
    def cores(self) -> int:
        return self.physical_cores or self.logical_cores

    def gpus(self) -> list[OvDevice]:
        return [d for d in self.devices if d.id.split(".")[0] == "GPU"]

    def usable_gpu(self) -> OvDevice | None:
        """Best Intel GPU for Whisper: Xe or newer, discrete before integrated.

        OpenVINO's GPU plugin only drives Intel GPUs, so every GPU it lists is
        Intel. An unknown architecture counts as usable; the benchmark then
        decides.
        """
        candidates = [
            d for d in self.gpus() if d.uarch_major is None or d.uarch_major >= MIN_GPU_UARCH
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda d: (d.type == "discrete", d.memory_gb or 0.0))

    def npu(self) -> OvDevice | None:
        return next((d for d in self.devices if d.id.split(".")[0] == "NPU"), None)


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


def _ov_property(core, device: str, name: str):
    try:
        return core.get_property(device, name)
    except Exception:
        return None


def _ov_devices() -> tuple[list[OvDevice], str]:
    """OpenVINO devices with their properties. Lazy import: works without OpenVINO."""
    try:
        import openvino as ov
    except Exception as error:
        log.info("OpenVINO not available: %s", error)
        return [], ""

    version = str(getattr(ov, "__version__", "") or "")
    try:
        core = ov.Core()
        ids = list(core.available_devices)
    except Exception as error:
        log.warning("OpenVINO did not list devices: %s", error)
        return [], version

    devices = []
    for ident in ids:
        dev = OvDevice(id=ident)
        name = _ov_property(core, ident, "FULL_DEVICE_NAME")
        if name:
            dev.name = str(name)
        kind = _ov_property(core, ident, "DEVICE_TYPE")
        if kind is not None:
            # The enum prints as "Type.INTEGRATED"; we keep "integrated".
            dev.type = str(getattr(kind, "name", kind)).split(".")[-1].lower()
        for prop in ("GPU_DEVICE_TOTAL_MEM_SIZE", "NPU_DEVICE_TOTAL_MEM_SIZE"):
            memory = _ov_property(core, ident, prop)
            if memory:
                try:
                    dev.memory_gb = int(memory) / GIB
                except (TypeError, ValueError):
                    pass
                break
        uarch = _ov_property(core, ident, "GPU_UARCH_VERSION")
        if uarch:
            dev.uarch = str(uarch)
        driver = _ov_property(core, ident, "NPU_DRIVER_VERSION")
        if driver:
            dev.driver = str(driver)
        devices.append(dev)
    return devices, version


def _safe(probe: Callable, default):
    try:
        return probe()
    except Exception:
        log.debug("Hardware probe %s failed", getattr(probe, "__name__", probe), exc_info=True)
        return default


def profile() -> HardwareProfile:
    """Reads the hardware profile. Every probe may fail without breaking the rest."""
    p = HardwareProfile(logical_cores=os.cpu_count() or 0)
    if sys.platform == "win32":
        p.ram_total_gb, p.ram_free_gb, p.ram_installed_gb = _safe(_ram, (0.0, 0.0, None))
        p.cpu_name = _safe(_cpu_name, "")
        p.physical_cores = _safe(_physical_cores, None)
        p.avx2 = _safe(lambda: _cpu_feature(PF_AVX2_INSTRUCTIONS_AVAILABLE), False)
        p.avx512 = _safe(lambda: _cpu_feature(PF_AVX512F_INSTRUCTIONS_AVAILABLE), False)
        p.adapters = _safe(_adapters, [])
    p.devices, p.openvino_version = _safe(_ov_devices, ([], ""))
    if not p.cpu_name:
        cpu = next((d for d in p.devices if d.id == "CPU"), None)
        p.cpu_name = cpu.name if cpu and cpu.name else ""
    return p


# --- rules -------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One row of the rule table (or of the step-down ladder)."""

    id: str
    model: str
    device: str  # "GPU" here means "the usable GPU found in the profile"
    dictation_model: str
    step_down: str | None  # next lighter row when the benchmark is too slow


# Rows 1-5 are the table from the v0.2 plan; the two "gpu_turbo" rows exist only
# as step-down targets. On a slow GPU the next row of the table (large-v3 on
# the CPU) would be slower still, so we keep the GPU and take a lighter model.
RULES: dict[str, Rule] = {
    r.id: r
    for r in (
        Rule("gpu_16", LARGE_INT8, "GPU", LARGE_FP16, "gpu_turbo"),
        Rule("gpu_8", LARGE_INT8, "GPU", LARGE_INT8, "gpu_turbo"),
        Rule("cpu_strong", LARGE_INT8, "CPU", TURBO_INT8, "cpu_weak"),
        Rule("cpu_weak", TURBO_INT8, "CPU", TURBO_INT4, "low_ram"),
        Rule("low_ram", TURBO_INT4, "CPU", TURBO_INT4, None),
        Rule("gpu_turbo", TURBO_INT8, "GPU", TURBO_INT8, "gpu_turbo_int4"),
        Rule("gpu_turbo_int4", TURBO_INT4, "GPU", TURBO_INT4, None),
    )
}


@dataclass
class Recommendation:
    model: str
    device: str
    dictation_model: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rule: str = ""


def _num(x: float, digits: int = 1) -> str:
    """Rounded number without a trailing ".0", decimal comma in Polish."""
    text = f"{x:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",") if language() == "pl" else text


def _model_name(repo_id: str) -> str:
    return repo_id.split("/")[-1].removeprefix("whisper-").removesuffix("-ov")


def _choose_rule(p: HardwareProfile) -> tuple[str, list[str], list[str]]:
    ram = p.ram_gb
    gpu = p.usable_gpu()
    reasons: list[str] = []
    warnings: list[str] = []

    if ram < 8:
        warnings.append(t("Less than 8 GB RAM ({ram} GB): the smallest model was chosen. "
                          "Close other programs before transcribing.", ram=_num(ram)))
        return "low_ram", reasons, warnings

    if gpu is not None:
        reasons.append(t("Graphics {gpu}: transcription on the GPU.", gpu=gpu.name or gpu.id))
        if ram >= 16:
            reasons.append(t("{ram} GB RAM: dictation gets its own FP16 model "
                             "(full precision).", ram=_num(ram)))
            return "gpu_16", reasons, warnings
        reasons.append(t("{ram} GB RAM: dictation shares the transcription model "
                         "to save memory.", ram=_num(ram)))
        return "gpu_8", reasons, warnings

    for old in p.gpus():
        reasons.append(t("{gpu} is an older Intel graphics generation, "
                         "too slow for the large model.", gpu=old.name or old.id))
    if not p.gpus():
        reasons.append(t("OpenVINO sees no Intel GPU: transcription on the CPU."))

    strong = p.avx512 or p.cores >= STRONG_CPU_CORES
    cpu_line = t("CPU {cpu}, cores: {cores}{avx}.", cpu=p.cpu_name or "?",
                 cores=p.cores, avx=", AVX-512" if p.avx512 else "")
    reasons.append(cpu_line)
    if strong and ram >= 16:
        reasons.append(t("CPU strong enough and {ram} GB RAM for the full large-v3 model.",
                         ram=_num(ram)))
        return "cpu_strong", reasons, warnings
    if not strong:
        reasons.append(t("Fewer than {n} cores and no AVX-512: the faster turbo model.",
                         n=STRONG_CPU_CORES))
    else:
        reasons.append(t("{ram} GB RAM is too little for large-v3 on the CPU: the faster turbo model.",
                         ram=_num(ram)))
    return "cpu_weak", reasons, warnings


def _from_rule(rule_id: str, p: HardwareProfile, reasons: list[str],
               warnings: list[str]) -> Recommendation:
    rule = RULES[rule_id]
    device = rule.device
    if device == "GPU":
        gpu = p.usable_gpu()
        device = gpu.id if gpu else "GPU"
    return Recommendation(rule.model, device, rule.dictation_model, reasons, warnings, rule_id)


def recommend(p: HardwareProfile) -> Recommendation:
    """Applies the rule table to a hardware profile. Pure: no I/O, no OpenVINO."""
    rule_id, reasons, warnings = _choose_rule(p)
    rec = _from_rule(rule_id, p, reasons, warnings)

    npu = p.npu()
    if npu is not None:
        rec.reasons.append(t("NPU found ({npu}). You can try it in Settings "
                             "as an experimental option.", npu=npu.name or npu.id))
    rec.reasons += _unsupported_adapters(p)
    return rec


def _unsupported_adapters(p: HardwareProfile) -> list[str]:
    """Reasons naming graphics cards OpenVINO cannot drive (NVIDIA, AMD)."""
    return [
        t("{gpu} is not supported by OpenVINO, so Ucho will not use it.", gpu=adapter.name)
        for adapter in p.adapters
        if any(word in adapter.name.upper() for word in ("NVIDIA", "RADEON", "AMD"))
    ]


@dataclass
class LlmRecommendation:
    model: str
    reasons: list[str] = field(default_factory=list)


def recommend_llm(p: HardwareProfile) -> LlmRecommendation:
    """Analysis language model (meeting names and summaries) for a hardware profile.

    Pure, like `recommend`. Only the model is chosen: the language model runs on
    the transcription device (with CPU fallback), and an integrated GPU shares
    the system RAM, so RAM decides unless a discrete GPU brings its own memory.
    """
    ram = p.ram_gb
    gpu = p.usable_gpu()
    if ram < 8:
        rec = LlmRecommendation(LLM_SMALL, [t(
            "Less than 8 GB RAM ({ram} GB): only the smallest language model fits.",
            ram=_num(ram))])
    elif gpu is not None and gpu.type == "discrete" and (gpu.memory_gb or 0) >= 8:
        rec = LlmRecommendation(LLM_LARGE, [t(
            "Graphics {gpu} with {gb} GB of its own memory: the largest language model fits.",
            gpu=gpu.name or gpu.id, gb=_num(gpu.memory_gb or 0))])
    elif gpu is not None and ram >= 16:
        rec = LlmRecommendation(LLM_LARGE, [t(
            "Graphics {gpu} and {ram} GB RAM: the largest language model runs fast enough.",
            gpu=gpu.name or gpu.id, ram=_num(ram))])
    elif gpu is not None:
        rec = LlmRecommendation(LLM_MEDIUM, [t(
            "{ram} GB RAM: the 8B model (5 GB) would leave too little memory; "
            "the 4B model writes good Polish.", ram=_num(ram))])
    else:
        rec = LlmRecommendation(LLM_MEDIUM, [t(
            "No usable GPU: the 8B model would be slow on the CPU; "
            "the 4B model writes good Polish.")])
    rec.reasons += _unsupported_adapters(p)
    return rec


def step_down(rec: Recommendation, p: HardwareProfile) -> Recommendation | None:
    """One row lighter, or None when the recommendation is already the lightest."""
    rule = RULES.get(rec.rule)
    if rule is None or rule.step_down is None:
        return None
    return _from_rule(rule.step_down, p, list(rec.reasons), list(rec.warnings))


def after_benchmark(rec: Recommendation, p: HardwareProfile, speed: float) -> Recommendation:
    """Keeps the recommendation when `speed` >= MIN_SPEED, otherwise steps one row down."""
    if speed >= MIN_SPEED:
        rec.reasons.append(t("Measured: {x}× faster than real time.", x=_num(speed)))
        return rec
    lower = step_down(rec, p)
    if lower is None:
        rec.warnings.append(t("Measured {x}× real time, below the {threshold}× threshold, and there "
                              "is no lighter model. Transcription will be slow.",
                              x=_num(speed), threshold=_num(MIN_SPEED)))
        return rec
    lower.reasons.append(t("Measured {x}× real time, below the {threshold}× threshold: "
                           "one step lighter model {model} chosen.",
                           x=_num(speed), threshold=_num(MIN_SPEED), model=_model_name(lower.model)))
    return lower


# --- benchmark ---------------------------------------------------------------


@dataclass
class BenchmarkResult:
    model: str
    device: str  # device actually used (Engine falls back to CPU)
    audio_s: float
    load_s: float
    process_s: float
    tokens: int  # decoder steps forced per run; 0 when the forcing was not accepted

    @property
    def speed(self) -> float:
        """How many times faster than real time (audio seconds per compute second)."""
        return self.audio_s / self.process_s if self.process_s > 0 else 0.0


# Decoder steps per second of audio the benchmark forces. The decoder dominates
# the cost, but on a synthetic signal Whisper emits 1-2 tokens and stops, which
# overstated the speed 2x (Arc 140V, large-v3 INT8: 0.42 s per 10 s with free
# decoding, 1.12 s with 50 forced tokens). Measured with the Whisper tokenizer
# on a business paragraph: Polish 2.56 tokens per word, English 1.23. At 130
# words per minute dense Polish speech gives ~5.5 tokens/s, English ~3. Real
# meetings have pauses (cut before the model), so 5/s is a conservative value.
TOKENS_PER_SECOND = 5

# (F1, F2, F3) in Hz for a, e, i, o, u - rough averages for an adult voice.
_FORMANTS = ((800, 1200, 2500), (500, 1800, 2500), (300, 2200, 3000),
             (500, 900, 2400), (350, 800, 2300))


def synthetic_speech(seconds: float, rate: int = 16_000, seed: int = 0):
    """Speech-like signal built from formant vowels, ~4 syllables per second.

    Deterministic, so every machine measures the same input. It passes the
    silence threshold of transcribe.py and goes through language detection
    like real speech. No user audio is involved.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    audio = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        length = int(rate * rng.uniform(0.15, 0.3))
        gap = int(rate * rng.uniform(0.03, 0.12))
        if rng.random() < 0.1:
            gap += int(rate * 0.4)  # an occasional longer pause between "words"
        end = min(n, pos + length)
        times = np.arange(end - pos) / rate
        f0 = rng.uniform(100, 180) * (1 + 0.1 * np.sin(2 * np.pi * 3 * times))
        phase = 2 * np.pi * np.cumsum(f0) / rate
        formants = _FORMANTS[rng.integers(len(_FORMANTS))]
        syllable = np.zeros_like(times)
        for k in range(1, 30):
            freq = k * f0.mean()
            if freq > rate / 2 - 500:
                break
            gain = sum(1.0 / (1.0 + ((freq - f) / 90.0) ** 2) for f in formants)
            syllable += gain / k**0.5 * np.sin(k * phase)
        syllable *= np.hanning(syllable.size)
        audio[pos:end] = syllable
        pos = end + gap
    audio += rng.normal(0, 0.002, n).astype(np.float32)
    peak = float(np.abs(audio).max()) or 1.0
    return np.ascontiguousarray(audio * (0.3 / peak), dtype=np.float32)


def _forced_kwargs(engine, tokens: int) -> dict:
    """generate() arguments that make the decoder run exactly `tokens` steps.

    Suppressing end-of-text means the model cannot stop early; max_new_tokens
    then ends the run. Language detection stays restricted as in "auto" mode.
    """
    gen_config = engine._pipe.get_generation_config()
    kwargs = {
        "task": "transcribe",
        "suppress_tokens": list(gen_config.suppress_tokens) + [int(gen_config.eos_token_id)],
        "max_new_tokens": tokens,
    }
    if engine._lang_to_id:
        kwargs["lang_to_id"] = engine._lang_to_id
    return kwargs


def benchmark(model: str, device: str, seconds: float = 10.0) -> BenchmarkResult:
    """Measures the real-time factor of `model` on `device` with a synthetic signal.

    Reuses transcribe.Engine for loading, so the GPU to CPU fallback and the
    restricted language detection behave as in a real transcription. The first
    call is a warm-up: on the GPU it allocates buffers, which a long recording
    pays only once. Raises when the model is not downloaded.
    """
    from . import transcribe

    engine = transcribe.Engine(model, device)
    start = time.monotonic()
    engine.load()
    load_s = time.monotonic() - start
    try:
        audio = synthetic_speech(seconds)
        tokens = max(1, round(seconds * TOKENS_PER_SECOND))
        try:
            kwargs = _forced_kwargs(engine, tokens)
            engine._call(audio[: transcribe.SAMPLE_RATE * 2], kwargs)
        except Exception as error:
            # An older openvino-genai may reject one of the arguments. Free
            # decoding still gives a (too optimistic) number, so we go on.
            log.warning("Forced decoding not accepted (%s), measuring free decoding", error)
            kwargs, tokens = None, 0
            engine.generate(audio[: transcribe.SAMPLE_RATE * 2], None)
        start = time.monotonic()
        if kwargs is None:
            engine.generate(audio, None)
        else:
            engine._call(audio, kwargs)
        process_s = time.monotonic() - start
        used = engine.device
    finally:
        engine.release()
    log.info("Benchmark %s on %s: %.2fs for %.0fs of audio (%d tokens), load %.1fs",
             model, used, process_s, seconds, tokens, load_s)
    return BenchmarkResult(model, used, seconds, load_s, process_s, tokens)


# --- settings ----------------------------------------------------------------

SETTINGS_KEYS = ("model", "device", "dictation_model")


def summary(p: HardwareProfile, rec: Recommendation,
            measured: BenchmarkResult | None = None) -> dict:
    """Short profile stored in settings under `hardware_profile` (no serial numbers or ids)."""
    gpu = p.usable_gpu()
    npu = p.npu()
    return {
        "cpu": p.cpu_name,
        "cores": p.cores,
        "avx512": p.avx512,
        "ram_gb": round(p.ram_gb, 1),
        "gpu": (gpu.name or gpu.id) if gpu else None,
        "npu": (npu.name or npu.id) if npu else None,
        "devices": [d.id for d in p.devices],
        "openvino": p.openvino_version,
        "rule": rec.rule,
        "recommended": {"model": rec.model, "device": rec.device,
                        "dictation_model": rec.dictation_model},
        "benchmark": None if measured is None else {
            "model": measured.model, "device": measured.device,
            "speed": round(measured.speed, 2),
        },
        "checked": time.strftime("%Y-%m-%d"),
    }


def save(settings: config.Settings, p: HardwareProfile, rec: Recommendation,
         measured: BenchmarkResult | None = None, keep_choice: bool = False) -> list[str]:
    """Writes the recommendation into settings. Returns the keys that changed.

    With `keep_choice` a key is overwritten only when it still holds the
    previous automatic recommendation, so re-running the installer refreshes
    the choice after e.g. a driver update but never undoes a manual change.
    Settings without a previous recommendation (v0.1) are kept as they are.
    """
    previous = settings["hardware_profile"]
    previous_rec = previous.get("recommended") if isinstance(previous, dict) else None
    new = {"model": rec.model, "device": rec.device, "dictation_model": rec.dictation_model}
    changed = []
    for key in SETTINGS_KEYS:
        if keep_choice and not (
            isinstance(previous_rec, dict) and settings[key] == previous_rec.get(key)
        ):
            continue
        if settings[key] != new[key]:
            settings[key] = new[key]
            changed.append(key)
    settings["hardware_profile"] = summary(p, rec, measured)
    settings.save()
    return changed


# --- CLI ---------------------------------------------------------------------


def _describe(p: HardwareProfile) -> list[str]:
    lines = [t("Hardware profile")]
    ram = t("{ram} GB visible to the system, {free} GB free",
            ram=_num(p.ram_total_gb), free=_num(p.ram_free_gb))
    if p.ram_installed_gb:
        ram += " (" + t("installed: {ram} GB", ram=_num(p.ram_installed_gb)) + ")"
    lines.append(f"  {t('RAM')}: {ram}")
    cores_text = t("cores: {physical}, threads: {logical}",
                     physical=p.physical_cores or "?", logical=p.logical_cores)
    isa = ", ".join(n for n, present in (("AVX2", p.avx2), ("AVX-512", p.avx512)) if present)
    lines.append(f"  {t('CPU')}: {p.cpu_name or '?'} ({cores_text}"
                 + (f", {isa}" if isa else "") + ")")
    if p.devices:
        lines.append(f"  {t('OpenVINO devices')} ({p.openvino_version or '?'}):")
        for d in p.devices:
            details = [x for x in (
                d.type if d.id != "CPU" else "",
                t("{gb} GB memory", gb=_num(d.memory_gb)) if d.memory_gb else "",
                f"uarch {d.uarch}" if d.uarch else "",
                t("driver {version}", version=d.driver) if d.driver else "",
            ) if x]
            lines.append(f"    {d.id}: {d.name or '?'}"
                         + (f" ({', '.join(details)})" if details else ""))
    else:
        lines.append(f"  {t('OpenVINO devices')}: {t('none (OpenVINO unavailable)')}")
    if p.adapters:
        lines.append(f"  {t('Graphics adapters')}:")
        for a in p.adapters:
            lines.append(f"    {a.name}" + (f" ({t('driver {version}', version=a.driver_version)})"
                                            if a.driver_version else ""))
    return lines


def _describe_rec(rec: Recommendation) -> list[str]:
    lines = [
        t("Recommendation"),
        f"  {t('Transcription model')}: {rec.model}",
        f"  {t('Device')}: {rec.device}",
        f"  {t('Dictation model')}: {rec.dictation_model}",
        f"  {t('Reasons')}:",
    ]
    lines += [f"    - {reason}" for reason in rec.reasons]
    if rec.warnings:
        lines.append(f"  {t('Warnings')}:")
        lines += [f"    ! {warning}" for warning in rec.warnings]
    return lines


def _describe_llm(rec: LlmRecommendation) -> list[str]:
    lines = [
        t("Language model (optional, for meeting names and summaries)"),
        f"  {t('Model')}: {rec.model}",
        f"  {t('Reasons')}:",
    ]
    return lines + [f"    - {reason}" for reason in rec.reasons]


def main(argv: list[str] | None = None) -> int:
    from . import i18n

    parser = argparse.ArgumentParser(
        prog="python -m ucho.hardware",
        description="Hardware profile and Whisper model recommendation (offline).",
    )
    parser.add_argument("--save", action="store_true",
                        help="write the recommendation to settings.json")
    parser.add_argument("--keep-choice", action="store_true",
                        help="with --save: keep model/device the user changed by hand")
    parser.add_argument("--benchmark", action="store_true",
                        help="measure the recommended model on a synthetic signal "
                             "(model must be downloaded)")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="length of the synthetic signal for --benchmark")
    parser.add_argument("--llm", action="store_true",
                        help="print only the recommended language model id")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    settings = config.Settings()
    i18n.set_language(settings["ui_language"])

    p = profile()
    if args.llm:
        # install.ps1 -WithSummary reads this one line.
        print(recommend_llm(p).model)
        return 0
    rec = recommend(p)
    measured: BenchmarkResult | None = None
    if args.benchmark:
        from . import transcribe

        if not transcribe.model_downloaded(rec.model):
            print(t("Measurement skipped: model {model} is not downloaded.", model=rec.model))
        else:
            print(t("Measuring {model} on {device} ({s} s of synthetic signal)...",
                    model=_model_name(rec.model), device=rec.device, s=_num(args.seconds)))
            try:
                measured = benchmark(rec.model, rec.device, args.seconds)
            except Exception as error:
                log.exception("Benchmark failed")
                print(t("Measurement failed: {error}", error=error))
            else:
                print(t("Load {load} s, processing {proc} s on {device}: "
                        "{x}× faster than real time.",
                        load=_num(measured.load_s), proc=_num(measured.process_s, 2),
                        device=measured.device, x=_num(measured.speed)))
                if measured.device != rec.device:
                    rec.warnings.append(t("The model did not start on {device}; the measurement is for {used}.",
                                          device=rec.device, used=measured.device))
                rec = after_benchmark(rec, p, measured.speed)

    print("\n".join(_describe(p)))
    print()
    print("\n".join(_describe_rec(rec)))
    print()
    print("\n".join(_describe_llm(recommend_llm(p))))

    if args.save:
        changed = save(settings, p, rec, measured, keep_choice=args.keep_choice)
        print()
        if changed:
            print(t("Saved to settings: {keys}.", keys=", ".join(changed)))
        else:
            print(t("Settings already match the recommendation; hardware profile saved."))
        manual = [k for k in SETTINGS_KEYS if settings[k] != getattr(rec, k)]
        if manual:
            print(t("Kept the manual choice: {keys}.", keys=", ".join(
                f"{k}={settings[k]}" for k in manual)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
