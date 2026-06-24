"""Tests for the :mod:`system_docs` gi-free loader."""

from __future__ import annotations

import unittest

from enums import SystemDocument
from system_docs import load_bytes, load_text


_PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"
"""The 8-byte PNG magic number every PNG file starts with."""

# The text-backed system documents (the ``.adoc`` sources) versus the one
# binary document (the demo image). Keeping the split explicit lets the
# loader tests exercise each reader against exactly the members it serves.
_TEXT_DOCUMENTS: tuple[SystemDocument, ...] = (
    SystemDocument.WELCOME,
    SystemDocument.HELP,
)
_BINARY_DOCUMENTS: tuple[SystemDocument, ...] = (
    SystemDocument.HELP_DEMO_IMAGE,
)


class LoadTextTests(unittest.TestCase):
    def test_every_text_document_resolves_to_non_empty_text(self) -> None:
        for document in _TEXT_DOCUMENTS:
            with self.subTest(document=document):
                text = load_text(document)
                self.assertIsInstance(text, str)
                self.assertNotEqual(text.strip(), "")

    def test_welcome_text_has_title_line(self) -> None:
        # The welcome source is a real note: a level-0 title plus tags.
        text = load_text(SystemDocument.WELCOME)
        self.assertTrue(text.startswith("= "))
        self.assertIn(":tags:", text)

    def test_help_text_has_title_and_image_macro(self) -> None:
        text = load_text(SystemDocument.HELP)
        self.assertTrue(text.startswith("= "))
        # The help must demonstrate the image capability, so its source
        # carries an image macro for the bundled demo image.
        self.assertIn(
            f"image::{SystemDocument.HELP_DEMO_IMAGE.value}[",
            text,
        )


class LoadBytesTests(unittest.TestCase):
    def test_every_binary_document_resolves_to_non_empty_bytes(self) -> None:
        for document in _BINARY_DOCUMENTS:
            with self.subTest(document=document):
                data = load_bytes(document)
                self.assertIsInstance(data, bytes)
                self.assertGreater(len(data), 0)

    def test_demo_image_is_a_png(self) -> None:
        data = load_bytes(SystemDocument.HELP_DEMO_IMAGE)
        self.assertTrue(data.startswith(_PNG_SIGNATURE))


class FilenameKeyingTests(unittest.TestCase):
    """The enum value is the package-relative filename the loader joins."""

    def test_values_are_distinct_filenames(self) -> None:
        values = [document.value for document in SystemDocument]
        self.assertEqual(len(values), len(set(values)))

    def test_text_members_name_adoc_files(self) -> None:
        for document in _TEXT_DOCUMENTS:
            with self.subTest(document=document):
                self.assertTrue(document.value.endswith(".adoc"))


if __name__ == "__main__":
    unittest.main()
