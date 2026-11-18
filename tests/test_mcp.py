# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""MCP server on a synthetic archive: protocol, tools, id security, old and new formats."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ucho_mcp.server import Archive, handle

ROOT = Path(__file__).resolve().parent.parent

OLD_ID = "2026-11-10_10-00_Budzet"
NEW_ID = "2026-11-12_14-30_Rekrutacja"
NEW_ID_2 = "2026-11-13_09-00_Standup"


def _old_session(folder: Path, name: str, date: str, segments: list[tuple]) -> None:
    """Pre-v0.2 format: Polish meta.json keys and file names."""
    session = folder / name
    session.mkdir(parents=True)
    meta = {
        "nazwa": name.split("_")[2],
        "data": date,
        "trwanie_s": 600,
        "segmenty": [{"start": s, "koniec": s + 5, "tekst": text, "mowca": speaker}
                     for s, speaker, text in segments],
    }
    (session / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (session / "transkrypt.md").write_text("# transkrypt", encoding="utf-8")
    (session / "podsumowanie.md").write_text("# Podsumowanie\nStary format.", encoding="utf-8")


def _new_session(folder: Path, name: str, date: str, segments: list[tuple],
                 summary: str | None = None) -> None:
    """Current format: English keys, speaker codes, English file names."""
    session = folder / name
    session.mkdir(parents=True)
    meta = {
        "name": name.split("_")[2],
        "date": date,
        "duration_s": 300,
        "segments": [{"start": s, "end": s + 5, "text": text, "speaker": speaker}
                     for s, speaker, text in segments],
    }
    (session / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (session / "transcript.md").write_text("# transcript", encoding="utf-8")
    if summary:
        (session / "summary.md").write_text(summary, encoding="utf-8")


@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    _old_session(tmp_path, OLD_ID, "2026-11-10 10:00", [
        (5, "Ja", "Zaczynamy od budżetu na czwarty kwartał."),
        (130, "Spotkanie", "Dostawca stali podniósł cenę o dziesięć procent."),
    ])
    _new_session(tmp_path, NEW_ID, "2026-11-12 14:30", [
        (10, "meeting", "Mamy trzech kandydatów na stanowisko."),
        (200, "me", "Umówmy rozmowy na środę."),
    ], summary="# Summary\nThree candidates.")
    _new_session(tmp_path, NEW_ID_2, "2026-11-13 09:00", [
        (3, "me", "Krótki standup zespołu."),
        (40, "meeting", "Taksówka z Łodzi kosztowała sto złotych."),
    ])
    return Archive(tmp_path)


def _call(archive, name, **arguments):
    response = handle(archive, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
    result = response["result"]
    return result["content"][0]["text"], result["isError"]


def test_initialize_and_tool_list(archive):
    init = handle(archive, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "ucho"
    tools = handle(archive, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    by_name = {tool["name"]: tool for tool in tools["result"]["tools"]}
    assert set(by_name) == {"list_meetings", "get_transcript", "get_summary", "search_transcripts"}
    assert set(by_name["list_meetings"]["inputSchema"]["properties"]) == {"from", "to", "query", "limit"}
    assert by_name["search_transcripts"]["inputSchema"]["required"] == ["query"]
    assert handle(archive, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_list_meetings_newest_first_both_formats(archive):
    text, error = _call(archive, "list_meetings")
    assert not error
    meetings = json.loads(text)
    assert [m["id"] for m in meetings] == [NEW_ID_2, NEW_ID, OLD_ID]
    old = meetings[2]
    assert old["title"] == "Budzet"
    assert old["date"] == "2026-11-10 10:00"
    assert old["duration"] == "00:10:00"
    assert old["transcript"] and old["summary"]
    new = meetings[1]
    assert new["title"] == "Rekrutacja" and new["duration"] == "00:05:00"
    assert new["transcript"] and new["summary"]
    assert meetings[0]["summary"] is False


def test_list_meetings_date_filter_and_old_alias(archive):
    text, _ = _call(archive, "list_meetings", **{"from": "2026-11-11", "to": "2026-11-12"})
    assert [m["id"] for m in json.loads(text)] == [NEW_ID]
    text, _ = _call(archive, "list_meetings", od="2026-11-11", do="2026-11-12")
    assert [m["id"] for m in json.loads(text)] == [NEW_ID]
    text, _ = _call(archive, "list_meetings", query="budzet")
    assert [m["id"] for m in json.loads(text)] == [OLD_ID]
    text, _ = _call(archive, "list_meetings", fraza="standup")
    assert [m["id"] for m in json.loads(text)] == [NEW_ID_2]


def test_get_transcript_old_format_with_minutes(archive):
    text, error = _call(archive, "get_transcript", id=OLD_ID, from_min=2)
    assert not error
    assert "Meeting: Dostawca stali" in text
    assert "budżetu" not in text
    text, _ = _call(archive, "get_transcript", id=OLD_ID, od_min=2)
    assert "Dostawca stali" in text and "budżetu" not in text
    text, _ = _call(archive, "get_transcript", id=OLD_ID)
    assert "[00:00:05] Me: Zaczynamy od budżetu" in text


def test_get_transcript_new_format_with_minutes(archive):
    text, error = _call(archive, "get_transcript", id=NEW_ID, to_min=1)
    assert not error
    assert "[00:00:10] Meeting: Mamy trzech kandydatów" in text
    assert "środę" not in text
    text, _ = _call(archive, "get_transcript", id=NEW_ID, do_min=1)
    assert "środę" not in text
    text, _ = _call(archive, "get_transcript", id=NEW_ID, from_min=3)
    assert "Me: Umówmy rozmowy na środę." in text


def test_get_summary_old_and_new_file_names(archive):
    text, _ = _call(archive, "get_summary", id=OLD_ID)
    assert "Stary format." in text
    text, _ = _call(archive, "get_summary", id=NEW_ID)
    assert "Three candidates." in text
    text, error = _call(archive, "get_summary", id=NEW_ID_2)
    assert not error and "no summary" in text


def test_search_without_diacritics_and_by_prefix(archive):
    text, error = _call(archive, "search_transcripts", query="budzet")
    assert not error
    hits = json.loads(text)
    assert hits[0]["id"] == OLD_ID
    assert hits[0]["time"] == "00:00:05"
    assert hits[0]["speaker"] == "Me"
    text, _ = _call(archive, "search_transcripts", query="kandyd")
    hit = json.loads(text)[0]
    assert hit["id"] == NEW_ID and hit["speaker"] == "Meeting"
    assert "kandydatów" in hit["excerpt"]
    text, _ = _call(archive, "search_transcripts", fraza="srod")
    assert json.loads(text)[0]["id"] == NEW_ID


def test_search_finds_l_with_stroke_without_it(archive):
    for query in ("zlotych", "lodzi", "złotych"):
        text, error = _call(archive, "search_transcripts", query=query)
        assert not error
        hit = json.loads(text)[0]
        assert hit["id"] == NEW_ID_2
        assert hit["excerpt"] == "Taksówka z Łodzi kosztowała sto złotych."


@pytest.mark.parametrize("bad_id", ["..", "../outside", "a/b", "C:\\Windows", "no-such-meeting"])
def test_id_stays_inside_archive(archive, bad_id):
    _, error = _call(archive, "get_transcript", id=bad_id)
    assert error


def _stdio(archive_dir: Path, flag: str) -> list[dict]:
    stdin = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "search_transcripts", "arguments": {"query": "stali"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_meetings", "arguments": {}}},
    ]) + "\n"
    process = subprocess.run(
        [sys.executable, str(ROOT / "mcp_server.py"), flag, str(archive_dir)],
        input=stdin.encode("utf-8"), capture_output=True, timeout=30, check=True)
    return [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]


@pytest.mark.parametrize("flag", ["--recordings-dir", "--katalog"])
def test_server_over_stdio(archive, flag):
    responses = _stdio(archive.folder, flag)
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert "Dostawca stali" in responses[1]["result"]["content"][0]["text"]
    meetings = json.loads(responses[2]["result"]["content"][0]["text"])
    assert {m["id"] for m in meetings} == {OLD_ID, NEW_ID, NEW_ID_2}


@pytest.mark.parametrize("message", [[], 5, "text", None, [1, 2]])
def test_non_object_message_is_invalid_request(archive, message):
    response = handle(archive, message)
    assert response == {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600, "message": "Invalid Request"}}


@pytest.mark.parametrize("params", [[], "x", 7, None])
def test_non_object_params_are_ignored(archive, params):
    response = handle(archive, {"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                                "params": params})
    assert "tools" in response["result"]


def test_server_survives_bad_messages(archive):
    lines = ['[1, "x"]', '7', 'null',
             '{"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": [1]}']
    process = subprocess.run(
        [sys.executable, str(ROOT / "mcp_server.py"), "--recordings-dir", str(archive.folder)],
        input=("\n".join(lines) + "\n").encode("utf-8"), capture_output=True, timeout=30,
        check=True)
    responses = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
    assert responses[0][0]["error"]["code"] == -32600
    assert responses[1]["error"]["code"] == -32600
    assert responses[2]["error"]["code"] == -32600
    assert responses[3]["id"] == 9 and "tools" in responses[3]["result"]


def _junction_or_skip(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are Windows only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_archive_rejects_dot_and_links(archive, tmp_path):
    from ucho_mcp.server import ArchiveError

    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir()
    _junction_or_skip(archive.folder / "2026-11-14_10-00_Link", outside)
    for bad in (".", "2026-11-14_10-00_Link"):
        with pytest.raises(ArchiveError):
            archive.session(bad)
    assert archive.session(NEW_ID).name == NEW_ID


def test_find_file_ignores_directories_and_links(tmp_path):
    from ucho import session as sessions

    folder = tmp_path / "s"
    folder.mkdir()
    (folder / "transcript.md").mkdir()  # a directory with the file's name
    assert sessions.find_file(folder, "transcript.md") is None
    (folder / "transcript.md").rmdir()
    (folder / "transcript.md").write_text("x", encoding="utf-8")
    assert sessions.find_file(folder, "transcript.md") == folder / "transcript.md"
    assert sessions.is_link(folder / "transcript.md") is False
