"""Tests for :mod:`giruntime.ui.application`'s window-lifetime policy.

These exercise :meth:`NotesApplication._on_main_window_close_request`
directly — the seam that stops a hide-on-close help window from holding
the process open after the main window is gone. The handler is checked
without registering or running the application: GTK supports only one
*registered* ``GtkApplication`` per process (see the shared
``_test_application`` in :mod:`giruntime.ui.test_main_window`), so the
application is built but never registered, and ``quit`` is spied on rather
than invoked. No GDK display is required.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from gi.repository import Gdk, Gtk

from giruntime.ui.application import (
    _APPLICATION_ID,
    NotesApplication,
    _register_application_icon_resources,
)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for icon-theme lookups."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


class MainWindowCloseRequestTests(unittest.TestCase):
    """Closing the primary window ends the application."""

    def test_close_request_quits_the_application(self) -> None:
        """The handler asks the application to quit exactly once.

        This is the fix: without it, a hidden hide-on-close help window
        keeps the application's main loop running after the main window
        closes, and the process hangs.
        """
        application = NotesApplication()

        with patch.object(application, "quit") as quit_spy:
            application._on_main_window_close_request(
                application.get_active_window()
            )

        quit_spy.assert_called_once_with()

    def test_close_request_does_not_veto_the_close(self) -> None:
        """The handler returns falsey so GTK still destroys the window.

        A truthy return from a ``close-request`` handler vetoes the close;
        the main window would then stay open. The lifetime handler must
        let the default close proceed.
        """
        application = NotesApplication()

        with patch.object(application, "quit"):
            proceed = application._on_main_window_close_request(
                application.get_active_window()
            )

        self.assertFalse(proceed)


@unittest.skipUnless(_display_available(), "no GDK display")
class RegisterApplicationIconResourcesTests(unittest.TestCase):
    """The bundled icon resolves by name once registration has run."""

    def test_icon_theme_gains_the_application_icon(self) -> None:
        """The gresource-bundled SVG becomes a resolvable icon name.

        Without :func:`_register_application_icon_resources`, the
        icon theme has never been told about ``folio.gresource``'s
        ``/org/folio/icons`` subtree, so it would not resolve
        :data:`_APPLICATION_ID` by name.
        """
        display = Gdk.Display.get_default()
        assert display is not None  # narrows for the type checker

        _register_application_icon_resources()

        icon_theme = Gtk.IconTheme.get_for_display(display)
        self.assertTrue(icon_theme.has_icon(_APPLICATION_ID))

    def test_sets_the_default_window_icon_name(self) -> None:
        """Every window without its own icon falls back to the app icon."""
        _register_application_icon_resources()

        self.assertEqual(
            Gtk.Window.get_default_icon_name(),
            _APPLICATION_ID,
        )


if __name__ == "__main__":
    unittest.main()
