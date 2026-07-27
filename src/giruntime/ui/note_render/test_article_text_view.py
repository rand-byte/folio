"""Tests for :mod:`giruntime.ui.note_render.article_text_view`."""

from __future__ import annotations

import unittest

from gi.repository import Gdk, GLib, Graphene, Gtk

from enums import AdmonitionKind, ColorScheme
from giruntime.ui.note_render import article_text_view
from giruntime.ui.note_render.article_text_view import (
    ArticleTextView,
    _HAIRLINE_THICKNESS_PX,
    _rgba_from_tint,
    _sheet_rect_for,
)
from giruntime.ui.note_render.palette import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    scheme_for_foreground,
)
from giruntime.ui.note_render.tag_table import (
    TagName,
    admonition_body_tag_name,
    build_sheet_wash,
    build_tag_table,
    build_wash_specs,
)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


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

    The colour scheme is **pinned to light**, not left to the ambient
    theme. Every wash and geometry assertion in this module compares
    against ``LIGHT_PALETTE`` tints, and an unpinned view re-themes
    itself the moment it resolves a dark style — so on a dark desktop
    those tests would compare light expectations against dark output and
    fail for reasons that have nothing to do with what they test. Cases
    that are *about* the dark path install their own probe afterwards.
    """
    table = build_tag_table(char_width_px=9, palette=LIGHT_PALETTE)
    text_view = ArticleTextView()
    buffer = Gtk.TextBuffer.new(table)
    text_view.set_buffer(buffer)
    text_view.install_wash_specs_from_table(table)
    text_view.install_scheme_probe(lambda: ColorScheme.LIGHT)
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


def _all_wash_rects(
    text_view: ArticleTextView,
) -> list[tuple[Gdk.RGBA, Graphene.Rect]]:
    """Wash rects for the whole buffer, bypassing the visible-span clip.

    :meth:`ArticleTextView._compute_wash_rects` clips to the visible
    line span, which needs a realised viewport; the per-line seam
    :meth:`ArticleTextView._wash_rects_for_span` takes an explicit span,
    so these tests probe per-line geometry and the mutual-exclusion
    guard over the *whole* buffer without windowing or a live paint —
    the same unwindowed, display-gated shape the wash tests always had.
    The visible-span clip itself (off-screen lines dropped) is covered
    by :class:`ArticleTextViewWashClipTests`.
    """
    buffer = text_view.get_buffer()
    return text_view._wash_rects_for_span(
        buffer, 0, buffer.get_line_count() - 1,
    )


def _teardown_window(window: Gtk.Window) -> None:
    """Unparent the child and destroy ``window``, then let teardown settle.

    Mirrors the teardown the windowed regression tests use: dropping the
    child before ``destroy`` keeps GTK from warning about leftover children,
    and a short real-loop settle lets the frame clock finish the unmap.
    """
    window.set_child(None)
    window.destroy()
    _settle_real_main_loop(timeout_ms=50)


def _present_scrolled_for_wash(
    test_case: unittest.TestCase,
    text_view: ArticleTextView,
    *,
    width_px: int,
    height_px: int,
) -> None:
    """Realise ``text_view`` inside a fixed-size :class:`Gtk.ScrolledWindow`.

    The scroller constrains the viewport to ``height_px``, so a buffer
    taller than that is only partly visible — which is what the clip
    tests need in order to see off-screen lines dropped. The
    view keeps ownership of the vertical adjustment (Option C, no
    interposed viewport), so :func:`_scroll_wash_view_to` drives it
    directly. Call after populating the buffer; teardown is registered
    on ``test_case``.
    """
    window = Gtk.Window()
    window.set_default_size(width_px, height_px)
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_child(text_view)
    window.set_child(scrolled)
    window.present()
    _settle_real_main_loop()
    test_case.addCleanup(_teardown_window, window)


def _scroll_wash_view_to(text_view: ArticleTextView, value: float) -> None:
    """Set the view's vertical scroll position and let a real loop settle.

    The visible rect only reflects the new position after the frame
    clock ticks, so this settles the real main loop (a pumped context
    does not advance the clock — see :func:`_settle_real_main_loop`).
    """
    text_view.get_vadjustment().set_value(value)
    _settle_real_main_loop(timeout_ms=200)


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
            len(text_view._wash_specs_by_tag), len(build_wash_specs(LIGHT_PALETTE)),
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
        self.assertEqual(_all_wash_rects(text_view), [])

    def test_one_admonition_body_paragraph_produces_one_rect(self) -> None:
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text("body of the admonition\n")
        _apply_tag_across_line(
            buffer, 0, admonition_body_tag_name(AdmonitionKind.NOTE).value,
        )
        rects = _all_wash_rects(text_view)
        self.assertEqual(len(rects), 1)
        # The colour must match the NOTE admonition's tint — the
        # painter uses the spec's tint verbatim.
        expected_color = _rgba_from_tint(
            build_wash_specs(LIGHT_PALETTE)[
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
        rects = _all_wash_rects(text_view)
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
        self.assertEqual(_all_wash_rects(text_view), [])

    def test_no_wash_specs_installed_produces_no_rects(self) -> None:
        # The default state of :class:`ArticleTextView` (before
        # :meth:`install_wash_specs` is called) is "no specs", so the
        # painter is a no-op. This is the right behaviour for tests
        # that construct the subclass standalone, and for the brief
        # window between constructor and wash-spec install.
        table = build_tag_table(char_width_px=9, palette=LIGHT_PALETTE)
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
        rects = _all_wash_rects(text_view)
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
        rects = _all_wash_rects(text_view)
        self.assertEqual(len(rects), 1)
        color, rect = rects[0]
        self.assertEqual(
            _tuple_of(color),
            _tuple_of(
                _rgba_from_tint(build_wash_specs(LIGHT_PALETTE)[TagName.TABLE_HEADER].tint)
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
        rects = _all_wash_rects(text_view)
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
        rects = _all_wash_rects(text_view)
        self.assertEqual(len(rects), 2)
        specs = build_wash_specs(LIGHT_PALETTE)
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
        rects = _all_wash_rects(text_view)
        self.assertEqual(len(rects), 1)
        spec = build_wash_specs(LIGHT_PALETTE)[TagName.BLOCKQUOTE_BODY]
        color, rect = rects[0]
        self.assertEqual(_tuple_of(color), _tuple_of(_rgba_from_tint(spec.tint)))
        self.assertEqual(rect.get_width(), float(spec.bar_width_px))
        ok, line_iter = buffer.get_iter_at_line(0)
        self.assertTrue(ok)
        _line_y_buffer, line_h = text_view.get_line_yrange(line_iter)
        self.assertEqual(rect.get_height(), float(line_h))


@unittest.skipUnless(_display_available(), "no GDK display")
class ArticleTextViewWashClipTests(unittest.TestCase):
    """The wash walk is clipped to the visible line span (perf, §2.1).

    :meth:`ArticleTextView._compute_wash_rects` runs on every frame, so
    it must not walk the whole document: it iterates only the logical
    lines intersecting the viewport. These tests constrain the viewport
    with a :class:`Gtk.ScrolledWindow` shorter than the buffer and
    assert that off-screen wash lines are excluded while on-screen ones
    are painted — the behaviour that turns per-frame cost from
    O(document) into O(viewport).

    A tall buffer (`_CLIP_LINE_COUNT` short lines) rendered into a small
    viewport keeps only a slice on screen. A note-admonition wash on the
    first line and a blockquote wash on the last give the two ends
    distinct tints, so a single rect's colour says which end produced
    it.
    """

    # Comfortably taller than the viewport below, so top and bottom are
    # never simultaneously visible.
    _CLIP_LINE_COUNT = 80
    _CLIP_VIEW_WIDTH_PX = 400
    _CLIP_VIEW_HEIGHT_PX = 240

    def _build_tall_two_ended_view(
        self,
    ) -> tuple[ArticleTextView, Gtk.TextBuffer]:
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text(
            "".join(f"line {i}\n" for i in range(self._CLIP_LINE_COUNT)),
        )
        _apply_tag_across_line(
            buffer, 0, admonition_body_tag_name(AdmonitionKind.NOTE).value,
        )
        _apply_tag_across_line(
            buffer, self._CLIP_LINE_COUNT - 1, TagName.BLOCKQUOTE_BODY.value,
        )
        _present_scrolled_for_wash(
            self, text_view,
            width_px=self._CLIP_VIEW_WIDTH_PX,
            height_px=self._CLIP_VIEW_HEIGHT_PX,
        )
        return text_view, buffer

    def _admonition_tint(self) -> tuple[float, float, float, float]:
        return _tuple_of(
            _rgba_from_tint(
                build_wash_specs(LIGHT_PALETTE)[
                    admonition_body_tag_name(AdmonitionKind.NOTE)
                ].tint
            )
        )

    def _blockquote_tint(self) -> tuple[float, float, float, float]:
        return _tuple_of(
            _rgba_from_tint(build_wash_specs(LIGHT_PALETTE)[TagName.BLOCKQUOTE_BODY].tint)
        )

    def test_scrolled_to_top_paints_only_the_on_screen_wash(self) -> None:
        # At the top the first-line admonition is visible and the
        # last-line blockquote is far below the viewport: exactly one
        # rect, carrying the admonition tint.
        text_view, _buffer = self._build_tall_two_ended_view()
        _scroll_wash_view_to(text_view, 0.0)
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 1)
        color, _rect = rects[0]
        self.assertEqual(_tuple_of(color), self._admonition_tint())

    def test_scrolled_to_bottom_paints_only_the_on_screen_wash(self) -> None:
        # Scrolled to the end the last-line blockquote is visible and the
        # first-line admonition is above the viewport: exactly one rect,
        # carrying the blockquote tint. This is the exclusion the clip
        # buys — before it, both ends painted every frame.
        text_view, _buffer = self._build_tall_two_ended_view()
        adjustment = text_view.get_vadjustment()
        _scroll_wash_view_to(text_view, adjustment.get_upper())
        rects = text_view._compute_wash_rects()
        self.assertEqual(len(rects), 1)
        color, _rect = rects[0]
        self.assertEqual(_tuple_of(color), self._blockquote_tint())

    def test_block_taller_than_viewport_paints_fewer_rects_than_its_lines(
        self,
    ) -> None:
        # A single wash block taller than the viewport must not paint a
        # rect per line of the whole block — only per visible line. This
        # is the O(viewport) property stated directly: the rect count is
        # bounded by the viewport, not the block length.
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text(
            "".join(f"quote {i}\n" for i in range(self._CLIP_LINE_COUNT)),
        )
        for line_no in range(self._CLIP_LINE_COUNT):
            _apply_tag_across_line(
                buffer, line_no, TagName.BLOCKQUOTE_BODY.value,
            )
        _present_scrolled_for_wash(
            self, text_view,
            width_px=self._CLIP_VIEW_WIDTH_PX,
            height_px=self._CLIP_VIEW_HEIGHT_PX,
        )
        _scroll_wash_view_to(text_view, 0.0)
        rects = text_view._compute_wash_rects()
        self.assertGreater(len(rects), 0)
        self.assertLess(len(rects), self._CLIP_LINE_COUNT)

    def test_partial_block_scrolled_from_top_still_paints(self) -> None:
        # A wash block whose start has scrolled above the viewport still
        # washes its visible lines: clipping by line number keeps the
        # lines straddling/after the top edge, and each paints its own
        # rect (the band fills down from the clipped top).
        text_view, buffer, _table = _build_article_text_view_with_buffer()
        buffer.set_text(
            "".join(f"quote {i}\n" for i in range(self._CLIP_LINE_COUNT)),
        )
        for line_no in range(self._CLIP_LINE_COUNT):
            _apply_tag_across_line(
                buffer, line_no, TagName.BLOCKQUOTE_BODY.value,
            )
        _present_scrolled_for_wash(
            self, text_view,
            width_px=self._CLIP_VIEW_WIDTH_PX,
            height_px=self._CLIP_VIEW_HEIGHT_PX,
        )
        adjustment = text_view.get_vadjustment()
        _scroll_wash_view_to(text_view, adjustment.get_upper() / 2)
        rects = text_view._compute_wash_rects()
        self.assertGreater(len(rects), 0)
        self.assertTrue(
            all(
                _tuple_of(color) == self._blockquote_tint()
                for color, _rect in rects
            )
        )


class SheetRectTests(unittest.TestCase):
    """Drive the pure sheet helper.

    :func:`_sheet_rect_for` is closed over its integer arguments, so it
    is the display-free seam for the sheet geometry the same way
    :func:`_rgba_from_tint` is for wash colours. The sheet starts at
    ``sheet_top`` (leaving desk above) and ends at ``sheet_bottom``
    (leaving desk below).
    """

    def setUp(self) -> None:
        self.sheet_tint = build_sheet_wash(LIGHT_PALETTE).tint

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
        # Reach the guard through the per-line seam over the whole buffer:
        # it does not need a realised viewport, and avoids driving an
        # invalid-tag state through a live snapshot (which would surface
        # the ValueError as a GTK paint-time traceback rather than here).
        with self.assertRaises(ValueError):
            _all_wash_rects(text_view)


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


@unittest.skipUnless(_display_available(), "requires a display")
class ColorSchemeReThemeTests(unittest.TestCase):
    """Switching colour scheme re-colours a live surface in place.

    The scheme is driven through the injected probe rather than through
    a real theme, so these assert the re-theme *mechanism* without
    depending on what theme the test compositor happens to run.
    :class:`ThemeChangeTests` covers the real trigger.
    """

    text_view: ArticleTextView
    buffer: Gtk.TextBuffer
    table: Gtk.TextTagTable

    def setUp(self) -> None:
        self.text_view, self.buffer, self.table = (
            _build_article_text_view_with_buffer()
        )

    def _go_dark(self) -> None:
        self.text_view.install_scheme_probe(lambda: ColorScheme.DARK)

    def test_starts_in_the_light_scheme(self) -> None:
        # An unrealised widget has no resolved style to measure, so the
        # view starts light and is corrected by the first css change.
        self.assertIs(self.text_view.color_scheme(), ColorScheme.LIGHT)

    def test_probe_switches_the_scheme(self) -> None:
        self._go_dark()
        self.assertIs(self.text_view.color_scheme(), ColorScheme.DARK)

    def test_probe_switches_the_palette(self) -> None:
        self._go_dark()
        self.assertIs(self.text_view.palette(), DARK_PALETTE)

    def test_sheet_follows_the_scheme(self) -> None:
        self._go_dark()
        self.assertEqual(
            self.text_view._sheet_wash, build_sheet_wash(DARK_PALETTE),
        )

    def test_tag_foregrounds_are_recoloured_in_place(self) -> None:
        # The buffer is bound to this tag table for life, so the
        # re-theme has to mutate these very tags rather than swap in a
        # new table.
        link = self.table.lookup(TagName.LINK.value)
        assert link is not None
        self._go_dark()
        rgba = link.get_property("foreground-rgba")
        expected = Gdk.RGBA()
        expected.parse(DARK_PALETTE.link_foreground)
        self.assertAlmostEqual(rgba.red, expected.red, places=3)
        self.assertAlmostEqual(rgba.green, expected.green, places=3)
        self.assertAlmostEqual(rgba.blue, expected.blue, places=3)

    def test_wash_specs_are_reinstalled_with_dark_tints(self) -> None:
        body_tag = self.table.lookup(
            admonition_body_tag_name(AdmonitionKind.NOTE).value
        )
        assert body_tag is not None
        self._go_dark()
        installed = self.text_view._wash_specs_by_tag[body_tag]
        self.assertEqual(
            installed.tint,
            DARK_PALETTE.admonition_tints[AdmonitionKind.NOTE],
        )

    def test_buffer_text_is_untouched_by_a_re_theme(self) -> None:
        # The whole point of re-colouring in place: no re-parse, no
        # re-render, and therefore no lost scroll position.
        self.buffer.set_text("some rendered text")
        self._go_dark()
        start, end = self.buffer.get_bounds()
        self.assertEqual(
            self.buffer.get_text(start, end, False), "some rendered text",
        )

    def test_switching_back_restores_the_light_palette(self) -> None:
        self._go_dark()
        self.text_view.install_scheme_probe(lambda: ColorScheme.LIGHT)
        self.assertIs(self.text_view.palette(), LIGHT_PALETTE)

    def test_an_unchanged_scheme_leaves_the_wash_map_alone(self) -> None:
        # css_changed also fires on hover and focus, so the no-change
        # path must be a genuine no-op rather than a silent rebuild.
        before = self.text_view._wash_specs_by_tag
        self.text_view.install_scheme_probe(lambda: ColorScheme.LIGHT)
        self.assertIs(self.text_view._wash_specs_by_tag, before)

    def test_a_view_without_a_tag_table_survives_a_scheme_change(self) -> None:
        # A bare view (constructed but never wired) has no table to
        # re-colour; the sheet must still follow the scheme.
        bare = ArticleTextView()
        bare.install_scheme_probe(lambda: ColorScheme.DARK)
        self.assertEqual(bare._sheet_wash, build_sheet_wash(DARK_PALETTE))


@unittest.skipUnless(_display_available(), "requires a display")
class ThemeChangeTests(unittest.TestCase):
    """The real trigger: GTK's own style-changed hook drives the probe.

    Drives ``gtk-application-prefer-dark-theme`` because it is the one
    lever a test can pull at runtime to make GTK re-resolve styles.
    Production never writes that property — it only ever *measures* the
    foreground it resolves to, which is what makes the detector work for
    themes this property cannot express.
    """

    def setUp(self) -> None:
        self.settings = Gtk.Settings.get_default()
        assert self.settings is not None
        self.addCleanup(
            self.settings.set_property,
            "gtk-application-prefer-dark-theme",
            self.settings.get_property("gtk-application-prefer-dark-theme"),
        )

    def test_the_view_tracks_a_theme_flip(self) -> None:
        # Asserts the view *agrees with the rule* after the flip, never
        # that dark won: whether this property darkens the resolved
        # style depends on the installed theme, and under an explicit
        # GTK_THEME it changes nothing at all. An earlier version
        # asserted DARK outright and failed on exactly those boxes.
        text_view = ArticleTextView()
        window = Gtk.Window()
        window.set_child(text_view)
        window.present()
        _settle_real_main_loop()
        self.settings.set_property(
            "gtk-application-prefer-dark-theme",
            not self.settings.get_property(
                "gtk-application-prefer-dark-theme"
            ),
        )
        _settle_real_main_loop()
        parent = text_view.get_parent()
        assert parent is not None
        color = parent.get_color()
        expected = scheme_for_foreground(color.red, color.green, color.blue)
        scheme = text_view.color_scheme()
        window.destroy()
        self.assertIs(scheme, expected)

    def test_the_default_probe_reads_the_parent_foreground(self) -> None:
        # The production probe classifies what the theme resolved for
        # the *parent* — neither Gtk.Settings property reports it
        # reliably, and this widget's own colour is the palette's ink.
        text_view = ArticleTextView()
        window = Gtk.Window()
        window.set_child(text_view)
        window.present()
        _settle_real_main_loop()
        parent = text_view.get_parent()
        assert parent is not None
        color = parent.get_color()
        expected = scheme_for_foreground(color.red, color.green, color.blue)
        scheme = text_view.color_scheme()
        window.destroy()
        self.assertIs(scheme, expected)


@unittest.skipUnless(_display_available(), "requires a display")
class ArticleInkTests(unittest.TestCase):
    """The note's default ink follows the palette, without a tag.

    The ink is set on the view's ``text`` CSS node, deliberately not on
    the widget node. That separation is load-bearing rather than
    incidental: :meth:`ArticleTextView._scheme_from_style` decides the
    colour scheme by reading the widget node's own resolved foreground,
    so ink written to the widget node would be read back as if it were
    the theme's — the view would see its own output as its input and
    latch. The last test here is what stops that regression.
    """

    def _ink_css(self) -> str:
        provider = article_text_view._ink_provider
        assert provider is not None
        css: str = provider.to_string()
        return css

    @staticmethod
    def _serialised(foreground: str) -> str:
        """The palette literal as GTK re-serialises it in a stylesheet.

        A provider normalises ``#1a1a18`` to ``rgb(26,26,24)``, so the
        expected value has to make the same round trip rather than be
        compared as the literal the palette stores.
        """
        rgba = Gdk.RGBA()
        rgba.parse(foreground)
        serialised: str = rgba.to_string()
        return serialised

    def test_light_ink_css_carries_the_palette_body_foreground(self) -> None:
        _build_article_text_view_with_buffer()
        self.assertIn(
            self._serialised(LIGHT_PALETTE.body_foreground), self._ink_css(),
        )

    def test_ink_is_scoped_to_the_article_text_node(self) -> None:
        # Not the bare widget: the source editor is a Gtk.TextView too
        # and must keep following the theme.
        _build_article_text_view_with_buffer()
        self.assertIn("article-text-view text", self._ink_css())

    def test_switching_to_dark_rewrites_the_ink(self) -> None:
        text_view, _, _ = _build_article_text_view_with_buffer()
        text_view.install_scheme_probe(lambda: ColorScheme.DARK)
        css = self._ink_css()
        self.assertIn(self._serialised(DARK_PALETTE.body_foreground), css)
        self.assertNotIn(
            self._serialised(LIGHT_PALETTE.body_foreground), css,
        )

    def test_ink_does_not_feed_back_into_scheme_detection(self) -> None:
        # The ink is written to this widget's own colour, so the probe
        # reads the *parent* instead. If it ever reads self, it sees its
        # own output: dark ink reads as "light theme", and the view can
        # never leave whichever scheme it is already in. Here the view is
        # forced dark while the test theme stays light — a self-reading
        # probe would answer DARK, a parent-reading one answers LIGHT.
        text_view, _, _ = _build_article_text_view_with_buffer()
        container = Gtk.Box()
        container.append(text_view)
        window = Gtk.Window()
        window.set_child(container)
        window.present()
        _settle_real_main_loop(200)
        text_view.install_scheme_probe(lambda: ColorScheme.DARK)
        _settle_real_main_loop(200)
        probed = ArticleTextView._scheme_from_style(text_view)
        theme_is_light = scheme_for_foreground(
            container.get_color().red,
            container.get_color().green,
            container.get_color().blue,
        )
        window.destroy()
        self.assertIs(probed, theme_is_light)

    def test_probe_falls_back_to_the_current_scheme_when_unparented(
        self,
    ) -> None:
        # The construction window, before the container adopts the view.
        text_view = ArticleTextView()
        self.assertIs(
            ArticleTextView._scheme_from_style(text_view),
            text_view.color_scheme(),
        )
