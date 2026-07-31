"""The fixed-width article column: geometry and scrolling.

Principles & invariants
-----------------------
* :class:`ArticleContainer` is a :class:`Gtk.Widget` that also implements
  :class:`Gtk.Scrollable`. It *establishes and enforces* the fixed-width text
  column and owns scrolling — the *geometry* half of the reading surface,
  distinct from the *appearance* half in
  :mod:`giruntime.ui.note_render.article_text_view`. It names no
  :class:`~asciidoc.ast.Document`, renderer, or tag-table symbol; the renderer
  merely *reads* the width it exposes, via an injected
  :data:`~ui.note_render.textbuffer_renderer.ColumnWidthMeasurer`,
  so the two stay decoupled.
* Vertical scrolling is pass-through: the container forwards the scrolled
  window's ``vadjustment`` / ``vscroll-policy`` to the (already scrollable)
  text view, which is the vertical scrollport. Horizontal is container-owned:
  wider than the column it centres the child; narrower it configures its own
  ``hadjustment`` and pans. The column never shrinks and the font never scales
  with window width.
* ``Gtk.Box`` cannot be the base (it delegates ``measure`` / ``size_allocate``
  to its layout manager, so Python overrides would be dead code); the only
  correct base is ``Gtk.Widget`` with manual single-child management via
  ``set_parent`` / ``unparent`` and :meth:`Gtk.Widget.allocate`, released at
  teardown through one guarded :meth:`_release_child` (from ``do_unroot`` in
  production, with a ``__del__`` net for never-rooted test widgets).
* The column width and the four article margins derive from injected Pango
  measurements (:data:`CharWidthMeasurer` / :data:`LineHeightMeasurer`), each
  invoked once and cached for the container's lifetime; a zero measurement
  falls back to :data:`_FALLBACK_CHAR_WIDTH_PX` / :data:`_FALLBACK_LINE_HEIGHT_PX`.
"""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import GObject, Graphene, Gsk, Gtk

from config.defaults import ARTICLE_INNER_HPADDING_CHARS, TARGET_CHARS_PER_LINE


type CharWidthMeasurer = Callable[[], int]
"""Callable returning the pixel width of a single representative glyph.

Injected at construction of :class:`ArticleContainer` so tests can pass
a fixed integer and production can wire a Pango-layout-based measurer
that runs against the live ``Gtk.TextView``. The result is cached after
the first call — see :meth:`ArticleContainer.char_width_px`.
"""


type LineHeightMeasurer = Callable[[], int]
"""Callable returning the pixel height of one line in the body font.

Injected the same way as :data:`CharWidthMeasurer` so tests can pass a
fixed integer; production wires a Pango-layout-based measurer that lays
out a single glyph and returns ``log_rect.height``. The result is
cached after the first call alongside the M-width measurement — see
:meth:`ArticleContainer.line_height_px`.
"""


_FALLBACK_CHAR_WIDTH_PX: int = 8
"""Defensive fallback if the production measurer reports a non-positive
width. A real font's "M" is never zero pixels wide, but defending
against a corner case (e.g. measuring before the widget has any font at
all) keeps the column at least usable rather than collapsing to zero.
"""


_FALLBACK_LINE_HEIGHT_PX: int = 2 * _FALLBACK_CHAR_WIDTH_PX
"""Defensive fallback for a non-positive line-height measurement.

Mirrors :data:`_FALLBACK_CHAR_WIDTH_PX`: fonts don't have a zero line
height in practice, but the symmetry with the M-width fallback keeps
the container drawable in pathological cases (e.g. measuring before
the widget has any font at all). The chosen value (16 px) matches the
default body-text line height of a 12-13 px font, which is sensible
for the rest of the app's chrome.
"""


_HSCROLL_STEP_FRACTION: float = 0.1
"""Fraction of the viewport width used as the horizontal adjustment's
*step* increment when :class:`ArticleContainer` configures the
container-owned ``hadjustment`` (arrow-key / button scroll granularity).
The pair :data:`_HSCROLL_STEP_FRACTION` / :data:`_HSCROLL_PAGE_FRACTION`
keeps :meth:`ArticleContainer.do_size_allocate` free of bare numeric
literals for the two increments ``Gtk.Adjustment.configure`` requires."""


_HSCROLL_PAGE_FRACTION: float = 0.9
"""Fraction of the viewport width used as the horizontal adjustment's
*page* increment (page-up / page-down scroll granularity). Companion to
:data:`_HSCROLL_STEP_FRACTION`; see
:meth:`ArticleContainer.do_size_allocate`."""


class ArticleContainer(Gtk.Widget, Gtk.Scrollable):
    """A scrollable, fixed-width article column wrapping a single child.

    The container holds a single child (in production, the rendered-view
    :class:`Gtk.TextView`) and enforces the column-width rule from §2 of
    the plan. It implements :class:`Gtk.Scrollable` so the parent
    ``Gtk.ScrolledWindow`` keeps it as its **direct** child and interposes
    **no** ``Gtk.Viewport``. That is the structural fix from Option C of
    the plan: with no separate viewport, no other widget caches a stale
    vertical extent, so the rendered pane shows the correct vertical
    scrollbar on first launch even for a note whose last line is a
    static-size image.

    The two axes are treated differently because they have different
    owners:

    * **Vertical — pass-through.** The container forwards the scrolled
      window's :attr:`vadjustment` and :attr:`vscroll_policy` straight
      down to the (already scrollable) text view. The text view becomes
      the vertical scrollport: it owns the scroll extent and writes the
      correct ``vadjustment.upper`` as part of its own layout, exactly as
      it does when it is the direct child of a ``Gtk.ScrolledWindow``.
      The widget that commits the extent is now the widget that knows the
      height, so there is no separate viewport holding a stale value —
      this is what removes the bug. The forwarding is wired from the
      ``notify::vadjustment`` / ``notify::vscroll-policy`` handlers and
      re-applied in :meth:`set_child` (the child may be set before or
      after the scrolled window installs the adjustments).
    * **Horizontal — owned by the container.** The reading column has a
      fixed width that can exceed the viewport, and scrolling a fixed,
      centred column is a container-level translation, not something the
      text view can do by scrolling its own (wrapped-to-width) content.
      So the container keeps ownership of the :attr:`hadjustment`: in
      :meth:`do_size_allocate` it configures the adjustment
      (``upper = max(outer column width, allocated width)``,
      ``page_size = allocated width``, value clamped to
      ``column − viewport``), allocates the child at the
      full column width, and translates it horizontally — centring it
      when the viewport is wider than the column and offsetting it by
      ``−hadjustment.value`` when narrower. Its overflow is ``HIDDEN`` so
      the column is clipped to the viewport, and it re-allocates on the
      adjustment's ``value-changed`` so a horizontal scroll repositions
      the column.

    The four :class:`Gtk.Scrollable` properties (:attr:`hadjustment`,
    :attr:`vadjustment`, :attr:`hscroll_policy`, :attr:`vscroll_policy`)
    are the interface's required surface. They are plain data properties —
    GObject stores the value and emits ``notify`` — and the container
    reacts via its own ``notify::`` handlers rather than property setters,
    which keeps the two per-axis behaviours above cleanly separated.

    Why ``Gtk.Widget`` and not ``Gtk.Box``: in GTK 4, ``Gtk.Box`` uses a
    ``BoxLayout`` *layout manager*, and the widget-level
    :meth:`measure` / :meth:`size_allocate` vfuncs on ``Gtk.Box``
    delegate to that layout manager at the C level. A Python override
    of :meth:`do_measure` / :meth:`do_size_allocate` on a ``Gtk.Box``
    subclass is therefore dead code — never reached at runtime, even
    though calling those methods directly from Python (as a unit test
    might) appears to work. ``Gtk.Widget`` has no such indirection.

    Single-child management is manual: :meth:`set_child` replaces
    :meth:`Gtk.Box.append`; it unparents any prior child and parents
    the new one via :meth:`Gtk.Widget.set_parent`. The child shows up
    via :meth:`Gtk.Widget.get_first_child` exactly as under any other
    ``Gtk.Widget`` parent.

    Construction takes a :data:`CharWidthMeasurer` and a
    :data:`LineHeightMeasurer`. Each measurer is invoked exactly once
    across the container's lifetime — the result is cached and reused
    by all subsequent getter calls. Three width getters
    (:meth:`text_column_width`, :meth:`outer_column_width`,
    :meth:`char_width_px`) are derived from the M-width measurement;
    one (:meth:`line_height_px`) from the line-height measurement. The
    :class:`NoteView` owns the *outer* widget size (used by the two
    vfuncs) while the renderer is fed the *text* width — the inner
    horizontal padding sits between the two and is enforced by the
    :class:`Gtk.TextView`'s ``left-margin`` / ``right-margin``.
    """

    # ----- Gtk.Scrollable interface properties -----
    # The interface defines exactly these four properties; implementing it
    # in Python means declaring them as data properties (GObject stores the
    # value and auto-emits ``notify``). The ``*_policy`` attribute names map
    # to the hyphenated GObject names (``hscroll-policy`` / ``vscroll-policy``)
    # that the interface and the parent ``Gtk.ScrolledWindow`` use. The
    # default ``MINIMUM`` policy matches a plain scrollable child.
    hadjustment: Gtk.Adjustment | None = GObject.Property(
        type=Gtk.Adjustment,
        default=None,
    )
    vadjustment: Gtk.Adjustment | None = GObject.Property(
        type=Gtk.Adjustment,
        default=None,
    )
    hscroll_policy: Gtk.ScrollablePolicy = GObject.Property(
        type=Gtk.ScrollablePolicy,
        default=Gtk.ScrollablePolicy.MINIMUM,
    )
    vscroll_policy: Gtk.ScrollablePolicy = GObject.Property(
        type=Gtk.ScrollablePolicy,
        default=Gtk.ScrollablePolicy.MINIMUM,
    )

    _char_width_measurer: CharWidthMeasurer
    _line_height_measurer: LineHeightMeasurer
    _cached_char_width_px: int | None
    _cached_line_height_px: int | None
    _child: Gtk.Widget | None
    # The container owns the horizontal axis, so it tracks the adjustment
    # the parent installs and the handler id of the ``value-changed``
    # subscription on it — both reset to "unconnected" sentinels so the
    # teardown / re-installation paths stay idempotent.
    _connected_hadjustment: Gtk.Adjustment | None
    _hadjustment_value_changed_id: int

    def __init__(
        self,
        *,
        char_width_measurer: CharWidthMeasurer,
        line_height_measurer: LineHeightMeasurer,
    ) -> None:
        super().__init__()
        self._char_width_measurer = char_width_measurer
        self._line_height_measurer = line_height_measurer
        self._cached_char_width_px = None
        self._cached_line_height_px = None
        self._child = None
        self._connected_hadjustment = None
        self._hadjustment_value_changed_id = 0
        # Clip the fixed-width column to the viewport: when the window is
        # narrower than the column, the overflow must be hidden (and only
        # reachable via the horizontal scrollbar), not painted past the
        # viewport edge. With no interposed ``Gtk.Viewport`` to clip for
        # us, this is the container's own responsibility.
        self.set_overflow(Gtk.Overflow.HIDDEN)
        # React to the parent ``Gtk.ScrolledWindow`` installing (or later
        # replacing) the adjustments / policies. Vertical changes are
        # passed straight through to the scrollable child; a new
        # horizontal adjustment is tracked so a scroll re-runs allocation.
        self.connect("notify::vadjustment", self._on_vertical_scroll_changed)
        self.connect(
            "notify::vscroll-policy",
            self._on_vertical_scroll_changed,
        )
        self.connect("notify::hadjustment", self._on_hadjustment_changed)

    def set_child(self, child: Gtk.Widget) -> None:
        """Attach the container's single child, replacing any prior one.

        Unparents the previously held child (if any) before parenting
        the new one via :meth:`Gtk.Widget.set_parent`, which is the GTK
        4 API for adding a child to a custom ``Gtk.Widget`` container
        that manages its child manually (i.e. without a layout
        manager). The child becomes visible via the standard
        :meth:`Gtk.Widget.get_first_child` walk after this call.
        """
        if self._child is not None:
            self._child.unparent()
        self._child = child
        child.set_parent(self)
        # The child may be set *before* the parent ``Gtk.ScrolledWindow``
        # installs the adjustments (it is, in :class:`NoteView`), so push
        # the current vertical adjustment + policy down now; the
        # ``notify::`` handlers cover the opposite order.
        self._forward_vertical_scrolling_to_child()

    def _on_vertical_scroll_changed(
        self,
        _source: ArticleContainer,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """Forward the vertical adjustment + policy to the scrollable child.

        Vertical pass-through (Option C): the text view — a
        ``Gtk.Scrollable`` — becomes the vertical scrollport and owns the
        v-extent, so the widget that commits ``vadjustment.upper`` is the
        widget that knows the height. That is what removes the original
        scrollbar bug: there is no separate viewport caching a stale
        extent. Fires on both ``notify::vadjustment`` and
        ``notify::vscroll-policy`` because the child needs whichever the
        scrolled window changed.
        """
        self._forward_vertical_scrolling_to_child()

    def _forward_vertical_scrolling_to_child(self) -> None:
        """Push the current vertical adjustment + policy onto the child.

        A no-op unless the child is a ``Gtk.Scrollable`` (the production
        text view is; the bare ``Gtk.Widget`` stand-ins the unit tests use
        are not, and they exercise only the horizontal-allocation path).
        Passing ``None`` is valid — it clears the child's adjustment — so
        an early call before the scrolled window installs one is harmless.
        """
        if isinstance(self._child, Gtk.Scrollable):
            self._child.set_vadjustment(self.get_vadjustment())
            self._child.set_vscroll_policy(self.get_vscroll_policy())

    def _on_hadjustment_changed(
        self,
        _source: ArticleContainer,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """Track the container-owned horizontal adjustment.

        The container owns the horizontal axis: the fixed column can be
        wider than the viewport and is scrolled by *translating* the child
        in :meth:`do_size_allocate`, not by the text view scrolling its
        own wrapped content. A horizontal scroll therefore has to re-run
        size-allocate to reposition the column, so this connects the new
        adjustment's ``value-changed`` to :meth:`Gtk.Widget.queue_allocate`.
        Any previously tracked adjustment is disconnected first so a
        replaced adjustment leaves no dangling handler.
        """
        self._disconnect_hadjustment()
        adjustment: Gtk.Adjustment | None = self.get_hadjustment()
        if adjustment is not None:
            self._connected_hadjustment = adjustment
            self._hadjustment_value_changed_id = adjustment.connect(
                "value-changed",
                self._on_hadjustment_value_changed,
            )

    def _on_hadjustment_value_changed(
        self,
        _adjustment: Gtk.Adjustment,
    ) -> None:
        """Re-position the column after a horizontal scroll.

        :meth:`Gtk.Widget.queue_allocate` re-runs :meth:`do_size_allocate`
        (without re-measuring), which re-reads the adjustment's value and
        applies the matching translate-X offset to the child. In the
        steady state ``do_size_allocate`` re-``configure``\\ s the
        adjustment to the same value, which emits no further
        ``value-changed``, so there is no allocation loop.
        """
        self.queue_allocate()

    def _disconnect_hadjustment(self) -> None:
        """Drop the ``value-changed`` subscription on the tracked adjustment.

        Idempotent and self-guarding (mirrors :meth:`_release_child`): the
        teardown hooks and the re-installation path in
        :meth:`_on_hadjustment_changed` can all call it without
        double-disconnecting. The adjustment is owned by the parent
        ``Gtk.ScrolledWindow``, so dropping the handler here prevents the
        closure from outliving the container.
        """
        if (
            self._connected_hadjustment is not None
            and self._hadjustment_value_changed_id != 0
        ):
            self._connected_hadjustment.disconnect(
                self._hadjustment_value_changed_id,
            )
        self._connected_hadjustment = None
        self._hadjustment_value_changed_id = 0

    def _release_child(self) -> None:
        """Unparent the single child if it is still parented to us.

        The lone place that severs the manual ``set_parent`` link. It is
        idempotent and self-guarding: it only unparents when a child is
        held *and* that child's parent is still this container, so the
        two teardown hooks below (:meth:`do_unroot` and :meth:`__del__`)
        can both call it without double-unparenting.
        """
        if self._child is not None and self._child.get_parent() is self:
            self._child.unparent()
        self._child = None

    def do_unroot(self) -> None:  # pylint: disable=arguments-differ
        """Release the manually parented child when leaving the widget tree.

        A custom ``Gtk.Widget`` that parents a child via
        :meth:`Gtk.Widget.set_parent` (as :meth:`set_child` does) owns
        that link and must drop it at teardown — GTK does not
        auto-unparent the children of a bare ``Gtk.Widget`` subclass the
        way it does for a ``Gtk.Box``. The natural hook would be
        ``dispose``, but PyGObject does not expose ``GObject``'s
        ``dispose`` vfunc for overriding, so ``do_unroot`` — which GTK
        invokes synchronously while tearing the window's widget tree
        down — is the reliable equivalent for any *rooted* container.
        Without this the container is finalized with the child still
        parented and GTK warns *"Finalizing … but it still has children
        left"*. The container is never re-rooted in this application
        (the :class:`NoteView` lives for the window's lifetime), so
        unparenting here is safe. The :meth:`__del__` below is the
        companion net for the never-rooted case (see its docstring).
        """
        self._disconnect_hadjustment()
        self._release_child()
        Gtk.Widget.do_unroot(self)

    def __del__(self) -> None:
        """Release the child for a container that is finalized un-rooted.

        :meth:`do_unroot` only fires for a container that was added to a
        window; a container built in isolation and dropped (as the unit
        tests do) is finalized without ever being rooted, so the
        unparent has to happen here instead. The container holds the
        only reference to its child via :meth:`Gtk.Widget.set_parent`,
        so the child is guaranteed still alive at this point; the
        :meth:`_release_child` guard makes this a no-op when
        :meth:`do_unroot` already ran.
        """
        self._disconnect_hadjustment()
        self._release_child()

    def char_width_px(self) -> int:
        """Return the cached measured width of the reference glyph.

        Computed via the injected :data:`CharWidthMeasurer` on the
        first call and cached afterwards. A non-positive measurement
        is replaced by :data:`_FALLBACK_CHAR_WIDTH_PX` so derived
        widths never collapse to zero pixels.
        """
        if self._cached_char_width_px is None:
            measured = self._char_width_measurer()
            self._cached_char_width_px = (
                measured if measured > 0 else _FALLBACK_CHAR_WIDTH_PX
            )
        return self._cached_char_width_px

    def line_height_px(self) -> int:
        """Return the cached measured pixel height of one body-font line.

        Computed via the injected :data:`LineHeightMeasurer` on the
        first call and cached afterwards. A non-positive measurement
        is replaced by :data:`_FALLBACK_LINE_HEIGHT_PX` for the same
        defensive reason as :meth:`char_width_px`.
        """
        if self._cached_line_height_px is None:
            measured = self._line_height_measurer()
            self._cached_line_height_px = (
                measured if measured > 0 else _FALLBACK_LINE_HEIGHT_PX
            )
        return self._cached_line_height_px

    def text_column_width(self) -> int:
        """Return the pixel width of the *text area* (no padding).

        Computed as :data:`TARGET_CHARS_PER_LINE` ×
        :meth:`char_width_px`. This is what the renderer needs for
        table / image layout — the width of one line of rendered
        prose, not including the inner horizontal padding that the
        :class:`Gtk.TextView`'s ``left-margin`` / ``right-margin`` add
        between the column edge and the text.
        """
        return TARGET_CHARS_PER_LINE * self.char_width_px()

    def outer_column_width(self) -> int:
        """Return the pixel width of the article column including padding.

        Computed as ``(TARGET_CHARS_PER_LINE + 2 ×
        ARTICLE_INNER_HPADDING_CHARS)`` × :meth:`char_width_px`. Used
        by :meth:`do_measure` and :meth:`do_size_allocate` as the
        actual widget width — the inner padding sits between this
        outer edge and the text area, so the 66-char text width is
        preserved while the column itself is wider.
        """
        return (
            (TARGET_CHARS_PER_LINE + 2 * ARTICLE_INNER_HPADDING_CHARS)
            * self.char_width_px()
        )

    def do_measure(  # pylint: disable=arguments-differ
        self,
        orientation: Gtk.Orientation,
        _for_size: int,
    ) -> tuple[int, int, int, int]:
        """Report the column width horizontally; defer the v-extent.

        On the horizontal axis the *minimum* is ``0`` and the *natural*
        is :meth:`outer_column_width`. Because the container is a
        ``Gtk.Scrollable``, a zero minimum lets the parent
        ``Gtk.ScrolledWindow`` allocate it *narrower* than the column;
        the horizontal scrollbar then exposes the overflow via the
        container-owned :attr:`hadjustment` (configured in
        :meth:`do_size_allocate`), rather than the column being forced to
        shrink. The natural width is the column the pane opens at when
        there is room. ``_for_size`` does not affect the horizontal report.

        On the vertical axis the container contributes nothing
        (``(0, 0, …)``): the vertical extent is owned by the scrollable
        child, which the container wires up as the vertical scrollport via
        :meth:`_forward_vertical_scrolling_to_child` (the text view writes
        its own ``vadjustment.upper`` from its layout). Re-deriving the
        v-extent here would merely reinvent the viewport and could
        reintroduce the stale-extent bug Option C removes. Baselines are
        not meaningful for this widget.
        """
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (0, self.outer_column_width(), -1, -1)
        return (0, 0, -1, -1)

    def do_size_allocate(  # pylint: disable=arguments-differ
        self,
        width: int,
        height: int,
        baseline: int,
    ) -> None:
        """Place and (horizontally) scroll the fixed-width article column.

        The child is always allocated exactly :meth:`outer_column_width`
        pixels wide and ``height`` tall — the column-pinning invariant —
        regardless of the viewport ``width``. Its horizontal position is
        then:

        * **centred** when the viewport is at least as wide as the column
          (``width >= outer``): the slack is split equally on both sides
          and applied as a translate-X :class:`Gsk.Transform`; and
        * **scrolled** when the viewport is narrower (``width < outer``):
          the child is offset by ``−hadjustment.value`` so the horizontal
          scrollbar pans across the column.

        The container owns the horizontal axis, so it configures its
        :attr:`hadjustment` here: the page is the viewport ``width``,
        ``upper`` is the column width but **never less than that page**,
        and the value is clamped to ``column − viewport`` so a stale
        scroll position from a wider layout cannot leave the column
        pinned off-screen. ``HIDDEN`` overflow (set in ``__init__``)
        clips the column to the viewport. The vertical axis is untouched
        here — the child owns it as the forwarded vertical scrollport.

        The ``max`` in ``upper`` is not cosmetic. A scrollable's extent
        is the pair ``(upper, page_size)``, and ``Gtk.ScrolledWindow``
        reads ``value > upper − page_size`` as a horizontal **overshoot**
        — the elastic state a kinetic scroll enters past the end. A bare
        ``upper = column`` makes that difference negative for every
        viewport wider than the column, i.e. the common case, so the
        scrolled window sits in a permanent ``width − column`` overshoot
        and the theme paints its ``overshoot.right`` node (a
        ``currentColor`` radial gradient) over the right half of the
        desk for as long as the pane is wide. Reporting
        ``upper == page_size`` instead says "nothing to scroll", which is
        what a viewport wider than its content means, and matches how the
        vertical axis already behaves (the text view writes an ``upper``
        of at least its page, so a short note draws no glow below the
        sheet).
        """
        if self._child is None:
            return
        outer = self.outer_column_width()
        adjustment: Gtk.Adjustment | None = self.get_hadjustment()
        if adjustment is not None:
            max_offset = max(0, outer - width)
            value = min(adjustment.get_value(), float(max_offset))
            adjustment.configure(
                value,
                0.0,
                float(max(outer, width)),
                width * _HSCROLL_STEP_FRACTION,
                width * _HSCROLL_PAGE_FRACTION,
                float(width),
            )
            scroll_offset = int(adjustment.get_value())
        else:
            scroll_offset = 0
        if width >= outer:
            x_offset = (width - outer) // 2
        else:
            x_offset = -scroll_offset
        transform = _translate_x_transform(x_offset)
        self._child.allocate(outer, height, baseline, transform)


def _translate_x_transform(dx: int) -> Gsk.Transform | None:
    """Return a translate-X :class:`Gsk.Transform`, or ``None`` for ``dx == 0``.

    Used by :meth:`ArticleContainer.do_size_allocate` to position its
    single child. Returning ``None`` for the zero case lets the child's
    :meth:`Gtk.Widget.allocate` take the no-transform fast path —
    matching the GTK 4 idiom of passing ``None`` when no transform is
    needed.
    """
    if dx == 0:
        return None
    point = Graphene.Point()
    point.init(float(dx), 0.0)
    return Gsk.Transform.new().translate(point)
