"""The rendered-note pane: a fixed-width article column inside a scroller.

Principles & invariants
-----------------------
* :class:`NoteView` is the pane in which the user reads a note. It is
  stateless with respect to notes — every render rebuilds the buffer
  from scratch, driven by :class:`AppState`. The body it renders comes
  from the in-memory :class:`controllers.note_list_store.NoteListStore`
  (never a database read); images still resolve through
  :class:`AttachmentStoreProtocol`. It re-renders on
  ``notify::selected-note-id`` and on a store ``items-changed`` that
  touches the displayed note (an edit replaces that note's row).
* The pane's layout is the three-step stack from §2 of the plan:
  ``Gtk.ScrolledWindow`` (horizontal AUTOMATIC, vertical AUTOMATIC) →
  :class:`ArticleContainer` (a ``Gtk.Widget`` that also implements
  ``Gtk.Scrollable``, with a single child that enforces the fixed-width
  text column rule) → read-only ``Gtk.TextView`` populated by
  :class:`TextBufferRenderer`. Parse errors are shown *inside* this
  same surface — the buffer is cleared and an error notice rendered
  into it (see the parse-error bullet below) — so the pane is a single
  scroller with no extra strip above it. Because the
  container is a ``Gtk.Scrollable``, the scrolled window keeps it as its
  **direct** child and interposes **no** ``Gtk.Viewport`` (Option C of
  the plan); the bug that motivated this — no vertical scrollbar on
  first launch for an image-last note — came from a viewport committing
  a stale extent, so removing the viewport removes the bug.
* :class:`ArticleContainer` enforces the text-column rule and treats the
  two scroll axes differently because they have different owners.
  *Vertical* is pass-through: the container forwards the scrolled
  window's ``vadjustment`` / ``vscroll-policy`` to the (already
  scrollable) text view, which becomes the vertical scrollport and owns
  the v-extent — the widget that commits ``vadjustment.upper`` is the
  one that knows the height. *Horizontal* is container-owned: when
  allocated *wider* than the target column the slack becomes an
  equal-on-both-sides translation of the child, centring the column;
  when allocated *narrower* the container configures its own
  ``hadjustment`` (``upper`` = column width, ``page`` = viewport) and
  offsets the child by ``−hadjustment.value`` so the horizontal
  scrollbar pans the column. The column never shrinks, and the font
  never scales with window width (see §2 / decision 7 of the plan).
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
  height, each **plus** the :data:`config.defaults.ARTICLE_END_GAP_LINES`
  desk band (so the same gap is reserved before and after the note); left
  and right are :data:`ARTICLE_INNER_HPADDING_CHARS` multiplied by the
  measured M-width.
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
  :class:`ArticleTextView` that paints tinted block backgrounds
  (admonition, blockquote, code block) at snapshot time. The
  paragraph tags from :mod:`ui.note_render.tag_table` deliberately
  carry only the *text position* (``accumulative-margin = True`` plus
  ``left-margin`` / ``right-margin`` = inset + one M-width); the
  matching tinted *wash* is painted by this subclass via
  :meth:`do_snapshot`. The wash extends one M-width beyond the text
  on each side, producing the visual "padded card" effect that
  ``paragraph-background-rgba`` cannot reproduce on its own — see
  :class:`ui.note_render.tag_table.WashSpec` for the per-tag
  parameters. The tag table is therefore built *after* M-width is
  measured (``char_width_px`` is required), and the wash-spec map
  passed to the subclass is keyed by :class:`Gtk.TextTag` objects
  (not names) so per-snapshot tag-lookup work stays O(1). The same
  subclass also paints the note *sheet*: because the
  text view is the vertical scrollport, its own background would fill
  the whole viewport, so the view's CSS background is made transparent
  (the ``article-text-view`` class) and ``do_snapshot`` paints an
  opaque sheet covering the content plus the breathing part of the top
  and bottom margins. Beyond that (above
  the top gap and below the bottom gap) the view paints nothing, so the
  scroller's own background (the "desk") shows through equally before and
  after the note — using the parent's real background rather than an
  invented colour. Both the top and bottom margins are sized at
  :data:`ARTICLE_TOP_MARGIN_LINES` / :data:`ARTICLE_BOTTOM_MARGIN_LINES` +
  :data:`config.defaults.ARTICLE_END_GAP_LINES`: the sheet claims only
  the breathing lines, leaving an equal desk band at each end so a note
  meets the desk at a visible edge above and below (and a note taller than
  the viewport reveals the bottom edge when scrolled down — see
  :func:`ui.note_render.tag_table.build_sheet_wash`,
  :func:`_sheet_rect_for`, :meth:`ArticleTextView.set_top_gap_px`,
  and :meth:`ArticleTextView.set_end_gap_px`).
* The size-allocate vfunc — *not* the ``size-allocate`` signal, which is
  deprecated in GTK 4 — is the documented place to react to a fresh
  allocation. :meth:`ArticleContainer.do_size_allocate` configures the
  container-owned horizontal ``hadjustment`` and builds a translate-X
  :class:`Gsk.Transform` to position the single child, then calls
  :meth:`Gtk.Widget.allocate` on it with that transform. This avoids the
  re-layout cycle that writing ``margin-start`` / ``margin-end`` on
  ``self`` from inside ``size_allocate`` would trigger; it is the GTK 4
  idiom for "offset the single child by N pixels along X without
  rerunning the parent's layout". A horizontal scroll re-runs this vfunc
  via :meth:`Gtk.Widget.queue_allocate` (wired from the adjustment's
  ``value-changed``); re-``configure``-ing the adjustment to an unchanged
  value emits no further ``value-changed``, so there is no allocation
  loop.
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
  clears the buffer and renders an in-surface *error notice* into it —
  a centred warning glyph, a headline, a user-facing message keyed by
  :class:`ParseErrorKind`, and a faint recovery hint — via
  :meth:`_insert_error_notice`. Selecting a note that doesn't parse
  therefore shows that notice in the reading column, *not* the previous
  note's stale render. The notice lives in the buffer (styled by the
  :data:`ui.note_render.tag_table.TagName.ERROR_NOTICE_*` tags), so
  there is no always-present banner widget reserving space above the
  pane when there is nothing to show: an error consumes the rendering
  surface only while it is being shown. :attr:`_error_message` mirrors
  the message currently on screen (``None`` when none), keeping the
  surface and that flag in lockstep — no "stale buffer + cleared flag"
  or "notice in buffer + ``None`` flag" state is produced here.
* The user-facing message table (:func:`_message_for`) lives in this
  module rather than as a method on :class:`ParseError` because the
  parser is pure and reusable; embedding UI copy in it would couple
  the parser to this UI's tone. The mapping is *exhaustive* over
  :class:`ParseErrorKind` so adding a new error kind forces an
  update here — caught by a unit test that iterates the enum.
* **Metadata line.** Directly under the rendered title the view
  inserts a dim-grey metadata line — ``Created <date>  ·  Modified
  <date>  ·  #tag …`` — as **plain text in the buffer**, carrying the
  :data:`ui.note_render.tag_table.TagName.METADATA` character tag. It
  is not a widget: there is no anchored child and no separate
  visibility toggle. The text is inserted by
  :meth:`NoteView._insert_metadata_after_title`, wired as the
  renderer's :data:`PostTitleHook`; the dates come from the
  :class:`Note` already fetched in :meth:`refresh` (stored on
  :attr:`_current_note` before the render so the hook can read it).
  A note with no tags shows only the two dates. A thin horizontal rule
  separating the metadata from the body is painted by
  :class:`ArticleTextView` as the ``hairline`` wash for the metadata
  tag (see :func:`ui.note_render.tag_table.build_wash_specs`), so the
  whole rendered-view styling stays in the tag table / wash painter
  and introduces no child widget. Because the right pane is a
  :class:`Gtk.Stack` that hides the whole :class:`NoteView` in SOURCE
  mode (where the raw ``:tags:`` line is visible in the editor), the
  buffer-resident metadata needs no view-mode toggle.
"""

# pylint: disable=too-many-lines
# This step pushed the file past pylint's default 1000-line ceiling
# because the snapshot-time wash painter (:class:`ArticleTextView`)
# adds the new subclass, its rect-computation seam, and the wash-spec
# wiring inside :meth:`NoteView.__init__`. The new class is tightly
# coupled to the existing :class:`ArticleContainer` / :class:`NoteView`
# pair — they share the M-width measurement and the tag-table key
# translation — so extracting it to its own module would scatter the
# wiring without improving readability.

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from gi.repository import Gdk, GObject, Graphene, Gsk, Gtk, Pango

from config.defaults import (
    ARTICLE_BOTTOM_MARGIN_LINES,
    ARTICLE_END_GAP_LINES,
    ARTICLE_INNER_HPADDING_CHARS,
    ARTICLE_TOP_MARGIN_LINES,
    TARGET_CHARS_PER_LINE,
)
from enums import LinkScheme, ParseErrorKind, WashShape
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui._dates import format_date_long
from giruntime.ui.note_render.tag_table import (
    SheetWash,
    TagName,
    WashSpec,
    build_sheet_wash,
    build_tag_table,
    build_wash_specs,
)
from giruntime.ui.note_render.textbuffer_renderer import (
    CellWidthMeasurer,
    TextBufferRenderer,
)
from models.note import Note
from models.parse_error import ParseError
from storage.protocols import (
    AttachmentStoreProtocol,
    ImageBytesResolver,
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


_ERROR_NOTICE_ICON_GLYPH: str = "\u26a0\ufe0e"
"""The warning glyph shown at the top of the in-surface parse-error
notice. ``U+26A0`` (warning sign) followed by ``U+FE0E`` (the text
variation selector) so it renders as a monochrome text glyph rather than
a colour emoji — the latter would ignore the amber foreground the
:data:`TagName.ERROR_NOTICE_ICON` tag sets. Scale and colour live in the
tag table; only the character itself lives here, beside the other notice
copy.
"""


_ERROR_NOTICE_HEADLINE: str = "This note can\u2019t be displayed"
"""Headline line of the parse-error notice. User-facing copy, so it
lives in this module next to :func:`_message_for` rather than in the
tag table (which owns only the *look*).
"""


_ERROR_NOTICE_HINT: str = "Switch to Source to fix it"
"""Faint recovery hint under the parse-error message — points the user
at the editor, where the offending line can be corrected.
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


_METADATA_SEPARATOR: str = "  \u00b7  "
"""Separator between the Created date, Modified date, and tags on the
metadata line. A middle dot (``·``) padded with two spaces on each
side, matching the design target
``Created … · Modified … · #tag …``."""

_METADATA_TAG_PREFIX: str = "#"
"""Visible prefix on each tag in the metadata line, matching the
sidebar's tag rows and the note-list row chips."""

_METADATA_TAG_JOINER: str = "  "
"""Spacing between adjacent ``#tag`` entries within the metadata
line's tag run."""

_METADATA_CREATED_LABEL: str = "Created "
"""Leader before the created-at date on the metadata line."""

_METADATA_MODIFIED_LABEL: str = "Modified "
"""Leader before the modified-at date on the metadata line."""

_HAIRLINE_THICKNESS_PX: int = 1
"""Height, in pixels, of the hairline rule the wash painter draws at
the bottom of a :class:`WashSpec` whose ``shape`` is
:data:`WashShape.HAIRLINE` (the metadata line's divider). Painted as a
1-px band rather than a full-height fill — see
:meth:`ArticleTextView._wash_rect_for_line`."""


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
        case ParseErrorKind.BAD_TAG_VALUE:
            return (
                f"Line {line}: the `:tags:` line has an invalid tag value. "
                "Tags use lowercase letters, digits, and hyphens, and must "
                "start with a letter or digit."
            )
        case ParseErrorKind.DUPLICATE_TAG_ATTRIBUTE:
            return (
                f"Line {line}: this note has more than one `:tags:` line — "
                "combine them into a single comma-separated list."
            )
        case ParseErrorKind.LIST_STARTS_BELOW_TOP_LEVEL:
            return (
                f"Line {line}: a list must start at the top level — begin "
                "with a single `*` or `.` before nesting deeper."
            )
        case ParseErrorKind.LIST_NESTING_SKIPS_LEVEL:
            return (
                f"Line {line}: this list item nests too fast — add only one "
                "more `*` or `.` than the item above it."
            )
        case ParseErrorKind.LIST_NESTING_TOO_DEEP:
            return (
                f"Line {line}: lists can nest at most three levels deep."
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


def _format_metadata_line(
    created_at: datetime,
    modified_at: datetime,
    tags: tuple[str, ...],
) -> str:
    """Build the metadata line under the title.

    Returns ``Created <date>  ·  Modified <date>`` followed, when
    ``tags`` is non-empty, by a third ``·``-separated segment of
    ``#tag`` entries (e.g.
    ``Created May 26, 2026  ·  Modified May 30, 2026  ·  #nothing  #test``).
    Pure and display-free so the ordering / tagless-note behaviour is
    unit-testable without building a widget. Dates are formatted via
    :func:`ui._dates.format_date_long` (locale-independent, with the
    year) so the rendered string is stable across environments.
    """
    segments = [
        f"{_METADATA_CREATED_LABEL}{format_date_long(created_at)}",
        f"{_METADATA_MODIFIED_LABEL}{format_date_long(modified_at)}",
    ]
    if tags:
        segments.append(
            _METADATA_TAG_JOINER.join(
                f"{_METADATA_TAG_PREFIX}{tag}" for tag in tags
            )
        )
    return _METADATA_SEPARATOR.join(segments)


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
      (``upper = outer column width``, ``page_size = allocated width``,
      value clamped to ``column − viewport``), allocates the child at the
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
        :attr:`hadjustment` here: ``upper`` is the column width, the page
        is the viewport ``width``, and the value is clamped to
        ``column − viewport`` so a stale scroll position from a wider
        layout cannot leave the column pinned off-screen. ``HIDDEN``
        overflow (set in ``__init__``) clips the column to the viewport.
        The vertical axis is untouched here — the child owns it as the
        forwarded vertical scrollport.
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
                float(outer),
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


@dataclass(frozen=True)
class ArticleSurface:
    """The shared fixed-width article reading surface.

    Bundles everything that makes a rendered note *look* like a note: the
    painted :class:`ArticleTextView` (which draws the paper sheet and the
    block-tint washes), its buffer and tag table, and the fixed-width
    :class:`ArticleContainer` that wraps the view (child already set,
    the four font-relative margins and the desk gaps already applied).

    Both :class:`NoteView` and :class:`giruntime.ui.help_window.HelpWindow`
    build their reading surface from :func:`build_article_surface`, so a
    note and the help reference share one column geometry: same centred
    fixed-width column on a desk, same paper sheet, and — because the
    block-tint washes are painted relative to that column — the same
    correctly-placed admonition / blockquote / code tints. The renderer
    is *not* built here (its image resolver differs per caller); the
    surface exposes the :attr:`tag_table` and
    :attr:`container` the caller passes into its own
    :class:`TextBufferRenderer`.

    The container is held so callers can read
    :meth:`ArticleContainer.text_column_width` for the renderer and parent
    the container into a :class:`Gtk.ScrolledWindow`;
    :attr:`outer_column_width_px` is the cached outer width (for sizing a
    host window) so a caller that only needs the number need not retain
    the widget.
    """

    text_view: ArticleTextView
    buffer: Gtk.TextBuffer
    tag_table: Gtk.TextTagTable
    container: ArticleContainer
    outer_column_width_px: int


def _apply_article_margins(
    container: ArticleContainer, text_view: ArticleTextView,
) -> None:
    """Set the four font-relative margins (plus desk gaps) on ``text_view``.

    All four are font-relative: top / bottom are multiples of the
    measured line height, left / right of the measured "M" width.
    Reading the cached values back from ``container`` (rather than calling
    the measurer callables again) ties the column width and the inner
    padding to the *same* M-width measurement so they cannot drift.

    The top and bottom margins are each the breathing space *plus* the
    same desk gap: the sheet painted by :class:`ArticleTextView` covers
    only the breathing lines, so the extra ``end_gap_px`` at each end is
    room the sheet does not claim, showing the desk with an equal gap
    before and after the note (at the bottom this doubles as scrollable
    room that reveals a long note's end). The gap is set on the view
    alongside both margins so the three cannot drift — see
    :data:`config.defaults.ARTICLE_END_GAP_LINES`.
    """
    char_w = container.char_width_px()
    line_h = container.line_height_px()
    end_gap_px = round(ARTICLE_END_GAP_LINES * line_h)
    text_view.set_left_margin(ARTICLE_INNER_HPADDING_CHARS * char_w)
    text_view.set_right_margin(ARTICLE_INNER_HPADDING_CHARS * char_w)
    text_view.set_top_margin(ARTICLE_TOP_MARGIN_LINES * line_h + end_gap_px)
    text_view.set_bottom_margin(
        ARTICLE_BOTTOM_MARGIN_LINES * line_h + end_gap_px,
    )
    text_view.set_top_gap_px(end_gap_px)
    text_view.set_end_gap_px(end_gap_px)


def build_article_surface() -> ArticleSurface:
    """Build the shared fixed-width article reading surface.

    The single constructor for the "rendered note" surface, used by both
    the note view and the help window so they render identically. Steps,
    in the order the dependencies require:

    1. a read-only, word-wrapping :class:`ArticleTextView` (the painter of
       the sheet + washes);
    2. the body-font measurers (M-width + line height) off that view's
       Pango context — :func:`_build_font_measurers` is the single seam
       tests stub;
    3. the shared tag table, parameterised by the measured M-width, and a
       buffer on it;
    4. the block-tint wash map, installed via the one shared seam
       :meth:`ArticleTextView.install_wash_specs_from_table`;
    5. the fixed-width :class:`ArticleContainer` wrapping the view, with
       the four font-relative margins + desk gaps applied.

    Returns the bundle; the caller owns the renderer (its image resolver
    is caller-specific) and parents :attr:`ArticleSurface.container` into
    its own scroller.
    """
    text_view = ArticleTextView()
    text_view.set_editable(False)
    text_view.set_cursor_visible(False)
    text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    text_view.set_hexpand(True)
    text_view.set_vexpand(True)

    char_width_measurer, line_height_measurer = _build_font_measurers(
        text_view,
    )

    tag_table = build_tag_table(char_width_px=char_width_measurer())
    buffer = Gtk.TextBuffer.new(tag_table)
    text_view.set_buffer(buffer)
    text_view.install_wash_specs_from_table(tag_table)

    container = ArticleContainer(
        char_width_measurer=char_width_measurer,
        line_height_measurer=line_height_measurer,
    )
    container.set_child(text_view)
    _apply_article_margins(container, text_view)

    return ArticleSurface(
        text_view=text_view,
        buffer=buffer,
        tag_table=tag_table,
        container=container,
        outer_column_width_px=container.outer_column_width(),
    )


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
    and the parse-error notice adds :attr:`_error_message` (the message
    currently shown in the surface, or ``None``).
    Splitting these into a helper class would obscure the obvious
    "the view holds the things it needs to render" relationship.
    """

    # Only fields used outside ``__init__`` are stored on ``self``.
    # The transient widgets built during construction
    # (``Gtk.TextTagTable``, :class:`ArticleContainer`,
    # ``Gtk.ScrolledWindow``) are kept alive by their GTK parent-child
    # references — adding them as ``self.`` attributes would duplicate
    # those references for no behavioural benefit. The parse-error
    # notice needs no stored widget: it is buffer text, so only the
    # :attr:`_error_message` flag (the message currently on screen, or
    # ``None``) is kept, toggled by :meth:`refresh`. The
    # :class:`ArticleContainer`'s
    # *outer column width* is stored as a derived ``int``
    # (``_outer_column_width_px``) — not the widget — because
    # :class:`MainWindow` needs the value to size the initial window
    # (:meth:`preferred_column_width_px`); caching the int keeps it tied
    # to the same M-width measurement without retaining the widget.
    _note_store: NoteListStore
    _attachments: AttachmentStoreProtocol | None
    _app_state: AppState
    _buffer: Gtk.TextBuffer
    _text_view: ArticleTextView
    _renderer: TextBufferRenderer
    _current_note_id: str | None
    _current_note: Note | None
    _error_message: str | None
    _outer_column_width_px: int

    def __init__(
        self,
        *,
        note_store: NoteListStore,
        app_state: AppState,
        attachments: AttachmentStoreProtocol | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_store = note_store
        self._attachments = attachments
        self._app_state = app_state
        # ``_current_note_id`` is the note whose source is presently
        # rendered in the buffer. The image-bytes resolver reads this
        # to scope its filename lookup to the right note's
        # attachments. ``refresh`` updates it on every render so the
        # closure always sees the current note context.
        self._current_note_id = None
        # The :class:`Note` whose source is presently rendered. Stored
        # so the post-title metadata hook can read its timestamps and
        # tags during a render without a second repository round-trip.
        self._current_note = None
        # The parse-error message currently shown in the surface (or
        # ``None`` when the buffer holds a real render / is empty). It
        # is the in-buffer notice's only piece of state — see
        # :meth:`refresh` and :meth:`_insert_error_notice`.
        self._error_message = None

        # The shared fixed-width article surface: the painted text view
        # (sheet + washes), its buffer + tag table, and the
        # :class:`ArticleContainer` that gives the column its width, desk
        # margins, and centring. The help window builds the *same* surface
        # so a note and the help reference render identically — see
        # :func:`build_article_surface`.
        surface = build_article_surface()
        self._text_view = surface.text_view
        self._buffer = surface.buffer
        # Cache the outer column width as a plain ``int`` (the container
        # widget is not retained on ``self`` — it is kept alive by the
        # scroller below). :class:`MainWindow` reads this via
        # :meth:`preferred_column_width_px` to size the initial window so
        # the column fits without a horizontal scroll.
        self._outer_column_width_px = surface.outer_column_width_px

        # ----- Metadata line -----
        # The dim-grey metadata line (Created · Modified · #tags) is
        # inserted as plain tagged text directly under the title by the
        # renderer's post-title hook (see
        # :meth:`_insert_metadata_after_title`). There is no widget to
        # build here — the text lives in the buffer and the hairline
        # rule below it is painted by :class:`ArticleTextView`.

        # The scroller: AUTOMATIC on both axes. Vertical scrolling is
        # the prose-reading direction; horizontal kicks in only when
        # the window is too narrow to fit the column at its target
        # width. The container is the scroller's direct child (it is a
        # ``Gtk.Scrollable``), so no ``Gtk.Viewport`` is interposed.
        scrolled_window = Gtk.ScrolledWindow.new()
        scrolled_window.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        scrolled_window.set_child(surface.container)
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
            column_width_px=surface.container.text_column_width,
            cell_width_px=make_cell_width_measurer(surface.text_view),
            tag_table=surface.tag_table,
        )

        # Subscribe to the selected-note signal. The handler is a bound
        # method so disconnecting later is simple if the widget is ever
        # torn down — but step 8 has a single window for the lifetime
        # of the application, so explicit disconnection isn't wired up.
        self._subscribe_to_state_and_store()

        # Initial render: pick up whatever ``selected_note_id`` is set
        # to before the view was constructed.
        self.refresh()

    def _subscribe_to_state_and_store(self) -> None:
        """Wire the two re-render triggers: selection and store edits."""
        self._app_state.connect(
            "notify::selected-note-id",
            self._on_selected_note_changed,
        )
        # Re-render when the *displayed* note's row is replaced in the
        # store (an edit splices a fresh ``NoteItem`` at its position).
        # Scoped to the current note so unrelated create / edit / delete
        # churn doesn't reset the reader's scroll position.
        self._note_store.connect(
            "items-changed",
            self._on_store_items_changed,
        )

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
        ``notify::selected-note-id``. Safe to invoke directly when
        outside code (e.g. a future editor that posts a new source)
        wants the rendered view to catch up immediately.

        Behaviour:

        * No selection → buffer cleared, error notice cleared,
          ``_current_note_id`` cleared.
        * Selection points to a note that no longer exists → buffer
          cleared, error notice cleared, ``_current_note_id`` cleared.
          The note-list widget elsewhere will pick a new selection on
          its next refresh; this view does not second-guess.
        * Parse error in the source → buffer cleared, then the error
          notice rendered into it with a kind-specific message.
          ``_current_note_id`` IS still updated to the new selection so
          any image lookup or subsequent re-render targets the right
          note.
        * Successful render → buffer populated with the rendered
          article, error notice cleared.

        The surface and :attr:`_error_message` are kept in lockstep —
        there is no combination "stale buffer + cleared flag" or
        "notice in buffer + ``None`` flag" produced by this method.
        """
        note_id = self._app_state.selected_note_id
        if note_id is None:
            self._current_note_id = None
            self._current_note = None
            self._buffer.set_text("")
            self._error_message = None
            return
        try:
            note = self._note_store.get_note(note_id)
        except KeyError:
            self._current_note_id = None
            self._current_note = None
            self._buffer.set_text("")
            self._error_message = None
            return
        # Update the resolver's view of "current note" BEFORE invoking
        # the renderer, so any image macro encountered during the
        # render walk sees the right scope. Updating after would race
        # with the renderer's own image-resolver calls. ``_current_note``
        # is set alongside so the post-title metadata hook can read the
        # note's timestamps and tags during the render.
        self._current_note_id = note.id
        self._current_note = note
        try:
            self._renderer.render_into(
                note.source,
                self._buffer,
                note_id=note.id,
                post_title_hook=self._insert_metadata_after_title,
            )
        except ParseError as exc:
            # The render raised, so the buffer may hold a partial render
            # (or the previously selected note's content if the renderer
            # rebuilds in place). Clear it and render the in-surface
            # error notice instead — without this the user would see the
            # wrong content for a note that doesn't parse. The metadata
            # hook never fired (the renderer raised before reaching it).
            self._insert_error_notice(_message_for(exc.kind, exc.line))
            return
        # Render succeeded — drop any error-notice state.
        self._error_message = None

    def _insert_error_notice(self, message: str) -> None:
        """Render the in-surface parse-error notice into the buffer.

        Clears the buffer first (so no partial or stale render remains),
        then inserts the four centred lines — warning glyph, headline,
        the kind-specific ``message``, and the recovery hint — each
        carrying its :data:`ui.note_render.tag_table.TagName.ERROR_NOTICE_*`
        tag. Records ``message`` on :attr:`_error_message` so
        :attr:`error_notice_text` can report it and :meth:`refresh`
        keeps the surface and that flag in lockstep.

        Each line is its own paragraph (the trailing ``\\n`` closes it),
        which is what lets the per-line tag carry the paragraph-level
        centre justification; the last line is unterminated so the
        notice adds no trailing blank line.
        """
        buffer = self._buffer
        buffer.set_text("")
        buffer.insert_with_tags_by_name(
            buffer.get_end_iter(),
            f"{_ERROR_NOTICE_ICON_GLYPH}\n",
            TagName.ERROR_NOTICE_ICON.value,
        )
        buffer.insert_with_tags_by_name(
            buffer.get_end_iter(),
            f"{_ERROR_NOTICE_HEADLINE}\n",
            TagName.ERROR_NOTICE_TITLE.value,
        )
        buffer.insert_with_tags_by_name(
            buffer.get_end_iter(),
            f"{message}\n",
            TagName.ERROR_NOTICE_DETAIL.value,
        )
        buffer.insert_with_tags_by_name(
            buffer.get_end_iter(),
            _ERROR_NOTICE_HINT,
            TagName.ERROR_NOTICE_HINT.value,
        )
        self._error_message = message

    def _insert_metadata_after_title(self, buffer: Gtk.TextBuffer) -> None:
        """Insert the dim-grey metadata line as the renderer's post-title hook.

        Wired as :data:`PostTitleHook`, so the renderer calls this once
        per successful render with ``buffer`` positioned (at its end
        iter) immediately below the title. Inserts
        ``Created <date>  ·  Modified <date>  ·  #tag …`` as plain text
        carrying the :data:`TagName.METADATA` character tag; the
        renderer then drops a blank line and the body below. The dates
        and tags come from :attr:`_current_note`, set by
        :meth:`refresh` before the render. When there is no current note
        the hook inserts nothing — but :meth:`refresh` only fires the
        hook on a successful render with a note in hand, so this guard
        is purely defensive. A note with no tags yields just the two
        dates.
        """
        note = self._current_note
        if note is None:
            return
        line = _format_metadata_line(
            note.created_at, note.modified_at, note.tags,
        )
        buffer.insert_with_tags_by_name(
            buffer.get_end_iter(), line, TagName.METADATA.value,
        )

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_selected_note_changed(
        self,
        _app_state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """Refresh on a selection change. Notify-only handler."""
        self.refresh()

    def _on_store_items_changed(
        self,
        _model: NoteListStore,
        _position: int,
        _removed: int,
        _added: int,
    ) -> None:
        """Re-render only when the *currently displayed* note changed.

        An edit replaces the note's row (``splice``) and a delete
        removes it; either way the store emits ``items-changed``. The
        view ignores changes that don't touch the displayed note — it
        compares the freshly-read value against the rendered one — so
        editing or creating *other* notes never disturbs the reader.
        """
        if self._current_note_id is None:
            return
        try:
            latest = self._note_store.get_note(self._current_note_id)
        except KeyError:
            # The displayed note was deleted; the controller also clears
            # the selection, but refreshing here keeps the buffer and
            # the error-notice state in lockstep without depending on
            # signal ordering.
            self.refresh()
            return
        if latest != self._current_note:
            self.refresh()

    # ------------------------------------------------------------------
    # Renderer wiring
    # ------------------------------------------------------------------

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
    def error_notice_visible(self) -> bool:
        """``True`` iff the in-surface parse-error notice is showing.

        Public read-only so tests can assert the success / failure
        bookkeeping without inspecting the buffer's tagged contents.
        """
        return self._error_message is not None

    @property
    def error_notice_text(self) -> str:
        """The kind-specific message of the parse-error notice.

        Empty when no parse error is being shown (the buffer holds a
        real render or is empty). This is the per-error *message* line
        only — not the static headline or hint.
        """
        return self._error_message or ""


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


_CELL_MEASURE_MONOSPACE_FAMILY: str = "monospace"
"""Font family the cell-width measurer applies for monospace runs.

It must match the family the :data:`TagName.MONOSPACE` tag sets (also
``"monospace"``) so a measured monospace cell width tracks how the tag
actually renders it. Kept local to the production measurer; the small
per-column gutter absorbs any residual difference.
"""


def make_cell_width_measurer(widget: Gtk.Widget) -> CellWidthMeasurer:
    """Build the production :data:`CellWidthMeasurer` for a widget's font.

    The returned closure lays the run's text out in the widget's Pango
    context, applying a bold-weight and/or monospace-family attribute to
    match the run's width class, and returns the logical pixel width.
    Shared by :class:`NoteView` and
    :class:`giruntime.ui.help_window.HelpWindow` — both build their own
    renderer and wire its ``cell_width_px`` from the article view via
    this one factory, so a table fits its column identically in a note
    and in the help reference.
    """

    def measure(text: str, bold: bool, monospace: bool) -> int:
        layout = widget.create_pango_layout(text)
        if bold or monospace:
            end_index = len(text.encode("utf-8"))
            attrs = Pango.AttrList.new()
            if bold:
                weight = Pango.attr_weight_new(Pango.Weight.BOLD)
                weight.start_index = 0
                weight.end_index = end_index
                attrs.insert(weight)
            if monospace:
                family = Pango.attr_family_new(_CELL_MEASURE_MONOSPACE_FAMILY)
                family.start_index = 0
                family.end_index = end_index
                attrs.insert(family)
            layout.set_attributes(attrs)
        _, log_rect = layout.get_pixel_extents()
        return int(log_rect.width)

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
