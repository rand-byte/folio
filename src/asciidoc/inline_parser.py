"""Inline-element parser for the AsciiDoc subset.

Principles & invariants
-----------------------
* Pure, deterministic, no I/O. Operates on a single string and a line
  number; produces a tuple of :data:`InlineNode` instances or raises
  :class:`ParseError`.
* **Total over formatting markers.** A ``*``, ``_``, ``#`` or backtick
  that does not resolve to a span is ordinary text — never an error.
  AsciiDoc is total: anything it does not recognise as markup is prose,
  so reporting a "malformed" line would assert a property of the
  document the language says is false. The parser's remaining errors
  are all *refusals of a construct the user reached for* — a ``link:``
  or ``attachment:`` macro, a ``++…++`` passthrough, the nesting cap —
  never a claim about prose.
* Each marker has a :class:`enums.MarkerForm`, and that is what decides
  where it may open and close:

  - **Constrained** (``*``, ``_``, backtick, ``[.role]#…#``): the
    opener must not be preceded by a word character (alphanumeric or
    underscore) nor by ``;`` or ``:``, and must not be followed by a
    space. The closer must not be preceded by a space and
    must not be followed by a word character. Both tests read only the
    two characters adjacent to the marker.
  - **Unconstrained** (``**``, ``__``, double-backtick): opens and
    closes anywhere. This is AsciiDoc's escape hatch for emphasising
    part of a word, and the reason ``a**b**c`` works while ``a*b*c``
    is literal.

  A candidate *closer* that fails its test is body text and the scan
  continues, which is what makes ``*a*b*c*`` one span over ``a*b*c``.
  It is never re-offered to the opener dispatch, so same-marker
  self-nesting remains impossible by construction.
* **An opener with no valid closer backtracks to literal text.**
  :meth:`_Scanner._parse_until` reports whether it found its terminator
  rather than raising, and the opener site restores the cursor and emits
  the marker as text. The guard is per-opener, so a failed inner span
  cannot take an outer one down with it.
* **The closer index is an invariant, not an optimisation.** Because
  both boundary tests are local, the highest position at which a marker
  *could* close is a property of the line, computable once. An opener at
  or after that position cannot possibly close, so it is never
  attempted. This is a necessary condition — it can only skip attempts
  that were going to fail, and it never changes the output — and it is
  what keeps backtracking linear instead of quadratic on a
  marker-dense line. Do not "simplify" it away.
* The recognised inline set is:

  - ``*bold*``, ``_italic_``, ``**bold**``, ``__italic__``,
    ``[.line-through]#strikethrough#``, ``[.underline]#underline#`` —
    matched-pair spans whose body is recursively re-parsed.
  - `````monospace````` and ```````monospace``````` — matched-pair
    spans whose body is **literal**; nothing inside is re-parsed. This
    is what makes it safe to wrap a snippet of source containing ``*``
    or ``_``.
  - Bare URLs (``https://x``, ``http://x``) — auto-linked when the
    scheme is in :class:`LinkScheme`. The URL is recognised only at a
    *word boundary*: the immediately preceding character in the source
    line must be non-alphanumeric, or the URL must be at the start of
    the input. This prevents the ``y`` in ``myhttps://x`` from being
    absorbed. A scheme with nothing after it is not a URL —
    ``https://`` is text.

    A bare URL ends at whitespace, at ``[`` or ``]``, at a doubled
    marker that **pairs** later on the line, or at the position where
    the enclosing span's marker validly closes. Nothing else ends it:
    ``*``, ``_``, single backtick and ``#`` all belong to the target,
    which is what makes ``https://x/a_b_c`` link whole. Trailing
    sentence punctuation (:data:`_URL_TRAILING_PUNCTUATION`) is peeled
    off the target and re-emitted as text, so a URL ending a sentence
    does not swallow the full stop. The peel does not apply to the
    labelled form, where the bracket already marks the end.
  - Bare email addresses (``a@b.com``) — auto-linked to a ``mailto:``
    target whose display text is the address itself. The accepted shape
    is the language's own: a dotted domain whose final label is two to
    five ASCII letters. Addresses outside it use the macro form.
    Recognition is suppressed after ``:`` or ``/`` (so a bare
    ``mailto:`` prefix and an address inside a URL path stay inert) and
    before an SSH remote's ``:`` (``git@github.com:org/repo.git``).
    Unlike the reference, a match never retreats to a shorter address:
    ``a@x.co.museum`` is text rather than a link to ``a@x.co``.
  - URL-with-text ``https://x[display]`` — same boundary rule; the
    display text is parsed recursively but with bare-URL and
    ``link:`` detection disabled (links cannot contain other links).
  - The ``link:`` macro ``link:URL[display]`` — the URL part may
    carry any syntactically-valid scheme; only schemes in
    :class:`LinkScheme` are accepted, others raise
    :class:`ParseErrorKind.UNSUPPORTED_LINK_SCHEME`. The macro must
    have a non-empty display text and a closing ``]`` on the same
    line, otherwise :class:`ParseErrorKind.BAD_LINK_MACRO` fires.
    The URL may be wrapped in a ``++…++`` passthrough — inside the
    passthrough every character is literal, including inline
    markers that would otherwise terminate a bare URL. After the
    closing ``++`` the URL is validated against :class:`LinkScheme`
    exactly as in the unwrapped form. An unmatched closing ``++``
    raises :class:`ParseErrorKind.UNTERMINATED_PASSTHROUGH`.
  - The ``attachment:`` macro ``attachment:FILE[label]`` — a *save*
    link naming an attachment of the current note by filename. Same
    word-boundary and commit-once-matched rules as ``link:``; every
    malformed remainder (no ``[``, no closing ``]``, a nested ``[``,
    an empty target, or a target carrying whitespace or a path
    separator) raises
    :class:`ParseErrorKind.BAD_ATTACHMENT_MACRO`. The label is
    optional — ``attachment:f[]`` displays the filename — and, like a
    link's, may carry other inline formatting.

* **Activatable things do not nest.** The ``forbid_link`` flag that
  enforced "links cannot contain links" now covers the attachment
  macro too, in both directions: neither a link nor an attachment
  macro may appear inside the display text of either.
* Marker matching is **non-greedy** and **recursive** for the spans
  whose body is re-parsed (``*``, ``_``, ``[.line-through]#…#``,
  ``[.underline]#…#``, link display text). Same-marker self-nesting is
  impossible by construction; different-marker nesting is allowed, and
  needs a non-word character between the two openers because that is
  what the constrained opener test demands (``*_x_*`` nests, ``*_*x*_*``
  does not). Monospace does not recurse — its body is consumed
  verbatim.
* Nesting is **bounded** by
  :data:`config.defaults.MAX_INLINE_DEPTH`. Recursion costs one Python
  frame per level, so an unbounded depth would let one pathological
  line exhaust the interpreter stack and raise ``RecursionError`` —
  outside the :class:`ParseError` contract. Going deeper is therefore
  an ordinary parse error
  (:class:`ParseErrorKind.INLINE_NESTING_TOO_DEEP`), as over-deep lists
  are in the block parser. Only *enclosing* spans count; siblings on
  one line do not accumulate.
* Mid-word emphasis is expressed with the doubled marker, which is
  AsciiDoc's own mechanism; there is still no backslash escape (see
  ``plan-inline-escaping.md``). A URL that must contain whitespace, a
  bracket, or a paired doubled marker is written with the
  ``link:++…++`` passthrough, which is what that construct is for.
* The scanner reports errors with ``column == 0``. Column tracking
  inside inline content adds complexity that the editor's gutter
  doesn't currently consume — the line number is enough to position
  the error indicator.
"""

# The module's size reflects the inline grammar's full surface area — the
# span dispatch table, the URL/link/attachment macro scanners, and the
# nesting guard all live here because they share the one `_Scanner` cursor
# and its private helpers. Splitting purely to satisfy the line counter
# would cut through that shared state. Same rationale as parser.py.
# pylint: disable=too-many-lines

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import assert_never

from asciidoc.ast import (
    AttachmentLink,
    Bold,
    InlineNode,
    Italic,
    Link,
    Monospace,
    Strikethrough,
    Text,
    Underline,
)
from config.defaults import MAX_INLINE_DEPTH
from enums import LinkScheme, MarkerForm, ParseErrorKind
from models.parse_error import ParseError


# ---------------------------------------------------------------------------
# Marker constants
# ---------------------------------------------------------------------------

_BOLD_MARKER: str = "*"
_ITALIC_MARKER: str = "_"
_LINE_THROUGH_OPEN: str = "[.line-through]#"
_UNDERLINE_OPEN: str = "[.underline]#"
_HASH_CLOSE: str = "#"
_MONOSPACE_MARKER: str = "`"

# The unconstrained (doubled) forms. AsciiDoc gives every constrained
# marker a doubled twin that may open and close mid-word; it is the
# language's own escape hatch, and the only way to write ``a**b**c``
# now that ``a*b*c`` is literal. There is deliberately no ``##`` twin:
# folio's ``#`` exists only inside ``[.role]#…#``, and bare
# ``##highlight##`` stays literal (a documented non-member of the
# subset).
_BOLD_UNCONSTRAINED_MARKER: str = "**"
_ITALIC_UNCONSTRAINED_MARKER: str = "__"
_MONOSPACE_UNCONSTRAINED_MARKER: str = "``"

# Characters that block a *constrained* opener even though they are not
# word characters, mirroring the reference implementation's ``[^\w;:]``
# guard: it keeps emphasis from firing after a macro prefix or an HTML
# entity (``link:*x*``, ``&amp;*x*``). Confirmed against Asciidoctor
# 4.0.7: ``a;*bold*`` and ``a:*bold*`` are literal while ``a,*bold*``
# and ``a.*bold*`` format. The doubled forms are exempt.
_OPENER_BLOCKING_PUNCTUATION: frozenset[str] = frozenset({";", ":"})
_LINK_MACRO_PREFIX: str = "link:"

# The inline attachment macro: ``attachment:FILE[label]``. A *single*
# colon — the double-colon ``attachments::[]`` block macro is a whole
# line and never reaches the inline parser (the lexer claims it), so
# the two cannot be confused. Recognised at a word boundary like
# ``link:``, and committed once the prefix matches: a malformed
# remainder raises rather than degrading to prose.
_ATTACHMENT_MACRO_PREFIX: str = "attachment:"

# Characters an attachment target may never contain. The target names
# an ``Attachment.filename`` of the current note — not a path — so a
# separator is rejected at parse time (defence in depth: the save
# dialog is pre-filled with ``Path(...).name`` as well). Whitespace is
# rejected because a target is a single filename token.
_ATTACHMENT_TARGET_SEPARATORS: tuple[str, ...] = ("/", "\\")
_DISPLAY_TEXT_OPEN: str = "["
_DISPLAY_TEXT_CLOSE: str = "]"

# Inline passthrough delimiter. Inside a ``link:`` macro, ``link:++…++[t]``
# wraps a URL whose body contains characters that would otherwise trip
# the inline scanner (``*``, ``_``, ``#``, ``[``, …) or whose scheme
# is not a member of :class:`LinkScheme`. Inside the passthrough, every
# character is literal — the scanner does not interpret inline markers
# and does not require the URL to begin with a recognised scheme. After
# the closing ``++`` the URL is unwrapped and validated against
# :class:`LinkScheme` like any other ``link:`` URL.
_PASSTHROUGH_MARKER: str = "++"

# Characters that always end a URL scan, regardless of context:
# whitespace and the two brackets. ``[`` opens a display text and ``]``
# closes one, so neither can belong to the target.
#
# The inline markers ``*``, ``_`` and backtick are deliberately **not**
# here. The reference keeps them inside a URL — ``https://x/a_b_c`` and
# ``https://x/a*b*c`` link whole — because its constrained boundary
# rules stop those markers opening a span mid-word in the first place.
# A marker only ends a URL when it is the marker that will actually
# close an enclosing span (handled by ``active_close``, see
# :meth:`_Scanner._url_extent`) or when it is a doubled form that pairs
# later on the line (:data:`_URL_STOP_SEQUENCES`).
#
# ``#`` is deliberately **not** in this set either, because it is a
# valid URL fragment delimiter (``https://x#section``). The ``#``
# character acts as a close marker only inside ``[.line-through]#…#``
# and ``[.underline]#…#`` spans, where ``active_close`` picks it up.
_URL_STOP_CHARACTERS: frozenset[str] = frozenset(
    {" ", "\t", _DISPLAY_TEXT_OPEN, _DISPLAY_TEXT_CLOSE}
)

# Doubled markers end a URL scan, but **only when they pair** later on
# the line. An unconstrained marker may open mid-word, so a matched
# ``**…**`` inside a URL becomes emphasis in the reference and truncates
# the target; a lone ``__`` with no twin is an ordinary URL character.
# ``https://x/a**b**c`` therefore splits while ``https://x/a__b`` does
# not.
_URL_STOP_SEQUENCES: tuple[str, ...] = (
    _BOLD_UNCONSTRAINED_MARKER,
    _ITALIC_UNCONSTRAINED_MARKER,
    _MONOSPACE_UNCONSTRAINED_MARKER,
)

# Sentence punctuation peeled off the end of a bare URL and re-emitted
# as text, repeatedly, until the target's last character is not one of
# them: ``https://x.com.`` links ``https://x.com`` and leaves the full
# stop behind. The peel is **not** parenthesis-balance-aware — the
# reference drops a trailing ``)`` from ``https://x.com/(a)`` too — and
# it does not apply to the labelled form (``https://x.com.[l]`` targets
# the dot), where the bracket already marks the end.
_URL_TRAILING_PUNCTUATION: frozenset[str] = frozenset(".,;:?!)")

# Pattern for a generic RFC-3986-style URL scheme. Used by the
# ``link:`` macro to extract whatever scheme the user wrote so it
# can be validated against :class:`LinkScheme` (and rejected with
# ``UNSUPPORTED_LINK_SCHEME`` if not in the allow-list).
_GENERIC_SCHEME_RE: re.Pattern[str] = re.compile(
    r"([A-Za-z][A-Za-z0-9+.\-]*):"
)

# A bare email address, autolinked to a ``mailto:`` target. The shape is
# the AsciiDoc language's own: a dotted domain whose final label is two
# to five ASCII letters, with ``.``, ``-`` and ``+`` the only permitted
# symbols. Addresses outside that shape are written with the macro form
# (``mailto:ada@example.museum[label]``), which is what the language
# prescribes for them.
#
# The trailing lookaheads are the one deliberate narrowing. The
# reference ends the pattern with ``\b``, which lets it retreat to an
# *earlier* label when the real final label is too long — turning
# ``a@x.co.museum`` into a live link to ``a@x.co`` and ``a@b.co-op``
# into one to ``a@b.co``, neither of them an address the user typed.
# Forbidding a domain continuation makes the match all-or-nothing: the
# address is recognised whole or the text stays prose. Every divergence
# that produces is folio *declining* where the reference links, which is
# a refusal the subset property permits — the rule can only be narrower
# than the reference's, never wider. A trailing ``.`` that ends a
# sentence is not a continuation, so ``a@b.com.`` still links.
_EMAIL_RE: re.Pattern[str] = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.%+-]*"
    r"@[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z]{2,5}"
    r"(?![A-Za-z0-9_])(?![.-][A-Za-z0-9])"
)

# Characters that suppress email recognition when they immediately
# precede the address. ``:`` keeps a bare ``mailto:a@b.com`` inert (the
# language activates the prefix only in its macro form) and ``/`` keeps
# an address inside a URL path from being re-recognised.
_EMAIL_BLOCKING_PREDECESSORS: frozenset[str] = frozenset(":/")

# ``git@github.com:org/repo.git`` is an SSH remote, not an address. The
# reference links it; folio declines, because a developer's notes carry
# more remotes than exotic addresses and the result would be a live
# wrong target. A colon followed by a space is ordinary punctuation
# (``write to a@b.com: see below``) and still links.
_SSH_REMOTE_SEPARATOR: str = ":"

# The scheme prefix a recognised address is given as its target. The
# display text stays the bare address, matching the reference.
_MAILTO_PREFIX: str = "mailto:"


def _is_word_character(char: str) -> bool:
    """Is ``char`` a word character for the constrained-marker rules?

    Word-shaped means alphanumeric **or** underscore, matching the
    reference implementation's regex classes. This is deliberately a
    *different* predicate from :meth:`_Scanner._at_word_boundary`, which
    governs URL and macro recognition and counts an underscore as a
    boundary; the two rules come from different parts of the language
    and are not being unified here.
    """
    return char.isalnum() or char == "_"


# Factory aliasing — kept named so the dispatch table reads cleanly. Each
# factory takes the parsed children and the source line and returns the
# matching inline node.
type _SpanFactory = Callable[[tuple[InlineNode, ...], int], InlineNode]


def _make_bold(children: tuple[InlineNode, ...], line: int) -> InlineNode:
    return Bold(children=children, source_line=line)


def _make_italic(children: tuple[InlineNode, ...], line: int) -> InlineNode:
    return Italic(children=children, source_line=line)


def _make_strikethrough(children: tuple[InlineNode, ...], line: int) -> InlineNode:
    return Strikethrough(children=children, source_line=line)


def _make_underline(children: tuple[InlineNode, ...], line: int) -> InlineNode:
    return Underline(children=children, source_line=line)


@dataclass(frozen=True)
class _SpanOpener:
    """One row of the dispatch table: how to recognise and build a span.

    The spans on this table all have a recursively-re-parsed body —
    which is why they can share one row shape. Monospace, URLs, and
    ``link:`` macros do not recursively re-parse their body so they
    are handled by dedicated branches in :class:`_Scanner` rather
    than by this table.

    ``form`` drives both boundary tests, so adding a row is a matter of
    naming its form rather than of writing new position arithmetic.
    """

    open_marker: str
    close_marker: str
    factory: _SpanFactory
    form: MarkerForm


@dataclass(frozen=True)
class _CloseMarker:
    """The terminator a recursive parse is looking for, and its form.

    Bundled into one value because the two are never useful apart: the
    marker says *what* ends the parse and the form says *where* it is
    allowed to. ``None`` in place of a :class:`_CloseMarker` means "parse
    to end of input", which only the top level does.
    """

    marker: str
    form: MarkerForm


@dataclass(frozen=True)
class _ScanResult:
    """What :meth:`_Scanner._parse_until` produced, and whether it closed.

    ``closed`` is :data:`False` only when a terminator was supplied and
    the input ended before it appeared. Reporting that rather than
    raising is what lets a span opener backtrack to literal text while a
    bracket terminator turns the same condition into the macro error its
    caller owns — one scan, two policies, no exception in between.
    """

    nodes: tuple[InlineNode, ...]
    closed: bool


@dataclass(frozen=True)
class _BareUrlPrefix:
    """A literal scheme prefix that may start a link in running text.

    ``requires_label`` says whether the prefix activates on its own.
    ``https://`` and ``http://`` do; ``mailto:`` does **not** — the
    language activates it only in its macro form, so ``mailto:a@b.com``
    is prose and ``mailto:a@b.com[Mail]`` is a link. A bare address is
    recognised by :data:`_EMAIL_RE` instead, not through this table.

    A bool rather than an enum: it is a two-valued predicate read at one
    site, and a scheme that never activates is expressed by absence from
    the table rather than by a third state. If a third activation mode
    ever appears, or a second call site starts branching on this, it
    should become a named :class:`enum.Enum` in :mod:`enums`.
    """

    prefix: str
    scheme: LinkScheme
    requires_label: bool


# Bare URL is recognised only when the source has one of these literal
# prefixes at a word boundary. They map onto :class:`LinkScheme` members
# so the parser never produces a :class:`Link` with an unsupported
# scheme.
_BARE_URL_PREFIXES: tuple[_BareUrlPrefix, ...] = (
    _BareUrlPrefix("https://", LinkScheme.HTTPS, requires_label=False),
    _BareUrlPrefix("http://", LinkScheme.HTTP, requires_label=False),
    _BareUrlPrefix(_MAILTO_PREFIX, LinkScheme.MAILTO, requires_label=True),
)


# Order matters: longer markers must be tried before shorter ones that
# share a prefix. ``[.line-through]#`` and ``[.underline]#`` both start
# with ``[``, and the doubled forms must be tried before their single
# twins or ``**bold**`` would open a constrained span on the first
# asterisk and close it immediately on the second.
_OPEN_SPANS: tuple[_SpanOpener, ...] = (
    _SpanOpener(
        _LINE_THROUGH_OPEN, _HASH_CLOSE, _make_strikethrough,
        MarkerForm.CONSTRAINED,
    ),
    _SpanOpener(
        _UNDERLINE_OPEN, _HASH_CLOSE, _make_underline,
        MarkerForm.CONSTRAINED,
    ),
    _SpanOpener(
        _BOLD_UNCONSTRAINED_MARKER, _BOLD_UNCONSTRAINED_MARKER, _make_bold,
        MarkerForm.UNCONSTRAINED,
    ),
    _SpanOpener(
        _ITALIC_UNCONSTRAINED_MARKER, _ITALIC_UNCONSTRAINED_MARKER,
        _make_italic, MarkerForm.UNCONSTRAINED,
    ),
    _SpanOpener(
        _BOLD_MARKER, _BOLD_MARKER, _make_bold, MarkerForm.CONSTRAINED,
    ),
    _SpanOpener(
        _ITALIC_MARKER, _ITALIC_MARKER, _make_italic, MarkerForm.CONSTRAINED,
    ),
)


# The one delimited terminator in the grammar: the ``]`` that ends a
# display text or an attachment label. It is a bracket, not a formatting
# marker, so it closes wherever it appears — which is exactly what
# :data:`MarkerForm.DELIMITED` says.
_BRACKET_CLOSE: _CloseMarker = _CloseMarker(
    marker=_DISPLAY_TEXT_CLOSE, form=MarkerForm.DELIMITED,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_inline(text: str, line: int) -> tuple[InlineNode, ...]:
    """Parse a single line's worth of inline content into AST nodes.

    ``text`` is the source line with its newline already stripped (the
    lexer already does this). ``line`` is the 1-indexed source line
    that will be attached to every produced node.

    Raises
    ------
    ParseError
        With one of the inline-related :class:`ParseErrorKind` values:
        :data:`UNSUPPORTED_LINK_SCHEME`, :data:`BAD_LINK_MACRO`,
        :data:`BAD_ATTACHMENT_MACRO`, :data:`UNTERMINATED_PASSTHROUGH`,
        or :data:`INLINE_NESTING_TOO_DEEP` — all of them refusals of a
        construct the source reached for. An unpaired formatting marker
        is **not** among them: it is returned as :class:`Text`.
    """
    scanner = _Scanner(text, line)
    return scanner.parse_top_level()


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class _Scanner:
    """Position-tracking inline scanner.

    The scanner maintains a single ``pos`` cursor and a recursive
    ``_parse_until`` method. Each open-span call recurses with the
    closing marker as the terminator; the recursion returns when that
    marker is encountered or when the end of the input is reached
    (which is a parse error if a terminator was set).

    The ``forbid_link`` argument to :meth:`_parse_until` is what
    enforces "activatable things cannot nest" — when set, a bare URL
    or ``link:`` macro found inside the body raises
    :class:`ParseErrorKind.BAD_LINK_MACRO`, and an ``attachment:``
    macro raises :class:`ParseErrorKind.BAD_ATTACHMENT_MACRO`. Other
    inline formatting is still accepted, so the display text of a link
    (or of an attachment macro, which sets the same flag) may still
    contain bold, italic, monospace, etc.

    ``depth`` is the number of spans currently enclosing the cursor —
    a scanner cursor like ``pos``, maintained by :meth:`_nested_span`
    around every recursive descent rather than threaded through the six
    helper methods that sit between :meth:`_parse_until` and its nested
    call sites. Siblings do not accumulate: the level unwinds on the way
    out, so ``*a* *b*`` reaches depth 1, not 2.
    """

    text: str
    line: int
    pos: int
    depth: int
    closer_index: dict[str, int]
    email_index: dict[int, str] | None

    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.line = line
        self.pos = 0
        self.depth = 0
        self.closer_index = {}
        self.email_index = None

    # ------------------------------------------------------------------
    # Nesting guard
    # ------------------------------------------------------------------

    @contextmanager
    def _nested_span(self) -> Iterator[None]:
        """Enter one nesting level, refusing to go past the cap.

        Wraps every recursive :meth:`_parse_until` call. Raising at the
        point of descent — before the frame is pushed — is what keeps a
        pathological line from exhausting the interpreter stack and
        surfacing a ``RecursionError`` instead of a
        :class:`ParseError`.
        """
        if self.depth >= MAX_INLINE_DEPTH:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "inline formatting is nested more than "
                    f"{MAX_INLINE_DEPTH} levels deep"
                ),
                kind=ParseErrorKind.INLINE_NESTING_TOO_DEEP,
            )
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    # ------------------------------------------------------------------
    # Top-level entry
    # ------------------------------------------------------------------

    def parse_top_level(self) -> tuple[InlineNode, ...]:
        """Parse to end of input with no closing marker."""
        return self._parse_until(None, forbid_link=False).nodes

    # ------------------------------------------------------------------
    # Core recursive descent
    # ------------------------------------------------------------------

    def _parse_until(
        self,
        close: _CloseMarker | None,
        *,
        forbid_link: bool,
    ) -> _ScanResult:
        """Parse inline content until ``close`` (or end of input).

        On a closed return ``self.pos`` points one past the terminator;
        on an unclosed one it points at the end of the input and the
        caller decides what that means — a span backtracks to literal
        text, a bracket terminator raises its macro error. ``close`` is
        :data:`None` only at the top level, where end of input *is* the
        terminator and the result is always closed.

        The terminator is tested before anything else, because it is the
        only way out of a recursion level and must not be shadowed by a
        more eager match. A constrained terminator that fails its
        boundary test is body text: the scan absorbs it and keeps
        looking, and deliberately does **not** offer it to the opener
        dispatch, which would let a marker open a nested span inside its
        own.
        """
        nodes: list[InlineNode] = []
        text_buffer: list[str] = []

        def flush() -> None:
            if text_buffer:
                nodes.append(
                    Text(content="".join(text_buffer), source_line=self.line)
                )
                text_buffer.clear()

        while self.pos < len(self.text):
            if close is not None and self._matches_at_pos(close.marker):
                if self._closes_at(self.pos, close):
                    flush()
                    self.pos += len(close.marker)
                    return _ScanResult(nodes=tuple(nodes), closed=True)
                text_buffer.append(close.marker)
                self.pos += len(close.marker)
                continue

            # Monospace: matched-pair span with verbatim body. Consumed
            # before the recursive-span dispatch table because a backtick
            # would otherwise fall through to plain text.
            monospace = self._try_consume_monospace()
            if monospace is not None:
                flush()
                nodes.append(monospace)
                continue

            # Bare URL (recognised at a word boundary) — covers both
            # the ``https://x`` shape and the ``https://x[t]`` shape.
            url_link = self._try_consume_bare_url(
                forbid_link=forbid_link,
                active_close=close,
            )
            if url_link is not None:
                flush()
                nodes.append(url_link)
                continue

            # ``link:`` macro — also boundary-required. Distinct from
            # bare URL because the URL part may carry any scheme
            # (validated downstream).
            macro_link = self._try_consume_link_macro(
                forbid_link=forbid_link,
                active_close=close,
            )
            if macro_link is not None:
                flush()
                nodes.append(macro_link)
                continue

            # Bare email address — autolinked to a ``mailto:`` target.
            # Attempted after the URL and macro forms so that an address
            # inside a URL path or after a ``mailto:`` prefix is already
            # consumed (or already suppressed) by the time the index is
            # consulted.
            email_link = self._try_consume_email(forbid_link=forbid_link)
            if email_link is not None:
                flush()
                nodes.append(email_link)
                continue

            # ``attachment:FILE[label]`` — the save-link sibling of
            # ``link:``. Consumed before the recursive-span dispatch so
            # its bracketed label is never mistaken for a span opener,
            # and gated by the same ``forbid_link`` flag: activatable
            # things do not nest.
            attachment_link = self._try_consume_attachment_macro(
                forbid_link=forbid_link,
            )
            if attachment_link is not None:
                flush()
                nodes.append(attachment_link)
                continue

            span = self._try_consume_span()
            if span is not None:
                flush()
                nodes.append(span)
                continue

            text_buffer.append(self.text[self.pos])
            self.pos += 1

        flush()
        return _ScanResult(nodes=tuple(nodes), closed=close is None)

    def _try_consume_span(self) -> InlineNode | None:
        """Try to consume a dispatch-table span at the cursor.

        Returns :data:`None` when no opener matches, when the opener
        fails its boundary test, or when the span was attempted and found
        no valid closer before end of line. In every one of those cases
        the cursor is left **on the opener**, which the caller then
        consumes one character at a time as ordinary text. Progress is
        therefore guaranteed even though a rejected marker is re-examined
        one position later — and that re-examination is wanted, since
        ``**a*b*`` must leave a literal asterisk and then open a
        constrained span on the second one.

        The attempt is guarded by :meth:`_last_valid_closer`, so a span
        that provably cannot close is never entered — see the closer-index
        invariant in the module docstring.
        """
        opener = self._find_opener_at_pos()
        if opener is None:
            return None
        close = _CloseMarker(marker=opener.close_marker, form=opener.form)
        if self._last_valid_closer(close) <= self.pos:
            return None
        opener_start = self.pos
        self.pos += len(opener.open_marker)
        with self._nested_span():
            result = self._parse_until(close, forbid_link=False)
        if not result.closed:
            self.pos = opener_start
            return None
        return opener.factory(result.nodes, self.line)

    # ------------------------------------------------------------------
    # Monospace
    # ------------------------------------------------------------------

    def _try_consume_monospace(self) -> Monospace | None:
        """Try to consume a monospace span at the cursor.

        Both forms are recognised here, doubled first so that
        ```` ``x`` ```` is one unconstrained span rather than two empty
        constrained ones. The body is consumed verbatim — no nested
        markers are interpreted — which is the whole point of monospace
        inside running prose.

        Returns :data:`None`, leaving the cursor on the opener, when the
        backtick is not at a position where its form may open or when no
        valid closer exists on the line. An unterminated backtick is
        prose, not an error: ``an `unterminated span`` renders exactly as
        typed, as it does in the reference implementation.
        """
        for marker, form in (
            (_MONOSPACE_UNCONSTRAINED_MARKER, MarkerForm.UNCONSTRAINED),
            (_MONOSPACE_MARKER, MarkerForm.CONSTRAINED),
        ):
            if not self._matches_at_pos(marker):
                continue
            if not self._opens_at(self.pos, marker, form):
                return None
            close = _CloseMarker(marker=marker, form=form)
            # The index first (O(1) after the first call on this line),
            # so a line full of unclosable backticks costs one scan in
            # total rather than one per backtick.
            if self._last_valid_closer(close) <= self.pos:
                return None
            body_start = self.pos + len(marker)
            body_end = self._next_valid_closer(close, body_start)
            if body_end < 0:
                return None
            self.pos = body_end + len(marker)
            return Monospace(
                content=self.text[body_start:body_end],
                source_line=self.line,
            )
        return None

    # ------------------------------------------------------------------
    # Bare URL  (https://x, http://x, mailto:x  — with optional [text])
    # ------------------------------------------------------------------

    def _try_consume_bare_url(
        self,
        *,
        forbid_link: bool,
        active_close: _CloseMarker | None,
    ) -> Link | None:
        """Try to consume a bare-URL link starting at ``self.pos``.

        Returns ``None`` if the cursor is not at the start of a URL
        with a recognised scheme prefix, or if the prefix is not at
        a word boundary. When the URL is recognised but
        ``forbid_link`` is set, the call raises
        :class:`ParseErrorKind.BAD_LINK_MACRO` rather than producing
        a node — this is the "links cannot contain other links" rule.

        ``active_close`` is the closing marker of the enclosing span
        (if any) — passed through so the URL scan terminates at it.
        Without this, a URL inside ``[.line-through]#…#`` would gobble
        the closing ``#``.
        """
        if not self._at_word_boundary():
            return None
        for entry in _BARE_URL_PREFIXES:
            if self._matches_at_pos(entry.prefix):
                if forbid_link:
                    raise ParseError(
                        line=self.line,
                        column=0,
                        message=(
                            "nested link is not allowed inside a link's "
                            "display text"
                        ),
                        kind=ParseErrorKind.BAD_LINK_MACRO,
                    )
                return self._consume_url_link(
                    entry=entry,
                    active_close=active_close,
                )
        return None

    def _consume_url_link(
        self,
        *,
        entry: _BareUrlPrefix,
        active_close: _CloseMarker | None,
    ) -> Link | None:
        """Consume the URL chars and an optional ``[display text]`` suffix.

        On entry ``self.pos`` points at the start of the scheme prefix
        (e.g. the ``h`` in ``https://``). The URL extends as far as
        :meth:`_url_extent` allows. If it stopped on ``[`` and a
        matching ``]`` is found on the same line, the bracketed text is
        parsed as the link's display text (with bare-URL and ``link:``
        detection disabled inside). Otherwise trailing sentence
        punctuation is peeled off and the URL itself is the display
        text.

        Returns :data:`None`, with the cursor restored to the prefix,
        when the prefix does not resolve to a link after all: a
        label-requiring scheme with no label (``mailto:a@b.com``), or a
        scheme with nothing left after it (``https://``, ``https://.``).
        The caller then emits the prefix as ordinary text.
        """
        url_start = self.pos
        url_end = self._url_extent(url_start, active_close)
        if (
            url_end < len(self.text)
            and self.text[url_end] == _DISPLAY_TEXT_OPEN
        ):
            return self._consume_labelled_url(entry, url_start, url_end)
        if entry.requires_label:
            self.pos = url_start
            return None
        url_end = self._strip_trailing_punctuation(
            url_start + len(entry.prefix), url_end
        )
        url = self.text[url_start:url_end]
        if len(url) <= len(entry.prefix):
            # Nothing but the scheme survived: this is not a URL.
            self.pos = url_start
            return None
        self.pos = url_end
        return Link(
            url=url,
            scheme=entry.scheme,
            text=(Text(content=url, source_line=self.line),),
            source_line=self.line,
        )

    def _consume_labelled_url(
        self, entry: _BareUrlPrefix, url_start: int, url_end: int
    ) -> Link | None:
        """Consume a ``URL[display]`` shape whose ``[`` sits at ``url_end``.

        The target is taken **unpeeled** — ``https://x.com.[l]`` targets
        the full stop, as the reference does, because the bracket has
        already marked where the URL ends.

        A label-requiring scheme needs a well-formed bracket pair to
        activate at all, but tolerates an empty one:
        ``mailto:a@b.com[]`` links and displays the address without its
        scheme, which is what the reference shows.
        """
        url = self.text[url_start:url_end]
        self.pos = url_end
        if not entry.requires_label:
            display = self._try_consume_link_display_text()
            if display is None:
                display = (Text(content=url, source_line=self.line),)
            return Link(
                url=url,
                scheme=entry.scheme,
                text=display,
                source_line=self.line,
            )
        if _DISPLAY_TEXT_CLOSE not in self.text[self.pos + 1:]:
            self.pos = url_start
            return None
        labelled = self._consume_link_display_text(required=False)
        if not labelled:
            labelled = (
                Text(
                    content=url[len(entry.prefix):],
                    source_line=self.line,
                ),
            )
        return Link(
            url=url,
            scheme=entry.scheme,
            text=labelled,
            source_line=self.line,
        )

    def _url_extent(
        self, url_start: int, active_close: _CloseMarker | None
    ) -> int:
        """Index at which a URL beginning at ``url_start`` ends.

        Three things end it, and only three: a character in
        :data:`_URL_STOP_CHARACTERS`, a doubled marker that pairs later
        on the line, or the position at which the enclosing span's
        marker *validly closes*.

        That last one is why the scan asks
        :meth:`_next_valid_closer` rather than looking for the marker
        character. In ``*see https://x.com/a*b*`` the first asterisk
        after the URL is followed by a word character, so it cannot
        close the bold span and belongs to the target; the second one
        can, and ends it. Testing for the character alone would truncate
        at the first.
        """
        enclosing_stop = (
            -1 if active_close is None
            else self._next_valid_closer(active_close, url_start)
        )
        index = url_start
        while index < len(self.text):
            if index == enclosing_stop:
                break
            if self.text[index] in _URL_STOP_CHARACTERS:
                break
            if self._at_paired_stop_sequence(index):
                break
            index += 1
        return index

    def _at_paired_stop_sequence(self, index: int) -> bool:
        """Does a doubled marker start at ``index`` *and* pair later?

        An unconstrained marker closes anywhere, so
        :meth:`_last_valid_closer` degenerates to "last occurrence on
        the line" and is exactly the pairing test — and it is cached per
        line, so a marker-dense line costs one scan per marker in total.
        """
        for sequence in _URL_STOP_SEQUENCES:
            if not self.text.startswith(sequence, index):
                continue
            close = _CloseMarker(
                marker=sequence, form=MarkerForm.UNCONSTRAINED
            )
            if self._last_valid_closer(close) >= index + len(sequence):
                return True
        return False

    def _strip_trailing_punctuation(self, floor: int, url_end: int) -> int:
        """Peel :data:`_URL_TRAILING_PUNCTUATION` off a URL's tail.

        ``floor`` is the first index the peel may not cross — the end of
        the scheme prefix — so a URL that is nothing but punctuation
        after its scheme collapses to the prefix and is refused by the
        caller rather than becoming a link to ``https://``.
        """
        while (
            url_end > floor
            and self.text[url_end - 1] in _URL_TRAILING_PUNCTUATION
        ):
            url_end -= 1
        return url_end

    # ------------------------------------------------------------------
    # Bare email address  (a@b.com — autolinked to a mailto: target)
    # ------------------------------------------------------------------

    def _try_consume_email(self, *, forbid_link: bool) -> Link | None:
        """Try to consume a bare email address at the cursor.

        Returns :data:`None` when the cursor is not at the start of a
        recognised address, when the address is preceded by a character
        that suppresses recognition, or when it is followed by an SSH
        remote's ``:``.

        Under ``forbid_link`` the index is not consulted at all and the
        address stays text. A nested bare URL raises there because the
        source *reached for* a link; an address inside a label is prose
        that happens to be recognisable, and refusing it would assert
        the document is malformed when it is not. The reference emits no
        nested anchor there either, so text is also the conformant
        answer.
        """
        if forbid_link:
            return None
        address = self._email_starts().get(self.pos)
        if address is None:
            return None
        if (
            self.pos > 0
            and self.text[self.pos - 1] in _EMAIL_BLOCKING_PREDECESSORS
        ):
            return None
        end = self.pos + len(address)
        if self._is_ssh_remote_at(end):
            return None
        self.pos = end
        return Link(
            url=_MAILTO_PREFIX + address,
            scheme=LinkScheme.MAILTO,
            text=(Text(content=address, source_line=self.line),),
            source_line=self.line,
        )

    def _email_starts(self) -> dict[int, str]:
        """Every address on the line, keyed by its start index.

        Built once per line and cached, in the same spirit as the closer
        index: leftmost-first matching is what makes ``foo.a@b.com``
        recognised from its ``f`` rather than from some later position
        inside the local part, with no preceding-character arithmetic.
        Consulting it only at the cursor is what makes an address inside
        an already-consumed URL unreachable by construction.
        """
        if self.email_index is None:
            self.email_index = {
                match.start(): match.group()
                for match in _EMAIL_RE.finditer(self.text)
            }
        return self.email_index

    def _is_ssh_remote_at(self, end: int) -> bool:
        """Is the address ending at ``end`` really an SSH remote?

        ``git@github.com:org/repo.git`` is a remote, not an address. A
        colon followed by a space is ordinary sentence punctuation and
        does not suppress the link.
        """
        if end >= len(self.text):
            return False
        if self.text[end] != _SSH_REMOTE_SEPARATOR:
            return False
        following = end + 1
        if following >= len(self.text):
            return False
        return not self.text[following].isspace()

    # ------------------------------------------------------------------
    # link: macro  (link:URL[text])
    # ------------------------------------------------------------------

    def _try_consume_link_macro(
        self,
        *,
        forbid_link: bool,
        active_close: _CloseMarker | None,
    ) -> Link | None:
        """Try to consume a ``link:URL[text]`` macro at ``self.pos``.

        Returns ``None`` if ``self.pos`` is not at a word boundary
        followed by the literal ``link:``. Once the prefix is matched
        the rest is committed: a malformed scheme, missing display
        text, or unmatched ``]`` raises a :class:`ParseError` with
        the appropriate :class:`ParseErrorKind`.

        ``active_close`` is plumbed through to the URL-portion scan
        so that a ``link:`` macro inside a strikethrough span does
        not gobble the closing ``#`` of its enclosing span.
        """
        if not self._at_word_boundary():
            return None
        if not self._matches_at_pos(_LINK_MACRO_PREFIX):
            return None
        if forbid_link:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "nested link is not allowed inside a link's "
                    "display text"
                ),
                kind=ParseErrorKind.BAD_LINK_MACRO,
            )
        return self._consume_link_macro(active_close=active_close)

    def _consume_link_macro(
        self, *, active_close: _CloseMarker | None
    ) -> Link:
        """Consume a committed ``link:URL[text]`` macro.

        On entry ``self.pos`` points at the ``l`` of ``link:``. On
        return it points one past the closing ``]`` of the display
        text.

        Two URL shapes are accepted:

        * ``link:URL[text]`` — the URL begins with a recognised
          scheme (``http``, ``https``, ``mailto``) and runs until
          a URL terminator or the active enclosing close marker.
        * ``link:++URL++[text]`` — the URL is wrapped in ``++``
          passthrough markers. Inside the passthrough every
          character is literal, including the inline markers
          (``*``, ``_``, ``#``, backtick) that would otherwise
          terminate a bare URL. After the closing ``++`` the URL
          is validated against :class:`LinkScheme` exactly as in
          the unwrapped form. An unmatched closing ``++`` raises
          :class:`ParseErrorKind.UNTERMINATED_PASSTHROUGH`.
        """
        self.pos += len(_LINK_MACRO_PREFIX)
        if self._matches_at_pos(_PASSTHROUGH_MARKER):
            url = self._consume_link_macro_passthrough_url()
            scheme = self._validate_link_scheme(url)
        else:
            url_start = self.pos
            scheme = self._consume_link_macro_scheme(url_start)
            # Scheme has been consumed; the rest of the URL runs to the
            # ``[`` that opens the display text, under the same extent
            # rules as a bare URL — including the enclosing close
            # marker, so a macro inside a strikethrough span does not
            # gobble the closing ``#``.
            self.pos = self._url_extent(self.pos, active_close)
            url = self.text[url_start:self.pos]
        if (
            self.pos >= len(self.text)
            or self.text[self.pos] != _DISPLAY_TEXT_OPEN
        ):
            raise ParseError(
                line=self.line,
                column=0,
                message="link: macro is missing the '[display text]' part",
                kind=ParseErrorKind.BAD_LINK_MACRO,
            )
        # Consume the ``[`` and parse the display text. Use the
        # shared helper so the missing-``]`` and empty-text errors
        # share one implementation with the URL-with-text path.
        display = self._consume_link_display_text(required=True)
        return Link(
            url=url,
            scheme=scheme,
            text=display,
            source_line=self.line,
        )

    def _consume_link_macro_passthrough_url(self) -> str:
        """Consume a ``++URL++`` passthrough body and return the URL.

        On entry ``self.pos`` points at the first ``+`` of the
        opening ``++`` marker. On return it points one past the
        closing ``++``. Raises
        :class:`ParseErrorKind.UNTERMINATED_PASSTHROUGH` if the line
        ends before a closing ``++`` is found.
        """
        # Skip the opening ``++``.
        self.pos += len(_PASSTHROUGH_MARKER)
        body_start = self.pos
        # Scan for the closing ``++`` on the same line. The body is
        # taken verbatim; no character inside the passthrough has
        # syntactic meaning (this is what makes the construct safe
        # for URLs containing ``*`` / ``_`` / ``#`` / ``[``).
        while self.pos < len(self.text):
            if self._matches_at_pos(_PASSTHROUGH_MARKER):
                body = self.text[body_start:self.pos]
                self.pos += len(_PASSTHROUGH_MARKER)
                return body
            self.pos += 1
        raise ParseError(
            line=self.line,
            column=0,
            message=(
                "unterminated passthrough span: expected closing '++' "
                "before end of line"
            ),
            kind=ParseErrorKind.UNTERMINATED_PASSTHROUGH,
        )

    def _validate_link_scheme(self, url: str) -> LinkScheme:
        """Validate that ``url`` starts with an allow-listed scheme.

        Used for ``link:++URL++[text]`` after the passthrough body
        has been unwrapped. Mirrors the validation in
        :meth:`_consume_link_macro_scheme`, but takes a pre-extracted
        URL rather than scanning ``self.text`` — the cursor has
        already moved past the closing ``++``.

        Raises :class:`ParseErrorKind.UNSUPPORTED_LINK_SCHEME` for a
        scheme outside :class:`LinkScheme`, and
        :class:`ParseErrorKind.BAD_LINK_MACRO` for a URL with no
        recognisable scheme.
        """
        match = _GENERIC_SCHEME_RE.match(url)
        if match is None:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "link: macro is missing a URL with a recognised scheme"
                ),
                kind=ParseErrorKind.BAD_LINK_MACRO,
            )
        scheme_text = match.group(1).lower()
        try:
            return LinkScheme(scheme_text)
        except ValueError as exc:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    f"unsupported link scheme: {scheme_text!r}; "
                    f"only {', '.join(s.value for s in LinkScheme)} are allowed"
                ),
                kind=ParseErrorKind.UNSUPPORTED_LINK_SCHEME,
            ) from exc

    def _consume_link_macro_scheme(self, url_start: int) -> LinkScheme:
        """Match a generic scheme after ``link:`` and validate it.

        Raises :class:`ParseErrorKind.UNSUPPORTED_LINK_SCHEME` when the
        scheme is not in :class:`LinkScheme`, and
        :class:`ParseErrorKind.BAD_LINK_MACRO` when no scheme is
        present at all (e.g. ``link:hello[t]``). The strict policy
        keeps the renderer's URL-launcher safe — only the three
        allow-listed schemes ever reach :class:`Gtk.UriLauncher`.
        """
        match = _GENERIC_SCHEME_RE.match(self.text, url_start)
        if match is None:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "link: macro is missing a URL with a recognised scheme"
                ),
                kind=ParseErrorKind.BAD_LINK_MACRO,
            )
        scheme_text = match.group(1).lower()
        try:
            scheme = LinkScheme(scheme_text)
        except ValueError as exc:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    f"unsupported link scheme: {scheme_text!r}; "
                    f"only {', '.join(s.value for s in LinkScheme)} are allowed"
                ),
                kind=ParseErrorKind.UNSUPPORTED_LINK_SCHEME,
            ) from exc
        # Advance past the matched scheme + ':'.
        self.pos = match.end()
        return scheme

    # ------------------------------------------------------------------
    # attachment: macro  (attachment:FILE[label])
    # ------------------------------------------------------------------

    def _try_consume_attachment_macro(
        self,
        *,
        forbid_link: bool,
    ) -> AttachmentLink | None:
        """Try to consume an ``attachment:FILE[label]`` macro at the cursor.

        Returns :data:`None` when the cursor is not at a word boundary
        followed by the literal ``attachment:`` — the same boundary rule
        the ``link:`` macro uses, so a mid-word ``myattachment:x[y]``
        stays prose.

        Once the prefix matches, parsing is **committed**: every
        malformed remainder raises
        :class:`ParseErrorKind.BAD_ATTACHMENT_MACRO` rather than
        degrading to text, exactly as ``link:`` does. ``forbid_link``
        (set while parsing the display text of a link *or* an attachment
        macro) turns the match itself into that error: activatable
        things do not nest.
        """
        if not self._at_word_boundary():
            return None
        if not self._matches_at_pos(_ATTACHMENT_MACRO_PREFIX):
            return None
        if forbid_link:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "nested attachment link is not allowed inside a "
                    "link's display text"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_MACRO,
            )
        return self._consume_attachment_macro()

    def _consume_attachment_macro(self) -> AttachmentLink:
        """Consume a committed ``attachment:FILE[label]`` macro.

        On entry ``self.pos`` points at the ``a`` of ``attachment:``; on
        return it points one past the closing ``]``.

        The target (the text between the colon and the ``[``) is
        validated here — non-empty, no whitespace, no path separator —
        because it names an :class:`Attachment` of the current note by
        filename. Whether such an attachment *exists* is not knowable to
        the parser (which is storage-free), so an unknown filename is a
        parse success and is reported at click time instead.

        The label is parsed recursively with ``forbid_link`` set, so it
        may carry bold / italic / monospace but no nested link or
        attachment macro. An empty label (``attachment:f[]``) falls back
        to the filename as the display text, mirroring the bare-URL rule
        that keeps every activatable node's ``text`` tuple non-empty.
        """
        self.pos += len(_ATTACHMENT_MACRO_PREFIX)
        open_index = self.text.find(_DISPLAY_TEXT_OPEN, self.pos)
        if open_index < 0:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "attachment: macro is missing the '[label]' part"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_MACRO,
            )
        target = self.text[self.pos:open_index]
        self._validate_attachment_target(target)
        close_index = self.text.find(_DISPLAY_TEXT_CLOSE, open_index + 1)
        if close_index < 0:
            raise ParseError(
                line=self.line,
                column=0,
                message="attachment: macro is missing the closing ']'",
                kind=ParseErrorKind.BAD_ATTACHMENT_MACRO,
            )
        if _DISPLAY_TEXT_OPEN in self.text[open_index + 1:close_index]:
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "attachment: macro label contains a nested '[' bracket"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_MACRO,
            )
        # Move onto the opening bracket and parse the label body.
        self.pos = open_index + len(_DISPLAY_TEXT_OPEN)
        with self._nested_span():
            result = self._parse_until(_BRACKET_CLOSE, forbid_link=True)
        if not result.closed:
            raise ParseError(
                line=self.line,
                column=0,
                message="attachment: macro is missing the closing ']'",
                kind=ParseErrorKind.BAD_ATTACHMENT_MACRO,
            )
        label = result.nodes
        if not label:
            label = (Text(content=target, source_line=self.line),)
        return AttachmentLink(
            filename=target,
            text=label,
            source_line=self.line,
        )

    def _validate_attachment_target(self, target: str) -> None:
        """Reject an empty / whitespace / path-bearing attachment target.

        Raises :class:`ParseErrorKind.BAD_ATTACHMENT_MACRO` — the target
        is a bare ``Attachment.filename``, never a path.
        """
        if not target:
            raise ParseError(
                line=self.line,
                column=0,
                message="attachment: macro has no filename",
                kind=ParseErrorKind.BAD_ATTACHMENT_MACRO,
            )
        if any(char.isspace() for char in target):
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "attachment: macro filename may not contain whitespace"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_MACRO,
            )
        if any(sep in target for sep in _ATTACHMENT_TARGET_SEPARATORS):
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    "attachment: macro filename may not contain a path "
                    "separator"
                ),
                kind=ParseErrorKind.BAD_ATTACHMENT_MACRO,
            )

    # ------------------------------------------------------------------
    # Shared display-text helper
    # ------------------------------------------------------------------

    def _try_consume_link_display_text(self) -> tuple[InlineNode, ...] | None:
        """Try to consume a ``[…]`` display text on a bare-URL.

        Returns the parsed display nodes if the bracket pair is
        well-formed and non-empty. Returns ``None`` if there is no
        ``]`` anywhere later on the line — in which case the
        caller treats the lone ``[`` as plain text following the
        URL.

        This rollback path is **only** for the "no ``]`` at all on
        the line" case. Once a ``]`` is present, parsing is
        committed: nested-link rejection (raised as
        :class:`ParseErrorKind.BAD_LINK_MACRO`) and any other
        :class:`ParseError` propagate to the caller. Empty display
        text — ``https://x[]`` — is rolled back so the URL itself
        remains the display, consistent with the user's likely
        intent. The ``link:`` macro form, by contrast, *requires*
        a non-empty display text and uses
        :meth:`_consume_link_display_text` directly.
        """
        # Pre-check: is there any ']' later on the line at all? If
        # not, the lone '[' is plain text — no display text exists.
        if _DISPLAY_TEXT_CLOSE not in self.text[self.pos + 1:]:
            return None
        save_pos = self.pos
        display = self._consume_link_display_text(required=False)
        if not display:
            self.pos = save_pos
            return None
        return display

    def _consume_link_display_text(
        self,
        *,
        required: bool,
    ) -> tuple[InlineNode, ...]:
        """Consume a ``[…]`` display text and return the parsed nodes.

        On entry ``self.pos`` points at the opening ``[``. On return
        it points one past the closing ``]``.

        ``required`` is :data:`True` only for the ``link:`` macro
        path, where empty or missing display text is a hard error
        (:data:`ParseErrorKind.BAD_LINK_MACRO`). When :data:`False`
        (the bare-URL path) an empty body is signalled to the caller
        by returning an empty tuple, which the caller can choose to
        re-interpret as "no display text" (i.e. fall back to the
        URL-as-text rendering).
        """
        # Skip the opening bracket.
        self.pos += len(_DISPLAY_TEXT_OPEN)
        with self._nested_span():
            result = self._parse_until(_BRACKET_CLOSE, forbid_link=True)
        if not result.closed:
            raise ParseError(
                line=self.line,
                column=0,
                message="link macro is missing the closing ']'",
                kind=ParseErrorKind.BAD_LINK_MACRO,
            )
        children = result.nodes
        if required and not children:
            raise ParseError(
                line=self.line,
                column=0,
                message="link: macro has empty display text",
                kind=ParseErrorKind.BAD_LINK_MACRO,
            )
        return children

    # ------------------------------------------------------------------
    # Boundary detection and span lookup
    # ------------------------------------------------------------------

    def _at_word_boundary(self) -> bool:
        """Is the cursor at the start of a "word" in the source line?

        Used by URL and ``link:`` recognition to avoid mid-word
        false positives like ``myhttps://x``. A boundary exists at
        position 0 (start of input) or when the immediately preceding
        character is non-alphanumeric.
        """
        if self.pos == 0:
            return True
        return not self.text[self.pos - 1].isalnum()

    def _matches_at_pos(self, marker: str) -> bool:
        """``True`` iff ``self.text`` has ``marker`` at the cursor."""
        return self.text.startswith(marker, self.pos)

    def _opens_at(self, index: int, marker: str, form: MarkerForm) -> bool:
        """May a ``marker`` of ``form`` at ``index`` open a span?

        Constrained: not preceded by a word character, ``;`` or ``:``,
        and not followed by a space (nor by end of line — ``*`` at the
        very end opens nothing). Unconstrained and delimited markers open
        wherever they appear.
        """
        match form:
            case MarkerForm.UNCONSTRAINED | MarkerForm.DELIMITED:
                return True
            case MarkerForm.CONSTRAINED:
                return self._constrained_opens_at(index, marker)
            case _ as unreachable:
                assert_never(unreachable)

    def _constrained_opens_at(self, index: int, marker: str) -> bool:
        if index > 0:
            preceding = self.text[index - 1]
            if (
                _is_word_character(preceding)
                or preceding in _OPENER_BLOCKING_PUNCTUATION
            ):
                return False
        following = index + len(marker)
        if following >= len(self.text):
            return False
        return not self.text[following].isspace()

    def _closes_at(self, index: int, close: _CloseMarker) -> bool:
        """May the terminator at ``index`` close its span?

        Constrained: not preceded by a space and not followed by a word
        character. That second clause is what keeps ``*bold*x`` literal
        and what makes ``*a*b*c*`` a single span — the earlier candidates
        are followed by a word character and are therefore body text.
        """
        match close.form:
            case MarkerForm.UNCONSTRAINED | MarkerForm.DELIMITED:
                return True
            case MarkerForm.CONSTRAINED:
                return self._constrained_closes_at(index, close.marker)
            case _ as unreachable:
                assert_never(unreachable)

    def _constrained_closes_at(self, index: int, marker: str) -> bool:
        if index == 0 or self.text[index - 1].isspace():
            return False
        following = index + len(marker)
        if following >= len(self.text):
            return True
        return not _is_word_character(self.text[following])

    def _last_valid_closer(self, close: _CloseMarker) -> int:
        """Highest index at which ``close`` could legally close a span.

        Returns ``-1`` when the marker never closes anywhere on the line.
        Both boundary tests read only the characters adjacent to the
        marker, so this is a property of the *line* and is computed once
        per marker and cached — see the closer-index invariant in the
        module docstring. It is a necessary condition on a span closing,
        never a sufficient one: a closer it reports may still be consumed
        by a nested span, in which case the ordinary scan fails and the
        opener backtracks as usual.
        """
        cached = self.closer_index.get(close.marker)
        if cached is not None:
            return cached
        found = -1
        index = 0
        while index < len(self.text):
            if (
                self.text.startswith(close.marker, index)
                and self._closes_at(index, close)
            ):
                found = index
            index += 1
        self.closer_index[close.marker] = found
        return found

    def _next_valid_closer(self, close: _CloseMarker, start: int) -> int:
        """Lowest index at or after ``start`` where ``close`` may close.

        Returns ``-1`` when there is none. Used by the monospace scan,
        whose body is verbatim and therefore needs the *first* legal
        closer rather than a recursive parse.
        """
        index = start
        while index < len(self.text):
            if (
                self.text.startswith(close.marker, index)
                and self._closes_at(index, close)
            ):
                return index
            index += 1
        return -1

    def _find_opener_at_pos(self) -> _SpanOpener | None:
        """Return the (longest) opener that may open at the cursor.

        A row whose marker matches but whose boundary test fails is not a
        span opener at all, so the cursor's character is prose. The scan
        stops at the first *matching* row either way: a constrained ``*``
        that cannot open must not fall through to some other row that
        happens to share its text.
        """
        for opener in _OPEN_SPANS:
            if self._matches_at_pos(opener.open_marker):
                if self._opens_at(
                    self.pos, opener.open_marker, opener.form
                ):
                    return opener
                return None
        return None
