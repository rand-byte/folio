"""Tests for :mod:`notes_app.asciidoc.lexer`.

The lexer's contract is line-based and context-free. Every test in this
module builds a small source string, calls :func:`tokenize`, and checks
the resulting tokens — including the line numbers, since the editor's
gutter relies on them being 1-indexed and exact.
"""

from __future__ import annotations

import unittest

from notes_app.asciidoc.lexer import (
    AdmonitionDirectiveToken,
    AdmonitionFenceToken,
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
from notes_app.enums import AdmonitionKind, TokenKind


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


class TableFenceTokenizationTests(unittest.TestCase):
    """``|===`` on its own line is a :class:`TableFenceToken`.

    The lexer emits the same token kind for opening and closing fences
    — they are indistinguishable at the line level. The parser pairs
    them by counting.
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("plain fence", "|==="),
            ("trailing whitespace stripped", "|===   "),
        )
        for desc, source in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, TableFenceToken)
                assert isinstance(token, TableFenceToken)
                self.assertEqual(token.kind, TokenKind.TABLE_FENCE)
                self.assertEqual(token.line, 1)

    def test_extra_equals_signs_are_not_a_fence(self) -> None:
        # ``|====`` is not the AsciiDoc fence — the lexer doesn't
        # recognise it. It falls through to a plain LineToken so the
        # parser can reject it as UNKNOWN_BLOCK.
        tokens = tokenize("|====")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_pair_of_fences_each_become_table_fence_tokens(self) -> None:
        tokens = tokenize("|===\n|cell\n|===\n")
        self.assertIsInstance(tokens[0], TableFenceToken)
        self.assertIsInstance(tokens[1], LineToken)
        self.assertIsInstance(tokens[2], TableFenceToken)


class ColsDirectiveTokenizationTests(unittest.TestCase):
    """``[cols="N,N,..."]`` produces a :class:`ColsDirectiveToken`.

    The lexer only checks the structural shape — non-empty body
    inside ``[cols="…"]``. The parser is responsible for validating
    that each value is a positive integer.
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, str], ...] = (
            ("two columns", '[cols="1,2"]', "1,2"),
            ("three columns", '[cols="1,2,3"]', "1,2,3"),
            (
                "internal whitespace preserved for parser",
                '[cols="1, 2 , 3"]',
                "1, 2 , 3",
            ),
            (
                "non-integer body — still tokenised, parser rejects",
                '[cols="1,foo"]',
                "1,foo",
            ),
            (
                "negative body — still tokenised, parser rejects",
                '[cols="-1,2"]',
                "-1,2",
            ),
        )
        for desc, source, raw in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, ColsDirectiveToken)
                assert isinstance(token, ColsDirectiveToken)
                self.assertEqual(token.kind, TokenKind.COLS_DIRECTIVE)
                self.assertEqual(token.raw, raw)

    def test_empty_body_falls_through_to_line_token(self) -> None:
        # ``[cols=""]`` — empty body. The lexer does not emit a
        # ColsDirectiveToken with empty body; it falls through to a
        # plain LineToken so the parser can reject the bracketed
        # shape as UNKNOWN_BLOCK.
        tokens = tokenize('[cols=""]')
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_missing_close_quote_falls_through(self) -> None:
        # ``[cols="1,2]`` — missing closing quote. Not the structural
        # shape we recognise; falls through to LineToken.
        tokens = tokenize('[cols="1,2]')
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_cols_with_sibling_options_field_recognised(self) -> None:
        # ``[cols="3,1", options="header"]`` — the cols directive
        # alongside another attribute. Real-world AsciiDoc routinely
        # combines these; the lexer extracts the cols value and
        # discards the sibling.
        tokens = tokenize('[cols="3,1", options="header"]')
        self.assertEqual(len(tokens), 1)
        token = tokens[0]
        self.assertIsInstance(token, ColsDirectiveToken)
        assert isinstance(token, ColsDirectiveToken)
        self.assertEqual(token.raw, "3,1")

    def test_cols_with_multiple_sibling_fields_recognised(self) -> None:
        # ``[cols="…", options="…", frame=topbot]`` — three fields,
        # quoted and unquoted, in any order. All siblings are
        # discarded by the lexer; only the cols value survives.
        tokens = tokenize(
            '[cols="1,2,3", options="header", frame=topbot]'
        )
        self.assertEqual(len(tokens), 1)
        token = tokens[0]
        self.assertIsInstance(token, ColsDirectiveToken)
        assert isinstance(token, ColsDirectiveToken)
        self.assertEqual(token.raw, "1,2,3")

    def test_attribute_list_without_cols_falls_through(self) -> None:
        # ``[options="header"]`` — a valid attribute list but with no
        # cols field. The lexer is **not** a permissive
        # attribute-list catch-all; without a cols field the line
        # falls through to LineToken and the parser rejects it.
        tokens = tokenize('[options="header"]')
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_cols_field_after_other_fields_recognised(self) -> None:
        # Cols may appear in any position within the attribute list.
        tokens = tokenize('[options="header", cols="2,3"]')
        self.assertEqual(len(tokens), 1)
        token = tokens[0]
        self.assertIsInstance(token, ColsDirectiveToken)
        assert isinstance(token, ColsDirectiveToken)
        self.assertEqual(token.raw, "2,3")

    def test_malformed_attribute_list_falls_through(self) -> None:
        # Trailing comma, unbalanced quotes, etc. all fall through to
        # a LineToken so the parser raises UNKNOWN_BLOCK against the
        # bracketed shape — strict mode is preserved.
        bad_inputs = (
            '[cols="1,2",]',  # trailing comma
            '[cols="unterminated]',  # unterminated quote
            '[cols=]',  # bare ``=``
            '[=value]',  # missing name
        )
        for source in bad_inputs:
            with self.subTest(source=source):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                self.assertIsInstance(tokens[0], LineToken)


# ---------------------------------------------------------------------------
# Attribute entry tokenisation
# ---------------------------------------------------------------------------


class AttributeEntryTokenizationTests(unittest.TestCase):
    """``:name: value`` and the bare ``:name:`` setter form lex distinctly.

    The lexer recognises the shape so the parser can consume-and-discard
    a contiguous header run without peeking at LineToken text. Whether
    the entry is *positionally* valid (i.e. inside the document header)
    is enforced by the parser, not the lexer.
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, str, str | None], ...] = (
            # (description, source, expected name, expected value)
            ("name with value", ":author: Me", "author", "Me"),
            (
                "name with hyphenated value",
                ":revdate: 2026-04-14",
                "revdate",
                "2026-04-14",
            ),
            (
                "name with comma-separated value",
                ":tags: favorite, reference",
                "tags",
                "favorite, reference",
            ),
            ("bare setter form", ":doctype:", "doctype", None),
            (
                "name with hyphens",
                ":source-highlighter: rouge",
                "source-highlighter",
                "rouge",
            ),
            (
                "name with underscores",
                ":my_attr: value",
                "my_attr",
                "value",
            ),
            (
                "trailing-space-only value collapses to bare setter form",
                ":empty: ",
                "empty",
                None,
            ),
        )
        for desc, source, expected_name, expected_value in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, AttributeEntryToken)
                assert isinstance(token, AttributeEntryToken)
                self.assertEqual(token.kind, TokenKind.ATTRIBUTE_ENTRY)
                self.assertEqual(token.name, expected_name)
                self.assertEqual(token.value, expected_value)

    def test_malformed_falls_through_to_line_token(self) -> None:
        # Empty name (``::``), name with space (``:bad name:``), and
        # name with a leading digit (``:123:``) all fall through to
        # LineToken — the parser then rejects them as UNKNOWN_BLOCK.
        for source in ("::", ":: value", ":bad name:", ":123: value"):
            with self.subTest(source=source):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                self.assertIsInstance(tokens[0], LineToken)


# ---------------------------------------------------------------------------
# Admonition tokenisation (step 15)
# ---------------------------------------------------------------------------


class AdmonitionFenceTokenizationTests(unittest.TestCase):
    """``====`` on its own line is an :class:`AdmonitionFenceToken`.

    The four-equals literal is checked **before** the heading
    classifier so it is never lexed as a level-4 heading with empty
    body. Headings of level 5 or 6 (``=====`` / ``======``) still
    parse as headings — the fence is exact-literal match only.
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("plain fence", "===="),
            ("trailing whitespace stripped", "====   "),
        )
        for desc, source in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, AdmonitionFenceToken)
                assert isinstance(token, AdmonitionFenceToken)
                self.assertEqual(token.kind, TokenKind.ADMONITION_FENCE)
                self.assertEqual(token.line, 1)

    def test_three_equals_is_a_heading_not_a_fence(self) -> None:
        # Level 3 heading marker takes precedence: ``===`` is not the
        # admonition fence. Falls through to HeadingToken with empty
        # text.
        tokens = tokenize("===")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], HeadingToken)

    def test_five_equals_is_a_heading_not_a_fence(self) -> None:
        # Level 5 heading; the fence is exact four-equals match only.
        tokens = tokenize("=====")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], HeadingToken)

    def test_six_equals_is_a_heading_not_a_fence(self) -> None:
        tokens = tokenize("======")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], HeadingToken)

    def test_pair_of_fences_each_become_admonition_fence_tokens(self) -> None:
        tokens = tokenize("[NOTE]\n====\nbody\n====\n")
        self.assertIsInstance(tokens[0], AdmonitionDirectiveToken)
        self.assertIsInstance(tokens[1], AdmonitionFenceToken)
        self.assertIsInstance(tokens[2], LineToken)
        self.assertIsInstance(tokens[3], AdmonitionFenceToken)


class AdmonitionDirectiveTokenizationTests(unittest.TestCase):
    """``[KIND]`` on its own line produces :class:`AdmonitionDirectiveToken`.

    The lexer is permissive — any all-caps single word in brackets
    matches. Validation against :class:`AdmonitionKind` happens at
    parse time so an unknown label like ``[INFO]`` reaches the parser
    as a directive token (and surfaces a specific
    :class:`ParseErrorKind.UNKNOWN_ADMONITION_TYPE` rather than the
    generic ``UNKNOWN_BLOCK``).
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("note", "[NOTE]"),
            ("tip", "[TIP]"),
            ("important", "[IMPORTANT]"),
            ("warning", "[WARNING]"),
            ("caution", "[CAUTION]"),
            ("unknown — still tokenised, parser rejects", "[INFO]"),
            ("unknown long — still tokenised, parser rejects", "[FOOBAR]"),
        )
        for desc, source in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, AdmonitionDirectiveToken)
                assert isinstance(token, AdmonitionDirectiveToken)
                self.assertEqual(token.kind, TokenKind.ADMONITION_DIRECTIVE)
                # The bracketed label is stripped of its delimiters.
                self.assertEqual(token.kind_str, source[1:-1])

    def test_lowercase_label_falls_through_to_line_token(self) -> None:
        # ``[note]`` (lowercase) does not match the all-caps pattern.
        tokens = tokenize("[note]")
        self.assertEqual(len(tokens), 1)
        # The bracketed shape is rejected later by _reject_unknown_block.
        self.assertIsInstance(tokens[0], LineToken)

    def test_mixed_case_falls_through(self) -> None:
        tokens = tokenize("[Note]")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_directive_with_extra_content_falls_through(self) -> None:
        # ``[NOTE foo]`` is not a clean directive; spaces/extras break
        # the all-caps single-word rule.
        tokens = tokenize("[NOTE foo]")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)


class SingleAdmonitionTokenizationTests(unittest.TestCase):
    """``KIND: text`` produces :class:`SingleAdmonitionToken`.

    Restricted at lex time to the five known kinds — anything else
    (``URL: foo``, ``TODO: bar``) is plain prose. The captured
    :class:`AdmonitionKind` is validated by construction (the regex's
    alternation enumerates the five labels).
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, AdmonitionKind, str], ...] = (
            ("note", "NOTE: hello", AdmonitionKind.NOTE, "hello"),
            ("tip", "TIP: a tip", AdmonitionKind.TIP, "a tip"),
            (
                "important",
                "IMPORTANT: read this",
                AdmonitionKind.IMPORTANT,
                "read this",
            ),
            (
                "warning",
                "WARNING: hot surface",
                AdmonitionKind.WARNING,
                "hot surface",
            ),
            (
                "caution",
                "CAUTION: be careful",
                AdmonitionKind.CAUTION,
                "be careful",
            ),
            (
                "trailing whitespace stripped",
                "NOTE: hello   ",
                AdmonitionKind.NOTE,
                "hello",
            ),
            (
                "inline markup is opaque to lexer",
                "NOTE: see *important* item",
                AdmonitionKind.NOTE,
                "see *important* item",
            ),
        )
        for desc, source, kind, text in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, SingleAdmonitionToken)
                assert isinstance(token, SingleAdmonitionToken)
                self.assertEqual(token.kind, TokenKind.SINGLE_ADMONITION)
                self.assertEqual(token.admonition_kind, kind)
                self.assertEqual(token.text, text)

    def test_unknown_kind_falls_through(self) -> None:
        # ``URL:`` is not a known admonition label — stays prose.
        tokens = tokenize("URL: https://example.com")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_lowercase_kind_falls_through(self) -> None:
        # ``note: …`` (lowercase) is not an admonition.
        tokens = tokenize("note: hello")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_kind_without_space_after_colon_falls_through(self) -> None:
        # ``NOTE:hello`` (no space) — the regex requires colon-space.
        tokens = tokenize("NOTE:hello")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_empty_text_after_colon_falls_through(self) -> None:
        # ``NOTE: `` (only whitespace after) — after right-stripping
        # becomes ``NOTE:`` which doesn't match (no space-then-text).
        # Falls through to LineToken (becomes paragraph).
        tokens = tokenize("NOTE: ")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)


# ---------------------------------------------------------------------------
# Blockquote tokenisation (step 15)
# ---------------------------------------------------------------------------


class QuoteFenceTokenizationTests(unittest.TestCase):
    """``____`` on its own line is a :class:`QuoteFenceToken`.

    Same fence-pair shape as ``|===`` and ``----`` — both opening and
    closing lines emit the same token kind.
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("plain fence", "____"),
            ("trailing whitespace stripped", "____   "),
        )
        for desc, source in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, QuoteFenceToken)
                assert isinstance(token, QuoteFenceToken)
                self.assertEqual(token.kind, TokenKind.QUOTE_FENCE)
                self.assertEqual(token.line, 1)

    def test_three_underscores_is_a_line_token(self) -> None:
        tokens = tokenize("___")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_five_underscores_is_a_line_token(self) -> None:
        tokens = tokenize("_____")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_pair_of_fences_each_become_quote_fence_tokens(self) -> None:
        tokens = tokenize("____\nbody\n____\n")
        self.assertIsInstance(tokens[0], QuoteFenceToken)
        self.assertIsInstance(tokens[1], LineToken)
        self.assertIsInstance(tokens[2], QuoteFenceToken)


class QuoteDirectiveTokenizationTests(unittest.TestCase):
    """``[quote, …]`` produces :class:`QuoteDirectiveToken`.

    The lexer captures the bracket body raw — the parser validates
    field non-emptiness and arity.
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, str | None], ...] = (
            ("bare", "[quote]", None),
            ("with author", "[quote, Author]", ", Author"),
            (
                "with author and source",
                "[quote, Author, Source]",
                ", Author, Source",
            ),
            (
                "trailing comma — parser rejects",
                "[quote,]",
                ",",
            ),
            (
                "extra commas — parser rejects",
                "[quote, A, B, C]",
                ", A, B, C",
            ),
        )
        for desc, source, raw_arguments in cases:
            with self.subTest(desc):
                tokens = tokenize(source)
                self.assertEqual(len(tokens), 1)
                token = tokens[0]
                self.assertIsInstance(token, QuoteDirectiveToken)
                assert isinstance(token, QuoteDirectiveToken)
                self.assertEqual(token.kind, TokenKind.QUOTE_DIRECTIVE)
                self.assertEqual(token.raw_arguments, raw_arguments)

    def test_quote_with_extra_text_in_keyword_falls_through(self) -> None:
        # ``[quotes]`` is not the directive — keyword must be exactly
        # ``quote``.
        tokens = tokenize("[quotes]")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], LineToken)

    def test_uppercase_quote_falls_through(self) -> None:
        # ``[QUOTE]`` matches AdmonitionDirective, not QuoteDirective —
        # the labels are case-sensitive.
        tokens = tokenize("[QUOTE]")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], AdmonitionDirectiveToken)



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
            ("comment — parser rejects", "// a comment", "// a comment"),
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
