"""Tests for :mod:`ui.note_render.textbuffer_renderer`."""

from __future__ import annotations

import struct
import unittest
import zlib
from collections.abc import Callable

from gi.repository import Gdk, GLib, Gtk, Pango

from giruntime.ui.note_render.tag_table import (
    TagName,
    admonition_body_tag_name,
    admonition_kind_tag_name,
    admonition_label_tag_name,
    build_tag_table,
)
from giruntime.ui.note_render.textbuffer_renderer import (
    TextBufferRenderer,
    _CellRun,
    _ORDERED_STYLES,
    _PlaceholderImagePaintable,
    _ScaledImagePaintable,
    _UNORDERED_GLYPHS,
    _format_ordinal,
    _table_column_pixels,
    _table_tab_stops,
    _truncate_cell,
)
from enums import AdmonitionKind, ListNumberStyle
from models.parse_error import ParseError
from config.defaults import MAX_LIST_DEPTH, TABLE_CELL_HPADDING_PX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A minimal valid 1×1 RGBA PNG, generated once at import time.
def _make_1x1_png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)  # 1x1 8-bit RGBA
    raw = b"\x00\xff\x00\x00\xff"  # filter byte + RGBA pixel
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


_PNG_1X1: bytes = _make_1x1_png()


def _make_solid_png(width: int, height: int) -> bytes:
    """Return a minimal RGBA PNG of the given dimensions, all-opaque-red.

    Used to exercise the :class:`_ScaledImagePaintable` scaling path —
    a texture wider than the column width must report a capped
    intrinsic width and a proportionally scaled intrinsic height.
    """
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    # One filter byte (0 = None) per row, then RGBA pixels.
    row = b"\x00" + (b"\xff\x00\x00\xff" * width)
    raw = row * height
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


_PNG_200X100: bytes = _make_solid_png(200, 100)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


_FAKE_CHAR_PX: int = 9
"""Plain-text pixels-per-character used by the fake cell measurer."""

_FAKE_BOLD_CHAR_PX: int = 10
"""Bold pixels-per-character (a hair wider than plain) for the fake measurer."""


def _fake_cell_width(text: str, bold: bool, monospace: bool) -> int:
    """Deterministic :data:`CellWidthMeasurer` fake for table tests.

    Plain and monospace glyphs are the same width
    (:data:`_FAKE_CHAR_PX` px each); bold glyphs are one px wider
    (:data:`_FAKE_BOLD_CHAR_PX`). Mirrors the ``char_width_px=9`` tag
    convention so truncation arithmetic is exact and readable in tests.
    """
    del monospace  # same width class as plain in the fake
    per_char = _FAKE_BOLD_CHAR_PX if bold else _FAKE_CHAR_PX
    return len(text) * per_char


def _anonymous_tag_count(table: Gtk.TextTagTable) -> int:
    """Count tags in ``table`` with no name (link + table-tab tags)."""
    count = 0

    def visit(tag: Gtk.TextTag) -> None:
        nonlocal count
        if tag.get_property("name") is None:
            count += 1

    table.foreach(visit)
    return count


def _full_text(buffer: Gtk.TextBuffer) -> str:
    """Whole buffer text excluding child-anchor placeholder characters."""
    text: str = buffer.get_text(
        buffer.get_start_iter(),
        buffer.get_end_iter(),
        False,
    )
    return text


def _tag_names_at(buffer: Gtk.TextBuffer, offset: int) -> set[str]:
    return {
        t.get_property("name") for t in buffer.get_iter_at_offset(offset).get_tags()
    }


def _ranges_with_tag(buffer: Gtk.TextBuffer, tag_name: str) -> list[tuple[int, int]]:
    """List of ``[start, end)`` offset ranges where ``tag_name`` is applied."""
    table = buffer.get_tag_table()
    tag = table.lookup(tag_name)
    if tag is None:
        return []
    ranges: list[tuple[int, int]] = []
    end_offset = buffer.get_end_iter().get_offset()
    iterator = buffer.get_start_iter()
    in_run = tag in iterator.get_tags()
    run_start = 0 if in_run else -1
    while iterator.get_offset() < end_offset:
        if not iterator.forward_to_tag_toggle(tag):
            break
        offset = iterator.get_offset()
        if in_run:
            ranges.append((run_start, offset))
            in_run = False
        else:
            run_start = offset
            in_run = True
    if in_run:
        ranges.append((run_start, end_offset))
    return ranges


def _anchor_offsets(buffer: Gtk.TextBuffer) -> list[int]:
    """Return the offsets of every child anchor, in order."""
    offsets: list[int] = []
    iterator = buffer.get_start_iter()
    while True:
        anchor = iterator.get_child_anchor()
        if anchor is not None:
            offsets.append(iterator.get_offset())
        if not iterator.forward_char():
            break
    return offsets


def _paintables_at(
    buffer: Gtk.TextBuffer,
) -> list[tuple[int, Gdk.Paintable]]:
    """Return ``(offset, paintable)`` pairs for every inline paintable.

    Images are inserted via :meth:`Gtk.TextBuffer.insert_paintable`, so
    they sit at a single buffer offset and are recoverable via
    :meth:`Gtk.TextIter.get_paintable`. The renderer's new image path
    relies on this — the test helper iterates the buffer once and
    returns the lot in document order.
    """
    found: list[tuple[int, Gdk.Paintable]] = []
    iterator = buffer.get_start_iter()
    while True:
        paintable = iterator.get_paintable()
        if paintable is not None:
            found.append((iterator.get_offset(), paintable))
        if not iterator.forward_char():
            break
    return found


def _build_renderer(
    *,
    image_bytes_for: Callable[[str], bytes] | None = None,
    column_width_px: Callable[[], int] | None = None,
    cell_width_px: Callable[[str, bool, bool], int] | None = None,
    tag_table: Gtk.TextTagTable | None = None,
) -> tuple[TextBufferRenderer, Gtk.TextBuffer, Gtk.TextTagTable]:
    """Construct a renderer and a buffer wired to a fresh tag table."""
    table = tag_table if tag_table is not None else build_tag_table(char_width_px=9)
    renderer = TextBufferRenderer(
        image_bytes_for=image_bytes_for if image_bytes_for is not None else (lambda _f: _PNG_1X1),
        column_width_px=column_width_px if column_width_px is not None else (lambda: 800),
        cell_width_px=cell_width_px if cell_width_px is not None else _fake_cell_width,
        tag_table=table,
    )
    buffer = Gtk.TextBuffer.new(table)
    return renderer, buffer, table


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class HeadingRenderingTests(unittest.TestCase):
    def test_document_title_is_tagged_heading_0(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("= Welcome\n", buffer, note_id="n1")
        text = _full_text(buffer)
        self.assertTrue(text.startswith("Welcome"))
        # The full title text carries heading_0.
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.HEADING_0.value),
            [(0, len("Welcome"))],
        )

    def test_section_headings_get_per_level_tags(self) -> None:
        src = "= Doc\n\n== Two\n\n=== Three\n\n====== Six\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        # Each heading body is the only place its tag appears.
        for body, tag_name in (
            ("Doc", TagName.HEADING_0),
            ("Two", TagName.HEADING_2),
            ("Three", TagName.HEADING_3),
            ("Six", TagName.HEADING_6),
        ):
            with self.subTest(heading=body):
                start = text.index(body)
                ranges = _ranges_with_tag(buffer, tag_name.value)
                self.assertEqual(ranges, [(start, start + len(body))])

    def test_inline_formatting_inside_heading_is_preserved(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("= Hello *world*\n", buffer, note_id="n1")
        text = _full_text(buffer)
        # Heading tag covers the whole title …
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.HEADING_0.value),
            [(0, len("Hello world"))],
        )
        # … and bold tag still covers just the bold span.
        bold_start = text.index("world")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.BOLD.value),
            [(bold_start, bold_start + len("world"))],
        )


@unittest.skipUnless(_display_available(), "no GDK display")
class InlineRenderingTests(unittest.TestCase):
    def test_bold_italic_strikethrough_underline(self) -> None:
        src = (
            "= D\n\n"
            "Plain *bold* _italic_ "
            "[.line-through]#strike# [.underline]#under#.\n"
        )
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        for body, tag_name in (
            ("bold", TagName.BOLD),
            ("italic", TagName.ITALIC),
            ("strike", TagName.STRIKETHROUGH),
            ("under", TagName.UNDERLINE),
        ):
            with self.subTest(body=body):
                start = text.index(body)
                self.assertEqual(
                    _ranges_with_tag(buffer, tag_name.value),
                    [(start, start + len(body))],
                )

    def test_nested_bold_inside_italic(self) -> None:
        # _italic *bold-inside-italic* still-italic_
        src = "= D\n\n_outer *inner* tail_\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        # Italic spans the whole inner text including the bold word.
        italic_start = text.index("outer")
        italic_end = italic_start + len("outer inner tail")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.ITALIC.value),
            [(italic_start, italic_end)],
        )
        # Bold sits strictly inside.
        bold_start = text.index("inner")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.BOLD.value),
            [(bold_start, bold_start + len("inner"))],
        )
        # The 'inner' word carries both tags simultaneously.
        self.assertEqual(
            _tag_names_at(buffer, bold_start),
            {TagName.ITALIC.value, TagName.BOLD.value},
        )

    def test_plain_text_has_no_tags(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("= D\n\nplain words.\n", buffer, note_id="n1")
        text = _full_text(buffer)
        plain_start = text.index("plain")
        self.assertEqual(_tag_names_at(buffer, plain_start), set())


@unittest.skipUnless(_display_available(), "no GDK display")
class ListRenderingTests(unittest.TestCase):
    def test_unordered_list_uses_bullet_glyphs(self) -> None:
        src = "= D\n\n* one\n* two\n* three\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        # Three bullet glyphs, one per item.
        self.assertEqual(text.count("•"), 3)
        # Items appear in order.
        idx_one = text.index("one")
        idx_two = text.index("two")
        idx_three = text.index("three")
        self.assertLess(idx_one, idx_two)
        self.assertLess(idx_two, idx_three)

    def test_ordered_list_uses_sequential_numbers(self) -> None:
        src = "= D\n\n. first\n. second\n. third\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        # Numbering is 1., 2., 3. — not the literal '. ' marker from
        # source. ``find`` returns -1 for missing, so use ``index`` to
        # assert presence.
        text.index("1. first")
        text.index("2. second")
        text.index("3. third")

    def test_blank_separated_ordered_list_numbers_continuously(self) -> None:
        # Blank lines between ordered items are absorbed by the parser into
        # one list, so the positional numbering continues 1., 2., 3. rather
        # than restarting at 1. on each blank-separated item.
        src = "= D\n\n. first\n\n. second\n\n. third\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        text.index("1. first")
        text.index("2. second")
        text.index("3. third")

    def test_list_items_carry_inline_formatting(self) -> None:
        src = "= D\n\n* an *emphatic* point\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        emp_start = text.index("emphatic")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.BOLD.value),
            [(emp_start, emp_start + len("emphatic"))],
        )

    def test_depth_tables_match_max_list_depth(self) -> None:
        # The cap and the two presentation tables cannot drift.
        self.assertEqual(len(_UNORDERED_GLYPHS), MAX_LIST_DEPTH)
        self.assertEqual(len(_ORDERED_STYLES), MAX_LIST_DEPTH)

    def test_nested_unordered_exact_buffer_text(self) -> None:
        # Three unordered levels: indent scales by depth (4 spaces per
        # level) and the glyph changes •/◦/▪ with depth. A trailing
        # paragraph makes the list's block separator deterministic (the
        # buffer's final blank line is otherwise trimmed).
        src = "= D\n\n* one\n** two\n*** three\n* four\n\nEnd.\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        self.assertEqual(
            _full_text(buffer),
            "D\n"
            "    •  one\n"
            "        ◦  two\n"
            "            ▪  three\n"
            "    •  four\n"
            "\n"
            "End.",
        )

    def test_nested_ordered_numbering_by_depth_restarts_per_sublist(
        self,
    ) -> None:
        # arabic at level 1, lower-alpha at level 2; each sub-list numbers
        # from the top.
        src = "= D\n\n. one\n.. sub-a\n.. sub-b\n. two\n\nEnd.\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        self.assertEqual(
            _full_text(buffer),
            "D\n"
            "    1. one\n"
            "        a. sub-a\n"
            "        b. sub-b\n"
            "    2. two\n"
            "\n"
            "End.",
        )

    def test_three_level_ordered_uses_roman_at_depth_three(self) -> None:
        src = "= D\n\n. one\n.. a\n... i\n\nEnd.\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        self.assertEqual(
            _full_text(buffer),
            "D\n"
            "    1. one\n"
            "        a. a\n"
            "            i. i\n"
            "\n"
            "End.",
        )

    def test_mixed_nesting_uses_child_list_kind(self) -> None:
        # An ordered sub-list under an unordered item renders with
        # ordered markers at the deeper indent.
        src = "= D\n\n* parent\n.. step\n\nEnd.\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        self.assertEqual(
            _full_text(buffer),
            "D\n    •  parent\n        a. step\n\nEnd.",
        )


class FormatOrdinalTests(unittest.TestCase):
    """The depth→style ordinal formatter and its base-26 / roman cases."""

    def test_arabic(self) -> None:
        self.assertEqual(_format_ordinal(ListNumberStyle.ARABIC, 1), "1.")
        self.assertEqual(_format_ordinal(ListNumberStyle.ARABIC, 42), "42.")

    def test_lower_alpha_basic_and_boundary(self) -> None:
        self.assertEqual(_format_ordinal(ListNumberStyle.LOWER_ALPHA, 1), "a.")
        self.assertEqual(_format_ordinal(ListNumberStyle.LOWER_ALPHA, 26), "z.")
        # Bijective base-26 rolls z -> aa (not `[a` or a gap).
        self.assertEqual(_format_ordinal(ListNumberStyle.LOWER_ALPHA, 27), "aa.")
        self.assertEqual(_format_ordinal(ListNumberStyle.LOWER_ALPHA, 28), "ab.")

    def test_lower_roman(self) -> None:
        self.assertEqual(_format_ordinal(ListNumberStyle.LOWER_ROMAN, 1), "i.")
        self.assertEqual(_format_ordinal(ListNumberStyle.LOWER_ROMAN, 4), "iv.")
        self.assertEqual(_format_ordinal(ListNumberStyle.LOWER_ROMAN, 9), "ix.")


@unittest.skipUnless(_display_available(), "no GDK display")
class CodeBlockRenderingTests(unittest.TestCase):
    """Code blocks render as a tinted, monospace paragraph range.

    The plan moved code blocks out of an anchored frame-and-scroller
    widget and into the buffer itself: the source content is inserted
    verbatim with both :data:`TagName.CODE_BLOCK` (paragraph
    background + side margins) and :data:`TagName.MONOSPACE` (font
    family) applied across the range. Wrapping comes from the outer
    :class:`Gtk.TextView`'s ``WORD_CHAR`` wrap mode — there is no
    inner scrolled window any more.
    """

    def test_code_block_content_is_inserted_into_buffer(self) -> None:
        src = "= D\n\n----\nprint('hi')\n----\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        # No child anchors anywhere: nothing escapes to widget land.
        self.assertEqual(_anchor_offsets(buffer), [])
        self.assertIn("print('hi')", _full_text(buffer))

    def test_code_block_carries_code_block_and_monospace_tags(self) -> None:
        # The two tags layer across the same range: CODE_BLOCK carries
        # the paragraph background tint, MONOSPACE carries the font.
        # Both must be present on every character of the content.
        src = "= D\n\n----\nabc\n----\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        start = text.index("abc")
        for offset in range(start, start + 3):
            tags = _tag_names_at(buffer, offset)
            self.assertIn(TagName.CODE_BLOCK.value, tags)
            self.assertIn(TagName.MONOSPACE.value, tags)

    def test_code_block_content_is_verbatim(self) -> None:
        # No whitespace normalisation, no re-parsing of inline markers
        # like ``*`` or ``_`` — code-block content is literal.
        code = "def f():\n    return 42"
        src = f"= D\n\n----\n{code}\n----\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        self.assertIn(code, _full_text(buffer))

    def test_code_block_does_not_attach_a_widget(self) -> None:
        # The whole point of the rewrite: no widgets for code blocks.
        # No child anchor is ever created.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "= D\n\n----\nx\n----\n",
            buffer,
            note_id="n1",
        )
        self.assertEqual(_anchor_offsets(buffer), [])


@unittest.skipUnless(_display_available(), "no GDK display")
class ImageRenderingTests(unittest.TestCase):
    def test_image_invokes_resolver_with_filename(self) -> None:
        calls: list[str] = []

        def resolver(filename: str) -> bytes:
            calls.append(filename)
            return _PNG_1X1

        renderer, buffer, _ = _build_renderer(image_bytes_for=resolver)
        renderer.render_into(
            "= D\n\nimage::cat.png[]\n", buffer, note_id="n1"
        )
        self.assertEqual(calls, ["cat.png"])

    def test_image_resolver_invoked_once_per_image(self) -> None:
        # Two image references — even with the same filename — produce
        # two resolver calls. The renderer doesn't cache; that is
        # ``ui/note_view``'s job per the plan.
        calls: list[str] = []

        def resolver(filename: str) -> bytes:
            calls.append(filename)
            return _PNG_1X1

        renderer, buffer, _ = _build_renderer(image_bytes_for=resolver)
        renderer.render_into(
            "= D\n\nimage::a.png[]\n\nimage::a.png[]\n",
            buffer,
            note_id="n1",
        )
        self.assertEqual(calls, ["a.png", "a.png"])

    def test_image_inserts_a_scaled_paintable(self) -> None:
        # Images are now inserted via insert_paintable; the wrapper
        # paintable scales the texture down to the column width if the
        # texture is wider than the column. The 1×1 PNG produced by the
        # default resolver is smaller than the column, so the
        # intrinsic width equals the texture width (1).
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 800)
        renderer.render_into(
            "= D\n\nimage::cat.png[]\n",
            buffer,
            note_id="n1",
        )
        paintables = _paintables_at(buffer)
        self.assertEqual(len(paintables), 1)
        offset, paintable = paintables[0]
        self.assertIsInstance(paintable, _ScaledImagePaintable)
        # The 1×1 PNG is below the column width — intrinsic width
        # equals the texture's width.
        self.assertEqual(paintable.get_intrinsic_width(), 1)
        self.assertGreaterEqual(offset, 0)

    def test_scaled_paintable_caps_intrinsic_width_at_column_width(self) -> None:
        # Construct the wrapper directly with a synthetic texture to
        # cover the scaling case without needing a large PNG fixture.
        # GObject-introspected member; pylint cannot see it when Graphene
        # is loaded alongside GLib (see the renderer's own use).
        # pylint: disable-next=no-member
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(_PNG_1X1))
        wrapper = _ScaledImagePaintable(texture=texture, column_width_px=4)
        # Texture is 1×1, column is 4 → texture fits without scaling.
        self.assertEqual(wrapper.get_intrinsic_width(), 1)
        self.assertEqual(wrapper.get_intrinsic_height(), 1)

    def test_scaled_paintable_scales_wide_image_proportionally(self) -> None:
        # 200×100 texture in a 50-pixel column → intrinsic width 50,
        # intrinsic height proportionally scaled to 25.
        # GObject-introspected member; pylint cannot see it when Graphene
        # is loaded alongside GLib (see the renderer's own use).
        # pylint: disable-next=no-member
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(_PNG_200X100))
        wrapper = _ScaledImagePaintable(texture=texture, column_width_px=50)
        self.assertEqual(wrapper.get_intrinsic_width(), 50)
        self.assertEqual(wrapper.get_intrinsic_height(), 25)

    def test_scaled_paintable_zero_column_width_uses_natural_dims(self) -> None:
        # Defensive: before the article container has been allocated
        # the column-width resolver may return 0. The wrapper falls
        # back to the texture's natural dimensions in that case so the
        # paintable doesn't collapse to invisible.
        # GObject-introspected member; pylint cannot see it when Graphene
        # is loaded alongside GLib (see the renderer's own use).
        # pylint: disable-next=no-member
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(_PNG_200X100))
        wrapper = _ScaledImagePaintable(texture=texture, column_width_px=0)
        self.assertEqual(wrapper.get_intrinsic_width(), 200)
        self.assertEqual(wrapper.get_intrinsic_height(), 100)

    def test_image_does_not_attach_a_widget(self) -> None:
        # Images are inline paintables now — no widget escape.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "= D\n\nimage::cat.png[]\n",
            buffer,
            note_id="n1",
        )
        self.assertEqual(_anchor_offsets(buffer), [])

    def test_decode_failure_produces_a_placeholder_paintable(self) -> None:
        # On Gdk decode error the renderer inserts a placeholder
        # paintable (a small grey rectangle) so the document remains
        # readable even when an image is missing or corrupted.
        renderer, buffer, _ = _build_renderer(
            image_bytes_for=lambda _f: b"not a png"
        )
        renderer.render_into(
            "= D\n\nimage::broken.png[]\n",
            buffer,
            note_id="n1",
        )
        paintables = _paintables_at(buffer)
        self.assertEqual(len(paintables), 1)
        _, paintable = paintables[0]
        self.assertIsInstance(paintable, _PlaceholderImagePaintable)
        # Placeholder has nonzero intrinsic dimensions so it actually
        # paints something visible.
        self.assertGreater(paintable.get_intrinsic_width(), 0)
        self.assertGreater(paintable.get_intrinsic_height(), 0)

    def test_resolver_exception_other_than_glib_propagates(self) -> None:
        # KeyError from a misconfigured resolver is *not* swallowed —
        # only Gdk decode errors fall back to a placeholder.
        def resolver(_filename: str) -> bytes:
            raise KeyError("not found")

        renderer, buffer, _ = _build_renderer(image_bytes_for=resolver)
        with self.assertRaises(KeyError):
            renderer.render_into(
                "= D\n\nimage::missing.png[]\n",
                buffer,
                note_id="n1",
            )

    def test_no_image_in_source_means_resolver_is_not_called(self) -> None:
        calls: list[str] = []

        def resolver(filename: str) -> bytes:
            calls.append(filename)
            return _PNG_1X1

        renderer, buffer, _ = _build_renderer(image_bytes_for=resolver)
        renderer.render_into(
            "= D\n\nJust prose, no images.\n", buffer, note_id="n1"
        )
        self.assertEqual(calls, [])


@unittest.skipUnless(_display_available(), "no GDK display")
class ColumnWidthResolverTests(unittest.TestCase):
    def test_resolver_is_not_called_for_text_only_blocks(self) -> None:
        # The renderer only invokes the column-width resolver when a
        # block actually needs a pixel width: tables (to set the
        # frame's size request) and images (to construct the scaled
        # paintable). Pure-prose blocks — headings, paragraphs, lists,
        # admonitions, blockquotes, code blocks — never call it.
        calls = 0

        def column_width() -> int:
            nonlocal calls
            calls += 1
            return 600

        renderer, buffer, _ = _build_renderer(column_width_px=column_width)
        renderer.render_into(
            "= Welcome\n\n"
            "A *para* with formatting.\n\n"
            "* One\n* Two\n\n"
            "NOTE: a note\n\n"
            "____\nq\n____\n\n"
            "----\ncode\n----\n",
            buffer,
            note_id="n1",
        )
        self.assertEqual(calls, 0)

    def test_resolver_is_called_when_image_is_present(self) -> None:
        # Images need the column width to construct the scaled
        # paintable. The resolver is read once per image.
        calls = 0

        def column_width() -> int:
            nonlocal calls
            calls += 1
            return 600

        renderer, buffer, _ = _build_renderer(column_width_px=column_width)
        renderer.render_into(
            "= D\n\nimage::a.png[]\n", buffer, note_id="n1"
        )
        self.assertEqual(calls, 1)

    def test_resolver_is_called_when_table_is_present(self) -> None:
        # Tables need the column width for both the frame's size
        # request and the cell-label max-width-chars arithmetic.
        calls = 0

        def column_width() -> int:
            nonlocal calls
            calls += 1
            return 600

        renderer, buffer, _ = _build_renderer(column_width_px=column_width)
        renderer.render_into(
            "|===\n|a|b\n|===\n", buffer, note_id="n1"
        )
        self.assertGreaterEqual(calls, 1)


@unittest.skipUnless(_display_available(), "no GDK display")
class RebuildSemanticsTests(unittest.TestCase):
    def test_render_clears_existing_buffer_content(self) -> None:
        renderer, buffer, _ = _build_renderer()
        buffer.insert(buffer.get_end_iter(), "STALE")
        renderer.render_into("= Fresh\n", buffer, note_id="n1")
        self.assertNotIn("STALE", _full_text(buffer))
        self.assertIn("Fresh", _full_text(buffer))

    def test_re_rendering_drops_previous_table_tab_tags(self) -> None:
        # Two render passes on the same buffer must not accumulate the
        # anonymous per-table tab tags. Tables no longer produce a child
        # anchor at all (every block kind renders inline), but each table
        # mints one anonymous tag carrying its ``Pango.TabArray``; the
        # next render sweeps the previous one, so a table-then-no-table
        # sequence leaves zero anonymous tags and zero anchors.
        renderer, buffer, table = _build_renderer()
        renderer.render_into(
            "= D\n\n|===\n|a|b\n|c|d\n|===\n",
            buffer,
            note_id="n1",
        )
        self.assertEqual(_anonymous_tag_count(table), 1)
        renderer.render_into(
            "= D\n\nNo table here.\n",
            buffer,
            note_id="n1",
        )
        # The previous table's tab tag was swept; no table this pass.
        self.assertEqual(_anonymous_tag_count(table), 0)
        self.assertEqual(_anchor_offsets(buffer), [])

    def test_buffer_does_not_end_with_blank_line(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("= D\n\nFinal paragraph.\n", buffer, note_id="n1")
        text = _full_text(buffer)
        # A reasonable upper bound: at most one trailing newline.
        self.assertFalse(text.endswith("\n\n"))

    def test_render_uses_renderers_tag_table(self) -> None:
        # If the buffer was constructed with a different tag table,
        # the renderer raises rather than silently writing tags that
        # are missing from the buffer.
        wrong_table = build_tag_table(char_width_px=9)
        right_table = build_tag_table(char_width_px=9)
        renderer = TextBufferRenderer(
            image_bytes_for=lambda _f: _PNG_1X1,
            column_width_px=lambda: 800,
            cell_width_px=_fake_cell_width,
            tag_table=right_table,
        )
        wrong_buffer = Gtk.TextBuffer.new(wrong_table)
        with self.assertRaises(ValueError):
            renderer.render_into("= D\n", wrong_buffer, note_id="n1")


@unittest.skipUnless(_display_available(), "no GDK display")
class ParseErrorPropagationTests(unittest.TestCase):
    def test_parse_error_propagates_to_caller(self) -> None:
        # An unterminated code fence should reach the caller as a
        # :class:`ParseError`, untouched. The renderer never silently
        # produces a degraded buffer for malformed source.
        renderer, buffer, _ = _build_renderer()
        with self.assertRaises(ParseError):
            renderer.render_into(
                "= D\n\n----\nopen forever\n",
                buffer,
                note_id="n1",
            )


@unittest.skipUnless(_display_available(), "no GDK display")
class EmptyDocumentTests(unittest.TestCase):
    def test_empty_source_yields_empty_buffer(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("", buffer, note_id="n1")
        self.assertEqual(_full_text(buffer), "")

    def test_titleless_document_still_renders_blocks(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("Just a paragraph.\n", buffer, note_id="n1")
        self.assertIn("Just a paragraph.", _full_text(buffer))
        # No heading_0 tag because there is no document title.
        self.assertEqual(_ranges_with_tag(buffer, TagName.HEADING_0.value), [])


# ---------------------------------------------------------------------------
# Monospace (step 13)
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class MonospaceRenderingTests(unittest.TestCase):
    """The MONOSPACE tag is applied to the literal content of `…`."""

    def test_monospace_span_emits_content_with_tag(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("Use `f(x)` here.\n", buffer, note_id="n1")
        text = _full_text(buffer)
        # Content is the literal body — no backticks.
        self.assertIn("Use f(x) here.", text)
        body_start = text.index("f(x)")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.MONOSPACE.value),
            [(body_start, body_start + len("f(x)"))],
        )

    def test_monospace_body_is_not_re_parsed(self) -> None:
        # The body contains *bold* characters, but they are literal —
        # no BOLD tag should appear on the monospace range.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "= D\n\nbefore `*not bold*` after\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        self.assertIn("*not bold*", text)
        self.assertEqual(_ranges_with_tag(buffer, TagName.BOLD.value), [])
        # MONOSPACE covers exactly the literal body (with the asterisks).
        body_start = text.index("*not bold*")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.MONOSPACE.value),
            [(body_start, body_start + len("*not bold*"))],
        )

    def test_monospace_inside_bold_carries_both_tags(self) -> None:
        # ``*outer `inner` end*`` — the monospace span sits inside the
        # bold span, so the inner range carries both tags.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "= D\n\n*outer `inner` end*\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        bold_ranges = _ranges_with_tag(buffer, TagName.BOLD.value)
        mono_ranges = _ranges_with_tag(buffer, TagName.MONOSPACE.value)
        # One bold range covering the whole "outer inner end".
        self.assertEqual(len(bold_ranges), 1)
        bold_start, bold_end = bold_ranges[0]
        self.assertEqual(text[bold_start:bold_end], "outer inner end")
        # One monospace range, fully inside the bold range.
        self.assertEqual(len(mono_ranges), 1)
        mono_start, mono_end = mono_ranges[0]
        self.assertEqual(text[mono_start:mono_end], "inner")
        self.assertGreaterEqual(mono_start, bold_start)
        self.assertLessEqual(mono_end, bold_end)


# ---------------------------------------------------------------------------
# Links (step 13)
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class LinkRenderingTests(unittest.TestCase):
    """LINK shared tag + per-link anonymous URL tag are both applied."""

    def test_bare_url_emits_link_tag_over_url_text(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "see https://example.com today\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        self.assertIn("https://example.com", text)
        link_start = text.index("https://example.com")
        link_end = link_start + len("https://example.com")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.LINK.value),
            [(link_start, link_end)],
        )

    def test_url_with_text_link_uses_display_text(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "click https://x[here] now\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        # Visible text is the display text, not the URL.
        self.assertIn("click here now", text)
        self.assertNotIn("https://x", text)
        # LINK tag covers exactly the display text "here".
        link_start = text.index("here")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.LINK.value),
            [(link_start, link_start + len("here"))],
        )

    def test_link_macro_uses_display_text(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "see link:https://x[the docs]\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        self.assertIn("see the docs", text)
        link_start = text.index("the docs")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.LINK.value),
            [(link_start, link_start + len("the docs"))],
        )

    def test_url_recoverable_via_url_for_tags(self) -> None:
        # The renderer's ``url_for_tags`` should return the URL of
        # whichever link the iter is inside. This is the contract
        # the click handler in ui/link_handler relies on.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "go to https://example.com[here] please\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        # Pick an offset inside the display text "here".
        offset = text.index("here") + 1
        tags = buffer.get_iter_at_offset(offset).get_tags()
        url = renderer.url_for_tags(list(tags))
        self.assertEqual(url, "https://example.com")

    def test_url_for_tags_returns_none_outside_link(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "no link here, just text\n",
            buffer,
            note_id="n1",
        )
        offset = 2  # somewhere inside "no link here..."
        tags = buffer.get_iter_at_offset(offset).get_tags()
        self.assertIsNone(renderer.url_for_tags(list(tags)))

    def test_two_links_get_distinct_url_tags(self) -> None:
        # Each link produces its own anonymous URL-marker tag —
        # confirmed by recovering distinct URLs from the two
        # display-text positions.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "first https://a.com[A] then https://b.com[B] done\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        a_offset = text.index("A")
        b_offset = text.index("B")
        a_tags = buffer.get_iter_at_offset(a_offset).get_tags()
        b_tags = buffer.get_iter_at_offset(b_offset).get_tags()
        self.assertEqual(renderer.url_for_tags(list(a_tags)), "https://a.com")
        self.assertEqual(renderer.url_for_tags(list(b_tags)), "https://b.com")

    def test_link_inside_bold_carries_both_tags(self) -> None:
        # *Read https://x[here] now* — bold wraps a link; the link
        # range carries BOLD, LINK, and the anon URL tag.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "= D\n\n*Read https://x[here] now*\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        link_start = text.index("here")
        link_end = link_start + len("here")
        # LINK and BOLD ranges both contain [link_start, link_end].
        bold_ranges = _ranges_with_tag(buffer, TagName.BOLD.value)
        link_ranges = _ranges_with_tag(buffer, TagName.LINK.value)
        self.assertTrue(
            any(s <= link_start and e >= link_end for s, e in bold_ranges),
            f"bold range {bold_ranges} did not enclose link [{link_start},{link_end})",
        )
        self.assertEqual(link_ranges, [(link_start, link_end)])

    def test_monospace_inside_link_display_carries_both_tags(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "= D\n\nthe https://x[`f()` function] runs\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        # Display text is "`f()` function" → as visible text, "f() function".
        self.assertIn("f() function", text)
        # LINK covers the whole display text.
        link_start = text.index("f() function")
        link_end = link_start + len("f() function")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.LINK.value),
            [(link_start, link_end)],
        )
        # MONOSPACE covers just "f()".
        mono_start = text.index("f()")
        self.assertEqual(
            _ranges_with_tag(buffer, TagName.MONOSPACE.value),
            [(mono_start, mono_start + len("f()"))],
        )

    def test_re_render_clears_stale_link_tags(self) -> None:
        # The renderer is responsible for cleaning up anonymous
        # link-URL tags between renders. After two renders, the tag
        # table must contain only the URL tags from the latest
        # render — the old ones have been removed.
        renderer, buffer, table = _build_renderer()
        renderer.render_into(
            "first https://a.com[A]\n",
            buffer,
            note_id="n1",
        )
        # Count anonymous tags in the table after first render.

        def count_anonymous_tags(t: Gtk.TextTagTable) -> int:
            collected: list[Gtk.TextTag] = []
            t.foreach(lambda tag, _data: collected.append(tag), None)
            return sum(
                1
                for tag in collected
                if tag.get_property("name") is None
            )

        first = count_anonymous_tags(table)
        self.assertEqual(first, 1)
        # Second render: same number of links → same count, NOT 2.
        renderer.render_into(
            "second https://b.com[B]\n",
            buffer,
            note_id="n2",
        )
        second = count_anonymous_tags(table)
        self.assertEqual(second, 1, "stale link tags accumulated")
        # And the URL recoverable from the new display range is the
        # new URL — confirming the old anon tag is gone, not aliased.
        text = _full_text(buffer)
        offset = text.index("B")
        tags = buffer.get_iter_at_offset(offset).get_tags()
        self.assertEqual(renderer.url_for_tags(list(tags)), "https://b.com")

    def test_url_for_tags_with_unrelated_tag_returns_none(self) -> None:
        # Sanity-check: passing a list that contains a non-link tag
        # (e.g. just BOLD) returns None rather than raising.
        renderer, _buffer, table = _build_renderer()
        bold_tag = table.lookup(TagName.BOLD.value)
        self.assertIsNone(renderer.url_for_tags([bold_tag]))


# ---------------------------------------------------------------------------
# Tables — pure layout helpers (no display, no font)
# ---------------------------------------------------------------------------


class TableColumnPixelsTests(unittest.TestCase):
    """``_table_column_pixels`` splits the column width by proportion."""

    def test_equal_two_columns_split_evenly(self) -> None:
        self.assertEqual(_table_column_pixels((1, 1), 900), (450, 450))

    def test_unequal_columns(self) -> None:
        # ``[cols="1,2"]`` over 900 px → 300, 600.
        self.assertEqual(_table_column_pixels((1, 2), 900), (300, 600))

    def test_widths_sum_to_column_width(self) -> None:
        # Cumulative rounding keeps the per-column widths summing to the
        # whole column, so the tab stops land on the column edges.
        for proportions, column_px in (
            ((1, 1, 1), 100),
            ((1, 2, 3), 901),
            ((2, 3), 777),
            ((1, 1, 1, 1, 1), 333),
        ):
            with self.subTest(proportions=proportions, column_px=column_px):
                widths = _table_column_pixels(proportions, column_px)
                self.assertEqual(sum(widths), column_px)
                self.assertEqual(len(widths), len(proportions))

    def test_non_positive_column_width_yields_zero_widths(self) -> None:
        # Before the article container is allocated the resolver can
        # return 0; the helper must not divide by zero.
        for column_px in (0, -1, -900):
            with self.subTest(column_px=column_px):
                self.assertEqual(
                    _table_column_pixels((1, 2, 3), column_px), (0, 0, 0),
                )


class TableTabStopsTests(unittest.TestCase):
    """``_table_tab_stops`` builds a pixel-positioned ``Pango.TabArray``."""

    @staticmethod
    def _locations(tabs: Pango.TabArray) -> list[int]:
        return [tabs.get_tab(i)[1] for i in range(tabs.get_size())]

    def test_stops_sit_at_cumulative_left_edges(self) -> None:
        tabs = _table_tab_stops((450, 450))
        self.assertTrue(tabs.get_positions_in_pixels())
        self.assertEqual(self._locations(tabs), [450])

    def test_three_columns_have_two_stops(self) -> None:
        tabs = _table_tab_stops((300, 300, 300))
        self.assertEqual(self._locations(tabs), [300, 600])

    def test_unequal_columns(self) -> None:
        tabs = _table_tab_stops((300, 600))
        self.assertEqual(self._locations(tabs), [300])

    def test_single_column_has_no_stops(self) -> None:
        # A one-column table emits no tab separators, so its array is
        # empty.
        tabs = _table_tab_stops((900,))
        self.assertEqual(tabs.get_size(), 0)


class TruncateCellTests(unittest.TestCase):
    """``_truncate_cell`` fits a cell to its column, ellipsising overflow.

    Driven by :func:`_fake_cell_width` (plain/monospace 9 px per char,
    bold 10 px per char) so the arithmetic is exact.
    """

    def test_within_budget_is_returned_unchanged(self) -> None:
        runs = [_CellRun(text="abc", bold=False, monospace=False, tags=())]
        # 3 chars × 9 = 27 px ≤ 100 − 8.
        self.assertEqual(_truncate_cell(runs, 100, 8, _fake_cell_width), runs)

    def test_empty_runs_return_empty(self) -> None:
        self.assertEqual(_truncate_cell([], 100, 8, _fake_cell_width), [])

    def test_exact_fit_boundary_is_not_truncated(self) -> None:
        # 10 chars × 9 = 90 px, budget = 98 − 8 = 90 → fits exactly.
        runs = [_CellRun(text="x" * 10, bold=False, monospace=False, tags=())]
        self.assertEqual(_truncate_cell(runs, 98, 8, _fake_cell_width), runs)

    def test_over_budget_is_truncated_with_ellipsis(self) -> None:
        runs = [_CellRun(text="x" * 30, bold=False, monospace=False, tags=())]
        out = _truncate_cell(runs, 100, 8, _fake_cell_width)
        text = "".join(run.text for run in out)
        self.assertTrue(text.endswith("\u2026"))
        # Prefix width + ellipsis width fits the budget (92 px); the
        # ellipsis is 9 px, so the prefix is at most 83 px = 9 chars.
        self.assertEqual(text, "x" * 9 + "\u2026")

    def test_ellipsis_width_is_accounted_for(self) -> None:
        # The fitted prefix plus the ellipsis must not exceed the budget.
        runs = [_CellRun(text="y" * 40, bold=False, monospace=False, tags=())]
        out = _truncate_cell(runs, 200, 8, _fake_cell_width)
        width = sum(_fake_cell_width(r.text, r.bold, r.monospace) for r in out)
        self.assertLessEqual(width, 200 - 8)

    def test_gutter_is_respected(self) -> None:
        # 10 chars × 9 = 90 px fits a 90 px column with no gutter, but
        # not once an 8 px gutter is reserved — so it truncates.
        runs = [_CellRun(text="z" * 10, bold=False, monospace=False, tags=())]
        self.assertEqual(_truncate_cell(runs, 90, 0, _fake_cell_width), runs)
        truncated = _truncate_cell(runs, 90, 8, _fake_cell_width)
        self.assertTrue(
            "".join(r.text for r in truncated).endswith("\u2026"),
        )

    def test_cut_inside_a_bold_run_preserves_its_formatting(self) -> None:
        # A bold run that crosses the budget is cut; the surviving prefix
        # keeps its bold width class and its tags.
        bold_tag = Gtk.TextTag.new("bold")
        runs = [
            _CellRun(text="b" * 20, bold=True, monospace=False, tags=(bold_tag,)),
        ]
        out = _truncate_cell(runs, 100, 8, _fake_cell_width)
        # First run is the cut bold prefix; last run is the plain ellipsis.
        self.assertTrue(out[0].bold)
        self.assertEqual(out[0].tags, (bold_tag,))
        self.assertEqual(out[-1].text, "\u2026")
        self.assertFalse(out[-1].bold)
        self.assertEqual(out[-1].tags, ())

    def test_cut_inside_a_link_keeps_target_on_prefix(self) -> None:
        # A link cut mid-run keeps its (URL) tag on the surviving
        # characters so the truncated label is still clickable.
        link_tag = Gtk.TextTag.new("link")
        url_tag = Gtk.TextTag.new(None)
        runs = [
            _CellRun(
                text="clickme " * 5,
                bold=False,
                monospace=False,
                tags=(link_tag, url_tag),
            ),
        ]
        out = _truncate_cell(runs, 100, 8, _fake_cell_width)
        self.assertEqual(out[0].tags, (link_tag, url_tag))
        self.assertEqual(out[-1].text, "\u2026")


# ---------------------------------------------------------------------------
# Tables — buffer rendering (tab-array rows)
# ---------------------------------------------------------------------------


def _line_text(buffer: Gtk.TextBuffer, line_no: int) -> str:
    """Return the text of one logical line, including its trailing \\n."""
    ok, start = buffer.get_iter_at_line(line_no)
    assert ok, f"line {line_no} should exist"
    if line_no + 1 < buffer.get_line_count():
        _ok, end = buffer.get_iter_at_line(line_no + 1)
    else:
        end = buffer.get_end_iter()
    text: str = buffer.get_text(start, end, False)
    return text


def _line_tag_names(buffer: Gtk.TextBuffer, line_no: int) -> set[str]:
    """Names of the tags carried by a line's first iter (anon → '')."""
    ok, start = buffer.get_iter_at_line(line_no)
    assert ok, f"line {line_no} should exist"
    return {t.get_property("name") or "" for t in start.get_tags()}


def _tab_array_on_line(
    buffer: Gtk.TextBuffer, line_no: int,
) -> Pango.TabArray | None:
    """Return the ``Pango.TabArray`` carried by the line's tab tag, if any."""
    ok, start = buffer.get_iter_at_line(line_no)
    assert ok, f"line {line_no} should exist"
    for tag in start.get_tags():
        tabs = tag.get_property("tabs")
        if tabs is not None:
            return tabs
    return None


@unittest.skipUnless(_display_available(), "no GDK display")
class TabArrayTableRenderingTests(unittest.TestCase):
    """Tables render as tab-separated buffer text, not a child widget."""

    def test_no_child_anchor_is_created(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("|===\n|a|b\n|c|d\n|===\n", buffer, note_id="n1")
        self.assertEqual(_anchor_offsets(buffer), [])

    def test_row_is_one_line_of_tab_separated_cells(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("|===\n|a|b\n|c|d\n|===\n", buffer, note_id="n1")
        # Two rows, each one logical line of ``cell \t cell``.
        self.assertEqual(_line_text(buffer, 0), "a\tb\n")
        self.assertEqual(_line_text(buffer, 1).rstrip("\n"), "c\td")

    def test_header_row_carries_header_tag_and_bold(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("|===\n|H1|H2\n|d1|d2\n|===\n", buffer, note_id="n1")
        header_tags = _line_tag_names(buffer, 0)
        self.assertIn(TagName.TABLE_HEADER.value, header_tags)
        self.assertNotIn(TagName.TABLE_ROW.value, header_tags)
        self.assertIn(TagName.BOLD.value, header_tags)

    def test_data_rows_carry_row_tag_without_header_or_bold(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("|===\n|H1|H2\n|d1|d2\n|===\n", buffer, note_id="n1")
        data_tags = _line_tag_names(buffer, 1)
        self.assertIn(TagName.TABLE_ROW.value, data_tags)
        self.assertNotIn(TagName.TABLE_HEADER.value, data_tags)
        self.assertNotIn(TagName.BOLD.value, data_tags)

    def test_row_tags_set_wrap_mode_none(self) -> None:
        # ``wrap-mode = NONE`` keeps a row on one line so its tab-array
        # column alignment holds. It lives on the named row/header tags.
        _renderer, _buffer, table = _build_renderer()
        for name in (TagName.TABLE_ROW, TagName.TABLE_HEADER):
            tag = table.lookup(name.value)
            self.assertEqual(
                tag.get_property("wrap-mode"), Gtk.WrapMode.NONE,
            )

    def test_row_carries_tab_array_with_expected_pixel_stops(self) -> None:
        # Two equal columns over a 900 px column → one tab stop at 450.
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 900)
        renderer.render_into("|===\n|a|b\n|===\n", buffer, note_id="n1")
        tabs = _tab_array_on_line(buffer, 0)
        self.assertIsNotNone(tabs)
        assert tabs is not None
        self.assertTrue(tabs.get_positions_in_pixels())
        self.assertEqual(tabs.get_size(), 1)
        self.assertEqual(tabs.get_tab(0)[1], 450)

    def test_cols_directive_sets_proportional_tab_stops(self) -> None:
        # ``[cols="1,2"]`` over 900 px → tab stop at 300.
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 900)
        renderer.render_into(
            "[cols=\"1,2\"]\n|===\n|a|b\n|===\n", buffer, note_id="n1",
        )
        tabs = _tab_array_on_line(buffer, 0)
        assert tabs is not None
        self.assertEqual(tabs.get_tab(0)[1], 300)

    def test_within_budget_cell_is_not_truncated(self) -> None:
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 900)
        renderer.render_into("|===\n|short\n|===\n", buffer, note_id="n1")
        self.assertEqual(_line_text(buffer, 0).rstrip("\n"), "short")

    def test_over_budget_cell_is_truncated_with_ellipsis(self) -> None:
        # A narrow column (90 px per column) forces the long cell to
        # truncate; the fake measurer makes the cut deterministic.
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 90)
        renderer.render_into(
            "|===\n|abcdefghijklmnopqrstuvwxyz\n|===\n", buffer, note_id="n1",
        )
        text = _line_text(buffer, 0).rstrip("\n")
        self.assertTrue(text.endswith("\u2026"))
        self.assertLess(len(text), len("abcdefghijklmnopqrstuvwxyz"))

    def test_cell_truncation_reserves_two_hpadding(self) -> None:
        # The renderer reserves ``2 × TABLE_CELL_HPADDING_PX`` as each
        # cell's right padding (the symmetric partner of the row tag's
        # left-margin text inset). A cell sized to leave only a token
        # 8 px of slack beyond it therefore truncates, because the
        # reserved ``2 × hpadding`` (≥ 8) exceeds that slack — pinning
        # that the renderer passes the larger reservation, not a small
        # gutter. The counterfactual (the same runs under an 8 px
        # reservation) is asserted directly so the straddle is explicit.
        chars = 12
        cell_px = chars * _FAKE_CHAR_PX
        column = cell_px + 8  # only 8 px of slack beyond the cell
        runs = [_CellRun(text="x" * chars, bold=False, monospace=False, tags=())]
        # Under a token 8 px reservation the cell fits unchanged …
        self.assertEqual(
            _truncate_cell(runs, column, 8, _fake_cell_width), runs,
        )
        # … but the renderer reserves 2 × hpadding, which is larger, so
        # the rendered cell is truncated.
        self.assertGreater(2 * TABLE_CELL_HPADDING_PX, 8)
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: column)
        renderer.render_into(
            f"|===\n|{'x' * chars}\n|===\n", buffer, note_id="n1",
        )
        text = _line_text(buffer, 0).rstrip("\n")
        self.assertTrue(text.endswith("\u2026"))
        self.assertLess(len(text), chars)

    def test_empty_cell_still_emits_its_tab(self) -> None:
        # A row whose first cell is empty keeps its separator so the
        # second cell still lands on its tab stop. A second row keeps the
        # header line off the buffer's end, so its trailing newline is
        # not stripped.
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 900)
        renderer.render_into("|===\n| |b\n|c|d\n|===\n", buffer, note_id="n1")
        self.assertEqual(_line_text(buffer, 0), "\tb\n")

    def test_cell_link_target_is_recoverable(self) -> None:
        # A link in a cell is selectable buffer text whose URL the
        # renderer can resolve from the tags at the link's offset.
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 900)
        renderer.render_into(
            "|===\n|https://example.com[label]\n|===\n", buffer, note_id="n1",
        )
        text = _full_text(buffer)
        offset = text.index("label")
        tags = buffer.get_iter_at_offset(offset).get_tags()
        self.assertEqual(renderer.url_for_tags(tags), "https://example.com")
        self.assertIn(TagName.LINK.value, _tag_names_at(buffer, offset))

    def test_three_column_table_has_two_tab_stops(self) -> None:
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 900)
        renderer.render_into(
            "[cols=\"1,1,1\"]\n|===\n|A|B|C\n|x|y|z\n|===\n",
            buffer,
            note_id="n1",
        )
        # Header row text has two tab separators.
        self.assertEqual(_line_text(buffer, 0), "A\tB\tC\n")
        tabs = _tab_array_on_line(buffer, 0)
        assert tabs is not None
        self.assertEqual(tabs.get_size(), 2)
        self.assertEqual(tabs.get_tab(0)[1], 300)
        self.assertEqual(tabs.get_tab(1)[1], 600)

    def test_per_table_tab_tags_do_not_accumulate(self) -> None:
        # Each render sweeps the previous table's anonymous tab tag, so
        # rendering the same table twice leaves exactly one.
        renderer, buffer, table = _build_renderer()
        renderer.render_into("|===\n|a|b\n|===\n", buffer, note_id="n1")
        self.assertEqual(_anonymous_tag_count(table), 1)
        renderer.render_into("|===\n|a|b\n|===\n", buffer, note_id="n1")
        self.assertEqual(_anonymous_tag_count(table), 1)


# ---------------------------------------------------------------------------
# Admonitions (step 15)
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class AdmonitionRenderingTests(unittest.TestCase):
    """Admonitions render as two tinted paragraphs (label + body).

    The plan moved admonitions out of an anchored ``Gtk.Frame`` widget
    and into the buffer itself. The kind name (``NOTE`` / ``TIP`` /
    …) sits on its own line carrying the per-kind label paragraph
    tag plus a character-level kind tag for the bold + accent
    foreground. Each body paragraph carries the per-kind body
    paragraph tag. Both paragraph tags share the same tint colour so
    the block reads as one rectangle.

    Single-line and block forms converge in the AST, so the renderer
    has one code path — these tests cover both source forms.
    """

    def test_single_line_admonition_does_not_attach_a_widget(self) -> None:
        # No widgets for admonitions — the whole block is in-buffer.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "NOTE: hello\n",
            buffer,
            note_id="n1",
        )
        self.assertEqual(_anchor_offsets(buffer), [])

    def test_single_line_admonition_buffer_contains_kind_and_body(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("NOTE: hello\n", buffer, note_id="n1")
        text = _full_text(buffer)
        # Both the kind label and the body prose appear in the buffer.
        self.assertIn("NOTE", text)
        self.assertIn("hello", text)
        # The kind label is on its own line, immediately preceding
        # the body. Specifically, "NOTE\nhello" must appear as a
        # substring — the label's terminating newline is what creates
        # the paragraph break between the two parts.
        self.assertIn("NOTE\nhello", text)

    def test_block_admonition_buffer_contains_kind_and_body(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "[NOTE]\n====\nbody\n====\n",
            buffer,
            note_id="n1",
        )
        self.assertIn("NOTE\nbody", _full_text(buffer))

    def test_label_paragraph_carries_per_kind_label_tag(self) -> None:
        # Every per-kind label paragraph tag is exhaustive over
        # AdmonitionKind. Iterate the kinds and assert the right tag
        # is applied to the kind-label range.
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                renderer, buffer, _ = _build_renderer()
                renderer.render_into(
                    f"{kind.value}: hello\n",
                    buffer,
                    note_id="n1",
                )
                text = _full_text(buffer)
                start = text.index(kind.value)
                tags_at_label = _tag_names_at(buffer, start)
                self.assertIn(
                    admonition_label_tag_name(kind).value,
                    tags_at_label,
                )

    def test_kind_text_carries_per_kind_kind_character_tag(self) -> None:
        # The kind-character tag (bold + accent foreground) applies
        # to the kind text itself but not to its terminating newline.
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                renderer, buffer, _ = _build_renderer()
                renderer.render_into(
                    f"{kind.value}: x\n",
                    buffer,
                    note_id="n1",
                )
                text = _full_text(buffer)
                start = text.index(kind.value)
                # Each character of the kind text bears the kind tag.
                for offset in range(start, start + len(kind.value)):
                    tags = _tag_names_at(buffer, offset)
                    self.assertIn(
                        admonition_kind_tag_name(kind).value,
                        tags,
                        f"kind tag missing at offset {offset} for {kind.value}",
                    )

    def test_body_paragraph_carries_per_kind_body_tag(self) -> None:
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                renderer, buffer, _ = _build_renderer()
                renderer.render_into(
                    f"{kind.value}: body text\n",
                    buffer,
                    note_id="n1",
                )
                text = _full_text(buffer)
                start = text.index("body text")
                tags_at_body = _tag_names_at(buffer, start)
                self.assertIn(
                    admonition_body_tag_name(kind).value,
                    tags_at_body,
                )

    def test_body_does_not_carry_kind_character_tag(self) -> None:
        # The character-level kind tag (bold + foreground) is scoped
        # to the kind label only. Body prose composes its own
        # inline formatting via the existing bold / italic / etc.
        # tags.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("NOTE: hello\n", buffer, note_id="n1")
        text = _full_text(buffer)
        body_offset = text.index("hello")
        tags = _tag_names_at(buffer, body_offset)
        self.assertNotIn(TagName.ADMONITION_NOTE_KIND.value, tags)

    def test_body_inline_formatting_composes_with_body_tag(self) -> None:
        # A bold span inside the body must keep its BOLD tag on top
        # of the body paragraph tag — they layer cleanly.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "NOTE: see *bold* text\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        bold_offset = text.index("bold")
        tags = _tag_names_at(buffer, bold_offset)
        self.assertIn(TagName.BOLD.value, tags)
        self.assertIn(TagName.ADMONITION_NOTE_BODY.value, tags)

    def test_two_body_paragraphs_each_tagged(self) -> None:
        # A two-paragraph admonition body produces two paragraph
        # spans, each carrying the body tag.
        src = "[NOTE]\n====\nfirst\n\nsecond\n====\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        for substring in ("first", "second"):
            with self.subTest(substring=substring):
                offset = text.index(substring)
                tags = _tag_names_at(buffer, offset)
                self.assertIn(TagName.ADMONITION_NOTE_BODY.value, tags)

    def test_empty_admonition_body_emits_only_kind_label(self) -> None:
        # ``[NOTE]\n====\n====\n`` parses to a kind-only admonition.
        # The renderer emits just the label paragraph plus the
        # block-separator newline — no body paragraph is created.
        src = "[NOTE]\n====\n====\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        self.assertIn("NOTE", text)
        # The label range carries the label tag.
        label_offset = text.index("NOTE")
        tags = _tag_names_at(buffer, label_offset)
        self.assertIn(TagName.ADMONITION_NOTE_LABEL.value, tags)
        # No range carries the BODY tag — the kind-only block has no
        # body paragraph at all.
        body_ranges = _ranges_with_tag(
            buffer,
            TagName.ADMONITION_NOTE_BODY.value,
        )
        self.assertEqual(body_ranges, [])


# ---------------------------------------------------------------------------
# Blockquotes (step 15)
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class BlockquoteRenderingTests(unittest.TestCase):
    """Blockquotes render as italic indented paragraphs + optional attribution.

    Body paragraphs carry :data:`TagName.BLOCKQUOTE_BODY` (tint +
    indent) plus :data:`TagName.ITALIC` for the italic style. An
    optional attribution paragraph carries
    :data:`TagName.BLOCKQUOTE_ATTRIBUTION` (right-aligned, smaller
    scale).
    """

    def test_unattributed_blockquote_does_not_attach_a_widget(self) -> None:
        # The whole block is in-buffer — no widget escape, no anchor.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "____\nA quote.\n____\n",
            buffer,
            note_id="n1",
        )
        self.assertEqual(_anchor_offsets(buffer), [])

    def test_blockquote_body_text_is_in_buffer(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "____\nA quote.\n____\n",
            buffer,
            note_id="n1",
        )
        self.assertIn("A quote.", _full_text(buffer))

    def test_body_paragraph_carries_body_and_italic_tags(self) -> None:
        # The italic style composes via the shared ITALIC tag, layered
        # on top of the body paragraph tag — so every body char must
        # bear both.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "____\nthe quote\n____\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        start = text.index("the quote")
        for offset in range(start, start + len("the quote")):
            tags = _tag_names_at(buffer, offset)
            self.assertIn(TagName.BLOCKQUOTE_BODY.value, tags)
            self.assertIn(TagName.ITALIC.value, tags)

    def test_no_attribution_text_when_directive_absent(self) -> None:
        # Without a ``[quote, …]`` directive there is no attribution
        # paragraph; the attribution tag is therefore applied to no
        # range at all.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "____\nthe quote\n____\n",
            buffer,
            note_id="n1",
        )
        attribution_ranges = _ranges_with_tag(
            buffer,
            TagName.BLOCKQUOTE_ATTRIBUTION.value,
        )
        self.assertEqual(attribution_ranges, [])

    def test_attribution_text_when_author_only(self) -> None:
        src = "[quote, Mark Twain]\n____\nq\n____\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        self.assertIn("— Mark Twain", text)
        # The attribution text bears the attribution paragraph tag.
        start = text.index("— Mark Twain")
        tags = _tag_names_at(buffer, start)
        self.assertIn(TagName.BLOCKQUOTE_ATTRIBUTION.value, tags)

    def test_attribution_text_when_author_and_source(self) -> None:
        src = "[quote, Mark Twain, Notebook]\n____\nq\n____\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        self.assertIn("— Mark Twain, Notebook", text)

    def test_bare_quote_directive_yields_no_attribution(self) -> None:
        # ``[quote]`` (no attribution fields) — same as no directive.
        src = "[quote]\n____\nq\n____\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        attribution_ranges = _ranges_with_tag(
            buffer,
            TagName.BLOCKQUOTE_ATTRIBUTION.value,
        )
        self.assertEqual(attribution_ranges, [])

    def test_attribution_does_not_carry_italic_tag(self) -> None:
        # The body is italic; the attribution is not. The attribution
        # paragraph tag must be applied without layering ITALIC on top.
        src = "[quote, Mark Twain]\n____\nq\n____\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        attribution_start = text.index("— Mark Twain")
        tags = _tag_names_at(buffer, attribution_start)
        self.assertNotIn(TagName.ITALIC.value, tags)
        self.assertNotIn(TagName.BLOCKQUOTE_BODY.value, tags)

    def test_two_body_paragraphs_each_tagged(self) -> None:
        src = "____\nfirst\n\nsecond\n____\n"
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(src, buffer, note_id="n1")
        text = _full_text(buffer)
        for substring in ("first", "second"):
            with self.subTest(substring=substring):
                offset = text.index(substring)
                tags = _tag_names_at(buffer, offset)
                self.assertIn(TagName.BLOCKQUOTE_BODY.value, tags)
                self.assertIn(TagName.ITALIC.value, tags)

    def test_body_inline_formatting_composes_with_body_tag(self) -> None:
        # A bold span inside the body must keep its BOLD tag on top
        # of the body paragraph tag and the italic tag — three tags
        # layered cleanly on the same range.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "____\nuse *bold* text\n____\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        bold_offset = text.index("bold")
        tags = _tag_names_at(buffer, bold_offset)
        self.assertIn(TagName.BOLD.value, tags)
        self.assertIn(TagName.BLOCKQUOTE_BODY.value, tags)
        self.assertIn(TagName.ITALIC.value, tags)


# ---------------------------------------------------------------------------
# Heterogeneous document composition
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class DocumentCompositionRenderingTests(unittest.TestCase):
    """The renderer walks a heterogeneous document in a single pass.

    Every block kind has its own focused rendering test above; this
    class is the one integration check that they *coexist* — a title, a
    discarded header attribute run, a top-level table, two sections, a
    list, and a multi-line admonition all render into one buffer without
    raising and with each block's text present. It replaces a former
    real-world note fixture: a hand-written source keeps the test about
    the renderer rather than about any particular note.
    """

    _SOURCE: str = (
        "= Doc Title\n"
        ":author: Me\n"
        "\n"
        "A lead paragraph.\n"
        "\n"
        '[cols="3,1"]\n'
        "|===\n"
        "|Ingredient |Grams\n"
        "|Flour |400\n"
        "|===\n"
        "\n"
        "== Notes\n"
        "\n"
        "* first point\n"
        "* second point\n"
        "\n"
        "NOTE: a hint that wraps\n"
        "onto a second line.\n"
        "\n"
        "== Result\n"
        "\n"
        "Final remark.\n"
    )

    def test_renders_every_block_into_one_non_empty_buffer(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            self._SOURCE,
            buffer,
            note_id="composition",
        )
        text = _full_text(buffer)
        self.assertTrue(text)
        # Title, both section headings, list text, and the admonition
        # body all reach the buffer.
        for fragment in (
            "Doc Title",
            "Notes",
            "Result",
            "first point",
            "second point",
            "Final remark.",
        ):
            self.assertIn(fragment, text)
        # The table is in-buffer text too — its cells reach the buffer
        # and no block kind escapes to an anchored widget.
        self.assertIn("Ingredient", text)
        self.assertIn("Flour", text)
        self.assertEqual(_anchor_offsets(buffer), [])

    def test_multi_line_admonition_joins_onto_one_logical_line(self) -> None:
        # The NOTE body wraps over two source lines; it must render as a
        # single soft-broken line, not with a literal newline embedded.
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(self._SOURCE, buffer, note_id="composition")
        text = _full_text(buffer)
        self.assertIn("a hint that wraps onto a second line.", text)
        self.assertNotIn("wraps\nonto", text)


@unittest.skipUnless(_display_available(), "no GDK display")
class SoftBreakRenderingTests(unittest.TestCase):
    """An in-paragraph source newline renders as a single space, not a
    hard break (the soft-line-break fix).
    """

    def test_soft_break_renders_as_single_space(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into("= D\n\nalpha\nbeta\n", buffer, note_id="n1")
        text = _full_text(buffer)
        self.assertIn("alpha beta", text)
        self.assertNotIn("alpha\nbeta", text)

    def test_admonition_continuation_renders_on_one_logical_line(self) -> None:
        renderer, buffer, _ = _build_renderer()
        renderer.render_into(
            "= D\n\nNOTE: first part\nsecond part\n",
            buffer,
            note_id="n1",
        )
        text = _full_text(buffer)
        self.assertIn("first part second part", text)
        self.assertNotIn("first part\nsecond part", text)


# ---------------------------------------------------------------------------
# post_title_hook
# ---------------------------------------------------------------------------


_TITLE_TRAILING_LEN: int = 1
"""Length, in characters, of the title's single trailing newline.

The titled document emits ``HeadingTrailing.SINGLE_NEWLINE`` after the
title text, so the post-title insertion point sits exactly this many
characters beyond the title text.
"""


_SENTINEL: str = "<<META>>"
"""A marker the test hook inserts at the post-title insertion point so
the tests can locate where the hook ran relative to the title and body.
Chosen to be a substring that never occurs in the surrounding rendered
text.
"""


@unittest.skipUnless(_display_available(), "no GDK display")
class PostTitleHookTests(unittest.TestCase):
    """``post_title_hook`` fires exactly once per render with the
    ``buffer`` positioned (at its end iter) at the title/body boundary
    (or at buffer-start when the note has no title). The hook inserts
    text there; the renderer drops a block separator and the body
    after it. No child anchor is created on this path.
    """

    def test_hook_fires_once_with_the_buffer(self) -> None:
        renderer, buffer, _ = _build_renderer()
        calls: list[Gtk.TextBuffer] = []

        renderer.render_into(
            "= Welcome\n\nfirst.\n\nsecond.\n",
            buffer,
            note_id="n1",
            post_title_hook=calls.append,
        )

        self.assertEqual(len(calls), 1)
        # The hook receives the buffer being rendered into, not a copy.
        self.assertIs(calls[0], buffer)

    def test_hook_fires_once_when_no_title(self) -> None:
        renderer, buffer, _ = _build_renderer()
        calls: list[Gtk.TextBuffer] = []

        renderer.render_into(
            "just a body paragraph.\n",
            buffer,
            note_id="n1",
            post_title_hook=calls.append,
        )

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], buffer)

    def test_inserted_text_lands_after_title_and_before_body(self) -> None:
        # The hook inserts at the buffer end iter, which sits one
        # newline past the title text. After the renderer drops the
        # block separator and body, the sentinel must appear between
        # the title and the body.
        renderer, buffer, _ = _build_renderer()

        def hook(buf: Gtk.TextBuffer) -> None:
            buf.insert(buf.get_end_iter(), _SENTINEL)

        renderer.render_into(
            "= Welcome\n\nbody.\n",
            buffer,
            note_id="n1",
            post_title_hook=hook,
        )

        text = _full_text(buffer)
        title_at = text.index("Welcome")
        sentinel_at = text.index(_SENTINEL)
        body_at = text.index("body.")
        self.assertLess(title_at, sentinel_at)
        self.assertLess(sentinel_at, body_at)
        # The sentinel hugs the title — exactly the title's single
        # trailing newline separates them.
        self.assertEqual(
            sentinel_at, title_at + len("Welcome") + _TITLE_TRAILING_LEN,
        )

    def test_inserted_text_lands_at_start_when_no_title(self) -> None:
        renderer, buffer, _ = _build_renderer()

        def hook(buf: Gtk.TextBuffer) -> None:
            buf.insert(buf.get_end_iter(), _SENTINEL)

        renderer.render_into(
            "body only.\n",
            buffer,
            note_id="n1",
            post_title_hook=hook,
        )

        text = _full_text(buffer)
        # No title: the insertion point is buffer-start, so the sentinel
        # opens the buffer and the body follows a block separator below.
        self.assertTrue(text.startswith(_SENTINEL))
        self.assertLess(text.index(_SENTINEL), text.index("body only."))

    def test_no_child_anchor_created_on_post_title_path(self) -> None:
        # The metadata hook inserts plain text, not a widget — the
        # renderer must create no child anchor on this path. (Tables
        # still anchor, but there are none in this document.)
        renderer, buffer, _ = _build_renderer()

        def hook(buf: Gtk.TextBuffer) -> None:
            buf.insert(buf.get_end_iter(), _SENTINEL)

        renderer.render_into(
            "= Welcome\n\nbody.\n",
            buffer,
            note_id="n1",
            post_title_hook=hook,
        )

        self.assertEqual(_anchor_offsets(buffer), [])

    def test_hook_not_called_when_parse_fails(self) -> None:
        renderer, buffer, _ = _build_renderer()
        calls: list[Gtk.TextBuffer] = []

        # An unterminated monospace span — guaranteed to raise
        # ``ParseError`` during ``parse(source)`` at the top of
        # ``render_into``, before any buffer mutation.
        with self.assertRaises(ParseError):
            renderer.render_into(
                "an `unterminated monospace span\n",
                buffer,
                note_id="n1",
                post_title_hook=calls.append,
            )

        self.assertEqual(calls, [])

    def test_hook_omitted_runs_clean(self) -> None:
        renderer, buffer, _ = _build_renderer()

        # Omitting the kwarg must be a no-op: no exception, no
        # stray anchor inserted into the buffer.
        renderer.render_into(
            "= Welcome\n\nbody.\n",
            buffer,
            note_id="n1",
        )

        self.assertEqual(_anchor_offsets(buffer), [])


if __name__ == "__main__":
    unittest.main()
