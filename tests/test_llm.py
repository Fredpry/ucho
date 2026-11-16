# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Synthetic tests for analysis model selection and validation (ucho/llm.py)."""

from __future__ import annotations

import logging
import struct
from pathlib import Path

import pytest

from ucho import config, i18n, llm

log = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _english_ui():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def _gguf_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _gguf_kv(key: str, vtype: int, payload: bytes) -> bytes:
    return _gguf_str(key) + struct.pack("<I", vtype) + payload


def _gguf_header(kv_items: list[bytes], version: int = 3, tensors: int = 0) -> bytes:
    return (
        llm.GGUF_MAGIC
        + struct.pack("<IQQ", version, tensors, len(kv_items))
        + b"".join(kv_items)
    )


def _make_ov_folder(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in llm.REQUIRED_FILES:
        (folder / name).write_bytes(b"")
    return folder


def test_is_custom():
    assert llm.is_custom("C:\\models\\x") is True
    assert llm.is_custom("D:/m/x.gguf") is True
    assert llm.is_custom("\\\\server\\share\\m") is True
    assert llm.is_custom("/home/u/m") is True

    assert llm.is_custom("OpenVINO/Qwen3-4B-int4-ov") is False
    assert llm.is_custom("LLM") is False
    assert llm.is_custom("") is False
    assert llm.is_custom(None) is False
    assert llm.is_custom("relative\\dir") is False


def test_gguf_file_type_versions_and_all_value_types(tmp_path: Path):
    # v2 with file_type first
    f_v2 = tmp_path / "v2.gguf"
    f_v2.write_bytes(
        _gguf_header(
            [
                _gguf_kv("general.file_type", 4, struct.pack("<I", 2)),
                _gguf_kv("after.ignored", 8, _gguf_str("ignored")),
            ],
            version=2,
        )
    )
    assert llm.gguf_file_type(f_v2) == 2

    # v3 with all value types before general.file_type
    nested_array_payload = (
        struct.pack("<IQ", 9, 2)
        + struct.pack("<IQ", 4, 2) + struct.pack("<II", 10, 20)
        + struct.pack("<IQ", 8, 1) + _gguf_str("nested")
    )
    kv_entries = [
        _gguf_kv("t.u8", 0, struct.pack("<B", 1)),
        _gguf_kv("t.i8", 1, struct.pack("<b", -2)),
        _gguf_kv("t.u16", 2, struct.pack("<H", 300)),
        _gguf_kv("t.i16", 3, struct.pack("<h", -300)),
        _gguf_kv("t.u32", 4, struct.pack("<I", 123456)),
        _gguf_kv("t.i32", 5, struct.pack("<i", -123456)),
        _gguf_kv("t.f32", 6, struct.pack("<f", 1.5)),
        _gguf_kv("t.bool", 7, struct.pack("<B", 1)),
        _gguf_kv("t.str", 8, _gguf_str("hello world")),
        _gguf_kv("t.arr_u32", 9, struct.pack("<IQ", 4, 3) + struct.pack("<III", 1, 2, 3)),
        _gguf_kv("t.arr_str", 9, struct.pack("<IQ", 8, 2) + _gguf_str("a") + _gguf_str("bc")),
        _gguf_kv("t.arr_nested", 9, nested_array_payload),
        _gguf_kv("t.u64", 10, struct.pack("<Q", 9999999999)),
        _gguf_kv("t.i64", 11, struct.pack("<q", -9999999999)),
        _gguf_kv("t.f64", 12, struct.pack("<d", 2.5)),
        _gguf_kv("general.file_type", 5, struct.pack("<i", 7)),
    ]
    f_v3 = tmp_path / "v3.gguf"
    f_v3.write_bytes(_gguf_header(kv_entries, version=3))
    assert llm.gguf_file_type(f_v3) == 7

    # Key absent -> None
    f_none = tmp_path / "none.gguf"
    f_none.write_bytes(_gguf_header([_gguf_kv("general.name", 8, _gguf_str("test"))]))
    assert llm.gguf_file_type(f_none) is None


def test_gguf_file_type_errors_and_huge_array_no_allocation(tmp_path: Path):
    bad_magic = tmp_path / "bad_magic.gguf"
    bad_magic.write_bytes(b"NOPE" + struct.pack("<IQQ", 3, 0, 0))
    with pytest.raises(ValueError):
        llm.gguf_file_type(bad_magic)

    v1 = tmp_path / "v1.gguf"
    v1.write_bytes(_gguf_header([], version=1))
    with pytest.raises(ValueError):
        llm.gguf_file_type(v1)

    truncated = tmp_path / "truncated.gguf"
    truncated.write_bytes(llm.GGUF_MAGIC + struct.pack("<I", 3))
    with pytest.raises(ValueError):
        llm.gguf_file_type(truncated)

    unknown_type = tmp_path / "unknown.gguf"
    unknown_type.write_bytes(_gguf_header([_gguf_kv("x", 99, b"\x00\x00\x00\x00")]))
    with pytest.raises(ValueError):
        llm.gguf_file_type(unknown_type)

    # Huge declared array (count 10**9 of uint8) before general.file_type in a small truncated file
    huge = tmp_path / "huge.gguf"
    huge.write_bytes(
        _gguf_header(
            [
                _gguf_kv("huge.array", 9, struct.pack("<IQ", 0, 10**9)),
                _gguf_kv("general.file_type", 4, struct.pack("<I", 2)),
            ]
        )
    )
    with pytest.raises(ValueError):
        llm.gguf_file_type(huge)


def test_validate_custom(tmp_path: Path):
    # Blank / empty
    norm, msg = llm.validate_custom("")
    assert norm is None and msg == "Choose a folder with an OpenVINO model or a .gguf file."
    norm, msg = llm.validate_custom("   ")
    assert norm is None and msg == "Choose a folder with an OpenVINO model or a .gguf file."

    # Nonexistent path
    missing_path = tmp_path / "does_not_exist"
    norm, msg = llm.validate_custom(missing_path)
    assert norm is None and msg == f"Not found: {missing_path}"

    # Valid OpenVINO folder & picking openvino_model.xml inside it
    ov_dir = _make_ov_folder(tmp_path / "my_ov_model")
    norm, msg = llm.validate_custom(ov_dir)
    assert norm == str(ov_dir) and msg is None
    norm_xml, msg_xml = llm.validate_custom(ov_dir / "openvino_model.xml")
    assert norm_xml == str(ov_dir) and msg_xml is None

    # Folder missing detokenizer
    incomplete = _make_ov_folder(tmp_path / "incomplete_ov")
    (incomplete / "openvino_detokenizer.xml").unlink()
    norm, msg = llm.validate_custom(incomplete)
    assert norm is None and msg is not None and "openvino_detokenizer.xml" in msg

    # Other file (.txt)
    txt = tmp_path / "notes.txt"
    txt.write_text("hi", encoding="utf-8")
    norm, msg = llm.validate_custom(txt)
    assert norm is None and msg == "Choose a folder with an OpenVINO model or a .gguf file."

    # GGUF Q4_0 (2) and Q8_0 (7) ok
    q4_0 = tmp_path / "model-q4_0.gguf"
    q4_0.write_bytes(_gguf_header([_gguf_kv("general.file_type", 4, struct.pack("<I", 2))]))
    assert llm.validate_custom(q4_0) == (str(q4_0), None)

    q8_0 = tmp_path / "model-q8_0.GGUF"  # upper-case suffix
    q8_0.write_bytes(_gguf_header([_gguf_kv("general.file_type", 4, struct.pack("<I", 7))]))
    assert llm.validate_custom(q8_0) == (str(q8_0), None)

    # GGUF without file_type accepted
    no_ft = tmp_path / "no-ft.gguf"
    no_ft.write_bytes(_gguf_header([_gguf_kv("general.name", 8, _gguf_str("m"))]))
    assert llm.validate_custom(no_ft) == (str(no_ft), None)

    # GGUF Q4_K_M (15) rejected with "Q4_K_M" in message
    q4_km = tmp_path / "model-q4_k_m.gguf"
    q4_km.write_bytes(_gguf_header([_gguf_kv("general.file_type", 4, struct.pack("<I", 15))]))
    norm, msg = llm.validate_custom(q4_km)
    assert norm is None and msg is not None and "Q4_K_M" in msg

    # Broken GGUF
    broken = tmp_path / "broken.gguf"
    broken.write_bytes(b"not a gguf")
    norm, msg = llm.validate_custom(broken)
    assert norm is None and msg == "This is not a valid GGUF file."

    # Never creates files
    before = sorted(p.name for p in tmp_path.iterdir())
    llm.validate_custom(tmp_path / "nonexistent_check.gguf")
    llm.validate_custom(ov_dir)
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after


def test_chosen(monkeypatch):
    downloaded: set[str] = set()
    monkeypatch.setattr(llm.transcribe, "model_downloaded", lambda m: m in downloaded)

    # Explicit string (including "", custom path, and whitespace stripped)
    assert llm.chosen({"analysis_model": "  OpenVINO/Qwen3-4B-int4-ov  "}) == "OpenVINO/Qwen3-4B-int4-ov"
    assert llm.chosen({"analysis_model": ""}) == ""
    assert llm.chosen({"analysis_model": "C:\\models\\custom.gguf"}) == "C:\\models\\custom.gguf"

    # Rule a): summary downloaded wins over TITLE_MODEL
    downloaded.update({config.DEFAULT_LLM_MODEL, config.TITLE_MODEL})
    assert llm.chosen({"analysis_model": None, "summary_model": config.DEFAULT_LLM_MODEL}) == config.DEFAULT_LLM_MODEL

    # Rule b): TITLE_MODEL when summary not downloaded
    downloaded.clear()
    downloaded.add(config.TITLE_MODEL)
    assert llm.chosen({"analysis_model": None, "summary_model": config.DEFAULT_LLM_MODEL}) == config.TITLE_MODEL

    # Rule c): summary_enabled with nothing downloaded -> summary_model
    downloaded.clear()
    assert llm.chosen({
        "analysis_model": None,
        "summary_model": config.DEFAULT_LLM_MODEL,
        "summary_enabled": True,
    }) == config.DEFAULT_LLM_MODEL

    # Rule d): nothing -> "" (also when key is missing from a plain dict)
    assert llm.chosen({"summary_model": config.DEFAULT_LLM_MODEL, "summary_enabled": False}) == ""
    assert llm.chosen({}) == ""


def test_set_choice():
    s = {"summary_model": config.DEFAULT_LLM_MODEL}
    llm.set_choice(s, config.TITLE_MODEL)
    assert s["analysis_model"] == config.TITLE_MODEL
    assert s["summary_model"] == config.TITLE_MODEL

    llm.set_choice(s, "C:\\models\\my.gguf")
    assert s["analysis_model"] == "C:\\models\\my.gguf"
    assert s["summary_model"] == config.TITLE_MODEL

    llm.set_choice(s, llm.NONE)
    assert s["analysis_model"] == ""
    assert s["summary_model"] == config.TITLE_MODEL


def test_available(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(llm.transcribe, "model_downloaded", lambda m: m == "OpenVINO/Qwen3-4B-int4-ov")
    assert llm.available(llm.NONE) is False
    assert llm.available(None) is False
    assert llm.available("OpenVINO/Qwen3-4B-int4-ov") is True
    assert llm.available("OpenVINO/Qwen3-8B-int4-ov") is False

    ov_dir = _make_ov_folder(tmp_path / "ov")
    assert llm.available(str(ov_dir)) is True
    assert llm.available(str(tmp_path / "missing")) is False


def test_location(tmp_path: Path, monkeypatch):
    calls: list[tuple[str, bool]] = []

    def fake_model_path(model: str, download: bool = False) -> Path:
        calls.append((model, download))
        return tmp_path / "hub"

    monkeypatch.setattr(llm.transcribe, "model_path", fake_model_path)

    with pytest.raises(FileNotFoundError):
        llm.location(llm.NONE)

    with pytest.raises(FileNotFoundError):
        llm.location(str(tmp_path / "missing_dir"))

    ov_dir = _make_ov_folder(tmp_path / "valid_ov")
    assert llm.location(str(ov_dir)) == ov_dir
    assert llm.location(str(ov_dir / "openvino_model.xml")) == ov_dir

    assert llm.location("OpenVINO/Qwen3-4B-int4-ov") == tmp_path / "hub"
    assert calls == [("OpenVINO/Qwen3-4B-int4-ov", False)]


def test_label_and_size():
    assert llm.label(None) == ""
    assert llm.label("") == ""
    assert llm.label("OpenVINO/Qwen3-8B-int4-ov") == "OpenVINO/Qwen3-8B-int4-ov"

    assert llm.label("C:\\Users\\Alice\\models\\Bielik-4.5B-ov") == "Bielik-4.5B-ov"
    assert llm.label("C:\\Users\\Alice\\models\\Bielik-4.5B-ov\\openvino_model.xml") == "Bielik-4.5B-ov"
    assert llm.label("/home/alice/models/model-q4_0.gguf") == "model-q4_0.gguf"
    assert "Alice" not in llm.label("C:\\Users\\Alice\\models\\my.gguf")

    hf_win = (
        "C:\\Users\\Alice\\Ucho\\models\\hub\\models--OpenVINO--Qwen3-8B-int4-ov"
        "\\snapshots\\5c47abf4b8e12ebe8e99745bb0c1ec17e0c0abcc"
    )
    assert llm.label(hf_win) == "OpenVINO/Qwen3-8B-int4-ov"
    assert llm.label(hf_win + "\\openvino_model.xml") == "OpenVINO/Qwen3-8B-int4-ov"

    assert llm.size(config.DEFAULT_LLM_MODEL) == config.LLM_MODELS[config.DEFAULT_LLM_MODEL]["size"]
    assert llm.size("C:\\models\\x.gguf") == ""
    assert llm.size("") == ""
    assert llm.size(None) == ""
