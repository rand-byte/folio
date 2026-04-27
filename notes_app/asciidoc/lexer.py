"""Line-level tokenizer for the AsciiDoc subset.

Principles & invariants
-----------------------
* The lexer is pure and deterministic. ``tokenize(source)`` is a function
  of the source string only — no I/O, no global state, no GTK.
* The lexer is **permissive**: it classifies each source line into the
  most specific token it recognises and, when no specialised pattern
  matches, emits a generic :class:`LineToken`. It does **not** raise on
  grammar issues — that is the parser's job. The lexer would only raise
  for un-tokenisable bytes, which our line-based scheme never produces
  in practice.
* The lexer is **line-based and context-free**. It looks at one line at
  a time and never consults the surrounding lines. This means a line
  like ``* foo`` is always classified as :class:`ListBulletToken` even
  if it is logically inside a code block — the parser, which *does*
  know context, looks at the surrounding fence tokens and reads the
  raw text of intermediate lines via :func:`source_lines` rather than
  via the token's parsed payload.
* Trailing whitespace on a line is irrelevant for classification, so the
  lexer matches against the right-stripped form. The original
  un-stripped source line is *not* carried on the token: when the
  parser needs verbatim text (e.g. the body of a code block), it reads
  it directly from the source — see :func:`source_lines`.
* Step 4's lexer recognises only the constructs that step 4's parser
  understands. Tokens for tables, admonitions, blockquotes, and quote
  / cols directives are listed in :class:`TokenKind` for forward
  compatibility but are not produced here. They will be added when the
  matching parser support lands (steps 14 / 15).
* The :data:`Token` union is closed: every concrete token class belongs
  to it. This lets the parser pattern-match exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from notes_app.enums import TokenKind


# ---------------------------------------------------------------------------
# Token classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeadingToken:
    """A line that starts with one or more ``=`` followed by space + text.

    The token is produced for any number of leading equals — the parser
    decides whether the level is in range (1..6) and whether the text is
    non-empty. ``text`` is the heading body with leading and trailing
    whitespace stripped; it may be the empty string for a bare
    equals-only line such as ``==``.
    """

    kind: ClassVar[TokenKind] = TokenKind.HEADING

    line: int
    level: int
    text: str


@dataclass(frozen=True)
class ListBulletToken:
    """An unordered-list line: ``* item-text``.

    ``text`` is everything after the first ``"* "`` — leading spaces in
    ``text`` are preserved, trailing whitespace on the line is stripped
    by the lexer.
    """

    kind: ClassVar[TokenKind] = TokenKind.LIST_BULLET

    line: int
    text: str


@dataclass(frozen=True)
class ListNumberToken:
    """An ordered-list line: ``. item-text``."""

    kind: ClassVar[TokenKind] = TokenKind.LIST_NUMBER

    line: int
    text: str


@dataclass(frozen=True)
class CodeFenceToken:
    """A code-block fence: a line that is exactly ``----``.

    The same token kind is emitted for both the opening and the closing
    fence — they are indistinguishable at the line level. The parser
    pairs them by counting.
    """

    kind: ClassVar[TokenKind] = TokenKind.CODE_FENCE

    line: int


@dataclass(frozen=True)
class CodeDirectiveToken:
    """A ``[source]`` or ``[source,LANG]`` directive line.

    The parser requires this token to be immediately followed by a
    :class:`CodeFenceToken` — anything else is rejected as
    :class:`ParseErrorKind.UNKNOWN_BLOCK`.

    ``language`` is ``None`` for ``[source]`` and the trimmed string
    after the comma otherwise. The lexer does not validate the language
    name; it is opaque to the parser as well.
    """

    kind: ClassVar[TokenKind] = TokenKind.CODE_DIRECTIVE

    line: int
    language: str | None


@dataclass(frozen=True)
class ImageMacroToken:
    """An ``image::FILE[ATTRS]`` block-level image macro.

    ``raw`` is the text *after* the ``image::`` prefix, exactly as it
    appeared in the source (modulo trailing whitespace). The parser
    splits it into filename and attribute list — the lexer does not,
    because the split *is* one of the validations the parser owns.
    """

    kind: ClassVar[TokenKind] = TokenKind.IMAGE_MACRO

    line: int
    raw: str


@dataclass(frozen=True)
class BlankToken:
    """A blank or whitespace-only line.

    Blank tokens are significant: they terminate paragraphs, separate
    list runs from following blocks, and act as separators inside
    code-block bodies (where they are read as raw blank lines, not as
    these tokens).
    """

    kind: ClassVar[TokenKind] = TokenKind.BLANK

    line: int


@dataclass(frozen=True)
class LineToken:
    """A line that did not match any specialised pattern.

    Inside a paragraph the line is parsed for inline formatting; at
    block-start the parser checks for known-but-unsupported constructs
    (``|===``, ``[NOTE]``, ``:attr: value``, ``// comment``) and raises
    :class:`ParseErrorKind.UNKNOWN_BLOCK` rather than silently treating
    them as paragraph content.

    ``text`` is the source line with trailing whitespace stripped.
    """

    kind: ClassVar[TokenKind] = TokenKind.LINE

    line: int
    text: str


type Token = (
    HeadingToken
    | ListBulletToken
    | ListNumberToken
    | CodeFenceToken
    | CodeDirectiveToken
    | ImageMacroToken
    | BlankToken
    | LineToken
)
"""Closed union of every token the step-4 lexer produces."""


# ---------------------------------------------------------------------------
# Module-level pattern constants
# ---------------------------------------------------------------------------

_LIST_BULLET_PREFIX: str = "* "
_LIST_NUMBER_PREFIX: str = ". "
_CODE_FENCE_LITERAL: str = "----"
_IMAGE_MACRO_PREFIX: str = "image::"

_CODE_DIRECTIVE_BARE: str = "[source]"
_CODE_DIRECTIVE_WITH_LANG_PREFIX: str = "[source,"
_CODE_DIRECTIVE_SUFFIX: str = "]"

_HEADING_MARKER_CHAR: str = "="


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tokenize(source: str) -> list[Token]:
    """Split ``source`` into a list of typed line tokens.

    The result has one token per source line. Line numbers in the
    returned tokens are 1-indexed and match what the user sees in the
    editor's gutter.
    """
    tokens: list[Token] = []
    for line_index, raw_line in enumerate(source.splitlines()):
        line_number = line_index + 1
        tokens.append(_classify_line(raw_line, line_number))
    return tokens


def source_lines(source: str) -> list[str]:
    """Return ``source`` split into lines without trailing newlines.

    The parser keeps this list alongside the tokens so that contexts
    that need *verbatim* line text (such as the body of a code block)
    can read the original line — including any trailing whitespace —
    rather than the lexer's right-stripped form. Calling this is
    O(n) in the source length; the parser caches the result once at
    construction.
    """
    return source.splitlines()


# ---------------------------------------------------------------------------
# Line classification
# ---------------------------------------------------------------------------


def _classify_line(raw_line: str, line_number: int) -> Token:
    """Decide which :data:`Token` kind ``raw_line`` represents."""
    # pylint: disable=too-many-return-statements
    # Each branch returns a different token class; flattening the
    # dispatch into a table would require boxing each branch into a
    # closure with no readability gain.
    line = raw_line.rstrip()
    if not line:
        return BlankToken(line=line_number)

    heading = _try_heading(line, line_number)
    if heading is not None:
        return heading

    if line.startswith(_LIST_BULLET_PREFIX):
        return ListBulletToken(
            line=line_number,
            text=line[len(_LIST_BULLET_PREFIX):],
        )
    if line.startswith(_LIST_NUMBER_PREFIX):
        return ListNumberToken(
            line=line_number,
            text=line[len(_LIST_NUMBER_PREFIX):],
        )

    if line == _CODE_FENCE_LITERAL:
        return CodeFenceToken(line=line_number)

    code_directive = _try_code_directive(line, line_number)
    if code_directive is not None:
        return code_directive

    if line.startswith(_IMAGE_MACRO_PREFIX):
        return ImageMacroToken(
            line=line_number,
            raw=line[len(_IMAGE_MACRO_PREFIX):],
        )

    return LineToken(line=line_number, text=line)


def _try_heading(line: str, line_number: int) -> HeadingToken | None:
    """If ``line`` looks like a heading, return the token; else ``None``.

    A heading is one or more ``=`` characters at the start of the line,
    followed by either end-of-line or whitespace and then the heading
    text. The number of equals is recorded as ``level`` regardless of
    whether it is in the supported range — the parser is responsible
    for rejecting headings deeper than level 6 and for raising
    :class:`ParseErrorKind.EMPTY_HEADING` when the text is empty.
    """
    if not line.startswith(_HEADING_MARKER_CHAR):
        return None
    level = 0
    while level < len(line) and line[level] == _HEADING_MARKER_CHAR:
        level += 1
    rest = line[level:]
    if rest and not rest.startswith(" "):
        # The equals were followed by something other than space —
        # this is not a heading marker. Examples: '==foo', '=#'.
        return None
    text = rest.lstrip(" ")
    return HeadingToken(line=line_number, level=level, text=text)


def _try_code_directive(line: str, line_number: int) -> CodeDirectiveToken | None:
    """If ``line`` is ``[source]`` or ``[source,LANG]``, return the token."""
    if line == _CODE_DIRECTIVE_BARE:
        return CodeDirectiveToken(line=line_number, language=None)
    if not (
        line.startswith(_CODE_DIRECTIVE_WITH_LANG_PREFIX)
        and line.endswith(_CODE_DIRECTIVE_SUFFIX)
    ):
        return None
    language = line[
        len(_CODE_DIRECTIVE_WITH_LANG_PREFIX): -len(_CODE_DIRECTIVE_SUFFIX)
    ].strip()
    if not language:
        # ``[source,]`` is not a valid directive — the language is
        # missing. Fall through to a plain LineToken so the parser can
        # raise UNKNOWN_BLOCK against the bracketed shape.
        return None
    return CodeDirectiveToken(line=line_number, language=language)
