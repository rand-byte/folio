"""Tests for :mod:`ui.dialogs`."""

from __future__ import annotations

import unittest

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk  # noqa: E402

from ui.dialogs import default_confirm_dialog_presenter


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Confirm-dialog presenter — production wrapper
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultConfirmDialogPresenterTests(unittest.TestCase):
    """The production presenter constructs without raising.

    The asynchronous ``Gtk.AlertDialog.choose`` cannot be driven
    inside a unit test without a real main loop, so the deeper
    behaviour of the production presenter is exercised by the
    fake-driven tests inside :mod:`test_toolbar`. These tests pin
    the construction surface only.
    """

    def test_construction_does_not_raise_with_a_parent(self) -> None:
        # The presenter sets ``modal=True`` and calls
        # ``Gtk.AlertDialog.choose``, which maps GTK's internal dialog
        # window synchronously. A modal dialog with no parent maps
        # *without a transient parent* — GTK prints "GtkDialog mapped
        # without a transient parent. This is discouraged." Production
        # always supplies the top-level window (toolbar / sidebar pass
        # the ``MainWindow``), so the test mirrors that by anchoring
        # the dialog to a real toplevel. The window is torn down on
        # cleanup, which also disposes the still-pending dialog.
        captured: list[bool] = []

        def on_result(value: bool) -> None:
            captured.append(value)

        parent = Gtk.Window()
        parent.present()
        self.addCleanup(parent.destroy)

        # Should not raise.
        default_confirm_dialog_presenter(
            parent,
            "Delete \"x\"?",
            "This cannot be undone.",
            "Delete",
            on_result,
        )
        # The result has not arrived yet (no main loop is driven to
        # completion) — that is the expected state for this test.
        self.assertEqual(captured, [])


if __name__ == "__main__":
    unittest.main()
