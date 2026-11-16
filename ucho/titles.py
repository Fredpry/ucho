# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Short automatic titles for finished meeting transcripts."""

from __future__ import annotations

import logging
import re
import time
import unicodedata

from . import summary
from .merge import Segment, as_text

log = logging.getLogger(__name__)

# The beginning and a sample from the middle: about 1,000 prompt tokens for
# Polish whatever the meeting length. The CPU time grows with the prompt:
# 1,450 tokens took 40-47 s with 4B on the CPU.
HEAD_CHARS = 1800
MIDDLE_CHARS = 900
MAX_TOKENS = 40
MIN_WORDS = 2
MAX_WORDS = 8
MAX_TITLE_CHARS = 60
GAP = "[...]"

# The names a meeting gets when the user typed none ("Meeting" and its Polish
# translation).
DEFAULT_NAMES = frozenset({"meeting", "spotkanie"})

GENERIC = frozenset({
    "meeting",
    "spotkanie",
    "rozmowa",
    "call",
    "discussion",
    "dyskusja",
    "transcript",
    "transkrypt",
    "title",
    "tytuł",
    "brak",
    "none",
    "untitled",
    "bez tytułu",
    "meeting title",
    "tytuł spotkania",
})

TITLE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}

# Example titles in the prompt were copied by the models into unrelated meetings
# ("Odbiór ..."), and a list of meeting kinds made them start every title with
# "Planowanie"/"Negocjacje", so the prompt has neither.
SYSTEM = {
    "pl": "Nadajesz krótkie, rzeczowe tytuły nagraniom spotkań. Odpowiadasz samym JSON-em.",
    "en": "You give short, factual titles to meeting recordings. You answer with JSON only.",
}

PARTS = {
    "pl": ("początek", "początek i środek"),
    "en": ("the beginning", "the beginning and the middle"),
}

PROMPT = {
    "pl": (
        "Poniżej fragmenty automatycznego transkryptu spotkania: {parts}. "
        'Etykiety "Ja:" i "Spotkanie:" oznaczają mikrofon i głośniki, a nie imiona.\n\n'
        'Zwróć JSON {{"title": "..."}}: tytuł spotkania po polsku, od 3 do 7 słów, '
        "mówiący, czego dotyczyło spotkanie. Bez daty, bez imion, bez cudzysłowów. "
        "Pomiń powitania, small talk i problemy z połączeniem.\n\n{material}"
    ),
    "en": (
        "Below are excerpts of an automatic meeting transcript: {parts}. "
        'The labels "Ja:" and "Spotkanie:" mean microphone and speakers, not names.\n\n'
        'Return JSON {{"title": "..."}}: a meeting title in English, 3 to 7 words, '
        "saying what the meeting was about. No date, no names, no quotes. "
        "Ignore greetings, small talk and connection problems.\n\n{material}"
    ),
}

PL_WORDS = frozenset({
    "się", "nie", "jest", "że", "na", "to", "w", "i", "z",
    "do", "jak", "tak", "ale", "co", "mamy", "czy", "jestem", "będzie",
})
EN_WORDS = frozenset({
    "the", "and", "is", "to", "we", "you", "it", "of",
    "that", "a", "in", "for", "this", "so", "are", "be",
})

_STRIP_CHARS = " \"'„”“«»*#.,:;-–—"
_PREFIX_RE = re.compile(r"^(?:title|tytu[łl]):\s*", re.IGNORECASE)
_EN_FILLER_RE = re.compile(
    r"^(?:meeting|discussion|call)\s+(?:on|about|regarding)\s+", re.IGNORECASE
)
_WORD_RE = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)


def meeting_language(text: str, hint: str | None = None) -> str:
    """Returns 'pl' or 'en' for the meeting transcript."""
    if hint in ("pl", "en"):
        return hint
    words = re.findall(r"\w+", text.lower())
    pl_count = sum(1 for word in words if word in PL_WORDS)
    en_count = sum(1 for word in words if word in EN_WORDS)
    return "en" if en_count > pl_count else "pl"


def excerpt(text: str) -> tuple[str, bool]:
    """Bounded transcript excerpt (beginning and optionally middle) + has_middle."""
    text = text.strip()
    if len(text) <= HEAD_CHARS + MIDDLE_CHARS:
        return (text, False)

    lines = text.splitlines()
    head: list[str] = []
    head_len = 0
    for line in lines:
        step = len(line) + 1
        if head and head_len + step > HEAD_CHARS:
            break
        if not head and len(line) > HEAD_CHARS:
            head.append(line[:HEAD_CHARS])
            break
        head.append(line)
        head_len += step

    h = len(head)
    mid = len(lines) // 2
    if mid < h:
        return ("\n".join(head), False)

    b = mid
    back_len = 0
    mid_line = lines[mid]
    mid_fits = len(mid_line) <= MIDDLE_CHARS
    mid_cost = len(mid_line) + 1
    while b > h and back_len < MIDDLE_CHARS // 2:
        step = len(lines[b - 1]) + 1
        # Keep enough room so lines[mid] is included whenever it fits on its own.
        if mid_fits and back_len + step + mid_cost > MIDDLE_CHARS:
            break
        back_len += step
        b -= 1

    middle: list[str] = []
    middle_len = 0
    for line in lines[b:]:
        step = len(line) + 1
        if middle and middle_len + step > MIDDLE_CHARS:
            break
        if not middle and len(line) > MIDDLE_CHARS:
            middle.append(line[:MIDDLE_CHARS])
            break
        middle.append(line)
        middle_len += step

    if not middle:
        return ("\n".join(head), False)

    return ("\n".join(head) + "\n" + GAP + "\n" + "\n".join(middle), True)


def _strip_edges(text: str) -> str:
    """Collapses whitespace and strips punctuation/quotes until stable."""
    while True:
        cleaned = " ".join(text.split()).strip(_STRIP_CHARS)
        if cleaned == text:
            return cleaned
        text = cleaned


def _no_diacritics(text: str) -> str:
    text = text.lower().replace("ł", "l")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def clean_title(raw, transcript: str) -> str:
    """Tidies a model-generated title; returns '' when unusable."""
    if not isinstance(raw, str):
        return ""

    title = _strip_edges(raw)
    title = _strip_edges(_PREFIX_RE.sub("", title))
    title = _strip_edges(_EN_FILLER_RE.sub("", title))
    title = _strip_edges(
        summary._without_invented_dates(title, summary._exact_words(transcript), transcript)
    )

    title_numbers = set(re.findall(r"\d+", title))
    if title_numbers and not (title_numbers <= set(re.findall(r"\d+", transcript))):
        return ""

    title_words = _WORD_RE.findall(title)
    # English titles come in Title Case ("Xero Invoice Sync Issue Resolution"),
    # where capitals do not mark names, so those are not checked. Polish titles
    # are in sentence case: a capital inside one is a name.
    longer = [w for w in title_words[1:] if len(w) >= 4]
    title_case = (len(longer) >= 2 and all(w[:1].isupper() for w in longer)
                  and meeting_language(transcript) == "en")
    if not title_case and any(w[:1].isupper() for w in title_words[1:]):
        exact_words = {m.group(0).lower() for m in _WORD_RE.finditer(transcript)}
        ascii_words = {_no_diacritics(w) for w in exact_words}
        stem_words = summary._words(transcript)
        for word in title_words[1:]:
            if not word[:1].isupper():
                continue
            if (
                word.lower() not in exact_words
                and _no_diacritics(word) not in ascii_words
                and not summary._name_in_text(word, stem_words)
            ):
                return ""

    if not (MIN_WORDS <= len(title.split()) <= MAX_WORDS) or len(title) > MAX_TITLE_CHARS:
        return ""

    folded = title.casefold()
    if folded in GENERIC or folded == "ja":
        return ""

    return title[:1].upper() + title[1:]


def wants_title(meta: dict, folder_name: str = "") -> bool:
    """Whether a session may receive an automatic title."""
    topic = meta.get("topic")
    if isinstance(topic, str) and topic.strip():
        return False

    source = meta.get("name_source")
    if source == "user":
        return False
    if source == "auto":
        return True

    name = str(meta.get("name") or "").strip()
    return name == "" or name == folder_name or name.casefold() in DEFAULT_NAMES


def transcript_text(segments: list[Segment]) -> str:
    """Renders segments into the prompt text format."""
    return as_text(segments, label=summary._prompt_label).strip() if segments else ""


def generate_title(
    segments: list[Segment],
    model: str,
    device: str = "GPU",
    cancel=None,
    language: str | None = None,
    engine_factory=None,
) -> dict:
    """Generates a short meeting title using a local LLM; returns {} when unusable."""
    text = transcript_text(segments)
    if not text:
        return {}

    lang = meeting_language(text, language)
    material, has_middle = excerpt(text)
    parts = PARTS[lang][1 if has_middle else 0]
    user = PROMPT[lang].format(parts=parts, material=material)

    factory = engine_factory or summary.LLMEngine
    engine = factory(model, device, system=SYSTEM[lang])
    start = time.monotonic()
    try:
        engine.load()
        data = engine.generate_json(user, TITLE_SCHEMA, MAX_TOKENS, cancel)
        elapsed = time.monotonic() - start
    finally:
        engine.release()

    if cancel is not None and cancel.is_set():
        return {}

    title = clean_title(data.get("title") if isinstance(data, dict) else None, text)
    if not title:
        log.warning("Title generation produced no usable output (lang=%s, %.1fs)", lang, elapsed)
        return {}

    log.info("Title ready in %.1fs (%d words, lang=%s)", elapsed, len(title.split()), lang)
    return {
        "name": title,
        "title_model": model,
        "title_device": getattr(engine, "device", device),
        "title_time_s": round(elapsed, 1),
    }
