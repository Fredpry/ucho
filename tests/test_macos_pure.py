# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tests for the pure-stdlib macOS platform modules and Apple Silicon hardware rules."""

from __future__ import annotations

import inspect
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucho import hardware, paths, platforms, theme
from ucho.platforms import base
from ucho.platforms.macos import fonts, shell, sounds, sysinfo

VM_STAT_M1 = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              131072.
Pages active:                            196608.
Pages inactive:                          131072.
Pages speculative:                         4096.
Pages throttled:                              0.
Pages wired down:                         65536.
"""


def _fake_runner(outputs: dict[tuple[str, ...], str | Exception]):
    def _run(args: list[str]) -> str:
        key = tuple(args)
        if key not in outputs:
            raise subprocess.CalledProcessError(1, args)
        val = outputs[key]
        if isinstance(val, Exception):
            raise val
        return val

    return _run


def test_sysinfo_m1_8gb_and_m3_pro_18gb():
    m1_outputs: dict[tuple[str, ...], str | Exception] = {
        ("/usr/sbin/sysctl", "-n", "hw.memsize"): "8589934592\n",
        ("/usr/bin/vm_stat",): VM_STAT_M1,
        ("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"): "Apple M1\n",
        ("/usr/sbin/sysctl", "-n", "hw.physicalcpu"): "8\n",
        ("/usr/sbin/sysctl", "-n", "hw.optional.arm64"): "1\n",
        ("/usr/sbin/sysctl", "-n", "machdep.cpu.leaf7_features"): "",
        ("/usr/bin/defaults", "read", "-g", "AppleLanguages"): '(\n    "pl-PL",\n    en\n)\n',
        ("/usr/bin/defaults", "read", "com.apple.universalaccess", "reduceMotion"): "1\n",
    }
    info_m1 = sysinfo.MacSystemInfo(run=_fake_runner(m1_outputs))
    total, avail, installed = info_m1.ram_gb()
    assert total == pytest.approx(8.0)
    assert installed == pytest.approx(8.0)
    assert avail == pytest.approx(4.0)
    assert info_m1.cpu_name() == "Apple M1"
    assert info_m1.physical_cores() == 8
    assert info_m1.cpu_features() == {"arm64": True, "avx2": False, "avx512": False}
    assert info_m1.adapters() == [hardware.GraphicsAdapter("Apple GPU (Metal)")]
    assert info_m1.ui_language() == "pl"
    assert info_m1.reduce_motion() is True

    m3_outputs = dict(m1_outputs)
    m3_outputs[("/usr/sbin/sysctl", "-n", "hw.memsize")] = "19327352832\n"
    m3_outputs[("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string")] = "Apple M3 Pro\n"
    m3_outputs[("/usr/sbin/sysctl", "-n", "hw.physicalcpu")] = "12\n"
    m3_outputs[("/usr/bin/defaults", "read", "-g", "AppleLanguages")] = '(\n    "zh-Hans-CN"\n)\n'
    m3_outputs[("/usr/bin/defaults", "read", "com.apple.universalaccess", "reduceMotion")] = "0\n"
    info_m3 = sysinfo.MacSystemInfo(run=_fake_runner(m3_outputs))
    total3, avail3, installed3 = info_m3.ram_gb()
    assert total3 == pytest.approx(18.0)
    assert installed3 == pytest.approx(18.0)
    assert avail3 == pytest.approx(4.0)
    assert info_m3.cpu_name() == "Apple M3 Pro"
    assert info_m3.physical_cores() == 12
    assert info_m3.ui_language() == "zh"
    assert info_m3.reduce_motion() is False


def test_sysinfo_errors_and_timeouts_return_unknown(monkeypatch):
    def _timeout(args: list[str]) -> str:
        raise subprocess.TimeoutExpired(args, sysinfo.TIMEOUT_S)

    info = sysinfo.MacSystemInfo(run=_timeout)
    assert info.ram_gb() == (0.0, 0.0, None)
    assert info.cpu_name() == ""
    assert info.physical_cores() is None
    assert info.cpu_features() == {"arm64": False, "avx2": False, "avx512": False}
    assert info.adapters() == []
    assert info.ui_language() is None
    assert info.reduce_motion() is False

    # vm_stat error alone keeps total and installed RAM.
    partial = sysinfo.MacSystemInfo(
        run=_fake_runner({("/usr/sbin/sysctl", "-n", "hw.memsize"): "8589934592\n"})
    )
    assert partial.ram_gb() == (8.0, 0.0, 8.0)

    # Default run_command goes through sysinfo.subprocess.run at call time.
    called: list[list[str]] = []

    def _fake_subproc_run(args, **_kwargs):
        called.append(list(args))
        return SimpleNamespace(stdout="Apple M2\n")

    monkeypatch.setattr(sysinfo.subprocess, "run", _fake_subproc_run)
    assert sysinfo.MacSystemInfo().cpu_name() == "Apple M2"
    assert called == [["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]]


def test_hardware_recommendations_darwin():
    p8 = hardware.HardwareProfile(ram_total_gb=8.0, ram_installed_gb=8.0, system="darwin")
    p16 = hardware.HardwareProfile(ram_total_gb=16.0, ram_installed_gb=16.0, system="darwin")
    p24 = hardware.HardwareProfile(ram_total_gb=24.0, ram_installed_gb=24.0, system="darwin")

    rec8 = hardware.recommend(p8)
    assert (rec8.rule, rec8.model, rec8.device, rec8.dictation_model) == (
        "mac_turbo",
        hardware.WCPP_TURBO,
        "METAL",
        hardware.WCPP_TURBO,
    )
    assert hardware.step_down(rec8, p8) is None

    for p in (p16, p24):
        rec = hardware.recommend(p)
        assert (rec.rule, rec.model, rec.device, rec.dictation_model) == (
            "mac_large",
            hardware.WCPP_LARGE,
            "METAL",
            hardware.WCPP_TURBO,
        )
        lower = hardware.step_down(rec, p)
        assert lower is not None and lower.rule == "mac_turbo"

    assert hardware.recommend_llm(p8).model == hardware.LLM_SMALL
    assert hardware.recommend_llm(p16).model == hardware.LLM_MEDIUM
    assert hardware.recommend_llm(p24).model == hardware.LLM_LARGE

    # Darwin profile without ram_installed_gb uses ram_total_gb without RAM_TOLERANCE_GB.
    p_no_inst = hardware.HardwareProfile(ram_total_gb=15.5, ram_installed_gb=None, system="darwin")
    assert p_no_inst.ram_gb == 15.5
    assert hardware.recommend(p_no_inst).rule == "mac_turbo"


def test_shell_autostart_plist_and_commands(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    opened: list[list[str]] = []
    sh = shell.MacShell(home=tmp_path, popen=lambda args, **_kw: opened.append(list(args)))

    assert sh.start_command() == [sys.executable, "-m", "ucho"]
    assert sh.worker_command("ucho.diarize", ["--worker"]) == [
        sys.executable,
        "-m",
        "ucho.diarize",
        "--worker",
    ]
    assert sh.autostart_get() is None
    assert sh.autostart_set(True) is True

    plist_path = tmp_path / "Library" / "LaunchAgents" / "app.ucho.plist"
    assert plist_path.is_file()
    data = plistlib.loads(plist_path.read_bytes())
    assert data["Label"] == "app.ucho"
    assert data["ProgramArguments"] == [sys.executable, "-m", "ucho", "--minimized"]
    assert data["RunAtLoad"] is True
    assert data["ProcessType"] == "Interactive"
    assert data["WorkingDirectory"] == str(paths.app_dir())
    assert sh.autostart_get() == shlex.join([sys.executable, "-m", "ucho", "--minimized"])

    # autostart_sync writes only when the plist differs from the wanted command.
    writes: list[bool] = []
    real_set = sh.autostart_set
    monkeypatch.setattr(sh, "autostart_set", lambda enabled: writes.append(enabled) or real_set(enabled))
    sh.autostart_sync(True)
    assert writes == []
    plist_path.write_bytes(plistlib.dumps({"Label": "app.ucho", "ProgramArguments": ["/old/python"]}))
    sh.autostart_sync(True)
    assert writes == [True]
    assert sh.autostart_get() == shlex.join([sys.executable, "-m", "ucho", "--minimized"])
    sh.autostart_sync(False)
    assert writes == [True, False]
    assert not plist_path.exists()
    sh.autostart_sync(False)
    assert writes == [True, False]

    assert sh.autostart_set(False) is True
    assert not plist_path.exists()
    assert sh.autostart_get() is None
    assert sh.autostart_set(False) is True

    sh.open_path(tmp_path)
    assert opened == [["/usr/bin/open", str(tmp_path)]]


def test_shell_frozen_bundle_and_translocation_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys,
        "executable",
        "/Applications/Ucho.app/Contents/MacOS/Ucho",
    )
    sh = shell.MacShell(home=tmp_path)
    assert shell.app_bundle() == Path("/Applications/Ucho.app")
    assert sh.start_command() == ["/usr/bin/open", "-a", str(Path("/Applications/Ucho.app"))]
    assert sh.worker_command("ucho.diarize", ["--worker"]) == [
        "/Applications/Ucho.app/Contents/MacOS/Ucho",
        "--worker",
        "ucho.diarize",
        "--worker",
    ]
    assert sh.autostart_set(True) is True
    plist_path = tmp_path / "Library" / "LaunchAgents" / "app.ucho.plist"
    data = plistlib.loads(plist_path.read_bytes())
    assert "WorkingDirectory" not in data
    assert data["ProgramArguments"] == [
        "/usr/bin/open",
        "-a",
        str(Path("/Applications/Ucho.app")),
        "--args",
        "--minimized",
    ]
    sh.autostart_set(False)

    for bad_exec in (
        "/Volumes/Ucho/Ucho.app/Contents/MacOS/Ucho",
        "/private/var/folders/xx/AppTranslocation/123/d/Ucho.app/Contents/MacOS/Ucho",
    ):
        monkeypatch.setattr(sys, "executable", bad_exec)
        assert sh.autostart_set(True) is False
        assert not plist_path.exists()


def test_sounds_afplay_fallback_and_nssound(monkeypatch):
    monkeypatch.setitem(sys.modules, "AppKit", None)
    calls: list[list[str]] = []

    def _fake_popen(args, **_kwargs):
        calls.append(list(args))

    player = sounds.MacSounds(popen=_fake_popen)
    player.play("start")
    player.play("stop")
    player.play("cancel")
    assert calls == [
        ["/usr/bin/afplay", "/System/Library/Sounds/Tink.aiff"],
        ["/usr/bin/afplay", "/System/Library/Sounds/Pop.aiff"],
        ["/usr/bin/afplay", "/System/Library/Sounds/Basso.aiff"],
    ]

    played: list[str] = []
    fake_appkit = SimpleNamespace(
        NSSound=SimpleNamespace(
            soundNamed_=lambda name: SimpleNamespace(play=lambda: played.append(name))
        )
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    calls.clear()
    player.play("stop")
    assert played == ["Pop"]
    assert not calls


def test_fonts_icon_font_and_glyphs():
    mf = fonts.MacFonts()
    icon_path = mf.icon_font()
    assert icon_path is not None
    font_file = Path(icon_path)
    assert font_file.is_file()
    assert font_file.stat().st_size < 100 * 1024

    glyphs = mf.glyphs()
    assert len(glyphs) == 19

    ImageFont = pytest.importorskip("PIL.ImageFont")
    pil_font = ImageFont.truetype(str(font_file), 24)
    for name, char in glyphs.items():
        mask = pil_font.getmask(char)
        assert mask.getbbox() is not None, f"Empty glyph mask for {name!r}"


def test_theme_resolve_keeps_dot_prefixed_system_family(monkeypatch):
    # Fonts.resolve accepts dot-prefixed macOS system families even when absent from families().
    tkinter_font = pytest.importorskip("tkinter.font")
    mf = fonts.MacFonts()
    # resolve() rewrites class attributes; monkeypatch restores them for later tests.
    for attr in ("sans", "display", "mono", "numeric", "semibold"):
        monkeypatch.setattr(theme.Fonts, attr, getattr(theme.Fonts, attr))
    monkeypatch.setattr(tkinter_font, "families", lambda: ("Helvetica", "Times", "Courier", "Menlo"))
    for attr in ("SANS", "DISPLAY", "MONO", "NUMERIC"):
        monkeypatch.setattr(theme, attr, getattr(mf, attr.lower()))
    monkeypatch.setattr(theme, "SEMIBOLD", dict(mf.strong))
    theme.Fonts.resolve()
    assert theme.Fonts.sans == ".AppleSystemUIFont"
    assert theme.Fonts.numeric == ".AppleSystemUIFont"
    assert theme.Fonts.display == "Times"
    assert theme.Fonts.mono == "Menlo"
    assert theme.Fonts.semibold == {}


def _check_contract(protocol: type, impl: object) -> None:
    assert isinstance(impl, protocol), f"{type(impl).__name__} does not satisfy {protocol.__name__}"
    for name, member in protocol.__dict__.items():
        if name.startswith("_") or not callable(member):
            continue
        proto_sig = inspect.signature(member)
        attr = getattr(impl, name)
        assert callable(attr)
        proto_params = list(proto_sig.parameters.values())[1:]
        impl_params = list(inspect.signature(attr).parameters.values())
        assert [p.name for p in impl_params] == [p.name for p in proto_params]
        assert [p.default for p in impl_params] == [p.default for p in proto_params]


def test_macos_protocol_contracts(tmp_path):
    _check_contract(base.SystemInfo, sysinfo.create())
    _check_contract(base.Shell, shell.MacShell(home=tmp_path))
    _check_contract(base.Sounds, sounds.create())
    _check_contract(base.Fonts, fonts.create())

    with platforms.override("macos"):
        assert isinstance(platforms.sysinfo(), sysinfo.MacSystemInfo)
        assert isinstance(platforms.shell(), shell.MacShell)
        assert isinstance(platforms.sounds(), sounds.MacSounds)
        assert isinstance(platforms.fonts(), fonts.MacFonts)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only plutil and sysctl checks")
def test_macos_live_system_and_plutil(tmp_path):
    sh = shell.MacShell(home=tmp_path)
    assert sh.autostart_set(True) is True
    plist_path = tmp_path / "Library" / "LaunchAgents" / "app.ucho.plist"
    subprocess.run(["/usr/bin/plutil", "-lint", str(plist_path)], check=True)

    info = sysinfo.MacSystemInfo()
    assert info.ram_gb()[0] > 1
    assert "Apple" in info.cpu_name()
