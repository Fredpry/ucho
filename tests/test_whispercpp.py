# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS whisper.cpp backend: language narrowing, timestamps, routing, model loading.

A fake `pywhispercpp` module stands in for the real C++ bindings, so tests run
offline on Windows with no package and no model files.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from ucho import config, downloads, parakeet, transcribe, whispercpp
from ucho.merge import Segment

RATE = 16_000


def speech(seconds: float = 2.0, level: float = 0.25) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def install_fake_pywhispercpp(
    monkeypatch,
    *,
    auto_result=(("ko", 0.5), {"ko": 0.5, "pl": 0.4, "en": 0.1}),
    segments=None,
    log_text: str = "ggml_metal_init: allocating\nwhisper_backend_init_gpu: using MTL0 backend\n",
):
    """Installs fake `pywhispercpp`, `pywhispercpp.model`, and `pywhispercpp.utils`."""
    calls: list[dict] = []
    download_calls: list[tuple] = []
    if segments is None:
        segments = [SimpleNamespace(t0=150, t1=420, text=" Dzień dobry ")]

    class FakeModel:
        def __init__(self, model_path, **kwargs):
            calls.append({"kind": "init", "model_path": model_path, "kwargs": kwargs})
            log_file = kwargs.get("redirect_whispercpp_logs_to")
            if log_file and log_text:
                # Like pywhispercpp 1.5.1 utils.redirect_stderr: mode "w".
                with open(log_file, "w", encoding="utf-8") as handle:
                    handle.write(log_text)

        def auto_detect_language(self, audio):
            calls.append({"kind": "auto_detect_language", "size": int(audio.size)})
            return auto_result

        def transcribe(self, audio, **kwargs):
            # The real binding (pywhispercpp 1.5.1) sets each parameter on a C++
            # struct and rejects None for a string field with TypeError.
            if "initial_prompt" in kwargs and not isinstance(kwargs["initial_prompt"], str):
                raise TypeError("initial_prompt must be str")
            calls.append({"kind": "transcribe", "size": int(audio.size), "kwargs": kwargs})
            return list(segments)

    def download_model(*args, **kwargs):
        download_calls.append((args, kwargs))
        raise AssertionError("pywhispercpp.utils.download_model must never be called")

    pkg = ModuleType("pywhispercpp")
    model_mod = ModuleType("pywhispercpp.model")
    model_mod.Model = FakeModel
    utils_mod = ModuleType("pywhispercpp.utils")
    utils_mod.download_model = download_model
    pkg.model = model_mod
    pkg.utils = utils_mod

    monkeypatch.setitem(sys.modules, "pywhispercpp", pkg)
    monkeypatch.setitem(sys.modules, "pywhispercpp.model", model_mod)
    monkeypatch.setitem(sys.modules, "pywhispercpp.utils", utils_mod)
    return calls, download_calls


def setup_model_file(monkeypatch, tmp_path: Path, repo_id: str = whispercpp.MODEL_LARGE) -> Path:
    _, filename = config.split_model_id(repo_id)
    assert filename is not None
    model_file = tmp_path / filename
    model_file.write_bytes(b"ggml")
    monkeypatch.setattr(transcribe, "model_path", lambda repo, download=False: tmp_path)
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "ucho.log")
    return model_file


def test_auto_language_picks_highest_allowed_and_reports_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LANGUAGES", {"auto": None, "pl": "<|pl|>", "en": "<|en|>"})
    setup_model_file(monkeypatch, tmp_path)
    calls, _ = install_fake_pywhispercpp(
        monkeypatch,
        auto_result=(("ko", 0.5), {"ko": 0.5, "pl": 0.4, "en": 0.1}),
    )

    engine = whispercpp.Engine(whispercpp.MODEL_LARGE)
    result = engine.generate(speech(2.0), language=None)

    transcribe_calls = [c for c in calls if c["kind"] == "transcribe"]
    assert len(transcribe_calls) == 1
    assert transcribe_calls[0]["kwargs"]["language"] == "pl"
    assert result.languages == ["<|pl|>"]
    assert transcribe._detected_language(result) == "pl"


def test_auto_language_fallback_and_dict_result_and_forced_token(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config, "LANGUAGES", {"auto": None, "pl": "<|pl|>", "en": "<|en|>"})
    setup_model_file(monkeypatch, tmp_path)
    calls, _ = install_fake_pywhispercpp(
        monkeypatch,
        auto_result={"ko": 0.9, "de": 0.1},
    )

    engine = whispercpp.Engine(whispercpp.MODEL_LARGE)
    with caplog.at_level("WARNING"):
        res_fallback = engine.generate(speech(1.0), language="auto")
    assert res_fallback.languages == ["<|pl|>"]
    assert "falling back" in caplog.text.lower()

    res_forced = engine.generate(speech(1.0), language="<|EN|>")
    assert res_forced.languages == ["<|en|>"]
    transcribe_calls = [c for c in calls if c["kind"] == "transcribe"]
    assert [c["kwargs"]["language"] for c in transcribe_calls] == ["pl", "en"]


def test_timestamps_joined_text_and_empty_audio_skips_model(monkeypatch, tmp_path):
    setup_model_file(monkeypatch, tmp_path)
    calls, _ = install_fake_pywhispercpp(
        monkeypatch,
        segments=[
            SimpleNamespace(t0=150, t1=420, text=" Dzień dobry "),
            SimpleNamespace(t0=420, t1=450, text="   "),
            SimpleNamespace(t0=460, t1=610, text=" Zaczynamy. "),
        ],
    )

    engine = whispercpp.Engine(whispercpp.MODEL_LARGE)
    empty = engine.generate(np.array([], dtype=np.float32), language="pl")
    assert empty.chunks == []
    assert empty.texts == []
    assert empty.languages == []
    assert calls == []

    result = engine.generate(speech(7.0), language="pl")
    assert len(result.chunks) == 2
    assert result.chunks[0].start_ts == pytest.approx(1.5)
    assert result.chunks[0].end_ts == pytest.approx(4.2)
    assert result.chunks[0].text == "Dzień dobry"
    assert result.chunks[1].start_ts == pytest.approx(4.6)
    assert result.chunks[1].end_ts == pytest.approx(6.1)
    assert result.texts == ["Dzień dobry Zaczynamy."]
    assert str(result) == "Dzień dobry Zaczynamy."

    engine.release()
    assert engine._model is None
    engine.release()  # idempotent when already released


def test_create_engine_routes_by_model():
    assert isinstance(
        transcribe.create_engine(whispercpp.MODEL_LARGE, "CPU"),
        whispercpp.Engine,
    )
    assert isinstance(
        transcribe.create_engine("OpenVINO/whisper-large-v3-int8-ov", "GPU"),
        transcribe.Engine,
    )


def test_offered_models_filters_by_platform_and_hidden(monkeypatch):
    monkeypatch.setattr(parakeet, "available", lambda: False)
    expected_ov = [
        k for k, info in config.MODELS.items()
        if info.get("backend") is None
    ]
    assert len(expected_ov) == 4

    monkeypatch.setattr(sys, "platform", "win32")
    win_models = transcribe.offered_models()
    assert all(":" not in m for m in win_models)
    assert win_models == expected_ov

    monkeypatch.setattr(sys, "platform", "darwin")
    mac_models = transcribe.offered_models()
    assert whispercpp.MODEL_LARGE in mac_models
    assert whispercpp.MODEL_TURBO in mac_models
    assert whispercpp.MODEL_TINY not in mac_models
    assert whispercpp.MODEL_BASE not in mac_models


@pytest.mark.parametrize("backend", ["parakeet", "whispercpp"])
def test_iter_transcribe_session_with_fake_engines(backend, monkeypatch, tmp_path):
    import soundfile as sf

    wav = tmp_path / "microphone.wav"
    sf.write(str(wav), speech(3.0), RATE)
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "ucho.log")

    if backend == "parakeet":
        class FakeStream:
            def __init__(self):
                self.result = None

            def accept_waveform(self, rate, audio):
                assert rate == RATE

        class FakeRecognizer:
            def create_stream(self):
                return FakeStream()

            def decode_stream(self, stream):
                stream.result = SimpleNamespace(
                    tokens=[" Dzień", " dobry", "."],
                    timestamps=[0.1, 0.4, 0.7],
                    durations=[0.2, 0.2, 0.1],
                    text=" Dzień dobry.",
                )

        monkeypatch.setattr(transcribe, "model_path", lambda repo, download=False: tmp_path)
        engine = parakeet.Engine(
            device="GPU",
            recognizer_factory=lambda folder, threads, files: FakeRecognizer(),
        )
    else:
        setup_model_file(monkeypatch, tmp_path)
        install_fake_pywhispercpp(
            monkeypatch,
            segments=[SimpleNamespace(t0=10, t1=120, text=" Dzień dobry. ")],
        )
        engine = whispercpp.Engine(whispercpp.MODEL_LARGE)

    segments = transcribe.drain(
        transcribe.iter_transcribe_session({"microphone": wav}, engine, language_code="auto")
    )
    assert len(segments) == 1
    assert isinstance(segments[0], Segment)
    assert segments[0].text == "Dzień dobry."
    assert segments[0].speaker == "me"


def test_load_requires_existing_file_and_passes_absolute_path(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "model_path", lambda repo, download=False: tmp_path)
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "ucho.log")
    calls, download_calls = install_fake_pywhispercpp(monkeypatch)

    engine = whispercpp.Engine(whispercpp.MODEL_LARGE)
    with pytest.raises(FileNotFoundError):
        engine.load()
    assert calls == []
    assert download_calls == []

    model_file = tmp_path / "ggml-large-v3-q5_0.bin"
    model_file.write_bytes(b"ggml")
    engine.load()
    engine.load()  # cached on second call

    init_calls = [c for c in calls if c["kind"] == "init"]
    assert len(init_calls) == 1
    passed_path = init_calls[0]["model_path"]
    assert isinstance(passed_path, str)
    assert Path(passed_path).is_absolute()
    assert passed_path == str(model_file)
    assert download_calls == []


def test_split_model_id_cases():
    assert config.split_model_id("ggerganov/whisper.cpp:ggml-large-v3-q5_0.bin") == (
        "ggerganov/whisper.cpp",
        "ggml-large-v3-q5_0.bin",
    )
    assert config.split_model_id("OpenVINO/whisper-large-v3-int8-ov") == (
        "OpenVINO/whisper-large-v3-int8-ov",
        None,
    )
    assert config.split_model_id("") == ("", None)
    assert config.split_model_id("C:\\x") == ("C:\\x", None)
    assert config.split_model_id("C:/x:foo") == ("C:/x:foo", None)
    assert config.split_model_id("a:b") == ("a:b", None)
    assert config.split_model_id("ggerganov/whisper.cpp:") == ("ggerganov/whisper.cpp:", None)


def test_device_fallback_from_whispercpp_log(monkeypatch, tmp_path):
    setup_model_file(monkeypatch, tmp_path)

    # A long CPU log first, then a shorter Metal log: the second load must
    # see its own lines although the library truncates the file.
    cpu_log = ("whisper_backend_init_gpu: no GPU found\n"
               "ggml_metal_init: error: failed to create command queue\n"
               "whisper_backend_init: using BLAS backend\n") * 5
    install_fake_pywhispercpp(monkeypatch, log_text=cpu_log)
    cpu_engine = whispercpp.Engine(whispercpp.MODEL_LARGE)
    cpu_engine.load()
    assert cpu_engine.requested_device == "METAL"
    assert cpu_engine.device == "CPU"

    install_fake_pywhispercpp(monkeypatch, log_text="whisper_backend_init_gpu: using MTL0 backend\n")
    metal_engine = whispercpp.Engine(whispercpp.MODEL_LARGE)
    metal_engine.load()
    assert metal_engine.device == "METAL"

    install_fake_pywhispercpp(monkeypatch, log_text="whisper_backend_init_gpu: using Metal backend\n")
    old_format = whispercpp.Engine(whispercpp.MODEL_LARGE)
    old_format.load()
    assert old_format.device == "METAL"


def test_initial_prompt_always_passed_so_a_stale_one_is_cleared(monkeypatch, tmp_path):
    setup_model_file(monkeypatch, tmp_path)
    calls, _ = install_fake_pywhispercpp(monkeypatch)

    engine = whispercpp.Engine(whispercpp.MODEL_LARGE)
    # pywhispercpp keeps transcribe() parameters for later calls, so a call
    # without a prompt must pass an empty one explicitly.
    engine.generate(speech(1.0), language="pl", prompt="Ala ma kota")
    engine.generate(speech(1.0), language="pl", prompt=None)
    engine.generate(speech(1.0), language="pl", prompt="")

    transcribe_calls = [c["kwargs"] for c in calls if c["kind"] == "transcribe"]
    assert transcribe_calls[0]["initial_prompt"] == "Ala ma kota"
    assert transcribe_calls[1]["initial_prompt"] == ""
    assert transcribe_calls[2]["initial_prompt"] == ""


def test_short_model_and_downloads_name_and_fallback(monkeypatch):
    settings_screen = pytest.importorskip("ucho.settings_screen")  # needs customtkinter
    assert settings_screen.short_model(whispercpp.MODEL_LARGE) == "large-v3-q5_0"
    assert settings_screen.short_model(config.DEFAULT_MODEL) == "large-v3-int8"
    assert downloads.model_name(whispercpp.MODEL_LARGE) == "ggml-large-v3-q5_0"
    assert downloads.model_name(config.DEFAULT_MODEL) == "whisper-large-v3-int8"

    monkeypatch.setattr(sys, "platform", "win32")
    assert downloads.fallback_speech_model(
        config.DEFAULT_MODEL,
        {whispercpp.MODEL_LARGE},
    ) is None

    monkeypatch.setattr(sys, "platform", "darwin")
    assert downloads.fallback_speech_model(
        whispercpp.MODEL_LARGE,
        {whispercpp.MODEL_TINY},
    ) is None
    assert downloads.fallback_speech_model(
        whispercpp.MODEL_LARGE,
        {whispercpp.MODEL_TURBO, whispercpp.MODEL_TINY},
    ) == whispercpp.MODEL_TURBO


def test_model_path_and_downloaded_with_split_key(monkeypatch, tmp_path, caplog):
    seen: list[tuple[tuple, dict]] = []
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    def fake_snapshot_download(*args, **kwargs):
        seen.append((args, kwargs))
        return str(snapshot_dir)

    fake_hf = ModuleType("huggingface_hub")
    fake_hf.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    with caplog.at_level("WARNING"):
        assert transcribe.model_downloaded(whispercpp.MODEL_LARGE) is False
    assert caplog.text == ""
    assert seen[0][0] == (whispercpp.REPO,)
    assert seen[0][1]["revision"] == config.REVISIONS[whispercpp.REPO]
    assert seen[0][1]["allow_patterns"] == ["ggml-large-v3-q5_0.bin"]
    assert seen[0][1]["local_files_only"] is True

    (snapshot_dir / "ggml-large-v3-q5_0.bin").write_bytes(b"ggml")
    assert transcribe.model_downloaded(whispercpp.MODEL_LARGE) is True
    assert transcribe.model_path(whispercpp.MODEL_LARGE, download=True) == snapshot_dir
    assert seen[-1][0] == (whispercpp.REPO,)
    assert seen[-1][1]["revision"] == config.REVISIONS[whispercpp.REPO]
    assert seen[-1][1]["allow_patterns"] == ["ggml-large-v3-q5_0.bin"]


def test_missing_pywhispercpp_raises_clear_error(monkeypatch, tmp_path):
    setup_model_file(monkeypatch, tmp_path)
    monkeypatch.delitem(sys.modules, "pywhispercpp", raising=False)
    monkeypatch.delitem(sys.modules, "pywhispercpp.model", raising=False)
    real_import = builtins.__import__

    def no_pywhispercpp(name, *args, **kwargs):
        if name == "pywhispercpp" or name.startswith("pywhispercpp."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pywhispercpp)
    engine = whispercpp.Engine(whispercpp.MODEL_LARGE)
    with pytest.raises(RuntimeError, match="pywhispercpp"):
        engine.load()


def test_model_downloaded_still_logs_unexpected_errors_for_openvino_keys(monkeypatch, caplog):
    def broken_snapshot_download(*args, **kwargs):
        raise FileNotFoundError("broken cache entry")

    fake_hf = ModuleType("huggingface_hub")
    fake_hf.snapshot_download = broken_snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    with caplog.at_level("WARNING"):
        assert transcribe.model_downloaded(config.DEFAULT_MODEL) is False
    assert "Checking model" in caplog.text
