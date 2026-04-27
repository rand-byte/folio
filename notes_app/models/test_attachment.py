"""Tests for :mod:`notes_app.models.attachment`."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields

from notes_app.enums import MimeKind
from notes_app.models.attachment import Attachment


class AttachmentDataclassTests(unittest.TestCase):
    def test_construction_assigns_every_field(self) -> None:
        att = Attachment(
            id="att1",
            note_id="note1",
            filename="cat.png",
            byte_size=12345,
            mime_type=MimeKind.PNG,
        )
        self.assertEqual(att.id, "att1")
        self.assertEqual(att.note_id, "note1")
        self.assertEqual(att.filename, "cat.png")
        self.assertEqual(att.byte_size, 12345)
        self.assertIs(att.mime_type, MimeKind.PNG)

    def test_is_frozen(self) -> None:
        att = Attachment(
            id="att1",
            note_id="note1",
            filename="cat.png",
            byte_size=1,
            mime_type=MimeKind.PNG,
        )
        with self.assertRaises(FrozenInstanceError):
            att.filename = "dog.png"  # type: ignore[misc]

    def test_does_not_carry_data_field(self) -> None:
        # This is the schema-level invariant from §6 of the plan: the
        # in-memory Attachment shape never holds the BLOB. Removing this
        # test would silently allow a `data` field to slip in.
        names = {f.name for f in fields(Attachment)}
        self.assertNotIn("data", names)

    def test_field_set_is_exact(self) -> None:
        names = {f.name for f in fields(Attachment)}
        self.assertEqual(
            names,
            {"id", "note_id", "filename", "byte_size", "mime_type"},
        )

    def test_mime_type_is_typed_as_enum(self) -> None:
        att = Attachment(
            id="att1",
            note_id="note1",
            filename="x.jpg",
            byte_size=1,
            mime_type=MimeKind.JPEG,
        )
        self.assertIsInstance(att.mime_type, MimeKind)

    def test_equality_by_value(self) -> None:
        a = Attachment(
            id="x", note_id="n", filename="f", byte_size=1,
            mime_type=MimeKind.PNG,
        )
        b = Attachment(
            id="x", note_id="n", filename="f", byte_size=1,
            mime_type=MimeKind.PNG,
        )
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
