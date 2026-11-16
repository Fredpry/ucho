# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Speaker names and diarized speaker labels across summary, archive, MCP and installer."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ucho import summary
from ucho.archive import Archive
from ucho.merge import Segment, save_results
from ucho.summary import (
    _LABEL_NOTE,
    MAX_PARTICIPANTS,
    _label_note,
    _names,
    _participants,
    _prompt_label,
    known_names,
    summarize,
)
from ucho_mcp.server import call_tool

ROOT = Path(__file__).resolve().parent.parent


def test_prompt_label_named_plain_and_diarized():
    names = {"meeting:1": "  Anna Nowak  ", "me": "Piotr"}
    assert _prompt_label("meeting:1", names) == "Anna Nowak"
    assert _prompt_label("me", names) == "Piotr"
    assert _prompt_label("me") == "Ja"
    assert _prompt_label("meeting") == "Spotkanie"
    assert _prompt_label("meeting:2", names) == "Mówca 2"
    assert _prompt_label("me:3", names) == "Mikrofon 3"
    assert _prompt_label("custom") == "custom"


def test_label_note_with_and_without_names():
    assert _label_note([]) == _LABEL_NOTE
    assert "Mówca N:" in _LABEL_NOTE and "Mikrofon N:" in _LABEL_NOTE
    with_names = _label_note(["Anna Nowak", "Marek"])
    assert with_names.startswith(_LABEL_NOTE)
    assert "Imiona przed dwukropkiem nadał użytkownik (Anna Nowak, Marek): to są uczestnicy spotkania." in with_names


def test_known_names_order_and_dedup():
    segments = [
        Segment(0.0, 2.0, "Cześć.", "meeting:2"),
        Segment(2.0, 4.0, "Dzień dobry.", "meeting:1"),
        Segment(4.0, 6.0, "Zaczynamy.", "me"),
        Segment(6.0, 8.0, "Tak.", "meeting:3"),
    ]
    names = {
        "meeting:1": "Anna",
        "meeting:2": "  Marek  ",
        "me": "anna",
        "meeting:4": "Ktoś spoza segmentów",
    }
    assert known_names(segments, names) == ["Marek", "Anna"]
    assert known_names([], names) == []
    assert known_names(segments, None) == []


def test_participants_known_first_dedup_and_cap():
    # Verbatim user-given names (including lowercase or role words) stay first;
    # duplicates of a known name or its first word are dropped.
    assert _participants(
        ["Anna Nowak", "dyrektor"],
        ["Anna", "Anna Nowak", "Marek", "Kasia"],
    ) == ["Anna Nowak", "dyrektor", "Marek", "Kasia"]

    # Total capped at MAX_PARTICIPANTS.
    found = [f"Osoba{i}" for i in range(10)]
    capped = _participants(["Anna", "Marek"], found)
    assert len(capped) == MAX_PARTICIPANTS
    assert capped[:2] == ["Anna", "Marek"]

    # More known names than MAX_PARTICIPANTS: all known names stay, no found name is added.
    many_known = [f"Znany {i}" for i in range(MAX_PARTICIPANTS + 2)]
    assert _participants(many_known, ["Marek"]) == many_known


def test_names_drops_known_names_and_mikrofon():
    text = "Anna, Marek i Piotr rozmawiają o budżecie."
    assert _names(["Anna", "Pan Anna Nowak", "Marek", "Mikrofon", "Piotr"], text, known=["Anna Nowak"]) == [
        "Marek",
        "Piotr",
    ]


def test_summarize_uses_speaker_names_and_filters_invented(monkeypatch):
    prompts: list[str] = []

    class FakeLLMEngine:
        def __init__(self, repo_id: str, device: str = "GPU"):
            self.repo_id = repo_id
            self.device = device

        def load(self) -> None:
            pass

        def release(self) -> None:
            pass

        def generate_json(self, user: str, schema: dict, max_tokens: int, cancel=None) -> dict:
            prompts.append(user)
            return {
                "temat": "Budżet",
                "uczestnicy": ["Anna", "Marek", "Zenon"],
                "tldr": "Omówiono budżet na kolejny kwartał.",
                "punkty": ["Koszty dostaw wzrosły."],
                "ustalenia": ["Marek zbierze drugą ofertę."],
            }

    monkeypatch.setattr(summary, "LLMEngine", FakeLLMEngine)
    segments = [
        Segment(1.0, 5.0, "Dzień dobry Marku, zaczynamy od budżetu.", "meeting:1"),
        Segment(6.0, 10.0, "Zajmę się drugą ofertą do piątku.", "meeting:2"),
    ]
    result = summarize(segments, "fake/model", speaker_names={"meeting:1": "Anna Nowak"})

    assert len(prompts) == 1
    assert "Anna Nowak: Dzień dobry Marku" in prompts[0]
    assert "Mówca 2: Zajmę się drugą ofertą" in prompts[0]
    assert "Imiona przed dwukropkiem nadał użytkownik (Anna Nowak): to są uczestnicy spotkania." in prompts[0]
    assert result["participants"] == ["Anna Nowak", "Marek"]


def test_archive_and_mcp_propagate_speaker_names(tmp_path: Path):
    folder = tmp_path / "2026-11-16_10-00_Spotkanie"
    segments = [
        Segment(2.0, 6.0, "Omawiamy harmonogram wdrożenia.", "meeting:1"),
        Segment(7.0, 11.0, "Testy zaczynamy we wtorek.", "meeting:2"),
        Segment(12.0, 15.0, "Potwierdzam harmonogram.", "me"),
    ]
    meta = {
        "name": "Spotkanie projektowe",
        "date": "2026-11-16 10:00",
        "duration_s": 60,
        "speaker_names": {"meeting:1": "Grażyna"},
    }
    save_results(folder, segments, meta)

    archive = Archive(tmp_path)
    desc = archive.describe(folder)
    assert desc["speakers"] == ["Grażyna"]

    matches = archive.find("grazyna")
    assert len(matches) == 1
    assert matches[0].session == folder
    assert matches[0].title_match

    hits_1 = archive.search("wdrozenia", 10)
    assert len(hits_1) == 1 and hits_1[0].name == "Grażyna"
    hits_2 = archive.search("wtorek", 10)
    assert len(hits_2) == 1 and hits_2[0].name == ""

    transcript = call_tool(archive, "get_transcript", {"id": folder.name})
    assert "[00:00:02] Grażyna: Omawiamy harmonogram wdrożenia." in transcript
    assert "[00:00:07] Speaker 2: Testy zaczynamy we wtorek." in transcript
    assert "[00:00:12] Me: Potwierdzam harmonogram." in transcript

    rows_1 = json.loads(call_tool(archive, "search_transcripts", {"query": "wdrozenia"}))
    assert rows_1[0]["speaker"] == "Grażyna"
    rows_2 = json.loads(call_tool(archive, "search_transcripts", {"query": "wtorek"}))
    assert rows_2[0]["speaker"] == "Speaker 2"

    # Renaming the speaker updates the archive index when meta.json changes.
    meta["speaker_names"] = {"meeting:1": "Barbara"}
    save_results(folder, segments, meta)
    meta_file = folder / "meta.json"
    stat = meta_file.stat()
    os.utime(meta_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert archive.find("grazyna") == []
    assert [m.session for m in archive.find("barbara")] == [folder]
    assert archive.search("wdrozenia", 10)[0].name == "Barbara"


def test_install_ps1_has_with_speakers_and_stays_ascii():
    data = (ROOT / "install.ps1").read_bytes()
    assert data.isascii()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    text = data.decode("ascii")
    assert "WithSpeakers" in text
