"""Tests for :mod:`notes_app.ui.main_window`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.controllers.note_controller import NoteController
from notes_app.enums import NotebookIcon, ViewMode
from notes_app.models.attachment import Attachment
from notes_app.models.note import Note
from notes_app.models.notebook import Notebook
from notes_app.ui.main_window import (
    MainWindow,
    _ARTICLE_SIDE_SLACK_PX,
    _MIN_DEFAULT_WINDOW_WIDTH_PX,
    _NOTE_LIST_INITIAL_POSITION_PX,
    _PANED_HANDLE_ALLOWANCE_PX,
    _SIDEBAR_INITIAL_POSITION_PX,
    _default_window_width,
)
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


@cache
def _test_application() -> Gtk.Application:
    """The single registered :class:`Gtk.Application` shared by every test.

    GTK supports **one** registered ``GtkApplication`` per process: the
    first to register becomes ``g_application_get_default()`` and installs
    process-global state, and a second *registered* ``GtkApplication`` is
    unsupported (it crashes). So the suite must not build a fresh
    application per test — it builds one, registers it once, and reuses it
    for every window. ``MainWindow`` is a ``Gtk.ApplicationWindow`` and
    several windows may share one application, so reuse is fine.

    Registering (once) before any window is added also suppresses GTK's
    "New application windows must be added after the GApplication::startup
    signal" warning. ``@cache`` makes construction lazy — it happens on the
    first call, i.e. only inside a display-gated test — and keeps the one
    instance alive for the whole process.
    """
    application = Gtk.Application.new(
        "org.notes_app.NotesApp.test",
        Gio.ApplicationFlags.DEFAULT_FLAGS,
    )
    application.register(None)
    return application


# ---------------------------------------------------------------------------
# Fakes — minimal protocol-conforming repositories
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    notes: dict[str, Note]
    update_calls: list[tuple[str, str, datetime]]

    def __init__(self) -> None:
        self.notes = {}
        self.update_calls = []

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
        # Record the call (tests inspect ``update_calls`` to assert
        # the editor's auto-save flow fired exactly once with the
        # right payload) AND mutate the stored note so a subsequent
        # ``get`` returns the just-saved source. The latter is what
        # lets the view-refresh-on-mode-change tests observe the
        # updated content after a flush.
        self.update_calls.append((note_id, source, modified_at))
        existing = self.notes[note_id]
        self.notes[note_id] = Note(
            id=existing.id,
            title=existing.title,
            notebook_id=existing.notebook_id,
            source=source,
            snippet=existing.snippet,
            created_at=existing.created_at,
            modified_at=modified_at,
        )

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


class DefaultWindowWidthTests(unittest.TestCase):
    """Pure arithmetic for :func:`_default_window_width` — no display.

    The function only does integer arithmetic over module constants, so
    these run without a GDK display (the module-level ``gi`` import is
    the only GTK dependency, shared by the whole test file).
    """

    _FIXED_OVERHEAD: int = (
        _SIDEBAR_INITIAL_POSITION_PX
        + _NOTE_LIST_INITIAL_POSITION_PX
        + _PANED_HANDLE_ALLOWANCE_PX
        + _ARTICLE_SIDE_SLACK_PX
    )

    def test_sums_overhead_and_column_when_above_floor(self) -> None:
        # A column wide enough that the sum clears the floor: the result
        # is the four fixed terms plus the column, exactly.
        column = _MIN_DEFAULT_WINDOW_WIDTH_PX  # generously above the floor
        self.assertEqual(
            _default_window_width(column),
            self._FIXED_OVERHEAD + column,
        )

    def test_clamps_up_to_floor_for_tiny_column(self) -> None:
        # A degenerate (zero) column must not yield a sub-floor window.
        self.assertEqual(
            _default_window_width(0),
            _MIN_DEFAULT_WINDOW_WIDTH_PX,
        )

    def test_is_monotonic_in_column_width(self) -> None:
        # A wider column never produces a narrower window.
        narrower = _default_window_width(800)
        wider = _default_window_width(1400)
        self.assertGreaterEqual(wider, narrower)

    def test_result_strictly_exceeds_overhead_plus_column(self) -> None:
        # Above the floor, the window is wider than overhead-without-slack
        # + column, i.e. there is genuine slack so the centring branch can
        # fire on first allocation.
        column = 900
        overhead_no_slack = (
            _SIDEBAR_INITIAL_POSITION_PX
            + _NOTE_LIST_INITIAL_POSITION_PX
            + _PANED_HANDLE_ALLOWANCE_PX
        )
        self.assertGreater(
            _default_window_width(column),
            overhead_no_slack + column,
        )


@unittest.skipUnless(_display_available(), "no GDK display")
class MainWindowConstructionTests(unittest.TestCase):
    """End-to-end smoke tests: the shell composes its panes without
    raising."""

    def _build_window(
        self,
        *,
        app_state: AppState | None = None,
    ) -> MainWindow:
        application = _test_application()
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
        # The default width is derived from the article column the
        # rendered view actually measured (font-dependent, so not a
        # literal here); the height stays the fixed design default.
        expected_width = _default_window_width(
            window._note_view.preferred_column_width_px(),
        )
        self.assertEqual(
            window.get_default_size(),
            (expected_width, 800),
        )

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
        application = _test_application()
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


@unittest.skipUnless(_display_available(), "no GDK display")
class MainWindowViewModeChangeFlushAndRefreshTests(unittest.TestCase):
    """The view-mode-change handler must flush the editor's pending
    autosave AND refresh the rendered view before swapping the stack.

    These tests exercise the bug "view mode shows stale content after
    editing": typing into the source editor and immediately clicking
    View used to reveal the pre-edit content because (a) the editor's
    300 ms debounced save had not yet flushed and (b) the view was
    never asked to re-read from the repository on a mode change.
    """

    def _build_window_with_note(
        self,
        *,
        view_mode: ViewMode,
        note_id: str = "n1",
        source: str = "= Hello\n\nbody.\n",
    ) -> tuple[MainWindow, _FakeNoteRepository, AppState]:
        """Build a window already pointing at a single seeded note.

        The seeded note is inserted into the repository *and* the
        :class:`AppState` is moved to point at it before
        :class:`MainWindow` is constructed, so both :class:`NoteEditor`
        and :class:`NoteView` pick it up in their respective
        constructor-time loads. Returning the repository and the
        :class:`AppState` together with the window lets the tests
        assert against the autosave path and drive view-mode toggles
        without reaching into private window state.
        """
        application = _test_application()
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
        notes.notes[note_id] = Note(
            id=note_id,
            title="Hello",
            notebook_id="nb-1",
            source=source,
            snippet="body.",
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        state = AppState(initial_view_mode=view_mode)
        # Set the selected note BEFORE constructing the window so the
        # editor's and view's constructor-time loads both see it. (Were
        # we to set it after construction, the same load would still
        # run through ``selected-note-changed`` — but pre-setting keeps
        # the test setup linear and avoids interleaving signal handlers
        # with assertion setup.)
        state.set_selected_note_id(note_id)
        controller = NoteController(
            repository=notes,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        window = MainWindow(
            application=application,
            note_repository=notes,
            notebook_repository=notebooks,
            note_controller=controller,
            app_state=state,
        )
        return window, notes, state

    def _view_buffer_text(self, window: MainWindow) -> str:
        """Pull the rendered view's buffer text as a plain string."""
        buffer = window._note_view._buffer
        return buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            False,
        )

    def _editor_buffer_text(self, window: MainWindow) -> str:
        """Pull the source editor's buffer text as a plain string."""
        buffer = window._note_editor._buffer
        return buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            False,
        )

    def test_view_mode_change_to_view_flushes_pending_editor_save(
        self,
    ) -> None:
        """A debounced autosave armed while in EDIT must hit the
        repository before the stack swaps to VIEW.

        This is half of the original bug: without the flush, the
        last-typed text sat only in the editor's in-memory buffer at
        the moment the user clicked View, so View reads from disk
        and shows the pre-edit content. The repository's recorded
        ``update_calls`` is the witness — exactly one call carrying
        the buffer's text after the toggle.

        Note on timer mechanics: no GLib main loop runs during the
        test, so the real 300 ms timer that ``_schedule_save``
        registers never fires on its own. The single
        ``update_calls`` entry can therefore only come from the
        synchronous flush our handler performs.
        """
        window, repo, state = self._build_window_with_note(
            view_mode=ViewMode.EDIT,
        )
        # Sanity-check setup: the editor loaded the seeded source and
        # no save has happened yet.
        self.assertEqual(self._editor_buffer_text(window), "= Hello\n\nbody.\n")
        self.assertEqual(repo.update_calls, [])

        # Simulate the user typing — programmatic insert produces
        # the same ``changed`` signal sequence as keypress-driven
        # input, which is what arms the debounced autosave.
        editor_buffer = window._note_editor._buffer
        editor_buffer.insert(editor_buffer.get_end_iter(), "XYZ")

        # Toggle to View. Our handler must flush the pending save
        # before the stack swaps.
        state.set_view_mode(ViewMode.VIEW)

        self.assertEqual(len(repo.update_calls), 1)
        saved_note_id, saved_source, _ = repo.update_calls[0]
        self.assertEqual(saved_note_id, "n1")
        self.assertEqual(saved_source, "= Hello\n\nbody.\nXYZ")

    def test_view_mode_change_to_view_refreshes_view_pane(self) -> None:
        """A mode change to VIEW must re-read the source from the
        repository so any disk-side change since the last render
        becomes visible.

        Even with the editor flush in place, the view would still
        show stale content unless it is asked to refresh on every
        mode change — its ``selected-note-changed`` subscription is
        not enough on its own. We simulate "disk got updated"
        without going through ``selected-note-changed`` by mutating
        the fake repository directly; the toggle to EDIT and back
        to VIEW is what must force the re-read.
        """
        window, repo, state = self._build_window_with_note(
            view_mode=ViewMode.VIEW,
            source="= old\n",
        )
        # The initial render already happened during construction.
        self.assertIn("old", self._view_buffer_text(window))

        # Mutate the underlying note out from under the view, without
        # firing ``selected-note-changed`` (i.e. simulate that disk
        # now holds different content).
        existing = repo.notes["n1"]
        repo.notes["n1"] = Note(
            id=existing.id,
            title=existing.title,
            notebook_id=existing.notebook_id,
            source="= new\n",
            snippet=existing.snippet,
            created_at=existing.created_at,
            modified_at=existing.modified_at,
        )

        # The view still shows "old" because nothing has prompted it
        # to re-read.
        self.assertIn("old", self._view_buffer_text(window))
        self.assertNotIn("new", self._view_buffer_text(window))

        # Toggle VIEW → EDIT → VIEW. The second transition is where
        # our handler asks the view to refresh.
        state.set_view_mode(ViewMode.EDIT)
        state.set_view_mode(ViewMode.VIEW)

        rendered = self._view_buffer_text(window)
        self.assertIn("new", rendered)
        self.assertNotIn("old", rendered)

    def test_view_mode_change_to_view_runs_flush_before_refresh(self) -> None:
        """The order matters: flush must precede refresh.

        If the refresh ran first, it would re-read the pre-edit
        source from the repository and the just-typed text would
        not appear in the rendered view (it would only land on disk
        a moment later when the flush ran, by which point the
        rendered buffer has already been re-populated with the old
        content).

        This is the end-to-end witness: type into the editor, toggle
        to View, and read the rendered text — it must contain the
        typed content.
        """
        window, _repo, state = self._build_window_with_note(
            view_mode=ViewMode.EDIT,
            source="= original\n",
        )
        editor_buffer = window._note_editor._buffer
        editor_buffer.insert(editor_buffer.get_end_iter(), "MARKER")

        # The view is still mid-construction-time text ("original");
        # no refresh has happened since the typing.
        self.assertNotIn("MARKER", self._view_buffer_text(window))

        state.set_view_mode(ViewMode.VIEW)

        # If flush ran AFTER refresh, the view would still show the
        # pre-edit content and this assertion would fail.
        rendered = self._view_buffer_text(window)
        self.assertIn("MARKER", rendered)

    def test_view_mode_change_to_edit_is_safe_when_nothing_pending(
        self,
    ) -> None:
        """The no-op path: a VIEW → EDIT toggle with no pending save
        must not produce a spurious repository write, and the editor
        must hold the note's source ready for editing.

        Both ``flush_pending_save`` (nothing pending) and
        ``refresh`` (idempotent re-render) are no-ops in this
        direction; the test pins that down so a future refactor
        cannot accidentally introduce a write on every toggle.
        """
        window, repo, state = self._build_window_with_note(
            view_mode=ViewMode.VIEW,
            source="= Hello\n",
        )
        self.assertEqual(repo.update_calls, [])

        state.set_view_mode(ViewMode.EDIT)

        # No save should have happened — nothing was pending and
        # nothing was typed.
        self.assertEqual(repo.update_calls, [])
        # And the editor's buffer correctly mirrors the note's source.
        self.assertEqual(self._editor_buffer_text(window), "= Hello\n")
        # The stack swap still happened.
        self.assertEqual(
            window._right_pane_stack.get_visible_child_name(),
            "edit",
        )


if __name__ == "__main__":
    unittest.main()
