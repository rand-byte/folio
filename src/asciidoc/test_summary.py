"""Tests for :mod:`asciidoc.summary`."""

from __future__ import annotations

import unittest

from asciidoc.ast import Document, UnreadBlock
from asciidoc.summary import _snippet_of, derive_summary
from config.defaults import SNIPPET_MAX_CHARS, UNTITLED
from enums import ParseErrorKind, UnreadScope
from models.note import NoteSummary


class DeriveSummaryTitleTests(unittest.TestCase):
    """The title is read straight off the parsed level-0 heading."""

    def test_title_present(self) -> None:
        summary = derive_summary("= Hello\n\nBody")
        self.assertEqual(summary.title, "Hello")

    def test_title_with_inline_formatting_is_flattened(self) -> None:
        summary = derive_summary("= A *bold* word\n\nBody")
        self.assertEqual(summary.title, "A bold word")

    def test_title_absent_paragraph_first(self) -> None:
        summary = derive_summary("Just some text\n")
        self.assertEqual(summary.title, UNTITLED)

    def test_title_absent_section_first(self) -> None:
        summary = derive_summary("== Section\n\nBody")
        self.assertEqual(summary.title, UNTITLED)

    def test_empty_source(self) -> None:
        summary = derive_summary("")
        self.assertEqual(
            summary,
            NoteSummary(title=UNTITLED, snippet="", tags=()),
        )


class DeriveSummarySnippetTests(unittest.TestCase):
    """The snippet collects prose and drops structure, AST-based."""

    def test_skips_title_uses_first_paragraph(self) -> None:
        summary = derive_summary("= Hello\n\nThe body.")
        self.assertEqual(summary.snippet, "The body.")

    def test_multiline_paragraph_joined_with_spaces(self) -> None:
        summary = derive_summary("First line.\nSecond line.\n")
        self.assertEqual(summary.snippet, "First line. Second line.")

    def test_hard_break_collapses_to_a_space_in_snippet(self) -> None:
        # Snippets are one-line previews, so a `` +`` hard break collapses
        # to a single space exactly like a soft break — and the literal
        # ``+`` marker never appears.
        summary = derive_summary("First line. +\nSecond line.\n")
        self.assertEqual(summary.snippet, "First line. Second line.")

    def test_document_attribute_entries_excluded(self) -> None:
        # The regression that motivated the change: attribute entries
        # under the title must not leak into the preview.
        source = (
            "= Recipe\n"
            ":author: Me\n"
            ":revdate: 2026-04-14\n"
            ":tags: fav\n"
            "\n"
            "A weekly bake.\n"
        )
        summary = derive_summary(source)
        self.assertEqual(summary.snippet, "A weekly bake.")
        self.assertNotIn(":author:", summary.snippet)

    def test_unordered_list_items_are_prose(self) -> None:
        source = "= T\n\n* Milk\n* Eggs\n* Flour\n"
        self.assertEqual(derive_summary(source).snippet, "Milk Eggs Flour")

    def test_ordered_list_items_are_prose(self) -> None:
        source = "= T\n\n. Mix\n. Bake\n"
        self.assertEqual(derive_summary(source).snippet, "Mix Bake")

    def test_nested_list_item_text_reaches_the_snippet(self) -> None:
        # Text in a nested sub-list must still flow into the note-list
        # snippet, in document order with the parent items.
        source = "= T\n\n* Produce\n** Apples\n** Pears\n* Dairy\n"
        self.assertEqual(
            derive_summary(source).snippet,
            "Produce Apples Pears Dairy",
        )

    def test_admonition_body_is_prose_label_dropped(self) -> None:
        source = "= T\n\nNOTE: Watch the oven.\n"
        self.assertEqual(derive_summary(source).snippet, "Watch the oven.")

    def test_blockquote_body_is_prose(self) -> None:
        source = "= T\n\n____\nTo be or not to be.\n____\n"
        self.assertEqual(derive_summary(source).snippet, "To be or not to be.")

    def test_recurses_into_sections(self) -> None:
        source = (
            "= Title\n"
            "\n"
            "== Section A\n"
            "\n"
            "Paragraph in section A.\n"
            "\n"
            "=== Subsection\n"
            "\n"
            "Paragraph in subsection.\n"
        )
        self.assertEqual(
            derive_summary(source).snippet,
            "Paragraph in section A. Paragraph in subsection.",
        )

    def test_code_block_is_structural(self) -> None:
        source = (
            "= T\n"
            "\n"
            "[source,python]\n"
            "----\n"
            "print('hi')\n"
            "----\n"
            "\n"
            "After the block.\n"
        )
        self.assertEqual(derive_summary(source).snippet, "After the block.")

    def test_image_is_structural(self) -> None:
        source = "= T\n\nimage::cat.png[]\n\nAfter the image.\n"
        self.assertEqual(derive_summary(source).snippet, "After the image.")

    def test_table_is_structural(self) -> None:
        source = (
            "= T\n"
            "\n"
            "|===\n"
            "| A | B\n"
            "| 1 | 2\n"
            "|===\n"
            "\n"
            "After the table.\n"
        )
        self.assertEqual(derive_summary(source).snippet, "After the table.")

    def test_only_title_yields_empty_snippet(self) -> None:
        self.assertEqual(derive_summary("= Just the title\n").snippet, "")

    def test_truncates_with_ellipsis_at_cap(self) -> None:
        body = "x" * (SNIPPET_MAX_CHARS + 50)
        snippet = derive_summary("= T\n\n" + body).snippet
        self.assertEqual(len(snippet), SNIPPET_MAX_CHARS)
        self.assertTrue(snippet.endswith("\u2026"))
        self.assertEqual(
            snippet[: SNIPPET_MAX_CHARS - 1],
            "x" * (SNIPPET_MAX_CHARS - 1),
        )

    def test_inline_markup_is_rendered_not_leaked(self) -> None:
        # Unlike the old prefix scanner, *bold* / _italic_ markers are
        # stripped because we flatten the parsed inlines.
        summary = derive_summary("= T\n\nThis is *bold* and _italic_.")
        self.assertEqual(summary.snippet, "This is bold and italic.")


class DeriveSummaryFallbackTests(unittest.TestCase):
    """On unparseable source the function falls back, never raises."""

    def test_unterminated_inline_does_not_raise(self) -> None:
        # An unmatched ``*`` makes the strict parser raise; the summary
        # must still come back so the note stays saveable.
        summary = derive_summary("= Draft\n\nThis *is unterminated")
        self.assertEqual(summary.title, "Draft")
        self.assertEqual(summary.snippet, "This *is unterminated")

    def test_fallback_title_without_heading(self) -> None:
        summary = derive_summary("not a heading and *broken")
        self.assertEqual(summary.title, UNTITLED)

    def test_fallback_truncates_like_happy_path(self) -> None:
        body = "y" * (SNIPPET_MAX_CHARS + 20)
        # Force a parse error with an unterminated span after the body.
        summary = derive_summary("= T\n\n" + body + " *broken")
        self.assertLessEqual(len(summary.snippet), SNIPPET_MAX_CHARS)
        self.assertTrue(summary.snippet.endswith("\u2026"))

    def test_returns_note_summary_instance(self) -> None:
        self.assertIsInstance(derive_summary("= ok\n"), NoteSummary)


class DeriveSummaryTagsTests(unittest.TestCase):
    """The tag tuple comes off the parsed Document on the happy path."""

    def test_no_tags_attribute_yields_empty_tuple(self) -> None:
        self.assertEqual(derive_summary("= T\n\nbody").tags, ())

    def test_single_tag(self) -> None:
        self.assertEqual(
            derive_summary("= T\n:tags: baking\n\nbody").tags,
            ("baking",),
        )

    def test_multiple_tags_sorted(self) -> None:
        self.assertEqual(
            derive_summary("= T\n:tags: zeta, alpha, beta\n\nbody").tags,
            ("alpha", "beta", "zeta"),
        )

    def test_dedup_and_sort(self) -> None:
        self.assertEqual(
            derive_summary("= T\n:tags: bread, baking, bread\n\nbody").tags,
            ("baking", "bread"),
        )

    def test_case_folded_to_lowercase(self) -> None:
        self.assertEqual(
            derive_summary("= T\n:tags: BAKING, Bread\n\nbody").tags,
            ("baking", "bread"),
        )

    def test_whitespace_tolerance_and_trailing_comma(self) -> None:
        self.assertEqual(
            derive_summary("= T\n:tags:   foo ,  bar ,\n\nbody").tags,
            ("bar", "foo"),
        )


class DeriveSummaryTagsFallbackTests(unittest.TestCase):
    """The fallback walks the lexer's tokens so a broken body still
    yields a valid tag tuple."""

    def test_broken_body_still_extracts_tags(self) -> None:
        # The body has an unterminated bold span — strict parser raises.
        # The fallback's tag arm walks the lexer's tokens and still
        # surfaces the ``:tags:`` line.
        source = "= Draft\n:tags: foo, bar\n\nThis *is unterminated"
        summary = derive_summary(source)
        self.assertEqual(summary.tags, ("bar", "foo"))

    def test_fallback_returns_empty_tags_when_tags_line_malformed(self) -> None:
        # ``:tags: foo bar`` has a space — invalid charset. The fallback
        # swallows the inner BAD_TAG_VALUE and returns no tags. The body
        # also has an unterminated bold marker so the strict parser
        # raises first; the fallback re-walks the header.
        source = "= Draft\n:tags: foo bar\n\nThis *is unterminated"
        summary = derive_summary(source)
        self.assertEqual(summary.tags, ())

    def test_fallback_returns_empty_tags_when_duplicate(self) -> None:
        source = (
            "= Draft\n"
            ":tags: foo\n"
            ":tags: bar\n"
            "\n"
            "This *is unterminated"
        )
        self.assertEqual(derive_summary(source).tags, ())


class AttachmentMacroSnippetTests(unittest.TestCase):
    """Snippets show an attachment link's label, never macro syntax."""

    def test_attachment_link_contributes_its_label(self) -> None:
        summary = derive_summary("= T\n\nSee attachment:a.pdf[the file].\n")
        self.assertEqual(summary.snippet, "See the file.")

    def test_bare_attachment_link_contributes_the_filename(self) -> None:
        summary = derive_summary("= T\n\nSee attachment:a.pdf[].\n")
        self.assertEqual(summary.snippet, "See a.pdf.")

    def test_attachments_table_contributes_no_prose(self) -> None:
        # The table's rows do not exist until render time, and the
        # snippet must never leak the macro's source syntax.
        summary = derive_summary("= T\n\nattachments::[]\n\nProse.\n")
        self.assertEqual(summary.snippet, "Prose.")

    def test_note_that_is_only_the_macro_has_an_empty_snippet(self) -> None:
        summary = derive_summary("= T\n\nattachments::[]\n")
        self.assertEqual(summary.snippet, "")


class SnippetStructureRuleScopeTests(unittest.TestCase):
    """The prose/structure rule governs the strict path, not the fallback.

    "A snippet never leaks macro syntax" reads as absolute, but it is a
    statement about walking an AST — and there is no AST when the source
    does not parse. These two tests pin both halves so the distinction
    is executable rather than implied.
    """

    _MACROS: str = "image::diagram.png[]\nattachments::[]"

    def test_clean_source_keeps_macro_syntax_out_of_the_snippet(self) -> None:
        summary = derive_summary(f"= T\n\n{self._MACROS}\n\nProse.\n")
        self.assertEqual(summary.snippet, "Prose.")

    def test_fallback_may_echo_a_macro_line_verbatim(self) -> None:
        """Intended, not a leak.

        The unterminated code fence makes the document unparseable, so
        the permissive extractor takes raw non-blank lines. Showing the
        source the user actually typed beats showing nothing for a note
        that is mid-edit.
        """
        source = f"= T\n\n----\nunterminated\n\n{self._MACROS}\n"
        summary = derive_summary(source)
        self.assertIn("image::diagram.png[]", summary.snippet)


class DeriveSummaryNeverRaisesTests(unittest.TestCase):
    """:func:`derive_summary` is total, including on pathological source.

    It runs on every save (``NoteRepository.insert`` / ``update_source``)
    and in the v2/v3 migration backfills, and the controller layer only
    captures ``sqlite3.DatabaseError`` — so anything else escaping here
    reaches the user as a crash on the autosave path. Deeply nested
    inline spans used to raise ``RecursionError`` from the parser and
    straight out through this function; see
    :data:`config.defaults.MAX_INLINE_DEPTH`.
    """

    def test_deeply_nested_inline_falls_back_instead_of_raising(self) -> None:
        body = "*_" * 600 + "deep" + "_*" * 600
        summary = derive_summary(f"= Title\n\n{body}\n")
        self.assertIsInstance(summary, NoteSummary)
        # The parse failed, so the permissive extractor supplied both
        # fields: the title line is still recognised by shape.
        self.assertEqual(summary.title, "Title")

    def test_deeply_nested_inline_in_a_titleless_note(self) -> None:
        summary = derive_summary("*_" * 600 + "deep" + "_*" * 600)
        self.assertIsInstance(summary, NoteSummary)
        self.assertEqual(summary.title, UNTITLED)


class UnreadBlockSnippetTests(unittest.TestCase):
    """Source folio could not read still counts as prose in a snippet.

    Unreachable from :func:`derive_summary` today, which parses strictly
    and falls back to permissive extraction — so the node is constructed
    directly here (the AST is pure data). The arm is what makes the
    eventual switch to :func:`asciidoc.parser.parse_recovering` a
    one-line change with the behaviour already decided, and this test is
    the only thing pinning that decision.
    """

    def _document(self, block: UnreadBlock) -> Document:
        return Document(
            title=None, tags=(), blocks=(block,), source_line=1,
        )

    def test_should_treat_unread_source_as_prose_in_a_snippet(self) -> None:
        # Given a document whose only content is a line that would not
        # parse
        document = self._document(
            UnreadBlock(
                lines=("foo_bar",),
                kind=ParseErrorKind.BAD_INLINE_SPAN,
                scope=UnreadScope.LINE,
                source_line=1,
            )
        )

        # When the snippet is derived
        snippet = _snippet_of(document)

        # Then the words survive, rather than the note-list row going
        # blank and the note becoming unfindable
        self.assertEqual(snippet, "foo_bar")

    def test_should_keep_every_line_of_a_multi_line_unread_block(self) -> None:
        document = self._document(
            UnreadBlock(
                lines=("|===", "| Region | Cluster"),
                kind=ParseErrorKind.UNTERMINATED_TABLE,
                scope=UnreadScope.BLOCK,
                source_line=1,
            )
        )
        self.assertEqual(_snippet_of(document), "|=== | Region | Cluster")


if __name__ == "__main__":
    unittest.main()
