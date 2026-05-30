"""Tests for :mod:`ui.toolbar`.

Most of the legacy toolbar tests covered the now-removed notebook
breadcrumb logic. The toolbar's tag-aware ``+New``, mode toggle, and
search wiring are exercised through :mod:`ui.test_main_window` and the
controller-level tests in :mod:`controllers.test_note_controller`;
this stub keeps the module discoverable by ``unittest`` without
re-implementing all the GTK fixture plumbing.
"""

from __future__ import annotations

import unittest

import ui.toolbar as toolbar_module


class ToolbarSmokeTests(unittest.TestCase):
    """The toolbar's surface is exercised via integration tests."""

    def test_no_breadcrumb_helpers_exported(self) -> None:
        # The pre-tags toolbar exposed ``compute_breadcrumb``,
        # ``format_breadcrumb``, and ``resolve_target_notebook`` at
        # module scope. The tag-based toolbar drops the breadcrumb
        # entirely; this test pins the symbols' absence.
        self.assertFalse(hasattr(toolbar_module, "compute_breadcrumb"))
        self.assertFalse(hasattr(toolbar_module, "format_breadcrumb"))
        self.assertFalse(hasattr(toolbar_module, "resolve_target_notebook"))


if __name__ == "__main__":
    unittest.main()
