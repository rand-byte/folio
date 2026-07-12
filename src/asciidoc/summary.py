"""Derive a note's cached ``(title, snippet, tags)`` summary from its source.

Principles & invariants
-----------------------
* This module is the **single source of truth** for the note-list
  summary and for tag extraction. The title, snippet, and tag tuple
  shown across the UI are all computed from the same parsed
  :class:`~asciidoc.ast.Document` that the rendered view is built
  from, so the preview can never disagree with what the note actually
  renders to.
* :func:`derive_summary` is pure and deterministic: a function of
  ``source`` alone. It parses once and reads all three fields off the
  resulting AST.
* **Robustness invariant — :func:`derive_summary` never raises.** The
  parser is strict and raises :class:`ParseError` on invalid source, but
  a note must stay saveable while the user is mid-edit. On
  :class:`ParseError` — and *only* that one named exception, never a
  blanket ``except`` — the function falls back to a minimal permissive
  extraction so the note list keeps showing something useful. The
  fallback's tag-extraction arm walks the (permissive) lexer's
  :class:`AttributeEntryToken` stream in the document-header position
  and applies the same normalisation as the strict parser via
  :func:`asciidoc.parser.parse_tags_value`. Any failure in that arm
  resolves to ``()`` rather than re-raising.
* Classification is **exhaustive** over the closed
  :data:`~asciidoc.ast.BlockNode` and
  :data:`~asciidoc.ast.InlineNode` unions. Both walkers end in
  :func:`typing.assert_never`, so adding a future node kind is a type
  error here rather than a silent skip — the new kind's prose/structure
  treatment has to be decided explicitly.
* Prose vs structure (snippet rule): paragraphs, list items, and the
  bodies of admonitions and blockquotes are *prose* and contribute their
  flattened text; section headings, code blocks, images, tables, and the
  generated attachments table are *structure* and contribute nothing (the
  attachments table's rows do not exist until render time, and a snippet
  must never leak the macro's source syntax) (a section's nested blocks are
  still descended into to reach prose under deeper headings).
* This module is pure: it imports only the parser, the lexer (for the
  fallback tag walk), the AST, the :class:`NoteSummary` value type, the
  :class:`ParseError` type, and the ``config`` constants. No ``gi``, no
  ``storage`` — it is reusable by any non-view consumer.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import assert_never

from asciidoc.ast import (
    Admonition,
    AttachmentLink,
    AttachmentTable,
    BlockNode,
    Blockquote,
    Bold,
    CodeBlock,
    Document,
    HardBreak,
    Image,
    InlineNode,
    Italic,
    Link,
    Monospace,
    OrderedList,
    Paragraph,
    Section,
    SoftBreak,
    Strikethrough,
    Table,
    Text,
    Underline,
    UnorderedList,
)
from asciidoc.lexer import (
    AttributeEntryToken,
    BlankToken,
    HeadingToken,
    tokenize,
)
from asciidoc.parser import parse, parse_tags_value
from config.defaults import SNIPPET_MAX_CHARS, UNTITLED
from models.note import NoteSummary
from models.parse_error import ParseError


_ELLIPSIS: str = "\u2026"
"""Single-character ellipsis appended when a snippet is truncated."""

_LEVEL_ZERO_PREFIX: str = "= "
"""Literal prefix of a level-0 heading, used only by the fallback path.

The happy path never scans for this — it reads
:attr:`Document.title` straight off the parsed AST. It exists only so
the :class:`ParseError` fallback can still surface a plausible title
from source that did not parse.
"""


def derive_summary(source: str) -> NoteSummary:
    """Return the cached :class:`NoteSummary` for ``source``.

    Parses ``source`` once and computes the title (:func:`_title_of`),
    the snippet (:func:`_snippet_of`), and the tag tuple (read straight
    off :attr:`Document.tags`) from the resulting :class:`Document`.
    If the source does not parse, falls back to
    :func:`_fallback_summary` so the call never raises — see the
    module's robustness invariant.
    """
    try:
        document = parse(source)
    except ParseError:
        return _fallback_summary(source)
    return NoteSummary(
        title=_title_of(document),
        snippet=_snippet_of(document),
        tags=document.tags,
    )


# ---------------------------------------------------------------------------
# Inline flattening
# ---------------------------------------------------------------------------


def _flatten(inlines: Iterable[InlineNode]) -> str:
    """Flatten inline nodes to their plain visible text.

    Exhaustive over :data:`InlineNode`: emphasis wrappers recurse into
    their children, a :class:`Monospace` span yields its literal
    content, a :class:`Link` yields its visible text (never the URL),
    and both a :class:`SoftBreak` and a :class:`HardBreak` become a
    single space (snippets are one-line previews, so even a hard break
    collapses rather than wrapping the snippet).
    """
    parts: list[str] = []
    for node in inlines:
        match node:
            case Text(content=content):
                parts.append(content)
            case SoftBreak() | HardBreak():
                parts.append(" ")
            case (
                Bold(children=children)
                | Italic(children=children)
                | Strikethrough(children=children)
                | Underline(children=children)
            ):
                parts.append(_flatten(children))
            case Monospace(content=content):
                parts.append(content)
            case Link(text=text) | AttachmentLink(text=text):
                parts.append(_flatten(text))
            case _:
                assert_never(node)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Title rule
# ---------------------------------------------------------------------------


def _title_of(document: Document) -> str:
    """Return the document title text, or :data:`UNTITLED`.

    Reads :attr:`Document.title` — the parsed inline content of the
    level-0 heading — directly: no line scanning, no positional
    special-casing. Falls back to :data:`UNTITLED` when the heading is
    absent or flattens to whitespace only.
    """
    if document.title is not None:
        text = _flatten(document.title).strip()
        if text:
            return text
    return UNTITLED


# ---------------------------------------------------------------------------
# Snippet rule
# ---------------------------------------------------------------------------


def _prose_pieces(block: BlockNode) -> list[str]:
    """Return the prose fragments a single block contributes.

    Exhaustive over :data:`BlockNode`. Prose blocks yield their
    flattened text (one fragment per list item / per paragraph body);
    structural blocks yield nothing; :class:`Section` yields nothing for
    its own heading but recurses into its nested blocks.
    """
    match block:
        case Paragraph(inlines=inlines):
            return [_flatten(inlines)]
        case OrderedList(items=items) | UnorderedList(items=items):
            list_pieces: list[str] = []
            for item in items:
                list_pieces.append(_flatten(item.inlines))
                for child in item.children:
                    list_pieces.extend(_prose_pieces(child))
            return list_pieces
        case Admonition(blocks=blocks) | Blockquote(blocks=blocks):
            pieces: list[str] = []
            for paragraph in blocks:
                pieces.extend(_prose_pieces(paragraph))
            return pieces
        case Section(blocks=blocks):
            nested: list[str] = []
            for nested_block in blocks:
                nested.extend(_prose_pieces(nested_block))
            return nested
        case CodeBlock() | Image() | Table() | AttachmentTable():
            return []
        case _:
            assert_never(block)


def _snippet_of(document: Document) -> str:
    """Return the bounded, ellipsised prose preview of ``document``.

    Walks blocks in document order collecting flattened prose fragments
    until the running length reaches :data:`SNIPPET_MAX_CHARS`, then
    joins the fragments with single spaces and truncates with an
    ellipsis if the result is over the cap.
    """
    fragments: list[str] = []
    accumulated = 0
    for block in document.blocks:
        for piece in _prose_pieces(block):
            stripped = piece.strip()
            if not stripped:
                continue
            fragments.append(stripped)
            accumulated += len(stripped) + 1  # +1 for the joining space
            if accumulated >= SNIPPET_MAX_CHARS:
                return _join_and_truncate(fragments)
    return _join_and_truncate(fragments)


def _join_and_truncate(fragments: Iterable[str]) -> str:
    """Join prose fragments with single spaces and cap the length.

    When the joined string exceeds :data:`SNIPPET_MAX_CHARS`, one slot
    is reserved for the trailing :data:`_ELLIPSIS` character.
    """
    snippet = " ".join(fragments)
    if len(snippet) > SNIPPET_MAX_CHARS:
        snippet = snippet[: SNIPPET_MAX_CHARS - 1].rstrip() + _ELLIPSIS
    return snippet


# ---------------------------------------------------------------------------
# ParseError fallback
# ---------------------------------------------------------------------------


def _fallback_summary(source: str) -> NoteSummary:
    """Minimal permissive extraction used when ``source`` will not parse.

    The note stays saveable while the user fixes a syntax error, so this
    path must produce *something*. It does no structural classification:

    * title — the first non-blank line with a leading ``"= "`` stripped,
      or :data:`UNTITLED` when that line is not a level-0 heading;
    * snippet — the first non-blank lines (the title line excepted),
      joined and ellipsised the same way the happy path bounds its
      output;
    * tags — read off the lexer's :class:`AttributeEntryToken` stream
      via :func:`_fallback_tags`. The lexer is permissive (never
      raises), so even when the body fails to parse a valid
      ``:tags:`` header line still yields tags. Any failure in that
      tag extraction resolves to empty tags rather than re-raising;
      the *"never raises"* invariant is preserved.
    """
    title = UNTITLED
    fragments: list[str] = []
    accumulated = 0
    title_line_consumed = False

    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if not title_line_consumed:
            title_line_consumed = True
            if stripped.startswith(_LEVEL_ZERO_PREFIX):
                heading = stripped[len(_LEVEL_ZERO_PREFIX):].strip()
                if heading:
                    title = heading
                continue
        fragments.append(stripped)
        accumulated += len(stripped) + 1
        if accumulated >= SNIPPET_MAX_CHARS:
            break

    return NoteSummary(
        title=title,
        snippet=_join_and_truncate(fragments),
        tags=_fallback_tags(source),
    )


def _fallback_tags(source: str) -> tuple[str, ...]:
    """Best-effort tag extraction for a source that did not parse.

    Walks the lexer's :class:`AttributeEntryToken` stream looking for
    a single ``:tags:`` entry in the document-header position
    (contiguous attribute entries / blanks at the start of the token
    stream, after an optional level-1 heading), and applies the same
    normalisation the strict parser does. Any failure — invalid
    individual tag, duplicate ``:tags:``, or any other surprise —
    resolves to ``()`` rather than re-raising.

    The lexer is itself permissive: it classifies one line at a time
    and never raises on grammar issues. So this function safely
    extracts tags even from a source whose body fails to parse for
    unrelated reasons (e.g. an unterminated code fence further down).
    """
    try:
        tokens = tokenize(source)
    except ParseError:
        return ()
    seen_tags = False
    result: tuple[str, ...] = ()
    pos = 0
    # Skip the optional level-1 heading line so a header attribute
    # immediately under the title is reached.
    if pos < len(tokens) and isinstance(tokens[pos], HeadingToken):
        pos += 1
    while pos < len(tokens):
        token = tokens[pos]
        if isinstance(token, AttributeEntryToken):
            if token.name == "tags":
                if seen_tags:
                    return ()
                seen_tags = True
                try:
                    result = parse_tags_value(token.value, token.line)
                except ParseError:
                    return ()
            pos += 1
            continue
        if isinstance(token, BlankToken):
            pos += 1
            continue
        break
    return result
