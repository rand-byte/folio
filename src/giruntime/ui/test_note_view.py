"""Tests for :mod:`ui.note_view`."""

from __future__ import annotations

import gc
import struct
import unittest
from tempfile import TemporaryDirectory
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from gi.repository import Gdk, GLib, GObject, Gsk, Gtk

from config.defaults import (
    ARTICLE_BOTTOM_MARGIN_LINES,
    ARTICLE_END_GAP_LINES,
    ARTICLE_INNER_HPADDING_CHARS,
    ARTICLE_TOP_MARGIN_LINES,
    TARGET_CHARS_PER_LINE,
)
from enums import (
    AdmonitionKind,
    AttachmentExportFailureReason,
    ParseErrorKind,
)
from storage.protocols import AttachmentExportFailed
from models.attachment import Attachment
from models.note import Note
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui import note_view as note_view_module
from giruntime.ui.note_render.tag_table import (
    TagName,
    admonition_body_tag_name,
    build_sheet_wash,
    build_tag_table,
    build_wash_specs,
)
from giruntime.ui.note_view import (
    ArticleContainer,
    CharWidthMeasurer,
    LineHeightMeasurer,
    NoteView,
    ArticleTextView,
    _FALLBACK_CHAR_WIDTH_PX,
    _FALLBACK_LINE_HEIGHT_PX,
    _HAIRLINE_THICKNESS_PX,
    _format_metadata_line,
    _message_for,
    _placeholder_image_bytes,
    _rgba_from_tint,
    _sheet_rect_for,
    build_article_surface,
)
from giruntime.ui._dates import format_date_long


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


def _solid_png(width: int, height: int) -> bytes:
    """Encode a minimal solid-colour RGB PNG of the given pixel size.

    The scrollbar regression test needs a *real, decodable* image whose
    last line is taller than the viewport. A hand-rolled encoder keeps the
    test self-contained (no Pillow / GdkPixbuf-save dependency) and lets it
    ask for an arbitrarily tall image; a solid fill compresses to a few
    bytes regardless of size. Colour type 2 is RGB, bit depth 8, no filter.
    """

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(
            ">I", crc
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + bytes((80, 120, 200)) * width
    image_data = zlib.compress(scanline * height, 9)
    return (
        signature
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", image_data)
        + _chunk(b"IEND", b"")
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

    def list_modified_since(self, _since: datetime) -> list[Note]:
        raise NotImplementedError

    def list_all(self) -> list[Note]:
        return list(self.notes.values())

    def search(self, _query: str) -> list[Note]:
        raise NotImplementedError

    def insert(self, _note: Note) -> Note:
        raise NotImplementedError

    def update_source(
        self,
        _note_id: str,
        _source: str,
        _modified_at: datetime,
    ) -> Note:
        raise NotImplementedError

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return ()


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
        # ``width >= outer`` centre branch runs and ``(outer - outer) //
        # 2`` is 0, so the child sits at offset 0 with no transform — the
        # same observable result as the narrow path's zero offset.
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
    """Pin the measure contract under Option C (the container is a
    ``Gtk.Scrollable``).

    Horizontally the *minimum* is ``0`` so the scrolled window may
    allocate the container narrower than the column — the container-owned
    ``hadjustment`` then drives the horizontal scrollbar — while the
    *natural* width is :meth:`outer_column_width` (text + inner padding),
    the column the pane opens at when there is room.

    Vertically the container contributes nothing (``(0, 0, …)``): the
    vertical extent is owned by the scrollable child (the text view), which
    the container wires up as the vertical scrollport by forwarding the
    ``vadjustment``. The container therefore never measures its child on
    the vertical axis — re-deriving the extent here would reinvent the
    viewport and could reintroduce the stale-extent bug.
    """

    def test_horizontal_minimum_is_zero_and_natural_is_outer_column_width(
        self,
    ) -> None:
        container = _make_test_article_container(char_w=10, line_h=20)
        outer = container.outer_column_width()
        minimum, natural, _, _ = container.do_measure(
            Gtk.Orientation.HORIZONTAL, -1
        )
        # Minimum 0 → the scrollable may be allocated narrower than the
        # column (the hadjustment exposes the overflow); natural is the
        # column width.
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, outer)

    def test_horizontal_measurement_is_independent_of_for_size(self) -> None:
        # The horizontal report is fixed by the column rule — it must
        # not vary with the cross-axis hint.
        container = _make_test_article_container(char_w=10, line_h=20)
        outer = container.outer_column_width()
        for for_size in (-1, 0, 100, 5000):
            minimum, natural, _, _ = container.do_measure(
                Gtk.Orientation.HORIZONTAL, for_size,
            )
            self.assertEqual(minimum, 0)
            self.assertEqual(natural, outer)

    def test_vertical_measure_with_no_child_returns_zero(self) -> None:
        # No child → nothing to contribute. The vertical axis is owned by
        # the forwarded text view, so the container reports zeroes
        # regardless.
        container = _make_test_article_container(char_w=10, line_h=20)
        minimum, natural, baseline_min, baseline_nat = container.do_measure(
            Gtk.Orientation.VERTICAL, -1,
        )
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, 0)
        self.assertEqual(baseline_min, -1)
        self.assertEqual(baseline_nat, -1)

    def test_vertical_measure_returns_zero_and_does_not_measure_child(
        self,
    ) -> None:
        # Option C: the vertical extent comes from the scrollable child
        # (it owns the forwarded ``vadjustment``), NOT from the container
        # measuring its child. The container must report ``(0, 0)`` and
        # must never call ``measure`` on the child vertically — doing so
        # is what reinvents the viewport.
        container = _make_test_article_container(char_w=10, line_h=20)
        child = _CapturingChild(reported_vertical_height=123)
        container.set_child(child)

        minimum, natural, _, _ = container.do_measure(
            Gtk.Orientation.VERTICAL, container.outer_column_width() + 500,
        )
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, 0)
        self.assertEqual(child.recorded_measure_calls, [])

    def test_vertical_measure_ignores_for_size(self) -> None:
        # The container's vertical report is constant ``(0, 0)`` whatever
        # the parent's cross-axis hint, and never touches the child.
        container = _make_test_article_container(char_w=10, line_h=20)
        child = _CapturingChild(reported_vertical_height=77)
        container.set_child(child)
        outer = container.outer_column_width()

        for for_size in (-1, 0, outer - 50, outer, outer + 500):
            with self.subTest(for_size=for_size):
                minimum, natural, _, _ = container.do_measure(
                    Gtk.Orientation.VERTICAL, for_size,
                )
                self.assertEqual((minimum, natural), (0, 0))
        self.assertEqual(child.recorded_measure_calls, [])


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerScrollableTests(unittest.TestCase):
    """Pin Option C: :class:`ArticleContainer` implements ``Gtk.Scrollable``.

    Implementing the interface is what makes the parent
    ``Gtk.ScrolledWindow`` keep the container as its *direct* child and
    interpose **no** ``Gtk.Viewport`` — the structural fix that removes the
    first-launch scrollbar bug. The container then treats the two axes
    differently: the vertical adjustment + policy are *forwarded* to the
    scrollable child (which owns the v-extent), while the horizontal axis is
    *owned* by the container (it configures the ``hadjustment`` and
    translates the fixed-width column itself).
    """

    def _capturing(
        self,
        *,
        char_w: int = 10,
        line_h: int = 20,
    ) -> tuple[ArticleContainer, _CapturingChild]:
        container = _make_test_article_container(char_w=char_w, line_h=line_h)
        child = _CapturingChild()
        container.set_child(child)
        return container, child

    def test_container_is_a_gtk_scrollable(self) -> None:
        # The base-class change is the whole point of Option C — without
        # it the ScrolledWindow interposes a viewport and the bug returns.
        container = _make_test_article_container()
        self.assertIsInstance(container, Gtk.Scrollable)

    def test_exposes_the_four_scrollable_interface_properties(self) -> None:
        # The interface's required surface, installed under the hyphenated
        # GObject names the ScrolledWindow drives.
        container = _make_test_article_container()
        prop_names = {pspec.name for pspec in container.list_properties()}
        self.assertLessEqual(
            {"hadjustment", "vadjustment", "hscroll-policy", "vscroll-policy"},
            prop_names,
        )

    def test_overflow_is_hidden_so_the_column_is_clipped(self) -> None:
        # With no interposed viewport the container must clip the column
        # to the viewport itself, or a column wider than the window paints
        # past the edge instead of being reached by the scrollbar.
        container = _make_test_article_container()
        self.assertEqual(container.get_overflow(), Gtk.Overflow.HIDDEN)

    # ----- vertical pass-through -----

    def test_vadjustment_is_forwarded_to_a_scrollable_child(self) -> None:
        container = _make_test_article_container()
        text_view = Gtk.TextView()
        container.set_child(text_view)
        vadj = Gtk.Adjustment()

        container.set_property("vadjustment", vadj)

        # The text view becomes the vertical scrollport: it owns the very
        # adjustment the ScrolledWindow reads for the scrollbar.
        self.assertIs(text_view.get_vadjustment(), vadj)

    def test_vadjustment_set_before_child_still_reaches_a_later_child(
        self,
    ) -> None:
        # In production the child is set before the ScrolledWindow installs
        # the adjustment; here we cover the opposite order so set_child's
        # own forwarding is exercised too.
        container = _make_test_article_container()
        vadj = Gtk.Adjustment()
        container.set_property("vadjustment", vadj)
        text_view = Gtk.TextView()

        container.set_child(text_view)

        self.assertIs(text_view.get_vadjustment(), vadj)

    def test_vscroll_policy_is_forwarded_to_a_scrollable_child(self) -> None:
        container = _make_test_article_container()
        text_view = Gtk.TextView()
        container.set_child(text_view)

        container.set_property(
            "vscroll-policy", Gtk.ScrollablePolicy.NATURAL
        )

        self.assertEqual(
            text_view.get_vscroll_policy(), Gtk.ScrollablePolicy.NATURAL
        )

    def test_forwarding_to_a_non_scrollable_child_is_a_no_op(self) -> None:
        # The bare-widget stand-in the allocation tests use is not a
        # Gtk.Scrollable; forwarding must skip it without raising.
        container, _child = self._capturing()
        container.set_property("vadjustment", Gtk.Adjustment())  # must not raise

    # ----- horizontal axis owned by the container -----

    def test_narrow_allocation_configures_hadjustment_extent(self) -> None:
        # Below the column width the container publishes the scroll extent
        # on its own hadjustment: upper = column, page = viewport, lower 0.
        # That overflow (upper > page) is what shows the horizontal
        # scrollbar under the AUTOMATIC policy.
        container, _child = self._capturing()
        outer = container.outer_column_width()
        hadj = Gtk.Adjustment()
        container.set_property("hadjustment", hadj)
        viewport = outer - 200

        container.do_size_allocate(viewport, 600, -1)

        self.assertEqual(hadj.get_lower(), 0.0)
        self.assertEqual(hadj.get_upper(), float(outer))
        self.assertEqual(hadj.get_page_size(), float(viewport))

    def test_horizontal_scroll_offsets_child_by_negative_value(self) -> None:
        # A scroll within range translates the column left by the scroll
        # value — the container, not the text view, does the panning.
        container, child = self._capturing()
        outer = container.outer_column_width()
        hadj = Gtk.Adjustment()
        container.set_property("hadjustment", hadj)
        viewport = outer - 200
        container.do_size_allocate(viewport, 600, -1)  # max offset 200

        hadj.set_value(150)
        child.recorded_allocate_calls.clear()
        container.do_size_allocate(viewport, 600, -1)

        width, _h, _b, transform = child.recorded_allocate_calls[-1]
        self.assertEqual(width, outer)  # column still pinned to full width
        self.assertEqual(_transform_x_offset(transform), -150)

    def test_horizontal_scroll_value_clamps_to_column_minus_viewport(
        self,
    ) -> None:
        # A value past the end (e.g. left over from a narrower-still
        # layout) is clamped to column − viewport so the column cannot be
        # pinned entirely off-screen.
        container, child = self._capturing()
        outer = container.outer_column_width()
        hadj = Gtk.Adjustment()
        container.set_property("hadjustment", hadj)
        viewport = outer - 200
        container.do_size_allocate(viewport, 600, -1)
        max_offset = outer - viewport  # 200

        hadj.set_value(max_offset + 10_000)
        child.recorded_allocate_calls.clear()
        container.do_size_allocate(viewport, 600, -1)

        self.assertEqual(int(hadj.get_value()), max_offset)
        _w, _h, _b, transform = child.recorded_allocate_calls[-1]
        self.assertEqual(_transform_x_offset(transform), -max_offset)

    def test_wide_allocation_pins_hadjustment_value_to_zero(self) -> None:
        # When the viewport is at least the column width there is nothing
        # to scroll: upper collapses to ≤ page and the value is pinned to
        # 0 while the column is centred.
        container, child = self._capturing()
        outer = container.outer_column_width()
        hadj = Gtk.Adjustment()
        container.set_property("hadjustment", hadj)
        wide = outer + 240

        container.do_size_allocate(wide, 600, -1)

        self.assertEqual(hadj.get_value(), 0.0)
        _w, _h, _b, transform = child.recorded_allocate_calls[-1]
        self.assertEqual(_transform_x_offset(transform), (wide - outer) // 2)

    def test_setting_hadjustment_connects_value_changed(self) -> None:
        # The container re-runs allocation on a horizontal scroll, so it
        # must subscribe to the adjustment it is given.
        container = _make_test_article_container()
        hadj = Gtk.Adjustment()

        container.set_property("hadjustment", hadj)

        self.assertIs(container._connected_hadjustment, hadj)
        self.assertTrue(
            GObject.signal_handler_is_connected(
                hadj, container._hadjustment_value_changed_id
            )
        )

    def test_value_changed_requests_reallocation(self) -> None:
        # The end-to-end reason for the subscription: a value change must
        # queue a fresh allocation so the column repositions.
        container = _make_test_article_container()
        hadj = Gtk.Adjustment()
        container.set_property("hadjustment", hadj)
        hadj.configure(0.0, 0.0, 1000.0, 10.0, 90.0, 500.0)
        reallocations: list[int] = []
        # Shadow the GTK method so the test observes the request without a
        # main loop; the handler calls ``self.queue_allocate()``.
        container.queue_allocate = lambda: reallocations.append(1)  # type: ignore[method-assign]

        hadj.set_value(120.0)

        self.assertTrue(reallocations)

    def test_replacing_hadjustment_disconnects_the_previous_one(self) -> None:
        # A replaced adjustment must leave no dangling handler closing over
        # the container.
        container = _make_test_article_container()
        first = Gtk.Adjustment()
        container.set_property("hadjustment", first)
        first_id = container._hadjustment_value_changed_id

        second = Gtk.Adjustment()
        container.set_property("hadjustment", second)

        self.assertFalse(
            GObject.signal_handler_is_connected(first, first_id)
        )
        self.assertIs(container._connected_hadjustment, second)

    def test_unroot_disconnects_the_hadjustment_handler(self) -> None:
        # The rooted (production) teardown path drops the subscription
        # alongside the child unparent.
        container = _make_test_article_container()
        container.set_child(_CapturingChild())
        window = Gtk.Window()
        window.set_child(container)
        hadj = Gtk.Adjustment()
        container.set_property("hadjustment", hadj)
        handler_id = container._hadjustment_value_changed_id

        window.set_child(None)  # unroots the container

        self.assertFalse(
            GObject.signal_handler_is_connected(hadj, handler_id)
        )
        self.assertIsNone(container._connected_hadjustment)
        window.destroy()


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleContainerScrollbarRegressionTests(unittest.TestCase):
    """End-to-end regression for the first-launch scrollbar bug.

    The original defect: on launch the rendered pane showed *no* vertical
    scrollbar even when the selected note overflowed the viewport, if the
    note's last line was a static-size image. The implicit ``Gtk.Viewport``
    committed a page-sized extent during its first allocation (while the
    text view still measured zero height) and never revised it, because a
    trailing static image produces no later height change. Option C removes
    the viewport, so the text view — which knows its own height — owns the
    vertical adjustment and writes the correct ``upper``.

    This test reproduces the exact trigger: a real :class:`NoteView` whose
    selected note ends with a tall image, presented on a real toplevel and
    settled through a **real main loop** (the bug only manifests after a
    frame-clock tick, which a manually pumped context never produces). It
    asserts the vertical adjustment overflows the page at startup — i.e.
    the scrollbar is shown — without any switch-and-back nudge.
    """

    def _build_image_last_view(
        self,
    ) -> tuple[NoteView, Gtk.Window]:
        repository = _FakeNoteRepository()
        note = _make_note(
            "img-last",
            source="= Title\n\nIntro paragraph.\n\nimage::tall.png[]",
        )
        repository.notes[note.id] = note
        store = _build_tracking_store(repository)
        attachments = _FakeAttachmentStore()
        # 100×900: comfortably taller than the 600 px viewport whether or
        # not the renderer scales it to the column width.
        attachments.seed("img-last", "tall.png", _solid_png(100, 900))
        app_state = AppState()
        app_state.set_selected_note_id("img-last")
        with mock.patch.object(
            note_view_module,
            "_build_font_measurers",
            _stub_font_measurers_factory(char_w=10, line_h=20),
        ):
            view = NoteView(
                note_store=store,
                app_state=app_state,
                attachments=attachments,
            )
        window = Gtk.Window()
        window.set_default_size(900, 600)
        window.set_child(view)
        return view, window

    def test_image_last_note_shows_vertical_scrollbar_on_first_launch(
        self,
    ) -> None:
        view, window = self._build_image_last_view()
        window.present()
        try:
            _settle_real_main_loop()
            scrolled = _find_scrolled_window(view)
            # Option C interposes no viewport — the container is the direct
            # scrollable child.
            self.assertNotIsInstance(scrolled.get_child(), Gtk.Viewport)
            vadjustment = scrolled.get_vadjustment()
            self.assertGreater(
                vadjustment.get_upper(),
                vadjustment.get_page_size(),
                "the rendered note overflows the viewport, so the vertical "
                "adjustment must report an extent larger than the page "
                "(i.e. the scrollbar is shown) at startup",
            )
        finally:
            window.set_child(None)
            window.destroy()
            _settle_real_main_loop(timeout_ms=50)


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


# ---------------------------------------------------------------------------
# Tiny helper to locate the inner TextView's buffer for assertions.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _message_for: exhaustiveness and content
# ---------------------------------------------------------------------------


class MessageForTests(unittest.TestCase):
    """Pin the user-facing message helper used by the parse-error
    notice. Exhaustiveness over :class:`ParseErrorKind` is enforced
    so a new error kind cannot ship without a notice message.
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
                # notice is the only context the user has, so the
                # location has to be visible.
                self.assertIn("42", message)

    def test_unsupported_link_scheme_message_lists_supported_schemes(self) -> None:
        # This message must name the schemes the user *can* use, so
        # pin its content explicitly.
        message = _message_for(ParseErrorKind.UNSUPPORTED_LINK_SCHEME, 39)
        self.assertIn("39", message)
        for scheme in ("http", "https", "mailto"):
            self.assertIn(scheme, message)

    def test_message_does_not_leak_internal_message(self) -> None:
        # Smoke check: the developer-oriented strings (square
        # brackets around `cols=` or specific quotes) don't leak
        # into the user-facing copy. The notice is consumer copy,
        # not a developer dump.
        message = _message_for(ParseErrorKind.BAD_COLS_DIRECTIVE, 7)
        self.assertNotIn("'", message)


# ---------------------------------------------------------------------------
# Parse-error notice integration with refresh
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteViewErrorNoticeTests(unittest.TestCase):
    """The in-surface parse-error notice is absent by default, rendered
    into the buffer on parse failure with a kind-specific message, and
    cleared when the user navigates to a parseable note."""

    def test_notice_absent_initially_with_no_selection(self) -> None:
        # No note selected at construction → no notice, the buffer
        # empty.
        repo = _FakeNoteRepository()
        app_state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=app_state)
        self.assertFalse(view.error_notice_visible)
        self.assertEqual(view.error_notice_text, "")

    def test_notice_absent_on_successful_render(self) -> None:
        repo = _FakeNoteRepository()
        repo.notes["note-A"] = _make_note("note-A")  # parses cleanly
        app_state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=app_state)
        app_state.set_selected_note_id("note-A")
        self.assertFalse(view.error_notice_visible)
        self.assertEqual(view.error_notice_text, "")

    def test_notice_shown_on_parse_error(self) -> None:
        # A note whose source raises ParseError renders the notice into
        # the surface with a kind-specific message AND replaces any
        # prior content.
        repo = _FakeNoteRepository()
        # `:bad name:` lexes as a LineToken; the parser raises
        # BAD_ATTRIBUTE_ENTRY against it.
        repo.notes["note-A"] = _make_note(
            "note-A",
            source=":bad name: value\n",
        )
        app_state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=app_state)
        app_state.set_selected_note_id("note-A")

        self.assertTrue(view.error_notice_visible)
        self.assertIn("Line 1", view.error_notice_text)
        # The buffer now holds the notice: its headline and the
        # kind-specific message, and nothing else.
        buffer = _find_text_view_buffer(view)
        rendered = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            False,
        )
        self.assertIn("This note", rendered)
        self.assertIn("Line 1", rendered)

    def test_notice_message_reflects_specific_error_kind(self) -> None:
        # Different parse-error kinds produce different messages.
        repo = _FakeNoteRepository()
        # Unsupported link scheme — an ftp:// link is outside the
        # allowlist and surfaces a distinct message.
        repo.notes["note-A"] = _make_note(
            "note-A",
            source="link:ftp://example.com[click]\n",
        )
        app_state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=app_state)
        app_state.set_selected_note_id("note-A")

        self.assertTrue(view.error_notice_visible)
        text = view.error_notice_text
        # The message says it's a link-scheme problem and lists the
        # supported schemes.
        self.assertIn("scheme", text)
        for scheme in ("http", "https", "mailto"):
            self.assertIn(scheme, text)

    def test_notice_recovers_when_selecting_clean_note(self) -> None:
        # After a parse-error display, navigating to a parseable
        # note clears the notice — surface state and the error flag
        # stay in lockstep with the current selection.
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note(
            "bad", source="link:javascript:alert(1)[x]\n",
        )
        repo.notes["good"] = _make_note("good")
        app_state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=app_state)

        app_state.set_selected_note_id("bad")
        self.assertTrue(view.error_notice_visible)

        app_state.set_selected_note_id("good")
        self.assertFalse(view.error_notice_visible)
        self.assertEqual(view.error_notice_text, "")

    def test_notice_cleared_when_selection_clears_after_error(self) -> None:
        # After a parse error, clearing the selection (None) must
        # also clear the notice — the user is no longer looking at a
        # note at all.
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note(
            "bad", source="*unclosed bold\n",
        )
        app_state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=app_state)
        app_state.set_selected_note_id("bad")
        self.assertTrue(view.error_notice_visible)

        app_state.set_selected_note_id(None)
        self.assertFalse(view.error_notice_visible)

    def test_notice_cleared_when_selection_points_to_missing_note(self) -> None:
        # A stale id (note deleted in another window) clears the
        # notice just like a None selection — the user gets neither
        # stale content nor a stale error.
        repo = _FakeNoteRepository()
        repo.notes["bad"] = _make_note(
            "bad", source="*unclosed\n",
        )
        app_state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=app_state)
        app_state.set_selected_note_id("bad")
        self.assertTrue(view.error_notice_visible)

        app_state.set_selected_note_id("does-not-exist")
        self.assertFalse(view.error_notice_visible)

    def test_navigating_to_bad_note_does_not_show_stale_content(self) -> None:
        # The plan's specific concern: the user clicks a note that
        # doesn't parse and sees the *previous* note's render.
        # After the fix, the buffer holds the error notice, not the
        # previous note's content.
        repo = _FakeNoteRepository()
        repo.notes["good"] = _make_note(
            "good", source="= Welcome\n\nIts contents.\n",
        )
        repo.notes["bad"] = _make_note(
            "bad", source="link:bogus://x[t]\n",
        )
        app_state = AppState()
        view = NoteView(note_store=_build_tracking_store(repo), app_state=app_state)

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
        # No leftover from "good"; the notice has taken over the surface.
        self.assertNotIn("Welcome", bad_text)
        self.assertIn("This note", bad_text)
        # And the notice explains what happened.
        self.assertTrue(view.error_notice_visible)


# ---------------------------------------------------------------------------
# ArticleTextView wash-painting tests
# ---------------------------------------------------------------------------
#
# The :meth:`ArticleTextView._compute_wash_rects` method is the test
# seam: it returns the list of ``(colour, rect)`` pairs the snapshot
# painter would append, without driving GTK's snapshot machinery. The
# tests below exercise it directly. The plain :class:`Gtk.TextView`
# methods this seam calls (``get_line_yrange``,
# ``buffer_to_window_coords``, ``get_width``, ``get_left_margin``,
# ``get_right_margin``) require a realised widget, so these tests are
# display-gated like the rest of the widget tests in this module.


def _build_article_text_view_with_buffer() -> tuple[
    ArticleTextView, Gtk.TextBuffer, Gtk.TextTagTable,
]:
    """Construct a wired :class:`ArticleTextView` for direct testing.

    Builds a tag table (with the same M-width fake used elsewhere in
    this module, ``9``), attaches a buffer to a fresh
    :class:`ArticleTextView`, and installs the wash specs via the same
    :meth:`ArticleTextView.install_wash_specs_from_table` seam
    :class:`NoteView` and :class:`HelpWindow` use. Returns the trio so
    individual tests can populate the buffer with tagged content and
    probe the painter.
    """
    table = build_tag_table(char_width_px=9)
    text_view = ArticleTextView()
    buffer = Gtk.TextBuffer.new(table)
    text_view.set_buffer(buffer)
    text_view.install_wash_specs_from_table(table)
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
            len(surface.text_view._wash_specs_by_tag), len(build_wash_specs()),
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


@unittest.skipUnless(_display_available(), "no GDK display")
class InstallWashSpecsFromTableTests(unittest.TestCase):
    """The shared seam that wires the block-tint painter.

    :meth:`ArticleTextView.install_wash_specs_from_table` is the single
    place that translates the :class:`TagName`-keyed
    :func:`build_wash_specs` map into the :class:`Gtk.TextTag`-keyed map
    the painter membership-tests against. Both the note view and the
    help window call it, so it must resolve *every* spec against a
    standard tag table — a dropped name would silently leave one block
    kind untinted.
    """

    def test_installs_a_spec_for_every_wash_name(self) -> None:
        text_view, _buffer, _table = _build_article_text_view_with_buffer()
        self.assertEqual(
            len(text_view._wash_specs_by_tag), len(build_wash_specs()),
        )


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleTextViewWashRectTests(unittest.TestCase):
    """Drive :meth:`ArticleTextView._compute_wash_rects` directly.

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
        # The default state of :class:`ArticleTextView` (before
        # :meth:`install_wash_specs` is called) is "no specs", so the
        # painter is a no-op. This is the right behaviour for tests
        # that construct the subclass standalone, and for the brief
        # window between constructor and wash-spec install.
        table = build_tag_table(char_width_px=9)
        text_view = ArticleTextView()
        buffer = Gtk.TextBuffer.new(table)
        text_view.set_buffer(buffer)
        buffer.set_text("anything\n")
        _apply_tag_across_line(
            buffer, 0, admonition_body_tag_name(AdmonitionKind.NOTE).value,
        )
        # No specs installed → painter never finds a matching tag.
        self.assertEqual(text_view._compute_wash_rects(), [])

    def test_metadata_line_produces_a_one_px_hairline_at_line_bottom(
        self,
    ) -> None:
        # The metadata tag's wash is a hairline: a 1-px rule painted at
        # the *bottom* of the line, not a full-height fill. Assert the
        # rect's height is the hairline thickness and that it sits at
        # the line's bottom edge.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("Created Apr 28, 2026  \u00b7  Modified Apr 28, 2026\n")
        _apply_tag_across_line(buffer, 0, TagName.METADATA.value)
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 1)
        _color, rect = rects[0]
        self.assertEqual(rect.get_height(), float(_HAIRLINE_THICKNESS_PX))
        # Recompute the line's bottom the same way the painter does and
        # confirm the rule sits there.
        ok, line_iter = buffer.get_iter_at_line(0)
        self.assertTrue(ok)
        line_y_buffer, line_h = text_view.get_line_yrange(line_iter)
        _, line_y_widget = text_view.buffer_to_window_coords(
            Gtk.TextWindowType.TEXT, 0, line_y_buffer,
        )
        self.assertEqual(
            rect.get_y(),
            float(line_y_widget + line_h - _HAIRLINE_THICKNESS_PX),
        )

    def test_table_header_line_produces_a_full_fill_rect(self) -> None:
        # The header row paints a tint band: a full-height fill (not a
        # hairline). Its rect height spans the whole logical line, and
        # its colour is the header tint.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("Ingredient\tGrams\n")
        _apply_tag_across_line(buffer, 0, TagName.TABLE_HEADER.value)
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 1)
        color, rect = rects[0]
        self.assertEqual(
            _tuple_of(color),
            _tuple_of(
                _rgba_from_tint(build_wash_specs()[TagName.TABLE_HEADER].tint)
            ),
        )
        ok, line_iter = buffer.get_iter_at_line(0)
        self.assertTrue(ok)
        _line_y_buffer, line_h = text_view.get_line_yrange(line_iter)
        self.assertEqual(rect.get_height(), float(line_h))

    def test_table_data_rows_each_produce_a_hairline_rect(self) -> None:
        # Each data row paints a 1-px rule at its bottom. Two data rows →
        # two hairline rects, each at its line's bottom edge.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("Flour\t400\nSugar\t200\n")
        _apply_tag_across_line(buffer, 0, TagName.TABLE_ROW.value)
        _apply_tag_across_line(buffer, 1, TagName.TABLE_ROW.value)
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 2)
        for line_no, (_color, rect) in enumerate(rects):
            with self.subTest(line=line_no):
                self.assertEqual(
                    rect.get_height(), float(_HAIRLINE_THICKNESS_PX),
                )

    def test_table_header_and_data_row_paint_one_rect_each(self) -> None:
        # A rendered table line carries exactly one of the two table tags
        # (the mutual-exclusion contract), so a header line plus a data
        # line produce two rects — a fill for the header, a hairline for
        # the row — without tripping the overlap guard. Heights depend on
        # live layout, so the robust distinguishers are the tints and the
        # row's fixed hairline thickness.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("Ingredient\tGrams\nFlour\t400\n")
        _apply_tag_across_line(buffer, 0, TagName.TABLE_HEADER.value)
        _apply_tag_across_line(buffer, 1, TagName.TABLE_ROW.value)
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 2)
        specs = build_wash_specs()
        header_color, _header_rect = rects[0]
        row_color, row_rect = rects[1]
        self.assertEqual(
            _tuple_of(header_color),
            _tuple_of(_rgba_from_tint(specs[TagName.TABLE_HEADER].tint)),
        )
        self.assertEqual(
            _tuple_of(row_color),
            _tuple_of(_rgba_from_tint(specs[TagName.TABLE_ROW].tint)),
        )
        # The row is the thin hairline regardless of layout.
        self.assertEqual(row_rect.get_height(), float(_HAIRLINE_THICKNESS_PX))

    def test_blockquote_body_line_produces_a_left_bar_rect(self) -> None:
        # The blockquote body paints a thin vertical rule at the box's
        # left edge, no fill — distinct from a full-width fill. Its rect
        # width is the spec's bar width (not the column width), it sits
        # at the box's left edge, and it spans the line's full height
        # the same way a FILL shape would.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("quoted body text\n")
        _apply_tag_across_line(buffer, 0, TagName.BLOCKQUOTE_BODY.value)
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 1)
        spec = build_wash_specs()[TagName.BLOCKQUOTE_BODY]
        color, rect = rects[0]
        self.assertEqual(_tuple_of(color), _tuple_of(_rgba_from_tint(spec.tint)))
        self.assertEqual(rect.get_width(), float(spec.bar_width_px))
        ok, line_iter = buffer.get_iter_at_line(0)
        self.assertTrue(ok)
        _line_y_buffer, line_h = text_view.get_line_yrange(line_iter)
        self.assertEqual(rect.get_height(), float(line_h))


class SheetRectTests(unittest.TestCase):
    """Drive the pure sheet helper.

    :func:`_sheet_rect_for` is closed over its integer arguments, so it
    is the display-free seam for the sheet geometry the same way
    :func:`_rgba_from_tint` is for wash colours. The sheet starts at
    ``sheet_top`` (leaving desk above) and ends at ``sheet_bottom``
    (leaving desk below).
    """

    def setUp(self) -> None:
        self.sheet_tint = build_sheet_wash().tint

    def test_short_content_sheet_spans_top_to_content(self) -> None:
        # A short note scrolled to the top: a top desk band of 30 px, then
        # the sheet down to the content's bottom at 200 px.
        color, rect = _sheet_rect_for(30, 200, 700, 560, self.sheet_tint)
        self.assertEqual(rect.get_x(), 0.0)
        self.assertEqual(rect.get_y(), 30.0)
        self.assertEqual(rect.get_width(), 700.0)
        self.assertEqual(rect.get_height(), 170.0)
        self.assertEqual(
            _tuple_of(color), _tuple_of(_rgba_from_tint(self.sheet_tint)),
        )

    def test_zero_top_keeps_sheet_at_the_very_top(self) -> None:
        # The construction default (top gap 0): the sheet starts at y=0,
        # exactly the pre-symmetry behaviour.
        _color, rect = _sheet_rect_for(0, 200, 700, 560, self.sheet_tint)
        self.assertEqual(rect.get_y(), 0.0)
        self.assertEqual(rect.get_height(), 200.0)

    def test_negative_top_is_clamped_to_zero(self) -> None:
        # Scrolled down past the top breathing margin: the sheet fills from
        # the top, no desk band above.
        _color, rect = _sheet_rect_for(-40, 200, 700, 560, self.sheet_tint)
        self.assertEqual(rect.get_y(), 0.0)
        self.assertEqual(rect.get_height(), 200.0)

    def test_content_filling_viewport_sheet_fills_to_bottom(self) -> None:
        # When content reaches the viewport bottom the sheet covers down to
        # the height (no transparent strip below), still starting at the gap.
        _color, rect = _sheet_rect_for(30, 560, 700, 560, self.sheet_tint)
        self.assertEqual(rect.get_y(), 30.0)
        self.assertEqual(rect.get_height(), 530.0)

    def test_content_past_viewport_sheet_fills_to_bottom(self) -> None:
        # A long note (or one scrolled past the end) still fills downward.
        _color, rect = _sheet_rect_for(0, 900, 700, 560, self.sheet_tint)
        self.assertEqual(rect.get_height(), 560.0)

    def test_empty_buffer_sheet_fills_viewport(self) -> None:
        # ``None`` (empty buffer) with a zero top paints a full-height sheet.
        _color, rect = _sheet_rect_for(0, None, 700, 560, self.sheet_tint)
        self.assertEqual(rect.get_y(), 0.0)
        self.assertEqual(rect.get_height(), 560.0)


def _tuple_of(rgba: Gdk.RGBA) -> tuple[float, float, float, float]:
    """Channel 4-tuple of a :class:`Gdk.RGBA`, for equality asserts."""
    return (rgba.red, rgba.green, rgba.blue, rgba.alpha)


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleTextViewSheetBottomTests(unittest.TestCase):
    """Drive :meth:`ArticleTextView._sheet_bottom_px` on a live view.

    The pure rect math is covered by :class:`SheetAndSeamRectTests`;
    these cover the part that needs a real :class:`Gtk.TextView` — the
    empty-buffer guard and the end-iter-to-widget coordinate mapping —
    so they are gated on a display like the wash-rect suite. Assertions
    stay font-independent: the empty-buffer contract, that a short note
    ends above the viewport bottom, and that a long note does not.
    """

    def test_empty_buffer_returns_none(self) -> None:
        # The parse-error / no-note state: a blank buffer has no sheet
        # edge, so the caller paints a full-height blank sheet.
        text_view, _buffer, _table = _build_article_text_view_with_buffer()
        self.assertIsNone(text_view._sheet_bottom_px())

    def test_short_note_in_tall_view_ends_above_bottom(self) -> None:
        # A couple of lines in a viewport tall enough to leave room
        # below: the sheet bottom sits above the viewport bottom, so a
        # strip of revealed desk results.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("A short note.\nTwo lines only.\n")
        _realize_in_window(text_view, width=700, height=600)
        try:
            sheet_bottom = text_view._sheet_bottom_px()
            self.assertIsNotNone(sheet_bottom)
            assert sheet_bottom is not None  # narrow for the type checker
            self.assertLess(sheet_bottom, 600)
        finally:
            _destroy_window_of(text_view)

    def test_buffer_taller_than_viewport_ends_below_bottom(self) -> None:
        # Many lines in a short viewport: the content bottom is past the
        # viewport, so the sheet fills it.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("\n".join(f"line {i}" for i in range(200)) + "\n")
        _realize_in_window(text_view, width=700, height=240)
        try:
            sheet_bottom = text_view._sheet_bottom_px()
            self.assertIsNotNone(sheet_bottom)
            assert sheet_bottom is not None  # narrow for the type checker
            self.assertGreaterEqual(sheet_bottom, 240)
        finally:
            _destroy_window_of(text_view)

    def test_end_gap_lifts_sheet_bottom_by_its_pixels(self) -> None:
        # The end gap is the slice of bottom-margin the sheet does NOT
        # claim: raising it by N px lowers the reported sheet bottom by
        # exactly N, independent of font, content, or scroll position.
        # This is the decoupling that lets a long note reveal desk + seam
        # at its end rather than filling the viewport.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("A short note.\n")
        text_view.set_bottom_margin(120)
        _realize_in_window(text_view, width=700, height=600)
        try:
            text_view.set_end_gap_px(0)
            without_gap = text_view._sheet_bottom_px()
            text_view.set_end_gap_px(45)
            with_gap = text_view._sheet_bottom_px()
            self.assertIsNotNone(without_gap)
            self.assertIsNotNone(with_gap)
            assert without_gap is not None and with_gap is not None  # narrow
            self.assertEqual(without_gap - with_gap, 45)
        finally:
            _destroy_window_of(text_view)


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleTextViewSheetTopTests(unittest.TestCase):
    """Drive :meth:`ArticleTextView._sheet_top_px` on a live view.

    The mirror of :class:`ArticleTextViewSheetBottomTests`: the pure rect
    math is covered by :class:`SheetAndSeamRectTests`, so these cover the
    part that needs a real :class:`Gtk.TextView` — the empty-buffer guard,
    the start-iter-to-widget coordinate mapping, and that the top gap lifts
    the sheet's top edge by exactly its pixels. Assertions stay
    font-independent.
    """

    def test_empty_buffer_returns_zero(self) -> None:
        # The parse-error / no-note state: a blank buffer reports a sheet
        # top of 0, so the caller paints a full-height blank sheet from the
        # very top.
        text_view, _buffer, _table = _build_article_text_view_with_buffer()
        self.assertEqual(text_view._sheet_top_px(), 0)

    def test_top_gap_shows_desk_above_when_scrolled_to_top(self) -> None:
        # A short note in a tall viewport sits scrolled to the top, so the
        # sheet top equals the reserved top gap — the desk band above the
        # note. With a zero gap the sheet starts at the very top.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("A short note.\nTwo lines only.\n")
        text_view.set_top_margin(80)
        _realize_in_window(text_view, width=700, height=600)
        try:
            text_view.set_top_gap_px(0)
            self.assertEqual(text_view._sheet_top_px(), 0)
            text_view.set_top_gap_px(30)
            self.assertEqual(text_view._sheet_top_px(), 30)
        finally:
            _destroy_window_of(text_view)

    def test_top_gap_lifts_sheet_top_by_its_pixels(self) -> None:
        # The mirror of the end-gap test: the top gap is the slice of the
        # top-margin the sheet does NOT claim, so raising it by N px lowers
        # the sheet's top edge by exactly N, independent of font, content,
        # or scroll position.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("A short note.\n")
        text_view.set_top_margin(120)
        _realize_in_window(text_view, width=700, height=600)
        try:
            text_view.set_top_gap_px(0)
            without_gap = text_view._sheet_top_px()
            text_view.set_top_gap_px(45)
            with_gap = text_view._sheet_top_px()
            self.assertEqual(with_gap - without_gap, 45)
        finally:
            _destroy_window_of(text_view)


def _realize_in_window(widget: Gtk.Widget, *, width: int, height: int) -> None:
    """Put ``widget`` in a presented window and give it a known allocation.

    The end-of-note mapping reads :meth:`Gtk.TextView.get_line_yrange`
    and :meth:`Gtk.TextView.get_height`, which only return real values
    once the widget has been realised and allocated. ``present`` realises
    it (so its Pango context exists and the text lays out); the explicit
    :meth:`Gtk.Widget.allocate` then pins a deterministic viewport size,
    since a headless compositor does not reliably map/allocate the
    presented surface under test load.
    """
    window = Gtk.Window()
    window.set_default_size(width, height)
    window.set_child(widget)
    window.present()
    context = GLib.MainContext.default()
    for _ in range(50):
        while context.pending():
            context.iteration(False)
    widget.allocate(width, height, -1, None)
    for _ in range(20):
        while context.pending():
            context.iteration(False)


def _destroy_window_of(widget: Gtk.Widget) -> None:
    """Destroy the toplevel hosting ``widget`` and drain pending events."""
    root = widget.get_root()
    if isinstance(root, Gtk.Window):
        root.destroy()
    context = GLib.MainContext.default()
    for _ in range(50):
        while context.pending():
            context.iteration(False)


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


def _buffer_text(buffer: Gtk.TextBuffer) -> str:
    """Whole buffer text (no anchors on the metadata path)."""
    text: str = buffer.get_text(
        buffer.get_start_iter(), buffer.get_end_iter(), False,
    )
    return text


@unittest.skipUnless(_display_available(), "no GDK display")
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


@unittest.skipUnless(_display_available(), "no GDK display")
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


@unittest.skipUnless(_display_available(), "no GDK display")
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


@unittest.skipUnless(_display_available(), "no GDK display")
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


if __name__ == "__main__":
    unittest.main()
