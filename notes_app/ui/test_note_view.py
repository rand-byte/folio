"""Tests for :mod:`notes_app.ui.note_view`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk  # noqa: E402

from notes_app.config.defaults import TARGET_CHARS_PER_LINE
from notes_app.controllers.app_state import AppState
from notes_app.enums import MimeKind
from notes_app.models.attachment import Attachment
from notes_app.models.note import Note
from notes_app.ui.note_view import (
    ArticleContainer,
    NoteView,
    _FALLBACK_CHAR_WIDTH_PX,
    _placeholder_image_bytes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def _make_note(
    note_id: str,
    *,
    source: str = "= Hello\n\nbody.\n",
    notebook_id: str = "nb-1",
    title: str | None = None,
) -> Note:
    """Build a deterministic :class:`Note` for tests."""
    return Note(
        id=note_id,
        title=title if title is not None else "Hello",
        notebook_id=notebook_id,
        source=source,
        snippet="body.",
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW + timedelta(seconds=1),
    )


class _FakeNoteRepository:
    """Minimal :class:`NoteRepositoryProtocol` impl for view tests.

    Only the methods :class:`NoteView` actually calls are filled in;
    the rest raise :class:`NotImplementedError` so a future test that
    invokes one by accident fails loudly rather than silently.
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

    # Unused by the view, but the protocol requires them.
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
        _note_id: str,
        _source: str,
        _modified_at: datetime,
    ) -> None:
        raise NotImplementedError

    def update_notebook(self, _note_id: str, _notebook_id: str) -> None:
        raise NotImplementedError

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError


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
            mime_type=MimeKind.PNG,
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


# ---------------------------------------------------------------------------
# _placeholder_image_bytes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ArticleContainer
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerTargetWidthTests(unittest.TestCase):
    def test_uses_target_chars_per_line_times_measured_glyph(self) -> None:
        container = ArticleContainer(char_width_measurer=lambda: 12)
        self.assertEqual(
            container.target_column_width(),
            TARGET_CHARS_PER_LINE * 12,
        )

    def test_measurer_invoked_only_once_across_calls(self) -> None:
        calls: list[None] = []

        def measure() -> int:
            calls.append(None)
            return 10

        container = ArticleContainer(char_width_measurer=measure)
        first = container.target_column_width()
        second = container.target_column_width()
        third = container.target_column_width()

        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(calls), 1)

    def test_non_positive_measurement_uses_fallback(self) -> None:
        # A real font's "M" is never zero pixels wide; the fallback
        # exists for the corner case (measuring before the widget has
        # any font at all). A zero result must yield a usable
        # column, not a zero-pixel one.
        container = ArticleContainer(char_width_measurer=lambda: 0)
        self.assertEqual(
            container.target_column_width(),
            TARGET_CHARS_PER_LINE * _FALLBACK_CHAR_WIDTH_PX,
        )

    def test_negative_measurement_uses_fallback(self) -> None:
        container = ArticleContainer(char_width_measurer=lambda: -3)
        self.assertEqual(
            container.target_column_width(),
            TARGET_CHARS_PER_LINE * _FALLBACK_CHAR_WIDTH_PX,
        )


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerSizeAllocateTests(unittest.TestCase):
    """Pin the column-width rule from §10 of the plan.

    A wide allocation centres the column with equal margins; a narrow
    or exact allocation leaves both margins at 0 (the parent
    :class:`Gtk.ScrolledWindow` is responsible for the horizontal
    scrollbar in that case — the test does not assert on that).
    """

    def test_wide_window_sets_equal_margins_absorbing_slack(self) -> None:
        container = ArticleContainer(char_width_measurer=lambda: 10)
        # target = TARGET_CHARS_PER_LINE * 10
        target = TARGET_CHARS_PER_LINE * 10
        allocated = target + 200  # 200 px of slack
        container.do_size_allocate(allocated, 600, -1)

        expected_side = (allocated - target) // 2  # 100
        self.assertEqual(container.get_margin_start(), expected_side)
        self.assertEqual(container.get_margin_end(), expected_side)

    def test_narrow_window_sets_zero_margins(self) -> None:
        container = ArticleContainer(char_width_measurer=lambda: 10)
        target = TARGET_CHARS_PER_LINE * 10
        # Narrower than the target — column does not shrink; outer
        # ScrolledWindow is responsible for the scrollbar (out of scope
        # here).
        container.do_size_allocate(target - 200, 600, -1)
        self.assertEqual(container.get_margin_start(), 0)
        self.assertEqual(container.get_margin_end(), 0)

    def test_exact_target_width_sets_zero_margins(self) -> None:
        # The boundary: allocated == target → no slack to absorb. The
        # ``>`` (strict) check in the implementation is what produces
        # this; ``>=`` would produce a 0-margin allocation here too,
        # but the strict form makes the equality case explicit.
        container = ArticleContainer(char_width_measurer=lambda: 10)
        target = TARGET_CHARS_PER_LINE * 10
        container.do_size_allocate(target, 600, -1)
        self.assertEqual(container.get_margin_start(), 0)
        self.assertEqual(container.get_margin_end(), 0)

    def test_repeated_allocate_with_same_width_is_idempotent(self) -> None:
        # The implementation guards margin writes with an inequality
        # check so the ``queue_resize`` triggered by ``set_margin_*``
        # cannot oscillate. After a stable allocation, repeating it
        # leaves the margins in place.
        container = ArticleContainer(char_width_measurer=lambda: 10)
        target = TARGET_CHARS_PER_LINE * 10
        allocated = target + 80
        for _ in range(3):
            container.do_size_allocate(allocated, 400, -1)
        expected_side = (allocated - target) // 2  # 40
        self.assertEqual(container.get_margin_start(), expected_side)
        self.assertEqual(container.get_margin_end(), expected_side)

    def test_widening_then_narrowing_resets_margins_to_zero(self) -> None:
        container = ArticleContainer(char_width_measurer=lambda: 10)
        target = TARGET_CHARS_PER_LINE * 10
        # Wide first.
        container.do_size_allocate(target + 300, 600, -1)
        self.assertGreater(container.get_margin_start(), 0)
        # Then narrow — margins must drop back to 0.
        container.do_size_allocate(target - 100, 600, -1)
        self.assertEqual(container.get_margin_start(), 0)
        self.assertEqual(container.get_margin_end(), 0)


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerMeasureTests(unittest.TestCase):
    """The horizontal-measurement override is what makes the column
    not shrink: the parent ``ScrolledWindow``'s viewport never
    allocates us less than the reported minimum, so a narrow window
    triggers a horizontal scrollbar instead of a smaller column.
    """

    def test_horizontal_minimum_and_natural_equal_target_column_width(
        self,
    ) -> None:
        container = ArticleContainer(char_width_measurer=lambda: 10)
        target = TARGET_CHARS_PER_LINE * 10
        minimum, natural, _, _ = container.do_measure(
            Gtk.Orientation.HORIZONTAL, -1
        )
        self.assertEqual(minimum, target)
        self.assertEqual(natural, target)

    def test_horizontal_measurement_is_independent_of_for_size(self) -> None:
        # The horizontal width is fixed by the column rule — it must
        # not vary with the cross-axis hint.
        container = ArticleContainer(char_width_measurer=lambda: 10)
        for for_size in (-1, 0, 100, 5000):
            minimum, natural, _, _ = container.do_measure(
                Gtk.Orientation.HORIZONTAL, for_size,
            )
            self.assertEqual(minimum, TARGET_CHARS_PER_LINE * 10)
            self.assertEqual(natural, TARGET_CHARS_PER_LINE * 10)

    def test_hexpand_is_set_so_wide_viewport_overshoots_natural(self) -> None:
        # ``hexpand`` is what tells a wide ``Viewport`` to allocate us
        # more than our natural width — the precondition for the
        # margin-absorbing branch in ``do_size_allocate``.
        container = ArticleContainer(char_width_measurer=lambda: 10)
        self.assertTrue(container.get_hexpand())


# ---------------------------------------------------------------------------
# NoteView smoke tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteViewSmokeTests(unittest.TestCase):
    """Per §10: smoke-test only — ``NoteView`` constructs and reacts to
    selection changes through :class:`AppState`. No interaction tests.
    """

    def test_constructs_with_empty_state(self) -> None:
        repo = _FakeNoteRepository()
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)
        # No note is selected, so the repo's ``get`` must not have run.
        self.assertEqual(repo.get_calls, [])
        # The widget exists and is a GTK box.
        self.assertIsInstance(view, Gtk.Box)

    def test_initial_render_pulls_currently_selected_note(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        app_state = AppState()
        # Selection is set *before* construction — the initial refresh
        # should pick this up.
        app_state.set_selected_note_id("note-A")

        NoteView(note_repository=repo, app_state=app_state)

        self.assertEqual(repo.get_calls, ["note-A"])

    def test_selection_change_triggers_refresh(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        repo.notes["note-B"] = _make_note(
            "note-B", source="= Other\n\nelsewhere.\n",
        )
        app_state = AppState()
        NoteView(note_repository=repo, app_state=app_state)
        self.assertEqual(repo.get_calls, [])

        app_state.set_selected_note_id("note-A")
        app_state.set_selected_note_id("note-B")

        # Both selections drove a refresh and therefore a repository
        # ``get``. Order matches the selection sequence.
        self.assertEqual(repo.get_calls, ["note-A", "note-B"])

    def test_clearing_selection_does_not_call_get(self) -> None:
        repo = _FakeNoteRepository()
        app_state = AppState()
        NoteView(note_repository=repo, app_state=app_state)

        app_state.set_selected_note_id(None)
        # Setting to None when it was already None is a no-op (the
        # signal is gated on a real change). Either way the repo is
        # not consulted for a None selection.
        self.assertEqual(repo.get_calls, [])

    def test_unknown_selected_note_id_is_handled_gracefully(self) -> None:
        # A stale id (e.g. note deleted in another window) must not
        # crash the view — it simply renders nothing.
        repo = _FakeNoteRepository()  # empty
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)

        app_state.set_selected_note_id("does-not-exist")

        # The repo *was* asked, but the missing-id path swallowed the
        # KeyError and cleared the buffer.
        self.assertEqual(repo.get_calls, ["does-not-exist"])
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


# ---------------------------------------------------------------------------
# Tiny helper to locate the inner TextView's buffer for assertions.
# ---------------------------------------------------------------------------


def _find_text_view_buffer(view: NoteView) -> Gtk.TextBuffer:
    """Walk the widget tree and pull out the inner TextView's buffer.

    The structure is ``NoteView → ScrolledWindow → [Viewport →]
    ArticleContainer → TextView``. ``Gtk.ScrolledWindow`` wraps any
    non-:class:`Gtk.Scrollable` child in a :class:`Gtk.Viewport`
    automatically; since :class:`ArticleContainer` is a ``Gtk.Box``
    (not natively scrollable), the viewport is always present in
    production and the helper steps past it.

    We walk ``get_first_child`` / ``get_child`` rather than reaching
    into private attributes of :class:`NoteView`, so the test stays
    agnostic to its internal field names.
    """
    scrolled = view.get_first_child()
    assert isinstance(scrolled, Gtk.ScrolledWindow), (
        f"first child should be a ScrolledWindow, got {type(scrolled).__name__}"
    )
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
    return text_view.get_buffer()


# ---------------------------------------------------------------------------
# Image-bytes resolver: integration with AttachmentStoreProtocol
# ---------------------------------------------------------------------------


_PNG_FIXTURE: bytes = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db4"
    "0000000049454e44ae426082"
)
"""Real-shape PNG bytes for resolver-round-trip tests.

The renderer-level tests use the same shape; here the bytes are
opaque payload — the resolver does not decode, only fetches.
"""


@unittest.skipUnless(_display_available(), "no GDK display")
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
            note_repository=repo,
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


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteViewAttachmentSmokeTests(unittest.TestCase):
    """Construction smoke: ``NoteView`` accepts an ``attachments``
    parameter and renders cleanly with one wired."""

    def test_constructs_with_attachment_store(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")
        store = _FakeAttachmentStore()
        state = AppState()
        view = NoteView(
            note_repository=repo,
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
        view = NoteView(note_repository=repo, app_state=state)
        self.assertEqual(view.image_bytes_resolver("any.png"), b"")


if __name__ == "__main__":
    unittest.main()
