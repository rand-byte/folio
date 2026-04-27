"""Tests for :mod:`notes_app.asciidoc.lexer`.

The lexer's contract is line-based and context-free. Every test in this
module builds a small source string, calls :func:`tokenize`, and checks
the resulting tokens — including the line numbers, since the editor's
gutter relies on them being 1-indexed and exact.
"""

from __future__ import annotations

import unittest

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
from notes_app.enums import TokenKind


# ---------------------------------------------------------------------------
# Single-line classification: each token kind in isolation
# ---------------------------------------------------------------------------


class HeadingTokenizationTests(unittest.TestCase):
    """``tokenize`` classifies any number of leading ``=`` as a heading."""

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, int, str], ...] = (
            # (description, source, level, text)
            ("level 1", "= Title", 1, "Title"),
            ("level 2", "== Section", 2, "Section"),
            ("level 3", "=== Sub", 3, "Sub"),
            ("level 4", "==== Sub-sub", 4, "Sub-sub"),
            ("level 5", "===== Deep", 5, "Deep"),
            ("level 6", "====== Deeper", 6, "Deeper"),
            (
                "level 7 still tokenised — parser rejects it",
                "======= Way too deep",
                7,
                "Way too deep",
            ),
            ("text with markup is opaque to lexer", "== *bold*", 2, "*bold*"),
            (
                "trailing whitespace is stripped",
                "== Heading   \t  ",
                2,
                "Heading",
            ),
            ("empty heading text — no body", "==", 2, ""),
            ("empty heading text — only space", "= ", 1, ""),
            (
                "multiple spaces after equals are collapsed to text",
                "==   spacey",
                2,
                "spacey",
            ),
        )
        for desc, source, level, text in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                assert isinstance(token, HeadingToken)
                self.assertEqual(token.kind, TokenKind.HEADING)
                self.assertEqual(token.level, level)
                self.assertEqual(token.text, text)
                self.assertEqual(token.line, 1)

    def test_equals_followed_by_non_space_is_not_a_heading(self) -> None:
        # ``=#anchor`` and ``==foo`` are not heading markers — they fall
        # through to LineToken because the equals are not followed by
        # whitespace or end-of-line.
        cases = ("=foo", "==foo", "==###", "=*bold*")
        for source in cases:
            with self.subTest(source=source):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, LineToken)
                assert isinstance(token, LineToken)
                self.assertEqual(token.text, source)


class ListTokenizationTests(unittest.TestCase):
    """``* `` and ``. `` line prefixes are list bullets."""

    def test_table(self) -> None:
        # (description, source, expected_token_class, expected_text)
        cases: tuple[tuple[str, str, type[Token], str], ...] = (
            ("unordered with text", "* item one", ListBulletToken, "item one"),
            (
                "unordered with inline markup",
                "* an _italic_ item",
                ListBulletToken,
                "an _italic_ item",
            ),
            (
                "unordered text trailing spaces stripped",
                "* item   ",
                ListBulletToken,
                "item",
            ),
            ("ordered with text", ". first", ListNumberToken, "first"),
            (
                "ordered with markup",
                ". with *bold*",
                ListNumberToken,
                "with *bold*",
            ),
        )
        for desc, source, token_class, expected_text in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, token_class)
                self.assertEqual(token.text, expected_text)  # type: ignore[union-attr]

    def test_no_space_after_marker_is_not_a_bullet(self) -> None:
        # ``*foo`` is bold-without-close, ``.foo`` is text starting
        # with a period — both belong to inline parsing, not lists.
        for source in ("*foo", ".foo", "*", ".", "*nope*"):
            with self.subTest(source=source):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                self.assertIsInstance(tokens[0], LineToken)


class CodeFenceTokenizationTests(unittest.TestCase):
    """A line that is exactly ``----`` is a code fence."""

    def test_exact_fence(self) -> None:
        tokens = tokenize("----")
        self.assertEqual(len(tokens), 1)
        token = tokens[0]
        self.assertIsInstance(token, CodeFenceToken)
        self.assertEqual(token.kind, TokenKind.CODE_FENCE)

    def test_fence_with_trailing_whitespace(self) -> None:
        # Trailing whitespace doesn't disqualify the fence — the lexer
        # right-strips before classification.
        tokens = tokenize("----   \t")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], CodeFenceToken)

    def test_other_dash_lengths_are_lines(self) -> None:
        # Three dashes is not a fence, neither is five, neither is a
        # fence with extra characters.
        for source in ("---", "-----", "---- foo", "----foo"):
            with self.subTest(source=source):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                self.assertIsInstance(tokens[0], LineToken)


class CodeDirectiveTokenizationTests(unittest.TestCase):
    """``[source]`` and ``[source,LANG]`` produce :class:`CodeDirectiveToken`."""

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, str | None], ...] = (
            ("bare directive", "[source]", None),
            ("with python", "[source,python]", "python"),
            ("with shell", "[source,shell]", "shell"),
            (
                "language is trimmed",
                "[source,  python  ]",
                "python",
            ),
            (
                "multi-word language opaque to lexer",
                "[source,c++]",
                "c++",
            ),
        )
        for desc, source, language in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, CodeDirectiveToken)
                assert isinstance(token, CodeDirectiveToken)
                self.assertEqual(token.kind, TokenKind.CODE_DIRECTIVE)
                self.assertEqual(token.language, language)

    def test_empty_lang_is_not_a_directive(self) -> None:
        # ``[source,]`` is malformed — the lexer falls through to a
        # LineToken so the parser can raise UNKNOWN_BLOCK on the
        # ``[…]`` shape.
        tokens = tokenize("[source,]")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)


class ImageMacroTokenizationTests(unittest.TestCase):
    """``image::FOO[BAR]`` produces an :class:`ImageMacroToken`.

    The lexer keeps the macro raw — the parser is responsible for
    splitting on the ``[`` and validating bracket pairing.
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, str], ...] = (
            ("simple", "image::cat.png[]", "cat.png[]"),
            ("with attrs", "image::cat.png[alt=Cat]", "cat.png[alt=Cat]"),
            (
                "missing close — still tokenised",
                "image::cat.png[",
                "cat.png[",
            ),
            (
                "missing open — still tokenised",
                "image::cat.png",
                "cat.png",
            ),
        )
        for desc, source, raw in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, ImageMacroToken)
                assert isinstance(token, ImageMacroToken)
                self.assertEqual(token.kind, TokenKind.IMAGE_MACRO)
                self.assertEqual(token.raw, raw)


class BlankTokenizationTests(unittest.TestCase):
    """Empty and whitespace-only lines produce :class:`BlankToken`."""

    def test_table(self) -> None:
        for source in ("", " ", "   ", "\t\t"):
            with self.subTest(repr(source)):
                tokens = tokenize(source)
                # An empty source produces zero tokens (no lines), but
                # whitespace-only sources have one.
                if source == "":
                    self.assertEqual(tokens, [])
                else:
                    self.assertEqual(len(tokens), 1)
                    self.assertIsInstance(tokens[0], BlankToken)
                    self.assertEqual(tokens[0].kind, TokenKind.BLANK)


class LineTokenizationTests(unittest.TestCase):
    """Anything not matching a specialised pattern is a :class:`LineToken`."""

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, str], ...] = (
            ("plain prose", "Hello world", "Hello world"),
            ("inline-marked prose", "Some *bold* text", "Some *bold* text"),
            ("attribute entry — parser rejects", ":author: me", ":author: me"),
            ("comment — parser rejects", "// a comment", "// a comment"),
            ("table fence — parser rejects", "|===", "|==="),
            (
                "blockquote fence — parser rejects",
                "____",
                "____",
            ),
            (
                "trailing whitespace stripped",
                "text with trailing   ",
                "text with trailing",
            ),
        )
        for desc, source, expected in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, LineToken)
                assert isinstance(token, LineToken)
                self.assertEqual(token.kind, TokenKind.LINE)
                self.assertEqual(token.text, expected)


# ---------------------------------------------------------------------------
# Multi-line tokenisation: order, line numbers, and CRLF
# ---------------------------------------------------------------------------


class MultiLineTokenizationTests(unittest.TestCase):

    def test_line_numbers_are_one_indexed(self) -> None:
        source = "first\nsecond\nthird"
        tokens = tokenize(source)
        self.assertEqual([token.line for token in tokens], [1, 2, 3])

    def test_blank_lines_are_emitted(self) -> None:
        source = "alpha\n\nbeta"
        tokens = tokenize(source)
        self.assertEqual(len(tokens), 3)
        self.assertIsInstance(tokens[0], LineToken)
        self.assertIsInstance(tokens[1], BlankToken)
        self.assertIsInstance(tokens[2], LineToken)

    def test_crlf_line_endings(self) -> None:
        # ``str.splitlines`` recognises ``\r\n`` as a single line break,
        # so CRLF input produces the same tokens as LF input.
        tokens = tokenize("a\r\nb\r\n")
        self.assertEqual(len(tokens), 2)
        for token in tokens:
            self.assertIsInstance(token, LineToken)

    def test_mixed_block_kinds_in_order(self) -> None:
        source = (
            "= Title\n"
            "\n"
            "para\n"
            "\n"
            "== Section\n"
            "\n"
            "* item\n"
            ". numbered\n"
            "\n"
            "[source,python]\n"
            "----\n"
            "code\n"
            "----\n"
            "\n"
            "image::cat.png[]\n"
        )
        tokens = tokenize(source)
        kinds = [type(token).__name__ for token in tokens]
        self.assertEqual(
            kinds,
            [
                "HeadingToken",      # 1
                "BlankToken",        # 2
                "LineToken",         # 3
                "BlankToken",        # 4
                "HeadingToken",      # 5
                "BlankToken",        # 6
                "ListBulletToken",   # 7
                "ListNumberToken",   # 8
                "BlankToken",        # 9
                "CodeDirectiveToken",# 10
                "CodeFenceToken",    # 11
                "LineToken",         # 12
                "CodeFenceToken",    # 13
                "BlankToken",        # 14
                "ImageMacroToken",   # 15
            ],
        )


class SourceLinesHelperTests(unittest.TestCase):
    """``source_lines`` returns lines without trailing newlines."""

    def test_basic(self) -> None:
        self.assertEqual(
            source_lines("a\nb\nc"),
            ["a", "b", "c"],
        )

    def test_preserves_trailing_whitespace(self) -> None:
        # The parser reads code-block bodies through this helper
        # because the token's right-stripped text would lose
        # intentional trailing whitespace.
        self.assertEqual(
            source_lines("foo  \n\tbar\t"),
            ["foo  ", "\tbar\t"],
        )

    def test_empty(self) -> None:
        self.assertEqual(source_lines(""), [])


if __name__ == "__main__":
    unittest.main()
