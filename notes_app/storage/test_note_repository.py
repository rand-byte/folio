"""Tests for :mod:`notes_app.storage.note_repository`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone

from notes_app.config.defaults import SEED_NOTEBOOK_ID_PERSONAL, UNTITLED
from notes_app.asciidoc.summary import derive_summary
from notes_app.models.note import Note
from notes_app.storage.database import Database
from notes_app.storage.migrations import apply_pending
from notes_app.storage.note_repository import NoteRepository


_FIXED_NOW: datetime = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _make_note(
    *,
    note_id: str,
    notebook_id: str = SEED_NOTEBOOK_ID_PERSONAL,
    source: str = "= Title\n\nBody.",
    created_at: datetime | None = None,
    modified_at: datetime | None = None,
) -> Note:
    """Build a :class:`Note` with derived title/snippet for tests.

    The title/snippet match what the repository would derive on insert,
    so the in-memory note and the stored row agree.
    """
    when = created_at if created_at is not None else _FIXED_NOW
    mod = modified_at if modified_at is not None else when
    summary = derive_summary(source)
    return Note(
        id=note_id,
        title=summary.title,
        notebook_id=notebook_id,
        source=source,
        snippet=summary.snippet,
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
        # don't have to special-case it.
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
        # The repository preserves whatever offset it was given; the
        # convention to use UTC is enforced upstream.
        offset = timezone(timedelta(hours=5, minutes=30))
        when = datetime(2026, 3, 4, 5, 6, 7, tzinfo=offset)
        self.repo.insert(_make_note(note_id="n1", created_at=when))
        fetched = self.repo.get("n1")
        self.assertEqual(fetched.created_at, when)


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


class ListingTests(_NoteRepoTestBase):
    def test_list_by_notebook_filters_correctly(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1", notebook_id="seed-personal"))
        self.repo.insert(_make_note(
            note_id="n2", notebook_id="seed-recipes"))
        self.repo.insert(_make_note(
            note_id="n3", notebook_id="seed-personal"))

        in_personal = self.repo.list_by_notebook("seed-personal")
        ids_in_personal = {n.id for n in in_personal}
        self.assertEqual(ids_in_personal, {"n1", "n3"})

        in_recipes = self.repo.list_by_notebook("seed-recipes")
        self.assertEqual([n.id for n in in_recipes], ["n2"])

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

    def test_list_modified_since_filters_inclusively(self) -> None:
        a = datetime(2026, 1, 1, tzinfo=UTC)
        b = datetime(2026, 1, 5, tzinfo=UTC)
        c = datetime(2026, 1, 10, tzinfo=UTC)
        self.repo.insert(_make_note(note_id="a", modified_at=a))
        self.repo.insert(_make_note(note_id="b", modified_at=b))
        self.repo.insert(_make_note(note_id="c", modified_at=c))

        # since=b returns b and c (>=).
        ids = [n.id for n in self.repo.list_modified_since(b)]
        self.assertEqual(ids, ["c", "b"])

    def test_list_modified_since_returns_empty_when_all_older(self) -> None:
        when = datetime(2026, 1, 1, tzinfo=UTC)
        self.repo.insert(_make_note(note_id="old", modified_at=when))
        future = datetime(2030, 1, 1, tzinfo=UTC)
        self.assertEqual(self.repo.list_modified_since(future), [])

    def test_listings_return_empty_on_empty_table(self) -> None:
        self.assertEqual(self.repo.list_all(), [])
        self.assertEqual(self.repo.list_by_notebook("seed-personal"), [])
        self.assertEqual(
            self.repo.list_modified_since(datetime(1970, 1, 1, tzinfo=UTC)),
            [],
        )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchTests(_NoteRepoTestBase):
    def test_search_finds_match_in_title(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1", source="= Quick Brown Fox\n\nbody"))
        self.repo.insert(_make_note(
            note_id="n2", source="= Other\n\nbody"))
        results = self.repo.search("brown")
        self.assertEqual([n.id for n in results], ["n1"])

    def test_search_finds_match_in_snippet(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1", source="= Title\n\nUnique snippet body."))
        results = self.repo.search("snippet")
        self.assertEqual([n.id for n in results], ["n1"])

    def test_search_finds_match_in_source(self) -> None:
        # Pick a token that's only in source: a tag the snippet would
        # have stripped. Block fences are stripped, so put the term in
        # a block delimited by a fence.
        source = "= Title\n\nshort\n\n----\nDeep_token here\n----\n"
        self.repo.insert(_make_note(note_id="n1", source=source))
        results = self.repo.search("Deep_token")
        self.assertEqual([n.id for n in results], ["n1"])

    def test_search_is_case_insensitive_for_ascii(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1", source="= MIXED case TITLE\n\nbody"))
        results = self.repo.search("mixed")
        self.assertEqual([n.id for n in results], ["n1"])

    def test_search_escapes_percent_wildcard(self) -> None:
        # The literal "20%" should match a note containing "20%" but
        # not a note that merely contains "20" surrounded by anything.
        self.repo.insert(_make_note(
            note_id="literal", source="= Discount\n\nNow 20% off."))
        self.repo.insert(_make_note(
            note_id="not_literal", source="= Plain\n\n20 dollars and nothing"))
        results = self.repo.search("20%")
        ids = {n.id for n in results}
        self.assertIn("literal", ids)
        self.assertNotIn("not_literal", ids)

    def test_search_escapes_underscore_wildcard(self) -> None:
        # Without escaping, the LIKE pattern "%a_b%" matches "axb".
        # With escaping, the literal "a_b" must match only itself.
        self.repo.insert(_make_note(
            note_id="literal", source="= Hit\n\nname is a_b here"))
        self.repo.insert(_make_note(
            note_id="not_literal", source="= Miss\n\nname is axb here"))
        results = self.repo.search("a_b")
        ids = {n.id for n in results}
        self.assertIn("literal", ids)
        self.assertNotIn("not_literal", ids)

    def test_search_escapes_backslash(self) -> None:
        # The escape character itself must round-trip safely.
        self.repo.insert(_make_note(
            note_id="literal", source="= Path\n\nC:\\\\foo\\\\bar text"))
        # Search for the literal backslash sequence.
        results = self.repo.search("\\\\foo")
        self.assertEqual([n.id for n in results], ["literal"])

    def test_search_returns_results_in_modified_desc_order(self) -> None:
        old = datetime(2026, 1, 1, tzinfo=UTC)
        new = datetime(2026, 1, 10, tzinfo=UTC)
        self.repo.insert(_make_note(
            note_id="old", source="= Match here\n\nold", modified_at=old))
        self.repo.insert(_make_note(
            note_id="new", source="= Match here\n\nnew", modified_at=new))
        self.assertEqual(
            [n.id for n in self.repo.search("match")],
            ["new", "old"],
        )

    def test_search_empty_string_matches_all(self) -> None:
        self.repo.insert(_make_note(note_id="a"))
        self.repo.insert(_make_note(note_id="b"))
        ids = {n.id for n in self.repo.search("")}
        self.assertEqual(ids, {"a", "b"})


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------


class UpdateSourceTests(_NoteRepoTestBase):
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
        # Source without a level-0 heading: title falls back to
        # "Untitled". This must be applied automatically.
        self.repo.insert(_make_note(note_id="n1"))
        self.repo.update_source(
            "n1", "no heading here", datetime(2026, 6, 1, tzinfo=UTC)
        )
        self.assertEqual(self.repo.get("n1").title, UNTITLED)


class UpdateNotebookTests(_NoteRepoTestBase):
    def test_update_notebook_changes_notebook_id(self) -> None:
        self.repo.insert(_make_note(
            note_id="n1", notebook_id="seed-personal"))
        self.repo.update_notebook("n1", "seed-recipes")
        self.assertEqual(self.repo.get("n1").notebook_id, "seed-recipes")

    def test_update_notebook_raises_keyerror_on_missing_id(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.update_notebook("ghost", "seed-personal")


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

    def test_delete_only_removes_target(self) -> None:
        self.repo.insert(_make_note(note_id="keep"))
        self.repo.insert(_make_note(note_id="drop"))
        self.repo.delete("drop")
        self.assertEqual({n.id for n in self.repo.list_all()}, {"keep"})


if __name__ == "__main__":
    unittest.main()
