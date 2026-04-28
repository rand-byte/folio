"""Tests for :mod:`notes_app.asciidoc.tag_table`."""

from __future__ import annotations

import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk, Pango  # noqa: E402

from notes_app.asciidoc.tag_table import (
    TagName,
    build_tag_table,
    heading_tag_name,
)


class TagNameTests(unittest.TestCase):
    def test_string_value_matches_name(self) -> None:
        # The value is what the GTK tag carries as its ``name`` property
        # — make sure the enum exposes a stable string for each member.
        self.assertEqual(TagName.BOLD.value, "bold")
        self.assertEqual(TagName.ITALIC.value, "italic")
        self.assertEqual(TagName.STRIKETHROUGH.value, "strikethrough")
        self.assertEqual(TagName.UNDERLINE.value, "underline")
        self.assertEqual(TagName.HEADING_0.value, "heading_0")
        self.assertEqual(TagName.HEADING_6.value, "heading_6")

    def test_no_heading_1_member(self) -> None:
        # The parser produces level-0 (Document.title) and 2..6
        # (Section.level). Level 1 is intentionally absent.
        self.assertNotIn("HEADING_1", TagName.__members__)

    def test_member_set_is_closed(self) -> None:
        # If a future build step adds new tags, the test should be
        # updated deliberately. Pin the current set so accidents trip
        # the test.
        expected = {
            "BOLD",
            "ITALIC",
            "STRIKETHROUGH",
            "UNDERLINE",
            "HEADING_0",
            "HEADING_2",
            "HEADING_3",
            "HEADING_4",
            "HEADING_5",
            "HEADING_6",
        }
        self.assertEqual(set(TagName.__members__), expected)


class HeadingTagNameTests(unittest.TestCase):
    def test_known_levels_map_to_enum_members(self) -> None:
        for level, expected in (
            (0, TagName.HEADING_0),
            (2, TagName.HEADING_2),
            (3, TagName.HEADING_3),
            (4, TagName.HEADING_4),
            (5, TagName.HEADING_5),
            (6, TagName.HEADING_6),
        ):
            with self.subTest(level=level):
                self.assertIs(heading_tag_name(level), expected)

    def test_level_1_raises_key_error(self) -> None:
        # Level 1 is not produced by the parser; the renderer asking
        # for it is a programming error and must fail loudly.
        with self.assertRaises(KeyError):
            heading_tag_name(1)

    def test_out_of_range_levels_raise_key_error(self) -> None:
        for level in (-1, 7, 100):
            with self.subTest(level=level):
                with self.assertRaises(KeyError):
                    heading_tag_name(level)


class BuildTagTableStructureTests(unittest.TestCase):
    """Membership checks that don't need a display.

    Tag tables and tags are pure GObject — they construct without an
    open GDK display, so these tests run anywhere.
    """

    def setUp(self) -> None:
        self.table = build_tag_table()

    def test_every_enum_member_has_a_tag(self) -> None:
        for name in TagName:
            with self.subTest(name=name):
                self.assertIsNotNone(
                    self.table.lookup(name.value),
                    f"missing tag for {name!r}",
                )

    def test_lookup_returns_a_text_tag(self) -> None:
        tag = self.table.lookup(TagName.BOLD.value)
        self.assertIsInstance(tag, Gtk.TextTag)

    def test_lookup_unknown_name_returns_none(self) -> None:
        # Sanity check that lookup is exact — the renderer relying on a
        # typo'd name would silently get None and skip the tag, so make
        # sure GTK's lookup is the strict match we expect.
        self.assertIsNone(self.table.lookup("not-a-tag"))

    def test_each_call_returns_a_fresh_table(self) -> None:
        # Per the module's invariant: a fresh table per call. This lets
        # tests construct independent buffers without aliasing.
        another = build_tag_table()
        self.assertIsNot(self.table, another)
        # The tags inside are also fresh — not aliased.
        self.assertIsNot(
            self.table.lookup(TagName.BOLD.value),
            another.lookup(TagName.BOLD.value),
        )

    def test_no_extra_tags_present(self) -> None:
        # Walk the table and assert the tag-name set matches the enum
        # exactly. Catches accidental addition of stray tags.
        collected: list[str] = []

        def collect(tag: Gtk.TextTag, _data: object) -> None:
            collected.append(tag.get_property("name"))

        self.table.foreach(collect, None)
        self.assertEqual(set(collected), {n.value for n in TagName})


class InlineTagPropertyTests(unittest.TestCase):
    """The four inline tags carry the visual properties they advertise."""

    def setUp(self) -> None:
        self.table = build_tag_table()

    def test_bold_tag_uses_bold_weight(self) -> None:
        tag = self.table.lookup(TagName.BOLD.value)
        self.assertEqual(tag.get_property("weight"), Pango.Weight.BOLD)

    def test_italic_tag_uses_italic_style(self) -> None:
        tag = self.table.lookup(TagName.ITALIC.value)
        self.assertEqual(tag.get_property("style"), Pango.Style.ITALIC)

    def test_strikethrough_tag_sets_strikethrough(self) -> None:
        tag = self.table.lookup(TagName.STRIKETHROUGH.value)
        self.assertTrue(tag.get_property("strikethrough"))

    def test_underline_tag_uses_single_underline(self) -> None:
        tag = self.table.lookup(TagName.UNDERLINE.value)
        self.assertEqual(tag.get_property("underline"), Pango.Underline.SINGLE)

    def test_inline_tags_do_not_set_unrelated_properties(self) -> None:
        # An inline tag setting weight=BOLD must not also stamp a scale
        # multiplier — heading-style on every bolded word would be very
        # wrong. Probe the property that bold *should not* touch.
        bold = self.table.lookup(TagName.BOLD.value)
        # ``scale-set`` is the GtkTextTag flag indicating whether scale
        # has been explicitly set. False means the tag defers to the
        # inherited size.
        self.assertFalse(bold.get_property("scale-set"))


class HeadingTagPropertyTests(unittest.TestCase):
    """Heading tags are bold-weight + scale, in monotone-decreasing scale."""

    def setUp(self) -> None:
        self.table = build_tag_table()

    def test_every_heading_tag_is_bold(self) -> None:
        for level in (0, 2, 3, 4, 5, 6):
            with self.subTest(level=level):
                tag = self.table.lookup(heading_tag_name(level).value)
                self.assertEqual(tag.get_property("weight"), Pango.Weight.BOLD)

    def test_every_heading_tag_has_scale_set(self) -> None:
        for level in (0, 2, 3, 4, 5, 6):
            with self.subTest(level=level):
                tag = self.table.lookup(heading_tag_name(level).value)
                self.assertTrue(tag.get_property("scale-set"))

    def test_heading_scales_decrease_with_level(self) -> None:
        # Document title (level 0) is the largest; h6 is the smallest.
        scales = [
            self.table.lookup(heading_tag_name(level).value).get_property("scale")
            for level in (0, 2, 3, 4, 5, 6)
        ]
        self.assertEqual(scales, sorted(scales, reverse=True))

    def test_h6_scale_is_at_least_one(self) -> None:
        # Headings should not shrink *below* body size — that defeats the
        # purpose of the heading marker.
        h6 = self.table.lookup(TagName.HEADING_6.value)
        self.assertGreaterEqual(h6.get_property("scale"), 1.0)

    def test_doc_title_scale_is_largest(self) -> None:
        h0 = self.table.lookup(TagName.HEADING_0.value).get_property("scale")
        h2 = self.table.lookup(TagName.HEADING_2.value).get_property("scale")
        self.assertGreater(h0, h2)


if __name__ == "__main__":
    unittest.main()
