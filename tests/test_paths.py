# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tests for platform-aware application and data paths in `ucho.paths`."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from ucho import config, paths

MAC_HOME = PurePosixPath("/Users/x")
LINUX_HOME = PurePosixPath("/home/x")


def test_macos_data_and_log_paths():
    assert paths.data_dir(system="darwin", home=MAC_HOME, env={}) == (
        PurePosixPath("/Users/x/Library/Application Support/Ucho")
    )
    assert paths.log_file(system="darwin", home=MAC_HOME, env={}) == (
        PurePosixPath("/Users/x/Library/Logs/Ucho/ucho.log")
    )
    assert paths.models_dir(system="darwin", home=MAC_HOME, env={}) == (
        PurePosixPath("/Users/x/Library/Application Support/Ucho/models")
    )
    assert paths.legacy_models_dir(system="darwin", home=MAC_HOME, env={}) == (
        PurePosixPath("/Users/x/Library/Application Support/Ucho/modele")
    )
    assert paths.settings_file(system="darwin", home=MAC_HOME, env={}) == (
        PurePosixPath("/Users/x/Library/Application Support/Ucho/settings.json")
    )


def test_ucho_data_dir_override_and_empty_ignored():
    env = {"UCHO_DATA_DIR": "/tmp/u"}
    assert paths.data_dir(system="darwin", home=MAC_HOME, env=env) == PurePosixPath("/tmp/u")
    assert paths.log_file(system="darwin", home=MAC_HOME, env=env) == (
        PurePosixPath("/tmp/u/ucho.log")
    )
    assert paths.settings_file(system="darwin", home=MAC_HOME, env=env) == (
        PurePosixPath("/tmp/u/settings.json")
    )

    empty = {"UCHO_DATA_DIR": ""}
    assert paths.data_dir(system="darwin", home=MAC_HOME, env=empty) == (
        PurePosixPath("/Users/x/Library/Application Support/Ucho")
    )
    assert paths.log_file(system="darwin", home=MAC_HOME, env=empty) == (
        PurePosixPath("/Users/x/Library/Logs/Ucho/ucho.log")
    )


def test_linux_xdg_and_default_paths():
    assert paths.data_dir(system="linux", home=LINUX_HOME, env={}) == (
        PurePosixPath("/home/x/.local/share/ucho")
    )
    assert paths.log_file(system="linux", home=LINUX_HOME, env={}) == (
        PurePosixPath("/home/x/.local/share/ucho/ucho.log")
    )
    assert paths.data_dir(
        system="linux", home=LINUX_HOME, env={"XDG_DATA_HOME": "/var/data"}
    ) == PurePosixPath("/var/data/ucho")
    assert paths.data_dir(
        system="linux", home=LINUX_HOME, env={"XDG_DATA_HOME": ""}
    ) == PurePosixPath("/home/x/.local/share/ucho")


def test_windows_data_dir_matches_app_dir():
    assert paths.data_dir(system="win32", env={}) == paths.app_dir()
    assert paths.log_file(system="win32", env={}) == paths.app_dir() / "ucho.log"
    assert paths.settings_file(system="win32", env={}) == paths.app_dir() / "settings.json"
    assert config.PROJECT_DIR == paths.app_dir()


def test_app_dir_frozen_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.app_dir() == Path(__file__).resolve().parent.parent

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.app_dir() == tmp_path


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("/Users/x/Library/Mobile Documents/com~apple~CloudDocs/x", True),
        ("/Users/x/Documents/x", True),
        ("/Users/x/Documents", True),
        ("/Users/x/documents/X", True),
        ("/Users/x/Library/CloudStorage/OneDrive-Firma/x", True),
        ("/Users/x/Dropbox/x", True),
        ("/Users/x/Desktop/x", True),
        ("/Users/x/Ucho/recordings", False),
        ("/Users/x/Documents-old/x", False),
    ],
)
def test_is_cloud_path_darwin(candidate, expected):
    assert (
        paths.is_cloud_path(
            PurePosixPath(candidate), system="darwin", home=MAC_HOME, env={}
        )
        is expected
    )


def test_is_cloud_path_windows():
    env = {"OneDrive": r"C:\Users\Test\OneDrive"}
    assert paths.is_cloud_path(
        PureWindowsPath(r"C:\Users\Test\OneDrive\Ucho\recordings"),
        system="win32",
        env=env,
    )
    assert paths.is_cloud_path(
        PureWindowsPath(r"D:\OneDrive - Firma\x"),
        system="win32",
        env=env,
    )
    assert not paths.is_cloud_path(
        PureWindowsPath(r"C:\Users\Test\Ucho\recordings"),
        system="win32",
        env=env,
    )
    assert paths.cloud_roots(
        system="win32",
        env={"OneDrive": r"C:\OD", "OneDriveCommercial": "", "OneDriveConsumer": r"D:\ODC"},
    ) == [PureWindowsPath(r"C:\OD"), PureWindowsPath(r"D:\ODC")]


def test_is_cloud_path_linux():
    assert paths.cloud_roots(system="linux", home=LINUX_HOME, env={}) == []
    assert not paths.is_cloud_path(
        PurePosixPath("/home/x/Documents/x"), system="linux", home=LINUX_HOME, env={}
    )


def test_import_config_creates_no_files(tmp_path):
    data = tmp_path / "data"
    env = os.environ.copy()
    env["UCHO_DATA_DIR"] = str(data)
    subprocess.run(
        [sys.executable, "-c", "import ucho.config"],
        cwd=paths.app_dir(),
        env=env,
        check=True,
    )
    assert not data.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(
    sys.platform != "win32" or bool(os.environ.get("UCHO_DATA_DIR")),
    reason="Windows paths without the UCHO_DATA_DIR override",
)
def test_windows_config_paths_unchanged():
    # Values as computed before ucho/paths.py existed.
    old_project = Path(config.__file__).resolve().parent.parent
    assert config.PROJECT_DIR == old_project
    assert config.DATA_DIR == old_project
    assert config.LEGACY_MODELS_DIR == old_project / "modele"
    assert config.MODELS_DIR in (old_project / "models", old_project / "modele")
    assert config.OV_CACHE_DIR == config.MODELS_DIR / "ovcache"
    assert config.SETTINGS_FILE == old_project / "settings.json"
    assert config.LOG_FILE == old_project / "ucho.log"
    assert type(config.SETTINGS_FILE) is type(old_project)
