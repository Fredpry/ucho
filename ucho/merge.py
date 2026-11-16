# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Merging the transcripts of two tracks into one transcript split by speaker.

The microphone is "me", the system audio is "meeting". Both tracks are
recorded in parallel and aligned to the wall clock, so sorting segments by
start time is enough to rebuild the course of the conversation.

Segments store stable speaker codes; the localized labels ("Me"/"Ja",
"Meeting"/"Spotkanie") are rendered only on output.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .i18n import t

SPEAKER_ME = "me"
SPEAKER_MEETING = "meeting"
SPEAKER_LABELS = {SPEAKER_ME: "Me", SPEAKER_MEETING: "Meeting"}
# Speaker values written before v0.2: the label in the UI language of the time.
LEGACY_SPEAKERS = {"Ja": SPEAKER_ME, "Me": SPEAKER_ME,
                   "Spotkanie": SPEAKER_MEETING, "Meeting": SPEAKER_MEETING}

TRANSCRIPT_MD = "transcript.md"
TRANSCRIPT_SRT = "transcript.srt"
TRANSCRIPT_TXT = "transcript.txt"
META_FILE = "meta.json"

# On silence and noise Whisper produces stock phrases from film subtitles.
# We drop the most common ones so they do not litter the transcript. The
# patterns match Polish and English speech, so they stay in both languages.
# The closing lines of subtitles and videos (`_OUTROS`) also get appended after
# the last real sentence, so they are cut from the end of a text as well
# (`strip_outro`).
_OUTROS = [
    r"^napisy (stworzone przez|robione przez|wykonane przez)",
    r"amara\.?org",
    r"^zapraszam(y)? (do subskrypcji|na kolejny)",
    r"^subskrybuj",
    # "dzi[eę]kuj": it used to be plain "dziekuj", so a real "Dziękuję" with
    # the Polish "ę" never matched (a test caught it).
    r"^dzi[eę]kuj[eę] za (uwag[eę]|obejrzenie|ogl[aą]danie)",
    r"^subtitles? (by|created by|provided by)",
    r"^transcri(pt|ption) by",
    r"^thanks? for watching",
    r"^please subscribe",
    r"^napisy:",
]
_HALLUCINATIONS = _OUTROS + [
    # Whisper puts these phrases in place of silence, also in Polish recordings.
    r"^(thank you|thanks|you|bye)[.!]?$",
    # A lone filler with no sentence around it is almost always a noise artifact.
    r"^(so|um|uh+|hmm+|mm+|ah+|oh+|eh+|yeah|no|tak|no tak)[.,!?]?$",
    r"^\W*$",
]
_HALLUCINATION_RES = [re.compile(p, re.IGNORECASE) for p in _HALLUCINATIONS]
_OUTRO_RES = [re.compile(p, re.IGNORECASE) for p in _OUTROS]
_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+")

# Minimum segment duration; shorter ones are usually artifacts.
_MIN_DURATION = 0.15
# Gap above which two utterances of the same speaker are not joined.
_MAX_JOIN_GAP = 1.5


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str


def normalize_speaker(value) -> str:
    """Speaker code from a stored value (also the pre-v0.2 labels)."""
    value = str(value or "")
    return LEGACY_SPEAKERS.get(value, value)


MAX_SPEAKER_NAME = 40


def split_speaker(code) -> tuple[str, int | None]:
    """Splits a diarized code like 'meeting:3' into ('meeting', 3); else (code, None)."""
    code = str(code or "")
    base, sep, rest = code.partition(":")
    if sep and base in (SPEAKER_ME, SPEAKER_MEETING) and rest.isascii() and rest.isdigit():
        n = int(rest)
        if n > 0:
            return base, n
    return code, None


def speaker_track(code: str) -> str:
    """Base track code of a diarized code ('meeting:2' -> 'meeting'), else `code`."""
    return split_speaker(code)[0]


def diarized_code(track_speaker: str, n: int) -> str:
    """Diarized speaker code for the `n`-th voice (1-based) on `track_speaker`."""
    return f"{track_speaker}:{n}"


def clean_speaker_name(value) -> str:
    """Collapses whitespace and control chars to single spaces, stripped and capped at 40 chars."""
    text = re.sub(r"[\s\x00-\x1f\x7f-\x9f]+", " ", str(value or "")).strip()
    return text[:MAX_SPEAKER_NAME].strip()


def clean_speaker_names(raw) -> dict[str, str]:
    """Cleaned {speaker_code: name} mapping; drops empty keys and values."""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        code = normalize_speaker(str(key or ""))
        name = clean_speaker_name(value)
        if code and name:
            cleaned[code] = name
    return cleaned


def speaker_label(speaker: str, names: dict | None = None, translate: bool = True) -> str:
    """Localized (or custom) label for a speaker code; unknown values are shown as they are."""
    if isinstance(names, dict):
        custom = clean_speaker_name(names.get(speaker))
        if custom:
            return custom
    base, n = split_speaker(speaker)
    if n is not None:
        if base == SPEAKER_MEETING:
            return t("Speaker {n}", n=n) if translate else f"Speaker {n}"
        if base == SPEAKER_ME:
            return t("Microphone {n}", n=n) if translate else f"Microphone {n}"
    label = SPEAKER_LABELS.get(speaker)
    if label:
        return t(label) if translate else label
    return speaker


def is_hallucination(text: str) -> bool:
    clean_text = text.strip()
    if not clean_text:
        return True
    return any(pattern.search(clean_text) for pattern in _HALLUCINATION_RES)


def strip_outro(text: str) -> str:
    """Cuts subtitle closing lines ("Dziękuję za uwagę.") from the end of a text.

    `is_hallucination` catches them only as a whole utterance. A dictation is
    one text for the whole recording, so "Spotkanie jutro o dziesiątej.
    Dziękuję za uwagę." went through with the invented ending. Only `_OUTROS`
    are cut here: a final "Dziękuję." or "Tak." can be what the user said.
    """
    text = text.rstrip()
    while True:
        breaks = [m.end() for m in _SENTENCE_BREAK.finditer(text)]
        if not breaks or not any(p.search(text[breaks[-1]:]) for p in _OUTRO_RES):
            return text
        text = text[: breaks[-1]].rstrip()


def clean(segments: list[Segment]) -> list[Segment]:
    """Drops empty segments, known hallucinations and direct repetitions."""
    result: list[Segment] = []
    for seg in segments:
        text = strip_outro(" ".join(seg.text.split()))
        if not text or is_hallucination(text):
            continue
        if seg.end - seg.start < _MIN_DURATION:
            continue
        if result and result[-1].speaker == seg.speaker and result[-1].text == text:
            # Whisper can loop on the same sentence; we keep it once.
            result[-1].end = seg.end
            continue
        result.append(Segment(seg.start, seg.end, text, seg.speaker))
    return result


def merge_tracks(*tracks: list[Segment]) -> list[Segment]:
    """Joins the segments of all tracks into one chronological conversation."""
    everything: list[Segment] = []
    for track in tracks:
        everything.extend(clean(track))
    everything.sort(key=lambda s: (s.start, s.speaker))

    joined: list[Segment] = []
    for seg in everything:
        previous = joined[-1] if joined else None
        if (
            previous is not None
            and previous.speaker == seg.speaker
            and seg.start - previous.end <= _MAX_JOIN_GAP
        ):
            previous.text = f"{previous.text} {seg.text}".strip()
            previous.end = max(previous.end, seg.end)
        else:
            joined.append(Segment(seg.start, seg.end, seg.text, seg.speaker))
    return joined


def slug(text: str, max_len: int = 48) -> str:
    """Any text -> a safe piece of a folder name.

    Underscores also become hyphens: the session folder name is split into
    parts exactly at underscores (date_time_topic_names).
    """
    # "ł" does not decompose in NFKD and used to vanish ("zespół" -> "zespo").
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-")


def format_hms(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def as_markdown(segments: list[Segment], meta: dict) -> str:
    title = meta.get("topic") or meta.get("name") or t("Meeting")
    lines = [
        "# " + t("Transcript: {title}", title=title),
        "",
        f"- **{t('Date')}:** {meta.get('date', '')}",
        f"- **{t('Duration')}:** {format_hms(meta.get('duration_s', 0))}",
        f"- **Model:** `{meta.get('model', '')}`",
        f"- **{t('Compute')}:** {meta.get('device', '')}",
        f"- **{t('Language')}:** {meta.get('language', 'auto')}",
    ]
    if meta.get("microphone_device"):
        lines.append(f"- **{t('Microphone')}:** {meta['microphone_device']}")
    if meta.get("system_device"):
        lines.append(f"- **{t('System audio')}:** {meta['system_device']}")
    if meta.get("participants"):
        lines.append(f"- **{t('Participants')}:** {', '.join(meta['participants'])}")
    lines += ["", "---", ""]
    if meta.get(TRANSCRIPT_PARTIAL):
        lines += ["_" + t("Recognized while recording; the transcript is not finished yet.") + "_", ""]

    # The summary goes on top so nobody has to read everything; the full
    # version with decisions is in summary.md.
    if meta.get("tldr"):
        lines += ["## " + t("In short"), "", meta["tldr"], ""]
        if meta.get("key_points"):
            lines += [f"- {point}" for point in meta["key_points"]] + [""]
        lines += ["---", ""]

    if not segments:
        lines.append("_" + t("No speech was recognized.") + "_")
        return "\n".join(lines) + "\n"

    names = clean_speaker_names(meta.get("speaker_names"))
    for seg in segments:
        lines.append(f"**[{format_hms(seg.start)}] {speaker_label(seg.speaker, names)}:** {seg.text}")
        lines.append("")
    return "\n".join(lines)


def as_srt(segments: list[Segment], names: dict | None = None) -> str:
    cleaned = clean_speaker_names(names)
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        end = max(seg.end, seg.start + 0.5)
        lines.append(str(i))
        lines.append(f"{_srt_time(seg.start)} --> {_srt_time(end)}")
        lines.append(f"{speaker_label(seg.speaker, cleaned)}: {seg.text}")
        lines.append("")
    return "\n".join(lines)


def as_text(segments: list[Segment],
            label: Callable[[str], str] = speaker_label) -> str:
    """Plain text, one utterance per line. `label` renders the speaker code."""
    return "\n".join(f"{label(seg.speaker)}: {seg.text}" for seg in segments) + "\n"


# meta.json keys written by meeting.py after a transcription run. Only a run
# that was neither cancelled nor had failed chunks sets TRANSCRIPT_COMPLETE
# to true; audio retention relies on it (retention.transcript_complete).
TRANSCRIPT_COMPLETE = "transcript_complete"
TRANSCRIPTION_CANCELLED = "transcription_cancelled"
TRANSCRIPTION_ERRORS = "transcription_errors"
# Written while a meeting records with live transcription (ucho/live_meeting.py):
# the text recognized so far, kept if the app is closed or crashes. The
# transcription after the stop replaces it and drops this key.
TRANSCRIPT_PARTIAL = "transcript_partial"


def save_results(folder: Path, segments: list[Segment], meta: dict) -> dict[str, Path]:
    """Writes the transcript in three formats plus the session metadata."""
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "md": folder / TRANSCRIPT_MD,
        "srt": folder / TRANSCRIPT_SRT,
        "txt": folder / TRANSCRIPT_TXT,
        "meta": folder / META_FILE,
    }
    names = clean_speaker_names(meta.get("speaker_names"))
    files["md"].write_text(as_markdown(segments, meta), encoding="utf-8")
    files["srt"].write_text(as_srt(segments, names), encoding="utf-8")
    files["txt"].write_text(
        as_text(segments, label=lambda code: speaker_label(code, names)),
        encoding="utf-8",
    )

    full_meta = dict(meta)
    if "speaker_names" in full_meta:
        if names:
            full_meta["speaker_names"] = names
        else:
            full_meta.pop("speaker_names", None)
    full_meta["saved"] = datetime.now().isoformat(timespec="seconds")
    full_meta["segment_count"] = len(segments)
    full_meta["segments"] = [asdict(s) for s in segments]
    files["meta"].write_text(
        json.dumps(full_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return files
