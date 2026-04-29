"""Tests for :mod:`notes_app.ui.note_view`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk  # noqa: E402

from notes_app.config.defaults import TARGET_CHARS_PER_LINE
from notes_app.controllers.app_state import AppState
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


if __name__ == "__main__":
    unittest.main()
