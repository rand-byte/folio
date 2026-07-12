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
* This module is shared between every later build step. Step 4 produced
  the original "core" set; step 13 extends the inline union with
  :class:`Monospace` and :class:`Link`; step 14 adds :class:`Table`,
  :class:`TableRow`, and :class:`TableCell` to the block union; step 15
  will add ``Admonition`` and ``Blockquote``. The invariant is that
  adding a node never changes the existing nodes.
"""

from __future__ import annotations

from dataclasses import dataclass

from enums import AdmonitionKind, AttachmentTableColumn, LinkScheme


# ---------------------------------------------------------------------------
# Inline nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Text:
    """A literal text run with no inline formatting applied.

    ``content`` is never empty: the inline parser flushes its
    accumulator only when there is something to flush. Source-line
    boundaries inside a multi-line paragraph are *not* recorded as
    ``Text`` runs — they are :class:`SoftBreak` joiners (see
    :class:`Paragraph`), so the renderer can decide how to treat soft
    line breaks without sniffing ``content`` for a literal ``"\\n"``.
    """

    content: str
    source_line: int


@dataclass(frozen=True)
class SoftBreak:
    """A source-line boundary inside a paragraph (a *soft* line break).

    The block parser inserts one of these between each source line's
    parsed inlines when a paragraph spans multiple lines without a
    blank line between them, *unless* the earlier line ended with the
    ` +` hard-break marker (in which case a :class:`HardBreak` is
    emitted instead). A soft break is presentation-only: the renderer
    collapses it to a single space so the lines reflow as one logical
    paragraph. The node carries ``source_line`` (the line the break
    precedes) purely for provenance, consistent with every other node.
    """

    source_line: int


@dataclass(frozen=True)
class HardBreak:
    """An AsciiDoc *hard* line break (a source line ending in `` +``).

    Like :class:`SoftBreak`, this is a purely structural joiner the block
    parser inserts between two source lines of a multi-line paragraph (or
    admonition continuation). It differs only in how the renderer treats
    it: where a :class:`SoftBreak` collapses to a single space so the
    lines reflow, a :class:`HardBreak` forces a visible line break
    (``"\\n"``) so the two lines render one above the other.

    The parser emits a :class:`HardBreak` rather than a :class:`SoftBreak`
    for the join *after* a line whose text ended with the ` +` hard-break
    marker; the marker itself is stripped before that line's inlines are
    parsed, so it never reaches the AST as literal text. The node carries
    ``source_line`` (the line the break precedes) for provenance,
    consistent with every other node.
    """

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


@dataclass(frozen=True)
class Monospace:
    """An inline ```code``` span.

    The content is held as a single literal :class:`str` rather than as
    a list of further inline nodes: by spec the body of a monospace span
    is **not re-parsed**, so any ``*``, ``_`` or ``[…]#…#`` characters
    inside are preserved as plain literal text. This is what makes it
    safe to wrap a snippet of source code that itself contains AsciiDoc
    delimiters in backticks.

    The inline parser is responsible for raising
    :class:`ParseErrorKind.UNTERMINATED_MONOSPACE` when an opener has
    no matching close on the same line.
    """

    content: str
    source_line: int


@dataclass(frozen=True)
class Link:
    """An inline link — bare URL, URL-with-text, or ``link:`` macro.

    The three source shapes (bare URL ``https://x``, URL-with-text
    ``https://x[t]``, and macro ``link:https://x[t]``) all parse to
    this same node. The bare-URL shape carries display text equal to
    a single :class:`Text` whose content is the URL itself, so that
    every :class:`Link` node always has a non-empty ``text`` tuple
    that the renderer can iterate without a special case.

    ``scheme`` is the parsed-and-validated :class:`LinkScheme` member
    — the parser rejects out-of-list schemes with
    :class:`ParseErrorKind.UNSUPPORTED_LINK_SCHEME` so that the
    renderer (and downstream :mod:`ui.link_handler`) never
    has to handle an invalid scheme at runtime.

    Display text supports nested formatting (bold, italic, monospace
    etc.) but **not** other links — the inline parser raises
    :class:`ParseErrorKind.BAD_LINK_MACRO` if a nested link is found.
    """

    url: str
    scheme: LinkScheme
    text: tuple[InlineNode, ...]
    source_line: int


@dataclass(frozen=True)
class AttachmentLink:
    """An inline ``attachment:FILE[label]`` macro — a *save* link.

    The sibling of :class:`Link`: both are *activatable* things the
    reader can click, and both carry a non-empty ``text`` tuple so the
    renderer iterates display children without a special case. Where a
    :class:`Link` names a remote URL, this node names an attachment of
    the **currently displayed note** by its :attr:`filename` — the same
    key :class:`Image` resolves against. It is not a path and not an
    attachment id; the parser rejects a target that is empty, carries
    whitespace, or contains a path separator with
    :class:`ParseErrorKind.BAD_ATTACHMENT_MACRO`.

    Resolution is deliberately **not** the parser's job: the parser is
    storage-free and cannot know which files are attached, so a macro
    naming a file that is not attached is a *parse success*. Whether the
    named attachment exists is discovered at click time, where the UI
    surfaces the failure.

    ``text`` is the bracketed label when the user wrote one, and a single
    :class:`Text` carrying the filename when the brackets are empty
    (``attachment:report.pdf[]``) — mirroring :class:`Link`'s bare-URL
    rule. The label may contain nested formatting (bold, italic,
    monospace) but **not** another link or attachment macro: activatable
    things do not nest, and the inline parser raises
    :class:`ParseErrorKind.BAD_ATTACHMENT_MACRO` (or
    :class:`ParseErrorKind.BAD_LINK_MACRO`) if one is found inside.
    """

    filename: str
    text: tuple[InlineNode, ...]
    source_line: int


type InlineNode = (
    Text | Bold | Italic | Strikethrough | Underline | Monospace | Link
    | AttachmentLink | SoftBreak | HardBreak
)
"""The closed union of inline node kinds the parser produces.

Step 4 produced :class:`Text`, :class:`Bold`, :class:`Italic`,
:class:`Strikethrough`, and :class:`Underline`. Step 13 extends this
union with :class:`Monospace` and :class:`Link`; the attachment-links
feature extends it with :class:`AttachmentLink`, the save-link sibling
of :class:`Link`. The soft-line-break
fix extends it with :class:`SoftBreak` — the typed joiner the block
parser emits between a multi-line paragraph's source lines (replacing
the former ``Text("\\n", …)`` connector). The hard-line-break feature
extends it with :class:`HardBreak`, the sibling joiner emitted for the
``+`` marker (soft → reflow to a space, hard → a forced newline).
Future build steps extend this further if new inline constructs are
added.
"""


# ---------------------------------------------------------------------------
# Block nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paragraph:
    """A run of inline content that belongs together as a block of prose.

    Multi-source-line paragraphs have a :class:`SoftBreak` joiner
    between each source line's parsed inlines; the renderer collapses
    it to a single space.
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
    """A single ``* …`` or ``. …`` list item, with optional sub-lists.

    ``inlines`` and ``children`` are orthogonal axes of one item.
    ``inlines`` is the item's *own* inline-parsed text — the same closed
    inline union (:class:`Text` / :class:`Bold` / … / :class:`Link`) every
    other inline container holds, and the inline-only rule for the item's
    own text is unchanged by nesting. ``children`` holds the sub-lists
    that hang beneath this item: a possibly-empty sequence of
    :class:`OrderedList` / :class:`UnorderedList` (``()`` when the item is
    a leaf). Nesting therefore lives entirely on the item — an item may
    hold nested lists, but still **no other block kinds** and no
    continuation.

    A recursive tree (rather than a flat ``level: int`` on the item) is
    what lets a deeper level change list kind — ``* a`` then ``.. b`` is
    an ordered sublist hanging under an unordered item — while each
    :class:`OrderedList` / :class:`UnorderedList` stays internally
    homogeneous.
    """

    inlines: tuple[InlineNode, ...]
    children: tuple[OrderedList | UnorderedList, ...]
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


@dataclass(frozen=True)
class TableCell:
    """A single ``|cell`` inside a table row.

    A cell holds **inline-only** content — no nested lists, code blocks,
    images, tables, admonitions, or blockquotes. The parser enforces
    this by parsing each cell's text through :func:`parse_inline`
    (which only produces inline nodes) rather than dispatching through
    the block-level parser. The "no nested blocks" rule for tables is
    therefore impossible to violate by construction in v1, where cells
    are single-line text bounded by ``|`` separators.

    ``source_line`` is the source line of the row that contains this
    cell — every cell on a given row shares the same line number.
    """

    inlines: tuple[InlineNode, ...]
    source_line: int


@dataclass(frozen=True)
class TableRow:
    """One row of a :class:`Table`.

    Every row has the same arity as the header row (``rows[0]`` of the
    enclosing :class:`Table`); the parser raises
    :class:`ParseErrorKind.TABLE_ROW_ARITY_MISMATCH` for any row whose
    cell count differs from the header's.

    The first row of a table is implicitly the header — there is no
    separate ``header`` field on the table. The renderer styles
    ``rows[0]`` differently (bold weight) to surface this convention.
    """

    cells: tuple[TableCell, ...]
    source_line: int


@dataclass(frozen=True)
class Table:
    """A fenced ``|===`` table.

    ``rows`` is the parsed sequence of rows. ``rows[0]`` is always the
    header — the parser raises :class:`ParseErrorKind.EMPTY_TABLE` if
    there are no rows at all between the fences, so a :class:`Table`
    instance always has at least one row.

    ``column_proportions`` carries the integer values from a leading
    ``[cols="N,N,..."]`` directive, when one was present, or
    :data:`None` otherwise. Values are positive integers (zero or
    negative values raise :class:`ParseErrorKind.BAD_COLS_DIRECTIVE`
    at parse time). When present, the directive's count must match
    the table's arity — the renderer relies on this to compute
    ``max-width-chars`` for each column. When absent, the renderer
    treats every column as equally proportioned.

    ``source_line`` is the line of the opening ``|===`` fence — or, if
    a ``[cols="..."]`` directive preceded it, the line of the
    directive itself.
    """

    rows: tuple[TableRow, ...]
    column_proportions: tuple[int, ...] | None
    source_line: int


@dataclass(frozen=True)
class Admonition:
    """A ``NOTE``/``TIP``/``IMPORTANT``/``WARNING``/``CAUTION`` callout.

    Both source forms — single-line ``NOTE: text`` and block
    ``[NOTE]`` followed by ``====``-fenced body — produce this same
    AST node. The renderer therefore needs only one branch to handle
    admonitions, and tests that round-trip valid admonitions of either
    form share assertions.

    ``kind`` is one of the five members of :class:`AdmonitionKind`.
    The parser raises :class:`ParseErrorKind.UNKNOWN_ADMONITION_TYPE`
    for any other label, so by the time a node reaches the renderer
    the kind is guaranteed to be valid — no runtime fallback needed.

    ``blocks`` is a tuple of :class:`Paragraph`. The body of an
    admonition accepts inline content only — no nested lists, code
    blocks, images, tables, admonitions, or blockquotes — so the only
    block kind that can appear here is :class:`Paragraph`. The parser
    enforces this with
    :class:`ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER`.
    The single-line shape (``NOTE: text``) produces exactly one
    :class:`Paragraph` containing the inline-parsed text; the block
    shape produces however many paragraphs the user wrote between
    the fences (separated by blank lines), which may be zero (an
    empty body) — that is permitted as a degenerate but well-formed
    case.

    ``source_line`` is the line of the construct's *opening* token —
    the ``NOTE: …`` line for the single-line form, the ``[NOTE]``
    directive line for the block form (not the ``====`` fence line).
    """

    kind: AdmonitionKind
    blocks: tuple[Paragraph, ...]
    source_line: int


@dataclass(frozen=True)
class Blockquote:
    """A ``____``-fenced block quotation, optionally attributed.

    A blockquote may carry a ``[quote, Author, Source]`` directive on
    the line immediately above the opening fence; both attribution
    fields are optional. The plan and AsciiDoc spec name the third
    positional argument *Source* — a citation source such as a book
    title or article URL — so this dataclass uses :attr:`source` to
    match. Note that this is unrelated to :attr:`source_line`, which
    is the line number of the opening directive (or fence, when no
    directive is present).

    ``author`` is :data:`None` when the directive is absent or is the
    bare ``[quote]``, and a non-empty string otherwise (the parser
    rejects empty author strings with
    :class:`ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE`). ``source`` is
    :data:`None` unless the directive carried a third comma-separated
    argument (and likewise non-empty when set).

    ``blocks`` follows the same rule as :class:`Admonition`: a tuple
    of :class:`Paragraph`, no other block kinds, enforced by
    :class:`ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER`. An
    empty body (no paragraphs between fences) is permitted.
    """

    author: str | None
    source: str | None
    blocks: tuple[Paragraph, ...]
    source_line: int


@dataclass(frozen=True)
class AttachmentTable:
    """The block macro ``attachments::[]`` — a *generated* table.

    A placeholder node, not a rendering: it records *that* the note asked
    for a table of its own attachments and *which columns* it wants. The
    node carries no attachment data at all, because the AST is pure —
    the parser has no access to storage.

    The node is expanded into an ordinary :class:`Table` (or, when the
    note has no attachments, a single italic paragraph) by a pure
    transform in the UI-render layer — the first layer that may know
    about both the AST and the storage models — so the table reaches the
    reader through the *same* renderer path a hand-written ``|===`` table
    does. No :class:`AttachmentTable` ever reaches the renderer.

    ``columns`` is the ordered, duplicate-free tuple of columns to render:
    the parsed ``cols="…"`` attribute, or every
    :class:`AttachmentTableColumn` member in declaration order when the
    attribute is absent.
    """

    columns: tuple[AttachmentTableColumn, ...]
    source_line: int


type BlockNode = (
    Section
    | Paragraph
    | OrderedList
    | UnorderedList
    | CodeBlock
    | Image
    | Table
    | Admonition
    | Blockquote
    | AttachmentTable
)
"""The closed union of block node kinds the parser produces.

Step 4 produced :class:`Section`, :class:`Paragraph`, :class:`OrderedList`,
:class:`UnorderedList`, :class:`CodeBlock`, and :class:`Image`. Step 14
extends this union with :class:`Table`. Step 15 extends it further with
:class:`Admonition` and :class:`Blockquote`. The attachment-links feature
adds :class:`AttachmentTable`, the only node that is *expanded away*
(into a :class:`Table`) before rendering rather than emitted directly.
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

    ``tags`` is the sorted, lowercase, deduplicated tuple of tags
    extracted from a single ``:tags:`` header attribute (the
    right-hand side of ``:tags: foo, bar``). An absent / empty
    ``:tags:`` line yields ``()``. The parser raises
    :class:`ParseErrorKind.BAD_TAG_VALUE` on a malformed individual tag
    and :class:`ParseErrorKind.DUPLICATE_TAG_ATTRIBUTE` when two
    ``:tags:`` entries appear in the same header. Other header
    attribute names continue to be discarded.

    ``source_line`` is always 1 — the document starts at the start of
    the source.
    """

    title: tuple[InlineNode, ...] | None
    tags: tuple[str, ...]
    blocks: tuple[BlockNode, ...]
    source_line: int
