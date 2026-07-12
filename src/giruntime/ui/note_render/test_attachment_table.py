"""Tests for :mod:`giruntime.ui.note_render.attachment_table`.

The transform is a **pure function** over the AST plus attachment
metadata, so every test here runs with no display, no GTK widget, and no
storage — which is the point of expanding the macro before the render
walk rather than inside it.
"""

from __future__ import annotations

import unittest

from asciidoc.ast import (
    AttachmentLink,
    AttachmentTable,
    Document,
    Italic,
    Paragraph,
    Section,
    Table,
    Text,
)
from asciidoc.parser import parse
from enums import AttachmentTableColumn
from giruntime.ui.note_render.attachment_table import (
    _EMPTY_TABLE_TEXT,
    expand_attachment_tables,
)
from models.attachment import Attachment


def _attachment(filename: str, byte_size: int) -> Attachment:
    return Attachment(
        id=f"att-{filename}",
        note_id="n1",
        filename=filename,
        byte_size=byte_size,
    )


class ExpansionTests(unittest.TestCase):
    """``attachments::[]`` becomes an ordinary :class:`Table`."""

    def test_macro_expands_to_a_table(self) -> None:
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1024)],
        )
        (block,) = expanded.blocks
        self.assertIsInstance(block, Table)

    def test_header_row_carries_the_column_labels(self) -> None:
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1024)],
        )
        table = expanded.blocks[0]
        assert isinstance(table, Table)
        header_texts = [
            cell.inlines[0].content
            for cell in table.rows[0].cells
            if isinstance(cell.inlines[0], Text)
        ]
        self.assertEqual(header_texts, ["Name", "Size"])

    def test_one_row_per_attachment_in_the_given_order(self) -> None:
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1), _attachment("b.png", 2)],
        )
        table = expanded.blocks[0]
        assert isinstance(table, Table)
        # Header + two data rows.
        self.assertEqual(len(table.rows), 3)
        names = [row.cells[0].inlines[0] for row in table.rows[1:]]
        self.assertEqual(
            [n.filename for n in names if isinstance(n, AttachmentLink)],
            ["a.pdf", "b.png"],
        )

    def test_name_cell_is_an_attachment_link(self) -> None:
        # The generated save link is the *same* node a hand-written
        # ``attachment:`` macro parses to — one activation mechanism.
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1024)],
        )
        table = expanded.blocks[0]
        assert isinstance(table, Table)
        cell = table.rows[1].cells[0]
        self.assertEqual(
            cell.inlines,
            (
                AttachmentLink(
                    filename="a.pdf",
                    text=(Text(content="a.pdf", source_line=1),),
                    source_line=1,
                ),
            ),
        )

    def test_size_cell_is_human_readable(self) -> None:
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1024)],
        )
        table = expanded.blocks[0]
        assert isinstance(table, Table)
        size_cell = table.rows[1].cells[1]
        self.assertEqual(
            size_cell.inlines,
            (Text(content="1 KB", source_line=1),),
        )

    def test_cols_attribute_selects_and_orders_the_columns(self) -> None:
        document = parse('attachments::[cols="size,name"]\n')
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1024)],
        )
        table = expanded.blocks[0]
        assert isinstance(table, Table)
        header = table.rows[0]
        self.assertEqual(
            [
                cell.inlines[0].content
                for cell in header.cells
                if isinstance(cell.inlines[0], Text)
            ],
            ["Size", "Name"],
        )
        self.assertEqual(len(table.rows[1].cells), 2)

    def test_generated_table_has_no_column_proportions(self) -> None:
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1)],
        )
        table = expanded.blocks[0]
        assert isinstance(table, Table)
        self.assertIsNone(table.column_proportions)


class EmptyAttachmentListTests(unittest.TestCase):
    """A note with no attachments expands to an italic paragraph."""

    def test_expands_to_a_paragraph_not_a_table(self) -> None:
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(document, [])
        (block,) = expanded.blocks
        self.assertIsInstance(block, Paragraph)

    def test_paragraph_is_the_italic_no_attachments_notice(self) -> None:
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(document, [])
        paragraph = expanded.blocks[0]
        assert isinstance(paragraph, Paragraph)
        self.assertEqual(
            paragraph.inlines,
            (
                Italic(
                    children=(
                        Text(content=_EMPTY_TABLE_TEXT, source_line=1),
                    ),
                    source_line=1,
                ),
            ),
        )


class RecursionTests(unittest.TestCase):
    """The macro is a top-level block; only sections are descended."""

    def test_macro_inside_a_section_is_expanded(self) -> None:
        document = parse("== S\n\nattachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1)],
        )
        section = expanded.blocks[0]
        assert isinstance(section, Section)
        self.assertIsInstance(section.blocks[0], Table)

    def test_nested_section_is_expanded(self) -> None:
        document = parse("== S\n\n=== T\n\nattachments::[]\n")
        expanded = expand_attachment_tables(document, [])
        outer = expanded.blocks[0]
        assert isinstance(outer, Section)
        inner = outer.blocks[0]
        assert isinstance(inner, Section)
        self.assertIsInstance(inner.blocks[0], Paragraph)

    def test_no_attachment_table_node_survives(self) -> None:
        document = parse("== S\n\nattachments::[]\n\nattachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1)],
        )
        section = expanded.blocks[0]
        assert isinstance(section, Section)
        for block in section.blocks:
            self.assertNotIsInstance(block, AttachmentTable)


class UntouchedDocumentTests(unittest.TestCase):
    """A document with no macro is rebuilt unchanged."""

    def test_document_without_the_macro_is_value_equal(self) -> None:
        document = parse("= T\n:tags: x\n\nSome *prose*.\n")
        self.assertEqual(
            expand_attachment_tables(document, [_attachment("a.pdf", 1)]),
            document,
        )

    def test_title_and_tags_are_preserved(self) -> None:
        document = parse("= T\n:tags: x\n\nattachments::[]\n")
        expanded = expand_attachment_tables(document, [])
        self.assertIsInstance(expanded, Document)
        self.assertEqual(expanded.title, document.title)
        self.assertEqual(expanded.tags, ("x",))


class ColumnCoverageTests(unittest.TestCase):
    """Every column has a heading and a cell builder."""

    def test_every_column_has_a_heading(self) -> None:
        document = parse("attachments::[]\n")
        expanded = expand_attachment_tables(
            document,
            [_attachment("a.pdf", 1)],
        )
        table = expanded.blocks[0]
        assert isinstance(table, Table)
        self.assertEqual(
            len(table.rows[0].cells),
            len(tuple(AttachmentTableColumn)),
        )


if __name__ == "__main__":
    unittest.main()
