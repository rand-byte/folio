"""Tests for :mod:`notes_app.asciidoc.parser`.

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

from notes_app.asciidoc.ast import (
    Bold,
    CodeBlock,
    Image,
    InlineNode,
    ListItem,
    OrderedList,
    Paragraph,
    Section,
    Text,
    UnorderedList,
)
from notes_app.asciidoc.parser import parse
from notes_app.config.defaults import SEED_WELCOME_NOTE_SOURCE
from notes_app.enums import ParseErrorKind
from notes_app.models.parse_error import ParseError


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

    def test_multi_line_paragraph_joined_with_newlines(self) -> None:
        doc = parse("one\ntwo\nthree\n")
        self.assertEqual(len(doc.blocks), 1)
        para = doc.blocks[0]
        assert isinstance(para, Paragraph)
        self.assertEqual(
            para.inlines,
            (
                _t("one", 1),
                _t("\n", 2),
                _t("two", 2),
                _t("\n", 3),
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

    def test_blank_line_breaks_list(self) -> None:
        doc = parse("* a\n* b\n\n* c\n")
        self.assertEqual(len(doc.blocks), 2)
        for block in doc.blocks:
            self.assertIsInstance(block, UnorderedList)
        first, second = doc.blocks
        assert isinstance(first, UnorderedList)
        assert isinstance(second, UnorderedList)
        self.assertEqual(len(first.items), 2)
        self.assertEqual(len(second.items), 1)

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
            ("table fence", "|===\n|cell\n|===\n"),
            ("blockquote fence", "____\nquote\n____\n"),
            ("attribute entry", ":doctype: book\n"),
            ("compound attribute", ":source-highlighter: rouge\n"),
            ("line comment", "// a comment\n"),
            ("admonition opener", "[NOTE]\n====\nbody\n====\n"),
            ("quote directive", "[quote]\n----\nq\n----\n"),
            ("cols directive", "[cols=\"1,1\"]\n|===\n|a|b\n|===\n"),
            (
                "[source,] empty lang falls through then rejected",
                "[source,]\n----\nx\n----\n",
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
        doc = parse(SEED_WELCOME_NOTE_SOURCE)
        # Title is present.
        self.assertIsNotNone(doc.title)
        # Document has a meaningful number of top-level blocks.
        self.assertGreater(len(doc.blocks), 0)

    def test_top_level_block_kinds(self) -> None:
        doc = parse(SEED_WELCOME_NOTE_SOURCE)
        kinds = [type(block).__name__ for block in doc.blocks]
        # The welcome note structure is: two paragraphs, then three
        # level-2 sections.
        self.assertEqual(kinds.count("Paragraph"), 2)
        self.assertEqual(kinds.count("Section"), 3)

    def test_features_section_contains_unordered_list(self) -> None:
        doc = parse(SEED_WELCOME_NOTE_SOURCE)
        sections = [block for block in doc.blocks if isinstance(block, Section)]
        features = sections[0]
        contains_unordered = any(
            isinstance(block, UnorderedList)
            for block in features.blocks
        )
        self.assertTrue(contains_unordered)

    def test_steps_section_contains_ordered_list(self) -> None:
        doc = parse(SEED_WELCOME_NOTE_SOURCE)
        sections = [block for block in doc.blocks if isinstance(block, Section)]
        steps = sections[1]
        contains_ordered = any(
            isinstance(block, OrderedList)
            for block in steps.blocks
        )
        self.assertTrue(contains_ordered)

    def test_code_section_contains_code_block(self) -> None:
        doc = parse(SEED_WELCOME_NOTE_SOURCE)
        sections = [block for block in doc.blocks if isinstance(block, Section)]
        code_section = sections[2]
        code_blocks = [
            block for block in code_section.blocks
            if isinstance(block, CodeBlock)
        ]
        self.assertEqual(len(code_blocks), 1)
        cb = code_blocks[0]
        self.assertIn("def hello", cb.content)


if __name__ == "__main__":
    unittest.main()
