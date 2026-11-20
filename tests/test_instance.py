# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Single instance: a second start hands its command to the first one (named mutex + events)."""
import sys
import threading
import uuid

import pytest

from ucho.instance import SingleInstance

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only: named mutex and events")


def test_second_start_passes_its_command():
    name = rf"Local\Ucho.Test.{uuid.uuid4().hex}"  # never the real app's name
    first, second = SingleInstance(name), SingleInstance(name)
    received: list[str] = []
    got = threading.Event()
    try:
        assert first.acquire()
        first.listen(lambda command: (received.append(command), got.set()))
        assert not second.acquire()
        assert second.signal("dictate")
        assert got.wait(5)
        assert received == ["dictate"]
        assert not second.signal("unknown-command")
    finally:
        second.close()
        first.close()


def test_after_close_the_name_is_free():
    name = rf"Local\Ucho.Test.{uuid.uuid4().hex}"
    first = SingleInstance(name)
    assert first.acquire()
    first.close()
    again = SingleInstance(name)
    assert again.acquire()
    again.close()
