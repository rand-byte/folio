"""Tests for :mod:`notes_app.asciidoc.textbuffer_renderer`."""

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
from gi.repository import Gdk, Gtk  # noqa: E402

from notes_app.asciidoc.tag_table import TagName, build_tag_table
from notes_app.asciidoc.textbuffer_renderer import TextBufferRenderer
from notes_app.models.parse_error import ParseError


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
    return buffer.get_text(
        buffer.get_start_iter(),
        buffer.get_end_iter(),
        False,
    )


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


def _build_renderer(
    *,
    image_bytes_for: Callable[[str], bytes] | None = None,
    column_width_px: Callable[[], int] | None = None,
    tag_table: Gtk.TextTagTable | None = None,
) -> tuple[TextBufferRenderer, Gtk.TextBuffer, Gtk.TextTagTable]:
    """Construct a renderer and a buffer wired to a fresh tag table."""
    table = tag_table if tag_table is not None else build_tag_table()
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
    def test_code_block_attaches_a_frame_widget(self) -> None:
        src = "= D\n\n----\nprint('hi')\n----\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        self.assertEqual(len(attached), 1)
        anchor, widget = attached[0]
        self.assertIsInstance(anchor, Gtk.TextChildAnchor)
        self.assertIsInstance(widget, Gtk.Frame)

    def test_code_block_inner_textview_has_wrap_none_and_monospace(self) -> None:
        src = "= D\n\n----\nlong code here\n----\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        frame = cast(Gtk.Frame, attached[0][1])
        scroll = cast(Gtk.ScrolledWindow, frame.get_child())
        self.assertIsInstance(scroll, Gtk.ScrolledWindow)
        # Horizontal scrolling is automatic; vertical never (the outer
        # article container handles vertical scrolling). GTK 4 returns
        # the pair as a named tuple from ``get_policy``.
        h_policy, v_policy = scroll.get_policy()
        self.assertEqual(h_policy, Gtk.PolicyType.AUTOMATIC)
        self.assertEqual(v_policy, Gtk.PolicyType.NEVER)
        # The TextView lives inside the ScrolledWindow.
        inner = cast(Gtk.TextView, scroll.get_child())
        self.assertIsInstance(inner, Gtk.TextView)
        self.assertEqual(inner.get_wrap_mode(), Gtk.WrapMode.NONE)
        self.assertTrue(inner.get_monospace())
        self.assertFalse(inner.get_editable())

    def test_code_block_widget_carries_verbatim_content(self) -> None:
        code = "def f():\n    return 42"
        src = f"= D\n\n----\n{code}\n----\n"
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            src, buffer, note_id="n1", attach_widget=_collect(attached)
        )
        frame = cast(Gtk.Frame, attached[0][1])
        scroll = cast(Gtk.ScrolledWindow, frame.get_child())
        inner = cast(Gtk.TextView, scroll.get_child())
        inner_buffer = inner.get_buffer()
        self.assertEqual(
            inner_buffer.get_text(
                inner_buffer.get_start_iter(),
                inner_buffer.get_end_iter(),
                True,
            ),
            code,
        )

    def test_code_block_anchor_is_placed_in_outer_buffer(self) -> None:
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\n----\nx\n----\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        anchor = attached[0][0]
        # The very same anchor object can be located in the buffer by
        # walking iterators — proving it lives at a real offset.
        anchor_offsets = _anchor_offsets(buffer)
        self.assertEqual(len(anchor_offsets), 1)
        located = buffer.get_iter_at_offset(anchor_offsets[0]).get_child_anchor()
        self.assertIs(located, anchor)


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

    def test_image_attaches_picture_with_scale_down(self) -> None:
        renderer, buffer, _ = _build_renderer()
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\nimage::cat.png[]\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        self.assertEqual(len(attached), 1)
        widget = attached[0][1]
        self.assertIsInstance(widget, Gtk.Picture)
        picture = cast(Gtk.Picture, widget)
        self.assertEqual(picture.get_content_fit(), Gtk.ContentFit.SCALE_DOWN)

    def test_decode_failure_produces_a_label_placeholder(self) -> None:
        renderer, buffer, _ = _build_renderer(
            image_bytes_for=lambda _f: b"not a png"
        )
        attached: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\nimage::broken.png[]\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached),
        )
        self.assertEqual(len(attached), 1)
        widget = attached[0][1]
        self.assertIsInstance(widget, Gtk.Label)
        # The filename appears in the placeholder so the user knows
        # which image is missing.
        self.assertIn("broken.png", cast(Gtk.Label, widget).get_label())

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
    def test_resolver_is_not_called_for_step_4_blocks(self) -> None:
        # Step 6 emits no widgets that depend on column width — the
        # resolver is wired in for forward compatibility (tables in
        # step 14) but should not be invoked yet by paragraph / list /
        # heading / code / image rendering.
        calls = 0

        def column_width() -> int:
            nonlocal calls
            calls += 1
            return 600

        renderer, buffer, _ = _build_renderer(column_width_px=column_width)
        renderer.render_into(
            "= Welcome\n\nA *para* with formatting.\n\n* One\n* Two\n",
            buffer,
            note_id="n1",
        )
        self.assertEqual(calls, 0)


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
        # anchors — the second render starts from a clean buffer.
        renderer, buffer, _ = _build_renderer()
        attached_first: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\nimage::a.png[]\n",
            buffer,
            note_id="n1",
            attach_widget=_collect(attached_first),
        )
        attached_second: list[tuple[Gtk.TextChildAnchor, Gtk.Widget]] = []
        renderer.render_into(
            "= D\n\nNo image here.\n",
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
        wrong_table = build_tag_table()
        right_table = build_tag_table()
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


if __name__ == "__main__":
    unittest.main()
