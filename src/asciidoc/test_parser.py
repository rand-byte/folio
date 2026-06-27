"""Tests for :mod:`asciidoc.parser`.

The parser is the integration point of the AsciiDoc subset: lexer →
inline parser → AST. Tests in this module deliberately cover both:

* **Happy paths** for every block kind (sections, paragraphs, lists,
  code blocks, images) plus the welcome note used as the seed for the
  database, which exercises most of the grammar in one fixture.
* **Every error variant** the parser can raise — :class:`ParseError`
  with each applicable :class:`ParseErrorKind` value. Errors propagated
  from the inline parser are also covered, since paragraph parsing
  invokes the inline parser per source line.
"""

from __future__ import annotations

import unittest

from asciidoc.ast import (
    Admonition,
    Blockquote,
    Bold,
    CodeBlock,
    Image,
    InlineNode,
    ListItem,
    OrderedList,
    Paragraph,
    Section,
    SoftBreak,
    Table,
    TableCell,
    TableRow,
    Text,
    UnorderedList,
)
from asciidoc.parser import parse
from enums import AdmonitionKind, ParseErrorKind, SystemDocument
from models.parse_error import ParseError
from system_docs import load_text


_WELCOME_SOURCE: str = load_text(SystemDocument.WELCOME)
"""The seed welcome note source, read from the ``system_docs`` package."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _t(content: str, line: int) -> Text:
    return Text(content=content, source_line=line)


def _para(line: int, *inlines: InlineNode) -> Paragraph:
    return Paragraph(inlines=tuple(inlines), source_line=line)


# ---------------------------------------------------------------------------
# Document title
# ---------------------------------------------------------------------------


class DocumentTitleTests(unittest.TestCase):

    def test_level1_heading_at_start_becomes_title(self) -> None:
        doc = parse("= Hello\n\nbody\n")
        self.assertEqual(doc.title, (_t("Hello", 1),))
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)

    def test_no_title_when_first_block_is_paragraph(self) -> None:
        doc = parse("just text\n")
        self.assertIsNone(doc.title)
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)

    def test_no_title_when_first_block_is_section(self) -> None:
        doc = parse("== Section\n\nbody\n")
        self.assertIsNone(doc.title)
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Section)

    def test_title_with_inline_markup(self) -> None:
        doc = parse("= *bold* title\n")
        self.assertIsNotNone(doc.title)
        # The title is a tuple of inline nodes — Bold + Text + Text.
        assert doc.title is not None
        self.assertEqual(len(doc.title), 2)
        first, second = doc.title
        self.assertIsInstance(first, Bold)
        self.assertIsInstance(second, Text)

    def test_leading_blanks_before_title_are_skipped(self) -> None:
        doc = parse("\n\n= Title\n")
        self.assertIsNotNone(doc.title)

    def test_empty_title_raises_empty_heading(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("=\n\nbody")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.EMPTY_HEADING)
        self.assertEqual(ctx.exception.line, 1)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class SectionTests(unittest.TestCase):

    def test_single_level_2_section(self) -> None:
        doc = parse("== Sec\n\nbody\n")
        self.assertEqual(len(doc.blocks), 1)
        section = doc.blocks[0]
        assert isinstance(section, Section)
        self.assertEqual(section.level, 2)
        self.assertEqual(section.title, (_t("Sec", 1),))
        self.assertEqual(section.source_line, 1)
        self.assertEqual(len(section.blocks), 1)
        self.assertIsInstance(section.blocks[0], Paragraph)

    def test_level_3_nests_inside_level_2(self) -> None:
        source = (
            "== L2\n"
            "\n"
            "outer body\n"
            "\n"
            "=== L3\n"
            "\n"
            "inner body\n"
        )
        doc = parse(source)
        self.assertEqual(len(doc.blocks), 1)
        outer = doc.blocks[0]
        assert isinstance(outer, Section)
        self.assertEqual(outer.level, 2)
        self.assertEqual(len(outer.blocks), 2)
        self.assertIsInstance(outer.blocks[0], Paragraph)
        inner = outer.blocks[1]
        assert isinstance(inner, Section)
        self.assertEqual(inner.level, 3)
        self.assertEqual(len(inner.blocks), 1)

    def test_sibling_level_2_does_not_nest(self) -> None:
        source = (
            "== A\n"
            "\n"
            "a\n"
            "\n"
            "== B\n"
            "\n"
            "b\n"
        )
        doc = parse(source)
        self.assertEqual(len(doc.blocks), 2)
        for section in doc.blocks:
            assert isinstance(section, Section)
            self.assertEqual(section.level, 2)

    def test_l3_then_sibling_l2_pops_back_out(self) -> None:
        source = (
            "== A\n"
            "\n"
            "=== A1\n"
            "\n"
            "deep\n"
            "\n"
            "== B\n"
        )
        doc = parse(source)
        self.assertEqual(len(doc.blocks), 2)
        a, b = doc.blocks
        assert isinstance(a, Section) and isinstance(b, Section)
        self.assertEqual(a.level, 2)
        self.assertEqual(b.level, 2)
        # ``A1`` must be inside ``A``, not a child of ``B``.
        self.assertEqual(len(a.blocks), 1)
        self.assertIsInstance(a.blocks[0], Section)
        self.assertEqual(b.blocks, ())

    def test_section_title_with_inline_markup(self) -> None:
        doc = parse("== *bold* heading\n")
        section = doc.blocks[0]
        assert isinstance(section, Section)
        self.assertEqual(len(section.title), 2)
        self.assertIsInstance(section.title[0], Bold)


# ---------------------------------------------------------------------------
# Paragraphs
# ---------------------------------------------------------------------------


class ParagraphTests(unittest.TestCase):

    def test_single_line_paragraph(self) -> None:
        doc = parse("hello\n")
        self.assertEqual(len(doc.blocks), 1)
        para = doc.blocks[0]
        assert isinstance(para, Paragraph)
        self.assertEqual(para.inlines, (_t("hello", 1),))
        self.assertEqual(para.source_line, 1)

    def test_multi_line_paragraph_joined_with_soft_breaks(self) -> None:
        doc = parse("one\ntwo\nthree\n")
        self.assertEqual(len(doc.blocks), 1)
        para = doc.blocks[0]
        assert isinstance(para, Paragraph)
        self.assertEqual(
            para.inlines,
            (
                _t("one", 1),
                SoftBreak(source_line=2),
                _t("two", 2),
                SoftBreak(source_line=3),
                _t("three", 3),
            ),
        )
        # Paragraph source_line is the *first* line.
        self.assertEqual(para.source_line, 1)

    def test_blank_line_separates_paragraphs(self) -> None:
        doc = parse("first\n\nsecond\n")
        self.assertEqual(len(doc.blocks), 2)
        for para in doc.blocks:
            self.assertIsInstance(para, Paragraph)

    def test_paragraph_with_inline_markers(self) -> None:
        doc = parse("a *b* _c_ [.line-through]#d# [.underline]#e#\n")
        para = doc.blocks[0]
        assert isinstance(para, Paragraph)
        kinds = [type(node).__name__ for node in para.inlines]
        self.assertEqual(
            kinds,
            ["Text", "Bold", "Text", "Italic", "Text",
             "Strikethrough", "Text", "Underline"],
        )

    def test_inline_error_uses_per_line_line_number(self) -> None:
        # The unmatched ``*`` is on line 2; the parser must report
        # line 2, not line 1.
        with self.assertRaises(ParseError) as ctx:
            parse("first\n*unclosed\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_INLINE_SPAN)
        self.assertEqual(ctx.exception.line, 2)


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


class ListTests(unittest.TestCase):

    def test_unordered_list(self) -> None:
        doc = parse("* a\n* b\n* c\n")
        self.assertEqual(len(doc.blocks), 1)
        ul = doc.blocks[0]
        assert isinstance(ul, UnorderedList)
        self.assertEqual(len(ul.items), 3)
        for item, expected_text, expected_line in zip(
            ul.items,
            ("a", "b", "c"),
            (1, 2, 3),
        ):
            assert isinstance(item, ListItem)
            self.assertEqual(item.source_line, expected_line)
            self.assertEqual(item.inlines, (_t(expected_text, expected_line),))

    def test_ordered_list(self) -> None:
        doc = parse(". one\n. two\n")
        self.assertEqual(len(doc.blocks), 1)
        ol = doc.blocks[0]
        assert isinstance(ol, OrderedList)
        self.assertEqual(len(ol.items), 2)

    def test_blank_line_does_not_break_same_marker_list(self) -> None:
        # Blank lines between same-marker items are internal separators:
        # the items join into one list rather than splitting into two.
        doc = parse("* a\n* b\n\n* c\n")
        self.assertEqual(len(doc.blocks), 1)
        ul = doc.blocks[0]
        assert isinstance(ul, UnorderedList)
        self.assertEqual(len(ul.items), 3)
        self.assertEqual([_item_text(i) for i in ul.items], ["a", "b", "c"])

    def test_blank_separated_ordered_list_is_one_continuous_list(
        self,
    ) -> None:
        # Ordered items separated by blanks stay in one list, so the
        # renderer's positional numbering yields 1, 2, 3 (no restart).
        doc = parse(". a\n\n. b\n\n. c\n")
        self.assertEqual(len(doc.blocks), 1)
        ol = doc.blocks[0]
        assert isinstance(ol, OrderedList)
        self.assertEqual(len(ol.items), 3)
        self.assertEqual([_item_text(i) for i in ol.items], ["a", "b", "c"])

    def test_blank_before_non_list_block_terminates_list(self) -> None:
        # A blank followed by a non-list block still ends the list.
        doc = parse("* a\n* b\n\nEnd.\n")
        self.assertEqual(len(doc.blocks), 2)
        ul, para = doc.blocks
        assert isinstance(ul, UnorderedList)
        self.assertIsInstance(para, Paragraph)
        self.assertEqual(len(ul.items), 2)

    def test_double_blank_between_items_still_joins(self) -> None:
        # Joining spans a run of consecutive blanks, not just a single one.
        doc = parse("* a\n\n\n* b\n")
        self.assertEqual(len(doc.blocks), 1)
        ul = doc.blocks[0]
        assert isinstance(ul, UnorderedList)
        self.assertEqual([_item_text(i) for i in ul.items], ["a", "b"])

    def test_list_items_carry_inline_markup(self) -> None:
        doc = parse("* this is *bold*\n")
        ul = doc.blocks[0]
        assert isinstance(ul, UnorderedList)
        item = ul.items[0]
        kinds = [type(node).__name__ for node in item.inlines]
        self.assertEqual(kinds, ["Text", "Bold"])

    def test_unmatched_inline_in_list_item_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("* *unclosed\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_INLINE_SPAN)
        self.assertEqual(ctx.exception.line, 1)


def _item_text(item: ListItem) -> str:
    """Concatenate the plain-text content of a leaf item's inlines."""
    return "".join(
        node.content for node in item.inlines if isinstance(node, Text)
    )


class NestedListTests(unittest.TestCase):
    """Multi-level lists build a recursive tree; depth rules are strict."""

    def test_same_type_nesting_builds_expected_tree(self) -> None:
        doc = parse("* L1\n** L2\n*** L3\n* L1b\n")
        self.assertEqual(len(doc.blocks), 1)
        top = doc.blocks[0]
        assert isinstance(top, UnorderedList)
        self.assertEqual(len(top.items), 2)
        l1, l1b = top.items
        self.assertEqual(_item_text(l1), "L1")
        self.assertEqual(_item_text(l1b), "L1b")
        self.assertEqual(l1b.children, ())
        # L1 -> [UL[L2 -> [UL[L3]]]]
        self.assertEqual(len(l1.children), 1)
        sub2 = l1.children[0]
        assert isinstance(sub2, UnorderedList)
        l2 = sub2.items[0]
        self.assertEqual(_item_text(l2), "L2")
        self.assertEqual(l2.source_line, 2)
        sub3 = l2.children[0]
        assert isinstance(sub3, UnorderedList)
        self.assertEqual(_item_text(sub3.items[0]), "L3")
        self.assertEqual(sub3.items[0].source_line, 3)

    def test_mixed_nesting_puts_ordered_sublist_under_unordered_item(
        self,
    ) -> None:
        doc = parse("* a\n.. b\n")
        top = doc.blocks[0]
        assert isinstance(top, UnorderedList)
        item = top.items[0]
        self.assertEqual(len(item.children), 1)
        sub = item.children[0]
        assert isinstance(sub, OrderedList)
        self.assertEqual(_item_text(sub.items[0]), "b")

    def test_dedent_pops_back_to_outer_level(self) -> None:
        doc = parse("* a\n** b\n* c\n")
        top = doc.blocks[0]
        assert isinstance(top, UnorderedList)
        # Two top-level items: a (with one child sublist) and c (a leaf).
        self.assertEqual([_item_text(i) for i in top.items], ["a", "c"])
        self.assertEqual(len(top.items[0].children), 1)
        self.assertEqual(top.items[1].children, ())

    def test_same_depth_type_switch_yields_two_sibling_lists(self) -> None:
        doc = parse("* a\n. b\n")
        self.assertEqual(len(doc.blocks), 2)
        first, second = doc.blocks
        self.assertIsInstance(first, UnorderedList)
        self.assertIsInstance(second, OrderedList)

    def test_nested_type_switch_makes_sibling_sublists(self) -> None:
        # Two sub-lists of different kinds hang under the same item.
        doc = parse("* a\n.. b\n** c\n")
        top = doc.blocks[0]
        assert isinstance(top, UnorderedList)
        children = top.items[0].children
        self.assertEqual(len(children), 2)
        self.assertIsInstance(children[0], OrderedList)
        self.assertIsInstance(children[1], UnorderedList)

    def test_blank_separated_type_switch_still_splits_siblings(self) -> None:
        # The blank is absorbed, then the top-level bullet→number switch
        # runs exactly as in the no-blank case: two sibling list blocks.
        doc = parse("* a\n\n. b\n")
        self.assertEqual(len(doc.blocks), 2)
        first, second = doc.blocks
        self.assertIsInstance(first, UnorderedList)
        self.assertIsInstance(second, OrderedList)

    def test_blank_inside_nested_list_keeps_one_tree(self) -> None:
        # A blank between two nested items is absorbed; the whole run is
        # one list with a, d at the top level and b, c nested under a.
        doc = parse("* a\n** b\n\n** c\n* d\n")
        self.assertEqual(len(doc.blocks), 1)
        top = doc.blocks[0]
        assert isinstance(top, UnorderedList)
        self.assertEqual([_item_text(i) for i in top.items], ["a", "d"])
        self.assertEqual(top.items[1].children, ())
        children = top.items[0].children
        self.assertEqual(len(children), 1)
        sub = children[0]
        assert isinstance(sub, UnorderedList)
        self.assertEqual([_item_text(i) for i in sub.items], ["b", "c"])

    def test_starts_below_top_level_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("** x\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.LIST_STARTS_BELOW_TOP_LEVEL,
        )
        self.assertEqual(ctx.exception.line, 1)

    def test_skips_level_raises_at_the_jumping_line(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("* a\n*** b\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.LIST_NESTING_SKIPS_LEVEL,
        )
        self.assertEqual(ctx.exception.line, 2)

    def test_too_deep_raises_at_the_fourth_level(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("* a\n** b\n*** c\n**** d\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.LIST_NESTING_TOO_DEEP,
        )
        self.assertEqual(ctx.exception.line, 4)

    def test_precedence_starts_below_wins_over_too_deep(self) -> None:
        # ``**** x`` as a list's first line is *both* below-top-level and
        # too-deep; the more fundamental condition reports first.
        with self.assertRaises(ParseError) as ctx:
            parse("**** x\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.LIST_STARTS_BELOW_TOP_LEVEL,
        )


# ---------------------------------------------------------------------------
# Code blocks
# ---------------------------------------------------------------------------


class CodeBlockTests(unittest.TestCase):

    def test_bare_fence_no_directive(self) -> None:
        doc = parse("----\nfoo\nbar\n----\n")
        self.assertEqual(len(doc.blocks), 1)
        cb = doc.blocks[0]
        assert isinstance(cb, CodeBlock)
        self.assertIsNone(cb.language)
        self.assertEqual(cb.content, "foo\nbar")
        # source_line points at the *opening fence* when there is no
        # directive.
        self.assertEqual(cb.source_line, 1)

    def test_fence_with_directive(self) -> None:
        doc = parse("[source,python]\n----\ndef f(): pass\n----\n")
        cb = doc.blocks[0]
        assert isinstance(cb, CodeBlock)
        self.assertEqual(cb.language, "python")
        self.assertEqual(cb.content, "def f(): pass")
        # Directive line is the source_line of the block.
        self.assertEqual(cb.source_line, 1)

    def test_bare_source_directive_no_lang(self) -> None:
        doc = parse("[source]\n----\nx\n----\n")
        cb = doc.blocks[0]
        assert isinstance(cb, CodeBlock)
        self.assertIsNone(cb.language)
        self.assertEqual(cb.content, "x")

    def test_empty_body(self) -> None:
        doc = parse("----\n----\n")
        cb = doc.blocks[0]
        assert isinstance(cb, CodeBlock)
        self.assertEqual(cb.content, "")

    def test_body_preserves_raw_lines(self) -> None:
        # Inline-looking syntax inside a code block is preserved verbatim.
        doc = parse("----\n*not bold*\n* not a list\n----\n")
        cb = doc.blocks[0]
        assert isinstance(cb, CodeBlock)
        self.assertEqual(cb.content, "*not bold*\n* not a list")

    def test_body_preserves_trailing_whitespace(self) -> None:
        # The lexer would right-strip a LineToken's text; the parser
        # therefore reads bodies through ``source_lines`` so trailing
        # whitespace inside code is kept.
        doc = parse("----\nindent\t\nspace  \n----\n")
        cb = doc.blocks[0]
        assert isinstance(cb, CodeBlock)
        self.assertEqual(cb.content, "indent\t\nspace  ")

    def test_unterminated_code_block_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("----\nfoo\nbar\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_CODE_BLOCK,
        )
        self.assertEqual(ctx.exception.line, 1)

    def test_unterminated_code_block_with_directive_points_at_fence(
        self,
    ) -> None:
        # The opening fence is on line 2; that's the line carried in
        # the error so the editor highlights the fence, not the
        # directive.
        with self.assertRaises(ParseError) as ctx:
            parse("[source,python]\n----\nfoo\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_CODE_BLOCK,
        )
        self.assertEqual(ctx.exception.line, 2)

    def test_directive_not_followed_by_fence_raises_unknown_block(
        self,
    ) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[source,python]\nnot a fence\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)
        self.assertEqual(ctx.exception.line, 1)

    def test_directive_at_end_of_file_raises_unknown_block(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[source,python]\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


class ImageTests(unittest.TestCase):

    def test_simple_image(self) -> None:
        doc = parse("image::cat.png[]\n")
        image = doc.blocks[0]
        assert isinstance(image, Image)
        self.assertEqual(image.filename, "cat.png")
        self.assertEqual(image.attrs, "")
        self.assertEqual(image.source_line, 1)

    def test_image_with_attrs(self) -> None:
        doc = parse("image::cat.png[alt=Cat,width=200]\n")
        image = doc.blocks[0]
        assert isinstance(image, Image)
        self.assertEqual(image.filename, "cat.png")
        self.assertEqual(image.attrs, "alt=Cat,width=200")

    def test_bad_image_macro_table(self) -> None:
        # (description, source) — every variant must raise
        # BAD_IMAGE_MACRO.
        cases: tuple[tuple[str, str], ...] = (
            ("missing brackets entirely", "image::cat.png\n"),
            ("missing closing bracket", "image::cat.png[\n"),
            ("missing opening bracket", "image::cat.png]\n"),
            ("empty filename", "image::[]\n"),
            ("nested open bracket in attrs", "image::cat.png[a[b]\n"),
            (
                "nested open bracket — outer close still missing",
                "image::cat.png[[\n",
            ),
        )
        for desc, source in cases:
            with self.subTest(desc):
                with self.assertRaises(ParseError) as ctx:
                    parse(source)
                self.assertEqual(
                    ctx.exception.kind,
                    ParseErrorKind.BAD_IMAGE_MACRO,
                )
                self.assertEqual(ctx.exception.line, 1)


# ---------------------------------------------------------------------------
# Heading-level errors
# ---------------------------------------------------------------------------


class HeadingErrorTests(unittest.TestCase):

    def test_level_7_or_more_is_unknown_block(self) -> None:
        for source in (
            "======= seven\n",
            "======== eight\n",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ParseError) as ctx:
                    parse(source)
                self.assertEqual(
                    ctx.exception.kind,
                    ParseErrorKind.UNKNOWN_BLOCK,
                )

    def test_level_1_heading_after_start_is_unknown_block(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("para\n\n= title here too late\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)
        self.assertEqual(ctx.exception.line, 3)

    def test_empty_section_heading_text_is_empty_heading(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("== \n\nbody")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.EMPTY_HEADING)
        self.assertEqual(ctx.exception.line, 1)


# ---------------------------------------------------------------------------
# Unknown-block detection at block start
# ---------------------------------------------------------------------------


class UnknownBlockDetectionTests(unittest.TestCase):
    """Lines that look like deferred AsciiDoc constructs raise UNKNOWN_BLOCK."""

    def test_table(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("line comment", "// a comment\n"),
            (
                "[source,] empty lang falls through then rejected",
                "[source,]\n----\nx\n----\n",
            ),
            (
                "[cols=\"\"] empty body falls through then rejected",
                "[cols=\"\"]\n|===\n|a\n|===\n",
            ),
            (
                "stray `====` admonition fence with no opener",
                "====\nbody\n====\n",
            ),
        )
        for desc, source in cases:
            with self.subTest(desc):
                with self.assertRaises(ParseError) as ctx:
                    parse(source)
                self.assertEqual(
                    ctx.exception.kind,
                    ParseErrorKind.UNKNOWN_BLOCK,
                )

    def test_unknown_block_does_not_fire_inside_paragraph(self) -> None:
        # A ``//`` line that is not at block-start is part of the
        # paragraph it follows. The unknown-block check is gated on
        # being at block-start; once we're collecting LineTokens for a
        # paragraph, every following LineToken is just paragraph text.
        doc = parse("first line\n// looks like a comment but is prose\n")
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)

    def test_attribute_entry_after_paragraph_is_unknown_block(self) -> None:
        # Attribute entries appearing *after* a body block (not in the
        # document header) raise UNKNOWN_BLOCK — positionally invalid,
        # not malformed.
        with self.assertRaises(ParseError) as ctx:
            parse("body paragraph.\n\n:doctype: book\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNKNOWN_BLOCK,
        )

    def test_attribute_entry_after_section_is_unknown_block(self) -> None:
        # Same — even when the body is a section heading, an
        # attribute entry past it is mid-document.
        with self.assertRaises(ParseError) as ctx:
            parse("== Section\n\n:author: me\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNKNOWN_BLOCK,
        )

    def test_malformed_attribute_entry_raises_bad_attribute_entry(self) -> None:
        # Names that don't match the lexer's strict shape (digit-led,
        # space-containing) lex as LineToken and reach the parser
        # via _reject_unknown_block, which raises the dedicated
        # BAD_ATTRIBUTE_ENTRY kind so the banner can show a tailored
        # message.
        for source in (":123: value\n", ":bad name: value\n"):
            with self.subTest(source=source):
                with self.assertRaises(ParseError) as ctx:
                    parse(source)
                self.assertEqual(
                    ctx.exception.kind,
                    ParseErrorKind.BAD_ATTRIBUTE_ENTRY,
                )


# ---------------------------------------------------------------------------
# Tables (step 14)
# ---------------------------------------------------------------------------


class TableTests(unittest.TestCase):
    """Happy-path table parsing — fences, cells, headers, cols directive."""

    def test_simple_two_column_table(self) -> None:
        doc = parse("|===\n|a|b\n|c|d\n|===\n")
        self.assertEqual(len(doc.blocks), 1)
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(len(table.rows), 2)
        # Header row.
        header = table.rows[0]
        assert isinstance(header, TableRow)
        self.assertEqual(len(header.cells), 2)
        for cell, expected_text in zip(header.cells, ("a", "b")):
            assert isinstance(cell, TableCell)
            self.assertEqual(cell.inlines, (_t(expected_text, 2),))
        # Data row.
        data = table.rows[1]
        for cell, expected_text in zip(data.cells, ("c", "d")):
            assert isinstance(cell, TableCell)
            self.assertEqual(cell.inlines, (_t(expected_text, 3),))
        self.assertIsNone(table.column_proportions)
        self.assertEqual(table.source_line, 1)

    def test_single_row_table(self) -> None:
        # A header-only table is valid — the parser only requires
        # at least one row between the fences.
        doc = parse("|===\n|only header|here\n|===\n")
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(len(table.rows[0].cells), 2)

    def test_three_column_table(self) -> None:
        doc = parse("|===\n|A|B|C\n|1|2|3\n|===\n")
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(len(table.rows[0].cells), 3)
        self.assertEqual(len(table.rows[1].cells), 3)

    def test_blank_lines_inside_fences_are_ignored(self) -> None:
        # The plan tolerates blank lines inside table fences as
        # visual whitespace — they don't add a row.
        doc = parse("|===\n|a|b\n\n|c|d\n|===\n")
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(len(table.rows), 2)

    def test_cell_inline_markup(self) -> None:
        doc = parse("|===\n|*bold*|_italic_\n|===\n")
        table = doc.blocks[0]
        assert isinstance(table, Table)
        first, second = table.rows[0].cells
        kinds_first = [type(n).__name__ for n in first.inlines]
        kinds_second = [type(n).__name__ for n in second.inlines]
        self.assertEqual(kinds_first, ["Bold"])
        self.assertEqual(kinds_second, ["Italic"])

    def test_cell_text_is_stripped_of_padding(self) -> None:
        # Padding around cell content (``| cell |``) is presentational —
        # the parsed inlines do not include the leading/trailing spaces.
        doc = parse("|===\n| a | b |\n|===\n")
        # Row split: ["| a ", " b ", ""]; trailing empty cell from
        # the trailing | is also a cell — three cells total.
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(len(table.rows[0].cells), 3)
        self.assertEqual(table.rows[0].cells[0].inlines, (_t("a", 2),))
        self.assertEqual(table.rows[0].cells[1].inlines, (_t("b", 2),))
        self.assertEqual(table.rows[0].cells[2].inlines, ())

    def test_cell_carries_source_line(self) -> None:
        doc = parse("\n\n|===\n|x|y\n|z|w\n|===\n")
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(table.rows[0].source_line, 4)
        self.assertEqual(table.rows[1].source_line, 5)
        # Every cell on a row shares that row's line number.
        for cell in table.rows[0].cells:
            self.assertEqual(cell.source_line, 4)


class TableColsDirectiveTests(unittest.TestCase):
    """The ``[cols="N,N,..."]`` directive parses to integer proportions."""

    def test_cols_directive_two_columns(self) -> None:
        doc = parse("[cols=\"1,2\"]\n|===\n|a|b\n|===\n")
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(table.column_proportions, (1, 2))
        # The block's source_line is the directive line, not the fence.
        self.assertEqual(table.source_line, 1)

    def test_cols_directive_three_columns(self) -> None:
        doc = parse("[cols=\"1,2,3\"]\n|===\n|a|b|c\n|===\n")
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(table.column_proportions, (1, 2, 3))

    def test_cols_directive_with_whitespace(self) -> None:
        # Whitespace around individual values is tolerated.
        doc = parse("[cols=\"1, 2 , 3\"]\n|===\n|a|b|c\n|===\n")
        table = doc.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(table.column_proportions, (1, 2, 3))


class TableErrorTests(unittest.TestCase):
    """Every error variant the table parser raises."""

    def test_unterminated_table_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n|a|b\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_TABLE,
        )
        self.assertEqual(ctx.exception.line, 1)

    def test_unterminated_table_with_directive_points_at_fence(self) -> None:
        # The opening fence is on line 2; the error should point there
        # so the user knows where to add the closing fence.
        with self.assertRaises(ParseError) as ctx:
            parse("[cols=\"1,1\"]\n|===\n|a|b\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_TABLE,
        )
        self.assertEqual(ctx.exception.line, 2)

    def test_empty_table_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n|===\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.EMPTY_TABLE)
        self.assertEqual(ctx.exception.line, 1)

    def test_empty_table_with_only_blanks_between_raises(self) -> None:
        # Blank lines don't count as rows.
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n\n\n|===\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.EMPTY_TABLE)

    def test_row_arity_mismatch_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n|a|b\n|c\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.TABLE_ROW_ARITY_MISMATCH,
        )
        # The mismatched row is on line 3.
        self.assertEqual(ctx.exception.line, 3)

    def test_row_arity_mismatch_extra_cell_raises(self) -> None:
        # Header has 2; data row has 3.
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n|a|b\n|c|d|e\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.TABLE_ROW_ARITY_MISMATCH,
        )

    def test_block_inside_cell_raises(self) -> None:
        # A heading inside the table fences is structurally invalid —
        # cells are inline-only.
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n== heading\n|a\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER,
        )
        self.assertEqual(ctx.exception.line, 2)

    def test_code_fence_inside_cell_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n----\n|a\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER,
        )

    def test_image_macro_inside_cell_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("|===\nimage::cat.png[]\n|a\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER,
        )

    def test_list_bullet_inside_cell_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n* a list item\n|a\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER,
        )

    def test_prose_inside_table_reports_unterminated(self) -> None:
        # A prose line (no leading |) inside the fences indicates
        # the user forgot to close the table — surface as
        # UNTERMINATED_TABLE rather than smushing the prose into a
        # cell.
        with self.assertRaises(ParseError) as ctx:
            parse("|===\nplain text\n|a|b\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_TABLE,
        )
        # Points at the offending prose line.
        self.assertEqual(ctx.exception.line, 2)

    def test_cols_directive_not_followed_by_fence_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[cols=\"1,1\"]\nnot a fence\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)
        self.assertEqual(ctx.exception.line, 1)

    def test_cols_directive_at_end_of_file_raises_unknown_block(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[cols=\"1,1\"]\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)

    def test_bad_cols_directive_non_integer_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[cols=\"1,foo\"]\n|===\n|a|b\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_COLS_DIRECTIVE,
        )
        self.assertEqual(ctx.exception.line, 1)

    def test_bad_cols_directive_zero_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[cols=\"0,1\"]\n|===\n|a|b\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_COLS_DIRECTIVE,
        )

    def test_bad_cols_directive_negative_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[cols=\"-1,1\"]\n|===\n|a|b\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_COLS_DIRECTIVE,
        )

    def test_bad_cols_directive_empty_value_raises(self) -> None:
        # ``[cols="1,,2"]`` — the empty middle value is a hard error.
        with self.assertRaises(ParseError) as ctx:
            parse("[cols=\"1,,2\"]\n|===\n|a|b|c\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_COLS_DIRECTIVE,
        )

    def test_cols_directive_arity_mismatch_raises(self) -> None:
        # Directive specifies 3 columns, table has 2.
        with self.assertRaises(ParseError) as ctx:
            parse("[cols=\"1,2,3\"]\n|===\n|a|b\n|===\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_COLS_DIRECTIVE,
        )
        # The directive line is what carries the mismatch info.
        self.assertEqual(ctx.exception.line, 1)

    def test_inline_error_inside_cell_propagates(self) -> None:
        # Unterminated bold in a cell should raise BAD_INLINE_SPAN
        # at the cell's line — same way paragraphs propagate inline
        # errors.
        with self.assertRaises(ParseError) as ctx:
            parse("|===\n|*unclosed\n|===\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_INLINE_SPAN)
        self.assertEqual(ctx.exception.line, 2)


# ---------------------------------------------------------------------------
# Admonitions (step 15) — single-line form
# ---------------------------------------------------------------------------


class SingleLineAdmonitionTests(unittest.TestCase):
    """``KIND: text`` parses to an :class:`Admonition` with one paragraph."""

    def test_basic_single_line_note(self) -> None:
        doc = parse("NOTE: hello\n")
        self.assertEqual(len(doc.blocks), 1)
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        self.assertEqual(admonition.kind, AdmonitionKind.NOTE)
        self.assertEqual(len(admonition.blocks), 1)
        paragraph = admonition.blocks[0]
        self.assertEqual(paragraph.inlines, (_t("hello", 1),))
        self.assertEqual(admonition.source_line, 1)

    def test_all_five_kinds(self) -> None:
        cases: tuple[tuple[str, AdmonitionKind], ...] = (
            ("NOTE: x\n", AdmonitionKind.NOTE),
            ("TIP: x\n", AdmonitionKind.TIP),
            ("IMPORTANT: x\n", AdmonitionKind.IMPORTANT),
            ("WARNING: x\n", AdmonitionKind.WARNING),
            ("CAUTION: x\n", AdmonitionKind.CAUTION),
        )
        for source, expected_kind in cases:
            with self.subTest(source=source):
                doc = parse(source)
                admonition = doc.blocks[0]
                assert isinstance(admonition, Admonition)
                self.assertEqual(admonition.kind, expected_kind)

    def test_inline_markup_inside_single_line_admonition(self) -> None:
        # The text after ``KIND: `` is run through parse_inline so
        # inline formatting is preserved.
        doc = parse("NOTE: see *important* item\n")
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        kinds = [type(n).__name__ for n in admonition.blocks[0].inlines]
        self.assertIn("Bold", kinds)

    def test_lowercase_kind_is_paragraph_not_admonition(self) -> None:
        # ``note: …`` (lowercase) is plain prose.
        doc = parse("note: hello\n")
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)


# ---------------------------------------------------------------------------
# Admonitions (step 15) — block form
# ---------------------------------------------------------------------------


class BlockAdmonitionTests(unittest.TestCase):
    """``[KIND]\\n====\\n…\\n====`` parses to an :class:`Admonition`."""

    def test_simple_block_note(self) -> None:
        doc = parse("[NOTE]\n====\nbody\n====\n")
        self.assertEqual(len(doc.blocks), 1)
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        self.assertEqual(admonition.kind, AdmonitionKind.NOTE)
        self.assertEqual(len(admonition.blocks), 1)
        # The source_line is the directive line (line 1), not the
        # fence line — this matters for error reporting.
        self.assertEqual(admonition.source_line, 1)

    def test_all_five_block_kinds(self) -> None:
        for kind in AdmonitionKind:
            source = f"[{kind.value}]\n====\nbody\n====\n"
            with self.subTest(kind=kind):
                doc = parse(source)
                admonition = doc.blocks[0]
                assert isinstance(admonition, Admonition)
                self.assertEqual(admonition.kind, kind)

    def test_block_admonition_with_multiple_paragraphs(self) -> None:
        source = (
            "[TIP]\n"
            "====\n"
            "First paragraph.\n"
            "\n"
            "Second paragraph.\n"
            "====\n"
        )
        doc = parse(source)
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        self.assertEqual(len(admonition.blocks), 2)

    def test_block_admonition_with_inline_markup(self) -> None:
        source = "[IMPORTANT]\n====\nUse *bold* here.\n====\n"
        doc = parse(source)
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        kinds = [type(n).__name__ for n in admonition.blocks[0].inlines]
        self.assertIn("Bold", kinds)

    def test_empty_block_admonition_body(self) -> None:
        # ``[NOTE]\n====\n====`` — empty body. The parser permits this;
        # the renderer shows just the kind header.
        doc = parse("[NOTE]\n====\n====\n")
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        self.assertEqual(admonition.blocks, ())

    def test_unknown_admonition_type_raises(self) -> None:
        # ``[INFO]`` is structurally well-formed but not a known kind.
        with self.assertRaises(ParseError) as ctx:
            parse("[INFO]\n====\nbody\n====\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNKNOWN_ADMONITION_TYPE,
        )
        self.assertEqual(ctx.exception.line, 1)

    def test_unterminated_admonition_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[NOTE]\n====\nbody\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_ADMONITION,
        )

    def test_directive_not_followed_by_fence_raises(self) -> None:
        # ``[NOTE]\nplain text`` — directive without the ==== fence.
        with self.assertRaises(ParseError) as ctx:
            parse("[NOTE]\nplain text\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)
        self.assertEqual(ctx.exception.line, 1)

    def test_blank_line_between_directive_and_fence_raises(self) -> None:
        # A blank line between the directive and the fence breaks the
        # binding, same as for ``[source]`` and ``[cols=…]``.
        with self.assertRaises(ParseError) as ctx:
            parse("[NOTE]\n\n====\nbody\n====\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)

    def test_directive_at_eof_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[NOTE]\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)

    def test_block_inside_admonition_body_raises(self) -> None:
        # Lists, code blocks, images, tables, nested admonitions, and
        # blockquotes are all rejected with the inline-only error.
        cases: tuple[tuple[str, str], ...] = (
            ("list", "[NOTE]\n====\n* an item\n====\n"),
            ("code fence", "[NOTE]\n====\n----\nx\n----\n====\n"),
            (
                "image macro",
                "[NOTE]\n====\nimage::cat.png[]\n====\n",
            ),
            (
                "nested admonition fence",
                "[NOTE]\n====\n[TIP]\n====\nx\n====\n====\n",
            ),
            (
                "table fence",
                "[NOTE]\n====\n|===\n|a|b\n|===\n====\n",
            ),
            (
                "blockquote fence",
                "[NOTE]\n====\n____\nq\n____\n====\n",
            ),
            (
                "heading",
                "[NOTE]\n====\n== heading\n====\n",
            ),
        )
        for desc, source in cases:
            with self.subTest(desc):
                with self.assertRaises(ParseError) as ctx:
                    parse(source)
                self.assertEqual(
                    ctx.exception.kind,
                    ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER,
                )

    def test_inline_error_inside_admonition_body_propagates(self) -> None:
        # An unterminated ``*`` inside the body raises BAD_INLINE_SPAN.
        with self.assertRaises(ParseError) as ctx:
            parse("[NOTE]\n====\n*unclosed\n====\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_INLINE_SPAN)

    def test_stray_admonition_fence_raises(self) -> None:
        # A bare ``====`` with no preceding directive — there's no kind
        # to associate the body with.
        with self.assertRaises(ParseError) as ctx:
            parse("====\nbody\n====\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.UNKNOWN_BLOCK)


# ---------------------------------------------------------------------------
# Blockquotes (step 15)
# ---------------------------------------------------------------------------


class BlockquoteNoDirectiveTests(unittest.TestCase):
    """``____\\n…\\n____`` parses to an unattributed :class:`Blockquote`."""

    def test_simple_blockquote(self) -> None:
        doc = parse("____\nA quote.\n____\n")
        self.assertEqual(len(doc.blocks), 1)
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        self.assertIsNone(quote.author)
        self.assertIsNone(quote.source)
        self.assertEqual(len(quote.blocks), 1)
        # Without a directive, the source_line is the opening fence line.
        self.assertEqual(quote.source_line, 1)

    def test_blockquote_with_multiple_paragraphs(self) -> None:
        source = (
            "____\n"
            "First paragraph.\n"
            "\n"
            "Second paragraph.\n"
            "____\n"
        )
        doc = parse(source)
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        self.assertEqual(len(quote.blocks), 2)

    def test_blockquote_inline_markup(self) -> None:
        doc = parse("____\nUse *bold* here.\n____\n")
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        kinds = [type(n).__name__ for n in quote.blocks[0].inlines]
        self.assertIn("Bold", kinds)

    def test_empty_blockquote_body(self) -> None:
        doc = parse("____\n____\n")
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        self.assertEqual(quote.blocks, ())


class BlockquoteWithDirectiveTests(unittest.TestCase):
    """``[quote, …]\\n____\\n…\\n____`` parses with attribution recorded."""

    def test_author_only(self) -> None:
        doc = parse("[quote, Mark Twain]\n____\nq\n____\n")
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        self.assertEqual(quote.author, "Mark Twain")
        self.assertIsNone(quote.source)

    def test_author_and_source(self) -> None:
        doc = parse(
            "[quote, Mark Twain, Notebook]\n____\nq\n____\n"
        )
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        self.assertEqual(quote.author, "Mark Twain")
        self.assertEqual(quote.source, "Notebook")

    def test_bare_quote_directive(self) -> None:
        # ``[quote]`` with no comma is the no-attribution shape.
        doc = parse("[quote]\n____\nq\n____\n")
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        self.assertIsNone(quote.author)
        self.assertIsNone(quote.source)

    def test_directive_source_line_is_directive_line(self) -> None:
        doc = parse("\n\n[quote, A]\n____\nq\n____\n")
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        # ``source_line`` is the directive line (line 3), not the
        # fence line (4).
        self.assertEqual(quote.source_line, 3)

    def test_attribution_whitespace_is_stripped(self) -> None:
        # ``[quote,  Author  ,  Source  ]`` — whitespace tolerated.
        doc = parse(
            "[quote,  Mark Twain  ,  Notebook  ]\n____\nq\n____\n"
        )
        quote = doc.blocks[0]
        assert isinstance(quote, Blockquote)
        self.assertEqual(quote.author, "Mark Twain")
        self.assertEqual(quote.source, "Notebook")


class BlockquoteErrorTests(unittest.TestCase):
    """Every error variant the blockquote parser raises."""

    def test_unterminated_blockquote_no_directive_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("____\nbody\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_BLOCKQUOTE,
        )

    def test_unterminated_blockquote_with_directive_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[quote, A]\n____\nbody\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_BLOCKQUOTE,
        )

    def test_directive_not_followed_by_fence_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[quote, A]\nplain text\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )

    def test_directive_followed_by_wrong_fence_raises(self) -> None:
        # ``[quote, A]\n----`` — a code fence after the directive.
        with self.assertRaises(ParseError) as ctx:
            parse("[quote, A]\n----\nq\n----\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )

    def test_empty_author_raises(self) -> None:
        # ``[quote,]`` — trailing comma, empty author.
        with self.assertRaises(ParseError) as ctx:
            parse("[quote,]\n____\nq\n____\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )

    def test_whitespace_only_author_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("[quote,   ]\n____\nq\n____\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )

    def test_empty_source_raises(self) -> None:
        # ``[quote, Author, ]`` — trailing comma, empty source.
        with self.assertRaises(ParseError) as ctx:
            parse("[quote, Author, ]\n____\nq\n____\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )

    def test_too_many_arguments_raises(self) -> None:
        # ``[quote, A, B, C]`` — three args, only two allowed.
        with self.assertRaises(ParseError) as ctx:
            parse("[quote, A, B, C]\n____\nq\n____\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )

    def test_block_inside_blockquote_body_raises(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("list", "____\n* an item\n____\n"),
            ("code fence", "____\n----\nx\n----\n____\n"),
            ("image macro", "____\nimage::cat.png[]\n____\n"),
            ("table fence", "____\n|===\n|a\n|===\n____\n"),
            ("admonition fence", "____\n[NOTE]\n====\nx\n====\n____\n"),
            ("heading", "____\n== heading\n____\n"),
        )
        for desc, source in cases:
            with self.subTest(desc):
                with self.assertRaises(ParseError) as ctx:
                    parse(source)
                self.assertEqual(
                    ctx.exception.kind,
                    ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER,
                )

    def test_inline_error_inside_blockquote_body_propagates(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("____\n*unclosed\n____\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_INLINE_SPAN)


# ---------------------------------------------------------------------------
# Welcome-note round trip
# ---------------------------------------------------------------------------


class WelcomeNoteRoundTripTests(unittest.TestCase):
    """The seed welcome note must parse without raising.

    This is an integration test for most of the supported grammar in a
    single shot: a level-1 title, paragraphs with inline markup,
    level-2 sections, an unordered list, an ordered list, and a fenced
    ``[source]``-less code block.
    """

    def test_round_trip(self) -> None:
        doc = parse(_WELCOME_SOURCE)
        # Title is present.
        self.assertIsNotNone(doc.title)
        # Document has a meaningful number of top-level blocks.
        self.assertGreater(len(doc.blocks), 0)

    def test_top_level_block_kinds(self) -> None:
        doc = parse(_WELCOME_SOURCE)
        kinds = [type(block).__name__ for block in doc.blocks]
        # The welcome note structure is: two paragraphs, then three
        # level-2 sections.
        self.assertEqual(kinds.count("Paragraph"), 2)
        self.assertEqual(kinds.count("Section"), 3)

    def test_features_section_contains_unordered_list(self) -> None:
        doc = parse(_WELCOME_SOURCE)
        sections = [block for block in doc.blocks if isinstance(block, Section)]
        features = sections[0]
        contains_unordered = any(
            isinstance(block, UnorderedList)
            for block in features.blocks
        )
        self.assertTrue(contains_unordered)

    def test_steps_section_contains_ordered_list(self) -> None:
        doc = parse(_WELCOME_SOURCE)
        sections = [block for block in doc.blocks if isinstance(block, Section)]
        steps = sections[1]
        contains_ordered = any(
            isinstance(block, OrderedList)
            for block in steps.blocks
        )
        self.assertTrue(contains_ordered)

    def test_code_section_contains_code_block(self) -> None:
        doc = parse(_WELCOME_SOURCE)
        sections = [block for block in doc.blocks if isinstance(block, Section)]
        code_section = sections[2]
        code_blocks = [
            block for block in code_section.blocks
            if isinstance(block, CodeBlock)
        ]
        self.assertEqual(len(code_blocks), 1)
        cb = code_blocks[0]
        self.assertIn("def hello", cb.content)


# ---------------------------------------------------------------------------
# Heterogeneous document composition
# ---------------------------------------------------------------------------


class DocumentCompositionTests(unittest.TestCase):
    """Heterogeneous block composition in document order.

    :class:`WelcomeNoteRoundTripTests` already proves that paragraphs,
    sections, lists, and a code block coexist in one document. This
    class adds the two shapes that one does not reach: a discarded
    document-header attribute run sitting above a *top-level* table,
    and an inline error raised from inside a list item.

    Every individual construct here has focused coverage elsewhere
    (header attributes, the cols directive, table arity, list and
    section parsing). What these synthetic sources add — and the only
    thing they are responsible for — is that the blocks compose in the
    expected order rather than swallowing or reordering one another.
    """

    _SOURCE: str = (
        "= Title\n"
        ":author: Me\n"
        ":tags: a, b\n"
        "\n"
        "A lead paragraph.\n"
        "\n"
        '[cols="3,1"]\n'
        "|===\n"
        "|Ingredient |Grams\n"
        "|Flour |400\n"
        "|===\n"
        "\n"
        "== One\n"
        "\n"
        "Body one.\n"
        "\n"
        "== Two\n"
        "\n"
        "Body two.\n"
    )

    def test_header_attrs_table_and_sections_compose_in_order(self) -> None:
        doc = parse(self._SOURCE)
        # The level-1 title is captured; the header attribute run that
        # follows it is discarded rather than emitted as blocks.
        self.assertIsNotNone(doc.title)
        kinds = [type(block).__name__ for block in doc.blocks]
        self.assertEqual(
            kinds, ["Paragraph", "Table", "Section", "Section"]
        )

    def test_top_level_table_keeps_its_cols_directive(self) -> None:
        # The cols directive sits *between* the lead paragraph and the
        # first section; it is parsed in place, not absorbed by either
        # neighbour.
        doc = parse(self._SOURCE)
        table = doc.blocks[1]
        assert isinstance(table, Table)
        self.assertEqual(table.column_proportions, (3, 1))
        self.assertEqual(len(table.rows), 2)

    def test_unsupported_link_scheme_in_list_item_reports_its_line(
        self,
    ) -> None:
        # An inline error raised from inside a list item must surface
        # with the *list item's* line number, not the document or list
        # start. BAD_INLINE_SPAN in a list item is covered by ListTests;
        # this adds the link-scheme variant on a non-first line.
        with self.assertRaises(ParseError) as ctx:
            parse("intro.\n\n* alpha\n* beta link:recipe://x[here]\n")
        self.assertEqual(
            ctx.exception.kind, ParseErrorKind.UNSUPPORTED_LINK_SCHEME
        )
        self.assertEqual(ctx.exception.line, 4)


# ---------------------------------------------------------------------------
# Document header attribute entries
# ---------------------------------------------------------------------------


class DocumentHeaderAttributeTests(unittest.TestCase):
    """A contiguous run of ``:name: value`` entries at the document
    header is consumed and discarded by the parser.

    The AST has no field for attributes — no consumer in the
    application currently reads them — so the test exercises that
    each entry reaches the parser, is recognised as valid, and does
    not appear in the resulting block list.
    """

    def test_single_header_attribute_after_title(self) -> None:
        doc = parse("= Title\n:author: Me\n\nbody\n")
        self.assertEqual(doc.title, (_t("Title", 1),))
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)

    def test_multiple_header_attributes_after_title(self) -> None:
        doc = parse(
            "= Title\n"
            ":author: Me\n"
            ":revdate: 2026-04-14\n"
            ":tags: favorite, reference\n"
            "\n"
            "body\n"
        )
        self.assertEqual(doc.title, (_t("Title", 1),))
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)

    def test_header_attributes_with_no_title(self) -> None:
        # Without a title, header entries are still consumed at the
        # very top of the document.
        doc = parse(":author: Me\n:revdate: 2026-04-14\n\nbody\n")
        self.assertIsNone(doc.title)
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)

    def test_bare_setter_form_is_consumed(self) -> None:
        doc = parse("= Title\n:doctype:\n\nbody\n")
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)

    def test_blank_line_between_attributes_is_tolerated(self) -> None:
        # A single blank between two attribute entries does not close
        # the header — the run extends across blanks until a real
        # body block appears.
        doc = parse(
            "= Title\n"
            ":author: Me\n"
            "\n"
            ":revdate: 2026-04-14\n"
            "\n"
            "body\n"
        )
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Paragraph)

    def test_attribute_after_first_body_block_is_unknown_block(self) -> None:
        # Once the first body block has been parsed, any subsequent
        # attribute entry is positionally invalid.
        with self.assertRaises(ParseError) as ctx:
            parse("= Title\n\nfirst body.\n\n:author: Me\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNKNOWN_BLOCK,
        )

    def test_header_attributes_dont_block_section_recognition(self) -> None:
        # After the header attribute run, a level-2 heading still
        # opens a section the normal way.
        doc = parse(
            "= Title\n"
            ":author: Me\n"
            "\n"
            "== Section\n"
            "\n"
            "body\n"
        )
        self.assertEqual(len(doc.blocks), 1)
        self.assertIsInstance(doc.blocks[0], Section)


# ---------------------------------------------------------------------------
# Multi-line single-line admonitions (§3.5)
# ---------------------------------------------------------------------------


class MultiLineSingleAdmonitionTests(unittest.TestCase):
    """``KIND: text`` followed by continuation lines absorbs them all
    into one paragraph inside the admonition.

    Without this rule, the second line would become a stray
    paragraph *outside* the admonition box.
    """

    def test_two_line_note_absorbs_continuation(self) -> None:
        doc = parse("NOTE: first line\nsecond line\n")
        self.assertEqual(len(doc.blocks), 1)
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        self.assertEqual(admonition.kind, AdmonitionKind.NOTE)
        self.assertEqual(len(admonition.blocks), 1)
        # The paragraph contains: 'first line' + soft break + 'second line'.
        paragraph = admonition.blocks[0]
        text_pieces = [
            n.content for n in paragraph.inlines if isinstance(n, Text)
        ]
        self.assertIn("first line", text_pieces)
        self.assertIn("second line", text_pieces)
        # The source-line boundary is recorded as exactly one SoftBreak,
        # not a literal-newline Text run.
        soft_breaks = [
            n for n in paragraph.inlines if isinstance(n, SoftBreak)
        ]
        self.assertEqual(len(soft_breaks), 1)
        self.assertNotIn("\n", text_pieces)

    def test_blank_line_terminates_continuation(self) -> None:
        # A blank line ends the admonition; the second paragraph is
        # *not* part of the admonition box.
        doc = parse("NOTE: first line\n\nsecond paragraph\n")
        self.assertEqual(len(doc.blocks), 2)
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        # The note's paragraph has only the first line.
        self.assertEqual(len(admonition.blocks), 1)
        admonition_text = [
            n.content
            for n in admonition.blocks[0].inlines
            if isinstance(n, Text)
        ]
        self.assertEqual(admonition_text, ["first line"])
        # The trailing paragraph is at the document level.
        self.assertIsInstance(doc.blocks[1], Paragraph)

    def test_block_start_after_admonition_ends_continuation(self) -> None:
        # A list bullet at block-start ends the admonition's run
        # without a blank between.
        doc = parse("TIP: hint\n* a list item\n")
        self.assertEqual(len(doc.blocks), 2)
        self.assertIsInstance(doc.blocks[0], Admonition)
        self.assertIsInstance(doc.blocks[1], UnorderedList)

    def test_continuation_line_inline_error_points_at_correct_line(self) -> None:
        # A bad inline span on a continuation line must be reported
        # against that line, not the admonition's opener.
        with self.assertRaises(ParseError) as ctx:
            parse("NOTE: first\nsecond *unclosed\n")
        self.assertEqual(ctx.exception.line, 2)

    def test_inline_markup_on_continuation_line(self) -> None:
        doc = parse("NOTE: plain\nthen *bold* text\n")
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        kinds = [type(n).__name__ for n in admonition.blocks[0].inlines]
        self.assertIn("Bold", kinds)

    def test_three_line_admonition_with_bold_on_each(self) -> None:
        doc = parse(
            "WARNING: *one*\n"
            "*two*\n"
            "*three*\n"
        )
        admonition = doc.blocks[0]
        assert isinstance(admonition, Admonition)
        bolds = [
            n
            for n in admonition.blocks[0].inlines
            if isinstance(n, Bold)
        ]
        self.assertEqual(len(bolds), 3)


# ---------------------------------------------------------------------------
# :tags: attribute parsing
# ---------------------------------------------------------------------------


class TagsAttributeHappyPathTests(unittest.TestCase):
    """The ``:tags:`` header attribute populates :attr:`Document.tags`."""

    def test_absent_tags_yields_empty_tuple(self) -> None:
        doc = parse("= T\n\nbody\n")
        self.assertEqual(doc.tags, ())

    def test_single_tag(self) -> None:
        doc = parse("= T\n:tags: baking\n\nbody\n")
        self.assertEqual(doc.tags, ("baking",))

    def test_multiple_tags(self) -> None:
        doc = parse("= T\n:tags: baking, bread\n\nbody\n")
        self.assertEqual(doc.tags, ("baking", "bread"))

    def test_whitespace_around_entries_is_stripped(self) -> None:
        doc = parse("= T\n:tags:  baking  ,   bread  \n\nbody\n")
        self.assertEqual(doc.tags, ("baking", "bread"))

    def test_trailing_comma_tolerated(self) -> None:
        doc = parse("= T\n:tags: foo, bar,\n\nbody\n")
        self.assertEqual(doc.tags, ("bar", "foo"))

    def test_dedup_preserves_set(self) -> None:
        doc = parse("= T\n:tags: bread, baking, bread, baking\n\nbody\n")
        self.assertEqual(doc.tags, ("baking", "bread"))

    def test_alphabetical_sort(self) -> None:
        doc = parse("= T\n:tags: zeta, alpha, mu\n\nbody\n")
        self.assertEqual(doc.tags, ("alpha", "mu", "zeta"))

    def test_uppercase_is_folded_to_lowercase(self) -> None:
        doc = parse("= T\n:tags: BAKING, Bread\n\nbody\n")
        self.assertEqual(doc.tags, ("baking", "bread"))

    def test_bare_setter_yields_empty(self) -> None:
        doc = parse("= T\n:tags:\n\nbody\n")
        self.assertEqual(doc.tags, ())

    def test_whitespace_only_value_yields_empty(self) -> None:
        # ``:tags:   `` after lexer right-strip becomes a bare setter
        # too; both shapes resolve to ``()``.
        doc = parse("= T\n:tags:   \n\nbody\n")
        self.assertEqual(doc.tags, ())

    def test_empty_entries_between_commas_dropped(self) -> None:
        doc = parse("= T\n:tags: foo, , bar\n\nbody\n")
        self.assertEqual(doc.tags, ("bar", "foo"))

    def test_digits_and_hyphens_in_charset(self) -> None:
        doc = parse("= T\n:tags: tag-1, 2024-recipe, plain\n\nbody\n")
        self.assertEqual(doc.tags, ("2024-recipe", "plain", "tag-1"))


class TagsAttributeRejectionTests(unittest.TestCase):
    """Malformed individual tag values raise BAD_TAG_VALUE."""

    def test_space_inside_tag_rejected(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("= T\n:tags: foo bar\n\nbody\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_TAG_VALUE)

    def test_leading_hyphen_rejected(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("= T\n:tags: -foo\n\nbody\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_TAG_VALUE)

    def test_underscore_rejected(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("= T\n:tags: foo_bar\n\nbody\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_TAG_VALUE)

    def test_punctuation_rejected(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("= T\n:tags: foo.bar\n\nbody\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_TAG_VALUE)

    def test_non_ascii_rejected(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("= T\n:tags: café\n\nbody\n")
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_TAG_VALUE)


class TagsAttributeDuplicateTests(unittest.TestCase):
    """Two ``:tags:`` entries in the same header raise."""

    def test_two_tags_entries_rejected(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("= T\n:tags: foo\n:tags: bar\n\nbody\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.DUPLICATE_TAG_ATTRIBUTE,
        )

    def test_two_tags_with_other_attr_between_rejected(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse("= T\n:tags: foo\n:author: me\n:tags: bar\n\nbody\n")
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.DUPLICATE_TAG_ATTRIBUTE,
        )


if __name__ == "__main__":
    unittest.main()
