"""Tests for :mod:`ui.toolbar`.

Three surfaces are covered here:

* a no-display smoke test pinning the absence of the removed
  notebook-breadcrumb helpers (the bulk of the toolbar's tag-aware
  ``+New`` / mode-toggle behaviour is exercised through
  :mod:`ui.test_main_window` and :mod:`controllers.test_note_controller`);
* a **display-gated** group that pins the search-entry binding: that the
  entry text and :attr:`AppState.query` track each other in both
  directions, and that repeated forward edits keep forwarding without a
  re-entrant echo. The cursor-reset reversal this binding once got wrong
  is now structural (``BIDIRECTIONAL`` suppresses the echo, see
  :mod:`ui.toolbar`), so it is pinned at the binding rather than
  re-derived through simulated per-character typing (which is also
  GTK-runtime-fragile — ``insert_text`` does not advance the cursor on
  every GTK version);
* **display-gated** groups for the collapsible centre search and the
  centre title: the search toggle is the single page switcher
  (pressed → search page + focused entry; unpressed or ``stop-search``
  → query cleared + title page + toggle unpressed), a non-empty query
  is never hidden behind the title (seeded and programmatic writes
  expand the search), and the title label mirrors the selected note —
  following ``selected-note-id`` and refreshing on the store's
  ``items-changed`` when a title is edited — with ellipsizing pinned;
* **display-gated** groups for the two promoted note/app actions — the
  note-scoped *Delete* button (trash icon; sensitive only with a
  selection) and the app-scoped *Help* button (always available;
  targets the ``app.help`` action). These replace the old *More* /
  primary-menu coverage now that both menus are gone.

The display-gating mirrors :mod:`ui.test_main_window`: each widget test
is decorated ``@unittest.skipUnless(_display_available(), ...)`` so a
run without a GDK display skips rather than fails.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from gi.repository import Gdk, Gtk, Pango

from enums import HeaderCentrePage
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
    """A repository the toolbar tests mostly never read from.

    The toolbar's search binding does not touch the repository. The
    calls the toolbar's behaviour can reach are :meth:`get` /
    :meth:`list_all` (title tracking off the store) and — via
    ``NoteListStore.update`` in the centre-title refresh test —
    :meth:`update_source`, which stands in for the real repository
    with the crudest title derivation possible (title = first line).
    Every other method raises so an inadvertent call surfaces as a
    test bug.
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
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        updated = replace(
            self.notes[note_id],
            source=source,
            title=source.splitlines()[0],
            modified_at=modified_at,
        )
        self.notes[note_id] = updated
        return updated

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


def _build_toolbar_and_store(
    app_state: AppState,
    repository: _FakeNoteRepository,
) -> tuple[Toolbar, NoteListStore]:
    """Construct a :class:`Toolbar` plus the store it observes.

    ``repository`` may be pre-populated with notes; the store loads
    them, so title-tracking tests can select and edit real rows.
    """
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
    toolbar = Toolbar(
        note_store=store,
        note_controller=controller,
        app_state=app_state,
    )
    return toolbar, store


def _build_toolbar(app_state: AppState) -> Toolbar:
    """Construct a :class:`Toolbar` wired to fake collaborators."""
    toolbar, _store = _build_toolbar_and_store(
        app_state,
        _FakeNoteRepository(),
    )
    return toolbar


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

    def test_entry_text_updates_app_state_query(self) -> None:
        # Forward direction: editing the entry's text flows into
        # AppState.query verbatim (no normalisation). Two successive
        # edits also pin loop-safety -- the bidirectional binding keeps
        # forwarding without a re-entrant echo doubling or dropping the
        # value. GObject suppresses the reverse echo within a propagation
        # cycle (see the binding in toolbar.py); a hand-rolled re-entrant
        # handler -- the design this replaced -- would have broken it.
        app_state = AppState()
        toolbar = _build_toolbar(app_state)
        entry = toolbar.search_entry

        entry.set_text("hello")
        self.assertEqual(app_state.query, "hello")

        entry.set_text("world")
        self.assertEqual(app_state.query, "world")

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


def _make_note(note_id: str, title: str) -> Note:
    """A minimal well-formed note for title-tracking fixtures."""
    return Note(
        id=note_id,
        title=title,
        source=title,
        snippet="",
        tags=(),
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW,
    )


@unittest.skipUnless(_display_available(), "no GDK display")
class HeaderSearchTests(unittest.TestCase):
    """The collapsible centre search driven by the toggle."""

    def test_default_state_is_title_with_toggle_unpressed(self) -> None:
        toolbar = _build_toolbar(AppState())
        self.assertEqual(toolbar.centre_page, HeaderCentrePage.TITLE)
        self.assertFalse(toolbar.search_toggle.get_active())

    def test_pressing_toggle_shows_search_page(self) -> None:
        toolbar = _build_toolbar(AppState())
        toolbar.search_toggle.set_active(True)
        self.assertEqual(toolbar.centre_page, HeaderCentrePage.SEARCH)

    def test_unpressing_toggle_clears_query_and_shows_title(self) -> None:
        # Collapsing must clear the query through the binding — a
        # collapsed search never hides a live filter behind the title.
        app_state = AppState()
        toolbar = _build_toolbar(app_state)
        toolbar.search_toggle.set_active(True)
        toolbar.search_entry.set_text("needle")
        self.assertEqual(app_state.query, "needle")

        toolbar.search_toggle.set_active(False)

        self.assertEqual(toolbar.centre_page, HeaderCentrePage.TITLE)
        self.assertEqual(app_state.query, "")
        self.assertEqual(toolbar.search_entry.get_text(), "")

    def test_stop_search_unpresses_toggle_and_collapses(self) -> None:
        # Escape reaches the toolbar as the entry's ``stop-search``
        # signal; it must unpress the toggle (its state mirrors the
        # search visibility) and clear the query like any collapse.
        app_state = AppState()
        toolbar = _build_toolbar(app_state)
        toolbar.search_toggle.set_active(True)
        toolbar.search_entry.set_text("needle")

        toolbar.search_entry.emit("stop-search")

        self.assertFalse(toolbar.search_toggle.get_active())
        self.assertEqual(toolbar.centre_page, HeaderCentrePage.TITLE)
        self.assertEqual(app_state.query, "")

    def test_seeded_query_starts_expanded(self) -> None:
        # The SYNC_CREATE seed copies a pre-populated query into the
        # entry at construction; the search must then start expanded,
        # never as a hidden filter behind the title.
        app_state = AppState()
        app_state.props.query = "seed"
        toolbar = _build_toolbar(app_state)

        self.assertEqual(toolbar.centre_page, HeaderCentrePage.SEARCH)
        self.assertTrue(toolbar.search_toggle.get_active())

    def test_programmatic_query_write_expands_search(self) -> None:
        # Same invariant for a post-construction programmatic write.
        app_state = AppState()
        toolbar = _build_toolbar(app_state)
        self.assertEqual(toolbar.centre_page, HeaderCentrePage.TITLE)

        app_state.props.query = "from-state"

        self.assertEqual(toolbar.centre_page, HeaderCentrePage.SEARCH)
        self.assertTrue(toolbar.search_toggle.get_active())

    def test_toggle_uses_search_icon_and_is_not_flat(self) -> None:
        # The toggle sits with the note-action buttons and must read
        # as one of them: a raised (never ``flat``) search-icon button.
        toolbar = _build_toolbar(AppState())
        self.assertEqual(
            toolbar.search_toggle.get_icon_name(),
            "system-search-symbolic",
        )
        self.assertFalse(toolbar.search_toggle.has_css_class("flat"))


@unittest.skipUnless(_display_available(), "no GDK display")
class CentreTitleTests(unittest.TestCase):
    """The centre title label mirroring the selected note."""

    def test_title_is_empty_without_selection(self) -> None:
        toolbar = _build_toolbar(AppState())
        self.assertEqual(toolbar.title_label.get_text(), "")

    def test_title_follows_selected_note(self) -> None:
        repository = _FakeNoteRepository()
        repository.notes["n1"] = _make_note("n1", "First note")
        repository.notes["n2"] = _make_note("n2", "Second note")
        app_state = AppState()
        toolbar, _store = _build_toolbar_and_store(app_state, repository)

        app_state.set_selected_note_id("n1")
        self.assertEqual(toolbar.title_label.get_text(), "First note")

        app_state.set_selected_note_id("n2")
        self.assertEqual(toolbar.title_label.get_text(), "Second note")

        app_state.set_selected_note_id(None)
        self.assertEqual(toolbar.title_label.get_text(), "")

    def test_title_refreshes_when_note_is_edited(self) -> None:
        # An edited title arrives through the store's ``items-changed``
        # (the selection does not change), the same channel every other
        # pane observes.
        repository = _FakeNoteRepository()
        repository.notes["n1"] = _make_note("n1", "Old title")
        app_state = AppState()
        toolbar, store = _build_toolbar_and_store(app_state, repository)
        app_state.set_selected_note_id("n1")
        self.assertEqual(toolbar.title_label.get_text(), "Old title")

        store.update("n1", "New title\nbody")

        self.assertEqual(toolbar.title_label.get_text(), "New title")

    def test_title_label_is_ellipsized_with_width_cap(self) -> None:
        # A long note title must never push the packed button groups
        # around: the label is end-ellipsized and width-capped.
        toolbar = _build_toolbar(AppState())
        self.assertEqual(
            toolbar.title_label.get_ellipsize(),
            Pango.EllipsizeMode.END,
        )
        self.assertGreater(toolbar.title_label.get_max_width_chars(), 0)


@unittest.skipUnless(_display_available(), "no GDK display")
class DeleteButtonTests(unittest.TestCase):
    """The note-scoped, standalone *Delete* button."""

    def test_delete_button_uses_trash_icon(self) -> None:
        toolbar = _build_toolbar(AppState())
        self.assertEqual(
            toolbar.delete_button.get_icon_name(),
            "user-trash-symbolic",
        )

    def test_delete_button_disabled_without_selection(self) -> None:
        # With no note selected the destructive action must be
        # unreachable — the rule the removed More menu carried.
        toolbar = _build_toolbar(AppState())
        self.assertFalse(toolbar.delete_button.get_sensitive())

    def test_delete_button_enabled_with_selection(self) -> None:
        # Selecting a note flips the button sensitive via the
        # notify::selected-note-id subscription.
        app_state = AppState()
        toolbar = _build_toolbar(app_state)

        app_state.set_selected_note_id("any-note-id")

        self.assertTrue(toolbar.delete_button.get_sensitive())


@unittest.skipUnless(_display_available(), "no GDK display")
class HelpButtonTests(unittest.TestCase):
    """The app-scoped *Help* button replacing the primary menu."""

    def test_help_button_targets_app_help_action(self) -> None:
        # The button activates the same app-scoped action the F1
        # accelerator triggers; it points at it by name rather than
        # carrying its own handler.
        toolbar = _build_toolbar(AppState())
        self.assertEqual(
            toolbar.help_button.get_action_name(),
            "app.help",
        )

    def test_help_button_label_names_the_syntax(self) -> None:
        # The label is more than a bare "?": it signals the AsciiDoc
        # syntax reference the help opens.
        toolbar = _build_toolbar(AppState())
        labels = [
            child.get_label()
            for child in _iter_descendants(toolbar.help_button)
            if isinstance(child, Gtk.Label)
        ]
        self.assertIn("Syntax", labels)


def _iter_descendants(widget: Gtk.Widget) -> list[Gtk.Widget]:
    """Flatten a widget's descendant tree (depth-first)."""
    found: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        found.extend(_iter_descendants(child))
        child = child.get_next_sibling()
    return found


if __name__ == "__main__":
    unittest.main()
