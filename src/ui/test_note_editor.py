"""Tests for :mod:`ui.note_editor`."""

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

from asciidoc.ast import (
    Admonition as _Admonition,
    Blockquote as _Blockquote,
    Link as _Link,
    Table as _Table,
)
from asciidoc.inline_parser import parse_inline as _parse_inline
from asciidoc.parser import parse as _parse_asciidoc
from controllers.app_state import AppState
from controllers.note_controller import NoteController
from enums import AdmonitionKind, AttachmentRejectionReason, MimeKind
from models.attachment import Attachment
from models.note import Note
from storage.protocols import AttachmentRejected
from ui.note_editor import (
    AUTOSAVE_DEBOUNCE_MS,
    LANGUAGE_ID,
    NoteEditor,
    _ADMONITION_TEMPLATE,
    _BLOCKQUOTE_TEMPLATE,
    _GRESOURCE_LANG_DIR,
    _LINK_TEMPLATE,
    _PLACEHOLDER_SELECTION_TEXT,
    _TABLE_TEMPLATE,
    _configure_search_path,
    _image_macro_for_filename,
    buffer_text,
    insert_block_line,
    insert_inline_text,
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
            source=source,
            snippet=existing.snippet,
            tags=existing.tags,
            created_at=existing.created_at,
            modified_at=modified_at,
        )

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return ()


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
    """Configurable :class:`AttachmentStoreProtocol` fake.

    Default behaviour: ``add_for_note`` returns a successful
    :class:`Attachment` whose filename echoes the source path's name.
    Tests that want a rejection assign a non-``None``
    :attr:`reject_with`; the next call raises
    :class:`AttachmentRejected` with that reason.

    Other methods are implemented as ``raise NotImplementedError``
    because the editor does not exercise them; a test that
    accidentally invokes one fails loudly rather than silently
    returning empty data.
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
            mime_type=MimeKind.PNG,
        )
        self.next_attachment_id += 1
        return attachment

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        raise NotImplementedError

    def count_for_note(self, _note_id: str) -> int:
        return 0

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError


class _FakeFileDialogOpener:
    """Synchronous stand-in for :data:`FileDialogOpener`.

    The real opener is asynchronous (:class:`Gtk.FileDialog.open`
    schedules a callback). The fake captures the most recent
    callback and result-receiver so tests can drive the post-pick
    code path explicitly.

    To simulate a successful pick, call :meth:`deliver` with a
    :class:`Path`. To simulate a cancellation, call
    :meth:`deliver(None)`. Until :meth:`deliver` is called the
    editor's image-button click is "in flight" — the buffer is
    untouched, the controller has not been called, and the test
    can assert on that intermediate state.
    """

    open_calls: list[Gtk.Widget]
    pending_callback: Callable[[Path | None], None] | None

    def __init__(self) -> None:
        self.open_calls = []
        self.pending_callback = None

    def __call__(
        self,
        parent: Gtk.Widget,
        on_result: Callable[[Path | None], None],
    ) -> None:
        self.open_calls.append(parent)
        self.pending_callback = on_result

    def deliver(self, path: Path | None) -> None:
        callback = self.pending_callback
        if callback is None:
            raise AssertionError(
                "FakeFileDialogOpener.deliver() called with no pending callback"
            )
        self.pending_callback = None
        callback(path)


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
# insert_inline_text — pure helper for inline-template inserts
# ---------------------------------------------------------------------------


class InsertInlineTextTests(unittest.TestCase):
    """Pure helper for inline content (no leading newline behaviour).

    Operates on a vanilla :class:`Gtk.TextBuffer` — no display required.
    """

    def _make_buffer(self, text: str) -> Gtk.TextBuffer:
        buffer = Gtk.TextBuffer.new(None)
        buffer.set_text(text)
        return buffer

    def test_inserts_at_cursor_with_no_newline(self) -> None:
        buffer = self._make_buffer("Visit  today")
        # Cursor between the two spaces in "Visit  today".
        buffer.place_cursor(buffer.get_iter_at_offset(6))
        insert_inline_text(buffer, text="link:https://x[here]")
        self.assertEqual(
            buffer_text(buffer),
            "Visit link:https://x[here] today",
        )

    def test_no_select_within_leaves_cursor_at_end_of_insert(self) -> None:
        buffer = self._make_buffer("")
        insert_inline_text(buffer, text="link:https://x[t]")
        cursor_iter = buffer.get_iter_at_mark(buffer.get_insert())
        self.assertEqual(
            cursor_iter.get_offset(),
            len("link:https://x[t]"),
        )

    def test_select_within_highlights_substring_post_insert(self) -> None:
        buffer = self._make_buffer("")
        # Select "https://example.com" inside "link:https://example.com[t]"
        # — offsets 5..24 of the inserted text.
        insert_inline_text(
            buffer,
            text="link:https://example.com[t]",
            select_within=(5, 24),
        )
        bounds = buffer.get_selection_bounds()
        self.assertIsNotNone(bounds)
        assert bounds is not None and len(bounds) == 2
        start, end = bounds
        self.assertEqual(
            buffer.get_text(start, end, False),
            "https://example.com",
        )

    def test_select_within_offsets_are_relative_to_insert(self) -> None:
        # If the cursor is mid-buffer, ``select_within`` offsets must
        # still be interpreted relative to the start of the inserted
        # text, not the start of the buffer.
        buffer = self._make_buffer("prefix ")
        buffer.place_cursor(buffer.get_iter_at_offset(7))
        insert_inline_text(
            buffer,
            text="ABCDE",
            select_within=(1, 4),
        )
        bounds = buffer.get_selection_bounds()
        assert bounds is not None and len(bounds) == 2
        start, end = bounds
        # Selection is "BCD" — i.e. offsets 1..4 of the inserted text,
        # which is offsets 8..11 of the full buffer.
        self.assertEqual(buffer.get_text(start, end, False), "BCD")
        self.assertEqual(start.get_offset(), 8)
        self.assertEqual(end.get_offset(), 11)

    def test_inline_insert_does_not_split_existing_line(self) -> None:
        # Mid-line insert keeps surrounding text on the same line —
        # this is the whole reason ``insert_inline_text`` exists
        # rather than reusing :func:`insert_block_line`.
        buffer = self._make_buffer("see https://x today")
        # Cursor at end ("today" is 5 chars).
        buffer.place_cursor(buffer.get_end_iter())
        insert_inline_text(buffer, text="!")
        self.assertEqual(
            buffer_text(buffer),
            "see https://x today!",
        )

    def test_undo_grouping_is_one_step(self) -> None:
        # The whole insert lives inside one ``begin_user_action`` /
        # ``end_user_action`` envelope so a single Ctrl-Z undoes it
        # all. Verify by checking ``get_can_undo`` is True after the
        # insert, then triggering a single undo and confirming the
        # buffer is empty again.
        buffer = GtkSource.Buffer.new(None)
        buffer.set_max_undo_levels(10)
        insert_inline_text(buffer, text="link:https://x[t]")
        self.assertTrue(buffer.get_can_undo())
        buffer.undo()
        self.assertEqual(buffer_text(buffer), "")


# ---------------------------------------------------------------------------
# Grammar loading (compiled GResource)
# ---------------------------------------------------------------------------


class ConfigureSearchPathTests(unittest.TestCase):
    def test_resource_dir_is_prepended_to_search_path(self) -> None:
        # The single grammar load path: _configure_search_path registers
        # the compiled GResource and prepends its ``resource:///`` grammar
        # directory to the manager's search path. Building a fresh manager
        # (not the cached singleton) keeps the assertion local and free of
        # cross-test ordering effects. This exercises the real loader — the
        # compiled ``folio.gresource`` must exist, which ``make test``
        # guarantees by depending on ``$(GRES)``.
        manager = GtkSource.LanguageManager.new()
        _configure_search_path(manager)
        self.assertIn(_GRESOURCE_LANG_DIR, manager.get_search_path())

    def test_resource_dir_is_first_in_search_path(self) -> None:
        # It must be *prepended* (not appended) so the bundled grammar
        # wins over any system-installed language of the same id.
        manager = GtkSource.LanguageManager.new()
        _configure_search_path(manager)
        self.assertEqual(manager.get_search_path()[0], _GRESOURCE_LANG_DIR)


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

    def test_toolbar_exposes_the_step_15_core_button_set(self) -> None:
        # Tooltips drive the assertion because button labels are
        # short ("H", "B", "I", …) and could collide. Tooltips are
        # the user-facing identification anyway. The image button's
        # tooltip changed from "Insert image macro" (step 10
        # placeholder) to "Insert image" (step 11 file dialog).
        # Step 13 added Monospace and Link to the inline group.
        # Step 14 added Table to the blocks group.
        # Step 15 adds Admonition and Blockquote at the end of
        # the blocks group.
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
                "Monospace",
                "Link",
                "Bullet list",
                "Numbered list",
                "Code block",
                "Insert image",
                "Table",
                "Admonition",
                "Blockquote",
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

    def test_clicking_monospace_button_wraps_selection_in_backticks(self) -> None:
        # Step 13: the monospace toolbar button is a wrap-button with
        # ``\``` as both delimiters. With a selection, the wrap is
        # symmetric just like Bold/Italic.
        editor = self._make_editor()
        editor._buffer.set_text("code")
        editor._buffer.select_range(
            editor._buffer.get_iter_at_offset(0),
            editor._buffer.get_iter_at_offset(4),
        )
        monospace_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Monospace"
        )
        monospace_button.emit("clicked")
        self.assertEqual(buffer_text(editor._buffer), "`code`")

    def test_clicking_monospace_button_with_no_selection_inserts_placeholder(
        self,
    ) -> None:
        # The wrap-button placeholder behaviour applies to monospace
        # too — clicking the button on an empty cursor yields
        # ``\`text\``` with the placeholder selected for immediate
        # overtype.
        editor = self._make_editor()
        editor._buffer.set_text("")
        monospace_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Monospace"
        )
        monospace_button.emit("clicked")
        self.assertEqual(
            buffer_text(editor._buffer),
            f"`{_PLACEHOLDER_SELECTION_TEXT}`",
        )

    def test_clicking_link_button_inserts_macro_template_inline(self) -> None:
        # Step 13: the link button drops a syntactically-valid
        # ``link:URL[label]`` template at the cursor. The macro is
        # inline — no leading newline added — so a click mid-paragraph
        # leaves the user typing in the same paragraph they were in.
        editor = self._make_editor()
        editor._buffer.set_text("see  for more")
        # Cursor between "see " and " for more".
        editor._buffer.place_cursor(editor._buffer.get_iter_at_offset(4))
        link_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Link"
        )
        link_button.emit("clicked")
        self.assertEqual(
            buffer_text(editor._buffer),
            "see link:https://example.com[link text] for more",
        )

    def test_clicking_link_button_preselects_url_placeholder(self) -> None:
        # The URL portion of the inserted template is left selected
        # so the user's first keystroke replaces it with a real URL.
        editor = self._make_editor()
        editor._buffer.set_text("")
        link_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Link"
        )
        link_button.emit("clicked")
        bounds = editor._buffer.get_selection_bounds()
        self.assertIsNotNone(bounds)
        assert bounds is not None and len(bounds) == 2
        start, end = bounds
        self.assertEqual(
            editor._buffer.get_text(start, end, False),
            "https://example.com",
        )

    def test_link_template_parses_cleanly_through_inline_parser(self) -> None:
        # Defence-in-depth: confirm the template the button inserts
        # actually parses without raising. If a future tweak to the
        # placeholder makes it malformed, this test fails immediately
        # — much better than a user clicking the button and getting
        # a parse error in the rendered view.
        result = _parse_inline(_LINK_TEMPLATE, line=1)
        # And it parses to exactly one Link node — not a Text fallback.
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], _Link)

    # -- table button (step 14) -----------------------------------------

    def test_clicking_table_button_inserts_template(self) -> None:
        # Step 14: the table toolbar button drops a 2-column, 3-row
        # template at the cursor. The template uses the block-line
        # insertion helper, so a click on an empty buffer produces the
        # template with a leading newline only when the cursor is not
        # at the start of a line.
        editor = self._make_editor()
        editor._buffer.set_text("")
        table_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Table"
        )
        table_button.emit("clicked")
        # The buffer ends with a trailing newline (block-line insertion
        # invariant — every block line ends with one).
        self.assertEqual(
            buffer_text(editor._buffer),
            _TABLE_TEMPLATE + "\n",
        )

    def test_table_template_parses_to_a_table(self) -> None:
        # Defence-in-depth: the template the button inserts must parse
        # cleanly into a :class:`Table` node so the rendered view
        # doesn't immediately show an error panel after the click.
        doc = _parse_asciidoc(_TABLE_TEMPLATE + "\n")
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], _Table)
        # The template has exactly the shape we documented in the
        # editor module — 2 columns, 3 rows total (header + 2 data).
        table = doc.blocks[0]
        assert isinstance(table, _Table)
        self.assertEqual(len(table.rows), 3)
        self.assertEqual(len(table.rows[0].cells), 2)

    # -- admonition button (step 15) ------------------------------------

    def test_clicking_admonition_button_inserts_template(self) -> None:
        # Step 15: the admonition toolbar button drops a [NOTE] block
        # template at the cursor.
        editor = self._make_editor()
        editor._buffer.set_text("")
        admonition_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Admonition"
        )
        admonition_button.emit("clicked")
        self.assertEqual(
            buffer_text(editor._buffer),
            _ADMONITION_TEMPLATE + "\n",
        )

    def test_admonition_template_parses_to_an_admonition(self) -> None:
        # Defence-in-depth: the template must parse cleanly so the
        # rendered view doesn't immediately show an error panel.
        doc = _parse_asciidoc(_ADMONITION_TEMPLATE + "\n")
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], _Admonition)
        admonition = doc.blocks[0]
        assert isinstance(admonition, _Admonition)
        # The template uses NOTE — the safest default kind.
        self.assertEqual(admonition.kind, AdmonitionKind.NOTE)
        # Exactly one body paragraph.
        self.assertEqual(len(admonition.blocks), 1)

    # -- blockquote button (step 15) ------------------------------------

    def test_clicking_blockquote_button_inserts_template(self) -> None:
        editor = self._make_editor()
        editor._buffer.set_text("")
        blockquote_button = next(
            b for b in self._toolbar_buttons(editor)
            if b.get_tooltip_text() == "Blockquote"
        )
        blockquote_button.emit("clicked")
        self.assertEqual(
            buffer_text(editor._buffer),
            _BLOCKQUOTE_TEMPLATE + "\n",
        )

    def test_blockquote_template_parses_to_a_blockquote(self) -> None:
        # Defence-in-depth: the template must parse cleanly. The
        # placeholder Author/Source values are non-empty, so the
        # parser's BAD_BLOCKQUOTE_DIRECTIVE check passes.
        doc = _parse_asciidoc(_BLOCKQUOTE_TEMPLATE + "\n")
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], _Blockquote)
        quote = doc.blocks[0]
        assert isinstance(quote, _Blockquote)
        self.assertEqual(quote.author, "Author")
        self.assertEqual(quote.source, "Source")
        self.assertEqual(len(quote.blocks), 1)


# ---------------------------------------------------------------------------
# _image_macro_for_filename
# ---------------------------------------------------------------------------


class ImageMacroForFilenameTests(unittest.TestCase):
    """Pure helper — no fixtures."""

    def test_basic_filename(self) -> None:
        self.assertEqual(
            _image_macro_for_filename("photo.png"),
            "image::photo.png[]",
        )

    def test_filename_with_spaces(self) -> None:
        # AsciiDoc accepts spaces in image filenames; the macro syntax
        # does not require quoting. The renderer's parser pins this
        # behaviour.
        self.assertEqual(
            _image_macro_for_filename("my photo.jpg"),
            "image::my photo.jpg[]",
        )

    def test_filename_with_unicode(self) -> None:
        self.assertEqual(
            _image_macro_for_filename("café.png"),
            "image::café.png[]",
        )


# ---------------------------------------------------------------------------
# Image button — file-dialog flow
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ImageButtonDialogFlowTests(unittest.TestCase):
    """End-to-end exercise of the editor's image button.

    Each test follows the same shape: build an editor wired to a
    :class:`_FakeAttachmentStore` and a :class:`_FakeFileDialogOpener`,
    click the Image button, then ``.deliver(...)`` a result and
    assert on the resulting buffer / store state.
    """

    def _build_editor(
        self,
        *,
        select_note: bool = True,
    ) -> tuple[
        NoteEditor,
        _FakeNoteRepository,
        _FakeAttachmentStore,
        _FakeFileDialogOpener,
        NoteController,
    ]:
        repo = _FakeNoteRepository()
        repo.notes["n1"] = _make_note("n1", source="")
        store = _FakeAttachmentStore()
        state = AppState()
        if select_note:
            state.set_selected_note_id("n1")
        controller = NoteController(
            repository=repo,
            attachments=store,
            app_state=state,
        )
        backend = _FakeTimeoutBackend()
        opener = _FakeFileDialogOpener()
        editor = NoteEditor(
            note_repository=repo,
            note_controller=controller,
            app_state=state,
            schedule_timeout=backend.schedule,
            cancel_timeout=backend.cancel,
            file_dialog_opener=opener,
        )
        return editor, repo, store, opener, controller

    def _image_button(self, editor: NoteEditor) -> Gtk.Button:
        toolbar = editor.get_first_child()
        assert isinstance(toolbar, Gtk.Box)
        child = toolbar.get_first_child()
        while child is not None:
            if (
                isinstance(child, Gtk.Button)
                and child.get_tooltip_text() == "Insert image"
            ):
                return child
            child = child.get_next_sibling()
        raise AssertionError("no image button on the toolbar")

    def test_clicking_image_button_opens_the_dialog(self) -> None:
        editor, _repo, _store, opener, _controller = self._build_editor()
        self._image_button(editor).emit("clicked")
        self.assertEqual(len(opener.open_calls), 1)
        # The parent passed to the opener is the editor itself —
        # the default opener walks ``get_root()`` from there to find
        # the window. At construction time before ``set_visible``
        # the editor has no root window; that's fine, the dialog
        # falls through to a None parent.
        self.assertIs(opener.open_calls[0], editor)

    def test_successful_pick_inserts_image_macro(self) -> None:
        editor, _repo, store, opener, _controller = self._build_editor()
        self._image_button(editor).emit("clicked")

        opener.deliver(Path("/tmp/photo.png"))

        # The store recorded the attempt, returned a fresh attachment.
        self.assertEqual(store.add_calls, [("n1", Path("/tmp/photo.png"))])
        # The buffer now contains the image macro for that filename.
        self.assertEqual(
            buffer_text(editor._buffer),
            "image::photo.png[]\n",
        )

    def test_macro_uses_attachments_filename_not_full_path(self) -> None:
        # The macro references the *filename*, not the full path —
        # that's the only stable identifier for the in-DB attachment.
        editor, _repo, _store, opener, _controller = self._build_editor()
        self._image_button(editor).emit("clicked")

        opener.deliver(Path("/some/nested/dir/holiday.jpg"))

        self.assertIn("image::holiday.jpg[]", buffer_text(editor._buffer))
        self.assertNotIn("/some/nested/dir", buffer_text(editor._buffer))

    def test_cancelled_pick_inserts_nothing(self) -> None:
        editor, _repo, store, opener, _controller = self._build_editor()
        # Pre-load some content so we can verify it's untouched.
        editor._buffer.set_text("existing content\n")
        self._image_button(editor).emit("clicked")

        opener.deliver(None)  # user cancelled

        # Controller never invoked, buffer untouched.
        self.assertEqual(store.add_calls, [])
        self.assertEqual(buffer_text(editor._buffer), "existing content\n")

    def test_rejected_attachment_inserts_nothing(self) -> None:
        editor, _repo, store, opener, controller = self._build_editor()
        # Capture the rejection signal so we can assert it fires.
        rejection_calls: list[AttachmentRejectionReason] = []
        controller.connect(
            "attachment-rejected",
            lambda _c, reason: rejection_calls.append(reason),
        )
        store.reject_with = AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT

        editor._buffer.set_text("")
        self._image_button(editor).emit("clicked")
        opener.deliver(Path("/tmp/huge.png"))

        # Store was consulted, but no macro was inserted.
        self.assertEqual(store.add_calls, [("n1", Path("/tmp/huge.png"))])
        self.assertEqual(buffer_text(editor._buffer), "")
        # The controller's typed-rejection signal fired with the
        # right reason — toast layer would surface this.
        self.assertEqual(
            rejection_calls,
            [AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT],
        )

    def test_each_rejection_reason_inserts_nothing(self) -> None:
        # Belt-and-braces: every reason in
        # AttachmentRejectionReason maps to "no macro inserted".
        for reason in AttachmentRejectionReason:
            with self.subTest(reason=reason.name):
                (
                    editor, _repo, store, opener, _controller,
                ) = self._build_editor()
                store.reject_with = reason
                editor._buffer.set_text("")
                self._image_button(editor).emit("clicked")
                opener.deliver(Path(f"/tmp/{reason.name}.png"))
                self.assertEqual(buffer_text(editor._buffer), "")

    def test_image_button_disabled_when_no_note_selected(self) -> None:
        editor, _repo, _store, _opener, _controller = self._build_editor(
            select_note=False,
        )
        # No selection at construction → button starts disabled.
        self.assertFalse(self._image_button(editor).get_sensitive())

    def test_image_button_enabled_after_selection(self) -> None:
        editor, _repo, _store, _opener, _controller = self._build_editor(
            select_note=False,
        )
        # Selecting a note enables the button.
        editor._app_state.set_selected_note_id("n1")
        self.assertTrue(self._image_button(editor).get_sensitive())

    def test_image_button_redisabled_when_selection_clears(self) -> None:
        editor, _repo, _store, _opener, _controller = self._build_editor()
        # Sanity: starts enabled.
        self.assertTrue(self._image_button(editor).get_sensitive())

        editor._app_state.set_selected_note_id(None)
        self.assertFalse(self._image_button(editor).get_sensitive())

    def test_click_with_no_selection_does_not_open_dialog(self) -> None:
        # Defensive: the button is disabled in normal use, but a
        # programmatic ``emit("clicked")`` bypasses the sensitivity
        # check. The handler must still bail rather than opening a
        # dialog with no note id to attach to.
        editor, _repo, _store, opener, _controller = self._build_editor(
            select_note=False,
        )
        self._image_button(editor).emit("clicked")
        self.assertEqual(opener.open_calls, [])

    def test_selection_clearing_during_dialog_drops_the_pick(self) -> None:
        # The dialog is asynchronous in production. Between opening
        # and the user picking, the selection might change to None
        # (e.g. the displayed note was deleted). The post-pick
        # handler must bail rather than calling controller methods
        # with a stale id.
        editor, _repo, store, opener, _controller = self._build_editor()
        self._image_button(editor).emit("clicked")
        # User clears selection while dialog is open.
        editor._app_state.set_selected_note_id(None)
        # Now the pick comes back.
        opener.deliver(Path("/tmp/photo.png"))

        # Nothing was added to the store; nothing inserted.
        self.assertEqual(store.add_calls, [])
        self.assertEqual(buffer_text(editor._buffer), "")

    def test_macro_insertion_triggers_autosave(self) -> None:
        # After a successful pick, the buffer change must trigger
        # the same auto-save flow as user typing — otherwise the
        # macro would be lost on the next selection change.
        repo = _FakeNoteRepository()
        repo.notes["n1"] = _make_note("n1", source="")
        store = _FakeAttachmentStore()
        state = AppState()
        state.set_selected_note_id("n1")
        controller = NoteController(
            repository=repo,
            attachments=store,
            app_state=state,
        )
        backend = _FakeTimeoutBackend()
        opener = _FakeFileDialogOpener()
        editor = NoteEditor(
            note_repository=repo,
            note_controller=controller,
            app_state=state,
            schedule_timeout=backend.schedule,
            cancel_timeout=backend.cancel,
            file_dialog_opener=opener,
        )

        # Emit click → deliver pick.
        toolbar = editor.get_first_child()
        assert isinstance(toolbar, Gtk.Box)
        button: Gtk.Button | None = None
        child = toolbar.get_first_child()
        while child is not None:
            if (
                isinstance(child, Gtk.Button)
                and child.get_tooltip_text() == "Insert image"
            ):
                button = child
                break
            child = child.get_next_sibling()
        assert button is not None
        button.emit("clicked")
        opener.deliver(Path("/tmp/x.png"))

        # An auto-save was scheduled by the buffer change.
        self.assertEqual(backend.pending_count, 1)
        backend.fire_pending()
        # The fired save included the inserted macro.
        self.assertEqual(len(repo.update_calls), 1)
        _, saved_source, _ = repo.update_calls[0]
        self.assertIn("image::x.png[]", saved_source)


if __name__ == "__main__":
    unittest.main()
