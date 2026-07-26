"""Tests for :mod:`giruntime.ui._timeouts`.

The module is the injection seam two panes debounce through, so what
matters is that the production wiring really schedules and cancels a
GLib source and that :data:`TIMEOUT_REMOVE` carries GLib's "do not
re-fire" value. No display is needed: GLib's main context is available
headless.
"""

from __future__ import annotations

import unittest

from gi.repository import GLib

from giruntime.ui._timeouts import (
    TIMEOUT_REMOVE,
    default_timeout_canceller,
    default_timeout_scheduler,
)


def _drain_main_context(iterations: int = 200) -> None:
    """Pump the default main context so due timeouts fire.

    Bounded and non-blocking so a callback that never becomes ready
    cannot hang the suite.
    """
    context = GLib.MainContext.default()
    for _ in range(iterations):
        if not context.iteration(False):
            continue


class TimeoutRemoveTests(unittest.TestCase):
    """The re-export must carry GLib's own semantics."""

    def test_matches_glib_source_remove(self) -> None:
        self.assertEqual(TIMEOUT_REMOVE, GLib.SOURCE_REMOVE)

    def test_is_falsey_so_a_source_is_dropped(self) -> None:
        # GLib drops a source whose callback returns a false value; a
        # truthy constant here would silently make every debounce
        # repeat forever.
        self.assertFalse(TIMEOUT_REMOVE)


class DefaultTimeoutSchedulerTests(unittest.TestCase):
    """The production scheduler wraps :func:`GLib.timeout_add`."""

    def test_returns_a_usable_source_handle(self) -> None:
        handle = default_timeout_scheduler(10_000, lambda: TIMEOUT_REMOVE)
        try:
            self.assertIsInstance(handle, int)
            self.assertIsNotNone(GLib.MainContext.default().find_source_by_id(
                handle,
            ))
        finally:
            default_timeout_canceller(handle)

    def test_callback_fires_after_the_delay(self) -> None:
        fired: list[bool] = []

        def _callback() -> bool:
            fired.append(True)
            return TIMEOUT_REMOVE

        default_timeout_scheduler(0, _callback)
        _drain_main_context()
        self.assertEqual(fired, [True])

    def test_callback_returning_remove_does_not_repeat(self) -> None:
        fired: list[bool] = []

        def _callback() -> bool:
            fired.append(True)
            return TIMEOUT_REMOVE

        default_timeout_scheduler(0, _callback)
        _drain_main_context()
        _drain_main_context()
        self.assertEqual(len(fired), 1)


class DefaultTimeoutCancellerTests(unittest.TestCase):
    """The production canceller wraps :func:`GLib.source_remove`."""

    def test_cancelled_callback_never_fires(self) -> None:
        fired: list[bool] = []

        def _callback() -> bool:
            fired.append(True)
            return TIMEOUT_REMOVE

        handle = default_timeout_scheduler(0, _callback)
        default_timeout_canceller(handle)
        _drain_main_context()
        self.assertEqual(fired, [])

    def test_cancelling_removes_the_source(self) -> None:
        handle = default_timeout_scheduler(10_000, lambda: TIMEOUT_REMOVE)
        default_timeout_canceller(handle)
        self.assertIsNone(
            GLib.MainContext.default().find_source_by_id(handle),
        )
