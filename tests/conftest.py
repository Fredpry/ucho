# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Tests that cannot run off Windows are marked
# `skipif(sys.platform != "win32", reason="Windows only: ...")`.
WINDOWS_ONLY = "Windows only:"


@pytest.fixture(autouse=True)
def english_ui():
    """Tests compare source texts, so the UI is always in English."""
    from ucho import i18n

    i18n.set_language("en")
    yield
    i18n.set_language("en")


def _skip_reason(report) -> str:
    longrepr = report.longrepr
    reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
    return reason.removeprefix("Skipped: ").strip()


def _other_skips(config) -> list:
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter is not None else []
    return [r for r in skipped if not _skip_reason(r).startswith(WINDOWS_ONLY)]


def pytest_sessionfinish(session, exitstatus):
    """With `UCHO_ONLY_WINDOWS_SKIPS` set (macOS CI), any other skip fails the run."""
    if (os.environ.get("UCHO_ONLY_WINDOWS_SKIPS") and exitstatus == pytest.ExitCode.OK
            and _other_skips(session.config)):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Lists skipped tests by reason, so a skip that hides a failure stands out.

    With `UCHO_SKIPPED_REPORT` set to a file (the macOS workflow passes
    `$GITHUB_STEP_SUMMARY`), the same list is appended to it as Markdown.
    """
    skipped = terminalreporter.stats.get("skipped", [])
    by_reason: dict[str, list[str]] = defaultdict(list)
    for report in skipped:
        by_reason[_skip_reason(report)].append(report.nodeid)
    if by_reason:
        terminalreporter.section(f"{len(skipped)} skipped, by reason")
        for reason, nodes in sorted(by_reason.items()):
            terminalreporter.write_line(f"{len(nodes):4d}  {reason}")
    if os.environ.get("UCHO_ONLY_WINDOWS_SKIPS") and _other_skips(config):
        terminalreporter.write_line(
            f"UCHO_ONLY_WINDOWS_SKIPS: skips without the {WINDOWS_ONLY!r} reason fail the run",
            red=True)

    target = os.environ.get("UCHO_SKIPPED_REPORT")
    if not target:
        return
    windows_only = sum(len(nodes) for reason, nodes in by_reason.items()
                       if reason.startswith(WINDOWS_ONLY))
    lines = [f"### Skipped tests on {sys.platform}: {len(skipped)}",
             "",
             f"Windows only: {windows_only}, other: {len(skipped) - windows_only}.",
             ""]
    for reason, nodes in sorted(by_reason.items()):
        lines.append(f"<details><summary>{len(nodes)} × {reason}</summary>")
        lines.append("")
        lines.extend(f"- `{node}`" for node in sorted(nodes))
        lines.append("")
        lines.append("</details>")
        lines.append("")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
