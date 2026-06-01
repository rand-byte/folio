"""Tests for :mod:`ui.note_render.tag_table`."""

from __future__ import annotations

import unittest

from gi.repository import Gtk, Pango

from giruntime.ui.note_render.tag_table import (
    TagName,
    WashSpec,
    admonition_body_tag_name,
    admonition_kind_tag_name,
    admonition_label_tag_name,
    build_tag_table,
    build_wash_specs,
    heading_tag_name,
)
from enums import AdmonitionKind


# Default M-width used by tests that don't care about the actual
# value. ``build_tag_table`` requires ``char_width_px`` (no default)
# so every call site supplies one — see the tag-table docstring for
# why this is required rather than defaulted. The literal ``9`` is
# arbitrary but typical (close to a real body font's M-width); what
# matters is that it's a positive int.
_TEST_CHAR_WIDTH_PX: int = 9


# Tag names that *used* to carry ``paragraph-background-rgba`` directly
# on the tag. The wash for these blocks is now painted at snapshot time
# by :class:`_ArticleTextView` (see :func:`build_wash_specs`), so the
# tags must *not* set ``paragraph-background-rgba`` — the inverted test
# below (:class:`ParagraphBackgroundIsNotOnTagsTests`) pins that
# invariant. Listed in one place here so a future "new block kind with
# a tint" adds itself to the list rather than tweaking every test that
# iterates it.
_PARAGRAPH_BACKGROUND_TAGS: tuple[TagName, ...] = (
    TagName.ADMONITION_NOTE_LABEL,
    TagName.ADMONITION_TIP_LABEL,
    TagName.ADMONITION_IMPORTANT_LABEL,
    TagName.ADMONITION_WARNING_LABEL,
    TagName.ADMONITION_CAUTION_LABEL,
    TagName.ADMONITION_NOTE_BODY,
    TagName.ADMONITION_TIP_BODY,
    TagName.ADMONITION_IMPORTANT_BODY,
    TagName.ADMONITION_WARNING_BODY,
    TagName.ADMONITION_CAUTION_BODY,
    TagName.BLOCKQUOTE_BODY,
    TagName.CODE_BLOCK,
)


class TagNameTests(unittest.TestCase):
    def test_string_value_matches_name(self) -> None:
        # The value is what the GTK tag carries as its ``name`` property
        # — make sure the enum exposes a stable string for each member.
        self.assertEqual(TagName.BOLD.value, "bold")
        self.assertEqual(TagName.ITALIC.value, "italic")
        self.assertEqual(TagName.STRIKETHROUGH.value, "strikethrough")
        self.assertEqual(TagName.UNDERLINE.value, "underline")
        self.assertEqual(TagName.MONOSPACE.value, "monospace")
        self.assertEqual(TagName.LINK.value, "link")
        self.assertEqual(TagName.HEADING_0.value, "heading_0")
        self.assertEqual(TagName.HEADING_6.value, "heading_6")
        self.assertEqual(
            TagName.ADMONITION_NOTE_LABEL.value, "admonition_note_label"
        )
        self.assertEqual(
            TagName.ADMONITION_NOTE_BODY.value, "admonition_note_body"
        )
        self.assertEqual(
            TagName.ADMONITION_NOTE_KIND.value, "admonition_note_kind"
        )
        self.assertEqual(TagName.BLOCKQUOTE_BODY.value, "blockquote_body")
        self.assertEqual(
            TagName.BLOCKQUOTE_ATTRIBUTION.value, "blockquote_attribution"
        )
        self.assertEqual(TagName.CODE_BLOCK.value, "code_block")

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
            "MONOSPACE",
            "LINK",
            "HEADING_0",
            "HEADING_2",
            "HEADING_3",
            "HEADING_4",
            "HEADING_5",
            "HEADING_6",
            "ADMONITION_NOTE_LABEL",
            "ADMONITION_TIP_LABEL",
            "ADMONITION_IMPORTANT_LABEL",
            "ADMONITION_WARNING_LABEL",
            "ADMONITION_CAUTION_LABEL",
            "ADMONITION_NOTE_BODY",
            "ADMONITION_TIP_BODY",
            "ADMONITION_IMPORTANT_BODY",
            "ADMONITION_WARNING_BODY",
            "ADMONITION_CAUTION_BODY",
            "ADMONITION_NOTE_KIND",
            "ADMONITION_TIP_KIND",
            "ADMONITION_IMPORTANT_KIND",
            "ADMONITION_WARNING_KIND",
            "ADMONITION_CAUTION_KIND",
            "BLOCKQUOTE_BODY",
            "BLOCKQUOTE_ATTRIBUTION",
            "CODE_BLOCK",
            "METADATA",
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


class AdmonitionTagNameLookupTests(unittest.TestCase):
    """The (kind → label / body / kind-character tag) mappings are exhaustive.

    Iterates :class:`AdmonitionKind` so adding a new admonition kind
    without extending the per-kind tables in :mod:`tag_table` fails the
    test rather than producing a silently-unstyled admonition.
    """

    def test_every_kind_resolves_to_a_label_tag_name(self) -> None:
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                self.assertIsInstance(admonition_label_tag_name(kind), TagName)

    def test_every_kind_resolves_to_a_body_tag_name(self) -> None:
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                self.assertIsInstance(admonition_body_tag_name(kind), TagName)

    def test_every_kind_resolves_to_a_kind_character_tag_name(self) -> None:
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                self.assertIsInstance(admonition_kind_tag_name(kind), TagName)

    def test_label_and_body_tags_are_distinct_per_kind(self) -> None:
        # The two paragraph roles must produce different names so the
        # renderer can apply them to different lines.
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                self.assertNotEqual(
                    admonition_label_tag_name(kind),
                    admonition_body_tag_name(kind),
                )

    def test_kind_tags_are_distinct_across_kinds(self) -> None:
        # The character tag for the kind label is per-kind so each
        # admonition's foreground accent is independent.
        names = {admonition_kind_tag_name(k) for k in AdmonitionKind}
        self.assertEqual(len(names), len(list(AdmonitionKind)))


class BuildTagTableStructureTests(unittest.TestCase):
    """Membership checks that don't need a display.

    Tag tables and tags are pure GObject — they construct without an
    open GDK display, so these tests run anywhere.
    """

    def setUp(self) -> None:
        self.table = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)

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
        another = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)
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
        self.table = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)

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

    def test_monospace_tag_uses_monospace_family(self) -> None:
        tag = self.table.lookup(TagName.MONOSPACE.value)
        self.assertEqual(tag.get_property("family"), "monospace")
        # Cross-check the property-set flag so that a future regression
        # like ``family=None`` doesn't silently leave monospace looking
        # like running text.
        self.assertTrue(tag.get_property("family-set"))

    def test_monospace_tag_does_not_set_color_or_underline(self) -> None:
        # Monospace must not stamp a foreground colour or underline —
        # those are reserved for the LINK tag, which composes with
        # MONOSPACE on monospace-inside-link spans.
        tag = self.table.lookup(TagName.MONOSPACE.value)
        self.assertFalse(tag.get_property("foreground-set"))
        self.assertFalse(tag.get_property("underline-set"))

    def test_link_tag_has_underline(self) -> None:
        tag = self.table.lookup(TagName.LINK.value)
        self.assertEqual(tag.get_property("underline"), Pango.Underline.SINGLE)

    def test_link_tag_has_foreground_color_set(self) -> None:
        tag = self.table.lookup(TagName.LINK.value)
        self.assertTrue(tag.get_property("foreground-set"))

    def test_link_tag_does_not_change_weight_or_family(self) -> None:
        # Link styling must compose: a link inside bold should look
        # bold-and-blue, a link wrapping a monospace span should look
        # monospace-and-blue. The shared LINK tag therefore must not
        # set weight or family of its own.
        tag = self.table.lookup(TagName.LINK.value)
        self.assertFalse(tag.get_property("weight-set"))
        self.assertFalse(tag.get_property("family-set"))

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
        self.table = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)

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


class ParagraphBackgroundIsNotOnTagsTests(unittest.TestCase):
    """No block-level paragraph tag carries ``paragraph-background-rgba``.

    The wash for admonition / blockquote / code-block paragraphs is
    painted at snapshot time by the article TextView subclass in
    :mod:`ui.note_view`, *not* via the paragraph tag. This
    is because GTK's ``paragraph-background-rgba`` paints exactly
    between the paragraph's effective ``left-margin`` and
    ``right-margin`` — there is no property that decouples wash
    position from text position. The snapshot-time painter produces
    the "padded card" effect (one M-width of tinted space between the
    text and the box edge on each side) that the user actually wants.
    See the module docstring for the long form.

    This test pins the structural invariant of the new design: every
    paragraph tag that previously carried a wash now has
    ``paragraph-background-set = False``. If a future change adds a
    wash directly to a tag, it will fire here — the message is that
    the wash should be painted via :func:`build_wash_specs` and
    :class:`_ArticleTextView` instead.
    """

    def setUp(self) -> None:
        self.table = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)

    def test_no_paragraph_tag_carries_a_paragraph_background_rgba(self) -> None:
        for name in _PARAGRAPH_BACKGROUND_TAGS:
            with self.subTest(name=name):
                tag = self.table.lookup(name.value)
                self.assertFalse(
                    tag.get_property("paragraph-background-set"),
                    f"{name!r} still carries paragraph-background-rgba; "
                    f"the wash should live on build_wash_specs() instead",
                )

    def test_attribution_does_not_carry_a_paragraph_background(self) -> None:
        # The attribution never had a tint, but pin the invariant
        # alongside its body sibling so the rule "no paragraph
        # backgrounds on block-level tags" is total.
        tag = self.table.lookup(TagName.BLOCKQUOTE_ATTRIBUTION.value)
        self.assertFalse(tag.get_property("paragraph-background-set"))


class AdmonitionTagPropertyTests(unittest.TestCase):
    """Admonition tags carry the visual properties the layout requires."""

    def setUp(self) -> None:
        self.table = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)

    def test_every_label_paragraph_tag_has_left_and_right_margins(self) -> None:
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                tag = self.table.lookup(admonition_label_tag_name(kind).value)
                self.assertGreater(tag.get_property("left-margin"), 0)
                self.assertGreater(tag.get_property("right-margin"), 0)

    def test_every_body_paragraph_tag_has_left_and_right_margins(self) -> None:
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                tag = self.table.lookup(admonition_body_tag_name(kind).value)
                self.assertGreater(tag.get_property("left-margin"), 0)
                self.assertGreater(tag.get_property("right-margin"), 0)

    def test_label_paragraph_has_top_padding(self) -> None:
        # The block's top margin lives on the label paragraph so the
        # NOTE / TIP / … label has air above it.
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                tag = self.table.lookup(admonition_label_tag_name(kind).value)
                self.assertGreater(tag.get_property("pixels-above-lines"), 0)

    def test_body_paragraph_has_bottom_padding(self) -> None:
        # The block's bottom margin lives on the body paragraph so the
        # last body line has air below it.
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                tag = self.table.lookup(admonition_body_tag_name(kind).value)
                self.assertGreater(tag.get_property("pixels-below-lines"), 0)

    def test_every_kind_character_tag_is_bold(self) -> None:
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                tag = self.table.lookup(admonition_kind_tag_name(kind).value)
                self.assertEqual(tag.get_property("weight"), Pango.Weight.BOLD)

    def test_every_kind_character_tag_has_foreground_set(self) -> None:
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                tag = self.table.lookup(admonition_kind_tag_name(kind).value)
                self.assertTrue(tag.get_property("foreground-set"))

    def test_paragraph_tags_do_not_set_weight_or_family(self) -> None:
        # The paragraph tags carry layout only; inline composition (bold
        # / italic / monospace inside admonition bodies) must work via
        # the existing inline tags, so the paragraph tags must not
        # stamp those properties of their own.
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                for name in (
                    admonition_label_tag_name(kind),
                    admonition_body_tag_name(kind),
                ):
                    tag = self.table.lookup(name.value)
                    self.assertFalse(tag.get_property("weight-set"))
                    self.assertFalse(tag.get_property("family-set"))

    def test_paragraph_tag_uses_accumulative_margin(self) -> None:
        # The wash painter relies on the text being positioned *inside*
        # the box one M-width on each side. The textview's widget-level
        # left/right margins set the column edge; the tag's
        # ``left-margin`` / ``right-margin`` must *stack* on top of
        # those (``accumulative-margin = True``) rather than
        # *replacing* them, otherwise the admonition would escape the
        # inner column.
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                for name in (
                    admonition_label_tag_name(kind),
                    admonition_body_tag_name(kind),
                ):
                    tag = self.table.lookup(name.value)
                    self.assertTrue(tag.get_property("accumulative-margin"))


class BlockquoteTagPropertyTests(unittest.TestCase):
    """Blockquote body and attribution tags."""

    def setUp(self) -> None:
        self.table = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)

    def test_body_has_left_margin_indent(self) -> None:
        # The indent is what distinguishes a quote from running prose.
        tag = self.table.lookup(TagName.BLOCKQUOTE_BODY.value)
        self.assertGreater(tag.get_property("left-margin"), 0)

    def test_body_does_not_set_italic_style(self) -> None:
        # Italic composes via the shared ITALIC tag — the body tag must
        # leave the style property alone so the composition is clean.
        tag = self.table.lookup(TagName.BLOCKQUOTE_BODY.value)
        self.assertFalse(tag.get_property("style-set"))

    def test_attribution_has_left_margin_matching_body(self) -> None:
        # The attribution sits flush with the body indent so the
        # citation reads as part of the quote block.
        body = self.table.lookup(TagName.BLOCKQUOTE_BODY.value)
        attr = self.table.lookup(TagName.BLOCKQUOTE_ATTRIBUTION.value)
        self.assertEqual(
            attr.get_property("left-margin"),
            body.get_property("left-margin"),
        )

    def test_attribution_is_right_justified(self) -> None:
        tag = self.table.lookup(TagName.BLOCKQUOTE_ATTRIBUTION.value)
        self.assertEqual(
            tag.get_property("justification"),
            Gtk.Justification.RIGHT,
        )

    def test_attribution_scale_is_less_than_one(self) -> None:
        # Smaller scale so the citation reads as secondary metadata.
        tag = self.table.lookup(TagName.BLOCKQUOTE_ATTRIBUTION.value)
        self.assertTrue(tag.get_property("scale-set"))
        self.assertLess(tag.get_property("scale"), 1.0)

    def test_body_paragraph_tag_uses_accumulative_margin(self) -> None:
        # Same invariant as the admonition tags: the wash painter
        # relies on the text being positioned *inside* the box; the
        # tag's ``left-margin`` / ``right-margin`` must stack on top
        # of the textview's widget-level margins rather than replace
        # them, otherwise the blockquote escapes the inner column.
        tag = self.table.lookup(TagName.BLOCKQUOTE_BODY.value)
        self.assertTrue(tag.get_property("accumulative-margin"))

    def test_attribution_tag_uses_accumulative_margin(self) -> None:
        # The attribution sits flush with the body's text, so its
        # margins must compose with the widget the same way.
        tag = self.table.lookup(TagName.BLOCKQUOTE_ATTRIBUTION.value)
        self.assertTrue(tag.get_property("accumulative-margin"))


class CodeBlockTagPropertyTests(unittest.TestCase):
    """Code-block tag carries layout but not the monospace family."""

    def setUp(self) -> None:
        self.table = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)

    def test_has_left_and_right_margins(self) -> None:
        tag = self.table.lookup(TagName.CODE_BLOCK.value)
        self.assertGreater(tag.get_property("left-margin"), 0)
        self.assertGreater(tag.get_property("right-margin"), 0)

    def test_does_not_set_monospace_family(self) -> None:
        # Monospace family comes from the shared MONOSPACE tag, layered
        # on top by the renderer. Setting it here would conflict with
        # that composition strategy.
        tag = self.table.lookup(TagName.CODE_BLOCK.value)
        self.assertFalse(tag.get_property("family-set"))

    def test_paragraph_tag_uses_accumulative_margin(self) -> None:
        # The wash painter relies on the text being positioned *inside*
        # the box one M-width on each side. The textview's widget-level
        # left/right margins set the column edge; the tag's
        # ``left-margin`` / ``right-margin`` must *stack* on top of
        # those (``accumulative-margin = True``) rather than
        # *replacing* them, otherwise a block-level paragraph would
        # escape the inner column.
        tag = self.table.lookup(TagName.CODE_BLOCK.value)
        self.assertTrue(tag.get_property("accumulative-margin"))


class WashSpecTests(unittest.TestCase):
    """The :func:`build_wash_specs` map drives the snapshot-time wash painter.

    Every key in the map must resolve on the tag table; every value
    must be a structurally sound :class:`WashSpec`; and the
    "attribution has no wash" invariant must hold (the painter
    paints nothing behind a :data:`TagName.BLOCKQUOTE_ATTRIBUTION`
    line). The admonition label and body for one kind must share an
    identical spec so they read as one rectangle.
    """

    def setUp(self) -> None:
        self.specs = build_wash_specs()
        self.table = build_tag_table(char_width_px=_TEST_CHAR_WIDTH_PX)

    def test_keys_are_tag_names(self) -> None:
        for name in self.specs:
            with self.subTest(name=name):
                self.assertIsInstance(name, TagName)

    def test_every_key_resolves_on_the_tag_table(self) -> None:
        # The painter looks tags up by name from the buffer's tag
        # table at construction time. A wash-spec key with no
        # corresponding tag would be silently dropped, so any
        # mismatch must fire here.
        for name in self.specs:
            with self.subTest(name=name):
                self.assertIsNotNone(self.table.lookup(name.value))

    def test_every_value_is_a_wash_spec_with_valid_fields(self) -> None:
        for name, spec in self.specs.items():
            with self.subTest(name=name):
                self.assertIsInstance(spec, WashSpec)
                # Tint is a 4-tuple of floats in [0, 1].
                self.assertEqual(len(spec.tint), 4)
                for component in spec.tint:
                    self.assertIsInstance(component, float)
                    self.assertGreaterEqual(component, 0.0)
                    self.assertLessEqual(component, 1.0)
                # Insets are non-negative ints.
                self.assertIsInstance(spec.box_left_inset_px, int)
                self.assertIsInstance(spec.box_right_inset_px, int)
                self.assertGreaterEqual(spec.box_left_inset_px, 0)
                self.assertGreaterEqual(spec.box_right_inset_px, 0)

    def test_admonition_label_and_body_share_identical_spec(self) -> None:
        # Label + body must form one rectangle visually, so the two
        # paragraph tags must paint the same colour at the same
        # horizontal extents — i.e. the *same* :class:`WashSpec`
        # instance. ``assertIs`` (identity, not equality) makes the
        # invariant tighter: a tweak to one tint would force the
        # shared spec, not two near-duplicate ones.
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                label = self.specs[admonition_label_tag_name(kind)]
                body = self.specs[admonition_body_tag_name(kind)]
                self.assertIs(label, body)

    def test_blockquote_attribution_has_no_wash_entry(self) -> None:
        # The attribution must paint nothing behind it — its only
        # styling is right-alignment and a smaller scale. The
        # painter paints rects for entries it finds in the map; an
        # absent entry produces no rect.
        self.assertNotIn(TagName.BLOCKQUOTE_ATTRIBUTION, self.specs)

    def test_every_paragraph_background_tag_has_a_wash_spec(self) -> None:
        # The inverted complement of the test above: every tag that
        # *used* to carry a paragraph background must now have a
        # wash spec (so the snapshot painter restores its visual
        # identity). Iterates the same shared listing
        # :data:`_PARAGRAPH_BACKGROUND_TAGS` to keep the two invariants
        # paired and prevent drift.
        for name in _PARAGRAPH_BACKGROUND_TAGS:
            with self.subTest(name=name):
                self.assertIn(name, self.specs)

    def test_metadata_wash_spec_is_present_and_hairline(self) -> None:
        # The metadata line under the title gets a hairline rule — a
        # 1-px divider drawn at the bottom of the line — rather than a
        # full-height fill. Its wash spec must therefore be present and
        # carry ``hairline=True``.
        self.assertIn(TagName.METADATA, self.specs)
        self.assertTrue(self.specs[TagName.METADATA].hairline)

    def test_only_metadata_is_a_hairline_spec(self) -> None:
        # Every other wash-bearing tag paints a full-height tinted
        # block, not a hairline. Guards against a future block kind
        # accidentally inheriting the hairline flag.
        for name, spec in self.specs.items():
            with self.subTest(name=name):
                self.assertEqual(spec.hairline, name is TagName.METADATA)


if __name__ == "__main__":
    unittest.main()
