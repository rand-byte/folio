"""Tests for :mod:`storage.migrations`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from config.defaults import (
    SEED_NOTEBOOKS,
    SEED_NOTEBOOK_ID_PERSONAL,
    SEED_NOTEBOOK_ID_RECIPES,
    SEED_WELCOME_NOTE_ID,
    SEED_WELCOME_NOTE_NOTEBOOK_ID,
    SEED_WELCOME_NOTE_SOURCE,
)
from asciidoc.summary import derive_summary
from storage.database import Database
from storage.migrations import (
    ALL_MIGRATIONS,
    apply_pending,
    current_schema_version,
)


_FIXED_NOW: datetime = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _all_table_names(database: Database) -> set[str]:
    cursor = database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    return {row[0] for row in cursor.fetchall()}


def _all_index_names(database: Database) -> set[str]:
    cursor = database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in cursor.fetchall()}


def _all_trigger_names(database: Database) -> set[str]:
    cursor = database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    )
    return {row[0] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# Schema-version bookkeeping
# ---------------------------------------------------------------------------


class SchemaVersionTests(unittest.TestCase):
    def test_current_schema_version_zero_on_fresh_db(self) -> None:
        db = Database.in_memory()
        self.addCleanup(db.close)
        self.assertEqual(current_schema_version(db), 0)

    def test_apply_pending_records_latest_version(self) -> None:
        db = Database.in_memory()
        self.addCleanup(db.close)
        apply_pending(db, now=_FIXED_NOW)
        self.assertEqual(
            current_schema_version(db),
            max(m.version for m in ALL_MIGRATIONS),
        )

    def test_apply_pending_is_idempotent(self) -> None:
        db = Database.in_memory()
        self.addCleanup(db.close)
        apply_pending(db, now=_FIXED_NOW)
        # A second call must be a no-op: no extra schema_version row,
        # no extra notebooks, no extra notes.
        apply_pending(db, now=_FIXED_NOW)

        self.assertEqual(
            current_schema_version(db),
            max(m.version for m in ALL_MIGRATIONS),
        )

        notebook_count = db.connection.execute(
            "SELECT COUNT(*) FROM notebooks"
        ).fetchone()[0]
        self.assertEqual(notebook_count, len(SEED_NOTEBOOKS))

        note_count = db.connection.execute(
            "SELECT COUNT(*) FROM notes"
        ).fetchone()[0]
        self.assertEqual(note_count, 1)

        version_row_count = db.connection.execute(
            "SELECT COUNT(*) FROM schema_version"
        ).fetchone()[0]
        self.assertEqual(version_row_count, len(ALL_MIGRATIONS))

    def test_all_migrations_have_unique_versions(self) -> None:
        versions = [m.version for m in ALL_MIGRATIONS]
        self.assertEqual(len(versions), len(set(versions)))

    def test_all_migrations_are_in_ascending_order(self) -> None:
        versions = [m.version for m in ALL_MIGRATIONS]
        self.assertEqual(versions, sorted(versions))


# ---------------------------------------------------------------------------
# v1 schema shape
# ---------------------------------------------------------------------------


class V1SchemaTests(unittest.TestCase):
    db: Database

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        apply_pending(self.db, now=_FIXED_NOW)

    def test_v1_creates_all_expected_tables(self) -> None:
        tables = _all_table_names(self.db)
        # schema_version is created by the runner; the rest by v1.
        self.assertIn("schema_version", tables)
        self.assertIn("notebooks", tables)
        self.assertIn("notes", tables)
        self.assertIn("attachments", tables)

    def test_v1_creates_expected_indexes(self) -> None:
        indexes = _all_index_names(self.db)
        self.assertIn("idx_notes_notebook", indexes)
        self.assertIn("idx_notes_modified", indexes)
        self.assertIn("idx_attachments_note", indexes)

    def test_v1_creates_expected_triggers(self) -> None:
        triggers = _all_trigger_names(self.db)
        self.assertIn("notebooks_no_deep_nesting_insert", triggers)
        self.assertIn("notebooks_no_deep_nesting_update", triggers)


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


class SeedDataTests(unittest.TestCase):
    db: Database

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        apply_pending(self.db, now=_FIXED_NOW)

    def test_seeds_all_notebooks(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT id, name, parent_id, icon, sort_order FROM notebooks "
            "ORDER BY sort_order"
        )
        rows = cursor.fetchall()
        self.assertEqual(len(rows), len(SEED_NOTEBOOKS))

        seeded_ids = [row["id"] for row in rows]
        expected_ids = [n.id for n in SEED_NOTEBOOKS]
        self.assertEqual(seeded_ids, expected_ids)

    def test_baking_and_weeknight_have_recipes_as_parent(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT id, parent_id FROM notebooks WHERE parent_id IS NOT NULL"
        )
        children = {row["id"]: row["parent_id"] for row in cursor.fetchall()}
        self.assertEqual(children.get("seed-baking"), SEED_NOTEBOOK_ID_RECIPES)
        self.assertEqual(
            children.get("seed-weeknight-dinners"),
            SEED_NOTEBOOK_ID_RECIPES,
        )

    def test_top_level_seeds_have_null_parent(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT id FROM notebooks WHERE parent_id IS NULL"
        )
        top_level = {row["id"] for row in cursor.fetchall()}
        # Every seed except the two children of "Recipes" is top-level.
        expected_top_level = {
            n.id for n in SEED_NOTEBOOKS if n.parent_id is None
        }
        self.assertEqual(top_level, expected_top_level)

    def test_welcome_note_in_personal_notebook(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT id, notebook_id FROM notes"
        )
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], SEED_WELCOME_NOTE_ID)
        self.assertEqual(rows[0]["notebook_id"], SEED_NOTEBOOK_ID_PERSONAL)
        self.assertEqual(SEED_WELCOME_NOTE_NOTEBOOK_ID, SEED_NOTEBOOK_ID_PERSONAL)

    def test_welcome_note_title_and_snippet_derived_from_source(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT title, snippet, source FROM notes "
            "WHERE id = ?",
            (SEED_WELCOME_NOTE_ID,),
        )
        row = cursor.fetchone()
        expected = derive_summary(SEED_WELCOME_NOTE_SOURCE)
        self.assertEqual(row["source"], SEED_WELCOME_NOTE_SOURCE)
        self.assertEqual(row["title"], expected.title)
        self.assertEqual(row["snippet"], expected.snippet)

    def test_welcome_note_uses_supplied_now(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT created_at, modified_at FROM notes WHERE id = ?",
            (SEED_WELCOME_NOTE_ID,),
        )
        row = cursor.fetchone()
        expected = _FIXED_NOW.isoformat()
        self.assertEqual(row["created_at"], expected)
        self.assertEqual(row["modified_at"], expected)

    def test_default_now_is_used_when_not_supplied(self) -> None:
        # We don't try to pin the value; we just confirm that calling
        # apply_pending() without ``now=`` succeeds and writes a UTC
        # ISO-8601 timestamp.
        db = Database.in_memory()
        self.addCleanup(db.close)
        apply_pending(db)
        cursor = db.connection.execute(
            "SELECT modified_at FROM notes WHERE id = ?",
            (SEED_WELCOME_NOTE_ID,),
        )
        row = cursor.fetchone()
        # If parsing the value back round-trips to a timezone-aware
        # datetime, we accept it as well-formed.
        parsed = datetime.fromisoformat(row["modified_at"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_seed_notebooks_get_consecutive_sort_orders(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT id, sort_order FROM notebooks ORDER BY sort_order ASC"
        )
        rows = cursor.fetchall()
        sort_orders = [row["sort_order"] for row in rows]
        self.assertEqual(sort_orders, list(range(len(SEED_NOTEBOOKS))))


# ---------------------------------------------------------------------------
# v2 backfill — title/snippet rewritten from derive_summary
# ---------------------------------------------------------------------------


class V2BackfillTests(unittest.TestCase):
    """The v2 migration rewrites cached title/snippet for every note."""

    def _fresh_v1_only_db(self) -> Database:
        """A database with only v1 applied, so v2 is still pending.

        Creates the schema by hand-running the first migration the same
        way the runner does, then records v1 — without letting
        ``apply_pending`` run v2 yet.
        """
        db = Database.in_memory()
        self.addCleanup(db.close)
        db.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL PRIMARY KEY)"
        )
        v1 = next(m for m in ALL_MIGRATIONS if m.version == 1)
        with db.transaction() as connection:
            v1.apply(connection, _FIXED_NOW)
            connection.execute(
                "INSERT INTO schema_version (version) VALUES (1)"
            )
        return db

    def test_backfill_rewrites_stale_snippet(self) -> None:
        db = self._fresh_v1_only_db()
        # Simulate a row written by an older release whose cached
        # columns are stale / leaky relative to the new derivation.
        source = (
            "= Recipe\n"
            ":author: Me\n"
            "\n"
            "A weekly bake.\n"
        )
        db.connection.execute(
            "INSERT INTO notes "
            "(id, title, notebook_id, source, snippet, "
            " created_at, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "stale-note",
                "WRONG TITLE",
                SEED_NOTEBOOK_ID_PERSONAL,
                source,
                ":author: Me A weekly bake.",  # leaky old-style snippet
                _FIXED_NOW.isoformat(),
                _FIXED_NOW.isoformat(),
            ),
        )

        apply_pending(db, now=_FIXED_NOW)

        row = db.connection.execute(
            "SELECT title, snippet FROM notes WHERE id = ?",
            ("stale-note",),
        ).fetchone()
        expected = derive_summary(source)
        self.assertEqual(row["title"], expected.title)
        self.assertEqual(row["snippet"], expected.snippet)
        self.assertNotIn(":author:", row["snippet"])

    def test_backfill_does_not_touch_modified_at(self) -> None:
        db = self._fresh_v1_only_db()
        original_modified = datetime(2025, 6, 1, tzinfo=UTC)
        db.connection.execute(
            "INSERT INTO notes "
            "(id, title, notebook_id, source, snippet, "
            " created_at, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "n",
                "t",
                SEED_NOTEBOOK_ID_PERSONAL,
                "= T\n\nBody.",
                "old",
                _FIXED_NOW.isoformat(),
                original_modified.isoformat(),
            ),
        )

        apply_pending(db, now=_FIXED_NOW)

        row = db.connection.execute(
            "SELECT modified_at FROM notes WHERE id = ?", ("n",)
        ).fetchone()
        self.assertEqual(row["modified_at"], original_modified.isoformat())

    def test_backfill_survives_unparseable_source(self) -> None:
        db = self._fresh_v1_only_db()
        db.connection.execute(
            "INSERT INTO notes "
            "(id, title, notebook_id, source, snippet, "
            " created_at, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "bad",
                "t",
                SEED_NOTEBOOK_ID_PERSONAL,
                "= Draft\n\nThis *is unterminated",
                "old",
                _FIXED_NOW.isoformat(),
                _FIXED_NOW.isoformat(),
            ),
        )

        # Must not raise — derive_summary's fallback handles bad source.
        apply_pending(db, now=_FIXED_NOW)

        row = db.connection.execute(
            "SELECT title, snippet FROM notes WHERE id = ?", ("bad",)
        ).fetchone()
        expected = derive_summary("= Draft\n\nThis *is unterminated")
        self.assertEqual(row["title"], expected.title)
        self.assertEqual(row["snippet"], expected.snippet)


if __name__ == "__main__":
    unittest.main()
