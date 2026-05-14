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
  :class:`ArticleContainer` (a ``Gtk.Box`` subclass that enforces the
  fixed-width text column rule) → read-only ``Gtk.TextView`` populated
  by :class:`TextBufferRenderer`. A :class:`Gtk.Revealer` containing
  the parse-error banner is *prepended* to that stack — it sits above
  the scroller at the top of the pane and is hidden by default.
* :class:`ArticleContainer` enforces the text-column rule: when allocated
  *wider* than the target column, the slack becomes equal left/right
  margins, centring the column; when allocated *narrower*, both margins
  are 0 and the parent ``ScrolledWindow`` is responsible for the
  horizontal scrollbar — the column never shrinks. The font never
  scales with window width (see §2 / decision 7 of the plan).
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
* The size-allocate vfunc — *not* the ``size-allocate`` signal, which is
  deprecated in GTK 4 — is the documented place to react to a fresh
  allocation. :meth:`ArticleContainer.do_size_allocate` updates
  :attr:`Gtk.Widget.margin-start` and :attr:`Gtk.Widget.margin-end`
  on ``self`` only when the values would actually change, so the
  ``queue_resize`` that follows a margin write does not introduce an
  oscillating layout pass.
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

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk  # noqa: E402

from notes_app.asciidoc.tag_table import build_tag_table
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


class ArticleContainer(Gtk.Box):
    """A vertical ``Gtk.Box`` that pins its content to a fixed column.

    The container is *vertical* (so the rendered article, plus future
    breadcrumb / metadata strip, stack top-to-bottom). Two vfunc
    overrides together implement the column-width rule from §2 of the
    plan:

    * :meth:`do_measure` reports :meth:`outer_column_width` as both the
      minimum and natural width on the horizontal axis. The minimum is
      what makes the parent ``Gtk.ScrolledWindow`` show a horizontal
      scrollbar when its allocation is below the target — the column
      does not shrink. The natural width gives parents a hint about
      our preferred size when nothing else constrains them.
    * :meth:`do_size_allocate` updates ``margin-start`` and
      ``margin-end`` so a *wider* allocation absorbs its slack as equal
      side margins (centring the column) without changing the inner
      content area's width. Chaining to ``Gtk.Box.do_size_allocate``
      lets the standard box layout run against the (now narrower by
      ``2 × margin``) inner area.

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

    def __init__(
        self,
        *,
        char_width_measurer: CharWidthMeasurer,
        line_height_measurer: LineHeightMeasurer,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._char_width_measurer = char_width_measurer
        self._line_height_measurer = line_height_measurer
        self._cached_char_width_px = None
        self._cached_line_height_px = None
        # ``hexpand`` is what tells the parent ``Gtk.ScrolledWindow``'s
        # ``Gtk.Viewport`` to allocate us *more* than our natural width
        # when there is room — without it, the viewport would clamp us
        # to the natural width and the wide-window margin path would
        # never fire because ``do_size_allocate`` would always receive
        # exactly :meth:`outer_column_width`.
        self.set_hexpand(True)

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
        and the extra is absorbed by the size-allocate margin logic.

        On the vertical axis, defer to :class:`Gtk.Box`'s standard
        measurement (sum of children's heights at ``for_size``).
        Baselines are not meaningful for this widget.
        """
        if orientation == Gtk.Orientation.HORIZONTAL:
            target = self.outer_column_width()
            return (target, target, -1, -1)
        # ``for_size`` here is the horizontal allocation; passing it
        # through means children that wrap (e.g. ``Gtk.TextView`` in
        # ``WORD_CHAR`` mode) compute their height against the actual
        # column width.
        return Gtk.Box.do_measure(self, orientation, for_size)

    def do_size_allocate(  # pylint: disable=arguments-differ
        self,
        width: int,
        height: int,
        baseline: int,
    ) -> None:
        """Centre the article column horizontally inside ``width``.

        When ``width`` is strictly greater than
        :meth:`outer_column_width`, the slack ``width - target`` is
        split equally between ``margin-start`` and ``margin-end``.
        Otherwise both margins are 0 — the parent ``ScrolledWindow``
        scrolls horizontally to expose the column at its target size.

        Margin writes are guarded with an inequality check so the
        ``queue_resize`` they trigger does not produce an oscillating
        layout pass: once the value stabilises, subsequent allocates
        with the same ``width`` are no-ops on the margins.
        """
        target = self.outer_column_width()
        if width > target:
            side_margin = (width - target) // 2
        else:
            side_margin = 0

        if self.get_margin_start() != side_margin:
            self.set_margin_start(side_margin)
        if self.get_margin_end() != side_margin:
            self.set_margin_end(side_margin)

        Gtk.Box.do_size_allocate(self, width, height, baseline)


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
    # them on every selection change.
    _note_repository: NoteRepositoryProtocol
    _attachments: AttachmentStoreProtocol | None
    _app_state: AppState
    _buffer: Gtk.TextBuffer
    _text_view: Gtk.TextView
    _renderer: TextBufferRenderer
    _current_note_id: str | None
    _error_banner_revealer: Gtk.Revealer
    _error_banner_label: Gtk.Label

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

        # Build the text-buffer rendering substrate. The tag table is
        # owned by the buffer in GTK 4; constructing the buffer with
        # ``Gtk.TextBuffer.new(tag_table)`` is the only way to associate
        # them.
        tag_table = build_tag_table()
        self._buffer = Gtk.TextBuffer.new(tag_table)

        # The actual text-rendering widget. Read-only, hides the cursor,
        # word-wraps long lines so prose flows naturally inside the
        # column.
        self._text_view = Gtk.TextView.new_with_buffer(self._buffer)
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        # The text view itself fills the inner column. Vertical expand
        # is what gives the scroller something to scroll.
        self._text_view.set_hexpand(True)
        self._text_view.set_vexpand(True)

        # The article container: a fixed-width column wrapping the
        # text view. Production wires the two measurers (M-width and
        # line-height) to Pango-layout closures against the text view
        # — see :func:`_build_font_measurers` for the single seam tests
        # monkey-patch.
        char_width_measurer, line_height_measurer = _build_font_measurers(
            self._text_view,
        )
        article_container = ArticleContainer(
            char_width_measurer=char_width_measurer,
            line_height_measurer=line_height_measurer,
        )
        article_container.append(self._text_view)

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
