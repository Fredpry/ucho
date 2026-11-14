# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Deleting recordings and the audio retention policy, on synthetic sessions only."""
import json
import os
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ucho import merge, retention, session
from ucho.archive import Archive, ArchiveError

NOW = datetime(2026, 11, 14, 12, 0)


def _session(root: Path, name: str, date: datetime | None = None, transcript: bool = True,
             audio: tuple[str, ...] = ("microphone.flac", "meeting.flac"), meta: dict | None = None,
             text: str = "The budget for the first quarter.") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    data = {"name": name.split("_")[-1], "segments": [
        {"start": 1, "end": 4, "text": text, "speaker": "me"}]}
    if date is not None:
        data["date"] = date.strftime("%Y-%m-%d %H:%M")
    if meta is not None:
        data = meta
    (folder / "meta.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for file in audio:
        (folder / file).write_bytes(b"fLaC synthetic")
    if transcript:
        (folder / "transcript.md").write_text("# Transcript\n\nsynthetic", encoding="utf-8")
        (folder / "transcript.srt").write_text("1\n", encoding="utf-8")
        (folder / "summary.md").write_text("# Summary", encoding="utf-8")
    return folder


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are Windows only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs Developer Mode or admin rights here")


# --- path guard -----------------------------------------------------------------------


def test_guard_accepts_a_direct_session_folder(tmp_path):
    root = tmp_path / "recordings"
    folder = _session(root, "2026-11-01_10-00_Standup")
    assert retention.session_folder(root, folder) == folder.resolve()
    # Another spelling of the same child is fine: it is still that session.
    assert retention.session_folder(root, root / "x" / ".." / folder.name) == folder.resolve()


@pytest.mark.parametrize("make", [
    lambda root, out: root,                                   # the archive itself
    lambda root, out: root / ".",                             # the archive, spelled differently
    lambda root, out: root / "2026-11-01_10-00_Standup" / "..",
    lambda root, out: root.parent,                            # above the archive
    lambda root, out: out,                                    # a folder elsewhere
    lambda root, out: root / ".." / out.name,                 # elsewhere, through ..
    lambda root, out: root / "2026-11-01_10-00_Standup" / "sub",  # nested, not a session
    lambda root, out: root / "missing",                       # does not exist
    lambda root, out: root / "2026-11-01_10-00_Standup" / "meta.json",  # a file
])
def test_guard_refuses_paths_outside_or_not_a_session(tmp_path, make):
    root = tmp_path / "recordings"
    folder = _session(root, "2026-11-01_10-00_Standup")
    (folder / "sub").mkdir()
    outside = tmp_path / "Documents"
    outside.mkdir()
    with pytest.raises(retention.DeleteError):
        retention.session_folder(root, make(root, outside))


def test_guard_refuses_a_missing_recordings_folder(tmp_path):
    with pytest.raises(retention.DeleteError):
        retention.session_folder(tmp_path / "nope", tmp_path / "nope" / "x")


def test_guard_refuses_a_junction_to_outside_and_to_a_sibling(tmp_path):
    root = tmp_path / "recordings"
    sibling = _session(root, "2026-11-01_10-00_Standup")
    outside = tmp_path / "Documents"
    outside.mkdir()
    _junction(root / "2026-11-02_10-00_Link-out", outside)
    _junction(root / "2026-11-03_10-00_Link-in", sibling)
    for name in ("2026-11-02_10-00_Link-out", "2026-11-03_10-00_Link-in"):
        with pytest.raises(retention.DeleteError):
            retention.session_folder(root, root / name)
        with pytest.raises(retention.DeleteError):
            retention.delete_session(root, root / name)
    assert outside.is_dir() and sibling.is_dir()


def test_guard_refuses_a_symlink(tmp_path):
    root = tmp_path / "recordings"
    _session(root, "2026-11-01_10-00_Standup")
    outside = tmp_path / "Documents"
    outside.mkdir()
    _symlink(root / "2026-11-02_10-00_Link", outside)
    with pytest.raises(retention.DeleteError):
        retention.session_folder(root, root / "2026-11-02_10-00_Link")


def test_guard_allows_a_recordings_folder_that_is_itself_a_junction(tmp_path):
    real = tmp_path / "D-drive" / "recordings"
    folder = _session(real, "2026-11-01_10-00_Standup")
    link = tmp_path / "recordings"
    _junction(link, real)
    assert retention.session_folder(link, link / folder.name) == folder.resolve()


# --- deleting a session -------------------------------------------------------------------


def test_delete_session_removes_the_folder_and_every_file(tmp_path):
    root = tmp_path / "recordings"
    keep = _session(root, "2026-11-01_10-00_Standup")
    folder = _session(root, "2026-11-02_10-00_Budget")
    (folder / "extra.txt").write_text("x", encoding="utf-8")
    (folder / "nested").mkdir()
    (folder / "nested" / "file.bin").write_bytes(b"x")
    readonly = folder / "transcript.txt"
    readonly.write_text("x", encoding="utf-8")
    os.chmod(readonly, stat.S_IREAD)
    retention.delete_session(root, folder)
    assert not folder.exists()
    assert keep.is_dir() and session.list_sessions(root) == [keep]


def test_delete_session_does_not_follow_a_junction_inside(tmp_path):
    root = tmp_path / "recordings"
    folder = _session(root, "2026-11-02_10-00_Budget")
    outside = tmp_path / "Documents"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    _junction(folder / "link", outside)
    retention.delete_session(root, folder)
    assert not folder.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_deleted_session_disappears_from_search(tmp_path):
    root = tmp_path / "recordings"
    _session(root, "2026-11-01_10-00_Standup", text="Weekly standup notes.")
    folder = _session(root, "2026-11-02_10-00_Budget", text="Steel price went up.")
    archive = Archive(root)
    assert [h.id for h in archive.search("steel", 10)] == [folder.name]
    retention.delete_session(root, folder)
    assert archive.search("steel", 10) == []
    assert [m.session.name for m in archive.find("")] == ["2026-11-01_10-00_Standup"]


def test_delete_with_an_open_file_deletes_nothing(tmp_path):
    root = tmp_path / "recordings"
    folder = _session(root, "2026-11-02_10-00_Budget")
    before = sorted(p.name for p in folder.iterdir())
    if sys.platform != "win32":
        pytest.skip("only Windows refuses to rename a folder with an open file")
    with open(folder / "meeting.flac", "rb"):  # e.g. a media player
        with pytest.raises(retention.DeleteError) as caught:
            retention.delete_session(root, folder)
    assert not isinstance(caught.value, retention.PartialDeleteError)
    assert sorted(p.name for p in folder.iterdir()) == before
    assert session.list_sessions(root) == [folder]


def _partial_rmtree(path, onexc=None):
    """rmtree that deletes one file and then hits a file another program holds."""
    files = sorted(p for p in Path(path).iterdir() if p.is_file())
    files[0].unlink()
    raise PermissionError(13, "in use", str(files[1]))


def test_partly_deleted_session_leaves_the_archive_and_is_cleaned_up_later(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    keep = _session(root, "2026-11-01_10-00_Standup", text="Weekly standup notes.")
    folder = _session(root, "2026-11-02_10-00_Budget", text="Steel price went up.")
    archive = Archive(root)
    assert archive.search("steel", 10)
    monkeypatch.setattr(retention.shutil, "rmtree", _partial_rmtree)
    with pytest.raises(retention.PartialDeleteError) as caught:
        retention.delete_session(root, folder)
    rest = caught.value.rest
    assert not folder.exists() and rest.parent == root and rest.is_dir()
    assert session.is_tombstone(rest.name) and "Budget" not in rest.name
    # Neither the list, the search nor the MCP id lookup sees the remains.
    assert session.list_sessions(root) == [keep]
    assert archive.search("steel", 10) == []
    assert [m.session for m in archive.find("")] == [keep]
    with pytest.raises(ArchiveError):
        archive.session(rest.name)
    assert retention.select_expired(root, 1, NOW) == [keep]
    # Later, with the file no longer in use.
    monkeypatch.undo()
    assert retention.remove_leftovers(root) == 1
    assert not rest.exists() and keep.is_dir()


def test_remove_leftovers_touches_only_tombstones(tmp_path):
    root = tmp_path / "recordings"
    keep = _session(root, "2026-11-01_10-00_Standup")
    outside = tmp_path / "Documents"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    _junction(root / (session.TOMBSTONE_PREFIX + "link"), outside)
    assert retention.remove_leftovers(root) == 0
    assert keep.is_dir() and (outside / "keep.txt").exists()


# --- audio retention ------------------------------------------------------------------


def test_retention_days_parsing(caplog, monkeypatch):
    monkeypatch.setattr(retention, "_reported_values", set())
    assert retention.retention_days(0) == 0
    assert retention.retention_days(None) == 0
    assert retention.retention_days(14) == 14
    assert retention.retention_days(365) == 365
    with caplog.at_level("WARNING", logger="ucho.retention"):
        # Only non-negative integers count; everything else means never.
        for odd in (True, False, "30", "7.5", 7.5, -5, "never", [7]):
            assert retention.retention_days(odd) == 0, odd
        retention.retention_days(True)  # reported once per value
    assert caplog.text.count("True is not a whole number") == 1
    assert "'7.5' is not a whole number" in caplog.text


def _transcribed(root, name, segments, **flags):
    """A session saved the way meeting.py saves it, with the given completion keys."""
    folder = root / name
    folder.mkdir(parents=True)
    for file in ("microphone.flac", "meeting.flac"):
        (folder / file).write_bytes(b"fLaC synthetic")
    meta = {"name": name.split("_")[-1], "date": (NOW - timedelta(days=40)).strftime("%Y-%m-%d %H:%M")}
    meta.update(flags)
    merge.save_results(folder, segments, meta)
    return folder


SEGMENTS = [merge.Segment(1.0, 3.0, "The budget for the first quarter.", "me")]


def test_empty_transcript_keeps_its_audio(tmp_path):
    root = tmp_path / "recordings"
    # The reviewer's case: save_results always writes a header, even for no segments.
    empty = _transcribed(root, "2026-10-01_10-00_Empty", [], transcript_complete=True)
    legacy_empty = _transcribed(root, "2026-10-02_10-00_Old-empty", [])
    assert retention.has_transcript(empty) and retention.has_transcript(legacy_empty)
    assert retention.select_expired(root, 7, NOW) == []
    assert retention.apply_retention(root, 7, now=NOW) == (0, 0)


def test_cancelled_or_failed_transcript_keeps_its_audio(tmp_path):
    root = tmp_path / "recordings"
    _transcribed(root, "2026-10-01_10-00_Stopped", SEGMENTS, transcript_complete=False,
                 transcription_cancelled=True)
    _transcribed(root, "2026-10-02_10-00_Gaps", SEGMENTS, transcript_complete=False,
                 transcription_errors=3)
    # A hand-edited flag does not override the cancel marker.
    _transcribed(root, "2026-10-03_10-00_Odd", SEGMENTS, transcript_complete=True,
                 transcription_cancelled=True)
    _transcribed(root, "2026-10-04_10-00_Text", SEGMENTS, transcript_complete="yes")
    assert retention.select_expired(root, 7, NOW) == []


def test_complete_transcript_loses_its_audio(tmp_path):
    root = tmp_path / "recordings"
    folder = _transcribed(root, "2026-10-01_10-00_Done", SEGMENTS, transcript_complete=True)
    assert retention.select_expired(root, 7, NOW) == [folder]
    assert retention.apply_retention(root, 7, now=NOW) == (1, 2)


def test_legacy_sessions_need_at_least_one_utterance(tmp_path):
    root = tmp_path / "recordings"
    with_segments = _transcribed(root, "2026-10-01_10-00_Old", SEGMENTS)
    # No segments in meta.json (hand-made or very old), utterances only in transcript.md.
    md_only = _session(root, "2026-10-02_10-00_Md-only", NOW - timedelta(days=40), meta={"name": "x"})
    (md_only / "transcript.md").write_text(
        "# Transcript\n\n---\n\n**[00:00:01] Me:** We start.\n", encoding="utf-8")
    header_only = _session(root, "2026-10-03_10-00_Header", NOW - timedelta(days=40), meta={"name": "y"})
    assert sorted(retention.select_expired(root, 7, NOW)) == sorted([with_segments, md_only])
    assert header_only.is_dir() and len(retention.audio_files(header_only)) == 2


def test_selection_by_age_transcript_and_activity(tmp_path):
    root = tmp_path / "recordings"
    old = _session(root, "2026-10-01_10-00_Old", NOW - timedelta(days=44))
    exactly = _session(root, "2026-11-07_12-00_Week", NOW - timedelta(days=7))
    young = _session(root, "2026-11-08_12-01_Young", NOW - timedelta(days=6, hours=23))
    untranscribed = _session(root, "2026-10-02_10-00_Raw", NOW - timedelta(days=40), transcript=False)
    empty = _session(root, "2026-10-03_10-00_Empty", NOW - timedelta(days=40))
    (empty / "transcript.md").write_text("", encoding="utf-8")
    busy = _session(root, "2026-10-04_10-00_Busy", NOW - timedelta(days=40))
    no_audio = _session(root, "2026-10-05_10-00_Gone", NOW - timedelta(days=40), audio=())

    selected = retention.select_expired(root, 7, NOW, active=lambda f: f.name == busy.name)
    assert sorted(selected) == sorted([old, exactly])
    assert young not in selected and untranscribed not in selected and no_audio not in selected
    assert retention.select_expired(root, 30, NOW) == [busy, old]
    assert retention.select_expired(root, 0, NOW) == []


def test_age_falls_back_to_the_folder_name(tmp_path):
    root = tmp_path / "recordings"
    folder = _session(root, "2026-10-01_10-00_No-date", meta={"name": "No date", "segments": [
        {"start": 1, "end": 2, "text": "Hello.", "speaker": "me"}]})
    assert retention.recorded_at(folder) == datetime(2026, 10, 1, 10, 0)
    assert retention.select_expired(root, 30, NOW) == [folder]


def test_retention_removes_only_audio_and_marks_meta(tmp_path):
    root = tmp_path / "recordings"
    folder = _session(root, "2026-10-01_10-00_Old", NOW - timedelta(days=44),
                      audio=("microphone.flac", "meeting.flac", "extra.wav"))
    young = _session(root, "2026-11-13_10-00_Young", NOW - timedelta(days=1))
    sessions_done, files_done = retention.apply_retention(root, 30, now=NOW)
    assert (sessions_done, files_done) == (1, 3)
    assert retention.audio_files(folder) == []
    assert sorted(p.name for p in folder.iterdir()) == [
        "meta.json", "summary.md", "transcript.md", "transcript.srt"]
    meta = session.read_meta(folder)
    assert retention.audio_removed(meta) and meta["audio_removed"] == "2026-11-14T12:00:00"
    assert meta["segments"] and meta["name"] == "Old"  # the rest of meta.json is untouched
    assert len(retention.audio_files(young)) == 2 and not retention.audio_removed(session.read_meta(young))
    # Nothing left to do on the next run.
    assert retention.apply_retention(root, 30, now=NOW) == (0, 0)


def test_retention_handles_v01_sessions_and_keeps_their_keys(tmp_path):
    root = tmp_path / "recordings"
    folder = root / "2026-09-01_10-00_Budzet"
    folder.mkdir(parents=True)
    raw = {"nazwa": "Budżet", "data": "2026-09-01 10:00",
           "segmenty": [{"start": 1, "koniec": 2, "tekst": "Zaczynamy.", "mowca": "Ja"}]}
    (folder / "meta.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    (folder / "mikrofon.flac").write_bytes(b"x")
    (folder / "spotkanie.flac").write_bytes(b"x")
    (folder / "transkrypt.md").write_text("# Transkrypt", encoding="utf-8")
    assert retention.apply_retention(root, 30, now=NOW) == (1, 2)
    assert session.audio_tracks(folder) == {}
    written = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert written["nazwa"] == "Budżet" and "segmenty" in written and written["audio_removed"]


def test_retention_skips_a_session_that_became_active(tmp_path):
    root = tmp_path / "recordings"
    folder = _session(root, "2026-10-01_10-00_Old", NOW - timedelta(days=44))
    selected = retention.select_expired(root, 30, NOW)
    assert selected == [folder]
    # A transcription started between the selection and the deletion.
    assert retention.apply_retention(root, 30, active=lambda f: True, now=NOW, folders=selected) == (0, 0)
    assert len(retention.audio_files(folder)) == 2


def test_retention_leaves_a_session_with_broken_meta_alone(tmp_path):
    root = tmp_path / "recordings"
    folder = _session(root, "2026-10-01_10-00_Old", NOW - timedelta(days=44))
    (folder / "meta.json").write_text("{not json", encoding="utf-8")
    assert retention.apply_retention(root, 30, now=NOW) == (0, 0)
    assert len(retention.audio_files(folder)) == 2


def test_retention_ignores_linked_session_folders(tmp_path):
    root = tmp_path / "recordings"
    root.mkdir()
    elsewhere = _session(tmp_path / "elsewhere", "2026-10-01_10-00_Old", NOW - timedelta(days=44))
    _junction(root / "2026-10-01_10-00_Old", elsewhere)
    assert retention.select_expired(root, 30, NOW) == []
    assert retention.apply_retention(root, 30, now=NOW, folders=[root / "2026-10-01_10-00_Old"]) == (0, 0)
    assert len(retention.audio_files(elsewhere)) == 2


def test_retention_logs_counts_not_names(tmp_path, caplog):
    root = tmp_path / "recordings"
    _session(root, "2026-10-01_10-00_Secret-merger", NOW - timedelta(days=44))
    with caplog.at_level("INFO", logger="ucho.retention"):
        retention.apply_retention(root, 30, now=NOW)
    assert "removed 2 audio files from 1 sessions" in caplog.text
    assert "Secret" not in caplog.text


def test_selection_for_the_settings_screen_runs_off_the_calling_thread(tmp_path, monkeypatch):
    import threading

    root = tmp_path / "recordings"
    folder = _session(root, "2026-10-01_10-00_Old", NOW - timedelta(days=44))
    threads, delivered = [], []
    real = retention.select_expired

    def select(recordings_dir, days, now=None, active=None):
        threads.append(threading.current_thread())
        return real(recordings_dir, days, NOW, active)

    monkeypatch.setattr(retention, "select_expired", select)
    retention.select_expired_in_background(root, 30, delivered.append).join(10)
    assert delivered == [[folder]] and threads[0] is not threading.current_thread()

    def broken(*_args, **_kwargs):
        raise OSError("network drive gone")

    monkeypatch.setattr(retention, "select_expired", broken)
    retention.select_expired_in_background(root, 30, delivered.append).join(10)
    assert delivered[-1] is None
