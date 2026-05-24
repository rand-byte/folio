"""Tests for :mod:`notes_app.ui.note_view`."""

from __future__ import annotations

import gc
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gsk, Gtk  # noqa: E402

from notes_app.asciidoc.tag_table import (
    TagName,
    WashSpec,
    admonition_body_tag_name,
    build_tag_table,
    build_wash_specs,
)
from notes_app.config.defaults import (
    ARTICLE_BOTTOM_MARGIN_LINES,
    ARTICLE_INNER_HPADDING_CHARS,
    ARTICLE_TOP_MARGIN_LINES,
    TARGET_CHARS_PER_LINE,
)
from notes_app.controllers.app_state import AppState
from notes_app.enums import AdmonitionKind, MimeKind, ParseErrorKind
from notes_app.models.attachment import Attachment
from notes_app.models.note import Note
from notes_app.ui import note_view as note_view_module
from notes_app.ui.note_view import (
    ArticleContainer,
    CharWidthMeasurer,
    LineHeightMeasurer,
    NoteView,
    _ArticleTextView,
    _FALLBACK_CHAR_WIDTH_PX,
    _FALLBACK_LINE_HEIGHT_PX,
    _message_for,
    _placeholder_image_bytes,
    _rgba_from_tint,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


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


def _make_test_article_container(
    *,
    char_w: int = 10,
    line_h: int = 20,
) -> ArticleContainer:
    """Build an :class:`ArticleContainer` wired with fixed measurers.

    Keeps the two-arg construction pattern out of every test that
    doesn't care about the specific values, while still letting the
    tests that do care override them.
    """
    return ArticleContainer(
        char_width_measurer=_fixed_measurer(char_w),
        line_height_measurer=_fixed_measurer(line_h),
    )


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
class ArticleContainerWidthGettersTests(unittest.TestCase):
    """The container exposes two width getters and two unit getters.

    * ``text_column_width`` is the inner *text-area* width — the
      66-character reading column the renderer lays tables and images
      against.
    * ``outer_column_width`` is the widget's actual width — the text
      area plus the inner horizontal padding on both sides. Used by
      :meth:`do_measure` and :meth:`do_size_allocate`.
    * ``char_width_px`` and ``line_height_px`` are the cached measured
      values that :class:`NoteView` reads when setting the four
      TextView margins.
    """

    def test_text_column_width_is_target_chars_times_m_width(self) -> None:
        container = _make_test_article_container(char_w=10, line_h=20)
        self.assertEqual(
            container.text_column_width(),
            TARGET_CHARS_PER_LINE * 10,
        )

    def test_outer_column_width_includes_horizontal_padding_on_both_sides(
        self,
    ) -> None:
        # outer = (66 + 2 × 8) × 10 = 820. The padding-aware width is
        # what the size-allocation and measurement vfuncs use; the text
        # area inside it remains 66 × char_w.
        container = _make_test_article_container(char_w=10, line_h=20)
        expected = (TARGET_CHARS_PER_LINE + 2 * ARTICLE_INNER_HPADDING_CHARS) * 10
        self.assertEqual(container.outer_column_width(), expected)

    def test_outer_minus_text_is_exactly_two_sides_of_padding(self) -> None:
        # The whole-point invariant of this change: the padding is
        # absorbed by the column's outer width, so the 66-char text
        # area is preserved. ``outer - text`` must be exactly
        # ``2 × ARTICLE_INNER_HPADDING_CHARS × char_w``.
        container = _make_test_article_container(char_w=10, line_h=20)
        slack = container.outer_column_width() - container.text_column_width()
        self.assertEqual(slack, 2 * ARTICLE_INNER_HPADDING_CHARS * 10)

    def test_line_height_px_returns_measured_value(self) -> None:
        container = _make_test_article_container(char_w=10, line_h=20)
        self.assertEqual(container.line_height_px(), 20)

    def test_char_width_px_returns_measured_value(self) -> None:
        container = _make_test_article_container(char_w=10, line_h=20)
        self.assertEqual(container.char_width_px(), 10)

    def test_non_positive_char_width_uses_fallback(self) -> None:
        # A real font's "M" is never zero pixels wide; the fallback
        # exists for the corner case (measuring before the widget has
        # any font at all). A zero result must yield a usable
        # column, not a zero-pixel one.
        container = ArticleContainer(
            char_width_measurer=_fixed_measurer(0),
            line_height_measurer=_fixed_measurer(20),
        )
        self.assertEqual(container.char_width_px(), _FALLBACK_CHAR_WIDTH_PX)
        self.assertEqual(
            container.text_column_width(),
            TARGET_CHARS_PER_LINE * _FALLBACK_CHAR_WIDTH_PX,
        )

    def test_negative_char_width_uses_fallback(self) -> None:
        container = ArticleContainer(
            char_width_measurer=_fixed_measurer(-3),
            line_height_measurer=_fixed_measurer(20),
        )
        self.assertEqual(container.char_width_px(), _FALLBACK_CHAR_WIDTH_PX)

    def test_non_positive_line_height_uses_fallback(self) -> None:
        # Symmetric to char_width: a zero measurement must yield the
        # fallback line height so the container's vertical metrics
        # remain usable.
        container = ArticleContainer(
            char_width_measurer=_fixed_measurer(10),
            line_height_measurer=_fixed_measurer(0),
        )
        self.assertEqual(container.line_height_px(), _FALLBACK_LINE_HEIGHT_PX)

    def test_negative_line_height_uses_fallback(self) -> None:
        container = ArticleContainer(
            char_width_measurer=_fixed_measurer(10),
            line_height_measurer=_fixed_measurer(-5),
        )
        self.assertEqual(container.line_height_px(), _FALLBACK_LINE_HEIGHT_PX)

    def test_measurers_are_invoked_at_most_once(self) -> None:
        # Locks in the caching invariant for both measurers. Calling
        # every getter ten times must still result in exactly one
        # invocation per measurer.
        char_calls: list[None] = []
        line_calls: list[None] = []

        def char_measure() -> int:
            char_calls.append(None)
            return 10

        def line_measure() -> int:
            line_calls.append(None)
            return 20

        container = ArticleContainer(
            char_width_measurer=char_measure,
            line_height_measurer=line_measure,
        )
        for _ in range(10):
            container.text_column_width()
            container.outer_column_width()
            container.char_width_px()
            container.line_height_px()

        self.assertEqual(len(char_calls), 1)
        self.assertEqual(len(line_calls), 1)


class _CapturingChild(Gtk.Widget):
    """A bare :class:`Gtk.Widget` that records its last allocate / measure call.

    Plugged into the size-allocate tests as :class:`ArticleContainer`'s
    single child so the tests can assert what arguments the container
    passes through :meth:`Gtk.Widget.allocate` (width / height /
    baseline / transform) and :meth:`Gtk.Widget.measure` (orientation /
    for-size) on it.

    The Python overrides of :meth:`allocate` and :meth:`measure`
    intercept the calls *before* they reach the C implementation, so
    no real layout work happens — the recorded args are the
    container's outputs verbatim. A reported height is returned from
    :meth:`measure` so the vertical-forwarding test has something
    deterministic to assert on.
    """

    recorded_allocate_calls: list[tuple[int, int, int, Gsk.Transform | None]]
    recorded_measure_calls: list[tuple[Gtk.Orientation, int]]
    _reported_vertical_height: int

    def __init__(self, *, reported_vertical_height: int = 0) -> None:
        super().__init__()
        self.recorded_allocate_calls = []
        self.recorded_measure_calls = []
        self._reported_vertical_height = reported_vertical_height

    def allocate(  # pylint: disable=arguments-differ
        self,
        width: int,
        height: int,
        baseline: int,
        transform: Gsk.Transform | None,
    ) -> None:
        self.recorded_allocate_calls.append((width, height, baseline, transform))

    def measure(  # pylint: disable=arguments-differ
        self,
        orientation: Gtk.Orientation,
        for_size: int,
    ) -> tuple[int, int, int, int]:
        self.recorded_measure_calls.append((orientation, for_size))
        if orientation == Gtk.Orientation.VERTICAL:
            h = self._reported_vertical_height
            return (h, h, -1, -1)
        return (0, 0, -1, -1)


def _transform_x_offset(transform: Gsk.Transform | None) -> int:
    """Extract the X translation of ``transform`` (or 0 for ``None``).

    Reads the affine 2-D components via :meth:`Gsk.Transform.to_2d`;
    the fifth value is ``dx``. Tests assert on the offset because the
    transform's identity isn't otherwise observable — the container's
    contract is "the child appears at X = offset", not "the container
    uses this particular ``Gsk.Transform`` object".
    """
    if transform is None:
        return 0
    _xx, _yx, _xy, _yy, dx, _dy = transform.to_2d()
    return int(dx)


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerBaseClassTests(unittest.TestCase):
    """Lock the base class so the GTK 4 ``Gtk.Box``-can't-override-vfuncs
    regression cannot reappear.

    ``Gtk.Box`` delegates :meth:`measure` / :meth:`size_allocate` to its
    ``BoxLayout`` layout manager at the C level, which means Python
    overrides of :meth:`do_measure` / :meth:`do_size_allocate` on a
    ``Gtk.Box`` subclass are dead code — the unit tests would pass
    (the methods exist and run when called directly) while the live
    widget behaved like a plain ``Gtk.Box``. The fix is to subclass
    :class:`Gtk.Widget` instead; this test asserts that base class.
    """

    def test_article_container_is_a_gtk_widget_not_a_gtk_box(self) -> None:
        container = _make_test_article_container()
        self.assertIsInstance(container, Gtk.Widget)
        self.assertNotIsInstance(container, Gtk.Box)


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerSizeAllocateTests(unittest.TestCase):
    """Pin the column-width rule from §10 of the plan.

    A wide allocation centres the column by offsetting the child via a
    translate-X :class:`Gsk.Transform`; a narrow or exact allocation
    leaves the offset at 0 (the parent :class:`Gtk.ScrolledWindow` is
    responsible for the horizontal scrollbar in that case — the test
    does not assert on that). In every case, the child is allocated
    exactly :meth:`ArticleContainer.outer_column_width` pixels wide —
    that is the column-pinning invariant.

    The assertions read the offset back from the recorded transform via
    :func:`_transform_x_offset`; the container's contract is "the child
    appears at X = offset", not "the container constructs this
    particular ``Gsk.Transform`` object", so the tests check the
    observable effect rather than object identity.
    """

    def _container_with_capturing_child(
        self,
        *,
        char_w: int = 10,
        line_h: int = 20,
    ) -> tuple[ArticleContainer, _CapturingChild]:
        container = _make_test_article_container(char_w=char_w, line_h=line_h)
        child = _CapturingChild()
        container.set_child(child)
        return container, child

    def test_wide_window_centres_child_with_half_slack_offset(self) -> None:
        container, child = self._container_with_capturing_child()
        outer = container.outer_column_width()
        allocated = outer + 200  # 200 px of slack
        container.do_size_allocate(allocated, 600, -1)

        self.assertEqual(len(child.recorded_allocate_calls), 1)
        width, height, baseline, transform = child.recorded_allocate_calls[0]
        self.assertEqual(width, outer)
        self.assertEqual(height, 600)
        self.assertEqual(baseline, -1)
        self.assertEqual(_transform_x_offset(transform), (allocated - outer) // 2)

    def test_narrow_window_places_child_at_zero_offset(self) -> None:
        container, child = self._container_with_capturing_child()
        outer = container.outer_column_width()
        # Narrower than the outer target — column does not shrink; the
        # outer ScrolledWindow is responsible for the scrollbar (out of
        # scope here).
        container.do_size_allocate(outer - 200, 600, -1)

        self.assertEqual(len(child.recorded_allocate_calls), 1)
        width, _height, _baseline, transform = child.recorded_allocate_calls[0]
        # Column-pinning invariant: child is allocated outer wide even
        # though the parent gave us less.
        self.assertEqual(width, outer)
        self.assertEqual(_transform_x_offset(transform), 0)
        # ``None`` is the GTK 4 idiom for "no transform"; verify the
        # zero-offset path takes that fast-path.
        self.assertIsNone(transform)

    def test_exact_outer_width_places_child_at_zero_offset(self) -> None:
        # The boundary: allocated == outer → no slack to absorb. The
        # ``>`` (strict) check in the implementation is what produces
        # this; ``>=`` would produce a 0-offset allocation here too,
        # but the strict form makes the equality case explicit.
        container, child = self._container_with_capturing_child()
        outer = container.outer_column_width()
        container.do_size_allocate(outer, 600, -1)

        self.assertEqual(len(child.recorded_allocate_calls), 1)
        _width, _height, _baseline, transform = child.recorded_allocate_calls[0]
        self.assertEqual(_transform_x_offset(transform), 0)
        self.assertIsNone(transform)

    def test_repeated_allocate_with_same_width_is_stable(self) -> None:
        # The implementation does not require an idempotence guard
        # (it doesn't write ``self.margin-*``, only allocates the
        # child) — every call produces the same offset against the
        # same width.
        container, child = self._container_with_capturing_child()
        outer = container.outer_column_width()
        allocated = outer + 80
        for _ in range(3):
            container.do_size_allocate(allocated, 400, -1)

        self.assertEqual(len(child.recorded_allocate_calls), 3)
        expected_offset = (allocated - outer) // 2  # 40
        for width, _height, _baseline, transform in child.recorded_allocate_calls:
            self.assertEqual(width, outer)
            self.assertEqual(_transform_x_offset(transform), expected_offset)

    def test_widening_then_narrowing_resets_offset_to_zero(self) -> None:
        container, child = self._container_with_capturing_child()
        outer = container.outer_column_width()
        # Wide first.
        container.do_size_allocate(outer + 300, 600, -1)
        _w0, _h0, _b0, transform_wide = child.recorded_allocate_calls[-1]
        self.assertGreater(_transform_x_offset(transform_wide), 0)
        # Then narrow — offset must drop back to 0.
        container.do_size_allocate(outer - 100, 600, -1)
        _w1, _h1, _b1, transform_narrow = child.recorded_allocate_calls[-1]
        self.assertEqual(_transform_x_offset(transform_narrow), 0)
        self.assertIsNone(transform_narrow)

    def test_child_always_allocated_outer_column_width_pixels_wide(
        self,
    ) -> None:
        # The column-pinning invariant: across wide, exact, and narrow
        # allocations, the width passed to the child is always exactly
        # :meth:`outer_column_width`. The parent allocation's slack is
        # absorbed by the offset, not by stretching or shrinking the
        # child.
        container, child = self._container_with_capturing_child()
        outer = container.outer_column_width()
        for parent_width in (outer - 200, outer, outer + 50, outer + 500):
            with self.subTest(parent_width=parent_width):
                child.recorded_allocate_calls.clear()
                container.do_size_allocate(parent_width, 500, -1)
                self.assertEqual(len(child.recorded_allocate_calls), 1)
                width, _h, _b, _t = child.recorded_allocate_calls[0]
                self.assertEqual(width, outer)

    def test_allocate_is_a_no_op_when_no_child_is_set(self) -> None:
        # Defensive path: the container is constructible without a
        # child (production sets one immediately, but unit tests build
        # one without). Allocating must not raise.
        container = _make_test_article_container(char_w=10, line_h=20)
        outer = container.outer_column_width()
        # No assertion target beyond "does not raise" — the
        # implementation has nothing to delegate to.
        container.do_size_allocate(outer + 100, 600, -1)


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerTeardownTests(unittest.TestCase):
    """Pin the teardown unparent that silences the GTK 4 finalize warning.

    :class:`ArticleContainer` parents its single child manually via
    :meth:`Gtk.Widget.set_parent`, so — unlike a ``Gtk.Box``, whose
    layout manager disposes of children for it — it must unparent that
    child itself before being finalized, or GTK prints *"Finalizing …
    but it still has children left"*. PyGObject does not expose
    ``GObject``'s ``dispose`` vfunc, so the container does this from
    :meth:`ArticleContainer.do_unroot` for the rooted (production) path
    and from :meth:`ArticleContainer.__del__` for a container that is
    finalized without ever being rooted (the standalone widgets these
    tests build). Both routes are exercised here.
    """

    def test_unroot_unparents_the_child(self) -> None:
        # The rooted path: adding the container to a window and then
        # destroying the window unroots it, which must drop the child.
        container = _make_test_article_container(char_w=10, line_h=20)
        child = _CapturingChild()
        container.set_child(child)
        window = Gtk.Window()
        window.set_child(container)
        self.assertIs(child.get_parent(), container)

        window.set_child(None)  # unroots the container

        self.assertIsNone(child.get_parent())
        self.assertIsNone(container.get_first_child())
        window.destroy()

    def test_release_child_is_idempotent(self) -> None:
        # Both teardown hooks call the same guarded helper; calling it
        # twice (as do_unroot + __del__ can) must not double-unparent.
        container = _make_test_article_container(char_w=10, line_h=20)
        child = _CapturingChild()
        container.set_child(child)

        container._release_child()
        container._release_child()  # second pass is a guarded no-op

        self.assertIsNone(child.get_parent())
        self.assertIsNone(container.get_first_child())

    def test_release_child_with_no_child_is_a_no_op(self) -> None:
        # The container is constructible without a child; releasing in
        # that state must not raise.
        container = _make_test_article_container(char_w=10, line_h=20)
        container._release_child()
        self.assertIsNone(container.get_first_child())

    def test_standalone_container_unparents_child_on_finalize(self) -> None:
        # The never-rooted path the rest of this test module hits: build
        # a container with a child, drop the only reference, force a GC
        # pass, and confirm the child is no longer parented (which is
        # what stops the finalize warning). The child is kept alive via
        # a weakref-free local so the assertion can read its parent
        # after the container is gone.
        child = _CapturingChild()
        container = _make_test_article_container(char_w=10, line_h=20)
        container.set_child(child)
        self.assertIs(child.get_parent(), container)

        del container
        gc.collect()

        self.assertIsNone(child.get_parent())


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerMeasureTests(unittest.TestCase):
    """The horizontal-measurement override is what makes the column
    not shrink: the parent ``ScrolledWindow``'s viewport never
    allocates us less than the reported minimum, so a narrow window
    triggers a horizontal scrollbar instead of a smaller column.

    The reported width is :meth:`outer_column_width` (text + inner
    padding), not the bare text width — the padding is part of the
    column from the layout's perspective.

    The vertical-measurement override forwards to the single child at
    the *outer* column width — the width the child will actually be
    allocated, so its height computation matches the column it
    renders into.
    """

    def test_horizontal_minimum_and_natural_equal_outer_column_width(
        self,
    ) -> None:
        container = _make_test_article_container(char_w=10, line_h=20)
        outer = container.outer_column_width()
        minimum, natural, _, _ = container.do_measure(
            Gtk.Orientation.HORIZONTAL, -1
        )
        self.assertEqual(minimum, outer)
        self.assertEqual(natural, outer)

    def test_horizontal_measurement_is_independent_of_for_size(self) -> None:
        # The horizontal width is fixed by the column rule — it must
        # not vary with the cross-axis hint.
        container = _make_test_article_container(char_w=10, line_h=20)
        outer = container.outer_column_width()
        for for_size in (-1, 0, 100, 5000):
            minimum, natural, _, _ = container.do_measure(
                Gtk.Orientation.HORIZONTAL, for_size,
            )
            self.assertEqual(minimum, outer)
            self.assertEqual(natural, outer)

    def test_hexpand_is_set_so_wide_viewport_overshoots_natural(self) -> None:
        # ``hexpand`` is what tells a wide ``Viewport`` to allocate us
        # more than our natural width — the precondition for the
        # centring branch in ``do_size_allocate``.
        container = _make_test_article_container(char_w=10, line_h=20)
        self.assertTrue(container.get_hexpand())

    def test_vertical_measure_with_no_child_returns_zero(self) -> None:
        # No child to measure → nothing to contribute. The horizontal
        # path stays at ``outer_column_width``; only the vertical path
        # is affected.
        container = _make_test_article_container(char_w=10, line_h=20)
        minimum, natural, baseline_min, baseline_nat = container.do_measure(
            Gtk.Orientation.VERTICAL, -1,
        )
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, 0)
        self.assertEqual(baseline_min, -1)
        self.assertEqual(baseline_nat, -1)

    def test_vertical_measure_forwards_to_child_at_outer_column_width(
        self,
    ) -> None:
        # Width reported by the child is what the container reports
        # vertically; ``for_size`` from the parent is capped to
        # ``outer_column_width`` so the child wraps against the column
        # it will be allocated, not against the parent's wider
        # viewport.
        container = _make_test_article_container(char_w=10, line_h=20)
        child = _CapturingChild(reported_vertical_height=123)
        container.set_child(child)
        outer = container.outer_column_width()

        minimum, natural, _, _ = container.do_measure(
            Gtk.Orientation.VERTICAL, outer + 500,
        )
        self.assertEqual(minimum, 123)
        self.assertEqual(natural, 123)
        # for_size > outer → capped to outer.
        self.assertEqual(
            child.recorded_measure_calls,
            [(Gtk.Orientation.VERTICAL, outer)],
        )

    def test_vertical_measure_passes_outer_when_for_size_is_unconstrained(
        self,
    ) -> None:
        # ``for_size == -1`` is GTK's "no horizontal constraint" hint;
        # the container substitutes ``outer_column_width`` because that
        # is what the child will actually be allocated.
        container = _make_test_article_container(char_w=10, line_h=20)
        child = _CapturingChild(reported_vertical_height=77)
        container.set_child(child)
        outer = container.outer_column_width()

        container.do_measure(Gtk.Orientation.VERTICAL, -1)
        self.assertEqual(
            child.recorded_measure_calls,
            [(Gtk.Orientation.VERTICAL, outer)],
        )

    def test_vertical_measure_passes_for_size_when_narrower_than_outer(
        self,
    ) -> None:
        # When the parent's hint is narrower than the column the
        # container forwards it as-is — there is no point asking the
        # child for a height at a width it will never see.
        container = _make_test_article_container(char_w=10, line_h=20)
        child = _CapturingChild(reported_vertical_height=42)
        container.set_child(child)
        outer = container.outer_column_width()
        narrower = outer - 50

        container.do_measure(Gtk.Orientation.VERTICAL, narrower)
        self.assertEqual(
            child.recorded_measure_calls,
            [(Gtk.Orientation.VERTICAL, narrower)],
        )


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


def _find_text_view(view: NoteView) -> Gtk.TextView:
    """Walk the widget tree and pull out the inner :class:`Gtk.TextView`.

    The structure is ``NoteView → [Revealer →] ScrolledWindow →
    [Viewport →] ArticleContainer → TextView``. From step 16 onwards
    the parse-error banner sits in a :class:`Gtk.Revealer` *prepended*
    to the vertical box, so the helper walks past it. The
    ``ScrolledWindow`` is the box's *next* sibling.
    ``Gtk.ScrolledWindow`` wraps any non-:class:`Gtk.Scrollable`
    child in a :class:`Gtk.Viewport` automatically; since
    :class:`ArticleContainer` is a ``Gtk.Widget`` (not natively
    scrollable), the viewport is always present in production and
    the helper steps past it.

    We walk ``get_first_child`` / ``get_next_sibling`` / ``get_child``
    rather than reaching into private attributes of :class:`NoteView`,
    so the test stays agnostic to its internal field names. Returning
    the ``Gtk.TextView`` itself lets margin-wiring tests read the four
    margin properties via the documented public API.
    """
    first = view.get_first_child()
    # The banner revealer (step 16+) is the very first child. Hop
    # past it; the ScrolledWindow follows.
    if isinstance(first, Gtk.Revealer):
        scrolled = first.get_next_sibling()
    else:
        scrolled = first
    assert isinstance(scrolled, Gtk.ScrolledWindow), (
        f"expected a ScrolledWindow in the NoteView stack, "
        f"got {type(scrolled).__name__}"
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
    return text_view


def _find_text_view_buffer(view: NoteView) -> Gtk.TextBuffer:
    """Return the rendered TextView's buffer.

    Thin wrapper over :func:`_find_text_view` that exists because most
    of the existing tests reach for the buffer rather than the widget.
    """
    return _find_text_view(view).get_buffer()


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


# ---------------------------------------------------------------------------
# NoteView margin wiring + renderer column-width wiring
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
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
            return NoteView(note_repository=repo, app_state=state)

    def test_textview_top_margin_is_four_line_heights(self) -> None:
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        self.assertEqual(
            text_view.get_top_margin(),
            ARTICLE_TOP_MARGIN_LINES * 20,
        )

    def test_textview_bottom_margin_is_four_line_heights(self) -> None:
        view = self._build_view_with_stubbed_font(char_w=10, line_h=20)
        text_view = _find_text_view(view)
        self.assertEqual(
            text_view.get_bottom_margin(),
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


@unittest.skipUnless(_display_available(), "no GDK display")
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
            return NoteView(note_repository=repo, app_state=state)

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


@unittest.skipUnless(_display_available(), "no GDK display")
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
            return NoteView(note_repository=repo, app_state=state)

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


# ---------------------------------------------------------------------------
# _message_for: exhaustiveness and content
# ---------------------------------------------------------------------------


class MessageForTests(unittest.TestCase):
    """Pin the user-facing message helper used by the parse-error
    banner. Exhaustiveness over :class:`ParseErrorKind` is enforced
    so a new error kind cannot ship without a banner message.
    """

    def test_every_parse_error_kind_has_a_message(self) -> None:
        # Iterating the enum is what makes this an exhaustiveness
        # check — a member with no entry in ``_message_for`` would
        # raise on the ``match`` (pattern-match exhaustiveness via
        # the missing case at runtime is by design here, since
        # Python doesn't enforce exhaustiveness at type-check time
        # for non-Literal enums without external tooling).
        for kind in ParseErrorKind:
            with self.subTest(kind=kind):
                message = _message_for(kind, 42)
                self.assertIsInstance(message, str)
                self.assertTrue(message)
                # The line number must appear in the message — the
                # banner is the only context the user has, so the
                # location has to be visible.
                self.assertIn("42", message)

    def test_unsupported_link_scheme_message_lists_supported_schemes(self) -> None:
        # The Sourdough note's specific failure is on this kind, so
        # pin its content explicitly.
        message = _message_for(ParseErrorKind.UNSUPPORTED_LINK_SCHEME, 39)
        self.assertIn("39", message)
        for scheme in ("http", "https", "mailto"):
            self.assertIn(scheme, message)

    def test_message_does_not_leak_internal_message(self) -> None:
        # Smoke check: the developer-oriented strings (square
        # brackets around `cols=` or specific quotes) don't leak
        # into the user-facing copy. The banner is consumer copy,
        # not a developer dump.
        message = _message_for(ParseErrorKind.BAD_COLS_DIRECTIVE, 7)
        self.assertNotIn("'", message)


# ---------------------------------------------------------------------------
# Parse-error banner integration with refresh
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteViewErrorBannerTests(unittest.TestCase):
    """The parse-error banner is hidden by default, revealed on
    parse failure with a kind-specific message, and re-hidden when
    the user navigates to a parseable note."""

    def test_banner_hidden_initially_with_no_selection(self) -> None:
        # No note selected at construction → the banner must be
        # hidden, the buffer empty.
        repo = _FakeNoteRepository()
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)
        self.assertFalse(view.error_banner_visible)
        self.assertEqual(view.error_banner_text, "")

    def test_banner_hidden_on_successful_render(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")  # parses cleanly
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)
        app_state.set_selected_note_id("note-A")
        self.assertFalse(view.error_banner_visible)
        self.assertEqual(view.error_banner_text, "")

    def test_banner_revealed_on_parse_error(self) -> None:
        # A note whose source raises ParseError makes the banner
        # appear with a kind-specific message AND clears the buffer.
        repo = _FakeNoteRepository()
        # `:bad name:` lexes as a LineToken; the parser raises
        # BAD_ATTRIBUTE_ENTRY against it.
        repo.notes["note-A"] = _make_note(
            "note-A",
            source=":bad name: value\n",
        )
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)
        app_state.set_selected_note_id("note-A")

        self.assertTrue(view.error_banner_visible)
        self.assertIn("Line 1", view.error_banner_text)
        # Buffer must be empty so no stale content sits below the
        # banner.
        buffer = _find_text_view_buffer(view)
        self.assertEqual(
            buffer.get_text(
                buffer.get_start_iter(),
                buffer.get_end_iter(),
                False,
            ),
            "",
        )

    def test_banner_message_reflects_specific_error_kind(self) -> None:
        # Different parse-error kinds produce different messages.
        repo = _FakeNoteRepository()
        # Unsupported link scheme — what the Sourdough fixture
        # actually trips on, after the parser fixes have landed.
        repo.notes["note-A"] = _make_note(
            "note-A",
            source="link:ftp://example.com[click]\n",
        )
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)
        app_state.set_selected_note_id("note-A")

        self.assertTrue(view.error_banner_visible)
        text = view.error_banner_text
        # The message says it's a link-scheme problem and lists the
        # supported schemes.
        self.assertIn("scheme", text)
        for scheme in ("http", "https", "mailto"):
            self.assertIn(scheme, text)

    def test_banner_recovers_when_selecting_clean_note(self) -> None:
        # After a parse-error display, navigating to a parseable
        # note re-hides the banner — banner state and buffer state
        # stay in lockstep with the current selection.
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note(
            "bad", source="link:javascript:alert(1)[x]\n",
        )
        repo.notes["good"] = _make_note("good")
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)

        app_state.set_selected_note_id("bad")
        self.assertTrue(view.error_banner_visible)

        app_state.set_selected_note_id("good")
        self.assertFalse(view.error_banner_visible)
        self.assertEqual(view.error_banner_text, "")

    def test_banner_hidden_when_selection_clears_after_error(self) -> None:
        # After a parse error, clearing the selection (None) must
        # also hide the banner — the user is no longer looking at a
        # note at all.
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note(
            "bad", source="*unclosed bold\n",
        )
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)
        app_state.set_selected_note_id("bad")
        self.assertTrue(view.error_banner_visible)

        app_state.set_selected_note_id(None)
        self.assertFalse(view.error_banner_visible)

    def test_banner_hidden_when_selection_points_to_missing_note(self) -> None:
        # A stale id (note deleted in another window) clears the
        # banner just like a None selection — the user gets neither
        # stale content nor a stale error.
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note(
            "bad", source="*unclosed\n",
        )
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)
        app_state.set_selected_note_id("bad")
        self.assertTrue(view.error_banner_visible)

        app_state.set_selected_note_id("does-not-exist")
        self.assertFalse(view.error_banner_visible)

    def test_navigating_to_bad_note_does_not_show_stale_content(self) -> None:
        # The plan's specific concern: the user clicks a note that
        # doesn't parse and sees the *previous* note's render.
        # After the fix, the buffer is empty.
        repo = _FakeNoteRepository()
        repo.notes["good"] = _make_note(
            "good", source="= Welcome\n\nIts contents.\n",
        )
        repo.notes["bad"] = _make_note(
            "bad", source="link:bogus://x[t]\n",
        )
        app_state = AppState()
        view = NoteView(note_repository=repo, app_state=app_state)

        app_state.set_selected_note_id("good")
        buffer = _find_text_view_buffer(view)
        good_text = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False,
        )
        self.assertIn("Welcome", good_text)

        app_state.set_selected_note_id("bad")
        bad_text = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False,
        )
        # Buffer has been cleared — no leftover from "good".
        self.assertEqual(bad_text, "")
        self.assertNotIn("Welcome", bad_text)
        # And the banner explains what happened.
        self.assertTrue(view.error_banner_visible)


# ---------------------------------------------------------------------------
# _ArticleTextView wash-painting tests
# ---------------------------------------------------------------------------
#
# The :meth:`_ArticleTextView._compute_wash_rects` method is the test
# seam: it returns the list of ``(colour, rect)`` pairs the snapshot
# painter would append, without driving GTK's snapshot machinery. The
# tests below exercise it directly. The plain :class:`Gtk.TextView`
# methods this seam calls (``get_line_yrange``,
# ``buffer_to_window_coords``, ``get_width``, ``get_left_margin``,
# ``get_right_margin``) require a realised widget, so these tests are
# display-gated like the rest of the widget tests in this module.


def _build_article_text_view_with_buffer() -> tuple[
    _ArticleTextView, Gtk.TextBuffer, Gtk.TextTagTable,
]:
    """Construct a wired :class:`_ArticleTextView` for direct testing.

    Builds a tag table (with the same M-width fake used elsewhere in
    this module, ``9``), attaches a buffer to a fresh
    :class:`_ArticleTextView`, and installs the wash specs translated
    to :class:`Gtk.TextTag` keys — the exact wiring
    :class:`NoteView` performs. Returns the trio so individual tests
    can populate the buffer with tagged content and probe the
    painter.
    """
    table = build_tag_table(char_width_px=9)
    text_view = _ArticleTextView()
    buffer = Gtk.TextBuffer.new(table)
    text_view.set_buffer(buffer)
    specs_by_tag: dict[Gtk.TextTag, WashSpec] = {}
    for tag_name, spec in build_wash_specs().items():
        tag = table.lookup(tag_name.value)
        if tag is not None:
            specs_by_tag[tag] = spec
    text_view.install_wash_specs(specs_by_tag)
    return text_view, buffer, table


def _apply_tag_across_line(
    buffer: Gtk.TextBuffer, line_no: int, tag_name: str,
) -> None:
    """Apply a tag across the entire content of one logical line.

    The painter walks logical lines and checks the first iter on each;
    applying the tag from the line start to the next line's start (or
    end-of-buffer) is the minimum needed for :func:`_spec_at_iter` to
    find it on that line.
    """
    ok, start = buffer.get_iter_at_line(line_no)
    assert ok, f"line {line_no} should exist"
    if line_no + 1 < buffer.get_line_count():
        ok_next, end = buffer.get_iter_at_line(line_no + 1)
        assert ok_next, f"line {line_no + 1} should exist"
    else:
        end = buffer.get_end_iter()
    buffer.apply_tag_by_name(tag_name, start, end)


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleTextViewWashRectTests(unittest.TestCase):
    """Drive :meth:`_ArticleTextView._compute_wash_rects` directly.

    Without wash-bearing tags applied, the painter must produce no
    rects (empty buffer included). With one wash-bearing tag applied
    to one logical line, exactly one rect appears, and it carries
    the tag's tint. Two different wash-bearing tags on two different
    lines produce two rects with two different tints. The
    blockquote-attribution tag has no wash spec — applying it must
    not produce a rect.

    Geometric assertions are deliberately limited to invariants that
    don't depend on real font rendering: the *count* of rects, the
    *colour* of each, and the fact that the rect is non-empty. The
    exact pixel positions depend on the live :class:`Gtk.TextView`'s
    allocated width and font metrics, which vary by environment.
    """

    def test_empty_buffer_produces_no_rects(self) -> None:
        text_view, _buffer, _table = _build_article_text_view_with_buffer()
        self.assertEqual(text_view._compute_wash_rects(), [])

    def test_buffer_with_no_wash_tags_produces_no_rects(self) -> None:
        # The painter looks for wash-bearing tags only; plain text
        # gets nothing painted behind it.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("just a plain paragraph with no tags\n")
        self.assertEqual(text_view._compute_wash_rects(), [])

    def test_one_admonition_body_paragraph_produces_one_rect(self) -> None:
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("body of the admonition\n")
        _apply_tag_across_line(
            buffer, 0, admonition_body_tag_name(AdmonitionKind.NOTE).value,
        )
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 1)
        # The colour must match the NOTE admonition's tint — the
        # painter uses the spec's tint verbatim.
        expected_color = _rgba_from_tint(
            build_wash_specs()[
                admonition_body_tag_name(AdmonitionKind.NOTE)
            ].tint
        )
        color, _rect = rects[0]
        self.assertEqual(color.red, expected_color.red)
        self.assertEqual(color.green, expected_color.green)
        self.assertEqual(color.blue, expected_color.blue)
        self.assertEqual(color.alpha, expected_color.alpha)

    def test_two_different_wash_tags_produce_rects_with_different_tints(
        self,
    ) -> None:
        # An admonition on one line plus a blockquote on another must
        # both be painted — and their tints must differ (they do, by
        # design: admonitions are per-kind colours, blockquotes are
        # grey).
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("admonition body\nblockquote body\n")
        _apply_tag_across_line(
            buffer, 0, admonition_body_tag_name(AdmonitionKind.NOTE).value,
        )
        _apply_tag_across_line(buffer, 1, TagName.BLOCKQUOTE_BODY.value)
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 2)
        color_a, _rect_a = rects[0]
        color_b, _rect_b = rects[1]
        # At minimum the alpha or one of the RGB channels must
        # differ — the two tints are not identical.
        self.assertNotEqual(
            (color_a.red, color_a.green, color_a.blue, color_a.alpha),
            (color_b.red, color_b.green, color_b.blue, color_b.alpha),
        )

    def test_blockquote_attribution_line_produces_no_rect(self) -> None:
        # The attribution paragraph carries a tag that the wash-spec
        # map deliberately omits — the painter must paint nothing
        # behind it.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("— Author, Source\n")
        _apply_tag_across_line(buffer, 0, TagName.BLOCKQUOTE_ATTRIBUTION.value)
        self.assertEqual(text_view._compute_wash_rects(), [])

    def test_no_wash_specs_installed_produces_no_rects(self) -> None:
        # The default state of :class:`_ArticleTextView` (before
        # :meth:`install_wash_specs` is called) is "no specs", so the
        # painter is a no-op. This is the right behaviour for tests
        # that construct the subclass standalone, and for the brief
        # window between constructor and wash-spec install.
        table = build_tag_table(char_width_px=9)
        text_view = _ArticleTextView()
        buffer = Gtk.TextBuffer.new(table)
        text_view.set_buffer(buffer)
        buffer.set_text("anything\n")
        _apply_tag_across_line(
            buffer, 0, admonition_body_tag_name(AdmonitionKind.NOTE).value,
        )
        # No specs installed → painter never finds a matching tag.
        self.assertEqual(text_view._compute_wash_rects(), [])


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleTextViewMutualExclusionTests(unittest.TestCase):
    """Defensive: two wash-bearing tags on one iter must raise.

    Block-level wash-bearing tags are mutually exclusive by parser
    construction (admonition body, blockquote body, and code block
    cannot apply to the same paragraph). If a future code path
    violates that invariant, :meth:`_compute_wash_rects` raises
    :class:`ValueError` rather than silently picking one of the
    overlapping tags.
    """

    def test_two_wash_tags_on_one_iter_raises_value_error(self) -> None:
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("overlapping tags\n")
        _apply_tag_across_line(
            buffer, 0, admonition_body_tag_name(AdmonitionKind.NOTE).value,
        )
        _apply_tag_across_line(buffer, 0, TagName.BLOCKQUOTE_BODY.value)
        with self.assertRaises(ValueError):
            text_view._compute_wash_rects()


class RgbaFromTintTests(unittest.TestCase):
    """The tint→Gdk.RGBA helper is pure and display-independent."""

    def test_components_round_trip(self) -> None:
        rgba = _rgba_from_tint((0.1, 0.2, 0.3, 0.4))
        self.assertAlmostEqual(rgba.red, 0.1, places=6)
        self.assertAlmostEqual(rgba.green, 0.2, places=6)
        self.assertAlmostEqual(rgba.blue, 0.3, places=6)
        self.assertAlmostEqual(rgba.alpha, 0.4, places=6)

    def test_returns_a_fresh_instance_each_call(self) -> None:
        # The painter appends one rect per logical line; sharing a
        # single :class:`Gdk.RGBA` instance across snapshot nodes
        # would risk one paint mutating the next. A fresh instance
        # per call keeps the snapshot nodes independent.
        a = _rgba_from_tint((0.5, 0.5, 0.5, 0.5))
        b = _rgba_from_tint((0.5, 0.5, 0.5, 0.5))
        self.assertIsNot(a, b)


if __name__ == "__main__":
    unittest.main()
