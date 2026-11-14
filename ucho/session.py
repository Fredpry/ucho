# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Reading a recorded session folder, in the current and the pre-v0.2 format.

Used by the app (recordings list, re-transcribe, re-summarize) and by the MCP
server, so both understand the same archives. Before v0.2 (and in Nagrywacz,
Ucho's predecessor) files and `meta.json` keys were Polish; they are mapped
to the current names here and nowhere else. Standard library only, because
the MCP server must not need anything else.

Nothing here writes: an old session is converted only when the app saves it
again (re-transcribe or summary).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .merge import META_FILE, TRANSCRIPT_MD, TRANSCRIPT_SRT, TRANSCRIPT_TXT, Segment, normalize_speaker

log = logging.getLogger(__name__)

MICROPHONE_FILE = "microphone.flac"
MEETING_FILE = "meeting.flac"
SUMMARY_MD = "summary.md"

# Current file name -> pre-v0.2 file name.
LEGACY_FILES = {
    MICROPHONE_FILE: "mikrofon.flac",
    MEETING_FILE: "spotkanie.flac",
    TRANSCRIPT_MD: "transkrypt.md",
    TRANSCRIPT_SRT: "transkrypt.srt",
    TRANSCRIPT_TXT: "transkrypt.txt",
    SUMMARY_MD: "podsumowanie.md",
}

# Audio track name (as in audio.Recorder) -> file name.
TRACK_FILES = {"microphone": MICROPHONE_FILE, "system": MEETING_FILE}

# Pre-v0.2 meta.json key -> current key.
LEGACY_META_KEYS = {
    "nazwa": "name",
    "data": "date",
    "trwanie_s": "duration_s",
    "segmenty": "segments",
    "uczestnicy": "participants",
    "temat": "topic",
    "punkty": "key_points",
    "ustalenia": "decisions",
    "jezyk": "language",
    "urzadzenie_mikrofon": "microphone_device",
    "urzadzenie_system": "system_device",
    "czas_transkrypcji_s": "transcription_time_s",
    "zapisano": "saved",
    "liczba_segmentow": "segment_count",
    "model_llm": "summary_model",
    "device_llm": "summary_device",
    "czas_podsumowania_s": "summary_time_s",
}
LEGACY_SEGMENT_KEYS = {"koniec": "end", "tekst": "text", "mowca": "speaker"}


def find_file(session: Path, name: str) -> Path | None:
    """The session file under its current name, else under the old one."""
    for candidate in (name, LEGACY_FILES.get(name)):
        if candidate and (session / candidate).exists():
            return session / candidate
    return None


def normalize_segment(raw) -> dict | None:
    """Segment dict with current keys and speaker code; None when unusable."""
    if not isinstance(raw, dict):
        return None
    seg = dict(raw)
    # A current key wins over its legacy twin when a file has both.
    for old, new in LEGACY_SEGMENT_KEYS.items():
        if new not in seg and old in seg:
            seg[new] = seg[old]
    try:
        return {
            "start": float(seg.get("start", 0) or 0),
            "end": float(seg.get("end", seg.get("start", 0)) or 0),
            "text": str(seg.get("text") or ""),
            "speaker": normalize_speaker(seg.get("speaker")),
        }
    except (TypeError, ValueError):
        return None


def normalize_meta(raw) -> dict:
    """meta.json content with current keys; segments normalized too."""
    if not isinstance(raw, dict):
        return {}
    meta: dict = {}
    for key, value in raw.items():
        new = LEGACY_META_KEYS.get(key, key)
        # A current key wins over its legacy twin when a file has both.
        if new != key and new in raw:
            continue
        meta[new] = value
    segments = meta.get("segments")
    if isinstance(segments, list):
        meta["segments"] = [s for s in map(normalize_segment, segments) if s is not None]
    elif "segments" in meta:
        meta["segments"] = []
    return meta


def read_meta(session: Path) -> dict:
    """Normalized meta.json of a session; {} when missing or broken."""
    try:
        raw = json.loads((session / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return normalize_meta(raw)


def segments(meta: dict) -> list[Segment]:
    """Segments of a normalized meta dict as `Segment` objects."""
    return [Segment(s["start"], s["end"], s["text"], s["speaker"])
            for s in meta.get("segments") or [] if s.get("text")]


def audio_tracks(session: Path) -> dict[str, Path]:
    """{"microphone": path, "system": path} for the tracks that exist."""
    tracks = {}
    for track, name in TRACK_FILES.items():
        path = find_file(session, name)
        if path is not None:
            tracks[track] = path
    return tracks


# A session being deleted is first renamed to this prefix plus a random part
# (retention.delete_session); what is left after a failed deletion is not a
# recording any more and must not show up anywhere.
TOMBSTONE_PREFIX = ".ucho-deleted-"


def is_tombstone(name: str) -> bool:
    return name.startswith(TOMBSTONE_PREFIX)


def list_sessions(recordings_dir: Path) -> list[Path]:
    """Session folders, newest first (names start with the timestamp)."""
    if not recordings_dir.is_dir():
        return []
    return sorted((s for s in recordings_dir.iterdir() if s.is_dir() and not is_tombstone(s.name)),
                  reverse=True)


def date_from_name(folder_name: str) -> str:
    """'2026-09-08_09-15_Standup' -> '2026-09-08 09:15'; '' for other names."""
    parts = folder_name.split("_")
    if len(parts) >= 2 and parts[1].count("-") == 1:
        return f"{parts[0]} {parts[1].replace('-', ':')}"
    return ""
