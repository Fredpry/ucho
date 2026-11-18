# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Deleting recordings: a whole session on request, and old audio by the retention setting.

Both delete files for good (no Recycle Bin), so every path goes through
`session_folder` first: it must resolve to a folder directly inside the
configured recordings directory, and must not be a symlink or junction
(a link inside the archive could point anywhere on the disk).

Retention removes only the audio tracks (.flac/.wav) of sessions that have a
complete transcript (`transcript_complete`) and are older than the chosen
number of days; the
transcript, the summary and `meta.json` stay, and `meta.json` gets
`audio_removed` so the app can say so instead of offering a player or
"Transcribe again". Sessions the app is recording, transcribing or
summarizing are skipped through the `active` callback.

Standard library only (tests run without the UI packages). Logs carry counts,
never folder names: the names are meeting titles.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from collections.abc import Callable

from . import session
from .config import write_json
from .merge import META_FILE, TRANSCRIPT_COMPLETE, TRANSCRIPT_MD, TRANSCRIPTION_CANCELLED

log = logging.getLogger(__name__)

# Choices offered in Settings; 0 = never. settings.json may hold another
# positive number of days (set by hand or by an administrator); it is honored.
RETENTION_CHOICES = (0, 7, 30, 90)
AUDIO_SUFFIXES = (".flac", ".wav")
AUDIO_REMOVED_KEY = "audio_removed"


class DeleteError(Exception):
    """A path that must not be deleted, or a deletion that failed (nothing was deleted)."""


class PartialDeleteError(DeleteError):
    """The session was taken out of the archive, but some of its files could not be deleted.

    `rest` is the tombstone folder that still holds them (not listed as a
    session); `remove_leftovers` tries again later.
    """

    def __init__(self, message: str, rest: Path):
        super().__init__(message)
        self.rest = rest


def session_folder(recordings_dir: Path, folder: Path) -> Path:
    """`folder` resolved, after checking that it is a session folder of the archive.

    Raises DeleteError unless `folder` is an existing folder, not a symlink or
    junction, whose resolved parent is the resolved recordings directory. `..`
    tricks and links that lead elsewhere (or to the archive itself) are refused.
    """
    # The recordings folder itself may be a link (moved to another drive with
    # a junction); only what lies inside it is checked.
    try:
        root = Path(recordings_dir).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise DeleteError("Recordings folder not found") from error
    if not root.is_dir():
        raise DeleteError("The recordings folder is not a folder")
    folder = Path(folder).absolute()
    if session.is_link(folder):
        raise DeleteError("A link, not a session folder")
    try:
        resolved = folder.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise DeleteError("Session folder not found") from error
    if resolved == root or resolved.parent != root or not resolved.is_dir() or session.is_link(resolved):
        raise DeleteError("Not a session folder of the recordings folder")
    # The path as given must name the same folder (no "..", no other spelling
    # that resolves differently): its own parent resolves to the archive too.
    if folder.name in ("", ".", "..") or folder.parent.resolve() != root:
        raise DeleteError("Not a session folder of the recordings folder")
    return resolved


def _clear_readonly_and_retry(function, path, _error) -> None:
    # Windows refuses to delete read-only files (e.g. copied from a CD or a
    # synced share); clear the flag and try once more.
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _error_text(error: OSError) -> str:
    # The type and code only: the message would contain the path, i.e. the meeting title.
    return f"{type(error).__name__} (winerror {getattr(error, 'winerror', None)})"


def delete_session(recordings_dir: Path, folder: Path) -> None:
    """Deletes a session folder with all its files, permanently.

    Raises DeleteError when nothing was deleted, PartialDeleteError when only
    part of it was. rmtree deletes file by file and stops at the first file
    another program has open, which would leave half a recording behind. So
    the folder is first renamed to a tombstone name in the same folder: Windows refuses that rename while any file inside is open, and
    then nothing has been touched. After the rename the session is gone from
    the archive (`session.list_sessions` skips tombstones) even if a file
    cannot be deleted.
    """
    target = session_folder(recordings_dir, folder)
    tombstone = target.parent / f"{session.TOMBSTONE_PREFIX}{uuid.uuid4().hex[:12]}"
    try:
        target.rename(tombstone)
    except OSError as error:
        raise DeleteError(_error_text(error)) from error
    try:
        # rmtree does not follow symlinks or junctions inside the folder; it removes the link.
        shutil.rmtree(tombstone, onexc=_clear_readonly_and_retry)
    except OSError as error:
        if not tombstone.exists():
            # Removed meanwhile by `remove_leftovers` on the retention thread.
            log.info("Deleted 1 session folder")
            return
        log.warning("Session folder only partly deleted: %s; the rest is in %s",
                    _error_text(error), tombstone.name)
        raise PartialDeleteError(_error_text(error), tombstone) from error
    log.info("Deleted 1 session folder")


def remove_leftovers(recordings_dir: Path) -> int:
    """Deletes tombstones left by deletions that failed halfway. Returns how many went."""
    try:
        root = Path(recordings_dir).resolve(strict=True)
        candidates = [p for p in root.iterdir() if session.is_tombstone(p.name)]
    except (OSError, RuntimeError):
        return 0
    removed = 0
    for folder in candidates:
        if session.is_link(folder) or not folder.is_dir():
            continue
        try:
            shutil.rmtree(folder, onexc=_clear_readonly_and_retry)
            removed += 1
        except OSError:
            pass  # still open somewhere: next time
    if removed:
        log.info("Removed %d leftover folders of deleted recordings", removed)
    return removed


# --- audio retention ------------------------------------------------------------------


_reported_values: set[str] = set()


def retention_days(value) -> int:
    """The setting as whole days; 0 (never) for anything but a non-negative integer.

    settings.json can be edited by hand. Values like `true` (which int() made
    1 day), "30" or 7.5 are not guessed at: they mean never, and the log says
    so once per value.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if value is not None:
        text = repr(value)[:40]
        if text not in _reported_values:
            _reported_values.add(text)
            log.warning("audio_retention_days %s is not a whole number of days >= 0 - audio is kept",
                        text)
    return 0


def audio_files(folder: Path) -> list[Path]:
    """Audio tracks of a session (current and v0.1 names, any .flac/.wav at the top level)."""
    try:
        return sorted(p for p in folder.iterdir()
                      if p.suffix.lower() in AUDIO_SUFFIXES and p.is_file() and not p.is_symlink())
    except OSError:
        return []


def has_transcript(folder: Path) -> bool:
    """transcript.md (or the v0.1 name) exists and is not empty; see also `transcript_complete`."""
    file = session.find_file(folder, TRANSCRIPT_MD)
    try:
        return file is not None and file.is_file() and file.stat().st_size > 0
    except OSError:
        return False


# An utterance line of transcript.md: "**[00:01:02] Me:** text" (merge.as_markdown).
_UTTERANCE = re.compile(r"^\*\*\[\d{1,3}:\d{2}:\d{2}\]", re.MULTILINE)


def _transcript_has_utterances(folder: Path) -> bool:
    file = session.find_file(folder, TRANSCRIPT_MD)
    try:
        return file is not None and bool(_UTTERANCE.search(file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def transcript_complete(folder: Path, meta: dict) -> bool:
    """True when the transcript can stand in for the audio.

    transcript.md alone proves nothing: it always has a header, and Stop
    during a transcription saves the segments so far as a normal result. So
    meeting.py records `transcript_complete` in meta.json, true only for a
    run that was neither cancelled nor had failed chunks; it is required
    here, together with at least one segment.

    Sessions transcribed before that flag existed (including v0.1) cannot
    tell a cancelled run from a finished one. For them the rule is the
    conservative one that can still be checked: at least one utterance, in
    meta.json or in transcript.md. An empty or header-only transcript keeps
    its audio; an old transcript cut short by Stop cannot be detected and is
    treated like a finished one.
    """
    if TRANSCRIPT_COMPLETE in meta:
        if meta.get(TRANSCRIPT_COMPLETE) is not True or meta.get(TRANSCRIPTION_CANCELLED):
            return False
        return bool(session.segments(meta))
    return bool(session.segments(meta)) or _transcript_has_utterances(folder)


def recorded_at(folder: Path, meta: dict | None = None) -> datetime | None:
    """When the session was recorded: meta date, else the folder name, else the newest audio file."""
    meta = session.read_meta(folder) if meta is None else meta
    for text in (meta.get("date"), session.date_from_name(folder.name)):
        if isinstance(text, str) and text:
            try:
                return datetime.strptime(text.strip()[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
    times = []
    for file in audio_files(folder):
        try:
            times.append(file.stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(max(times)) if times else None


def audio_expired(folder: Path, days: int, now: datetime | None = None,
                  active: Callable[[Path], bool] | None = None) -> bool:
    """True when retention may remove the audio of `folder` now."""
    days = retention_days(days)
    if not days or session.is_link(folder) or not folder.is_dir():
        return False
    if active is not None and active(folder):
        return False
    if not audio_files(folder) or not has_transcript(folder):
        return False
    meta_file = folder / META_FILE
    meta = session.read_meta(folder)
    if meta_file.exists() and not meta:
        return False  # unreadable meta.json: the flag could not be written, leave it alone
    if not transcript_complete(folder, meta):
        return False
    when = recorded_at(folder, meta)
    now = now or datetime.now()
    return when is not None and now - when >= timedelta(days=days)


def select_expired(recordings_dir: Path, days: int, now: datetime | None = None,
                   active: Callable[[Path], bool] | None = None) -> list[Path]:
    """Session folders whose audio retention would remove now."""
    if not retention_days(days):
        return []
    result = []
    for folder in session.list_sessions(Path(recordings_dir)):
        try:
            session_folder(recordings_dir, folder)
        except DeleteError:
            continue
        if audio_expired(folder, days, now, active):
            result.append(folder)
    return result


def select_expired_in_background(recordings_dir: Path, days: int,
                                 deliver: Callable[[list[Path] | None], None]) -> threading.Thread:
    """`select_expired` on a worker thread: it reads every meta.json, too slow for the UI thread.

    `deliver` gets the list, or None when the selection failed, on the worker
    thread; the UI posts it to its own thread and re-checks `active` there.
    """
    def work() -> None:
        try:
            selected = select_expired(recordings_dir, days)
        except Exception as error:
            # The type only: messages of file errors contain paths, i.e. meeting titles.
            log.error("Selecting expired audio failed (%s)", type(error).__name__)
            selected = None
        deliver(selected)

    thread = threading.Thread(target=work, name="retention-select", daemon=True)
    thread.start()
    return thread


def _mark_audio_removed(folder: Path, when: datetime) -> None:
    """Adds `audio_removed` to meta.json, keeping every other key as it is (v0.1 keys too)."""
    file = folder / META_FILE
    raw: dict = {}
    if file.exists():
        loaded = json.loads(file.read_text(encoding="utf-8"))
        raw = loaded if isinstance(loaded, dict) else {}
    raw[AUDIO_REMOVED_KEY] = when.isoformat(timespec="seconds")
    write_json(file, raw)


def remove_audio(recordings_dir: Path, folder: Path, now: datetime | None = None) -> int:
    """Deletes the audio files of one session and marks it in meta.json. Returns the file count.

    Callers check eligibility (`audio_expired`) right before; this only
    re-checks that the folder belongs to the archive.
    """
    target = session_folder(recordings_dir, folder)
    removed = 0
    for file in audio_files(target):
        try:
            file.unlink()
            removed += 1
        except PermissionError:
            try:
                os.chmod(file, stat.S_IWRITE)
                file.unlink()
                removed += 1
            except OSError:
                pass  # open in a player: next run
        except OSError:
            pass
    if removed:
        try:
            _mark_audio_removed(target, now or datetime.now())
        except (OSError, ValueError) as error:
            # The type only: the message would contain the path, i.e. the meeting title.
            log.warning("Audio removed but meta.json could not be updated (%s)", type(error).__name__)
    return removed


def apply_retention(recordings_dir: Path, days: int, active: Callable[[Path], bool] | None = None,
                    now: datetime | None = None, folders: list[Path] | None = None) -> tuple[int, int]:
    """Removes expired audio. Returns (sessions, files). `folders`: a preselected list to re-check."""
    days = retention_days(days)
    if not days:
        return 0, 0
    candidates = select_expired(recordings_dir, days, now, active) if folders is None else folders
    sessions_done = files_done = 0
    for folder in candidates:
        # Re-checked here: a transcription may have started since the selection.
        if not audio_expired(folder, days, now, active):
            continue
        try:
            removed = remove_audio(recordings_dir, folder, now)
        except DeleteError:
            continue
        if removed:
            sessions_done += 1
            files_done += removed
    if sessions_done:
        log.info("Audio retention (%d days): removed %d audio files from %d sessions",
                 days, files_done, sessions_done)
    return sessions_done, files_done


def audio_removed(meta: dict) -> bool:
    return bool(meta.get(AUDIO_REMOVED_KEY))
