"""Tests for :mod:`ui.note_view`."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from gi.repository import GLib, Gtk

from config.defaults import (
    ARTICLE_BOTTOM_MARGIN_LINES,
    ARTICLE_END_GAP_LINES,
    ARTICLE_INNER_HPADDING_CHARS,
    ARTICLE_TOP_MARGIN_LINES,
    TARGET_CHARS_PER_LINE,
)
from enums import AttachmentExportFailureReason
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui import note_view as note_view_module
from giruntime.ui._dates import format_date_long
from giruntime.ui.article_container import (
    ArticleContainer,
    CharWidthMeasurer,
    LineHeightMeasurer,
)
from giruntime.ui.note_render.article_text_view import ArticleTextView
from giruntime.ui.note_render.palette import LIGHT_PALETTE
from giruntime.ui.note_render.tag_table import (
    TagName,
    build_wash_specs,
)
from giruntime.ui.note_render.tag_table import MONOSPACE_SCALE
from giruntime.ui.note_view import (
    make_cell_width_measurer,
    NoteView,
    _format_metadata_line,
    _placeholder_image_bytes,
    build_article_surface,
)
from giruntime.ui.test_display_guard import display_available
from models.attachment import Attachment
from models.note import Note
from storage.protocols import AttachmentExportFailed


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _fixed_measurer(value: int) -> CharWidthMeasurer:
    """Return a measurer callable that always reports ``value``.

    Used by the :class:`ArticleContainer` tests below so the two
    measurer slots (M-width and line-height) can be filled with a
    fixed integer without writing a lambda per call site. The return
    type is :data:`CharWidthMeasurer`; :data:`LineHeightMeasurer` has
    the same shape (``Callable[[], int]``) so the same factory plugs
    into either slot.
    """
    return lambda: value


def _stub_font_measurers_factory(
    *,
    char_w: int,
    line_h: int,
) -> Callable[[Gtk.TextView], tuple[CharWidthMeasurer, LineHeightMeasurer]]:
    """Build a stand-in for :func:`note_view._build_font_measurers`.

    The returned callable matches the production helper's signature
    so it can be monkey-patched in place, but ignores the live
    :class:`Gtk.TextView` and returns fixed-value measurers instead.
    Tests use this to drive :class:`NoteView` construction with
    deterministic font dimensions, side-stepping the real Pango
    layout.
    """

    def build(
        _text_view: Gtk.TextView,
    ) -> tuple[CharWidthMeasurer, LineHeightMeasurer]:
        return (_fixed_measurer(char_w), _fixed_measurer(line_h))

    return build


def _make_note(
    note_id: str,
    *,
    source: str = "= Hello\n\nbody.\n",
    tags: tuple[str, ...] = (),
    title: str | None = None,
) -> Note:
    """Build a deterministic :class:`Note` for tests."""
    return Note(
        id=note_id,
        title=title if title is not None else "Hello",
        source=source,
        snippet="body.",
        tags=tags,
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW + timedelta(seconds=1),
    )


def _settle_real_main_loop(timeout_ms: int = 400) -> None:
    """Run a real :class:`GLib.MainLoop`, quitting after ``timeout_ms``.

    Unlike the manually pumped ``MainContext.iteration`` loop most widget
    tests use, this drives the *real* main loop so the frame clock actually
    ticks and the window maps. The scrollbar bug only manifests after that
    tick (a pumped context never advances the frame clock), so the
    regression test must settle this way rather than pumping iterations.
    """
    loop = GLib.MainLoop()

    def _quit() -> bool:
        loop.quit()
        result: bool = GLib.SOURCE_REMOVE
        return result

    GLib.timeout_add(timeout_ms, _quit)
    loop.run()


class _FakeNoteRepository:
    """Minimal :class:`NoteRepositoryProtocol` impl for view tests.

    The methods the view tests exercise are filled in — the view's read
    path, plus the single ``update_source`` write the §2.2 hidden-pane
    deferral tests drive through :meth:`NoteListStore.update`. The rest
    raise :class:`NotImplementedError` so a future test that invokes one
    by accident fails loudly rather than silently.
    """

    notes: dict[str, Note]
    get_calls: list[str]

    def __init__(self) -> None:
        self.notes = {}
        self.get_calls = []

    # The single read path :class:`NoteView` uses.
    def get(self, note_id: str) -> Note:
        self.get_calls.append(note_id)
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
        """Return an edited copy of ``note_id`` carrying the new source.

        Implemented (unlike the other write stubs) because the §2.2
        hidden-pane re-render tests drive a real edit through
        :meth:`NoteListStore.update`, which persists via this method
        before splicing the fresh row back into the store.
        """
        existing = self.notes[note_id]
        edited = Note(
            id=existing.id,
            title=existing.title,
            source=source,
            snippet=existing.snippet,
            tags=existing.tags,
            created_at=existing.created_at,
            modified_at=modified_at,
        )
        self.notes[note_id] = edited
        return edited

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError


class _TrackingNoteListStore(NoteListStore):
    """A :class:`NoteListStore` that records :meth:`get_note` calls.

    Lets the view smoke-tests assert which note the view read, and in
    what order, now that body reads come from the store rather than the
    repository.
    """

    get_calls: list[str]

    def __init__(self, *, repository: _FakeNoteRepository) -> None:
        super().__init__(repository=repository)
        self.get_calls = []

    def get_note(self, note_id: str) -> Note:
        self.get_calls.append(note_id)
        return super().get_note(note_id)


def _build_tracking_store(repo: _FakeNoteRepository) -> _TrackingNoteListStore:
    """Build a loaded tracking store over ``repo``'s seeded notes."""
    store = _TrackingNoteListStore(repository=repo)
    store.load()
    return store


class _FakeAttachmentStore:
    """Minimal :class:`AttachmentStoreProtocol` impl for view tests.

    The store is dict-backed: :attr:`metadata` is a per-note list of
    :class:`Attachment` instances, :attr:`blobs` maps attachment id
    to bytes. Tests prime both directly (no ``add_for_note`` flow
    involved — that's the editor's concern) and assert on the
    ``calls_*`` lists to verify the resolver called the right methods
    in the right order.
    """

    metadata_by_note: dict[str, list[Attachment]]
    blobs: dict[str, bytes]
    list_calls: list[str]
    get_bytes_calls: list[str]

    def __init__(self) -> None:
        self.metadata_by_note = {}
        self.blobs = {}
        self.list_calls = []
        self.get_bytes_calls = []

    # --- helpers used by tests to seed the store ---

    def seed(self, note_id: str, filename: str, data: bytes) -> Attachment:
        attachment = Attachment(
            id=f"att-{len(self.blobs) + 1}",
            note_id=note_id,
            filename=filename,
            byte_size=len(data),
        )
        self.metadata_by_note.setdefault(note_id, []).append(attachment)
        self.blobs[attachment.id] = data
        return attachment

    # --- protocol surface ---

    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        raise NotImplementedError

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, note_id: str) -> list[Attachment]:
        self.list_calls.append(note_id)
        return list(self.metadata_by_note.get(note_id, ()))

    def get_bytes(self, attachment_id: str) -> bytes:
        self.get_bytes_calls.append(attachment_id)
        return self.blobs[attachment_id]

    def count_for_note(self, note_id: str) -> int:
        return len(self.metadata_by_note.get(note_id, ()))

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


class PlaceholderImageBytesTests(unittest.TestCase):
    def test_returns_empty_bytes(self) -> None:
        # Empty bytes are what trigger the renderer's
        # ``GLib.Error``-catching fallback to its small placeholder
        # widget. The contract here is intentionally minimal: any
        # input filename, the same empty-bytes output.
        self.assertEqual(_placeholder_image_bytes("anything.png"), b"")

    def test_filename_is_irrelevant(self) -> None:
        self.assertEqual(
            _placeholder_image_bytes("a.png"),
            _placeholder_image_bytes("b.jpg"),
        )


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewSmokeTests(unittest.TestCase):
    """Per §10: smoke-test only — ``NoteView`` constructs and reacts to
    selection changes through :class:`AppState`. No interaction tests.
    """

    def test_constructs_with_empty_state(self) -> None:
        repo = _FakeNoteRepository()
        store = _build_tracking_store(repo)
        app_state = AppState()
        view = NoteView(note_store=store, app_state=app_state)
        # No note is selected, so the store's ``get_note`` must not run.
        self.assertEqual(store.get_calls, [])
        # The widget exists and is a GTK box.
        self.assertIsInstance(view, Gtk.Box)

    def test_initial_render_pulls_currently_selected_note(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        store = _build_tracking_store(repo)
        app_state = AppState()
        # Selection is set *before* construction — the initial refresh
        # should pick this up.
        app_state.set_selected_note_id("note-A")

        NoteView(note_store=store, app_state=app_state)

        self.assertEqual(store.get_calls, ["note-A"])

    def test_selection_change_triggers_refresh(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        repo.notes["note-B"] = _make_note(
            "note-B", source="= Other\n\nelsewhere.\n",
        )
        store = _build_tracking_store(repo)
        app_state = AppState()
        NoteView(note_store=store, app_state=app_state)
        self.assertEqual(store.get_calls, [])

        app_state.set_selected_note_id("note-A")
        app_state.set_selected_note_id("note-B")

        # Both selections drove a refresh and therefore a store
        # ``get_note``. Order matches the selection sequence.
        self.assertEqual(store.get_calls, ["note-A", "note-B"])

    def test_clearing_selection_does_not_call_get(self) -> None:
        repo = _FakeNoteRepository()
        store = _build_tracking_store(repo)
        app_state = AppState()
        NoteView(note_store=store, app_state=app_state)

        app_state.set_selected_note_id(None)
        # Setting to None when it was already None is a no-op (the
        # signal is gated on a real change). Either way the store is
        # not consulted for a None selection.
        self.assertEqual(store.get_calls, [])

    def test_unknown_selected_note_id_is_handled_gracefully(self) -> None:
        # A stale id (e.g. note deleted in another window) must not
        # crash the view — it simply renders nothing.
        repo = _FakeNoteRepository()  # empty
        store = _build_tracking_store(repo)
        app_state = AppState()
        view = NoteView(note_store=store, app_state=app_state)

        app_state.set_selected_note_id("does-not-exist")

        # The store *was* asked, but the missing-id path swallowed the
        # KeyError and cleared the buffer.
        self.assertEqual(store.get_calls, ["does-not-exist"])
        # The widget is still alive and the underlying buffer is empty.
        text_view_buffer = _find_text_view_buffer(view)
        self.assertEqual(
            text_view_buffer.get_text(
                text_view_buffer.get_start_iter(),
                text_view_buffer.get_end_iter(),
                False,
            ),
            "",
        )


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewHiddenPaneRerenderTests(unittest.TestCase):
    """§2.2: the store-driven (``items-changed``) re-render is deferred
    while the pane is hidden and performed exactly once on reveal; the
    selection-change render is never deferred.

    Visibility is driven through the injected ``pane_is_visible``
    predicate so both branches are exercised without a real mapped
    window. The two reveal-via-``map`` cases present a real window so the
    ``map`` signal wiring is covered end to end.
    """

    _ORIGINAL_SOURCE = "= Original\n\noriginal body.\n"
    _EDITED_SOURCE = "= Edited\n\nedited body.\n"

    @staticmethod
    def _buffer_text(view: NoteView) -> str:
        buffer = _find_text_view_buffer(view)
        text: str = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            False,
        )
        return text

    def _view_on_note_a(
        self,
        visible: list[bool],
    ) -> tuple[NoteView, _TrackingNoteListStore]:
        """Build a view rendering ``note-A`` with an injected visibility flag.

        ``visible`` is a one-element list used as a mutable flag the test
        controls: ``visible[0]`` is what the injected predicate reports.
        Returns ``(view, store)``; the initial (unconditional) render has
        already run, so the buffer holds the original source.
        """
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A", source=self._ORIGINAL_SOURCE,
        )
        store = _build_tracking_store(repo)
        app_state = AppState()
        app_state.set_selected_note_id("note-A")
        view = NoteView(
            note_store=store,
            app_state=app_state,
            pane_is_visible=lambda: visible[0],
        )
        return view, store

    def test_edit_while_hidden_defers_render(self) -> None:
        # Given a hidden pane showing the original source
        visible = [False]
        view, store = self._view_on_note_a(visible)
        self.assertIn("Original", self._buffer_text(view))

        # When the displayed note is edited in the store
        store.update("note-A", self._EDITED_SOURCE)

        # Then no render happened: the buffer still shows the original,
        # and a render is recorded as owed.
        text = self._buffer_text(view)
        self.assertIn("Original", text)
        self.assertNotIn("Edited", text)
        self.assertTrue(view._render_pending)

    def test_view_mode_reveal_renders_deferred_edit(self) -> None:
        # Given a hidden pane with a deferred edit
        visible = [False]
        view, store = self._view_on_note_a(visible)
        store.update("note-A", self._EDITED_SOURCE)
        self.assertNotIn("Edited", self._buffer_text(view))

        # When the view-mode toggle reveals the pane (MainWindow calls
        # refresh() unconditionally on EDIT->VIEW)
        visible[0] = True
        view.refresh()

        # Then the edited source is now rendered and nothing is owed.
        self.assertIn("Edited", self._buffer_text(view))
        self.assertFalse(view._render_pending)

    def test_map_flushes_deferred_render(self) -> None:
        # Given a hidden pane with a deferred edit
        visible = [False]
        view, store = self._view_on_note_a(visible)
        store.update("note-A", self._EDITED_SOURCE)
        self.assertNotIn("Edited", self._buffer_text(view))

        # When the pane is actually mapped (the stack reveals it)
        window = Gtk.Window()
        window.set_default_size(900, 600)
        window.set_child(view)
        window.present()
        try:
            _settle_real_main_loop()
            # Then the map handler drained the owed render.
            self.assertIn("Edited", self._buffer_text(view))
            self.assertFalse(view._render_pending)
        finally:
            window.set_child(None)
            window.destroy()
            _settle_real_main_loop(timeout_ms=50)

    def test_map_without_pending_render_does_not_re_render(self) -> None:
        # Given a *visible* pane with no deferred edit — the initial
        # render read the note exactly once.
        visible = [True]
        view, store = self._view_on_note_a(visible)
        self.assertEqual(store.get_calls, ["note-A"])

        # When the pane is mapped with nothing owed
        window = Gtk.Window()
        window.set_default_size(900, 600)
        window.set_child(view)
        window.present()
        try:
            _settle_real_main_loop()
            # Then the map handler is a no-op: no extra render (no extra
            # store read), guarding against a double render on the common
            # EDIT->VIEW path where refresh() already ran before the map.
            self.assertEqual(store.get_calls, ["note-A"])
        finally:
            window.set_child(None)
            window.destroy()
            _settle_real_main_loop(timeout_ms=50)

    def test_edit_while_visible_renders_immediately(self) -> None:
        # Given a visible pane showing the original source
        visible = [True]
        view, store = self._view_on_note_a(visible)

        # When the displayed note is edited
        store.update("note-A", self._EDITED_SOURCE)

        # Then it re-renders at once (deferral is gated strictly on
        # visibility), and nothing is left owed.
        self.assertIn("Edited", self._buffer_text(view))
        self.assertFalse(view._render_pending)

    def test_selection_change_while_hidden_renders_immediately(self) -> None:
        # Given a hidden pane, with a second note available
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A", source=self._ORIGINAL_SOURCE,
        )
        repo.notes["note-B"] = _make_note(
            "note-B", source="= Second\n\nsecond body.\n",
        )
        store = _build_tracking_store(repo)
        app_state = AppState()
        app_state.set_selected_note_id("note-A")
        view = NoteView(
            note_store=store,
            app_state=app_state,
            pane_is_visible=lambda: False,
        )

        # When the selection changes while hidden
        app_state.set_selected_note_id("note-B")

        # Then the selection render is NOT deferred — the newly selected
        # note is rendered immediately so the pane is correct the instant
        # it is shown.
        self.assertIn("Second", self._buffer_text(view))
        self.assertFalse(view._render_pending)


def _find_scrolled_window(view: NoteView) -> Gtk.ScrolledWindow:
    """Walk the :class:`NoteView` stack and return its ``Gtk.ScrolledWindow``.

    The structure is ``NoteView → ScrolledWindow → …``. The parse-error
    notice is rendered into the note buffer rather than into a separate
    banner widget, so the ``ScrolledWindow`` is the view's *first*
    child. Walking the public child API keeps the tests agnostic to
    :class:`NoteView`'s field names.
    """
    scrolled = view.get_first_child()
    assert isinstance(scrolled, Gtk.ScrolledWindow), (
        f"expected a ScrolledWindow in the NoteView stack, "
        f"got {type(scrolled).__name__}"
    )
    return scrolled


def _find_text_view(view: NoteView) -> Gtk.TextView:
    """Walk the widget tree and pull out the inner :class:`Gtk.TextView`.

    The structure is ``NoteView → ScrolledWindow →
    ArticleContainer → TextView``. Under Option C the
    :class:`ArticleContainer` implements ``Gtk.Scrollable``, so the
    ``ScrolledWindow`` keeps it as its **direct** child and interposes no
    :class:`Gtk.Viewport`. The helper still tolerates a viewport (it steps
    past one if present) so it stays robust to layout changes, but in the
    current production tree there is none.

    We walk ``get_first_child`` / ``get_next_sibling`` / ``get_child``
    rather than reaching into private attributes of :class:`NoteView`,
    so the test stays agnostic to its internal field names. Returning
    the ``Gtk.TextView`` itself lets margin-wiring tests read the four
    margin properties via the documented public API.
    """
    scrolled = _find_scrolled_window(view)
    inner = scrolled.get_child()
    if isinstance(inner, Gtk.Viewport):
        article = inner.get_child()
    else:
        article = inner
    assert isinstance(article, ArticleContainer), (
        f"scrolled child should be an ArticleContainer, got {type(article).__name__}"
    )
    text_view = article.get_first_child()
    assert isinstance(text_view, Gtk.TextView), (
        f"article child should be a TextView, got {type(text_view).__name__}"
    )
    return text_view


def _find_text_view_buffer(view: NoteView) -> Gtk.TextBuffer:
    """Return the rendered TextView's buffer.

    Thin wrapper over :func:`_find_text_view` that exists because most
    of the existing tests reach for the buffer rather than the widget.
    """
    return _find_text_view(view).get_buffer()


_PNG_FIXTURE: bytes = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db4"
    "0000000049454e44ae426082"
)


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewImageResolverTests(unittest.TestCase):
    """Pin the wiring between :class:`NoteView` and the injected
    :class:`AttachmentStoreProtocol`. The resolver is the
    construction-time hook the renderer holds, so we exercise it via
    :attr:`NoteView.image_bytes_resolver` rather than rendering a
    full document — the renderer is tested elsewhere.
    """

    def _build_view(
        self,
        *,
        attachments: _FakeAttachmentStore | None,
    ) -> tuple[NoteView, _FakeNoteRepository, AppState]:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        repo.notes["note-B"] = _make_note(
            "note-B", source="= Other\n\nbody.\n"
        )
        state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo),
            app_state=state,
            attachments=attachments,
        )
        return view, repo, state

    def test_resolver_returns_empty_bytes_when_no_store_wired(self) -> None:
        # Without an attachment store, every resolver call is the
        # placeholder. Matches the step-8 behaviour for tests that
        # don't care about images.
        view, _, state = self._build_view(attachments=None)
        state.set_selected_note_id("note-A")
        self.assertEqual(view.image_bytes_resolver("any.png"), b"")

    def test_resolver_returns_empty_bytes_when_no_note_selected(self) -> None:
        store = _FakeAttachmentStore()
        view, _, _state = self._build_view(attachments=store)
        # Construction with no selection leaves _current_note_id None.
        self.assertIsNone(view.current_note_id)
        # Resolver short-circuits before consulting the store.
        self.assertEqual(view.image_bytes_resolver("foo.png"), b"")
        self.assertEqual(store.list_calls, [])
        self.assertEqual(store.get_bytes_calls, [])

    def test_resolver_returns_attached_bytes_for_matching_filename(self) -> None:
        store = _FakeAttachmentStore()
        attachment = store.seed("note-A", "photo.png", _PNG_FIXTURE)
        view, _, state = self._build_view(attachments=store)

        state.set_selected_note_id("note-A")
        self.assertEqual(view.current_note_id, "note-A")

        result = view.image_bytes_resolver("photo.png")
        self.assertEqual(result, _PNG_FIXTURE)

        # The resolver consulted the store correctly: list scoped to
        # the current note id, then get_bytes for the matching id.
        self.assertIn("note-A", store.list_calls)
        self.assertEqual(store.get_bytes_calls, [attachment.id])

    def test_resolver_returns_empty_bytes_for_unknown_filename(self) -> None:
        store = _FakeAttachmentStore()
        store.seed("note-A", "real.png", _PNG_FIXTURE)
        view, _, state = self._build_view(attachments=store)
        state.set_selected_note_id("note-A")

        result = view.image_bytes_resolver("missing.png")
        # Empty bytes → renderer falls back to placeholder. The list
        # was consulted, but get_bytes was NOT called for an
        # unmatched filename — that would be a wasted BLOB read.
        self.assertEqual(result, b"")
        self.assertEqual(store.get_bytes_calls, [])

    def test_resolver_scopes_lookup_to_current_note(self) -> None:
        # Two notes, each with a same-named attachment. Switching
        # selection must change the resolver's answer.
        store = _FakeAttachmentStore()
        store.seed("note-A", "shared.png", b"A's bytes")
        store.seed("note-B", "shared.png", b"B's bytes")
        view, _, state = self._build_view(attachments=store)

        state.set_selected_note_id("note-A")
        self.assertEqual(view.image_bytes_resolver("shared.png"), b"A's bytes")

        state.set_selected_note_id("note-B")
        self.assertEqual(view.image_bytes_resolver("shared.png"), b"B's bytes")

    def test_resolver_clears_when_selection_clears(self) -> None:
        store = _FakeAttachmentStore()
        store.seed("note-A", "photo.png", _PNG_FIXTURE)
        view, _, state = self._build_view(attachments=store)
        state.set_selected_note_id("note-A")
        # Sanity: before clearing, lookup returns bytes.
        self.assertEqual(view.image_bytes_resolver("photo.png"), _PNG_FIXTURE)
        store.list_calls.clear()
        store.get_bytes_calls.clear()

        state.set_selected_note_id(None)
        # After clearing, the resolver does not touch the store.
        self.assertEqual(view.image_bytes_resolver("photo.png"), b"")
        self.assertEqual(store.list_calls, [])
        self.assertEqual(store.get_bytes_calls, [])
        self.assertIsNone(view.current_note_id)

    def test_resolver_clears_on_unknown_selection(self) -> None:
        # A stale selection (e.g. note deleted out from under the
        # view) must not leave the resolver pointing at the old id.
        store = _FakeAttachmentStore()
        store.seed("note-A", "photo.png", _PNG_FIXTURE)
        view, _, state = self._build_view(attachments=store)
        state.set_selected_note_id("note-A")
        self.assertEqual(view.current_note_id, "note-A")

        state.set_selected_note_id("does-not-exist")
        self.assertIsNone(view.current_note_id)
        self.assertEqual(view.image_bytes_resolver("photo.png"), b"")


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewAttachmentSmokeTests(unittest.TestCase):
    """Construction smoke: ``NoteView`` accepts an ``attachments``
    parameter and renders cleanly with one wired."""

    def test_constructs_with_attachment_store(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        store = _FakeAttachmentStore()
        state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo),
            app_state=state,
            attachments=store,
        )
        self.assertIsInstance(view, Gtk.Box)
        # No selection at construction → no lookup yet.
        self.assertEqual(store.list_calls, [])

    def test_default_attachments_is_none_for_back_compat(self) -> None:
        # Existing callers (tests, the legacy main_window construction
        # path) build ``NoteView`` without an ``attachments`` kwarg.
        # That must keep working — the parameter has a default of
        # ``None`` and the resolver falls back to placeholder bytes.
        repo = _FakeNoteRepository()
        state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=state)
        self.assertEqual(view.image_bytes_resolver("any.png"), b"")


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewMarginWiringTests(unittest.TestCase):
    """Pin the four breathing-space margins on the rendered-view
    ``Gtk.TextView``.

    Stubbing :func:`note_view._build_font_measurers` (the single seam
    that constructs the production Pango measurers) lets the tests
    drive ``NoteView.__init__`` with deterministic font dimensions —
    fixed integer char-width and line-height — so the resulting
    margin values are exact rather than font-dependent.
    """

    def _build_view_with_stubbed_font(
        self, *, char_w: int, line_h: int,
    ) -> NoteView:
        repo = _FakeNoteRepository()
        state = AppState()
        with mock.patch.object(
            note_view_module,
            "_build_font_measurers",
            _stub_font_measurers_factory(char_w=char_w, line_h=line_h),
        ):
            return NoteView(note_store=_build_tracking_store(repo), app_state=state)

    def test_textview_top_margin_is_breathing_plus_end_gap(self) -> None:
        # The top margin now reserves the breathing lines *and* the same
        # desk band as the bottom, so it is the sum of the two constants —
        # the gap before the note matches the gap after it.
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        self.assertEqual(
            text_view.get_top_margin(),
            ARTICLE_TOP_MARGIN_LINES * 20 + round(ARTICLE_END_GAP_LINES * 20),
        )

    def test_textview_top_gap_is_set_and_below_the_top_margin(self) -> None:
        # The view's top gap matches the constant, and the breathing sheet
        # (top margin minus the gap) is exactly the top margin lines — so
        # the two halves cannot drift apart.
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        gap_px = round(ARTICLE_END_GAP_LINES * 20)
        self.assertEqual(text_view._top_gap_px, gap_px)
        self.assertEqual(
            text_view.get_top_margin() - text_view._top_gap_px,
            ARTICLE_TOP_MARGIN_LINES * 20,
        )

    def test_top_and_bottom_gaps_are_equal(self) -> None:
        # The whole point of the symmetry: the same desk band before and
        # after the note, derived from one constant so they cannot drift.
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        self.assertEqual(text_view._top_gap_px, text_view._end_gap_px)

    def test_textview_bottom_margin_is_breathing_plus_end_gap(self) -> None:
        # The bottom margin reserves the breathing lines *and* the
        # end-gap desk band, so it is the sum of the two constants.
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        self.assertEqual(
            text_view.get_bottom_margin(),
            ARTICLE_BOTTOM_MARGIN_LINES * 20 + round(ARTICLE_END_GAP_LINES * 20),
        )

    def test_textview_end_gap_is_set_and_below_the_bottom_margin(self) -> None:
        # The view's end-gap matches the constant, and the breathing
        # sheet (bottom margin minus the gap) is exactly the bottom
        # margin lines — so the two halves cannot drift apart.
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        end_gap_px = round(ARTICLE_END_GAP_LINES * 20)
        self.assertEqual(text_view._end_gap_px, end_gap_px)
        self.assertEqual(
            text_view.get_bottom_margin() - text_view._end_gap_px,
            ARTICLE_BOTTOM_MARGIN_LINES * 20,
        )

    def test_textview_left_margin_is_eight_char_widths(self) -> None:
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        self.assertEqual(
            text_view.get_left_margin(),
            ARTICLE_INNER_HPADDING_CHARS * 10,
        )

    def test_textview_right_margin_is_eight_char_widths(self) -> None:
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        self.assertEqual(
            text_view.get_right_margin(),
            ARTICLE_INNER_HPADDING_CHARS * 10,
        )

    def test_margins_scale_with_measured_font_dimensions(self) -> None:
        # Doubling the measured font dimensions doubles every margin
        # — the wiring reads cached measurements, not a constant.
        view_small = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        view_large = self._build_view_with_stubbed_font(char_w=20, line_h=40)
        tv_small = _find_text_view(view_small)
        tv_large = _find_text_view(view_large)

        self.assertEqual(tv_large.get_left_margin(), 2 * tv_small.get_left_margin())
        self.assertEqual(tv_large.get_right_margin(), 2 * tv_small.get_right_margin())
        self.assertEqual(tv_large.get_top_margin(), 2 * tv_small.get_top_margin())
        self.assertEqual(tv_large.get_bottom_margin(), 2 * tv_small.get_bottom_margin())


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewPreferredColumnWidthTests(unittest.TestCase):
    """Pin :meth:`NoteView.preferred_column_width_px`.

    The accessor reports the *outer* column width — text column plus
    the inner horizontal padding on both sides — which is what
    :class:`MainWindow` adds to the left-pane widths to size the
    initial window. Stubbing the font measurers makes the value exact
    rather than font-dependent.
    """

    def _build_view_with_stubbed_font(
        self, *, char_w: int, line_h: int,
    ) -> NoteView:
        repo = _FakeNoteRepository()
        state = AppState()
        with mock.patch.object(
            note_view_module,
            "_build_font_measurers",
            _stub_font_measurers_factory(char_w=char_w, line_h=line_h),
        ):
            return NoteView(note_store=_build_tracking_store(repo), app_state=state)

    def test_reports_outer_column_width(self) -> None:
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        expected = (
            TARGET_CHARS_PER_LINE + 2 * ARTICLE_INNER_HPADDING_CHARS
        ) * 10
        self.assertEqual(view.preferred_column_width_px(), expected)

    def test_scales_with_measured_char_width(self) -> None:
        # Doubling the measured M-width doubles the reported column —
        # the value tracks the font, it is not a constant.
        narrow = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        wide = self._build_view_with_stubbed_font(char_w=20, line_h=20)
        self.assertEqual(
            wide.preferred_column_width_px(),
            2 * narrow.preferred_column_width_px(),
        )


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewRendererWiringTests(unittest.TestCase):
    """The renderer must be fed the *text* column width — not the
    outer (padded) width — so tables and images continue to lay out
    against the 66-character reading column the user actually sees,
    independent of the inner horizontal padding.
    """

    def _build_view_with_stubbed_font(
        self, *, char_w: int, line_h: int,
    ) -> NoteView:
        repo = _FakeNoteRepository()
        state = AppState()
        with mock.patch.object(
            note_view_module,
            "_build_font_measurers",
            _stub_font_measurers_factory(char_w=char_w, line_h=line_h),
        ):
            return NoteView(note_store=_build_tracking_store(repo), app_state=state)

    def test_renderer_receives_text_column_width_not_outer(self) -> None:
        # char_w=10 → text width = 66 × 10 = 660; outer width =
        # (66 + 2 × 8) × 10 = 820. The wired column-width resolver
        # must report 660 — the text width, not the outer.
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        # Reading the renderer's private column-width callable is
        # fine here — the test files have ``protected-access``
        # disabled, and both attributes are typed on their respective
        # classes so mypy is happy. There is no public introspection
        # surface for the renderer's wiring.
        column_width_callable = view._renderer._column_width_px
        self.assertEqual(column_width_callable(), TARGET_CHARS_PER_LINE * 10)

    def test_horizontal_padding_does_not_change_text_width(self) -> None:
        # Two NoteViews with different char widths — the renderer's
        # wired callable must scale linearly with char_w, and in
        # particular must return exactly 66 × char_w in each (no
        # contamination from the 2 × 8 padding term that bumps the
        # outer width).
        for char_w in (10, 20):
            view = self._build_view_with_stubbed_font(char_w=char_w, line_h=20)
            column_width_callable = view._renderer._column_width_px
            with self.subTest(char_w=char_w):
                self.assertEqual(
                    column_width_callable(),
                    TARGET_CHARS_PER_LINE * char_w,
                )


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewUnreadSourceTests(unittest.TestCase):
    """A note that will not parse still renders.

    The pane no longer replaces a note with a notice. Source folio cannot
    read is carried into the buffer verbatim and marked in place when the
    failure is structural; the rest of the note renders normally.
    """

    def test_no_unread_blocks_with_no_selection(self) -> None:
        repo = _FakeNoteRepository()
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        self.assertEqual(view.unread_block_count, 0)

    def test_no_unread_blocks_on_a_clean_note(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        app_state.set_selected_note_id("note-A")
        self.assertEqual(view.unread_block_count, 0)

    def test_structural_failure_is_counted(self) -> None:
        repo = _FakeNoteRepository()
        # `:bad name:` lexes as a LineToken the parser rejects at
        # block start — a structural failure, so it is marked.
        repo.notes["note-A"] = _make_note(
            "note-A", source=":bad name: value\n",
        )
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        app_state.set_selected_note_id("note-A")
        self.assertEqual(view.unread_block_count, 1)

    def test_inline_failure_is_not_counted(self) -> None:
        # An unpaired marker renders as ordinary prose and carries no
        # mark, so counting it would report a problem the reader has no
        # evidence of.
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A", source="a snake_case word\n",
        )
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        app_state.set_selected_note_id("note-A")
        self.assertEqual(view.unread_block_count, 0)

    def test_structural_failure_renders_its_source_verbatim(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A", source=":bad name: value\n",
        )
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        app_state.set_selected_note_id("note-A")

        buffer = _find_text_view_buffer(view)
        rendered = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False,
        )
        self.assertIn(":bad name: value", rendered)

    def test_structural_failure_renders_a_reason(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A", source="= D\n\n----\nopen forever\n",
        )
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        app_state.set_selected_note_id("note-A")

        buffer = _find_text_view_buffer(view)
        rendered = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False,
        )
        self.assertIn("code block", rendered)

    def test_the_rest_of_a_broken_note_still_renders(self) -> None:
        # The whole point of the change: one bad construct costs that
        # construct, not the note. Before this, the reader saw a notice
        # and none of the surrounding prose.
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A",
            source=(
                "= Deploy notes\n\nBefore the break.\n\n"
                "// a comment\n\nAfter the break.\n"
            ),
        )
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        app_state.set_selected_note_id("note-A")

        buffer = _find_text_view_buffer(view)
        rendered = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False,
        )
        self.assertIn("Before the break.", rendered)
        self.assertIn("After the break.", rendered)

    def test_count_clears_when_selecting_a_clean_note(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note("bad", source="// a comment\n")
        repo.notes["good"] = _make_note("good")
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )

        app_state.set_selected_note_id("bad")
        self.assertEqual(view.unread_block_count, 1)

        app_state.set_selected_note_id("good")
        self.assertEqual(view.unread_block_count, 0)

    def test_count_clears_when_selection_clears(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note("bad", source="// a comment\n")
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        app_state.set_selected_note_id("bad")
        self.assertEqual(view.unread_block_count, 1)

        app_state.set_selected_note_id(None)
        self.assertEqual(view.unread_block_count, 0)

    def test_count_clears_when_selection_points_to_missing_note(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note("bad", source="// a comment\n")
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )
        app_state.set_selected_note_id("bad")
        self.assertEqual(view.unread_block_count, 1)

        app_state.set_selected_note_id("does-not-exist")
        self.assertEqual(view.unread_block_count, 0)

    def test_navigating_to_a_broken_note_shows_no_stale_content(self) -> None:
        # The buffer is rebuilt per render, so the previous note's text
        # must not survive into a note that fails to parse.
        repo = _FakeNoteRepository()
        repo.notes["good"] = _make_note(
            "good", source="= Welcome\n\nIts contents.\n",
        )
        repo.notes["bad"] = _make_note("bad", source="// a comment\n")
        app_state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo), app_state=app_state,
        )

        app_state.set_selected_note_id("good")
        buffer = _find_text_view_buffer(view)
        self.assertIn(
            "Welcome",
            buffer.get_text(
                buffer.get_start_iter(), buffer.get_end_iter(), False,
            ),
        )

        app_state.set_selected_note_id("bad")
        bad_text = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False,
        )
        self.assertNotIn("Welcome", bad_text)
        self.assertIn("// a comment", bad_text)


@unittest.skipUnless(display_available(), "no GDK display")
class BuildArticleSurfaceTests(unittest.TestCase):
    """The shared article-surface constructor.

    :func:`build_article_surface` is the single place that assembles the
    "rendered note" surface both :class:`NoteView` and
    :class:`giruntime.ui.help_window.HelpWindow` build on, so they render
    identically. The surface must come back fully wired: the painted view
    parented into a fixed-width :class:`ArticleContainer`, the block-tint
    washes installed, the font-relative margins applied, and the outer
    column width cached. Font dimensions are stubbed (10 px M, 20 px line)
    so the margin assertions are exact.
    """

    def _build(self) -> note_view_module.ArticleSurface:
        with mock.patch.object(
            note_view_module,
            "_build_font_measurers",
            _stub_font_measurers_factory(char_w=10, line_h=20),
        ):
            return build_article_surface()

    def test_view_is_parented_into_a_fixed_width_container(self) -> None:
        surface = self._build()
        self.assertIsInstance(surface.text_view, ArticleTextView)
        self.assertIsInstance(surface.container, ArticleContainer)
        self.assertIs(surface.text_view.get_parent(), surface.container)

    def test_block_tints_are_installed(self) -> None:
        surface = self._build()
        self.assertEqual(
            len(surface.text_view._wash_specs_by_tag), len(build_wash_specs(LIGHT_PALETTE)),
        )

    def test_outer_column_width_matches_the_container(self) -> None:
        surface = self._build()
        self.assertEqual(
            surface.outer_column_width_px,
            surface.container.outer_column_width(),
        )

    def test_font_relative_margins_are_applied(self) -> None:
        surface = self._build()
        view = surface.text_view
        self.assertEqual(
            view.get_left_margin(), ARTICLE_INNER_HPADDING_CHARS * 10,
        )
        self.assertEqual(
            view.get_right_margin(), ARTICLE_INNER_HPADDING_CHARS * 10,
        )
        end_gap_px = round(ARTICLE_END_GAP_LINES * 20)
        self.assertEqual(
            view.get_top_margin(), ARTICLE_TOP_MARGIN_LINES * 20 + end_gap_px,
        )
        self.assertEqual(
            view.get_bottom_margin(),
            ARTICLE_BOTTOM_MARGIN_LINES * 20 + end_gap_px,
        )


def _buffer_text(buffer: Gtk.TextBuffer) -> str:
    """Whole buffer text (no anchors on the metadata path)."""
    text: str = buffer.get_text(
        buffer.get_start_iter(), buffer.get_end_iter(), False,
    )
    return text


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewMetadataTests(unittest.TestCase):
    """The metadata line is inserted as plain tagged text directly under
    the title — ``Created … · Modified … · #tag …`` — not as an
    anchored chip widget. These tests pin its placement, tag, ordering,
    the tagless form, and the absence of any anchored widget.
    """

    def _build_view(
        self,
        repo: _FakeNoteRepository,
        state: AppState,
    ) -> NoteView:
        """Build a :class:`NoteView` with stubbed font measurers.

        Mirrors the construction pattern used by
        :class:`NoteViewMarginWiringTests`: the deterministic font
        dimensions are irrelevant to the metadata text, but the stubbed
        factory keeps the widget tree free of Pango / theme
        dependencies.
        """
        with mock.patch.object(
            note_view_module,
            "_build_font_measurers",
            _stub_font_measurers_factory(char_w=10, line_h=20),
        ):
            return NoteView(note_store=_build_tracking_store(repo), app_state=state)

    def test_metadata_line_sits_immediately_under_the_title(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A",
            source="= Hello\n:tags: bar, foo\n\nbody.\n",
            tags=("bar", "foo"),
        )
        state = AppState()
        state.set_selected_note_id("note-A")
        view = self._build_view(repo, state)

        text = _buffer_text(view._buffer)
        # Title, then the metadata line on the very next line.
        self.assertTrue(text.startswith("Hello\nCreated "))

    def test_metadata_line_carries_the_metadata_tag(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A",
            source="= Hello\n:tags: foo\n\nbody.\n",
            tags=("foo",),
        )
        state = AppState()
        state.set_selected_note_id("note-A")
        view = self._build_view(repo, state)

        text = _buffer_text(view._buffer)
        tag = view._buffer.get_tag_table().lookup(TagName.METADATA.value)
        self.assertIsNotNone(tag)
        meta_iter = view._buffer.get_iter_at_offset(text.index("Created"))
        self.assertTrue(meta_iter.has_tag(tag))

    def test_metadata_order_is_created_modified_tags(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A",
            source="= Hello\n:tags: bar, foo\n\nbody.\n",
            tags=("bar", "foo"),
        )
        state = AppState()
        state.set_selected_note_id("note-A")
        view = self._build_view(repo, state)

        text = _buffer_text(view._buffer)
        self.assertLess(text.index("Created"), text.index("Modified"))
        self.assertLess(text.index("Modified"), text.index("#bar"))
        # Both tags appear, in the note's (sorted) order.
        self.assertLess(text.index("#bar"), text.index("#foo"))

    def test_tagless_note_shows_only_the_two_dates(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A",
            source="= Hello\n\nbody.\n",
            tags=(),
        )
        state = AppState()
        state.set_selected_note_id("note-A")
        view = self._build_view(repo, state)

        text = _buffer_text(view._buffer)
        # The metadata line is the second line of the buffer.
        metadata_line = text.split("\n")[1]
        self.assertIn("Created", metadata_line)
        self.assertIn("Modified", metadata_line)
        # No tag run when the note is untagged.
        self.assertNotIn("#", metadata_line)

    def test_no_chip_widget_is_anchored_in_the_text_view(self) -> None:
        # The metadata is plain text — there must be no child anchor in
        # the buffer (the note has no table, the only other anchor
        # source), and the view holds no chip-row widget.
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A",
            source="= Hello\n:tags: foo\n\nbody.\n",
            tags=("foo",),
        )
        state = AppState()
        state.set_selected_note_id("note-A")
        view = self._build_view(repo, state)

        iterator = view._buffer.get_start_iter()
        while True:
            self.assertIsNone(iterator.get_child_anchor())
            if not iterator.forward_char():
                break
        self.assertFalse(hasattr(view, "_chip_row"))


class FormatMetadataLineTests(unittest.TestCase):
    """:func:`_format_metadata_line` is pure and display-independent."""

    def test_includes_both_dates_in_order(self) -> None:
        created = datetime(2026, 5, 26, tzinfo=UTC)
        modified = datetime(2026, 5, 30, tzinfo=UTC)
        line = _format_metadata_line(created, modified, ())
        self.assertEqual(
            line,
            f"Created {format_date_long(created)}"
            f"  \u00b7  Modified {format_date_long(modified)}",
        )

    def test_appends_tag_run_when_tags_present(self) -> None:
        created = datetime(2026, 5, 26, tzinfo=UTC)
        modified = datetime(2026, 5, 30, tzinfo=UTC)
        line = _format_metadata_line(created, modified, ("nothing", "test"))
        self.assertTrue(line.endswith("#nothing  #test"))
        # The tag run is its own ``·``-separated segment.
        self.assertIn("\u00b7  #nothing", line)

    def test_no_tag_run_when_tagless(self) -> None:
        created = datetime(2026, 5, 26, tzinfo=UTC)
        modified = datetime(2026, 5, 30, tzinfo=UTC)
        self.assertNotIn("#", _format_metadata_line(created, modified, ()))


class _RecordingSaveDialog:
    """A synchronous stand-in for the production save-dialog opener.

    Records the suggested name it was offered and hands back a path the
    test dictates — ``None`` models a cancelled dialog.
    """

    suggested_names: list[str]
    result: Path | None

    def __init__(self, result: Path | None) -> None:
        self.suggested_names = []
        self.result = result

    def __call__(
        self,
        _parent: Gtk.Widget,
        suggested_name: str,
        on_result: Callable[[Path | None], None],
    ) -> None:
        self.suggested_names.append(suggested_name)
        on_result(self.result)


class _RecordingExportController:
    """Captures :meth:`NoteController.export_attachment` calls."""

    exports: list[tuple[str, Path]]

    def __init__(self) -> None:
        self.exports = []

    def export_attachment(self, attachment_id: str, destination: Path) -> bool:
        self.exports.append((attachment_id, destination))
        return True


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewAttachmentActivationTests(unittest.TestCase):
    """Clicking a save link: resolve → dialog → controller export."""

    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self._dir = TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _build_view(
        self,
        *,
        attachments: _FakeAttachmentStore | None,
        dialog: _RecordingSaveDialog,
        controller: _RecordingExportController | None,
    ) -> tuple[NoteView, AppState]:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo),
            app_state=state,
            attachments=attachments,
            note_controller=controller,  # type: ignore[arg-type]
            save_dialog_opener=dialog,
        )
        return view, state

    def test_known_filename_opens_the_dialog_with_that_name(self) -> None:
        store = _FakeAttachmentStore()
        store.seed("note-A", "photo.png", _PNG_FIXTURE)
        dialog = _RecordingSaveDialog(result=self.root / "out.png")
        controller = _RecordingExportController()
        view, state = self._build_view(
            attachments=store,
            dialog=dialog,
            controller=controller,
        )
        state.set_selected_note_id("note-A")

        view._activate_attachment("photo.png")
        self.assertEqual(dialog.suggested_names, ["photo.png"])

    def test_chosen_path_is_exported_through_the_controller(self) -> None:
        store = _FakeAttachmentStore()
        attachment = store.seed("note-A", "photo.png", _PNG_FIXTURE)
        destination = self.root / "out.png"
        dialog = _RecordingSaveDialog(result=destination)
        controller = _RecordingExportController()
        view, state = self._build_view(
            attachments=store,
            dialog=dialog,
            controller=controller,
        )
        state.set_selected_note_id("note-A")

        view._activate_attachment("photo.png")
        self.assertEqual(controller.exports, [(attachment.id, destination)])

    def test_cancelled_dialog_exports_nothing(self) -> None:
        store = _FakeAttachmentStore()
        store.seed("note-A", "photo.png", _PNG_FIXTURE)
        dialog = _RecordingSaveDialog(result=None)
        controller = _RecordingExportController()
        view, state = self._build_view(
            attachments=store,
            dialog=dialog,
            controller=controller,
        )
        state.set_selected_note_id("note-A")

        view._activate_attachment("photo.png")
        self.assertEqual(controller.exports, [])

    def test_unknown_filename_opens_no_dialog(self) -> None:
        store = _FakeAttachmentStore()
        store.seed("note-A", "real.png", _PNG_FIXTURE)
        dialog = _RecordingSaveDialog(result=self.root / "out.png")
        controller = _RecordingExportController()
        view, state = self._build_view(
            attachments=store,
            dialog=dialog,
            controller=controller,
        )
        state.set_selected_note_id("note-A")

        view._activate_attachment("missing.png")
        self.assertEqual(dialog.suggested_names, [])
        self.assertEqual(controller.exports, [])

    def test_no_store_opens_no_dialog(self) -> None:
        dialog = _RecordingSaveDialog(result=self.root / "out.png")
        view, state = self._build_view(
            attachments=None,
            dialog=dialog,
            controller=_RecordingExportController(),
        )
        state.set_selected_note_id("note-A")

        view._activate_attachment("photo.png")
        self.assertEqual(dialog.suggested_names, [])


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewAttachmentListResolverTests(unittest.TestCase):
    """The metadata-only resolver the ``attachments::[]`` macro expands with."""

    def _build_view(
        self,
        *,
        attachments: _FakeAttachmentStore | None,
    ) -> tuple[NoteView, AppState]:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note(
            "note-A",
            source="= A\n\nattachments::[]\n",
        )
        repo.notes["note-B"] = _make_note("note-B")
        state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo),
            app_state=state,
            attachments=attachments,
        )
        return view, state

    def test_no_store_resolves_to_an_empty_tuple(self) -> None:
        view, state = self._build_view(attachments=None)
        state.set_selected_note_id("note-A")
        self.assertEqual(view._list_attachments(), ())

    def test_no_selection_resolves_to_an_empty_tuple(self) -> None:
        store = _FakeAttachmentStore()
        view, _ = self._build_view(attachments=store)
        self.assertEqual(view._list_attachments(), ())
        self.assertEqual(store.list_calls, [])

    def test_resolver_is_scoped_to_the_current_note(self) -> None:
        store = _FakeAttachmentStore()
        mine = store.seed("note-A", "a.pdf", b"x")
        store.seed("note-B", "b.pdf", b"y")
        view, state = self._build_view(attachments=store)
        state.set_selected_note_id("note-A")
        self.assertEqual(view._list_attachments(), (mine,))

    def test_no_blob_is_read_to_draw_the_table(self) -> None:
        # The metadata/bytes split: rendering the table must not touch
        # a single BLOB.
        store = _FakeAttachmentStore()
        store.seed("note-A", "a.pdf", b"x")
        view, state = self._build_view(attachments=store)
        state.set_selected_note_id("note-A")
        buffer = _find_text_view_buffer(view)
        text = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            False,
        )
        self.assertIn("a.pdf", text)
        self.assertEqual(store.get_bytes_calls, [])


@unittest.skipUnless(display_available(), "no GDK display")
class NoteViewAttachmentNamedTests(unittest.TestCase):
    """The image macro and the save link share one lookup."""

    def test_lookup_finds_the_attachment_of_the_current_note(self) -> None:
        store = _FakeAttachmentStore()
        attachment = store.seed("note-A", "photo.png", _PNG_FIXTURE)
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        state = AppState()
        view = NoteView(
            note_store=_build_tracking_store(repo),
            app_state=state,
            attachments=store,
        )
        state.set_selected_note_id("note-A")
        self.assertEqual(view._attachment_named("photo.png"), attachment)
        self.assertIsNone(view._attachment_named("missing.png"))


class CellWidthMeasurerAppliesTheMonospaceCorrectionTests(unittest.TestCase):
    """A monospace cell is measured at the size it is drawn at.

    The measurer sizes table columns; the MONOSPACE tag draws the text.
    They apply the family and the size correction from the same two
    constants in ``tag_table`` precisely so they cannot disagree — a
    column measured for text a tenth larger than what lands in it is
    a column with a visible gap on the right of every cell.
    """

    @unittest.skipUnless(display_available(), "needs a display")
    def test_monospace_is_measured_narrower_than_the_bare_family(self) -> None:
        label = Gtk.Label()
        measure = make_cell_width_measurer(label)

        proportional = measure("MMMMMMMM", False, False)
        monospace = measure("MMMMMMMM", False, True)

        # Not a fixed ratio — the two faces differ in advance width as
        # well as in size, and the available monospace font varies by
        # host. What must hold is that the correction was applied at
        # all, so the measured width is below what the uncorrected
        # family alone would give.
        self.assertGreater(monospace, 0)
        self.assertLess(MONOSPACE_SCALE, 1.0)
        self.assertLess(monospace, proportional / MONOSPACE_SCALE)
