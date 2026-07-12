"""Recursive-descent parser turning lexer tokens into an AST :class:`Document`.

Principles & invariants
-----------------------
* Pure and deterministic. ``parse(source)`` is a function of ``source``
  alone: no I/O, no global state, no GTK.
* The parser is **strict**. Every syntactic violation in the AsciiDoc
  subset is converted to a :class:`ParseError` carrying a
  :class:`ParseErrorKind`. The parser never silently produces a wrong
  AST in order to "be helpful": a user staring at a parse error in the
  rendered view sees the exact line and a category-specific message.
* The parser is **layered on top of the lexer**, not coupled to source
  text byte-by-byte. It walks tokens. The single exception is
  :class:`CodeBlock` content: code is read verbatim from the original
  source via :func:`source_lines`, since the lexer's right-stripped
  payload would lose intended trailing whitespace inside a fence.
* Block dispatch is exhaustive over :data:`Token`. Any future token kind
  added to the lexer must be accepted here or rejected as
  :class:`ParseErrorKind.UNKNOWN_BLOCK` — there is no fallthrough that
  silently treats a structural token as paragraph text.
* Step 4 produced the constructs the original :data:`BlockNode`
  union admitted: sections, paragraphs, ordered/unordered lists, code
  blocks, and images. Step 14 extends this with :class:`Table`, parsed
  via the shared ``parse_inline_only_until``-style helper
  :func:`_parse_inline_only` so that step 15's admonitions and
  blockquotes (which also accept inline-only content) reuse the same
  enforcement of "no nested blocks". Step 15 adds :class:`Admonition`
  (in both single-line ``NOTE: text`` and block
  ``[NOTE]`` + ``====``-fenced forms) and :class:`Blockquote`
  (``____``-fenced, optionally preceded by a ``[quote, …]``
  directive). Both reuse :meth:`_Parser._read_paragraphs_until_fence`
  for the body — the shared helper that walks tokens between two
  fences, accepting only paragraphs and rejecting any other block
  shape with :class:`ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER`.
  Tokens that look like AsciiDoc but belong to other constructs
  (attribute entries, comments) are detected at block-start and
  rejected as :class:`ParseErrorKind.UNKNOWN_BLOCK`. They never
  become paragraphs.
* Sections are parsed recursively on level. A level-N heading opens a
  section that contains every following block until the next heading of
  level ``<= N`` (or end of input). Levels 2..6 are valid section
  headings; level 1 is the document title and is consumed once at the
  start of the document, raising :class:`ParseErrorKind.UNKNOWN_BLOCK`
  if encountered later; levels 7+ are always
  :class:`ParseErrorKind.UNKNOWN_BLOCK`.
* Paragraphs concatenate one or more adjacent :class:`LineToken`s.
  Inline formatting is parsed per source line so that
  :class:`ParseError.line` for an unmatched inline marker points at
  the exact source line, never at the first line of the paragraph.
  Lines are joined in the AST with :class:`SoftBreak` connectors.
* Lists may nest up to :data:`config.defaults.MAX_LIST_DEPTH` levels,
  ordered and unordered, mixed freely. Within a single marker kind the
  repeated-marker run length is the depth (``*``/``.`` = 1, ``**``/``..``
  = 2, ``***``/``...`` = 3). A single stack-based builder
  (:meth:`_Parser._parse_list`) turns the flat run of depth-tagged list
  tokens into a recursive tree, where each :class:`ListItem` carries its
  own inline text plus its child sub-lists. **A marker whose ``(kind,
  depth)`` signature is not already open anywhere on the stack always
  begins one new nesting level under the item currently being built** —
  this is what lets ``* A`` immediately followed by ``. B`` nest ``B``
  under ``A`` rather than starting a second, sibling list, matching the
  rule that a new list level is created for each unique marker
  encountered, independent of numeric run length. A marker whose
  signature *is* already open — either the current deepest frame or an
  ancestor — instead resumes at that existing level: an exact match on
  the current frame starts a sibling item there, and a match further up
  the stack closes the intervening frames (hanging each as a child of
  its parent's current item) before starting the sibling item, so
  reusing an outer-level marker returns to that outer level rather than
  opening yet another one. A blank line internal to a list is a
  separator, not a terminator: the run continues across it when the next
  non-blank line is another list item (so ordered numbering stays
  continuous rather than restarting). A run is terminated by the first
  non-blank, non-list token (heading, fence, paragraph, EOF) — a marker
  kind change alone, at any depth, no longer ends the run. Three depth
  conditions are hard errors, checked in this precedence:
  starts-below-top-level, skips-a-level, too-deep. The skips-a-level
  check only constrains a *same-kind* deepening (run length must be
  exactly the parent's plus one); a kind switch that opens a level not
  already on the stack carries no such run-length expectation of its
  own — only the too-deep cap, now measured by resulting stack depth
  rather than the token's raw run length, still applies. Multi-line
  items and ``+`` continuations remain deferred.
"""

# The module's size reflects the asciidoc subset's full surface area —
# every block kind, every error variant, the inline-only-container
# helper, and the attribution helper all live here together because
# they share private helpers (_at_block_start, _read_paragraphs_until_fence,
# the regexes). Splitting purely to satisfy the line counter would
# obscure that cohesion.
# pylint: disable=too-many-lines

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from asciidoc.ast import (
    Admonition,
    AttachmentTable,
    BlockNode,
    Blockquote,
    CodeBlock,
    Document,
    HardBreak,
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
    UnorderedList,
)
from asciidoc.inline_parser import parse_inline
from asciidoc.lexer import (
    AdmonitionDirectiveToken,
    AdmonitionFenceToken,
    AttachmentTableMacroToken,
    AttributeEntryToken,
    BlankToken,
    CodeDirectiveToken,
    CodeFenceToken,
    ColsDirectiveToken,
    HeadingToken,
    ImageMacroToken,
    LineToken,
    ListBulletToken,
    ListNumberToken,
    QuoteDirectiveToken,
    QuoteFenceToken,
    SingleAdmonitionToken,
    TableFenceToken,
    Token,
    source_lines,
    tokenize,
)
from config.defaults import MAX_LIST_DEPTH
from enums import (
    AdmonitionKind,
    AttachmentTableColumn,
    ParseErrorKind,
    TokenKind,
)
from models.parse_error import ParseError


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_DOCUMENT_TITLE_LEVEL: int = 1
_MIN_SECTION_LEVEL: int = 2
_MAX_SECTION_LEVEL: int = 6
_DOCUMENT_SOURCE_LINE: int = 1

# The depth of an outermost list item. The lexer counts marker runs
# starting at 1, so a list whose first item is not at this depth opens
# below the top level (``LIST_STARTS_BELOW_TOP_LEVEL``).
_TOP_LIST_DEPTH: int = 1

_COMMENT_PREFIX: str = "//"

# Cell separator inside table rows. A row line begins with this
# character; subsequent ``|`` characters split the line into cells.
_TABLE_CELL_SEPARATOR: str = "|"

# A line that *looks* like an attribute entry but failed to lex as one
# (because the lexer is strict: name must be letter-led and contain
# only letters / digits / underscores / hyphens). The parser uses
# this to raise :class:`ParseErrorKind.BAD_ATTRIBUTE_ENTRY` against
# malformed shapes — distinct from ``UNKNOWN_BLOCK`` so the banner
# in :mod:`ui.note_view` can render a tailored message.
# Valid attribute entries arrive as :class:`AttributeEntryToken` and
# never reach this regex; only malformed ones fall through to
# :class:`LineToken`.
_MALFORMED_ATTRIBUTE_ENTRY_RE: re.Pattern[str] = re.compile(r"^:[^:\n]*:")

# An image macro is split into ``filename`` and ``attrs`` on the first
# unescaped ``[``. Step 4 does not interpret ``attrs`` — but it does
# enforce that the brackets are matched and that no nested brackets
# appear inside ``attrs``.
_IMAGE_OPEN_BRACKET: str = "["
_IMAGE_CLOSE_BRACKET: str = "]"

# The ``attachments::[…]`` block macro's attribute list. The only
# attribute the macro accepts is ``cols``, whose value is a
# comma-separated list of :class:`AttachmentTableColumn` tokens — the
# same *word* the ``[cols="1,2"]`` table directive uses, but with
# column **names** rather than proportions as its values. The two never
# co-occur (the directive attaches to a ``|===`` fence, this attrlist to
# the macro), so the overload is unambiguous in practice.
_ATTACHMENT_TABLE_COLS_ATTRIBUTE: str = "cols"
_ATTACHMENT_TABLE_ATTRIBUTE_RE: re.Pattern[str] = re.compile(
    r'^([A-Za-z][A-Za-z0-9_-]*)="?([^"]*)"?$'
)
_ATTACHMENT_TABLE_COLUMN_SEPARATOR: str = ","


# Tag charset: lowercase letters, digits, and hyphens; must start with
# a lowercase letter or digit. Validation runs *after* the per-entry
# lowercasing in :func:`_parse_tags_value`, so the regex matches the
# already-normalised form.
_TAG_ATTRIBUTE_NAME: str = "tags"
_TAG_VALIDATION_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(source: str) -> Document:
    """Parse ``source`` into a :class:`Document`.

    Raises
    ------
    ParseError
        On any structural or inline-syntactic violation. The exception
        carries a 1-indexed source line and a :class:`ParseErrorKind`
        so the caller can render a category-specific help message.
    """
    parser = _Parser(source)
    return parser.parse_document()


# ---------------------------------------------------------------------------
# Parser implementation
# ---------------------------------------------------------------------------


@dataclass
class _OpenList:
    """One list under construction at a single nesting depth.

    The stack-based list builder (:meth:`_Parser._parse_list`) keeps one
    of these per currently-open list, ordered from the depth-1 root to
    the deepest open sub-list. ``items`` accumulates the already-finalised
    :class:`ListItem`\\ s; the ``cur_*`` fields hold the item currently
    being extended — the most recent at this level, which may still gain
    child sub-lists before it is finalised. Mutable on purpose: it is a
    transient accumulator local to one ``_parse_list`` call, never part of
    the immutable AST it produces.

    ``kind`` is the marker kind (:data:`TokenKind.LIST_BULLET` or
    :data:`TokenKind.LIST_NUMBER`); it decides whether the finished list
    is an :class:`UnorderedList` or an :class:`OrderedList`. Together
    with ``depth``, ``(kind, depth)`` is the frame's *signature* —
    :meth:`_Parser._parse_list` matches an incoming token's signature
    against the open frames to decide whether it is a sibling item on
    an already-open frame (signature matches) or the start of a brand
    new nesting level (signature matches nothing currently open).
    """

    kind: TokenKind
    depth: int
    source_line: int
    items: list[ListItem]
    cur_inlines: tuple[InlineNode, ...]
    cur_children: list[OrderedList | UnorderedList]
    cur_source_line: int


@dataclass
class _Parser:
    """Token cursor and recursive-descent state.

    Mutable on purpose: ``pos`` advances as the parser consumes tokens.
    The mutability is contained to a single ``parse_document`` call
    started from :func:`parse` — externally the parser is a pure
    function.
    """

    tokens: list[Token]
    source_lines: list[str]
    pos: int

    def __init__(self, source: str) -> None:
        self.tokens = tokenize(source)
        self.source_lines = source_lines(source)
        self.pos = 0

    # -- top-level -----------------------------------------------------------

    def parse_document(self) -> Document:
        """Parse the whole token stream into a :class:`Document`."""
        self._skip_blanks()
        title = self._try_consume_document_title()
        tags = self._consume_document_attributes()
        blocks = self._parse_blocks(stop_at_heading_level=None)
        return Document(
            title=title,
            tags=tags,
            blocks=tuple(blocks),
            source_line=_DOCUMENT_SOURCE_LINE,
        )

    def _consume_document_attributes(self) -> tuple[str, ...]:
        """Consume a contiguous run of header attribute entries.

        The header attribute run sits between the optional level-1
        title and the first body block. It is a sequence of
        :class:`AttributeEntryToken`\\ s (potentially interleaved with
        :class:`BlankToken`\\ s — blanks alone do not close the
        header). The run ends at the first non-attribute,
        non-blank token.

        Most attribute names are discarded: the AST has no field for
        them. The single exception is ``tags`` — when encountered,
        its value is parsed and validated per the rules in
        :func:`_parse_tags_value`, normalised (lowercased, trimmed,
        deduplicated, sorted), and returned to :meth:`parse_document`
        as the :attr:`Document.tags` tuple. A second ``:tags:`` entry
        anywhere in the same header is rejected with
        :class:`ParseErrorKind.DUPLICATE_TAG_ATTRIBUTE`.

        An :class:`AttributeEntryToken` reaching the parser anywhere
        *after* this consumption is rejected as
        :class:`ParseErrorKind.UNKNOWN_BLOCK` — see
        :meth:`_parse_non_heading_block`.
        """
        tags: tuple[str, ...] = ()
        tags_seen = False
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            if isinstance(token, AttributeEntryToken):
                if token.name == _TAG_ATTRIBUTE_NAME:
                    if tags_seen:
                        raise ParseError(
                            line=token.line,
                            column=0,
                            message=(
                                "duplicate :tags: attribute in document "
                                "header"
                            ),
                            kind=ParseErrorKind.DUPLICATE_TAG_ATTRIBUTE,
                        )
                    tags_seen = True
                    tags = parse_tags_value(token.value, token.line)
                self.pos += 1
                continue
            if isinstance(token, BlankToken):
                # A blank between two attribute entries does not close
                # the header; advance and check the next token.
                lookahead = self.pos + 1
                if (
                    lookahead < len(self.tokens)
                    and isinstance(
                        self.tokens[lookahead],
                        AttributeEntryToken,
                    )
                ):
                    self.pos += 1
                    continue
            return tags
        return tags

    def _try_consume_document_title(self) -> tuple[InlineNode, ...] | None:
        """If the next token is a level-1 heading, consume it as the title.

        An empty title raises :class:`ParseErrorKind.EMPTY_HEADING`. If
        the next token is anything else, the document has no title and
        ``None`` is returned without advancing the cursor.
        """
        if self.pos >= len(self.tokens):
            return None
        token = self.tokens[self.pos]
        if not isinstance(token, HeadingToken):
            return None
        if token.level != _DOCUMENT_TITLE_LEVEL:
            # Levels 2..6 starting the document is a valid section, not a
            # title. Levels 7+ are rejected later as UNKNOWN_BLOCK.
            return None
        if not token.text:
            raise ParseError(
                line=token.line,
                column=0,
                message="document title heading has no text",
                kind=ParseErrorKind.EMPTY_HEADING,
            )
        self.pos += 1
        return parse_inline(token.text, token.line)

    # -- block-list parsing --------------------------------------------------

    def _parse_blocks(
        self,
        stop_at_heading_level: int | None,
    ) -> list[BlockNode]:
        """Parse a run of blocks at one nesting level.

        ``stop_at_heading_level`` is the level of the section we are
        parsing inside, or ``None`` at document level. We stop when we
        see a heading whose level is ``<= stop_at_heading_level`` —
        that heading belongs to the enclosing section (or its sibling)
        and our caller will pick it up.
        """
        blocks: list[BlockNode] = []
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]

            if isinstance(token, BlankToken):
                self.pos += 1
                continue

            if isinstance(token, HeadingToken):
                if (
                    stop_at_heading_level is not None
                    and _MIN_SECTION_LEVEL <= token.level <= _MAX_SECTION_LEVEL
                    and token.level <= stop_at_heading_level
                ):
                    # Belongs to an enclosing scope.
                    return blocks
                blocks.append(self._parse_heading_block(token))
                continue

            blocks.append(self._parse_non_heading_block(token))

        return blocks

    # -- block dispatch ------------------------------------------------------

    def _parse_heading_block(self, token: HeadingToken) -> BlockNode:
        """Handle every :class:`HeadingToken` reaching the block dispatch."""
        if token.level == _DOCUMENT_TITLE_LEVEL:
            # Level 1 only valid as the very first non-blank line, which
            # was handled by ``_try_consume_document_title``.
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    "level-1 heading is only valid at the start of the "
                    "document"
                ),
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        if token.level > _MAX_SECTION_LEVEL:
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    f"heading level {token.level} is out of range "
                    f"(supported: 2..{_MAX_SECTION_LEVEL})"
                ),
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        if not token.text:
            raise ParseError(
                line=token.line,
                column=0,
                message="section heading has no text",
                kind=ParseErrorKind.EMPTY_HEADING,
            )
        # Valid section opener. Consume the heading and recurse into the
        # body until a heading at this level (or shallower) closes us.
        self.pos += 1
        title = parse_inline(token.text, token.line)
        body = self._parse_blocks(stop_at_heading_level=token.level)
        return Section(
            level=token.level,
            title=title,
            blocks=tuple(body),
            source_line=token.line,
        )

    # The dispatch is intentionally an isinstance ladder rather than a
    # type-keyed table because Token is a typing.Union and runtime
    # dispatch over union members is the idiomatic shape. The "too
    # many returns" warning is the exception-shaped cost of writing
    # this clearly.
    # pylint: disable-next=too-many-return-statements,too-many-branches
    def _parse_non_heading_block(self, token: Token) -> BlockNode:
        """Dispatch every non-heading block-start token to its parser."""
        if isinstance(token, (ListBulletToken, ListNumberToken)):
            return self._parse_list()
        if isinstance(token, CodeDirectiveToken):
            return self._parse_code_block_with_directive(token)
        if isinstance(token, CodeFenceToken):
            return self._parse_code_block_no_directive(token)
        if isinstance(token, ImageMacroToken):
            return self._parse_image(token)
        if isinstance(token, AttachmentTableMacroToken):
            return self._parse_attachment_table(token)
        if isinstance(token, ColsDirectiveToken):
            return self._parse_table_with_directive(token)
        if isinstance(token, TableFenceToken):
            return self._parse_table_no_directive(token)
        if isinstance(token, AdmonitionDirectiveToken):
            return self._parse_block_admonition(token)
        if isinstance(token, SingleAdmonitionToken):
            return self._parse_single_admonition(token)
        if isinstance(token, AdmonitionFenceToken):
            # A bare ``====`` with no preceding ``[NOTE]`` directive is
            # a stray fence — there is no admonition kind to associate
            # the body with. Reject as ``UNKNOWN_BLOCK``: there is no
            # specialised "stray admonition fence" error variant
            # because the user's most likely fix is to add a
            # ``[NOTE]`` directive above, which is the same family of
            # mistake as any other unrecognised directive shape.
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    "stray `====` fence — admonition blocks must be "
                    "preceded by a `[NOTE]`/`[TIP]`/… directive"
                ),
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        if isinstance(token, QuoteDirectiveToken):
            return self._parse_blockquote_with_directive(token)
        if isinstance(token, QuoteFenceToken):
            return self._parse_blockquote_no_directive(token)
        if isinstance(token, AttributeEntryToken):
            # Header attribute entries are consumed by
            # ``_consume_document_attributes`` before block dispatch
            # ever sees them. Reaching this branch means the entry
            # appears mid-document, which is not a position AsciiDoc
            # permits. Reject with ``UNKNOWN_BLOCK`` (positionally
            # invalid), not ``BAD_ATTRIBUTE_ENTRY`` (which is reserved
            # for *malformed* shapes).
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    "attribute entries are only valid in the document "
                    "header (between the title and the first body block)"
                ),
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        if isinstance(token, LineToken):
            self._reject_unknown_block(token)
            return self._parse_paragraph()
        # The two remaining token kinds — BlankToken and HeadingToken —
        # are filtered before we get here. Reaching this branch would
        # mean the lexer grew a new token kind without the parser
        # learning about it.
        raise ParseError(
            line=getattr(token, "line", 0),
            column=0,
            message=f"unexpected token at block start: {type(token).__name__}",
            kind=ParseErrorKind.UNKNOWN_BLOCK,
        )

    # -- unknown-block detection --------------------------------------------

    def _reject_unknown_block(self, token: LineToken) -> None:
        """Raise if the line begins a known-but-unsupported block.

        Lines that look like AsciiDoc structural directives we do not
        yet implement (line comments, ``[…]`` directives that fell
        through every specialised matcher) must produce a parse error
        rather than be silently swept into a paragraph. The parser
        detects these at block-start only — once we are mid-paragraph,
        a ``//`` line is part of the prose.

        Step 14 dropped the ``|===`` table-fence rejection from this
        check because tables are now supported — a ``|===`` line
        becomes a :class:`TableFenceToken` at the lexer level and is
        consumed by :meth:`_parse_table_no_directive`, never reaching
        this method as a :class:`LineToken`. Step 15 dropped the
        ``____`` blockquote-fence rejection for the same reason —
        ``____`` is now a :class:`QuoteFenceToken`. The bracketed
        ``[…]`` rejection still catches arbitrary directives the
        specialised matchers did not recognise (e.g. malformed
        ``[source,]`` and ``[cols=""]`` shapes that the lexer falls
        through).

        Attribute entries are now consumed as :class:`AttributeEntryToken`
        at the document header (see :meth:`_consume_document_attributes`).
        A malformed shape (``::``, ``:bad name:``, ``:123: x``) lexes
        as :class:`LineToken` and is rejected here as
        :class:`ParseErrorKind.BAD_ATTRIBUTE_ENTRY` — distinct from
        ``UNKNOWN_BLOCK`` so the banner can show a tailored message.
        """
        text = token.text

        if text.startswith(_COMMENT_PREFIX):
            raise ParseError(
                line=token.line,
                column=0,
                message="line comments are not supported",
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        if _MALFORMED_ATTRIBUTE_ENTRY_RE.match(text):
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    "malformed attribute entry: name must start with a "
                    "letter and contain only letters, digits, "
                    "underscores, or hyphens"
                ),
                kind=ParseErrorKind.BAD_ATTRIBUTE_ENTRY,
            )
        # Lines wrapped in ``[…]`` — even malformed ``[source,]`` and
        # ``[cols=""]`` — all land here. They are paragraph-shaped
        # only by accident.
        if text.startswith("[") and text.endswith("]") and len(text) >= 2:
            raise ParseError(
                line=token.line,
                column=0,
                message=f"unrecognised block directive: {text}",
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )

    # -- paragraphs ---------------------------------------------------------

    def _parse_paragraph(self) -> Paragraph:
        """Consume consecutive :class:`LineToken`s into one paragraph.

        Inline content is parsed per-line so that error line numbers in
        :class:`ParseError`\\ s are exact. Lines are joined in the AST by
        :func:`_join_source_lines`, which emits a :class:`SoftBreak`
        between adjacent lines — or a :class:`HardBreak` when the earlier
        line ended with the `` +`` marker — so the renderer can decide how
        to honour each kind of line break.
        """
        first_line_token = self.tokens[self.pos]
        assert isinstance(first_line_token, LineToken), (
            "_parse_paragraph called without a LineToken at the cursor"
        )
        start_line = first_line_token.line

        line_tokens: list[LineToken] = []
        while (
            self.pos < len(self.tokens)
            and isinstance(self.tokens[self.pos], LineToken)
        ):
            line_token = self.tokens[self.pos]
            assert isinstance(line_token, LineToken)
            line_tokens.append(line_token)
            self.pos += 1

        return Paragraph(
            inlines=_join_source_lines(line_tokens),
            source_line=start_line,
        )

    # -- lists --------------------------------------------------------------

    def _parse_list(self) -> OrderedList | UnorderedList:
        """Consume one adjacent list run and build its nested tree.

        Walks the flat run of depth-tagged :class:`ListBulletToken` /
        :class:`ListNumberToken` tokens with an explicit stack of
        :class:`_OpenList` frames (depth-1 root … deepest open sub-list),
        each identified by its ``(kind, depth)`` signature. For each list
        token, relative to the open frames:

        * **matches the current (deepest) frame's signature** — finalise
          its current item and start a sibling item on it;
        * **matches an ancestor frame's signature** — pop frames down to
          that ancestor (hanging each popped frame under its parent's
          current item), then start a sibling item on it, exactly as
          above. This is what lets an outer-level marker reappear after a
          deeper digression and resume at its original level instead of
          opening yet another one;
        * **matches no open frame at all** — the marker has not been seen
          anywhere in this run, so it always opens one new nesting level
          under the item the deepest frame is currently building (see
          :meth:`_open_deeper` for the skip-level / depth-cap checks that
          gate this).

        A blank line internal to the run is a separator, not a terminator:
        when the next non-blank token is another list marker the blank run
        is absorbed (via :meth:`_run_continues_after_blanks`) and the list
        continues across it. The run ends at the first non-blank, non-list
        token (heading, fence, paragraph, EOF); ``self.pos`` is left on
        that terminating token — a blank that ends the run is left in
        place so :meth:`_parse_blocks` skips it exactly as before. The
        three depth errors are raised with the precedence pinned in the
        module docstring.
        """
        stack: list[_OpenList] = []
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            if not isinstance(token, (ListBulletToken, ListNumberToken)):
                if (
                    isinstance(token, BlankToken)
                    and self._run_continues_after_blanks()
                ):
                    # Internal separator: absorb the blank run and resume
                    # the loop on the next list marker.
                    self._skip_blanks()
                    continue
                break

            if not stack:
                # First item of the whole run: it must open at the top.
                if token.depth != _TOP_LIST_DEPTH:
                    raise ParseError(
                        line=token.line,
                        column=0,
                        message=(
                            f"list item opens at depth {token.depth}; the "
                            "first item of a list must be at the top level"
                        ),
                        kind=ParseErrorKind.LIST_STARTS_BELOW_TOP_LEVEL,
                    )
                stack.append(self._open_list(token))
                self.pos += 1
                continue

            top = stack[-1]
            if token.kind == top.kind and token.depth == top.depth:
                self._start_sibling_item(top, token)
                self.pos += 1
                continue

            ancestor_index = self._find_matching_frame(stack, token)
            if ancestor_index is not None:
                while len(stack) - 1 > ancestor_index:
                    self._close_top(stack)
                self._start_sibling_item(stack[ancestor_index], token)
                self.pos += 1
                continue

            # No open frame carries this signature: it is a marker not
            # yet seen in this run, so it always deepens by one level
            # under the item the current deepest frame is building.
            self._open_deeper(stack, token)
            self.pos += 1

        return self._close_all(stack)

    @staticmethod
    def _find_matching_frame(
        stack: list[_OpenList],
        token: ListBulletToken | ListNumberToken,
    ) -> int | None:
        """Return the index of the open frame whose signature matches.

        Searches the stack excluding its top (the caller already checked
        the top frame itself via the fast same-signature path in
        :meth:`_parse_list`), from the nearest ancestor outward, for a
        frame whose ``(kind, depth)`` equals ``token``'s. Returns ``None``
        when the marker's signature is not currently open anywhere,
        meaning it starts a brand new nesting level rather than resuming
        an existing one. Signatures are unique across the stack: a token
        that matched an already-open frame always resumes there instead
        of opening a duplicate, so at most one index can ever match.
        """
        for index in range(len(stack) - 2, -1, -1):
            frame = stack[index]
            if frame.kind == token.kind and frame.depth == token.depth:
                return index
        return None

    def _run_continues_after_blanks(self) -> bool:
        """Report whether a list run continues past the blanks at ``pos``.

        Called from :meth:`_parse_list` with ``self.pos`` on a
        :class:`BlankToken` inside an open run. Scans forward over the
        contiguous blank run **without moving** ``self.pos`` and returns
        ``True`` when the first following non-blank token is a list marker
        — meaning the blanks are an internal separator and the run should
        continue across them — and ``False`` when the run is followed by a
        non-list block (or end of input), so the blank terminates the list.
        """
        look = self.pos
        while (
            look < len(self.tokens)
            and isinstance(self.tokens[look], BlankToken)
        ):
            look += 1
        return look < len(self.tokens) and isinstance(
            self.tokens[look], (ListBulletToken, ListNumberToken)
        )

    @staticmethod
    def _open_list(token: ListBulletToken | ListNumberToken) -> _OpenList:
        """Open a fresh frame whose first (current) item is ``token``."""
        return _OpenList(
            kind=token.kind,
            depth=token.depth,
            source_line=token.line,
            items=[],
            cur_inlines=parse_inline(token.text, token.line),
            cur_children=[],
            cur_source_line=token.line,
        )

    def _open_deeper(
        self,
        stack: list[_OpenList],
        token: ListBulletToken | ListNumberToken,
    ) -> None:
        """Push a new deepest frame, enforcing skip-level then depth-cap.

        Reached only when ``token``'s ``(kind, depth)`` signature matches
        no frame currently open (:meth:`_parse_list` handles same-frame
        and ancestor-frame matches itself), so this always adds one level
        under the item the current deepest frame is building. Two
        different legality checks apply depending on ``token``'s kind:

        * **Same kind as the current deepest frame** — the
          repeated-marker-run-length convention is in effect for that
          kind's chain, so ``token.depth`` must be exactly one more than
          the current frame's, else :class:`ParseErrorKind.
          LIST_NESTING_SKIPS_LEVEL`.
        * **Different kind** — this marker has no run-length precedent of
          its own at this position (nothing of its kind is open here), so
          any run length is accepted; only the depth cap below applies.
          This is why ``* a`` / ``.. b`` may open directly at depth two —
          ``..`` is simply ``b``'s own first-appearance marker, not a
          "jump" relative to ``a``'s unrelated bullet chain.

        Either way the resulting stack depth (``len(stack) + 1``) — not
        ``token.depth``, which no longer tracks stack depth once kind
        switches are involved — must not exceed
        :data:`config.defaults.MAX_LIST_DEPTH`, else :class:`ParseErrorKind.
        LIST_NESTING_TOO_DEEP`. ``LIST_STARTS_BELOW_TOP_LEVEL`` is handled
        by the first-item check in :meth:`_parse_list`, so it never
        competes with these two.
        """
        top = stack[-1]
        if token.kind == top.kind and token.depth != top.depth + 1:
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    f"list item jumps from depth {top.depth} to "
                    f"{token.depth}; nesting may deepen by only one level "
                    "at a time"
                ),
                kind=ParseErrorKind.LIST_NESTING_SKIPS_LEVEL,
            )
        new_stack_depth = len(stack) + 1
        if new_stack_depth > MAX_LIST_DEPTH:
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    f"list nests {new_stack_depth} levels deep; the "
                    f"maximum is {MAX_LIST_DEPTH}"
                ),
                kind=ParseErrorKind.LIST_NESTING_TOO_DEEP,
            )
        stack.append(self._open_list(token))

    @staticmethod
    def _start_sibling_item(
        frame: _OpenList,
        token: ListBulletToken | ListNumberToken,
    ) -> None:
        """Finalise ``frame``'s current item and begin a sibling item."""
        _Parser._finalize_current_item(frame)
        frame.cur_inlines = parse_inline(token.text, token.line)
        frame.cur_children = []
        frame.cur_source_line = token.line

    @staticmethod
    def _finalize_current_item(frame: _OpenList) -> None:
        """Append the in-progress item (with its children) to ``frame``."""
        frame.items.append(
            ListItem(
                inlines=frame.cur_inlines,
                children=tuple(frame.cur_children),
                source_line=frame.cur_source_line,
            )
        )

    @staticmethod
    def _build_list(frame: _OpenList) -> OrderedList | UnorderedList:
        """Finalise ``frame`` and turn it into its AST list node."""
        _Parser._finalize_current_item(frame)
        items = tuple(frame.items)
        if frame.kind is TokenKind.LIST_NUMBER:
            return OrderedList(items=items, source_line=frame.source_line)
        return UnorderedList(items=items, source_line=frame.source_line)

    @staticmethod
    def _close_top(stack: list[_OpenList]) -> None:
        """Pop the deepest frame and hang it under its parent's item.

        Only called while a parent frame exists (while dedenting to a
        matching ancestor frame in :meth:`_parse_list`, and by
        :meth:`_close_all` down to the root), so the built sub-list
        always has a current item to attach to.
        """
        child = _Parser._build_list(stack.pop())
        stack[-1].cur_children.append(child)

    @staticmethod
    def _close_all(stack: list[_OpenList]) -> OrderedList | UnorderedList:
        """Collapse the whole stack and return the depth-1 root list."""
        while len(stack) > 1:
            _Parser._close_top(stack)
        return _Parser._build_list(stack[0])

    # -- code blocks --------------------------------------------------------

    def _parse_code_block_with_directive(
        self,
        directive: CodeDirectiveToken,
    ) -> CodeBlock:
        """Parse ``[source[,LANG]]`` immediately followed by a fenced block.

        The directive must be followed *immediately* by a code fence —
        any other token (including a blank line) breaks the binding,
        so we raise :class:`ParseErrorKind.UNKNOWN_BLOCK` against the
        directive line. This is intentional: a blank between the
        directive and the fence is almost always a typo, and silently
        accepting it would drop the language hint.
        """
        next_index = self.pos + 1
        if (
            next_index >= len(self.tokens)
            or not isinstance(self.tokens[next_index], CodeFenceToken)
        ):
            raise ParseError(
                line=directive.line,
                column=0,
                message=(
                    "[source] directive must be immediately followed by "
                    "a `----` fence"
                ),
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        # Consume the directive; the fence is consumed by the body
        # reader below.
        self.pos += 1
        fence = self.tokens[self.pos]
        assert isinstance(fence, CodeFenceToken)
        return self._read_code_body(
            opening_fence=fence,
            language=directive.language,
            block_source_line=directive.line,
        )

    def _parse_code_block_no_directive(
        self,
        fence: CodeFenceToken,
    ) -> CodeBlock:
        """Parse a bare ``----`` fenced block with no language directive."""
        return self._read_code_body(
            opening_fence=fence,
            language=None,
            block_source_line=fence.line,
        )

    def _read_code_body(
        self,
        opening_fence: CodeFenceToken,
        language: str | None,
        block_source_line: int,
    ) -> CodeBlock:
        """Consume content from after the opening fence to the closing fence.

        The body is read verbatim from :attr:`source_lines` rather than
        from token payloads, because the lexer right-strips trailing
        whitespace from line classifications — and trailing whitespace
        inside a code block is intentional. An empty body (the closing
        fence immediately follows the opener) is allowed and produces
        ``content == ""``.
        """
        # Advance past the opening fence.
        self.pos += 1
        body_line_indices: list[int] = []
        while self.pos < len(self.tokens):
            current = self.tokens[self.pos]
            if isinstance(current, CodeFenceToken):
                # Closing fence found — consume it and return.
                self.pos += 1
                content = "\n".join(
                    self.source_lines[line_index]
                    for line_index in body_line_indices
                )
                return CodeBlock(
                    language=language,
                    content=content,
                    source_line=block_source_line,
                )
            # Any other token contributes its source line verbatim. The
            # lexer guarantees one token per source line, so token.line
            # is the right index into ``source_lines`` (1-based).
            body_line_indices.append(current.line - 1)  # type: ignore[union-attr]
            self.pos += 1

        # End of input without a closing fence.
        raise ParseError(
            line=opening_fence.line,
            column=0,
            message="code block has no closing `----` fence",
            kind=ParseErrorKind.UNTERMINATED_CODE_BLOCK,
        )

    # -- images -------------------------------------------------------------

    def _parse_image(self, token: ImageMacroToken) -> Image:
        """Validate and split an ``image::FILE[ATTRS]`` macro.

        ``token.raw`` is everything after the ``image::`` prefix. The
        parser splits it into filename and attribute list and rejects
        every degenerate shape with
        :class:`ParseErrorKind.BAD_IMAGE_MACRO`.
        """
        self.pos += 1
        raw = token.raw

        open_index = raw.find(_IMAGE_OPEN_BRACKET)
        if open_index < 0:
            raise ParseError(
                line=token.line,
                column=0,
                message="image macro is missing `[` before the attribute list",
                kind=ParseErrorKind.BAD_IMAGE_MACRO,
            )
        if not raw.endswith(_IMAGE_CLOSE_BRACKET):
            raise ParseError(
                line=token.line,
                column=0,
                message="image macro is missing the closing `]`",
                kind=ParseErrorKind.BAD_IMAGE_MACRO,
            )

        filename = raw[:open_index]
        # Slice between the first ``[`` and the trailing ``]``.
        attrs = raw[open_index + 1 : -1]

        if not filename:
            raise ParseError(
                line=token.line,
                column=0,
                message="image macro has no filename",
                kind=ParseErrorKind.BAD_IMAGE_MACRO,
            )
        if (
            _IMAGE_OPEN_BRACKET in attrs
            or _IMAGE_CLOSE_BRACKET in attrs
        ):
            raise ParseError(
                line=token.line,
                column=0,
                message="image macro attributes contain unbalanced brackets",
                kind=ParseErrorKind.BAD_IMAGE_MACRO,
            )

        return Image(
            filename=filename,
            attrs=attrs,
            source_line=token.line,
        )

    # -- attachment table ---------------------------------------------------

    def _parse_attachment_table(
        self,
        token: AttachmentTableMacroToken,
    ) -> AttachmentTable:
        """Validate an ``attachments::[ATTRS]`` block macro.

        ``token.raw`` is everything after the ``attachments::`` prefix —
        the bracketed attribute list, which must be present, balanced,
        and free of nested brackets
        (:class:`ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO`). The macro's
        *target* is empty by design: the note scope is implicit, exactly
        as AsciiDoc's own ``toc::[]`` spells it.

        The attribute list accepts a single attribute, ``cols``, whose
        value is a comma-separated list of
        :class:`AttachmentTableColumn` tokens. An unknown column name
        raises :class:`ParseErrorKind.UNKNOWN_ATTACHMENT_TABLE_COLUMN`; a
        duplicate name, an empty value, an unknown attribute, or a
        malformed list raises
        :class:`ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO`. An absent
        ``cols`` selects every column in declaration order.
        """
        self.pos += 1
        raw = token.raw
        if not (
            raw.startswith(_IMAGE_OPEN_BRACKET)
            and raw.endswith(_IMAGE_CLOSE_BRACKET)
            and len(raw) >= 2
        ):
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    "attachments:: macro must be followed by a balanced "
                    "`[…]` attribute list"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO,
            )
        body = raw[1:-1].strip()
        if (
            _IMAGE_OPEN_BRACKET in body
            or _IMAGE_CLOSE_BRACKET in body
        ):
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    "attachments:: macro attributes contain unbalanced "
                    "brackets"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO,
            )
        return AttachmentTable(
            columns=_parse_attachment_table_columns(body, token.line),
            source_line=token.line,
        )

    # -- tables -------------------------------------------------------------

    def _parse_table_with_directive(
        self,
        directive: ColsDirectiveToken,
    ) -> Table:
        """Parse ``[cols="N,N,..."]`` immediately followed by a table fence.

        Mirrors the pattern of :meth:`_parse_code_block_with_directive`
        — the directive must be followed *immediately* by the opening
        ``|===`` fence. Any other token (including a blank line) breaks
        the binding and we raise ``UNKNOWN_BLOCK`` against the
        directive line: a blank between ``[cols=…]`` and ``|===`` is
        almost always a typo, and silently accepting it would drop the
        column-proportions hint the renderer relies on.

        The directive's body (e.g. ``"1,2,3"``) is parsed into a tuple
        of positive integers here. The lexer guarantees the body is
        non-empty; this method is what enforces "every value is a
        positive integer" with
        :class:`ParseErrorKind.BAD_COLS_DIRECTIVE` on any failure.
        """
        next_index = self.pos + 1
        if (
            next_index >= len(self.tokens)
            or not isinstance(self.tokens[next_index], TableFenceToken)
        ):
            raise ParseError(
                line=directive.line,
                column=0,
                message=(
                    "[cols=\"…\"] directive must be immediately followed by "
                    "a `|===` fence"
                ),
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        proportions = _parse_cols_proportions(directive.raw, directive.line)
        # Consume the directive; the fence is consumed by the body
        # reader below.
        self.pos += 1
        fence = self.tokens[self.pos]
        assert isinstance(fence, TableFenceToken)
        return self._read_table_body(
            opening_fence=fence,
            column_proportions=proportions,
            block_source_line=directive.line,
        )

    def _parse_table_no_directive(
        self,
        fence: TableFenceToken,
    ) -> Table:
        """Parse a bare ``|===`` table with no ``[cols=…]`` directive."""
        return self._read_table_body(
            opening_fence=fence,
            column_proportions=None,
            block_source_line=fence.line,
        )

    def _read_table_body(
        self,
        opening_fence: TableFenceToken,
        column_proportions: tuple[int, ...] | None,
        block_source_line: int,
    ) -> Table:
        """Consume rows from after the opening fence to the closing fence.

        Each row is one source line beginning with ``|``. Cells are
        produced by splitting the line on ``|`` and parsing each piece
        through :func:`_parse_inline_only`. Rows whose cell count
        differs from the header (the first row) raise
        :class:`ParseErrorKind.TABLE_ROW_ARITY_MISMATCH`. Blank lines
        inside the fences are tolerated as visual whitespace and
        ignored. An empty table (no rows between the fences) raises
        :class:`ParseErrorKind.EMPTY_TABLE`. An unterminated fence
        raises :class:`ParseErrorKind.UNTERMINATED_TABLE`.
        """
        # Advance past the opening fence.
        self.pos += 1
        rows: list[TableRow] = []
        while self.pos < len(self.tokens):
            current = self.tokens[self.pos]
            if isinstance(current, TableFenceToken):
                # Closing fence found. Validate and return.
                self.pos += 1
                if not rows:
                    raise ParseError(
                        line=opening_fence.line,
                        column=0,
                        message="table has no rows between the `|===` fences",
                        kind=ParseErrorKind.EMPTY_TABLE,
                    )
                self._validate_cols_directive_arity(
                    column_proportions=column_proportions,
                    header=rows[0],
                    directive_line=block_source_line,
                )
                return Table(
                    rows=tuple(rows),
                    column_proportions=column_proportions,
                    source_line=block_source_line,
                )
            if isinstance(current, BlankToken):
                # Blank lines inside a table are visual whitespace — they
                # neither add a row nor terminate the table. Skip.
                self.pos += 1
                continue
            if isinstance(current, LineToken):
                rows.append(self._parse_table_row(current, header=rows[0] if rows else None))
                self.pos += 1
                continue
            # Any other token kind inside a table body — heading, code
            # fence, image macro, list bullet — is structurally invalid
            # because table cells are inline-only. The grammar of the
            # subset has the row "shape" as ``('|' inline)+``: a line
            # that does not start with ``|`` simply isn't a row, and
            # block-level tokens explicitly aren't either.
            raise ParseError(
                line=getattr(current, "line", opening_fence.line),
                column=0,
                message=(
                    "table cells must be inline-only; "
                    f"{type(current).__name__} is not allowed inside a table"
                ),
                kind=ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER,
            )

        # End of input without a closing fence.
        raise ParseError(
            line=opening_fence.line,
            column=0,
            message="table has no closing `|===` fence",
            kind=ParseErrorKind.UNTERMINATED_TABLE,
        )

    def _parse_table_row(
        self,
        token: LineToken,
        *,
        header: TableRow | None,
    ) -> TableRow:
        """Parse one row of a table from a line that starts with ``|``.

        Raises :class:`ParseErrorKind.UNTERMINATED_TABLE` if the line
        does not start with the cell separator — that means the row
        token is not actually a row, so the table never closed and the
        scanner walked past prose inside the fences. (The lexer emits
        ``LineToken`` for both.) Raises
        :class:`ParseErrorKind.TABLE_ROW_ARITY_MISMATCH` when the cell
        count differs from the header's.
        """
        text = token.text
        if not text.startswith(_TABLE_CELL_SEPARATOR):
            # A non-``|`` line inside a table is paragraph-shaped
            # content that the user probably meant to end the table
            # before. Report it as an unterminated table — pointing at
            # the current line gives the user the closest reasonable
            # spot to add a closing fence.
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    "expected a `|cell` row inside the table, got "
                    f"prose: {text!r}"
                ),
                kind=ParseErrorKind.UNTERMINATED_TABLE,
            )
        # ``|a|b|c`` → ["", "a", "b", "c"] — drop the empty leading
        # element produced by the row-leading ``|``.
        cells_text = text.split(_TABLE_CELL_SEPARATOR)[1:]
        cells = tuple(
            TableCell(
                inlines=_parse_inline_only(piece, token.line),
                source_line=token.line,
            )
            for piece in cells_text
        )
        if header is not None and len(cells) != len(header.cells):
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    f"table row has {len(cells)} cell(s); "
                    f"header has {len(header.cells)}"
                ),
                kind=ParseErrorKind.TABLE_ROW_ARITY_MISMATCH,
            )
        return TableRow(cells=cells, source_line=token.line)

    @staticmethod
    def _validate_cols_directive_arity(
        *,
        column_proportions: tuple[int, ...] | None,
        header: TableRow,
        directive_line: int,
    ) -> None:
        """Reject a ``[cols=…]`` whose count differs from the header arity.

        The renderer uses the directive's tuple, indexed by column, to
        compute ``max-width-chars`` for each cell. A mismatch would
        index past the end and the wrong column would receive the
        wrong proportion — both are silent visual bugs the parser is
        responsible for catching.
        """
        if column_proportions is None:
            return
        if len(column_proportions) != len(header.cells):
            raise ParseError(
                line=directive_line,
                column=0,
                message=(
                    f"[cols=\"…\"] specifies {len(column_proportions)} "
                    f"column(s) but the table has {len(header.cells)}"
                ),
                kind=ParseErrorKind.BAD_COLS_DIRECTIVE,
            )

    # -- admonitions --------------------------------------------------------

    def _parse_single_admonition(
        self,
        token: SingleAdmonitionToken,
    ) -> Admonition:
        """Wrap a ``KIND: text`` line plus continuation lines in an admonition.

        The kind has already been validated by the lexer (the regex
        only matches the five known labels). The text is run through
        :func:`parse_inline` so inline formatting like ``*bold*`` or
        ``_italic_`` inside the admonition body is preserved.

        Because users routinely wrap admonition prose across multiple
        source lines without a blank between, the parser absorbs any
        immediately-following :class:`LineToken`\\ s into the same
        paragraph. The opener and its continuation lines are joined by
        the shared :func:`_join_source_lines` helper — the same helper
        :meth:`_parse_paragraph` uses — so a `` +`` hard-break marker on
        the opener or any continuation line yields a :class:`HardBreak`
        joiner, and an unmarked boundary a :class:`SoftBreak`. The opener
        (a :class:`SingleAdmonitionToken`, not a :class:`LineToken`) is
        wrapped in a synthetic :class:`LineToken` carrying its text and
        line so the join logic — including marker detection — applies to
        it uniformly. The run ends at the first :class:`BlankToken` or
        non-paragraph block-start token.

        The result is exactly one :class:`Paragraph` in the
        :class:`Admonition`'s ``blocks`` field, so the renderer's
        single-form / block-form code paths fully converge.

        Per-line inline parsing keeps :class:`ParseError.line` exact
        for an unmatched marker on a continuation line — without it,
        the error would point at the admonition's opener line.
        """
        self.pos += 1
        # Treat the opener's text as the first source line of the run, then
        # gather the consecutive continuation LineTokens. Stop at the first
        # BlankToken or non-LineToken — that token is the next block (or
        # paragraph terminator) and is left for the outer block dispatch.
        line_tokens: list[LineToken] = [
            LineToken(line=token.line, text=token.text),
        ]
        while (
            self.pos < len(self.tokens)
            and isinstance(self.tokens[self.pos], LineToken)
        ):
            continuation = self.tokens[self.pos]
            assert isinstance(continuation, LineToken)
            line_tokens.append(continuation)
            self.pos += 1
        paragraph = Paragraph(
            inlines=_join_source_lines(line_tokens),
            source_line=token.line,
        )
        return Admonition(
            kind=token.admonition_kind,
            blocks=(paragraph,),
            source_line=token.line,
        )

    def _parse_block_admonition(
        self,
        directive: AdmonitionDirectiveToken,
    ) -> Admonition:
        """Parse ``[NOTE]`` immediately followed by a ``====`` fence.

        Mirrors the ``[source]`` / ``[cols=…]`` patterns: the directive
        must be followed *immediately* by the opening fence — any
        other token (including a blank line) breaks the binding and
        we raise ``UNKNOWN_BLOCK`` against the directive line. The
        kind is validated against :class:`AdmonitionKind` here; an
        unknown label (e.g. ``[INFO]``) raises
        :class:`ParseErrorKind.UNKNOWN_ADMONITION_TYPE`.
        """
        try:
            kind = AdmonitionKind(directive.kind_str)
        except ValueError as exc:
            raise ParseError(
                line=directive.line,
                column=0,
                message=(
                    f"unknown admonition kind {directive.kind_str!r} "
                    "(expected NOTE, TIP, IMPORTANT, WARNING, or CAUTION)"
                ),
                kind=ParseErrorKind.UNKNOWN_ADMONITION_TYPE,
            ) from exc

        next_index = self.pos + 1
        if (
            next_index >= len(self.tokens)
            or not isinstance(self.tokens[next_index], AdmonitionFenceToken)
        ):
            raise ParseError(
                line=directive.line,
                column=0,
                message=(
                    f"[{directive.kind_str}] directive must be immediately "
                    "followed by a `====` fence"
                ),
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        # Consume the directive; the fence is consumed by the body
        # reader below.
        self.pos += 1
        fence = self.tokens[self.pos]
        assert isinstance(fence, AdmonitionFenceToken)
        body = self._read_paragraphs_until_fence(
            opening_fence_line=fence.line,
            fence_type=AdmonitionFenceToken,
            unterminated_kind=ParseErrorKind.UNTERMINATED_ADMONITION,
            unterminated_message=(
                "admonition block has no closing `====` fence"
            ),
        )
        return Admonition(
            kind=kind,
            blocks=body,
            source_line=directive.line,
        )

    # -- blockquotes --------------------------------------------------------

    def _parse_blockquote_with_directive(
        self,
        directive: QuoteDirectiveToken,
    ) -> Blockquote:
        """Parse ``[quote, …]`` immediately followed by a ``____`` fence.

        Same shape as :meth:`_parse_block_admonition` and the
        ``[source]`` / ``[cols=…]`` patterns: the directive must be
        followed *immediately* by the opening fence. The directive's
        attribution is validated here — empty or whitespace-only
        author/source raises :class:`ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE`.
        """
        author, source = _parse_blockquote_attribution(
            directive.raw_arguments,
            directive.line,
        )

        next_index = self.pos + 1
        if (
            next_index >= len(self.tokens)
            or not isinstance(self.tokens[next_index], QuoteFenceToken)
        ):
            raise ParseError(
                line=directive.line,
                column=0,
                message=(
                    "[quote] directive must be immediately followed by "
                    "a `____` fence"
                ),
                kind=ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
            )
        # Consume the directive; the fence is consumed by the body
        # reader below.
        self.pos += 1
        fence = self.tokens[self.pos]
        assert isinstance(fence, QuoteFenceToken)
        body = self._read_paragraphs_until_fence(
            opening_fence_line=fence.line,
            fence_type=QuoteFenceToken,
            unterminated_kind=ParseErrorKind.UNTERMINATED_BLOCKQUOTE,
            unterminated_message=(
                "blockquote has no closing `____` fence"
            ),
        )
        return Blockquote(
            author=author,
            source=source,
            blocks=body,
            source_line=directive.line,
        )

    def _parse_blockquote_no_directive(
        self,
        fence: QuoteFenceToken,
    ) -> Blockquote:
        """Parse a bare ``____`` blockquote with no ``[quote, …]`` directive."""
        body = self._read_paragraphs_until_fence(
            opening_fence_line=fence.line,
            fence_type=QuoteFenceToken,
            unterminated_kind=ParseErrorKind.UNTERMINATED_BLOCKQUOTE,
            unterminated_message=(
                "blockquote has no closing `____` fence"
            ),
        )
        return Blockquote(
            author=None,
            source=None,
            blocks=body,
            source_line=fence.line,
        )

    # -- shared helper for inline-only block bodies -------------------------

    def _read_paragraphs_until_fence(
        self,
        *,
        opening_fence_line: int,
        fence_type: type[AdmonitionFenceToken] | type[QuoteFenceToken],
        unterminated_kind: ParseErrorKind,
        unterminated_message: str,
    ) -> tuple[Paragraph, ...]:
        """Consume paragraphs from after the opening fence to the closing fence.

        This is the **single shared helper** for inline-only block
        bodies: admonitions and blockquotes both delegate here. The
        rule is "paragraphs only — no nested blocks of any kind". A
        line that starts a non-paragraph block (heading, list bullet,
        code fence, image macro, table fence, nested admonition or
        blockquote, etc.) is rejected with
        :class:`ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER`.
        Blank lines are tolerated as paragraph separators. A closing
        fence of the same ``fence_type`` ends the body; EOF before
        the closing fence raises ``unterminated_kind`` with
        ``unterminated_message``.

        Empty bodies (closing fence immediately after opening) are
        permitted and produce an empty paragraph tuple — the
        renderer handles this by emitting just the framing chrome.
        """
        # Advance past the opening fence (the caller positions us on
        # it; we own consuming both fences).
        self.pos += 1
        paragraphs: list[Paragraph] = []
        while self.pos < len(self.tokens):
            current = self.tokens[self.pos]
            if isinstance(current, fence_type):
                # Closing fence found — consume it and return.
                self.pos += 1
                return tuple(paragraphs)
            if isinstance(current, BlankToken):
                # Blank lines separate paragraphs; advance past.
                self.pos += 1
                continue
            if isinstance(current, LineToken):
                # A run of one-or-more LineTokens forms one paragraph.
                # The existing _parse_paragraph helper does exactly
                # that — and respects per-line inline parsing for
                # exact error line numbers.
                paragraphs.append(self._parse_paragraph())
                continue
            # Anything else is a block-shaped token — heading, list
            # bullet, code fence, image macro, table fence, the OTHER
            # fence type, an admonition directive, a quote directive,
            # etc. Reject as the inline-only-container error.
            raise ParseError(
                line=getattr(current, "line", opening_fence_line),
                column=0,
                message=(
                    "this body accepts paragraphs only — "
                    f"{type(current).__name__} is not allowed"
                ),
                kind=ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER,
            )

        # End of input without a closing fence.
        raise ParseError(
            line=opening_fence_line,
            column=0,
            message=unterminated_message,
            kind=unterminated_kind,
        )

    # -- helpers ------------------------------------------------------------

    def _skip_blanks(self) -> None:
        """Advance ``pos`` past any leading :class:`BlankToken`s."""
        while (
            self.pos < len(self.tokens)
            and isinstance(self.tokens[self.pos], BlankToken)
        ):
            self.pos += 1


# ---------------------------------------------------------------------------
# Tag-value parsing helper
# ---------------------------------------------------------------------------


def parse_tags_value(
    value: str | None,
    line: int,
) -> tuple[str, ...]:
    """Parse a ``:tags:`` attribute value into a sorted tag tuple.

    Shared between the strict parser (called from
    :meth:`_Parser._consume_document_attributes`) and the permissive
    fallback in :mod:`asciidoc.summary`. The normalisation is the same
    in both call sites — only the error handling differs (strict
    re-raises, fallback swallows).

    Normalisation per entry, in order:

    1. Split the raw value on ``","``.
    2. Strip ASCII whitespace from each entry.
    3. Lowercase the entry.
    4. Drop empties (so ``"foo, , bar"`` parses to ``("bar", "foo")``).
    5. Validate each remaining entry against the tag charset
       (``[a-z0-9][a-z0-9-]*``); the first violation raises
       :class:`ParseErrorKind.BAD_TAG_VALUE`.
    6. Deduplicate (first-seen wins).
    7. Sort alphabetically.

    A bare ``:tags:`` setter (``value is None``) or a whitespace-only
    value yields ``()`` with no error.
    """
    if value is None:
        return ()
    seen: set[str] = set()
    normalised: list[str] = []
    for raw in value.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if not _TAG_VALIDATION_RE.match(entry):
            raise ParseError(
                line=line,
                column=0,
                message=f"invalid tag value: {entry!r}",
                kind=ParseErrorKind.BAD_TAG_VALUE,
            )
        if entry in seen:
            continue
        seen.add(entry)
        normalised.append(entry)
    return tuple(sorted(normalised))


# ---------------------------------------------------------------------------
# Multi-line paragraph joining (soft / hard line breaks)
# ---------------------------------------------------------------------------


_HARD_BREAK_MARKER: str = " +"
"""The AsciiDoc inline hard-break marker: a space followed by a plus.

The lexer rstrips trailing whitespace off :attr:`LineToken.text`, so a
line carries a hard break *iff* its (already-rstripped) text ends with
this exact two-character sequence. See :func:`_split_hard_break_marker`.
"""


def _split_hard_break_marker(text: str) -> tuple[str, bool]:
    """Return ``(content_without_marker, had_marker)``.

    ``text`` is already rstripped by the lexer, so the marker is exactly
    a trailing :data:`_HARD_BREAK_MARKER` (`` +``). When present, drop the
    ``+`` and rstrip the residual whitespace; the leftover is the line's
    content. A bare ``+`` with no preceding space (e.g. ``a+b``) is *not*
    a marker and is returned unchanged — ``+`` has no other inline
    meaning, so nothing else is affected.
    """
    if text.endswith(_HARD_BREAK_MARKER):
        return text[:-1].rstrip(), True
    return text, False


def _join_source_lines(line_tokens: Iterable[LineToken]) -> tuple[InlineNode, ...]:
    """Parse a run of paragraph source lines into joined inline nodes.

    Each line's inlines are parsed individually (so :attr:`ParseError.line`
    stays exact). Between two adjacent lines the joiner is a
    :class:`HardBreak` when the *earlier* line ended with the `` +``
    hard-break marker, else a :class:`SoftBreak`. The marker is stripped
    from a line's text before its inlines are parsed (via
    :func:`_split_hard_break_marker`); a marker on the final line is
    stripped but emits no trailing break.

    This is the single shared join helper for both block contexts that
    fold multiple source lines together — paragraphs
    (:meth:`_Parser._parse_paragraph`) and admonition continuation lines
    (:meth:`_Parser._parse_single_admonition`) — so the marker logic
    lives in exactly one place.
    """
    inlines: list[InlineNode] = []
    is_first = True
    previous_had_marker = False
    for line_token in line_tokens:
        content, had_marker = _split_hard_break_marker(line_token.text)
        if not is_first:
            joiner: InlineNode = (
                HardBreak(source_line=line_token.line)
                if previous_had_marker
                else SoftBreak(source_line=line_token.line)
            )
            inlines.append(joiner)
        inlines.extend(parse_inline(content, line_token.line))
        previous_had_marker = had_marker
        is_first = False
    return tuple(inlines)


# ---------------------------------------------------------------------------
# Inline-only container helper
# ---------------------------------------------------------------------------


def _parse_inline_only(text: str, line: int) -> tuple[InlineNode, ...]:
    """Parse ``text`` as inline content for an inline-only container.

    Step 14 introduces this helper for table cells; step 15 will reuse
    it for admonition bodies and blockquote bodies. Centralising the
    "inline content only — no nested blocks" rule in one place keeps
    the strict-mode enforcement consistent across every container that
    needs it.

    For v1 the rule is enforced *by construction*: the function
    delegates to :func:`parse_inline`, which never produces block
    nodes. The only block-shaped content that could appear inside an
    inline-only container is something the *containing* parser
    consumes before this helper sees it — for tables, that's a
    block-level token (heading, code fence, image macro, …) inside
    the fence, which :meth:`_Parser._read_table_body` rejects with
    :class:`ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER`.

    Strips leading and trailing whitespace from the cell text before
    parsing so that ``| cell | other |`` produces tidy inline
    content. Inline syntax itself does not depend on outer padding,
    so this is a presentational normalisation, not a semantic one.
    """
    return parse_inline(text.strip(), line)


def _parse_attachment_table_columns(
    body: str,
    line: int,
) -> tuple[AttachmentTableColumn, ...]:
    """Parse an ``attachments::[…]`` attribute body into its columns.

    ``body`` is the text between the macro's brackets, already stripped.
    An empty body selects every :class:`AttachmentTableColumn` member in
    declaration order — the default column set.

    Otherwise the body must be exactly ``cols="a,b"`` (the quotes are
    optional): any other attribute name, a malformed ``key=value``
    shape, an empty value, or a duplicated column raises
    :class:`ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO`, and a value that
    is not an :class:`AttachmentTableColumn` token raises
    :class:`ParseErrorKind.UNKNOWN_ATTACHMENT_TABLE_COLUMN`.
    """
    if not body:
        return tuple(AttachmentTableColumn)
    match = _ATTACHMENT_TABLE_ATTRIBUTE_RE.match(body)
    if match is None or match.group(1) != _ATTACHMENT_TABLE_COLS_ATTRIBUTE:
        raise ParseError(
            line=line,
            column=0,
            message=(
                "attachments:: macro accepts only a `cols=\"…\"` attribute"
            ),
            kind=ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO,
        )
    columns: list[AttachmentTableColumn] = []
    for part in match.group(2).split(_ATTACHMENT_TABLE_COLUMN_SEPARATOR):
        name = part.strip()
        if not name:
            raise ParseError(
                line=line,
                column=0,
                message=(
                    "attachments:: macro `cols` attribute contains an "
                    "empty column name"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO,
            )
        try:
            column = AttachmentTableColumn(name)
        except ValueError as exc:
            allowed = ", ".join(c.value for c in AttachmentTableColumn)
            raise ParseError(
                line=line,
                column=0,
                message=(
                    f"unknown attachments:: column: {name!r}; "
                    f"only {allowed} are allowed"
                ),
                kind=ParseErrorKind.UNKNOWN_ATTACHMENT_TABLE_COLUMN,
            ) from exc
        if column in columns:
            raise ParseError(
                line=line,
                column=0,
                message=(
                    f"attachments:: macro `cols` attribute repeats the "
                    f"{name!r} column"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO,
            )
        columns.append(column)
    return tuple(columns)


def _parse_cols_proportions(raw: str, line: int) -> tuple[int, ...]:
    """Parse a ``[cols=…]`` directive body into integer proportions.

    The body shape is ``"N,N,..."`` (without the surrounding quotes;
    the lexer already strips them). Each ``N`` must be a positive
    integer. Whitespace around individual values is tolerated.

    Raises :class:`ParseErrorKind.BAD_COLS_DIRECTIVE` for any other
    shape: empty values (``"1,,2"``), non-numeric (``"1,foo"``),
    zero (``"0,1"``), or negative (``"-1"``).
    """
    parts = raw.split(",")
    proportions: list[int] = []
    for part in parts:
        stripped = part.strip()
        if not stripped:
            raise ParseError(
                line=line,
                column=0,
                message=(
                    "[cols=\"…\"] directive contains an empty value"
                ),
                kind=ParseErrorKind.BAD_COLS_DIRECTIVE,
            )
        try:
            value = int(stripped)
        except ValueError as exc:
            raise ParseError(
                line=line,
                column=0,
                message=(
                    f"[cols=\"…\"] directive value is not an integer: "
                    f"{stripped!r}"
                ),
                kind=ParseErrorKind.BAD_COLS_DIRECTIVE,
            ) from exc
        if value <= 0:
            raise ParseError(
                line=line,
                column=0,
                message=(
                    f"[cols=\"…\"] directive value must be positive: "
                    f"{value}"
                ),
                kind=ParseErrorKind.BAD_COLS_DIRECTIVE,
            )
        proportions.append(value)
    return tuple(proportions)


def _parse_blockquote_attribution(
    raw_arguments: str | None,
    line: int,
) -> tuple[str | None, str | None]:
    """Parse a ``[quote, …]`` directive's argument body into ``(author, source)``.

    ``raw_arguments`` is the substring captured by the lexer's
    :data:`_QUOTE_DIRECTIVE_RE` — :data:`None` for the bare
    ``[quote]`` form, or a string starting with ``,`` and continuing
    through the rest of the bracket body.

    The first comma-separated argument is the author; the second is
    the source. A trailing comma without content, an empty author
    field, an empty source field, or a third comma-separated field
    are all rejected with
    :class:`ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE`. Whitespace
    around individual values is tolerated and stripped.

    Both returned fields are :data:`None` when the directive carries
    no arguments (``[quote]``); each is a non-empty string when set.
    """
    if raw_arguments is None:
        # ``[quote]`` — no attribution.
        return None, None

    # ``raw_arguments`` always starts with ``,`` because the lexer's
    # capture group includes the leading comma. Drop it before
    # splitting. ``maxsplit=2`` lets us detect a third (forbidden)
    # comma-separated field — split into at most three parts.
    arguments = raw_arguments[1:]
    parts = arguments.split(",", maxsplit=2)

    # parts has 1, 2, or 3 elements. We only allow 1 (author only)
    # or 2 (author + source); 3 is malformed.
    if len(parts) > 2:
        raise ParseError(
            line=line,
            column=0,
            message=(
                "[quote, …] directive accepts at most two attribution "
                "arguments (author, source); got more"
            ),
            kind=ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )

    author_raw = parts[0]
    author = author_raw.strip()
    if not author:
        raise ParseError(
            line=line,
            column=0,
            message=(
                "[quote, …] directive author argument is empty"
            ),
            kind=ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )

    if len(parts) == 1:
        return author, None

    source_raw = parts[1]
    source = source_raw.strip()
    if not source:
        raise ParseError(
            line=line,
            column=0,
            message=(
                "[quote, …] directive source argument is empty"
            ),
            kind=ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE,
        )
    return author, source
