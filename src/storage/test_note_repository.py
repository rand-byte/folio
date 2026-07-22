"""Tests for :mod:`storage.note_repository`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone

from asciidoc.summary import derive_summary
from config.defaults import UNTITLED
from models.note import Note
from storage.database import Database
from storage.migrations import apply_pending
from storage.note_repository import NoteRepository


_FIXED_NOW: datetime = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _make_note(
    *,
    note_id: str,
    source: str = "= Title\n\nBody.",
    created_at: datetime | None = None,
    modified_at: datetime | None = None,
) -> Note:
    """Build a :class:`Note` with derived title/snippet/tags for tests.

    The cached fields match what the repository would derive on insert,
    so the in-memory note and the stored row agree.
    """
    when = created_at if created_at is not None else _FIXED_NOW
    mod = modified_at if modified_at is not None else when
    summary = derive_summary(source)
    return Note(
        id=note_id,
        title=summary.title,
        source=source,
        snippet=summary.snippet,
        tags=summary.tags,
        created_at=when,
        modified_at=mod,
    )


class _NoteRepoTestBase(unittest.TestCase):
    """Common setup: in-memory DB with migrations applied + a repo."""

    db: Database
    repo: NoteRepository

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        apply_pending(self.db, now=_FIXED_NOW)
        self.repo = NoteRepository(self.db)
        # Each test starts from a clean slate w.r.t. notes — drop the
        # seeded welcome note so assertions about counts and ordering
        # don't have to special-case it. ON DELETE CASCADE removes its
        # note_tags rows automatically.
        self.db.connection.execute("DELETE FROM notes")


# ---------------------------------------------------------------------------
# get / insert
# ---------------------------------------------------------------------------


class GetAndInsertTests(_NoteRepoTestBase):
    def test_insert_then_get_round_trips(self) -> None:
        note = _make_note(note_id="n1")
        self.repo.insert(note)
        fetched = self.repo.get("n1")
        self.assertEqual(fetched, note)

    def test_insert_returns_derived_note(self) -> None:
        note = _make_note(
            note_id="n1",
            source="= Heading\n:tags: zeta, alpha\n\nBody text.",
        )
        returned = self.repo.insert(note)
        expected = derive_summary(note.source)
        self.assertEqual(returned.id, "n1")
        self.assertEqual(returned.source, note.source)
        self.assertEqual(returned.title, expected.title)
        self.assertEqual(returned.snippet, expected.snippet)
        self.assertEqual(returned.tags, expected.tags)
        self.assertEqual(returned.created_at, note.created_at)
        self.assertEqual(returned.modified_at, note.modified_at)
        # The returned value matches a subsequent ``get``.
        self.assertEqual(returned, self.repo.get("n1"))

    def test_get_raises_keyerror_on_missing_id(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.get("does-not-exist")

    def test_inserted_note_visible_in_list_all(self) -> None:
        self.repo.insert(_make_note(note_id="n1"))
        all_notes = self.repo.list_all()
        self.assertEqual(len(all_notes), 1)
        self.assertEqual(all_notes[0].id, "n1")

    def test_timestamps_round_trip_with_utc_timezone(self) -> None:
        when = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        self.repo.insert(_make_note(note_id="n1", created_at=when))
        fetched = self.repo.get("n1")
        self.assertEqual(fetched.created_at, when)
        self.assertIsNotNone(fetched.created_at.tzinfo)

    def test_timestamps_round_trip_with_non_utc_timezone(self) -> None:
        offset = timezone(timedelta(hours=5, minutes=30))
        when = datetime(2026, 3, 4, 5, 6, 7, tzinfo=offset)
        self.repo.insert(_make_note(note_id="n1", created_at=when))
        fetched = self.repo.get("n1")
        self.assertEqual(fetched.created_at, when)


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


class ListingTests(_NoteRepoTestBase):
    def test_list_all_orders_by_modified_desc(self) -> None:
        oldest = datetime(2026, 1, 1, tzinfo=UTC)
        middle = datetime(2026, 1, 5, tzinfo=UTC)
        newest = datetime(2026, 1, 10, tzinfo=UTC)
        self.repo.insert(_make_note(note_id="old", modified_at=oldest))
        self.repo.insert(_make_note(note_id="new", modified_at=newest))
        self.repo.insert(_make_note(note_id="mid", modified_at=middle))
        self.assertEqual(
            [n.id for n in self.repo.list_all()],
            ["new", "mid", "old"],
        )

    def test_listings_return_empty_on_empty_table(self) -> None:
        self.assertEqual(self.repo.list_all(), [])


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------


class UpdateSourceTests(_NoteRepoTestBase):
    def test_update_source_returns_derived_note(self) -> None:
        original = datetime(2026, 1, 1, tzinfo=UTC)
        self.repo.insert(_make_note(
            note_id="n1",
            source="= Old\n:tags: alpha\n\nold body",
            created_at=original,
        ))
        new_source = "= Renamed\n:tags: gamma, beta\n\nNew snippet preview."
        new_modified = datetime(2027, 1, 1, tzinfo=UTC)
        returned = self.repo.update_source("n1", new_source, new_modified)
        expected = derive_summary(new_source)
        self.assertEqual(returned.id, "n1")
        self.assertEqual(returned.source, new_source)
        self.assertEqual(returned.title, expected.title)
        self.assertEqual(returned.snippet, expected.snippet)
        self.assertEqual(returned.tags, expected.tags)
        # ``created_at`` is preserved across the update (recovered via
        # the RETURNING clause), ``modified_at`` is the new value.
        self.assertEqual(returned.created_at, original)
        self.assertEqual(returned.modified_at, new_modified)
        self.assertEqual(returned, self.repo.get("n1"))

    def test_update_source_rewrites_source_title_and_snippet(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1", source="= Original Title\n\nOriginal body."))
        new_source = "= Renamed\n\nNew snippet preview."
        new_modified = datetime(2027, 1, 1, tzinfo=UTC)
        self.repo.update_source("n1", new_source, new_modified)

        fetched = self.repo.get("n1")
        expected = derive_summary(new_source)
        self.assertEqual(fetched.source, new_source)
        self.assertEqual(fetched.title, expected.title)
        self.assertEqual(fetched.snippet, expected.snippet)
        self.assertEqual(fetched.modified_at, new_modified)

    def test_update_source_replaces_tag_rows(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1",
            source="= T\n:tags: alpha, beta\n\nbody",
        ))
        self.repo.update_source(
            "n1",
            "= T\n:tags: gamma\n\nbody",
            datetime(2027, 1, 1, tzinfo=UTC),
        )
        fetched = self.repo.get("n1")
        self.assertEqual(fetched.tags, ("gamma",))
        # The junction table has exactly the new row.
        rows = [
            row[0] for row in self.db.connection.execute(
                "SELECT tag FROM note_tags WHERE note_id = 'n1' ORDER BY tag"
            )
        ]
        self.assertEqual(rows, ["gamma"])

    def test_update_source_preserves_created_at(self) -> None:
        original = datetime(2026, 1, 1, tzinfo=UTC)
        self.repo.insert(_make_note(note_id="n1", created_at=original))
        self.repo.update_source(
            "n1", "= New\n\nbody", datetime(2027, 1, 1, tzinfo=UTC)
        )
        self.assertEqual(self.repo.get("n1").created_at, original)

    def test_update_source_raises_keyerror_on_missing_id(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.update_source(
                "ghost", "= x\n\ny", datetime(2026, 1, 1, tzinfo=UTC)
            )

    def test_update_source_handles_untitled_source(self) -> None:
        self.repo.insert(_make_note(note_id="n1"))
        self.repo.update_source(
            "n1", "no heading here", datetime(2026, 6, 1, tzinfo=UTC)
        )
        self.assertEqual(self.repo.get("n1").title, UNTITLED)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class DeleteTests(_NoteRepoTestBase):
    def test_delete_removes_the_note(self) -> None:
        self.repo.insert(_make_note(note_id="n1"))
        self.repo.delete("n1")
        with self.assertRaises(KeyError):
            self.repo.get("n1")

    def test_delete_raises_keyerror_on_missing_id(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.delete("ghost")

    def test_delete_cascades_tag_rows(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1", source="= T\n:tags: foo, bar\n\nbody"
        ))
        self.repo.delete("n1")
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM note_tags WHERE note_id = 'n1'"
        ).fetchone()[0]
        self.assertEqual(rows, 0)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class InsertTagsRoundTripTests(_NoteRepoTestBase):
    def test_tags_round_trip_on_insert(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1", source="= T\n:tags: zeta, alpha\n\nbody"
        ))
        fetched = self.repo.get("n1")
        self.assertEqual(fetched.tags, ("alpha", "zeta"))

    def test_no_tag_attribute_yields_empty_tuple(self) -> None:
        self.repo.insert(_make_note(note_id="n1"))
        self.assertEqual(self.repo.get("n1").tags, ())

    def test_list_all_carries_tags_per_row(self) -> None:
        self.repo.insert(_make_note(
            note_id="a", source="= A\n:tags: bread\n\nbody",
            modified_at=datetime(2026, 1, 2, tzinfo=UTC),
        ))
        self.repo.insert(_make_note(
            note_id="b", source="= B\n:tags: baking, bread\n\nbody",
            modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        ))
        notes = self.repo.list_all()
        # Newest first.
        self.assertEqual(notes[0].id, "a")
        self.assertEqual(notes[0].tags, ("bread",))
        self.assertEqual(notes[1].id, "b")
        self.assertEqual(notes[1].tags, ("baking", "bread"))


if __name__ == "__main__":
    unittest.main()
