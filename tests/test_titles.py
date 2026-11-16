# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Synthetic tests for automatic meeting titles (ucho/titles.py)."""

from __future__ import annotations

import threading

import pytest

from ucho import config, summary, titles
from ucho.merge import SPEAKER_ME, SPEAKER_MEETING, Segment

PL_SEGMENTS = [
    Segment(0.0, 4.0, "Cześć, omówmy dzisiaj budżet na Q4 oraz plan prac z Pawłem.", SPEAKER_ME),
    Segment(4.5, 9.0, "Jasne, w takim razie przejrzyjmy koszty serwerów i migrację z Jenkinsa na GitHub.", SPEAKER_MEETING),
]

EN_SEGMENTS = [
    Segment(0.0, 4.0, "Hi, we need to review the workload planning for the next sprint.", SPEAKER_ME),
    Segment(4.5, 9.0, "Sure, let us check the capacity and the release schedule.", SPEAKER_MEETING),
]

PL_TRANSCRIPT = titles.transcript_text(PL_SEGMENTS)
EN_TRANSCRIPT = titles.transcript_text(EN_SEGMENTS)


class FakeEngine:
    def __init__(self, repo_id: str, device: str = "GPU", system: str | None = None, *,
                 result: dict | None = None, error: Exception | None = None,
                 cancel_on_generate: threading.Event | None = None):
        self.repo_id = repo_id
        self.device = device
        self.system = system
        self._result = result if result is not None else {}
        self._error = error
        self._cancel_on_generate = cancel_on_generate
        self.loaded = False
        self.released = False
        self.calls: list[tuple[str, dict, int, object]] = []

    def load(self) -> None:
        self.loaded = True

    def generate_json(self, user: str, schema: dict, max_tokens: int, cancel=None) -> dict:
        self.calls.append((user, schema, max_tokens, cancel))
        if self._cancel_on_generate is not None:
            self._cancel_on_generate.set()
        if self._error is not None:
            raise self._error
        return self._result

    def release(self) -> None:
        self.released = True


def _make_factory(created: list[FakeEngine], **engine_kwargs):
    def factory(model: str, device: str = "GPU", system: str | None = None) -> FakeEngine:
        engine = FakeEngine(model, device, system=system, **engine_kwargs)
        created.append(engine)
        return engine

    return factory


# --- excerpt ----------------------------------------------------------------


def test_excerpt_short_text_returned_whole():
    text = "Ja: Krótkie spotkanie.\nSpotkanie: Tak, w porządku."
    material, has_middle = titles.excerpt(f"  {text}  ")
    assert material == text
    assert has_middle is False


def test_excerpt_long_text_bounds_and_middle():
    lines = [f"Ja: Linia numer {i:04d} z treścią spotkania o projekcie." for i in range(400)]
    text = "\n".join(lines)

    material, has_middle = titles.excerpt(text)
    assert has_middle is True
    assert titles.GAP in material
    assert len(material) <= titles.HEAD_CHARS + titles.MIDDLE_CHARS + len(titles.GAP) + 2

    head_part, middle_part = material.split(f"\n{titles.GAP}\n")
    head_lines = head_part.splitlines()
    middle_lines = middle_part.splitlines()

    assert lines[:len(head_lines)] == head_lines
    mid_index = len(lines) // 2
    assert lines[mid_index] in middle_lines

    start_index = lines.index(middle_lines[0])
    assert start_index >= len(head_lines)
    assert lines[start_index:start_index + len(middle_lines)] == middle_lines


def test_excerpt_ten_times_longer_stays_within_bound():
    lines = [f"Ja: Wypowiedź {i} o architekturze i testach automatycznych." for i in range(4000)]
    material, has_middle = titles.excerpt("\n".join(lines))
    assert has_middle is True
    assert len(material) <= titles.HEAD_CHARS + titles.MIDDLE_CHARS + len(titles.GAP) + 2


def test_excerpt_single_huge_line_has_no_middle():
    text = "x" * (titles.HEAD_CHARS + titles.MIDDLE_CHARS + 500)
    material, has_middle = titles.excerpt(text)
    assert has_middle is False
    assert len(material) == titles.HEAD_CHARS


def test_excerpt_few_long_lines_where_mid_before_head_has_no_middle():
    lines = ["a" * 500, "b" * 500, "c" * 2000]
    material, has_middle = titles.excerpt("\n".join(lines))
    assert has_middle is False
    assert material == "\n".join(lines[:2])


# --- meeting_language -------------------------------------------------------


def test_meeting_language_detection_and_hint():
    assert titles.meeting_language(PL_TRANSCRIPT) == "pl"
    assert titles.meeting_language(EN_TRANSCRIPT) == "en"
    assert titles.meeting_language(PL_TRANSCRIPT, hint="en") == "en"
    assert titles.meeting_language(EN_TRANSCRIPT, hint="pl") == "pl"
    assert titles.meeting_language("") == "pl"


# --- clean_title ------------------------------------------------------------


def test_clean_title_strips_quotes_prefixes_and_fixes_case():
    assert titles.clean_title('  "„budżet na Q4.”"  ', PL_TRANSCRIPT) == "Budżet na Q4"
    assert titles.clean_title("Title: budżet na Q4", PL_TRANSCRIPT) == "Budżet na Q4"
    assert titles.clean_title("Tytuł: plan prac", PL_TRANSCRIPT) == "Plan prac"
    assert titles.clean_title("Meeting on Workload Planning", EN_TRANSCRIPT) == "Workload Planning"
    assert titles.clean_title("plan prac", PL_TRANSCRIPT) == "Plan prac"


def test_clean_title_rejects_bad_length_generic_and_non_str():
    assert titles.clean_title(None, PL_TRANSCRIPT) == ""
    assert titles.clean_title(123, PL_TRANSCRIPT) == ""
    assert titles.clean_title("Budżet", PL_TRANSCRIPT) == ""
    assert titles.clean_title("Spotkanie", PL_TRANSCRIPT) == ""
    assert titles.clean_title("Meeting title", EN_TRANSCRIPT) == ""
    assert titles.clean_title("Tytuł spotkania", PL_TRANSCRIPT) == ""
    assert titles.clean_title("To jest zdecydowanie za długi tytuł dla tego spotkania dziś", PL_TRANSCRIPT) == ""
    assert titles.clean_title("Bardzo długie i rozbudowane omówienie architektury oprogramowania", PL_TRANSCRIPT) == ""


def test_clean_title_filters_invented_names_numbers_and_months():
    assert titles.clean_title("Spotkanie z Barbarą w sprawie budżetu", PL_TRANSCRIPT) == ""
    assert titles.clean_title("Plan prac z Pawłem", PL_TRANSCRIPT) == "Plan prac z Pawłem"
    assert titles.clean_title("Migracja z Jenkinsa na GitHub Actions", PL_TRANSCRIPT) == ""
    assert titles.clean_title("Migracja z Jenkinsa na GitHub", PL_TRANSCRIPT) == "Migracja z Jenkinsa na GitHub"
    assert titles.clean_title("Budżet na 2027", PL_TRANSCRIPT) == ""
    assert titles.clean_title("Budżet na marzec", PL_TRANSCRIPT) == "Budżet na"


# --- wants_title ------------------------------------------------------------


def test_wants_title_rules():
    assert titles.wants_title({"topic": "Budżet Q4", "name": "Spotkanie"}) is False
    assert titles.wants_title({"name_source": "user", "name": "Spotkanie"}) is False
    assert titles.wants_title({"name_source": "auto", "name": "Stary tytuł"}) is True
    assert titles.wants_title({"name": "Meeting"}) is True
    assert titles.wants_title({"name": "Spotkanie"}) is True
    assert titles.wants_title({"name": ""}) is True
    assert titles.wants_title({}) is True
    assert titles.wants_title({"name": "2026-09-08_09-15_Spotkanie"}, "2026-09-08_09-15_Spotkanie") is True
    assert titles.wants_title({"name": "Przegląd kwartalny"}) is False


# --- generate_title & LLMEngine ---------------------------------------------


def test_generate_title_happy_path_pl_and_en(caplog):
    created: list[FakeEngine] = []
    factory = _make_factory(created, result={"title": "Budżet i plan prac"})

    with caplog.at_level("INFO", logger="ucho.titles"):
        result = titles.generate_title(
            PL_SEGMENTS, config.TITLE_MODEL, device="CPU", engine_factory=factory
        )

    assert set(result) == {"name", "title_model", "title_device", "title_time_s"}
    assert result["name"] == "Budżet i plan prac"
    assert result["title_model"] == config.TITLE_MODEL
    assert result["title_device"] == "CPU"
    assert isinstance(result["title_time_s"], float)

    assert len(created) == 1
    engine = created[0]
    assert engine.loaded is True and engine.released is True
    assert engine.system == titles.SYSTEM["pl"]
    user_prompt, schema, max_tokens, _ = engine.calls[0]
    assert schema == titles.TITLE_SCHEMA
    assert max_tokens == titles.MAX_TOKENS
    assert titles.PARTS["pl"][0] in user_prompt
    assert PL_TRANSCRIPT in user_prompt
    assert "Budżet i plan prac" not in caplog.text
    assert "Jenkinsa" not in caplog.text


def test_generate_title_language_hint_and_middle_parts():
    long_segments = [
        Segment(float(i), float(i + 1), f"Omówienie elementu {i} planu prac i testów.", SPEAKER_ME)
        for i in range(120)
    ]
    created: list[FakeEngine] = []
    factory = _make_factory(created, result={"title": "Workload planning review"})

    result = titles.generate_title(
        long_segments, config.TITLE_MODEL, language="en", engine_factory=factory
    )
    assert result["name"] == "Workload planning review"
    assert created[0].system == titles.SYSTEM["en"]
    user_prompt = created[0].calls[0][0]
    assert titles.PARTS["en"][1] in user_prompt
    assert titles.GAP in user_prompt


def test_generate_title_releases_on_error_and_handles_cancel_unusable_empty():
    created: list[FakeEngine] = []
    failing = _make_factory(created, error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        titles.generate_title(PL_SEGMENTS, config.TITLE_MODEL, engine_factory=failing)
    assert created[0].released is True

    cancel = threading.Event()
    cancelled = _make_factory(created, result={"title": "Budżet i plan"}, cancel_on_generate=cancel)
    assert titles.generate_title(PL_SEGMENTS, config.TITLE_MODEL, cancel=cancel, engine_factory=cancelled) == {}
    assert created[-1].released is True

    unusable = _make_factory(created, result={"title": "Spotkanie"})
    assert titles.generate_title(PL_SEGMENTS, config.TITLE_MODEL, engine_factory=unusable) == {}
    assert created[-1].released is True

    empty_calls: list[FakeEngine] = []
    assert titles.generate_title([], config.TITLE_MODEL, engine_factory=_make_factory(empty_calls)) == {}
    assert empty_calls == []


def test_llm_engine_system_parameter():
    assert summary.LLMEngine("x", "CPU").system == summary.SYSTEM
    assert summary.LLMEngine("x", "CPU", system="Custom system").system == "Custom system"


def test_generate_title_stores_short_label_for_custom_path():
    created: list[FakeEngine] = []
    factory = _make_factory(created, result={"title": "Budżet i plan prac"})
    custom_path = "C:\\Users\\SecretUser\\models\\custom-q4_0.gguf"
    result = titles.generate_title(PL_SEGMENTS, custom_path, device="CPU", engine_factory=factory)
    assert result["title_model"] == "custom-q4_0.gguf"
    assert "SecretUser" not in result["title_model"]
