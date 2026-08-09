"""Tests for the bundled GtkSourceView grammar ``language_spec.lang``.

Principles & invariants
-----------------------
* The grammar is a **data file, not a module**, so it has no import to
  exercise. Its regexes were previously covered only by
  ``test_note_editor.py``'s load-time smoke check ("the file parses and
  its ``<language id>`` resolves"), which a wrong — or dead — pattern
  passes unnoticed. This suite reads the committed file and pins each
  pattern's behaviour directly, so a construct the editor stops
  highlighting fails here rather than being noticed by eye.
* The patterns are exercised through Python's :mod:`re` engine, not
  GtkSourceView's. The two agree on the constructs this grammar uses
  (PCRE-style lookaround, character classes, non-capturing groups), and
  the trade is deliberate: a display-free, GTK-free suite that can
  assert *which* construct matched. GtkSourceView's context engine
  applies its tags anonymously, so an engine-level test could assert
  little beyond "something was highlighted". The residue this leaves —
  a pattern GRegex would reject outright — is covered by
  ``test_note_editor.py``: a grammar GtkSourceView cannot load yields no
  :class:`GtkSource.Language`, and the editor's tests fail.
* :data:`re.MULTILINE` is the mode that reproduces GtkSourceView's
  matching, whose contexts are evaluated per line: ``^`` and ``$`` must
  mean line boundaries, since almost every block-level pattern here is
  line-anchored.
* Matching alone is not the whole grammar — **order in the top-level
  dispatch context is semantics**, and it is not observable from any
  single pattern (``bare-url`` matches inside every bracketed link
  form; ``inline-bold`` matches a leading list bullet). The structural
  suite therefore pins the documented precedence rules alongside the
  wiring invariants (every context reachable, every ``style-ref``
  declared), which is what catches a context that was defined but never
  added to the dispatch list — dead in production, yet green in every
  behavioural test that names it directly.
* Context ids and fixture text are spelled as **literals** rather than
  routed through shared constants: a test that reads as a sentence at a
  glance is worth the duplication (the project's DAMP-over-DRY rule).
  The one exception is :data:`giruntime.ui.note_editor.LANGUAGE_ID`,
  imported so this suite pins the constant *against* the file instead of
  restating the id a third time. That import is also this module's only
  dependency on ``gi``.
"""

from __future__ import annotations

import importlib.resources
import re
import unittest
from enum import StrEnum
from typing import Final
from xml.etree import ElementTree

from giruntime.ui.note_editor import LANGUAGE_ID

# ---------------------------------------------------------------------------
# Grammar access
# ---------------------------------------------------------------------------


_GRAMMAR_PACKAGE: Final[str] = "giruntime.ui"
"""Package the grammar file ships in, read via :mod:`importlib.resources`.

The same access route :mod:`giruntime.ui._gresource` uses for the
compiled bundle. Reading the *source* ``.lang`` (rather than the
compiled ``folio.gresource``) is deliberate: this suite tests the
committed input, and needs no ``make resource`` to have run.
"""

_GRAMMAR_FILE: Final[str] = "language_spec.lang"
"""File name of the GtkSourceView language definition."""


class _Tag(StrEnum):
    """Element names in the GtkSourceView language-definition schema.

    A fixed vocabulary defined by GtkSourceView, so it is an enum rather
    than loose strings: every lookup in this module names an element
    through a member, and a typo becomes an attribute error instead of a
    silently-empty :meth:`ElementTree.Element.find`.
    """

    STYLES = "styles"
    STYLE = "style"
    DEFINITIONS = "definitions"
    CONTEXT = "context"
    INCLUDE = "include"
    MATCH = "match"
    START = "start"
    END = "end"


class _Attr(StrEnum):
    """Attribute names in the GtkSourceView language-definition schema.

    Same rationale as :class:`_Tag`: a closed vocabulary owned by the
    schema, named once here rather than quoted at each call site.
    """

    ID = "id"
    REF = "ref"
    STYLE_REF = "style-ref"


def _grammar_root() -> ElementTree.Element:
    """Parse the committed grammar and return its ``<language>`` root."""
    text = (
        importlib.resources.files(_GRAMMAR_PACKAGE)
        .joinpath(_GRAMMAR_FILE)
        .read_text(encoding="utf-8")
    )
    return ElementTree.fromstring(text)


_ROOT: Final[ElementTree.Element] = _grammar_root()
"""The parsed grammar, read once per process.

The file is immutable input, so re-parsing it per test would buy
nothing; a module-level parse also means a malformed grammar fails
every test in this module at import rather than one assertion at a
time.
"""


def _child(parent: ElementTree.Element, tag: _Tag) -> ElementTree.Element:
    """Return ``parent``'s ``tag`` child, or raise if it is absent.

    :meth:`ElementTree.Element.find` returns ``None`` for a missing
    element; raising instead keeps the callers below free of ``None``
    handling for a structure that is malformed if the element is
    missing at all.
    """
    element = parent.find(tag)
    if element is None:
        raise LookupError(f"<{parent.tag}> has no <{tag}> child")
    return element


def _defined_contexts() -> tuple[ElementTree.Element, ...]:
    """Return the ``<context>`` elements declared under ``<definitions>``.

    Top-level only: the ``sub-pattern`` contexts nested inside a
    region's ``<include>`` are styling directives for that region, not
    contexts the dispatch list can reference.
    """
    return tuple(_child(_ROOT, _Tag.DEFINITIONS).findall(_Tag.CONTEXT))


def _dispatch_context() -> ElementTree.Element:
    """Return the top-level context that dispatches to all the others.

    GtkSourceView requires this context to carry the language's own id,
    which is what identifies it here.
    """
    for context in _defined_contexts():
        if context.get(_Attr.ID) == LANGUAGE_ID:
            return context
    raise LookupError(f"no top-level context with id {LANGUAGE_ID!r}")


def _context_by_id(context_id: str) -> ElementTree.Element:
    """Return the ``<context>`` element declaring ``context_id``."""
    for context in _defined_contexts():
        if context.get(_Attr.ID) == context_id:
            return context
    raise LookupError(f"no context with id {context_id!r}")


def _dispatched_ids() -> tuple[str, ...]:
    """Return the context ids the dispatch list references, in order.

    Order is meaningful — GtkSourceView tries the referenced contexts in
    the order they appear — so this is a tuple, not a set.
    """
    include = _child(_dispatch_context(), _Tag.INCLUDE)
    return tuple(
        ref
        for context in include.findall(_Tag.CONTEXT)
        if (ref := context.get(_Attr.REF)) is not None
    )


def _pattern(context_id: str, tag: _Tag) -> re.Pattern[str]:
    """Compile the ``tag`` regex of context ``context_id``.

    ``re.MULTILINE`` mirrors GtkSourceView's line-oriented matching:
    without it, ``^`` and ``$`` would mean "start/end of the whole
    fixture" and every line-anchored block pattern would be tested
    against the wrong rule.
    """
    element = _child(_context_by_id(context_id), tag)
    if element.text is None:
        raise LookupError(f"context {context_id!r} has an empty <{tag}>")
    return re.compile(element.text, re.MULTILINE)


def _first_match(context_id: str, text: str) -> str | None:
    """Return the text a ``<match>`` context highlights in ``text``.

    ``None`` means the context does not fire — the assertion a negative
    fixture makes. Returning the matched *substring* rather than a bool
    is what lets a positive fixture pin the extent of the highlight,
    which for the marker-only contexts (list bullets, cell separators)
    is the whole point.
    """
    found = _pattern(context_id, _Tag.MATCH).search(text)
    return found.group(0) if found else None


def _first_start(context_id: str, text: str) -> str | None:
    """Return the text a region context's ``<start>`` matches, if any."""
    found = _pattern(context_id, _Tag.START).search(text)
    return found.group(0) if found else None


def _first_end(context_id: str, text: str) -> str | None:
    """Return the text a region context's ``<end>`` matches, if any."""
    found = _pattern(context_id, _Tag.END).search(text)
    return found.group(0) if found else None


# ---------------------------------------------------------------------------
# Structure: identity, wiring, precedence
# ---------------------------------------------------------------------------


class GrammarIdentityTests(unittest.TestCase):
    """The file's own identity, as the loader expects to find it."""

    def test_language_id_matches_the_constant_the_editor_looks_up(self) -> None:
        # note_editor.load_asciidoc_language() asks the manager for
        # LANGUAGE_ID; if the file's id drifted, that lookup returns
        # None and the editor silently falls back to no highlighting.
        self.assertEqual(_ROOT.get(_Attr.ID), LANGUAGE_ID)

    def test_dispatch_context_is_named_after_the_language(self) -> None:
        # GtkSourceView enters a language through the context whose id
        # equals the language id; a differently-named top-level context
        # would leave every definition below unreachable.
        self.assertEqual(_dispatch_context().get(_Attr.ID), LANGUAGE_ID)


class GrammarWiringTests(unittest.TestCase):
    """Every declared part is reachable, and every reference resolves.

    These are the failures no behavioural test can see: a context whose
    pattern is perfect but which the dispatch list never reaches is dead
    in the editor and green everywhere else.
    """

    def test_every_defined_context_is_reachable_from_the_dispatch_list(self) -> None:
        defined = {
            context_id
            for context in _defined_contexts()
            if (context_id := context.get(_Attr.ID)) is not None
            and context_id != LANGUAGE_ID
        }
        self.assertEqual(defined - set(_dispatched_ids()), set())

    def test_every_dispatched_reference_names_a_defined_context(self) -> None:
        defined = {context.get(_Attr.ID) for context in _defined_contexts()}
        self.assertEqual(set(_dispatched_ids()) - defined, set())

    def test_dispatch_list_references_each_context_once(self) -> None:
        # A repeated ref is not an error GtkSourceView reports, but it
        # means one of the two positions is doing nothing — and which
        # one is doing nothing depends on the precedence rules below.
        dispatched = _dispatched_ids()
        self.assertEqual(len(dispatched), len(set(dispatched)))

    def test_every_style_reference_resolves_to_a_declared_style(self) -> None:
        # An unknown style-ref is not a load failure: GtkSourceView
        # simply renders that context unstyled, so the construct stops
        # being highlighted without anything being logged.
        declared = {
            style.get(_Attr.ID) for style in _child(_ROOT, _Tag.STYLES).findall(_Tag.STYLE)
        }
        referenced = {
            style_ref
            for element in _ROOT.iter()
            if (style_ref := element.get(_Attr.STYLE_REF)) is not None
        }
        self.assertEqual(referenced - declared, set())

    def test_every_declared_style_is_referenced_by_some_context(self) -> None:
        # The reverse direction: a style nobody references is a leftover
        # from a removed construct, and the next person to add one will
        # reasonably assume it is already wired up.
        declared = {
            style.get(_Attr.ID) for style in _child(_ROOT, _Tag.STYLES).findall(_Tag.STYLE)
        }
        referenced = {
            style_ref
            for element in _ROOT.iter()
            if (style_ref := element.get(_Attr.STYLE_REF)) is not None
        }
        self.assertEqual(declared - referenced, set())

    def test_every_pattern_compiles(self) -> None:
        # Cheap guard against a malformed regex reaching the file: here
        # it is a test failure naming the context, rather than a
        # GtkSourceView load error at editor construction.
        for context in _defined_contexts():
            context_id = context.get(_Attr.ID)
            for tag in (_Tag.MATCH, _Tag.START, _Tag.END):
                if context.find(tag) is None:
                    continue
                with self.subTest(context=context_id, pattern=tag):
                    assert context_id is not None
                    self.assertIsInstance(
                        _pattern(context_id, tag),
                        re.Pattern,
                    )


class GrammarPrecedenceTests(unittest.TestCase):
    """Order in the dispatch list, where order changes what is matched.

    Each rule here is one the grammar's own comments state; the
    patterns overlap by design, and the ordering is the only thing
    resolving them.
    """

    def test_list_bullets_are_tried_before_inline_bold(self) -> None:
        # "* item" would otherwise open a bold span at column 0.
        dispatched = _dispatched_ids()
        self.assertLess(
            dispatched.index("unordered-list-bullet"),
            dispatched.index("inline-bold"),
        )

    def test_bracketed_link_forms_are_tried_before_the_bare_url(self) -> None:
        # bare-url matches the URL inside every bracketed form (see
        # BareUrlPatternTests), so if it were tried first it would
        # consume the destination and strand the label.
        dispatched = _dispatched_ids()
        bare_url = dispatched.index("bare-url")
        self.assertLess(dispatched.index("link-macro"), bare_url)
        self.assertLess(dispatched.index("attachment-macro"), bare_url)
        self.assertLess(dispatched.index("url-with-text"), bare_url)

    def test_the_email_context_is_tried_after_the_url_contexts(self) -> None:
        # An address inside a URL path must be coloured as part of the
        # URL, not re-claimed as a mail link.
        dispatched = _dispatched_ids()
        self.assertLess(dispatched.index("bare-url"), dispatched.index("email"))

    def test_single_line_admonition_is_tried_before_inline_styles(self) -> None:
        # Its body is prose, and prose contexts must not claim the
        # "NOTE:" prefix before the admonition context sees the line.
        dispatched = _dispatched_ids()
        self.assertLess(
            dispatched.index("single-admonition"),
            dispatched.index("inline-bold"),
        )

    def test_doubled_forms_are_tried_before_their_constrained_twins(
        self,
    ) -> None:
        # "**bold**" must highlight as one span. Tried the other way
        # round, the constrained context would claim an empty span at
        # the first pair of asterisks and leave the word bare.
        dispatched = _dispatched_ids()
        for doubled, single in (
            ("inline-bold-unconstrained", "inline-bold"),
            ("inline-italic-unconstrained", "inline-italic"),
            ("inline-monospace-unconstrained", "inline-monospace"),
        ):
            with self.subTest(doubled):
                self.assertLess(
                    dispatched.index(doubled), dispatched.index(single)
                )

    def test_code_block_is_tried_first_of_all(self) -> None:
        # Inside the fence nothing is interpreted, matching the
        # parser's verbatim treatment — only reachable if the region
        # opens before any other context can claim its content.
        self.assertEqual(_dispatched_ids()[0], "code-block")


# ---------------------------------------------------------------------------
# Block-level constructs
# ---------------------------------------------------------------------------


class CodeBlockPatternTests(unittest.TestCase):
    def test_fence_matches_a_bare_four_hyphen_line(self) -> None:
        self.assertEqual(_first_start("code-block", "----"), "----")

    def test_fence_closes_on_the_same_shape_it_opens_on(self) -> None:
        self.assertEqual(_first_end("code-block", "----"), "----")

    def test_fence_rejects_a_longer_hyphen_run(self) -> None:
        self.assertIsNone(_first_start("code-block", "-----"))

    def test_fence_rejects_trailing_content(self) -> None:
        self.assertIsNone(_first_start("code-block", "---- python"))


class SourceDirectivePatternTests(unittest.TestCase):
    def test_matches_a_directive_naming_a_language(self) -> None:
        self.assertEqual(
            _first_match("source-directive", "[source,python]"),
            "[source,python]",
        )

    def test_matches_a_bare_source_directive(self) -> None:
        self.assertEqual(_first_match("source-directive", "[source]"), "[source]")

    def test_rejects_a_different_directive_starting_with_source(self) -> None:
        self.assertIsNone(_first_match("source-directive", "[sourced]"))


class HeadingPatternTests(unittest.TestCase):
    def test_matches_a_level_one_heading(self) -> None:
        self.assertEqual(_first_match("heading", "= Document title"), "= Document title")

    def test_matches_the_deepest_supported_level(self) -> None:
        self.assertEqual(_first_match("heading", "====== Level six"), "====== Level six")

    def test_rejects_a_seventh_level(self) -> None:
        # Seven equals signs is not a heading in the subset; the parser
        # reports it, and the editor must not suggest otherwise.
        self.assertIsNone(_first_match("heading", "======= Level seven"))

    def test_rejects_a_marker_with_no_separating_space(self) -> None:
        self.assertIsNone(_first_match("heading", "==NoSpace"))

    def test_rejects_a_marker_with_no_title_text(self) -> None:
        self.assertIsNone(_first_match("heading", "== "))


class ListBulletPatternTests(unittest.TestCase):
    def test_unordered_bullet_highlights_the_marker_only(self) -> None:
        # Only the marker is styled so inline emphasis inside the item
        # still reaches the inline contexts.
        self.assertEqual(_first_match("unordered-list-bullet", "* Item text"), "* ")

    def test_unordered_bullet_matches_a_nested_marker_run(self) -> None:
        self.assertEqual(_first_match("unordered-list-bullet", "** Nested item"), "** ")

    def test_unordered_bullet_rejects_a_bold_span_at_column_zero(self) -> None:
        self.assertIsNone(_first_match("unordered-list-bullet", "*bold* opens the line"))

    def test_unordered_bullet_rejects_an_indented_marker(self) -> None:
        # Nesting is expressed by repeating the marker, not by
        # indenting it — an indented line is not a list item.
        self.assertIsNone(_first_match("unordered-list-bullet", "  * Indented"))

    def test_ordered_bullet_highlights_the_marker_only(self) -> None:
        self.assertEqual(_first_match("ordered-list-bullet", ". Item text"), ". ")

    def test_ordered_bullet_rejects_explicit_numbering(self) -> None:
        # "1." is AsciiDoc's explicit-number form, outside this subset.
        self.assertIsNone(_first_match("ordered-list-bullet", "1. Item text"))

    def test_ordered_bullet_rejects_a_sentence_period(self) -> None:
        self.assertIsNone(_first_match("ordered-list-bullet", "One sentence. Then more."))


class ImageMacroPatternTests(unittest.TestCase):
    def test_matches_a_block_image_macro(self) -> None:
        self.assertEqual(
            _first_match("image-macro", "image::diagram.png[]"),
            "image::diagram.png[]",
        )

    def test_matches_a_block_image_macro_with_attributes(self) -> None:
        self.assertEqual(
            _first_match("image-macro", "image::diagram.png[width=50]"),
            "image::diagram.png[width=50]",
        )

    def test_rejects_the_single_colon_inline_form(self) -> None:
        # Inline images are not part of the supported subset.
        self.assertIsNone(_first_match("image-macro", "image:diagram.png[]"))

    def test_rejects_a_macro_embedded_in_prose(self) -> None:
        self.assertIsNone(_first_match("image-macro", "see image::diagram.png[] above"))


class AttachmentTableMacroPatternTests(unittest.TestCase):
    def test_matches_the_bare_block_macro(self) -> None:
        self.assertEqual(
            _first_match("attachment-table-macro", "attachments::[]"),
            "attachments::[]",
        )

    def test_matches_the_block_macro_with_a_cols_attribute(self) -> None:
        self.assertEqual(
            _first_match("attachment-table-macro", 'attachments::[cols="name,size"]'),
            'attachments::[cols="name,size"]',
        )

    def test_rejects_the_singular_spelling(self) -> None:
        # "attachment:" is the inline save-link macro; the table macro
        # is plural and takes two colons.
        self.assertIsNone(_first_match("attachment-table-macro", "attachment::[]"))


class TablePatternTests(unittest.TestCase):
    def test_cols_directive_matches_a_quoted_proportion_list(self) -> None:
        self.assertEqual(
            _first_match("cols-directive", '[cols="1,2,1"]'),
            '[cols="1,2,1"]',
        )

    def test_cols_directive_rejects_unquoted_proportions(self) -> None:
        self.assertIsNone(_first_match("cols-directive", "[cols=1,2,1]"))

    def test_fence_matches_the_table_delimiter(self) -> None:
        self.assertEqual(_first_match("table-fence", "|==="), "|===")

    def test_fence_rejects_a_longer_delimiter(self) -> None:
        self.assertIsNone(_first_match("table-fence", "|===="))

    def test_cell_separator_matches_the_pipe_opening_a_row(self) -> None:
        self.assertEqual(_first_match("table-cell-separator", "|First cell"), "|")

    def test_cell_separator_matches_a_pipe_between_cells(self) -> None:
        self.assertEqual(_first_match("table-cell-separator", "|First |Second"), "|")

    def test_cell_separator_also_matches_a_pipe_inside_prose(self) -> None:
        # Pins current behaviour, which is NOT what the context's own
        # comment claims ("anchored to the start of a line so paragraph
        # prose containing a literal | outside a table is unaffected").
        # The second alternative, `(?<=[^|])\|(?=.*$)`, carries no line
        # anchor, so a pipe in ordinary prose is styled as a cell
        # separator. Recorded here rather than quietly corrected: the
        # fix is a grammar change, not a test change.
        self.assertEqual(_first_match("table-cell-separator", "a | b, in prose"), "|")


class AdmonitionPatternTests(unittest.TestCase):
    def test_fence_matches_the_four_equals_delimiter(self) -> None:
        self.assertEqual(_first_match("admonition-fence", "===="), "====")

    def test_fence_rejects_a_longer_delimiter(self) -> None:
        self.assertIsNone(_first_match("admonition-fence", "====="))

    def test_directive_matches_each_known_kind(self) -> None:
        # The five kinds are a closed set — the parser rejects any
        # other label, so the highlighter must not encourage one.
        for kind in ("NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION"):
            with self.subTest(kind=kind):
                self.assertEqual(_first_match("admonition-directive", f"[{kind}]"), f"[{kind}]")

    def test_directive_rejects_an_unknown_kind(self) -> None:
        self.assertIsNone(_first_match("admonition-directive", "[FIXME]"))

    def test_directive_rejects_a_lowercased_kind(self) -> None:
        self.assertIsNone(_first_match("admonition-directive", "[Note]"))

    def test_single_line_form_matches_kind_and_body(self) -> None:
        self.assertEqual(
            _first_match("single-admonition", "NOTE: remember this"),
            "NOTE: remember this",
        )

    def test_single_line_form_requires_whitespace_after_the_colon(self) -> None:
        self.assertIsNone(_first_match("single-admonition", "NOTE:remember this"))

    def test_single_line_form_rejects_a_kind_mid_sentence(self) -> None:
        # The construct owns the whole line; "as the NOTE: above says"
        # is prose.
        self.assertIsNone(_first_match("single-admonition", "as the NOTE: above says"))


class QuotePatternTests(unittest.TestCase):
    def test_fence_matches_the_four_underscore_delimiter(self) -> None:
        self.assertEqual(_first_match("quote-fence", "____"), "____")

    def test_fence_rejects_a_longer_delimiter(self) -> None:
        self.assertIsNone(_first_match("quote-fence", "_____"))

    def test_directive_matches_the_bare_form(self) -> None:
        self.assertEqual(_first_match("quote-directive", "[quote]"), "[quote]")

    def test_directive_matches_an_attribution(self) -> None:
        self.assertEqual(
            _first_match("quote-directive", "[quote, Ada Lovelace, Notes]"),
            "[quote, Ada Lovelace, Notes]",
        )

    def test_directive_rejects_a_different_directive_starting_with_quote(self) -> None:
        self.assertIsNone(_first_match("quote-directive", "[quoted]"))


# ---------------------------------------------------------------------------
# Inline constructs
# ---------------------------------------------------------------------------


class InlineEmphasisPatternTests(unittest.TestCase):
    def test_bold_matches_a_span_including_its_delimiters(self) -> None:
        self.assertEqual(_first_match("inline-bold", "a *bold* word"), "*bold*")

    def test_bold_rejects_delimiters_padded_with_spaces(self) -> None:
        # Matches the parser's rule: "* foo *" is plain prose.
        self.assertIsNone(_first_match("inline-bold", "* not bold *"))

    def test_bold_rejects_asterisks_inside_a_word(self) -> None:
        self.assertIsNone(_first_match("inline-bold", "file*name*here"))

    def test_bold_rejects_a_closer_followed_by_a_word_character(self) -> None:
        # The parser reads "*bold*x" as prose; the editor must not
        # colour what the reader will not see emphasised.
        self.assertIsNone(_first_match("inline-bold", "a *bold*x word"))

    def test_bold_rejects_an_opener_after_a_colon_or_semicolon(self) -> None:
        for source in ("see:*bold*", "see;*bold*"):
            with self.subTest(source):
                self.assertIsNone(_first_match("inline-bold", source))

    def test_italic_matches_a_span_including_its_delimiters(self) -> None:
        self.assertEqual(_first_match("inline-italic", "a _quiet_ word"), "_quiet_")

    def test_italic_rejects_underscores_inside_an_identifier(self) -> None:
        # The case the word-boundary lookaround exists for: snake_case
        # in prose must not turn into an italic span.
        self.assertIsNone(_first_match("inline-italic", "call snake_case_name here"))

    def test_italic_rejects_delimiters_padded_with_spaces(self) -> None:
        self.assertIsNone(_first_match("inline-italic", "_ not italic _"))

    def test_doubled_bold_matches_inside_a_word(self) -> None:
        self.assertEqual(
            _first_match("inline-bold-unconstrained", "a**b**c"), "**b**"
        )

    def test_doubled_italic_matches_inside_a_word(self) -> None:
        self.assertEqual(
            _first_match("inline-italic-unconstrained", "word__it__word"),
            "__it__",
        )

    def test_doubled_forms_reject_an_unclosed_opener(self) -> None:
        self.assertIsNone(
            _first_match("inline-bold-unconstrained", "a **unclosed run")
        )

    def test_strikethrough_matches_the_role_prefixed_span(self) -> None:
        self.assertEqual(
            _first_match("inline-strikethrough", "was [.line-through]#gone# then"),
            "[.line-through]#gone#",
        )

    def test_strikethrough_rejects_a_bare_hash_span(self) -> None:
        # Without the role prefix "#x#" is not a supported construct.
        self.assertIsNone(_first_match("inline-strikethrough", "was #gone# then"))

    def test_underline_matches_the_role_prefixed_span(self) -> None:
        self.assertEqual(
            _first_match("inline-underline", "an [.underline]#emphasis# here"),
            "[.underline]#emphasis#",
        )

    def test_underline_rejects_a_bare_hash_span(self) -> None:
        self.assertIsNone(_first_match("inline-underline", "an #emphasis# here"))


class InlineMonospacePatternTests(unittest.TestCase):
    def test_matches_a_backtick_pair_including_its_delimiters(self) -> None:
        self.assertEqual(_first_match("inline-monospace", "run `make test` now"), "`make test`")

    def test_rejects_an_unclosed_backtick(self) -> None:
        self.assertIsNone(_first_match("inline-monospace", "an unclosed ` backtick"))

    def test_rejects_backticks_inside_a_word(self) -> None:
        # Constrained, like bold and italic: "a`b`c" is prose.
        self.assertIsNone(_first_match("inline-monospace", "a`b`c"))

    def test_doubled_backticks_match_inside_a_word(self) -> None:
        self.assertEqual(
            _first_match("inline-monospace-unconstrained", "a``m``b"), "``m``"
        )

    def test_does_not_span_a_line_break(self) -> None:
        # The parser treats inline constructs as line-local; a backtick
        # left open must not paint the rest of the note.
        self.assertIsNone(_first_match("inline-monospace", "open `here\nclosed` there"))


# ---------------------------------------------------------------------------
# Links and macros
# ---------------------------------------------------------------------------


class BareUrlPatternTests(unittest.TestCase):
    def test_matches_an_https_url_in_prose(self) -> None:
        self.assertEqual(
            _first_match("bare-url", "see https://example.com for more"),
            "https://example.com",
        )

    def test_does_not_match_a_bare_mailto_prefix(self) -> None:
        # The language activates mailto: only in its macro form, so a
        # bare prefix is prose and must not be coloured as a link.
        self.assertIsNone(
            _first_match("bare-url", "write to mailto:ada@example.com")
        )

    def test_keeps_an_underscore_inside_the_url(self) -> None:
        self.assertEqual(
            _first_match(
                "bare-url", "https://en.wikipedia.org/wiki/Naive_set_theory"
            ),
            "https://en.wikipedia.org/wiki/Naive_set_theory",
        )

    def test_stops_before_a_full_stop_that_ends_a_sentence(self) -> None:
        self.assertEqual(
            _first_match("bare-url", "See https://example.com."),
            "https://example.com",
        )

    def test_stops_at_a_closing_bracket(self) -> None:
        self.assertEqual(
            _first_match("bare-url", "https://example.com/a]b"),
            "https://example.com/a",
        )

    def test_over_colours_a_paired_doubled_marker(self) -> None:
        # Known divergence, pinned rather than endorsed: the parser
        # ends the URL at the ``**`` because it pairs later on the
        # line, which a single regex cannot ask about.
        self.assertEqual(
            _first_match("bare-url", "https://example.com/a**b**c"),
            "https://example.com/a**b**c",
        )

    def test_rejects_a_scheme_outside_the_supported_set(self) -> None:
        self.assertIsNone(_first_match("bare-url", "ftp://example.com/pub"))

    def test_stops_at_the_bracket_opening_a_label(self) -> None:
        # This is the overlap the dispatch order resolves: on its own
        # the pattern claims the destination of a labelled link and
        # leaves the label behind, so url-with-text must be tried first
        # (see GrammarPrecedenceTests).
        self.assertEqual(
            _first_match("bare-url", "https://example.com[Example]"),
            "https://example.com",
        )


class EmailPatternTests(unittest.TestCase):
    """The ``email`` context mirrors the parser's address rule."""

    def test_matches_a_bare_address(self) -> None:
        self.assertEqual(
            _first_match("email", "write to ada@example.com today"),
            "ada@example.com",
        )

    def test_stops_before_a_full_stop_that_ends_a_sentence(self) -> None:
        self.assertEqual(
            _first_match("email", "write to ada@example.com."),
            "ada@example.com",
        )

    def test_rejects_a_six_letter_final_label(self) -> None:
        self.assertIsNone(_first_match("email", "ada@example.museum"))

    def test_rejects_a_domain_continuing_past_a_valid_suffix(self) -> None:
        self.assertIsNone(_first_match("email", "ada@example.co.museum"))

    def test_rejects_an_address_after_a_mailto_prefix(self) -> None:
        self.assertIsNone(_first_match("email", "mailto:ada@example.com"))

    def test_rejects_a_domain_without_a_dot(self) -> None:
        self.assertIsNone(_first_match("email", "ada@example"))


class UrlWithTextPatternTests(unittest.TestCase):
    def test_start_matches_the_destination_up_to_the_label(self) -> None:
        self.assertEqual(
            _first_start("url-with-text", "https://example.com[Example]"),
            "https://example.com[",
        )

    def test_end_matches_the_closing_bracket(self) -> None:
        self.assertEqual(_first_end("url-with-text", "Example]"), "]")

    def test_start_rejects_a_url_with_no_label(self) -> None:
        self.assertIsNone(_first_start("url-with-text", "https://example.com"))


class LinkMacroPatternTests(unittest.TestCase):
    def test_start_matches_the_plain_macro_form(self) -> None:
        self.assertEqual(
            _first_start("link-macro", "link:https://example.com[Example]"),
            "link:https://example.com[",
        )

    def test_start_matches_the_passthrough_form(self) -> None:
        # ++...++ is what lets a destination carry '_' or '#' — the
        # characters that otherwise terminate a URL — as one unit.
        self.assertEqual(
            _first_start("link-macro", "link:++https://example.com/a_b#c++[Example]"),
            "link:++https://example.com/a_b#c++[",
        )

    def test_start_rejects_a_macro_with_no_scheme(self) -> None:
        self.assertIsNone(_first_start("link-macro", "link:[Example]"))

    def test_start_rejects_a_word_ending_in_link(self) -> None:
        # The lookbehind is what keeps "blink:" from opening a macro.
        self.assertIsNone(_first_start("link-macro", "blink:https://example.com[Example]"))


class AttachmentMacroPatternTests(unittest.TestCase):
    def test_start_matches_the_inline_save_link(self) -> None:
        self.assertEqual(
            _first_start("attachment-macro", "attachment:notes.pdf[The notes]"),
            "attachment:notes.pdf[",
        )

    def test_start_rejects_a_target_containing_a_path_separator(self) -> None:
        # Attachment names are flat; the parser rejects a path, and the
        # highlighter must not make one look supported.
        self.assertIsNone(_first_start("attachment-macro", "attachment:sub/notes.pdf[The notes]"))

    def test_start_rejects_the_word_followed_by_prose(self) -> None:
        self.assertIsNone(_first_start("attachment-macro", "attachment: see the list below"))

    def test_end_matches_the_closing_bracket(self) -> None:
        self.assertEqual(_first_end("attachment-macro", "The notes]"), "]")


class EscapePatternTests(unittest.TestCase):
    """The escape token: a backslash plus the opener it suppresses.

    The context colours the token, not the whole escaped construct —
    once the opener is claimed here, the inline contexts cannot match
    the rest of the run, which is the behaviour that matters.
    """

    def test_matches_a_backslash_before_a_constrained_marker(self) -> None:
        self.assertEqual(_first_match("escape", "\\*bold*"), "\\*")

    def test_matches_a_backslash_before_a_doubled_marker(self) -> None:
        # The doubled alternatives are listed first, so the token is the
        # whole marker rather than one of its two characters.
        self.assertEqual(_first_match("escape", "\\**bold**"), "\\**")

    def test_matches_two_backslashes_before_a_doubled_marker(self) -> None:
        # The language's own spelling for a two-character marker; the
        # parser absorbs both, so both are part of the token.
        self.assertEqual(_first_match("escape", "\\\\__func__"), "\\\\__")

    def test_matches_a_backslash_before_a_role_span(self) -> None:
        self.assertEqual(
            _first_match("escape", "\\[.underline]#x#"),
            "\\[.underline]#",
        )

    def test_matches_a_backslash_before_a_url(self) -> None:
        self.assertEqual(
            _first_match("escape", "see \\https://example.com"),
            "\\https://",
        )

    def test_matches_a_backslash_before_a_macro_prefix(self) -> None:
        self.assertEqual(
            _first_match("escape", "\\attachment:notes.pdf[l]"),
            "\\attachment:",
        )

    def test_does_not_match_a_backslash_before_ordinary_text(self) -> None:
        self.assertIsNone(_first_match("escape", "C:\\temp and D:\\data"))

    def test_does_not_match_a_lone_trailing_backslash(self) -> None:
        self.assertIsNone(_first_match("escape", "ends with\\"))

    def test_known_divergence_matches_an_opener_that_never_closes(
        self,
    ) -> None:
        """The parser keeps this backslash; the grammar cannot tell.

        Deciding it needs the closer search that makes ``\\*bold`` prose,
        and a context regex cannot ask whether a marker has a partner
        later on the line. Pinned as current behaviour, not endorsed —
        the same shape as the bare-url doubled-marker divergence.
        """
        self.assertEqual(_first_match("escape", "\\*bold"), "\\*")


if __name__ == "__main__":
    unittest.main()
