"""Tests for :mod:`asciidoc.summary`."""

from __future__ import annotations

import unittest

from asciidoc.summary import derive_summary
from config.defaults import SNIPPET_MAX_CHARS, UNTITLED
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
        self.assertEqual(summary, NoteSummary(title=UNTITLED, snippet=""))


class DeriveSummarySnippetTests(unittest.TestCase):
    """The snippet collects prose and drops structure, AST-based."""

    def test_skips_title_uses_first_paragraph(self) -> None:
        summary = derive_summary("= Hello\n\nThe body.")
        self.assertEqual(summary.snippet, "The body.")

    def test_multiline_paragraph_joined_with_spaces(self) -> None:
        summary = derive_summary("First line.\nSecond line.\n")
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


if __name__ == "__main__":
    unittest.main()
