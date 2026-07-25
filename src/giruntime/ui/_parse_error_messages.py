"""User-facing messages for parse errors.

Principles & invariants
-----------------------
* :func:`_message_for` maps a :class:`~enums.ParseErrorKind` to the single
  sentence shown in the read pane's in-surface error notice. The ``match``
  is **exhaustive** over :class:`ParseErrorKind` — one distinct message per
  member — and is closed for the type-checker (a missing arm is a ``Never``
  error), so this table and the enum cannot drift. It carries only the
  detail line; the notice chrome (icon, headline, hint) is the pane's.
* Pure: no GTK, no storage, no widget state. Depends only on
  :mod:`enums`.
"""
from __future__ import annotations

from enums import AttachmentTableColumn, LinkScheme, ParseErrorKind


_ALLOWED_SCHEMES_LIST: str = ", ".join(s.value for s in LinkScheme)
"""Pre-computed comma-joined list of supported link schemes, used in
the user-facing message for :data:`ParseErrorKind.UNSUPPORTED_LINK_SCHEME`.
Computed once at import time so the message is stable and the enum is
queried only once.
"""


_ATTACHMENT_TABLE_COLUMNS_LIST: str = ", ".join(
    c.value for c in AttachmentTableColumn
)
"""Pre-computed list of the columns ``attachments::[cols=…]`` accepts.

Computed once at import time, like :data:`_ALLOWED_SCHEMES_LIST`, so the
user-facing message for
:data:`ParseErrorKind.UNKNOWN_ATTACHMENT_TABLE_COLUMN` names exactly the
enum's members and cannot drift from them.
"""


def _message_for(kind: ParseErrorKind, line: int) -> str:
    # pylint: disable=too-many-return-statements
    # The ``match`` is intentionally exhaustive over
    # :class:`ParseErrorKind` — every member produces a distinct
    # user-facing message, so the number of cases equals the size of
    # the enum. Splitting them into a dispatch dict would replace
    # one ``match`` block with a dict literal of equal length and
    # would break Python's pattern-match exhaustiveness story (a
    # missing key fails at runtime, while a missing match arm shows
    # up to type-checkers that understand ``Never``).
    """Return a user-facing message for a parse error.

    The mapping is exhaustive over :class:`ParseErrorKind` — every
    member must produce a sentence. A unit test iterates the enum and
    asserts each kind has an entry, so adding a new kind forces an
    update here at the same time.

    The parser's internal ``ParseError.message`` is *not* shown
    verbatim because those strings are written for developers and
    would confuse end users. The message is short, line-prefixed
    where useful, and mentions the user's most likely fix when
    obvious.
    """
    match kind:
        case ParseErrorKind.UNTERMINATED_CODE_BLOCK:
            return (
                f"Line {line}: a code block was opened but never closed "
                "with `----`."
            )
        case ParseErrorKind.UNKNOWN_BLOCK:
            return (
                f"Line {line}: this construct isn't recognised. Check for "
                "a typo, an unsupported directive, or a misplaced attribute."
            )
        case ParseErrorKind.BAD_IMAGE_MACRO:
            return (
                f"Line {line}: the image macro is malformed. Expected "
                "`image::filename[attrs]`."
            )
        case ParseErrorKind.BAD_INLINE_SPAN:
            return (
                f"Line {line}: an inline formatting marker (`*`, `_`, or "
                "`#`) was opened but not closed on the same line."
            )
        case ParseErrorKind.EMPTY_HEADING:
            return f"Line {line}: a heading marker has no text after it."
        case ParseErrorKind.UNTERMINATED_TABLE:
            return (
                f"Line {line}: a table was opened but never closed with "
                "`|===`."
            )
        case ParseErrorKind.EMPTY_TABLE:
            return f"Line {line}: this table has no rows between the fences."
        case ParseErrorKind.TABLE_ROW_ARITY_MISMATCH:
            return (
                f"Line {line}: a table row has a different number of cells "
                "than the header."
            )
        case ParseErrorKind.BAD_COLS_DIRECTIVE:
            return (
                f"Line {line}: the `[cols=…]` directive is malformed. Each "
                "value must be a positive integer."
            )
        case ParseErrorKind.UNTERMINATED_ADMONITION:
            return (
                f"Line {line}: an admonition block was opened but never "
                "closed with `====`."
            )
        case ParseErrorKind.UNKNOWN_ADMONITION_TYPE:
            return (
                f"Line {line}: unknown admonition kind — expected NOTE, "
                "TIP, IMPORTANT, WARNING, or CAUTION."
            )
        case ParseErrorKind.UNTERMINATED_BLOCKQUOTE:
            return (
                f"Line {line}: a blockquote was opened but never closed "
                "with `____`."
            )
        case ParseErrorKind.BAD_BLOCKQUOTE_DIRECTIVE:
            return (
                f"Line {line}: the `[quote, …]` directive is malformed. "
                "Expected up to two non-empty fields after `quote`."
            )
        case ParseErrorKind.UNSUPPORTED_LINK_SCHEME:
            return (
                f"Line {line}: this note uses a link scheme that isn't "
                f"supported (only {_ALLOWED_SCHEMES_LIST})."
            )
        case ParseErrorKind.BAD_LINK_MACRO:
            return (
                f"Line {line}: the `link:` macro is malformed. Expected "
                "`link:URL[display text]`."
            )
        case ParseErrorKind.UNTERMINATED_MONOSPACE:
            return (
                f"Line {line}: a backtick-monospace span was opened but "
                "never closed."
            )
        case ParseErrorKind.UNTERMINATED_PASSTHROUGH:
            return (
                f"Line {line}: a `++…++` passthrough was opened but never "
                "closed."
            )
        case ParseErrorKind.BAD_ATTRIBUTE_ENTRY:
            return (
                f"Line {line}: malformed attribute entry. The name must "
                "start with a letter and contain only letters, digits, "
                "underscores, or hyphens."
            )
        case ParseErrorKind.BLOCK_INSIDE_INLINE_ONLY_CONTAINER:
            return (
                f"Line {line}: this container only accepts paragraphs — "
                "block-level constructs (headings, lists, code blocks, "
                "tables, admonitions, blockquotes) cannot appear inside it."
            )
        case ParseErrorKind.BAD_TAG_VALUE:
            return (
                f"Line {line}: the `:tags:` line has an invalid tag value. "
                "Tags use lowercase letters, digits, and hyphens, and must "
                "start with a letter or digit."
            )
        case ParseErrorKind.DUPLICATE_TAG_ATTRIBUTE:
            return (
                f"Line {line}: this note has more than one `:tags:` line — "
                "combine them into a single comma-separated list."
            )
        case ParseErrorKind.LIST_STARTS_BELOW_TOP_LEVEL:
            return (
                f"Line {line}: a list must start at the top level — begin "
                "with a single `*` or `.` before nesting deeper."
            )
        case ParseErrorKind.LIST_NESTING_SKIPS_LEVEL:
            return (
                f"Line {line}: this list item nests too fast — add only one "
                "more `*` or `.` than the item above it."
            )
        case ParseErrorKind.LIST_NESTING_TOO_DEEP:
            return (
                f"Line {line}: lists can nest at most three levels deep."
            )
        case ParseErrorKind.BAD_ATTACHMENT_MACRO:
            return (
                f"Line {line}: the attachment macro is malformed. Expected "
                "`attachment:filename[label]`, where the filename names an "
                "attachment of this note (no spaces, no path)."
            )
        case ParseErrorKind.BAD_ATTACHMENT_TABLE_MACRO:
            return (
                f"Line {line}: the attachments-table macro is malformed. "
                "Expected `attachments::[]`, optionally with "
                "`[cols=\"name,size\"]`."
            )
        case ParseErrorKind.UNKNOWN_ATTACHMENT_TABLE_COLUMN:
            return (
                f"Line {line}: unknown column in `attachments::[cols=…]`. "
                f"The columns are: {_ATTACHMENT_TABLE_COLUMNS_LIST}."
            )
