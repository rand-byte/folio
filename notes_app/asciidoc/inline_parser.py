"""Inline-element parser for the AsciiDoc subset.

Principles & invariants
-----------------------
* Pure, deterministic, no I/O. Operates on a single string and a line
  number; produces a tuple of :data:`InlineNode` instances or raises
  :class:`ParseError`.
* Strict: every formatting marker must be paired. An unmatched opener
  raises :class:`ParseErrorKind.BAD_INLINE_SPAN` — the inline parser
  never silently treats an unbalanced ``*`` or ``_`` as literal text.
  This is a core promise of the subset: malformed inline syntax always
  surfaces an error rather than producing a corrupted render.
* Step 4 markers are: ``*bold*``, ``_italic_``,
  ``[.line-through]#strikethrough#``, ``[.underline]#underline#``. Other
  characters that future steps will treat as markers (backtick for
  monospace, ``[…]`` for links) are *literal text* in step 4. They can
  be added without changing the structure of this module — the
  ``_OPEN_SPANS`` table is the single seam.
* Marker matching is **non-greedy** and **recursive**. The first
  candidate close marker at the current nesting level closes the open
  span. Same-marker self-nesting for ``*`` and ``_`` is impossible by
  construction (the inner ``*`` always closes the outer ``*``);
  different-marker nesting is allowed.
* There is no escape mechanism. Users cannot place a literal ``*`` or
  ``_`` in inline text. This is a documented limitation of the subset,
  not an oversight.
* The scanner reports errors with ``column == 0``. Column tracking
  inside inline content adds complexity that the editor's gutter
  doesn't currently consume — the line number is enough to position
  the error indicator. If column reporting becomes useful later, the
  scanner already records ``self.pos`` at the open of every span and
  this is the only place that would need updating.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from notes_app.asciidoc.ast import (
    Bold,
    InlineNode,
    Italic,
    Strikethrough,
    Text,
    Underline,
)
from notes_app.enums import ParseErrorKind
from notes_app.models.parse_error import ParseError


# ---------------------------------------------------------------------------
# Marker constants
# ---------------------------------------------------------------------------

_BOLD_MARKER: str = "*"
_ITALIC_MARKER: str = "_"
_LINE_THROUGH_OPEN: str = "[.line-through]#"
_UNDERLINE_OPEN: str = "[.underline]#"
_HASH_CLOSE: str = "#"


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
    """One row of the dispatch table: how to recognise and build a span."""

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
    lexer already does this). ``line`` is the 1-indexed source line that
    will be attached to every produced node.

    Raises
    ------
    ParseError
        With kind :class:`ParseErrorKind.BAD_INLINE_SPAN` when an open
        marker has no matching close in the rest of the line.
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
    """

    text: str
    line: int
    pos: int

    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.line = line
        self.pos = 0

    def parse_top_level(self) -> list[InlineNode]:
        """Parse to end of input with no closing marker."""
        nodes = self._parse_until(close_marker=None)
        return nodes

    def _parse_until(self, close_marker: str | None) -> list[InlineNode]:
        """Parse inline content until ``close_marker`` (or end of input).

        On return ``self.pos`` points one past the close marker (when one
        was supplied) or at the end of the input (when ``close_marker``
        is ``None``). Raises :class:`ParseError` on an unmatched opener.
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
            if close_marker is not None and self._matches_at_pos(close_marker):
                flush()
                self.pos += len(close_marker)
                return nodes

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
            raise ParseError(
                line=self.line,
                column=0,
                message=(
                    f"unterminated inline span: expected closing "
                    f"{close_marker!r}"
                ),
                kind=ParseErrorKind.BAD_INLINE_SPAN,
            )
        flush()
        return nodes

    def _matches_at_pos(self, marker: str) -> bool:
        """``True`` iff ``self.text`` has ``marker`` at the cursor."""
        return self.text.startswith(marker, self.pos)

    def _find_opener_at_pos(self) -> _SpanOpener | None:
        """Return the (longest) opener that matches at the cursor, if any."""
        for opener in _OPEN_SPANS:
            if self._matches_at_pos(opener.open_marker):
                return opener
        return None
