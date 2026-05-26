"""Tests for :mod:`ui.note_list`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from controllers.app_state import AppState
from enums import NoteSortKey, NotebookIcon, SmartFilter
from models.attachment import Attachment
from models.note import Note
from models.notebook import Notebook
from search.note_filter import (
    RECENT_WINDOW_DAYS,
    NotebookSelection,
    SmartSelection,
)
from ui.note_list import (
    NoteList,
    _make_meta_line,
    _make_note_row,
    _NoteListRow,
    _NoteRowPayload,
    compute_display_notes,
    expand_notebook_selection_to_ids,
    format_date_short,
    title_for_selection,
)


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_note(  # pylint: disable=too-many-arguments
    note_id: str,
    *,
    notebook_id: str = "nb-1",
    title: str | None = None,
    snippet: str = "snippet body",
    modified_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Note:
    if modified_at is None:
        modified_at = _FIXED_NOW
    if created_at is None:
        created_at = modified_at - timedelta(days=1)
    return Note(
        id=note_id,
        title=title if title is not None else note_id,
        notebook_id=notebook_id,
        source=f"= {note_id}\n\n{snippet}\n",
        snippet=snippet,
        created_at=created_at,
        modified_at=modified_at,
    )


def _make_notebook(
    notebook_id: str,
    *,
    parent_id: str | None = None,
    name: str | None = None,
    icon: NotebookIcon = NotebookIcon.FOLDER,
) -> Notebook:
    return Notebook(
        id=notebook_id,
        name=name if name is not None else notebook_id,
        parent_id=parent_id,
        icon=icon,
    )


class _FakeNoteRepository:
    """Minimal :class:`NoteRepositoryProtocol` implementation."""

    notes: dict[str, Note]

    def __init__(self) -> None:
        self.notes = {}

    def list_all(self) -> list[Note]:
        return list(self.notes.values())

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]

    def list_by_notebook(self, notebook_id: str) -> list[Note]:
        return [n for n in self.notes.values() if n.notebook_id == notebook_id]

    def list_modified_since(self, _since: datetime) -> list[Note]:
        raise NotImplementedError

    def search(self, _query: str) -> list[Note]:
        raise NotImplementedError

    def insert(self, _note: Note) -> None:
        raise NotImplementedError

    def update_source(
        self,
        _note_id: str,
        _source: str,
        _modified_at: datetime,
    ) -> None:
        raise NotImplementedError

    def update_notebook(self, _note_id: str, _notebook_id: str) -> None:
        raise NotImplementedError

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError


class _FakeNotebookRepository:
    """Minimal :class:`NotebookRepositoryProtocol` implementation."""

    notebooks: dict[str, Notebook]
    insertion_order: list[str]

    def __init__(self) -> None:
        self.notebooks = {}
        self.insertion_order = []

    def add(self, notebook: Notebook) -> None:
        self.notebooks[notebook.id] = notebook
        if notebook.id not in self.insertion_order:
            self.insertion_order.append(notebook.id)

    def list_all(self) -> list[Notebook]:
        return [self.notebooks[nb_id] for nb_id in self.insertion_order]

    def get(self, notebook_id: str) -> Notebook:
        return self.notebooks[notebook_id]

    def insert(self, _notebook: Notebook) -> None:
        raise NotImplementedError

    def rename(self, _notebook_id: str, _new_name: str) -> None:
        raise NotImplementedError

    def set_icon(self, _notebook_id: str, _icon: NotebookIcon) -> None:
        raise NotImplementedError

    def delete_and_reparent_notes(
        self,
        _notebook_id: str,
        _target_id: str,
    ) -> None:
        raise NotImplementedError


def _fixed_clock() -> datetime:
    return _FIXED_NOW


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class FormatDateShortTests(unittest.TestCase):
    def test_april_14_renders_as_apr_14(self) -> None:
        # Anchor against the example in the design's formatDateShort.
        self.assertEqual(
            format_date_short(datetime(2026, 4, 14, tzinfo=UTC)),
            "Apr 14",
        )

    def test_january_first_uses_jan(self) -> None:
        self.assertEqual(
            format_date_short(datetime(2026, 1, 1, tzinfo=UTC)),
            "Jan 1",
        )

    def test_december_last_uses_dec(self) -> None:
        self.assertEqual(
            format_date_short(datetime(2026, 12, 31, tzinfo=UTC)),
            "Dec 31",
        )

    def test_no_zero_padding_on_day(self) -> None:
        # Pin the contract: day is unpadded — "Apr 5", not "Apr 05".
        self.assertEqual(
            format_date_short(datetime(2026, 4, 5, tzinfo=UTC)),
            "Apr 5",
        )


class ExpandNotebookSelectionToIdsTests(unittest.TestCase):
    def test_no_children_returns_only_the_id(self) -> None:
        notebooks = [_make_notebook("a"), _make_notebook("b")]
        self.assertEqual(
            expand_notebook_selection_to_ids("a", notebooks),
            ["a"],
        )

    def test_children_are_appended_in_declaration_order(self) -> None:
        notebooks = [
            _make_notebook("parent"),
            _make_notebook("child-1", parent_id="parent"),
            _make_notebook("child-2", parent_id="parent"),
            _make_notebook("unrelated"),
        ]
        self.assertEqual(
            expand_notebook_selection_to_ids("parent", notebooks),
            ["parent", "child-1", "child-2"],
        )

    def test_unknown_id_returns_only_the_id(self) -> None:
        # The function does not fail on a stale id — it just returns
        # the id alone, and the caller (compute_display_notes) reads
        # zero notes from list_by_notebook for it.
        notebooks = [_make_notebook("a")]
        self.assertEqual(
            expand_notebook_selection_to_ids("missing", notebooks),
            ["missing"],
        )


class TitleForSelectionTests(unittest.TestCase):
    def test_smart_all_uses_all_notes_label(self) -> None:
        self.assertEqual(
            title_for_selection(
                SmartSelection(smart_filter=SmartFilter.ALL),
                _FakeNotebookRepository(),
            ),
            "All notes",
        )

    def test_smart_recent_uses_recent_label(self) -> None:
        self.assertEqual(
            title_for_selection(
                SmartSelection(smart_filter=SmartFilter.RECENT),
                _FakeNotebookRepository(),
            ),
            "Recent",
        )

    def test_notebook_selection_uses_notebook_name(self) -> None:
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="Personal"))
        self.assertEqual(
            title_for_selection(
                NotebookSelection(notebook_id="nb-1"),
                notebooks,
            ),
            "Personal",
        )

    def test_unknown_notebook_id_falls_back_to_empty_string(self) -> None:
        # KeyError must be swallowed — the note-list will simul-
        # taneously empty out, and a header label that loudly
        # complains about a stale id is the wrong place to do it.
        self.assertEqual(
            title_for_selection(
                NotebookSelection(notebook_id="never-existed"),
                _FakeNotebookRepository(),
            ),
            "",
        )


class ComputeDisplayNotesTests(unittest.TestCase):
    def _setup_recipes(
        self,
    ) -> tuple[_FakeNoteRepository, _FakeNotebookRepository]:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-recipes", name="Recipes"))
        notebooks.add(
            _make_notebook("nb-baking", name="Baking", parent_id="nb-recipes")
        )
        notebooks.add(_make_notebook("nb-personal", name="Personal"))
        notes.notes["n-r1"] = _make_note("n-r1", notebook_id="nb-recipes")
        notes.notes["n-b1"] = _make_note("n-b1", notebook_id="nb-baking")
        notes.notes["n-p1"] = _make_note("n-p1", notebook_id="nb-personal")
        return notes, notebooks

    def test_smart_all_passes_through_every_note(self) -> None:
        notes, notebooks = self._setup_recipes()
        result = compute_display_notes(
            note_repository=notes,
            notebook_repository=notebooks,
            selection=SmartSelection(smart_filter=SmartFilter.ALL),
            query="",
            sort_key=NoteSortKey.MODIFIED,
            now=_FIXED_NOW,
        )
        self.assertEqual({n.id for n in result}, {"n-r1", "n-b1", "n-p1"})

    def test_smart_recent_filters_by_modified_at(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1"))
        notes.notes["recent"] = _make_note(
            "recent",
            notebook_id="nb-1",
            modified_at=_FIXED_NOW,
        )
        notes.notes["old"] = _make_note(
            "old",
            notebook_id="nb-1",
            modified_at=_FIXED_NOW - timedelta(days=RECENT_WINDOW_DAYS + 1),
        )
        result = compute_display_notes(
            note_repository=notes,
            notebook_repository=notebooks,
            selection=SmartSelection(smart_filter=SmartFilter.RECENT),
            query="",
            sort_key=NoteSortKey.MODIFIED,
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["recent"])

    def test_notebook_selection_includes_children(self) -> None:
        notes, notebooks = self._setup_recipes()
        result = compute_display_notes(
            note_repository=notes,
            notebook_repository=notebooks,
            selection=NotebookSelection(notebook_id="nb-recipes"),
            query="",
            sort_key=NoteSortKey.MODIFIED,
            now=_FIXED_NOW,
        )
        self.assertEqual({n.id for n in result}, {"n-r1", "n-b1"})

    def test_notebook_selection_without_children_is_just_that_notebook(
        self,
    ) -> None:
        notes, notebooks = self._setup_recipes()
        result = compute_display_notes(
            note_repository=notes,
            notebook_repository=notebooks,
            selection=NotebookSelection(notebook_id="nb-personal"),
            query="",
            sort_key=NoteSortKey.MODIFIED,
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["n-p1"])

    def test_query_filters_by_substring(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1"))
        notes.notes["sourdough"] = _make_note(
            "sourdough",
            notebook_id="nb-1",
            title="Sourdough country loaf",
            snippet="bread",
        )
        notes.notes["pasta"] = _make_note(
            "pasta",
            notebook_id="nb-1",
            title="Pasta primavera",
            snippet="quick weeknight",
        )
        result = compute_display_notes(
            note_repository=notes,
            notebook_repository=notebooks,
            selection=SmartSelection(smart_filter=SmartFilter.ALL),
            query="sourdough",
            sort_key=NoteSortKey.MODIFIED,
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["sourdough"])

    def test_sort_key_title_orders_alphabetically(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1"))
        notes.notes["b"] = _make_note("b", notebook_id="nb-1", title="bbb")
        notes.notes["a"] = _make_note("a", notebook_id="nb-1", title="aaa")
        result = compute_display_notes(
            note_repository=notes,
            notebook_repository=notebooks,
            selection=SmartSelection(smart_filter=SmartFilter.ALL),
            query="",
            sort_key=NoteSortKey.TITLE,
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["a", "b"])

    def test_sort_key_modified_orders_newest_first(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1"))
        notes.notes["older"] = _make_note(
            "older",
            notebook_id="nb-1",
            modified_at=_FIXED_NOW - timedelta(days=2),
        )
        notes.notes["newer"] = _make_note(
            "newer",
            notebook_id="nb-1",
            modified_at=_FIXED_NOW,
        )
        result = compute_display_notes(
            note_repository=notes,
            notebook_repository=notebooks,
            selection=SmartSelection(smart_filter=SmartFilter.ALL),
            query="",
            sort_key=NoteSortKey.MODIFIED,
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["newer", "older"])


# ---------------------------------------------------------------------------
# Widget tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListConstructionTests(unittest.TestCase):
    def test_constructs_with_empty_repositories(self) -> None:
        widget = NoteList(
            note_repository=_FakeNoteRepository(),
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        self.assertIsInstance(widget, Gtk.Box)

    def test_default_sort_key_is_modified(self) -> None:
        widget = NoteList(
            note_repository=_FakeNoteRepository(),
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        self.assertEqual(widget.sort_key, NoteSortKey.MODIFIED)


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListHeaderTests(unittest.TestCase):
    def test_initial_title_for_default_selection_is_all_notes(self) -> None:
        widget = NoteList(
            note_repository=_FakeNoteRepository(),
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        self.assertEqual(widget._title_label.get_text(), "All notes")

    def test_title_reflects_notebook_selection(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="Personal"))
        app_state = AppState(
            initial_selection=NotebookSelection(notebook_id="nb-1"),
        )
        widget = NoteList(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=app_state,
            clock=_fixed_clock,
        )
        self.assertEqual(widget._title_label.get_text(), "Personal")

    def test_count_label_reflects_filtered_list(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notes.notes["a"] = _make_note("a")
        notes.notes["b"] = _make_note("b")
        notes.notes["c"] = _make_note("c")
        widget = NoteList(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        self.assertEqual(widget._count_label.get_text(), "3")


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListRowTests(unittest.TestCase):
    def test_one_row_per_note(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["a"] = _make_note("a")
        notes.notes["b"] = _make_note("b")
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        self.assertEqual(set(widget._row_for_note_id.keys()), {"a", "b"})

    def test_every_row_carries_a_typed_payload(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["a"] = _make_note("a")
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        row = widget._row_for_note_id["a"]
        self.assertIsInstance(row, _NoteListRow)
        self.assertIsInstance(row.payload, _NoteRowPayload)
        self.assertEqual(row.payload.note_id, "a")

    def test_empty_filtered_list_shows_empty_state(self) -> None:
        widget = NoteList(
            note_repository=_FakeNoteRepository(),
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        self.assertTrue(widget._empty_label.get_visible())
        self.assertFalse(widget._list_box.get_visible())

    def test_non_empty_filtered_list_hides_empty_state(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["a"] = _make_note("a")
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        self.assertFalse(widget._empty_label.get_visible())
        self.assertTrue(widget._list_box.get_visible())


class _FakeAttachmentStore:
    """Minimal attachment store returning counts from a fixed mapping.

    Only :meth:`count_for_note` is exercised by :class:`NoteList`; the
    remaining protocol methods are present so the fake conforms to
    :class:`AttachmentStoreProtocol` but raise if ever called.
    """

    counts: dict[str, int]

    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self.counts = counts if counts is not None else {}

    def count_for_note(self, note_id: str) -> int:
        return self.counts.get(note_id, 0)

    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        raise NotImplementedError

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        raise NotImplementedError

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError


def _row_labels(row: _NoteListRow) -> list[Gtk.Label]:
    """Collect every :class:`Gtk.Label` descendant of a row, in order."""
    labels: list[Gtk.Label] = []

    def walk(widget: Gtk.Widget) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                labels.append(child)
            walk(child)
            child = child.get_next_sibling()

    walk(row)
    return labels


def _meta_texts(note: Note, attachment_count: int) -> list[str]:
    """Return the meta line's label texts left-to-right."""
    meta = _make_meta_line(note, attachment_count)
    texts: list[str] = []
    child = meta.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            texts.append(child.get_text())
        child = child.get_next_sibling()
    return texts


class NoteRowPresentationTests(unittest.TestCase):
    """Bold title, two-line dim snippet, and the meta line layout."""

    def test_title_label_is_bold_via_css_class(self) -> None:
        row = _make_note_row(_make_note("a", title="My Title"), 0)
        labels = _row_labels(row)
        title_label = labels[0]
        self.assertEqual(title_label.get_text(), "My Title")
        self.assertTrue(title_label.has_css_class("note-title"))

    def test_snippet_is_two_lines_dim_and_wrapping(self) -> None:
        row = _make_note_row(_make_note("a", snippet="Some preview text"), 0)
        snippet_label = next(
            label for label in _row_labels(row)
            if label.has_css_class("note-snippet")
        )
        self.assertEqual(snippet_label.get_lines(), 2)
        self.assertTrue(snippet_label.get_wrap())
        self.assertEqual(snippet_label.get_ellipsize(), Pango.EllipsizeMode.END)

    def test_row_without_snippet_omits_snippet_label(self) -> None:
        row = _make_note_row(_make_note("a", snippet=""), 0)
        self.assertFalse(
            any(label.has_css_class("note-snippet") for label in _row_labels(row))
        )

    def test_meta_line_date_only_when_no_attachments(self) -> None:
        note = _make_note("a", modified_at=datetime(2026, 4, 14, tzinfo=UTC))
        self.assertEqual(_meta_texts(note, 0), ["Apr 14"])

    def test_meta_line_shows_paperclip_count_separator_and_date(self) -> None:
        note = _make_note("a", modified_at=datetime(2026, 4, 14, tzinfo=UTC))
        self.assertEqual(_meta_texts(note, 2), ["\U0001f4ce 2", "|", "Apr 14"])

    def test_meta_count_and_date_share_dim_class(self) -> None:
        note = _make_note("a")
        meta = _make_meta_line(note, 3)
        dim = [
            child for child in _iter_children(meta)
            if isinstance(child, Gtk.Label) and child.has_css_class("note-meta")
        ]
        # Paperclip-count, separator, and date all carry .note-meta.
        self.assertEqual(len(dim), 3)

    def test_separator_carries_its_own_low_opacity_class(self) -> None:
        note = _make_note("a")
        meta = _make_meta_line(note, 1)
        separator = next(
            child for child in _iter_children(meta)
            if isinstance(child, Gtk.Label) and child.get_text() == "|"
        )
        self.assertTrue(separator.has_css_class("note-meta-separator"))

    def test_note_list_wires_counts_from_attachment_store(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["a"] = _make_note("a")
        store = _FakeAttachmentStore({"a": 4})
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
            attachment_store=store,
        )
        row = widget._row_for_note_id["a"]
        texts = {label.get_text() for label in _row_labels(row)}
        self.assertIn("\U0001f4ce 4", texts)

    def test_note_list_without_store_shows_no_paperclip(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["a"] = _make_note("a")
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        row = widget._row_for_note_id["a"]
        self.assertFalse(
            any("\U0001f4ce" in label.get_text() for label in _row_labels(row))
        )


def _iter_children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    """Return a widget's direct children left-to-right."""
    out: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListSelectionPlumbingTests(unittest.TestCase):
    def test_row_activation_updates_app_state(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["a"] = _make_note("a")
        app_state = AppState()
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=app_state,
            clock=_fixed_clock,
        )
        widget._on_row_activated(
            widget._list_box,
            widget._row_for_note_id["a"],
        )
        self.assertEqual(app_state.selected_note_id, "a")

    def test_app_state_selected_note_change_highlights_row(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["a"] = _make_note("a")
        notes.notes["b"] = _make_note("b")
        app_state = AppState()
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=app_state,
            clock=_fixed_clock,
        )
        app_state.set_selected_note_id("b")
        self.assertIs(
            widget._list_box.get_selected_row(),
            widget._row_for_note_id["b"],
        )

    def test_selected_note_id_outside_filtered_set_unselects(self) -> None:
        # If AppState carries a selected_note_id whose note is no
        # longer in the displayed list (e.g. it was filtered out by
        # a query), the list-box must show no selected row.
        notes = _FakeNoteRepository()
        notes.notes["visible"] = _make_note("visible")
        app_state = AppState()
        app_state.set_selected_note_id("not-in-list")
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=app_state,
            clock=_fixed_clock,
        )
        self.assertIsNone(widget._list_box.get_selected_row())

    def test_selection_change_refreshes_list(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-a", name="A"))
        notebooks.add(_make_notebook("nb-b", name="B"))
        notes.notes["a1"] = _make_note("a1", notebook_id="nb-a")
        notes.notes["b1"] = _make_note("b1", notebook_id="nb-b")
        app_state = AppState()  # default = SmartFilter.ALL
        widget = NoteList(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=app_state,
            clock=_fixed_clock,
        )
        self.assertEqual(set(widget._row_for_note_id.keys()), {"a1", "b1"})

        app_state.set_selection(NotebookSelection(notebook_id="nb-a"))
        self.assertEqual(set(widget._row_for_note_id.keys()), {"a1"})
        self.assertEqual(widget._title_label.get_text(), "A")

    def test_query_change_refreshes_list(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["sourdough"] = _make_note(
            "sourdough",
            title="Sourdough country loaf",
        )
        notes.notes["pasta"] = _make_note("pasta", title="Pasta primavera")
        app_state = AppState()
        widget = NoteList(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=app_state,
            clock=_fixed_clock,
        )
        self.assertEqual(
            set(widget._row_for_note_id.keys()),
            {"sourdough", "pasta"},
        )

        app_state.set_query("sourdough")
        self.assertEqual(set(widget._row_for_note_id.keys()), {"sourdough"})


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListSortDropdownTests(unittest.TestCase):
    def test_changing_dropdown_changes_sort_key(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notes.notes["b"] = _make_note(
            "b",
            title="bbb",
            modified_at=_FIXED_NOW - timedelta(days=1),
        )
        notes.notes["a"] = _make_note("a", title="aaa", modified_at=_FIXED_NOW)
        widget = NoteList(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        # Default MODIFIED → newer first.
        self.assertEqual(widget.sort_key, NoteSortKey.MODIFIED)

        # Drive the dropdown selection to TITLE (index 2 in
        # _SORT_KEY_DROPDOWN_ORDER).
        widget._sort_dropdown.set_selected(2)
        self.assertEqual(widget.sort_key, NoteSortKey.TITLE)


if __name__ == "__main__":
    unittest.main()
