"""Tests for :mod:`ui.main_window`."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gtk

from asciidoc.summary import derive_summary
from enums import AttachmentExportFailureReason, ViewMode, WindowAction
from storage.protocols import AttachmentExportFailed
from models.attachment import Attachment
from models.note import Note
from models.session_state import DEFAULT_SESSION_STATE, SessionState
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui.application import NotesApplication
from giruntime.ui.main_window import (
    MainWindow,
    _ARTICLE_SIDE_SLACK_PX,
    _MIN_DEFAULT_WINDOW_WIDTH_PX,
    _NOTE_LIST_INITIAL_POSITION_PX,
    _PANED_HANDLE_ALLOWANCE_PX,
    _SIDEBAR_INITIAL_POSITION_PX,
    _default_window_width,
)
from giruntime.ui.note_editor import NoteEditor
from giruntime.ui.note_list import NoteList
from giruntime.ui.note_view import NoteView
from giruntime.ui.sidebar import Sidebar


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _id_sequence() -> Callable[[], str]:
    """Return a deterministic, monotonically-increasing id factory."""
    counter = {"n": 1}

    def factory() -> str:
        counter["n"] += 1
        return f"gen-{counter['n']}"

    return factory


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget
    construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def _pump_main_loop() -> None:
    """Drain pending GLib main-loop events.

    A headless compositor does not reliably map/allocate a presented
    surface synchronously, so any assertion that depends on realised
    window state (e.g. :meth:`Gtk.Window.is_maximized` after
    :meth:`Gtk.Window.present`) needs the loop pumped first.
    """
    context = GLib.MainContext.default()
    for _ in range(50):
        while context.pending():
            context.iteration(False)


_TEST_APPLICATION_ID: str = "io.github.rand_byte.Folio.test"
"""Application id the shared test app registers under.

Deliberately distinct from production's ``io.github.rand_byte.Folio`` so
the test app never connects — as a single-instance remote — to a real
``folio`` already running on the developer's session bus. A remote registration
would not emit ``startup`` locally, which is exactly the state that
trips the window-add warning this fixture exists to avoid.
"""


@cache
def _test_application() -> NotesApplication:
    """The single registered :class:`NotesApplication` shared by every test.

    GTK supports **one** registered ``GtkApplication`` per process: the
    first to register becomes ``g_application_get_default()`` and installs
    process-global state, and a second *registered* ``GtkApplication`` is
    unsupported (it crashes). So the suite must not build a fresh
    application per test — it builds one, registers it once, and reuses it
    for every window. ``MainWindow`` and ``HelpWindow`` are
    ``Gtk.ApplicationWindow`` s, and several windows may share one
    application, so reuse is fine.

    It is a real :class:`NotesApplication` rather than a bare
    ``Gtk.Application`` so the display-gated help tests can drive the
    app-scoped help seams (:meth:`NotesApplication._ensure_help_window`,
    :meth:`NotesApplication._install_help_action`) against a *registered*
    owner. A ``Gtk.ApplicationWindow`` may only be added to an application
    whose ``startup`` has fired — which registration is what does — so the
    help window, like every ``MainWindow`` here, needs a registered owner;
    an unregistered one both warns and silently drops the window. The id
    and flags are reset to the isolated test values *before* registering
    (they are read-write until then). Registration alone does **not** open
    the database — that is :meth:`do_activate`'s job (via
    ``_initialise_runtime``), and the app is never activated here.

    Registering (once) before any window is added is also what suppresses
    GTK's "New application windows must be added after the
    GApplication::startup signal" warning. ``@cache`` makes construction
    lazy — it happens on the first call, i.e. only inside a display-gated
    test — and keeps the one instance alive for the whole process.
    """
    application = NotesApplication()
    application.set_application_id(_TEST_APPLICATION_ID)
    application.set_flags(Gio.ApplicationFlags.DEFAULT_FLAGS)
    application.register(None)
    return application


# ---------------------------------------------------------------------------
# Fakes — minimal protocol-conforming repositories
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    notes: dict[str, Note]
    update_calls: list[tuple[str, str, datetime]]
    tag_pairs: tuple[tuple[str, int], ...]

    def __init__(self) -> None:
        self.notes = {}
        self.update_calls = []
        # Mutable so a test can put a tag "on disk" and then drive a
        # refresh through ``notes-changed``; defaults empty so the
        # existing tests (which never touch it) keep seeing no tags.
        self.tag_pairs = ()

    def list_all(self) -> list[Note]:
        return list(self.notes.values())

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]


    def list_modified_since(self, _since: datetime) -> list[Note]:
        raise NotImplementedError

    def search(self, _query: str) -> list[Note]:
        raise NotImplementedError

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
        self.notes[note.id] = persisted
        return persisted

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        # Record the call (tests inspect ``update_calls`` to assert
        # the editor's auto-save flow fired exactly once with the
        # right payload) AND re-derive the cached fields so a tag edit
        # propagates to the sidebar the way the real repository does.
        self.update_calls.append((note_id, source, modified_at))
        existing = self.notes[note_id]
        summary = derive_summary(source)
        updated = Note(
            id=existing.id,
            title=summary.title,
            source=source,
            snippet=summary.snippet,
            tags=summary.tags,
            created_at=existing.created_at,
            modified_at=modified_at,
        )
        self.notes[note_id] = updated
        return updated

    def delete(self, note_id: str) -> None:
        del self.notes[note_id]

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return self.tag_pairs


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

    def count_for_note(self, _note_id: str) -> int:
        return 0

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError

    def export_to(self, attachment_id: str, destination: Path) -> None:
        """Write the attachment's bytes out (the outbound mirror of add)."""
        try:
            data = self.get_bytes(attachment_id)
        except KeyError as exc:
            raise AttachmentExportFailed(
                AttachmentExportFailureReason.UNKNOWN_ATTACHMENT,
            ) from exc
        try:
            destination.write_bytes(data)
        except OSError as exc:
            raise AttachmentExportFailed(
                AttachmentExportFailureReason.DESTINATION_UNWRITABLE,
            ) from exc


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
        restored_state: SessionState = DEFAULT_SESSION_STATE,
    ) -> MainWindow:
        application = _test_application()
        notes = _FakeNoteRepository()
        notes.notes["n1"] = Note(
            id="n1",
            title="Hello",
            source="= Hello\n\nbody.\n",
            snippet="body.",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        state = app_state if app_state is not None else AppState()
        store = NoteListStore(repository=notes)
        store.load()
        controller = NoteController(
            note_store=store,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        return MainWindow(
            application=application,
            note_store=store,
            note_controller=controller,
            app_state=state,
            restored_state=restored_state,
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
class RestoredSessionStateTests(unittest.TestCase):
    """A restored :class:`SessionState` overrides the computed default
    window size and drives the maximized state; omitting it (the
    :data:`DEFAULT_SESSION_STATE` default) reproduces today's
    unrestored behaviour."""

    def _build_window(self, *, restored_state: SessionState) -> MainWindow:
        application = _test_application()
        notes = _FakeNoteRepository()
        notes.notes["n1"] = Note(
            id="n1",
            title="Hello",
            source="= Hello\n\nbody.\n",
            snippet="body.",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        state = AppState()
        store = NoteListStore(repository=notes)
        store.load()
        controller = NoteController(
            note_store=store,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        return MainWindow(
            application=application,
            note_store=store,
            note_controller=controller,
            app_state=state,
            restored_state=restored_state,
        )

    def test_no_restored_state_uses_computed_default(self) -> None:
        window = self._build_window(restored_state=DEFAULT_SESSION_STATE)
        expected_width = _default_window_width(
            window._note_view.preferred_column_width_px(),
        )
        self.assertEqual(
            window.get_default_size(),
            (expected_width, 800),
        )
        self.assertFalse(window.is_maximized())

    def test_restored_size_overrides_the_computed_default(self) -> None:
        restored = SessionState(
            selected_note_id=None,
            window_size=(1500, 950),
            window_maximized=False,
        )
        window = self._build_window(restored_state=restored)
        self.assertEqual(window.get_default_size(), (1500, 950))

    def test_restored_maximized_true_maximizes_the_window(self) -> None:
        restored = SessionState(
            selected_note_id=None,
            window_size=(1200, 800),
            window_maximized=True,
        )
        window = self._build_window(restored_state=restored)
        window.present()
        _pump_main_loop()
        self.assertTrue(window.is_maximized())

    def test_restored_maximized_false_does_not_maximize(self) -> None:
        restored = SessionState(
            selected_note_id=None,
            window_size=(1200, 800),
            window_maximized=False,
        )
        window = self._build_window(restored_state=restored)
        window.present()
        _pump_main_loop()
        self.assertFalse(window.is_maximized())


@unittest.skipUnless(_display_available(), "no GDK display")
class MainWindowViewModeStackTests(unittest.TestCase):
    """The right-pane stack tracks :attr:`AppState.view_mode`."""

    def _build_window(self, *, view_mode: ViewMode) -> MainWindow:
        application = _test_application()
        notes = _FakeNoteRepository()
        state = AppState(initial_view_mode=view_mode)
        store = NoteListStore(repository=notes)
        store.load()
        controller = NoteController(
            note_store=store,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        return MainWindow(
            application=application,
            note_store=store,
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
        notes.notes[note_id] = Note(
            id=note_id,
            title="Hello",
            source=source,
            snippet="body.",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        state = AppState(initial_view_mode=view_mode)
        # Set the selected note BEFORE constructing the window so the
        # editor's and view's constructor-time loads both see it. (Were
        # we to set it after construction, the same load would still
        # run through ``notify::selected-note-id`` — but pre-setting keeps
        # the test setup linear and avoids interleaving signal handlers
        # with assertion setup.)
        state.set_selected_note_id(note_id)
        store = NoteListStore(repository=notes)
        store.load()
        controller = NoteController(
            note_store=store,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        window = MainWindow(
            application=application,
            note_store=store,
            note_controller=controller,
            app_state=state,
        )
        return window, notes, state

    def _view_buffer_text(self, window: MainWindow) -> str:
        """Pull the rendered view's buffer text as a plain string."""
        buffer = window._note_view._buffer
        text: str = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            False,
        )
        return text

    def _editor_buffer_text(self, window: MainWindow) -> str:
        """Pull the source editor's buffer text as a plain string."""
        buffer = window._note_editor._buffer
        text: str = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            False,
        )
        return text

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
        """Toggling to VIEW flushes the editor and shows the new content.

        With the store as the source of truth the view re-renders from
        it; the remaining job of the mode-change handler is to flush the
        editor's pending (debounced, unfired) autosave so the just-typed
        text is in the store before the view reads it. We start in EDIT,
        retype the body, and toggle to VIEW: the flush writes through to
        the store and the rendered view shows the new title.
        """
        window, _repo, state = self._build_window_with_note(
            view_mode=ViewMode.EDIT,
            source="= old\n",
        )
        # The view pane rendered "old" at construction time.
        self.assertIn("old", self._view_buffer_text(window))

        # Retype the body in the editor. The autosave is debounced and
        # — with no GLib loop running — has not fired, so the store
        # still holds "old".
        editor_buffer = window._note_editor._buffer
        editor_buffer.set_text("= new\n")
        self.assertEqual(window._note_store.get_note("n1").source, "= old\n")

        # Toggle VIEW. The handler flushes (editor → store) then the
        # view re-reads and shows "new".
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


@unittest.skipUnless(_display_available(), "no GDK display")
class MainWindowFlushEditorTests(unittest.TestCase):
    """`MainWindow.flush_editor` forces a pending debounced autosave to
    the store.

    This is the seam :class:`NotesApplication`'s ``close-request``
    handler calls before quitting: window close ends the process, so a
    save still inside the editor's 300 ms debounce window would be lost.
    The tests pin the end-to-end path type-into-editor → ``flush_editor``
    → store row updated — exactly the "typed → closed within the debounce
    window → row updated" case that previously survived unnoticed because
    nothing exercised the close path end to end.
    """

    def _build_window_with_note(
        self,
        *,
        note_id: str = "n1",
        source: str = "= Hello\n\nbody.\n",
    ) -> tuple[MainWindow, NoteListStore]:
        """Build a window in EDIT mode already pointing at one note.

        Mirrors the view-mode class's builder: seed the repository and
        move :class:`AppState` to the note *before* constructing the
        window, so the editor's constructor-time load picks it up. The
        store is returned so tests can assert the write-through row
        without reaching into private editor state.
        """
        application = _test_application()
        notes = _FakeNoteRepository()
        notes.notes[note_id] = Note(
            id=note_id,
            title="Hello",
            source=source,
            snippet="body.",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        state = AppState(initial_view_mode=ViewMode.EDIT)
        state.set_selected_note_id(note_id)
        store = NoteListStore(repository=notes)
        store.load()
        controller = NoteController(
            note_store=store,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        window = MainWindow(
            application=application,
            note_store=store,
            note_controller=controller,
            app_state=state,
        )
        return window, store

    def test_flush_editor_writes_pending_save_to_store(self) -> None:
        """Typing then flushing (no mode change, no timer) updates the row.

        No GLib loop runs during the test, so the debounced 300 ms save
        never fires on its own; the store update can therefore only come
        from the synchronous flush. This is the close-path guarantee —
        keystrokes typed within the debounce window survive the close.
        """
        window, store = self._build_window_with_note(source="= Hello\n")
        # Nothing saved yet: the store still holds the seeded source.
        self.assertEqual(store.get_note("n1").source, "= Hello\n")

        # Simulate the user typing — a programmatic insert produces the
        # same ``changed`` signal that arms the debounced autosave.
        editor_buffer = window._note_editor._buffer
        editor_buffer.insert(editor_buffer.get_end_iter(), "MORE")

        window.flush_editor()

        self.assertEqual(store.get_note("n1").source, "= Hello\nMORE")

    def test_flush_editor_is_a_noop_when_nothing_pending(self) -> None:
        """A flush with no armed save must not write to the store.

        ``flush_editor`` is called unconditionally on close, so it has to
        be safe when the user typed nothing since the last save. The
        seeded row must be left exactly as it was.
        """
        window, store = self._build_window_with_note(source="= Hello\n")

        window.flush_editor()

        self.assertEqual(store.get_note("n1").source, "= Hello\n")


@unittest.skipUnless(_display_available(), "no GDK display")
class MainWindowStorePropagationTests(unittest.TestCase):
    """A store mutation propagates to the data-driven panes.

    There is no longer a ``notes-changed`` fan-out: the note list binds
    a ``Filter``/``Sort``/``ListView`` chain over the store and the
    sidebar binds a :class:`TagCountsModel` over it, so a create / edit /
    delete in the store ripples to both panes via ``items-changed``
    without the window arbitrating.
    """

    def _build_window(
        self,
        *,
        select_note: bool = False,
    ) -> tuple[MainWindow, NoteListStore, AppState]:
        application = _test_application()
        notes = _FakeNoteRepository()
        notes.notes["n1"] = Note(
            id="n1",
            title="Hello",
            source="= Hello\n\nbody.\n",
            snippet="body.",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        state = AppState()
        if select_note:
            state.set_selected_note_id("n1")
        store = NoteListStore(
            repository=notes,
            clock=lambda: _FIXED_NOW,
            id_factory=_id_sequence(),
        )
        store.load()
        controller = NoteController(
            note_store=store,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        window = MainWindow(
            application=application,
            note_store=store,
            note_controller=controller,
            app_state=state,
        )
        return window, store, state

    @staticmethod
    def _note_ids(window: MainWindow) -> list[str]:
        model = window._note_list._sort_model
        return [model.get_item(i).note.id for i in range(model.get_n_items())]

    def test_store_create_appears_in_note_list(self) -> None:
        window, store, _state = self._build_window()
        self.assertEqual(self._note_ids(window), ["n1"])
        self.assertEqual(window._note_list._count_label.get_text(), "1 notes")

        store.create("= Note n2\n\nbody")

        ids = self._note_ids(window)
        self.assertIn("n1", ids)
        self.assertEqual(len(ids), 2)
        self.assertEqual(window._note_list._count_label.get_text(), "2 notes")

    def test_store_tagged_note_appears_in_sidebar(self) -> None:
        window, store, _state = self._build_window()
        sidebar = window._sidebar
        self.assertEqual(sidebar.tag_model.get_n_items(), 0)

        store.create("= Project note\n:tags: project\n\nbody")

        self.assertEqual(sidebar.tag_model.get_n_items(), 1)
        item = sidebar.tag_model.get_item(0)
        self.assertEqual(getattr(item, "name", None), "project")
        self.assertEqual(getattr(item, "count", None), 1)

    def test_store_mutation_preserves_unrelated_selection(self) -> None:
        window, store, _state = self._build_window(select_note=True)
        selection = window._note_list._selection_model
        self.assertEqual(selection.get_selected_item().note.id, "n1")

        # A direct store create does not touch AppState's selection, so
        # the note list must keep "n1" highlighted across the splice.
        store.create("= Note n2\n\nbody")

        self.assertEqual(selection.get_selected_item().note.id, "n1")

    def test_delete_of_selected_note_clears_list_selection(self) -> None:
        window, _store, _state = self._build_window(select_note=True)
        selection = window._note_list._selection_model
        self.assertEqual(selection.get_selected_item().note.id, "n1")

        # Deleting through the controller removes the row and clears the
        # AppState selection; the note list mirrors that to no selection.
        window._note_controller.request_delete("n1")

        self.assertIsNone(selection.get_selected_item())


@unittest.skipUnless(_display_available(), "no GDK display")
class MainWindowKeyboardActionTests(unittest.TestCase):
    """The window-scoped ``win.*`` keyboard actions: that they are
    registered, carry the right accelerators (and that Delete carries
    *none*), and drive the same behaviour as their toolbar buttons."""

    def _build_window(self, *, app_state: AppState) -> MainWindow:
        application = _test_application()
        notes = _FakeNoteRepository()
        notes.notes["n1"] = Note(
            id="n1",
            title="Hello",
            source="= Hello\n\nbody.\n",
            snippet="body.",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        store = NoteListStore(repository=notes)
        store.load()
        controller = NoteController(
            note_store=store,
            attachments=_FakeAttachmentStore(),
            app_state=app_state,
        )
        return MainWindow(
            application=application,
            note_store=store,
            note_controller=controller,
            app_state=app_state,
        )

    def test_all_window_actions_are_registered(self) -> None:
        window = self._build_window(app_state=AppState())
        for window_action in WindowAction:
            self.assertIsNotNone(
                window.lookup_action(window_action.value),
                f"missing window action: {window_action.value}",
            )

    def test_letter_actions_carry_control_accelerators(self) -> None:
        application = _test_application()
        self._build_window(app_state=AppState())
        self.assertEqual(
            application.get_accels_for_action("win.new-note"),
            ["<Control>n"],
        )
        self.assertEqual(
            application.get_accels_for_action("win.focus-search"),
            ["<Control>f"],
        )
        self.assertEqual(
            application.get_accels_for_action("win.toggle-mode"),
            ["<Control>e"],
        )

    def test_delete_has_no_application_accelerator(self) -> None:
        # The tripwire for the "Delete eats editor text" failure mode:
        # delete must be a focus-local shortcut on the note list, never a
        # window/application accelerator that fires while editing source.
        application = _test_application()
        self._build_window(app_state=AppState())
        self.assertEqual(
            application.get_accels_for_action("win.delete-note"),
            [],
        )

    def test_new_note_action_creates_a_note_and_enters_edit(self) -> None:
        app_state = AppState()
        window = self._build_window(app_state=app_state)
        before = window._note_store.get_n_items()

        action = window.lookup_action("new-note")
        assert action is not None
        action.activate(None)

        self.assertEqual(window._note_store.get_n_items(), before + 1)
        self.assertEqual(app_state.view_mode, ViewMode.EDIT)

    def test_toggle_mode_action_flips_between_view_and_edit(self) -> None:
        app_state = AppState(initial_view_mode=ViewMode.VIEW)
        window = self._build_window(app_state=app_state)

        action = window.lookup_action("toggle-mode")
        assert action is not None

        action.activate(None)
        self.assertEqual(app_state.view_mode, ViewMode.EDIT)
        action.activate(None)
        self.assertEqual(app_state.view_mode, ViewMode.VIEW)

    def test_delete_action_enabled_tracks_selection(self) -> None:
        app_state = AppState()
        window = self._build_window(app_state=app_state)

        action = window.lookup_action("delete-note")
        assert action is not None

        self.assertFalse(action.get_enabled())
        app_state.set_selected_note_id("n1")
        self.assertTrue(action.get_enabled())
        app_state.set_selected_note_id(None)
        self.assertFalse(action.get_enabled())


if __name__ == "__main__":
    unittest.main()
