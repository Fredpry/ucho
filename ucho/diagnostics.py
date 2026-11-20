# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Error recognition and the diagnostics package (no Tk here).

`classify()` turns an exception into a plain-language `ErrorInfo` for the
error dialog (ucho/error_dialog.py). It matches by type name, errno and
winerror, never by importing requests, httpx, huggingface_hub, PyAudio or
OpenVINO, so it works whichever of them raised and costs nothing to import.

`build_package()` writes a ZIP the user can e-mail to the maintainer: the
log with personal text removed, the app version, OS and hardware, settings
without personal paths and the list of downloaded models. Ucho does not send
it anywhere; `reveal()` shows the file in Explorer and `open_mail()` opens an
e-mail draft in the user's own mail program, and the user attaches the file.

Redaction (`redact_line`) works on the log line formats this code base writes
(and wrote in earlier versions): dictated text, summary topics, invented-date
filters, names and session folder names are replaced by `<redacted N chars>`,
the user's home folder by `~`, the Windows user name by `<user>`, the computer
name by `<computer>` and session folders inside the recordings folder by
`<recordings>\\<session>`. Audio, transcripts, summaries and meta.json are
never read.
"""

from __future__ import annotations

import ast
import errno
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import config, platform_text
from .i18n import language, t

log = logging.getLogger(__name__)

# --- error recognition ----------------------------------------------------------------------

NETWORK = "network"
DISK_FULL = "disk_full"
PERMISSION = "permission"
FILE_LOCKED = "file_locked"
MODEL_FILES = "model_files"
AUDIO_DEVICE = "audio_device"
MEMORY = "memory"
DEVICE = "device"
GENERIC = "generic"

# Context passed by the caller; it changes the wording, not the recognition.
MODEL_DOWNLOAD = "model_download"


@dataclass(frozen=True)
class ErrorInfo:
    kind: str
    title: str
    message: str
    hint: str


# Exception class names (anywhere in the MRO) of requests, httpx, urllib3,
# urllib, socket and huggingface_hub that mean "no network or server unreachable".
_NETWORK_TYPES = {
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "TimeoutError",
    "TimeoutException", "SSLError", "SSLCertVerificationError", "ProxyError", "ConnectError",
    "NetworkError", "RemoteProtocolError", "URLError", "gaierror",
    "herror", "MaxRetryError", "NewConnectionError", "NameResolutionError", "ProtocolError",
    "ChunkedEncodingError", "IncompleteRead", "OfflineModeIsEnabled", "HfHubHTTPError",
    "HTTPError", "HTTPStatusError", "TooManyRedirects",
}
# Hugging Face errors that say the files are missing (locally or on the server).
_MODEL_TYPES = {"LocalEntryNotFoundError", "EntryNotFoundError", "RepositoryNotFoundError",
                "RevisionNotFoundError", "GatedRepoError"}
_NETWORK_ERRNOS = {errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.ENETUNREACH,
                   errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.ENETDOWN}
# WSAETIMEDOUT, WSAECONNREFUSED, WSAENETUNREACH, WSAEHOSTUNREACH, WSAECONNRESET,
# WSAECONNABORTED, WSAENETDOWN, WSAHOST_NOT_FOUND, WSATRY_AGAIN, WSANO_DATA;
# ERROR_INTERNET_* of WinINet (12002 timeout, 12007 name not resolved, 12029 cannot connect).
_NETWORK_WINERRORS = {10060, 10061, 10051, 10065, 10054, 10053, 10050, 11001, 11002, 11004,
                      12002, 12007, 12029}
_DISK_FULL_ERRNOS = {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}
_DISK_FULL_WINERRORS = {112, 39}  # ERROR_DISK_FULL, ERROR_HANDLE_DISK_FULL
_PERMISSION_WINERRORS = {5}       # ERROR_ACCESS_DENIED
_LOCKED_WINERRORS = {32, 33}      # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
_MEMORY_WINERRORS = {8, 14, 1455}  # NOT_ENOUGH_MEMORY, OUTOFMEMORY, COMMITMENT_LIMIT
# PortAudio error codes are -10000 (paNotInitialized) .. -9972; PyAudio puts
# them into OSError.errno ("[Errno -9996] Invalid input device").
_PORTAUDIO_ERRNOS = range(-10000, -9971)

_DISK_FULL_TEXT = ("no space left on device", "not enough space on the disk",
                   "there is not enough space", "disk full")
_LOCKED_TEXT = ("being used by another process", "used by another process",
                "sharing violation", "lock violation")
_MEMORY_TEXT = ("out of memory", "bad_alloc", "failed to allocate", "cannot allocate memory",
                "not enough memory", "cl_out_of_resources", "cl_out_of_host_memory",
                "cl_mem_object_allocation_failure", "paging file is too small")
_AUDIO_TEXT = ("invalid input device", "invalid output device", "no default input device",
               "no default output device", "device unavailable", "unanticipated host error",
               "invalid number of channels", "invalid sample rate", "portaudio",
               "audclnt_e_", "no audio devices available")
_MODEL_TEXT = ("unable to read the model", "network_not_read", "openvino_model.xml",
               "openvino_model.bin", "openvino_tokenizer", "openvino_detokenizer",
               "incorrect weights", "consistency check failed", "model file is corrupted",
               "failed to read the model")
_DEVICE_TEXT = ("intel_gpu", "intel_npu", "opencl", "level zero", "level_zero", "ze_result",
                "gpu plugin", "npu plugin", "not registered in the openvino runtime",
                "clgetplatformids", "no gpu device", "device is not available")


def _names(error: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(error).__mro__}


def _chain(error: BaseException, limit: int = 8) -> list[BaseException]:
    """The error, then its __cause__/__context__ chain (outermost first)."""
    chain: list[BaseException] = []
    while error is not None and error not in chain and len(chain) < limit:
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


def _text(error: BaseException) -> str:
    try:
        return str(error).lower()
    except Exception:
        return ""


def _codes(error: BaseException) -> tuple[int | None, int | None]:
    number = getattr(error, "errno", None)
    winerror = getattr(error, "winerror", None)
    return (number if isinstance(number, int) else None,
            winerror if isinstance(winerror, int) else None)


def _is_memory(error: BaseException, _context: str | None) -> bool:
    number, winerror = _codes(error)
    return ("MemoryError" in _names(error) or number == errno.ENOMEM
            or winerror in _MEMORY_WINERRORS or any(m in _text(error) for m in _MEMORY_TEXT))


def _is_disk_full(error: BaseException, _context: str | None) -> bool:
    number, winerror = _codes(error)
    return (number in _DISK_FULL_ERRNOS or winerror in _DISK_FULL_WINERRORS
            or any(m in _text(error) for m in _DISK_FULL_TEXT))


def _is_locked(error: BaseException, _context: str | None) -> bool:
    _number, winerror = _codes(error)
    return winerror in _LOCKED_WINERRORS or any(m in _text(error) for m in _LOCKED_TEXT)


def _is_permission(error: BaseException, _context: str | None) -> bool:
    number, winerror = _codes(error)
    if winerror in _LOCKED_WINERRORS:
        return False
    return ("PermissionError" in _names(error) or winerror in _PERMISSION_WINERRORS
            or number in (errno.EACCES, errno.EPERM) and isinstance(error, OSError))


def _is_network(error: BaseException, context: str | None) -> bool:
    names = _names(error)
    if names & _MODEL_TYPES:
        # A download that cannot reach the server ends in LocalEntryNotFoundError
        # ("cannot find the requested files in the local cache").
        return context == MODEL_DOWNLOAD and "LocalEntryNotFoundError" in names
    number, winerror = _codes(error)
    return bool(names & _NETWORK_TYPES) or number in _NETWORK_ERRNOS \
        or winerror in _NETWORK_WINERRORS


def _is_audio(error: BaseException, _context: str | None) -> bool:
    number, _winerror = _codes(error)
    names = _names(error)
    if {"PortAudioError", "PaError"} & names:
        return True
    if number is not None and number in _PORTAUDIO_ERRNOS and isinstance(error, OSError):
        return True
    text = _text(error)
    # audio.Recorder raises these with translated text.
    own = (t("No audio devices available for recording."),
           t("Could not start any recording track."))
    return any(m in text for m in _AUDIO_TEXT) or any(m.lower() in text for m in own)


def _in_models_dir(path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(config.MODELS_DIR.resolve())
    except (OSError, TypeError, ValueError):
        return False


def _is_model_files(error: BaseException, _context: str | None) -> bool:
    if _names(error) & _MODEL_TYPES:
        return True
    if isinstance(error, FileNotFoundError) and error.filename and _in_models_dir(error.filename):
        return True
    return any(m in _text(error) for m in _MODEL_TEXT)


def _is_device(error: BaseException, _context: str | None) -> bool:
    text = _text(error)
    if any(m in text for m in _DEVICE_TEXT):
        return True
    # OpenVINO raises plain RuntimeErrors that start with "Exception from src/...".
    return "exception from src" in text and ("gpu" in text or "npu" in text)


# Checked in this order over the whole exception chain, so a specific cause
# (disk full inside a failed download) wins over the wrapper's type.
_MATCHERS: tuple[tuple[str, Callable[[BaseException, str | None], bool]], ...] = (
    (MEMORY, _is_memory),
    (DISK_FULL, _is_disk_full),
    (FILE_LOCKED, _is_locked),
    (PERMISSION, _is_permission),
    (NETWORK, _is_network),
    (AUDIO_DEVICE, _is_audio),
    (MODEL_FILES, _is_model_files),
    (DEVICE, _is_device),
)


def recognize(error: BaseException, context: str | None = None) -> str:
    """The kind of `error` (one of the constants above), GENERIC when unknown."""
    chain = _chain(error)
    for kind, matches in _MATCHERS:
        for item in chain:
            try:
                if matches(item, context):
                    return kind
            except Exception:
                log.debug("Error matcher %s failed", kind, exc_info=True)
    return GENERIC


def _error_path(error: BaseException) -> str:
    for item in _chain(error):
        path = getattr(item, "filename", None)
        if isinstance(path, str | os.PathLike) and str(path):
            return str(path)
    return ""


def classify(error: BaseException, context: str | None = None) -> ErrorInfo:
    """Plain-language title, message and hint for `error` (texts through t())."""
    kind = recognize(error, context)
    download = context == MODEL_DOWNLOAD
    if kind == NETWORK:
        return ErrorInfo(
            kind,
            t("The model could not be downloaded") if download else t("No connection"),
            t("Ucho could not reach the download server (Hugging Face). The computer may be "
              "offline, or a firewall or proxy blocks the connection."),
            t("Check the internet connection and try again. A company network may have to allow "
              "huggingface.co and *.hf.co."))
    if kind == DISK_FULL:
        return ErrorInfo(
            kind, t("The disk is full"),
            t("There is not enough free space on the disk to finish this."),
            t("Free some space (models need up to a few GB) and try again."))
    if kind in (PERMISSION, FILE_LOCKED):
        if kind == FILE_LOCKED:
            title = t("A file is in use")
            message = t("Another program has a file open that Ucho needs.")
        else:
            title = t("Access denied")
            message = platform_text.access_denied_message()
        path = _error_path(error)
        if (path and config.onedrive_folder(Path(path))) or "onedrive" in _text(error):
            hint = platform_text.cloud_lock_hint()
        elif kind == FILE_LOCKED:
            hint = t("Close programs that may use the file (an antivirus scan, a backup, another "
                     "Ucho window) and try again.")
        else:
            hint = t("Check that the folder is not read-only, or choose another folder in "
                     "Settings.")
        return ErrorInfo(kind, title, message, hint)
    if kind == MODEL_FILES:
        return ErrorInfo(
            kind, t("Model files are missing or damaged"),
            t("The model is not fully downloaded, or its files are damaged."),
            t("Download the model again in Settings. If it keeps failing, delete the models "
              "folder in the app folder and download again."))
    if kind == AUDIO_DEVICE:
        return ErrorInfo(
            kind, t("Audio device unavailable"),
            t("The microphone or speakers could not be opened. The device may be unplugged, "
              "disabled, or used by another program in exclusive mode."),
            platform_text.audio_device_hint())
    if kind == MEMORY:
        return ErrorInfo(
            kind, t("Not enough memory"),
            t("The computer ran out of memory for this task."),
            t("Close other programs, or choose a smaller model in Settings."))
    if kind == DEVICE:
        return ErrorInfo(
            kind, t("Graphics or NPU device error"),
            t("OpenVINO could not use the graphics card or NPU."),
            t("Update the Intel graphics driver, or choose CPU as the device in Settings."))
    return ErrorInfo(
        GENERIC,
        t("The model could not be downloaded") if download else t("Something went wrong"),
        t("Ucho ran into an unexpected error."),
        t("If it happens again, save a diagnostics package and send it to the author."))


def format_details(error: BaseException, recordings: Iterable[str] = ()) -> str:
    """The redacted traceback of `error` for "Show details" and error.txt."""
    try:
        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    except Exception:
        text = f"{type(error).__name__}: {error}"
    redactor = Redactor(recordings=list(recordings))
    return "\n".join(redactor.lines(text.rstrip().splitlines()))


# --- redaction ------------------------------------------------------------------------------

def _redacted(count: int) -> str:
    return f"<redacted {count} chars>"


def _python_text_length(raw: str) -> int:
    """Length of a repr()-ed string as logged with %r; raw length if it is not one."""
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return len(raw)
    return len(value) if isinstance(value, str) else len(raw)


# Dictated text: `log.info("Recognized %.1f s of audio in %.1f s: %r", ...)` in
# dictation.py (v0.2+) and its v0.1 Polish wording. dictate.py restores the last
# dictation from this line; keep both regexes in step.
_DICTATION = re.compile(
    r"((?:Recognized [\d.]+ s of audio in [\d.]+ s|Rozpoznano [\d.]+ s audio w [\d.]+ s): )(.+)$")

# Other user text: (prefix, text, suffix). Current formats and the formats of
# earlier versions that may still sit in an old ucho.log / ucho.log.1.
_USER_TEXT = [re.compile(pattern) for pattern in (
    # summary.py: the summary topic; the invented-date filters
    r"((?:Summary ready in|Podsumowanie gotowe w) [\d.]+s: )(.+)()$",
    r"((?:Removing invented month|Usuwam zmyslony miesiac) )(.+)()$",
    r"((?:Invented numeric date|Zmyslona data liczbowa) )(.+?)"
    r"( - keeping the day only| - zostawiam sam dzien)?$",
    # summary.py before v0.5: rejected person names
    r"((?:Rejecting name|Odrzucam imie) )(.+?)( - not said in the transcript| - nie pada w "
    r"transkrypcie)$",
    # meeting.py before v0.5: session folder names (they hold the meeting title)
    r"(Recording started: )(.+)()$",
    r"(Transcription of )(.+?)( already running or queued| queued \(\d+ waiting\))$",
    r"(Queued session )(.+?)( no longer exists - skipped)$",
    r"((?:Session renamed|Sesja przemianowana): )(.+)()$",
    r"((?:Could not rename|Nie udalo sie zmienic nazwy) )(.+)()$",
    r"(Quit with \d+ queued transcription\(s\) not started: )(.+)()$",
)]

# A session folder name anywhere: "2026-01-05_10-00_Budget-review" -> keep the
# timestamp, drop the title (and participant names) after it.
_SESSION_NAME = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})_[^\s'\"\\/,;:)\]}]+")


def _path_pattern(path: str) -> str:
    """Regex for a Windows path written with \\, \\\\ (repr) or / separators, any case."""
    parts = [re.escape(part) for part in re.split(r"[\\/]+", path.strip("\\/")) if part]
    # A POSIX path (macOS home "/Users/jan") keeps its leading "/", or
    # "/Users/jan/x" would become "/~/x". Windows paths never start with "/".
    lead = "/" if path.startswith("/") else ""
    return lead + r"[\\/]{1,2}".join(parts)


def _user_names() -> list[str]:
    names = {os.environ.get("USERNAME", ""), Path.home().name}
    # Names shorter than 2 characters would hit ordinary words.
    return sorted({n for n in names if len(n) >= 2}, key=len, reverse=True)


@dataclass
class Redactor:
    """Replaces personal text in log lines; counts what it removed.

    include_dictation: keep dictated text (the user opted in); paths, names
    and the other user text are redacted either way.
    """

    include_dictation: bool = False
    home: str = field(default_factory=lambda: str(Path.home()))
    user_names: list[str] = field(default_factory=_user_names)
    computer: str = field(default_factory=lambda: os.environ.get("COMPUTERNAME", ""))
    recordings: list[str] = field(default_factory=list)
    dictations: int = 0
    fragments: int = 0

    def __post_init__(self) -> None:
        folders = [str(config.RECORDINGS_DIR), *self.recordings]
        folders = sorted({f for f in folders if f and len(f) > 3}, key=len, reverse=True)
        # (?![\w.-]): the home "C:\Users\jan" must not eat the start of "C:\Users\janek".
        end = r"(?![\w.-])"
        self._recordings = [re.compile(_path_pattern(f) + end + r"(?:[\\/]{1,2}([^\\/'\"\r\n]+))?",
                                       re.IGNORECASE) for f in folders]
        self._home = (re.compile(_path_pattern(self.home) + end, re.IGNORECASE)
                      if len(self.home) > 3 else None)
        names = [n for n in self.user_names if n]
        self._names = (re.compile(r"(?<![\w])(?:" + "|".join(re.escape(n) for n in names)
                                  + r")(?![\w])", re.IGNORECASE) if names else None)
        self._computer = (re.compile(r"(?<![\w-])" + re.escape(self.computer) + r"(?![\w-])",
                                     re.IGNORECASE) if len(self.computer) >= 2 else None)

    def text(self, value: str, recordings: bool = True) -> str:
        """Paths, user and computer names only (for settings and system info).

        recordings=False leaves the recordings folder a path under ~, so
        settings.json still shows whether it is the default or in OneDrive.
        """
        for pattern in self._recordings if recordings else ():
            value = pattern.sub(lambda m: "<recordings>" + ("\\<session>" if m.group(1) else ""),
                                value)
        if self._home is not None:
            value = self._home.sub("~", value)
        # The computer name first: it often contains the user name ("JAN-PC").
        if self._computer is not None:
            value = self._computer.sub("<computer>", value)
        if self._names is not None:
            value = self._names.sub("<user>", value)
        return value

    def line(self, line: str) -> str:
        match = _DICTATION.search(line)
        if match:
            if not self.include_dictation:
                self.dictations += 1
                line = (line[:match.start(2)] + _redacted(_python_text_length(match.group(2))))
            return self.text(line) if not self.include_dictation else self._paths_only(line)
        for pattern in _USER_TEXT:
            match = pattern.search(line)
            if match:
                self.fragments += 1
                line = (line[:match.start(2)] + _redacted(_python_text_length(match.group(2)))
                        + (match.group(3) or ""))
                break
        line, count = _SESSION_NAME.subn(lambda m: m.group(1) + "_<title>", line)
        self.fragments += count
        return self.text(line)

    def _paths_only(self, line: str) -> str:
        """A dictation line the user chose to include: redact only the log prefix.

        The dictated text itself stays as dictated, even if it names the user.
        """
        match = _DICTATION.search(line)
        head = self.text(line[:match.start(2)]) if match else self.text(line)
        return head + (match.group(2) if match else "")

    def lines(self, lines: Iterable[str]) -> list[str]:
        return [self.line(line) for line in lines]


def redact_log(text: str, include_dictation: bool = False,
               recordings: Iterable[str] = ()) -> str:
    """The whole log text redacted (see Redactor)."""
    redactor = Redactor(include_dictation=include_dictation, recordings=list(recordings))
    return "\n".join(redactor.lines(text.splitlines()))


# --- package --------------------------------------------------------------------------------

LOG_EXCERPT_LINES = 40
# What a package may contain, in this order (system.txt etc. are always there).
PACKAGE_FILES = ("README.txt", "system.txt", "settings.json", "models.txt", "error.txt",
                 "ucho.log", "ucho.log.1")


def log_files() -> list[Path]:
    return [p for p in (config.LOG_FILE, config.LOG_FILE.with_name(config.LOG_FILE.name + ".1"))
            if p.is_file()]


def _read_log(file: Path) -> str:
    try:
        return file.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"<could not read {file.name}: {type(error).__name__}>"


def _settings_file(settings) -> Path:
    if settings is not None and getattr(settings, "file", None):
        return Path(settings.file)
    return config.SETTINGS_FILE


def _recordings_dirs(settings) -> list[str]:
    folders = []
    try:
        if settings is not None:
            folders.append(str(settings.recordings_dir))
    except Exception:
        pass
    try:
        stored = json.loads(_settings_file(settings).read_text(encoding="utf-8-sig"))
        if isinstance(stored, dict) and isinstance(stored.get("recordings_dir"), str):
            folders.append(stored["recordings_dir"])
    except (OSError, ValueError):
        pass
    return folders


@dataclass
class Preview:
    files: list[str]
    log_excerpt: str
    dictations: int
    fragments: int


def _file_list(logs: dict[str, str], error: BaseException | None) -> list[str]:
    present = {"README.txt", "system.txt", "settings.json", "models.txt", *logs}
    if error is not None:
        present.add("error.txt")
    return [name for name in PACKAGE_FILES if name in present]


def _redacted_logs(include_log_text: bool, settings) -> tuple[dict[str, str], Redactor]:
    redactor = Redactor(include_dictation=include_log_text, recordings=_recordings_dirs(settings))
    logs = {}
    for file in log_files():
        logs[file.name] = "\n".join(redactor.lines(_read_log(file).splitlines())) + "\n"
    return logs, redactor


def preview(include_log_text: bool = False, settings=None, error: BaseException | None = None
            ) -> Preview:
    """What `build_package` would write: file names and the end of the redacted log."""
    logs, redactor = _redacted_logs(include_log_text, settings)
    files = _file_list(logs, error)
    main = logs.get(config.LOG_FILE.name) or next(iter(logs.values()), "")
    excerpt = "\n".join(main.rstrip("\n").splitlines()[-LOG_EXCERPT_LINES:])
    return Preview(files, excerpt, redactor.dictations, redactor.fragments)


def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def _folder_size(folder: Path) -> tuple[int, bool]:
    """(bytes of real files, any unfinished download) without following links."""
    total, incomplete = 0, False
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = Path(root) / name
            try:
                if path.is_symlink():
                    continue
                total += path.stat().st_size
            except OSError:
                continue
            incomplete |= name.endswith(".incomplete")
    return total, incomplete


def models_report(models_dir: Path | None = None) -> str:
    """Downloaded model repos with sizes and pinned-revision status."""
    models_dir = models_dir or config.MODELS_DIR
    lines = ["Downloaded models (Hugging Face cache in the app's models folder)", ""]
    hub = models_dir / "hub"
    repos = sorted(hub.glob("models--*")) if hub.is_dir() else []
    if not repos:
        lines.append("(none)")
    for repo in repos:
        repo_id = repo.name.removeprefix("models--").replace("--", "/", 1)
        size, incomplete = _folder_size(repo)
        snapshots = sorted(p.name for p in (repo / "snapshots").glob("*")) \
            if (repo / "snapshots").is_dir() else []
        pinned = config.REVISIONS.get(repo_id)
        state = []
        if pinned:
            state.append("pinned revision present" if pinned in snapshots
                         else "pinned revision MISSING")
        if incomplete:
            state.append("unfinished download")
        lines.append(f"{repo_id}  {_size(size)}  snapshots: {len(snapshots)}"
                     + (f"  ({', '.join(state)})" if state else ""))
    cache = models_dir / "ovcache"
    if cache.is_dir():
        lines += ["", f"OpenVINO compile cache: {_size(_folder_size(cache)[0])}"]
    return "\n".join(lines) + "\n"


def _free_space(path: Path) -> str:
    import shutil

    try:
        while not path.exists() and path.parent != path:
            path = path.parent
        return _size(shutil.disk_usage(path).free)
    except OSError:
        return "?"


def system_report(redactor: Redactor, settings=None, profile=None) -> str:
    """App version, Python, Windows, hardware; every probe may fail."""

    def safe(probe: Callable[[], str]) -> str:
        try:
            return probe()
        except Exception as error:
            return f"? ({type(error).__name__})"

    def windows() -> str:
        version = sys.getwindowsversion()
        return f"{platform.platform()} (build {version.build})"

    def genai() -> str:
        from . import transcribe

        return transcribe.genai_version()

    lines = [
        f"{config.APP_NAME} {config.VERSION}",
        f"Created: {datetime.now().isoformat(timespec='seconds')}",
        f"Python: {sys.version.split()[0]} ({platform.architecture()[0]})",
        f"Windows: {safe(windows) if sys.platform == 'win32' else platform.platform()}",
        f"Machine: {safe(platform.machine)}",
        f"OpenVINO GenAI: {safe(genai)}",
        f"UI language: {language()}",
        f"App folder: {redactor.text(str(config.PROJECT_DIR))}"
        f"  (OneDrive: {config.onedrive_folder(config.PROJECT_DIR)})",
        f"Free space, app folder: {_free_space(config.PROJECT_DIR)}",
    ]
    recordings = _recordings_dirs(settings)
    if recordings:
        folder = Path(recordings[0])
        lines.append(f"Free space, recordings folder: {_free_space(folder)}"
                     f"  (OneDrive: {config.onedrive_folder(folder)})")
    lines.append(f"sherpa-onnx (Parakeet, speakers): {safe(lambda: str(config.sherpa_available()))}")
    lines.append("")
    try:
        from . import hardware

        hardware_profile = profile if profile is not None else hardware.profile()
        lines += [redactor.text(line) for line in hardware._describe(hardware_profile)]
    except Exception as error:
        lines.append(f"Hardware profile: ? ({type(error).__name__})")
    return "\n".join(lines) + "\n"


def settings_report(redactor: Redactor, settings=None) -> str:
    """settings.json with personal paths and names replaced, dictionary words removed."""
    try:
        data = json.loads(_settings_file(settings).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        return json.dumps({"_error": f"settings.json not readable: {type(error).__name__}"}) + "\n"
    if not isinstance(data, dict):
        return json.dumps({"_error": "settings.json is not an object"}) + "\n"

    def clean(value):
        if isinstance(value, str):
            return redactor.text(value, recordings=False)
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value

    result = {}
    for key, value in data.items():
        if key == "dictation_dictionary" and isinstance(value, dict):
            # The user's own words and names: only how many there are.
            result[key] = f"<{len(value)} entries>"
        else:
            result[key] = clean(value)
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def _readme(include_log_text: bool, files: list[str]) -> str:
    log_note = ("The log INCLUDES the dictated text: you chose to include it."
                if include_log_text else
                "Dictated text is replaced by <redacted N chars>.")
    return "\n".join([
        f"{config.APP_NAME} diagnostics package ({config.VERSION})",
        "",
        "Made by Ucho on your computer. Ucho does not send it anywhere: if you want",
        f"help, attach this file to an e-mail to {config.SUPPORT_EMAIL} yourself.",
        "",
        "Contents:",
        *[f"  {name:14} {_README_FILES[name]}" for name in files if name in _README_FILES],
        "",
        "Not included: recordings (audio), transcripts, summaries, meta.json.",
        "Removed from the log and settings: meeting titles and folder names, summary",
        "topics, person names, your user name (<user>), your home folder (~), the",
        f"computer name (<computer>) and dictionary words. {log_note}",
        "",
        "You can open the files with Notepad and delete anything before sending.",
        "",
    ])


_README_FILES = {
    "README.txt": "this file",
    "system.txt": "Ucho version, Python, Windows, CPU, RAM, graphics, free space",
    "settings.json": "your settings without personal paths and names",
    "models.txt": "downloaded models and their sizes",
    "error.txt": "the error shown in the error dialog, with its traceback",
    "ucho.log": "the app log (redacted)",
    "ucho.log.1": "the older part of the log (redacted)",
}


def package_name(now: datetime | None = None) -> str:
    return f"ucho-diagnostics-{(now or datetime.now()):%Y%m%d-%H%M%S}.zip"


def build_package(dest_dir: Path, include_log_text: bool = False, *, settings=None,
                  error: BaseException | None = None, info: ErrorInfo | None = None,
                  profile=None, now: datetime | None = None) -> Path:
    """Writes the diagnostics ZIP into `dest_dir` and returns its path.

    Never reads recordings, transcripts, summaries or meta.json. A probe that
    fails leaves a "?" in its file instead of failing the package.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    logs, redactor = _redacted_logs(include_log_text, settings)
    contents: dict[str, str] = {
        "system.txt": system_report(redactor, settings, profile),
        "settings.json": settings_report(redactor, settings),
        "models.txt": models_report(),
    }
    if error is not None:
        info = info or classify(error)
        contents["error.txt"] = (f"Kind: {info.kind}\nTitle: {info.title}\n\n"
                                 + format_details(error, _recordings_dirs(settings)) + "\n")
    contents.update(logs)
    files = _file_list(logs, error)
    contents["README.txt"] = _readme(include_log_text, files)
    target = dest_dir / package_name(now)
    partial = target.with_name(target.name + ".part")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.writestr(name, contents[name])
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    log.info("Diagnostics package saved (%d files, include dictation: %s)", len(files),
             include_log_text)
    return target


# --- where to save, Explorer and e-mail -----------------------------------------------------

_FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"


def _known_folder(folder_id: str) -> Path | None:
    """A Windows Known Folder (follows a Downloads folder moved to another drive)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        guid = GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(folder_id), ctypes.byref(guid))
        pointer = ctypes.c_wchar_p()
        ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                                   ctypes.byref(pointer))
        try:
            return Path(pointer.value) if pointer.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pointer)
    except Exception:
        log.debug("Known folder %s not available", folder_id, exc_info=True)
        return None


def save_locations() -> list[Path]:
    """Downloads, then Desktop, then the log folder (existing folders first)."""
    candidates = [_known_folder(_FOLDERID_DOWNLOADS) or Path.home() / "Downloads",
                  _known_folder(_FOLDERID_DESKTOP) or Path.home() / "Desktop",
                  config.LOG_FILE.parent]
    result: list[Path] = []
    for folder in candidates:
        if folder not in result:
            result.append(folder)
    return result


def save_package(include_log_text: bool = False, **kwargs) -> Path:
    """build_package into the first save location that works."""
    last: BaseException | None = None
    for folder in save_locations():
        if not folder.is_dir():
            continue
        try:
            return build_package(folder, include_log_text, **kwargs)
        except OSError as error:
            last = error
            log.warning("Could not save the diagnostics package here: %s", type(error).__name__)
    raise last or OSError(errno.ENOENT, "No folder to save the diagnostics package")


def reveal(path: Path) -> None:
    """Opens Explorer with the file selected."""
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
    except Exception as error:
        log.warning("Could not show the package in Explorer: %s", type(error).__name__)


def mail_url(package: Path | str, info: ErrorInfo | None = None,
             address: str | None = None) -> str:
    """mailto: URL with the subject and a short body (UTF-8, percent-encoded)."""
    name = Path(package).name
    happened = f"{info.title} ({info.kind})" if info else t("(describe what happened)")
    body = "\r\n".join([
        t("Hello,"),
        "",
        t("What happened: {what}", what=happened),
        t("Ucho version: {version}", version=config.VERSION),
        t("Diagnostics package: {file}", file=name),
        "",
        t("Please attach the file {file} from the folder {folder} before sending.", file=name,
          folder=Path(package).parent.name or "?"),
        "",
    ])
    subject = f"Ucho diagnostics {config.VERSION}"
    return (f"mailto:{address or config.SUPPORT_EMAIL}?subject={quote(subject, safe='')}"
            f"&body={quote(body, safe='')}")


def open_mail(url: str) -> bool:
    """Opens the mail draft in the default mail program. False when there is none."""
    try:
        if sys.platform == "win32":
            os.startfile(url)  # the user's own mail program; Ucho sends nothing
        else:
            import webbrowser

            return webbrowser.open(url)
        return True
    except Exception as error:
        log.warning("Could not open an e-mail draft: %s", type(error).__name__)
        return False


# --- throttling of the error dialog ----------------------------------------------------------

class Throttle:
    """Decides whether an error opens a dialog, so a loop of errors opens one.

    - while a dialog is open, new errors are only counted;
    - the same error (type and message) again within `repeat_s` is skipped;
    - at most `burst` dialogs per `window_s`.
    Thread-safe: hooks call `allow` from any thread.
    """

    def __init__(self, repeat_s: float = 60.0, burst: int = 3, window_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        self.repeat_s, self.burst, self.window_s = repeat_s, burst, window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._open = False
        self._last: dict[str, float] = {}
        self._shown: list[float] = []
        self.suppressed = 0

    @staticmethod
    def key(error: BaseException) -> str:
        return f"{type(error).__module__}.{type(error).__qualname__}: {error}"[:500]

    def allow(self, error: BaseException, force: bool = False) -> bool:
        """True: show a dialog now (the caller must call `closed()` when it closes).

        force: an error the user caused again on purpose (e.g. Retry); only
        the "one dialog at a time" rule applies.
        """
        now = self._clock()
        key = self.key(error)
        with self._lock:
            self._shown = [s for s in self._shown if now - s < self.window_s]
            last = self._last.get(key)
            limited = (last is not None and now - last < self.repeat_s) \
                or len(self._shown) >= self.burst
            if self._open or (limited and not force):
                self.suppressed += 1
                return False
            self._open = True
            self._last[key] = now
            self._shown.append(now)
            return True

    def closed(self) -> None:
        with self._lock:
            self._open = False
            self.suppressed = 0

    @property
    def is_open(self) -> bool:
        return self._open


class Reporter:
    """Turns errors from any thread into at most one dialog at a time.

    post: runs a function on the UI thread (App.post). show(error, context,
    on_close) builds the dialog there and returns it; the dialog calls
    on_close when it closes. The dialog may have `update_suppressed(count)`.
    """

    def __init__(self, post: Callable[[Callable[[], None]], None],
                 show: Callable[[BaseException, str | None, Callable[[], None]], object],
                 owner=None, throttle: Throttle | None = None):
        self.post, self.show, self.owner = post, show, owner
        self.throttle = throttle or Throttle()
        self.dialog = None

    def report(self, error: BaseException, context: str | None = None,
               force: bool = False) -> None:
        if isinstance(error, KeyboardInterrupt | SystemExit):
            return
        if not self.throttle.allow(error, force=force):
            dialog = self.dialog
            if dialog is not None and hasattr(dialog, "update_suppressed"):
                count = self.throttle.suppressed
                self.post(lambda: dialog.update_suppressed(count))
            return
        self.post(lambda: self._show(error, context))

    def _show(self, error: BaseException, context: str | None) -> None:
        try:
            self.dialog = self.show(error, context, self._closed)
        except Exception:
            self._closed()
            # A warning, not an error: an error here must not come back to this hook.
            log.warning("Could not show the error dialog", exc_info=True)

    def _closed(self) -> None:
        self.dialog = None
        self.throttle.closed()
