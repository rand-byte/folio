"""Tests for :mod:`ui.note_list`.

The pre-tags note-list tests exercised notebook-subtree expansion and
per-notebook list helpers — both removed in the tag migration. The
note-list's remaining surface (sort, search filtering, row
construction) is covered by the pure functions in
:mod:`search.note_filter` and by the integration paths in
:mod:`ui.test_main_window`.
"""

from __future__ import annotations

import unittest

import ui.note_list as note_list_module


class NoteListSmokeTests(unittest.TestCase):
    """Smoke checks for the slimmer note-list surface."""

    def test_no_notebook_helpers_exported(self) -> None:
        # The pre-tags note list exposed a number of notebook-subtree
        # helpers at module scope. The tag-based note list drops the
        # whole concept; this pins their absence.
        self.assertFalse(hasattr(note_list_module, "_expand_notebook_subtree"))
        self.assertFalse(hasattr(note_list_module, "_list_for_notebook_subtree"))


if __name__ == "__main__":
    unittest.main()
