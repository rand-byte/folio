"""Inline-element parser for the AsciiDoc subset.

Principles & invariants
-----------------------
* Pure, deterministic, no I/O. Operates on a single string and a line
  number; produces a tuple of :data:`InlineNode` instances or raises
  :class:`ParseError`.
* Strict: every formatting marker must be paired. An unmatched opener
  raises :class:`ParseErrorKind.BAD_INLINE_SPAN` — the inline parser
  never silently treats an unbalanced ``*`` or ``_`` as literal text.
  Monospace has its own dedicated error variant
  (:class:`ParseErrorKind.UNTERMINATED_MONOSPACE`) because the editor's
  gutter renders a different help message for it.
  This is a core promise of the subset: malformed inline syntax always
  surfaces an error rather than producing a corrupted render.
* Step 13 extends the recognised inline set to:

  - ``*bold*``, ``_italic_``,
    ``[.line-through]#strikethrough#``, ``[.underline]#underline#`` —
    matched-pair spans whose body is recursively re-parsed.
  - `````monospace````` — a matched-pair span whose body is
    **literal**; nothing inside is re-parsed. This is what makes it
    safe to wrap a snippet of source containing ``*`` or ``_``.
  - Bare URLs (``https://x``, ``http://x``, ``mailto:x``) — auto-linked
    when the scheme is in :class:`LinkScheme`. The URL is recognised
    only at a *word boundary*: the immediately preceding character in
    the source line must be non-alphanumeric, or the URL must be at
    the start of the input. This prevents the ``y`` in ``myhttps://x``
    from being absorbed.
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

* Marker matching is **non-greedy** and **recursive** for the spans
  whose body is re-parsed (``*``, ``_``, ``[.line-through]#…#``,
  ``[.underline]#…#``, link display text). Same-marker self-nesting
  for ``*`` and ``_`` is impossible by construction (the inner ``*``
  always closes the outer ``*``); different-marker nesting is allowed.
  Monospace does not recurse — its body is consumed verbatim.
* There is no escape mechanism. Users cannot place a literal ``*``,
  ``_``, ``#``, or backtick inside a same-marker context. This is a
  documented limitation of the subset, not an oversight. URLs cannot
  contain whitespace, ``[``, end-of-line, or any of the inline marker
  characters (``*``, ``_``, ``#``, backtick); for URLs containing
  those characters, users must encode them in the source URL.
* The scanner reports errors with ``column == 0``. Column tracking
  inside inline content adds complexity that the editor's gutter
  doesn't currently consume — the line number is enough to position
  the error indicator.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from notes_app.asciidoc.ast import (
    Bold,
    InlineNode,
    Italic,
    Link,
    Monospace,
    Strikethrough,
    Text,
    Underline,
)
from notes_app.enums import LinkScheme, ParseErrorKind
from notes_app.models.parse_error import ParseError


# ---------------------------------------------------------------------------
# Marker constants
# ---------------------------------------------------------------------------

_BOLD_MARKER: str = "*"
_ITALIC_MARKER: str = "_"
_LINE_THROUGH_OPEN: str = "[.line-through]#"
_UNDERLINE_OPEN: str = "[.underline]#"
_HASH_CLOSE: str = "#"
_MONOSPACE_MARKER: str = "`"
_LINK_MACRO_PREFIX: str = "link:"
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

# Bare URL is recognised only when the source has one of these literal
# prefixes at a word boundary. They map onto :class:`LinkScheme` members
# so the parser never produces a :class:`Link` with an unsupported
# scheme.
_BARE_URL_PREFIXES: tuple[tuple[str, LinkScheme], ...] = (
    ("https://", LinkScheme.HTTPS),
    ("http://", LinkScheme.HTTP),
    ("mailto:", LinkScheme.MAILTO),
)

# Characters that always terminate a bare-URL scan, regardless of
# context. Whitespace and ``[`` are the canonical AsciiDoc URL
# terminators. The inline markers ``*``, ``_``, and backtick are
# added so a URL that happens to butt up against an inline-style
# marker (e.g. ``*see https://x*``) is parsed as URL + close-marker
# rather than gobbling the marker into the URL.
#
# ``#`` is deliberately **not** in this set, because it is a valid
# URL fragment delimiter (``https://x#section``). The ``#`` character
# acts as a close marker only inside ``[.line-through]#…#`` and
# ``[.underline]#…#`` spans; in those contexts the URL scanner picks
# it up via the dynamic ``active_close`` parameter (see
# :meth:`_Scanner._consume_url_link`).
_URL_TERMINATORS: frozenset[str] = frozenset(
    {" ", "\t", "[", _BOLD_MARKER, _ITALIC_MARKER, _MONOSPACE_MARKER}
)

# Pattern for a generic RFC-3986-style URL scheme. Used by the
# ``link:`` macro to extract whatever scheme the user wrote so it
# can be validated against :class:`LinkScheme` (and rejected with
# ``UNSUPPORTED_LINK_SCHEME`` if not in the allow-list).
_GENERIC_SCHEME_RE: re.Pattern[str] = re.compile(
    r"([A-Za-z][A-Za-z0-9+.\-]*):"
)


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
    """

    open_marker: str
    close_marker: str
    factory: _SpanFactory


# Order matters: longer markers must be tried before shorter ones that
# share a prefix. ``[.line-through]#`` and ``[.underline]#`` both start
# with ``[`` so single-character markers come last.
_OPEN_SPANS: tuple[_SpanOpener, ...] = (
    _SpanOpener(_LINE_THROUGH_OPEN, _HASH_CLOSE, _make_strikethrough),
    _SpanOpener(_UNDERLINE_OPEN, _HASH_CLOSE, _make_underline),
    _SpanOpener(_BOLD_MARKER, _BOLD_MARKER, _make_bold),
    _SpanOpener(_ITALIC_MARKER, _ITALIC_MARKER, _make_italic),
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
        :data:`BAD_INLINE_SPAN`, :data:`UNTERMINATED_MONOSPACE`,
        :data:`UNSUPPORTED_LINK_SCHEME`, or :data:`BAD_LINK_MACRO`.
    """
    scanner = _Scanner(text, line)
    nodes = scanner.parse_top_level()
    return tuple(nodes)


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
    enforces "links cannot contain links" — when set, a bare URL or
    ``link:`` macro found inside the body raises
    :class:`ParseErrorKind.BAD_LINK_MACRO`. Other inline formatting
    is still accepted, so a link's display text may still contain
    bold, italic, monospace, etc.
    """

    text: str
    line: int
    pos: int

    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.line = line
        self.pos = 0

    # ------------------------------------------------------------------
    # Top-level entry
    # ------------------------------------------------------------------

    def parse_top_level(self) -> list[InlineNode]:
        """Parse to end of input with no closing marker."""
        return self._parse_until(close_marker=None)

    # ------------------------------------------------------------------
    # Core recursive descent
    # ------------------------------------------------------------------

    def _parse_until(
        self,
        close_marker: str | None,
        *,
        forbid_link: bool = False,
        unmatched_kind: ParseErrorKind = ParseErrorKind.BAD_INLINE_SPAN,
        unmatched_message: str | None = None,
    ) -> list[InlineNode]:
        """Parse inline content until ``close_marker`` (or end of input).

        On return ``self.pos`` points one past the close marker (when
        one was supplied) or at the end of the input (when
        ``close_marker`` is ``None``).

        Raises :class:`ParseError` with ``unmatched_kind`` (default
        :data:`BAD_INLINE_SPAN`) if the close marker is supplied but
        the input ends before it is found. ``unmatched_kind`` is what
        lets link display-text parsing report
        :data:`BAD_LINK_MACRO` instead of the generic span-error
        kind on a missing ``]``.
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
            # Close marker for the enclosing span takes priority over
            # everything else: it is the only way out of a recursion
            # level and must not be shadowed by a more eager match.
            if close_marker is not None and self._matches_at_pos(close_marker):
                flush()
                self.pos += len(close_marker)
                return nodes

            # Monospace: matched-pair span with verbatim body. Consumed
            # before the recursive-span dispatch table because ``\```
            # would otherwise fall through to plain text.
            if self._matches_at_pos(_MONOSPACE_MARKER):
                flush()
                nodes.append(self._consume_monospace())
                continue

            # Bare URL (recognised at a word boundary) — covers both
            # the ``https://x`` shape and the ``https://x[t]`` shape.
            url_link = self._try_consume_bare_url(
                forbid_link=forbid_link,
                active_close=close_marker,
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
                active_close=close_marker,
            )
            if macro_link is not None:
                flush()
                nodes.append(macro_link)
                continue

            opener = self._find_opener_at_pos()
            if opener is not None:
                flush()
                self.pos += len(opener.open_marker)
                children = self._parse_until(close_marker=opener.close_marker)
                nodes.append(opener.factory(tuple(children), self.line))
                continue

            text_buffer.append(self.text[self.pos])
            self.pos += 1

        # End of input.
        if close_marker is not None:
            message = unmatched_message or (
                f"unterminated inline span: expected closing {close_marker!r}"
            )
            raise ParseError(
                line=self.line,
                column=0,
                message=message,
                kind=unmatched_kind,
            )
        flush()
        return nodes

    # ------------------------------------------------------------------
    # Monospace
    # ------------------------------------------------------------------

    def _consume_monospace(self) -> Monospace:
        """Consume a `````…````` span starting at the current position.

        On entry ``self.pos`` points at the opening backtick. On
        return it points one past the closing backtick. The body is
        consumed verbatim — no nested markers are interpreted —
        which is the whole point of monospace inside running prose.

        Raises :class:`ParseErrorKind.UNTERMINATED_MONOSPACE` if the
        line ends before a closing backtick is found.
        """
        # Skip the opening backtick.
        self.pos += len(_MONOSPACE_MARKER)
        body_start = self.pos
        # Scan for the closing backtick on the same line.
        while self.pos < len(self.text):
            if self._matches_at_pos(_MONOSPACE_MARKER):
                content = self.text[body_start:self.pos]
                # Skip the closing backtick.
                self.pos += len(_MONOSPACE_MARKER)
                return Monospace(content=content, source_line=self.line)
            self.pos += 1
        raise ParseError(
            line=self.line,
            column=0,
            message="unterminated monospace span: expected closing '`'",
            kind=ParseErrorKind.UNTERMINATED_MONOSPACE,
        )

    # ------------------------------------------------------------------
    # Bare URL  (https://x, http://x, mailto:x  — with optional [text])
    # ------------------------------------------------------------------

    def _try_consume_bare_url(
        self,
        *,
        forbid_link: bool,
        active_close: str | None,
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
        for prefix, scheme in _BARE_URL_PREFIXES:
            if self._matches_at_pos(prefix):
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
                    scheme=scheme,
                    active_close=active_close,
                )
        return None

    def _consume_url_link(
        self,
        *,
        scheme: LinkScheme,
        active_close: str | None,
    ) -> Link:
        """Consume the URL chars and an optional ``[display text]`` suffix.

        On entry ``self.pos`` points at the start of the scheme prefix
        (e.g. the ``h`` in ``https://``). The URL extends until any
        terminator in :data:`_URL_TERMINATORS`, the active enclosing
        close marker (passed via ``active_close``), or end of line. If
        the terminator is ``[`` and a matching ``]`` is found on the
        same line, the bracketed text is parsed as the link's display
        text (with bare-URL and ``link:`` detection disabled inside).
        Otherwise the URL itself is the display text.
        """
        url_start = self.pos
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char in _URL_TERMINATORS:
                break
            if active_close is not None and char == active_close:
                break
            self.pos += 1
        url = self.text[url_start:self.pos]

        # Optional ``[text]`` suffix — only consumed if the closing
        # bracket is on the same line. If the bracket count doesn't
        # balance, the ``[`` is treated as plain text following the
        # URL: we do not raise here because, unlike ``link:``, the
        # bare URL form is valid without a display-text suffix.
        if (
            self.pos < len(self.text)
            and self.text[self.pos] == _DISPLAY_TEXT_OPEN
        ):
            display = self._try_consume_link_display_text()
            if display is not None:
                return Link(
                    url=url,
                    scheme=scheme,
                    text=display,
                    source_line=self.line,
                )

        # No display text — the URL itself is what the renderer shows.
        display_text: tuple[InlineNode, ...] = (
            Text(content=url, source_line=self.line),
        )
        return Link(
            url=url,
            scheme=scheme,
            text=display_text,
            source_line=self.line,
        )

    # ------------------------------------------------------------------
    # link: macro  (link:URL[text])
    # ------------------------------------------------------------------

    def _try_consume_link_macro(
        self,
        *,
        forbid_link: bool,
        active_close: str | None,
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

    def _consume_link_macro(self, *, active_close: str | None) -> Link:
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
            # Scheme has been consumed; continue scanning the rest of
            # the URL until the ``[`` that opens the display text —
            # also bounded by the active enclosing close marker, when
            # set.
            while self.pos < len(self.text):
                char = self.text[self.pos]
                if char in _URL_TERMINATORS:
                    break
                if active_close is not None and char == active_close:
                    break
                self.pos += 1
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
        children = self._parse_until(
            close_marker=_DISPLAY_TEXT_CLOSE,
            forbid_link=True,
            unmatched_kind=ParseErrorKind.BAD_LINK_MACRO,
            unmatched_message="link macro is missing the closing ']'",
        )
        if required and not children:
            raise ParseError(
                line=self.line,
                column=0,
                message="link: macro has empty display text",
                kind=ParseErrorKind.BAD_LINK_MACRO,
            )
        return tuple(children)

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

    def _find_opener_at_pos(self) -> _SpanOpener | None:
        """Return the (longest) opener that matches at the cursor, if any."""
        for opener in _OPEN_SPANS:
            if self._matches_at_pos(opener.open_marker):
                return opener
        return None
