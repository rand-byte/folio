"""Tests for :mod:`ui.note_render.textbuffer_renderer`."""

from __future__ import annotations

import struct
import unittest
import zlib
from collections.abc import Callable
from typing import cast

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from asciidoc.ast import (
    Bold,
    Italic,
    Monospace,
    SoftBreak,
    Text,
)
from ui.note_render.tag_table import (
    TagName,
    admonition_body_tag_name,
    admonition_kind_tag_name,
    admonition_label_tag_name,
    build_tag_table,
)
from ui.note_render.textbuffer_renderer import (
    TextBufferRenderer,
    _inlines_to_pango_markup,
    _max_chars_per_column,
    _PlaceholderImagePaintable,
    _ScaledImagePaintable,
)
from config.defaults import TARGET_CHARS_PER_LINE
from enums import AdmonitionKind
from models.parse_error import ParseError


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


def _collect(
    attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]],
) -> Callable[[Gtk.TextChildAnchor, Gtk.Widget], None]:
    """Return an attach_widget closure that records every (anchor, widget)."""

    def attach(anchor: Gtk.TextChildAnchor, widget: Gtk.Widget) -> None:
        attached.append((anchor, widget))

    return attach


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
    tag_table: Gtk.TextTagTable | None = None,
) -> tuple[TextBufferRenderer, Gtk.TextBuffer, Gtk.TextTagTable]:
    """Construct a renderer and a buffer wired to a fresh tag table."""
    table = tag_table if tag_table is not None else build_tag_table(char_width_px=9)
    renderer = TextBufferRenderer(
        image_bytes_for=image_bytes_for if image_bytes_for is not None else (lambda _f: _PNG_1X1),
        column_width_px=column_width_px if column_width_px is not None else (lambda: 800),
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
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        # No child anchors: code blocks no longer escape to widget land.
        self.assertEqual(attached, [])
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
        # The attach_widget callback must not fire even once.
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\n----\nx\n----\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        self.assertEqual(attached, [])


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
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\nimage::cat.png[]\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        self.assertEqual(attached, [])

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

    def test_re_rendering_drops_previous_anchors(self) -> None:
        # Two render passes on the same buffer must not accumulate
        # anchors — the second render starts from a clean buffer. The
        # source uses a table because tables are the one remaining
        # block kind that produces a child anchor; the other former
        # anchor-producers (admonition, blockquote, code block, image)
        # now render inline.
        renderer, buffer, _ = _build_renderer()
        attached_first: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\n|===\n|a|b\n|===\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached_first),
        )
        attached_second: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\nNo table here.\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached_second),
        )
        self.assertEqual(len(attached_first), 1)
        self.assertEqual(len(attached_second), 0)
        # The buffer now has no anchors at all.
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
# Tables (step 14) — pure helper tests
# ---------------------------------------------------------------------------


class MaxCharsPerColumnTests(unittest.TestCase):
    """``_max_chars_per_column`` distributes the character budget."""

    def test_equal_proportions_split_budget_evenly(self) -> None:
        # Two columns at proportion (1, 1) each get half the budget.
        # The column_width_px factor cancels, so the result is
        # independent of the absolute pixel width.
        result = _max_chars_per_column((1, 1), 800)
        expected_each = round(TARGET_CHARS_PER_LINE / 2)
        self.assertEqual(result, (expected_each, expected_each))

    def test_unequal_proportions(self) -> None:
        # ``[cols="1,2"]`` over a 66-char budget → 22, 44.
        result = _max_chars_per_column((1, 2), 800)
        self.assertEqual(
            result,
            (
                round(TARGET_CHARS_PER_LINE * 1 / 3),
                round(TARGET_CHARS_PER_LINE * 2 / 3),
            ),
        )

    def test_three_equal_columns(self) -> None:
        result = _max_chars_per_column((1, 1, 1), 800)
        per_col = round(TARGET_CHARS_PER_LINE / 3)
        self.assertEqual(result, (per_col, per_col, per_col))

    def test_result_is_invariant_under_column_width_px(self) -> None:
        # The derivation cancels ``column_width_px`` algebraically;
        # this test pins that contract so a future, font-aware
        # derivation that breaks invariance does it intentionally.
        for width in (200, 800, 1600, 4000):
            with self.subTest(column_width_px=width):
                self.assertEqual(
                    _max_chars_per_column((1, 2), width),
                    _max_chars_per_column((1, 2), 800),
                )

    def test_zero_or_negative_column_width_yields_minimum_widths(self) -> None:
        # Before the article container has been allocated the resolver
        # can return 0 — the function should not divide by zero.
        for width in (0, -1, -800):
            with self.subTest(column_width_px=width):
                result = _max_chars_per_column((1, 2, 3), width)
                self.assertEqual(result, (1, 1, 1))

    def test_zero_clamped_to_at_least_one(self) -> None:
        # Pathological case: a column whose proportion is negligible
        # against a huge total. The function clamps to a minimum of 1.
        # (The parser rejects zero/negative values, but the helper
        # itself stays defensive — defence in depth.)
        result = _max_chars_per_column((1,) + (10000,) * 100, 800)
        self.assertGreaterEqual(result[0], 1)


class InlinesToPangoMarkupTests(unittest.TestCase):
    """The inline-to-Pango converter handles every inline kind."""

    def test_plain_text_is_escaped(self) -> None:
        markup = _inlines_to_pango_markup((Text(content="a & b", source_line=1),))
        # ``&`` becomes ``&amp;`` — Pango requires escaped entities.
        self.assertEqual(markup, "a &amp; b")

    def test_text_with_angle_brackets_is_escaped(self) -> None:
        markup = _inlines_to_pango_markup(
            (Text(content="<x>", source_line=1),)
        )
        self.assertEqual(markup, "&lt;x&gt;")

    def test_bold_wraps_content(self) -> None:
        markup = _inlines_to_pango_markup(
            (Bold(children=(Text(content="x", source_line=1),), source_line=1),)
        )
        self.assertEqual(markup, "<b>x</b>")

    def test_italic_wraps_content(self) -> None:
        markup = _inlines_to_pango_markup(
            (Italic(children=(Text(content="x", source_line=1),), source_line=1),)
        )
        self.assertEqual(markup, "<i>x</i>")

    def test_monospace_wraps_with_tt_and_escapes_body(self) -> None:
        # Monospace body is a literal :class:`str` — the converter
        # must escape it (no HTML interpretation) and wrap in <tt>.
        markup = _inlines_to_pango_markup(
            (Monospace(content="a&b<c>", source_line=1),)
        )
        self.assertEqual(markup, "<tt>a&amp;b&lt;c&gt;</tt>")

    def test_bold_flag_wraps_whole_result(self) -> None:
        # ``bold=True`` adds an outer <b> so header cells render bold.
        markup = _inlines_to_pango_markup(
            (Text(content="header", source_line=1),),
            bold=True,
        )
        self.assertEqual(markup, "<b>header</b>")

    def test_bold_flag_preserves_nested_inlines(self) -> None:
        markup = _inlines_to_pango_markup(
            (
                Text(content="x ", source_line=1),
                Italic(
                    children=(Text(content="y", source_line=1),),
                    source_line=1,
                ),
            ),
            bold=True,
        )
        self.assertEqual(markup, "<b>x <i>y</i></b>")

    def test_empty_inlines_produce_empty_string(self) -> None:
        # An empty cell (e.g. trailing ``|`` in a row) has no inlines.
        self.assertEqual(_inlines_to_pango_markup(()), "")
        # With bold flag, still wraps in <b> — Pango renders <b></b>
        # as nothing, which is the right visual outcome.
        self.assertEqual(_inlines_to_pango_markup((), bold=True), "<b></b>")


# ---------------------------------------------------------------------------
# Tables (step 14) — widget rendering
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class TableRenderingTests(unittest.TestCase):
    """Tables produce a :class:`Gtk.Frame` containing a :class:`Gtk.Grid`."""

    def test_table_attaches_a_frame_widget(self) -> None:
        src = "= D\n\n|===\n|a|b\n|c|d\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        self.assertEqual(len(attached), 1)
        anchor, widget = attached[0]
        self.assertIsInstance(anchor, Gtk.TextChildAnchor)
        self.assertIsInstance(widget, Gtk.Frame)

    def test_frame_contains_a_grid_with_one_label_per_cell(self) -> None:
        src = "|===\n|a|b\n|c|d\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        frame = cast(Gtk.Frame, attached[0][1])
        grid = frame.get_child()
        self.assertIsInstance(grid, Gtk.Grid)
        assert isinstance(grid, Gtk.Grid)
        # Each cell occupies a 1x1 grid slot.
        for row in range(2):
            for col in range(2):
                child = grid.get_child_at(col, row)
                self.assertIsInstance(child, Gtk.Label)

    def test_cells_carry_pango_markup_with_inline_formatting(self) -> None:
        src = "|===\n|*A*|_B_\n|`c`|d\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        frame = cast(Gtk.Frame, attached[0][1])
        grid = cast(Gtk.Grid, frame.get_child())
        # Header cells get an outer <b> from the bold flag.
        header_a = cast(Gtk.Label, grid.get_child_at(0, 0))
        header_b = cast(Gtk.Label, grid.get_child_at(1, 0))
        self.assertEqual(header_a.get_label(), "<b><b>A</b></b>")
        self.assertEqual(header_b.get_label(), "<b><i>B</i></b>")
        # Data cells: monospace and plain text.
        data_c = cast(Gtk.Label, grid.get_child_at(0, 1))
        data_d = cast(Gtk.Label, grid.get_child_at(1, 1))
        self.assertEqual(data_c.get_label(), "<tt>c</tt>")
        self.assertEqual(data_d.get_label(), "d")

    def test_cell_labels_have_wrap_true(self) -> None:
        # ``wrap = TRUE`` is the core layout invariant for table cells —
        # the renderer relies on wrapping (not horizontal scroll) to
        # fit content within the article column.
        src = "|===\n|a|b\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        grid = cast(Gtk.Grid, cast(Gtk.Frame, attached[0][1]).get_child())
        for col in range(2):
            label = cast(Gtk.Label, grid.get_child_at(col, 0))
            self.assertTrue(
                label.get_wrap(),
                f"cell at column {col} must have wrap=TRUE",
            )

    def test_cell_max_width_chars_reflects_equal_split_when_no_directive(self) -> None:
        # No ``[cols=…]`` directive → each of two columns gets
        # TARGET_CHARS_PER_LINE / 2.
        src = "|===\n|a|b\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        grid = cast(Gtk.Grid, cast(Gtk.Frame, attached[0][1]).get_child())
        expected = round(TARGET_CHARS_PER_LINE / 2)
        for col in range(2):
            label = cast(Gtk.Label, grid.get_child_at(col, 0))
            self.assertEqual(label.get_max_width_chars(), expected)

    def test_cell_max_width_chars_respects_cols_directive(self) -> None:
        # ``[cols="1,2"]`` → column 0 gets 1/3 of budget, column 1
        # gets 2/3.
        src = "[cols=\"1,2\"]\n|===\n|a|b\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        grid = cast(Gtk.Grid, cast(Gtk.Frame, attached[0][1]).get_child())
        col_0 = cast(Gtk.Label, grid.get_child_at(0, 0))
        col_1 = cast(Gtk.Label, grid.get_child_at(1, 0))
        self.assertEqual(
            col_0.get_max_width_chars(),
            round(TARGET_CHARS_PER_LINE / 3),
        )
        self.assertEqual(
            col_1.get_max_width_chars(),
            round(TARGET_CHARS_PER_LINE * 2 / 3),
        )

    def test_table_anchor_is_placed_in_outer_buffer(self) -> None:
        # Same invariant as code blocks — the anchor lives at a real
        # offset and is recoverable from the buffer.
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "|===\n|a\n|===\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        anchor = attached[0][0]
        anchor_offsets = _anchor_offsets(buffer)
        self.assertEqual(len(anchor_offsets), 1)
        located = buffer.get_iter_at_offset(anchor_offsets[0]).get_child_anchor()
        self.assertIs(located, anchor)

    def test_three_column_table_with_directive(self) -> None:
        src = "[cols=\"1,2,3\"]\n|===\n|A|B|C\n|x|y|z\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        grid = cast(Gtk.Grid, cast(Gtk.Frame, attached[0][1]).get_child())
        # Six labels: 3 cols × 2 rows.
        for row in range(2):
            for col in range(3):
                self.assertIsInstance(
                    grid.get_child_at(col, row),
                    Gtk.Label,
                )
        # Proportions sum to 6 — each column gets p/6 of the budget.
        for col, proportion in enumerate((1, 2, 3)):
            label = cast(Gtk.Label, grid.get_child_at(col, 0))
            self.assertEqual(
                label.get_max_width_chars(),
                round(TARGET_CHARS_PER_LINE * proportion / 6),
            )

    def test_only_first_row_is_styled_as_header(self) -> None:
        # Header cell markup is wrapped in <b>…</b>; data cells are not.
        src = "|===\n|H1|H2\n|d1|d2\n|x1|x2\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        grid = cast(Gtk.Grid, cast(Gtk.Frame, attached[0][1]).get_child())
        # Row 0: header — markup begins with "<b>".
        for col in range(2):
            label = cast(Gtk.Label, grid.get_child_at(col, 0))
            self.assertTrue(label.get_label().startswith("<b>"))
            self.assertTrue(label.get_label().endswith("</b>"))
        # Rows 1, 2: data — markup is bare text, no <b>.
        for row in (1, 2):
            for col in range(2):
                label = cast(Gtk.Label, grid.get_child_at(col, row))
                self.assertFalse(label.get_label().startswith("<b>"))

    def test_column_width_resolver_called_at_render_time(self) -> None:
        # The renderer reads ``column_width_px`` once per render so a
        # subsequent allocation change (next render) picks up the new
        # value. This test asserts the resolver is invoked; the
        # specific pixel value isn't part of the cell-label width
        # derivation in this implementation (we use the static
        # TARGET_CHARS_PER_LINE budget), but the resolver must still
        # be called so future implementations that scale by pixels
        # have a stable contract to extend.
        calls: list[int] = []

        def resolver() -> int:
            calls.append(1)
            return 800

        renderer, buffer, _ = _build_renderer(column_width_px=resolver)
        renderer.render_into(
            "|===\n|a|b\n|===\n",
            buffer,
            note_id="n1",
        )
        self.assertGreaterEqual(len(calls), 1)

    def test_cell_with_link_emits_anchor_markup(self) -> None:
        # A link in a cell becomes <a href="..."> markup.
        src = "|===\n|https://example.com[label]\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        grid = cast(Gtk.Grid, cast(Gtk.Frame, attached[0][1]).get_child())
        label = cast(Gtk.Label, grid.get_child_at(0, 0))
        # Header row, so wrapped in <b>. Link href present.
        self.assertIn('<a href="https://example.com">', label.get_label())
        self.assertIn("label</a>", label.get_label())

    def test_cell_label_alignment_is_top_left(self) -> None:
        # xalign=0.0, yalign=0.0 — left-aligned, top-aligned.
        src = "|===\n|a\n|===\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        grid = cast(Gtk.Grid, cast(Gtk.Frame, attached[0][1]).get_child())
        label = cast(Gtk.Label, grid.get_child_at(0, 0))
        self.assertEqual(label.get_xalign(), 0.0)
        self.assertEqual(label.get_yalign(), 0.0)

    def test_frame_size_request_matches_column_width(self) -> None:
        # Anchored children of Gtk.TextView ignore ``hexpand`` and get
        # allocated their natural width, so the renderer forces the
        # table frame's horizontal size-request to the live column
        # width. Without this, the table collapses to the natural
        # width of its cell labels instead of filling the column.
        renderer, buffer, _ = _build_renderer(column_width_px=lambda: 712)
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "|===\n|a|b\n|===\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        frame = cast(Gtk.Frame, attached[0][1])
        request_w, request_h = frame.get_size_request()
        self.assertEqual(request_w, 712)
        # Height is left to GTK's natural measurement.
        self.assertEqual(request_h, -1)


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
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "NOTE: hello\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        self.assertEqual(attached, [])
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
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "____\nA quote.\n____\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        self.assertEqual(attached, [])
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
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            self._SOURCE,
            buffer,
            note_id="composition",
            attach_widget=_collect(attached),
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
        # Of these block kinds only the table escapes to an anchored
        # widget; everything else is in-buffer text.
        self.assertEqual(len(attached), 1)

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


class SoftBreakPangoMarkupTests(unittest.TestCase):
    """The markup ladder (used for table/header-cell labels) maps a
    SoftBreak to a single space. No display required.
    """

    def test_soft_break_pango_markup_is_space(self) -> None:
        markup = _inlines_to_pango_markup(
            (Text("a", 1), SoftBreak(source_line=2), Text("b", 2))
        )
        self.assertEqual(markup, "a b")


# ---------------------------------------------------------------------------
# post_title_hook
# ---------------------------------------------------------------------------


_BLOCK_SEPARATOR_LEN: int = 2
"""Length, in characters, of the renderer's inter-block separator.

The renderer inserts ``"\\n\\n"`` after every block including the title;
the post-title anchor sits *after* that separator. Tests assert anchor
offsets relative to this length so a future change to the separator
text fans out to exactly one place.
"""


@unittest.skipUnless(_display_available(), "no GDK display")
class PostTitleHookTests(unittest.TestCase):
    """``post_title_hook`` fires exactly once per render with an anchor
    positioned at the title/body boundary (or at buffer-start when the
    note has no title). Verified through small documents whose offsets
    are easy to compute by hand.
    """

    def test_hook_fires_once_when_title_present(self) -> None:
        renderer, buffer, _ = _build_renderer()
        anchors: list[Gtk.TextChildAnchor] = []

        renderer.render_into(
            "= Welcome\n\nfirst.\n\nsecond.\n\nthird.\n",
            buffer,
            note_id="n1",
            post_title_hook=anchors.append,
        )

        self.assertEqual(len(anchors), 1)
        self.assertIsInstance(anchors[0], Gtk.TextChildAnchor)

    def test_hook_fires_once_when_no_title(self) -> None:
        renderer, buffer, _ = _build_renderer()
        anchors: list[Gtk.TextChildAnchor] = []

        renderer.render_into(
            "just a body paragraph.\n",
            buffer,
            note_id="n1",
            post_title_hook=anchors.append,
        )

        self.assertEqual(len(anchors), 1)
        self.assertIsInstance(anchors[0], Gtk.TextChildAnchor)

    def test_hook_anchor_offset_is_after_title(self) -> None:
        renderer, buffer, _ = _build_renderer()
        anchors: list[Gtk.TextChildAnchor] = []

        renderer.render_into(
            "= Welcome\n\nbody.\n",
            buffer,
            note_id="n1",
            post_title_hook=anchors.append,
        )

        # The title text plus its trailing block separator is the
        # offset the hook should receive — i.e. the anchor sits right
        # before the first body block's text.
        expected_offset = len("Welcome") + _BLOCK_SEPARATOR_LEN
        anchor_iter = self._iter_for_anchor(buffer, anchors[0])
        self.assertIsNotNone(anchor_iter)
        assert anchor_iter is not None  # for mypy/pylint
        self.assertEqual(anchor_iter.get_offset(), expected_offset)

    def test_hook_anchor_offset_is_zero_when_no_title(self) -> None:
        renderer, buffer, _ = _build_renderer()
        anchors: list[Gtk.TextChildAnchor] = []

        renderer.render_into(
            "body only.\n",
            buffer,
            note_id="n1",
            post_title_hook=anchors.append,
        )

        anchor_iter = self._iter_for_anchor(buffer, anchors[0])
        self.assertIsNotNone(anchor_iter)
        assert anchor_iter is not None  # for mypy/pylint
        self.assertEqual(anchor_iter.get_offset(), 0)

    def test_hook_not_called_when_parse_fails(self) -> None:
        renderer, buffer, _ = _build_renderer()
        anchors: list[Gtk.TextChildAnchor] = []

        # An unterminated monospace span — guaranteed to raise
        # ``ParseError`` during ``parse(source)`` at the top of
        # ``render_into``, before any buffer mutation.
        with self.assertRaises(ParseError):
            renderer.render_into(
                "an `unterminated monospace span\n",
                buffer,
                note_id="n1",
                post_title_hook=anchors.append,
            )

        self.assertEqual(anchors, [])

    def test_hook_omitted_runs_clean(self) -> None:
        renderer, buffer, _ = _build_renderer()

        # Omitting the kwarg must be a no-op: no exception, no
        # stray anchor inserted into the buffer.
        renderer.render_into(
            "= Welcome\n\nbody.\n",
            buffer,
            note_id="n1",
        )

        # Walk every character offset and confirm no
        # ``Gtk.TextChildAnchor`` is exposed by the iter — the
        # renderer only creates anchors for tables (none here) and
        # the optional post-title path.
        iterator = buffer.get_start_iter()
        while True:
            self.assertIsNone(iterator.get_child_anchor())
            if not iterator.forward_char():
                break

    @staticmethod
    def _iter_for_anchor(
        buffer: Gtk.TextBuffer,
        anchor: Gtk.TextChildAnchor,
    ) -> Gtk.TextIter | None:
        """Return the iter at which ``anchor`` is embedded, or None.

        ``Gtk.TextChildAnchor`` does not directly expose its buffer
        position — it carries one only while inserted. We walk the
        buffer once, comparing each iter's child anchor against the
        target. Linear, but the buffers in these tests are tiny.
        """
        iterator = buffer.get_start_iter()
        while True:
            if iterator.get_child_anchor() is anchor:
                return iterator
            if not iterator.forward_char():
                return None


if __name__ == "__main__":
    unittest.main()
