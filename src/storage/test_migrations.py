"""Tests for :mod:`storage.migrations`."""

from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, datetime

from asciidoc.summary import derive_summary
from config.defaults import SEED_WELCOME_NOTE_ID
from enums import SystemDocument
from storage.database import Database
from storage.migrations import (
    ALL_MIGRATIONS,
    apply_pending,
    current_schema_version,
)
from system_docs import load_text


_FIXED_NOW: datetime = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

_WELCOME_SOURCE: str = load_text(SystemDocument.WELCOME)
"""The welcome source the v1 migration seeds, read from ``system_docs``."""


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


def _column_names(database: Database, table: str) -> set[str]:
    cursor = database.connection.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


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
        apply_pending(db, now=_FIXED_NOW)
        self.assertEqual(
            current_schema_version(db),
            max(m.version for m in ALL_MIGRATIONS),
        )
        # Schema-version table has exactly one row per shipped migration.
        cursor = db.connection.execute("SELECT version FROM schema_version")
        versions = sorted(int(row[0]) for row in cursor.fetchall())
        self.assertEqual(
            versions, [m.version for m in ALL_MIGRATIONS],
        )


# ---------------------------------------------------------------------------
# Post-migration shape: notes table, note_tags table, no notebooks
# ---------------------------------------------------------------------------


class PostMigrationSchemaTests(unittest.TestCase):
    """v3 demolished the notebook schema; v1's leftovers must be gone."""

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        apply_pending(self.db, now=_FIXED_NOW)

    def test_notebooks_table_is_gone(self) -> None:
        self.assertNotIn("notebooks", _all_table_names(self.db))

    def test_notes_table_has_no_notebook_id(self) -> None:
        self.assertNotIn("notebook_id", _column_names(self.db, "notes"))

    def test_notes_table_keeps_core_columns(self) -> None:
        self.assertEqual(
            _column_names(self.db, "notes"),
            {
                "id", "title", "source", "snippet",
                "created_at", "modified_at",
            },
        )

    def test_note_tags_junction_table_present(self) -> None:
        self.assertIn("note_tags", _all_table_names(self.db))
        self.assertEqual(
            _column_names(self.db, "note_tags"),
            {"note_id", "tag"},
        )

    def test_note_tags_index_present(self) -> None:
        self.assertIn("idx_note_tags_tag", _all_index_names(self.db))

    def test_notebook_depth_triggers_dropped(self) -> None:
        triggers = _all_trigger_names(self.db)
        self.assertNotIn("notebooks_no_deep_nesting_insert", triggers)
        self.assertNotIn("notebooks_no_deep_nesting_update", triggers)

    def test_notebook_index_dropped(self) -> None:
        self.assertNotIn("idx_notes_notebook", _all_index_names(self.db))

    def test_attachments_table_has_no_mime_type(self) -> None:
        # v4 dropped the unused classification column; the BLOB and
        # the metadata columns survive.
        self.assertEqual(
            _column_names(self.db, "attachments"),
            {"id", "note_id", "filename", "byte_size", "data"},
        )


# ---------------------------------------------------------------------------
# Seed welcome note still lands; its tags backfill from :tags: welcome
# ---------------------------------------------------------------------------


class SeedWelcomeNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        apply_pending(self.db, now=_FIXED_NOW)

    def test_welcome_note_is_present(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT id, title, source FROM notes WHERE id = ?",
            (SEED_WELCOME_NOTE_ID,),
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        expected = derive_summary(_WELCOME_SOURCE)
        self.assertEqual(row["title"], expected.title)
        self.assertEqual(row["source"], _WELCOME_SOURCE)

    def test_welcome_note_has_welcome_tag(self) -> None:
        cursor = self.db.connection.execute(
            "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag",
            (SEED_WELCOME_NOTE_ID,),
        )
        tags = [row[0] for row in cursor.fetchall()]
        self.assertEqual(tags, ["welcome"])


# ---------------------------------------------------------------------------
# Frozen-migration golden test: the exact bytes v1 seeds must not drift
# ---------------------------------------------------------------------------


class V1SeedGoldenTests(unittest.TestCase):
    """Pin the exact welcome bytes the v1 migration inserts.

    v1 is a shipped, frozen migration ("never edit a shipped one"): its
    *data behaviour* must not change for any existing upgrade path. The
    welcome source now lives in ``system_docs/welcome.adoc`` and is read
    at seed time, so an accidental edit to that file would silently
    change what v1 seeds. This golden test fixes the exact byte content
    (by length and SHA-256) so such an edit fails loudly instead.

    The digest is over the UTF-8 encoding of the seeded ``source`` column
    — identical to the bytes of ``welcome.adoc`` on disk. If the welcome
    text is *intentionally* revised, that is a new note for users to
    receive via a *new* migration, not an edit to v1; this test failing
    is the reminder.
    """

    _EXPECTED_LENGTH: int = 960
    _EXPECTED_SHA256: str = (
        "26d9fcbb8c098f38e6213a46cb939f4578b281b653554c8fea3992313ff9b5a3"
    )

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        apply_pending(self.db, now=_FIXED_NOW)

    def _seeded_source(self) -> str:
        cursor = self.db.connection.execute(
            "SELECT source FROM notes WHERE id = ?",
            (SEED_WELCOME_NOTE_ID,),
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        source: str = row["source"]
        return source

    def test_seeded_welcome_source_length_is_pinned(self) -> None:
        self.assertEqual(len(self._seeded_source()), self._EXPECTED_LENGTH)

    def test_seeded_welcome_source_digest_is_pinned(self) -> None:
        digest = hashlib.sha256(
            self._seeded_source().encode("utf-8"),
        ).hexdigest()
        self.assertEqual(digest, self._EXPECTED_SHA256)

    def test_loader_source_matches_seeded_source(self) -> None:
        # The migration and the loader must agree: what v1 writes is what
        # ``system_docs`` serves, so the two can never diverge silently.
        self.assertEqual(self._seeded_source(), _WELCOME_SOURCE)


# ---------------------------------------------------------------------------
# v3 tag backfill exercises every note's :tags: header
# ---------------------------------------------------------------------------


class V3TagBackfillTests(unittest.TestCase):
    """A v2-shaped database upgraded to v3 has note_tags populated from
    every note's source."""

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        # Run only the first two migrations to produce a v2-state DB.
        v1_and_v2 = tuple(m for m in ALL_MIGRATIONS if m.version <= 2)
        self.db.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL PRIMARY KEY)"
        )
        for migration in v1_and_v2:
            with self.db.transaction() as connection:
                migration.apply(connection, _FIXED_NOW)
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (migration.version,),
                )

    def _insert_pre_v3_note(
        self,
        *,
        note_id: str,
        source: str,
    ) -> None:
        # Direct insert against the v1+v2 schema (with notebook_id).
        summary = derive_summary(source)
        timestamp = _FIXED_NOW.isoformat()
        self.db.connection.execute(
            "INSERT INTO notes "
            "(id, title, notebook_id, source, snippet, created_at, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                note_id, summary.title, "seed-personal",
                source, summary.snippet, timestamp, timestamp,
            ),
        )

    def test_v3_backfills_tags_for_every_note(self) -> None:
        self._insert_pre_v3_note(
            note_id="n-baking",
            source="= Sourdough\n:tags: baking, bread\n\nA loaf.",
        )
        self._insert_pre_v3_note(
            note_id="n-untagged",
            source="= No tags here\n\nbody only",
        )
        self._insert_pre_v3_note(
            note_id="n-broken",
            source="= Broken tag header\n:tags: foo bar\n\nbody",
        )
        # Now finish the upgrade.
        apply_pending(self.db, now=_FIXED_NOW)

        # n-baking has its two derived tags.
        baking_tags = [
            row[0] for row in self.db.connection.execute(
                "SELECT tag FROM note_tags WHERE note_id = 'n-baking' "
                "ORDER BY tag"
            )
        ]
        self.assertEqual(baking_tags, ["baking", "bread"])

        # n-untagged has none.
        untagged_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM note_tags WHERE note_id = 'n-untagged'"
        ).fetchone()[0]
        self.assertEqual(untagged_count, 0)

        # n-broken (BAD_TAG_VALUE) backfills as untagged via the
        # permissive fallback. The migration must not abort on it.
        broken_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM note_tags WHERE note_id = 'n-broken'"
        ).fetchone()[0]
        self.assertEqual(broken_count, 0)


# ---------------------------------------------------------------------------
# v4 drops attachments.mime_type, preserving existing rows
# ---------------------------------------------------------------------------


class V4MimeTypeColumnDropTests(unittest.TestCase):
    """A v3-shaped database upgraded through v4 keeps its attachment
    rows (metadata and BLOB alike) while losing the column."""

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        # Run only the first three migrations to produce a v3-state DB
        # (whose attachments table still carries ``mime_type``).
        v1_to_v3 = tuple(m for m in ALL_MIGRATIONS if m.version <= 3)
        self.db.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL PRIMARY KEY)"
        )
        for migration in v1_to_v3:
            with self.db.transaction() as connection:
                migration.apply(connection, _FIXED_NOW)
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (migration.version,),
                )
        # Seed one attachment against the welcome note using the
        # pre-v4 column set — exactly what an existing database holds.
        self.db.connection.execute(
            "INSERT INTO attachments "
            "(id, note_id, filename, byte_size, mime_type, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "att-legacy", SEED_WELCOME_NOTE_ID, "cat.png",
                3, "image/png", b"\x01\x02\x03",
            ),
        )

    def test_v3_state_still_has_the_column(self) -> None:
        # Pin the fixture: before v4 runs the column exists, so the
        # drop below is exercising a real transition.
        self.assertIn("mime_type", _column_names(self.db, "attachments"))

    def test_v4_drops_the_column(self) -> None:
        apply_pending(self.db, now=_FIXED_NOW)
        self.assertEqual(
            _column_names(self.db, "attachments"),
            {"id", "note_id", "filename", "byte_size", "data"},
        )

    def test_existing_rows_survive_the_drop(self) -> None:
        apply_pending(self.db, now=_FIXED_NOW)
        row = self.db.connection.execute(
            "SELECT id, note_id, filename, byte_size, data "
            "FROM attachments WHERE id = 'att-legacy'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["note_id"], SEED_WELCOME_NOTE_ID)
        self.assertEqual(row["filename"], "cat.png")
        self.assertEqual(row["byte_size"], 3)
        self.assertEqual(bytes(row["data"]), b"\x01\x02\x03")


# ---------------------------------------------------------------------------
# Migration registry shape
# ---------------------------------------------------------------------------


class MigrationRegistryTests(unittest.TestCase):
    def test_versions_are_monotonic_and_one_indexed(self) -> None:
        versions = [m.version for m in ALL_MIGRATIONS]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(versions[0], 1)


if __name__ == "__main__":
    unittest.main()
