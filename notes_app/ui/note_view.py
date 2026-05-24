"""The rendered-note pane: a fixed-width article column inside a scroller.

Principles & invariants
-----------------------
* :class:`NoteView` is the pane in which the user reads a note. It is
  stateless with respect to notes — every render rebuilds the buffer
  from scratch, driven by :class:`AppState`. It never calls into
  ``storage`` directly with concrete classes; reads go through
  :class:`NoteRepositoryProtocol` and (from build step 11)
  :class:`AttachmentStoreProtocol`.
* The pane's layout is the three-step stack from §2 of the plan:
  ``Gtk.ScrolledWindow`` (horizontal AUTOMATIC, vertical AUTOMATIC) →
  :class:`ArticleContainer` (a ``Gtk.Widget`` subclass with a single
  child that enforces the fixed-width text column rule) → read-only
  ``Gtk.TextView`` populated by :class:`TextBufferRenderer`. A
  :class:`Gtk.Revealer` containing the parse-error banner is
  *prepended* to that stack — it sits above the scroller at the top
  of the pane and is hidden by default.
* :class:`ArticleContainer` enforces the text-column rule: when allocated
  *wider* than the target column, the slack becomes an equal-on-both-sides
  horizontal translation of the child, centring the column; when allocated
  *narrower*, the child is placed at offset 0 and the parent
  ``ScrolledWindow`` is responsible for the horizontal scrollbar — the
  column never shrinks. The font never scales with window width (see
  §2 / decision 7 of the plan).
* ``Gtk.Box`` subclasses cannot override ``measure`` / ``size_allocate``
  in GTK 4 because ``Gtk.Box`` delegates to its ``BoxLayout`` layout
  manager — those vfuncs are invoked through the layout manager at the
  C level and a Python-level override on the box subclass is dead code.
  The only correct base for this widget is therefore ``Gtk.Widget``,
  with manual single-child management via :meth:`set_parent` /
  :meth:`unparent` and :meth:`Gtk.Widget.allocate` on the child.
  Because that parent link is owned manually, the container must also
  release it at teardown, or GTK finalizes the container with the child
  still parented and warns about leftover children. ``dispose`` — the
  natural hook in C — is not exposed for override by PyGObject, so the
  unparent runs from :meth:`ArticleContainer.do_unroot` (fired by GTK
  while a *rooted* tree is torn down, i.e. in production) with a
  :meth:`ArticleContainer.__del__` net for a container that is
  finalized without ever being rooted (i.e. the standalone widgets the
  unit tests build). Both funnel through one guarded
  :meth:`ArticleContainer._release_child`.
* The target column width is :data:`TARGET_CHARS_PER_LINE` ×
  *measured glyph width*. The measurement is injected as a callable so
  tests can stub it without needing a realised font, and so production
  can wire a closure that uses ``Gtk.Widget.create_pango_layout("M")``
  on the live :class:`Gtk.TextView`. The result is cached for the
  lifetime of the :class:`ArticleContainer` — font changes during a
  session would invalidate the cache, but v1 has no in-app font
  customisation so this is a non-issue.
* The four article margins (top / bottom / left / right) are derived
  from the same injected Pango measurements as the column width — both
  the M-width measurer (existing) and a sibling line-height measurer
  (new). Cached for the container's lifetime via the same
  ``_cached_..._px`` pattern. Top and bottom are :data:`ARTICLE_TOP_MARGIN_LINES`
  / :data:`ARTICLE_BOTTOM_MARGIN_LINES` multiplied by the measured line
  height; left and right are :data:`ARTICLE_INNER_HPADDING_CHARS`
  multiplied by the measured M-width.
* :class:`ArticleContainer` exposes three sizing getters:
  :meth:`text_column_width` (the 66-character text area, passed to the
  renderer for table / image layout), :meth:`outer_column_width` (the
  widget's actual width, including inner horizontal padding on both
  sides, used by :meth:`do_measure` and :meth:`do_size_allocate`), and
  :meth:`line_height_px` / :meth:`char_width_px` (the font-derived
  units the :class:`NoteView` reads when setting the four
  :class:`Gtk.TextView` margins).
* The four ``Gtk.TextView`` margins are set once at
  :meth:`NoteView.__init__`. They do not change on selection or on
  render — :meth:`NoteView.refresh` only rebuilds buffer contents, not
  chrome. (Same lifecycle invariant the rest of this docstring states
  for the widget tree.)
* The article's :class:`Gtk.TextView` is a private subclass
  :class:`_ArticleTextView` that paints tinted block backgrounds
  (admonition, blockquote, code block) at snapshot time. The
  paragraph tags from :mod:`notes_app.asciidoc.tag_table` deliberately
  carry only the *text position* (``accumulative-margin = True`` plus
  ``left-margin`` / ``right-margin`` = inset + one M-width); the
  matching tinted *wash* is painted by this subclass via
  :meth:`do_snapshot`. The wash extends one M-width beyond the text
  on each side, producing the visual "padded card" effect that
  ``paragraph-background-rgba`` cannot reproduce on its own — see
  :class:`notes_app.asciidoc.tag_table.WashSpec` for the per-tag
  parameters. The tag table is therefore built *after* M-width is
  measured (``char_width_px`` is required), and the wash-spec map
  passed to the subclass is keyed by :class:`Gtk.TextTag` objects
  (not names) so per-snapshot tag-lookup work stays O(1).
* The size-allocate vfunc — *not* the ``size-allocate`` signal, which is
  deprecated in GTK 4 — is the documented place to react to a fresh
  allocation. :meth:`ArticleContainer.do_size_allocate` builds a
  translate-X :class:`Gsk.Transform` to position the single child and
  calls :meth:`Gtk.Widget.allocate` on it with that transform. This
  avoids the re-layout cycle that writing ``margin-start`` /
  ``margin-end`` on ``self`` from inside ``size_allocate`` would
  trigger; it is the GTK 4 idiom for "offset the single child by N
  pixels along X without rerunning the parent's layout".
* Image resolution flows through an :data:`ImageBytesResolver` built
  internally by :class:`NoteView` from an injected
  :class:`AttachmentStoreProtocol`. The resolver is a closure over
  ``self``: each call reads :attr:`_current_note_id` (set on every
  :meth:`refresh`) and asks the attachment store for the matching
  metadata-then-bytes. Tests that don't care about images can
  construct :class:`NoteView` with ``attachments=None`` — the
  fallback :func:`_placeholder_image_bytes` resolver is wired and
  every image renders as the renderer's small placeholder paintable
  (a grey rectangle that signals the missing image without aborting
  the document). Tests that *do* care wire a fake
  :class:`AttachmentStoreProtocol`.
* Filename-to-attachment lookup is intentionally O(N) per image
  (linear scan of the metadata list). For the v1 expectation of "a
  handful of images per note" this is dominated by the texture decode
  cost; introducing a per-note dict cache would add stale-cache
  hazards across edits (rename / delete attachment) for no measurable
  win. If the assumption breaks the cache lives at the resolver
  level — keyed by ``(note_id, filename)`` — and the renderer above
  stays untouched.
* The widget tree is constructed once at ``__init__``. :meth:`refresh`
  re-runs the parser and renderer against the currently selected note,
  but never reshapes the widget tree.
* **Parse-error handling.** When the parser raises, :meth:`refresh`
  clears the buffer and reveals the inline-notice banner with a
  user-facing message keyed by :class:`ParseErrorKind`. Selecting a
  note that doesn't parse therefore shows an empty article column
  with a banner pointing at the offending line, *not* the previous
  note's stale render. Banner state and buffer state are kept in
  lockstep — there is no combination "stale buffer + visible banner"
  or "empty buffer + hidden banner".
* The user-facing message table (:func:`_message_for`) lives in this
  module rather than as a method on :class:`ParseError` because the
  parser is pure and reusable; embedding UI copy in it would couple
  the parser to this UI's tone. The mapping is *exhaustive* over
  :class:`ParseErrorKind` so adding a new error kind forces an
  update here — caught by a unit test that iterates the enum.
"""

# pylint: disable=too-many-lines
# This step pushed the file past pylint's default 1000-line ceiling
# because the snapshot-time wash painter (:class:`_ArticleTextView`)
# adds the new subclass, its rect-computation seam, and the wash-spec
# wiring inside :meth:`NoteView.__init__`. The new class is tightly
# coupled to the existing :class:`ArticleContainer` / :class:`NoteView`
# pair — they share the M-width measurement and the tag-table key
# translation — so extracting it to its own module would scatter the
# wiring without improving readability.

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gdk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402

from notes_app.asciidoc.tag_table import (
    WashSpec,
    build_tag_table,
    build_wash_specs,
)
from notes_app.asciidoc.textbuffer_renderer import TextBufferRenderer
from notes_app.config.defaults import (
    ARTICLE_BOTTOM_MARGIN_LINES,
    ARTICLE_INNER_HPADDING_CHARS,
    ARTICLE_TOP_MARGIN_LINES,
    TARGET_CHARS_PER_LINE,
)
from notes_app.controllers.app_state import AppState
from notes_app.enums import LinkScheme, ParseErrorKind
from notes_app.models.parse_error import ParseError
from notes_app.storage.protocols import (
    AttachmentStoreProtocol,
    ImageBytesResolver,
    NoteRepositoryProtocol,
)


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


_BANNER_CSS_CLASS: str = "note-view-banner"
"""CSS class applied to the banner ``Gtk.Box`` so the bundled
stylesheet can style it (warning yellow background, padded
inline-notice look). The class name is stable across releases — the
stylesheet that targets it is shipped with the application.
"""


_ALLOWED_SCHEMES_LIST: str = ", ".join(s.value for s in LinkScheme)
"""Pre-computed comma-joined list of supported link schemes, used in
the user-facing message for :data:`ParseErrorKind.UNSUPPORTED_LINK_SCHEME`.
Computed once at import time so the message is stable and the enum is
queried only once.
"""


def _message_for(kind: ParseErrorKind, line: int) -> str:
    # pylint: disable=too-many-return-statements,too-many-branches
    # The ``match`` is intentionally exhaustive over
    # :class:`ParseErrorKind` — every member produces a distinct
    # user-facing message, so the number of cases equals the size of
    # the enum. Splitting them into a dispatch dict would replace
    # one ``match`` block with a dict literal of equal length and
    # would break Python's pattern-match exhaustiveness story (a
    # missing key fails at runtime, while a missing match arm shows
    # up to type-checkers that understand ``Never``).
    """Return a user-facing message for a parse error.

    The mapping is exhaustive over :class:`ParseErrorKind` — every
    member must produce a sentence. A unit test iterates the enum and
    asserts each kind has an entry, so adding a new kind forces an
    update here at the same time.

    The parser's internal ``ParseError.message`` is *not* shown
    verbatim because those strings are written for developers and
    would confuse end users. The message is short, line-prefixed
    where useful, and mentions the user's most likely fix when
    obvious.
    """
    match kind:
        case ParseErrorKind.UNTERMINATED_CODE_BLOCK:
            return (
                f"Line {line}: a code block was opened but never closed "
                "with `----`."
            )
        case ParseErrorKind.UNKNOWN_BLOCK:
            return (
                f"Line {line}: this construct isn't recognised. Check for "
                "a typo, an unsupported directive, or a misplaced attribute."
            )
        case ParseErrorKind.BAD_IMAGE_MACRO:
            return (
                f"Line {line}: the image macro is malformed. Expected "
                "`image::filename[attrs]`."
            )
        case ParseErrorKind.BAD_INLINE_SPAN:
            return (
                f"Line {line}: an inline formatting marker (`*`, `_`, or "
                "`#`) was opened but not closed on the same line."
            )
        case ParseErrorKind.EMPTY_HEADING:
            return f"Line {line}: a heading marker has no text after it."
        case ParseErrorKind.UNTERMINATED_TABLE:
            return (
                f"Line {line}: a table was opened but never closed with "
                "`|===`."
            )
        case ParseErrorKind.EMPTY_TABLE:
            return f"Line {line}: this table has no rows between the fences."
        case ParseErrorKind.TABLE_ROW_ARITY_MISMATCH:
            return (
                f"Line {line}: a table row has a different number of cells "
                "than the header."
            )
        case ParseErrorKind.BAD_COLS_DIRECTIVE:
            return (
                f"Line {line}: the `[cols=…]` directive is malformed. Each "
                "value must be a positive integer."
            )
        case ParseErrorKind.UNTERMINATED_ADMONITION:
            return (
                f"Line {line}: an admonition block was opened but never "
                "closed with `====`."
            )
        case ParseErrorKind.UNKNOWN_ADMONITION_TYPE:
            return (
                f"Line {line}: unknown admonition kind — expected NOTE, "
                "TIP, IMPORTANT, WARNING, or CAUTION."
            )
        case ParseErrorKind.UNTERMINATED_BLOCKQUOTE:
            return (
                f"Line {line}: a blockquote was opened but never closed "
                "with `____`."
            )
        case ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE:
            return (
                f"Line {line}: the `[quote, …]` directive is malformed. "
                "Expected up to two non-empty fields after `quote`."
            )
        case ParseErrorKind.UNSUPPORTED_LINK_SCHEME:
            return (
                f"Line {line}: this note uses a link scheme that isn't "
                f"supported (only {_ALLOWED_SCHEMES_LIST})."
            )
        case ParseErrorKind.BAD_LINK_MACRO:
            return (
                f"Line {line}: the `link:` macro is malformed. Expected "
                "`link:URL[display text]`."
            )
        case ParseErrorKind.UNTERMINATED_MONOSPACE:
            return (
                f"Line {line}: a backtick-monospace span was opened but "
                "never closed."
            )
        case ParseErrorKind.UNTERMINATED_PASSTHROUGH:
            return (
                f"Line {line}: a `++…++` passthrough was opened but never "
                "closed."
            )
        case ParseErrorKind.BAD_ATTRIBUTE_ENTRY:
            return (
                f"Line {line}: malformed attribute entry. The name must "
                "start with a letter and contain only letters, digits, "
                "underscores, or hyphens."
            )
        case ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER:
            return (
                f"Line {line}: this container only accepts paragraphs — "
                "block-level constructs (headings, lists, code blocks, "
                "tables, admonitions, blockquotes) cannot appear inside it."
            )


def _placeholder_image_bytes(_filename: str) -> bytes:
    """Fallback image resolver used when no attachment store is wired.

    The renderer attempts ``Gdk.Texture.new_from_bytes`` on the result.
    Empty bytes raise ``GLib.Error``, which the renderer catches and
    converts into its small placeholder paintable — a constant grey
    rectangle that signals the missing image without aborting the
    document.

    Production wires a real :class:`AttachmentStoreProtocol` so this
    function is bypassed; it remains as a graceful degradation for
    tests and for the (defensive) case where the application is
    constructed without attachment plumbing.
    """
    del _filename  # unused — the placeholder is filename-independent
    return b""


@dataclass(frozen=True)
class _WidgetXMetrics:
    """Horizontal layout metrics for the article text view, captured once
    per snapshot.

    :class:`_ArticleTextView` reads three GTK getters
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


class _ArticleTextView(Gtk.TextView):
    """A :class:`Gtk.TextView` subclass that paints wider washes for tinted block paragraphs.

    The paragraph tags in :mod:`notes_app.asciidoc.tag_table`
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
    """

    _wash_specs_by_tag: Mapping[Gtk.TextTag, WashSpec]

    def __init__(self) -> None:
        super().__init__()
        # No wash specs installed yet — the painter is a no-op until
        # :meth:`install_wash_specs` is called. Tests that construct
        # the subclass directly get a plain :class:`Gtk.TextView` of
        # behaviour, which matches the inert pre-install state.
        self._wash_specs_by_tag = {}

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

    def do_snapshot(  # pylint: disable=arguments-differ
        self, snapshot: Gtk.Snapshot,
    ) -> None:
        """Paint the per-paragraph washes, then delegate to the parent.

        The wash colour nodes are appended *before* the parent
        snapshot, so the text renders on top. The order matters: GTK
        snapshot nodes are stacked back-to-front in the order they
        are appended.
        """
        for color, rect in self._compute_wash_rects():
            snapshot.append_color(color, rect)
        Gtk.TextView.do_snapshot(self, snapshot)

    def _compute_wash_rects(
        self,
    ) -> list[tuple[Gdk.RGBA, Graphene.Rect]]:
        """Return one ``(colour, rect)`` per wash-bearing logical line.

        Walks the buffer one logical line at a time. For every line
        whose first iter carries a tag in :attr:`_wash_specs_by_tag`,
        records a coloured rect that spans the full vertical extent
        of the logical line (i.e. all of its visual wraps, returned
        by :meth:`Gtk.TextView.get_line_yrange`) and is one M-width
        wider than the text column on each side.

        Mutual exclusion: paragraph-level wash-bearing tags are
        mutually exclusive by parser construction — admonition label,
        admonition body, blockquote body, and code block are distinct
        paragraph types and never overlap on the same iter. The
        method enforces this defensively: if an iter carries more
        than one wash-bearing tag it raises :class:`ValueError`
        rather than silently picking one, so a future code path that
        violates the invariant fails loudly.
        """
        rects: list[tuple[Gdk.RGBA, Graphene.Rect]] = []
        if not self._wash_specs_by_tag:
            return rects
        buffer = self.get_buffer()
        metrics = _WidgetXMetrics(
            width=self.get_width(),
            left_margin=self.get_left_margin(),
            right_margin=self.get_right_margin(),
        )
        for line_no in range(buffer.get_line_count()):
            rect_with_color = self._wash_rect_for_line(
                buffer, line_no, metrics,
            )
            if rect_with_color is not None:
                rects.append(rect_with_color)
        return rects

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
        perform.
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
        rect.init(
            float(box_x), float(line_y_widget), float(box_w), float(line_h),
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


def _rgba_from_tint(tint: tuple[float, float, float, float]) -> Gdk.RGBA:
    """Build a :class:`Gdk.RGBA` from a 4-tuple of floats in ``[0, 1]``.

    Used by :class:`_ArticleTextView` to translate a
    :class:`notes_app.asciidoc.tag_table.WashSpec`'s tint into the
    colour type :meth:`Gtk.Snapshot.append_color` expects. A free
    function (rather than a static method on the subclass) so the
    test suite can call it directly when asserting on wash colours.
    """
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = tint
    return rgba


class ArticleContainer(Gtk.Widget):
    """A :class:`Gtk.Widget` subclass with one child that pins width to a column.

    The container holds a single child (in production, the rendered-view
    :class:`Gtk.TextView`) and enforces the column-width rule from §2 of
    the plan via two vfunc overrides:

    * :meth:`do_measure` reports :meth:`outer_column_width` as both the
      minimum and natural width on the horizontal axis — the minimum is
      what makes the parent ``Gtk.ScrolledWindow`` show a horizontal
      scrollbar when its allocation is below the target. On the
      vertical axis, the call is forwarded to the child's
      :meth:`Gtk.Widget.measure` at the outer column width so the
      child's wrapping (e.g. ``Gtk.TextView`` in ``WORD_CHAR`` mode)
      computes its height against the width it will actually receive.
    * :meth:`do_size_allocate` builds a translate-X
      :class:`Gsk.Transform` that offsets the child by half the
      available slack when the allocation is wider than the column,
      and by zero otherwise. The child is always allocated exactly
      :meth:`outer_column_width` pixels wide — the column never
      shrinks, even when the parent allocation is narrower (the
      overflow is what the parent ``ScrolledWindow`` scrolls).

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

    _char_width_measurer: CharWidthMeasurer
    _line_height_measurer: LineHeightMeasurer
    _cached_char_width_px: int | None
    _cached_line_height_px: int | None
    _child: Gtk.Widget | None

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
        # ``hexpand`` is what tells the parent ``Gtk.ScrolledWindow``'s
        # ``Gtk.Viewport`` to allocate us *more* than our natural width
        # when there is room — without it, the viewport would clamp us
        # to the natural width and the wide-window centring path would
        # never fire because ``do_size_allocate`` would always receive
        # exactly :meth:`outer_column_width`.
        self.set_hexpand(True)

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
        for_size: int,
    ) -> tuple[int, int, int, int]:
        """Report the outer column width as both min and natural.

        On the horizontal axis, both minimum and natural width equal
        :meth:`outer_column_width`. The minimum is what makes a
        narrow allocation trigger the parent
        ``Gtk.ScrolledWindow``'s horizontal scrollbar; the natural is
        the hint a parent uses when it has flexibility about how much
        to give us. Reporting the same value for both means the column
        never spontaneously grows past its target on its own — only
        ``hexpand`` (set in ``__init__``) lets a parent give us more,
        and the extra is absorbed by the size-allocate centring logic.

        On the vertical axis, defer to the single child's
        :meth:`Gtk.Widget.measure`, asking it for its height at the
        *outer* column width — the width the child will actually be
        allocated. ``for_size`` (the parent's cross-axis hint) is
        clamped to at most :meth:`outer_column_width` so the child
        wraps at the column rather than at a wider viewport. With no
        child, vertical measure returns zeroes. Baselines are not
        meaningful for this widget.
        """
        if orientation == Gtk.Orientation.HORIZONTAL:
            target = self.outer_column_width()
            return (target, target, -1, -1)
        if self._child is None:
            return (0, 0, -1, -1)
        outer = self.outer_column_width()
        child_for_size = min(for_size, outer) if for_size > 0 else outer
        return self._child.measure(orientation, child_for_size)

    def do_size_allocate(  # pylint: disable=arguments-differ
        self,
        width: int,
        height: int,
        baseline: int,
    ) -> None:
        """Centre the article column horizontally inside ``width``.

        When ``width`` is strictly greater than
        :meth:`outer_column_width`, the slack ``width - target`` is
        split equally on either side of the child and applied as a
        translate-X :class:`Gsk.Transform` on the child's allocate
        call. Otherwise the child is allocated at offset 0 (transform
        ``None``) — the parent ``ScrolledWindow`` scrolls horizontally
        to expose the column at its target size. The child is always
        allocated exactly :meth:`outer_column_width` pixels wide
        regardless of ``width``; that is the column-pinning invariant.
        The transform path is allocate-cycle-free, unlike the prior
        ``set_margin_*`` approach.
        """
        if self._child is None:
            return
        outer = self.outer_column_width()
        if width > outer:
            x_offset = (width - outer) // 2
        else:
            x_offset = 0
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


class NoteView(Gtk.Box):
    # pylint: disable=too-many-instance-attributes
    """The rendered-note pane.

    The pane is a vertical box: today only the scrolled article; later
    builds will prepend the breadcrumb and metadata strip in the same
    container. Construction wires the renderer, the buffer, and the
    :class:`AppState` subscription that triggers a refresh whenever the
    selected note changes.

    Read access to the underlying note goes through the protocol
    parameter — concrete repositories are not imported. Image bytes
    flow through an internally-built :data:`ImageBytesResolver` that
    closes over an injected :class:`AttachmentStoreProtocol`; if the
    store is ``None`` (test default) the fallback
    :func:`_placeholder_image_bytes` is wired instead.

    The instance-attribute count exceeds pylint's default ceiling of
    seven because step 11 introduced two fields
    (:attr:`_attachments`, :attr:`_current_note_id`) on top of the
    five already required to wire the renderer + selection plumbing,
    and step 16 adds two more (:attr:`_error_banner_revealer`,
    :attr:`_error_banner_label`) for the inline parse-error banner.
    Splitting these into a helper class would obscure the obvious
    "the view holds the things it needs to render" relationship.
    """

    # Only fields used outside ``__init__`` are stored on ``self``.
    # The transient widgets built during construction
    # (``Gtk.TextTagTable``, :class:`ArticleContainer`,
    # ``Gtk.ScrolledWindow``) are kept alive by their GTK parent-child
    # references — adding them as ``self.`` attributes would duplicate
    # those references for no behavioural benefit. The error banner's
    # revealer and label *are* stored because :meth:`refresh` toggles
    # them on every selection change. The :class:`ArticleContainer`'s
    # *outer column width* is stored as a derived ``int``
    # (``_outer_column_width_px``) — not the widget — because
    # :class:`MainWindow` needs the value to size the initial window
    # (:meth:`preferred_column_width_px`); caching the int keeps it tied
    # to the same M-width measurement without retaining the widget.
    _note_repository: NoteRepositoryProtocol
    _attachments: AttachmentStoreProtocol | None
    _app_state: AppState
    _buffer: Gtk.TextBuffer
    _text_view: _ArticleTextView
    _renderer: TextBufferRenderer
    _current_note_id: str | None
    _error_banner_revealer: Gtk.Revealer
    _error_banner_label: Gtk.Label
    _outer_column_width_px: int

    def __init__(
        self,
        *,
        note_repository: NoteRepositoryProtocol,
        app_state: AppState,
        attachments: AttachmentStoreProtocol | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_repository = note_repository
        self._attachments = attachments
        self._app_state = app_state
        # ``_current_note_id`` is the note whose source is presently
        # rendered in the buffer. The image-bytes resolver reads this
        # to scope its filename lookup to the right note's
        # attachments. ``refresh`` updates it on every render so the
        # closure always sees the current note context.
        self._current_note_id = None

        # Build the parse-error banner and prepend it to the vertical
        # stack. The revealer hides itself by default with a 0 ms
        # transition so the construction-time refresh does not flash a
        # banner before its hide-on-success arm fires. When a parse
        # error fires, ``refresh`` calls ``set_reveal_child(True)``;
        # on success it calls ``set_reveal_child(False)``.
        self._error_banner_revealer = Gtk.Revealer()
        self._error_banner_revealer.set_transition_type(
            Gtk.RevealerTransitionType.NONE,
        )
        self._error_banner_revealer.set_reveal_child(False)
        banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        banner_box.add_css_class(_BANNER_CSS_CLASS)
        self._error_banner_label = Gtk.Label()
        self._error_banner_label.set_wrap(True)
        self._error_banner_label.set_xalign(0.0)
        self._error_banner_label.set_hexpand(True)
        banner_box.append(self._error_banner_label)
        self._error_banner_revealer.set_child(banner_box)
        self.append(self._error_banner_revealer)

        # Build the text-rendering widget *before* the tag table so we
        # have a Pango context to measure the M-width against. The
        # subclass :class:`_ArticleTextView` adds the snapshot-time
        # wash painter on top of the standard :class:`Gtk.TextView`
        # behaviour; everything below treats it as a regular text view
        # because that is the type-correct view.
        self._text_view = _ArticleTextView()
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        # The text view itself fills the inner column. Vertical expand
        # is what gives the scroller something to scroll.
        self._text_view.set_hexpand(True)
        self._text_view.set_vexpand(True)

        # Measure the body font's M-width and line-height now — both
        # the tag table (for paragraph margins) and the article
        # container (for the column width) need it.
        char_width_measurer, line_height_measurer = _build_font_measurers(
            self._text_view,
        )

        # Build the tag table parameterised by the measured M-width so
        # the paragraph margins encode "inset + M-width" — see
        # :class:`notes_app.asciidoc.tag_table.WashSpec` for the split
        # between text position (tag margins) and wash position
        # (paint-time, in :class:`_ArticleTextView`).
        tag_table = build_tag_table(char_width_px=char_width_measurer())
        self._buffer = Gtk.TextBuffer.new(tag_table)
        self._text_view.set_buffer(self._buffer)

        # Translate the wash-spec map (keyed by :class:`TagName`) to a
        # map keyed by :class:`Gtk.TextTag` objects so the snapshot
        # path can membership-test in O(1) without re-resolving tag
        # names on every paint. ``lookup`` returns ``None`` only for
        # an unknown tag name; every key in :func:`build_wash_specs`
        # is registered in the table by :func:`build_tag_table`, so
        # the lookups always succeed — but a defensive filter keeps
        # the type narrow.
        wash_specs_by_tag: dict[Gtk.TextTag, WashSpec] = {}
        for tag_name, spec in build_wash_specs().items():
            tag = tag_table.lookup(tag_name.value)
            if tag is not None:
                wash_specs_by_tag[tag] = spec
        self._text_view.install_wash_specs(wash_specs_by_tag)

        # The article container: a fixed-width column wrapping the
        # text view. Production wires the two measurers (M-width and
        # line-height) — see :func:`_build_font_measurers` for the
        # single seam tests monkey-patch. We pass the *same*
        # measurer callables that already ran above; their results
        # are cached on first call inside the container, so this is
        # not a double measurement.
        article_container = ArticleContainer(
            char_width_measurer=char_width_measurer,
            line_height_measurer=line_height_measurer,
        )
        article_container.set_child(self._text_view)

        # Cache the outer column width as a plain ``int`` (the widget
        # itself is not retained — see the stored-fields note above).
        # :class:`MainWindow` reads this via
        # :meth:`preferred_column_width_px` to size the initial window
        # so the column fits without a horizontal scroll. Caching the
        # derived value keeps it tied to the *same* M-width measurement
        # the column and margins already use, so the window cannot drift
        # from the column it renders.
        self._outer_column_width_px = article_container.outer_column_width()

        # The four breathing-space margins on the rendered-view
        # ``Gtk.TextView``. All four are font-relative — top / bottom
        # are multiples of the measured line height, left / right are
        # multiples of the measured "M" width. Reading the cached
        # values back from ``article_container`` (rather than calling
        # the measurer callables directly) ensures the column width
        # and the inner padding are derived from the same M-width
        # measurement — they cannot drift.
        self._text_view.set_left_margin(
            ARTICLE_INNER_HPADDING_CHARS * article_container.char_width_px(),
        )
        self._text_view.set_right_margin(
            ARTICLE_INNER_HPADDING_CHARS * article_container.char_width_px(),
        )
        self._text_view.set_top_margin(
            ARTICLE_TOP_MARGIN_LINES * article_container.line_height_px(),
        )
        self._text_view.set_bottom_margin(
            ARTICLE_BOTTOM_MARGIN_LINES * article_container.line_height_px(),
        )

        # The scroller: AUTOMATIC on both axes. Vertical scrolling is
        # the prose-reading direction; horizontal kicks in only when
        # the window is too narrow to fit the column at its target
        # width.
        scrolled_window = Gtk.ScrolledWindow.new()
        scrolled_window.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        scrolled_window.set_child(article_container)
        scrolled_window.set_hexpand(True)
        scrolled_window.set_vexpand(True)
        self.append(scrolled_window)

        # The renderer's image-bytes resolver is built here so it
        # closes over ``self`` and reads the live ``_current_note_id``
        # / ``_attachments`` rather than a snapshot. The
        # ``column_width_px`` resolver is the container's bound method
        # for the same reason — and is fed the *text* width (not the
        # outer width including padding) because the renderer lays
        # tables and images against the actual reading column, not the
        # widget's outer footprint.
        self._renderer = TextBufferRenderer(
            image_bytes_for=self._resolve_image_bytes,
            column_width_px=article_container.text_column_width,
            tag_table=tag_table,
        )

        # Subscribe to the selected-note signal. The handler is a bound
        # method so disconnecting later is simple if the widget is ever
        # torn down — but step 8 has a single window for the lifetime
        # of the application, so explicit disconnection isn't wired up.
        self._app_state.connect(
            "selected-note-changed",
            self._on_selected_note_changed,
        )

        # Initial render: pick up whatever ``selected_note_id`` is set
        # to before the view was constructed.
        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def preferred_column_width_px(self) -> int:
        """Return the rendered article column's outer width in pixels.

        This is ``(TARGET_CHARS_PER_LINE + 2 ×
        ARTICLE_INNER_HPADDING_CHARS) × measured-M-width`` — the width
        the fixed-width column wants, including its inner horizontal
        padding. It is the value :class:`MainWindow` adds to the two
        left-pane widths to pick an initial window width that shows the
        column without a horizontal scroll. Because it is derived from
        the same M-width measurement as the column and its margins, the
        window and the column it renders cannot disagree.
        """
        return self._outer_column_width_px

    def refresh(self) -> None:
        """Render the currently selected note into the buffer.

        Called automatically on construction and on every
        ``selected-note-changed`` signal. Safe to invoke directly when
        outside code (e.g. a future editor that posts a new source)
        wants the rendered view to catch up immediately.

        Behaviour:

        * No selection → buffer cleared, banner hidden,
          ``_current_note_id`` cleared.
        * Selection points to a note that no longer exists → buffer
          cleared, banner hidden, ``_current_note_id`` cleared. The
          note-list widget elsewhere will pick a new selection on its
          next refresh; this view does not second-guess.
        * Parse error in the source → buffer cleared, banner revealed
          with a kind-specific message. ``_current_note_id`` IS still
          updated to the new selection so any image lookup or
          subsequent re-render targets the right note.
        * Successful render → buffer populated with the rendered
          article, banner hidden.

        Banner state and buffer state are kept in lockstep — there is
        no combination "stale buffer + visible banner" or "empty
        buffer + hidden banner" produced by this method.
        """
        note_id = self._app_state.selected_note_id
        if note_id is None:
            self._current_note_id = None
            self._buffer.set_text("")
            self._hide_error_banner()
            return
        try:
            note = self._note_repository.get(note_id)
        except KeyError:
            self._current_note_id = None
            self._buffer.set_text("")
            self._hide_error_banner()
            return
        # Update the resolver's view of "current note" BEFORE invoking
        # the renderer, so any image macro encountered during the
        # render walk sees the right scope. Updating after would race
        # with the renderer's own image-resolver calls.
        self._current_note_id = note.id
        try:
            self._renderer.render_into(
                note.source,
                self._buffer,
                note_id=note.id,
                attach_widget=self._attach_child_widget,
            )
        except ParseError as exc:
            # Clear the buffer so a stale render from the previously
            # selected note does not sit under the new note's title;
            # show the banner with a user-facing message keyed by the
            # error's kind. This is the failure mode the plan calls
            # out: without these two lines the user sees the previous
            # note's content for a note that doesn't parse.
            self._buffer.set_text("")
            self._error_banner_label.set_text(_message_for(exc.kind, exc.line))
            self._error_banner_revealer.set_reveal_child(True)
            return
        # Render succeeded — make sure no stale banner remains visible.
        self._hide_error_banner()

    def _hide_error_banner(self) -> None:
        """Hide the parse-error banner.

        Centralised because both the no-selection / not-found arms
        and the success path of :meth:`refresh` use it; keeping the
        sequence (``set_reveal_child(False)`` + clear label text) in
        one place ensures the banner cannot end up "hidden but with
        last error's text", which would be a confusing state should
        the revealer's transition ever be made non-instant.
        """
        self._error_banner_revealer.set_reveal_child(False)
        self._error_banner_label.set_text("")

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_selected_note_changed(self, _app_state: AppState) -> None:
        """Refresh on a selection change. Payload-free signal."""
        self.refresh()

    # ------------------------------------------------------------------
    # Renderer wiring
    # ------------------------------------------------------------------

    def _attach_child_widget(
        self,
        anchor: Gtk.TextChildAnchor,
        widget: Gtk.Widget,
    ) -> None:
        """Adapter for the renderer's ``WidgetAttacher`` contract.

        :data:`WidgetAttacher` (defined in
        :mod:`notes_app.asciidoc.textbuffer_renderer`) is
        ``Callable[[Gtk.TextChildAnchor, Gtk.Widget], None]`` —
        anchor first, widget second — because anchor *creation* is
        the renderer's first step. :meth:`Gtk.TextView.add_child_at_anchor`
        in GTK 4 takes the *child first, anchor second*. This adapter
        bridges the two without leaking the GTK 4 parameter order
        into the renderer's pure-AST interface.
        """
        self._text_view.add_child_at_anchor(widget, anchor)

    def _resolve_image_bytes(self, filename: str) -> bytes:
        """The :data:`ImageBytesResolver` plugged into the renderer.

        Reads :attr:`_current_note_id` (set by :meth:`refresh`) and
        looks up the matching attachment in
        :attr:`_attachments`. Returns the bytes if found; an empty
        ``bytes`` if not — which causes the renderer to fall back to
        its placeholder paintable (a small grey rectangle). This
        matches the placeholder-bytes contract from build step 8;
        the renderer's image path now inserts the placeholder via
        ``insert_paintable`` rather than building an anchored widget.

        Lookup is a linear scan over
        :meth:`AttachmentStoreProtocol.list_for_note`. For v1's
        "handful of images per note" this is cheaper than a per-note
        dict cache that would have to be invalidated on every
        attachment add / remove. The list call itself is metadata-
        only: the BLOB column is not selected.
        """
        if self._attachments is None:
            return _placeholder_image_bytes(filename)
        if self._current_note_id is None:
            # Defensive: a malformed renderer call before refresh has
            # set the current note would land here. The placeholder
            # contract keeps the document viewable.
            return _placeholder_image_bytes(filename)
        for attachment in self._attachments.list_for_note(self._current_note_id):
            if attachment.filename == filename:
                return self._attachments.get_bytes(attachment.id)
        # No match — the image macro references a filename that has
        # no attachment row. The renderer's decode-failure branch
        # produces the placeholder paintable on empty bytes, which is
        # the right user-visible signal for "image not found".
        return _placeholder_image_bytes(filename)

    @property
    def current_note_id(self) -> str | None:
        """The id of the note presently rendered in the buffer.

        ``None`` when no note is selected or the selection points at
        a deleted note. Public read-only because the image-bytes
        resolver tests need to verify the closure follows the
        selection.
        """
        return self._current_note_id

    @property
    def image_bytes_resolver(self) -> ImageBytesResolver:
        """The bound resolver method exposed for tests.

        Tests that want to verify the resolver's behaviour without
        rendering a document call this method directly. The returned
        callable is the same object the renderer holds, so any state
        mutation (e.g. a selection change) is visible through it.
        """
        return self._resolve_image_bytes

    @property
    def error_banner_visible(self) -> bool:
        """``True`` iff the parse-error banner is currently revealed.

        Public read-only so tests can assert the success / failure
        bookkeeping without reaching into the revealer's children.
        """
        return self._error_banner_revealer.get_reveal_child()

    @property
    def error_banner_text(self) -> str:
        """The current text of the parse-error banner.

        Empty when no parse error is being shown — see
        :meth:`_hide_error_banner` for the convention.
        """
        return self._error_banner_label.get_text()


# ---------------------------------------------------------------------------
# Production char-width measurement
# ---------------------------------------------------------------------------


_MEASUREMENT_GLYPH: str = "M"
"""The reference glyph the typography literature uses for column width.

A capital M is wide, fixed-width-friendly, and present in every Latin
font, so the resulting measurement is a stable upper-bound on
character width. Matches the "66 × Pango.Layout.get_pixel_extents('M')"
formula stated in §2 of the plan.
"""


def _make_pango_char_width_measurer(widget: Gtk.Widget) -> CharWidthMeasurer:
    """Build a measurer that reads the live Pango font of ``widget``.

    The returned closure constructs a :class:`Pango.Layout` for the
    widget, lays out a single :data:`_MEASUREMENT_GLYPH`, and returns
    the logical pixel extents' width. The widget does not need to be
    realised — :meth:`Gtk.Widget.create_pango_layout` works against the
    Pango context derived from the widget's CSS / theme, which is set
    up at widget construction time.
    """

    def measure() -> int:
        layout = widget.create_pango_layout(_MEASUREMENT_GLYPH)
        _, log_rect = layout.get_pixel_extents()
        return int(log_rect.width)

    return measure


def _make_pango_line_height_measurer(widget: Gtk.Widget) -> LineHeightMeasurer:
    """Build a measurer that returns the pixel height of one line.

    Sibling of :func:`_make_pango_char_width_measurer`. The closure
    lays out the same reference glyph (:data:`_MEASUREMENT_GLYPH`)
    with the widget's Pango context and returns ``log_rect.height`` —
    the actual rendered line height for the body font, including the
    font's leading. Sharing the reference glyph keeps the two
    measurements coherent: a future change to the glyph is one edit.
    """

    def measure() -> int:
        layout = widget.create_pango_layout(_MEASUREMENT_GLYPH)
        _, log_rect = layout.get_pixel_extents()
        return int(log_rect.height)

    return measure


def _build_font_measurers(
    text_view: Gtk.TextView,
) -> tuple[CharWidthMeasurer, LineHeightMeasurer]:
    """Pair the two production Pango measurers for a ``Gtk.TextView``.

    Returned as a 2-tuple ``(char_width_measurer, line_height_measurer)``
    so :meth:`NoteView.__init__` can unpack and inject both into
    :class:`ArticleContainer`. Lives as its own function so the test
    suite can monkey-patch a single seam to supply stubbed measurers
    without instantiating a real font context.
    """
    return (
        _make_pango_char_width_measurer(text_view),
        _make_pango_line_height_measurer(text_view),
    )
