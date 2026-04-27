"""Tests for :mod:`notes_app.models.note`."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone

from notes_app.models.note import (
    SNIPPET_MAX_CHARS,
    UNTITLED,
    Note,
    derive_snippet,
    derive_title,
)


class NoteDataclassTests(unittest.TestCase):
    """Smoke-test the :class:`Note` dataclass shape."""

    def setUp(self) -> None:
        self.created = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
        self.modified = datetime(2026, 4, 26, 13, 0, tzinfo=timezone.utc)

    def test_construction_assigns_every_field(self) -> None:
        note = Note(
            id="n1",
            title="Hello",
            notebook_id="nb1",
            source="= Hello\n\nBody",
            snippet="Body",
            created_at=self.created,
            modified_at=self.modified,
        )
        self.assertEqual(note.id, "n1")
        self.assertEqual(note.title, "Hello")
        self.assertEqual(note.notebook_id, "nb1")
        self.assertEqual(note.source, "= Hello\n\nBody")
        self.assertEqual(note.snippet, "Body")
        self.assertEqual(note.created_at, self.created)
        self.assertEqual(note.modified_at, self.modified)

    def test_is_frozen(self) -> None:
        note = Note(
            id="n1",
            title="Hello",
            notebook_id="nb1",
            source="",
            snippet="",
            created_at=self.created,
            modified_at=self.modified,
        )
        with self.assertRaises(FrozenInstanceError):
            note.title = "Mutated"  # type: ignore[misc]

    def test_field_set_is_exact(self) -> None:
        # Guards against accidentally adding or removing a field — the
        # storage layer's schema mirrors this set.
        names = {f.name for f in fields(Note)}
        self.assertEqual(
            names,
            {
                "id",
                "title",
                "notebook_id",
                "source",
                "snippet",
                "created_at",
                "modified_at",
            },
        )


class DeriveTitleTests(unittest.TestCase):
    """Table-driven tests for :func:`derive_title`."""

    def test_table(self) -> None:
        cases: tuple[tuple[str, str, str], ...] = (
            # (description, source, expected_title)
            ("level-0 with body", "= Hello\n\nBody", "Hello"),
            ("level-0 only", "= Just a title", "Just a title"),
            (
                "leading blanks before title",
                "\n\n\n= After blanks\n\nBody",
                "After blanks",
            ),
            (
                "leading whitespace then title",
                "   \n\t\n= Trimmed\n",
                "Trimmed",
            ),
            (
                "title with surrounding spaces is stripped",
                "=    Hello world   \n",
                "Hello world",
            ),
            ("no title — paragraph first", "Just some text\n", UNTITLED),
            (
                "no title — section heading first",
                "== Section\n\nBody",
                UNTITLED,
            ),
            ("no title — empty source", "", UNTITLED),
            ("no title — only blanks", "\n\n   \n", UNTITLED),
            (
                "no title — equals without space",
                "=Hello\n\nBody",
                UNTITLED,
            ),
            (
                "no title — bare equals sign",
                "=\n\nBody",
                UNTITLED,
            ),
            (
                "title-with-empty-text falls back to Untitled",
                "= \n\nBody",
                UNTITLED,
            ),
            (
                "deeper heading does not count as a title",
                "=== H3 first\n\nBody",
                UNTITLED,
            ),
        )
        for description, source, expected in cases:
            with self.subTest(description):
                self.assertEqual(derive_title(source), expected)


class DeriveSnippetTests(unittest.TestCase):
    """Table-driven tests for :func:`derive_snippet`."""

    def test_skips_title_line(self) -> None:
        source = "= Hello\n\nThe body."
        self.assertEqual(derive_snippet(source), "The body.")

    def test_uses_first_paragraph_when_no_title(self) -> None:
        source = "First line.\nSecond line.\n"
        # Both lines join with a single space, no title to skip.
        self.assertEqual(derive_snippet(source), "First line. Second line.")

    def test_skips_section_headings(self) -> None:
        source = (
            "= Title\n"
            "\n"
            "== Section A\n"
            "\n"
            "Paragraph in section A.\n"
            "\n"
            "=== Subsection\n"
            "Paragraph in subsection.\n"
        )
        self.assertEqual(
            derive_snippet(source),
            "Paragraph in section A. Paragraph in subsection.",
        )

    def test_skips_block_delimiters_and_attribute_lines(self) -> None:
        source = (
            "= Title\n"
            "\n"
            "[source,python]\n"
            "----\n"
            "print('hi')\n"
            "----\n"
            "\n"
            "After the block.\n"
        )
        self.assertEqual(
            derive_snippet(source),
            "print('hi') After the block.",
        )

    def test_skips_image_macro(self) -> None:
        source = "= Title\n\nimage::cat.png[]\n\nAfter the image.\n"
        self.assertEqual(derive_snippet(source), "After the image.")

    def test_skips_admonition_selector_lines(self) -> None:
        source = (
            "= Title\n"
            "\n"
            "[NOTE]\n"
            "====\n"
            "An admonition body.\n"
            "====\n"
            "\n"
            "After.\n"
        )
        self.assertEqual(
            derive_snippet(source),
            "An admonition body. After.",
        )

    def test_keeps_inline_markers_verbatim(self) -> None:
        # We deliberately do not strip *bold* / _italic_ — the parser
        # would do that and we have no parser at this layer.
        source = "= Title\n\nThis is *bold* and _italic_."
        self.assertEqual(
            derive_snippet(source),
            "This is *bold* and _italic_.",
        )

    def test_truncates_with_ellipsis(self) -> None:
        body = "x" * (SNIPPET_MAX_CHARS + 50)
        snippet = derive_snippet("= Title\n\n" + body)
        self.assertEqual(len(snippet), SNIPPET_MAX_CHARS)
        self.assertTrue(snippet.endswith("\u2026"))
        # The non-ellipsis prefix is purely the original character.
        self.assertEqual(snippet[: SNIPPET_MAX_CHARS - 1], "x" * (SNIPPET_MAX_CHARS - 1))

    def test_explicit_max_chars(self) -> None:
        source = "= Title\n\nabcdefghijklmnopqrstuvwxyz"
        snippet = derive_snippet(source, max_chars=10)
        self.assertEqual(len(snippet), 10)
        self.assertTrue(snippet.endswith("\u2026"))

    def test_zero_or_negative_max_chars_returns_empty(self) -> None:
        self.assertEqual(derive_snippet("= Title\n\nbody", max_chars=0), "")
        self.assertEqual(derive_snippet("= Title\n\nbody", max_chars=-3), "")

    def test_empty_source(self) -> None:
        self.assertEqual(derive_snippet(""), "")

    def test_only_title_yields_empty_snippet(self) -> None:
        self.assertEqual(derive_snippet("= Just the title\n"), "")

    def test_only_blanks_yields_empty_snippet(self) -> None:
        self.assertEqual(derive_snippet("\n   \n\n"), "")

    def test_handles_crlf_line_endings(self) -> None:
        source = "= Title\r\n\r\nFirst line.\r\nSecond line.\r\n"
        self.assertEqual(
            derive_snippet(source),
            "First line. Second line.",
        )


if __name__ == "__main__":
    unittest.main()
