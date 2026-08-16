"""Tests for :mod:`giruntime.ui.article_container`."""

from __future__ import annotations

import gc
import unittest

from gi.repository import Gdk, GLib, GObject, Gsk, Gtk

from config.defaults import (
    ARTICLE_INNER_HPADDING_CHARS,
    TARGET_CHARS_PER_LINE,
)
from giruntime.ui.article_container import (
    ArticleContainer,
    CharWidthMeasurer,
    _FALLBACK_CHAR_WIDTH_PX,
    _FALLBACK_LINE_HEIGHT_PX,
)
from giruntime.ui.test_display_guard import display_available


_RGB_BYTES_PER_PIXEL: int = 3
"""Bytes per pixel in ``Gdk.MemoryFormat.R8G8B8`` — the format
:func:`_solid_texture` builds. Named so the stride computation reads as
"one row of pixels" rather than as an unexplained multiplier."""

_FIXTURE_RGB: tuple[int, int, int] = (80, 120, 200)
"""The fill colour of the regression fixture's texture. Any opaque
colour works — the extent the test asserts on depends on the paintable's
*size*, never its pixels — so this is simply a visible blue."""

_FIXTURE_IMAGE_WIDTH_PX: int = 100
_FIXTURE_IMAGE_HEIGHT_PX: int = 900
"""Pixel size of the fixture image. The height is chosen to exceed
:data:`_FIXTURE_WINDOW_HEIGHT_PX` on its own, so the rendered content
overflows the viewport whether or not the column width scales it."""

_FIXTURE_WINDOW_WIDTH_PX: int = 900
_FIXTURE_WINDOW_HEIGHT_PX: int = 600
"""Size of the toplevel the regression test presents. The width is wide
enough for the article column to be centred rather than horizontally
scrolled (the horizontal axis is not what this test is about); the
height is the page the vertical extent must exceed."""


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


def _solid_texture(width: int, height: int) -> Gdk.Texture:
    """Build a solid-colour RGB texture of the given pixel size.

    The scrollbar regression test needs a *static-size* trailing block
    taller than the viewport: a paintable reports a fixed intrinsic
    height, so unlike text it produces no later height change once the
    text view has measured it — which is precisely the shape that used to
    leave a stale extent uncorrected. Building the texture straight from
    memory keeps the fixture self-contained (no image encoder, no
    attachment store) and lets it ask for an arbitrarily tall image.
    """
    stride = width * _RGB_BYTES_PER_PIXEL
    payload = GLib.Bytes.new(bytes(_FIXTURE_RGB) * width * height)
    return Gdk.MemoryTexture.new(
        width,
        height,
        Gdk.MemoryFormat.R8G8B8,
        payload,
        stride,
    )


def _text_view_ending_in_a_tall_image() -> Gtk.TextView:
    """Build a scrollable child whose last block is a tall static image.

    Mirrors the production child's relevant properties — read-only,
    word-wrapping, and a ``Gtk.Scrollable`` so the container's vertical
    pass-through has somewhere to forward to — without pulling in the
    renderer, the tag table or a note store. Only two things about the
    content matter to the extent the parent scroller reads: it is taller
    than the viewport, and its last block is static-size.
    """
    text_view = Gtk.TextView()
    text_view.set_editable(False)
    text_view.set_cursor_visible(False)
    text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    text_view.set_hexpand(True)
    text_view.set_vexpand(True)
    buffer = text_view.get_buffer()
    buffer.insert(buffer.get_end_iter(), "Title\n\nIntro paragraph.\n\n")
    # 100x900: comfortably taller than the 600 px viewport.
    buffer.insert_paintable(
        buffer.get_end_iter(),
        _solid_texture(_FIXTURE_IMAGE_WIDTH_PX, _FIXTURE_IMAGE_HEIGHT_PX),
    )
    return text_view


@unittest.skipUnless(display_available(), "no GDK display")
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


@unittest.skipUnless(display_available(), "no GDK display")
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


@unittest.skipUnless(display_available(), "no GDK display")
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


@unittest.skipUnless(display_available(), "no GDK display")
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


@unittest.skipUnless(display_available(), "no GDK display")
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


@unittest.skipUnless(display_available(), "no GDK display")
class ArticleContainerFontMetricInvalidationTests(unittest.TestCase):
    """Cached measurements can be dropped when the font changes.

    Both measurements are memoised for the life of the surface, which is
    only correct while the font is fixed. A font change arrives through
    ``do_css_changed`` — the same hook as a theme change — so without an
    invalidation path the text redraws at the new size while the column
    width, the block margins baked into the tag table and the per-table
    tab stops all keep the old geometry.
    """

    def test_re_measures_after_invalidation(self) -> None:
        measured = [10]
        container = ArticleContainer(
            char_width_measurer=lambda: measured[0],
            line_height_measurer=lambda: 20,
        )
        self.assertEqual(container.char_width_px(), 10)

        measured[0] = 17
        # Without the invalidation the cache would still answer 10.
        container.invalidate_font_metrics()

        self.assertEqual(container.char_width_px(), 17)

    def test_reports_whether_the_metrics_moved(self) -> None:
        # do_css_changed also fires on hover and focus, so the caller
        # needs to tell a real font change from a colour-only one before
        # rebuilding tag geometry and re-rendering the buffer.
        measured = [10]
        container = ArticleContainer(
            char_width_measurer=lambda: measured[0],
            line_height_measurer=lambda: 20,
        )
        # Both caches have to be primed: an unmeasured metric counts as
        # a change, because the surface has no geometry to keep.
        container.char_width_px()
        container.line_height_px()

        self.assertFalse(container.invalidate_font_metrics())

        measured[0] = 17
        self.assertTrue(container.invalidate_font_metrics())

    def test_first_invalidation_reports_a_change(self) -> None:
        # Nothing was measured yet, so the surface has no geometry to
        # keep — reporting "changed" makes the caller establish it.
        container = _make_test_article_container()
        self.assertTrue(container.invalidate_font_metrics())


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

    def test_wide_allocation_does_not_publish_a_sub_page_extent(
        self,
    ) -> None:
        # Above the column width there is nothing to scroll, so upper
        # must equal the page rather than drop to the column.
        # Gtk.ScrolledWindow reads value > upper − page_size as a
        # horizontal overshoot and paints the theme's overshoot glow
        # over the desk for as long as that holds.
        container, _child = self._capturing()
        outer = container.outer_column_width()
        hadj = Gtk.Adjustment()
        container.set_property("hadjustment", hadj)
        viewport = outer + 200

        container.do_size_allocate(viewport, 600, -1)

        self.assertEqual(hadj.get_lower(), 0.0)
        self.assertEqual(hadj.get_upper(), float(viewport))
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
        container.queue_allocate = lambda: reallocations.append(1)

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


@unittest.skipUnless(display_available(), "no GDK display")
class ArticleContainerScrollbarRegressionTests(unittest.TestCase):
    """Regression for the first-launch scrollbar bug.

    The original defect: on launch the rendered pane showed *no* vertical
    scrollbar even when the content overflowed the viewport, if its last
    line was a static-size image. The pre-fix container was a plain
    ``Gtk.Widget`` that re-derived the vertical extent in ``do_measure``,
    so the parent ``Gtk.ScrolledWindow`` interposed a ``Gtk.Viewport``;
    the viewport committed a page-sized extent during its first
    allocation (while the text view still measured zero height) and never
    revised it, because a trailing static image produces no later height
    change. Option C makes the container a ``Gtk.Scrollable`` that
    contributes nothing vertically, so no viewport is interposed and the
    text view — which knows its own height — owns the vertical adjustment
    and writes the correct ``upper``.

    Both halves of that fix live in :mod:`giruntime.ui.article_container`
    (the ``Gtk.Scrollable`` base, the vertical pass-through in
    :meth:`ArticleContainer._forward_vertical_scrolling_to_child`, and the
    zero vertical report from :meth:`ArticleContainer.do_measure`), so the
    scenario is built from the container and a bare scrollable child
    rather than from a :class:`~giruntime.ui.note_view.NoteView`: nothing
    but the container can be responsible for the outcome, and the test
    needs no note store, renderer or attachment fixtures. The one thing
    it cannot do without is a **real main loop** — the extent is only
    committed after a frame-clock tick, which a manually pumped
    ``MainContext`` never produces.

    Verified to fail against the pre-fix container shape and pass against
    the current one, so it is a live guard rather than a description.
    """

    def _build_scrolled_stack(self) -> tuple[Gtk.ScrolledWindow, Gtk.Window]:
        """Assemble ``Window → ScrolledWindow → ArticleContainer → view``.

        The same stack :class:`~giruntime.ui.note_view.NoteView` builds
        around the surface, reduced to the widgets whose collaboration
        the extent depends on. The scroller's policy is ``AUTOMATIC`` on
        both axes, as in production, because that is what makes it read
        the adjustment to decide whether to show a scrollbar at all.
        """
        container = _make_test_article_container()
        container.set_child(_text_view_ending_in_a_tall_image())

        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        scrolled.set_child(container)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)

        window = Gtk.Window()
        window.set_default_size(
            _FIXTURE_WINDOW_WIDTH_PX,
            _FIXTURE_WINDOW_HEIGHT_PX,
        )
        window.set_child(scrolled)
        return scrolled, window

    def test_image_last_content_shows_vertical_scrollbar_on_first_launch(
        self,
    ) -> None:
        # Given a container holding content that ends in a tall static
        # image, inside a scroller on a real toplevel
        scrolled, window = self._build_scrolled_stack()

        # When the window is presented and the frame clock ticks
        window.present()
        try:
            _settle_real_main_loop()

            # Then no viewport was interposed (the container is the
            # direct scrollable child) ...
            self.assertNotIsInstance(scrolled.get_child(), Gtk.Viewport)
            # ... and the forwarded vertical adjustment reports an extent
            # larger than the page, i.e. the scrollbar is shown at
            # startup without any switch-away-and-back nudge.
            vadjustment = scrolled.get_vadjustment()
            self.assertGreater(
                vadjustment.get_upper(),
                vadjustment.get_page_size(),
                "the content overflows the viewport, so the vertical "
                "adjustment must report an extent larger than the page "
                "(i.e. the scrollbar is shown) at startup",
            )
        finally:
            window.set_child(None)
            window.destroy()
            _settle_real_main_loop(timeout_ms=50)

    def test_content_shorter_than_the_viewport_shows_no_scrollbar(
        self,
    ) -> None:
        # The negative control for the test above: without it, an
        # assertion that merely fired on every extent would look green.
        container = _make_test_article_container()
        text_view = Gtk.TextView()
        text_view.set_editable(False)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        buffer = text_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), "One short line.")
        container.set_child(text_view)

        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        scrolled.set_child(container)
        window = Gtk.Window()
        window.set_default_size(
            _FIXTURE_WINDOW_WIDTH_PX,
            _FIXTURE_WINDOW_HEIGHT_PX,
        )
        window.set_child(scrolled)

        window.present()
        try:
            _settle_real_main_loop()
            vadjustment = scrolled.get_vadjustment()
            self.assertEqual(
                vadjustment.get_upper(),
                vadjustment.get_page_size(),
                "content that fits the viewport must report an extent "
                "equal to the page, so no vertical scrollbar is shown",
            )
        finally:
            window.set_child(None)
            window.destroy()
            _settle_real_main_loop(timeout_ms=50)
