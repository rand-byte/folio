"""Tests for :mod:`ui.sidebar`."""

from __future__ import annotations

import importlib.resources
import unittest
from datetime import UTC, datetime, timedelta

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402

from controllers.app_state import AppState
from enums import NotebookIcon, SmartFilter
from models.note import Note
from models.notebook import Notebook
from search.note_filter import (
    RECENT_WINDOW_DAYS,
    NotebookSelection,
    SmartSelection,
)
from ui.sidebar import (
    Sidebar,
    _build_notebook_items,
    _children_of,
    _count_notebook,
    _count_smart_filter,
    _icon_name_for_notebook,
    _SidebarItem,
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
# Item-builder tests — pure, no display needed
# ---------------------------------------------------------------------------


class BuildNotebookItemsTests(unittest.TestCase):
    """The pure tree-of-items builder underlying the notebook model.

    These replace the old "the rightmost label in the row widget"
    assertions: counts and hierarchy are read straight off the
    :class:`_SidebarItem` tree, which is display-independent.
    """

    def _recipes_tree(self) -> tuple[list[Note], list[Notebook]]:
        notebooks = [
            _make_notebook("nb-personal", name="Personal"),
            _make_notebook("nb-recipes", name="Recipes"),
            _make_notebook("nb-baking", name="Baking", parent_id="nb-recipes"),
            _make_notebook(
                "nb-dinners",
                name="Weeknight dinners",
                parent_id="nb-recipes",
            ),
        ]
        notes = [
            _make_note("n1", notebook_id="nb-recipes"),
            _make_note("n2", notebook_id="nb-baking"),
            _make_note("n3", notebook_id="nb-dinners"),
            _make_note("n4", notebook_id="nb-personal"),
        ]
        return notes, notebooks

    def test_only_top_level_items_at_the_root(self) -> None:
        notes, notebooks = self._recipes_tree()
        items = _build_notebook_items(notebooks, notes)
        self.assertEqual(
            [item.label for item in items],
            ["Personal", "Recipes"],
        )

    def test_children_hang_off_their_parent_item(self) -> None:
        notes, notebooks = self._recipes_tree()
        items = _build_notebook_items(notebooks, notes)
        recipes = next(i for i in items if i.label == "Recipes")
        self.assertEqual(
            [child.label for child in recipes.children],
            ["Baking", "Weeknight dinners"],
        )

    def test_leaf_items_have_no_children(self) -> None:
        notes, notebooks = self._recipes_tree()
        items = _build_notebook_items(notebooks, notes)
        personal = next(i for i in items if i.label == "Personal")
        self.assertEqual(personal.children, ())

    def test_parent_count_includes_children(self) -> None:
        notes, notebooks = self._recipes_tree()
        items = _build_notebook_items(notebooks, notes)
        recipes = next(i for i in items if i.label == "Recipes")
        # nb-recipes (1) + nb-baking (1) + nb-dinners (1).
        self.assertEqual(recipes.count, 3)

    def test_leaf_count_is_its_own_notes_only(self) -> None:
        notes, notebooks = self._recipes_tree()
        items = _build_notebook_items(notebooks, notes)
        personal = next(i for i in items if i.label == "Personal")
        self.assertEqual(personal.count, 1)

    def test_item_payload_is_a_notebook_selection(self) -> None:
        notes, notebooks = self._recipes_tree()
        items = _build_notebook_items(notebooks, notes)
        recipes = next(i for i in items if i.label == "Recipes")
        self.assertEqual(
            recipes.payload,
            NotebookSelection(notebook_id="nb-recipes"),
        )
        baking = recipes.children[0]
        self.assertEqual(
            baking.payload,
            NotebookSelection(notebook_id="nb-baking"),
        )


# ---------------------------------------------------------------------------
# Model-access helpers (test-local)
# ---------------------------------------------------------------------------


def _tree_rows(selection: Gtk.SingleSelection) -> list[Gtk.TreeListRow]:
    """Every realised :class:`Gtk.TreeListRow` in a selection's model."""
    model = selection.get_model()
    rows: list[Gtk.TreeListRow] = []
    for position in range(model.get_n_items()):
        row = model.get_item(position)
        assert isinstance(row, Gtk.TreeListRow)
        rows.append(row)
    return rows


def _items(selection: Gtk.SingleSelection) -> list[_SidebarItem]:
    """The :class:`_SidebarItem`\\s currently exposed by a selection."""
    items: list[_SidebarItem] = []
    for row in _tree_rows(selection):
        item = row.get_item()
        assert isinstance(item, _SidebarItem)
        items.append(item)
    return items


def _row_for(
    selection: Gtk.SingleSelection,
    notebook_id: str,
) -> Gtk.TreeListRow:
    """The tree row whose item targets ``notebook_id``."""
    for row in _tree_rows(selection):
        item = row.get_item()
        assert isinstance(item, _SidebarItem)
        if item.payload == NotebookSelection(notebook_id=notebook_id):
            return row
    raise AssertionError(f"no row for {notebook_id!r}")


def _expand(selection: Gtk.SingleSelection, notebook_id: str) -> None:
    """Expand the row for ``notebook_id`` (drives the TreeExpander path)."""
    _row_for(selection, notebook_id).set_expanded(True)


def _notebook_ids(selection: Gtk.SingleSelection) -> set[str]:
    """The set of notebook ids currently rendered in the notebook model."""
    ids: set[str] = set()
    for item in _items(selection):
        if isinstance(item.payload, NotebookSelection):
            ids.add(item.payload.notebook_id)
    return ids


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
class SmartFilterModelTests(unittest.TestCase):
    """Smart-filter model items reflect the seeded note set."""

    def test_initial_smart_filter_items_have_correct_counts(self) -> None:
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

        counts = {
            item.payload: item.count
            for item in _items(sidebar._smart_selection)
        }
        self.assertEqual(
            counts[SmartSelection(smart_filter=SmartFilter.ALL)],
            2,
        )
        self.assertEqual(
            counts[SmartSelection(smart_filter=SmartFilter.RECENT)],
            1,
        )


@unittest.skipUnless(_display_available(), "no GDK display")
class NotebookTreeModelTests(unittest.TestCase):
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

    def _sidebar(self) -> Sidebar:
        notes, notebooks = self._make_recipes_tree()
        return Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )

    def test_collapsed_tree_only_exposes_top_level_rows(self) -> None:
        sidebar = self._sidebar()
        # Collapsed: the model exposes only the two top-level notebooks.
        self.assertEqual(
            _notebook_ids(sidebar._notebook_selection),
            {"nb-personal", "nb-recipes"},
        )

    def test_expanding_a_parent_exposes_its_children(self) -> None:
        sidebar = self._sidebar()
        _expand(sidebar._notebook_selection, "nb-recipes")
        self.assertEqual(
            _notebook_ids(sidebar._notebook_selection),
            {"nb-personal", "nb-recipes", "nb-baking", "nb-dinners"},
        )

    def test_collapsing_drops_children_from_the_model(self) -> None:
        sidebar = self._sidebar()
        row = _row_for(sidebar._notebook_selection, "nb-recipes")
        row.set_expanded(True)
        self.assertIn("nb-baking", _notebook_ids(sidebar._notebook_selection))
        row.set_expanded(False)
        self.assertNotIn(
            "nb-baking", _notebook_ids(sidebar._notebook_selection)
        )

    def test_parent_row_is_expandable_and_leaf_is_not(self) -> None:
        sidebar = self._sidebar()
        recipes = _row_for(sidebar._notebook_selection, "nb-recipes")
        personal = _row_for(sidebar._notebook_selection, "nb-personal")
        self.assertTrue(recipes.is_expandable())
        self.assertFalse(personal.is_expandable())

    def test_child_row_is_a_leaf(self) -> None:
        sidebar = self._sidebar()
        _expand(sidebar._notebook_selection, "nb-recipes")
        baking = _row_for(sidebar._notebook_selection, "nb-baking")
        self.assertFalse(baking.is_expandable())

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
        recipes = _row_for(sidebar._notebook_selection, "nb-recipes").get_item()
        personal = _row_for(
            sidebar._notebook_selection, "nb-personal"
        ).get_item()
        assert isinstance(recipes, _SidebarItem)
        assert isinstance(personal, _SidebarItem)
        self.assertEqual(recipes.count, 3)
        self.assertEqual(personal.count, 1)

    def test_expansion_is_preserved_across_refresh(self) -> None:
        # The widget-local expansion snapshot must survive a model
        # rebuild — a refresh should not collapse an open notebook.
        sidebar = self._sidebar()
        _expand(sidebar._notebook_selection, "nb-recipes")
        self.assertIn("nb-baking", _notebook_ids(sidebar._notebook_selection))

        sidebar.refresh()

        self.assertIn("nb-baking", _notebook_ids(sidebar._notebook_selection))

    def test_expansion_for_deleted_parent_is_dropped_on_refresh(self) -> None:
        # Defends the stale-expansion path: a notebook expanded then
        # deleted between refreshes must not reappear, and restoring
        # the snapshot must find no matching row (no crash).
        notes, notebooks = self._make_recipes_tree()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        _expand(sidebar._notebook_selection, "nb-recipes")
        self.assertEqual(
            sidebar._snapshot_expanded_notebook_ids(), {"nb-recipes"}
        )

        # Recipes (and its children) are deleted between refreshes.
        for nb_id in ("nb-recipes", "nb-baking", "nb-dinners"):
            del notebooks.notebooks[nb_id]
            notebooks.insertion_order.remove(nb_id)

        sidebar.refresh()

        self.assertEqual(
            _notebook_ids(sidebar._notebook_selection),
            {"nb-personal"},
        )
        self.assertEqual(sidebar._snapshot_expanded_notebook_ids(), set())


@unittest.skipUnless(_display_available(), "no GDK display")
class SidebarSelectionPlumbingTests(unittest.TestCase):
    """Selection → :class:`AppState`; AppState → highlight."""

    def test_smart_filter_selection_updates_app_state(self) -> None:
        sidebar = _empty_sidebar()
        recent_pos = _position_of(
            sidebar._smart_selection,
            SmartSelection(smart_filter=SmartFilter.RECENT),
        )
        sidebar._smart_selection.set_selected(recent_pos)
        self.assertEqual(
            sidebar._app_state.selection,
            SmartSelection(smart_filter=SmartFilter.RECENT),
        )

    def test_notebook_selection_updates_app_state(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="One"))
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        nb_pos = _position_of(
            sidebar._notebook_selection,
            NotebookSelection(notebook_id="nb-1"),
        )
        sidebar._notebook_selection.set_selected(nb_pos)
        self.assertEqual(
            sidebar._app_state.selection,
            NotebookSelection(notebook_id="nb-1"),
        )

    def test_smart_selection_clears_notebook_section(self) -> None:
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
        # The notebook section starts with the matching row selected.
        self.assertEqual(
            _selected_payload(sidebar._notebook_selection),
            NotebookSelection(notebook_id="nb-1"),
        )

        # Switching to a smart filter clears the notebook section.
        app_state.set_selection(SmartSelection(smart_filter=SmartFilter.ALL))
        self.assertEqual(
            sidebar._notebook_selection.get_selected(),
            Gtk.INVALID_LIST_POSITION,
        )
        self.assertEqual(
            _selected_payload(sidebar._smart_selection),
            SmartSelection(smart_filter=SmartFilter.ALL),
        )

    def test_notebook_selection_clears_smart_filter_section(self) -> None:
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
        self.assertEqual(
            _selected_payload(sidebar._smart_selection),
            SmartSelection(smart_filter=SmartFilter.ALL),
        )

        app_state.set_selection(NotebookSelection(notebook_id="nb-1"))
        self.assertEqual(
            sidebar._smart_selection.get_selected(),
            Gtk.INVALID_LIST_POSITION,
        )
        self.assertEqual(
            _selected_payload(sidebar._notebook_selection),
            NotebookSelection(notebook_id="nb-1"),
        )

    def test_unknown_notebook_id_clears_both_sections(self) -> None:
        # A NotebookSelection whose id no longer exists must not
        # crash the sidebar — both sections simply unselect.
        sidebar = _empty_sidebar()
        sidebar._app_state.set_selection(
            NotebookSelection(notebook_id="never-existed"),
        )
        self.assertEqual(
            sidebar._notebook_selection.get_selected(),
            Gtk.INVALID_LIST_POSITION,
        )
        self.assertEqual(
            sidebar._smart_selection.get_selected(),
            Gtk.INVALID_LIST_POSITION,
        )

    def test_reselecting_current_smart_filter_is_a_noop(self) -> None:
        # Re-selecting the row that is already current must not
        # re-emit. AppState.set_selection short-circuits on equality;
        # this pins that contract through the SingleSelection path.
        sidebar = _empty_sidebar()
        events: list[None] = []
        sidebar._app_state.connect(
            "selection-changed",
            lambda _state: events.append(None),
        )
        all_pos = _position_of(
            sidebar._smart_selection,
            SmartSelection(smart_filter=SmartFilter.ALL),
        )
        # All is already selected (AppState's default).
        sidebar._smart_selection.set_selected(all_pos)
        self.assertEqual(events, [])

    def test_apply_highlight_does_not_loop_back_into_app_state(self) -> None:
        # The re-entrancy fence: programmatic set_selected during
        # _apply_highlight emits selection-changed, which must NOT
        # push a fresh selection back into AppState.
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="One"))
        app_state = AppState()
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=app_state,
            clock=_fixed_clock,
        )
        events: list[None] = []
        app_state.connect(
            "selection-changed",
            lambda _state: events.append(None),
        )
        app_state.set_selection(NotebookSelection(notebook_id="nb-1"))
        # Exactly one emission — the deliberate one. The highlight's
        # own programmatic selection did not bounce back.
        self.assertEqual(len(events), 1)
        # And the highlight did land: the notebook row is selected.
        self.assertEqual(
            _selected_payload(sidebar._notebook_selection),
            NotebookSelection(notebook_id="nb-1"),
        )


@unittest.skipUnless(_display_available(), "no GDK display")
class RowItemIntegrityTests(unittest.TestCase):
    """Every row in either section is backed by a typed item/payload."""

    def test_every_smart_filter_item_carries_a_smart_payload(self) -> None:
        sidebar = _empty_sidebar()
        payloads = {item.payload for item in _items(sidebar._smart_selection)}
        self.assertEqual(
            payloads,
            {
                SmartSelection(smart_filter=SmartFilter.ALL),
                SmartSelection(smart_filter=SmartFilter.RECENT),
            },
        )

    def test_every_notebook_item_carries_a_notebook_payload(self) -> None:
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
        for item in _items(sidebar._notebook_selection):
            with self.subTest(label=item.label):
                self.assertIsInstance(item, _SidebarItem)
                self.assertIsInstance(item.payload, NotebookSelection)


@unittest.skipUnless(_display_available(), "no GDK display")
class IconColumnAlignmentTests(unittest.TestCase):
    """Regression pin for §2.2/§2.3: one icon column across all rows.

    Needs realised geometry, so it builds a seeded sidebar inside a
    window, loads the application stylesheet (the alignment rule lives
    in ``app.css``), pumps the main loop, and asserts every top-level
    notebook row's icon shares one x-origin — parents (which own an
    expander arrow) included. Without the
    ``treeexpander indent { -gtk-icon-size: 16px }`` rule the parent
    icon outdents its leaf siblings, so this test fails if that rule
    silently regresses.
    """

    @staticmethod
    def _install_application_css() -> None:
        """Attach the bundled ``app.css`` to the default display.

        Mirrors :func:`ui.application._load_application_css`
        so the alignment rule under test is the shipped one, not a
        copy pasted into the test.
        """
        css_source = (
            importlib.resources.files("ui.css")
            .joinpath("app.css")
            .read_text(encoding="utf-8")
        )
        provider = Gtk.CssProvider.new()
        provider.load_from_string(css_source)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def test_top_level_icons_share_one_x_origin(self) -> None:
        self._install_application_css()
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        # A parent (expander arrow) and a childless leaf side by side.
        notebooks.add(_make_notebook("nb-personal", name="Personal"))
        notebooks.add(_make_notebook("nb-recipes", name="Recipes"))
        notebooks.add(
            _make_notebook("nb-baking", name="Baking", parent_id="nb-recipes")
        )
        sidebar = Sidebar(
            note_repository=notes,
            notebook_repository=notebooks,
            app_state=AppState(),
            clock=_fixed_clock,
        )
        window = Gtk.Window()
        window.set_default_size(260, 500)
        window.set_child(sidebar)
        window.present()
        self.addCleanup(window.destroy)
        _pump_main_loop()

        list_view = _section_list_views(sidebar)[1]  # notebook section
        x_origins = _icon_x_origins(list_view)
        self.assertGreaterEqual(len(x_origins), 2, "need ≥2 top-level rows")
        self.assertEqual(
            len(set(x_origins)),
            1,
            f"icons not in one column: {x_origins}",
        )


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


def _position_of(
    selection: Gtk.SingleSelection,
    payload: SmartSelection | NotebookSelection,
) -> int:
    """Position of the row whose item carries ``payload``."""
    for position, item in enumerate(_items(selection)):
        if item.payload == payload:
            return position
    raise AssertionError(f"no row for {payload!r}")


def _selected_payload(
    selection: Gtk.SingleSelection,
) -> SmartSelection | NotebookSelection | None:
    """The payload of the selected row, or None when nothing is selected."""
    position = selection.get_selected()
    if position == Gtk.INVALID_LIST_POSITION:
        return None
    row = selection.get_model().get_item(position)
    assert isinstance(row, Gtk.TreeListRow)
    item = row.get_item()
    assert isinstance(item, _SidebarItem)
    return item.payload


def _pump_main_loop(iterations: int = 300) -> None:
    """Drain pending GLib events so widget geometry is realised."""
    context = GLib.MainContext.default()
    for _ in range(iterations):
        while context.pending():
            context.iteration(False)


def _section_list_views(sidebar: Sidebar) -> list[Gtk.ListView]:
    """The two section :class:`Gtk.ListView`\\s, in sidebar order."""
    views: list[Gtk.ListView] = []
    child = sidebar.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.ScrolledWindow):
            inner = child.get_child()
            assert isinstance(inner, Gtk.ListView)
            views.append(inner)
        child = child.get_next_sibling()
    return views


def _icon_x_origins(list_view: Gtk.ListView) -> list[float]:
    """The x-origin (relative to ``list_view``) of every row icon."""
    origins: list[float] = []
    _collect_icon_x(list_view, list_view, origins)
    return origins


def _collect_icon_x(
    widget: Gtk.Widget,
    relative_to: Gtk.ListView,
    origins: list[float],
) -> None:
    if isinstance(widget, Gtk.Image):
        ok, point = widget.compute_point(
            relative_to, Graphene.Point().init(0, 0)
        )
        if ok:
            origins.append(round(point.x, 1))
    child = widget.get_first_child()
    while child is not None:
        _collect_icon_x(child, relative_to, origins)
        child = child.get_next_sibling()


if __name__ == "__main__":
    unittest.main()
