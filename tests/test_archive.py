# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Shared archive search used by the app's recordings list and the MCP server."""
import json
from pathlib import Path

import pytest

from ucho.archive import Archive, ArchiveError, fold


def _session(folder: Path, name: str, meta: dict) -> None:
    session = folder / name
    session.mkdir(parents=True)
    (session / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    _session(tmp_path, "2026-11-10_10-00_Budzet", {
        "nazwa": "Budżet", "segmenty": [
            {"start": 5, "koniec": 9, "tekst": "Zaczynamy od budżetu.", "mowca": "Ja"}]})
    _session(tmp_path, "2026-11-12_14-30_Rekrutacja", {
        "name": "Rekrutacja", "topic": "Rekrutacja programistów",
        "tldr": "Trzech kandydatów, rozmowy w środę.",
        "segments": [
            {"start": 10, "end": 15, "text": "Mamy trzech kandydatów.", "speaker": "meeting"},
            {"start": 200, "end": 205, "text": "Budżet na rekrutację jest zatwierdzony.",
             "speaker": "me"}]})
    _session(tmp_path, "2026-11-13_09-00_Standup", {"name": "Standup", "segments": []})
    return Archive(tmp_path)


def test_fold_removes_diacritics():
    assert fold("Zażółć GĘŚLĄ jaźń") == "zazolc gesla jazn"


def test_empty_query_lists_everything_newest_first(archive):
    names = [m.session.name for m in archive.find("")]
    assert names == ["2026-11-13_09-00_Standup", "2026-11-12_14-30_Rekrutacja",
                     "2026-11-10_10-00_Budzet"]


def test_find_by_transcript_without_diacritics(archive):
    matches = archive.find("budzet")
    assert [m.session.name for m in matches] == ["2026-11-12_14-30_Rekrutacja",
                                                 "2026-11-10_10-00_Budzet"]
    assert matches[0].hits[0].start == 200
    assert matches[1].title_match  # the old-format name "Budżet"


def test_find_by_title_summary_and_folder_name(archive):
    assert [m.session.name for m in archive.find("programist")] == ["2026-11-12_14-30_Rekrutacja"]
    assert [m.session.name for m in archive.find("srodę")] == ["2026-11-12_14-30_Rekrutacja"]
    match = archive.find("standup")[0]
    assert match.title_match and not match.hits


def test_index_follows_changes(archive):
    assert archive.find("faktur") == []
    _session(archive.folder, "2026-11-14_08-00_Finanse", {
        "segments": [{"start": 1, "end": 2, "text": "Wysłałem fakturę.", "speaker": "me"}]})
    assert [m.session.name for m in archive.find("faktur")] == ["2026-11-14_08-00_Finanse"]


def test_search_rejects_empty_query(archive):
    with pytest.raises(ArchiveError):
        archive.search("  ...  ", 10)


@pytest.mark.parametrize("bad_id", ["..", "../x", "a/b", "C:\\x", "missing"])
def test_session_id_stays_inside(archive, bad_id):
    with pytest.raises(ArchiveError):
        archive.session(bad_id)
