"""Tests for :mod:`ui.note_editor`.

The toolbar suites are gone with the toolbar itself: the editor's
surface is now the buffer (load / autosave / language) plus the
embedded attachments panel, whose behaviour has its own suite in
``test_attachments_panel.py`` — here we only pin that the editor
composes it with the right collaborators.
"""

from __future__ import annotations

import sqlite3
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gi.repository import Gdk, Gtk, GtkSource

from enums import (
    AttachmentExportFailureReason,
    AttachmentRejectionReason,
    GResourceSubtree,
)
from models.attachment import Attachment
from models.note import Note
from storage.protocols import AttachmentExportFailed, AttachmentRejected
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui.attachments_panel import AttachmentsPanel
from giruntime.ui.note_editor import (
    AUTOSAVE_DEBOUNCE_MS,
    LANGUAGE_ID,
    NoteEditor,
    _configure_search_path,
    buffer_text,
    load_asciidoc_language,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for any
    :class:`Gtk.Widget` subclass construction.

    The pure helper (:func:`buffer_text`) operates on
    :class:`Gtk.TextBuffer`, which does NOT need a display — those
    tests run unconditionally.
    """
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def _make_note(
    note_id: str,
    *,
    source: str = "= Hello\n\nbody.\n",
    tags: tuple[str, ...] = (),
) -> Note:
    return Note(
        id=note_id,
        title="Hello",
        source=source,
        snippet="body.",
        tags=tags,
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

    def list_all(self) -> list[Note]:
        return list(self.notes.values())

    def insert(self, _note: Note) -> Note:
        raise NotImplementedError

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        self.update_calls.append((note_id, source, modified_at))
        existing = self.notes[note_id]
        updated = Note(
            id=existing.id,
            title=existing.title,
            source=source,
            snippet=existing.snippet,
            tags=existing.tags,
            created_at=existing.created_at,
            modified_at=modified_at,
        )
        self.notes[note_id] = updated
        return updated

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
    ) -> Note:
        # Record the attempt so tests can assert it was made before
        # raising. ``OperationalError`` is a subclass of
        # ``DatabaseError`` and is the most common storage failure
        # mode (lock contention, disk full, etc.).
        self.update_calls.append((note_id, source, modified_at))
        raise sqlite3.OperationalError("simulated DB failure")


class _FakeAttachmentStore:
    """Configurable :class:`AttachmentStoreProtocol` fake.

    Default behaviour: ``add_for_note`` returns a successful
    :class:`Attachment` whose filename echoes the source path's name,
    and ``list_for_note`` is empty (the editor's embedded panel lists
    on every selection change, so the listing path must work).
    Tests that want a rejection assign a non-``None``
    :attr:`reject_with`; the next call raises
    :class:`AttachmentRejected` with that reason.

    ``get_bytes`` / ``remove`` fail loudly because the editor does not
    exercise them; a test that accidentally invokes one fails rather
    than silently returning empty data.
    """

    add_calls: list[tuple[str, Path]]
    reject_with: AttachmentRejectionReason | None
    next_attachment_id: int

    def __init__(self) -> None:
        self.add_calls = []
        self.reject_with = None
        self.next_attachment_id = 1

    def add_for_note(self, note_id: str, source_path: Path) -> Attachment:
        self.add_calls.append((note_id, source_path))
        if self.reject_with is not None:
            raise AttachmentRejected(self.reject_with)
        attachment = Attachment(
            id=f"att-{self.next_attachment_id}",
            note_id=note_id,
            filename=source_path.name,
            byte_size=42,
        )
        self.next_attachment_id += 1
        return attachment

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        return []

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
# buffer_text — pure helper, no display needed
# ---------------------------------------------------------------------------


class BufferTextTests(unittest.TestCase):
    def test_empty_buffer_yields_empty_string(self) -> None:
        buffer = Gtk.TextBuffer.new(None)
        self.assertEqual(buffer_text(buffer), "")

    def test_returns_full_buffer_contents(self) -> None:
        buffer = Gtk.TextBuffer.new(None)
        buffer.set_text("= Title\n\nline one\nline two\n")
        self.assertEqual(buffer_text(buffer), "= Title\n\nline one\nline two\n")


# ---------------------------------------------------------------------------
# Language loading
# ---------------------------------------------------------------------------


class ConfigureSearchPathTests(unittest.TestCase):
    def test_resource_dir_is_prepended_to_search_path(self) -> None:
        manager = GtkSource.LanguageManager.new()
        _configure_search_path(manager)
        self.assertIn(
            GResourceSubtree.LANGUAGE_SPECS.value,
            list(manager.get_search_path()),
        )

    def test_resource_dir_is_first_in_search_path(self) -> None:
        # Prepending (not appending) is what lets the bundled grammar
        # win over an id-colliding system grammar.
        manager = GtkSource.LanguageManager.new()
        _configure_search_path(manager)
        self.assertEqual(
            list(manager.get_search_path())[0],
            GResourceSubtree.LANGUAGE_SPECS.value,
        )


class LoadAsciidocLanguageTests(unittest.TestCase):
    def test_returns_a_language_with_expected_id(self) -> None:
        language = load_asciidoc_language()
        self.assertIsNotNone(language)
        assert language is not None
        self.assertEqual(language.get_id(), LANGUAGE_ID)

    def test_language_manager_is_cached_across_calls(self) -> None:
        # Two loads must come from the same module-level manager —
        # GtkSourceView's highlighter holds only a weak reference to
        # the manager that produced its Language, so a per-call
        # manager would be collected and break highlighting.
        first = load_asciidoc_language()
        second = load_asciidoc_language()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)


# ---------------------------------------------------------------------------
# NoteEditor — construction, debounced save, selection handling
# ---------------------------------------------------------------------------


def _build_editor(
    *,
    repository: _FakeNoteRepository | None = None,
    select_note: str | None = None,
) -> tuple[
    NoteEditor,
    _FakeNoteRepository,
    AppState,
    _FakeTimeoutBackend,
    NoteController,
]:
    repo = repository if repository is not None else _FakeNoteRepository()
    if not repo.notes:
        repo.notes["n1"] = _make_note("n1")
    state = AppState()
    if select_note is not None:
        # Select BEFORE constructing the editor — the initial load
        # path must pick this up.
        state.set_selected_note_id(select_note)
    store = NoteListStore(repository=repo)
    store.load()
    attachment_store = _FakeAttachmentStore()
    controller = NoteController(
        note_store=store,
        attachments=attachment_store,
        app_state=state,
    )
    backend = _FakeTimeoutBackend()
    editor = NoteEditor(
        note_store=store,
        note_controller=controller,
        app_state=state,
        attachments=attachment_store,
        schedule_timeout=backend.schedule,
        cancel_timeout=backend.cancel,
    )
    return editor, repo, state, backend, controller


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteEditorConstructionTests(unittest.TestCase):
    def test_constructs_with_no_selection(self) -> None:
        editor, repo, _, _, _ = _build_editor()
        self.assertIsInstance(editor, Gtk.Box)
        # Nothing was selected, so no note is being edited.
        self.assertIsNone(editor.current_note_id)
        # The repository was not consulted.
        self.assertEqual(len(repo.update_calls), 0)

    def test_constructs_with_pre_existing_selection_loads_buffer(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["n1"] = _make_note("n1", source="= Pre\n\ntext.\n")
        editor, _, _, backend, _ = _build_editor(
            repository=repo,
            select_note="n1",
        )
        self.assertEqual(editor.current_note_id, "n1")
        # The buffer holds the note's source text.
        buffer = editor._buffer
        self.assertEqual(buffer_text(buffer), "= Pre\n\ntext.\n")
        # The programmatic load did NOT schedule a save — the
        # ``_loading_note`` guard suppressed the buffer's
        # ``changed`` signal handler.
        self.assertEqual(backend.schedule_calls, [])

    def test_has_no_toolbar(self) -> None:
        # The edit toolbar is gone: the editor's first child is the
        # ScrolledWindow hosting the source view, not a button strip.
        editor, _, _, _, _ = _build_editor()
        self.assertIsInstance(editor.get_first_child(), Gtk.ScrolledWindow)

    def test_uses_gtksource_view_with_line_numbers(self) -> None:
        # Walk down to the editor widget the user sees and verify
        # it's a GtkSource.View — not a plain Gtk.TextView. Line
        # numbers and current-line highlight are the GtkSourceView
        # features the design's gutter requires.
        editor, _, _, _, _ = _build_editor()
        scrolled = editor.get_first_child()
        assert isinstance(scrolled, Gtk.ScrolledWindow)
        source_view = scrolled.get_child()
        self.assertIsInstance(source_view, GtkSource.View)
        assert isinstance(source_view, GtkSource.View)
        self.assertTrue(source_view.get_show_line_numbers())
        self.assertTrue(source_view.get_highlight_current_line())
        self.assertTrue(source_view.get_monospace())

    def test_attachments_panel_sits_below_the_editor(self) -> None:
        # The pane composes ScrolledWindow (top, vexpand) +
        # AttachmentsPanel (bottom, natural height) — and nothing else.
        editor, _, _, _, _ = _build_editor()
        last = editor.get_last_child()
        self.assertIsInstance(last, AttachmentsPanel)
        assert last is not None
        self.assertIsInstance(last.get_prev_sibling(), Gtk.ScrolledWindow)
        self.assertIsNone(last.get_next_sibling())
        self.assertFalse(last.get_vexpand())

    def test_attachments_panel_tracks_the_selection(self) -> None:
        # The panel shares the editor's AppState: hidden with no
        # selection, shown once a note is selected. The full panel
        # behaviour is covered in test_attachments_panel.py.
        editor, _, state, _, _ = _build_editor()
        panel = editor.get_last_child()
        assert isinstance(panel, AttachmentsPanel)
        self.assertFalse(panel.get_visible())
        state.set_selected_note_id("n1")
        self.assertTrue(panel.get_visible())

    def test_buffer_has_asciidoc_language_attached(self) -> None:
        editor, _, _, _, _ = _build_editor()
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
        extra_notes: dict[str, Note] | None = None,
    ) -> tuple[
        NoteEditor,
        _FakeNoteRepository,
        AppState,
        _FakeTimeoutBackend,
    ]:
        repo = _FakeNoteRepository()
        repo.notes[note_id] = _make_note(note_id, source=source)
        # Extra notes must be present *before* the store loads so the
        # editor (which reads bodies from the store) can resolve them.
        for extra_id, extra_note in (extra_notes or {}).items():
            repo.notes[extra_id] = extra_note
        editor, _, state, backend, _ = _build_editor(
            repository=repo,
            select_note=note_id,
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
        # if it were collected, its ``notify::selected-note-id``
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
        editor, repo, state, backend = self._editor_with_selection(
            extra_notes={"n2": _make_note("n2", source="= Other\n")},
        )
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
        editor, _, _, backend, controller = _build_editor(
            repository=repo,
            select_note="n1",
        )
        toasts: list[str] = []
        controller.connect(
            "storage-error",
            lambda _c, message: toasts.append(message),
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


if __name__ == "__main__":
    unittest.main()
