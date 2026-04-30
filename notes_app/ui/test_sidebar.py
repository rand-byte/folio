"""Tests for :mod:`notes_app.ui.sidebar`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.enums import NotebookIcon, SmartFilter
from notes_app.models.note import Note
from notes_app.models.notebook import Notebook
from notes_app.search.note_filter import (
    RECENT_WINDOW_DAYS,
    NotebookSelection,
    SmartSelection,
)
from notes_app.ui.sidebar import (
    Sidebar,
    _NotebookRowPayload,
    _SidebarRow,
    _SmartRowPayload,
    _children_of,
    _count_notebook,
    _count_smart_filter,
    _icon_name_for_notebook,
    _top_level_notebooks,
)


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_note(
    note_id: str,
    *,
    notebook_id: str = "nb-1",
    modified_at: datetime | None = None,
) -> Note:
    """Build a deterministic :class:`Note` for tests."""
    if modified_at is None:
        modified_at = _FIXED_NOW
    return Note(
        id=note_id,
        title=note_id,
        notebook_id=notebook_id,
        source=f"= {note_id}\n\nbody.\n",
        snippet="body.",
        created_at=_FIXED_NOW,
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
    """:class:`NoteRepositoryProtocol` minimal in-memory impl."""

    notes: dict[str, Note]

    def __init__(self) -> None:
        self.notes = {}

    def list_all(self) -> list[Note]:
        # Sidebar relies only on list_all in step 9.
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
    """:class:`NotebookRepositoryProtocol` minimal in-memory impl."""

    notebooks: dict[str, Notebook]
    insertion_order: list[str]

    def __init__(self) -> None:
        self.notebooks = {}
        self.insertion_order = []

    def add(self, notebook: Notebook) -> None:
        """Test helper — bypass the protocol and seed directly."""
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
# Pure helpers — no GTK needed
# ---------------------------------------------------------------------------


class CountSmartFilterTests(unittest.TestCase):
    def test_all_returns_total_count(self) -> None:
        notes = [_make_note(f"n{i}") for i in range(5)]
        self.assertEqual(
            _count_smart_filter(SmartFilter.ALL, notes, now=_FIXED_NOW),
            5,
        )

    def test_recent_keeps_notes_within_window(self) -> None:
        # Modified just before the cutoff → counted.
        recent = _make_note(
            "recent",
            modified_at=_FIXED_NOW - timedelta(days=RECENT_WINDOW_DAYS - 1),
        )
        # Modified just after the window → excluded.
        old = _make_note(
            "old",
            modified_at=_FIXED_NOW - timedelta(days=RECENT_WINDOW_DAYS + 1),
        )
        self.assertEqual(
            _count_smart_filter(
                SmartFilter.RECENT,
                [recent, old],
                now=_FIXED_NOW,
            ),
            1,
        )

    def test_recent_includes_cutoff_itself_inclusively(self) -> None:
        """The filter is inclusive at the cutoff (matches the search
        layer's contract: ``modified_at >= cutoff``)."""
        boundary = _make_note(
            "boundary",
            modified_at=_FIXED_NOW - timedelta(days=RECENT_WINDOW_DAYS),
        )
        self.assertEqual(
            _count_smart_filter(
                SmartFilter.RECENT,
                [boundary],
                now=_FIXED_NOW,
            ),
            1,
        )

    def test_empty_input_returns_zero_for_all_kinds(self) -> None:
        for smart_filter in (SmartFilter.ALL, SmartFilter.RECENT):
            with self.subTest(smart_filter=smart_filter):
                self.assertEqual(
                    _count_smart_filter(smart_filter, [], now=_FIXED_NOW),
                    0,
                )


class CountNotebookTests(unittest.TestCase):
    def test_counts_only_matching_notebook_id(self) -> None:
        notes = [
            _make_note("n1", notebook_id="nb-a"),
            _make_note("n2", notebook_id="nb-a"),
            _make_note("n3", notebook_id="nb-b"),
        ]
        self.assertEqual(_count_notebook("nb-a", notes, []), 2)

    def test_counts_include_children(self) -> None:
        notes = [
            _make_note("n1", notebook_id="parent"),
            _make_note("n2", notebook_id="child-1"),
            _make_note("n3", notebook_id="child-2"),
            _make_note("n4", notebook_id="other"),
        ]
        self.assertEqual(
            _count_notebook("parent", notes, ["child-1", "child-2"]),
            3,
        )

    def test_empty_children_list_means_only_parent_counted(self) -> None:
        notes = [
            _make_note("n1", notebook_id="parent"),
            _make_note("n2", notebook_id="some-other-notebook"),
        ]
        self.assertEqual(_count_notebook("parent", notes, []), 1)


class TreeHelpersTests(unittest.TestCase):
    def test_top_level_notebooks_excludes_children(self) -> None:
        notebooks = [
            _make_notebook("a"),
            _make_notebook("b"),
            _make_notebook("a-child", parent_id="a"),
        ]
        result = _top_level_notebooks(notebooks)
        self.assertEqual([nb.id for nb in result], ["a", "b"])

    def test_children_of_returns_only_direct_children(self) -> None:
        notebooks = [
            _make_notebook("parent"),
            _make_notebook("child-1", parent_id="parent"),
            _make_notebook("child-2", parent_id="parent"),
            _make_notebook("other-parent"),
            _make_notebook("other-child", parent_id="other-parent"),
        ]
        result = _children_of("parent", notebooks)
        self.assertEqual([nb.id for nb in result], ["child-1", "child-2"])

    def test_children_of_empty_when_no_match(self) -> None:
        notebooks = [_make_notebook("a"), _make_notebook("b")]
        self.assertEqual(_children_of("does-not-exist", notebooks), [])


class IconNameLookupTests(unittest.TestCase):
    def test_known_icon_returns_mapped_name(self) -> None:
        # FOLDER is one of the entries — and the value is a non-empty
        # FreeDesktop icon name.
        self.assertTrue(_icon_name_for_notebook(NotebookIcon.FOLDER))

    def test_every_enum_member_is_mapped(self) -> None:
        # Pin the contract from the docstring: every NotebookIcon
        # value resolves to *some* non-empty icon name (either the
        # mapped one or the documented fallback).
        for icon in NotebookIcon:
            with self.subTest(icon=icon):
                name = _icon_name_for_notebook(icon)
                self.assertIsInstance(name, str)
                self.assertTrue(name)


# ---------------------------------------------------------------------------
# Widget tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class SidebarConstructionTests(unittest.TestCase):
    """The sidebar constructs cleanly across a range of repositories."""

    def test_constructs_with_empty_repositories(self) -> None:
        sidebar = Sidebar(
            note_repository=_FakeNoteRepository(),
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )
        self.assertIsInstance(sidebar, Gtk.Box)

    def test_constructs_with_seeded_data(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-personal", name="Personal"))
        notebooks.add(_make_notebook("nb-recipes", name="Recipes"))
        notebooks.add(
            _make_notebook("nb-baking", name="Baking", parent_id="nb-recipes")
        )
        notes.notes["n1"] = _make_note("n1", notebook_id="nb-personal")

        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )

        self.assertIsInstance(sidebar, Gtk.Box)


@unittest.skipUnless(_display_available(), "no GDK display")
class SmartFilterRowTests(unittest.TestCase):
    """Smart-filter rows reflect the seeded note set."""

    def test_initial_smart_filter_rows_have_correct_counts(self) -> None:
        notes = _FakeNoteRepository()
        notes.notes["recent"] = _make_note("recent", modified_at=_FIXED_NOW)
        notes.notes["old"] = _make_note(
            "old",
            modified_at=_FIXED_NOW - timedelta(days=RECENT_WINDOW_DAYS + 5),
        )

        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=_FakeNotebookRepository(),
            app_state=AppState(),
            clock=_fixed_clock,
        )

        all_label = sidebar._smart_filter_count_labels[SmartFilter.ALL]
        recent_label = sidebar._smart_filter_count_labels[SmartFilter.RECENT]
        self.assertEqual(all_label.get_text(), "2")
        self.assertEqual(recent_label.get_text(), "1")


@unittest.skipUnless(_display_available(), "no GDK display")
class NotebookTreeTests(unittest.TestCase):
    def _make_recipes_tree(
        self,
    ) -> tuple[_FakeNoteRepository, _FakeNotebookRepository]:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-personal", name="Personal"))
        notebooks.add(_make_notebook("nb-recipes", name="Recipes"))
        notebooks.add(
            _make_notebook("nb-baking", name="Baking", parent_id="nb-recipes")
        )
        notebooks.add(
            _make_notebook(
                "nb-dinners",
                name="Weeknight dinners",
                parent_id="nb-recipes",
            )
        )
        return notes, notebooks

    def test_collapsed_tree_only_renders_top_level_rows(self) -> None:
        notes, notebooks = self._make_recipes_tree()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        # Only the two top-level notebooks have rows. Children are
        # not rendered until the parent is expanded.
        self.assertEqual(
            set(sidebar._notebook_rows.keys()),
            {"nb-personal", "nb-recipes"},
        )

    def test_expanding_a_parent_renders_its_children(self) -> None:
        notes, notebooks = self._make_recipes_tree()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        sidebar._toggle_expansion("nb-recipes")

        self.assertEqual(
            set(sidebar._notebook_rows.keys()),
            {"nb-personal", "nb-recipes", "nb-baking", "nb-dinners"},
        )
        self.assertIn("nb-recipes", sidebar._expanded_notebook_ids)

    def test_collapsing_drops_children_from_the_render(self) -> None:
        notes, notebooks = self._make_recipes_tree()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        sidebar._toggle_expansion("nb-recipes")
        self.assertIn("nb-baking", sidebar._notebook_rows)
        sidebar._toggle_expansion("nb-recipes")
        self.assertNotIn("nb-baking", sidebar._notebook_rows)

    def test_parent_count_includes_children(self) -> None:
        notes, notebooks = self._make_recipes_tree()
        notes.notes["n1"] = _make_note("n1", notebook_id="nb-recipes")
        notes.notes["n2"] = _make_note("n2", notebook_id="nb-baking")
        notes.notes["n3"] = _make_note("n3", notebook_id="nb-dinners")
        notes.notes["n4"] = _make_note("n4", notebook_id="nb-personal")

        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        # The recipes row's count widget is the rightmost label.
        recipes_row = sidebar._notebook_rows["nb-recipes"]
        count_label = _count_label_in_row(recipes_row)
        self.assertEqual(count_label.get_text(), "3")

        personal_row = sidebar._notebook_rows["nb-personal"]
        self.assertEqual(_count_label_in_row(personal_row).get_text(), "1")

    def test_top_level_row_with_children_carries_has_children_payload(
        self,
    ) -> None:
        notes, notebooks = self._make_recipes_tree()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        recipes_row = sidebar._notebook_rows["nb-recipes"]
        assert isinstance(recipes_row.payload, _NotebookRowPayload)
        self.assertTrue(recipes_row.payload.has_children)

        personal_row = sidebar._notebook_rows["nb-personal"]
        assert isinstance(personal_row.payload, _NotebookRowPayload)
        self.assertFalse(personal_row.payload.has_children)

    def test_child_row_carries_is_child_payload(self) -> None:
        notes, notebooks = self._make_recipes_tree()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        sidebar._toggle_expansion("nb-recipes")
        baking_row = sidebar._notebook_rows["nb-baking"]
        assert isinstance(baking_row.payload, _NotebookRowPayload)
        self.assertTrue(baking_row.payload.is_child)
        self.assertFalse(baking_row.payload.has_children)

    def test_expansion_state_for_deleted_parent_is_pruned_on_refresh(
        self,
    ) -> None:
        # Defends the "stale ids in _expanded_notebook_ids" branch
        # in _build_notebook_rows: a notebook id that was once
        # expanded but no longer exists must be removed from the
        # set.
        notes, notebooks = self._make_recipes_tree()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        sidebar._toggle_expansion("nb-recipes")
        self.assertIn("nb-recipes", sidebar._expanded_notebook_ids)

        # Simulate the recipes notebook being deleted between
        # refreshes (the repository drops the id; the sidebar
        # rebuilds).
        del notebooks.notebooks["nb-recipes"]
        notebooks.insertion_order.remove("nb-recipes")
        del notebooks.notebooks["nb-baking"]
        notebooks.insertion_order.remove("nb-baking")
        del notebooks.notebooks["nb-dinners"]
        notebooks.insertion_order.remove("nb-dinners")

        sidebar.refresh()

        self.assertNotIn("nb-recipes", sidebar._expanded_notebook_ids)


@unittest.skipUnless(_display_available(), "no GDK display")
class SidebarSelectionPlumbingTests(unittest.TestCase):
    """Click input → :class:`AppState`; AppState → highlight."""

    def test_smart_filter_row_activation_updates_selection(self) -> None:
        sidebar = _empty_sidebar()
        sidebar._on_smart_filter_row_activated(
            sidebar._smart_filter_listbox,
            sidebar._smart_filter_rows[SmartFilter.RECENT],
        )
        self.assertEqual(
            sidebar._app_state.selection,
            SmartSelection(smart_filter=SmartFilter.RECENT),
        )

    def test_notebook_row_activation_updates_selection(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="One"))
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        sidebar._on_notebook_row_activated(
            sidebar._notebook_listbox,
            sidebar._notebook_rows["nb-1"],
        )
        self.assertEqual(
            sidebar._app_state.selection,
            NotebookSelection(notebook_id="nb-1"),
        )

    def test_smart_selection_clears_notebook_listbox_selection(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="One"))
        app_state = AppState(
            initial_selection=NotebookSelection(notebook_id="nb-1"),
        )
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=app_state,
            clock=_fixed_clock,
        )
        # The notebook listbox has the matching row selected.
        self.assertIs(
            sidebar._notebook_listbox.get_selected_row(),
            sidebar._notebook_rows["nb-1"],
        )

        # Switching to a smart filter clears the notebook selection.
        app_state.set_selection(SmartSelection(smart_filter=SmartFilter.ALL))
        self.assertIsNone(sidebar._notebook_listbox.get_selected_row())
        self.assertIs(
            sidebar._smart_filter_listbox.get_selected_row(),
            sidebar._smart_filter_rows[SmartFilter.ALL],
        )

    def test_notebook_selection_clears_smart_filter_listbox_selection(
        self,
    ) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="One"))
        # Default app state starts on SmartFilter.ALL.
        app_state = AppState()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=app_state,
            clock=_fixed_clock,
        )
        self.assertIs(
            sidebar._smart_filter_listbox.get_selected_row(),
            sidebar._smart_filter_rows[SmartFilter.ALL],
        )

        app_state.set_selection(NotebookSelection(notebook_id="nb-1"))
        self.assertIsNone(sidebar._smart_filter_listbox.get_selected_row())
        self.assertIs(
            sidebar._notebook_listbox.get_selected_row(),
            sidebar._notebook_rows["nb-1"],
        )

    def test_unknown_notebook_id_in_app_state_clears_both_listboxes(
        self,
    ) -> None:
        # A NotebookSelection whose id no longer exists must not
        # crash the sidebar — both list-boxes simply unselect.
        sidebar = _empty_sidebar()
        sidebar._app_state.set_selection(
            NotebookSelection(notebook_id="never-existed"),
        )
        self.assertIsNone(sidebar._notebook_listbox.get_selected_row())
        self.assertIsNone(sidebar._smart_filter_listbox.get_selected_row())

    def test_clicking_already_selected_smart_filter_is_a_noop(self) -> None:
        # Clicking the row that is already the current selection
        # must not flip a flag, double-emit, or change anything
        # observable. AppState.set_selection short-circuits on
        # equality; this test pins that contract end-to-end.
        sidebar = _empty_sidebar()
        events: list[None] = []
        sidebar._app_state.connect(
            "selection-changed",
            lambda _state: events.append(None),
        )
        # Re-activate the All row (already selected because it's
        # AppState's default).
        sidebar._on_smart_filter_row_activated(
            sidebar._smart_filter_listbox,
            sidebar._smart_filter_rows[SmartFilter.ALL],
        )
        self.assertEqual(events, [])


@unittest.skipUnless(_display_available(), "no GDK display")
class RowPayloadIntegrityTests(unittest.TestCase):
    """Every row added to either list-box is a :class:`_SidebarRow`
    with a typed payload — no bare :class:`Gtk.ListBoxRow` slips in."""

    def test_every_smart_filter_row_carries_a_smart_payload(self) -> None:
        sidebar = _empty_sidebar()
        for smart_filter in SmartFilter:
            with self.subTest(smart_filter=smart_filter):
                row = sidebar._smart_filter_rows[smart_filter]
                self.assertIsInstance(row, _SidebarRow)
                self.assertIsInstance(row.payload, _SmartRowPayload)

    def test_every_notebook_row_carries_a_notebook_payload(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-a", name="A"))
        notebooks.add(_make_notebook("nb-b", name="B"))
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        for nb_id in ("nb-a", "nb-b"):
            with self.subTest(nb_id=nb_id):
                row = sidebar._notebook_rows[nb_id]
                self.assertIsInstance(row, _SidebarRow)
                self.assertIsInstance(row.payload, _NotebookRowPayload)


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _empty_sidebar() -> Sidebar:
    """Sidebar with empty repositories, used in selection-plumbing tests."""
    return Sidebar(
        note_repository=_FakeNoteRepository(),
        notebook_repository=_FakeNotebookRepository(),
        app_state=AppState(),
        clock=_fixed_clock,
    )


def _count_label_in_row(row: _SidebarRow) -> Gtk.Label:
    """Walk a :class:`_SidebarRow` and return its rightmost
    :class:`Gtk.Label` — the count column.

    Layout is ``[chevron|spacer] icon label count_label`` inside a
    horizontal :class:`Gtk.Box`. The count label is always the last
    child of the row's box.
    """
    box = row.get_child()
    assert isinstance(box, Gtk.Box), "row child must be a Box"
    last_child = box.get_last_child()
    assert isinstance(last_child, Gtk.Label), (
        f"last child should be a Label, got {type(last_child).__name__}"
    )
    return last_child


if __name__ == "__main__":
    unittest.main()
