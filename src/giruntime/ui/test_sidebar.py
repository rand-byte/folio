"""Tests for :mod:`ui.sidebar`.

Two layers are covered here:

* The pure, display-free helpers — :func:`count_untagged` and
  :func:`tags_header_accent_text`. The latter locks the
  ``"(N selected)"`` wording that regressed to invisible, independently
  of GTK.
* The widget-level selection rendering — that a selected tag row shows
  the leading ✓ (opacity 1.0) while an unselected one reserves the
  column (opacity 0.0), that the check image carries the icon name and
  CSS class the stylesheet keys off, that the Tags list carries the
  scoping class, and that the header accent label tracks the selection.
  These build a real :class:`Sidebar` and so are gated behind a GDK
  display (they *skip* without one — see ``README.md`` §5).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from gi.repository import Gdk, GLib, Gtk

from asciidoc.summary import derive_summary
from enums import SmartFilter
from models.note import Note
from search.note_filter import TagSelection
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import make_initial_source
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui.sidebar import (
    Sidebar,
    count_untagged,
    tags_header_accent_text,
)


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget
    construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


class CountUntaggedTests(unittest.TestCase):
    """:func:`count_untagged` is a pure helper — verifiable
    without GTK."""

    def test_empty_list(self) -> None:
        self.assertEqual(count_untagged([]), 0)

    def test_counts_notes_with_empty_tags_tuple(self) -> None:
        class _Stub:
            def __init__(self, tags: tuple[str, ...]) -> None:
                self.tags = tags

        notes = [_Stub(()), _Stub(("a",)), _Stub(()), _Stub(("b", "c"))]
        self.assertEqual(count_untagged(notes), 2)

    def test_tags_attribute_optional(self) -> None:
        # Anything without a ``tags`` attribute is treated as
        # untagged — matches the SmartFilter.UNTAGGED predicate.
        notes = [object(), object()]
        self.assertEqual(count_untagged(notes), 2)


class TagsHeaderAccentTextTests(unittest.TestCase):
    """:func:`tags_header_accent_text` is a pure helper — it is the
    single source of the Tags-header parenthetical wording, so this
    locks the string that regressed to invisible.
    """

    def test_zero_selected_is_empty(self) -> None:
        self.assertEqual(tags_header_accent_text(0), "")

    def test_negative_is_empty(self) -> None:
        # Defensive: a non-positive count never renders an accent.
        self.assertEqual(tags_header_accent_text(-1), "")

    def test_one_selected(self) -> None:
        self.assertEqual(tags_header_accent_text(1), "(1 selected)")

    def test_two_selected(self) -> None:
        self.assertEqual(tags_header_accent_text(2), "(2 selected)")


class _FakeNoteRepository:
    """Minimal :class:`NoteRepositoryProtocol` for the sidebar.

    The sidebar only reads :meth:`list_all` (which seeds the store that
    both the Library counts and the derived Tags rows are computed from);
    every other method raising :class:`NotImplementedError` makes any
    inadvertent call a loud test bug rather than a silent pass.
    """

    notes: list[Note]

    def __init__(self, *, notes: list[Note]) -> None:
        self.notes = notes

    def list_all(self) -> list[Note]:
        return list(self.notes)

    def get(self, note_id: str) -> Note:
        raise NotImplementedError

    def insert(self, note: Note) -> Note:
        raise NotImplementedError

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        raise NotImplementedError

    def delete(self, note_id: str) -> None:
        raise NotImplementedError


def _note_with_tags(note_id: str, tags: tuple[str, ...]) -> Note:
    return Note(
        id=note_id,
        title=f"note {note_id}",
        source="",
        snippet="",
        tags=tags,
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW,
    )


def _pump(iterations: int = 200) -> None:
    """Drive the default main context so the ListView realises its
    rows. Non-blocking iterations keep the pump bounded and crash-proof
    under the cairo software renderer."""
    context = GLib.MainContext.default()
    for _ in range(iterations):
        context.iteration(False)


def _tag_position(sidebar: Sidebar, name: str) -> int:
    """Return the store position of the tag row labelled ``name``."""
    store = sidebar.tag_model
    for index in range(store.get_n_items()):
        item = store.get_item(index)
        if getattr(item, "name", None) == name:
            return index
    raise AssertionError(f"tag {name!r} not found in the store")


@unittest.skipUnless(_display_available(), "no GDK display")
class SidebarSelectionRenderingTests(unittest.TestCase):
    """A selected tag row reads as the theme selection pill (no leading
    ✓), and the header accent tracks the tag selection. Builds a real
    :class:`Sidebar` realised in a window so the factory's rows exist."""

    app_state: AppState
    sidebar: Sidebar
    window: Gtk.Window

    def setUp(self) -> None:
        repository = _FakeNoteRepository(
            notes=[
                _note_with_tags("1", ("baking", "bread")),
                _note_with_tags("2", ("bread",)),
            ],
        )
        store = NoteListStore(repository=repository)
        store.load()
        self.app_state = AppState()
        self.sidebar = Sidebar(
            note_store=store,
            app_state=self.app_state,
        )
        self.window = Gtk.Window()
        self.window.set_child(self.sidebar)
        self.window.present()
        _pump()

    def tearDown(self) -> None:
        self.window.set_child(None)
        self.window.destroy()
        _pump(20)

    def test_selected_tag_is_in_the_model_selection(self) -> None:
        # With the leading ✓ gone, "selected" is read straight off the
        # model: the row carries the theme pill because GTK paints
        # ``row:selected`` for items the MultiSelection holds.
        self.app_state.toggle_tag("baking")
        _pump()
        baking_pos = _tag_position(self.sidebar, "baking")
        bread_pos = _tag_position(self.sidebar, "bread")
        self.assertTrue(self.sidebar.tag_selection.is_selected(baking_pos))
        self.assertFalse(self.sidebar.tag_selection.is_selected(bread_pos))

    def test_header_accent_visible_when_selected_hidden_when_not(self) -> None:
        accent = self.sidebar.tags_header_box.get_last_child()
        assert isinstance(accent, Gtk.Label)
        # Nothing selected initially: accent hidden.
        self.assertFalse(accent.get_visible())

        self.app_state.toggle_tag("bread")
        _pump()
        self.assertTrue(accent.get_visible())
        self.assertEqual(accent.get_text(), "(1 selected)")

        # Returning to a smart selection clears the tag set and hides it.
        self.app_state.set_smart(SmartFilter.ALL)
        _pump()
        self.assertFalse(accent.get_visible())
        self.assertEqual(accent.get_text(), "")


@unittest.skipUnless(_display_available(), "no GDK display")
class SidebarMultiSelectTests(unittest.TestCase):
    """A plain single click toggles a tag additively — no modifier key.

    Drives :meth:`Sidebar._on_tag_row_clicked` (the per-row gesture's
    callback) for two distinct positions and asserts the model, the
    mirrored :class:`AppState` selection, and the header accent all
    reflect a two-tag selection.
    """

    app_state: AppState
    sidebar: Sidebar
    window: Gtk.Window

    def setUp(self) -> None:
        repository = _FakeNoteRepository(
            notes=[
                _note_with_tags("1", ("baking", "bread")),
                _note_with_tags("2", ("bread",)),
            ],
        )
        store = NoteListStore(repository=repository)
        store.load()
        self.app_state = AppState()
        self.sidebar = Sidebar(
            note_store=store,
            app_state=self.app_state,
        )
        self.window = Gtk.Window()
        self.window.set_child(self.sidebar)
        self.window.present()
        _pump()

    def tearDown(self) -> None:
        self.window.set_child(None)
        self.window.destroy()
        _pump(20)

    def test_two_single_clicks_select_both_tags(self) -> None:
        baking_pos = _tag_position(self.sidebar, "baking")
        bread_pos = _tag_position(self.sidebar, "bread")

        # Two plain clicks on two different rows — no modifier.
        self.sidebar._on_tag_row_clicked(baking_pos)
        _pump()
        self.sidebar._on_tag_row_clicked(bread_pos)
        _pump()

        # (a) Both selected in the model.
        self.assertTrue(self.sidebar.tag_selection.is_selected(baking_pos))
        self.assertTrue(self.sidebar.tag_selection.is_selected(bread_pos))

        # (b) AppState mirrors a TagSelection holding both names.
        selection = self.app_state.selection
        self.assertIsInstance(selection, TagSelection)
        assert isinstance(selection, TagSelection)  # for mypy/pylint
        self.assertEqual(selection.tags, frozenset({"baking", "bread"}))

        # (c) The Tags header accent reads "(2 selected)".
        accent = self.sidebar.tags_header_box.get_last_child()
        assert isinstance(accent, Gtk.Label)
        self.assertEqual(accent.get_text(), "(2 selected)")

    def test_clicking_a_selected_row_again_deselects_only_it(self) -> None:
        baking_pos = _tag_position(self.sidebar, "baking")
        bread_pos = _tag_position(self.sidebar, "bread")

        self.sidebar._on_tag_row_clicked(baking_pos)
        _pump()
        self.sidebar._on_tag_row_clicked(bread_pos)
        _pump()
        # Re-click baking — it toggles off, bread stays.
        self.sidebar._on_tag_row_clicked(baking_pos)
        _pump()

        self.assertFalse(self.sidebar.tag_selection.is_selected(baking_pos))
        self.assertTrue(self.sidebar.tag_selection.is_selected(bread_pos))
        selection = self.app_state.selection
        self.assertIsInstance(selection, TagSelection)
        assert isinstance(selection, TagSelection)  # for mypy/pylint
        self.assertEqual(selection.tags, frozenset({"bread"}))


class _WritableNoteRepository:
    """A note repository whose ``insert`` actually persists.

    ``list_all`` seeds the store and ``insert`` derives the cached
    ``(title, snippet, tags)`` from the source exactly as the real
    :class:`storage.note_repository.NoteRepository` does — so a
    ``store.create`` lands a note carrying its parsed ``:tags:``,
    driving the same ``items-changed`` the production path emits. Every
    other protocol method is unused here and raises.
    """

    _notes: list[Note]

    def __init__(self, notes: list[Note]) -> None:
        self._notes = list(notes)

    def list_all(self) -> list[Note]:
        return list(self._notes)

    def insert(self, note: Note) -> Note:
        summary = derive_summary(note.source)
        persisted = Note(
            id=note.id,
            title=summary.title,
            source=note.source,
            snippet=summary.snippet,
            tags=summary.tags,
            created_at=note.created_at,
            modified_at=note.modified_at,
        )
        self._notes.append(persisted)
        return persisted

    def get(self, note_id: str) -> Note:
        raise NotImplementedError

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        raise NotImplementedError

    def delete(self, note_id: str) -> None:
        raise NotImplementedError


def _tag_row_count_text(sidebar: Sidebar, name: str) -> str:
    """The rendered count text on the realised tag row for ``name``.

    Walks the Tags ``ListView``'s realised widget subtree, finds the
    ``#name`` label, and reads its sibling count label — i.e. what the
    user actually sees, not the model's ``count`` (which was always
    right; the regression was the *label* going stale).
    """
    wanted = f"#{name}"
    stack: list[Gtk.Widget | None] = [sidebar.tag_list_view.get_first_child()]
    while stack:
        widget = stack.pop()
        if widget is None:
            continue
        if isinstance(widget, Gtk.Label) and widget.get_text() == wanted:
            box = widget.get_parent()
            assert isinstance(box, Gtk.Box)
            count_label = box.get_last_child()
            assert isinstance(count_label, Gtk.Label)
            return str(count_label.get_text())
        stack.append(widget.get_next_sibling())
        stack.append(widget.get_first_child())
    raise AssertionError(f"no realised tag row for {name!r}")


@unittest.skipUnless(_display_available(), "no GDK display")
class SidebarTagCountLiveUpdateTests(unittest.TestCase):
    """A new note carrying an existing tag bumps that tag's *rendered*
    count live.

    Regression: the row's count label was painted once at ``bind`` time,
    but a same-tag bump (``1 -> 2``) is a count-only ``notify::count`` on
    the existing :class:`TagItem` — no ``items-changed``, so GTK never
    re-binds the row — and the label stayed at ``1``. The factory now
    binds ``TagItem:count`` to the label, so the displayed text tracks
    the model.
    """

    app_state: AppState
    store: NoteListStore
    sidebar: Sidebar
    window: Gtk.Window

    def setUp(self) -> None:
        repository = _WritableNoteRepository(
            notes=[_note_with_tags("1", ("work",))],
        )
        self.store = NoteListStore(repository=repository)
        self.store.load()
        self.app_state = AppState()
        self.sidebar = Sidebar(
            note_store=self.store,
            app_state=self.app_state,
        )
        self.window = Gtk.Window()
        self.window.set_child(self.sidebar)
        self.window.present()
        _pump()

    def tearDown(self) -> None:
        self.window.set_child(None)
        self.window.destroy()
        _pump(20)

    def test_second_note_with_existing_tag_updates_count_label(self) -> None:
        # Precondition: the lone "work" note renders a count of 1.
        self.assertEqual(_tag_row_count_text(self.sidebar, "work"), "1")

        # Create a second note carrying the same tag — exactly what the
        # toolbar's "+ New" does while a tag is selected.
        source = make_initial_source(TagSelection(frozenset({"work"})))
        self.store.create(source)
        _pump()

        # The store holds two notes and — the regression point — the
        # rendered label now reads 2 rather than the stale bind-time 1.
        self.assertEqual(self.store.get_n_items(), 2)
        self.assertEqual(_tag_row_count_text(self.sidebar, "work"), "2")


if __name__ == "__main__":
    unittest.main()
