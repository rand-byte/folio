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
  call :meth:`render_into` again after a column-width change). Step 6
  does not yet emit table widgets — but the resolver is wired in from
  day one so step 14 only adds a new block branch, not a new
  parameter.
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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

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
    OrderedList,
    Paragraph,
    Section,
    Strikethrough,
    Text,
    Underline,
    UnorderedList,
)
from notes_app.asciidoc.parser import parse
from notes_app.asciidoc.tag_table import TagName, heading_tag_name
from notes_app.storage.protocols import ColumnWidthResolver, ImageBytesResolver

if TYPE_CHECKING:
    pass


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


class TextBufferRenderer:
    """Render an AsciiDoc source string into a :class:`Gtk.TextBuffer`.

    Construction-time dependencies (the two resolvers and the tag
    table) are injected. Tests swap in fakes; production wires them up
    in the ``ui/note_view`` module from build step 8 onward.
    """

    _image_bytes_for: ImageBytesResolver
    _column_width_px: ColumnWidthResolver
    _tag_table: Gtk.TextTagTable

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
        if document.title is not None:
            self._emit_heading(buffer, document.title, level=0)
        for block in document.blocks:
            self._emit_block(buffer, block, attacher)
        self._strip_trailing_blank(buffer)

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
        else:
            # Exhaustive over the step-4 :data:`BlockNode` union. New
            # block kinds (Table, Admonition, Blockquote in steps 14
            # and 15) must extend this dispatch — the ``else`` makes
            # forgetting one a hard failure rather than silent omission.
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

    # ------------------------------------------------------------------
    # Inline emission
    # ------------------------------------------------------------------

    def _emit_inline(
        self,
        buffer: Gtk.TextBuffer,
        inline: InlineNode,
        tag_stack: list[Gtk.TextTag],
    ) -> None:
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
        # Closed union; new inline kinds (Monospace, Link in step 13)
        # must extend this dispatch.
        raise TypeError(f"unknown inline node: {type(inline).__name__}")

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
