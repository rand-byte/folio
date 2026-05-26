"""Tests for :mod:`models.note`."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone

from models.note import Note, NoteSummary


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


class NoteSummaryTests(unittest.TestCase):
    """The derived ``(title, snippet)`` value type."""

    def test_construction_assigns_fields(self) -> None:
        summary = NoteSummary(title="A title", snippet="A snippet")
        self.assertEqual(summary.title, "A title")
        self.assertEqual(summary.snippet, "A snippet")

    def test_is_frozen(self) -> None:
        summary = NoteSummary(title="t", snippet="s")
        with self.assertRaises(FrozenInstanceError):
            summary.title = "other"  # type: ignore[misc]

    def test_equality_by_value(self) -> None:
        self.assertEqual(
            NoteSummary(title="t", snippet="s"),
            NoteSummary(title="t", snippet="s"),
        )
        self.assertNotEqual(
            NoteSummary(title="t", snippet="s"),
            NoteSummary(title="t", snippet="other"),
        )

    def test_is_hashable(self) -> None:
        # Frozen dataclasses hash by value; usable as a dict key / set member.
        self.assertEqual(
            len({NoteSummary("t", "s"), NoteSummary("t", "s")}),
            1,
        )

    def test_field_set_is_exact(self) -> None:
        names = {f.name for f in fields(NoteSummary)}
        self.assertEqual(names, {"title", "snippet"})


if __name__ == "__main__":
    unittest.main()
