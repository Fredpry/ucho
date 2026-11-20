# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Tests for ucho/platform_text.py (no customtkinter required)."""

from __future__ import annotations

import logging

import pytest

from ucho import config, i18n, platform_text, platforms


class _FakePermissions:
    def __init__(self, states=None, urls=None, state_error=None, request_error=None, url_error=None):
        self._states = dict(states or {})
        self._urls = dict(urls or {})
        self._state_error = state_error
        self._request_error = request_error
        self._url_error = url_error
        self.requested: list[str] = []

    def state(self):
        if self._state_error is not None:
            raise self._state_error
        return dict(self._states)

    def request(self, kind: str) -> None:
        self.requested.append(kind)
        if self._request_error is not None:
            raise self._request_error

    def settings_url(self, kind: str) -> str | None:
        if self._url_error is not None:
            raise self._url_error
        return self._urls.get(kind)


class _FakeShell:
    def __init__(self, open_error=None):
        self.opened: list[object] = []
        self._open_error = open_error

    def open_path(self, path) -> None:
        self.opened.append(path)
        if self._open_error is not None:
            raise self._open_error


class _FakeInserter:
    def __init__(self, label: str):
        self._label = label

    def paste_label(self) -> str:
        return self._label


@pytest.fixture(autouse=True)
def _english_ui():
    previous = i18n.language()
    i18n.set_language("en")
    try:
        yield
    finally:
        i18n.set_language(previous)


def test_permission_rows_states_and_flags(monkeypatch):
    fake = _FakePermissions(
        states={
            "microphone": "granted",
            "system_audio": "denied",
            "accessibility": "unknown",
        },
        urls={
            "system_audio": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        },
    )
    monkeypatch.setattr(platforms, "permissions", lambda: fake)

    rows = platform_text.permission_rows()
    assert [r.kind for r in rows] == ["microphone", "system_audio", "accessibility"]
    assert [r.title for r in rows] == ["Microphone", "System audio", "Accessibility"]
    assert rows[0].hint == "Needed to record your voice and to dictate."
    assert rows[1].hint == (
        "Needed to record the other participants of a meeting. "
        "Without it Ucho records the microphone only."
    )
    assert rows[2].hint == "Needed to paste dictated text into other windows and to type it."

    assert (rows[0].state, rows[0].allow, rows[0].open_settings) == ("granted", False, False)
    assert (rows[1].state, rows[1].allow, rows[1].open_settings) == ("denied", False, True)
    assert (rows[2].state, rows[2].allow, rows[2].open_settings) == ("unknown", True, False)
    assert not platform_text.all_granted(rows)


def test_permission_rows_unsupported_missing_invalid_and_errors(monkeypatch):
    fake = _FakePermissions(
        states={
            "microphone": "unsupported",
            "system_audio": "bogus",
        },
        urls={"accessibility": ""},
    )
    monkeypatch.setattr(platforms, "permissions", lambda: fake)

    rows = platform_text.permission_rows()
    assert [(r.state, r.allow, r.open_settings) for r in rows] == [
        ("unsupported", False, False),
        ("unknown", True, False),
        ("unknown", True, False),
    ]

    denied_no_url = _FakePermissions(states={"microphone": "denied"}, urls={})
    monkeypatch.setattr(platforms, "permissions", lambda: denied_no_url)
    rows = platform_text.permission_rows()
    assert (rows[0].state, rows[0].allow, rows[0].open_settings) == ("denied", False, False)

    url_raising = _FakePermissions(
        states={"microphone": "denied", "system_audio": "granted", "accessibility": "granted"},
        url_error=RuntimeError("url failed"),
    )
    monkeypatch.setattr(platforms, "permissions", lambda: url_raising)
    rows = platform_text.permission_rows()
    assert rows[0].open_settings is False

    state_raising = _FakePermissions(state_error=RuntimeError("state failed"))
    monkeypatch.setattr(platforms, "permissions", lambda: state_raising)
    rows = platform_text.permission_rows()
    assert all(r.state == "unknown" and r.allow and not r.open_settings for r in rows)


def test_permission_rows_explicit_state_and_all_granted(monkeypatch):
    monkeypatch.setattr(platforms, "permissions", lambda: _FakePermissions())
    rows = platform_text.permission_rows(
        {"microphone": "granted", "system_audio": "granted", "accessibility": "granted"}
    )
    assert platform_text.all_granted(rows) is True


def test_state_text():
    assert platform_text.state_text("granted") == "Allowed"
    assert platform_text.state_text("denied") == "Denied"
    assert platform_text.state_text("unknown") == "Not asked yet"
    assert platform_text.state_text("unsupported") == "Not available"


def test_request_permission_swallows_errors_without_secret_text(monkeypatch, caplog):
    fake = _FakePermissions(request_error=RuntimeError("sensitive user path"))
    monkeypatch.setattr(platforms, "permissions", lambda: fake)

    with caplog.at_level(logging.WARNING, logger="ucho.platform_text"):
        platform_text.request_permission("microphone")

    assert fake.requested == ["microphone"]
    assert "sensitive user path" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_open_permission_settings(monkeypatch):
    url = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    fake_perms = _FakePermissions(urls={"accessibility": url})
    fake_shell = _FakeShell()
    monkeypatch.setattr(platforms, "permissions", lambda: fake_perms)
    monkeypatch.setattr(platforms, "shell", lambda: fake_shell)

    assert platform_text.open_permission_settings("accessibility") is True
    assert fake_shell.opened == [url]

    fake_shell_none = _FakeShell()
    monkeypatch.setattr(platforms, "shell", lambda: fake_shell_none)
    assert platform_text.open_permission_settings("microphone") is False
    assert fake_shell_none.opened == []

    failing_shell = _FakeShell(open_error=OSError("cannot open"))
    monkeypatch.setattr(platforms, "shell", lambda: failing_shell)
    assert platform_text.open_permission_settings("accessibility") is False


def test_wizard_steps():
    expected_win = ["language", "folder", "hardware", "analysis", "consent"]
    expected_mac = ["language", "folder", "hardware", "analysis", "permissions", "consent"]

    assert platform_text.wizard_steps("windows") == expected_win
    assert platform_text.wizard_steps("null") == expected_win
    assert platform_text.wizard_steps("macos") == expected_mac

    with platforms.override("windows"):
        assert platform_text.wizard_steps() == expected_win
    with platforms.override("macos"):
        assert platform_text.wizard_steps() == expected_mac


def test_windows_texts_identical_and_macos_texts(monkeypatch):
    with platforms.override("windows"):
        assert platform_text.system_theme_label() == "Same as Windows"
        assert platform_text.dictation_mode_labels() == config.DICTATION_MODE_LABELS
        assert platform_text.cloud_folder_note() == (
            "This folder is synced by OneDrive: recordings and transcripts "
            "would go to the cloud. Choose a folder outside OneDrive."
        )
        assert platform_text.cloud_folder_note(r"C:\Ucho\recordings") == (
            "This folder is synced by OneDrive: recordings and transcripts would go to "
            r"the cloud. Choose a folder outside OneDrive, for example C:\Ucho\recordings."
        )
        assert platform_text.cloud_lock_hint() == (
            "The file is in a OneDrive folder, which can lock files while they sync. "
            "Wait for OneDrive to finish or choose a folder outside OneDrive."
        )
        assert platform_text.access_denied_message() == (
            "Windows did not let Ucho read or write a file or folder."
        )
        assert platform_text.audio_device_hint() == (
            "Check the device in Windows sound settings, close other programs that use it "
            "and choose it again in Settings."
        )
        assert platform_text.system_info_description() == (
            "Ucho version, Windows, processor, memory and graphics"
        )

    with platforms.override("macos"):
        monkeypatch.setattr(platforms, "inserter", lambda: _FakeInserter("⌘V"))
        assert platform_text.system_theme_label() == "Same as the system"
        assert platform_text.dictation_mode_labels() == {
            "paste": "Paste (clipboard + ⌘V)",
            "type": "Type (simulated keyboard)",
        }
        assert platform_text.cloud_folder_note() == (
            "This folder is synced to the cloud (iCloud Drive, Dropbox or similar): recordings "
            "and transcripts would go to the cloud. Choose a folder outside it."
        )
        assert platform_text.cloud_folder_note("/Users/demo/Ucho/recordings") == (
            "This folder is synced to the cloud (iCloud Drive, Dropbox or similar): recordings "
            "and transcripts would go to the cloud. Choose a folder outside it, "
            "for example /Users/demo/Ucho/recordings."
        )
        assert platform_text.cloud_lock_hint() == (
            "The file is in a folder synced to the cloud, which can lock files while it syncs. "
            "Wait for the sync to finish or choose a folder outside it."
        )
        assert platform_text.access_denied_message() == (
            "macOS did not let Ucho read or write a file or folder."
        )
        assert platform_text.audio_device_hint() == (
            "Check the device in the macOS Sound settings, close other programs that use it "
            "and choose it again in Settings."
        )
        assert platform_text.system_info_description() == (
            "Ucho version, macOS, processor, memory and graphics"
        )


def test_model_tail_and_system_audio_unavailable():
    assert platform_text.model_tail("OpenVINO/whisper-large-v3-int8-ov") == "whisper-large-v3-int8-ov"
    assert (
        platform_text.model_tail("ggerganov/whisper.cpp:ggml-large-v3-turbo-q5_0.bin")
        == "ggml-large-v3-turbo-q5_0"
    )
    assert (
        platform_text.system_audio_unavailable_text()
        == "System audio is unavailable, recording the microphone only"
    )
