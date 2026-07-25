"""The article read view: painter of the note sheet and block-tint washes.

Principles & invariants
-----------------------
* :class:`ArticleTextView` is a read-only :class:`Gtk.TextView` subclass
  whose job is *painting*, not text handling. The paragraph tags from
  :mod:`giruntime.ui.note_render.tag_table` carry only text position
  (``left-margin`` / ``right-margin`` = inset + one M-width); the matching
  tinted *wash* — extending one M-width beyond the text on each side, the
  "padded card" effect ``paragraph-background-rgba`` cannot produce — is
  painted here in :meth:`do_snapshot`.
* The same subclass paints the note *sheet*: because the view is the
  vertical scrollport, its own background would fill the whole viewport, so
  its CSS background is made transparent (``article-text-view``) and
  ``do_snapshot`` paints an opaque sheet over the content plus the breathing
  part of the top/bottom margins, letting the scroller's "desk" show through
  equally before and after the note.
* This module is the *appearance* half of the reading surface and lives with
  the tag table whose :class:`WashSpec` / :class:`SheetWash` it consumes. It
  owns no geometry: the fixed-width column and scrolling belong to
  :class:`giruntime.ui.article_container.ArticleContainer`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from gi.repository import Gdk, Graphene, Gtk

from enums import WashShape
from giruntime.ui.note_render.tag_table import (
    SheetWash,
    WashSpec,
    build_sheet_wash,
    build_wash_specs,
)


_ARTICLE_TEXT_VIEW_CSS_CLASS: str = "article-text-view"
"""CSS class applied to :class:`ArticleTextView` so the bundled
stylesheet can make its background (and its text window's background)
transparent. The view paints its own opaque *sheet* in
:meth:`ArticleTextView.do_snapshot`, ending at the note's content, so
the scroller's background shows through below it; if the framework
painted the view's background it would fill the whole viewport and hide
that. The class name is stable across releases — the stylesheet that
targets it ships with the application.
"""


_HAIRLINE_THICKNESS_PX: int = 1
"""Height, in pixels, of the hairline rule the wash painter draws at
the bottom of a :class:`WashSpec` whose ``shape`` is
:data:`WashShape.HAIRLINE` (the metadata line's divider). Painted as a
1-px band rather than a full-height fill — see
:meth:`ArticleTextView._wash_rect_for_line`."""


@dataclass(frozen=True)
class _WidgetXMetrics:
    """Horizontal layout metrics for the article text view, captured once
    per snapshot.

    :class:`ArticleTextView` reads three GTK getters
    (:meth:`Gtk.Widget.get_width`, :meth:`Gtk.TextView.get_left_margin`,
    :meth:`Gtk.TextView.get_right_margin`) on every paint. Bundling the
    three into one frozen value lets the per-line rect computation
    receive a single ``metrics`` argument instead of three separate
    ints — and keeps the outer loop body slim enough to stay under
    pylint's local-count ceiling. The values do not change between
    iterations of the loop, which is the other reason for the
    captured-once shape.
    """

    width: int
    left_margin: int
    right_margin: int


class ArticleTextView(Gtk.TextView):
    """A :class:`Gtk.TextView` subclass that paints wider washes for tinted block paragraphs.

    The paragraph tags in :mod:`ui.note_render.tag_table`
    deliberately omit ``paragraph-background-rgba``; this subclass
    paints the matching wash itself via :meth:`do_snapshot`. For every
    visible logical line whose first iter carries a tag listed in the
    wash-spec map, it appends a :class:`Gsk.ColorNode` to the snapshot
    at a rect that extends one M-width beyond the text on each side,
    then delegates to :meth:`Gtk.TextView.do_snapshot` so inline text
    renders on top.

    The wash-spec map is supplied post-construction via
    :meth:`install_wash_specs`, keyed by :class:`Gtk.TextTag` objects
    (rather than tag names) so the per-snapshot lookup stays O(1) and
    avoids re-walking the tag table on every paint. Before
    :meth:`install_wash_specs` is called the painter is a no-op —
    that is the right behaviour for the brief window between
    constructor and wash-spec install, and the right fallback for
    test code that constructs the view without wiring the painter.

    Splitting :meth:`_compute_wash_rects` out of :meth:`do_snapshot`
    is the test seam: tests assert the list of rects directly without
    driving GTK's snapshot machinery.

    **The sheet.** The view's CSS background is transparent (set via the
    ``article-text-view`` style class, see ``css/app.css``); the page is
    instead painted here in :meth:`do_snapshot` as an opaque *sheet*
    covering the content plus the breathing part of the top and bottom
    margins. Above the sheet's top and below its bottom the view paints
    nothing, so the scroller's own background (the "desk") shows through —
    that is what frames the note with an equal gap before and after, using
    the *parent's* real background rather than a separately-invented colour
    that could drift from the theme. The sheet meets the desk directly, with
    no rule painted at the boundary. The colour comes from
    :func:`ui.note_render.tag_table.build_sheet_wash` (one place for
    every rendered-view colour) and the geometry is factored into the
    free function :func:`_sheet_rect_for` so the rect math is unit-testable
    without a realised font. While a long note is scrolled so that content
    still extends past the viewport bottom the sheet fills the lower
    viewport; scrolled to its very end, the end-gap desk band
    reserved by :meth:`set_end_gap_px` brings the sheet bottom into view
    with desk beneath. The matching top gap reserved by
    :meth:`set_top_gap_px` shows the same desk band above the sheet when the
    note is scrolled to the top; once scrolled past it the sheet fills the
    upper viewport. An empty buffer (the
    parse-error / no-note state) paints a full-height blank sheet.
    """

    _wash_specs_by_tag: Mapping[Gtk.TextTag, WashSpec]
    _sheet_wash: SheetWash
    _top_gap_px: int
    _end_gap_px: int

    def __init__(self) -> None:
        super().__init__()
        # The page is painted by do_snapshot (an opaque sheet ending at
        # the content), so the framework must not paint a background of
        # its own underneath — that would fill the whole viewport and
        # hide the desk below a short note. The style class drives the
        # ``background: transparent`` rule in css/app.css.
        self.add_css_class(_ARTICLE_TEXT_VIEW_CSS_CLASS)
        # No wash specs installed yet — the painter is a no-op until
        # :meth:`install_wash_specs` is called. Tests that construct
        # the subclass directly get a plain :class:`Gtk.TextView` of
        # behaviour, which matches the inert pre-install state.
        self._wash_specs_by_tag = {}
        # The sheet colour is static (no per-note parameters), so it is
        # resolved once at construction from the single rendered-view
        # colour source.
        self._sheet_wash = build_sheet_wash()
        # The desk bands the sheet does not cover, above and below the
        # content. Both zero until NoteView sets them alongside the
        # margins; at zero the sheet covers the whole top/bottom margin —
        # the pre-gap behaviour that test code constructing a bare view
        # relies on. They are set from the same constant so they match.
        self._top_gap_px = 0
        self._end_gap_px = 0

    def install_wash_specs(
        self, specs_by_tag: Mapping[Gtk.TextTag, WashSpec],
    ) -> None:
        """Install the wash-spec map this view paints.

        Keys are :class:`Gtk.TextTag` *objects* (not names) — the
        constructor looks them up once by name from the buffer's tag
        table, so the snapshot path can do a direct ``tag in map``
        membership test rather than re-resolving the name on every
        paint. Calling this replaces the previous map outright.
        """
        self._wash_specs_by_tag = specs_by_tag

    def install_wash_specs_from_table(
        self, tag_table: Gtk.TextTagTable,
    ) -> None:
        """Build the wash-spec map from ``tag_table`` and install it.

        The one place that translates :func:`build_wash_specs` (keyed by
        :class:`TagName`) into the :class:`Gtk.TextTag`-keyed map
        :meth:`install_wash_specs` wants, resolving each name against
        ``tag_table``. Every consumer that wants the standard block
        tints (the note view *and* the help window) calls this rather
        than re-deriving the loop, so the two cannot drift in how they
        wire the painter. ``lookup`` returns ``None`` only for an unknown
        tag name; every key in :func:`build_wash_specs` is registered by
        :func:`build_tag_table`, so the lookups succeed — the defensive
        filter merely keeps the type narrow.
        """
        specs_by_tag: dict[Gtk.TextTag, WashSpec] = {}
        for tag_name, spec in build_wash_specs().items():
            tag = tag_table.lookup(tag_name.value)
            if tag is not None:
                specs_by_tag[tag] = spec
        self.install_wash_specs(specs_by_tag)

    def set_top_gap_px(self, top_gap_px: int) -> None:
        """Set the desk band (in px) reserved above the painted sheet.

        The mirror of :meth:`set_end_gap_px`: this is the slice of the
        view's ``top-margin`` that the sheet does **not** cover.
        :meth:`_sheet_top_px` keeps it as desk above the sheet, so when the
        note is scrolled to the top the same band of desk shows above the
        sheet as :meth:`set_end_gap_px` reserves below it — the gap before
        and after the note matches (see
        :data:`config.defaults.ARTICLE_END_GAP_LINES`). The production
        wiring in :class:`NoteView` sets this together with the
        ``top-margin`` so the two cannot drift; left at ``0`` (the
        construction default) the sheet covers the whole top margin.
        """
        self._top_gap_px = top_gap_px

    def set_end_gap_px(self, end_gap_px: int) -> None:
        """Set the desk band (in px) reserved below the painted sheet.

        This is the slice of the view's ``bottom-margin`` that the sheet
        does **not** cover: :meth:`_sheet_bottom_px` subtracts it, so the
        sheet ends ``end_gap_px`` above the bottom of the scrollable
        region. Scrolling a note taller than the viewport to its end then
        brings the sheet's bottom edge into view with
        that band of desk beneath it, giving a long note the same visible
        end a short note already has (see
        :data:`config.defaults.ARTICLE_END_GAP_LINES`). The production
        wiring in :class:`NoteView` sets this together with the
        ``bottom-margin`` so the two cannot drift; left at ``0`` (the
        construction default) the sheet covers the whole bottom margin.
        """
        self._end_gap_px = end_gap_px

    def do_snapshot(  # pylint: disable=arguments-differ
        self, snapshot: Gtk.Snapshot,
    ) -> None:
        """Paint the sheet, the per-paragraph washes, then the text.

        The view's CSS background is transparent (see ``__init__``), so
        this method paints the page itself. Order is back-to-front, the
        order :meth:`Gtk.Snapshot.append_color` stacks nodes:

        1. the *sheet* — an opaque page background covering the content
           (plus the breathing part of the top and bottom margins). Above
           the sheet's top and below its bottom the view paints nothing, so
           the scroller's own background (the "desk") shows through and the
           note meets the desk at a visible edge with an equal gap before
           and after it;
        2. the per-paragraph *washes*, behind the text;
        3. the *text*, via the parent snapshot.
        """
        sheet_top = self._sheet_top_px()
        sheet_bottom = self._sheet_bottom_px()
        width = self.get_width()
        height = self.get_height()
        sheet = _sheet_rect_for(
            sheet_top, sheet_bottom, width, height,
            self._sheet_wash.tint,
        )
        snapshot.append_color(*sheet)
        for color, rect in self._compute_wash_rects():
            snapshot.append_color(color, rect)
        Gtk.TextView.do_snapshot(self, snapshot)

    def _compute_wash_rects(
        self,
    ) -> list[tuple[Gdk.RGBA, Graphene.Rect]]:
        """Return one ``(colour, rect)`` per *visible* wash-bearing logical line.

        Walks only the logical lines intersecting the viewport (see
        :meth:`_visible_line_span`), one at a time, rather than the
        whole buffer: :meth:`do_snapshot` calls this every frame, so
        the cost is bounded to what is on screen (O(viewport)) instead
        of O(document). For every walked line whose first iter carries
        a tag in :attr:`_wash_specs_by_tag`, records a coloured rect
        that spans the full vertical extent of the logical line (i.e.
        all of its visual wraps, returned by
        :meth:`Gtk.TextView.get_line_yrange`) and is one M-width wider
        than the text column on each side.

        Clipping to the visible span is safe because each wash is a
        per-logical-line rect: a paragraph that begins above the
        viewport still qualifies on every line of it that is on screen
        (its band fills down from the clipped top), and a multi-line
        blockquote's :data:`WashShape.LEFT_BAR` rule stays continuous
        because each visible line contributes its own bar. Lines off
        screen were only ever clipped away by the snapshot anyway.

        Mutual exclusion: paragraph-level wash-bearing tags are
        mutually exclusive by parser construction — admonition label,
        admonition body, blockquote body, and code block are distinct
        paragraph types and never overlap on the same iter. The
        method enforces this defensively: if an iter carries more
        than one wash-bearing tag it raises :class:`ValueError`
        rather than silently picking one, so a future code path that
        violates the invariant fails loudly.
        """
        if not self._wash_specs_by_tag:
            return []
        buffer = self.get_buffer()
        span = self._visible_line_span(buffer)
        if span is None:
            return []
        first_line, last_line = span
        return self._wash_rects_for_span(buffer, first_line, last_line)

    def _wash_rects_for_span(
        self, buffer: Gtk.TextBuffer, first_line: int, last_line: int,
    ) -> list[tuple[Gdk.RGBA, Graphene.Rect]]:
        """Collect wash rects for the inclusive ``[first_line, last_line]`` span.

        The per-line seam, split from :meth:`_compute_wash_rects` so the
        per-line geometry and the mutual-exclusion guard can be exercised
        over an explicit span without a realised viewport (the caller
        supplies the visible span from :meth:`_visible_line_span`, which
        *does* need one). Walks the span one logical line at a time and
        records one rect per wash-bearing line — see
        :meth:`_wash_rect_for_line` for the per-line shape and
        :meth:`_spec_at_iter` for the mutual-exclusion :class:`ValueError`.
        """
        rects: list[tuple[Gdk.RGBA, Graphene.Rect]] = []
        metrics = _WidgetXMetrics(
            width=self.get_width(),
            left_margin=self.get_left_margin(),
            right_margin=self.get_right_margin(),
        )
        for line_no in range(first_line, last_line + 1):
            rect_with_color = self._wash_rect_for_line(
                buffer, line_no, metrics,
            )
            if rect_with_color is not None:
                rects.append(rect_with_color)
        return rects

    def _visible_line_span(
        self, buffer: Gtk.TextBuffer,
    ) -> tuple[int, int] | None:
        """Return the inclusive ``(first, last)`` logical-line span on screen.

        Derives the span from the view's own viewport:
        :meth:`Gtk.TextView.get_visible_rect` gives the visible region
        in buffer coordinates, and :meth:`Gtk.TextView.get_line_at_y`
        maps its top and bottom edges to the enclosing logical lines.
        Both bounds are inclusive — a line straddling either edge is
        partly visible and must be painted — so the caller iterates
        ``range(first, last + 1)``.

        Returns ``None`` (nothing to paint this frame) when the buffer
        is empty, or when the view has no positive-height viewport yet.
        The latter is the pre-allocation state: before the widget is
        sized, ``get_visible_rect`` reports a zero-height rect and
        ``get_line_at_y`` cannot resolve a meaningful line, so there is
        genuinely nothing on screen to wash.
        """
        if buffer.get_char_count() == 0:
            return None
        visible = self.get_visible_rect()
        if visible.height <= 0:
            return None
        top_iter, _ = self.get_line_at_y(visible.y)
        bottom_iter, _ = self.get_line_at_y(visible.y + visible.height)
        return top_iter.get_line(), bottom_iter.get_line()

    def _wash_rect_for_line(
        self,
        buffer: Gtk.TextBuffer,
        line_no: int,
        metrics: _WidgetXMetrics,
    ) -> tuple[Gdk.RGBA, Graphene.Rect] | None:
        """Compute the wash rect for one logical line, or ``None`` if
        the line carries no wash-bearing tag.

        Extracted from :meth:`_compute_wash_rects` so the inner
        per-line geometry lives in one place and the outer loop stays
        slim. Reads the line's vertical extent via
        :meth:`Gtk.TextView.get_line_yrange` and translates the
        buffer-coordinate y into widget-coordinate y via
        :meth:`Gtk.TextView.buffer_to_window_coords` — the same
        translation a manual draw against the text window would
        perform. :data:`WashShape.HAIRLINE` paints a 1-px rule at the
        line's bottom instead of a full-height fill;
        :data:`WashShape.LEFT_BAR` paints a thin vertical rule of width
        ``spec.bar_width_px`` at the box's left edge instead of a fill;
        :data:`WashShape.FILL` (the default) fills the full vertical
        extent of the line.
        """
        ok, line_iter = buffer.get_iter_at_line(line_no)
        if not ok:
            return None
        spec = self._spec_at_iter(line_iter)
        if spec is None:
            return None
        line_y_buffer, line_h = self.get_line_yrange(line_iter)
        _, line_y_widget = self.buffer_to_window_coords(
            Gtk.TextWindowType.TEXT, 0, line_y_buffer,
        )
        box_x = metrics.left_margin + spec.box_left_inset_px
        box_w = (
            metrics.width
            - metrics.left_margin
            - metrics.right_margin
            - spec.box_left_inset_px
            - spec.box_right_inset_px
        )
        rect = Graphene.Rect()
        if spec.shape is WashShape.HAIRLINE:
            # A 1-px rule at the bottom of the line rather than a
            # full-height fill: the divider between the metadata line
            # and the body. ``pixels-below-lines`` on the metadata tag
            # opens the gap above it, so the rule sits clear of the
            # text.
            rect.init(
                float(box_x),
                float(line_y_widget + line_h - _HAIRLINE_THICKNESS_PX),
                float(box_w),
                float(_HAIRLINE_THICKNESS_PX),
            )
        elif spec.shape is WashShape.LEFT_BAR:
            # A thin vertical rule at the box's left edge, no fill: the
            # blockquote left rule. Spans the same vertical extent a
            # FILL shape would, so stacking the per-line rects across a
            # multi-line body forms one continuous rule.
            rect.init(
                float(box_x),
                float(line_y_widget),
                float(spec.bar_width_px),
                float(line_h),
            )
        else:
            rect.init(
                float(box_x), float(line_y_widget),
                float(box_w), float(line_h),
            )
        return _rgba_from_tint(spec.tint), rect

    def _spec_at_iter(self, line_iter: Gtk.TextIter) -> WashSpec | None:
        """Return the :class:`WashSpec` for the line's wash-bearing tag.

        Returns ``None`` when the iter carries no wash-bearing tag.
        Raises :class:`ValueError` when the iter carries more than
        one wash-bearing tag — see :meth:`_compute_wash_rects` for
        the mutual-exclusion contract.
        """
        matching: list[WashSpec] = []
        for tag in line_iter.get_tags():
            spec = self._wash_specs_by_tag.get(tag)
            if spec is not None:
                matching.append(spec)
        if not matching:
            return None
        if len(matching) > 1:
            raise ValueError(
                "more than one wash-bearing tag on the same iter "
                "violates the paragraph-tag mutual-exclusion invariant"
            )
        return matching[0]

    def _sheet_top_px(self) -> int:
        """Return the widget-coordinate y at which the note's sheet starts.

        The mirror of :meth:`_sheet_bottom_px`. It is the top of the first
        logical line (mapped to widget coordinates the same way) **minus the
        breathing part of the top margin**, i.e. the top margin less the
        top-gap desk band set by :meth:`set_top_gap_px`. The top margin
        reserves breathing space *plus* the desk gap; the sheet claims only
        the breathing part, so the gap is left as desk above the sheet —
        visible when the note is scrolled to the top, mirroring the band
        below. With the default top gap of ``0`` the sheet starts at the
        very top.

        The value may be negative once the note is scrolled down (the
        breathing margin has passed above the viewport); the pure
        :func:`_sheet_rect_for` helper clamps
        it, so the sheet then fills the upper viewport. An
        empty buffer (the parse-error / no-note state) reports ``0`` so the
        caller paints a full-height blank sheet from the top.
        """
        buffer = self.get_buffer()
        if buffer.get_char_count() == 0:
            return 0
        line_y_buffer, _ = self.get_line_yrange(buffer.get_start_iter())
        _, line_y_widget = self.buffer_to_window_coords(
            Gtk.TextWindowType.TEXT, 0, line_y_buffer,
        )
        return int(
            line_y_widget
            - (self.get_top_margin() - self._top_gap_px)
        )

    def _sheet_bottom_px(self) -> int | None:
        """Return the widget-coordinate y at which the note's sheet ends.

        That is the bottom of the last logical line (via
        :meth:`Gtk.TextView.get_line_yrange` on the end iter, mapped to
        widget coordinates the same way :meth:`_wash_rect_for_line` maps
        wash lines) plus the view's ``bottom-margin``, **minus the
        end-gap desk band** set by :meth:`set_end_gap_px`. The bottom
        margin reserves breathing space *plus* the desk gap; the sheet
        claims only the breathing part, so subtracting the gap leaves
        that band of desk below the sheet — reachable by
        scrolling to the end of a note taller than the viewport. With the
        default end gap of ``0`` the sheet covers the whole margin.

        Returns ``None`` for an empty buffer — the parse-error / no-note
        state — so the caller paints a full-height sheet (a blank page),
        not a sheet that collapses to the top of the view.
        """
        buffer = self.get_buffer()
        if buffer.get_char_count() == 0:
            return None
        line_y_buffer, line_h = self.get_line_yrange(buffer.get_end_iter())
        _, line_y_widget = self.buffer_to_window_coords(
            Gtk.TextWindowType.TEXT, 0, line_y_buffer,
        )
        return int(
            line_y_widget
            + line_h
            + self.get_bottom_margin()
            - self._end_gap_px
        )


def _rgba_from_tint(tint: tuple[float, float, float, float]) -> Gdk.RGBA:
    """Build a :class:`Gdk.RGBA` from a 4-tuple of floats in ``[0, 1]``.

    Used by :class:`ArticleTextView` to translate a
    :class:`ui.note_render.tag_table.WashSpec`'s tint into the
    colour type :meth:`Gtk.Snapshot.append_color` expects. A free
    function (rather than a static method on the subclass) so the
    test suite can call it directly when asserting on wash colours.
    """
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = tint
    return rgba


def _sheet_rect_for(
    sheet_top_px: int,
    sheet_bottom_px: int | None,
    width_px: int,
    height_px: int,
    sheet_tint: tuple[float, float, float, float],
) -> tuple[Gdk.RGBA, Graphene.Rect]:
    """Return the opaque sheet rect painted behind the note's content.

    ``sheet_top_px`` is the widget-coordinate y at which the content
    begins (its breathing margin), and ``sheet_bottom_px`` the y at which
    it ends, or ``None`` for an empty buffer. The sheet spans the full
    width between the two; ``sheet_top_px`` is clamped up to ``0`` (the
    breathing margin scrolled above the viewport top) and
    ``sheet_bottom_px`` down to ``height_px`` (content filling or passing
    the viewport, or an empty buffer). Above the top and below the bottom
    the view paints nothing, so the parent's background — the desk — shows
    through equally before and after the note.

    A free function (not a method) so the rect geometry is unit-testable
    without a realised :class:`Gtk.TextView` or font, mirroring
    :func:`_rgba_from_tint`.
    """
    if sheet_bottom_px is None or sheet_bottom_px >= height_px:
        sheet_bottom_px = height_px
    top = max(0, sheet_top_px)
    rect = Graphene.Rect()
    rect.init(
        0.0, float(top), float(width_px), float(max(0, sheet_bottom_px - top)),
    )
    return _rgba_from_tint(sheet_tint), rect
