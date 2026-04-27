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
* Step 4 only produces the constructs the matching :data:`BlockNode`
  union admits: sections, paragraphs, ordered/unordered lists, code
  blocks, and images. Tokens that look like AsciiDoc but belong to
  later steps (table fences, admonition directives, attribute entries,
  comments, blockquote fences) are detected at block-start and
  rejected as :class:`ParseErrorKind.UNKNOWN_BLOCK`. They never become
  paragraphs.
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
  Lines are joined in the AST with ``Text("\\n", line)`` connectors.
* Lists are flat in step 4: a single run of adjacent ``*`` or ``.``
  bullets terminated by anything else (blank, heading, fence, EOF).
  Multi-line items, nesting, and ``+`` continuations are deferred —
  the welcome note exercises only the supported shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from notes_app.asciidoc.ast import (
    BlockNode,
    CodeBlock,
    Document,
    Image,
    InlineNode,
    ListItem,
    OrderedList,
    Paragraph,
    Section,
    Text,
    UnorderedList,
)
from notes_app.asciidoc.inline_parser import parse_inline
from notes_app.asciidoc.lexer import (
    BlankToken,
    CodeDirectiveToken,
    CodeFenceToken,
    HeadingToken,
    ImageMacroToken,
    LineToken,
    ListBulletToken,
    ListNumberToken,
    Token,
    source_lines,
    tokenize,
)
from notes_app.enums import ParseErrorKind
from notes_app.models.parse_error import ParseError


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_DOCUMENT_TITLE_LEVEL: int = 1
_MIN_SECTION_LEVEL: int = 2
_MAX_SECTION_LEVEL: int = 6
_DOCUMENT_SOURCE_LINE: int = 1

_TABLE_FENCE_LITERAL: str = "|==="
_BLOCKQUOTE_FENCE_LITERAL: str = "____"
_COMMENT_PREFIX: str = "//"

# An attribute entry such as ``:doctype: book`` or ``:source-highlighter:``.
# Step 4 does not support attribute entries, so any line matching this
# pattern is rejected as ``UNKNOWN_BLOCK`` rather than treated as prose.
_ATTRIBUTE_ENTRY_RE: re.Pattern[str] = re.compile(r"^:[A-Za-z0-9_-]+:")

# An image macro is split into ``filename`` and ``attrs`` on the first
# unescaped ``[``. Step 4 does not interpret ``attrs`` — but it does
# enforce that the brackets are matched and that no nested brackets
# appear inside ``attrs``.
_IMAGE_OPEN_BRACKET: str = "["
_IMAGE_CLOSE_BRACKET: str = "]"


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
        blocks = self._parse_blocks(stop_at_heading_level=None)
        return Document(
            title=title,
            blocks=tuple(blocks),
            source_line=_DOCUMENT_SOURCE_LINE,
        )

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

    def _parse_non_heading_block(self, token: Token) -> BlockNode:
        """Dispatch every non-heading block-start token to its parser."""
        if isinstance(token, ListBulletToken):
            return self._parse_unordered_list()
        if isinstance(token, ListNumberToken):
            return self._parse_ordered_list()
        if isinstance(token, CodeDirectiveToken):
            return self._parse_code_block_with_directive(token)
        if isinstance(token, CodeFenceToken):
            return self._parse_code_block_no_directive(token)
        if isinstance(token, ImageMacroToken):
            return self._parse_image(token)
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
        yet implement (table fences, admonition directives, attribute
        entries, line comments, blockquote fences, ``[…]`` directives)
        must produce a parse error rather than be silently swept into a
        paragraph. The parser detects these at block-start only — once
        we are mid-paragraph, a ``//`` line is part of the prose.
        """
        text = token.text

        if text == _TABLE_FENCE_LITERAL:
            raise ParseError(
                line=token.line,
                column=0,
                message="table fences are not supported in this build step",
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        if text == _BLOCKQUOTE_FENCE_LITERAL:
            raise ParseError(
                line=token.line,
                column=0,
                message=(
                    "blockquote fences are not supported in this build step"
                ),
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        if text.startswith(_COMMENT_PREFIX):
            raise ParseError(
                line=token.line,
                column=0,
                message="line comments are not supported",
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        if _ATTRIBUTE_ENTRY_RE.match(text):
            raise ParseError(
                line=token.line,
                column=0,
                message="attribute entries are not supported",
                kind=ParseErrorKind.UNKNOWN_BLOCK,
            )
        # Lines wrapped in ``[…]`` — admonition openers (``[NOTE]``),
        # ``[quote]``, ``[cols=…]``, even malformed ``[source,]`` — all
        # land here. They are paragraph-shaped only by accident.
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
        :class:`ParseError`\\ s are exact. Lines are joined in the AST
        with ``Text("\\n", source_line)`` connectors so the renderer
        can decide whether to honour soft line breaks.
        """
        first_line_token = self.tokens[self.pos]
        assert isinstance(first_line_token, LineToken), (
            "_parse_paragraph called without a LineToken at the cursor"
        )
        start_line = first_line_token.line

        inlines: list[InlineNode] = []
        is_first = True
        while (
            self.pos < len(self.tokens)
            and isinstance(self.tokens[self.pos], LineToken)
        ):
            line_token = self.tokens[self.pos]
            assert isinstance(line_token, LineToken)
            if not is_first:
                inlines.append(
                    Text(content="\n", source_line=line_token.line)
                )
            inlines.extend(parse_inline(line_token.text, line_token.line))
            is_first = False
            self.pos += 1

        return Paragraph(inlines=tuple(inlines), source_line=start_line)

    # -- lists --------------------------------------------------------------

    def _parse_unordered_list(self) -> UnorderedList:
        """Consume a run of adjacent ``* …`` lines."""
        items: list[ListItem] = []
        start_line = self.tokens[self.pos].line  # type: ignore[union-attr]
        while (
            self.pos < len(self.tokens)
            and isinstance(self.tokens[self.pos], ListBulletToken)
        ):
            bullet = self.tokens[self.pos]
            assert isinstance(bullet, ListBulletToken)
            inlines = parse_inline(bullet.text, bullet.line)
            items.append(
                ListItem(inlines=inlines, source_line=bullet.line)
            )
            self.pos += 1
        return UnorderedList(items=tuple(items), source_line=start_line)

    def _parse_ordered_list(self) -> OrderedList:
        """Consume a run of adjacent ``. …`` lines."""
        items: list[ListItem] = []
        start_line = self.tokens[self.pos].line  # type: ignore[union-attr]
        while (
            self.pos < len(self.tokens)
            and isinstance(self.tokens[self.pos], ListNumberToken)
        ):
            number = self.tokens[self.pos]
            assert isinstance(number, ListNumberToken)
            inlines = parse_inline(number.text, number.line)
            items.append(
                ListItem(inlines=inlines, source_line=number.line)
            )
            self.pos += 1
        return OrderedList(items=tuple(items), source_line=start_line)

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

    # -- helpers ------------------------------------------------------------

    def _skip_blanks(self) -> None:
        """Advance ``pos`` past any leading :class:`BlankToken`s."""
        while (
            self.pos < len(self.tokens)
            and isinstance(self.tokens[self.pos], BlankToken)
        ):
            self.pos += 1
