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
* Step 4's lexer recognised only the constructs that step 4's parser
  understood. Step 14 extended the produced set with
  :class:`TableFenceToken` and :class:`ColsDirectiveToken` for table
  support. Step 15 adds five further token classes:
  :class:`AdmonitionDirectiveToken` (``[NOTE]`` and friends),
  :class:`AdmonitionFenceToken` (``====``),
  :class:`SingleAdmonitionToken` (``NOTE: text``),
  :class:`QuoteDirectiveToken` (``[quote, Author, Source]``), and
  :class:`QuoteFenceToken` (``____``).
* The :data:`Token` union is closed: every concrete token class belongs
  to it. This lets the parser pattern-match exhaustively.
* The four-equals admonition fence and four-underscore blockquote fence
  literals are matched **before** the heading classifier. Without the
  precedence, ``====`` would otherwise be lexed as a level-4 heading
  with empty body — a precedence bug that would make every block
  admonition look like a string of empty headings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

from notes_app.enums import AdmonitionKind, TokenKind


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
class TableFenceToken:
    """A table fence: a line that is exactly ``|===``.

    The same token kind is emitted for both the opening and the closing
    fence — they are indistinguishable at the line level. The parser
    pairs them by counting, the same way it does for ``----`` code
    fences.
    """

    kind: ClassVar[TokenKind] = TokenKind.TABLE_FENCE

    line: int


@dataclass(frozen=True)
class ColsDirectiveToken:
    """A ``[cols="N,N,..."]`` directive that precedes a table fence.

    ``raw`` is the substring inside the quotes, exactly as it appeared
    in the source (modulo whitespace stripping). The parser splits it
    into integers and validates that each is positive — the lexer does
    not, because rejection of zero / negative / non-numeric values is
    one of the strict-mode error contracts the parser owns
    (:class:`ParseErrorKind.BAD_COLS_DIRECTIVE`).

    The parser requires this token to be immediately followed by a
    :class:`TableFenceToken` — anything else is rejected as
    :class:`ParseErrorKind.UNKNOWN_BLOCK`.
    """

    kind: ClassVar[TokenKind] = TokenKind.COLS_DIRECTIVE

    line: int
    raw: str


@dataclass(frozen=True)
class AdmonitionDirectiveToken:
    """A ``[NOTE]``-shape line that opens a block admonition.

    The lexer matches any all-caps single word inside square brackets
    on a line by itself — ``[NOTE]``, ``[TIP]``, ``[INFO]``, ``[FOO]``
    all produce this token. The parser is responsible for validating
    the kind against :class:`AdmonitionKind`; an unrecognised label
    raises :class:`ParseErrorKind.UNKNOWN_ADMONITION_TYPE`. Keeping
    the lexer permissive here matches how the parser reports the
    error pointing at the directive line rather than at a generic
    "unrecognised block".

    The parser also requires this token to be immediately followed by
    an :class:`AdmonitionFenceToken` — anything else is rejected.
    """

    kind: ClassVar[TokenKind] = TokenKind.ADMONITION_DIRECTIVE

    line: int
    kind_str: str


@dataclass(frozen=True)
class AdmonitionFenceToken:
    """An admonition fence: a line that is exactly ``====``.

    Matched ahead of :class:`HeadingToken` in the line classifier so
    the user's ``====`` opener does not accidentally become a level-4
    heading with empty body. The same token kind is emitted for both
    the opening and the closing fence — they are indistinguishable at
    the line level. The parser pairs them by counting, exactly as it
    does for ``----`` code fences and ``|===`` table fences.

    A stray ``====`` (one not preceded by an
    :class:`AdmonitionDirectiveToken`) is rejected at the parser's
    block-dispatch as :class:`ParseErrorKind.UNKNOWN_BLOCK`. The
    lexer does not know whether the fence has an opener — that is
    by design (the lexer is line-based and context-free).
    """

    kind: ClassVar[TokenKind] = TokenKind.ADMONITION_FENCE

    line: int


@dataclass(frozen=True)
class SingleAdmonitionToken:
    """A single-line admonition: ``NOTE: text``, ``TIP: text``, etc.

    Restricted at lex time to the five labels in :class:`AdmonitionKind`
    — anything else (e.g. ``URL: https://example.com``) is plain prose
    that lexes to :class:`LineToken` and parses as a paragraph. This
    keeps every other ``WORD: …`` line from being misinterpreted as a
    malformed admonition. The :class:`AdmonitionKind` member is
    captured here so the parser does not need to re-validate.

    ``text`` is the substring after the ``"<KIND>: "`` prefix, with
    trailing whitespace already stripped by the line classifier. By
    construction it is non-empty (the lexer's pattern requires at
    least one character after the colon-space).
    """

    kind: ClassVar[TokenKind] = TokenKind.SINGLE_ADMONITION

    line: int
    admonition_kind: AdmonitionKind
    text: str


@dataclass(frozen=True)
class QuoteDirectiveToken:
    """A ``[quote, …]`` directive that may precede a blockquote fence.

    ``raw_arguments`` is the substring inside the brackets *after* the
    ``quote`` keyword and *including* its leading comma when present —
    or :data:`None` when the directive is the bare ``[quote]`` form.
    The lexer never validates the contents; the parser splits on
    commas, validates non-emptiness, and raises
    :class:`ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE` for malformed
    attribution.

    The parser also requires this token to be immediately followed by
    a :class:`QuoteFenceToken` — anything else is rejected.
    """

    kind: ClassVar[TokenKind] = TokenKind.QUOTE_DIRECTIVE

    line: int
    raw_arguments: str | None


@dataclass(frozen=True)
class QuoteFenceToken:
    """A blockquote fence: a line that is exactly ``____``.

    Matched ahead of :class:`LineToken` so that bare ``____`` opens a
    blockquote (with no attribution). The same token kind is emitted
    for both opening and closing fences. A stray ``____`` is also
    valid — it opens an unattributed blockquote. The parser then
    walks paragraphs until the matching closing fence.
    """

    kind: ClassVar[TokenKind] = TokenKind.QUOTE_FENCE

    line: int


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
    | TableFenceToken
    | ColsDirectiveToken
    | AdmonitionDirectiveToken
    | AdmonitionFenceToken
    | SingleAdmonitionToken
    | QuoteDirectiveToken
    | QuoteFenceToken
    | BlankToken
    | LineToken
)
"""Closed union of every token the lexer produces.

Step 4 produced the original eight token classes; step 14 extended the
union with :class:`TableFenceToken` and :class:`ColsDirectiveToken`;
step 15 extends it further with :class:`AdmonitionDirectiveToken`,
:class:`AdmonitionFenceToken`, :class:`SingleAdmonitionToken`,
:class:`QuoteDirectiveToken`, and :class:`QuoteFenceToken`.
"""


# ---------------------------------------------------------------------------
# Module-level pattern constants
# ---------------------------------------------------------------------------

_LIST_BULLET_PREFIX: str = "* "
_LIST_NUMBER_PREFIX: str = ". "
_CODE_FENCE_LITERAL: str = "----"
_IMAGE_MACRO_PREFIX: str = "image::"
_TABLE_FENCE_LITERAL: str = "|==="
_ADMONITION_FENCE_LITERAL: str = "===="
_QUOTE_FENCE_LITERAL: str = "____"

_CODE_DIRECTIVE_BARE: str = "[source]"
_CODE_DIRECTIVE_WITH_LANG_PREFIX: str = "[source,"
_CODE_DIRECTIVE_SUFFIX: str = "]"

_COLS_DIRECTIVE_PREFIX: str = '[cols="'
_COLS_DIRECTIVE_SUFFIX: str = '"]'

_HEADING_MARKER_CHAR: str = "="

# An ``[ALL_CAPS]`` directive on a line by itself — admonition opener
# shape. The lexer matches any all-caps single word so that an
# unrecognised label like ``[INFO]`` reaches the parser as an
# :class:`AdmonitionDirectiveToken` and surfaces as
# :class:`ParseErrorKind.UNKNOWN_ADMONITION_TYPE` (a specific error
# pointing at the directive line) rather than the generic
# ``UNKNOWN_BLOCK`` produced by ``_reject_unknown_block``.
_ADMONITION_DIRECTIVE_RE: re.Pattern[str] = re.compile(r"^\[([A-Z]+)\]$")

# A single-line admonition: one of the five known kinds, a colon, a
# space, then non-empty text. Restricted to the five labels at lex time
# so prose lines like ``URL: https://example.com`` stay paragraphs.
_SINGLE_ADMONITION_RE: re.Pattern[str] = re.compile(
    r"^(NOTE|TIP|IMPORTANT|WARNING|CAUTION): (.+)$"
)

# A ``[quote]`` or ``[quote, …]`` directive. The bracketed body after
# the ``quote`` keyword is captured raw — the parser splits and
# validates. The lexer only checks the structural shape.
_QUOTE_DIRECTIVE_RE: re.Pattern[str] = re.compile(r"^\[quote(,.*)?\]$")


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
    # pylint: disable=too-many-return-statements,too-many-branches
    # Each branch returns a different token class; flattening the
    # dispatch into a table would require boxing each branch into a
    # closure with no readability gain. The branch count grew with
    # step 14 (table fence / cols directive) and step 15 (admonition
    # and quote fences and directives) — every additional construct
    # is one more block-line shape to recognise.
    line = raw_line.rstrip()
    if not line:
        return BlankToken(line=line_number)

    # The four-equals admonition fence and four-underscore quote fence
    # are checked **before** the heading classifier. Without this
    # precedence ``====`` would be lexed as a level-4 heading with
    # empty text — the heading classifier accepts any number of leading
    # ``=`` followed by no content. The fences are exact-literal
    # matches, so the two checks do not overlap with valid heading
    # levels (5 or 6 equals are still headings, never fences).
    if line == _ADMONITION_FENCE_LITERAL:
        return AdmonitionFenceToken(line=line_number)
    if line == _QUOTE_FENCE_LITERAL:
        return QuoteFenceToken(line=line_number)

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

    if line == _TABLE_FENCE_LITERAL:
        return TableFenceToken(line=line_number)

    code_directive = _try_code_directive(line, line_number)
    if code_directive is not None:
        return code_directive

    cols_directive = _try_cols_directive(line, line_number)
    if cols_directive is not None:
        return cols_directive

    # Quote directive comes before the generic admonition directive
    # because ``[quote]`` would otherwise match the all-caps admonition
    # directive's regex (``[A-Z]+`` admits no lowercase letters in the
    # one-word case, but ``quote`` is lowercase so they don't actually
    # overlap — keeping this order preserves intent regardless).
    quote_directive = _try_quote_directive(line, line_number)
    if quote_directive is not None:
        return quote_directive

    admonition_directive = _try_admonition_directive(line, line_number)
    if admonition_directive is not None:
        return admonition_directive

    single_admonition = _try_single_admonition(line, line_number)
    if single_admonition is not None:
        return single_admonition

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


def _try_cols_directive(line: str, line_number: int) -> ColsDirectiveToken | None:
    """If ``line`` is ``[cols="N,N,..."]``, return the token.

    The lexer only checks the structural shape: a ``[cols="`` prefix, a
    closing ``"]`` suffix, and a non-empty body in between. Whether the
    body is well-formed (positive integers separated by commas) is the
    parser's job — :class:`ParseErrorKind.BAD_COLS_DIRECTIVE` carries
    that contract. Returning ``None`` here for a malformed-looking shape
    falls through to a plain :class:`LineToken`, which the parser then
    rejects as ``UNKNOWN_BLOCK`` (matching how the same path handles
    ``[source,]`` and other near-miss directive shapes).
    """
    if not (
        line.startswith(_COLS_DIRECTIVE_PREFIX)
        and line.endswith(_COLS_DIRECTIVE_SUFFIX)
    ):
        return None
    raw = line[
        len(_COLS_DIRECTIVE_PREFIX): -len(_COLS_DIRECTIVE_SUFFIX)
    ].strip()
    if not raw:
        # ``[cols=""]`` — no proportions at all. Fall through so the
        # parser can raise UNKNOWN_BLOCK against the bracketed shape.
        return None
    return ColsDirectiveToken(line=line_number, raw=raw)


def _try_admonition_directive(
    line: str,
    line_number: int,
) -> AdmonitionDirectiveToken | None:
    """If ``line`` is ``[ALL_CAPS]``, return the directive token.

    Permissive at the lexer level: any all-caps single word inside
    brackets matches. Validation against :class:`AdmonitionKind`
    happens at parse time so an unrecognised label produces a specific
    :class:`ParseErrorKind.UNKNOWN_ADMONITION_TYPE` rather than the
    generic ``UNKNOWN_BLOCK`` produced by ``_reject_unknown_block``.
    """
    match = _ADMONITION_DIRECTIVE_RE.match(line)
    if match is None:
        return None
    return AdmonitionDirectiveToken(
        line=line_number,
        kind_str=match.group(1),
    )


def _try_single_admonition(
    line: str,
    line_number: int,
) -> SingleAdmonitionToken | None:
    """If ``line`` is ``KIND: text``, return the single-line token.

    Restricted to the five known kinds. The match is anchored at the
    start of the line so a mid-prose ``Note: see foo`` is not
    misinterpreted (the leading ``Note`` is mixed-case, and the line
    classifier only sees the line as a whole). Conversely, a literal
    ``URL: https://example.com`` falls through to
    :class:`LineToken` because ``URL`` is not a known kind.
    """
    match = _SINGLE_ADMONITION_RE.match(line)
    if match is None:
        return None
    kind_str = match.group(1)
    text = match.group(2)
    # ``kind_str`` is one of the five labels by construction (the
    # alternation in the regex enforces it), so the lookup is always
    # well-defined and never raises.
    return SingleAdmonitionToken(
        line=line_number,
        admonition_kind=AdmonitionKind(kind_str),
        text=text,
    )


def _try_quote_directive(
    line: str,
    line_number: int,
) -> QuoteDirectiveToken | None:
    """If ``line`` is ``[quote]`` or ``[quote, …]``, return the token.

    The captured group includes the leading comma when arguments are
    present (or :data:`None` for the bare ``[quote]`` form). The
    parser splits the captured arguments on commas and validates each
    field for non-emptiness — the lexer is intentionally permissive.
    """
    match = _QUOTE_DIRECTIVE_RE.match(line)
    if match is None:
        return None
    return QuoteDirectiveToken(
        line=line_number,
        raw_arguments=match.group(1),
    )
