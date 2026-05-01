"""Tests for :mod:`notes_app.ui.toolbar`."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.controllers.note_controller import NoteController
from notes_app.enums import NotebookIcon, ViewMode
from notes_app.models.attachment import Attachment
from notes_app.models.note import Note
from notes_app.models.notebook import Notebook
from notes_app.search.note_filter import NotebookSelection, SmartSelection
from notes_app.ui.toolbar import (
    Toolbar,
    compute_breadcrumb,
    format_breadcrumb,
    resolve_target_notebook,
)
from notes_app.enums import SmartFilter


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Test factories
# ---------------------------------------------------------------------------


def _make_note(
    note_id: str = "n1",
    *,
    title: str = "A note",
    notebook_id: str = "nb-1",
) -> Note:
    return Note(
        id=note_id,
        title=title,
        notebook_id=notebook_id,
        source=f"= {title}\n\nbody.\n",
        snippet="body.",
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW,
    )


def _make_notebook(
    notebook_id: str = "nb-1",
    *,
    name: str = "Personal",
    parent_id: str | None = None,
    icon: NotebookIcon = NotebookIcon.HOME,
) -> Notebook:
    return Notebook(
        id=notebook_id,
        name=name,
        parent_id=parent_id,
        icon=icon,
    )


# ---------------------------------------------------------------------------
# Pure helpers — no GTK display required
# ---------------------------------------------------------------------------


class ResolveTargetNotebookTests(unittest.TestCase):
    """The new-note target notebook follows the React fallback chain."""

    def test_notebook_selection_wins(self) -> None:
        result = resolve_target_notebook(
            selection=NotebookSelection(notebook_id="nb-selected"),
            current_note=_make_note(notebook_id="nb-from-note"),
            notebooks=[_make_notebook("nb-first")],
        )
        self.assertEqual(result, "nb-selected")

    def test_smart_selection_falls_back_to_current_notes_notebook(self) -> None:
        result = resolve_target_notebook(
            selection=SmartSelection(smart_filter=SmartFilter.ALL),
            current_note=_make_note(notebook_id="nb-from-note"),
            notebooks=[_make_notebook("nb-first")],
        )
        self.assertEqual(result, "nb-from-note")

    def test_no_current_note_falls_back_to_first_notebook(self) -> None:
        result = resolve_target_notebook(
            selection=SmartSelection(smart_filter=SmartFilter.ALL),
            current_note=None,
            notebooks=[_make_notebook("nb-first"), _make_notebook("nb-second")],
        )
        self.assertEqual(result, "nb-first")

    def test_empty_repository_returns_none(self) -> None:
        result = resolve_target_notebook(
            selection=SmartSelection(smart_filter=SmartFilter.ALL),
            current_note=None,
            notebooks=[],
        )
        self.assertIsNone(result)

    def test_smart_recent_smart_filter_takes_same_branch_as_all(self) -> None:
        # The SmartSelection variant is what matters, not the
        # specific smart filter inside it. RECENT must follow the
        # same fallback path as ALL.
        result = resolve_target_notebook(
            selection=SmartSelection(smart_filter=SmartFilter.RECENT),
            current_note=_make_note(notebook_id="nb-x"),
            notebooks=[_make_notebook("nb-first")],
        )
        self.assertEqual(result, "nb-x")


class ComputeBreadcrumbTests(unittest.TestCase):
    """The breadcrumb trail builds correctly across the hierarchy shapes."""

    def test_top_level_notebook_yields_two_segments(self) -> None:
        notebook = _make_notebook("nb-personal", name="Personal")
        note = _make_note(title="Hello", notebook_id="nb-personal")
        trail = compute_breadcrumb(note, {"nb-personal": notebook})
        self.assertEqual(trail, ["Personal", "Hello"])

    def test_child_notebook_yields_three_segments(self) -> None:
        recipes = _make_notebook("nb-recipes", name="Recipes")
        baking = _make_notebook(
            "nb-baking",
            name="Baking",
            parent_id="nb-recipes",
        )
        note = _make_note(title="Sourdough", notebook_id="nb-baking")
        trail = compute_breadcrumb(
            note,
            {"nb-recipes": recipes, "nb-baking": baking},
        )
        self.assertEqual(trail, ["Recipes", "Baking", "Sourdough"])

    def test_orphaned_note_yields_just_the_title(self) -> None:
        # The notebook has been deleted from the snapshot but the
        # note still references it. Storage's foreign keys make
        # this practically impossible at runtime; the helper is
        # defensive about it anyway.
        note = _make_note(title="Orphan", notebook_id="nb-gone")
        trail = compute_breadcrumb(note, {})
        self.assertEqual(trail, ["Orphan"])

    def test_child_notebook_with_missing_parent_drops_parent_segment(
        self,
    ) -> None:
        # Child notebook references a parent that is not in the
        # snapshot. We cannot prepend the parent name; we still
        # render the child + note title.
        baking = _make_notebook(
            "nb-baking",
            name="Baking",
            parent_id="nb-recipes",
        )
        note = _make_note(title="Sourdough", notebook_id="nb-baking")
        trail = compute_breadcrumb(note, {"nb-baking": baking})
        self.assertEqual(trail, ["Baking", "Sourdough"])


class FormatBreadcrumbTests(unittest.TestCase):
    def test_joins_segments_with_separator(self) -> None:
        text = format_breadcrumb(["Recipes", "Baking", "Sourdough"])
        # The actual separator is U+203A surrounded by spaces — we
        # test the high-level contract by checking the segments
        # appear in order with *some* visible glue between them.
        self.assertIn("Recipes", text)
        self.assertIn("Baking", text)
        self.assertIn("Sourdough", text)
        # Separators between consecutive segments.
        self.assertEqual(text.count("\u203a"), 2)

    def test_empty_trail_yields_empty_string(self) -> None:
        self.assertEqual(format_breadcrumb([]), "")

    def test_single_segment_has_no_separator(self) -> None:
        text = format_breadcrumb(["Only"])
        self.assertEqual(text, "Only")


# ---------------------------------------------------------------------------
# Fakes — minimal protocol-conforming repositories for widget tests
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    notes: dict[str, Note]
    insertions: list[Note]
    deletions: list[str]
    duplications: list[Note]

    def __init__(self) -> None:
        self.notes = {}
        self.insertions = []
        self.deletions = []
        self.duplications = []

    def add(self, note: Note) -> None:
        self.notes[note.id] = note

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

    def insert(self, note: Note) -> None:
        # Behave like a real repository: store the note so the
        # controller's post-insert ``selected_note_id`` set can
        # be followed up by widgets that read the note back.
        self.notes[note.id] = note
        self.insertions.append(note)

    def update_source(
        self,
        _note_id: str,
        _source: str,
        _modified_at: datetime,
    ) -> None:
        raise NotImplementedError

    def update_notebook(self, _note_id: str, _notebook_id: str) -> None:
        raise NotImplementedError

    def delete(self, note_id: str) -> None:
        self.deletions.append(note_id)
        self.notes.pop(note_id, None)


class _FakeNotebookRepository:
    notebooks: dict[str, Notebook]
    order: list[str]

    def __init__(self) -> None:
        self.notebooks = {}
        self.order = []

    def add(self, notebook: Notebook) -> None:
        self.notebooks[notebook.id] = notebook
        if notebook.id not in self.order:
            self.order.append(notebook.id)

    def list_all(self) -> list[Notebook]:
        return [self.notebooks[nb_id] for nb_id in self.order]

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


class _FakeAttachmentStore:
    """Attachments aren't exercised by the toolbar — every method
    raises so any inadvertent call is a test bug."""

    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        raise NotImplementedError

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        return []

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError


class _FakeConfirmDialogPresenter:
    """Records calls and lets the test invoke the result callback."""

    calls: list[tuple[str, str, str]]
    last_callback: Callable[[bool], None] | None
    last_parent: Gtk.Window | None

    def __init__(self) -> None:
        self.calls = []
        self.last_callback = None
        self.last_parent = None

    def __call__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        parent_window: Gtk.Window | None,
        title: str,
        detail: str,
        confirm_label: str,
        on_result: Callable[[bool], None],
    ) -> None:
        self.calls.append((title, detail, confirm_label))
        self.last_callback = on_result
        self.last_parent = parent_window


# ---------------------------------------------------------------------------
# Construction helper used by widget tests
# ---------------------------------------------------------------------------


def _build_toolbar(
    *,
    notes: _FakeNoteRepository | None = None,
    notebooks: _FakeNotebookRepository | None = None,
    app_state: AppState | None = None,
    presenter: _FakeConfirmDialogPresenter | None = None,
    deterministic_ids: bool = True,
) -> tuple[
    Toolbar,
    _FakeNoteRepository,
    _FakeNotebookRepository,
    AppState,
    NoteController,
    _FakeConfirmDialogPresenter,
]:
    if notes is None:
        notes = _FakeNoteRepository()
    if notebooks is None:
        notebooks = _FakeNotebookRepository()
    if app_state is None:
        app_state = AppState()
    if presenter is None:
        presenter = _FakeConfirmDialogPresenter()

    counter = {"i": 0}

    def fixed_clock() -> datetime:
        return _FIXED_NOW

    def counter_id_factory() -> str:
        counter["i"] += 1
        return f"new-{counter['i']}"

    controller = NoteController(
        repository=notes,
        attachments=_FakeAttachmentStore(),
        app_state=app_state,
        clock=fixed_clock,
        id_factory=counter_id_factory if deterministic_ids else None,  # type: ignore[arg-type]
    )

    toolbar = Toolbar(
        note_repository=notes,
        notebook_repository=notebooks,
        note_controller=controller,
        app_state=app_state,
        confirm_dialog_presenter=presenter,
    )
    return toolbar, notes, notebooks, app_state, controller, presenter


# ---------------------------------------------------------------------------
# Widget construction & initial state
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ToolbarConstructionTests(unittest.TestCase):
    def test_constructs_with_empty_repositories(self) -> None:
        toolbar, *_ = _build_toolbar()
        self.assertIsInstance(toolbar, Gtk.HeaderBar)

    def test_breadcrumb_label_is_empty_when_no_note_selected(self) -> None:
        toolbar, *_ = _build_toolbar()
        self.assertEqual(toolbar.breadcrumb_label.get_text(), "")

    def test_more_menu_button_is_disabled_when_no_note_selected(self) -> None:
        toolbar, *_ = _build_toolbar()
        self.assertFalse(toolbar.more_menu_button.get_sensitive())


@unittest.skipUnless(_display_available(), "no GDK display")
class ToolbarBreadcrumbTests(unittest.TestCase):
    """Breadcrumb tracks selected note and updates on selection change."""

    def test_breadcrumb_renders_for_initially_selected_note(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-personal", name="Personal"))
        notes.add(
            _make_note(
                "n1",
                title="Welcome",
                notebook_id="nb-personal",
            )
        )
        app_state = AppState()
        app_state.set_selected_note_id("n1")  # before constructing toolbar

        toolbar, *_ = _build_toolbar(
            notes=notes,
            notebooks=notebooks,
            app_state=app_state,
        )
        text = toolbar.breadcrumb_label.get_text()
        self.assertIn("Personal", text)
        self.assertIn("Welcome", text)

    def test_breadcrumb_updates_when_selection_changes(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-personal", name="Personal"))
        notebooks.add(_make_notebook("nb-recipes", name="Recipes"))
        notes.add(
            _make_note("n1", title="Hello", notebook_id="nb-personal")
        )
        notes.add(
            _make_note("n2", title="Sourdough", notebook_id="nb-recipes")
        )
        toolbar, _n, _nb, app_state, _ctrl, _p = _build_toolbar(
            notes=notes,
            notebooks=notebooks,
        )

        app_state.set_selected_note_id("n1")
        self.assertIn("Hello", toolbar.breadcrumb_label.get_text())

        app_state.set_selected_note_id("n2")
        self.assertIn("Sourdough", toolbar.breadcrumb_label.get_text())

    def test_breadcrumb_clears_when_selection_cleared(self) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1"))
        notes.add(_make_note("n1", notebook_id="nb-1"))

        toolbar, _n, _nb, app_state, _ctrl, _p = _build_toolbar(
            notes=notes,
            notebooks=notebooks,
        )
        app_state.set_selected_note_id("n1")
        self.assertNotEqual(toolbar.breadcrumb_label.get_text(), "")

        app_state.set_selected_note_id(None)
        self.assertEqual(toolbar.breadcrumb_label.get_text(), "")


# ---------------------------------------------------------------------------
# Search entry binding
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ToolbarSearchEntryTests(unittest.TestCase):
    def test_typing_in_search_entry_mutates_app_state_query(self) -> None:
        toolbar, _n, _nb, app_state, _c, _p = _build_toolbar()
        toolbar.search_entry.set_text("sourdough")
        toolbar.search_entry.emit("search-changed")
        self.assertEqual(app_state.query, "sourdough")

    def test_external_query_change_updates_search_entry(self) -> None:
        toolbar, _n, _nb, app_state, _c, _p = _build_toolbar()
        app_state.set_query("from elsewhere")
        self.assertEqual(toolbar.search_entry.get_text(), "from elsewhere")

    def test_initial_query_value_is_reflected_at_construction(self) -> None:
        app_state = AppState()
        app_state.set_query("preexisting")
        toolbar, *_ = _build_toolbar(app_state=app_state)
        self.assertEqual(toolbar.search_entry.get_text(), "preexisting")


# ---------------------------------------------------------------------------
# Mode toggle binding
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ToolbarModeToggleTests(unittest.TestCase):
    def test_initial_mode_view_pre_presses_view_button(self) -> None:
        toolbar, _n, _nb, _state, _c, _p = _build_toolbar()
        self.assertTrue(toolbar.view_button.get_active())
        self.assertFalse(toolbar.source_button.get_active())

    def test_initial_mode_edit_pre_presses_source_button(self) -> None:
        app_state = AppState(initial_view_mode=ViewMode.EDIT)
        toolbar, *_ = _build_toolbar(app_state=app_state)
        self.assertFalse(toolbar.view_button.get_active())
        self.assertTrue(toolbar.source_button.get_active())

    def test_clicking_source_toggle_switches_app_state_to_edit(self) -> None:
        toolbar, _n, _nb, app_state, _c, _p = _build_toolbar()
        # Activating the Source button.
        toolbar.source_button.set_active(True)
        self.assertEqual(app_state.view_mode, ViewMode.EDIT)

    def test_external_view_mode_change_pushes_to_toggles(self) -> None:
        toolbar, _n, _nb, app_state, _c, _p = _build_toolbar()
        app_state.set_view_mode(ViewMode.EDIT)
        self.assertFalse(toolbar.view_button.get_active())
        self.assertTrue(toolbar.source_button.get_active())

        app_state.set_view_mode(ViewMode.VIEW)
        self.assertTrue(toolbar.view_button.get_active())
        self.assertFalse(toolbar.source_button.get_active())


# ---------------------------------------------------------------------------
# New button
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ToolbarNewButtonTests(unittest.TestCase):
    def _setup_toolbar_with_one_notebook(
        self,
    ) -> tuple[Toolbar, _FakeNoteRepository, AppState]:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="Personal"))
        toolbar, _n, _nb, app_state, _c, _p = _build_toolbar(
            notes=notes,
            notebooks=notebooks,
        )
        return toolbar, notes, app_state

    def test_new_button_inserts_note_into_first_notebook_when_no_selection(
        self,
    ) -> None:
        toolbar, notes, _state = self._setup_toolbar_with_one_notebook()
        # Find and click the new button. It is the first child
        # widget packed at the start of the header bar — but we
        # can drive it more robustly through its semantics by
        # invoking the same path the user click takes.
        # Easiest: emit "clicked" on the underlying button by
        # finding it via the toolbar's get_first_child traversal.
        # Simpler still: the construction path connected
        # ``_on_new_clicked`` to the button's clicked signal; we
        # call it through the controller-as-known route by
        # asserting the result of "what the click would do".
        # For determinism, exercise the click-equivalent code path
        # by emitting on the actual button widget. Locate it:
        new_button = _find_button_with_label(toolbar, "New")
        self.assertIsNotNone(new_button)
        assert new_button is not None
        new_button.emit("clicked")

        self.assertEqual(len(notes.insertions), 1)
        self.assertEqual(notes.insertions[0].notebook_id, "nb-1")

    def test_new_button_uses_selected_notebook_when_one_is_selected(
        self,
    ) -> None:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1", name="Personal"))
        notebooks.add(_make_notebook("nb-2", name="Recipes"))
        app_state = AppState(
            initial_selection=NotebookSelection(notebook_id="nb-2"),
        )
        toolbar, _n, _nb, _state, _c, _p = _build_toolbar(
            notes=notes,
            notebooks=notebooks,
            app_state=app_state,
        )
        new_button = _find_button_with_label(toolbar, "New")
        assert new_button is not None
        new_button.emit("clicked")

        self.assertEqual(notes.insertions[0].notebook_id, "nb-2")

    def test_new_button_switches_app_state_to_edit_mode(self) -> None:
        toolbar, _notes, app_state = self._setup_toolbar_with_one_notebook()
        new_button = _find_button_with_label(toolbar, "New")
        assert new_button is not None
        new_button.emit("clicked")
        self.assertEqual(app_state.view_mode, ViewMode.EDIT)

    def test_new_button_is_a_noop_when_no_notebooks_exist(self) -> None:
        # Empty repositories — clicking New must not raise and must
        # not perform an insertion (no place to put it).
        toolbar, notes, _nb, _state, _c, _p = _build_toolbar()
        new_button = _find_button_with_label(toolbar, "New")
        assert new_button is not None
        new_button.emit("clicked")
        self.assertEqual(notes.insertions, [])


# ---------------------------------------------------------------------------
# More menu — Duplicate / Delete
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ToolbarMoreMenuTests(unittest.TestCase):
    def _build_with_selected_note(
        self,
        *,
        title: str = "My note",
    ) -> tuple[
        Toolbar,
        _FakeNoteRepository,
        AppState,
        _FakeConfirmDialogPresenter,
    ]:
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(_make_notebook("nb-1"))
        notes.add(_make_note("n1", title=title, notebook_id="nb-1"))
        app_state = AppState()
        toolbar, _notes, _nb, _state, _c, presenter = _build_toolbar(
            notes=notes,
            notebooks=notebooks,
            app_state=app_state,
        )
        app_state.set_selected_note_id("n1")
        return toolbar, notes, app_state, presenter

    def test_more_menu_button_becomes_sensitive_with_a_selected_note(
        self,
    ) -> None:
        toolbar, _notes, _state, _p = self._build_with_selected_note()
        self.assertTrue(toolbar.more_menu_button.get_sensitive())

    def test_more_menu_button_becomes_insensitive_after_clearing_selection(
        self,
    ) -> None:
        toolbar, _notes, app_state, _p = self._build_with_selected_note()
        app_state.set_selected_note_id(None)
        self.assertFalse(toolbar.more_menu_button.get_sensitive())

    def test_duplicate_button_invokes_controller_duplicate_note(self) -> None:
        toolbar, notes, _state, _p = self._build_with_selected_note()
        duplicate_button = _find_button_in_popover(
            toolbar.more_popover,
            "Duplicate note",
        )
        assert duplicate_button is not None
        duplicate_button.emit("clicked")
        # The duplicate flow inserts a new note via the controller.
        # The fake repository records every insertion.
        self.assertEqual(len(notes.insertions), 1)
        new_note = notes.insertions[0]
        self.assertNotEqual(new_note.id, "n1")

    def test_delete_button_opens_confirm_dialog_with_note_title(self) -> None:
        toolbar, _notes, _state, presenter = self._build_with_selected_note(
            title="Important",
        )
        delete_button = _find_button_in_popover(
            toolbar.more_popover,
            "Delete note",
        )
        assert delete_button is not None
        delete_button.emit("clicked")

        # Exactly one dialog presented.
        self.assertEqual(len(presenter.calls), 1)
        title, detail, confirm_label = presenter.calls[0]
        self.assertIn("Important", title)
        self.assertIn("cannot be undone", detail.lower())
        self.assertEqual(confirm_label, "Delete")

    def test_confirming_delete_invokes_controller_request_delete(self) -> None:
        toolbar, notes, _state, presenter = self._build_with_selected_note()
        delete_button = _find_button_in_popover(
            toolbar.more_popover,
            "Delete note",
        )
        assert delete_button is not None
        delete_button.emit("clicked")

        # User clicks Delete in the dialog.
        assert presenter.last_callback is not None
        presenter.last_callback(True)

        self.assertEqual(notes.deletions, ["n1"])

    def test_cancelling_delete_does_not_call_controller(self) -> None:
        toolbar, notes, _state, presenter = self._build_with_selected_note()
        delete_button = _find_button_in_popover(
            toolbar.more_popover,
            "Delete note",
        )
        assert delete_button is not None
        delete_button.emit("clicked")

        # User clicks Cancel — or dismisses the dialog.
        assert presenter.last_callback is not None
        presenter.last_callback(False)

        self.assertEqual(notes.deletions, [])


# ---------------------------------------------------------------------------
# Helpers — widget tree traversal for tests
# ---------------------------------------------------------------------------


def _find_button_with_label(
    root: Gtk.Widget,
    label: str,
) -> Gtk.Button | None:
    """Walk ``root``'s descendants for a :class:`Gtk.Button` whose
    visible label matches ``label``.

    Buttons in the toolbar carry text labels either directly
    (``Gtk.Button.new_with_label``) or inside a child :class:`Gtk.Box`
    that contains a :class:`Gtk.Label`. This helper handles both
    shapes by recursively walking the widget tree.
    """
    found: list[Gtk.Button] = []

    def visit(widget: Gtk.Widget) -> None:
        if isinstance(widget, Gtk.Button) and _button_label_matches(widget, label):
            found.append(widget)
            return
        child = widget.get_first_child()
        while child is not None:
            visit(child)
            child = child.get_next_sibling()

    visit(root)
    return found[0] if found else None


def _button_label_matches(button: Gtk.Button, label: str) -> bool:
    """Return whether ``button`` shows ``label`` somewhere on its surface."""
    direct = button.get_label()
    if direct is not None and direct == label:
        return True
    # Buttons set up via ``set_child(Gtk.Box(...))`` carry their
    # label inside the child box. Walk one level into the child.
    child = button.get_child()
    if child is None:
        return False
    if isinstance(child, Gtk.Label):
        return child.get_text() == label
    if isinstance(child, Gtk.Box):
        descendant = child.get_first_child()
        while descendant is not None:
            if isinstance(descendant, Gtk.Label) and descendant.get_text() == label:
                return True
            descendant = descendant.get_next_sibling()
    return False


def _find_button_in_popover(
    popover: Gtk.Popover,
    label: str,
) -> Gtk.Button | None:
    """Find a labelled button inside the More popover's contents box."""
    return _find_button_with_label(popover, label)


if __name__ == "__main__":
    unittest.main()
