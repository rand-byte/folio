"""Tests for :mod:`asciidoc.ast`.

These are smoke tests rather than behavioural tests: the AST module
defines no behaviour beyond what dataclasses provide. We assert the
shape (field names, frozenness) and the basic construction shape so
that accidentally renaming a field or unfreezing a node is caught here
rather than later in the parser.
"""

from __future__ import annotations

import typing
import unittest
from dataclasses import FrozenInstanceError, fields, is_dataclass

from asciidoc.ast import (
    Admonition,
    Blockquote,
    Bold,
    CodeBlock,
    Document,
    HardBreak,
    Image,
    InlineNode,
    Italic,
    ListItem,
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
from enums import AdmonitionKind


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_text(content: str = "hello", line: int = 1) -> Text:
    return Text(content=content, source_line=line)


# ---------------------------------------------------------------------------
# Construction & field-set
# ---------------------------------------------------------------------------


class AstNodeShapeTests(unittest.TestCase):
    """Each node is a dataclass with a fixed, exact field set."""

    def test_each_class_is_a_dataclass(self) -> None:
        classes = (
            Text,
            Bold,
            Italic,
            Strikethrough,
            Underline,
            SoftBreak,
            HardBreak,
            Paragraph,
            Section,
            ListItem,
            OrderedList,
            UnorderedList,
            CodeBlock,
            Image,
            TableCell,
            TableRow,
            Table,
            Admonition,
            Blockquote,
            Document,
        )
        for cls in classes:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(
                    is_dataclass(cls),
                    f"{cls.__name__} should be a dataclass",
                )

    def test_field_sets(self) -> None:
        cases: tuple[tuple[type, set[str]], ...] = (
            (Text, {"content", "source_line"}),
            (Bold, {"children", "source_line"}),
            (Italic, {"children", "source_line"}),
            (Strikethrough, {"children", "source_line"}),
            (Underline, {"children", "source_line"}),
            (SoftBreak, {"source_line"}),
            (HardBreak, {"source_line"}),
            (Paragraph, {"inlines", "source_line"}),
            (Section, {"level", "title", "blocks", "source_line"}),
            (ListItem, {"inlines", "children", "source_line"}),
            (OrderedList, {"items", "source_line"}),
            (UnorderedList, {"items", "source_line"}),
            (CodeBlock, {"language", "content", "source_line"}),
            (Image, {"filename", "attrs", "source_line"}),
            (TableCell, {"inlines", "source_line"}),
            (TableRow, {"cells", "source_line"}),
            (Table, {"rows", "column_proportions", "source_line"}),
            (Admonition, {"kind", "blocks", "source_line"}),
            (
                Blockquote,
                {"author", "source", "blocks", "source_line"},
            ),
            (Document, {"title", "tags", "blocks", "source_line"}),
        )
        for cls, expected in cases:
            with self.subTest(cls=cls.__name__):
                names = {f.name for f in fields(cls)}
                self.assertEqual(names, expected)


class AstFrozenTests(unittest.TestCase):
    """Mutating any field on any node raises :class:`FrozenInstanceError`."""

    def test_text_is_frozen(self) -> None:
        node = _make_text()
        with self.assertRaises(FrozenInstanceError):
            node.content = "mutated"  # type: ignore[misc]

    def test_soft_break_is_frozen(self) -> None:
        node = SoftBreak(source_line=2)
        with self.assertRaises(FrozenInstanceError):
            node.source_line = 3  # type: ignore[misc]

    def test_hard_break_is_frozen(self) -> None:
        node = HardBreak(source_line=2)
        with self.assertRaises(FrozenInstanceError):
            node.source_line = 3  # type: ignore[misc]

    def test_section_is_frozen(self) -> None:
        section = Section(
            level=2,
            title=(_make_text("t"),),
            blocks=(),
            source_line=1,
        )
        with self.assertRaises(FrozenInstanceError):
            section.level = 3  # type: ignore[misc]

    def test_document_is_frozen(self) -> None:
        document = Document(title=None, tags=(), blocks=(), source_line=1)
        with self.assertRaises(FrozenInstanceError):
            document.blocks = ()  # type: ignore[misc]

    def test_table_is_frozen(self) -> None:
        cell = TableCell(inlines=(_make_text("a"),), source_line=2)
        row = TableRow(cells=(cell,), source_line=2)
        table = Table(rows=(row,), column_proportions=None, source_line=1)
        with self.assertRaises(FrozenInstanceError):
            table.rows = ()  # type: ignore[misc]

    def test_admonition_is_frozen(self) -> None:
        admonition = Admonition(
            kind=AdmonitionKind.NOTE,
            blocks=(),
            source_line=1,
        )
        with self.assertRaises(FrozenInstanceError):
            admonition.kind = AdmonitionKind.TIP  # type: ignore[misc]

    def test_blockquote_is_frozen(self) -> None:
        blockquote = Blockquote(
            author=None,
            source=None,
            blocks=(),
            source_line=1,
        )
        with self.assertRaises(FrozenInstanceError):
            blockquote.author = "Anon"  # type: ignore[misc]


class AstConstructionTests(unittest.TestCase):
    """Each node accepts the documented arguments and exposes them back."""

    def test_text(self) -> None:
        node = Text(content="hi", source_line=4)
        self.assertEqual(node.content, "hi")
        self.assertEqual(node.source_line, 4)

    def test_soft_break(self) -> None:
        node = SoftBreak(source_line=2)
        self.assertEqual(node.source_line, 2)

    def test_hard_break(self) -> None:
        node = HardBreak(source_line=2)
        self.assertEqual(node.source_line, 2)

    def test_bold_holds_children_as_tuple(self) -> None:
        node = Bold(
            children=(_make_text("a"), _make_text("b")),
            source_line=2,
        )
        self.assertIsInstance(node.children, tuple)
        self.assertEqual(len(node.children), 2)
        self.assertEqual(node.source_line, 2)

    def test_section_records_level_and_title(self) -> None:
        title = (_make_text("Heading"),)
        section = Section(
            level=3,
            title=title,
            blocks=(Paragraph(inlines=(_make_text("body"),), source_line=2),),
            source_line=1,
        )
        self.assertEqual(section.level, 3)
        self.assertIs(section.title, title)
        self.assertEqual(len(section.blocks), 1)

    def test_code_block_language_optional(self) -> None:
        with_lang = CodeBlock(
            language="python",
            content="print('x')",
            source_line=10,
        )
        no_lang = CodeBlock(
            language=None,
            content="raw",
            source_line=10,
        )
        self.assertEqual(with_lang.language, "python")
        self.assertIsNone(no_lang.language)

    def test_image_carries_filename_and_attrs(self) -> None:
        image = Image(filename="cat.png", attrs="alt=Cat", source_line=5)
        self.assertEqual(image.filename, "cat.png")
        self.assertEqual(image.attrs, "alt=Cat")

    def test_document_title_optional(self) -> None:
        without = Document(title=None, tags=(), blocks=(), source_line=1)
        with_title = Document(
            title=(_make_text("Title"),),
            tags=(),
            blocks=(),
            source_line=1,
        )
        self.assertIsNone(without.title)
        self.assertIsNotNone(with_title.title)

    def test_document_tags_field(self) -> None:
        doc = Document(
            title=None,
            tags=("baking", "bread"),
            blocks=(),
            source_line=1,
        )
        self.assertEqual(doc.tags, ("baking", "bread"))

    def test_lists_hold_list_items(self) -> None:
        item = ListItem(inlines=(_make_text("x"),), children=(), source_line=2)
        ordered = OrderedList(items=(item,), source_line=2)
        unordered = UnorderedList(items=(item,), source_line=2)
        self.assertEqual(ordered.items, (item,))
        self.assertEqual(unordered.items, (item,))

    def test_list_item_holds_child_lists_and_stays_frozen(self) -> None:
        leaf = ListItem(inlines=(_make_text("Produce"),), children=(), source_line=3)
        sublist = UnorderedList(items=(leaf,), source_line=3)
        parent = ListItem(
            inlines=(_make_text("Shopping"),),
            children=(sublist,),
            source_line=2,
        )
        self.assertEqual(parent.children, (sublist,))
        self.assertEqual(leaf.children, ())
        with self.assertRaises(FrozenInstanceError):
            parent.children = ()  # type: ignore[misc]

    def test_table_cell_holds_inline_tuple(self) -> None:
        cell = TableCell(inlines=(_make_text("x"),), source_line=3)
        self.assertEqual(cell.inlines, (_make_text("x"),))
        self.assertEqual(cell.source_line, 3)

    def test_table_row_holds_cells(self) -> None:
        cell_a = TableCell(inlines=(_make_text("a"),), source_line=4)
        cell_b = TableCell(inlines=(_make_text("b"),), source_line=4)
        row = TableRow(cells=(cell_a, cell_b), source_line=4)
        self.assertEqual(len(row.cells), 2)
        self.assertEqual(row.source_line, 4)

    def test_table_records_rows_and_optional_proportions(self) -> None:
        cell = TableCell(inlines=(_make_text("a"),), source_line=2)
        row = TableRow(cells=(cell,), source_line=2)
        # Without a [cols=…] directive, column_proportions is None.
        without_directive = Table(
            rows=(row,),
            column_proportions=None,
            source_line=1,
        )
        self.assertIsNone(without_directive.column_proportions)
        # With a directive, the proportions are recorded as a tuple.
        with_directive = Table(
            rows=(row,),
            column_proportions=(1, 2, 3),
            source_line=1,
        )
        self.assertEqual(with_directive.column_proportions, (1, 2, 3))

    def test_admonition_records_kind_and_blocks(self) -> None:
        para = Paragraph(inlines=(_make_text("body"),), source_line=2)
        for kind in AdmonitionKind:
            with self.subTest(kind=kind):
                admonition = Admonition(
                    kind=kind,
                    blocks=(para,),
                    source_line=1,
                )
                self.assertEqual(admonition.kind, kind)
                self.assertEqual(admonition.blocks, (para,))
                self.assertEqual(admonition.source_line, 1)

    def test_admonition_accepts_empty_body(self) -> None:
        # An empty body (no paragraphs) is permitted: the parser
        # produces this when ``[NOTE]\n====\n====`` is seen.
        admonition = Admonition(
            kind=AdmonitionKind.NOTE,
            blocks=(),
            source_line=1,
        )
        self.assertEqual(admonition.blocks, ())

    def test_admonition_holds_multiple_paragraphs(self) -> None:
        p1 = Paragraph(inlines=(_make_text("first"),), source_line=2)
        p2 = Paragraph(inlines=(_make_text("second"),), source_line=4)
        admonition = Admonition(
            kind=AdmonitionKind.TIP,
            blocks=(p1, p2),
            source_line=1,
        )
        self.assertEqual(len(admonition.blocks), 2)

    def test_blockquote_optional_author_and_source(self) -> None:
        # All four combinations of author/source are valid.
        for author, source in (
            (None, None),
            ("Author", None),
            ("Author", "Source"),
        ):
            with self.subTest(author=author, source=source):
                quote = Blockquote(
                    author=author,
                    source=source,
                    blocks=(),
                    source_line=1,
                )
                self.assertEqual(quote.author, author)
                self.assertEqual(quote.source, source)

    def test_blockquote_records_blocks(self) -> None:
        para = Paragraph(inlines=(_make_text("quoted"),), source_line=2)
        quote = Blockquote(
            author="Mark Twain",
            source="A Book",
            blocks=(para,),
            source_line=1,
        )
        self.assertEqual(quote.blocks, (para,))


class InlineUnionMembershipTests(unittest.TestCase):
    """The structural line-break joiners are members of the inline union."""

    def test_break_joiners_are_inline_union_members(self) -> None:
        members = set(typing.get_args(getattr(InlineNode, "__value__")))
        self.assertIn(SoftBreak, members)
        self.assertIn(HardBreak, members)


class AstEqualityTests(unittest.TestCase):
    """Frozen tuples make AST equality structural and useful in tests."""

    def test_two_paragraphs_with_equal_inlines_are_equal(self) -> None:
        a = Paragraph(inlines=(_make_text("x"),), source_line=1)
        b = Paragraph(inlines=(_make_text("x"),), source_line=1)
        self.assertEqual(a, b)

    def test_paragraphs_with_different_lines_differ(self) -> None:
        a = Paragraph(inlines=(_make_text("x"),), source_line=1)
        b = Paragraph(inlines=(_make_text("x"),), source_line=2)
        self.assertNotEqual(a, b)

    def test_hard_break_and_soft_break_are_unequal(self) -> None:
        # Same field value, different type: the renderer must be able to
        # tell a reflow joiner from a forced break.
        self.assertNotEqual(HardBreak(source_line=1), SoftBreak(source_line=1))


if __name__ == "__main__":
    unittest.main()
