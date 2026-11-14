# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""The recordings archive: listing, describing and full-text search of sessions.

Shared by the app (recordings list and its search box) and the MCP server, so
both find the same meetings for the same words. Sessions are read through
`ucho.session`, which also understands the pre-v0.2 format (Polish file names
and `meta.json` keys).

Search: an in-memory SQLite FTS5 index over the transcript segments
(diacritics-insensitive, prefix match), rebuilt when any `meta.json` changes.
Standard library only, because the MCP server must not need anything else.

The index holds a folded copy of each segment (`fold`: lowercase, no
diacritics) and queries are folded the same way. The FTS5 tokenizer option
`remove_diacritics 2` alone is not enough: "ł" has no Unicode decomposition,
so "zlotych" did not find "złotych" and "Lodz" did not find "Łódź". Results
always carry the original text; `match_spans` maps a folded match back to
offsets in that original text for highlighting.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import session as sessions
from .merge import META_FILE, TRANSCRIPT_MD


class ArchiveError(Exception):
    """Bad input from the caller (invalid id, empty query); the message is shown as is."""


def hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fold(text: str) -> str:
    """Lowercase without diacritics: 'Budżet' -> 'budzet' ('ł' -> 'l' too)."""
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def query_words(query: str) -> list[str]:
    return re.findall(r"\w+", query or "", flags=re.UNICODE)


def folded_words(query: str) -> list[str]:
    """The query words as they are matched against the index."""
    return [token for word in query_words(query) for token in tokens(fold(word)) if token]


def fold_map(text: str) -> tuple[str, list[int]]:
    """`fold(text)` character by character, with the original index of every folded character.

    Folding can change the length ('ﬁ' -> 'fi'), so offsets in the folded
    text are mapped back through this list.
    """
    folded: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        piece = fold(char) if not char.isascii() else char.lower()
        folded.append(piece)
        origin.extend([index] * len(piece))
    return "".join(folded), origin


def is_token_char(char: str) -> bool:
    """A character the FTS5 unicode61 tokenizer keeps inside a token.

    Its default `categories` are "L* N* Co": letters, numbers and private
    use. Everything else separates tokens, also "_" (which `\\w` counts as a
    word character), so "q1" finds "budżet_q1" and must be highlighted there.
    """
    category = unicodedata.category(char)
    return category[0] in "LN" or category == "Co"


def tokens(folded: str) -> list[str]:
    """The tokens of a folded text, split the way FTS5 unicode61 splits them."""
    result, current = [], []
    for char in folded:
        if is_token_char(char):
            current.append(char)
        elif current:
            result.append("".join(current))
            current = []
    if current:
        result.append("".join(current))
    return result


def match_spans(text: str, words: list[str]) -> list[tuple[int, int]]:
    """(start, end) offsets in the original `text` of words that start with a query word.

    The same rule as the search: folded, a prefix of a token. 'zlot' marks
    'Złot' in 'Złotych', 'lodz' marks 'Łódź', 'q1' marks 'q1' in 'budżet_q1'.
    Combining marks right after the match ('ź' written as 'z' plus an accent)
    belong to it.
    """
    folded, origin = fold_map(text)
    spans = []
    for word in {fold(w) for w in words if w}:
        # Lookahead: every start position, also where matches would overlap.
        for found in re.finditer("(?=" + re.escape(word) + ")", folded):
            start, end = found.start(), found.start() + len(word)
            if start > 0 and is_token_char(folded[start - 1]):
                continue  # inside a token, not at its start
            last = origin[end - 1] + 1
            while last < len(text) and unicodedata.combining(text[last]):
                last += 1
            spans.append((origin[start], last))
    return sorted(set(spans))


def _like_escape(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _matches_words(folded: str, words: list[str]) -> bool:
    """Every query word is a prefix of a token in the folded text (the FTS5 rule)."""
    found = tokens(folded)
    return all(any(token.startswith(word) for token in found) for word in words)


@dataclass
class Hit:
    """One transcript segment that matches a query."""

    id: str  # session folder name
    start: float
    speaker: str  # speaker code ("me" / "meeting" / other)
    text: str


@dataclass
class Match:
    """A session that matches a query: by its title or summary, or by transcript hits."""

    session: Path
    title_match: bool = False
    hits: list[Hit] = field(default_factory=list)


class Archive:
    """Read-only view of a recordings folder. Thread-safe."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.fts = True
        self._index: sqlite3.Connection | None = None
        self._index_state: tuple | None = None
        self._titles: dict[str, str] = {}
        self._lock = threading.RLock()

    # Security: the id is only a folder name directly inside the archive.
    def session(self, id_: str) -> Path:
        if not isinstance(id_, str) or not id_ or re.search(r"[\\/:]|\.\.", id_):
            raise ArchiveError(f"Invalid id: {id_!r}")
        path = self.folder / id_
        if sessions.is_tombstone(id_) or not path.is_dir():
            raise ArchiveError(f"No meeting with id {id_!r}. Use list_meetings.")
        return path

    def sessions(self) -> list[Path]:
        return sessions.list_sessions(self.folder)

    @staticmethod
    def meta(session: Path) -> dict:
        return sessions.read_meta(session)

    def describe(self, session: Path, meta: dict | None = None) -> dict:
        meta = self.meta(session) if meta is None else meta
        description = {
            "id": session.name,
            "title": meta.get("topic") or meta.get("name") or session.name,
            "date": meta.get("date") or sessions.date_from_name(session.name),
            "duration": hms(meta["duration_s"]) if meta.get("duration_s") else None,
            "transcript": sessions.find_file(session, TRANSCRIPT_MD) is not None,
            "summary": sessions.find_file(session, sessions.SUMMARY_MD) is not None,
        }
        if meta.get("participants"):
            description["participants"] = meta["participants"]
        if meta.get("tldr"):
            description["tldr"] = meta["tldr"]
        return description

    def segments(self, session: Path) -> list[dict]:
        return [s for s in self.meta(session).get("segments") or [] if s.get("text")]

    # --- full-text index ------------------------------------------------

    def _state(self) -> tuple:
        state = []
        for session in self.sessions():
            try:
                state.append((session.name, (session / META_FILE).stat().st_mtime_ns))
            except OSError:
                continue
        return tuple(state)

    def index(self) -> sqlite3.Connection:
        """In-memory FTS5 index, rebuilt when the archive changes."""
        with self._lock:
            state = self._state()
            if self._index is not None and state == self._index_state:
                return self._index
            # The app searches from a worker thread; the lock serializes access.
            db = sqlite3.connect(":memory:", check_same_thread=False)
            # `text` is the original, returned in results; only `folded` is searched.
            try:
                db.execute("CREATE VIRTUAL TABLE u USING fts5(session UNINDEXED, start UNINDEXED,"
                           " speaker UNINDEXED, text UNINDEXED, folded,"
                           " tokenize='unicode61 remove_diacritics 2')")
                self.fts = True
            except sqlite3.OperationalError:
                # SQLite without FTS5: a plain table, LIKE to narrow, then the same prefix rule.
                db.execute("CREATE TABLE u (session, start, speaker, text, folded)")
                self.fts = False
            titles = []
            for name, _ in state:
                meta = self.meta(self.folder / name)
                titles.append((name, fold(" ".join(str(meta.get(k) or "") for k in (
                    "topic", "name", "tldr")) + " " + " ".join(meta.get("participants") or []))))
                for seg in meta.get("segments") or []:
                    if seg.get("text"):
                        text = str(seg["text"])
                        db.execute("INSERT INTO u VALUES (?, ?, ?, ?, ?)",
                                   (name, float(seg.get("start", 0)), seg.get("speaker", ""),
                                    text, fold(text)))
            db.commit()
            if self._index is not None:
                self._index.close()
            self._index, self._index_state = db, state
            self._titles = dict(titles)
            return db

    def search(self, query: str, limit: int) -> list[Hit]:
        """Transcript segments matching all words of `query` (prefix, no diacritics)."""
        words = folded_words(query)
        if not words:
            raise ArchiveError("Empty query.")
        with self._lock:
            db = self.index()
            if self.fts:
                match = " ".join(f'"{w}"*' for w in words)
                rows = db.execute(
                    "SELECT session, start, speaker, text FROM u WHERE u MATCH ? "
                    "ORDER BY rank LIMIT ?", (match, limit)).fetchall()
            else:
                # LIKE only narrows (substring); the prefix rule decides, as in FTS5.
                condition = " AND ".join("folded LIKE ? ESCAPE '\\'" for _ in words)
                candidates = db.execute(
                    f"SELECT session, start, speaker, text, folded FROM u WHERE {condition}",
                    [f"%{_like_escape(w)}%" for w in words]).fetchall()
                rows = [row[:4] for row in candidates if _matches_words(row[4], words)][:limit]
        return [Hit(s, float(st), str(sp or ""), str(tx)) for s, st, sp, tx in rows]

    def find(self, query: str, limit: int = 60, hits_per_session: int = 3) -> list[Match]:
        """Sessions matching `query`, newest first, for the app's search box.

        A session matches when every word is a prefix of a word in its title,
        topic, one-line summary or participants, or when transcript segments
        match. Up to `hits_per_session` segment hits are kept per session.
        """
        words = folded_words(query)
        if not words:
            return [Match(s) for s in self.sessions()[:limit]]
        hits = self.search(query, limit=2000)
        with self._lock:
            titles = dict(self._titles)
        by_session: dict[str, list[Hit]] = {}
        for hit in sorted(hits, key=lambda h: h.start):
            by_session.setdefault(hit.id, []).append(hit)

        result = []
        for session in self.sessions():
            title_words = tokens(titles.get(session.name, "") + " "
                                 + fold(session.name.replace("-", " ").replace("_", " ")))
            title_match = all(any(t.startswith(w) for t in title_words) for w in words)
            session_hits = by_session.get(session.name, [])
            if title_match or session_hits:
                result.append(Match(session, title_match, session_hits[:hits_per_session]))
                if len(result) >= limit:
                    break
        return result
