"""Walks an AsciiDoc :class:`Document` and populates a ``Gtk.TextBuffer``.

Principles & invariants
-----------------------
* The renderer is the **only** module in :mod:`notes_app.asciidoc` that
  imports ``gi`` at runtime alongside :mod:`tag_table`. Everything from
  ``ast`` through ``parser`` is pure. The renderer never imports from
  :mod:`notes_app.storage` — image bytes flow exclusively through the
  injected :data:`ImageBytesResolver` callable.
* :meth:`TextBufferRenderer.render_into` is the single public entry
  point. It clears the buffer and rebuilds it from scratch on every
  call. There is no incremental-update path: the source change → AST →
  buffer pipeline is short enough that "rebuild" is the cheapest
  consistent strategy.
* Block embeds (images, code blocks) are inserted via
  :class:`Gtk.TextChildAnchor`. The renderer creates the anchor on the
  buffer and then constructs the corresponding child widget. Because a
  child widget is only visible once it has been *attached* to the
  parent :class:`Gtk.TextView` via
  :meth:`Gtk.TextView.add_child_at_anchor`, the renderer accepts an
  ``attach_widget`` callback as the testability seam: production wires
  it to ``text_view.add_child_at_anchor``, tests pass a list-collector.
* Image bytes are resolved through :data:`ImageBytesResolver`. Decode
  failures (``GLib.Error`` from :meth:`Gdk.Texture.new_from_bytes`)
  produce a tiny placeholder widget rather than aborting the whole
  render. Any other resolver exception propagates — a missing
  attachment is the resolver's contract violation, not the renderer's
  to translate.
* The :data:`ColumnWidthResolver` is read once per render (callers can
  call :meth:`render_into` again after a column-width change). Step 14
  introduces the first block that actually consults it: tables, whose
  cell ``Gtk.Label``s use ``max-width-chars`` derived from the live
  column width and the ``[cols="…"]`` proportions so the table fits
  the article column without an internal scrollbar.
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
* Links carry their *URL identity* on a per-link **anonymous** tag
  (a :class:`Gtk.TextTag` constructed with ``name=None``) that is
  added to the tag table at render time. The renderer keeps a
  ``dict[Gtk.TextTag, str]`` mapping each anonymous tag to its URL,
  exposed via :meth:`url_for_tags`. The shared
  :data:`TagName.LINK` tag from :mod:`tag_table` provides the visual
  styling — colour and underline — that every link shares. Anonymous
  tags from a *previous* render are removed from the tag table at the
  start of every :meth:`render_into` call so the table cannot
  accumulate stale link tags as the user edits a note.
* Monospace inline content is emitted as plain text with the
  :data:`TagName.MONOSPACE` tag added to whatever tag stack is
  currently active. Because :class:`Monospace` carries its content as
  a single literal :class:`str` (not a list of further inline nodes),
  it never recurses through :meth:`_emit_inline`.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from notes_app.asciidoc.ast import (
    Bold,
    BlockNode,
    CodeBlock,
    Image,
    InlineNode,
    Italic,
    Link,
    Monospace,
    OrderedList,
    Paragraph,
    Section,
    Strikethrough,
    Table,
    TableCell,
    Text,
    Underline,
    UnorderedList,
)
from notes_app.asciidoc.parser import parse
from notes_app.asciidoc.tag_table import TagName, heading_tag_name
from notes_app.config.defaults import TARGET_CHARS_PER_LINE
from notes_app.storage.protocols import ColumnWidthResolver, ImageBytesResolver


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

type WidgetAttacher = Callable[[Gtk.TextChildAnchor, Gtk.Widget], None]
"""Hook used to bind a widget to its child anchor.

Production wires this to :meth:`Gtk.TextView.add_child_at_anchor` of the
note-view widget. Tests pass a list-collector that records every
``(anchor, widget)`` pair so assertions can introspect what would have
been displayed without a live :class:`Gtk.TextView`.
"""


# ---------------------------------------------------------------------------
# Layout-level constants
# ---------------------------------------------------------------------------

# Bullet glyph used for unordered list items. Unicode bullet so it
# renders predictably regardless of the user's font.
_UNORDERED_BULLET: str = "•  "

# Spaces of indentation that prefix every list item. Plain ASCII space
# rather than the ``left_margin`` tag property, because tag-driven
# margins do not affect the buffer's character offsets and tests check
# the exact buffer text.
_LIST_ITEM_INDENT: str = "    "

# Two newlines = one blank line between blocks. Block emitters that
# need explicit separation insert this at the end.
_BLOCK_SEPARATOR: str = "\n\n"

# Spacing and padding inside a rendered table. Pixel values, applied
# directly to the :class:`Gtk.Grid`'s row/column spacing and to each
# cell label's margins. Kept as module constants so a tweak to "how
# spacious do tables look" is one edit, and tests can introspect the
# exact values when asserting layout behaviour.
_TABLE_COLUMN_SPACING: int = 12
_TABLE_ROW_SPACING: int = 4
_TABLE_CELL_PADDING: int = 4


class TextBufferRenderer:
    """Render an AsciiDoc source string into a :class:`Gtk.TextBuffer`.

    Construction-time dependencies (the two resolvers and the tag
    table) are injected. Tests swap in fakes; production wires them up
    in the ``ui/note_view`` module from build step 8 onward.
    """

    _image_bytes_for: ImageBytesResolver
    _column_width_px: ColumnWidthResolver
    _tag_table: Gtk.TextTagTable
    _link_url_tags: dict[Gtk.TextTag, str]

    def __init__(
        self,
        *,
        image_bytes_for: ImageBytesResolver,
        column_width_px: ColumnWidthResolver,
        tag_table: Gtk.TextTagTable,
    ) -> None:
        self._image_bytes_for = image_bytes_for
        self._column_width_px = column_width_px
        self._tag_table = tag_table
        # Anonymous per-link tags currently registered on
        # ``self._tag_table``. Cleared at the start of every render
        # so stale link tags don't accumulate as the user edits.
        self._link_url_tags = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render_into(
        self,
        source: str,
        buffer: Gtk.TextBuffer,
        *,
        note_id: str,  # pylint: disable=unused-argument
        attach_widget: WidgetAttacher | None = None,
    ) -> None:
        """Parse ``source`` and rebuild ``buffer`` to reflect the AST.

        ``note_id`` is part of the protocol surface so future caching
        and diagnostics can key on it; step 6 does not yet use it.

        ``attach_widget`` is an optional hook called once per child
        anchor. When ``None``, anchors are still created but the
        produced widgets are dropped on the floor — useful for
        text/tag-only assertions that do not care about the embedded
        widgets.
        """
        document = parse(source)  # may raise ParseError
        attacher = attach_widget if attach_widget is not None else _noop_attacher
        buffer.set_text("")
        if buffer.get_tag_table() is not self._tag_table:
            # The tag table is part of the buffer's identity in GTK 4.
            # Step 6 leaves it the caller's job to build the buffer
            # with the correct table; we sanity-check rather than
            # mutate. A mismatch is a wiring bug, not a runtime fault.
            raise ValueError(
                "buffer's tag table is not the renderer's tag table",
            )
        self._clear_link_url_tags()
        if document.title is not None:
            self._emit_heading(buffer, document.title, level=0)
        for block in document.blocks:
            self._emit_block(buffer, block, attacher)
        self._strip_trailing_blank(buffer)

    def url_for_tags(self, tags: list[Gtk.TextTag]) -> str | None:
        """Return the URL associated with the first link tag in ``tags``.

        Used by :mod:`notes_app.ui.link_handler` to recover the URL
        to launch when the user clicks somewhere inside a link
        decoration. The argument is the list returned by
        :meth:`Gtk.TextIter.get_tags` for the click position; if none
        of those tags are link-URL markers, the method returns
        :data:`None` and the caller does nothing.

        The lookup is per-renderer because the URL→tag mapping is
        per-render: a renderer instance is the smallest scope that
        owns one consistent set of link tags.
        """
        for tag in tags:
            url = self._link_url_tags.get(tag)
            if url is not None:
                return url
        return None

    # ------------------------------------------------------------------
    # Link tag lifecycle
    # ------------------------------------------------------------------

    def _clear_link_url_tags(self) -> None:
        """Remove every per-link anonymous tag from the tag table.

        Called at the start of each :meth:`render_into` so the tag
        table doesn't accumulate stale link tags as the user edits.
        Any range still bearing one of these tags becomes "untagged
        in that respect" — and the buffer is about to be cleared by
        ``set_text("")`` anyway, so there are no application ranges
        to worry about.
        """
        for tag in self._link_url_tags:
            self._tag_table.remove(tag)
        self._link_url_tags.clear()

    def _make_link_url_tag(self, url: str) -> Gtk.TextTag:
        """Build and register an anonymous tag carrying a link's URL.

        The tag itself has no visual properties — visual styling
        comes from the shared :data:`TagName.LINK` tag. This tag's
        sole purpose is to associate a buffer range with a URL via
        the ``self._link_url_tags`` mapping.
        """
        tag = Gtk.TextTag.new(None)
        self._tag_table.add(tag)
        self._link_url_tags[tag] = url
        return tag

    # ------------------------------------------------------------------
    # Block emission
    # ------------------------------------------------------------------

    def _emit_block(
        self,
        buffer: Gtk.TextBuffer,
        block: BlockNode,
        attacher: WidgetAttacher,
    ) -> None:
        if isinstance(block, Section):
            self._emit_section(buffer, block, attacher)
        elif isinstance(block, Paragraph):
            self._emit_paragraph(buffer, block)
        elif isinstance(block, UnorderedList):
            self._emit_unordered_list(buffer, block)
        elif isinstance(block, OrderedList):
            self._emit_ordered_list(buffer, block)
        elif isinstance(block, CodeBlock):
            self._emit_code_block(buffer, block, attacher)
        elif isinstance(block, Image):
            self._emit_image(buffer, block, attacher)
        elif isinstance(block, Table):
            self._emit_table(buffer, block, attacher)
        else:
            # Exhaustive over the current :data:`BlockNode` union. New
            # block kinds (Admonition, Blockquote in step 15) must extend
            # this dispatch — the ``else`` makes forgetting one a hard
            # failure rather than silent omission.
            raise TypeError(f"unknown block node: {type(block).__name__}")

    def _emit_section(
        self,
        buffer: Gtk.TextBuffer,
        section: Section,
        attacher: WidgetAttacher,
    ) -> None:
        self._emit_heading(buffer, section.title, level=section.level)
        for block in section.blocks:
            self._emit_block(buffer, block, attacher)

    def _emit_heading(
        self,
        buffer: Gtk.TextBuffer,
        title_inlines: tuple[InlineNode, ...],
        *,
        level: int,
    ) -> None:
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
        buffer.insert(buffer.get_end_iter(), _BLOCK_SEPARATOR)

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
    ) -> None:
        for item in ulist.items:
            buffer.insert(
                buffer.get_end_iter(),
                f"{_LIST_ITEM_INDENT}{_UNORDERED_BULLET}",
            )
            for inline in item.inlines:
                self._emit_inline(buffer, inline, [])
            buffer.insert(buffer.get_end_iter(), "\n")
        buffer.insert(buffer.get_end_iter(), "\n")

    def _emit_ordered_list(
        self,
        buffer: Gtk.TextBuffer,
        olist: OrderedList,
    ) -> None:
        for index, item in enumerate(olist.items, start=1):
            buffer.insert(
                buffer.get_end_iter(),
                f"{_LIST_ITEM_INDENT}{index}. ",
            )
            for inline in item.inlines:
                self._emit_inline(buffer, inline, [])
            buffer.insert(buffer.get_end_iter(), "\n")
        buffer.insert(buffer.get_end_iter(), "\n")

    def _emit_code_block(
        self,
        buffer: Gtk.TextBuffer,
        code_block: CodeBlock,
        attacher: WidgetAttacher,
    ) -> None:
        anchor = buffer.create_child_anchor(buffer.get_end_iter())
        widget = _build_code_block_widget(code_block.content)
        attacher(anchor, widget)
        buffer.insert(buffer.get_end_iter(), _BLOCK_SEPARATOR)

    def _emit_image(
        self,
        buffer: Gtk.TextBuffer,
        image: Image,
        attacher: WidgetAttacher,
    ) -> None:
        anchor = buffer.create_child_anchor(buffer.get_end_iter())
        widget = self._build_image_widget(image.filename)
        attacher(anchor, widget)
        buffer.insert(buffer.get_end_iter(), _BLOCK_SEPARATOR)

    def _emit_table(
        self,
        buffer: Gtk.TextBuffer,
        table: Table,
        attacher: WidgetAttacher,
    ) -> None:
        """Insert a table widget at a child anchor.

        The table is a :class:`Gtk.Grid` of :class:`Gtk.Label`s with
        ``wrap = TRUE``. Each label's ``max-width-chars`` is derived
        from the live column width (read once via the injected
        :data:`ColumnWidthResolver`) and the ``[cols="…"]``
        proportions, so wrapping tracks the user's window size and
        the column proportions the source declared. The first row
        carries bold weight to surface its header role visually.

        The renderer never produces a per-table horizontal scrollbar:
        cell content is inline-only in v1, so wrapping is always
        meaningful, and an internal scrollbar inside an article column
        would be visually noisy.
        """
        anchor = buffer.create_child_anchor(buffer.get_end_iter())
        widget = _build_table_widget(table, self._column_width_px())
        attacher(anchor, widget)
        buffer.insert(buffer.get_end_iter(), _BLOCK_SEPARATOR)

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
        if isinstance(inline, Link):
            self._emit_link(buffer, inline, tag_stack)
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

    def _emit_link(
        self,
        buffer: Gtk.TextBuffer,
        link: Link,
        tag_stack: list[Gtk.TextTag],
    ) -> None:
        """Emit a link's display children with link tags added.

        Two tags are stacked for the duration of the link's body:
        the shared :data:`TagName.LINK` tag (visual styling) and a
        fresh anonymous tag carrying the URL (consumed by
        :meth:`url_for_tags` for click handling). The display text
        is iterated through :meth:`_emit_inline` so any nested
        formatting in the link's display text composes correctly.
        """
        link_tag = self._tag(TagName.LINK)
        url_tag = self._make_link_url_tag(link.url)
        new_stack = [*tag_stack, link_tag, url_tag]
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
    # Image widget construction (and decode-failure fallback)
    # ------------------------------------------------------------------

    def _build_image_widget(self, filename: str) -> Gtk.Widget:
        data = self._image_bytes_for(filename)
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except GLib.Error:
            return _build_image_placeholder(filename)
        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        # Track the natural pixel dimensions of the image so the picture
        # never *upscales* a small image to fill the column. SCALE_DOWN
        # keeps it from overflowing a narrow column; pinning the
        # natural width via ``set_size_request`` keeps it at native size
        # when the column is wider than the image.
        picture.set_size_request(texture.get_width(), texture.get_height())
        return picture

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

        Each block emitter terminates with ``\\n\\n``; the last block
        therefore leaves a redundant blank line at the very end. Strip
        until at most one terminating newline remains — but only if
        the buffer has any content at all.
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


# ---------------------------------------------------------------------------
# Helper widget builders (module level; no renderer state required)
# ---------------------------------------------------------------------------


def _build_code_block_widget(content: str) -> Gtk.Widget:
    """Build the read-only code-block widget displayed in the buffer.

    Layout: ``Gtk.Frame`` → ``Gtk.ScrolledWindow`` →
    ``Gtk.TextView`` (read-only, ``wrap-mode = NONE``,
    ``family = monospace``). The scrolled window's horizontal policy is
    ``AUTOMATIC`` and vertical is ``NEVER`` — long unwrappable lines
    scroll inside the code block; vertical scrolling is left to the
    outer article container.
    """
    inner = Gtk.TextView.new()
    inner.set_editable(False)
    inner.set_cursor_visible(False)
    inner.set_monospace(True)
    inner.set_wrap_mode(Gtk.WrapMode.NONE)
    inner.get_buffer().set_text(content)

    scroll = Gtk.ScrolledWindow.new()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    scroll.set_child(inner)

    frame = Gtk.Frame.new()
    frame.set_hexpand(True)
    frame.set_child(scroll)
    return frame


def _build_table_widget(table: Table, column_width_px: int) -> Gtk.Widget:
    """Build the read-only table widget displayed in the buffer.

    Layout: ``Gtk.Frame`` → ``Gtk.Grid`` of cell ``Gtk.Label``\\ s. Each
    label uses Pango markup to render its inline formatting (bold,
    italic, monospace, etc.) and is configured with ``wrap = TRUE`` so
    long cell content wraps within the column rather than overflowing.

    ``column_width_px`` is the live pixel width of the article column
    (resolved at render time by :data:`ColumnWidthResolver`). It is
    converted to characters via :data:`TARGET_CHARS_PER_LINE`: the
    article column targets that many characters in body text, so the
    average glyph width is ``column_width_px / TARGET_CHARS_PER_LINE``
    pixels and dividing each table column's pixel slice by that gives
    its ``max-width-chars``. When the table carries no ``[cols=…]``
    directive, the budget is split equally; with a directive,
    ``proportion[i] / sum(proportions)`` of the column's pixels go
    to column ``i``. The math collapses algebraically (the
    ``column_width_px`` factor cancels), but threading it through
    keeps the contract honest — a future implementation that swaps
    in a Pango-measured glyph width plugs in here without changing
    the call site.

    The header row (``rows[0]``) is rendered with a bold label to
    visually distinguish it from data rows. No per-table horizontal
    scrollbar — wrapping inside the column is the only fitting
    strategy because cell content is inline-only in v1, and an
    internal scrollbar inside an article column would be visually
    noisy.
    """
    grid = Gtk.Grid.new()
    grid.set_hexpand(True)
    grid.set_column_spacing(_TABLE_COLUMN_SPACING)
    grid.set_row_spacing(_TABLE_ROW_SPACING)

    column_count = len(table.rows[0].cells)
    proportions = (
        table.column_proportions
        if table.column_proportions is not None
        else (1,) * column_count
    )
    max_chars_per_column = _max_chars_per_column(proportions, column_width_px)

    for row_index, row in enumerate(table.rows):
        is_header = row_index == 0
        for col_index, cell in enumerate(row.cells):
            label = _build_cell_label(
                cell,
                is_header=is_header,
                max_width_chars=max_chars_per_column[col_index],
            )
            grid.attach(label, col_index, row_index, 1, 1)

    frame = Gtk.Frame.new()
    frame.set_hexpand(True)
    frame.set_child(grid)
    return frame


def _build_cell_label(
    cell: TableCell,
    *,
    is_header: bool,
    max_width_chars: int,
) -> Gtk.Label:
    """Build a single :class:`Gtk.Label` for a table cell.

    The label renders the cell's inline content via Pango markup.
    ``wrap = TRUE`` lets long content wrap inside the column;
    ``max_width_chars`` is the upper-bound character width derived
    from the column proportion. ``xalign = 0.0`` left-aligns the
    text — table cells in this app are always left-aligned (the
    plan does not expose a per-cell alignment attribute).
    """
    markup = _inlines_to_pango_markup(cell.inlines, bold=is_header)
    label = Gtk.Label.new(None)
    label.set_markup(markup)
    label.set_wrap(True)
    label.set_max_width_chars(max_width_chars)
    label.set_xalign(0.0)
    label.set_yalign(0.0)
    label.set_hexpand(True)
    label.set_margin_start(_TABLE_CELL_PADDING)
    label.set_margin_end(_TABLE_CELL_PADDING)
    label.set_margin_top(_TABLE_CELL_PADDING)
    label.set_margin_bottom(_TABLE_CELL_PADDING)
    return label


def _max_chars_per_column(
    proportions: tuple[int, ...],
    column_width_px: int,
) -> tuple[int, ...]:
    """Return ``max-width-chars`` for each column from its proportion.

    The derivation in two steps:

    1. Average glyph width in pixels is
       ``column_width_px / TARGET_CHARS_PER_LINE``, on the basis that
       the article column targets that many characters of body text.
    2. Each table column's pixel slice is
       ``column_width_px * p_i / sum(proportions)``; dividing by the
       glyph width gives its character count.

    The two ``column_width_px`` factors cancel algebraically, so the
    result reduces to ``round(p_i / sum(proportions) * TARGET_CHARS_PER_LINE)``.
    Threading ``column_width_px`` through anyway keeps the contract
    honest — a future implementation that measures glyph width
    differently (e.g. via Pango against a real font) plugs in here.

    Each result is clamped to a minimum of one so a label always has
    at least some width to wrap into. ``column_width_px`` of zero or
    less (which only happens before the article container has been
    allocated) collapses to an all-ones tuple via the clamp.
    """
    total = sum(proportions)
    if column_width_px <= 0:
        return tuple(1 for _ in proportions)
    glyph_width_px = column_width_px / TARGET_CHARS_PER_LINE
    return tuple(
        max(
            1,
            round((column_width_px * p / total) / glyph_width_px),
        )
        for p in proportions
    )


def _inlines_to_pango_markup(
    inlines: tuple[InlineNode, ...],
    *,
    bold: bool = False,
) -> str:
    """Convert an inline-node tuple to a Pango markup string.

    Pango markup is the format :meth:`Gtk.Label.set_markup` consumes,
    and it covers everything the v1 inline subset needs: ``<b>``,
    ``<i>``, ``<s>``, ``<u>``, ``<tt>``, and ``<a href="…">``. Plain
    text is escaped with :func:`GLib.markup_escape_text` so user
    content with literal ``<`` or ``&`` never accidentally introduces
    markup.

    ``bold`` wraps the whole result in a single ``<b>…</b>`` so header
    cells render with bold weight. This is preferable to setting the
    label's ``weight`` attribute because nested inline formatting
    (e.g. an italic span inside a header) still needs to compose with
    the header's boldness — Pango handles the composition naturally.
    """
    body = "".join(_inline_to_pango_markup(node) for node in inlines)
    if bold:
        return f"<b>{body}</b>"
    return body


# Same dispatch-ladder shape as the parser's _parse_non_heading_block:
# isinstance over a closed union, one return per branch. Combining
# branches via a lookup table would require uniform child-traversal
# shapes the inline kinds don't share (Monospace's content is a str,
# Link's text uses a different field name, plain Text has no
# children at all).
# pylint: disable-next=too-many-return-statements
def _inline_to_pango_markup(node: InlineNode) -> str:
    """Convert a single inline node to its Pango markup form."""
    if isinstance(node, Text):
        return GLib.markup_escape_text(node.content)
    if isinstance(node, Bold):
        inner = "".join(_inline_to_pango_markup(c) for c in node.children)
        return f"<b>{inner}</b>"
    if isinstance(node, Italic):
        inner = "".join(_inline_to_pango_markup(c) for c in node.children)
        return f"<i>{inner}</i>"
    if isinstance(node, Strikethrough):
        inner = "".join(_inline_to_pango_markup(c) for c in node.children)
        return f"<s>{inner}</s>"
    if isinstance(node, Underline):
        inner = "".join(_inline_to_pango_markup(c) for c in node.children)
        return f"<u>{inner}</u>"
    if isinstance(node, Monospace):
        # Monospace's content is a literal :class:`str`, not a list of
        # nested inlines — match :class:`Monospace`'s "no re-parsing"
        # rule by escaping the content directly. Pango's ``<tt>`` tag
        # selects a monospace family.
        return f"<tt>{GLib.markup_escape_text(node.content)}</tt>"
    if isinstance(node, Link):
        inner = "".join(_inline_to_pango_markup(c) for c in node.text)
        # Pango's ``<a href="…">`` requires the URL itself to be
        # markup-escaped to handle ``&`` characters in query strings.
        href = GLib.markup_escape_text(node.url)
        return f'<a href="{href}">{inner}</a>'
    # Closed union; new inline kinds must extend this dispatch.
    raise TypeError(f"unknown inline node: {type(node).__name__}")


def _build_image_placeholder(filename: str) -> Gtk.Widget:
    """Fallback widget shown when image bytes fail to decode.

    A tiny labelled box rather than a blown-out broken-image icon — the
    document stays readable, and the user gets the original filename
    back as a hint at what is missing.
    """
    label = Gtk.Label.new(f"[Image: {filename}]")
    label.set_xalign(0.0)
    return label


def _noop_attacher(_anchor: Gtk.TextChildAnchor, _widget: Gtk.Widget) -> None:
    """Default ``attach_widget`` callback — drops widgets on the floor."""
