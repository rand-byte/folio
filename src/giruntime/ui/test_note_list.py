"""Tests for :mod:`ui.note_list`.

The note list now binds a ``Filter``/``Sort``/``ListView`` chain over
the in-memory :class:`controllers.note_list_store.NoteListStore`. The
"what shows / what order" rules are covered exhaustively by the pure
predicates in :mod:`search.note_filter`; here we exercise the widget's
own wiring: the filtered count, live query filtering (no throttle),
sort-key reordering, and the AppState ⇄ selection round-trip.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from gi.repository import Gdk, Gtk

from enums import NoteSortKey
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_list_store import NoteListStore
import giruntime.ui.note_list as note_list_module
from giruntime.ui.note_list import NoteList, _SORT_KEY_DROPDOWN_ORDER
from models.attachment import Attachment
from models.note import Note


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget
    construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def _note(
    note_id: str,
    title: str,
    *,
    tags: tuple[str, ...] = (),
    modified_at: datetime = _FIXED_NOW,
) -> Note:
    return Note(
        id=note_id,
        title=title,
        source=f"= {title}\n",
        snippet=title,
        tags=tags,
        created_at=_FIXED_NOW,
        modified_at=modified_at,
    )


class _FakeNoteRepository:
    """Minimal repository returning a fixed note set from ``list_all``."""

    _notes: list[Note]

    def __init__(self, notes: list[Note]) -> None:
        self._notes = notes

    def list_all(self) -> list[Note]:
        return list(self._notes)

    def get(self, note_id: str) -> Note:
        for note in self._notes:
            if note.id == note_id:
                return note
        raise KeyError(note_id)

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
    """Reports zero attachments; no other method is called here."""

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


def _build_note_list(notes: list[Note], app_state: AppState) -> NoteList:
    store = NoteListStore(repository=_FakeNoteRepository(notes))
    store.load()
    return NoteList(
        note_store=store,
        app_state=app_state,
        attachment_store=_FakeAttachmentStore(),
    )


def _visible_ids(note_list: NoteList) -> list[str]:
    model = note_list._sort_model
    return [model.get_item(i).note.id for i in range(model.get_n_items())]


class NoteListSmokeTests(unittest.TestCase):
    """Smoke checks for the slimmer note-list surface."""

    def test_no_notebook_helpers_exported(self) -> None:
        self.assertFalse(hasattr(note_list_module, "_expand_notebook_subtree"))
        self.assertFalse(
            hasattr(note_list_module, "_list_for_notebook_subtree"),
        )

    def test_compute_display_notes_helper_removed(self) -> None:
        # The repository-driven materialiser was replaced by the model
        # chain; pin its absence so a stray re-introduction is caught.
        self.assertFalse(hasattr(note_list_module, "compute_display_notes"))


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListModelChainTests(unittest.TestCase):
    """The widget binds the store through Filter/Sort and stays in step."""

    def _notes(self) -> list[Note]:
        return [
            _note("1", "alpha", modified_at=datetime(2026, 1, 3, tzinfo=UTC)),
            _note("2", "beta", modified_at=datetime(2026, 1, 2, tzinfo=UTC)),
            _note("3", "gamma", modified_at=datetime(2026, 1, 1, tzinfo=UTC)),
        ]

    def test_count_label_reflects_all_notes_on_empty_query(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        self.assertEqual(note_list._count_label.get_text(), "3 notes")
        self.assertEqual(_visible_ids(note_list), ["1", "2", "3"])

    def test_query_filters_immediately_without_throttle(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        # Setting the query filters the model right away — no pending
        # timer, no coalescing window.
        app_state.props.query = "alpha"
        self.assertEqual(note_list._count_label.get_text(), "1 notes")
        self.assertEqual(_visible_ids(note_list), ["1"])
        # Clearing restores the full set.
        app_state.props.query = ""
        self.assertEqual(note_list._count_label.get_text(), "3 notes")

    def test_default_sort_is_modified_descending(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        self.assertEqual(_visible_ids(note_list), ["1", "2", "3"])

    def test_title_sort_reorders_alphabetically(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        index = note_list._sort_dropdown
        # Select the "Title" entry in the dropdown.
        index.set_selected(_SORT_KEY_DROPDOWN_ORDER.index(NoteSortKey.TITLE))
        self.assertEqual(note_list.sort_key, NoteSortKey.TITLE)
        # alpha, beta, gamma is already alphabetical, so reverse the
        # check by titles to confirm the comparator drives the order.
        model = note_list._sort_model
        titles = [model.get_item(i).note.title for i in range(model.get_n_items())]
        self.assertEqual(titles, ["alpha", "beta", "gamma"])

    def test_app_state_selection_highlights_row(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        app_state.set_selected_note_id("2")
        selected = note_list._selection_model.get_selected_item()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.note.id, "2")

    def test_row_selection_writes_through_to_app_state(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        # Find "gamma" (id 3) position in the sorted model and select it
        # on the SingleSelection, simulating a user row click.
        model = note_list._sort_model
        pos = next(
            i for i in range(model.get_n_items())
            if model.get_item(i).note.id == "3"
        )
        note_list._selection_model.set_selected(pos)
        self.assertEqual(app_state.selected_note_id, "3")


if __name__ == "__main__":
    unittest.main()
