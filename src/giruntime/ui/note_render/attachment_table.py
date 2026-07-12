"""Expand ``attachments::[]`` macros into ordinary AST tables.

Principles & invariants
-----------------------
* This module is a **pure AST → AST transform**: given a parsed
  :class:`~asciidoc.ast.Document` and the attachment *metadata* of the
  note it belongs to, it returns a new document in which every
  :class:`~asciidoc.ast.AttachmentTable` node has been replaced by an
  ordinary :class:`~asciidoc.ast.Table` (or, for a note with no
  attachments, a single italic paragraph). No GTK, no storage, no I/O —
  it is unit-testable with no display.
* It lives under ``note_render`` because this is the **first layer
  allowed to know about both the AST and the storage models**:
  :mod:`asciidoc` is storage-free by construction, and
  :class:`models.attachment.Attachment` may not be imported there. The
  transform is therefore the seam where "the note asked for a table of
  its attachments" (a parse-time fact) meets "these are its attachments"
  (a render-time fact).
* **The expansion produces nodes the renderer already knows how to
  emit** — that is the whole design. The renderer owns column geometry
  (``Pango.TabArray``, live column width, cell truncation, the header
  wash) exactly once, in ``_emit_table``; a second table-emitting path
  would duplicate all of it. Likewise a generated name cell holds an
  :class:`~asciidoc.ast.AttachmentLink`, the *same* node a hand-written
  ``attachment:`` macro produces, so every save link in the table goes
  through one activation mechanism rather than two.
* :class:`~asciidoc.ast.AttachmentTable` is a **top-level block**: the
  transform recurses into :class:`~asciidoc.ast.Section` and nowhere
  else, matching the parser, which cannot produce one inside an
  admonition, a blockquote, a list item, or a table cell (all
  inline-or-paragraph containers).
* **A note with no attachments expands to a paragraph, not an empty
  table.** A :class:`~asciidoc.ast.Table` may not be empty (the parser's
  own ``EMPTY_TABLE`` invariant), and silently dropping the node would
  hide the fact that the macro is there. An italic *"No attachments."*
  keeps the document readable and the macro's presence visible.
* The column set is exhaustive over :class:`enums.AttachmentTableColumn`
  (:func:`_cells_for`'s ``match`` ends in :func:`typing.assert_never`),
  so adding a column is an enum member plus one cell-builder arm — never
  a redesign.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, assert_never

from asciidoc.ast import (
    AttachmentLink,
    AttachmentTable,
    BlockNode,
    Document,
    InlineNode,
    Italic,
    Paragraph,
    Section,
    Table,
    TableCell,
    TableRow,
    Text,
)
from enums import AttachmentTableColumn
from giruntime.ui._filesize import format_byte_size
from models.attachment import Attachment


_COLUMN_HEADINGS: Final[dict[AttachmentTableColumn, str]] = {
    AttachmentTableColumn.NAME: "Name",
    AttachmentTableColumn.SIZE: "Size",
}
"""The visible header-row label of each column.

Presentation, not source syntax: the enum's *values* are the tokens the
user writes in ``cols="…"`` (lowercase, persisted in note source), while
these are what the reader sees. Keeping them apart is what lets the
heading be re-worded without touching a single stored note.
"""

_EMPTY_TABLE_TEXT: Final[str] = "No attachments."
"""Body of the italic paragraph a note with no attachments expands to."""


def expand_attachment_tables(
    document: Document,
    attachments: Sequence[Attachment],
) -> Document:
    """Return ``document`` with every :class:`AttachmentTable` replaced.

    ``attachments`` is the metadata of the note's attachments, in the
    order the table should list them (production passes
    ``list_for_note``'s order — i.e. insertion order).

    The returned document is a fresh value; the input is untouched (every
    AST node is frozen). A document with no ``attachments::[]`` macro is
    rebuilt identically, so callers may run this unconditionally.
    """
    return Document(
        title=document.title,
        tags=document.tags,
        blocks=_expand_blocks(document.blocks, attachments),
        source_line=document.source_line,
    )


def _expand_blocks(
    blocks: Sequence[BlockNode],
    attachments: Sequence[Attachment],
) -> tuple[BlockNode, ...]:
    """Replace every :class:`AttachmentTable` in one block sequence.

    Recurses into :class:`Section` — and only there: an
    :class:`AttachmentTable` is a top-level block and cannot appear in
    any other container (see the module docstring).
    """
    expanded: list[BlockNode] = []
    for block in blocks:
        if isinstance(block, AttachmentTable):
            expanded.append(_expand_one(block, attachments))
        elif isinstance(block, Section):
            expanded.append(
                Section(
                    level=block.level,
                    title=block.title,
                    blocks=_expand_blocks(block.blocks, attachments),
                    source_line=block.source_line,
                )
            )
        else:
            expanded.append(block)
    return tuple(expanded)


def _expand_one(
    node: AttachmentTable,
    attachments: Sequence[Attachment],
) -> BlockNode:
    """Expand a single macro node against the note's attachments.

    Zero attachments → the italic "No attachments." paragraph (a
    :class:`Table` may not be empty). Otherwise a :class:`Table` whose
    header row carries the column labels and whose data rows carry one
    attachment each, in the order supplied.
    """
    line = node.source_line
    if not attachments:
        return Paragraph(
            inlines=(
                Italic(
                    children=(
                        Text(content=_EMPTY_TABLE_TEXT, source_line=line),
                    ),
                    source_line=line,
                ),
            ),
            source_line=line,
        )
    header = TableRow(
        cells=tuple(
            TableCell(
                inlines=(
                    Text(
                        content=_COLUMN_HEADINGS[column],
                        source_line=line,
                    ),
                ),
                source_line=line,
            )
            for column in node.columns
        ),
        source_line=line,
    )
    rows = tuple(
        TableRow(
            cells=tuple(
                _cell_for(column, attachment, line)
                for column in node.columns
            ),
            source_line=line,
        )
        for attachment in attachments
    )
    return Table(
        rows=(header, *rows),
        column_proportions=None,
        source_line=line,
    )


def _cell_for(
    column: AttachmentTableColumn,
    attachment: Attachment,
    line: int,
) -> TableCell:
    """Build one data cell.

    Exhaustive over :class:`AttachmentTableColumn` — the ``match`` ends
    in :func:`assert_never`, so a new column is a type error here until
    its cell is decided.

    The ``name`` cell is an :class:`AttachmentLink`: the same node a
    hand-written ``attachment:`` macro parses to, so the generated table's
    save links and the authored ones share one click path. The ``size``
    cell reuses :func:`giruntime.ui._filesize.format_byte_size`, the same
    formatting the attachments panel shows.
    """
    inlines: tuple[InlineNode, ...]
    match column:
        case AttachmentTableColumn.NAME:
            inlines = (
                AttachmentLink(
                    filename=attachment.filename,
                    text=(
                        Text(content=attachment.filename, source_line=line),
                    ),
                    source_line=line,
                ),
            )
        case AttachmentTableColumn.SIZE:
            inlines = (
                Text(
                    content=format_byte_size(attachment.byte_size),
                    source_line=line,
                ),
            )
        case _:
            assert_never(column)
    return TableCell(inlines=inlines, source_line=line)
