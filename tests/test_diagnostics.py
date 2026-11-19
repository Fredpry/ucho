# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Error recognition, log redaction, the diagnostics package and the dialog throttle.

Synthetic data only: fake exception classes named like the library ones,
log lines built with the same format strings the app uses.
"""

import errno
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from ucho import config, diagnostics, dictate
from ucho.diagnostics import classify, recognize

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _named(name: str, base: type = Exception) -> type:
    """A class with a library's exception name (no import of the library)."""
    return type(name, (base,), {})


def _line(name: str, message: str, *args) -> str:
    """A log line exactly as the app's RotatingFileHandler writes it."""
    record = logging.LogRecord(name, logging.INFO, __file__, 1, message, args, None)
    record.asctime = "2026-11-19 10:00:00,123"
    return LOG_FORMAT % {**record.__dict__, "message": record.getMessage()}


# --- classify ---------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    _named("ConnectionError", OSError)("Max retries exceeded"),            # requests
    _named("ConnectTimeout")("timed out"),                                 # requests / httpx
    _named("ConnectError")("[Errno 11001] getaddrinfo failed"),            # httpx
    _named("SSLError")("certificate verify failed"),
    _named("NameResolutionError")("Failed to resolve 'huggingface.co'"),   # urllib3
    _named("URLError", OSError)("<urlopen error [Errno 11001]>"),           # urllib
    _named("OfflineModeIsEnabled")("offline"),                             # huggingface_hub
    _named("HfHubHTTPError")("503 Server Error"),
    TimeoutError("timed out"),
    OSError(errno.ECONNREFUSED, "Connection refused"),
])
def test_network_errors(error):
    info = classify(error)
    assert info.kind == diagnostics.NETWORK
    assert info.title and info.message and info.hint


def test_network_error_by_winerror():
    error = OSError(None, "No such host is known")
    error.winerror = 11001  # WSAHOST_NOT_FOUND
    assert recognize(error) == diagnostics.NETWORK


def test_offline_download_ends_in_local_entry_not_found():
    local = _named("LocalEntryNotFoundError", FileNotFoundError)
    error = local("cannot find the requested files in the local cache")
    assert recognize(error, diagnostics.MODEL_DOWNLOAD) == diagnostics.NETWORK
    assert classify(error, diagnostics.MODEL_DOWNLOAD).title == "The model could not be downloaded"
    # Loading a model that is not there is a missing model, not the network.
    assert recognize(error) == diagnostics.MODEL_FILES


def test_cause_in_the_chain_wins():
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError as inner:
            raise RuntimeError("download failed") from inner
    except RuntimeError as outer:
        assert recognize(outer) == diagnostics.DISK_FULL
    wrapped = RuntimeError("load failed")
    wrapped.__context__ = _named("ReadTimeout")("read timed out")
    assert recognize(wrapped) == diagnostics.NETWORK


@pytest.mark.parametrize("winerror", [112, 39])
def test_disk_full_by_winerror(winerror):
    error = OSError(None, "There is not enough space on the disk")
    error.winerror = winerror
    assert classify(error).kind == diagnostics.DISK_FULL


def test_disk_full_by_errno():
    assert recognize(OSError(errno.ENOSPC, "No space left on device")) == diagnostics.DISK_FULL


def test_permission_and_locked_files():
    denied = PermissionError(errno.EACCES, "Access is denied", r"C:\Program Files\x.txt")
    denied.winerror = 5
    assert classify(denied).kind == diagnostics.PERMISSION
    locked = PermissionError(errno.EACCES, "The process cannot access the file because it is "
                             "being used by another process", r"D:\data\meeting.flac")
    locked.winerror = 32
    assert classify(locked).kind == diagnostics.FILE_LOCKED
    lock_violation = OSError(None, "lock")
    lock_violation.winerror = 33
    assert recognize(lock_violation) == diagnostics.FILE_LOCKED


def test_onedrive_hint(monkeypatch):
    monkeypatch.setenv("OneDrive", r"C:\Users\someone\OneDrive")
    error = PermissionError(errno.EACCES, "Access is denied",
                            r"C:\Users\someone\OneDrive\Ucho\recordings\a.flac")
    error.winerror = 32
    assert "OneDrive" in classify(error).hint
    plain = PermissionError(errno.EACCES, "Access is denied", r"D:\local\a.flac")
    assert "OneDrive" not in classify(plain).hint


@pytest.mark.parametrize("error", [
    _named("EntryNotFoundError")("openvino_model.bin not found"),
    _named("RepositoryNotFoundError", _named("HfHubHTTPError"))("404"),
    RuntimeError("Exception from src/inference/src/cpp/core.cpp:93: Unable to read the model: "
                 "openvino_model.xml"),
    RuntimeError("Check 'false' failed: [ NETWORK_NOT_READ ] incorrect weights"),
])
def test_model_files(error):
    assert classify(error).kind == diagnostics.MODEL_FILES


def test_missing_file_inside_the_models_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    inside = FileNotFoundError(errno.ENOENT, "No such file", str(tmp_path / "models" / "hub" / "x"))
    assert recognize(inside) == diagnostics.MODEL_FILES
    outside = FileNotFoundError(errno.ENOENT, "No such file", str(tmp_path / "other"))
    assert recognize(outside) == diagnostics.GENERIC


@pytest.mark.parametrize("error", [
    OSError(-9996, "Invalid input device (no default output device)"),   # PyAudio
    OSError(-9985, "Device unavailable"),
    OSError(-9999, "Unanticipated host error"),
    _named("PortAudioError")("Error opening InputStream"),                # sounddevice
    RuntimeError("No audio devices available for recording."),          # audio.Recorder
])
def test_audio_device(error):
    assert classify(error).kind == diagnostics.AUDIO_DEVICE


def test_audio_device_message_in_polish():
    from ucho import i18n

    i18n.set_language("pl")
    error = RuntimeError(i18n.t("No audio devices available for recording."))
    assert recognize(error) == diagnostics.AUDIO_DEVICE
    assert classify(error).title == "Urządzenie audio niedostępne"


@pytest.mark.parametrize("error", [
    MemoryError(),
    _named("_ArrayMemoryError", MemoryError)("Unable to allocate 3.1 GiB"),   # numpy
    RuntimeError("Exception from src/plugins/intel_gpu: CL_OUT_OF_RESOURCES"),
    RuntimeError("std::bad_alloc"),
])
def test_memory(error):
    assert classify(error).kind == diagnostics.MEMORY


def test_memory_by_winerror():
    error = OSError(None, "The paging file is too small for this operation to complete")
    error.winerror = 1455
    assert recognize(error) == diagnostics.MEMORY


@pytest.mark.parametrize("message", [
    "Exception from src/inference/src/core.cpp: [GPU] clGetPlatformIDs failed",
    "Device with \"NPU\" name is not registered in the OpenVINO Runtime",
    "Exception from src/plugins/intel_gpu/src/runtime/ocl/ocl_engine.cpp: OpenCL error",
])
def test_device(message):
    assert classify(RuntimeError(message)).kind == diagnostics.DEVICE


def test_generic():
    info = classify(ValueError("something odd"))
    assert info.kind == diagnostics.GENERIC
    assert info.title == "Something went wrong"


def test_classify_survives_a_broken_str():
    class Broken(Exception):
        def __str__(self):
            raise RuntimeError("no")

    assert classify(Broken()).kind == diagnostics.GENERIC


# --- redaction ----------------------------------------------------------------------------------

HOME = r"C:\Users\Jan.Kowalski"
RECORDINGS = r"D:\Spotkania"


def _redactor(**kwargs) -> diagnostics.Redactor:
    options = {"home": HOME, "user_names": ["Jan.Kowalski"], "computer": "JAN-LAPTOP",
               "recordings": [RECORDINGS]}
    options.update(kwargs)
    return diagnostics.Redactor(**options)


def _dictation_line(text: str) -> str:
    # The format of dictation.py; dictate.last_text_from_log parses it.
    return _line("ucho.dictation", "Recognized %.1f s of audio in %.1f s: %r", 4.2, 1.3, text)


def test_dictation_line_is_redacted():
    text = "Zażółć gęślą jaźń, spotkanie o 15:00 z panią Nowak"
    line = _dictation_line(text)
    redactor = _redactor()
    result = redactor.line(line)
    assert "Nowak" not in result and "Zażółć" not in result
    assert result.endswith(f": <redacted {len(text)} chars>")
    assert "Recognized 4.2 s of audio in 1.3 s" in result
    assert redactor.dictations == 1


def test_dictation_line_matches_the_restore_parser(tmp_path):
    """The redaction and dictate.last_text_from_log read the same line."""
    file = tmp_path / "ucho.log"
    file.write_text(_dictation_line("Test line 'with' quotes\nand a newline") + "\n", encoding="utf-8")
    assert dictate.last_text_from_log(file) == "Test line 'with' quotes\nand a newline"
    result = _redactor().line(file.read_text(encoding="utf-8").strip())
    assert "quotes" not in result and "<redacted 37 chars>" in result


def test_v01_polish_dictation_line():
    line = _line("dyktuj", "Rozpoznano %.1f s audio w %.1f s: %r", 2.0, 0.8, "Tajny tekst")
    assert "Tajny" not in _redactor().line(line)


def test_dictation_kept_when_opted_in():
    line = _dictation_line("Jan.Kowalski dyktuje notatkę")
    redactor = _redactor(include_dictation=True)
    assert redactor.line(line).endswith("'Jan.Kowalski dyktuje notatkę'")
    assert redactor.dictations == 0


@pytest.mark.parametrize("name,message,args,secret", [
    ("ucho.summary", "Summary ready in %.1fs: %s", (12.3, "Budżet Q1 i dostawca stali"), "Budżet"),
    ("ucho.summary", "Removing invented month %r", ("marca",), "marca"),
    ("ucho.summary", "Invented numeric date %r - keeping the day only", ("29.02",), "29.02"),
    ("ucho.summary", "Rejecting name %r - not said in the transcript", ("Teresa",), "Teresa"),
    ("ucho.meeting", "Recording started: %s", ("2026-01-05_10-00_Budget",), "Budget"),
    ("ucho.meeting", "Transcription of %s queued (%d waiting)", ("2026-01-05_10-00_Budget", 2),
     "Budget"),
    ("ucho.meeting", "Session renamed: %s -> %s", ("2026-01-05_10-00_A", "2026-01-05_10-00_Topic_Anna"),
     "Anna"),
    ("ucho.meeting", "Could not rename %s -> %s: %s", ("x_Secret", "y", "err"), "Secret"),
    ("ucho.meeting", "Quit with %d queued transcription(s) not started: %s",
     (1, "2026-01-05_10-00_Supplier-call"), "Supplier"),
])
def test_other_user_text_is_redacted(name, message, args, secret):
    result = _redactor().line(_line(name, message, *args))
    assert secret not in result
    assert "<redacted" in result


def test_session_folder_names_and_paths():
    redactor = _redactor()
    trace = (r'  File "D:\\Spotkania\\2026-01-05_10-00_Budget-review_Anna\\meta.json", line 3'
             "\nFileNotFoundError: [Errno 2] No such file: "
             r"'C:\Users\Jan.Kowalski\Ucho\app\models\hub'")
    result = "\n".join(redactor.lines(trace.splitlines()))
    assert "Budget" not in result and "Anna" not in result and "Kowalski" not in result
    assert r"<recordings>\<session>" in result
    assert r"'~\Ucho\app\models\hub'" in result
    loose = redactor.line("Opened 2026-01-05_10-00_Budget-review_Anna, done")
    assert loose == "Opened 2026-01-05_10-00_<title>, done"


def test_user_and_computer_names():
    redactor = _redactor(home=r"C:\Users\ola", user_names=["ola"], computer="OLA-PC")
    result = redactor.line(r"Microphone 'Słuchawki (ola)' on OLA-PC, file C:\Users\ola\x.txt; "
                           r"C:\Users\olaf\y.txt; parabola")
    assert result == (r"Microphone 'Słuchawki (<user>)' on <computer>, file ~\x.txt; "
                      r"C:\Users\olaf\y.txt; parabola")


def test_home_with_forward_slashes_and_case():
    assert _redactor().text("c:/users/jan.kowalski/Ucho") == "~/Ucho"


def test_ordinary_lines_stay():
    line = _line("ucho.engine", "Dictation model ready: %s on %s (%.1f s)",
                 "OpenVINO/whisper-large-v3-fp16-ov", "GPU", 2.5)
    assert _redactor().line(line) == line


# --- package --------------------------------------------------------------------------------

@pytest.fixture
def app_folder(tmp_path, monkeypatch):
    """A fake app folder: log, settings and a models cache; no real data."""
    folder = tmp_path / "app"
    folder.mkdir()
    monkeypatch.setattr(config, "PROJECT_DIR", folder)
    monkeypatch.setattr(config, "LOG_FILE", folder / "ucho.log")
    monkeypatch.setattr(config, "SETTINGS_FILE", folder / "settings.json")
    monkeypatch.setattr(config, "MODELS_DIR", folder / "models")
    home = str(Path.home())
    lines = [
        _line("ucho.app", "Start %s %s (UI: %s, layout: %s)", "Ucho", "0.5.0", "pl", "single"),
        _dictation_line("SEKRETNE dyktowanie numer jeden"),
        _line("ucho.summary", "Summary ready in %.1fs: %s", 9.0, "TAJNY temat"),
        _line("ucho.config", "Could not create the recordings folder %s", home + r"\Ucho\recordings"),
    ]
    (folder / "ucho.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (folder / "ucho.log.1").write_text(_dictation_line("SEKRETNE starsze dyktowanie") + "\n",
                                       encoding="utf-8")
    (folder / "settings.json").write_text(json.dumps({
        "recordings_dir": home + r"\Ucho\recordings",
        "analysis_model": home + r"\Modele\moj-model.gguf",
        "dictation_dictionary": {"Cloud Code": "Claude Code", "Kowalsky": "Kowalski"},
        "microphone_name": "Mikrofon (Realtek)",
        "model": "OpenVINO/whisper-large-v3-int8-ov",
    }), encoding="utf-8")
    repo = folder / "models" / "hub" / "models--OpenVINO--whisper-large-v3-int8-ov"
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / "abc").write_bytes(b"x" * 2048)
    revision = config.REVISIONS.get("OpenVINO/whisper-large-v3-int8-ov", "rev")
    (repo / "snapshots" / revision).mkdir(parents=True)
    return folder


def _zip_text(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def test_build_package_contents(app_folder, tmp_path):
    from ucho import hardware

    path = diagnostics.build_package(tmp_path / "out", profile=hardware.HardwareProfile(),
                                     now=datetime(2026, 11, 19, 14, 5, 9))
    assert path.name == "ucho-diagnostics-20261119-140509.zip"
    files = _zip_text(path)
    assert set(files) == {"README.txt", "system.txt", "settings.json", "models.txt",
                          "ucho.log", "ucho.log.1"}
    everything = "\n".join(files.values())
    assert "SEKRETNE" not in everything and "TAJNY" not in everything
    assert str(Path.home()) not in everything
    assert "<redacted" in files["ucho.log"] and "<redacted" in files["ucho.log.1"]
    settings = json.loads(files["settings.json"])
    assert settings["dictation_dictionary"] == "<2 entries>"
    assert settings["recordings_dir"] == r"~\Ucho\recordings"
    assert settings["analysis_model"] == r"~\Modele\moj-model.gguf"
    assert settings["microphone_name"] == "Mikrofon (Realtek)"
    assert "Ucho 0.5.0" in files["system.txt"] or config.VERSION in files["system.txt"]
    assert "OpenVINO/whisper-large-v3-int8-ov" in files["models.txt"]
    assert "2.0 KB" in files["models.txt"]
    assert "Not included: recordings" in files["README.txt"]
    assert not list((tmp_path / "out").glob("*.part"))


def test_build_package_with_dictation_opt_in(app_folder, tmp_path):
    from ucho import hardware

    path = diagnostics.build_package(tmp_path, include_log_text=True,
                                     profile=hardware.HardwareProfile())
    files = _zip_text(path)
    assert "SEKRETNE dyktowanie numer jeden" in files["ucho.log"]
    assert "SEKRETNE starsze dyktowanie" in files["ucho.log.1"]
    # Only dictation is opted in; the summary topic stays out.
    assert "TAJNY" not in files["ucho.log"]
    assert "INCLUDES the dictated text" in files["README.txt"]


def test_build_package_with_an_error(app_folder, tmp_path):
    from ucho import hardware

    try:
        raise OSError(errno.ENOSPC, "No space left on device", str(Path.home() / "x.bin"))
    except OSError as error:
        path = diagnostics.build_package(tmp_path, error=error, profile=hardware.HardwareProfile())
    files = _zip_text(path)
    assert "Kind: disk_full" in files["error.txt"]
    assert "Traceback" in files["error.txt"]
    assert str(Path.home()) not in files["error.txt"]


def test_build_package_survives_failing_probes(app_folder, tmp_path, monkeypatch):
    from ucho import hardware

    def broken():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(hardware, "profile", broken)
    (app_folder / "settings.json").write_text("{broken", encoding="utf-8")
    files = _zip_text(diagnostics.build_package(tmp_path))
    assert "Hardware profile: ? (RuntimeError)" in files["system.txt"]
    assert "_error" in json.loads(files["settings.json"])


def test_preview(app_folder):
    result = diagnostics.preview()
    assert result.files == ["README.txt", "system.txt", "settings.json", "models.txt",
                            "ucho.log", "ucho.log.1"]
    assert "SEKRETNE" not in result.log_excerpt
    assert result.dictations == 2
    assert result.fragments >= 1
    opted = diagnostics.preview(include_log_text=True, error=ValueError("x"))
    assert "SEKRETNE dyktowanie numer jeden" in opted.log_excerpt
    assert "error.txt" in opted.files and opted.dictations == 0


def test_preview_without_logs(app_folder):
    (app_folder / "ucho.log").unlink()
    (app_folder / "ucho.log.1").unlink()
    result = diagnostics.preview()
    assert "ucho.log" not in result.files and result.log_excerpt == ""


def test_save_locations_fall_back(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, "_known_folder", lambda _id: None)
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "app" / "ucho.log")
    locations = diagnostics.save_locations()
    assert locations[0] == Path.home() / "Downloads"
    assert locations[1] == Path.home() / "Desktop"
    assert locations[-1] == tmp_path / "app"


def test_save_package_uses_the_first_existing_folder(app_folder, tmp_path, monkeypatch):
    from ucho import hardware

    missing, existing = tmp_path / "missing", tmp_path / "desk"
    existing.mkdir()
    monkeypatch.setattr(diagnostics, "save_locations", lambda: [missing, existing])
    path = diagnostics.save_package(profile=hardware.HardwareProfile())
    assert path.parent == existing


# --- e-mail -----------------------------------------------------------------------------------

def test_mail_url_encoding():
    from ucho import i18n

    i18n.set_language("pl")
    info = classify(OSError(errno.ENOSPC, "No space left on device"))
    url = diagnostics.mail_url(Path(r"C:\x\Pobrane\ucho-diagnostics-20261119-140509.zip"), info)
    parts = urlsplit(url)
    assert parts.scheme == "mailto" and parts.path == config.SUPPORT_EMAIL
    assert " " not in url and "\n" not in url and "ł" not in url
    query = parse_qs(parts.query)
    assert query["subject"] == [f"Ucho diagnostics {config.VERSION}"]
    body = query["body"][0]
    assert "Dysk jest pełny (disk_full)" in body
    assert "ucho-diagnostics-20261119-140509.zip" in body
    assert "Pobrane" in body
    assert "\r\n" in body
    assert "%C5%82" in url  # "ł" as UTF-8
    assert unquote(url).count("&") == 1


def test_mail_url_without_an_error():
    body = parse_qs(urlsplit(diagnostics.mail_url("a.zip")).query)["body"][0]
    assert "(describe what happened)" in body


# --- throttle -----------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_throttle_one_dialog_at_a_time():
    clock = Clock()
    throttle = diagnostics.Throttle(clock=clock)
    assert throttle.allow(ValueError("a"))
    for _ in range(100):  # a loop of errors while the dialog is open
        assert not throttle.allow(RuntimeError("b"))
    assert throttle.suppressed == 100
    throttle.closed()
    assert throttle.allow(RuntimeError("b"))


def test_throttle_repeats_and_bursts():
    clock = Clock()
    throttle = diagnostics.Throttle(repeat_s=60, burst=3, window_s=300, clock=clock)
    assert throttle.allow(ValueError("same"))
    throttle.closed()
    clock.now += 10
    assert not throttle.allow(ValueError("same"))       # the same error again soon
    assert throttle.allow(ValueError("same"), force=True)  # the user retried on purpose
    throttle.closed()
    clock.now += 61
    assert throttle.allow(ValueError("other"))
    throttle.closed()
    clock.now += 1
    assert not throttle.allow(ValueError("third"))      # 3 dialogs in 5 minutes
    clock.now += 300
    assert throttle.allow(ValueError("third"))


def test_reporter_shows_one_dialog_for_a_burst_of_thread_errors():
    posted, shown = [], []

    def show(error, context, on_close):
        shown.append((error, context, on_close))
        return object()

    reporter = diagnostics.Reporter(posted.append, show)
    for i in range(50):  # e.g. a failing thread in a loop
        reporter.report(RuntimeError(f"loop {i}"))
    reporter.report(KeyboardInterrupt())
    assert len(posted) == 1
    posted.pop()()
    assert len(shown) == 1 and reporter.throttle.suppressed == 49
    shown[0][2]()  # the dialog closes
    reporter.report(OSError(errno.ENOSPC, "full"), "model_download", force=True)
    posted.pop()()
    assert shown[1][1] == "model_download"


def test_reporter_releases_the_throttle_when_the_dialog_fails():
    posted = []

    def show(error, context, on_close):
        raise RuntimeError("no window")

    reporter = diagnostics.Reporter(posted.append, show)
    reporter.report(ValueError("a"))
    posted.pop()()
    assert not reporter.throttle.is_open
