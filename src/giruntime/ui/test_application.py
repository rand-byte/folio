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

from giruntime.ui.application import NotesApplication


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


if __name__ == "__main__":
    unittest.main()
