"""Tests for :mod:`models.attachment`."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields

from models.attachment import Attachment


class AttachmentDataclassTests(unittest.TestCase):
    def test_construction_assigns_every_field(self) -> None:
        att = Attachment(
            id="att1",
            note_id="note1",
            filename="cat.png",
            byte_size=12345,
        )
        self.assertEqual(att.id, "att1")
        self.assertEqual(att.note_id, "note1")
        self.assertEqual(att.filename, "cat.png")
        self.assertEqual(att.byte_size, 12345)

    def test_is_frozen(self) -> None:
        att = Attachment(
            id="att1",
            note_id="note1",
            filename="cat.png",
            byte_size=1,
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
        # Notably absent: ``mime_type``. Attachments carry no
        # content-type classification — they are opaque blobs, and the
        # filename extension preserves any future ability to re-derive
        # a type.
        names = {f.name for f in fields(Attachment)}
        self.assertEqual(
            names,
            {"id", "note_id", "filename", "byte_size"},
        )

    def test_equality_by_value(self) -> None:
        a = Attachment(id="x", note_id="n", filename="f", byte_size=1)
        b = Attachment(id="x", note_id="n", filename="f", byte_size=1)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
