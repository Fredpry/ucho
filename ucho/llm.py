# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Analysis language model choice and local model validation (OpenVINO IR or GGUF)."""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from . import config, transcribe
from .i18n import t

log = logging.getLogger(__name__)

NONE = ""

REQUIRED_FILES = (
    "openvino_model.xml",
    "openvino_model.bin",
    "openvino_tokenizer.xml",
    "openvino_detokenizer.xml",
)

GGUF_MAGIC = b"GGUF"

GGUF_LOADABLE = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    7: "Q8_0",
}

GGUF_TYPE_NAMES = {
    **GGUF_LOADABLE,
    8: "Q5_0",
    9: "Q5_1",
    10: "Q2_K",
    11: "Q3_K_S",
    12: "Q3_K_M",
    13: "Q3_K_L",
    14: "Q4_K_S",
    15: "Q4_K_M",
    16: "Q5_K_S",
    17: "Q5_K_M",
    18: "Q6_K",
}

_GGUF_FIXED_SIZES = {
    0: 1,
    1: 1,
    2: 2,
    3: 2,
    4: 4,
    5: 4,
    6: 4,
    7: 1,
    10: 8,
    11: 8,
    12: 8,
}

_GGUF_INT_FORMATS = {
    0: "<B",
    1: "<b",
    2: "<H",
    3: "<h",
    4: "<I",
    5: "<i",
    10: "<Q",
    11: "<q",
}


def is_custom(model: str | None) -> bool:
    """True when `model` is an absolute path in Windows or POSIX form."""
    if not isinstance(model, str) or not model:
        return False
    return PureWindowsPath(model).is_absolute() or PurePosixPath(model).is_absolute()


def _read_exact(f: BinaryIO, n: int, file_size: int) -> bytes:
    if n < 0 or f.tell() + n > file_size:
        raise ValueError("Truncated GGUF header")
    data = f.read(n)
    if len(data) != n:
        raise ValueError("Truncated GGUF header")
    return data


def _skip_bytes(f: BinaryIO, n: int, file_size: int) -> None:
    if n < 0 or f.tell() + n > file_size:
        raise ValueError("Truncated GGUF header")
    f.seek(n, 1)


def _skip_gguf_value(f: BinaryIO, vtype: int, file_size: int) -> None:
    fixed = _GGUF_FIXED_SIZES.get(vtype)
    if fixed is not None:
        _skip_bytes(f, fixed, file_size)
        return
    if vtype == 8:
        (length,) = struct.unpack("<Q", _read_exact(f, 8, file_size))
        _skip_bytes(f, length, file_size)
        return
    if vtype == 9:
        elem_type, count = struct.unpack("<IQ", _read_exact(f, 12, file_size))
        elem_fixed = _GGUF_FIXED_SIZES.get(elem_type)
        if elem_fixed is not None:
            _skip_bytes(f, count * elem_fixed, file_size)
        elif elem_type in (8, 9):
            for _ in range(count):
                _skip_gguf_value(f, elem_type, file_size)
        else:
            raise ValueError(f"Unknown GGUF array element type: {elem_type}")
        return
    raise ValueError(f"Unknown GGUF value type: {vtype}")


def gguf_file_type(path: str | Path) -> int | None:
    """Reads `general.file_type` from the GGUF header without loading the rest of the file."""
    p = Path(path)
    with p.open("rb") as f:
        file_size = os.fstat(f.fileno()).st_size
        if _read_exact(f, 4, file_size) != GGUF_MAGIC:
            raise ValueError("Not a GGUF file")
        (version,) = struct.unpack("<I", _read_exact(f, 4, file_size))
        if version not in (2, 3):
            raise ValueError(f"Unsupported GGUF version: {version}")
        _tensor_count, kv_count = struct.unpack("<QQ", _read_exact(f, 16, file_size))
        for _ in range(kv_count):
            (key_len,) = struct.unpack("<Q", _read_exact(f, 8, file_size))
            key_bytes = _read_exact(f, key_len, file_size)
            try:
                key = key_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("Invalid UTF-8 in GGUF key") from error
            (vtype,) = struct.unpack("<I", _read_exact(f, 4, file_size))
            if key == "general.file_type":
                fmt = _GGUF_INT_FORMATS.get(vtype)
                if fmt is None:
                    raise ValueError(f"general.file_type is not an integer: {vtype}")
                size = _GGUF_FIXED_SIZES[vtype]
                (value,) = struct.unpack(fmt, _read_exact(f, size, file_size))
                return int(value)
            _skip_gguf_value(f, vtype, file_size)
    return None


def validate_custom(path: str | Path) -> tuple[str | None, str | None]:
    """Validates a custom OpenVINO model folder or .gguf file; returns (normalized_path, error_message)."""
    try:
        if path is None or not str(path).strip():
            return (None, t("Choose a folder with an OpenVINO model or a .gguf file."))
        p = Path(str(path).strip()).expanduser()
        if not p.is_absolute():
            p = p.absolute()
        if not p.exists():
            return (None, t("Not found: {path}", path=str(p)))
        if p.is_file() and p.name.lower() == "openvino_model.xml":
            p = p.parent
        if p.is_dir():
            missing = [f for f in REQUIRED_FILES if not (p / f).is_file()]
            if missing:
                return (
                    None,
                    t(
                        "This folder is not an OpenVINO language model: missing {files}. "
                        "Export the model with optimum-cli (OpenVINO IR) or choose a .gguf file.",
                        files=", ".join(missing),
                    ),
                )
            return (str(p), None)
        if p.is_file() and p.suffix.lower() == ".gguf":
            try:
                kind = gguf_file_type(p)
            except ValueError:
                return (None, t("This is not a valid GGUF file."))
            if kind is not None and kind not in GGUF_LOADABLE:
                return (
                    None,
                    t(
                        "This GGUF file uses {kind} quantization, which OpenVINO cannot load. "
                        "Choose a Q4_0, Q8_0 or F16 file, or an OpenVINO model folder.",
                        kind=GGUF_TYPE_NAMES.get(kind, f"type {kind}"),
                    ),
                )
            return (str(p), None)
        return (None, t("Choose a folder with an OpenVINO model or a .gguf file."))
    except (OSError, ValueError) as error:
        return (None, str(error))


def chosen(settings) -> str:
    """Returns the active analysis model (repo id, custom absolute path, or NONE)."""
    value = settings.get("analysis_model")
    if isinstance(value, str):
        return value.strip()
    summary = settings.get("summary_model") or ""
    if summary and available(summary):
        return summary
    if transcribe.model_downloaded(config.TITLE_MODEL):
        return config.TITLE_MODEL
    if settings.get("summary_enabled") and summary:
        return summary
    return NONE


def set_choice(settings, model: str) -> None:
    """Sets `analysis_model` and mirrors built-in models to `summary_model` for downgrade compatibility."""
    settings["analysis_model"] = model
    if model in config.LLM_MODELS:
        settings["summary_model"] = model


def available(model: str | None) -> bool:
    """True when `model` is on disk and usable."""
    if not model:
        return False
    try:
        if is_custom(model):
            return validate_custom(model)[1] is None
        return bool(transcribe.model_downloaded(model))
    except Exception:
        return False


def location(model: str) -> Path:
    """Filesystem path passed to `LLMPipeline` (never downloads)."""
    if not model:
        raise FileNotFoundError(t("No language model is chosen."))
    if is_custom(model):
        normalized, message = validate_custom(model)
        if normalized is None:
            raise FileNotFoundError(message)
        return Path(normalized)
    return transcribe.model_path(model, download=False)


def label(model: str | None) -> str:
    """Short, privacy-safe name for meta.json, logs and the UI (never a full local path)."""
    if not model:
        return ""
    if not is_custom(model):
        return model
    p = PureWindowsPath(model) if PureWindowsPath(model).is_absolute() else PurePosixPath(model)
    if p.name.lower() == "openvino_model.xml":
        p = p.parent
    if p.parent.name == "snapshots" and p.parent.parent.name.startswith("models--"):
        return p.parent.parent.name.removeprefix("models--").replace("--", "/")
    return p.name


def size(model: str | None) -> str:
    """Download size string for built-in models, or '' for custom/none."""
    if not model or model not in config.LLM_MODELS:
        return ""
    return config.LLM_MODELS[model]["size"]
