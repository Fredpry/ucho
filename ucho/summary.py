# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Local transcript summary with a small language model.

Same runtime as Whisper - OpenVINO GenAI on the same integrated GPU - so there
is no Ollama, HTTP server or background process here. The model loads only
after transcription has finished and is released right after the summary is
generated (`LLMEngine.release`), so ~2 GB of memory is not held for the whole
life of the app. No network: the model is read from `models\\hub` or from the
user's own model folder/file (`llm.location`), never downloaded here.

The model returns JSON constrained by a schema (`StructuredOutputConfig`), not
free text to parse - so the folder name and the summary do not depend on
whether a small model happened to get the format right.

The prompts are Polish because the summaries are Polish; the JSON field names
the prompts refer to (temat, uczestnicy, punkty, ustalenia) stay Polish too and
are mapped to English keys in `_normalize`.
"""

from __future__ import annotations

import difflib
import gc
import json
import logging
import re
import threading
import time
import unicodedata
from typing import Callable

from . import config, llm, session
from .i18n import t
from .merge import (SPEAKER_ME, SPEAKER_MEETING, Segment, as_text, clean_speaker_name,
                    clean_speaker_names, format_hms, slug, split_speaker)

log = logging.getLogger(__name__)

# How many transcript characters go into one model pass. A one-hour meeting is
# about 50-80 kB of text, far more than is worth pushing into a 4B model on an
# iGPU at once - which is why longer transcripts go through map-reduce.
#
# The threshold is chosen so a typical meeting (up to ~40 min, about 24 kB)
# fits in ONE pass. Map-reduce loses decisions negotiated over the whole
# conversation: in a test the model took the first proposed date ("wtorek 29")
# and did not see that half an hour later it was moved to Wednesday. A summary
# of summaries has nothing to rebuild that from - the Tuesday part and the
# Wednesday part are separate. 24 kB is about 8k tokens, still well within
# Qwen's context window.
MAX_CHUNK_CHARS = 24_000
MAX_CHUNKS = 8

# A chunk now returns three lists instead of two (decisions were added), so 320
# tokens could cut the answer off in the middle of the last decision.
MAX_TOKENS_CHUNK = 560
MAX_TOKENS_FINAL = 700

MAX_PARTICIPANTS = 6
# Similarity threshold between a name and a transcript word - see `_name_in_text`.
NAME_SIMILARITY = 0.8
# Above this two points count as the same sentence - see `_sentences`.
SENTENCE_SIMILARITY = 0.75
MAX_POINTS = 5
MAX_DECISIONS = 6
MAX_TOPIC_CHARS = 60

# Polish speaker labels used in the text sent to the model. The Polish prompt
# (`_LABEL_NOTE`) refers to exactly these, whatever the UI language is.
_PROMPT_LABELS = {SPEAKER_ME: "Ja", SPEAKER_MEETING: "Spotkanie"}

# Audio track labels and Polish role words - the model likes to take them for
# names, so they are filtered out hard. Polish data, matched against Polish output.
NOT_NAMES = {
    "ja", "spotkanie", "mikrofon", "uczestnik", "uczestnicy", "rozmowca", "rozmowcy",
    "mowca", "prowadzacy", "nieznany", "brak", "wszyscy", "gosc", "user",
    # The same words with Polish letters: "Mówca N" is the label of a detected voice.
    "mówca", "mówcy", "rozmówca", "rozmówcy", "prowadzący", "gość",
    # Functions and roles - the model gives them instead of a name when no name was said.
    "dyrektor", "dyrektorka", "prezes", "kierownik", "kierowniczka", "szef",
    "szefowa", "klient", "klientka", "zespol", "zespół", "zarzad", "zarząd",
    "administrator", "moderator", "asystent", "asystentka",
}

# Without this the same person lands on the list twice: once as "Pan Marek",
# once as "Marek". Polish honorifics.
_HONORIFICS = re.compile(
    r"^(pan|pani|panie|pana|panu|panią|pania|dr|prof|inż|inz|mgr)\.?\s+",
    re.IGNORECASE,
)

StatusFn = Callable[[str], None]


# JSON schemas for the model. Field names are Polish because the Polish prompts
# below name them; `_normalize` maps them to English result keys.
CHUNK_SCHEMA = {
    "type": "object",
    "properties": {
        "punkty": {"type": "array", "items": {"type": "string"}},
        "ustalenia": {"type": "array", "items": {"type": "string"}},
        "uczestnicy": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["punkty", "ustalenia", "uczestnicy"],
    "additionalProperties": False,
}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "temat": {"type": "string"},
        "uczestnicy": {"type": "array", "items": {"type": "string"}},
        "tldr": {"type": "string"},
        "punkty": {"type": "array", "items": {"type": "string"}},
        "ustalenia": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["temat", "uczestnicy", "tldr", "punkty", "ustalenia"],
    "additionalProperties": False,
}

SYSTEM = (
    "Jesteś asystentem, który robi notatki ze spotkań biznesowych po polsku. "
    "Czytasz automatyczny transkrypt rozmowy i wyciągasz z niego to, do czego "
    "ktoś wróci za tydzień: co ustalono, kto co robi i na kiedy.\n"
    "Zasady, których trzymasz się bezwzględnie:\n"
    "1. KONKRETY SĄ NAJWAŻNIEJSZE. Daty, dni tygodnia, godziny, liczby, kwoty, "
    "nazwy firm, systemów i stanowisk przepisujesz dokładnie tak, jak padły. "
    "Nigdy nie zamieniasz ich na ogólniki typu „ustalono termin”. Ale też niczego "
    "do nich nie dokładasz: jeśli padł sam dzień tygodnia i numer dnia, piszesz "
    "tylko tyle. Nie dopisujesz miesiąca ani roku, których nikt nie wymówił.\n"
    "2. Opierasz się wyłącznie na transkrypcie — niczego nie zmyślasz i nie "
    "dopowiadasz. Czego nie ma w tekście, tego nie ma w notatce.\n"
    "3. Pomijasz small talk sprzed i po spotkaniu: testy mikrofonu, „czy mnie "
    "słychać”, problemy z kamerą, pogodę, dygresje prywatne.\n"
    "4. Transkrypt jest automatyczny, więc bywa przekręcony. Fragmenty, których "
    "nie rozumiesz, po prostu pomijasz — nie zgadujesz, co mogły znaczyć.\n"
    "5. Ustalenia bywają negocjowane: pada jeden termin, potem drugi. Liczy się "
    "WERSJA OSTATECZNA, ta z końca rozmowy — nie pierwsza propozycja.\n"
    "Odpowiadasz zawsze samym poprawnym JSON-em, bez komentarza."
)

_LABEL_NOTE = (
    'Etykiety "Ja:" i "Spotkanie:" oznaczają źródło dźwięku (mikrofon i głośniki), '
    'a "Mówca N:" i "Mikrofon N:" rozpoznany głos — to NIE są imiona i nigdy nie '
    "traktuj ich jako uczestników."
)


def _label_note(known: list[str] | tuple[str, ...] = ()) -> str:
    if not known:
        return _LABEL_NOTE
    names = ", ".join(known)
    return (
        f"{_LABEL_NOTE} Imiona przed dwukropkiem nadał użytkownik ({names}): "
        "to są uczestnicy spotkania."
    )


CHUNK_PROMPT = """Fragment {number} z {total} transkryptu spotkania. {note}

Zwróć JSON o polach:
- "punkty": do 3 najważniejszych rzeczy merytorycznych z tego fragmentu, każda jako jedno krótkie zdanie po polsku. Pomiń small talk i sprawy techniczne połączenia.
- "ustalenia": wszystko, co w tym fragmencie zostało UZGODNIONE lub komuś zlecone — spotkania i terminy (dzień tygodnia, data, godzina, czas trwania), zadania (kto i co robi), zobowiązania („podeślemy”, „przygotujemy”, „odezwę się”). Przepisz daty, godziny i liczby dokładnie tak, jak padły. Jeśli w tym fragmencie nic nie ustalono — pusta lista.
- "uczestnicy": imiona, które PADŁY w tym fragmencie (powitanie, zwrot do kogoś, przedstawienie się). Jeśli żadne imię nie padło — pusta lista. Nie zgaduj.

Fragment:
---
{material}
---"""

SUMMARY_PROMPT = """Poniżej {source} spotkania. {note}

Zwróć JSON o polach:
- "temat": 2-5 słów nazywających to spotkanie, po polsku, bez daty. Ma opisywać po co się spotkano, a nie firmę wspomnianą po drodze.
- "uczestnicy": imiona osób, które PADŁY w rozmowie. Jeśli żadne imię nie padło albo nie masz pewności — pusta lista. Nie zgaduj i nie wymyślaj.
- "tldr": 2-3 zdania streszczenia całości. Pierwsze zdanie mówi, po co było to spotkanie, ostatnie — czym się skończyło.
- "punkty": do 5 najważniejszych tematów, każdy jako jedno krótkie zdanie. Każdy o czymś INNYM — nie powtarzaj tej samej myśli w kilku punktach. Bez small talku i spraw technicznych połączenia.
- "ustalenia": konkretne decyzje, terminy i zadania (kto, co, na kiedy). Zachowaj daty, dni tygodnia, godziny i liczby dokładnie tak, jak padły — to najważniejsza część notatki. Jeśli nic nie ustalono — pusta lista.

{material}"""


# --- model ----------------------------------------------------------------


class LLMEngine:
    """Lazily built LLMPipeline with automatic fallback to the CPU.

    Deliberately mirrors `transcribe.Engine` - the same order of attempts
    (requested device, then CPU; with the compile cache, then without).
    """

    def __init__(self, repo_id: str, device: str = "GPU", system: str | None = None):
        self.repo_id = repo_id
        self.requested_device = device
        self.device = device
        self.system = system or SYSTEM
        self._pipe = None

    def load(self) -> None:
        if self._pipe is not None:
            return

        import openvino_genai as ov_genai

        folder = str(llm.location(self.repo_id))
        cache = str(config.OV_CACHE_DIR)
        config.OV_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        candidates = [self.requested_device]
        if self.requested_device != "CPU":
            candidates.append("CPU")

        last_error: Exception | None = None
        for device in candidates:
            for kwargs in ({"CACHE_DIR": cache}, {}):
                try:
                    start = time.monotonic()
                    self._pipe = ov_genai.LLMPipeline(folder, device, **kwargs)
                    self.device = device
                    log.info(
                        "LLM ready on %s in %.1fs (%s, cache=%s)",
                        device, time.monotonic() - start, llm.label(self.repo_id), bool(kwargs),
                    )
                    return
                except Exception as error:
                    last_error = error
                    log.warning("Could not build the LLM on %s: %s", device, error)

        raise RuntimeError(f"Could not load the language model: {last_error}")

    def release(self) -> None:
        """Frees the memory held by the model - this is the 'off after use'."""
        if self._pipe is None:
            return
        self._pipe = None
        gc.collect()
        log.info("LLM pipeline released")

    def _prompt(self, user: str) -> str:
        """Builds the prompt in the format the model expects.

        Qwen3 turns on reasoning mode by default and wastes hundreds of tokens on
        a <think> block. We turn it off through `extra_context`, and if this
        template version does not know it - we build ChatML by hand with an
        empty <think> block, exactly as the Qwen3 template does with
        enable_thinking=False.
        """
        history = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": user},
        ]
        tokenizer = self._pipe.get_tokenizer()
        for kwargs in ({"extra_context": {"enable_thinking": False}}, {}):
            try:
                prompt = tokenizer.apply_chat_template(history, True, **kwargs)
                if prompt:
                    return prompt
            except Exception as error:
                log.debug("apply_chat_template(%s) failed: %s", kwargs, error)

        prompt = (
            f"<|im_start|>system\n{self.system}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        if "qwen3" in self.repo_id.lower():
            prompt += "<think>\n\n</think>\n\n"
        return prompt

    def generate_json(self, user: str, schema: dict, max_tokens: int,
                      cancel: threading.Event | None = None) -> dict:
        """One model pass with an enforced JSON schema.

        `cancel` is checked through the streamer, i.e. after every token -
        otherwise the "Stop" button would do nothing for many seconds of generation.
        """
        import openvino_genai as ov_genai

        prompt = self._prompt(user)

        streamer = None
        if cancel is not None:
            def streamer(_piece: str):
                return (ov_genai.StreamingStatus.CANCEL if cancel.is_set()
                        else ov_genai.StreamingStatus.RUNNING)

        for enforce in (True, False):
            gen_config = ov_genai.GenerationConfig()
            gen_config.max_new_tokens = max_tokens
            gen_config.do_sample = False
            if enforce:
                try:
                    gen_config.structured_output_config = ov_genai.StructuredOutputConfig(
                        json_schema=json.dumps(schema)
                    )
                except Exception as error:
                    log.warning("Enforced JSON unavailable: %s", error)
                    continue
            try:
                result = self._pipe.generate(prompt, gen_config, streamer)
            except Exception as error:
                # Grammar-constrained decoding can fail to start on the GPU -
                # then the second iteration uses plain decoding.
                log.warning("Generation (enforced JSON=%s) failed: %s", enforce, error)
                continue

            if cancel is not None and cancel.is_set():
                return {}

            data = _load_json(_without_reasoning(str(result)))
            if data is not None:
                return data
            log.warning("The model did not return valid JSON (enforced=%s)", enforce)

        return {}


# --- text -----------------------------------------------------------------


def _without_reasoning(text: str) -> str:
    """Removes <think>...</think> blocks, also unclosed ones."""
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.replace("<think>", " ").strip()


def _load_json(text: str) -> dict | None:
    """Parses the model answer; with extra text around it cuts out the first object."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _chunks(text: str) -> list[str]:
    """Cuts the transcript into pieces at whole utterances.

    For very long meetings we take an even sample of the pieces - better to
    summarize eight pieces from the whole meeting than to grind through
    everything and make the user wait many minutes.
    """
    pieces: list[str] = []
    current: list[str] = []
    length = 0

    for line in text.splitlines():
        if current and length + len(line) > MAX_CHUNK_CHARS:
            pieces.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1

    if current:
        pieces.append("\n".join(current))
    if not pieces:
        return []
    if len(pieces) <= MAX_CHUNKS:
        return pieces

    step = len(pieces) / MAX_CHUNKS
    return [pieces[min(int(i * step), len(pieces) - 1)] for i in range(MAX_CHUNKS)]


# --- cleaning up the answer -----------------------------------------------


def _sentences(value, limit: int) -> list[str]:
    """Tidies the model's list of sentences, dropping repetitions.

    Not only identical ones: a small model can fill the list with five variants
    of the same sentence ("Firma chce zarejestrować wizytę, by zaprezentować
    rozwiązanie" / "...by omówić zastosowanie"). Better to return three
    different points than one point five times.
    """
    result: list[str] = []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return result
    for item in value:
        text = " ".join(str(item).split()).strip(" -•*")
        if not text:
            continue
        if any(difflib.SequenceMatcher(None, text.lower(), seen.lower()).ratio()
               >= SENTENCE_SIMILARITY for seen in result):
            continue
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _simplified(text: str) -> str:
    """Lower case without diacritics - for comparing names with the transcript."""
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


def _words(text: str) -> set[str]:
    """Set of transcript words in a form comparable with names."""
    return set(re.findall(r"[a-z]+", _simplified(text)))


def _exact_words(text: str) -> set[str]:
    """Like `_words`, but with Polish diacritics kept.

    Month names need the diacritics: without them "mają" merges with "maja",
    so every meeting where someone "has" something would confirm May.
    """
    return set(re.findall(r"[a-ząćęłńóśźż]+", text.lower()))


# The model likes to add a month where only the day number was said - in a test
# it turned "wtorek 29" into "29 lutego" and then "29 marca". No instruction in
# the prompt stopped it, so we cut them afterwards: a month stays only when it
# was really said in the conversation. Polish month forms (nominative, genitive,
# locative) - data matched against Polish summaries.
MONTHS = {
    "styczeń": ("styczeń", "stycznia", "styczniu"),
    "luty": ("luty", "lutego", "lutym"),
    "marzec": ("marzec", "marca", "marcu"),
    "kwiecień": ("kwiecień", "kwietnia", "kwietniu"),
    "maj": ("maj", "maja", "maju"),
    "czerwiec": ("czerwiec", "czerwca", "czerwcu"),
    "lipiec": ("lipiec", "lipca", "lipcu"),
    "sierpień": ("sierpień", "sierpnia", "sierpniu"),
    "wrzesień": ("wrzesień", "września", "wrześniu"),
    "październik": ("październik", "października", "październiku"),
    "listopad": ("listopad", "listopada", "listopadzie"),
    "grudzień": ("grudzień", "grudnia", "grudniu"),
}


# Numeric date with a month: "29.02", "29.02.2026", "29/02". The second part
# must be a valid month (01-12) so a date is not confused with a time - "12.00"
# has "00" there and "13.30" has "30", so both stay untouched.
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./](0?[1-9]|1[0-2])(?:[./](\d{2,4}))?\b")


def _without_invented_dates(sentence: str, words: set[str], transcript: str) -> str:
    """Removes date parts from the sentence that were not said in the transcript.

    Month names disappear entirely, numeric dates keep only the day. No prompt
    solved this: 4B turned "wtorek 29" first into "29 lutego", then "29 marca",
    and 8B into "29.02". Better an incomplete date than an invented one.
    """
    present = set(re.findall(r"[a-ząćęłńóśźż]+", sentence.lower()))
    for forms in MONTHS.values():
        if not (present & set(forms)) or (words & set(forms)):
            continue
        pattern = re.compile(r"\s*\b(?:%s)\b" % "|".join(forms), re.IGNORECASE)
        sentence = pattern.sub("", sentence)
        log.info("Removing invented month %r", forms[0])

    def day_only(match: re.Match) -> str:
        if match.group(0) in transcript:
            return match.group(0)
        log.info("Invented numeric date %r - keeping the day only", match.group(0))
        return match.group(1)

    sentence = _NUMERIC_DATE.sub(day_only, sentence)
    return " ".join(sentence.split())


def _name_in_text(name: str, words: set[str]) -> bool:
    """Whether the name is really said in the transcript (tolerating inflection).

    The model can add a person who was not there - in a test it invented
    "Teresę" from a garbled "Teros?". So we check that the transcript contains
    a word similar enough to the name.

    We compare WHOLE words, not substrings: "Teresa" hides inside
    "zainteresowani", and "Robert" in "robimy". And with tolerance for
    inflection, because Polish names also have a mobile e - "Paweł" occurs in
    the recording only as "Pawle" and "Pawła", never in the nominative.
    Threshold 0.8 measured on a meeting transcript: 6/6 present names found,
    0/8 absent names let through.
    """
    stem = _simplified(name).split()[0]
    if len(stem) < 3:
        return False
    candidates = [word for word in words if word[:3] == stem[:3]]
    return bool(difflib.get_close_matches(stem, candidates, n=1,
                                          cutoff=NAME_SIMILARITY))


def known_names(segments: list[Segment], speaker_names: dict | None) -> list[str]:
    """Distinct non-empty cleaned names of speaker codes occurring in `segments`, in first-appearance order."""
    if not segments or not isinstance(speaker_names, dict):
        return []
    cleaned = clean_speaker_names(speaker_names)
    if not cleaned:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for code in session.speaker_codes(segments):
        name = cleaned.get(code)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def _known_keys(known) -> set[str]:
    keys: set[str] = set()
    for item in known or ():
        clean = " ".join(str(item).split()).strip()
        if clean:
            lower = clean.lower()
            keys.add(lower)
            keys.add(lower.split()[0])
    return keys


def _names(value, transcript: str | None = None, known=()) -> list[str]:
    """Keeps only what looks like a name - the rest of the model output is dropped.

    With `transcript`, names are also checked for whether they were said in it
    at all. Names already covered by `known` are skipped here and added by
    `_participants`.
    """
    result: list[str] = []
    seen: set[str] = set()
    if not isinstance(value, (list, tuple)):
        return result

    comparable = _words(transcript) if transcript else None
    covered = _known_keys(known)

    for item in value:
        name = " ".join(str(item).split()).strip(" -,.")
        name = _HONORIFICS.sub("", name).strip()
        if not (2 <= len(name) <= 24):
            continue
        if not re.fullmatch(r"[^\W\d_][\w'’-]*(?: [^\W\d_][\w'’-]*)?", name, re.UNICODE):
            continue
        # The first word too, otherwise "Kierownik produkcji" or "Dyrektor
        # finansowy" slip past the single-word filter.
        if name.lower() in seen or name.lower() in covered:
            continue
        if {name.lower(), name.lower().split()[0]} & NOT_NAMES:
            continue
        if comparable is not None and not _name_in_text(name, comparable):
            log.info("Rejecting name %r - not said in the transcript", name)
            continue
        seen.add(name.lower())
        result.append(name[:1].upper() + name[1:])
        if len(result) >= MAX_PARTICIPANTS:
            break
    return result


def _participants(known: list[str], found: list[str]) -> list[str]:
    """User-given `known` names first (verbatim, never dropped), then uncovered `found` names up to MAX_PARTICIPANTS."""
    result = list(known or [])
    covered = _known_keys(result)
    for name in found or []:
        if len(result) >= MAX_PARTICIPANTS:
            break
        cleaned = _HONORIFICS.sub("", " ".join(str(name).split()).strip(" -,.")).strip()
        lower = cleaned.lower()
        if not lower or lower in covered:
            continue
        covered.add(lower)
        result.append(name)
    return result


def _normalize(data: dict, transcript: str | None = None, known=()) -> dict:
    """Model answer (Polish schema fields) -> cleaned result with English keys."""
    topic = " ".join(str(data.get("temat", "")).split()).strip(" .-\"'")
    tldr = " ".join(str(data.get("tldr", "")).split())
    key_points = _sentences(data.get("punkty"), MAX_POINTS)
    decisions = _sentences(data.get("ustalenia"), MAX_DECISIONS)

    if transcript:
        words = _exact_words(transcript)
        tldr = _without_invented_dates(tldr, words, transcript)
        key_points = [_without_invented_dates(p, words, transcript) for p in key_points]
        decisions = [_without_invented_dates(d, words, transcript) for d in decisions]

    known_list = list(known or [])
    return {
        "topic": topic[:MAX_TOPIC_CHARS],
        "participants": _participants(known_list, _names(data.get("uczestnicy"), transcript, known_list)),
        "tldr": tldr,
        "key_points": key_points,
        "decisions": decisions,
    }


def _prompt_label(speaker: str, names: dict | None = None) -> str:
    if isinstance(names, dict):
        custom = clean_speaker_name(names.get(speaker))
        if custom:
            return custom
    base, n = split_speaker(speaker)
    if n is not None:
        if base == SPEAKER_MEETING:
            return f"Mówca {n}"
        if base == SPEAKER_ME:
            return f"Mikrofon {n}"
    return _PROMPT_LABELS.get(speaker, speaker)


# --- main function --------------------------------------------------------


def summarize(
    segments: list[Segment],
    model: str,
    device: str = "GPU",
    status: StatusFn | None = None,
    cancel: threading.Event | None = None,
    speaker_names: dict | None = None,
) -> dict:
    """Returns {topic, participants, tldr, key_points, decisions, summary_model,
    summary_device, summary_time_s}.

    An empty dict means there was nothing to summarize or the model returned
    nothing useful - the caller should then leave everything as it was.
    """
    text = (
        as_text(segments, label=lambda code: _prompt_label(code, speaker_names)).strip()
        if segments else ""
    )
    if not text:
        return {}
    known = known_names(segments, speaker_names)
    note = _label_note(known)

    engine = LLMEngine(model, device)
    start = time.monotonic()
    try:
        if status:
            status(t("Loading the summary model..."))
        engine.load()

        chunks = _chunks(text)
        names: list[str] = []

        if len(chunks) > 1:
            notes: list[str] = []
            decisions: list[str] = []
            for i, chunk in enumerate(chunks, start=1):
                if cancel is not None and cancel.is_set():
                    break
                if status:
                    status(t("Summarizing part {i}/{n}...", i=i, n=len(chunks)))
                part = engine.generate_json(
                    CHUNK_PROMPT.format(
                        number=i, total=len(chunks),
                        note=note, material=chunk,
                    ),
                    CHUNK_SCHEMA,
                    MAX_TOKENS_CHUNK,
                    cancel,
                )
                notes.extend(_sentences(part.get("punkty"), 3))
                decisions.extend(_sentences(part.get("ustalenia"), MAX_DECISIONS))
                names.extend(_names(part.get("uczestnicy"), text, known))

            if not notes and not decisions:
                return {}
            material = "Notatki z kolejnych fragmentów:\n" + "\n".join(
                f"- {point}" for point in notes
            )
            source = "notatki z transkryptu"
            # Decisions are passed separately, because they were what got lost
            # most often when joining notes - the model treated a date as one
            # point among many and cut it at the limit of five.
            if decisions:
                material += ("\n\nUstalenia i terminy wychwycone w fragmentach "
                             "(zachowaj daty i godziny co do słowa):\n"
                             + "\n".join(f"- {d}" for d in dict.fromkeys(decisions)))
            if names:
                material += ("\n\nImiona wychwycone w rozmowie: "
                             + ", ".join(dict.fromkeys(names)))
        else:
            material = f"Transkrypt:\n---\n{chunks[0]}\n---"
            source = "transkrypt"

        if cancel is not None and cancel.is_set():
            return {}
        if status:
            status(t("Assembling the summary..."))

        result = _normalize(
            engine.generate_json(
                SUMMARY_PROMPT.format(
                    source=source, note=note, material=material
                ),
                SUMMARY_SCHEMA,
                MAX_TOKENS_FINAL,
                cancel,
            ),
            text,
            known,
        )
        if cancel is not None and cancel.is_set():
            return {}
        if len(result["participants"]) <= len(known) and names:
            result["participants"] = _participants(known, _names(names, text, known))
        if not result["topic"] and not result["tldr"]:
            return {}

        result["summary_model"] = llm.label(model)
        result["summary_device"] = engine.device
        result["summary_time_s"] = round(time.monotonic() - start, 1)
        log.info("Summary ready in %.1fs: %s",
                 result["summary_time_s"], result["topic"])
        return result
    finally:
        engine.release()


# --- saving and folder name -----------------------------------------------


def as_markdown(result: dict, meta: dict) -> str:
    """summary.md. The document stays Polish: the summary content itself is
    Polish (the prompts ask for it), so English headings would only mix languages."""
    lines = [f"# {result.get('topic') or meta.get('name', 'Spotkanie')}", ""]

    if meta.get("date"):
        lines.append(f"- **Data:** {meta['date']}")
    if meta.get("duration_s"):
        lines.append(f"- **Czas trwania:** {format_hms(meta['duration_s'])}")
    if result.get("participants"):
        lines.append(f"- **Uczestnicy:** {', '.join(result['participants'])}")
    lines += ["", "## W skrócie", "", result.get("tldr") or "_Brak._", ""]

    if result.get("key_points"):
        lines += ["## Najważniejsze punkty", ""]
        lines += [f"- {point}" for point in result["key_points"]]
        lines.append("")

    if result.get("decisions"):
        lines += ["## Ustalenia", ""]
        lines += [f"- {decision}" for decision in result["decisions"]]
        lines.append("")

    lines += [
        "---",
        "",
        f"_Wygenerowane lokalnie modelem `{result.get('summary_model', '')}` "
        f"na {result.get('summary_device', '?')} w {result.get('summary_time_s', 0):.0f}s. "
        "Nic nie opuściło tego komputera._",
    ]
    return "\n".join(lines) + "\n"


def timestamp_from_name(folder_name: str) -> str | None:
    """'2026-09-08_09-15_Standup' -> '2026-09-08_09-15'. None for another format."""
    parts = folder_name.split("_")
    if len(parts) < 2:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]):
        return None
    if not re.fullmatch(r"\d{2}-\d{2}", parts[1]):
        return None
    return f"{parts[0]}_{parts[1]}"


def _topic_part(topic: str, max_len: int = 40) -> str:
    """Topic -> folder name part, cut at a word boundary.

    `slug` cuts by characters, so a long topic ended with half a word
    ('architektura-dan'). The last word is dropped only when the cut really
    fell in its middle - otherwise 'Plan na Q4' would lose 'Q4'.
    """
    full = slug(topic, max_len=200)
    if len(full) <= max_len:
        return full
    if full[max_len] == "-":          # the cut fell exactly between words
        return full[:max_len]
    return full[:max_len].rpartition("-")[0].strip("-") or full[:max_len]


def folder_name(old_name: str, result: dict) -> str | None:
    """Builds '2026-09-08_09-15_Topic_Anna-Marek'. None = nothing to change.

    The timestamp comes from the old name, not the clock - otherwise
    re-transcribing an archived recording would change its date.
    """
    timestamp = timestamp_from_name(old_name)
    topic = _topic_part(result.get("topic", ""))
    if not timestamp or not topic:
        return None

    names = slug("-".join(name.split()[0] for name in result.get("participants", [])[:3]),
                 max_len=30)
    new = f"{timestamp}_{topic}"
    if names:
        new += f"_{names}"
    return new if new != old_name else None
