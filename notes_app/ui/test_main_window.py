"""Tests for :mod:`notes_app.ui.main_window`."""

from __future__ import annotations

import unittest
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
from notes_app.ui.main_window import MainWindow
from notes_app.ui.note_editor import NoteEditor
from notes_app.ui.note_list import NoteList
from notes_app.ui.note_view import NoteView
from notes_app.ui.sidebar import Sidebar


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget
    construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Fakes — minimal protocol-conforming repositories
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    notes: dict[str, Note]
    last_update: tuple[str, str, datetime] | None

    def __init__(self) -> None:
        self.notes = {}
        self.last_update = None

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
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> None:
        # Record the call so the editor's auto-save flow can be
        # asserted against this fake from the controller's tests.
        self.last_update = (note_id, source, modified_at)

    def update_notebook(self, _note_id: str, _notebook_id: str) -> None:
        raise NotImplementedError

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError


class _FakeNotebookRepository:
    notebooks: dict[str, Notebook]

    def __init__(self) -> None:
        self.notebooks = {}

    def add(self, notebook: Notebook) -> None:
        self.notebooks[notebook.id] = notebook

    def list_all(self) -> list[Notebook]:
        return list(self.notebooks.values())

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
    """Fake :class:`AttachmentStoreProtocol` for window-level tests.

    ``MainWindow`` itself does not call any attachment methods — only
    :class:`NoteController.add_attachment` would. The controller is
    constructed but never asked to attach anything in these tests, so
    raising ``NotImplementedError`` on every method is correct: any
    inadvertent call is a test bug.
    """

    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        raise NotImplementedError

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        raise NotImplementedError

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class MainWindowConstructionTests(unittest.TestCase):
    """End-to-end smoke tests: the shell composes its panes without
    raising."""

    def _build_window(
        self,
        *,
        app_state: AppState | None = None,
    ) -> MainWindow:
        application = Gtk.Application.new(
            "org.notes_app.NotesApp.test",
            0,
        )
        # Register the application before adding windows. Without
        # this GTK emits a critical warning ("New application windows
        # must be added after the GApplication::startup signal has
        # been emitted"), which clutters test output even though the
        # window itself is constructed correctly.
        application.register(None)
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        notebooks.add(
            Notebook(
                id="nb-1",
                name="Personal",
                parent_id=None,
                icon=NotebookIcon.HOME,
            )
        )
        notes.notes["n1"] = Note(
            id="n1",
            title="Hello",
            notebook_id="nb-1",
            source="= Hello\n\nbody.\n",
            snippet="body.",
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        state = app_state if app_state is not None else AppState()
        controller = NoteController(
            repository=notes,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        return MainWindow(
            application=application,
            note_repository=notes,
            notebook_repository=notebooks,
            note_controller=controller,
            app_state=state,
        )

    def test_constructs_and_reports_default_size(self) -> None:
        window = self._build_window()
        self.assertIsInstance(window, Gtk.ApplicationWindow)
        self.assertEqual(window.get_default_size(), (1200, 800))

    def test_window_title_is_notes(self) -> None:
        window = self._build_window()
        self.assertEqual(window.get_title(), "Notes")

    def test_four_panes_are_assigned(self) -> None:
        # The four pane fields are public-ish (single-leading-
        # underscore — internal but stable for tests). Construction
        # populates them with the right concrete types.
        window = self._build_window()
        self.assertIsInstance(window._sidebar, Sidebar)
        self.assertIsInstance(window._note_list, NoteList)
        self.assertIsInstance(window._note_view, NoteView)
        self.assertIsInstance(window._note_editor, NoteEditor)

    def test_root_child_is_outer_paned(self) -> None:
        # The window's child must be a Gtk.Paned (the outer split:
        # sidebar | rest). The end-child of that outer paned must
        # itself be a Gtk.Paned (the inner split: note list |
        # right-pane stack). This is the layout the design demands.
        window = self._build_window()
        outer = window.get_child()
        self.assertIsInstance(outer, Gtk.Paned)

        # Outer start = sidebar; outer end = inner paned.
        assert isinstance(outer, Gtk.Paned)
        self.assertIs(outer.get_start_child(), window._sidebar)
        inner = outer.get_end_child()
        self.assertIsInstance(inner, Gtk.Paned)

        # Inner start = note list; inner end = right-pane stack.
        assert isinstance(inner, Gtk.Paned)
        self.assertIs(inner.get_start_child(), window._note_list)
        self.assertIs(inner.get_end_child(), window._right_pane_stack)

    def test_right_pane_stack_holds_view_and_editor(self) -> None:
        # The Gtk.Stack must contain both the rendered view and the
        # editor — never one or the other. Both stay live across
        # mode toggles so their internal state (undo history, child
        # anchors) is preserved.
        window = self._build_window()
        stack = window._right_pane_stack
        self.assertIsInstance(stack, Gtk.Stack)
        self.assertIs(stack.get_child_by_name("view"), window._note_view)
        self.assertIs(stack.get_child_by_name("edit"), window._note_editor)


@unittest.skipUnless(_display_available(), "no GDK display")
class MainWindowViewModeStackTests(unittest.TestCase):
    """The right-pane stack tracks :attr:`AppState.view_mode`."""

    def _build_window(self, *, view_mode: ViewMode) -> MainWindow:
        application = Gtk.Application.new(
            "org.notes_app.NotesApp.test",
            0,
        )
        application.register(None)
        notes = _FakeNoteRepository()
        notebooks = _FakeNotebookRepository()
        state = AppState(initial_view_mode=view_mode)
        controller = NoteController(
            repository=notes,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        return MainWindow(
            application=application,
            note_repository=notes,
            notebook_repository=notebooks,
            note_controller=controller,
            app_state=state,
        )

    def test_initial_mode_view_shows_view_pane(self) -> None:
        window = self._build_window(view_mode=ViewMode.VIEW)
        self.assertEqual(
            window._right_pane_stack.get_visible_child_name(),
            "view",
        )
        self.assertIs(
            window._right_pane_stack.get_visible_child(),
            window._note_view,
        )

    def test_initial_mode_edit_shows_editor_pane(self) -> None:
        window = self._build_window(view_mode=ViewMode.EDIT)
        self.assertEqual(
            window._right_pane_stack.get_visible_child_name(),
            "edit",
        )
        self.assertIs(
            window._right_pane_stack.get_visible_child(),
            window._note_editor,
        )

    def test_changing_view_mode_swaps_visible_child(self) -> None:
        window = self._build_window(view_mode=ViewMode.VIEW)
        # AppState mutation is what the future toolbar will perform;
        # the window listens for that change and swaps panes.
        window._app_state.set_view_mode(ViewMode.EDIT)
        self.assertEqual(
            window._right_pane_stack.get_visible_child_name(),
            "edit",
        )
        # And back again.
        window._app_state.set_view_mode(ViewMode.VIEW)
        self.assertEqual(
            window._right_pane_stack.get_visible_child_name(),
            "view",
        )


if __name__ == "__main__":
    unittest.main()
