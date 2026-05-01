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
  by :class:`TextBufferRenderer`.
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
  every image renders as the small ``[Image: filename]`` placeholder
  widget. Tests that *do* care wire a fake :class:`AttachmentStoreProtocol`.
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
  but never reshapes the widget tree. This keeps GTK's child-anchor
  bookkeeping clean: a render that fails (parse error) leaves the
  previous valid render in place, exactly as the plan's error-handling
  policy requires.
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
from notes_app.config.defaults import TARGET_CHARS_PER_LINE
from notes_app.controllers.app_state import AppState
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
the first call — see :meth:`ArticleContainer.target_column_width`.
"""


_FALLBACK_CHAR_WIDTH_PX: int = 8
"""Defensive fallback if the production measurer reports a non-positive
width. A real font's "M" is never zero pixels wide, but defending
against a corner case (e.g. measuring before the widget has any font at
all) keeps the column at least usable rather than collapsing to zero.
"""


def _placeholder_image_bytes(_filename: str) -> bytes:
    """Fallback image resolver used when no attachment store is wired.

    The renderer attempts ``Gdk.Texture.new_from_bytes`` on the result.
    Empty bytes raise ``GLib.Error``, which the renderer catches and
    converts into its small ``[Image: filename]`` placeholder widget.

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

    * :meth:`do_measure` reports :meth:`target_column_width` as both the
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

    Construction takes a :data:`CharWidthMeasurer`. The measurer is
    invoked exactly once across the container's lifetime — the result
    is cached and used for every subsequent
    :meth:`target_column_width` call (which both vfuncs above invoke).
    """

    _char_width_measurer: CharWidthMeasurer
    _cached_char_width_px: int | None

    def __init__(self, *, char_width_measurer: CharWidthMeasurer) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._char_width_measurer = char_width_measurer
        self._cached_char_width_px = None
        # ``hexpand`` is what tells the parent ``Gtk.ScrolledWindow``'s
        # ``Gtk.Viewport`` to allocate us *more* than our natural width
        # when there is room — without it, the viewport would clamp us
        # to the natural width and the wide-window margin path would
        # never fire because ``do_size_allocate`` would always receive
        # exactly :meth:`target_column_width`.
        self.set_hexpand(True)

    def target_column_width(self) -> int:
        """Return the desired pixel width of the article column.

        Computed as :data:`TARGET_CHARS_PER_LINE` × the measured glyph
        width. The measurement is taken on the first call and cached
        afterwards. A non-positive measurement is replaced by
        :data:`_FALLBACK_CHAR_WIDTH_PX` so the column is never zero
        pixels wide.
        """
        if self._cached_char_width_px is None:
            measured = self._char_width_measurer()
            self._cached_char_width_px = (
                measured if measured > 0 else _FALLBACK_CHAR_WIDTH_PX
            )
        return TARGET_CHARS_PER_LINE * self._cached_char_width_px

    def do_measure(  # pylint: disable=arguments-differ
        self,
        orientation: Gtk.Orientation,
        for_size: int,
    ) -> tuple[int, int, int, int]:
        """Report the target column width as both min and natural.

        On the horizontal axis, both minimum and natural width equal
        :meth:`target_column_width`. The minimum is what makes a
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
            target = self.target_column_width()
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
        :meth:`target_column_width`, the slack ``width - target`` is
        split equally between ``margin-start`` and ``margin-end``.
        Otherwise both margins are 0 — the parent ``ScrolledWindow``
        scrolls horizontally to expose the column at its target size.

        Margin writes are guarded with an inequality check so the
        ``queue_resize`` they trigger does not produce an oscillating
        layout pass: once the value stabilises, subsequent allocates
        with the same ``width`` are no-ops on the margins.
        """
        target = self.target_column_width()
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
    seven because step 11 introduces two new fields
    (:attr:`_attachments`, :attr:`_current_note_id`) on top of the
    five already required to wire the renderer + selection plumbing.
    Splitting these into a helper class would obscure the obvious
    "the view holds the things it needs to render" relationship.
    """

    # Only fields used outside ``__init__`` are stored on ``self``.
    # The transient widgets built during construction
    # (``Gtk.TextTagTable``, :class:`ArticleContainer`,
    # ``Gtk.ScrolledWindow``) are kept alive by their GTK parent-child
    # references — adding them as ``self.`` attributes would duplicate
    # those references for no behavioural benefit.
    _note_repository: NoteRepositoryProtocol
    _attachments: AttachmentStoreProtocol | None
    _app_state: AppState
    _buffer: Gtk.TextBuffer
    _text_view: Gtk.TextView
    _renderer: TextBufferRenderer
    _current_note_id: str | None

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
        # text view. Production wires the char-width measurer to a
        # Pango-layout closure against the text view.
        article_container = ArticleContainer(
            char_width_measurer=_make_pango_char_width_measurer(self._text_view),
        )
        article_container.append(self._text_view)

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
        # for the same reason.
        self._renderer = TextBufferRenderer(
            image_bytes_for=self._resolve_image_bytes,
            column_width_px=article_container.target_column_width,
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

        * No selection → buffer cleared, ``_current_note_id`` cleared.
        * Selection points to a note that no longer exists → buffer
          cleared, ``_current_note_id`` cleared. The note-list widget
          elsewhere will pick a new selection on its next refresh;
          this view does not second-guess.
        * Parse error in the source → buffer left untouched (so the
          previous valid render stays visible). The plan's error-
          handling policy is to surface parse errors via the editor
          gutter; doing nothing here is what preserves that contract.
          ``_current_note_id`` IS still updated to the new selection
          so the next render attempt and any image lookup target the
          right note.
        """
        note_id = self._app_state.selected_note_id
        if note_id is None:
            self._current_note_id = None
            self._buffer.set_text("")
            return
        try:
            note = self._note_repository.get(note_id)
        except KeyError:
            self._current_note_id = None
            self._buffer.set_text("")
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
        except ParseError:
            # Per the plan: "the source is still saved (the user's
            # text is sacred) — only the *rendered* view is gated on
            # parse success." Leaving the buffer untouched keeps the
            # last-good render in place. Step 9+ surfaces the error
            # through an editor-side panel; step 8 simply preserves
            # the old render.
            return

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
        its ``[Image: filename]`` placeholder widget. This matches
        the placeholder-bytes contract from build step 8.

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
        # produces the placeholder widget on empty bytes, which is
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
