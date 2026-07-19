"""Walks an AsciiDoc :class:`Document` and populates a ``Gtk.TextBuffer``.

Principles & invariants
-----------------------
* The renderer is the **only** module in :mod:`asciidoc` that
  imports ``gi`` at runtime alongside :mod:`tag_table`. Everything from
  ``ast`` through ``parser`` is pure. The renderer never imports from
  :mod:`storage` — image bytes flow exclusively through the
  injected :data:`ImageBytesResolver` callable.
* :meth:`TextBufferRenderer.render_into` is the single public entry
  point. It clears the buffer and rebuilds it from scratch on every
  call. There is no incremental-update path: the source change → AST →
  buffer pipeline is short enough that "rebuild" is the cheapest
  consistent strategy.
* **Every block-level construct renders as styled paragraphs / inline
  paintables in the buffer — no construct escapes to a child widget.**
  Admonitions, blockquotes, and code blocks are inserted directly into
  the buffer with paragraph tags that carry the background tint,
  margins, and padding. Images are inserted via
  :meth:`Gtk.TextBuffer.insert_paintable` so they participate in the
  buffer's native selection model. Tables, formerly the one anchored
  widget, are now native buffer text too: each row is one logical line
  of tab-separated cells, aligned by a per-table :class:`Pango.TabArray`
  and tagged with :data:`TagName.TABLE_HEADER` / :data:`TagName.TABLE_ROW`
  (see :meth:`_emit_table`). :class:`Gtk.TextTag` has no grid primitive,
  but a tab array plus snapshot-painted row washes expresses a
  left-aligned grid without one.
* The selection contract follows from the above: drag-select works
  across all prose, headings, lists, admonitions, blockquotes, code
  blocks, tables, and images; ``Ctrl+A`` selects everything; ``Ctrl+C``
  copies the buffer text unchanged. There is no selection break — a
  table is selectable / copyable buffer text like everything else.
* Images use a private :class:`_ScaledImagePaintable` that wraps a
  :class:`Gdk.Texture` and reports
  ``min(texture_width, column_width_px)`` as its intrinsic width, with
  height scaled proportionally. The texture is the actual drawing —
  the wrapper exists only to constrain intrinsic dimensions so a
  large image doesn't overflow the article column. Decode failures
  (``GLib.Error`` from :meth:`Gdk.Texture.new_from_bytes`) produce a
  :class:`_PlaceholderImagePaintable` instead — a tiny grey rectangle
  that signals the missing image without aborting the whole render.
* The :data:`ColumnWidthResolver` is read once per render (callers can
  call :meth:`render_into` again after a column-width change). It is
  consulted by both the image and table paths: tables divide it into
  per-column tab stops, images cap their intrinsic width against it via
  the :class:`_ScaledImagePaintable` constructor.
* Table cells are fitted to their column by measurement, not wrapping.
  With ``wrap-mode = NONE`` on the row, an over-wide cell would push
  Pango on to the next tab stop and cascade the row's later cells out of
  alignment. Pango offers no per-tab-column ellipsization, so the
  renderer measures cell text through the injected
  :data:`CellWidthMeasurer` and truncates over-budget cells with an
  ellipsis (:func:`_truncate_cell`). Cells are padded symmetrically: the
  row tag's ``left-margin`` insets the text by
  :data:`config.defaults.TABLE_CELL_HPADDING_PX` on the left, and this
  path reserves ``2 ×`` that value as the right truncation budget, so a
  fitted cell ends the same distance short of the next column's boundary
  and never reaches its tab stop. Copying a truncated cell yields the
  truncated display text — the rendered buffer is a read-only projection
  of the source, not the source of truth.
* Inline runs are emitted with a tag stack. A run of plain
  :class:`Text` records its start offset, inserts text, and applies
  every tag currently on the stack to the inserted range. This makes
  nested formatting (``*_bold italic_*``) commute with insert order
  and keeps the algorithm single-pass.
* Block separation is handled by the renderer, not by AST nodes
  themselves: every block ends in exactly one trailing newline that
  also acts as the visual gap to the next block. A redundant trailing
  newline at the very end of the buffer is trimmed so the buffer does
  not finish with a blank line.
* An ``attachments::[]`` macro never reaches the emit walk: ``render_into``
  first runs the pure :func:`~giruntime.ui.note_render.attachment_table.expand_attachment_tables`
  transform, which replaces the macro node with an ordinary
  :class:`~asciidoc.ast.Table` built from the injected
  :data:`AttachmentListResolver` (metadata only — no BLOB is read to draw
  the table), or with an italic "No attachments." paragraph when the note
  has none. The generated table therefore reuses ``_emit_table``'s column
  geometry wholesale rather than duplicating it, and the save links in its
  name column are the *same* :class:`~asciidoc.ast.AttachmentLink` node a
  hand-written ``attachment:`` macro produces — one activation mechanism,
  not two.
* Links carry their *URL identity* on a per-link **anonymous** tag
  (a :class:`Gtk.TextTag` constructed with ``name=None``) that is
  added to the tag table at render time. The renderer keeps a
  ``dict[Gtk.TextTag, ActivationTarget]`` mapping each anonymous tag to
  what it activates — a :class:`UrlTarget` for a web link, an
  :class:`AttachmentTarget` for an ``attachment:`` save link — exposed via
  :meth:`target_for_tags` as a closed union. The shared
  :data:`TagName.LINK` tag from :mod:`tag_table` provides the visual
  styling — colour and underline — that every link shares, save links
  included (**no new tag**: both mean "clickable"). Anonymous
  tags from a *previous* render are removed from the tag table at the
  start of every :meth:`render_into` call so the table cannot
  accumulate stale link tags as the user edits a note.
* Monospace inline content is emitted as plain text with the
  :data:`TagName.MONOSPACE` tag added to whatever tag stack is
  currently active. Because :class:`Monospace` carries its content as
  a single literal :class:`str` (not a list of further inline nodes),
  it never recurses through :meth:`_emit_inline`.
* :meth:`render_into` accepts an optional :data:`PostTitleHook`. When
  supplied, it is invoked **exactly once per render** with the
  ``buffer`` whose insertion point (end iter) sits at the boundary
  between the rendered title and the first body block. The title emits
  only a **single** trailing newline (:data:`HeadingTrailing.SINGLE_NEWLINE`)
  so the hook's inserted text hugs the title on the next line; the
  renderer then inserts the remaining :data:`_BLOCK_SEPARATOR` newline
  *after* the hook's text, so the first body block drops a clear blank
  line below it. When the document has no title, the same hook fires
  with the insertion point at buffer-start; the block separator is
  still inserted after it, so the inserted text sits alone on the first
  line and the body drops a clear line below it. The hook fires *after*
  the buffer has been cleared and the title (if any) has been emitted,
  but *before* any body block is walked, so the caller observes a
  stable insertion point. The renderer creates **no** child anchor on
  this path — the previous render's contents are destroyed by
  ``buffer.set_text("")``, and the caller inserts plain (tagged) text
  rather than anchoring a widget. The hook is **not** invoked when
  :meth:`render_into` raises a :class:`ParseError` — the buffer is
  unchanged by the raise.
"""

# The module's size reflects the breadth of block kinds the renderer
# handles end-to-end: heading, paragraph, ordered/unordered lists,
# code block, image, table, admonition, and blockquote — each with
# its own emit helper, plus the inline-tag application code, the
# table cell-flatten / measure / truncate / tab-stop helpers, and the
# image paintables. Splitting solely to satisfy the line counter would
# scatter helpers that share private constants and conventions.
# pylint: disable=too-many-lines

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from gi.repository import Gdk, GLib, GObject, Graphene, Gtk, Pango

from asciidoc.ast import (
    Admonition,
    AttachmentLink,
    AttachmentTable,
    BlockNode,
    Blockquote,
    Bold,
    CodeBlock,
    HardBreak,
    Image,
    InlineNode,
    Italic,
    Link,
    ListItem,
    Monospace,
    OrderedList,
    Paragraph,
    Section,
    SoftBreak,
    Strikethrough,
    Table,
    TableCell,
    TableRow,
    Text,
    Underline,
    UnorderedList,
)
from asciidoc.parser import parse
from giruntime.ui.note_render.attachment_table import expand_attachment_tables
from giruntime.ui.note_render.tag_table import (
    TagName,
    admonition_body_tag_name,
    admonition_kind_tag_name,
    admonition_label_tag_name,
    heading_tag_name,
    list_item_tag_name,
)
from config.defaults import TABLE_CELL_HPADDING_PX
from enums import HeadingTrailing, ListNumberStyle
from storage.protocols import (
    AttachmentListResolver,
    ColumnWidthResolver,
    ImageBytesResolver,
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

type CellWidthMeasurer = Callable[[str, bool, bool], int]
"""Measures the rendered pixel width of a table-cell text run.

Called ``measure(text, bold, monospace)`` and returns the width, in
pixels, that ``text`` occupies in the body font with the given width
class — ``bold`` selects the bold weight, ``monospace`` the monospace
family. It is the seam the table renderer uses to fit each cell to its
column: there is no per-tab-stop ellipsization in Pango, so the
renderer must measure cell text itself and truncate over-wide cells (see
:meth:`TextBufferRenderer._emit_table` and :func:`_truncate_cell`).

Production wires this to a :class:`Pango.Layout`-backed measurer built
from the article view's Pango context (see
:func:`ui.note_view.make_cell_width_measurer`). Tests pass a fake (e.g.
a fixed pixels-per-character with monospace identical and bold a hair
wider) so the truncation logic is unit-testable without a real font —
mirroring the ``build_tag_table(char_width_px=9)`` test convention.
"""


@dataclass(frozen=True)
class UrlTarget:
    """What a clicked :class:`~asciidoc.ast.Link` activates: a URL."""

    url: str


@dataclass(frozen=True)
class AttachmentTarget:
    """What a clicked :class:`~asciidoc.ast.AttachmentLink` activates.

    The attachment is named by :attr:`filename` — the same key the
    ``image::`` resolver uses — and is scoped to the note currently
    rendered. Resolving it to an :class:`~models.attachment.Attachment`
    (and reporting a filename that matches none) happens at click time,
    in the widget that knows the current note.
    """

    filename: str


type ActivationTarget = UrlTarget | AttachmentTarget
"""The closed union of things a click in the rendered view can activate.

Both members ride the *same* per-link anonymous tag machinery and the
same shared :data:`TagName.LINK` styling — a save link and a web link
both mean "clickable". The union is what keeps the two apart at the
point of *dispatch*: :meth:`TextBufferRenderer.target_for_tags` returns
it, and its single consumer (``giruntime.ui.link_handler``) matches on
it with :func:`typing.assert_never`, so adding a third activatable thing
is a type error until every consumer handles it.
"""


type PostTitleHook = Callable[[Gtk.TextBuffer], None]
"""Hook called once per render with the buffer positioned after the title.

The buffer's insertion point (``buffer.get_end_iter()``) sits at the
boundary between the rendered document title and the first body block —
immediately after the title's *single* trailing newline, or at
buffer-start when the document has no title. The caller is expected to
**insert text** at that point (typically the dim-grey metadata line,
via :meth:`Gtk.TextBuffer.insert` /
:meth:`Gtk.TextBuffer.insert_with_tags_by_name`). The renderer then
inserts the block-separating newline *after* whatever the hook added,
so the body drops a clear line below the inserted text. Production
wires this in :class:`ui.note_view.NoteView` to insert the note's
metadata line directly below the title; tests pass a collector that
records each call (and may insert a sentinel) to assert hook-firing
semantics. The renderer creates **no** child anchor on this path —
anchors remain only for tables.
"""


# ---------------------------------------------------------------------------
# Layout-level constants
# ---------------------------------------------------------------------------

# Unordered-list bullet glyphs by nesting depth (1-based: index
# ``depth - 1``). Unicode glyphs so they render predictably regardless
# of the user's font. The table is sized to :data:`MAX_LIST_DEPTH` so the
# depth cap and the presentation table cannot drift — a renderer test
# asserts ``len(_UNORDERED_GLYPHS) == MAX_LIST_DEPTH``.
_UNORDERED_GLYPHS: tuple[str, ...] = ("•", "◦", "▪")

# Tab character used to build a list item's ``\t{marker}\t{text}`` line.
# The per-depth list-item tag carries two stops (see
# ``tag_table._make_list_item_tag``): the *leading* tab drives the marker
# to a RIGHT stop at the period column (right-aligning it, so periods line
# up within a list), and the *separating* tab drives the text to a LEFT
# stop at the text column (fixed per depth, so every list at a depth shares
# it). Tabs — not runs of spaces — are what keep the columns aligned across
# markers of different widths.
_LIST_MARKER_TAB: str = "\t"

# Ordered-list numbering style by nesting depth (1-based: index
# ``depth - 1``). Arabic at the top, then lower-alpha, then lower-roman.
# Sized to :data:`MAX_LIST_DEPTH` for the same no-drift reason as
# :data:`_UNORDERED_GLYPHS`.
_ORDERED_STYLES: tuple[ListNumberStyle, ...] = (
    ListNumberStyle.ARABIC,
    ListNumberStyle.LOWER_ALPHA,
    ListNumberStyle.LOWER_ROMAN,
)

# Depth of an outermost list. Only the top-level list appends the
# trailing blank line that separates it from the next block; nested
# sub-lists stay flush under their parent item.
_TOP_LIST_DEPTH: int = 1

# Two newlines = one blank line between blocks. Block emitters that
# need explicit separation insert this at the end.
_BLOCK_SEPARATOR: str = "\n\n"

# Suffix appended to a table cell whose content is truncated to fit its
# column. A single-character ellipsis so the cut reads as "more here"
# without consuming much of the already-tight column width.
_ELLIPSIS: str = "…"

# Prefix and separator for the blockquote attribution string. An en-dash
# matches typographic conventions for citations; using Unicode literals
# keeps the source readable and avoids HTML-escape gymnastics.
_BLOCKQUOTE_ATTRIBUTION_PREFIX: str = "— "
_BLOCKQUOTE_ATTRIBUTION_SEPARATOR: str = ", "

# Intrinsic dimensions for the placeholder paintable shown when an
# image fails to decode. Kept small and visually neutral so the
# document remains readable around the failure.
_PLACEHOLDER_PAINTABLE_WIDTH_PX: int = 48
_PLACEHOLDER_PAINTABLE_HEIGHT_PX: int = 48

# RGBA for the placeholder paintable's fill. Mid-grey at moderate
# alpha so it shows on both light and dark themes without screaming.
_PLACEHOLDER_PAINTABLE_RGBA: tuple[float, float, float, float] = (
    0.6,
    0.6,
    0.6,
    0.5,
)


@dataclass(frozen=True)
class _CellRun:
    """One formatted text run flattened out of a table cell.

    A cell's inline tree (``*bold*``, ``_italic_``, ``\\`mono\\```,
    ``link[…]`` …) is flattened to a sequence of these runs so the
    truncation algorithm can measure and cut on a flat character stream
    while still re-emitting the original formatting. ``text`` is the
    run's literal characters; ``bold`` / ``monospace`` are its *width
    class* (what :data:`CellWidthMeasurer` needs); ``tags`` are the
    :class:`Gtk.TextTag`\\ s to apply when the run is inserted — the
    visual style tags plus, for a link, the per-link anonymous URL tag,
    so a link that survives truncation keeps its click target on the
    surviving characters.
    """

    text: str
    bold: bool
    monospace: bool
    tags: tuple[Gtk.TextTag, ...]


class TextBufferRenderer:
    """Render an AsciiDoc source string into a :class:`Gtk.TextBuffer`.

    Construction-time dependencies (the two resolvers and the tag
    table) are injected. Tests swap in fakes; production wires them up
    in the ``ui/note_view`` module.
    """

    _image_bytes_for: ImageBytesResolver
    _attachments_for: AttachmentListResolver
    _column_width_px: ColumnWidthResolver
    _cell_width_px: CellWidthMeasurer
    _tag_table: Gtk.TextTagTable
    _activation_tags: dict[Gtk.TextTag, ActivationTarget]
    _table_tab_tags: list[Gtk.TextTag]

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        image_bytes_for: ImageBytesResolver,
        attachments_for: AttachmentListResolver,
        column_width_px: ColumnWidthResolver,
        cell_width_px: CellWidthMeasurer,
        tag_table: Gtk.TextTagTable,
    ) -> None:
        self._image_bytes_for = image_bytes_for
        self._attachments_for = attachments_for
        self._column_width_px = column_width_px
        self._cell_width_px = cell_width_px
        self._tag_table = tag_table
        # Anonymous per-activation tags currently registered on
        # ``self._tag_table`` — one per link *and* per attachment link.
        # Cleared at the start of every render so stale tags don't
        # accumulate as the user edits.
        self._activation_tags = {}
        # Anonymous per-table tab tags (each carries one table's
        # ``Pango.TabArray``). Swept at the start of every render the
        # same way the link tags are, so the tag table cannot
        # accumulate stale tab tags across edits.
        self._table_tab_tags = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render_into(
        self,
        source: str,
        buffer: Gtk.TextBuffer,
        *,
        note_id: str,  # pylint: disable=unused-argument
        post_title_hook: PostTitleHook | None = None,
    ) -> None:
        """Parse ``source`` and rebuild ``buffer`` to reflect the AST.

        ``note_id`` is part of the protocol surface so future caching
        and diagnostics can key on it; the current step does not yet
        use it.

        ``post_title_hook`` is an optional callback invoked exactly
        once per render with the ``buffer`` positioned (at its end iter)
        immediately after the rendered title's single trailing newline
        (or at buffer-start when ``document.title is None``). Production
        wires this to insert the note's metadata line directly below the
        title, with the body block then dropping a blank line below the
        inserted text — see the :data:`PostTitleHook` docstring and the
        module docstring for the contract. No child anchor is created on
        this path. The hook is only fired on a successful parse; if
        :func:`parse` raises a :class:`ParseError`, the buffer is left
        untouched and the hook is never invoked.
        """
        document = parse(source)  # may raise ParseError
        # Expand every ``attachments::[]`` macro into an ordinary table
        # *before* the emit walk, so the generated table reaches the
        # reader through the same ``_emit_table`` path a hand-written one
        # does. The transform is pure; the resolver is metadata-only (no
        # BLOB is read to draw the table).
        document = expand_attachment_tables(document, self._attachments_for())
        buffer.set_text("")
        if buffer.get_tag_table() is not self._tag_table:
            # The tag table is part of the buffer's identity in GTK 4.
            # We sanity-check rather than mutate. A mismatch is a wiring
            # bug, not a runtime fault.
            raise ValueError(
                "buffer's tag table is not the renderer's tag table",
            )
        self._clear_activation_tags()
        self._clear_table_tab_tags()
        if document.title is not None:
            # Emit the title with only a SINGLE newline so the metadata
            # line, inserted on the next line below, hugs the title. The
            # remaining newline that completes the inter-block gap is
            # inserted *after* the metadata text (below), so the body
            # drops a clear blank line beneath it. Body-section headings
            # keep the default full block separator.
            self._emit_heading(
                buffer,
                document.title,
                level=0,
                trailing=HeadingTrailing.SINGLE_NEWLINE,
            )
        # The post-title insertion point sits at the boundary between
        # the title's single trailing newline and the block separator
        # that follows it, or at buffer-start when there is no title.
        # The hook inserts its text (the metadata line) at the buffer's
        # end iter; the renderer then completes the inter-block gap
        # *after* it so the first body block starts a clear line below.
        if post_title_hook is not None:
            post_title_hook(buffer)
            # Complete the inter-block gap *after* the hook's insertion
            # so the first body block starts a clear line below the
            # metadata line. This runs whenever a hook is present, titled
            # or not: a titled note's single leading newline (above) puts
            # the metadata on its own line and this separator drops the
            # body a blank line below; a titleless note has the metadata
            # at buffer-start, so this separator is the sole gap and the
            # body again sits a clear line below it.
            buffer.insert(buffer.get_end_iter(), _BLOCK_SEPARATOR)
        for block in document.blocks:
            self._emit_block(buffer, block)
        self._strip_trailing_blank(buffer)

    def target_for_tags(
        self,
        tags: list[Gtk.TextTag],
    ) -> ActivationTarget | None:
        """Return what the first activation tag in ``tags`` activates.

        Used by :mod:`giruntime.ui.link_handler` to recover the click
        target — a :class:`UrlTarget` to launch, or an
        :class:`AttachmentTarget` to save. The argument is the list
        returned by :meth:`Gtk.TextIter.get_tags` for the click position;
        if none of those tags is an activation marker the method returns
        :data:`None` and the caller does nothing (the common case — most
        of a document is not clickable).

        The lookup is per-renderer because the tag→target mapping is
        per-render: a renderer instance is the smallest scope that owns
        one consistent set of activation tags.
        """
        for tag in tags:
            target = self._activation_tags.get(tag)
            if target is not None:
                return target
        return None

    # ------------------------------------------------------------------
    # Activation tag lifecycle
    # ------------------------------------------------------------------

    def _clear_activation_tags(self) -> None:
        """Remove every per-activation anonymous tag from the tag table.

        Called at the start of each :meth:`render_into` so the tag
        table doesn't accumulate stale link tags as the user edits.
        Any range still bearing one of these tags becomes "untagged
        in that respect" — and the buffer is about to be cleared by
        ``set_text("")`` anyway, so there are no application ranges
        to worry about.
        """
        for tag in self._activation_tags:
            self._tag_table.remove(tag)
        self._activation_tags.clear()

    def _make_activation_tag(self, target: ActivationTarget) -> Gtk.TextTag:
        """Build and register an anonymous tag carrying a click target.

        The tag itself has no visual properties — visual styling comes
        from the shared :data:`TagName.LINK` tag, which a web link and a
        save link both wear (both mean "clickable"). This tag's sole
        purpose is to associate a buffer range with its
        :data:`ActivationTarget` via the ``self._activation_tags``
        mapping.
        """
        tag = Gtk.TextTag.new(None)
        self._tag_table.add(tag)
        self._activation_tags[tag] = target
        return tag

    # ------------------------------------------------------------------
    # Table tab-tag lifecycle
    # ------------------------------------------------------------------

    def _clear_table_tab_tags(self) -> None:
        """Remove every per-table tab tag from the tag table.

        Mirrors :meth:`_clear_activation_tags`: a table's column geometry
        varies (column count / proportions / live column width), so its
        :class:`Pango.TabArray` cannot live on a fixed named tag and is
        carried on an anonymous tag minted per render. Sweeping them at
        the start of each render keeps the tag table from accumulating
        stale tab tags as the user edits.
        """
        for tag in self._table_tab_tags:
            self._tag_table.remove(tag)
        self._table_tab_tags.clear()

    def _make_table_tab_tag(self, tabs: Pango.TabArray) -> Gtk.TextTag:
        """Build and register an anonymous tag carrying a table's tab stops.

        The tag carries only the ``tabs`` property (the per-table
        :class:`Pango.TabArray`); the row's other paragraph properties
        (``wrap-mode = NONE``, the wash) live on the shared
        :data:`TagName.TABLE_ROW` / :data:`TagName.TABLE_HEADER` tags the
        renderer layers alongside it. Registered for the per-render
        sweep (:meth:`_clear_table_tab_tags`).
        """
        tag = Gtk.TextTag.new(None)
        tag.set_property("tabs", tabs)
        self._tag_table.add(tag)
        self._table_tab_tags.append(tag)
        return tag

    # ------------------------------------------------------------------
    # Block emission
    # ------------------------------------------------------------------

    def _emit_block(
        self,
        buffer: Gtk.TextBuffer,
        block: BlockNode,
    ) -> None:
        if isinstance(block, Section):
            self._emit_section(buffer, block)
        elif isinstance(block, Paragraph):
            self._emit_paragraph(buffer, block)
        elif isinstance(block, UnorderedList):
            self._emit_unordered_list(buffer, block)
        elif isinstance(block, OrderedList):
            self._emit_ordered_list(buffer, block)
        elif isinstance(block, CodeBlock):
            self._emit_code_block(buffer, block)
        elif isinstance(block, Image):
            self._emit_image(buffer, block)
        elif isinstance(block, Table):
            self._emit_table(buffer, block)
        elif isinstance(block, Admonition):
            self._emit_admonition(buffer, block)
        elif isinstance(block, Blockquote):
            self._emit_blockquote(buffer, block)
        elif isinstance(block, AttachmentTable):
            # Unreachable in a well-formed render: ``render_into``
            # expands every ``attachments::[]`` macro into an ordinary
            # ``Table`` before the walk starts, which is what lets the
            # generated table reuse ``_emit_table`` wholesale. Reaching
            # here means a caller walked an unexpanded document.
            raise TypeError(
                "AttachmentTable must be expanded before rendering",
            )
        else:
            # Exhaustive over the current :data:`BlockNode` union. New
            # block kinds must extend this dispatch — the ``else`` makes
            # forgetting one a hard failure rather than silent omission.
            raise TypeError(f"unknown block node: {type(block).__name__}")

    def _emit_section(
        self,
        buffer: Gtk.TextBuffer,
        section: Section,
    ) -> None:
        # Reclaim the blank line the previous block left: _strip_trailing_
        # blank drops *every* trailing newline (it is also the helper
        # that guarantees no dangling blank line at the very end of the
        # whole render), so a single line-ending newline is put back
        # immediately after it — unless the buffer is still empty, i.e.
        # this heading is the very first content and there is nothing to
        # reclaim. The net effect for non-empty buffers is exactly one
        # separator newline instead of the block separator's two, so the
        # heading's own top gap is its paragraph tag's pixels-above-lines
        # alone, not a blank line plus padding (see _make_heading_tag's
        # 2 : 1 spacing model).
        self._strip_trailing_blank(buffer)
        if buffer.get_end_iter().get_offset() > 0:
            buffer.insert(buffer.get_end_iter(), "\n")
        self._emit_heading(
            buffer,
            section.title,
            level=section.level,
            trailing=HeadingTrailing.SINGLE_NEWLINE,
        )
        for block in section.blocks:
            self._emit_block(buffer, block)

    def _emit_heading(
        self,
        buffer: Gtk.TextBuffer,
        title_inlines: tuple[InlineNode, ...],
        *,
        level: int,
        trailing: HeadingTrailing,
    ) -> None:
        # ``trailing`` controls what follows the heading text. Both
        # callers now pass ``SINGLE_NEWLINE``, but for different reasons:
        # the document title so the metadata line (inserted by
        # ``render_into``'s post-title hook) can sit on the immediately
        # following line, with the renderer completing the block gap
        # after that inserted text; a body section heading so its own
        # bottom gap is the heading paragraph tag's pixels-below-lines
        # alone (see ``_emit_section``, which also strips the preceding
        # blank line so the top gap is pixels-above-lines alone).
        # Explicit at each call site (no default) because a value that
        # is always supplied is not meaningfully optional.
        heading_tag = self._tag(heading_tag_name(level))
        start_offset = buffer.get_end_iter().get_offset()
        for inline in title_inlines:
            self._emit_inline(buffer, inline, [])
        # Apply the heading-level tag across the whole title text. Inline
        # tags from inside the title (bold, italic, …) were applied during
        # _emit_inline; layering the heading tag on top composes correctly.
        end_offset = buffer.get_end_iter().get_offset()
        if end_offset > start_offset:
            buffer.apply_tag(
                heading_tag,
                buffer.get_iter_at_offset(start_offset),
                buffer.get_iter_at_offset(end_offset),
            )
        buffer.insert(buffer.get_end_iter(), trailing.value)

    def _emit_paragraph(
        self,
        buffer: Gtk.TextBuffer,
        paragraph: Paragraph,
    ) -> None:
        for inline in paragraph.inlines:
            self._emit_inline(buffer, inline, [])
        buffer.insert(buffer.get_end_iter(), _BLOCK_SEPARATOR)

    def _emit_unordered_list(
        self,
        buffer: Gtk.TextBuffer,
        ulist: UnorderedList,
        depth: int = 1,
    ) -> None:
        glyph = _UNORDERED_GLYPHS[depth - 1]
        for item in ulist.items:
            self._emit_list_item(buffer, item, glyph, depth)
        if depth == _TOP_LIST_DEPTH:
            buffer.insert(buffer.get_end_iter(), "\n")

    def _emit_ordered_list(
        self,
        buffer: Gtk.TextBuffer,
        olist: OrderedList,
        depth: int = 1,
    ) -> None:
        style = _ORDERED_STYLES[depth - 1]
        for index, item in enumerate(olist.items, start=1):
            ordinal = _format_ordinal(style, index)
            self._emit_list_item(buffer, item, ordinal, depth)
        if depth == _TOP_LIST_DEPTH:
            buffer.insert(buffer.get_end_iter(), "\n")

    def _emit_list_item(
        self,
        buffer: Gtk.TextBuffer,
        item: ListItem,
        marker: str,
        depth: int,
    ) -> None:
        """Emit one item line then recurse into its child sub-lists.

        The line is a leading tab, the marker, a tab, then the item text
        (``\\t{marker}\\t{text}``); the marker is the bare bullet glyph or
        ordinal (no indent or gap spaces). Layout is *tag geometry*, not
        buffer whitespace: the whole item line (both tabs, marker, text, and
        its terminating newline) is tagged with the depth's
        :data:`TagName.LIST_ITEM_1` … :data:`LIST_ITEM_3` tag. The leading
        tab hits that tag's RIGHT stop, right-aligning the marker so periods
        line up within the list; the second tab hits the LEFT stop, placing
        the text on the depth's fixed text column (so every list at the depth
        aligns); the negative ``indent`` hangs wrapped lines under that text
        column (see :func:`tag_table._make_list_item_tag`). Tagging through
        the newline keeps the paragraph geometry applied to the last line.

        Numbering and glyph restart per nested list because each sub-list is
        emitted from its own :meth:`_emit_ordered_list` /
        :meth:`_emit_unordered_list` call. Only the top-level list appends
        the trailing blank line, so nested sub-lists stay flush under their
        parent item.
        """
        start_offset = buffer.get_end_iter().get_offset()
        buffer.insert(
            buffer.get_end_iter(),
            f"{_LIST_MARKER_TAB}{marker}{_LIST_MARKER_TAB}",
        )
        for inline in item.inlines:
            self._emit_inline(buffer, inline, [])
        buffer.insert(buffer.get_end_iter(), "\n")
        end_iter = buffer.get_end_iter()
        buffer.apply_tag(
            self._tag(list_item_tag_name(depth)),
            buffer.get_iter_at_offset(start_offset),
            end_iter,
        )
        for child in item.children:
            if isinstance(child, UnorderedList):
                self._emit_unordered_list(buffer, child, depth + 1)
            else:
                self._emit_ordered_list(buffer, child, depth + 1)

    def _emit_code_block(
        self,
        buffer: Gtk.TextBuffer,
        code_block: CodeBlock,
    ) -> None:
        """Insert a code block as a tinted, monospace paragraph range.

        The content is inserted verbatim. Two tags are layered across
        the same range: :data:`TagName.CODE_BLOCK` for the paragraph
        background tint, side margins, and zero inter-line leading, and
        :data:`TagName.MONOSPACE` for the monospace family. The outer
        ``Gtk.TextView`` already sets ``wrap-mode = WORD_CHAR``, so
        unwrappably-long lines soft-wrap inside the column — no
        horizontal scrollbar, no wrap indicator (deferred). Copy through
        the block yields the original source unchanged.

        Because :data:`TagName.CODE_BLOCK` carries no vertical padding
        of its own (adjacent lines must abut so box-drawing characters
        connect), the block's top and bottom breathing room is applied
        separately: :data:`TagName.CODE_BLOCK_TOP_PAD` is layered across
        the block's first logical line and
        :data:`TagName.CODE_BLOCK_BOTTOM_PAD` across its last — the same
        line for a single-line block, which then carries both.
        """
        if not code_block.content:
            buffer.insert(buffer.get_end_iter(), _BLOCK_SEPARATOR)
            return
        content = code_block.content
        start_offset = buffer.get_end_iter().get_offset()
        buffer.insert(buffer.get_end_iter(), content)
        # Terminate with a newline so the paragraph tag's
        # paragraph-background-rgba paints to the line edge on the last
        # source line of the block.
        buffer.insert(buffer.get_end_iter(), "\n")
        end_offset = buffer.get_end_iter().get_offset()
        start_iter = buffer.get_iter_at_offset(start_offset)
        end_iter = buffer.get_iter_at_offset(end_offset)
        buffer.apply_tag(self._tag(TagName.CODE_BLOCK), start_iter, end_iter)
        buffer.apply_tag(self._tag(TagName.MONOSPACE), start_iter, end_iter)
        # First logical line: from the block's start to (and including)
        # its first internal newline, or to the block's end if the
        # content is a single line.
        first_newline_index = content.find("\n")
        first_line_end_offset = (
            start_offset + first_newline_index + 1
            if first_newline_index != -1
            else end_offset
        )
        # Last logical line: from just after the block's last internal
        # newline to its end, or from the block's start if the content
        # is a single line.
        last_newline_index = content.rfind("\n")
        last_line_start_offset = (
            start_offset + last_newline_index + 1
            if last_newline_index != -1
            else start_offset
        )
        buffer.apply_tag(
            self._tag(TagName.CODE_BLOCK_TOP_PAD),
            start_iter,
            buffer.get_iter_at_offset(first_line_end_offset),
        )
        buffer.apply_tag(
            self._tag(TagName.CODE_BLOCK_BOTTOM_PAD),
            buffer.get_iter_at_offset(last_line_start_offset),
            end_iter,
        )
        buffer.insert(buffer.get_end_iter(), "\n")

    def _emit_image(
        self,
        buffer: Gtk.TextBuffer,
        image: Image,
    ) -> None:
        """Insert an image as an inline paintable.

        Bytes are resolved through the injected
        :data:`ImageBytesResolver`. On a successful decode, the
        texture is wrapped in :class:`_ScaledImagePaintable` so its
        intrinsic width is capped at the live column width — large
        images therefore scale down to fit the article column rather
        than overflowing it. On ``GLib.Error`` from
        :meth:`Gdk.Texture.new_from_bytes` (corrupted bytes, unknown
        format, empty payload, …) the renderer inserts a
        :class:`_PlaceholderImagePaintable` instead so the document
        remains readable. Any other resolver exception propagates —
        a missing attachment is the resolver's contract violation,
        not the renderer's to translate.

        Paintables occupy exactly one buffer offset, so selection
        across an image yields the surrounding text unchanged and the
        image contributes no characters to a ``Ctrl+C`` copy.
        """
        column_width_px = self._column_width_px()
        data = self._image_bytes_for(image.filename)
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except GLib.Error:
            paintable: Gdk.Paintable = _PlaceholderImagePaintable()
        else:
            paintable = _ScaledImagePaintable(
                texture=texture,
                column_width_px=column_width_px,
            )
        buffer.insert_paintable(buffer.get_end_iter(), paintable)
        buffer.insert(buffer.get_end_iter(), _BLOCK_SEPARATOR)

    def _emit_table(
        self,
        buffer: Gtk.TextBuffer,
        table: Table,
    ) -> None:
        """Insert a table as native, selectable buffer text.

        Each row becomes one logical buffer line of ``cell \\t cell \\t …
        cell`` whose columns are aligned by a per-table
        :class:`Pango.TabArray` (pixel tab stops at the cumulative
        column edges). Wrapping is disabled on the row (via the
        :data:`TagName.TABLE_ROW` / :data:`TagName.TABLE_HEADER` tag), so
        each cell is padded symmetrically: the row tag's ``left-margin``
        insets the text by :data:`config.defaults.TABLE_CELL_HPADDING_PX`
        on the left, and each cell is truncated with an ellipsis to its
        column width less ``2 × TABLE_CELL_HPADDING_PX`` — reserving the
        same padding on the right and keeping a fitted cell short of its
        tab stop so it never cascades the rest of the row out of
        alignment (see :func:`_truncate_cell`). The first row is the
        header: its cells render bold and its line carries
        :data:`TagName.TABLE_HEADER` (a tint band); every other row
        carries :data:`TagName.TABLE_ROW` (a bottom hairline rule).

        No child anchor or widget is created — the table is part of the
        selectable / copyable buffer. Copying a truncated cell yields the
        truncated display text, consistent with the rendered buffer
        being a read-only projection of the source.
        """
        column_px = self._column_width_px()
        column_count = len(table.rows[0].cells)
        proportions = (
            table.column_proportions
            if table.column_proportions is not None
            else (1,) * column_count
        )
        column_widths = _table_column_pixels(proportions, column_px)
        tab_tag = self._make_table_tab_tag(_table_tab_stops(column_widths))
        for row_index, row in enumerate(table.rows):
            self._emit_table_row(
                buffer,
                row,
                is_header=row_index == 0,
                column_widths=column_widths,
                tab_tag=tab_tag,
            )
        buffer.insert(buffer.get_end_iter(), "\n")

    def _emit_table_row(  # pylint: disable=too-many-arguments
        self,
        buffer: Gtk.TextBuffer,
        row: TableRow,
        *,
        is_header: bool,
        column_widths: tuple[int, ...],
        tab_tag: Gtk.TextTag,
    ) -> None:
        """Emit one table row as a tab-separated, tagged buffer line.

        Each cell is flattened, truncated to its column width (less
        ``2 × TABLE_CELL_HPADDING_PX``, the reserved right padding), and
        inserted; a ``\\t`` separates columns (emitted even for an empty
        cell so the next cell keeps its tab stop). The whole line then
        carries the row/header paragraph tag (which adds the matching
        left ``left-margin`` text inset) plus the per-table ``tab_tag``.
        """
        row_start = buffer.get_end_iter().get_offset()
        last_col = len(row.cells) - 1
        for col_index, cell in enumerate(row.cells):
            runs = _truncate_cell(
                self._flatten_cell(cell, header_bold=is_header),
                column_widths[col_index],
                2 * TABLE_CELL_HPADDING_PX,
                self._cell_width_px,
            )
            for run in runs:
                self._emit_cell_run(buffer, run)
            if col_index < last_col:
                buffer.insert(buffer.get_end_iter(), "\t")
        buffer.insert(buffer.get_end_iter(), "\n")
        row_tag = self._tag(
            TagName.TABLE_HEADER if is_header else TagName.TABLE_ROW,
        )
        start_iter = buffer.get_iter_at_offset(row_start)
        end_iter = buffer.get_end_iter()
        buffer.apply_tag(row_tag, start_iter, end_iter)
        buffer.apply_tag(tab_tag, start_iter, end_iter)

    def _flatten_cell(
        self,
        cell: TableCell,
        *,
        header_bold: bool,
    ) -> list[_CellRun]:
        """Flatten a cell's inline tree to a list of :class:`_CellRun`.

        Walks the cell's inline nodes, carrying a tag tuple plus the
        ``bold`` / ``monospace`` width-class flags down the tree, so each
        leaf :class:`Text` (or :class:`Monospace`) becomes one run that
        records both the tags to re-apply and the width class to measure.
        ``header_bold`` seeds the walk with the shared
        :data:`TagName.BOLD` tag and the bold width class so a header
        row's cells render and measure as bold.
        """
        base_tags: tuple[Gtk.TextTag, ...] = (
            (self._tag(TagName.BOLD),) if header_bold else ()
        )
        runs: list[_CellRun] = []
        for inline in cell.inlines:
            self._flatten_inline(
                inline,
                tags=base_tags,
                bold=header_bold,
                monospace=False,
                runs=runs,
            )
        return runs

    def _flatten_inline(  # pylint: disable=too-many-return-statements,too-many-arguments
        self,
        inline: InlineNode,
        *,
        tags: tuple[Gtk.TextTag, ...],
        bold: bool,
        monospace: bool,
        runs: list[_CellRun],
    ) -> None:
        """Append the runs for one inline node (recursing into children).

        The dispatch mirrors :meth:`_emit_inline`'s closed union, but
        produces measurable runs instead of inserting directly, because
        truncation must measure a cell *before* any text reaches the
        buffer.
        """
        if isinstance(inline, Text):
            if inline.content:
                runs.append(
                    _CellRun(
                        text=inline.content,
                        bold=bold,
                        monospace=monospace,
                        tags=tags,
                    )
                )
            return
        if isinstance(inline, Monospace):
            # Monospace carries a literal str (no nested inlines) — it is
            # a leaf with the monospace width class and the MONOSPACE tag.
            if inline.content:
                runs.append(
                    _CellRun(
                        text=inline.content,
                        bold=bold,
                        monospace=True,
                        tags=(*tags, self._tag(TagName.MONOSPACE)),
                    )
                )
            return
        if isinstance(inline, Bold):
            self._flatten_children(
                inline.children,
                tags=tags,
                added=TagName.BOLD,
                bold=True,
                monospace=monospace,
                runs=runs,
            )
            return
        if isinstance(inline, Italic):
            self._flatten_children(
                inline.children,
                tags=tags,
                added=TagName.ITALIC,
                bold=bold,
                monospace=monospace,
                runs=runs,
            )
            return
        if isinstance(inline, Strikethrough):
            self._flatten_children(
                inline.children,
                tags=tags,
                added=TagName.STRIKETHROUGH,
                bold=bold,
                monospace=monospace,
                runs=runs,
            )
            return
        if isinstance(inline, Underline):
            self._flatten_children(
                inline.children,
                tags=tags,
                added=TagName.UNDERLINE,
                bold=bold,
                monospace=monospace,
                runs=runs,
            )
            return
        if isinstance(inline, (Link, AttachmentLink)):
            # An activatable node's identity rides a fresh anonymous tag
            # (as in ``_emit_activatable``); a
            # truncated link keeps its target on the surviving characters
            # because the tag is on every run. Both kinds appear in cells:
            # the generated attachments table's name column *is* an
            # ``AttachmentLink``.
            link_tags = (
                *tags,
                self._tag(TagName.LINK),
                self._make_activation_tag(_target_of(inline)),
            )
            for child in inline.text:
                self._flatten_inline(
                    child,
                    tags=link_tags,
                    bold=bold,
                    monospace=monospace,
                    runs=runs,
                )
            return
        if isinstance(inline, SoftBreak):
            # A soft break cannot occur in a single-line cell, but the
            # union must stay exhaustive — render it as a space, matching
            # ``_emit_inline``.
            runs.append(
                _CellRun(text=" ", bold=bold, monospace=monospace, tags=tags),
            )
            return
        if isinstance(inline, HardBreak):
            # Like SoftBreak, a hard break cannot occur in a single-line
            # cell (cells never join source lines), but the union must stay
            # exhaustive. Collapse it to a space so a stray marker can never
            # smuggle a newline into a table cell.
            runs.append(
                _CellRun(text=" ", bold=bold, monospace=monospace, tags=tags),
            )
            return
        raise TypeError(f"unknown inline node: {type(inline).__name__}")

    def _flatten_children(  # pylint: disable=too-many-arguments
        self,
        children: tuple[InlineNode, ...],
        *,
        tags: tuple[Gtk.TextTag, ...],
        added: TagName,
        bold: bool,
        monospace: bool,
        runs: list[_CellRun],
    ) -> None:
        """Recurse into a styled span's children with the added tag.

        Shared by the bold / italic / strikethrough / underline arms of
        :meth:`_flatten_inline`: each pushes its style tag onto ``tags``
        and recurses; ``bold`` is threaded through (the bold arm passes
        ``True``) so the width class tracks the actual weight.
        """
        new_tags = (*tags, self._tag(added))
        for child in children:
            self._flatten_inline(
                child,
                tags=new_tags,
                bold=bold,
                monospace=monospace,
                runs=runs,
            )

    def _emit_cell_run(self, buffer: Gtk.TextBuffer, run: _CellRun) -> None:
        """Insert one :class:`_CellRun`'s text and apply its tags."""
        if not run.text:
            return
        start_offset = buffer.get_end_iter().get_offset()
        buffer.insert(buffer.get_end_iter(), run.text)
        end_offset = buffer.get_end_iter().get_offset()
        start_iter = buffer.get_iter_at_offset(start_offset)
        end_iter = buffer.get_iter_at_offset(end_offset)
        for tag in run.tags:
            buffer.apply_tag(tag, start_iter, end_iter)

    def _emit_admonition(
        self,
        buffer: Gtk.TextBuffer,
        admonition: Admonition,
    ) -> None:
        """Insert an admonition as a tinted label-plus-body paragraph block.

        Two paragraph tags carry the per-kind tint:
        :func:`admonition_label_tag_name` for the kind-label line and
        :func:`admonition_body_tag_name` for each body paragraph. The
        kind-label character tag (:func:`admonition_kind_tag_name`)
        adds bold weight plus the accent foreground to the label
        text itself. Inline formatting inside body paragraphs flows
        through the existing :meth:`_emit_inline`, so bold / italic /
        monospace / link spans compose normally.

        An empty body (zero paragraphs) is permitted by the parser —
        only the label paragraph is emitted in that case.
        """
        kind = admonition.kind
        label_paragraph_tag = self._tag(admonition_label_tag_name(kind))
        body_paragraph_tag = self._tag(admonition_body_tag_name(kind))
        kind_character_tag = self._tag(admonition_kind_tag_name(kind))

        # Label paragraph: insert the kind text, then a newline so the
        # paragraph tag's background paints to the line edge.
        label_start = buffer.get_end_iter().get_offset()
        buffer.insert(buffer.get_end_iter(), kind.value)
        label_text_end = buffer.get_end_iter().get_offset()
        buffer.insert(buffer.get_end_iter(), "\n")
        label_end = buffer.get_end_iter().get_offset()
        buffer.apply_tag(
            label_paragraph_tag,
            buffer.get_iter_at_offset(label_start),
            buffer.get_iter_at_offset(label_end),
        )
        # Character tag covers just the label text (no newline) so the
        # bold + accent styling doesn't bleed across paragraph breaks.
        buffer.apply_tag(
            kind_character_tag,
            buffer.get_iter_at_offset(label_start),
            buffer.get_iter_at_offset(label_text_end),
        )

        for paragraph in admonition.blocks:
            body_start = buffer.get_end_iter().get_offset()
            for inline in paragraph.inlines:
                self._emit_inline(buffer, inline, [])
            buffer.insert(buffer.get_end_iter(), "\n")
            body_end = buffer.get_end_iter().get_offset()
            buffer.apply_tag(
                body_paragraph_tag,
                buffer.get_iter_at_offset(body_start),
                buffer.get_iter_at_offset(body_end),
            )

        # Single trailing newline = the inter-block gap. The last
        # paragraph already contributed its own terminating newline.
        buffer.insert(buffer.get_end_iter(), "\n")

    def _emit_blockquote(
        self,
        buffer: Gtk.TextBuffer,
        blockquote: Blockquote,
    ) -> None:
        """Insert a blockquote as italic body paragraphs plus optional attribution.

        The :data:`TagName.BLOCKQUOTE_BODY` paragraph tag carries the
        neutral tint and the indent. Italic style composes via the
        shared :data:`TagName.ITALIC` tag, applied across each body
        paragraph after the paragraph tag — keeping italic in one
        place. Attribution, if present, is a follow-on paragraph
        carrying :data:`TagName.BLOCKQUOTE_ATTRIBUTION` (smaller scale,
        right-aligned).

        An empty body is permitted by the parser; in that case only
        the (optional) attribution line is emitted.
        """
        body_paragraph_tag = self._tag(TagName.BLOCKQUOTE_BODY)
        italic_tag = self._tag(TagName.ITALIC)

        for paragraph in blockquote.blocks:
            body_start = buffer.get_end_iter().get_offset()
            for inline in paragraph.inlines:
                self._emit_inline(buffer, inline, [])
            buffer.insert(buffer.get_end_iter(), "\n")
            body_end = buffer.get_end_iter().get_offset()
            start_iter = buffer.get_iter_at_offset(body_start)
            end_iter = buffer.get_iter_at_offset(body_end)
            buffer.apply_tag(body_paragraph_tag, start_iter, end_iter)
            buffer.apply_tag(italic_tag, start_iter, end_iter)

        attribution_text = _build_attribution_text(
            blockquote.author,
            blockquote.source,
        )
        if attribution_text is not None:
            attribution_paragraph_tag = self._tag(TagName.BLOCKQUOTE_ATTRIBUTION)
            attribution_start = buffer.get_end_iter().get_offset()
            buffer.insert(buffer.get_end_iter(), attribution_text)
            buffer.insert(buffer.get_end_iter(), "\n")
            attribution_end = buffer.get_end_iter().get_offset()
            buffer.apply_tag(
                attribution_paragraph_tag,
                buffer.get_iter_at_offset(attribution_start),
                buffer.get_iter_at_offset(attribution_end),
            )

        buffer.insert(buffer.get_end_iter(), "\n")

    # ------------------------------------------------------------------
    # Inline emission
    # ------------------------------------------------------------------

    def _emit_inline(  # pylint: disable=too-many-return-statements
        self,
        buffer: Gtk.TextBuffer,
        inline: InlineNode,
        tag_stack: list[Gtk.TextTag],
    ) -> None:
        # One return per inline AST kind. The closed-union dispatch
        # is intentionally kept as an :func:`isinstance` cascade
        # (rather than a class-keyed dispatch table) so adding a
        # new inline node forces a static-typing visit here AND
        # the final ``raise`` flags the omission at runtime.
        if isinstance(inline, Text):
            self._emit_text(buffer, inline, tag_stack)
            return
        if isinstance(inline, Bold):
            self._emit_styled(buffer, inline.children, tag_stack, TagName.BOLD)
            return
        if isinstance(inline, Italic):
            self._emit_styled(buffer, inline.children, tag_stack, TagName.ITALIC)
            return
        if isinstance(inline, Strikethrough):
            self._emit_styled(
                buffer,
                inline.children,
                tag_stack,
                TagName.STRIKETHROUGH,
            )
            return
        if isinstance(inline, Underline):
            self._emit_styled(buffer, inline.children, tag_stack, TagName.UNDERLINE)
            return
        if isinstance(inline, Monospace):
            self._emit_monospace(buffer, inline, tag_stack)
            return
        if isinstance(inline, (Link, AttachmentLink)):
            self._emit_activatable(buffer, inline, tag_stack)
            return
        if isinstance(inline, SoftBreak):
            # A source-line boundary inside a paragraph: a soft break is a
            # reflow point — render it as a single space. The joiner is
            # always a top-level child of Paragraph.inlines, so tag_stack
            # is empty here and a space carries no visible style anyway.
            buffer.insert(buffer.get_end_iter(), " ")
            return
        if isinstance(inline, HardBreak):
            # The ` +` hard-break sibling of SoftBreak: force a visible
            # line break instead of reflowing. Like SoftBreak it is always
            # a top-level child of Paragraph.inlines, so tag_stack is empty
            # and the newline carries no inherited style.
            buffer.insert(buffer.get_end_iter(), "\n")
            return
        # Closed union; new inline kinds must extend this dispatch.
        raise TypeError(f"unknown inline node: {type(inline).__name__}")

    def _emit_monospace(
        self,
        buffer: Gtk.TextBuffer,
        monospace: Monospace,
        tag_stack: list[Gtk.TextTag],
    ) -> None:
        """Insert verbatim monospace content with the MONOSPACE tag added.

        :class:`Monospace`'s ``content`` is a literal :class:`str` —
        no nested inline parsing happens here, by design. This is
        what makes it safe to wrap a snippet that contains ``*`` or
        ``_`` in backticks.
        """
        if not monospace.content:
            return
        start_offset = buffer.get_end_iter().get_offset()
        buffer.insert(buffer.get_end_iter(), monospace.content)
        end_offset = buffer.get_end_iter().get_offset()
        start_iter = buffer.get_iter_at_offset(start_offset)
        end_iter = buffer.get_iter_at_offset(end_offset)
        for tag in tag_stack:
            buffer.apply_tag(tag, start_iter, end_iter)
        buffer.apply_tag(self._tag(TagName.MONOSPACE), start_iter, end_iter)

    def _emit_activatable(
        self,
        buffer: Gtk.TextBuffer,
        link: Link | AttachmentLink,
        tag_stack: list[Gtk.TextTag],
    ) -> None:
        """Emit a link's or save-link's display children, tagged.

        Two tags are stacked for the duration of the body: the shared
        :data:`TagName.LINK` tag (visual styling — **no new tag** for
        attachment links, because a save link and a web link both mean
        "clickable") and a fresh anonymous tag carrying the
        :data:`ActivationTarget` (consumed by :meth:`target_for_tags` for
        click handling). The display text is iterated through
        :meth:`_emit_inline` so any nested formatting composes correctly.
        """
        link_tag = self._tag(TagName.LINK)
        target_tag = self._make_activation_tag(_target_of(link))
        new_stack = [*tag_stack, link_tag, target_tag]
        for child in link.text:
            self._emit_inline(buffer, child, new_stack)

    def _emit_text(
        self,
        buffer: Gtk.TextBuffer,
        text: Text,
        tag_stack: list[Gtk.TextTag],
    ) -> None:
        if not text.content:
            return
        start_offset = buffer.get_end_iter().get_offset()
        buffer.insert(buffer.get_end_iter(), text.content)
        if not tag_stack:
            return
        end_offset = buffer.get_end_iter().get_offset()
        start_iter = buffer.get_iter_at_offset(start_offset)
        end_iter = buffer.get_iter_at_offset(end_offset)
        for tag in tag_stack:
            buffer.apply_tag(tag, start_iter, end_iter)

    def _emit_styled(
        self,
        buffer: Gtk.TextBuffer,
        children: tuple[InlineNode, ...],
        tag_stack: list[Gtk.TextTag],
        added_tag_name: TagName,
    ) -> None:
        new_stack = [*tag_stack, self._tag(added_tag_name)]
        for child in children:
            self._emit_inline(buffer, child, new_stack)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _tag(self, name: TagName) -> Gtk.TextTag:
        tag = self._tag_table.lookup(name.value)
        if tag is None:
            # Tag-table mismatch — the renderer was constructed with a
            # tag table that does not contain a tag the AST requires.
            raise LookupError(f"tag {name.value!r} missing from tag table")
        return tag

    @staticmethod
    def _strip_trailing_blank(buffer: Gtk.TextBuffer) -> None:
        """Drop any trailing newlines so the buffer doesn't end in blank
        lines.

        Each block emitter terminates with one or more newlines; the
        last block therefore leaves a redundant blank line at the very
        end. Strip until at most one terminating newline remains — but
        only if the buffer has any content at all.
        """
        end = buffer.get_end_iter()
        while end.get_offset() > 0:
            prev = buffer.get_iter_at_offset(end.get_offset() - 1)
            if buffer.get_text(prev, end, True) != "\n":
                break
            end = prev
        # Keep the terminating newline if there's content. Avoids the
        # "cursor sits past the last visible char" oddity at the buffer
        # end while still avoiding a visible blank line.
        if end.get_offset() == buffer.get_end_iter().get_offset():
            return
        buffer.delete(end, buffer.get_end_iter())


def _target_of(node: Link | AttachmentLink) -> ActivationTarget:
    """Return the :data:`ActivationTarget` an activatable node carries.

    The single place the AST's two activatable nodes map onto the
    renderer's activation union, shared by the paragraph path
    (:meth:`TextBufferRenderer._emit_activatable`) and the table-cell
    path (:meth:`TextBufferRenderer._flatten_inline`).
    """
    if isinstance(node, Link):
        return UrlTarget(url=node.url)
    return AttachmentTarget(filename=node.filename)


# ---------------------------------------------------------------------------
# Image paintables
# ---------------------------------------------------------------------------


class _ScaledImagePaintable(GObject.GObject, Gdk.Paintable):
    """A :class:`Gdk.Paintable` wrapper that scales an image to fit a column.

    Wraps a :class:`Gdk.Texture` and reports its intrinsic width as
    ``min(texture_width, column_width_px)`` with height scaled
    proportionally. The actual drawing delegates to the wrapped
    texture — :class:`Gtk.TextView` re-scales the snapshot to the
    intrinsic dimensions reported here, so a 2000-pixel-wide image
    paints into a 700-pixel-wide column without overflowing.

    Intrinsic dimensions are computed once at construction. The
    paintable is :data:`Gdk.PaintableFlags.STATIC_CONTENTS` and
    :data:`Gdk.PaintableFlags.STATIC_SIZE` since both the texture
    and the column width are captured at construction time.
    """

    _texture: Gdk.Texture
    _intrinsic_width: int
    _intrinsic_height: int

    def __init__(
        self,
        *,
        texture: Gdk.Texture,
        column_width_px: int,
    ) -> None:
        super().__init__()
        self._texture = texture
        tex_width = texture.get_width()
        tex_height = texture.get_height()
        if column_width_px <= 0 or tex_width <= column_width_px:
            self._intrinsic_width = tex_width
            self._intrinsic_height = tex_height
        else:
            ratio = column_width_px / tex_width
            self._intrinsic_width = column_width_px
            # ``max(1, …)`` so a sub-pixel scaled height never collapses
            # to zero pixels for very small textures.
            self._intrinsic_height = max(1, int(tex_height * ratio))

    def do_get_intrinsic_width(self) -> int:
        return self._intrinsic_width

    def do_get_intrinsic_height(self) -> int:
        return self._intrinsic_height

    def do_get_intrinsic_aspect_ratio(self) -> float:
        if self._intrinsic_height <= 0:
            return 0.0
        return self._intrinsic_width / self._intrinsic_height

    def do_get_flags(self) -> Gdk.PaintableFlags:
        return Gdk.PaintableFlags.CONTENTS | Gdk.PaintableFlags.SIZE

    def do_snapshot(
        self,
        snapshot: Gtk.Snapshot,
        width: float,
        height: float,
    ) -> None:
        self._texture.snapshot(snapshot, width, height)


class _PlaceholderImagePaintable(GObject.GObject, Gdk.Paintable):
    """Constant placeholder :class:`Gdk.Paintable` inserted on decode failure.

    A small grey rectangle: visible enough to flag a missing image,
    quiet enough that surrounding text stays the focus. The constant
    dimensions and fill colour are module-level so a future tweak is
    one edit.
    """

    def do_get_intrinsic_width(self) -> int:
        return _PLACEHOLDER_PAINTABLE_WIDTH_PX

    def do_get_intrinsic_height(self) -> int:
        return _PLACEHOLDER_PAINTABLE_HEIGHT_PX

    def do_get_intrinsic_aspect_ratio(self) -> float:
        return (
            _PLACEHOLDER_PAINTABLE_WIDTH_PX / _PLACEHOLDER_PAINTABLE_HEIGHT_PX
        )

    def do_get_flags(self) -> Gdk.PaintableFlags:
        return Gdk.PaintableFlags.CONTENTS | Gdk.PaintableFlags.SIZE

    def do_snapshot(
        self,
        snapshot: Gtk.Snapshot,
        width: float,
        height: float,
    ) -> None:
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = _PLACEHOLDER_PAINTABLE_RGBA
        rect = Graphene.Rect.alloc()
        rect.init(0, 0, width, height)
        snapshot.append_color(rgba, rect)


# ---------------------------------------------------------------------------
# Table layout helpers (pure — unit-testable without a font or a buffer)
# ---------------------------------------------------------------------------


def _table_column_pixels(
    proportions: tuple[int, ...],
    column_px: int,
) -> tuple[int, ...]:
    """Return each column's pixel width from its proportion.

    Columns are sized by ``column_px * p_i / sum(proportions)``. Widths
    are derived from *cumulative* rounded edges (rather than rounding
    each column independently) so they sum to ``column_px`` and the tab
    stops computed from them in :func:`_table_tab_stops` land on the
    same edges. A non-positive ``column_px`` (before the article
    container has been allocated) yields all-zero widths; the next
    render after allocation produces real widths.
    """
    total = sum(proportions)
    if not proportions or total <= 0 or column_px <= 0:
        return tuple(0 for _ in proportions)
    widths: list[int] = []
    previous_edge = 0
    cumulative_proportion = 0
    for proportion in proportions:
        cumulative_proportion += proportion
        edge = round(column_px * cumulative_proportion / total)
        widths.append(max(0, edge - previous_edge))
        previous_edge = edge
    return tuple(widths)


def _table_tab_stops(column_widths: tuple[int, ...]) -> Pango.TabArray:
    """Build the pixel-positioned :class:`Pango.TabArray` for one table.

    A tab stop is placed at the cumulative left edge of every column
    *after* the first — i.e. ``len(column_widths) - 1`` stops, since the
    last column needs no trailing tab. Positions are in pixels (the
    array is created with ``positions_in_pixels = True``). A single-column
    table yields an empty array (no separators are emitted for it).
    """
    stop_count = max(0, len(column_widths) - 1)
    tabs = Pango.TabArray.new(stop_count, True)
    edge = 0
    for index in range(stop_count):
        edge += column_widths[index]
        tabs.set_tab(index, Pango.TabAlign.LEFT, edge)
    return tabs


def _measure_runs(
    runs: list[_CellRun],
    measure: CellWidthMeasurer,
) -> int:
    """Sum the measured pixel width of every run in ``runs``."""
    return sum(measure(run.text, run.bold, run.monospace) for run in runs)


def _prefix_runs(runs: list[_CellRun], char_count: int) -> list[_CellRun]:
    """Return the runs covering the first ``char_count`` characters.

    Whole runs are kept until the budget is exhausted; the run that
    crosses the boundary is cut (preserving its formatting and tags) so
    a link or a bold span that is truncated mid-run keeps its style on
    the surviving characters.
    """
    prefix: list[_CellRun] = []
    remaining = char_count
    for run in runs:
        if remaining <= 0:
            break
        if len(run.text) <= remaining:
            prefix.append(run)
            remaining -= len(run.text)
        else:
            prefix.append(replace(run, text=run.text[:remaining]))
            remaining = 0
    return prefix


def _truncate_cell(
    runs: list[_CellRun],
    column_px: int,
    gutter_px: int,
    measure: CellWidthMeasurer,
) -> list[_CellRun]:
    """Truncate ``runs`` to fit ``column_px - gutter_px``, with an ellipsis.

    Returns ``runs`` unchanged when the cell already fits the budget.
    Otherwise binary-searches the largest character prefix whose width
    plus the ellipsis width fits, cuts the runs to that prefix (keeping
    per-run formatting up to the cut) and appends a plain ellipsis run.
    The gutter keeps a fitted cell short of its tab stop so it never
    cascades the rest of the row out of alignment.
    """
    budget = column_px - gutter_px
    if _measure_runs(runs, measure) <= budget:
        return runs
    ellipsis_width = measure(_ELLIPSIS, False, False)
    total_chars = sum(len(run.text) for run in runs)
    low, high = 0, total_chars
    best = 0
    while low <= high:
        mid = (low + high) // 2
        prefix = _prefix_runs(runs, mid)
        if _measure_runs(prefix, measure) + ellipsis_width <= budget:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    truncated = _prefix_runs(runs, best)
    truncated.append(
        _CellRun(text=_ELLIPSIS, bold=False, monospace=False, tags=()),
    )
    return truncated


# ---------------------------------------------------------------------------
# Blockquote attribution helper
# ---------------------------------------------------------------------------


def _build_attribution_text(
    author: str | None,
    source: str | None,
) -> str | None:
    """Build the attribution line shown below a blockquote body.

    Returns :data:`None` when both fields are :data:`None` (a bare
    ``[quote]`` or no directive at all — no attribution shown).
    Returns ``"— Author"`` when only the author is set, or
    ``"— Author, Source"`` when both are. The leading en-dash and
    comma separator are module-level constants
    (:data:`_BLOCKQUOTE_ATTRIBUTION_PREFIX`,
    :data:`_BLOCKQUOTE_ATTRIBUTION_SEPARATOR`).
    """
    if author is None and source is None:
        return None
    parts: list[str] = []
    if author is not None:
        parts.append(author)
    if source is not None:
        parts.append(source)
    return (
        _BLOCKQUOTE_ATTRIBUTION_PREFIX
        + _BLOCKQUOTE_ATTRIBUTION_SEPARATOR.join(parts)
    )


# ---------------------------------------------------------------------------
# Ordered-list ordinal formatting
# ---------------------------------------------------------------------------

# Largest-to-smallest (value, lowercase-numeral) pairs for the standard
# additive/subtractive roman conversion used by lower-roman ordinals.
_ROMAN_NUMERALS: tuple[tuple[int, str], ...] = (
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
    (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
    (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
)

# Size of the alphabet used by the bijective base-26 lower-alpha scheme.
_ALPHABET_SIZE: int = 26


def _format_ordinal(style: ListNumberStyle, index: int) -> str:
    """Render a 1-based ordered-list item ``index`` as ``"<ordinal>."``.

    ``match``-es exhaustively on ``style`` so a new
    :class:`ListNumberStyle` member is a compile-time obligation here:
    arabic yields ``"1."``, lower-alpha ``"a."`` (bijective base-26 past
    ``z``: ``…z, aa, ab…``), lower-roman ``"i."`` (standard roman). The
    alpha/roman schemes never run out of representations, so an
    out-of-range index degrades gracefully rather than crashing — though
    at the capped depth ≤ 3 such indices are vanishingly unlikely.
    """
    match style:
        case ListNumberStyle.ARABIC:
            return f"{index}."
        case ListNumberStyle.LOWER_ALPHA:
            return f"{_to_lower_alpha(index)}."
        case ListNumberStyle.LOWER_ROMAN:
            return f"{_to_lower_roman(index)}."


def _to_lower_alpha(index: int) -> str:
    """Bijective base-26: ``1 -> a``, ``26 -> z``, ``27 -> aa``, …."""
    letters: list[str] = []
    remaining = index
    while remaining > 0:
        remaining, offset = divmod(remaining - 1, _ALPHABET_SIZE)
        letters.append(chr(ord("a") + offset))
    return "".join(reversed(letters))


def _to_lower_roman(index: int) -> str:
    """Standard lowercase roman numeral: ``1 -> i``, ``4 -> iv``, …."""
    numeral: list[str] = []
    remaining = index
    for value, symbol in _ROMAN_NUMERALS:
        count, remaining = divmod(remaining, value)
        numeral.append(symbol * count)
    return "".join(numeral)
