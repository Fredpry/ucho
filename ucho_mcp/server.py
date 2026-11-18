# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""MCP server (stdio) that gives AI agents access to the Ucho recordings archive.

Deliberately without external libraries: Python standard library only,
read-only, stdio only. The server opens no ports, does not touch the network
and does not change files: it reads `meta.json`, the transcript and the
summary from the recordings folder. That keeps the whole code reviewable in
a few minutes.

Sessions are read and searched through `ucho.archive` (built on
`ucho.session`), the same code the app's recordings list uses, so archives in
the current format and in the pre-v0.2 one (Polish file names and `meta.json`
keys) both work.

Protocol: JSON-RPC 2.0, one message per line (MCP stdio transport).
Only protocol responses go to stdout; logs go to stderr.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from ucho import session as sessions
from ucho.archive import Archive, ArchiveError
from ucho.config import VERSION  # one version for the app and the server
from ucho.merge import TRANSCRIPT_MD, format_hms, speaker_label

log = logging.getLogger("ucho_mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
NAME = "ucho"

INSTRUCTIONS = (
    "Archive of local meeting recordings and transcripts from the Ucho app. "
    "Start with list_meetings or search_transcripts, then fetch get_transcript "
    "or get_summary for the chosen id. Transcripts may contain confidential "
    "conversations: quote only what the answer needs."
)

TOOLS = [
    {
        "name": "list_meetings",
        "description": (
            "List recorded meetings, newest first. Optional filters: date range "
            "(YYYY-MM-DD) and a phrase in the meeting name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Start date (YYYY-MM-DD), inclusive."},
                "to": {"type": "string", "description": "End date (YYYY-MM-DD), inclusive."},
                "query": {"type": "string", "description": "Part of the meeting name."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_transcript",
        "description": (
            "Meeting transcript with timestamps and speakers ('Me' = the microphone of "
            "the person who recorded, 'Meeting' = system audio (everyone else), "
            "'Speaker N' = a voice told apart inside the system audio, "
            "'Microphone N' = a voice told apart inside the microphone track; "
            "a person's name instead of a label was given by the user). "
            "Optionally only a range of minutes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "id from list_meetings (folder name)."},
                "from_min": {"type": "number", "description": "From this minute."},
                "to_min": {"type": "number", "description": "Up to this minute."},
            },
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_summary",
        "description": (
            "Meeting summary, if one was generated (summaries are optional in Ucho). "
            "If there is none, fetch the transcript and summarize it yourself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "search_transcripts",
        "description": (
            "Full-text search across all transcripts. Returns the meeting, time, "
            "speaker and an excerpt of the utterance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to find."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]

# Argument names used before v0.2, still accepted so existing agent prompts work.
ARGUMENT_ALIASES = {"od": "from", "do": "to", "fraza": "query",
                    "od_min": "from_min", "do_min": "to_min"}


# Bad arguments go back to the agent as isError. The archive raises the same class.
ToolError = ArchiveError


def _speaker(code, names: dict | None = None) -> str:
    return speaker_label(str(code or ""), names, translate=False)


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    for format_ in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text.strip(), format_)
        except ValueError:
            continue
    return None


def search(archive: Archive, query: str, limit: int) -> list[dict]:
    """search_transcripts result rows (the search itself lives in ucho.archive)."""
    return [{"id": h.id, "time": format_hms(h.start), "speaker": h.name or _speaker(h.speaker),
             "excerpt": h.text[:400]} for h in archive.search(query, limit)]


# --- tools -------------------------------------------------------------------


def _number(value, name: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number") from None


def _arguments(args: dict) -> dict:
    """Arguments with the pre-v0.2 Polish names mapped to the current ones."""
    result = dict(args)
    for old, new in ARGUMENT_ALIASES.items():
        if result.get(new) is None and result.get(old) is not None:
            result[new] = result[old]
    return result


def call_tool(archive: Archive, name: str, args: dict) -> str:
    args = _arguments(args)

    if name == "list_meetings":
        start, end = _date(args.get("from")), _date(args.get("to"))
        query = (args.get("query") or "").lower()
        limit = _number(args.get("limit"), "limit", 50, 500)
        result = []
        for session in archive.sessions():
            description = archive.describe(session)
            date = _date(description["date"][:10]) if description["date"] else None
            if start and (date is None or date < start):
                continue
            if end and (date is None or date > end):
                continue
            if query and query not in f"{description['title']} {session.name}".lower():
                continue
            result.append(description)
            if len(result) >= limit:
                break
        if not result:
            return f"No meetings match the criteria in {archive.folder}."
        return json.dumps(result, ensure_ascii=False, indent=1)

    if name == "get_transcript":
        session = archive.session(args.get("id"))
        start = float(args["from_min"]) * 60 if args.get("from_min") is not None else None
        end = float(args["to_min"]) * 60 if args.get("to_min") is not None else None
        segments = archive.segments(session)
        if not segments:
            file = sessions.find_file(session, TRANSCRIPT_MD)
            if file is not None and start is None and end is None:
                return file.read_text(encoding="utf-8")
            return "This meeting has no transcript yet."
        description = archive.describe(session)
        names = sessions.speaker_names(archive.meta(session))
        lines = [f"# {description['title']} ({description['date']})", ""]
        for seg in segments:
            at = float(seg.get("start", 0))
            if (start is not None and at < start) or (end is not None and at > end):
                continue
            lines.append(f"[{format_hms(at)}] {_speaker(seg.get('speaker'), names)}: {seg['text']}")
        return "\n".join(lines)

    if name == "get_summary":
        session = archive.session(args.get("id"))
        file = sessions.find_file(session, sessions.SUMMARY_MD)
        if file is not None:
            return file.read_text(encoding="utf-8")
        return ("This meeting has no summary (summaries are optional in Ucho). "
                "Fetch get_transcript and summarize it yourself.")

    if name == "search_transcripts":
        query = args.get("query")
        if not isinstance(query, str):
            raise ToolError("Provide a query.")
        hits = search(archive, query, _number(args.get("limit"), "limit", 20, 200))
        if not hits:
            return f"Nothing found: {query!r}"
        return json.dumps(hits, ensure_ascii=False, indent=1)

    raise ToolError(f"Unknown tool: {name}")


# --- protocol ----------------------------------------------------------------


def handle(archive: Archive, message: dict) -> dict | None:
    """One JSON-RPC message -> response (or None for notifications)."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    id_ = message.get("id")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    if id_ is None:
        return None  # notification, e.g. notifications/initialized

    def result(data: dict) -> dict:
        return {"jsonrpc": "2.0", "id": id_, "result": data}

    def error(code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": text}}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return result({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            text = call_tool(archive, name, arguments)
            return result({"content": [{"type": "text", "text": text}], "isError": False})
        except ToolError as problem:
            return result({"content": [{"type": "text", "text": str(problem)}], "isError": True})
        except Exception as problem:  # the agent should get the error text
            log.exception("Tool %s failed", name)
            return result({"content": [{"type": "text", "text": f"Error: {problem}"}],
                           "isError": True})
    if method in ("resources/list", "prompts/list"):
        return result({method.split("/")[0]: []})
    return error(-32601, f"Method not supported: {method}")


def recordings_dir(argv: list[str]) -> Path:
    """--recordings-dir <path> > UCHO_RECORDINGS_DIR variable > settings.json > default.

    The pre-v0.2 names `--katalog` and UCHO_NAGRANIA are still accepted.
    """
    import os

    for flag in ("--recordings-dir", "--katalog"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return Path(argv[i + 1]).expanduser()
    for variable in ("UCHO_RECORDINGS_DIR", "UCHO_NAGRANIA"):
        if os.environ.get(variable):
            return Path(os.environ[variable]).expanduser()
    from ucho import config

    return config.Settings().recordings_dir


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    archive = Archive(recordings_dir(argv))
    log.info("Archive: %s", archive.folder)

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}}
        else:
            try:
                if isinstance(message, list):  # JSON-RPC batch
                    response = [r for r in (handle(archive, m) for m in message) if r]
                    response = response or None
                else:
                    response = handle(archive, message)
            except Exception:  # one bad message must not end the server
                log.exception("Could not handle a message")
                id_ = message.get("id") if isinstance(message, dict) else None
                response = None if id_ is None and isinstance(message, dict) else {
                    "jsonrpc": "2.0", "id": id_,
                    "error": {"code": -32603, "message": "Internal error"}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
    return 0
