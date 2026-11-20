# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""macOS clipboard via NSPasteboard, with private markers and a lazily provided paste.

The Windows dictation paste hands the text over with delayed rendering, so
the previous clipboard comes back only after the target has really taken the
dictation (ucho/platforms/windows/clipboard.py explains why a fixed delay was
not safe). NSPasteboard has the same mechanism: an `NSPasteboardItem` with a
data provider puts a promise on the pasteboard, and AppKit asks the provider
for the text when a program reads it. `MacDelayedOffer` uses that, so
`rendered` means "a program read the text", not "some time has passed".

macOS does not tell who read the text. `confirmed` therefore means that the
frontmost app at the time of the read is the app the dictation was meant for;
clipboard managers that honour the private markers below skip the item
without reading it.

Data requests arrive through a run loop (the main one in the app, where Tk
runs it). `pump` runs the calling thread's run loop as well, so a caller on
the main thread (tests) is served too.

PyObjC is imported inside `CocoaPasteboard` only; the offer logic works on any
object with the same methods, which is how the tests run it on Windows.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .inserter import frontmost_pid, settle

log = logging.getLogger(__name__)

TEXT_TYPE = "public.utf8-plain-text"  # NSPasteboardTypeString
# nspasteboard.org markers: clipboard managers and Universal Clipboard skip
# items that carry them (the counterpart of the Windows history formats).
CONCEALED_TYPE = "org.nspasteboard.ConcealedType"
TRANSIENT_TYPE = "org.nspasteboard.TransientType"
PRIVATE_TYPES = (CONCEALED_TYPE, TRANSIENT_TYPE)
PUMP_SLICE_S = 0.05


class Pasteboard(Protocol):
    """What the clipboard logic needs from NSPasteboard (a fake in the tests)."""

    def change_count(self) -> int: ...
    def read_text(self) -> str | None: ...
    def write_text(self, text: str | None, private: bool) -> int: ...
    def promise_text(self, provide: Callable[[], str], private: bool) -> int: ...
    def run_loop(self, seconds: float) -> None: ...


_provider_class: Any = None
_provider_lock = threading.Lock()


def _provider_type() -> Any:
    """The NSPasteboardItemDataProvider class, defined once per process (ObjC class names are global)."""
    global _provider_class
    with _provider_lock:
        if _provider_class is None:
            import objc
            from Foundation import NSObject

            try:
                protocols = [objc.protocolNamed("NSPasteboardItemDataProvider")]
            except Exception:
                protocols = []

            class UchoPasteboardProvider(NSObject, protocols=protocols):
                def pasteboard_item_provideDataForType_(self, pasteboard, item, data_type):
                    provide = getattr(self, "ucho_provide", None)
                    try:
                        text = provide() if provide is not None else None
                        if text is not None:
                            item.setString_forType_(text, data_type)
                    except Exception:
                        log.warning("Could not hand the dictation to the pasteboard", exc_info=True)

                def pasteboardFinishedWithDataProvider_(self, pasteboard):
                    self.ucho_provide = None

            _provider_class = UchoPasteboardProvider
        return _provider_class


class CocoaPasteboard:
    """`Pasteboard` over `NSPasteboard.generalPasteboard()`."""

    def __init__(self) -> None:
        self._providers: list[Any] = []  # kept alive while their promise may be read

    @staticmethod
    def _board():
        from AppKit import NSPasteboard

        return NSPasteboard.generalPasteboard()

    def change_count(self) -> int:
        return int(self._board().changeCount())

    def read_text(self) -> str | None:
        text = self._board().stringForType_(TEXT_TYPE)
        return None if text is None else str(text)

    @staticmethod
    def _item(private: bool):
        from AppKit import NSPasteboardItem
        from Foundation import NSData

        item = NSPasteboardItem.alloc().init()
        if private:
            for marker in PRIVATE_TYPES:
                item.setData_forType_(NSData.data(), marker)
        return item

    def _write(self, item) -> int:
        board = self._board()
        board.clearContents()
        if not board.writeObjects_([item]):
            raise RuntimeError("NSPasteboard writeObjects failed")
        return int(board.changeCount())

    def write_text(self, text: str | None, private: bool) -> int:
        self._providers.clear()
        if text is None:
            board = self._board()
            board.clearContents()
            return int(board.changeCount())
        item = self._item(private)
        item.setString_forType_(text, TEXT_TYPE)
        return self._write(item)

    def promise_text(self, provide: Callable[[], str], private: bool) -> int:
        provider = _provider_type().alloc().init()
        provider.ucho_provide = provide
        item = self._item(private)
        if not item.setDataProvider_forTypes_(provider, [TEXT_TYPE]):
            raise RuntimeError("NSPasteboardItem setDataProvider failed")
        count = self._write(item)
        self._providers[:] = [provider]
        return count

    def run_loop(self, seconds: float) -> None:
        settle(seconds)


class MacDelayedOffer:
    """The dictation on the pasteboard as a promise, handed over only when a program reads it.

    Same contract as the Windows `DelayedClipboard`: `offer()` takes the
    pasteboard, `pump` waits, `restore` puts the previous text back only
    while the pasteboard is still ours, `close` (always, in a finally) leaves
    the dictation as real text when nobody has read it yet.
    """

    def __init__(self, pasteboard: Pasteboard, text: str, consumer_pid: int | None,
                 frontmost: Callable[[], int | None] = frontmost_pid):
        self._pasteboard = pasteboard
        self._frontmost = frontmost
        self.text = text
        self.consumer_pid = consumer_pid  # the app expected to paste
        self.rendered = False   # a program read the text
        self.confirmed = False  # ... while the expected app was in front
        self.lost = False       # someone else wrote to the pasteboard
        self._count: int | None = None  # changeCount after our last write
        self._lock = threading.Lock()

    def offer(self) -> None:
        self._count = self._pasteboard.promise_text(self._provide, True)

    def _provide(self) -> str:
        """Called by AppKit (any thread) when a program reads the promised text."""
        with self._lock:
            if not self.rendered:
                self.rendered = True
                try:
                    reader = self._frontmost()
                except Exception:
                    reader = None
                self.confirmed = self.consumer_pid is not None and reader == self.consumer_pid
        return self.text

    def _still_ours(self) -> bool:
        if self.lost or self._count is None:
            return False
        if self._pasteboard.change_count() != self._count:
            self.lost = True
            return False
        return True

    def pump(self, seconds: float, until: Callable[[], bool] | None = None) -> bool:
        """Waits up to `seconds` (running the run loop); True as soon as `until()` holds."""
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            self._pasteboard.run_loop(min(PUMP_SLICE_S, max(0.0, left)))
            if until is not None and until():
                return True
            if time.monotonic() >= end:
                return False

    def restore(self, previous: str | None) -> bool:
        """Puts `previous` back (None empties) if the pasteboard is still ours. True when done.

        NSPasteboard has no lock: a copy made between the check and the write
        is overwritten. The window is a few microseconds; Windows closes it
        by holding the clipboard open.
        """
        if not self._still_ours():
            return False
        self._count = self._pasteboard.write_text(previous, True)
        return True

    def close(self) -> None:
        """Replaces an unread promise with the text, so it stays for a manual ⌘V after Ucho exits."""
        if self._count is None or self.rendered:
            return
        try:
            if self._still_ours():
                self._count = self._pasteboard.write_text(self.text, True)
                self.rendered = True  # not `confirmed`: nobody pasted it
        except Exception:
            log.warning("Could not leave the dictation on the clipboard", exc_info=True)


class MacClipboard:
    """macOS `Clipboard` implementation."""

    def __init__(self, pasteboard: Pasteboard | None = None,
                 frontmost: Callable[[], int | None] = frontmost_pid) -> None:
        self._pasteboard = pasteboard if pasteboard is not None else CocoaPasteboard()
        self._frontmost = frontmost

    def get_text(self) -> str | None:
        return self._pasteboard.read_text()

    def set_text(self, text: str | None, private: bool = True) -> int:
        return self._pasteboard.write_text(text, private)

    def sequence(self) -> int:
        return self._pasteboard.change_count()

    def offer_delayed(self, text: str, consumer_pid: int | None) -> MacDelayedOffer:
        return MacDelayedOffer(self._pasteboard, text, consumer_pid, self._frontmost)


def create() -> MacClipboard:
    return MacClipboard()
