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
* **The ``items-changed`` re-render is deferred while the pane is not
  visible.** In EDIT mode the pane is the hidden child of the right-pane
  ``Gtk.Stack``, yet the editor's debounced autosave splices a fresh row
  every ~300 ms of typing; rendering there would re-parse and rebuild a
  buffer nobody is looking at (and re-decode its images) on every pause.
  Instead, when the injected :data:`PaneVisibilityPredicate` reports the
  pane hidden, ``items-changed`` sets :attr:`_render_pending` rather than
  rendering, and the pane renders exactly once on becoming visible again
  (its ``map`` handler, plus the unconditional :meth:`refresh` the
  view-mode toggle already performs). The *selection-change* re-render is
  **not** gated — switching notes is a one-shot event, not a per-keystroke
  storm — so the pane always shows the selected note the instant it is
  shown. :meth:`refresh` stays the unconditional "render now" primitive
  and clears :attr:`_render_pending`, so no combination leaves an owed
  render outstanding after a render has happened.
* **Clicks in the read pane run through one :class:`LinkHandler`.** The
  renderer tags every clickable range with a closed
  :data:`~giruntime.ui.note_render.textbuffer_renderer.ActivationTarget`;
  the handler dispatches a ``UrlTarget`` to the URI launcher and an
  ``AttachmentTarget`` to this view's :meth:`_activate_attachment`, which
  resolves the filename against the current note, opens a *save* dialog
  pre-filled with the attachment's name, and hands the chosen path to
  :meth:`NoteController.export_attachment` (storage owns the write). A
  filename matching no attachment opens **no** dialog. The save-link and
  image macros share one filename→attachment lookup
  (:meth:`_attachment_named`), so they cannot disagree.
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
* **Unreadable source is shown, not hidden.** :meth:`refresh` never
  has to handle a parse failure: the renderer parses with
  :func:`asciidoc.parser.parse_recovering`, so a note that will not
  parse still renders, with the source folio could not read carried
  verbatim into the buffer in the position it occupied. A stray
  character on line 300 costs that construct, not the whole note.
  :meth:`render_into` returns the :class:`asciidoc.ast.UnreadBlock`\\ s it
  emitted, and this pane keeps the count of the *marked* ones (see
  :attr:`unread_block_count`) — the inline ones render as ordinary prose
  and carry no mark, so counting them would report a problem the reader
  has no evidence of.
* The user-facing message table (:func:`message_for`) lives in the UI
  layer rather than on :class:`ParseError` because the parser is pure and
  reusable; embedding UI copy in it would couple the parser to this UI's
  tone. It is now read by the *renderer*, which is what places a reason
  beside the source it explains. The mapping stays exhaustive over
  :class:`ParseErrorKind`, so adding a new error kind still forces an
  update there — caught by a unit test that iterates the enum.
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

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gi.repository import GObject, Gtk, Pango

from config.defaults import (
    ARTICLE_BOTTOM_MARGIN_LINES,
    ARTICLE_END_GAP_LINES,
    ARTICLE_INNER_HPADDING_CHARS,
    ARTICLE_TOP_MARGIN_LINES,
)
from enums import UnreadScope
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui._dates import format_date_long
from giruntime.ui._file_picker import (
    FileSaveDialogOpener,
    default_file_save_dialog_opener,
)
from giruntime.ui.article_container import (
    ArticleContainer,
    CharWidthMeasurer,
    LineHeightMeasurer,
)
from giruntime.ui.link_handler import (
    LinkHandler,
    UriLauncherFactory,
    default_launcher_factory,
)
from giruntime.ui.note_render.article_text_view import ArticleTextView
from giruntime.ui.note_render.tag_table import (
    TagName,
    build_tag_table,
)
from giruntime.ui.note_render.textbuffer_renderer import (
    CellWidthMeasurer,
    ImageBytesResolver,
    TextBufferRenderer,
)
from models.attachment import Attachment
from models.note import Note
from storage.protocols import AttachmentStoreProtocol


type PaneVisibilityPredicate = Callable[[], bool]
"""Callable answering "is the read pane currently on screen?".

Injected at construction of :class:`NoteView` (same dependency-injection
seam as :data:`CharWidthMeasurer` and the renderer's resolvers) so the
pane can gate its store-driven re-render on visibility without knowing it
lives in a :class:`Gtk.Stack`. Production defaults it to the pane's own
:meth:`Gtk.Widget.get_mapped` — a widget is *mapped* only when it is the
stack's shown child *and* its window is on screen, which is exactly the
"nobody is looking" test the deferral needs. Tests pass a synchronous
fake reporting visible / hidden deterministically, since a directly
constructed widget is never mapped.
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
    3. the shared tag table, parameterised by the measured M-width and by
       the view's own palette (the view owns the colour-scheme decision,
       so asking it here is what starts the surface self-consistent), and
       a buffer on it;
    4. the block-tint wash map, installed via the one shared seam
       :meth:`ArticleTextView.install_wash_specs_from_table`;
    5. the fixed-width :class:`ArticleContainer` wrapping the view, with
       the four font-relative margins + desk gaps applied.

    Returns the bundle; the caller owns the renderer (its image resolver
    is caller-specific) and parents :attr:`ArticleSurface.container` into
    its own scroller.

    Nothing here has to handle a *later* theme change: the view re-colours
    the tag table it was given and re-installs its own washes when the
    style changes (see :meth:`ArticleTextView.do_css_changed`), so both
    callers of this factory get dark-mode support without knowing about
    it.
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

    tag_table = build_tag_table(
        char_width_px=char_width_measurer(),
        palette=text_view.palette(),
    )
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
    the parse-error notice adds :attr:`_error_message` (the message
    currently shown in the surface, or ``None``), and the hidden-pane
    render deferral adds :attr:`_pane_is_visible` (the injected
    visibility predicate) and :attr:`_render_pending` (whether a render
    is owed). Splitting these into a helper class would obscure the
    obvious "the view holds the things it needs to render" relationship.
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
    _note_controller: NoteController | None
    _save_dialog_opener: FileSaveDialogOpener
    _app_state: AppState
    _buffer: Gtk.TextBuffer
    _text_view: ArticleTextView
    _renderer: TextBufferRenderer
    _link_handler: LinkHandler
    _current_note_id: str | None
    _current_note: Note | None
    _unread_block_count: int
    _outer_column_width_px: int
    # "Is the pane on screen?" — injected so the store-driven re-render
    # can be skipped while the pane is the hidden stack child, and so the
    # deferral is testable without a real mapped window. Defaults to this
    # widget's ``get_mapped``; see :data:`PaneVisibilityPredicate`.
    _pane_is_visible: PaneVisibilityPredicate
    # Set true when a store ``items-changed`` arrived while the pane was
    # hidden, meaning a render is owed. Consulted (and cleared) on the
    # next map; also cleared whenever :meth:`refresh` renders, so a
    # visible render never leaves a stale "owed" flag behind.
    _render_pending: bool

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        note_store: NoteListStore,
        app_state: AppState,
        attachments: AttachmentStoreProtocol | None = None,
        note_controller: NoteController | None = None,
        save_dialog_opener: FileSaveDialogOpener = (
            default_file_save_dialog_opener
        ),
        launcher_factory: UriLauncherFactory = default_launcher_factory,
        pane_is_visible: PaneVisibilityPredicate | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_store = note_store
        self._attachments = attachments
        # The visibility predicate defaults to this widget's own
        # ``get_mapped``. It cannot be a module-level default value
        # because it is a bound method that only exists once ``self``
        # does, so the ``None`` sentinel selects the production default
        # here — the established pattern for "the default is a bound
        # method of the instance". Tests inject a fake; see
        # :data:`PaneVisibilityPredicate`.
        self._pane_is_visible = (
            pane_is_visible if pane_is_visible is not None else self.get_mapped
        )
        # No render is owed until a hidden-pane ``items-changed`` records
        # one. The initial ``refresh`` below renders unconditionally and
        # leaves this false.
        self._render_pending = False
        # The controller performs the export (storage owns the file I/O);
        # the opener asks the user where to put it. Both are ``None`` /
        # defaulted on the same contract as ``attachments``: a view built
        # without them still renders — a save link then reports "no
        # attachment" rather than writing anything.
        self._note_controller = note_controller
        self._save_dialog_opener = save_dialog_opener
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
        # How many *marked* unread blocks the rendered buffer is
        # showing. Kept in step with the surface by :meth:`refresh`,
        # which is the only writer — see :attr:`unread_block_count`.
        self._unread_block_count = 0

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
            attachments_for=self._list_attachments,
            column_width_px=surface.container.text_column_width,
            cell_width_px=make_cell_width_measurer(surface.text_view),
            tag_table=surface.tag_table,
        )

        # Clicks in the read pane: a web link launches, an
        # ``attachment:`` save link opens the save dialog. Both ride the
        # one handler — the renderer hands it a closed activation target,
        # and this view supplies the attachment half of the dispatch.
        self._link_handler = LinkHandler(
            text_view=self._text_view,
            renderer=self._renderer,
            launcher_factory=launcher_factory,
            attachment_activator=self._activate_attachment,
        )
        self._link_handler.install()

        # Subscribe to the selected-note signal. The handler is a bound
        # method so disconnecting later is simple if the widget is ever
        # torn down — but step 8 has a single window for the lifetime
        # of the application, so explicit disconnection isn't wired up.
        self._subscribe_to_state_and_store()

        # Initial render: pick up whatever ``selected_note_id`` is set
        # to before the view was constructed.
        self.refresh()

    def _subscribe_to_state_and_store(self) -> None:
        """Wire the re-render triggers: selection, store edits, and map."""
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
        # When the pane becomes visible again (the stack reveals it),
        # perform any render deferred while it was hidden. The common
        # EDIT->VIEW toggle also drives an unconditional
        # :meth:`refresh` from ``MainWindow``, so this handler is the
        # self-contained belt-and-braces path for any other reveal — and
        # it renders only when one is actually owed, so it never
        # double-renders on top of that toggle's refresh.
        self.connect("map", self._on_mapped)

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

        This is the unconditional "render now" primitive: it always
        renders regardless of pane visibility and clears
        :attr:`_render_pending`, so any render deferred while the pane
        was hidden is satisfied and never re-fires on a later map. The
        visibility-gated entry point is :meth:`_refresh_or_defer`.
        """
        # A render is happening now, so nothing is owed afterwards.
        # Cleared up front so every early-return path below leaves the
        # flag false.
        self._render_pending = False
        note_id = self._app_state.selected_note_id
        if note_id is None:
            self._clear_surface()
            return
        try:
            note = self._note_store.get_note(note_id)
        except KeyError:
            self._clear_surface()
            return
        # Update the resolver's view of "current note" BEFORE invoking
        # the renderer, so any image macro encountered during the
        # render walk sees the right scope. Updating after would race
        # with the renderer's own image-resolver calls. ``_current_note``
        # is set alongside so the post-title metadata hook can read the
        # note's timestamps and tags during the render.
        self._current_note_id = note.id
        self._current_note = note
        unread = self._renderer.render_into(
            note.source,
            self._buffer,
            note_id=note.id,
            post_title_hook=self._insert_metadata_after_title,
        )
        # Only a structurally-unread block is marked in the surface, so
        # only those are counted: the number this reports and the number
        # of marks on screen are the same number.
        self._unread_block_count = sum(
            1 for block in unread if block.scope is UnreadScope.BLOCK
        )

    def _clear_surface(self) -> None:
        """Empty the buffer and the per-note state that describes it.

        The one path for "there is nothing to show": no selection, or a
        selection pointing at a note the store no longer has. Keeping it
        in one place is what stops the buffer and
        :attr:`_unread_block_count` drifting apart.
        """
        self._current_note_id = None
        self._current_note = None
        self._buffer.set_text("")
        self._unread_block_count = 0

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
        """Refresh on a selection change. Notify-only handler.

        Rendered immediately, *not* deferred on visibility: selecting a
        different note is a one-shot event, not the per-keystroke storm
        the hidden-pane deferral targets, and rendering it now keeps the
        buffer correct for the moment the pane is next shown.
        """
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

        The re-render is routed through :meth:`_refresh_or_defer`, so
        when the pane is hidden (EDIT mode) the debounced autosave that
        drives this signal only marks a render owed instead of rebuilding
        an off-screen buffer every ~300 ms — the reveal render catches it
        up. Both the deleted-note branch and the content-changed branch
        defer identically: while hidden nothing is on screen to fall out
        of lockstep, and the reveal render restores it before anything is
        shown (on a delete the controller has also cleared the selection,
        so that render clears the buffer).
        """
        if self._current_note_id is None:
            return
        try:
            latest = self._note_store.get_note(self._current_note_id)
        except KeyError:
            # The displayed note was deleted; the controller also clears
            # the selection. Deferring while hidden keeps the buffer and
            # the error-notice state in lockstep on the next reveal
            # without depending on signal ordering.
            self._refresh_or_defer()
            return
        if latest != self._current_note:
            self._refresh_or_defer()

    def _refresh_or_defer(self) -> None:
        """Render now if the pane is visible, else mark a render owed.

        The visibility-gated entry point for the store-driven re-render.
        When :attr:`_pane_is_visible` reports the pane on screen it
        renders immediately via :meth:`refresh`; otherwise it records the
        owed render on :attr:`_render_pending`, which :meth:`_on_mapped`
        (or the view-mode toggle's unconditional :meth:`refresh`) drains
        when the pane is next shown.
        """
        if self._pane_is_visible():
            self.refresh()
        else:
            self._render_pending = True

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        """Drain a deferred render when the pane becomes visible.

        Renders iff one was owed (:attr:`_render_pending`), so on the
        common EDIT->VIEW path — where ``MainWindow`` already called
        :meth:`refresh` (which cleared the flag) before the stack mapped
        this pane — it is a no-op and there is no double render.
        """
        if self._render_pending:
            self.refresh()

    # ------------------------------------------------------------------
    # Renderer wiring
    # ------------------------------------------------------------------

    def _attachment_named(self, filename: str) -> Attachment | None:
        """Find the current note's attachment called ``filename``.

        The single filename→attachment lookup, shared by the image
        resolver (``image::FILE[]``) and the save-link activator
        (``attachment:FILE[label]``) — the two macros key on the same
        thing, so they must resolve it the same way.

        Returns :data:`None` when there is no attachment store, no
        current note, or no attachment of that name. The scan is linear
        over :meth:`AttachmentStoreProtocol.list_for_note` (metadata
        only — the BLOB column is not selected): for v1's "handful of
        attachments per note" that is cheaper than a cache which would
        have to be invalidated on every add / remove.
        """
        if self._attachments is None or self._current_note_id is None:
            return None
        for attachment in self._attachments.list_for_note(
            self._current_note_id,
        ):
            if attachment.filename == filename:
                return attachment
        return None

    def _list_attachments(self) -> tuple[Attachment, ...]:
        """The :data:`AttachmentListResolver` plugged into the renderer.

        Feeds the ``attachments::[]`` expansion the current note's
        attachment **metadata**, in ``list_for_note`` order (insertion
        order). Metadata only: no BLOB is read to draw the table, which
        is the point of the metadata/bytes split. An empty tuple — no
        store, no selection, or a note with no attachments — expands to
        the italic "No attachments." paragraph.
        """
        if self._attachments is None or self._current_note_id is None:
            return ()
        return tuple(self._attachments.list_for_note(self._current_note_id))

    def _activate_attachment(self, filename: str) -> None:
        """The :data:`AttachmentActivator` the link handler dispatches to.

        Called when the reader clicks an ``attachment:FILE[…]`` link (in
        prose or in a generated ``attachments::[]`` row — both are the
        same AST node, so both land here).

        A filename matching no attachment of the current note is a *dead
        link*: the parser could not know (it is storage-free), so this is
        where it surfaces — and no dialog is opened. Otherwise the save
        dialog opens pre-filled with the attachment's name
        (``Path(...).name``, defence in depth: the parser already rejects
        separators), and a chosen path is handed to the controller, which
        owns the write. Cancelling writes nothing.
        """
        attachment = self._attachment_named(filename)
        if attachment is None or self._note_controller is None:
            return
        controller = self._note_controller

        def _on_path_chosen(destination: Path | None) -> None:
            if destination is None:
                # Cancelled, backend error, or a non-local URI — all
                # three mean "no path", and all three mean do nothing.
                return
            controller.export_attachment(attachment.id, destination)

        self._save_dialog_opener(
            self,
            Path(attachment.filename).name,
            _on_path_chosen,
        )

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

        Lookup goes through the shared :meth:`_attachment_named` — the
        same helper the ``attachment:`` save link uses, because both
        macros name an attachment by filename and must agree on what
        that resolves to.
        """
        attachment = self._attachment_named(filename)
        if attachment is None or self._attachments is None:
            # No store, no current note, or no attachment of that name.
            # The renderer's decode-failure branch produces the
            # placeholder paintable on empty bytes, which is the right
            # user-visible signal for "image not found".
            return _placeholder_image_bytes(filename)
        return self._attachments.get_bytes(attachment.id)

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
    def unread_block_count(self) -> int:
        """How many unread-source marks the rendered surface is showing.

        ``0`` for a note that parses cleanly, for a note whose only
        failures were inline (those render as ordinary prose and carry no
        mark), and when nothing is selected. Counts
        :data:`enums.UnreadScope.BLOCK` nodes only, so it never reports a
        problem the reader has no evidence of.
        """
        return self._unread_block_count


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
