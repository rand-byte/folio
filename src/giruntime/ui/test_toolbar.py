"""Tests for :mod:`ui.toolbar`.

Two surfaces are covered here:

* a no-display smoke test pinning the absence of the removed
  notebook-breadcrumb helpers (the bulk of the toolbar's tag-aware
  ``+New`` / mode-toggle behaviour is exercised through
  :mod:`ui.test_main_window` and :mod:`controllers.test_note_controller`);
* a **display-gated** group that pins the search-entry binding — most
  importantly that typing into the entry no longer reverses characters
  (the bug this design removed) and that the entry and
  :attr:`AppState.query` track each other in both directions.

The display-gating mirrors :mod:`ui.test_main_window`: each widget test
is decorated ``@unittest.skipUnless(_display_available(), ...)`` so a
run without a GDK display skips rather than fails.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from gi.repository import Gdk, Gtk

from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
from models.attachment import Attachment
from models.note import Note
import giruntime.ui.toolbar as toolbar_module
from giruntime.ui.toolbar import Toolbar


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget
    construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Fakes — minimal protocol-conforming collaborators
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    """A repository the binding tests never read from.

    The toolbar's search binding does not touch the repository; the
    only repository call the toolbar makes is :meth:`get` from the
    delete path, which these tests do not exercise. Every method raises
    so an inadvertent call surfaces as a test bug.
    """

    notes: dict[str, Note]

    def __init__(self) -> None:
        self.notes = {}

    def list_all(self) -> list[Note]:
        return list(self.notes.values())

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]

    def list_modified_since(self, _since: datetime) -> list[Note]:
        raise NotImplementedError

    def search(self, _query: str) -> list[Note]:
        raise NotImplementedError

    def insert(self, _note: Note) -> Note:
        raise NotImplementedError

    def update_source(
        self,
        _note_id: str,
        _source: str,
        _modified_at: datetime,
    ) -> Note:
        raise NotImplementedError

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return ()


class _FakeAttachmentStore:
    """No attachment method is called by these tests."""

    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        raise NotImplementedError

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        raise NotImplementedError

    def count_for_note(self, _note_id: str) -> int:
        return 0

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError


def _build_toolbar(app_state: AppState) -> Toolbar:
    """Construct a :class:`Toolbar` wired to fake collaborators."""
    repository = _FakeNoteRepository()
    store = NoteListStore(
        repository=repository,
        clock=lambda: _FIXED_NOW,
        id_factory=lambda: "id",
    )
    store.load()
    controller = NoteController(
        note_store=store,
        attachments=_FakeAttachmentStore(),
        app_state=app_state,
    )
    return Toolbar(
        note_store=store,
        note_controller=controller,
        app_state=app_state,
    )


def _type_at_cursor(entry: Gtk.SearchEntry, text: str) -> None:
    """Insert ``text`` one character at a time at the live cursor.

    This is the faithful reproduction of typing: each character is
    inserted at the entry's *current* insertion position rather than at
    a recomputed end-of-text offset. If a reverse echo were to reset the
    cursor to 0 after each keystroke (the bug this design removed), the
    characters would land in reverse order; with the bidirectional
    binding the cursor is left undisturbed and the text stays in order.
    """
    for char in text:
        position = entry.get_position()
        entry.insert_text(char, 1, position)


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


@unittest.skipUnless(_display_available(), "no GDK display")
class SearchBindingTests(unittest.TestCase):
    """The ``query ↔ search-entry`` bidirectional binding."""

    def test_typing_keeps_characters_in_order(self) -> None:
        # Pins the reversal bug: appending characters at the cursor must
        # leave the text in typed order, not reversed. A reverse echo
        # that reset the cursor to 0 after each keystroke would turn
        # "test" into "tset"/"tte..."-style scrambles.
        app_state = AppState()
        toolbar = _build_toolbar(app_state)
        entry = toolbar.search_entry

        _type_at_cursor(entry, "test")

        self.assertEqual(entry.get_text(), "test")

    def test_entry_text_updates_app_state_query(self) -> None:
        # Forward direction: text typed into the entry flows into
        # AppState.query verbatim (no normalisation).
        app_state = AppState()
        toolbar = _build_toolbar(app_state)
        entry = toolbar.search_entry

        _type_at_cursor(entry, "hello")

        self.assertEqual(app_state.query, "hello")

    def test_programmatic_query_updates_entry(self) -> None:
        # Reverse direction: a programmatic write to AppState.query is
        # mirrored into the entry's text, confirming the binding is live
        # both ways.
        app_state = AppState()
        toolbar = _build_toolbar(app_state)
        entry = toolbar.search_entry

        app_state.props.query = "from-state"

        self.assertEqual(entry.get_text(), "from-state")

    def test_sync_create_seeds_entry_from_initial_query(self) -> None:
        # SYNC_CREATE performs the initial query -> text copy at
        # construction, so an entry built against a pre-populated query
        # shows it without any explicit sync call.
        app_state = AppState()
        app_state.props.query = "seed"
        toolbar = _build_toolbar(app_state)

        self.assertEqual(toolbar.search_entry.get_text(), "seed")


@unittest.skipUnless(_display_available(), "no GDK display")
class PrimaryMenuTests(unittest.TestCase):
    """The app-scoped primary (hamburger) menu surfaces the Help item."""

    def test_primary_menu_button_uses_open_menu_icon(self) -> None:
        toolbar = _build_toolbar(AppState())
        self.assertEqual(
            toolbar.primary_menu_button.get_icon_name(),
            "open-menu-symbolic",
        )

    def test_primary_menu_has_single_help_item(self) -> None:
        toolbar = _build_toolbar(AppState())
        menu = toolbar.primary_menu_button.get_menu_model()
        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.get_n_items(), 1)

    def test_help_item_targets_app_help_action(self) -> None:
        toolbar = _build_toolbar(AppState())
        menu = toolbar.primary_menu_button.get_menu_model()
        assert menu is not None
        action = menu.get_item_attribute_value(0, "action", None)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.get_string(), "app.help")

    def test_primary_menu_is_not_note_scoped(self) -> None:
        # Unlike the note-scoped More menu (disabled with no selection),
        # the app-scoped primary menu is always available.
        toolbar = _build_toolbar(AppState())
        self.assertTrue(toolbar.primary_menu_button.get_sensitive())


if __name__ == "__main__":
    unittest.main()
