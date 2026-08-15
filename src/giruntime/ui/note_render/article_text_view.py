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
* **The view decides which colour scheme the note is drawn in**, because it
  is the widget that knows: :meth:`Gtk.Widget.get_color` reports the
  foreground the active theme resolved *for this widget*, and its luminance
  says whether the chrome around the sheet is light or dark. That is a
  measurement of the outcome, so it is correct however the user got there —
  the settings portal, ``GTK_THEME``, a ``settings.ini``, a third-party dark
  theme — with no D-Bus, no gsettings and no libadwaita. Neither
  ``Gtk.Settings`` probe would do: under ``GTK_THEME=Adwaita:dark`` the theme
  name still reads ``"Default"`` and ``gtk-application-prefer-dark-theme``
  still reads :data:`False`.
* The trigger is :meth:`do_css_changed`, GTK's own "your style changed"
  hook, so a live theme flip re-themes the note with no polling and no
  subscription bookkeeping. Because each surface measures itself, a note
  window and the help window agree by construction rather than by sharing a
  theme-manager singleton.
* **The note's default ink is a widget property, not a tag.** Body text
  and headings set no foreground of their own, so without this they
  inherit the *theme's* — invisible on the application-painted sheet
  whenever the two disagree (white ink on the white sheet under a dark
  theme). The ink is therefore applied as ``color`` on the view's CSS
  node, from the palette, via a display-wide provider scoped to this
  widget's style class.

  It was briefly a lowest-priority :class:`Gtk.TextTag` applied across
  the whole buffer instead. That is *correct* but not *safe*: applying a
  tag over the entire buffer after the content is inserted invalidates
  the text layout again, and the first paint that follows uses estimated
  line heights — so the sheet and the block washes, which are computed
  from :meth:`Gtk.TextView.get_line_yrange`, were painted a block short
  and stayed that way until some unrelated redraw (a mouse move) fixed
  them. A CSS colour changes no layout at all and cannot reintroduce it.
* Re-theming **never re-renders**. It re-colours the existing tags in place
  (:func:`tag_table.apply_palette`), re-installs the wash map and the sheet,
  and queues a draw — the buffer's text is untouched, so there is no
  re-parse and the reader keeps their scroll position.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gi.repository import Gdk, Graphene, Gsk, Gtk

from enums import ColorScheme, WashShape
from giruntime.ui.note_render.palette import (
    Palette,
    palette_for,
    scheme_for_foreground,
)
from giruntime.ui.note_render.tag_table import (
    SheetWash,
    WashSpec,
    apply_palette,
    build_sheet_wash,
    build_wash_specs,
)


type ColorSchemeProbe = Callable[[], ColorScheme]
""""Which colour scheme should the note be drawn in, right now?"

Injected so the re-theme path is testable without depending on the
compositor's theme: production passes
:meth:`ArticleTextView._scheme_from_style`, which measures the widget's
own resolved foreground, and tests pass a fake that returns whichever
scheme the case is about. Follows the same seam pattern as
``PaneVisibilityPredicate`` in :mod:`giruntime.ui.note_view`.
"""


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


_ink_provider: Gtk.CssProvider | None = None
"""The display-wide provider carrying the note's default ink.

Module-level because a :class:`Gtk.CssProvider` is added to a *display*,
not a widget — per-widget providers need :class:`Gtk.StyleContext`,
deprecated since GTK 4.10. One provider for every article surface is
also what we want: the note view and the help window are always in the
same colour scheme, so there is nothing per-instance to vary.
"""


def _apply_article_ink(palette: Palette) -> None:
    """Set the default text colour of every article surface.

    Writes ``color`` on both the widget node and its ``text`` child, for
    widgets carrying :data:`_ARTICLE_TEXT_VIEW_CSS_CLASS` — so it reaches
    the article view and nothing else, notably not the source editor,
    which is also a :class:`Gtk.TextView` subclass and correctly follows
    the theme. Both nodes are needed: the ``text`` node alone leaves the
    glyphs black, because the text layout takes its default colour from
    the *widget's* resolved colour (verified by measuring rendered
    pixels, not by reading the docs).

    The colour still comes from the palette, so
    :mod:`giruntime.ui.note_render.palette` remains the single home for
    it; only the *mechanism* is CSS. A :class:`Gtk.TextTag` foreground
    beats a CSS colour, so every tag that sets its own — link, metadata,
    admonition kind labels, the notice lines — still wins on its range.

    Returns silently without a display, the same guard the application's
    stylesheet loading uses for embedded and test contexts.
    """
    global _ink_provider  # pylint: disable=global-statement
    display = Gdk.Display.get_default()
    if display is None:
        return
    if _ink_provider is None:
        _ink_provider = Gtk.CssProvider.new()
        Gtk.StyleContext.add_provider_for_display(
            display,
            _ink_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
    _ink_provider.load_from_string(
        f".{_ARTICLE_TEXT_VIEW_CSS_CLASS},"
        f" .{_ARTICLE_TEXT_VIEW_CSS_CLASS} text {{"
        f" color: {palette.body_foreground}; }}"
    )


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
    _color_scheme: ColorScheme
    _scheme_probe: ColorSchemeProbe
    _tag_table: Gtk.TextTagTable | None

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
        # The desk bands the sheet does not cover, above and below the
        # content. Both zero until NoteView sets them alongside the
        # margins; at zero the sheet covers the whole top/bottom margin —
        # the pre-gap behaviour that test code constructing a bare view
        # relies on. They are set from the same constant so they match.
        self._top_gap_px = 0
        self._end_gap_px = 0
        # The scheme starts LIGHT and is corrected by the first
        # ``css_changed``. It cannot be measured here: an unrealised
        # widget has no resolved style yet, and guessing from a
        # not-yet-valid colour would mean building the tag table in one
        # palette and immediately re-colouring it in another.
        self._color_scheme = ColorScheme.LIGHT
        # The sheet colour has no per-note parameters, so it is resolved
        # from the current palette here and again on every scheme change
        # (see :meth:`_sync_color_scheme`). It is the one palette value
        # kept in a field: :meth:`do_snapshot` reads it every frame.
        self._sheet_wash = build_sheet_wash(self.palette())
        _apply_article_ink(self.palette())
        self._scheme_probe = self._scheme_from_style
        # Set by :meth:`install_wash_specs_from_table`. Until then the
        # view has no table to re-colour, so a theme change is a no-op
        # rather than a reach into GTK's default buffer.
        self._tag_table = None

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

        The tints come from the view's *current* palette, and the table
        is retained so a later theme change can re-colour that same
        table and re-install the map (see :meth:`do_css_changed`) —
        which is why this is the seam both consumers already share.
        """
        self._tag_table = tag_table
        specs_by_tag: dict[Gtk.TextTag, WashSpec] = {}
        for tag_name, spec in build_wash_specs(self.palette()).items():
            tag = tag_table.lookup(tag_name.value)
            if tag is not None:
                specs_by_tag[tag] = spec
        self.install_wash_specs(specs_by_tag)

    def install_scheme_probe(self, probe: ColorSchemeProbe) -> None:
        """Replace how the view decides its colour scheme, and re-theme.

        The test seam for the whole dark-mode path: production leaves
        the default (measure the widget's own resolved foreground),
        while a test installs a fake returning a fixed scheme and gets a
        deterministic re-theme without a themed compositor. Applying
        immediately — rather than waiting for the next style change —
        is what makes it usable as an arrange step.
        """
        self._scheme_probe = probe
        self._sync_color_scheme()

    def color_scheme(self) -> ColorScheme:
        """Return the colour scheme this view is currently drawn in."""
        return self._color_scheme

    def palette(self) -> Palette:
        """Return the palette this view is currently drawn in.

        Derived from the scheme rather than stored beside it: two fields
        that must agree are two fields that can drift, and the lookup is
        a dict access.

        :func:`giruntime.ui.note_view.build_article_surface` reads this
        to build the tag table in the same palette the view will paint
        its sheet and washes in, so the surface starts self-consistent
        rather than relying on the first style change to align it.
        """
        return palette_for(self._color_scheme)

    def do_css_changed(  # pylint: disable=arguments-differ
        self, change: Gtk.CssStyleChange,
    ) -> None:
        """Re-theme the note when the widget's resolved style changes.

        GTK's own hook for "your CSS style just changed", which fires on
        a theme switch — and also on ordinary state changes (hover,
        focus), so this must stay cheap and idempotent.
        :meth:`_sync_color_scheme` returns immediately when the scheme
        is unchanged, which is the overwhelmingly common case.

        The parent implementation runs first so the widget's own style
        bookkeeping is done before the colour is read back.
        """
        Gtk.TextView.do_css_changed(self, change)
        self._sync_color_scheme()

    def _scheme_from_style(self) -> ColorScheme:
        """Classify the theme's resolved foreground into a colour scheme.

        The default probe. :meth:`Gtk.Widget.get_color` gives the colour
        the active theme resolved for a widget; its luminance says
        whether the surrounding chrome is light or dark (see
        :func:`giruntime.ui.note_render.palette.scheme_for_foreground`).

        It reads the **parent**, not ``self``, and that is essential
        rather than incidental: this view's own colour is the palette's
        ink, written by :func:`_apply_article_ink`. Measuring it would
        feed the view its own output — it would read dark ink, conclude
        "light theme", and could never leave whichever scheme it was
        already in. The parent (the article container) carries no such
        override, so its colour is the theme's answer.

        Falls back to the current scheme while unparented, which is the
        construction window before the container adopts the view; the
        first style change after that corrects it.
        """
        parent = self.get_parent()
        if parent is None:
            return self._color_scheme
        color = parent.get_color()
        return scheme_for_foreground(color.red, color.green, color.blue)

    def _sync_color_scheme(self) -> None:
        """Adopt the probed colour scheme, re-theming if it changed.

        The whole re-theme, and deliberately short: re-colour the tags
        in place, rebuild and re-install the wash map, swap the sheet,
        queue a draw. No text is touched, so no re-parse and no re-render
        happen and the reader's scroll position survives.

        Returns early when the scheme is unchanged (every hover and
        focus change lands here) or when no tag table has been installed
        yet (a bare view built by a test, or the window between
        construction and :meth:`install_wash_specs_from_table`).
        """
        scheme = self._scheme_probe()
        if scheme is self._color_scheme:
            return
        self._color_scheme = scheme
        palette = self.palette()
        self._sheet_wash = build_sheet_wash(palette)
        _apply_article_ink(palette)
        if self._tag_table is not None:
            apply_palette(self._tag_table, palette)
            self.install_wash_specs_from_table(self._tag_table)
        self.queue_draw()

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

        **The text layer is built first and appended last.** Painting
        order is unchanged; *computation* order is not. The sheet and the
        washes derive their geometry from :meth:`Gtk.TextView.get_line_yrange`,
        which reads per-line heights out of the ``GtkTextBTree`` — and
        those heights are only correct once ``GtkTextLayout`` has been
        validated. Validation happens inside the parent snapshot:
        ``gtk_text_view_paint`` flushes the pending first-validate idle
        before it draws, because drawing has no way to fix bad heights.
        Computing layers 1 and 2 before that flush therefore reads
        *unvalidated* lines, which report height 0 — the whole wash layer
        collapses into degenerate rects at the document's end, and no
        corrected frame follows, because the ``queue_draw`` the
        validation emits is issued from inside the snapshot and is
        swallowed (``gtk_widget_do_snapshot`` clears ``draw_needed``
        after ``create_render_node`` returns). Building the text layer
        into a detached :class:`Gtk.Snapshot` first forces the flush, so
        layers 1 and 2 are computed against a validated layout.

        The invariant: **no geometry may be read from the text layout
        before the text layer has been built.**
        """
        text_node = self._snapshot_text_layer()
        sheet_top = self._sheet_top_px()
        sheet_bottom = self._sheet_bottom_px()
        width = self.get_width()
        height = self.get_height()
        sheet = _sheet_rect_for(
            sheet_top, sheet_bottom, width, height,
            self._sheet_wash.tint,
        )
        snapshot.append_color(*sheet)
        washes = self._compute_wash_rects()
        for color, rect in washes:
            snapshot.append_color(color, rect)
        if text_node is not None:
            snapshot.append_node(text_node)

    def _snapshot_text_layer(self) -> Gsk.RenderNode | None:
        """Build the parent's text layer into a detached snapshot.

        Returns the resulting node, or ``None`` when the parent painted
        nothing (an empty buffer produces no node —
        :meth:`Gtk.Snapshot.to_node` returns ``None`` rather than an
        empty container, so callers must not assume a node exists).

        Called first by :meth:`do_snapshot` for its *side effect* as much
        as its result: the parent snapshot flushes ``GtkTextLayout``
        validation, and the sheet and wash geometry are only correct once
        that has happened. The node is appended last so painting order is
        unaffected.

        A detached :class:`Gtk.Snapshot` starts with an identity
        transform and no clip, which is the state the snapshot GTK hands
        to :meth:`do_snapshot` is also in — the widget's own transform
        and clip are applied by ``gtk_widget_create_render_node`` around
        the call, not inside it. The layer is therefore recorded in
        widget-local coordinates either way.

        The node also carries anything else the parent snapshots,
        including widgets at :class:`Gtk.TextChildAnchor`\\ s. That is
        currently vacuous — ``textbuffer_renderer`` renders *every*
        block-level construct as buffer text or an inline paintable and
        anchors no widget at all (tables, once the sole anchored widget,
        are native text; images go through
        :meth:`Gtk.TextBuffer.insert_paintable`) — so this layer is only
        ever text. **A construct that anchors a widget would change
        that**, and this method would need re-checking: such children
        would move inside the detached node rather than being snapshotted
        against the live one.
        """
        text_snapshot = Gtk.Snapshot()
        Gtk.TextView.do_snapshot(self, text_snapshot)
        return text_snapshot.to_node()

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
