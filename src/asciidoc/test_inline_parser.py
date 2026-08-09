"""Tests for :mod:`asciidoc.inline_parser`.

The inline parser is a pure ``str -> tuple[InlineNode, ...]`` function.
Every test in this module builds a small input string, calls
:func:`parse_inline`, and asserts on the produced tree.

Two recurring shapes:
* **Valid input** — the parser returns a tuple of inline nodes whose
  structure we assert against an expected tuple. Source-line numbers
  are checked because the renderer uses them for error positioning.
* **Invalid input** — the parser raises with the offending source line
  and column ``0``, which is what the editor's gutter renderer expects.
  Note what is *not* in this bucket: a formatting marker that does not
  resolve to a span is prose, never an error, so every unpaired ``*`` or
  ``_`` below is asserted as :class:`Text`. The failures left are all
  refusals of a construct the user reached for — a ``link:`` or
  ``attachment:`` macro, a passthrough, the nesting cap.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

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
from asciidoc import inline_parser
from asciidoc.inline_parser import parse_inline
from config.defaults import MAX_INLINE_DEPTH
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


def _nested_spans(levels: int, body: str = "x") -> str:
    """Build a source line nesting ``levels`` formatting spans around ``body``.

    Markers alternate between ``**`` and ``__`` because same-marker
    self-nesting is impossible by construction — an inner ``**`` always
    closes the outer one. Each marker therefore contributes exactly one
    nesting level, so ``levels`` is the depth the scanner will reach.

    The *unconstrained* forms are what make arbitrary depth expressible.
    Their constrained twins cannot stack directly: ``*_x_*`` nests, but
    ``*_*x*_*`` does not, because the third marker is preceded by ``_``
    and an underscore is a word character for the opener test. That is
    the reference implementation's rule, not a folio limitation.
    """
    markers = ["**" if index % 2 == 0 else "__" for index in range(levels)]
    return "".join(markers) + body + "".join(reversed(markers))


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
                "interior markers are body text, not closers",
                "*a*b*c*",
                (_bold(_t("a*b*c")),),
            ),
            (
                "doubled marker with nothing to close is literal",
                "**",
                (_t("**"),),
            ),
            (
                "doubled italic marker with nothing to close is literal",
                "__",
                (_t("__"),),
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
                "underscore in running prose",
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
# Markers that are prose
# ---------------------------------------------------------------------------


class LiteralMarkerTests(unittest.TestCase):
    """A marker that does not resolve to a span is ordinary text.

    These are the inputs the old contract was written around — every one
    of them used to raise. They are kept, inverted, because they are the
    clearest evidence for the rule that replaced it: AsciiDoc is total,
    so an unpaired marker is prose and reporting it would assert
    something about the document the language says is false.
    """

    def test_table(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("bare bold opener", "*unclosed"),
            ("bold opener with trailing space", "*nope "),
            ("bare italic opener", "_unclosed"),
            ("strikethrough opener with no close", "[.line-through]#oops"),
            ("underline opener with no close", "[.underline]#oops"),
            ("strikethrough with prefix only", "[.line-through]#"),
            ("doubled marker with no close", "**unclosed"),
            ("unterminated monospace", "an `unterminated span"),
        )
        for desc, source in cases:
            with self.subTest(desc):
                self.assertEqual(parse_inline(source, _LINE), (_t(source),))

    def test_an_unresolvable_inner_marker_leaves_the_outer_span(self) -> None:
        # ``_inner`` never closes, so it is text *inside* the bold span
        # the surrounding asterisks do form.
        self.assertEqual(
            parse_inline("*outer _inner*", _LINE),
            (_bold(_t("outer _inner")),),
        )

    def test_an_unresolvable_outer_marker_leaves_the_inner_span(self) -> None:
        # The mirror image: the outer ``*`` has no valid closer (the
        # line ends in a space), the inner italic is untouched.
        self.assertEqual(
            parse_inline("*outer _inner_ ", _LINE),
            (_t("*outer "), _italic(_t("inner")), _t(" ")),
        )


class ConstrainedOpenerTests(unittest.TestCase):
    """Where a constrained marker may open a span.

    The rule has two halves — not preceded by a word character (nor by
    ``;`` / ``:``), and not followed by a space — and both are pinned
    against Asciidoctor 4.0.7 output.
    """

    def test_marker_after_a_word_character_is_literal(self) -> None:
        cases = ("a*b*c", "a_b_c", "a`b`c", "snake_case_name")
        for source in cases:
            with self.subTest(source):
                self.assertEqual(
                    parse_inline(source, _LINE), (_t(source),)
                )

    def test_marker_followed_by_a_space_is_literal(self) -> None:
        self.assertEqual(
            parse_inline("2 * 3 * 4", _LINE), (_t("2 * 3 * 4"),)
        )

    def test_semicolon_and_colon_block_an_opener(self) -> None:
        # The reference guards its constrained openers with ``[^\w;:]``
        # so emphasis cannot fire after a macro prefix or an entity.
        for source in ("a;*bold*", "a:*bold*", "a;_it_", "a;`m`"):
            with self.subTest(source):
                self.assertEqual(
                    parse_inline(source, _LINE), (_t(source),)
                )

    def test_other_punctuation_does_not_block_an_opener(self) -> None:
        for prefix in (",", ".", ")", "-", "/", "="):
            with self.subTest(prefix):
                self.assertEqual(
                    parse_inline(f"a{prefix}*bold*", _LINE),
                    (_t(f"a{prefix}"), _bold(_t("bold"))),
                )

    def test_punctuation_around_a_span_still_formats(self) -> None:
        for source, before, after in (
            ("(*bold*)", "(", ")"),
            ('"*bold*"', '"', '"'),
            ("-*bold*-", "-", "-"),
        ):
            with self.subTest(source):
                self.assertEqual(
                    parse_inline(source, _LINE),
                    (_t(before), _bold(_t("bold")), _t(after)),
                )


class ConstrainedCloserTests(unittest.TestCase):
    """Where a constrained marker may close a span."""

    def test_closer_followed_by_a_word_character_is_body_text(self) -> None:
        # No later candidate closes either, so the whole line is prose.
        self.assertEqual(parse_inline("*bold*x", _LINE), (_t("*bold*x"),))

    def test_scan_continues_past_an_invalid_closer(self) -> None:
        # The first two ``*`` are followed by word characters; the last
        # one closes, so the span covers everything between.
        self.assertEqual(
            parse_inline("*a*b*c*", _LINE), (_bold(_t("a*b*c")),)
        )

    def test_closer_preceded_by_a_space_is_body_text(self) -> None:
        self.assertEqual(parse_inline("*bold *", _LINE), (_t("*bold *"),))

    def test_a_failed_closer_never_opens_a_nested_span(self) -> None:
        # ``*b*`` looks like a span, but its opening ``*`` is the outer
        # span's own marker: offering it to the opener dispatch would
        # nest bold inside bold. The reference produces this same tree.
        self.assertEqual(
            parse_inline("*a *b* c*", _LINE),
            (_bold(_t("a *b")), _t(" c*")),
        )

    def test_role_span_closer_obeys_the_same_rule(self) -> None:
        self.assertEqual(
            parse_inline("[.underline]#x#y", _LINE), (_t("[.underline]#x#y"),)
        )


class UnconstrainedMarkerTests(unittest.TestCase):
    """The doubled forms open and close anywhere, including mid-word."""

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, tuple[InlineNode, ...]], ...] = (
            (
                "bold inside a word",
                "a**b**c",
                (_t("a"), _bold(_t("b")), _t("c")),
            ),
            (
                "italic inside a word",
                "word__it__word",
                (_t("word"), _italic(_t("it")), _t("word")),
            ),
            (
                "monospace inside a word",
                "a``m``b",
                (_t("a"), _mono("m"), _t("b")),
            ),
            (
                "doubled bold on its own",
                "**bold**",
                (_bold(_t("bold")),),
            ),
            (
                "doubled italic on its own",
                "__it__",
                (_italic(_t("it")),),
            ),
        )
        for desc, source, expected in cases:
            with self.subTest(desc):
                self.assertEqual(parse_inline(source, _LINE), expected)

    def test_semicolon_does_not_block_a_doubled_opener(self) -> None:
        # Unconstrained markers are exempt from the opener rule
        # altogether — confirmed against the reference.
        self.assertEqual(
            parse_inline("a;**bold**", _LINE),
            (_t("a;"), _bold(_t("bold"))),
        )

    def test_body_may_hold_the_single_marker(self) -> None:
        self.assertEqual(
            parse_inline("**a*b**", _LINE), (_bold(_t("a*b")),)
        )


class BacktrackingTests(unittest.TestCase):
    """An opener with no valid closer becomes text, and only itself."""

    def test_valid_spans_on_the_same_line_survive(self) -> None:
        # The whole point of backtracking over quarantining the line:
        # one unresolvable marker must not cost the line its formatting.
        self.assertEqual(
            parse_inline("_ok_ and *bold", _LINE),
            (_italic(_t("ok")), _t(" and *bold")),
        )

    def test_a_failed_inner_span_does_not_fail_the_outer(self) -> None:
        self.assertEqual(
            parse_inline("*bold [.underline]#x*", _LINE),
            (_bold(_t("bold [.underline]#x")),),
        )

    def test_a_rejected_doubled_marker_leaves_its_twin_available(self) -> None:
        # ``**`` cannot close, so the first asterisk is text and the
        # second opens a constrained span — as it does in the reference.
        self.assertEqual(
            parse_inline("**a*b*", _LINE), (_t("*"), _bold(_t("a*b")))
        )

    def test_macro_errors_still_escape_a_span_body(self) -> None:
        # Backtracking must not swallow a refusal of a construct the
        # user actually reached for.
        with self.assertRaises(ParseError) as ctx:
            parse_inline("*bold link:ftp://x.test[y]*", _LINE)
        self.assertEqual(
            ctx.exception.kind, ParseErrorKind.UNSUPPORTED_LINK_SCHEME
        )


class CloserIndexTests(unittest.TestCase):
    """The closer index skips only attempts that were going to fail."""

    def test_output_matches_an_unguarded_scan(self) -> None:
        # The guard is a necessary condition on a span closing, so
        # disabling it must change nothing. Patching the index to a
        # value that can never skip is the cheapest way to say that.
        sources = (
            "*a*b*c*", "**a*b*", "*a *b* c*", "_ok_ and *bold",
            "a ``m`` b", "an `unterminated span", "[.underline]#x#y",
        )
        for source in sources:
            with self.subTest(source):
                guarded = parse_inline(source, _LINE)
                with mock.patch.object(
                    inline_parser._Scanner,
                    "_last_valid_closer",
                    lambda self, close: len(self.text),
                ):
                    unguarded = parse_inline(source, _LINE)
                self.assertEqual(guarded, unguarded)

    def test_a_marker_dense_line_stays_linear(self) -> None:
        # Without the index every opener rescans to end of line, which
        # is quadratic: this input took ~10s before the guard existed.
        source = "*a " * 2000
        start = time.perf_counter()
        parse_inline(source, _LINE)
        self.assertLess(time.perf_counter() - start, 1.0)


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

    def test_a_lone_doubled_backtick_is_literal(self) -> None:
        # Two backticks are now the *unconstrained* opener, so this is
        # an opener with nothing to close rather than an empty span.
        # ``a````b`` is how an empty monospace span is written.
        self.assertEqual(parse_inline("``", _LINE), (_t("``"),))

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

    def test_unterminated_monospace_is_literal(self) -> None:
        # A backtick with no valid closer is prose, exactly as the
        # reference renders it — there is no "unterminated monospace"
        # failure any more, so the editor's gutter has nothing to say.
        self.assertEqual(
            parse_inline("`unclosed", _LINE), (_t("`unclosed"),)
        )

    def test_unterminated_monospace_leaves_earlier_spans_alone(self) -> None:
        # The bold span is fine and stays formatted; only the backtick
        # degrades to text.
        self.assertEqual(
            parse_inline("*ok* `unclosed", _LINE),
            (_bold(_t("ok")), _t(" `unclosed")),
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
    """Auto-linked ``http://`` and ``https://`` URLs."""

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
        # so the URL is recognised at a boundary. The space matters:
        # without it the bold closer would be followed by a word
        # character and the whole run would be prose.
        result = parse_inline("*ok* https://x", _LINE)
        self.assertEqual(
            result,
            (
                _bold(_t("ok")),
                _t(" "),
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

    def test_url_terminates_at_enclosing_close_marker(self) -> None:
        # ``*see https://x*`` should parse as Bold containing URL,
        # not as Bold whose body absorbs the closing ``*`` into the
        # URL string. The asterisk ends the URL because it is where
        # the enclosing span validly closes -- not because ``*`` is a
        # URL character (at top level it is one, see UrlExtentTests).
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
# URL extent, trailing punctuation, and mail (phase B)
# ---------------------------------------------------------------------------


class UrlTrailingPunctuationTests(unittest.TestCase):
    """Sentence punctuation is not part of a bare URL's target."""

    def test_trailing_full_stop_is_not_part_of_the_target(self) -> None:
        result = parse_inline("https://x.com.", _LINE)
        link = result[0]
        assert isinstance(link, Link)
        self.assertEqual(link.url, "https://x.com")

    def test_trailing_full_stop_survives_as_following_text(self) -> None:
        result = parse_inline("https://x.com.", _LINE)
        self.assertEqual(
            result,
            (
                _link("https://x.com", LinkScheme.HTTPS, _t("https://x.com")),
                _t("."),
            ),
        )

    def test_a_run_of_trailing_punctuation_is_peeled_entirely(self) -> None:
        result = parse_inline("https://x.com/a);", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/a", LinkScheme.HTTPS, _t("https://x.com/a")
                ),
                _t(");"),
            ),
        )

    def test_trailing_bracket_is_peeled_regardless_of_balance(self) -> None:
        # The reference is not balance-aware: the closing bracket of
        # ``/(a)`` goes even though its opener is inside the URL.
        result = parse_inline("https://x.com/(a)", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/(a",
                    LinkScheme.HTTPS,
                    _t("https://x.com/(a"),
                ),
                _t(")"),
            ),
        )

    def test_an_apostrophe_stays_in_the_target(self) -> None:
        result = parse_inline("https://x.com'", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com'", LinkScheme.HTTPS, _t("https://x.com'")
                ),
            ),
        )

    def test_punctuation_inside_the_path_is_kept(self) -> None:
        result = parse_inline("https://x.com/a,b", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/a,b",
                    LinkScheme.HTTPS,
                    _t("https://x.com/a,b"),
                ),
            ),
        )

    def test_a_labelled_url_keeps_its_trailing_punctuation(self) -> None:
        # The bracket already marks where the URL ends, so there is
        # nothing to peel and the full stop belongs to the target.
        result = parse_inline("https://x.com.[l]", _LINE)
        self.assertEqual(
            result,
            (_link("https://x.com.", LinkScheme.HTTPS, _t("l")),),
        )


class UrlExtentTests(unittest.TestCase):
    """What a bare URL absorbs, and what ends it."""

    def test_an_underscored_url_links_whole(self) -> None:
        url = "https://en.wikipedia.org/wiki/Naive_set_theory"
        result = parse_inline(url, _LINE)
        self.assertEqual(result, (_link(url, LinkScheme.HTTPS, _t(url)),))

    def test_an_asterisk_in_a_path_does_not_end_the_url(self) -> None:
        result = parse_inline("https://x.com/a*b*c", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/a*b*c",
                    LinkScheme.HTTPS,
                    _t("https://x.com/a*b*c"),
                ),
            ),
        )

    def test_a_backtick_in_a_path_does_not_end_the_url(self) -> None:
        result = parse_inline("https://x.com/a`b`c", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/a`b`c",
                    LinkScheme.HTTPS,
                    _t("https://x.com/a`b`c"),
                ),
            ),
        )

    def test_a_fragment_is_part_of_the_url(self) -> None:
        result = parse_inline("https://x.com/a#b", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/a#b",
                    LinkScheme.HTTPS,
                    _t("https://x.com/a#b"),
                ),
            ),
        )

    def test_a_closing_bracket_ends_the_url(self) -> None:
        result = parse_inline("https://x.com/foo]bar", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/foo",
                    LinkScheme.HTTPS,
                    _t("https://x.com/foo"),
                ),
                _t("]bar"),
            ),
        )


class UrlDoubledMarkerTests(unittest.TestCase):
    """A doubled marker ends a URL only when it pairs on the line."""

    def test_a_paired_doubled_marker_ends_the_url(self) -> None:
        result = parse_inline("https://x.com/a**b**c", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/a", LinkScheme.HTTPS, _t("https://x.com/a")
                ),
                _bold(_t("b")),
                _t("c"),
            ),
        )

    def test_an_unpaired_doubled_marker_stays_in_the_url(self) -> None:
        # No second ``__`` on the line, so nothing can become emphasis
        # and the underscores are ordinary URL characters.
        result = parse_inline("https://x.com/a__b", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com/a__b",
                    LinkScheme.HTTPS,
                    _t("https://x.com/a__b"),
                ),
            ),
        )


class UrlInsideSpanTests(unittest.TestCase):
    """A URL ends where its enclosing span validly closes."""

    def test_a_url_ends_where_the_enclosing_bold_validly_closes(self) -> None:
        # The first asterisk after the URL is followed by a word
        # character, so it cannot close the span and belongs to the
        # target; the second one closes it.
        result = parse_inline("*see https://x.com/a*b*", _LINE)
        self.assertEqual(
            result,
            (
                _bold(
                    _t("see "),
                    _link(
                        "https://x.com/a*b",
                        LinkScheme.HTTPS,
                        _t("https://x.com/a*b"),
                    ),
                ),
            ),
        )

    def test_a_url_ends_at_the_first_valid_closer_not_the_last(self) -> None:
        # Here the first asterisk after the URL is followed by a space,
        # so it does close -- the trailing ``*`` is left as prose.
        result = parse_inline("*a https://x.com/b* c*", _LINE)
        self.assertEqual(
            result,
            (
                _bold(
                    _t("a "),
                    _link(
                        "https://x.com/b",
                        LinkScheme.HTTPS,
                        _t("https://x.com/b"),
                    ),
                ),
                _t(" c*"),
            ),
        )

    def test_a_url_inside_an_underline_span_keeps_a_fragment(self) -> None:
        # ``#`` closes the span, but only where it validly closes: the
        # first one is followed by a word character, so it is a URL
        # fragment delimiter.
        result = parse_inline("[.underline]#https://x.com/a#b#", _LINE)
        self.assertEqual(
            result,
            (
                _under(
                    _link(
                        "https://x.com/a#b",
                        LinkScheme.HTTPS,
                        _t("https://x.com/a#b"),
                    ),
                ),
            ),
        )


class DegenerateUrlTests(unittest.TestCase):
    """A scheme with no body is text, not a link."""

    def test_a_scheme_with_no_body_is_plain_text(self) -> None:
        result = parse_inline("https://", _LINE)
        self.assertEqual(result, (_t("https://"),))

    def test_a_scheme_followed_only_by_punctuation_is_plain_text(self) -> None:
        # The full stop is peeled, leaving nothing but the scheme.
        result = parse_inline("https://.", _LINE)
        self.assertEqual(result, (_t("https://."),))


class EmailAutolinkTests(unittest.TestCase):
    """Bare addresses autolink to a ``mailto:`` target."""

    def test_a_bare_address_links_to_a_mailto_target(self) -> None:
        result = parse_inline("ada@example.com", _LINE)
        link = result[0]
        assert isinstance(link, Link)
        self.assertEqual(link.url, "mailto:ada@example.com")

    def test_the_display_text_is_the_address_without_the_scheme(self) -> None:
        result = parse_inline("ada@example.com", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:ada@example.com",
                    LinkScheme.MAILTO,
                    _t("ada@example.com"),
                ),
            ),
        )

    def test_a_dotted_local_part_is_part_of_the_address(self) -> None:
        result = parse_inline("ada.l@example.com", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:ada.l@example.com",
                    LinkScheme.MAILTO,
                    _t("ada.l@example.com"),
                ),
            ),
        )

    def test_a_trailing_full_stop_is_not_part_of_the_address(self) -> None:
        result = parse_inline("Write to ada@example.com.", _LINE)
        self.assertEqual(
            result,
            (
                _t("Write to "),
                _link(
                    "mailto:ada@example.com",
                    LinkScheme.MAILTO,
                    _t("ada@example.com"),
                ),
                _t("."),
            ),
        )

    def test_a_bracketed_suffix_after_an_address_is_literal_text(self) -> None:
        # Only the ``mailto:`` macro form takes a label; a bare
        # address does not consume one.
        result = parse_inline("ada@example.com[Mail me]", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:ada@example.com",
                    LinkScheme.MAILTO,
                    _t("ada@example.com"),
                ),
                _t("[Mail me]"),
            ),
        )

    def test_a_domain_without_a_dot_is_plain_text(self) -> None:
        result = parse_inline("ada@example", _LINE)
        self.assertEqual(result, (_t("ada@example"),))

    def test_a_six_letter_final_label_is_plain_text(self) -> None:
        # The language caps the domain suffix at five characters and
        # points longer ones at the macro form.
        result = parse_inline("ada@example.museum", _LINE)
        self.assertEqual(result, (_t("ada@example.museum"),))

    def test_a_five_letter_final_label_links(self) -> None:
        result = parse_inline("ada@example.email", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:ada@example.email",
                    LinkScheme.MAILTO,
                    _t("ada@example.email"),
                ),
            ),
        )

    def test_a_domain_continuing_past_a_valid_suffix_is_plain_text(
        self,
    ) -> None:
        # The reference retreats to ``ada@example.co`` here and links a
        # target the source never wrote. folio declines instead.
        result = parse_inline("ada@example.co.museum", _LINE)
        self.assertEqual(result, (_t("ada@example.co.museum"),))

    def test_a_hyphenated_label_after_a_valid_suffix_is_plain_text(
        self,
    ) -> None:
        result = parse_inline("ada@example.co-op", _LINE)
        self.assertEqual(result, (_t("ada@example.co-op"),))

    def test_an_address_after_a_colon_is_plain_text(self) -> None:
        result = parse_inline("mailto:ada@example.com", _LINE)
        self.assertEqual(result, (_t("mailto:ada@example.com"),))

    def test_an_address_inside_a_url_path_is_not_re_recognised(self) -> None:
        url = "https://x.com/ada@example.com"
        result = parse_inline(url, _LINE)
        self.assertEqual(result, (_link(url, LinkScheme.HTTPS, _t(url)),))

    def test_an_ssh_remote_is_plain_text(self) -> None:
        # ``git@github.com:org/repo.git`` is a remote, not an address.
        result = parse_inline("git@github.com:org/repo.git", _LINE)
        self.assertEqual(result, (_t("git@github.com:org/repo.git"),))

    def test_a_colon_followed_by_a_space_still_links(self) -> None:
        result = parse_inline("Write to ada@example.com: today", _LINE)
        self.assertEqual(
            result,
            (
                _t("Write to "),
                _link(
                    "mailto:ada@example.com",
                    LinkScheme.MAILTO,
                    _t("ada@example.com"),
                ),
                _t(": today"),
            ),
        )

    def test_an_address_inside_link_display_text_is_plain_text(self) -> None:
        # A nested bare URL raises, because the source reached for a
        # link. An address in a label is prose that happens to be
        # recognisable, so refusing it would call a well-formed
        # document malformed.
        result = parse_inline(
            "link:https://x.com[write to ada@example.com]", _LINE
        )
        self.assertEqual(
            result,
            (
                _link(
                    "https://x.com",
                    LinkScheme.HTTPS,
                    _t("write to ada@example.com"),
                ),
            ),
        )


class MailtoMacroTests(unittest.TestCase):
    """``mailto:`` activates only in its macro (bracketed) form."""

    def test_a_bare_mailto_prefix_is_plain_text(self) -> None:
        result = parse_inline("mailto:ada@example.com", _LINE)
        self.assertEqual(result, (_t("mailto:ada@example.com"),))

    def test_a_mailto_macro_with_a_label_links(self) -> None:
        result = parse_inline("mailto:ada@example.com[Mail me]", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:ada@example.com",
                    LinkScheme.MAILTO,
                    _t("Mail me"),
                ),
            ),
        )

    def test_a_mailto_macro_with_an_empty_label_shows_the_address(
        self,
    ) -> None:
        result = parse_inline("mailto:ada@example.com[]", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:ada@example.com",
                    LinkScheme.MAILTO,
                    _t("ada@example.com"),
                ),
            ),
        )

    def test_a_mailto_macro_label_supports_inline_formatting(self) -> None:
        result = parse_inline("mailto:ada@example.com[Mail *me*]", _LINE)
        link = result[0]
        assert isinstance(link, Link)
        self.assertEqual(link.text, (_t("Mail "), _bold(_t("me"))))

    def test_an_unclosed_mailto_label_is_plain_text(self) -> None:
        result = parse_inline("mailto:ada@example.com[oops", _LINE)
        self.assertEqual(result, (_t("mailto:ada@example.com[oops"),))

    def test_an_uppercase_mailto_prefix_is_plain_text(self) -> None:
        result = parse_inline("MAILTO:ada@example.com[Mail]", _LINE)
        self.assertEqual(result, (_t("MAILTO:ada@example.com[Mail]"),))

    def test_the_link_macro_still_accepts_a_mailto_url(self) -> None:
        result = parse_inline("link:mailto:ada@example.com[Mail]", _LINE)
        self.assertEqual(
            result,
            (
                _link(
                    "mailto:ada@example.com", LinkScheme.MAILTO, _t("Mail")
                ),
            ),
        )


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
        # validation still applies after the closing ``++`` — a
        # ``recipe://`` scheme is outside LinkScheme and is rejected.
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


class AttachmentMacroTests(unittest.TestCase):
    """``attachment:FILE[label]`` — the inline save link."""

    def test_macro_with_label_parses(self) -> None:
        (node,) = parse_inline("attachment:report.pdf[the report]", 1)
        self.assertEqual(
            node,
            AttachmentLink(
                filename="report.pdf",
                text=(Text(content="the report", source_line=1),),
                source_line=1,
            ),
        )

    def test_empty_label_falls_back_to_the_filename(self) -> None:
        (node,) = parse_inline("attachment:report.pdf[]", 1)
        assert isinstance(node, AttachmentLink)
        self.assertEqual(node.text, (Text(content="report.pdf", source_line=1),))

    def test_label_may_contain_inline_formatting(self) -> None:
        (node,) = parse_inline("attachment:a.pdf[the *report*]", 1)
        assert isinstance(node, AttachmentLink)
        self.assertEqual(
            node.text,
            (
                Text(content="the ", source_line=1),
                Bold(
                    children=(Text(content="report", source_line=1),),
                    source_line=1,
                ),
            ),
        )

    def test_macro_inside_prose_keeps_the_surrounding_text(self) -> None:
        nodes = parse_inline("see attachment:a.pdf[here] now", 1)
        self.assertEqual(
            [type(n).__name__ for n in nodes],
            ["Text", "AttachmentLink", "Text"],
        )

    def test_mid_word_prefix_is_not_a_macro(self) -> None:
        # The same word-boundary rule ``link:`` uses.
        nodes = parse_inline("myattachment:a.pdf[x]", 1)
        self.assertEqual([type(n).__name__ for n in nodes], ["Text"])

    def test_missing_brackets_raise(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:a.pdf", 1)
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_ATTACHMENT_MACRO)

    def test_unclosed_bracket_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:a.pdf[label", 1)
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_ATTACHMENT_MACRO)

    def test_nested_bracket_in_the_label_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:a.pdf[a[b]]", 1)
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_ATTACHMENT_MACRO)

    def test_empty_target_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:[label]", 1)
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_ATTACHMENT_MACRO)

    def test_target_with_whitespace_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:my file.pdf[label]", 1)
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_ATTACHMENT_MACRO)

    def test_target_with_path_separator_raises(self) -> None:
        for target in ("dir/a.pdf", "dir\\a.pdf"):
            with self.subTest(target=target):
                with self.assertRaises(ParseError) as ctx:
                    parse_inline(f"attachment:{target}[label]", 1)
                self.assertEqual(
                    ctx.exception.kind,
                    ParseErrorKind.BAD_ATTACHMENT_MACRO,
                )

    def test_error_carries_the_source_line(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:a.pdf", 7)
        self.assertEqual(ctx.exception.line, 7)


class ActivatableNestingTests(unittest.TestCase):
    """Activatable things do not nest — in either direction."""

    def test_link_macro_inside_an_attachment_label_raises(self) -> None:
        # A ``link:`` macro carries its own brackets, so the label's
        # no-nested-bracket rule catches it first — either way the
        # nesting is rejected, and the error names the outer macro.
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:a.pdf[link:https://x.test[y]]", 1)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_ATTACHMENT_MACRO,
        )

    def test_bare_url_inside_an_attachment_label_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:a.pdf[https://x.test]", 1)
        self.assertEqual(ctx.exception.kind, ParseErrorKind.BAD_LINK_MACRO)

    def test_attachment_inside_a_link_label_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("link:https://x.test[attachment:a.pdf[y]]", 1)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_ATTACHMENT_MACRO,
        )

    def test_attachment_inside_an_attachment_label_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline("attachment:a.pdf[attachment:b.pdf[y]]", 1)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.BAD_ATTACHMENT_MACRO,
        )


class InlineNestingDepthTests(unittest.TestCase):
    """Nesting is capped at :data:`MAX_INLINE_DEPTH` enclosing spans.

    Without the cap the scanner recurses one Python frame per level, so
    a long enough line raised ``RecursionError`` — an exception outside
    the :class:`ParseError` contract. These tests pin both edges of the
    cap and the three recursive descents it guards (the span dispatch
    table, a link's display text, an attachment macro's label).
    """

    def test_nesting_at_the_cap_is_accepted(self) -> None:
        nodes = parse_inline(_nested_spans(MAX_INLINE_DEPTH), _LINE)
        self.assertEqual(len(nodes), 1)

    def test_nesting_past_the_cap_is_rejected(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_inline(_nested_spans(MAX_INLINE_DEPTH + 1), _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.INLINE_NESTING_TOO_DEEP,
        )
        self.assertEqual(ctx.exception.line, _LINE)
        # Column 0 is the documented "whole line" sentinel.
        self.assertEqual(ctx.exception.column, 0)

    def test_sibling_spans_do_not_accumulate_depth(self) -> None:
        """``*a* *b* *c*`` is depth 1, however many siblings there are.

        The guard unwinds each level on the way out, so only *enclosing*
        spans count. A paragraph full of bold words must never trip a
        cap meant for nesting.
        """
        source = " ".join(["*w*"] * (MAX_INLINE_DEPTH * 4))
        nodes = parse_inline(source, _LINE)
        self.assertEqual(len(nodes), MAX_INLINE_DEPTH * 8 - 1)

    def test_monospace_body_costs_no_nesting_level(self) -> None:
        """Monospace is consumed verbatim, so it never recurses."""
        source = _nested_spans(MAX_INLINE_DEPTH, body="`code`")
        nodes = parse_inline(source, _LINE)
        self.assertEqual(len(nodes), 1)

    def test_link_display_text_counts_towards_the_cap(self) -> None:
        inner = _nested_spans(MAX_INLINE_DEPTH)
        with self.assertRaises(ParseError) as ctx:
            parse_inline(f"link:https://example.com[{inner}]", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.INLINE_NESTING_TOO_DEEP,
        )

    def test_attachment_label_counts_towards_the_cap(self) -> None:
        inner = _nested_spans(MAX_INLINE_DEPTH)
        with self.assertRaises(ParseError) as ctx:
            parse_inline(f"attachment:notes.pdf[{inner}]", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.INLINE_NESTING_TOO_DEEP,
        )

    def test_a_rejected_line_does_not_poison_the_next_parse(self) -> None:
        """Depth is per-call state, not module state."""
        with self.assertRaises(ParseError):
            parse_inline(_nested_spans(MAX_INLINE_DEPTH + 1), _LINE)
        self.assertEqual(
            parse_inline("*bold*", _LINE),
            (_bold(_t("bold")),),
        )

    def test_an_escaped_span_at_the_cap_is_accepted(self) -> None:
        """The trial costs no level of its own."""
        source = f"\\{_nested_spans(MAX_INLINE_DEPTH)}"
        self.assertEqual(
            parse_inline(source, _LINE),
            (_t(source[1:]),),
        )

    def test_an_escaped_span_past_the_cap_is_still_rejected(self) -> None:
        """The cap is a refusal to spend stack, not a claim about prose.

        A backslash cannot rescue it, and swallowing the error would
        change nothing: the deep nesting is still in the text for the
        ordinary scan to walk into.
        """
        with self.assertRaises(ParseError) as ctx:
            parse_inline(f"\\{_nested_spans(MAX_INLINE_DEPTH + 1)}", _LINE)
        self.assertEqual(
            ctx.exception.kind,
            ParseErrorKind.INLINE_NESTING_TOO_DEEP,
        )


class EscapeTests(unittest.TestCase):
    """A backslash suppresses the construct that would have followed it.

    The rule is *the recogniser*: a backslash escapes exactly when
    running the ordinary recognition at the next position succeeds, and
    the whole construct's source is then emitted literally.
    """

    def test_should_escape_bold(self) -> None:
        self.assertEqual(
            parse_inline("\\*bold*", _LINE),
            (_t("*bold*"),),
        )

    def test_should_escape_italic(self) -> None:
        self.assertEqual(
            parse_inline("\\_it_", _LINE),
            (_t("_it_"),),
        )

    def test_should_escape_monospace(self) -> None:
        self.assertEqual(
            parse_inline("\\`code`", _LINE),
            (_t("`code`"),),
        )

    def test_should_escape_a_role_span(self) -> None:
        self.assertEqual(
            parse_inline("\\[.underline]#x#", _LINE),
            (_t("[.underline]#x#"),),
        )

    def test_should_emit_the_whole_construct_not_just_the_marker(
        self,
    ) -> None:
        """The span's extent is literal, so nothing inside re-forms.

        Emitting only the opening marker would leave ``*b*`` to be
        recognised one position later, splitting the line into a literal
        head, a bold span and a literal tail.
        """
        self.assertEqual(
            parse_inline("\\*a *b* c*", _LINE),
            (_t("*a *b* c*"),),
        )

    def test_escaped_text_merges_into_one_text_node(self) -> None:
        self.assertEqual(
            parse_inline("a\\*b* c", _LINE),
            (_t("a*b* c"),),
        )

    def test_should_escape_a_bare_url(self) -> None:
        self.assertEqual(
            parse_inline("\\https://example.com[l]", _LINE),
            (_t("https://example.com[l]"),),
        )

    def test_should_escape_a_bare_address(self) -> None:
        self.assertEqual(
            parse_inline("\\ada@example.com", _LINE),
            (_t("ada@example.com"),),
        )

    def test_an_escaped_url_does_not_disarm_a_later_one(self) -> None:
        self.assertEqual(
            parse_inline("see \\https://a.com and https://b.com", _LINE),
            (
                _t("see https://a.com and "),
                _link(
                    "https://b.com",
                    LinkScheme.HTTPS,
                    _t("https://b.com"),
                ),
            ),
        )

    def test_should_escape_a_link_macro_without_raising(self) -> None:
        """The regression this feature exists for.

        ``link:https://x`` with no display text is a committed macro
        with a malformed remainder, so it raises today. Escaped, the
        author has said it is prose — the error is caught inside the
        trial and the macro's lexical extent is emitted literally.
        """
        self.assertEqual(
            parse_inline("\\link:https://example.com", _LINE),
            (_t("link:https://example.com"),),
        )

    def test_should_escape_a_malformed_link_macro_without_raising(
        self,
    ) -> None:
        self.assertEqual(
            parse_inline("\\link:nonsense[t]", _LINE),
            (_t("link:nonsense[t]"),),
        )

    def test_should_escape_inside_a_span_body(self) -> None:
        self.assertEqual(
            parse_inline("*a \\_b_ c*", _LINE),
            (_bold(_t("a _b_ c")),),
        )

    def test_should_escape_inside_a_link_display_text(self) -> None:
        self.assertEqual(
            parse_inline("https://example.com[\\*t*]", _LINE),
            (_link("https://example.com", LinkScheme.HTTPS, _t("*t*")),),
        )

    def test_should_emphasise_a_body_containing_literal_markers(
        self,
    ) -> None:
        """Escaping inside a doubled pair is how this is written."""
        self.assertEqual(
            parse_inline("**\\*b***", _LINE),
            (_bold(_t("*b*")),),
        )

    def test_an_escaped_region_is_emitted_verbatim(self) -> None:
        """A backslash inside an escaped region is one of the characters.

        Escape processing is suspended for the duration of a trial, so
        the inner marker cannot consume the closer the outer span needs.
        """
        self.assertEqual(
            parse_inline("\\*a\\*b*", _LINE),
            (_t("*a\\*b*"),),
        )


class EscapeIsDecidedByRecognitionTests(unittest.TestCase):
    """The backslash survives when nothing would have been recognised.

    Each pair here differs only in whether the construct after the
    backslash would have been a construct at all — which is the whole
    rule, and the reason there is no separate list of escapable
    characters.
    """

    def test_keeps_the_backslash_with_no_valid_closer(self) -> None:
        self.assertEqual(
            parse_inline("\\*bold", _LINE),
            (_t("\\*bold"),),
        )

    def test_keeps_the_backslash_in_a_windows_path(self) -> None:
        self.assertEqual(
            parse_inline("C:\\path\\to", _LINE),
            (_t("C:\\path\\to"),),
        )

    def test_keeps_the_backslash_when_the_closer_is_followed_by_a_word(
        self,
    ) -> None:
        self.assertEqual(
            parse_inline("a\\_b_c", _LINE),
            (_t("a\\_b_c"),),
        )

    def test_consumes_the_backslash_when_the_closer_is_valid(self) -> None:
        """The pair to the test above: only the closer differs."""
        self.assertEqual(
            parse_inline("a\\_b_", _LINE),
            (_t("a_b_"),),
        )

    def test_the_backslash_is_the_preceding_character(self) -> None:
        """``word_word_`` is prose; ``word\\_word_`` escapes emphasis.

        The marker can only open because the backslash sits in front of
        it, and the constrained opener test counts that as a word
        boundary.
        """
        self.assertEqual(
            parse_inline("word\\_word_", _LINE),
            (_t("word_word_"),),
        )

    def test_keeps_the_backslash_before_a_space(self) -> None:
        self.assertEqual(
            parse_inline("x \\* y", _LINE),
            (_t("x \\* y"),),
        )

    def test_keeps_a_trailing_lone_backslash(self) -> None:
        self.assertEqual(
            parse_inline("ends with\\", _LINE),
            (_t("ends with\\"),),
        )

    def test_an_escape_does_not_protect_a_closer(self) -> None:
        """The terminator test runs before the escape branch."""
        self.assertEqual(
            parse_inline("*bold \\* still*", _LINE),
            (_bold(_t("bold \\")), _t(" still*")),
        )

    def test_no_escape_processing_inside_a_monospace_body(self) -> None:
        self.assertEqual(
            parse_inline("`a\\*b`", _LINE),
            (_mono("a\\*b"),),
        )


class DoubledMarkerEscapeTests(unittest.TestCase):
    """A two-character marker takes one backslash or two.

    The language documents ``\\\\__func__`` for a doubled marker, so that
    spelling must not be the one that leaves a stray backslash on
    screen; a single backslash also works, because an escape suppresses
    the construct rather than half of it.
    """

    def test_one_backslash_escapes_a_doubled_marker(self) -> None:
        self.assertEqual(
            parse_inline("\\**b**", _LINE),
            (_t("**b**"),),
        )

    def test_two_backslashes_escape_a_doubled_marker(self) -> None:
        self.assertEqual(
            parse_inline("\\\\__func__", _LINE),
            (_t("__func__"),),
        )

    def test_two_backslashes_before_a_constrained_marker_keep_one(
        self,
    ) -> None:
        """The absorption needs a doubled marker; this is the boundary."""
        self.assertEqual(
            parse_inline("\\\\*bold*", _LINE),
            (_t("\\*bold*"),),
        )

    def test_a_lone_pair_of_backslashes_is_two_characters(self) -> None:
        """There is no backslash-escapes-backslash rule."""
        self.assertEqual(
            parse_inline("\\\\", _LINE),
            (_t("\\\\"),),
        )


class EscapeDivergesFromTheReferenceTests(unittest.TestCase):
    """Four escape differences from Asciidoctor that are decisions.

    Recorded as tests because the temptation is to "fix" them after
    comparing folio to the reference. The full reasoning is in the
    module docstring of ``asciidoc/inline_parser.py``; each test names
    the reference's answer and why folio's differs.
    """

    def test_escaping_the_attachment_macro_consumes_the_backslash(
        self,
    ) -> None:
        """Reference: keeps the backslash — it has no such macro.

        folio implements ``attachment:``, so folio must let it be
        escaped: an extension that is active must also be escapable.
        """
        self.assertEqual(
            parse_inline("\\attachment:notes.pdf[l]", _LINE),
            (_t("attachment:notes.pdf[l]"),),
        )

    def test_escaping_a_labelled_link_macro_produces_no_link(self) -> None:
        """Reference: keeps the backslash *and* still renders the link.

        That is an artefact of its substitution order — the bare-URL
        pass consumes the URL before the macro pass sees the escape —
        against its own documented rule that every inline macro can be
        escaped with a leading backslash. folio follows the rule.
        """
        self.assertEqual(
            parse_inline("\\link:https://example.com[t]", _LINE),
            (_t("link:https://example.com[t]"),),
        )

    def test_escaping_a_doubled_marker_suppresses_it_entirely(self) -> None:
        """Reference: half-escapes into ``*``, bold ``b``, ``*``.

        And it does so differently at line start than mid-word. An
        escape here suppresses the construct; it never half-fires.
        """
        self.assertEqual(
            parse_inline("a\\**b**c", _LINE),
            (_t("a**b**c"),),
        )

    def test_escaping_a_non_member_construct_keeps_the_backslash(
        self,
    ) -> None:
        """Reference: drops it, because ``#…#`` is a highlight there.

        A bare ``#…#`` is not in this subset, so the backslash prevented
        nothing and stays — the mirror image of the ``attachment:`` row
        above, and the same rule producing both.
        """
        self.assertEqual(
            parse_inline("\\#x#", _LINE),
            (_t("\\#x#"),),
        )


if __name__ == "__main__":
    unittest.main()
