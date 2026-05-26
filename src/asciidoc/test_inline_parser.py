"""Tests for :mod:`asciidoc.inline_parser`.

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

from asciidoc.ast import (
    Bold,
    InlineNode,
    Italic,
    Link,
    Monospace,
    Strikethrough,
    Text,
    Underline,
)
from asciidoc.inline_parser import parse_inline
from enums import LinkScheme, ParseErrorKind
from models.parse_error import ParseError


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


def _mono(content: str, line: int = _LINE) -> Monospace:
    return Monospace(content=content, source_line=line)


def _link(
    url: str,
    scheme: LinkScheme,
    *children: InlineNode,
    line: int = _LINE,
) -> Link:
    """Build a :class:`Link` node from a URL and display children.

    A bare URL with no explicit display text passes the URL itself
    as the single ``Text`` child (matching what the parser produces);
    callers express that by passing ``_t(url)`` as the only child.
    """
    return Link(
        url=url,
        scheme=scheme,
        text=tuple(children),
        source_line=line,
    )


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
                "backtick now opens a monospace span (step 13)",
                "use `code` here",
                (
                    _t("use "),
                    _mono("code"),
                    _t(" here"),
                ),
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


# ---------------------------------------------------------------------------
# Monospace (step 13)
# ---------------------------------------------------------------------------


class MonospaceTests(unittest.TestCase):
    """The ``\\`code\\``` inline span — verbatim, no nesting, line-bounded."""

    def test_simple_monospace_span(self) -> None:
        result = parse_inline("`code`", _LINE)
        self.assertEqual(result, (_mono("code"),))

    def test_monospace_with_surrounding_text(self) -> None:
        result = parse_inline("call `f(x)` then return", _LINE)
        self.assertEqual(
            result,
            (
                _t("call "),
                _mono("f(x)"),
                _t(" then return"),
            ),
        )

    def test_empty_monospace_renders_as_empty(self) -> None:
        # ``\\`\\``` opens then immediately closes; the body is empty.
        result = parse_inline("``", _LINE)
        self.assertEqual(result, (_mono(""),))

    def test_monospace_body_is_not_re_parsed(self) -> None:
        # Bold and italic markers inside a monospace span are
        # preserved verbatim — not parsed as nested formatting.
        result = parse_inline("`*literal* _stars_`", _LINE)
        self.assertEqual(result, (_mono("*literal* _stars_"),))

    def test_monospace_body_preserves_brackets_and_hashes(self) -> None:
        # Other "structural" characters that could trip the recursive
        # span dispatch are also literal inside monospace.
        result = parse_inline("`[.line-through]#x#`", _LINE)
        self.assertEqual(result, (_mono("[.line-through]#x#"),))

    def test_unterminated_monospace_raises_dedicated_kind(self) -> None:
        # The dedicated kind exists so the editor's gutter can show a
        # different help message ("missing closing backtick") than the
        # generic BAD_INLINE_SPAN one.
        with self.assertRaises(ParseError) as ctx:
            parse_inline("`unclosed", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_MONOSPACE,
        )
        self.assertEqual(ctx.exception.line, _LINE)
        self.assertEqual(ctx.exception.column, 0)

    def test_unterminated_monospace_with_other_markers(self) -> None:
        # An unclosed backtick AFTER a closed bold span still raises
        # UNTERMINATED_MONOSPACE — the bold span is fine, the
        # monospace is not.
        with self.assertRaises(ParseError) as ctx:
            parse_inline("*ok* `unclosed", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_MONOSPACE,
        )

    def test_monospace_inside_bold(self) -> None:
        # Bold is a recursive-span; monospace is consumed inside its
        # body the same way other plain content is.
        result = parse_inline("*outer `code` end*", _LINE)
        self.assertEqual(
            result,
            (
                _bold(
                    _t("outer "),
                    _mono("code"),
                    _t(" end"),
                ),
            ),
        )

    def test_monospace_takes_precedence_over_bold(self) -> None:
        # ``\\`*\\``` is a monospace span with body ``*``; the inner
        # ``*`` is NOT a bold opener because monospace is matched
        # before the recursive-span dispatch.
        result = parse_inline("`*`", _LINE)
        self.assertEqual(result, (_mono("*"),))


# ---------------------------------------------------------------------------
# Bare URLs (step 13)
# ---------------------------------------------------------------------------


class BareUrlTests(unittest.TestCase):
    """Auto-linked ``http://``, ``https://``, ``mailto:`` URLs."""

    def test_simple_https_url(self) -> None:
        result = parse_inline("https://example.com", _LINE)
        self.assertEqual(
            result,
            (_link("https://example.com", LinkScheme.HTTPS, _t("https://example.com")),),
        )

    def test_simple_http_url(self) -> None:
        result = parse_inline("http://example.com/path", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "http://example.com/path",
                    LinkScheme.HTTP,
                    _t("http://example.com/path"),
                ),
            ),
        )

    def test_mailto_url(self) -> None:
        result = parse_inline("mailto:user@example.com", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:user@example.com",
                    LinkScheme.MAILTO,
                    _t("mailto:user@example.com"),
                ),
            ),
        )

    def test_url_inside_prose(self) -> None:
        result = parse_inline(
            "see https://example.com today",
            _LINE,
        )
        self.assertEqual(
            result,
            (
                _t("see "),
                _link(
                    "https://example.com",
                    LinkScheme.HTTPS,
                    _t("https://example.com"),
                ),
                _t(" today"),
            ),
        )

    def test_url_terminates_at_whitespace(self) -> None:
        # The URL stops at the first whitespace; trailing text is
        # plain prose.
        result = parse_inline(
            "https://x.com/abc def",
            _LINE,
        )
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/abc",
                    LinkScheme.HTTPS,
                    _t("https://x.com/abc"),
                ),
                _t(" def"),
            ),
        )

    def test_url_with_query_string_and_fragment(self) -> None:
        url = "https://example.com/path?q=1&r=2#section"
        result = parse_inline(url, _LINE)
        self.assertEqual(
            result,
            (_link(url, LinkScheme.HTTPS, _t(url)),),
        )

    def test_url_word_boundary_excludes_mid_word_match(self) -> None:
        # "myhttps://x" is plain text — there's no word boundary
        # before the scheme prefix, so URL recognition does not fire.
        result = parse_inline("myhttps://example.com", _LINE)
        self.assertEqual(result, (_t("myhttps://example.com"),))

    def test_url_at_start_of_input_is_a_boundary(self) -> None:
        # Position 0 is treated as a boundary even though there's no
        # preceding character.
        result = parse_inline("https://x", _LINE)
        self.assertEqual(
            result,
            (_link("https://x", LinkScheme.HTTPS, _t("https://x")),),
        )

    def test_url_after_closing_bold_marker(self) -> None:
        # After ``*ok*`` the previous char is ``*`` (non-alphanumeric),
        # so the URL is recognised at a boundary.
        result = parse_inline("*ok*https://x", _LINE)
        self.assertEqual(
            result,
            (
                _bold(_t("ok")),
                _link("https://x", LinkScheme.HTTPS, _t("https://x")),
            ),
        )

    def test_url_with_display_text(self) -> None:
        result = parse_inline(
            "https://example.com[click here]", _LINE
        )
        self.assertEqual(
            result,
            (
                _link(
                    "https://example.com",
                    LinkScheme.HTTPS,
                    _t("click here"),
                ),
            ),
        )

    def test_url_with_display_text_supports_inline_formatting(self) -> None:
        # The plan requires display text to support nested formatting
        # (other than other links). Bold inside display works.
        result = parse_inline(
            "https://x[click *here*]",
            _LINE,
        )
        link = result[0]
        assert isinstance(link, Link)
        self.assertEqual(
            link.text,
            (_t("click "), _bold(_t("here"))),
        )

    def test_url_with_empty_brackets_falls_back_to_url_text(self) -> None:
        # An empty ``[]`` after a URL is a quirk of the user's source;
        # the parser keeps the URL itself as the display text and
        # leaves the brackets as plain trailing prose.
        result = parse_inline("https://x[]", _LINE)
        self.assertEqual(
            result,
            (
                _link("https://x", LinkScheme.HTTPS, _t("https://x")),
                _t("[]"),
            ),
        )

    def test_url_with_unmatched_bracket_falls_back(self) -> None:
        # A ``[`` after a URL with no matching ``]`` on the line is
        # not a malformed link — it's just a stray ``[`` that the
        # bare-URL form tolerates by leaving the bracket as text.
        result = parse_inline("https://x[oops", _LINE)
        self.assertEqual(
            result,
            (
                _link("https://x", LinkScheme.HTTPS, _t("https://x")),
                _t("[oops"),
            ),
        )

    def test_url_terminates_at_inline_marker(self) -> None:
        # ``*see https://x*`` should parse as Bold containing URL,
        # not as Bold whose body absorbs the closing ``*`` into the
        # URL string.
        result = parse_inline("*see https://x*", _LINE)
        self.assertEqual(
            result,
            (
                _bold(
                    _t("see "),
                    _link("https://x", LinkScheme.HTTPS, _t("https://x")),
                ),
            ),
        )

    def test_two_urls_in_one_line(self) -> None:
        result = parse_inline(
            "see https://a.com and https://b.com today",
            _LINE,
        )
        kinds = [type(node).__name__ for node in result]
        self.assertEqual(
            kinds,
            ["Text", "Link", "Text", "Link", "Text"],
        )

    def test_url_with_fragment_inside_strikethrough(self) -> None:
        # Inside a strikethrough span the close marker is ``#``. The
        # URL scanner must terminate at that ``#`` so the span closes
        # cleanly — even though ``#`` is otherwise a valid URL
        # fragment delimiter at top level.
        result = parse_inline(
            "[.line-through]#https://x#",
            _LINE,
        )
        self.assertEqual(
            result,
            (
                _strike(
                    _link("https://x", LinkScheme.HTTPS, _t("https://x")),
                ),
            ),
        )


class BareUrlSchemeAllowListTests(unittest.TestCase):
    """Schemes outside the allow-list are NOT auto-linked as bare URLs."""

    def test_javascript_scheme_is_plain_text(self) -> None:
        # Bare-URL recognition only triggers on the three allow-listed
        # prefixes. ``javascript:`` is just text.
        result = parse_inline("javascript:alert(1)", _LINE)
        self.assertEqual(result, (_t("javascript:alert(1)"),))

    def test_file_scheme_is_plain_text(self) -> None:
        result = parse_inline("file:///tmp/x", _LINE)
        self.assertEqual(result, (_t("file:///tmp/x"),))

    def test_ftp_scheme_is_plain_text(self) -> None:
        result = parse_inline("ftp://x.com", _LINE)
        self.assertEqual(result, (_t("ftp://x.com"),))


# ---------------------------------------------------------------------------
# link: macro (step 13)
# ---------------------------------------------------------------------------


class LinkMacroTests(unittest.TestCase):
    """``link:URL[display]`` — explicit-form link with display text."""

    def test_link_macro_with_https(self) -> None:
        result = parse_inline("link:https://x[click here]", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x",
                    LinkScheme.HTTPS,
                    _t("click here"),
                ),
            ),
        )

    def test_link_macro_with_http(self) -> None:
        result = parse_inline("link:http://x.com[home]", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "http://x.com",
                    LinkScheme.HTTP,
                    _t("home"),
                ),
            ),
        )

    def test_link_macro_with_mailto(self) -> None:
        result = parse_inline(
            "link:mailto:a@b.com[email me]", _LINE
        )
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:a@b.com",
                    LinkScheme.MAILTO,
                    _t("email me"),
                ),
            ),
        )

    def test_link_macro_inside_prose(self) -> None:
        result = parse_inline(
            "see link:https://x[here] for more",
            _LINE,
        )
        self.assertEqual(
            result,
            (
                _t("see "),
                _link("https://x", LinkScheme.HTTPS, _t("here")),
                _t(" for more"),
            ),
        )

    def test_link_macro_display_supports_inline_formatting(self) -> None:
        result = parse_inline(
            "link:https://x[*bold* link]", _LINE
        )
        link = result[0]
        assert isinstance(link, Link)
        self.assertEqual(
            link.text,
            (_bold(_t("bold")), _t(" link")),
        )

    def test_link_macro_unsupported_scheme_javascript_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline(
                "link:javascript:alert(1)[click]", _LINE
            )
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNSUPPORTED_LINK_SCHEME,
        )

    def test_link_macro_unsupported_scheme_file_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:file:///x[bad]", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNSUPPORTED_LINK_SCHEME,
        )

    def test_link_macro_unsupported_scheme_ftp_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:ftp://x[bad]", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNSUPPORTED_LINK_SCHEME,
        )

    def test_link_macro_missing_close_bracket_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:https://x[oops", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_link_macro_missing_brackets_entirely_raises(self) -> None:
        # ``link:URL`` with no ``[...]`` part — BAD_LINK_MACRO.
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:https://x", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_link_macro_empty_display_text_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:https://x[]", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_link_macro_no_url_raises(self) -> None:
        # ``link:[t]`` — no URL at all between the prefix and the
        # bracket. Treated as a malformed macro.
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:[t]", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_link_macro_word_boundary_required(self) -> None:
        # ``mylink:`` is plain text — the ``link:`` prefix is not at
        # a word boundary, so macro recognition does not fire on
        # that prefix. The ``https://`` that follows IS at a boundary
        # (preceding char is ``:``, non-alphanumeric), so the
        # remainder parses as a regular URL-with-text link.
        result = parse_inline("mylink:https://x[t]", _LINE)
        kinds = [type(node).__name__ for node in result]
        # First node: literal ``mylink:`` — confirms the macro was
        # NOT recognised against ``mylink:``.
        self.assertEqual(kinds[0], "Text")
        first = result[0]
        assert isinstance(first, Text)
        self.assertEqual(first.content, "mylink:")
        # Second node: a regular link (URL-with-text form).
        self.assertEqual(kinds[1], "Link")
        second = result[1]
        assert isinstance(second, Link)
        self.assertEqual(second.url, "https://x")
        self.assertEqual(second.text, (_t("t"),))


# ---------------------------------------------------------------------------
# Nested-link rejection (step 13)
# ---------------------------------------------------------------------------


class NestedLinkRejectionTests(unittest.TestCase):
    """Links cannot contain other links — verified for both forms."""

    def test_bare_url_inside_link_macro_display_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline(
                "link:https://x[see https://y]", _LINE
            )
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_bare_url_inside_url_with_text_display_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline(
                "https://x[also https://y]", _LINE
            )
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_link_macro_inside_link_macro_display_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline(
                "link:https://x[wrap link:https://y[inner]]",
                _LINE,
            )
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_link_macro_inside_url_with_text_display_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline(
                "https://x[wrap link:https://y[inner]]", _LINE
            )
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )


# ---------------------------------------------------------------------------
# Integration: monospace + links + classic spans
# ---------------------------------------------------------------------------


class MixedConstructTests(unittest.TestCase):
    """Spot-checks that the new constructs compose with the old ones."""

    def test_monospace_inside_link_display(self) -> None:
        # A monospace span inside the display text of a URL-with-text
        # link. The renderer needs to apply both link decoration and
        # monospace styling.
        result = parse_inline(
            "https://x[the `f()` function]", _LINE
        )
        link = result[0]
        assert isinstance(link, Link)
        self.assertEqual(
            link.text,
            (_t("the "), _mono("f()"), _t(" function")),
        )

    def test_link_inside_bold(self) -> None:
        result = parse_inline(
            "*Read https://x[here] now*", _LINE
        )
        self.assertEqual(
            result,
            (
                _bold(
                    _t("Read "),
                    _link("https://x", LinkScheme.HTTPS, _t("here")),
                    _t(" now"),
                ),
            ),
        )

    def test_monospace_after_link(self) -> None:
        result = parse_inline(
            "see https://x[here] then `code`", _LINE
        )
        kinds = [type(node).__name__ for node in result]
        self.assertEqual(
            kinds,
            ["Text", "Link", "Text", "Monospace"],
        )


class LinkMacroPassthroughTests(unittest.TestCase):
    """``link:++URL++[text]`` — passthrough URL containing inline
    markers that would otherwise be interpreted by the scanner.

    The construct exists so a user can paste a URL containing ``*``,
    ``_``, ``#``, or ``[`` without escape gymnastics. Inside the
    passthrough every character is literal; after the closing
    ``++`` the URL is validated against :class:`LinkScheme` exactly
    as in the unwrapped form.
    """

    def test_passthrough_around_https_url(self) -> None:
        result = parse_inline(
            "link:++https://example.com++[text]", _LINE
        )
        self.assertEqual(
            result,
            (
                _link(
                    "https://example.com",
                    LinkScheme.HTTPS,
                    _t("text"),
                ),
            ),
        )

    def test_passthrough_preserves_inline_markers_in_url(self) -> None:
        # The whole point of the passthrough: a ``*`` inside the URL
        # is literal, not a bold opener.
        result = parse_inline(
            "link:++https://example.com/a*b++[text]", _LINE
        )
        link = result[0]
        assert isinstance(link, Link)
        self.assertEqual(link.url, "https://example.com/a*b")

    def test_passthrough_preserves_brackets_in_url(self) -> None:
        # ``[`` inside the URL is literal — without the passthrough,
        # it would be the display-text opener.
        result = parse_inline(
            "link:++https://example.com/a[b]c++[text]", _LINE
        )
        link = result[0]
        assert isinstance(link, Link)
        self.assertEqual(link.url, "https://example.com/a[b]c")

    def test_passthrough_unterminated_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:++https://x[text]", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNTERMINATED_PASSTHROUGH,
        )

    def test_passthrough_with_unsupported_scheme_raises(self) -> None:
        # The passthrough wraps the URL syntactically, but the scheme
        # validation still applies after the closing ``++``. The
        # Sourdough fixture's ``link:++recipe://…++[…]`` lands here.
        with self.assertRaises(ParseError) as ctx:
            parse_inline(
                "link:++recipe://x++[t]", _LINE
            )
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.UNSUPPORTED_LINK_SCHEME,
        )

    def test_passthrough_with_no_scheme_raises_bad_link_macro(self) -> None:
        # ``link:++++[t]`` — empty passthrough body. The unwrapped
        # URL has no scheme at all → BAD_LINK_MACRO.
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:++++[t]", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_passthrough_followed_by_missing_brackets_raises(self) -> None:
        # ``link:++https://x++`` — passthrough closed but no
        # ``[display]`` afterwards.
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:++https://x++", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_LINK_MACRO,
        )

    def test_passthrough_with_display_inline_formatting(self) -> None:
        # The display text after a passthrough URL still parses for
        # inline formatting, just like the non-passthrough form.
        result = parse_inline(
            "link:++https://x++[*bold* link]", _LINE
        )
        link = result[0]
        assert isinstance(link, Link)
        # The display contains a Bold node and a Text node.
        kinds = [type(n).__name__ for n in link.text]
        self.assertIn("Bold", kinds)


if __name__ == "__main__":
    unittest.main()
