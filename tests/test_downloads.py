# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Model download progress and the speech model gate for recording (#28).

No network and no huggingface_hub: the progress bar class is driven with the
same calls `snapshot_download` makes in huggingface_hub 1.30.
"""
from __future__ import annotations

import errno
import sys
import threading
from types import SimpleNamespace

import pytest

from ucho import downloads
from ucho.downloads import (DOWNLOADING, FAILED, MISSING, READY, UNKNOWN, ByteCounter, Download,
                            Downloads, Progress, block_reason, describe_error, progress_bar_class,
                            speech_readiness)

MB = 1024 * 1024


def _snapshot_bars(bar_class):
    """The three bars snapshot_download creates from `tqdm_class` (huggingface_hub 1.30)."""
    transfer = bar_class(desc="Downloading bytes", total=0, initial=0, unit="B", unit_scale=True,
                         bar_format="{desc}: {bar}| {n_fmt:>5}B{postfix:>12}")
    reconstruct = bar_class(desc="Reconstructing (incomplete total...)", total=0, initial=0,
                            unit="B", unit_scale=True, bar_format="{l_bar}{bar}|")
    return transfer, reconstruct


def _start_file(transfer, reconstruct, size: int, initial: int = 0) -> None:
    """What `_AggregatedTqdm.__init__` does for each file that starts downloading."""
    reconstruct.total = (reconstruct.total or 0) + size
    transfer.total = (transfer.total or 0) + size
    reconstruct.refresh()
    if initial:
        reconstruct.update(initial)


# --- progress ---------------------------------------------------------------------


def test_bytes_add_up_across_files_and_are_not_counted_twice():
    counter = ByteCounter()
    transfer, reconstruct = _snapshot_bars(progress_bar_class(counter))
    with progress_bar_class(counter)(desc="Fetching 2 files", total=2) as files:
        _start_file(transfer, reconstruct, 300 * MB)
        _start_file(transfer, reconstruct, 100 * MB)
        reconstruct.update(150 * MB)
        transfer.update(150 * MB)            # network bytes: the same data again
        transfer.total = 900 * MB            # its guessed total grows by itself
        reconstruct.update(50 * MB)
        transfer.update(50 * MB)
        reconstruct.set_postfix_str("12 MB/s", refresh=False)
        assert reconstruct.format_dict["rate"] is None
        files.update(1)
    progress = counter.progress()
    assert progress == Progress(200 * MB, 400 * MB)
    assert progress.fraction == pytest.approx(0.5)
    assert progress.describe() == "50% · 200 / 400 MB"


def test_network_bytes_move_the_bar_while_the_disk_count_lags():
    # Xet writes to the disk in large steps; the network count is smoother.
    counter = ByteCounter()
    transfer, reconstruct = _snapshot_bars(progress_bar_class(counter))
    _start_file(transfer, reconstruct, 100 * MB)
    transfer.update(30 * MB)
    assert counter.progress() == Progress(30 * MB, 100 * MB)
    # Compressed or deduplicated: fewer network bytes than the file; the disk finishes it.
    transfer.update(50 * MB)
    reconstruct.update(100 * MB)
    assert counter.progress() == Progress(100 * MB, 100 * MB)
    # Never above the total because of the network count.
    transfer.update(500 * MB)
    assert counter.progress().fraction == 1.0


def test_total_unknown_until_the_first_file_starts():
    counter = ByteCounter()
    _transfer, reconstruct = _snapshot_bars(progress_bar_class(counter))
    assert counter.progress() == Progress(0, 0)
    assert counter.progress().fraction is None
    reconstruct.update(3 * MB)  # bytes without a known size (no Content-Length)
    progress = counter.progress()
    assert progress.total == 0 and progress.fraction is None
    assert progress.describe() == "3 MB"


def test_never_more_than_100_percent_and_never_negative():
    counter = ByteCounter()
    transfer, reconstruct = _snapshot_bars(progress_bar_class(counter))
    _start_file(transfer, reconstruct, 10 * MB)
    reconstruct.update(12 * MB)
    assert counter.progress().fraction == 1.0
    assert counter.progress().describe() == "100% · 12 / 12 MB"
    # A retry that starts a file again rolls the bar back (http_get does update(-resume_size)).
    reconstruct.update(-50 * MB)
    assert counter.progress().done == 0


def test_resumed_file_counts_its_existing_bytes():
    counter = ByteCounter()
    transfer, reconstruct = _snapshot_bars(progress_bar_class(counter))
    _start_file(transfer, reconstruct, 100 * MB, initial=40 * MB)
    assert counter.progress() == Progress(40 * MB, 100 * MB)


def test_updates_from_many_download_threads():
    counter = ByteCounter()
    transfer, reconstruct = _snapshot_bars(progress_bar_class(counter))

    def worker():
        _start_file(transfer, reconstruct, 1000)
        for _ in range(1000):
            reconstruct.update(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert counter.progress() == Progress(8000, 8000)


def test_bar_class_is_not_a_tqdm_subclass_without_lock_api():
    # huggingface_hub then neither passes `name`/`disable` nor shares a tqdm lock.
    bar_class = progress_bar_class(ByteCounter())
    assert not hasattr(bar_class, "get_lock")
    bar = bar_class(desc="x", total=3)
    bar.set_description("done")
    bar.close()
    assert list(bar_class([1, 2, 3])) == [1, 2, 3]


# --- errors -----------------------------------------------------------------------


class ConnectError(Exception):
    pass


class LocalEntryNotFoundError(Exception):
    pass


def test_errors_get_a_short_reason():
    assert "connection" in describe_error(ConnectError("[Errno 11001] getaddrinfo failed"))
    wrapped = LocalEntryNotFoundError("An error happened while trying to locate the files...")
    assert "connection" in describe_error(wrapped)
    try:
        try:
            raise ConnectError("dns")
        except ConnectError as cause:
            raise RuntimeError("download failed") from cause
    except RuntimeError as error:
        assert "connection" in describe_error(error)
    assert describe_error(OSError(errno.ENOSPC, "No space left on device")) == "Not enough free disk space."
    long = RuntimeError("x" * 500 + "\nsecond line")
    assert describe_error(long) == "x" * 160
    assert describe_error(RuntimeError()) == "RuntimeError"


# --- downloads ----------------------------------------------------------------------


class Ui:
    """`post` collects callbacks; `pump` runs them like the UI thread."""

    def __init__(self):
        self.posted: list = []

    def post(self, fn):
        self.posted.append(fn)

    def pump(self):
        while self.posted:
            self.posted.pop(0)()


def _sync(target, _name):
    target()


def test_successful_download_reports_and_is_forgotten():
    ui = Ui()
    manager = Downloads(ui.post, spawn=_sync)
    finished, done = [], []
    manager.listen(finished.append)

    def work(bar_class):
        transfer, reconstruct = _snapshot_bars(bar_class)
        _start_file(transfer, reconstruct, 10)
        reconstruct.update(10)
        # Still running for the UI until the UI thread hears about the end.
        assert manager.is_running("repo/model")
        assert manager.get("repo/model").progress == Progress(10, 10)

    assert manager.start("repo/model", "model", downloads.SPEECH, work, on_done=done.append)
    assert [item.key for item in manager.running()] == ["repo/model"]
    ui.pump()
    assert done == [None]
    assert [item.key for item in finished] == ["repo/model"]
    assert manager.get("repo/model") is None
    assert manager.running() == []


def test_failed_download_stays_visible_until_retried():
    ui = Ui()
    manager = Downloads(ui.post, spawn=_sync)
    done = []

    def failing(_bar_class):
        raise ConnectError("network is unreachable")

    manager.start("repo/model", "model", downloads.SPEECH, failing, on_done=done.append)
    ui.pump()
    item = manager.get("repo/model")
    assert item is not None and not item.running
    assert "connection" in item.error
    assert done == [item.error]
    readiness = speech_readiness("repo/model", False, item)
    assert readiness.state == FAILED and readiness.blocks
    assert item.error in block_reason(readiness)

    # Retry: a new attempt replaces the failure.
    assert manager.start("repo/model", "model", downloads.SPEECH, lambda _bar: None)
    ui.pump()
    assert manager.get("repo/model") is None


def test_one_download_per_key_and_callbacks_join_the_running_one():
    ui = Ui()
    started = []
    manager = Downloads(ui.post, spawn=lambda target, _name: started.append(target))
    first, second = [], []
    assert manager.start("k", "k", downloads.LANGUAGE, lambda _bar: None, on_done=first.append)
    assert not manager.start("k", "k", downloads.LANGUAGE, lambda _bar: None, on_done=second.append)
    assert len(started) == 1
    started[0]()
    ui.pump()
    assert first == [None] and second == [None]


def test_failed_download_keeps_the_exception_for_the_error_dialog():
    # #29: the error dialog classifies the original exception, not the short text.
    from ucho import diagnostics
    ui = Ui()
    manager = Downloads(ui.post, spawn=_sync)
    failure = ConnectError("network is unreachable")

    def failing(_bar_class):
        raise failure

    manager.start("repo/model", "model", downloads.SPEECH, failing)
    ui.pump()
    item = manager.get("repo/model")
    assert item.exception is failure
    assert diagnostics.classify(item.exception, diagnostics.MODEL_DOWNLOAD).kind == "network"

    manager.start("repo/model", "model", downloads.SPEECH, lambda _bar: None)
    ui.pump()
    assert manager.get("repo/model") is None


def test_a_failing_callback_does_not_stop_the_others(caplog):
    ui = Ui()
    manager = Downloads(ui.post, spawn=_sync)
    seen = []
    manager.listen(lambda _item: 1 / 0)
    manager.listen(seen.append)
    manager.start("k", "k", downloads.SPEAKERS, lambda _bar: None)
    ui.pump()
    assert len(seen) == 1


def test_forget_drops_only_finished_failures():
    ui = Ui()
    manager = Downloads(ui.post, spawn=lambda target, _name: None)
    manager.start("k", "k", downloads.SPEECH, lambda _bar: None)
    manager.forget("k")
    assert manager.is_running("k")


# --- readiness --------------------------------------------------------------------


def _download(running: bool, error: str | None = None, done: int = 0, total: int = 0) -> Download:
    item = Download("repo/whisper-x-ov", "whisper-x", downloads.SPEECH, running=running, error=error)
    item.counter.add_total(total)
    item.counter.advance(done)
    return item


@pytest.mark.parametrize("on_disk, download, state, blocks", [
    (True, None, READY, False),
    (None, None, UNKNOWN, False),        # not checked yet: never blocks on a guess
    (False, None, MISSING, True),
    (False, _download(False, "No connection"), FAILED, True),
    (False, _download(True), DOWNLOADING, True),
    (True, _download(True), DOWNLOADING, True),  # files are still being written
    (True, _download(False, "old failure"), READY, False),
])
def test_speech_readiness(on_disk, download, state, blocks):
    readiness = speech_readiness("repo/whisper-x-ov", on_disk, download)
    assert readiness.state == state
    assert readiness.blocks is blocks
    assert (block_reason(readiness) is not None) is blocks


def test_block_reasons_name_the_model_and_the_progress():
    missing = block_reason(speech_readiness("OpenVINO/whisper-large-v3-int8-ov", False, None))
    assert "whisper-large-v3-int8" in missing
    downloading = block_reason(speech_readiness(
        "repo/whisper-x-ov", False, _download(True, done=512 * MB, total=1024 * MB)))
    assert "50% · 512 / 1024 MB" in downloading


def test_recording_blocked_helper_tolerates_apps_without_a_gate():
    assert downloads.recording_blocked(SimpleNamespace()) is None
    assert downloads.recording_blocked(SimpleNamespace(recording_blocked=lambda check: object())) is None
    asked = []
    app = SimpleNamespace(recording_blocked=lambda check: asked.append(check) or "no model")
    assert downloads.recording_blocked(app, check=False) == "no model"
    assert asked == [False]


def test_meeting_does_not_start_without_a_speech_model(tmp_path):
    from ucho import meeting as meeting_mod

    refused = []
    app = SimpleNamespace(
        settings={"recordings_dir": tmp_path},
        recording_blocked=lambda check=True: "The speech model is not downloaded.",
        refuse_recording=refused.append,
        post=lambda fn: fn(),
    )
    controller = meeting_mod.MeetingController.__new__(meeting_mod.MeetingController)
    controller.app = app
    controller.recording = False
    assert controller.start("Standup") is False
    assert refused == ["The speech model is not downloaded."]
    assert controller.recording is False


@pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictation uses WinAPI")
def test_dictation_does_not_start_without_a_speech_model():
    from ucho import dictation

    refused, events = [], []
    app = SimpleNamespace(recording_blocked=lambda check=True: "Downloading the speech model.",
                          refuse_recording=refused.append)
    controller = dictation.DictationController.__new__(dictation.DictationController)
    controller.app = app
    controller.recording = False
    controller._emit = lambda event, data=None: events.append(event)
    assert controller.start() is False
    assert refused == ["Downloading the speech model."]
    assert controller.recording is False and events == ["state"]


def test_model_names_and_sizes():
    assert downloads.model_name("OpenVINO/whisper-large-v3-int8-ov") == "whisper-large-v3-int8"
    assert downloads.model_size("OpenVINO/whisper-large-v3-int8-ov") == "~1.6 GB"
    assert downloads.model_size("unknown/repo") == "?"


# --- fallback: recording waits only while NO speech model is ready ------------------

INT8 = "OpenVINO/whisper-large-v3-int8-ov"
FP16 = "OpenVINO/whisper-large-v3-fp16-ov"


def test_fallback_uses_the_chosen_model_when_it_is_ready():
    assert downloads.fallback_speech_model(INT8, {INT8, FP16}) == INT8


def test_fallback_while_the_chosen_model_downloads():
    # The new main model is downloading (its files may already be partly there).
    assert downloads.fallback_speech_model(FP16, {INT8, FP16}, downloading=[FP16]) == INT8
    assert downloads.fallback_speech_model(FP16, {INT8}, downloading=[FP16]) == INT8


def test_no_fallback_when_nothing_else_is_on_the_disk():
    assert downloads.fallback_speech_model(INT8, set()) is None
    assert downloads.fallback_speech_model(INT8, {INT8}, downloading=[INT8]) is None
    assert downloads.fallback_speech_model(INT8, set(), prefer=[FP16]) is None


def test_fallback_prefers_the_dictation_model_then_the_model_list():
    # Only the dictation model is on the disk.
    assert downloads.fallback_speech_model(INT8, {FP16}, prefer=[INT8, FP16]) == FP16
    # Preference order wins over the order of config.MODELS.
    assert downloads.fallback_speech_model("x/new-ov", {INT8, FP16}, prefer=[FP16]) == FP16
    assert downloads.fallback_speech_model("x/new-ov", {INT8, FP16}) == INT8
    # Never a model whose download runs, even when preferred.
    assert downloads.fallback_speech_model("x/new-ov", {INT8, FP16}, [FP16], prefer=[FP16]) == INT8


@pytest.mark.parametrize("on_disk, download, state, blocks", [
    (False, None, downloads.FALLBACK, False),
    (False, _download(True, done=1, total=4), downloads.FALLBACK, False),
    (False, _download(False, "No connection"), downloads.FALLBACK, False),
    (True, _download(True), downloads.FALLBACK, False),
])
def test_readiness_with_a_fallback_does_not_block(on_disk, download, state, blocks):
    readiness = speech_readiness("repo/whisper-x-ov", on_disk, download, fallback=INT8)
    assert readiness.state == state and readiness.blocks is blocks
    assert readiness.used == INT8
    assert block_reason(readiness) is None
    assert readiness.running is bool(download is not None and download.running)
    if download is not None and download.error:
        assert readiness.error == download.error


def test_readiness_used_model():
    assert speech_readiness(INT8, True, None, fallback=FP16).used == INT8   # ready: no fallback
    assert speech_readiness(INT8, False, None).used is None                 # blocked
    assert speech_readiness(INT8, None, None).used == INT8                  # not checked yet


def test_speech_model_for_helper():
    assert downloads.speech_model_for(SimpleNamespace(), INT8) == INT8
    app = SimpleNamespace(speech_model_for=lambda preferred: None)
    assert downloads.speech_model_for(app, INT8) is None
    app = SimpleNamespace(speech_model_for=lambda preferred: object())
    assert downloads.speech_model_for(app, INT8) == INT8


# --- meeting with a fallback model ------------------------------------------------------


class _FakeRecorder:
    def __init__(self, **_kwargs):
        self.warnings: list[str] = []
        self.levels: dict = {}

    def start(self, folder):
        folder.mkdir(parents=True)
        self.folder = folder
        (folder / "microphone.flac").write_bytes(b"")

    def stop(self):
        return {"folder": self.folder, "duration_s": 12.0,
                "tracks": {"microphone": {"file": self.folder / "microphone.flac", "device": "mic"}}}


class _MeetingSettings(dict):
    def __init__(self, folder, **values):
        super().__init__(model=INT8, device="CPU", language="auto", tracks="microphone",
                         microphone_name="", system_name="", microphone_index=None,
                         system_index=None, transcribe_after_stop=True, speaker_detection=False,
                         summary_enabled=False, auto_title=False, rename_folder=False, **values)
        self.recordings_dir = folder

    def load(self):
        pass

    def meeting_live(self):
        return False


class _MeetingApp:
    def __init__(self, folder, on_disk: set[str]):
        self.settings = _MeetingSettings(folder)
        self.on_disk = on_disk
        self.jobs: list = []
        self.refused: list = []
        self.engine = SimpleNamespace(transcribe=self._transcribe)

    def _transcribe(self, files, model, device, language, **kwargs):
        job = SimpleNamespace(model=model, cancel=threading.Event(), **kwargs)
        self.jobs.append(job)
        return job

    def post(self, fn):
        fn()

    def recording_blocked(self, check=True):
        return None if self.speech_model_for(self.settings["model"]) else "no speech model"

    def speech_model_for(self, preferred):
        return downloads.fallback_speech_model(preferred, self.on_disk,
                                               prefer=(self.settings["model"],))

    def refuse_recording(self, reason):
        self.refused.append(reason)


@pytest.fixture
def meeting_app(monkeypatch, tmp_path):
    from ucho import meeting as meeting_mod

    fake_audio = SimpleNamespace(Recorder=_FakeRecorder)
    monkeypatch.setitem(sys.modules, "ucho.audio", fake_audio)
    import ucho

    monkeypatch.setattr(ucho, "audio", fake_audio, raising=False)
    monkeypatch.setattr(meeting_mod.transcribe, "model_downloaded", lambda _m: True)
    monkeypatch.setattr(meeting_mod.transcribe, "genai_version", lambda: "test")

    def make(on_disk):
        app = _MeetingApp(tmp_path, on_disk)
        controller = meeting_mod.MeetingController(app)
        events: list = []
        controller.listen(lambda event, data: events.append((event, data)))
        return app, controller, events

    return make


def test_meeting_records_and_transcribes_with_the_fallback_model(meeting_app):
    import json

    app, controller, events = meeting_app({FP16})   # the chosen INT8 is not on the disk
    assert controller.start("Standup")
    assert controller.session_model == FP16
    notices = [data for event, data in events if event == "notice"]
    assert notices and "whisper-large-v3-fp16" in notices[0] and "whisper-large-v3-int8" in notices[0]
    folder = controller.recording_dir
    controller.stop()
    assert json.loads((folder / "meta.json").read_text(encoding="utf-8"))["model"] == FP16
    assert [job.model for job in app.jobs] == [FP16]
    assert controller.last_meta["model"] == FP16


def test_queued_session_keeps_the_model_it_was_recorded_with(meeting_app):
    app, controller, _events = meeting_app({FP16})
    controller.transcribing = True          # another session is being transcribed
    assert controller.start("Standup")
    controller.stop()
    assert controller.queued == 1 and app.jobs == []
    app.on_disk.add(INT8)                   # the chosen model arrived meanwhile
    controller.transcribing = False
    controller._advance()
    assert [job.model for job in app.jobs] == [FP16]


def test_transcribe_again_uses_the_chosen_model_when_it_is_ready(meeting_app, tmp_path):
    app, controller, _events = meeting_app({FP16, INT8})
    folder = tmp_path / "old-session"
    folder.mkdir()
    assert controller.transcribe(folder, {"microphone": folder / "microphone.flac"}, {"model": FP16})
    assert [job.model for job in app.jobs] == [INT8]


def test_transcribe_again_falls_back_while_the_chosen_model_is_missing(meeting_app, tmp_path):
    app, controller, _events = meeting_app({FP16})
    folder = tmp_path / "old-session"
    folder.mkdir()
    assert controller.transcribe(folder, {"microphone": folder / "microphone.flac"}, {})
    assert [job.model for job in app.jobs] == [FP16]


def test_meeting_is_blocked_only_without_any_speech_model(meeting_app):
    app, controller, _events = meeting_app(set())
    assert controller.start("Standup") is False
    assert app.refused == ["no speech model"]


@pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictation uses WinAPI")
def test_dictation_uses_its_own_model_when_only_that_one_is_on_the_disk():
    from ucho import config, dictation

    settings = config.Settings.__new__(config.Settings)
    settings._data = {"model": INT8, "dictation_model": FP16, "device": "GPU"}
    on_disk = {FP16}
    app = SimpleNamespace(
        settings=settings,
        speech_model_for=lambda preferred: downloads.fallback_speech_model(
            preferred, on_disk, prefer=(INT8, FP16)),
    )
    app.recording_blocked = lambda check=True: (
        None if app.speech_model_for(INT8) else "no speech model")
    controller = dictation.DictationController.__new__(dictation.DictationController)
    controller.app = app
    controller._reported_missing = None
    assert controller.model_key() == (FP16, "GPU")
    assert downloads.recording_blocked(app) is None      # dictation is not blocked
    # The other way round: own model missing, the meeting model on the disk.
    on_disk.clear()
    on_disk.add(INT8)
    assert controller.model_key() == (INT8, "GPU")
