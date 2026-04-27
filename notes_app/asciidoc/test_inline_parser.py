"""Tests for :mod:`notes_app.asciidoc.inline_parser`.

The inline parser is a pure ``str -> tuple[InlineNode, ...]`` function.
Every test in this module builds a small input string, calls
:func:`parse_inline`, and asserts on the produced tree.

Two recurring shapes:
* **Valid input** — the parser returns a tuple of inline nodes whose
  structure we assert against an expected tuple. Source-line numbers
  are checked because the renderer uses them for error positioning.
* **Invalid input** — the parser raises
  :class:`ParseErrorKind.BAD_INLINE_SPAN` with the offending source
  line. Column is always ``0`` for inline failures, which is what the
  editor's gutter renderer expects.
"""

from __future__ import annotations

import unittest

from notes_app.asciidoc.ast import (
    Bold,
    InlineNode,
    Italic,
    Strikethrough,
    Text,
    Underline,
)
from notes_app.asciidoc.inline_parser import parse_inline
from notes_app.enums import ParseErrorKind
from notes_app.models.parse_error import ParseError


# Convenient line constant used by every fixture — no test cares about
# the specific line, only that it is propagated correctly to every
# produced node.
_LINE: int = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _t(content: str, line: int = _LINE) -> Text:
    return Text(content=content, source_line=line)


def _bold(*children: InlineNode, line: int = _LINE) -> Bold:
    return Bold(children=tuple(children), source_line=line)


def _italic(*children: InlineNode, line: int = _LINE) -> Italic:
    return Italic(children=tuple(children), source_line=line)


def _strike(*children: InlineNode, line: int = _LINE) -> Strikethrough:
    return Strikethrough(children=tuple(children), source_line=line)


def _under(*children: InlineNode, line: int = _LINE) -> Underline:
    return Underline(children=tuple(children), source_line=line)


# ---------------------------------------------------------------------------
# Valid input
# ---------------------------------------------------------------------------


class ValidInlineTests(unittest.TestCase):
    """Every supported inline shape parses to the expected tuple."""

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, tuple[InlineNode, ...]], ...] = (
            ("empty input", "", ()),
            ("plain text", "hello world", (_t("hello world"),)),
            (
                "bold span",
                "*hello*",
                (_bold(_t("hello")),),
            ),
            (
                "italic span",
                "_hello_",
                (_italic(_t("hello")),),
            ),
            (
                "strikethrough span",
                "[.line-through]#done#",
                (_strike(_t("done")),),
            ),
            (
                "underline span",
                "[.underline]#important#",
                (_under(_t("important")),),
            ),
            (
                "leading and trailing text around bold",
                "before *middle* after",
                (
                    _t("before "),
                    _bold(_t("middle")),
                    _t(" after"),
                ),
            ),
            (
                "italic inside bold (cross-marker nesting)",
                "*foo _bar_ baz*",
                (
                    _bold(
                        _t("foo "),
                        _italic(_t("bar")),
                        _t(" baz"),
                    ),
                ),
            ),
            (
                "bold inside italic",
                "_foo *bar* baz_",
                (
                    _italic(
                        _t("foo "),
                        _bold(_t("bar")),
                        _t(" baz"),
                    ),
                ),
            ),
            (
                "strikethrough inside bold",
                "*old [.line-through]#wrong#*",
                (
                    _bold(
                        _t("old "),
                        _strike(_t("wrong")),
                    ),
                ),
            ),
            (
                "two adjacent bolds resolve as alternating",
                "*a*b*c*",
                (
                    _bold(_t("a")),
                    _t("b"),
                    _bold(_t("c")),
                ),
            ),
            (
                "empty bold span renders as Bold([])",
                "**",
                (_bold(),),
            ),
            (
                "empty italic span renders as Italic([])",
                "__",
                (_italic(),),
            ),
            (
                "empty strikethrough renders as Strikethrough([])",
                "[.line-through]##",
                (_strike(),),
            ),
            (
                "lone open-bracket is literal text",
                "see [docs] for more",
                (_t("see [docs] for more"),),
            ),
            (
                "backtick is literal text in step 4",
                "use `code` here",
                (_t("use `code` here"),),
            ),
            (
                "underscore-in-word — unmatched _ would error, "
                "so we test fully matched",
                "say _hi_ to me",
                (
                    _t("say "),
                    _italic(_t("hi")),
                    _t(" to me"),
                ),
            ),
        )
        for desc, source, expected in cases:
            with self.subTest(desc):
                actual = parse_inline(source, _LINE)
                self.assertEqual(actual, expected)


class LineNumberPropagationTests(unittest.TestCase):
    """Every produced node carries the line number passed to the parser."""

    def test_line_attached_to_every_node(self) -> None:
        line = 42
        nodes = parse_inline(
            "before *bold _italic_* after [.underline]#u#",
            line,
        )
        # Walk the tree and check every node.
        seen: list[int] = []

        def walk(items: tuple[InlineNode, ...]) -> None:
            for node in items:
                seen.append(node.source_line)
                if isinstance(node, (Bold, Italic, Strikethrough, Underline)):
                    walk(node.children)

        walk(nodes)
        self.assertTrue(seen, "expected at least one node")
        for actual in seen:
            self.assertEqual(actual, line)


# ---------------------------------------------------------------------------
# Invalid input — every unmatched-marker variant raises
# ---------------------------------------------------------------------------


class UnmatchedSpanTests(unittest.TestCase):
    """Each marker raises :class:`ParseErrorKind.BAD_INLINE_SPAN`."""

    def test_table(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("bare bold opener", "*unclosed"),
            ("bold opener with trailing space", "*nope "),
            ("bare italic opener", "_unclosed"),
            ("strikethrough opener with no close", "[.line-through]#oops"),
            ("underline opener with no close", "[.underline]#oops"),
            (
                "nested unclosed inner",
                "*outer _inner*",
            ),
            (
                "nested unclosed outer",
                "*outer _inner_ ",
            ),
            (
                "strikethrough with prefix only",
                "[.line-through]#",
            ),
        )
        for desc, source in cases:
            with self.subTest(desc):
                with self.assertRaises(ParseError) as ctx:
                    parse_inline(source, _LINE)
                self.assertEqual(
                    ctx.exception.kind,
                    ParseErrorKind.BAD_INLINE_SPAN,
                )
                self.assertEqual(ctx.exception.line, _LINE)
                # Column 0 is the documented "whole line" sentinel.
                self.assertEqual(ctx.exception.column, 0)


class MarkerPriorityTests(unittest.TestCase):
    """Longer markers are tried before shorter ones that share a prefix."""

    def test_line_through_wins_over_open_bracket(self) -> None:
        # Without longer-first matching, the leading ``[`` of
        # ``[.line-through]`` would be treated as literal text and the
        # trailing ``#…#`` would be unmatched literal text too.
        result = parse_inline("[.line-through]#x#", _LINE)
        self.assertEqual(result, (_strike(_t("x")),))

    def test_underline_wins_over_open_bracket(self) -> None:
        result = parse_inline("[.underline]#x#", _LINE)
        self.assertEqual(result, (_under(_t("x")),))


if __name__ == "__main__":
    unittest.main()
