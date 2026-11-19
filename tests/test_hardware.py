# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Rule table, step-down and settings for ucho.hardware on fake hardware profiles."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sys

import pytest

from ucho import config, hardware
from ucho.hardware import (
    LARGE_FP16,
    LARGE_INT8,
    TURBO_INT4,
    TURBO_INT8,
    GraphicsAdapter,
    HardwareProfile,
    OvDevice,
)

CPU = OvDevice("CPU", "Some CPU")
ARC_140V = OvDevice("GPU", "Intel(R) Arc(TM) 140V GPU (16GB) (iGPU)", "integrated", 16.5, "20.4.4")
IRIS_XE = OvDevice("GPU", "Intel(R) Iris(R) Xe Graphics", "integrated", 6.0, "12.0.0")
UHD_620 = OvDevice("GPU", "Intel(R) UHD Graphics 620", "integrated", 3.0, "9.1.0")
NPU = OvDevice("NPU", "Intel(R) AI Boost", "integrated", 16.0, driver="1004512")


def make(ram=16.0, installed=None, cores=8, logical=None, avx512=False, devices=(CPU,),
         adapters=()) -> HardwareProfile:
    return HardwareProfile(
        ram_total_gb=ram, ram_free_gb=ram / 2, ram_installed_gb=installed,
        cpu_name="Test CPU", logical_cores=logical or cores, physical_cores=cores,
        avx2=True, avx512=avx512, devices=list(devices), adapters=list(adapters),
    )


# Profile, expected rule, model, device, dictation model.
CASES = [
    pytest.param(make(31.6, 32, devices=(CPU, ARC_140V, NPU)),
                 "gpu_16", LARGE_INT8, "GPU", LARGE_FP16, id="arc-140v-32gb"),
    pytest.param(make(15.6, None, cores=4, devices=(CPU, IRIS_XE)),
                 "gpu_16", LARGE_INT8, "GPU", LARGE_FP16, id="iris-xe-16gb-visible-only"),
    pytest.param(make(7.8, 8, cores=4, devices=(CPU, IRIS_XE)),
                 "gpu_8", LARGE_INT8, "GPU", LARGE_INT8, id="iris-xe-8gb"),
    pytest.param(make(11.8, 12, cores=4, devices=(CPU, IRIS_XE)),
                 "gpu_8", LARGE_INT8, "GPU", LARGE_INT8, id="iris-xe-12gb"),
    pytest.param(make(31.8, 32, cores=6, avx512=True),
                 "cpu_strong", LARGE_INT8, "CPU", TURBO_INT8, id="no-gpu-avx512-6-cores"),
    pytest.param(make(15.7, 16, cores=8, adapters=(GraphicsAdapter("NVIDIA GeForce RTX 4060"),)),
                 "cpu_strong", LARGE_INT8, "CPU", TURBO_INT8, id="nvidia-8-cores-16gb"),
    pytest.param(make(15.7, 16, cores=4, logical=8),
                 "cpu_weak", TURBO_INT8, "CPU", TURBO_INT4, id="4-cores-8-threads-16gb"),
    pytest.param(make(11.8, 12, cores=12),
                 "cpu_weak", TURBO_INT8, "CPU", TURBO_INT4, id="12-cores-12gb"),
    pytest.param(make(15.7, 16, cores=4, devices=(CPU, UHD_620)),
                 "cpu_weak", TURBO_INT8, "CPU", TURBO_INT4, id="old-uhd-620"),
    pytest.param(make(5.8, 6, cores=4, devices=(CPU, IRIS_XE)),
                 "low_ram", TURBO_INT4, "CPU", TURBO_INT4, id="6gb-with-gpu"),
    pytest.param(make(3.8, None, cores=2),
                 "low_ram", TURBO_INT4, "CPU", TURBO_INT4, id="4gb-no-gpu"),
]


@pytest.mark.parametrize("profile, rule, model, device, dictation", CASES)
def test_rule_table(profile, rule, model, device, dictation):
    rec = hardware.recommend(profile)
    assert (rec.rule, rec.model, rec.device, rec.dictation_model) == (rule, model, device, dictation)
    assert rec.reasons or rec.warnings


def test_every_model_in_rules_is_known():
    for rule in hardware.RULES.values():
        assert rule.model in config.MODELS
        assert rule.dictation_model in config.MODELS
        assert config.split_model_id(rule.model)[0] in config.REVISIONS
        assert rule.step_down is None or rule.step_down in hardware.RULES


def test_low_ram_warns():
    rec = hardware.recommend(make(5.8, 6))
    assert rec.warnings and "8 GB" in rec.warnings[0]


def test_npu_and_nvidia_are_mentioned():
    rec = hardware.recommend(make(31.6, 32, devices=(CPU, ARC_140V, NPU),
                                  adapters=(GraphicsAdapter("NVIDIA GeForce RTX 4070"),)))
    text = " ".join(rec.reasons)
    assert "NPU" in text and "NVIDIA GeForce RTX 4070" in text


def test_discrete_gpu_preferred_over_integrated():
    discrete = OvDevice("GPU.1", "Intel(R) Arc(TM) A770 Graphics", "discrete", 16.0, "12.55.8")
    integrated = OvDevice("GPU.0", "Intel(R) UHD Graphics 770", "integrated", 6.0, "12.2.0")
    rec = hardware.recommend(make(31.8, 32, devices=(CPU, integrated, discrete)))
    assert rec.device == "GPU.1"


def test_unknown_gpu_architecture_counts_as_usable():
    gpu = OvDevice("GPU", "Intel(R) Graphics")
    assert hardware.recommend(make(15.7, 16, devices=(CPU, gpu))).rule == "gpu_16"


def test_installed_ram_wins_over_visible():
    # 16 GB installed, only 13.5 GB visible because the firmware reserved memory.
    assert hardware.recommend(make(13.5, 16, devices=(CPU, IRIS_XE))).rule == "gpu_16"


# --- step down ---------------------------------------------------------------


def test_fast_enough_keeps_recommendation():
    p = make(31.6, 32, devices=(CPU, ARC_140V))
    rec = hardware.after_benchmark(hardware.recommend(p), p, 8.9)
    assert rec.rule == "gpu_16" and "8.9" in rec.reasons[-1]


def test_numbers_use_decimal_comma_in_polish():
    from ucho import i18n

    p = make(31.6, 32, devices=(CPU, ARC_140V))
    i18n.set_language("pl")
    try:
        rec = hardware.after_benchmark(hardware.recommend(p), p, 8.9)
    finally:
        i18n.set_language("en")
    assert "8,9" in rec.reasons[-1]


def test_threshold_is_inclusive():
    p = make(31.8, 32, cores=8)
    assert hardware.after_benchmark(hardware.recommend(p), p, hardware.MIN_SPEED).rule == "cpu_strong"


@pytest.mark.parametrize("start, expected", [
    ("cpu_strong", "cpu_weak"),
    ("cpu_weak", "low_ram"),
    ("gpu_16", "gpu_turbo"),
    ("gpu_8", "gpu_turbo"),
    ("gpu_turbo", "gpu_turbo_int4"),
])
def test_slow_steps_down_one_row(start, expected):
    p = make(31.8, 32, devices=(CPU, ARC_140V))
    rec = hardware.Recommendation("", "", "", rule=start)
    lower = hardware.after_benchmark(rec, p, 1.2)
    assert lower.rule == expected
    assert lower.model == hardware.RULES[expected].model
    assert "1.2" in lower.reasons[-1]


def test_gpu_step_down_stays_on_the_found_gpu():
    discrete = OvDevice("GPU.1", "Intel(R) Arc(TM) A380 Graphics", "discrete", 6.0, "12.55.8")
    p = make(15.7, 16, devices=(CPU, discrete))
    lower = hardware.after_benchmark(hardware.recommend(p), p, 0.9)
    assert (lower.model, lower.device) == (TURBO_INT8, "GPU.1")


@pytest.mark.parametrize("bottom", ["low_ram", "gpu_turbo_int4"])
def test_bottom_row_stays_and_warns(bottom):
    p = make(31.8, 32, devices=(CPU, ARC_140V))
    rec = hardware.Recommendation(TURBO_INT4, "CPU", TURBO_INT4, rule=bottom)
    assert hardware.step_down(rec, p) is None
    kept = hardware.after_benchmark(rec, p, 0.7)
    assert kept.rule == bottom and kept.warnings


# --- profile -----------------------------------------------------------------


def test_profile_without_openvino(monkeypatch):
    # None in sys.modules makes "import openvino" raise ImportError.
    monkeypatch.setitem(sys.modules, "openvino", None)
    p = hardware.profile()
    assert p.devices == [] and p.openvino_version == ""
    assert p.logical_cores >= 1
    # The Windows rules are under test here; the Apple Silicon ones have their own tests.
    assert hardware.recommend(dataclasses.replace(p, system="win32")).device == "CPU"


def test_profile_survives_failing_probes(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("probe failed")

    for name in ("_ram", "_cpu_name", "_physical_cores", "_cpu_feature", "_adapters", "_ov_devices"):
        monkeypatch.setattr(hardware, name, boom)
    p = hardware.profile()
    assert p.ram_total_gb == 0.0 and p.devices == []
    assert hardware.recommend(dataclasses.replace(p, system="win32")).rule == "low_ram"


def test_profile_reads_this_machine():
    p = hardware.profile()
    if sys.platform == "win32":
        assert p.ram_total_gb > 1 and p.cpu_name


# --- settings ----------------------------------------------------------------


def _settings(tmp_path, **values) -> config.Settings:
    file = tmp_path / "settings.json"
    if values:
        file.write_text(json.dumps(values), encoding="utf-8")
    return config.Settings(file)


def test_new_keys_are_in_defaults():
    assert "dictation_model" in config.DEFAULTS and "hardware_profile" in config.DEFAULTS
    assert config.DEFAULTS["dictation_model"] in config.MODELS


def test_save_writes_recommendation(tmp_path):
    p = make(15.7, 16, cores=4)
    rec = hardware.recommend(p)
    settings = _settings(tmp_path)
    changed = hardware.save(settings, p, rec)
    assert set(changed) == {"model", "device", "dictation_model"}
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert (saved["model"], saved["device"], saved["dictation_model"]) == (TURBO_INT8, "CPU", TURBO_INT4)
    assert saved["hardware_profile"]["recommended"]["model"] == TURBO_INT8
    assert saved["hardware_profile"]["ram_gb"] == 16


def test_keep_choice_respects_manual_change(tmp_path):
    p = make(31.6, 32, devices=(CPU, ARC_140V))
    previous = {"recommended": {"model": TURBO_INT8, "device": "CPU", "dictation_model": TURBO_INT4}}
    # The user changed the model by hand; device and dictation model are still automatic.
    settings = _settings(tmp_path, model=LARGE_FP16, device="CPU", dictation_model=TURBO_INT4,
                         hardware_profile=previous)
    changed = hardware.save(settings, p, hardware.recommend(p), keep_choice=True)
    assert set(changed) == {"device", "dictation_model"}
    assert settings["model"] == LARGE_FP16 and settings["device"] == "GPU"


def test_keep_choice_without_previous_profile_changes_nothing(tmp_path):
    p = make(31.6, 32, devices=(CPU, ARC_140V))
    settings = _settings(tmp_path, model=TURBO_INT4, device="CPU")
    assert hardware.save(settings, p, hardware.recommend(p), keep_choice=True) == []
    assert settings["model"] == TURBO_INT4
    assert settings["hardware_profile"]["gpu"] == ARC_140V.name


def test_summary_is_json_with_benchmark():
    p = make(31.6, 32, devices=(CPU, ARC_140V, NPU))
    result = hardware.BenchmarkResult(LARGE_INT8, "GPU", 10.0, 1.8, 1.12, 50)
    data = json.loads(json.dumps(hardware.summary(p, hardware.recommend(p), result)))
    assert data["benchmark"]["speed"] == pytest.approx(8.93, abs=0.01)
    assert data["npu"] == NPU.name


def test_synthetic_speech_passes_silence_threshold():
    np = pytest.importorskip("numpy")
    audio = hardware.synthetic_speech(3.0)
    assert audio.dtype == np.float32 and audio.size == 48_000
    assert np.abs(audio).max() <= 0.31
    assert np.array_equal(audio, hardware.synthetic_speech(3.0))


# --- language model recommendation (#24) ---

A770 = OvDevice("GPU.1", "Intel(R) Arc(TM) A770 Graphics", "discrete", 16.0, "12.55.8")
A380 = OvDevice("GPU.1", "Intel(R) Arc(TM) A380 Graphics", "discrete", 6.0, "12.56.5")
L, M, S = hardware.LLM_LARGE, hardware.LLM_MEDIUM, hardware.LLM_SMALL

def test_llm_constants():
    assert (L, M, S) == ("OpenVINO/Qwen3-8B-int4-ov", "OpenVINO/Qwen3-4B-int4-ov",
                         "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov")
    assert all(m in config.LLM_MODELS for m in (L, M, S))


@pytest.mark.parametrize("p,model", [
    (make(31.6, 32, devices=(CPU, ARC_140V)), L),
    (make(15.6, None, devices=(CPU, IRIS_XE)), L),
    (make(15.7, 16, devices=(CPU, IRIS_XE)), L),
    (make(11.8, 12, devices=(CPU, IRIS_XE)), M),
    (make(7.8, 8, devices=(CPU, IRIS_XE)), M),
    (make(7.8, 8, devices=(CPU, A770)), L),
    (make(7.8, 8, devices=(CPU, A380)), M),
    (make(31.8, 32), M),
    (make(63.8, 64), M),
    (make(15.7, 16, devices=(CPU, UHD_620)), M),
    (make(15.7, 16, adapters=(GraphicsAdapter("NVIDIA GeForce RTX 4060"),)), M),
    (make(5.8, 6, devices=(CPU, IRIS_XE)), S),
    (make(3.8, None), S),
    (make(7.0, 8), M),
    (HardwareProfile(), S),
])
def test_llm_rules(p, model):
    rec = hardware.recommend_llm(p)
    assert rec.model == model
    assert rec.reasons and all(isinstance(r, str) and r for r in rec.reasons)


def test_llm_nvidia_and_amd_named():
    p = make(15.7, 16, adapters=(GraphicsAdapter("NVIDIA GeForce RTX 4070"),
                                 GraphicsAdapter("AMD Radeon RX 7600")))
    text = " ".join(hardware.recommend_llm(p).reasons)
    assert "NVIDIA GeForce RTX 4070" in text and "AMD Radeon RX 7600" in text


def test_llm_intel_adapter_not_reported_as_unusable():
    p = make(15.7, 16, devices=(CPU, IRIS_XE),
             adapters=(GraphicsAdapter("Intel(R) Iris(R) Xe Graphics"),))
    reasons = hardware.recommend_llm(p).reasons
    assert not any("OpenVINO" in r and "Iris" in r for r in reasons)


def test_llm_gpu_named_in_reasons():
    text = " ".join(hardware.recommend_llm(make(31.6, 32, devices=(CPU, A770))).reasons)
    assert "A770" in text


def test_llm_cli_prints_only_the_id(monkeypatch, capsys):
    monkeypatch.setattr(hardware, "profile", lambda: make(31.6, 32, devices=(CPU, ARC_140V)))
    assert hardware.main(["--llm"]) == 0
    assert capsys.readouterr().out.strip() == L


def test_llm_cli_does_not_save(monkeypatch, capsys):
    monkeypatch.setattr(hardware, "profile", lambda: make(3.8))
    called = []
    monkeypatch.setattr(hardware, "save", lambda *a, **k: called.append(1) or [])
    assert hardware.main(["--llm"]) == 0
    assert capsys.readouterr().out.strip() == S and not called


def test_llm_polish_translations_exist():
    pl = json.loads((Path(hardware.__file__).parent / "locale" / "pl.json").read_text(encoding="utf-8"))
    assert "Recommended for this computer: {model}. {reason}" in pl
    assert " (recommended)" in pl
