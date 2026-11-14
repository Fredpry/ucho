# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Deleting recordings: a whole session on request, and old audio by the retention setting.

Both delete files for good (no Recycle Bin), so every path goes through
`session_folder` first: it must resolve to a folder directly inside the
configured recordings directory, and must not be a symlink or junction
(a link inside the archive could point anywhere on the disk).

Retention removes only the audio tracks (.flac/.wav) of sessions that have a
finished transcript and are older than the chosen number of days; the
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
import shutil
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import session
from .merge import META_FILE, TRANSCRIPT_MD

log = logging.getLogger(__name__)

# Choices offered in Settings; 0 = never. settings.json may hold another
# positive number of days (set by hand or by an administrator); it is honored.
RETENTION_CHOICES = (0, 7, 30, 90)
AUDIO_SUFFIXES = (".flac", ".wav")
AUDIO_REMOVED_KEY = "audio_removed"


class DeleteError(Exception):
    """A path that must not be deleted, or a deletion that failed."""


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


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
    if _is_link(folder):
        raise DeleteError("A link, not a session folder")
    try:
        resolved = folder.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise DeleteError("Session folder not found") from error
    if resolved == root or resolved.parent != root or not resolved.is_dir() or _is_link(resolved):
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


def delete_session(recordings_dir: Path, folder: Path) -> None:
    """Deletes a session folder with all its files, permanently. Raises DeleteError."""
    target = session_folder(recordings_dir, folder)
    try:
        # rmtree does not follow symlinks or junctions inside the folder; it removes the link.
        shutil.rmtree(target, onexc=_clear_readonly_and_retry)
    except OSError as error:
        raise DeleteError(f"{type(error).__name__} (winerror {getattr(error, 'winerror', None)})") from error
    log.info("Deleted 1 session folder")


# --- audio retention ------------------------------------------------------------------


def retention_days(value) -> int:
    """The setting as whole days; 0 (never) for anything that is not a positive number."""
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 0
    return days if days > 0 else 0


def audio_files(folder: Path) -> list[Path]:
    """Audio tracks of a session (current and v0.1 names, any .flac/.wav at the top level)."""
    try:
        return sorted(p for p in folder.iterdir()
                      if p.suffix.lower() in AUDIO_SUFFIXES and p.is_file() and not p.is_symlink())
    except OSError:
        return []


def has_transcript(folder: Path) -> bool:
    """A finished transcript: transcript.md (or the v0.1 name) exists and is not empty."""
    file = session.find_file(folder, TRANSCRIPT_MD)
    try:
        return file is not None and file.is_file() and file.stat().st_size > 0
    except OSError:
        return False


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
    if not days or _is_link(folder) or not folder.is_dir():
        return False
    if active is not None and active(folder):
        return False
    if not audio_files(folder) or not has_transcript(folder):
        return False
    meta_file = folder / META_FILE
    meta = session.read_meta(folder)
    if meta_file.exists() and not meta:
        return False  # unreadable meta.json: the flag could not be written, leave it alone
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


def _mark_audio_removed(folder: Path, when: datetime) -> None:
    """Adds `audio_removed` to meta.json, keeping every other key as it is (v0.1 keys too)."""
    file = folder / META_FILE
    raw: dict = {}
    if file.exists():
        loaded = json.loads(file.read_text(encoding="utf-8"))
        raw = loaded if isinstance(loaded, dict) else {}
    raw[AUDIO_REMOVED_KEY] = when.isoformat(timespec="seconds")
    temp = file.with_name(file.name + ".tmp")
    temp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, file)


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
