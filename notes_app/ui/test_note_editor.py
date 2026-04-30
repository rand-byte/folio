"""Tests for :mod:`notes_app.ui.note_editor`."""

from __future__ import annotations

import sqlite3
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GtkSource", "5")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk, GtkSource  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.controllers.note_controller import NoteController
from notes_app.models.attachment import Attachment
from notes_app.models.note import Note
from notes_app.ui.note_editor import (
    AUTOSAVE_DEBOUNCE_MS,
    LANGUAGE_FILE_NAME,
    LANGUAGE_ID,
    NoteEditor,
    _PLACEHOLDER_SELECTION_TEXT,
    _bundled_language_dir,
    buffer_text,
    insert_block_line,
    load_asciidoc_language,
    wrap_selection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for any
    :class:`Gtk.Widget` subclass construction.

    The pure helpers (:func:`wrap_selection`, :func:`insert_block_line`,
    :func:`buffer_text`) operate on :class:`Gtk.TextBuffer`, which does
    NOT need a display — those tests run unconditionally.
    """
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def _make_note(
    note_id: str,
    *,
    source: str = "= Hello\n\nbody.\n",
    notebook_id: str = "nb-1",
) -> Note:
    return Note(
        id=note_id,
        title="Hello",
        notebook_id=notebook_id,
        source=source,
        snippet="body.",
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW + timedelta(seconds=1),
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    notes: dict[str, Note]
    update_calls: list[tuple[str, str, datetime]]

    def __init__(self) -> None:
        self.notes = {}
        self.update_calls = []

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]

    def list_by_notebook(self, _notebook_id: str) -> list[Note]:
        raise NotImplementedError

    def list_modified_since(self, _since: datetime) -> list[Note]:
        raise NotImplementedError

    def list_all(self) -> list[Note]:
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


class _RaisingNoteRepository(  # pylint: disable=abstract-method
    _FakeNoteRepository,
):
    """Variant whose :meth:`update_source` raises a database error.

    Used to verify the editor's auto-save callback survives a failed
    save: the controller's ``storage-error`` signal still fires, but
    the GLib timer callback must not re-raise into the main loop.

    The ``abstract-method`` suppression: pylint reads the parent's
    ``raise NotImplementedError`` bodies as abstract declarations, but
    they are deliberately concrete runtime guards on methods this fake
    never expects to be called. Inheriting them unchanged is correct;
    overriding here would just duplicate the parent's bodies.
    """

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> None:
        # Record the attempt so tests can assert it was made before
        # raising. ``OperationalError`` is a subclass of
        # ``DatabaseError`` and is the most common storage failure
        # mode (lock contention, disk full, etc.).
        self.update_calls.append((note_id, source, modified_at))
        raise sqlite3.OperationalError("simulated DB failure")


class _FakeAttachmentStore:
    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        raise NotImplementedError

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        raise NotImplementedError

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError


class _FakeTimeoutBackend:
    """Synchronous stand-in for :func:`GLib.timeout_add` / :func:`source_remove`.

    The editor schedules and cancels timers through this object's
    bound methods. Tests drive the editor's auto-save flow by calling
    :meth:`fire_pending` to invoke the most recent scheduled callback
    synchronously — no main loop required.
    """

    schedule_calls: list[tuple[int, Callable[[], bool]]]
    cancel_calls: list[int]
    _next_handle: int
    _pending: dict[int, Callable[[], bool]]

    def __init__(self) -> None:
        self.schedule_calls = []
        self.cancel_calls = []
        self._next_handle = 1000
        self._pending = {}

    def schedule(
        self,
        delay_ms: int,
        callback: Callable[[], bool],
    ) -> int:
        self.schedule_calls.append((delay_ms, callback))
        handle = self._next_handle
        self._next_handle += 1
        self._pending[handle] = callback
        return handle

    def cancel(self, handle: int) -> None:
        self.cancel_calls.append(handle)
        self._pending.pop(handle, None)

    def fire_pending(self) -> None:
        """Synchronously invoke every still-pending callback.

        Mirrors GLib firing all registered timers. Each callback's
        return value is honoured: ``False`` (``SOURCE_REMOVE``)
        removes it from the pending set, ``True`` keeps it alive
        for another fire — though the editor never returns the
        latter.
        """
        for handle, callback in list(self._pending.items()):
            keep = callback()
            if not keep:
                self._pending.pop(handle, None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)


# ---------------------------------------------------------------------------
# Pure helpers — wrap_selection / insert_block_line / buffer_text
# ---------------------------------------------------------------------------


class WrapSelectionTests(unittest.TestCase):
    """Pure helper — :class:`Gtk.TextBuffer` is enough; no display."""

    def _make_buffer(self, text: str) -> Gtk.TextBuffer:
        buffer = Gtk.TextBuffer.new(None)
        buffer.set_text(text)
        return buffer

    def test_wraps_existing_selection(self) -> None:
        buffer = self._make_buffer("hello world")
        # Select "hello" (offsets 0..5).
        buffer.select_range(
            buffer.get_iter_at_offset(0),
            buffer.get_iter_at_offset(5),
        )
        wrap_selection(buffer, before="*", after="*")
        self.assertEqual(buffer_text(buffer), "*hello* world")

    def test_wraps_with_distinct_open_close(self) -> None:
        buffer = self._make_buffer("foo bar")
        buffer.select_range(
            buffer.get_iter_at_offset(4),
            buffer.get_iter_at_offset(7),
        )
        wrap_selection(buffer, before="[.line-through]#", after="#")
        self.assertEqual(buffer_text(buffer), "foo [.line-through]#bar#")

    def test_no_selection_inserts_placeholder_and_wraps_it(self) -> None:
        buffer = self._make_buffer("")
        # Cursor sits at offset 0 by default.
        wrap_selection(buffer, before="*", after="*")
        self.assertEqual(
            buffer_text(buffer),
            f"*{_PLACEHOLDER_SELECTION_TEXT}*",
        )

    def test_no_selection_leaves_placeholder_selected(self) -> None:
        buffer = self._make_buffer("")
        wrap_selection(buffer, before="*", after="*")
        bounds = buffer.get_selection_bounds()
        self.assertIsNotNone(bounds)
        assert bounds is not None and len(bounds) == 2
        start, end = bounds
        # The placeholder text is the four characters
        # immediately after the opening "*".
        self.assertEqual(start.get_offset(), 1)
        self.assertEqual(
            end.get_offset(),
            1 + len(_PLACEHOLDER_SELECTION_TEXT),
        )

    def test_existing_selection_remains_selected_after_wrap(self) -> None:
        # The reference behaviour (React's ``setSelectionRange``):
        # the user's content stays highlighted so they can keep
        # editing it after the wrap.
        buffer = self._make_buffer("hello world")
        buffer.select_range(
            buffer.get_iter_at_offset(0),
            buffer.get_iter_at_offset(5),
        )
        wrap_selection(buffer, before="_", after="_")
        bounds = buffer.get_selection_bounds()
        self.assertIsNotNone(bounds)
        assert bounds is not None and len(bounds) == 2
        start, end = bounds
        # Original selection was [0..5]; after a 1-char prefix
        # insert, it shifts to [1..6].
        self.assertEqual(start.get_offset(), 1)
        self.assertEqual(end.get_offset(), 6)

    def test_underline_wraps_with_distinct_open_close(self) -> None:
        # Coverage for the [.underline]# … # delimiters as well —
        # asserts that the helper handles asymmetric delimiters
        # generally, not just for strikethrough.
        buffer = self._make_buffer("note")
        buffer.select_range(
            buffer.get_iter_at_offset(0),
            buffer.get_iter_at_offset(4),
        )
        wrap_selection(buffer, before="[.underline]#", after="#")
        self.assertEqual(buffer_text(buffer), "[.underline]#note#")


class InsertBlockLineTests(unittest.TestCase):
    """Pure helper — operates on a vanilla :class:`Gtk.TextBuffer`."""

    def _make_buffer(self, text: str) -> Gtk.TextBuffer:
        buffer = Gtk.TextBuffer.new(None)
        buffer.set_text(text)
        return buffer

    def test_inserts_into_empty_buffer_without_leading_newline(self) -> None:
        buffer = self._make_buffer("")
        insert_block_line(buffer, text="== Heading")
        self.assertEqual(buffer_text(buffer), "== Heading\n")

    def test_inserts_after_existing_text_with_leading_newline(self) -> None:
        buffer = self._make_buffer("first paragraph")
        # Place cursor at end (offset 15 = "first paragraph").
        buffer.place_cursor(buffer.get_iter_at_offset(15))
        insert_block_line(buffer, text="== Heading")
        # Leading newline appears because the cursor was on a
        # non-empty line.
        self.assertEqual(
            buffer_text(buffer),
            "first paragraph\n== Heading\n",
        )

    def test_inserts_on_empty_line_without_extra_leading_newline(self) -> None:
        # Cursor at the start of a fresh blank line — no leading
        # newline should be added.
        buffer = self._make_buffer("first\n")
        buffer.place_cursor(buffer.get_iter_at_offset(6))
        insert_block_line(buffer, text="* item")
        self.assertEqual(buffer_text(buffer), "first\n* item\n")

    def test_cursor_lands_at_end_of_inserted_text(self) -> None:
        buffer = self._make_buffer("")
        insert_block_line(buffer, text="* item")
        # Cursor should be at offset len("* item") = 6, i.e. just
        # before the trailing newline. The user's next keystroke
        # extends the bullet item, not the line below.
        cursor_iter = buffer.get_iter_at_mark(buffer.get_insert())
        self.assertEqual(cursor_iter.get_offset(), 6)

    def test_inserts_multi_line_template_intact(self) -> None:
        buffer = self._make_buffer("paragraph")
        buffer.place_cursor(buffer.get_iter_at_offset(9))
        insert_block_line(buffer, text="----\ncode\n----")
        self.assertEqual(
            buffer_text(buffer),
            "paragraph\n----\ncode\n----\n",
        )

    def test_whitespace_only_line_counts_as_empty(self) -> None:
        # A line that contains only spaces / tabs should still be
        # treated as empty for the purposes of the leading-newline
        # rule, matching the React reference's ``trim() === ""``.
        buffer = self._make_buffer("   ")
        buffer.place_cursor(buffer.get_iter_at_offset(3))
        insert_block_line(buffer, text="== Heading")
        # No leading newline; the prefix was whitespace-only.
        self.assertEqual(buffer_text(buffer), "   == Heading\n")


class BufferTextTests(unittest.TestCase):
    def test_empty_buffer_yields_empty_string(self) -> None:
        buffer = Gtk.TextBuffer.new(None)
        self.assertEqual(buffer_text(buffer), "")

    def test_returns_full_buffer_contents(self) -> None:
        buffer = Gtk.TextBuffer.new(None)
        buffer.set_text("alpha\nbeta")
        self.assertEqual(buffer_text(buffer), "alpha\nbeta")


# ---------------------------------------------------------------------------
# Language file loading
# ---------------------------------------------------------------------------


class BundledLanguageDirTests(unittest.TestCase):
    def test_directory_contains_the_language_file(self) -> None:
        # The directory is computed at runtime from the asciidoc
        # package's ``__file__``. The bundled .lang must be sitting
        # right next to it for GtkSource to discover it.
        directory = _bundled_language_dir()
        self.assertTrue(
            (directory / LANGUAGE_FILE_NAME).is_file(),
            f"missing {LANGUAGE_FILE_NAME} in {directory}",
        )


@unittest.skipUnless(_display_available(), "no GDK display")
class LoadAsciidocLanguageTests(unittest.TestCase):
    def test_returns_a_language_with_expected_id(self) -> None:
        language = load_asciidoc_language()
        self.assertIsNotNone(language)
        assert language is not None
        self.assertEqual(language.get_id(), LANGUAGE_ID)

    def test_language_manager_is_cached_across_calls(self) -> None:
        # Calling twice yields the same Language instance because
        # the underlying LanguageManager is the cached singleton.
        # If it weren't, every call would build a new manager (and
        # eventually trip the GtkSourceView "language manager
        # finalized" warning that the cache exists to avoid).
        first = load_asciidoc_language()
        second = load_asciidoc_language()
        self.assertIs(first, second)


# ---------------------------------------------------------------------------
# NoteEditor — debounced save, selection handling, smoke tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteEditorConstructionTests(unittest.TestCase):
    def _make_editor(self) -> tuple[
        NoteEditor,
        _FakeNoteRepository,
        AppState,
        _FakeTimeoutBackend,
    ]:
        repo = _FakeNoteRepository()
        repo.notes["n1"] = _make_note("n1")
        state = AppState()
        controller = NoteController(
            repository=repo,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        backend = _FakeTimeoutBackend()
        editor = NoteEditor(
            note_repository=repo,
            note_controller=controller,
            app_state=state,
            schedule_timeout=backend.schedule,
            cancel_timeout=backend.cancel,
        )
        return editor, repo, state, backend

    def test_constructs_with_no_selection(self) -> None:
        editor, repo, _, _ = self._make_editor()
        self.assertIsInstance(editor, Gtk.Box)
        # Nothing was selected, so no note is being edited.
        self.assertIsNone(editor.current_note_id)
        # The repository was not consulted.
        self.assertEqual(len(repo.update_calls), 0)

    def test_constructs_with_pre_existing_selection_loads_buffer(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["n1"] = _make_note("n1", source="= Pre\n\ntext.\n")
        state = AppState()
        # Select BEFORE constructing the editor — the initial load
        # path must pick this up.
        state.set_selected_note_id("n1")
        controller = NoteController(
            repository=repo,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        backend = _FakeTimeoutBackend()
        editor = NoteEditor(
            note_repository=repo,
            note_controller=controller,
            app_state=state,
            schedule_timeout=backend.schedule,
            cancel_timeout=backend.cancel,
        )
        self.assertEqual(editor.current_note_id, "n1")
        # The buffer holds the note's source text.
        buffer = editor._buffer
        self.assertEqual(buffer_text(buffer), "= Pre\n\ntext.\n")
        # The programmatic load did NOT schedule a save — the
        # ``_loading_note`` guard suppressed the buffer's
        # ``changed`` signal handler.
        self.assertEqual(backend.schedule_calls, [])

    def test_uses_gtksource_view_with_line_numbers(self) -> None:
        # Walk down to the editor widget the user sees and verify
        # it's a GtkSource.View — not a plain Gtk.TextView. Line
        # numbers and current-line highlight are the GtkSourceView
        # features the design's gutter requires.
        editor, _, _, _ = self._make_editor()
        scrolled = editor.get_last_child()
        assert isinstance(scrolled, Gtk.ScrolledWindow)
        source_view = scrolled.get_child()
        self.assertIsInstance(source_view, GtkSource.View)
        assert isinstance(source_view, GtkSource.View)
        self.assertTrue(source_view.get_show_line_numbers())
        self.assertTrue(source_view.get_highlight_current_line())
        self.assertTrue(source_view.get_monospace())

    def test_buffer_has_asciidoc_language_attached(self) -> None:
        editor, _, _, _ = self._make_editor()
        buffer = editor._buffer
        self.assertIsInstance(buffer, GtkSource.Buffer)
        assert isinstance(buffer, GtkSource.Buffer)
        language = buffer.get_language()
        self.assertIsNotNone(language)
        assert language is not None
        self.assertEqual(language.get_id(), LANGUAGE_ID)
        self.assertTrue(buffer.get_highlight_syntax())


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteEditorAutosaveTests(unittest.TestCase):
    """The debounce contract: every change reschedules the timer; the
    save fires only once on the timer's expiry."""

    def _editor_with_selection(
        self,
        *,
        note_id: str = "n1",
        source: str = "= Hi\n\nbody.\n",
    ) -> tuple[
        NoteEditor,
        _FakeNoteRepository,
        AppState,
        _FakeTimeoutBackend,
    ]:
        repo = _FakeNoteRepository()
        repo.notes[note_id] = _make_note(note_id, source=source)
        state = AppState()
        state.set_selected_note_id(note_id)
        controller = NoteController(
            repository=repo,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        backend = _FakeTimeoutBackend()
        editor = NoteEditor(
            note_repository=repo,
            note_controller=controller,
            app_state=state,
            schedule_timeout=backend.schedule,
            cancel_timeout=backend.cancel,
        )
        return editor, repo, state, backend

    def test_buffer_change_schedules_save_at_300_ms(self) -> None:
        editor, _, _, backend = self._editor_with_selection()
        # Simulate the user typing — programmatic insert behaves
        # identically to keypress-driven insert from the buffer's
        # signal-emission point of view.
        editor._buffer.insert(
            editor._buffer.get_end_iter(),
            "extra",
        )
        self.assertEqual(len(backend.schedule_calls), 1)
        delay_ms, _callback = backend.schedule_calls[0]
        self.assertEqual(delay_ms, AUTOSAVE_DEBOUNCE_MS)

    def test_subsequent_changes_cancel_and_reschedule(self) -> None:
        editor, _, _, backend = self._editor_with_selection()
        # Two typing events within the debounce window.
        editor._buffer.insert(editor._buffer.get_end_iter(), "a")
        editor._buffer.insert(editor._buffer.get_end_iter(), "b")
        # Two schedules, exactly one cancel between them.
        self.assertEqual(len(backend.schedule_calls), 2)
        self.assertEqual(len(backend.cancel_calls), 1)
        # And only one timer is currently pending.
        self.assertEqual(backend.pending_count, 1)

    def test_timer_fires_invokes_repository_update_source(self) -> None:
        editor, repo, _, backend = self._editor_with_selection()
        editor._buffer.insert(editor._buffer.get_end_iter(), "z")
        backend.fire_pending()
        # Exactly one save, against the right note id, with the
        # buffer's current contents.
        self.assertEqual(len(repo.update_calls), 1)
        note_id, source, _ = repo.update_calls[0]
        self.assertEqual(note_id, "n1")
        self.assertEqual(source, "= Hi\n\nbody.\nz")

    def test_timer_fire_clears_pending_handle(self) -> None:
        editor, _, _, backend = self._editor_with_selection()
        editor._buffer.insert(editor._buffer.get_end_iter(), "z")
        # Pending before the fire …
        self.assertIsNotNone(editor._pending_save_handle)
        backend.fire_pending()
        # … and cleared afterwards. A subsequent change would
        # therefore schedule a fresh timer rather than try to
        # cancel the one that just fired.
        self.assertIsNone(editor._pending_save_handle)

    def test_loading_note_does_not_schedule_save(self) -> None:
        # Selection change triggers a programmatic buffer load; the
        # editor's ``_loading_note`` guard must keep the resulting
        # buffer-changed signal from queueing a redundant save.
        editor, _, state, backend = self._editor_with_selection()
        # The editor reference must outlive the selection change —
        # if it were collected, its ``selected-note-changed`` signal
        # connection would die with it and the load path under test
        # would never run. Asserting against a property keeps the
        # reference alive for pylint as well as the GC.
        self.assertIsNotNone(editor.current_note_id)
        # Reset bookkeeping — the construction-time load has already
        # been verified above.
        backend.schedule_calls.clear()
        backend.cancel_calls.clear()
        # Switch to a new note. The editor's repo doesn't have it,
        # so the buffer gets cleared (an unknown selection is
        # permitted — see _load_selected_note).
        state.set_selected_note_id("does-not-exist")
        self.assertEqual(backend.schedule_calls, [])

    def test_flush_pending_save_runs_synchronously(self) -> None:
        editor, repo, _, backend = self._editor_with_selection()
        editor._buffer.insert(editor._buffer.get_end_iter(), "x")
        self.assertEqual(backend.pending_count, 1)
        # Force the flush before the timer would naturally fire.
        editor.flush_pending_save()
        # Save happened, timer was cancelled, handle cleared.
        self.assertEqual(len(repo.update_calls), 1)
        self.assertEqual(len(backend.cancel_calls), 1)
        self.assertIsNone(editor._pending_save_handle)

    def test_flush_pending_save_is_noop_when_nothing_pending(self) -> None:
        editor, repo, _, backend = self._editor_with_selection()
        editor.flush_pending_save()
        self.assertEqual(len(repo.update_calls), 0)
        self.assertEqual(len(backend.cancel_calls), 0)

    def test_selection_change_flushes_pending_save_for_old_note(self) -> None:
        # The plan's lossless-switch invariant: switching notes
        # while a save is pending must not lose those edits.
        editor, repo, state, backend = self._editor_with_selection()
        repo.notes["n2"] = _make_note("n2", source="= Other\n")
        # User types in n1 …
        editor._buffer.insert(editor._buffer.get_end_iter(), "!")
        self.assertEqual(backend.pending_count, 1)
        # … and immediately switches to n2.
        state.set_selected_note_id("n2")
        # The save fired BEFORE the buffer was overwritten — the
        # captured source is n1's buffer contents, not n2's.
        self.assertEqual(len(repo.update_calls), 1)
        saved_id, saved_source, _ = repo.update_calls[0]
        self.assertEqual(saved_id, "n1")
        self.assertEqual(saved_source, "= Hi\n\nbody.\n!")
        # The pending timer was cancelled.
        self.assertEqual(len(backend.cancel_calls), 1)
        # And the editor is now on n2.
        self.assertEqual(editor.current_note_id, "n2")

    def test_storage_error_is_swallowed_in_timer_callback(self) -> None:
        # The controller's ``capturing_storage_errors`` emits a
        # toast and re-raises. The editor catches that re-raise so
        # the GLib timer callback does not propagate the exception
        # into the main loop. The user-visible toast still fires
        # via the controller's signal — we are not silently
        # dropping the error, only declining to double-report.
        repo = _RaisingNoteRepository()
        repo.notes["n1"] = _make_note("n1")
        state = AppState()
        state.set_selected_note_id("n1")
        controller = NoteController(
            repository=repo,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        toasts: list[str] = []
        controller.connect(
            "storage-error",
            lambda _c, message: toasts.append(message),
        )
        backend = _FakeTimeoutBackend()
        editor = NoteEditor(
            note_repository=repo,
            note_controller=controller,
            app_state=state,
            schedule_timeout=backend.schedule,
            cancel_timeout=backend.cancel,
        )
        editor._buffer.insert(editor._buffer.get_end_iter(), "x")

        # The timer fire must NOT raise.
        try:
            backend.fire_pending()
        except sqlite3.DatabaseError:  # pragma: no cover — failure path
            self.fail("auto-save timer leaked a sqlite3.DatabaseError")

        # The repo *was* asked to update, the controller *did* emit
        # its toast — the editor only declined to re-raise.
        self.assertEqual(len(repo.update_calls), 1)
        self.assertEqual(len(toasts), 1)
        self.assertIn("save note", toasts[0])


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteEditorToolbarTests(unittest.TestCase):
    """Spot-checks that the toolbar buttons map to the right helpers.

    The pure helpers are exhaustively tested above; these tests verify
    that the editor's toolbar wires up at least one button per kind
    (wrap and insert) so a future refactor cannot accidentally leave
    a button unwired.
    """

    def _make_editor(self) -> NoteEditor:
        repo = _FakeNoteRepository()
        repo.notes["n1"] = _make_note("n1", source="")
        state = AppState()
        state.set_selected_note_id("n1")
        controller = NoteController(
            repository=repo,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        backend = _FakeTimeoutBackend()
        return NoteEditor(
            note_repository=repo,
            note_controller=controller,
            app_state=state,
            schedule_timeout=backend.schedule,
            cancel_timeout=backend.cancel,
        )

    def _toolbar_buttons(self, editor: NoteEditor) -> list[Gtk.Button]:
        toolbar = editor.get_first_child()
        assert isinstance(toolbar, Gtk.Box)
        buttons: list[Gtk.Button] = []
        child = toolbar.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                buttons.append(child)
            child = child.get_next_sibling()
        return buttons

    def test_toolbar_exposes_the_step_10_core_button_set(self) -> None:
        # Tooltips drive the assertion because button labels are
        # short ("H", "B", "I", …) and could collide. Tooltips are
        # the user-facing identification anyway.
        editor = self._make_editor()
        tooltips = {
            b.get_tooltip_text() for b in self._toolbar_buttons(editor)
        }
        self.assertEqual(
            tooltips,
            {
                "Heading",
                "Bold",
                "Italic",
                "Strikethrough",
                "Underline",
                "Bullet list",
                "Numbered list",
                "Code block",
                "Insert image macro",
            },
        )

    def test_clicking_bold_button_wraps_selection(self) -> None:
        editor = self._make_editor()
        # Type some content and select it.
        editor._buffer.set_text("hi")
        editor._buffer.select_range(
            editor._buffer.get_iter_at_offset(0),
            editor._buffer.get_iter_at_offset(2),
        )
        bold_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Bold"
        )
        bold_button.emit("clicked")
        self.assertEqual(buffer_text(editor._buffer), "*hi*")

    def test_clicking_heading_button_inserts_block_line(self) -> None:
        editor = self._make_editor()
        editor._buffer.set_text("")
        heading_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Heading"
        )
        heading_button.emit("clicked")
        self.assertEqual(buffer_text(editor._buffer), "== Heading\n")


if __name__ == "__main__":
    unittest.main()
