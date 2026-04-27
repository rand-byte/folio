"""AST node dataclasses produced by the AsciiDoc parser.

Principles & invariants
-----------------------
* Every node is a frozen dataclass, so once a :class:`Document` is parsed
  it is immutable. Walkers that need to transform an AST produce a new
  AST — they never mutate in place.
* Each node carries a 1-indexed ``source_line`` so the renderer and the
  error-reporting paths can point users back at the offending source
  line. Lines are recorded at the node's *opening* position (the heading
  line, the opening fence, the first list bullet, etc.) — never at its
  end.
* The two top-level union types — :data:`InlineNode` and
  :data:`BlockNode` — are *closed* over the constructs the parser
  produces. Adding a new construct is a deliberate change that must
  extend the relevant union, so every walker (renderer, future
  transformers, tests) is forced to consider the new case.
* Children are stored as ``tuple[..., ...]`` rather than ``list``. This
  means equality and hashing of nodes are well-defined, ``frozen=True``
  is meaningful, and a careless caller cannot accidentally mutate a
  shared subtree.
* No node refers to filesystem state, GTK widgets, or storage. The AST
  is pure data: any node can be constructed in a unit test without
  spinning up the database, the renderer, or a display server.
* This module is shared between every later build step. Step 4 produces
  only the constructs declared here as "core"; later steps (13/14/15)
  will extend the unions with ``Monospace``, ``Link``, ``Table`` /
  ``TableRow`` / ``TableCell``, ``Admonition``, and ``Blockquote``. The
  invariant is that adding a node never changes the existing nodes.
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Inline nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Text:
    """A literal text run with no inline formatting applied.

    ``content`` may include newline characters when a paragraph spans
    multiple source lines — the parser inserts a ``Text("\\n", …)`` between
    each source line's parsed inlines so the renderer can decide how to
    treat soft line breaks. ``content`` is never empty: the inline parser
    flushes its accumulator only when there is something to flush.
    """

    content: str
    source_line: int


@dataclass(frozen=True)
class Bold:
    """Bold inline run.

    ``children`` are arbitrary inline nodes other than further
    :class:`Bold` instances — the inline parser's recursive descent on
    matched ``*`` delimiters precludes same-marker nesting, since the
    inner ``*`` always closes the outer span.
    """

    children: tuple[InlineNode, ...]
    source_line: int


@dataclass(frozen=True)
class Italic:
    """Italic inline run; same nesting rule as :class:`Bold` but on ``_``."""

    children: tuple[InlineNode, ...]
    source_line: int


@dataclass(frozen=True)
class Strikethrough:
    """``[.line-through]#…#`` inline span."""

    children: tuple[InlineNode, ...]
    source_line: int


@dataclass(frozen=True)
class Underline:
    """``[.underline]#…#`` inline span."""

    children: tuple[InlineNode, ...]
    source_line: int


type InlineNode = Text | Bold | Italic | Strikethrough | Underline
"""The closed union of inline node kinds the step-4 parser produces.

Later build steps extend this with ``Monospace`` and ``Link``.
"""


# ---------------------------------------------------------------------------
# Block nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paragraph:
    """A run of inline content that belongs together as a block of prose.

    Multi-source-line paragraphs have a ``Text("\\n", …)`` joiner between
    each source line's parsed inlines.
    """

    inlines: tuple[InlineNode, ...]
    source_line: int


@dataclass(frozen=True)
class Section:
    """A section opened by a level 2..6 heading.

    ``level`` equals the count of ``=`` characters in the heading marker:
    ``==`` → 2, …, ``======`` → 6. Level 1 (``=``) is the document title
    and is represented on :class:`Document` instead, never as a Section.

    ``blocks`` may itself contain :class:`Section`s of strictly greater
    level (deeper headings) — they are nested under this one. A heading
    of equal-or-lower level closes the current section.
    """

    level: int
    title: tuple[InlineNode, ...]
    blocks: tuple[BlockNode, ...]
    source_line: int


@dataclass(frozen=True)
class ListItem:
    """A single ``* …`` or ``. …`` list item.

    A list item is purely inline content in this subset — no nested
    blocks, no continuation. Multi-line items are not supported.
    """

    inlines: tuple[InlineNode, ...]
    source_line: int


@dataclass(frozen=True)
class OrderedList:
    """A run of one-or-more adjacent ``. item`` lines."""

    items: tuple[ListItem, ...]
    source_line: int


@dataclass(frozen=True)
class UnorderedList:
    """A run of one-or-more adjacent ``* item`` lines."""

    items: tuple[ListItem, ...]
    source_line: int


@dataclass(frozen=True)
class CodeBlock:
    """A fenced ``----`` code block.

    ``language`` is the value parsed from a leading ``[source,LANG]``
    directive, or ``None`` if the fence had no directive. The renderer
    does not syntax-highlight in v1; the language is carried so a
    future build can.

    ``content`` is the verbatim text between the opening and closing
    fences with source lines joined by single ``\\n`` characters. There
    is no trailing newline. The text is *never* re-parsed: anything that
    looks like AsciiDoc inside a code fence is preserved as written.
    """

    language: str | None
    content: str
    source_line: int


@dataclass(frozen=True)
class Image:
    """A block image macro: ``image::FILE[ATTRS]``.

    ``filename`` is the substring between ``image::`` and the opening
    ``[``. ``attrs`` is the substring between the brackets — its
    contents are *not* interpreted in v1; only structural well-formedness
    (matched brackets, no nested brackets) is enforced by the parser.
    """

    filename: str
    attrs: str
    source_line: int


type BlockNode = (
    Section
    | Paragraph
    | OrderedList
    | UnorderedList
    | CodeBlock
    | Image
)
"""The closed union of block node kinds the step-4 parser produces.

Later build steps extend this with ``Table``, ``Admonition``, and
``Blockquote``.
"""


# ---------------------------------------------------------------------------
# Document root
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Document:
    """The root of a parsed AsciiDoc document.

    ``title`` carries the parsed inline content of the level-0 heading
    (``= …``) when one is present at the start of the document, and is
    ``None`` otherwise. Level-0 headings are only valid as the first
    non-blank line; the parser raises :class:`ParseError` for level-0
    headings encountered later in the source.

    ``source_line`` is always 1 — the document starts at the start of
    the source.
    """

    title: tuple[InlineNode, ...] | None
    blocks: tuple[BlockNode, ...]
    source_line: int
