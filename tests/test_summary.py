# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Filters that keep the summary from inventing names and dates.

The cases come from the description in the README: each of them was once a
real bug. Test data is Polish because the filters work on Polish speech.
"""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

from ucho import config, llm, summary
from ucho.merge import Segment
from ucho.summary import (_load_json, _name_in_text, _normalize, _without_invented_dates,
                          _words, folder_name)

TRANSCRIPT = (
    "Pawle, czy możesz przygotować budżet? Jesteśmy zainteresowani ofertą. "
    "Robimy to we wtorek 29, spotkanie o 12.00 albo 13.30. Dziękuję, Pawła poproszę o notatkę."
)


def test_inflected_name_passes():
    assert _name_in_text("Paweł", _words(TRANSCRIPT))


def test_name_hidden_in_another_word_does_not_pass():
    words = _words(TRANSCRIPT)
    assert not _name_in_text("Teresa", words)  # "zainteresowani"
    assert not _name_in_text("Robert", words)  # "robimy"


def test_unspoken_month_disappears():
    words = _words(TRANSCRIPT)
    result = _without_invented_dates("Spotkanie we wtorek 29 lutego.", words, TRANSCRIPT)
    assert "lutego" not in result
    assert "29" in result


def test_times_stay():
    words = _words(TRANSCRIPT)
    result = _without_invented_dates("Spotkanie o 12.00 albo 13.30.", words, TRANSCRIPT)
    assert "12.00" in result and "13.30" in result


def test_load_json_with_code_block_and_noise():
    assert _load_json('```json\n{"temat": "Budżet"}\n```') == {"temat": "Budżet"}
    assert _load_json('Oto wynik: {"temat": "X"} i koniec') == {"temat": "X"}
    assert _load_json("brak obiektu") is None


def test_normalize_maps_polish_schema_fields_to_english_keys():
    result = _normalize({
        "temat": "Budżet Q4.",
        "uczestnicy": ["Paweł", "Spotkanie"],
        "tldr": "Omówiono budżet.",
        "punkty": ["Budżet na Q4", "Budżet na Q4"],
        "ustalenia": ["Paweł przygotuje budżet na wtorek 29"],
    }, TRANSCRIPT)
    assert set(result) == {"topic", "participants", "tldr", "key_points", "decisions"}
    assert result["topic"] == "Budżet Q4"
    assert result["participants"] == ["Paweł"]
    assert result["key_points"] == ["Budżet na Q4"]
    assert result["decisions"] == ["Paweł przygotuje budżet na wtorek 29"]


def test_folder_name_keeps_the_timestamp():
    new = folder_name("2026-09-08_09-15_Spotkanie",
                      {"topic": "Budżet Q4", "participants": ["Anna Nowak", "Marek"]})
    assert new is not None
    assert new.startswith("2026-09-08_09-15_")
    assert "Anna" in new and "Marek" in new
    assert folder_name("bez-znacznika", {"topic": "X"}) is None


def test_llm_engine_load_and_summarize_use_custom_location_and_short_label(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "OV_CACHE_DIR", tmp_path / "ovcache")
    custom_dir = tmp_path / "PrivateUser" / "MyCustomModel-ov"
    custom_dir.mkdir(parents=True)
    for name in llm.REQUIRED_FILES:
        (custom_dir / name).write_bytes(b"")

    loaded_args: list[tuple[str, str]] = []

    class FakePipe:
        def get_tokenizer(self):
            return SimpleNamespace(apply_chat_template=lambda *_a, **_kw: "prompt")

        def generate(self, *_a, **_kw):
            return '{"temat": "Budżet Q4", "uczestnicy": [], "tldr": "Omówiono budżet.", "punkty": [], "ustalenia": []}'

    def fake_pipeline(folder: str, device: str, **_kwargs):
        loaded_args.append((folder, device))
        return FakePipe()

    fake_ov = SimpleNamespace(
        LLMPipeline=fake_pipeline,
        GenerationConfig=lambda: SimpleNamespace(max_new_tokens=0, do_sample=False),
        StructuredOutputConfig=lambda json_schema: json_schema,
    )
    monkeypatch.setitem(sys.modules, "openvino_genai", fake_ov)

    res = summary.summarize([Segment(0.0, 2.0, TRANSCRIPT, "me")], str(custom_dir), device="CPU")
    assert loaded_args == [(str(llm.location(str(custom_dir))), "CPU")]
    assert res["summary_model"] == "MyCustomModel-ov"
    assert "PrivateUser" not in res["summary_model"]
