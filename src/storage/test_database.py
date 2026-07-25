"""Tests for :mod:`storage.database`."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from config.defaults import SQLITE_BUSY_TIMEOUT_MS
from enums import PragmaEnforcement, SqlitePragma, SqliteJournalMode
from storage.database import _CONNECTION_PRAGMAS, Database


def _create_test_table(database: Database) -> None:
    """Create a small ``items`` table used by the transaction tests."""
    database.connection.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
    )


def _count_rows(database: Database) -> int:
    cursor = database.connection.execute("SELECT COUNT(*) FROM items")
    return int(cursor.fetchone()[0])


# ---------------------------------------------------------------------------
# Construction & basic configuration
# ---------------------------------------------------------------------------


class DatabaseConstructionTests(unittest.TestCase):
    def test_in_memory_factory_returns_database(self) -> None:
        db = Database.in_memory()
        self.addCleanup(db.close)
        self.assertIsInstance(db, Database)

    def test_in_memory_databases_are_independent(self) -> None:
        # Two ``:memory:`` connections each have their own database;
        # writes to one are invisible to the other.
        a = Database.in_memory()
        self.addCleanup(a.close)
        b = Database.in_memory()
        self.addCleanup(b.close)
        _create_test_table(a)
        with self.assertRaises(sqlite3.OperationalError):
            # b doesn't have the items table; "no such table" should fire.
            b.connection.execute("SELECT * FROM items")

    def test_accepts_pathlib_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.db"
            db = Database(path)
            self.addCleanup(db.close)
            _create_test_table(db)
            db.connection.execute("INSERT INTO items VALUES (1, 'a')")
            db.close()

            # Re-open the same file; the row must persist.
            db2 = Database(path)
            self.addCleanup(db2.close)
            self.assertEqual(_count_rows(db2), 1)

    def test_foreign_keys_pragma_is_on(self) -> None:
        db = Database.in_memory()
        self.addCleanup(db.close)
        cursor = db.connection.execute("PRAGMA foreign_keys")
        self.assertEqual(cursor.fetchone()[0], 1)

    def test_row_factory_is_sqlite3_row(self) -> None:
        db = Database.in_memory()
        self.addCleanup(db.close)
        self.assertIs(db.connection.row_factory, sqlite3.Row)

    def test_autocommit_mode_no_implicit_transaction(self) -> None:
        # With autocommit=True the connection is *not* in a transaction
        # immediately after a write — the driver does not open one for us.
        db = Database.in_memory()
        self.addCleanup(db.close)
        _create_test_table(db)
        db.connection.execute("INSERT INTO items VALUES (1, 'x')")
        self.assertFalse(db.connection.in_transaction)


# ---------------------------------------------------------------------------
# Connection pragmas (WAL / busy_timeout / foreign_keys)
# ---------------------------------------------------------------------------


class ConnectionPragmaTableTests(unittest.TestCase):
    """The declarative pragma table is well-formed — pure, no connection."""

    def test_every_pragma_appears_exactly_once(self) -> None:
        # Mirrors the exhaustiveness habit used elsewhere (the help-doc
        # coverage test): each SqlitePragma member is configured, and none
        # twice, so the table cannot silently drop or duplicate a setting.
        configured = [setting.pragma for setting in _CONNECTION_PRAGMAS]
        self.assertCountEqual(configured, list(SqlitePragma))

    def test_foreign_keys_is_applied_first(self) -> None:
        # foreign_keys must precede any FK-dependent statement, so it leads
        # the ordered tuple.
        self.assertEqual(
            _CONNECTION_PRAGMAS[0].pragma, SqlitePragma.FOREIGN_KEYS
        )

    def test_foreign_keys_is_required(self) -> None:
        # A silently-ignored foreign_keys breaks ON DELETE CASCADE, so it
        # is a REQUIRED (verified) setting, never best-effort.
        (setting,) = [
            s
            for s in _CONNECTION_PRAGMAS
            if s.pragma is SqlitePragma.FOREIGN_KEYS
        ]
        self.assertIs(setting.enforcement, PragmaEnforcement.REQUIRED)

    def test_journal_mode_is_best_effort(self) -> None:
        # WAL is requested, not required: :memory: and shared-memory-less
        # filesystems legitimately keep their existing mode.
        (setting,) = [
            s
            for s in _CONNECTION_PRAGMAS
            if s.pragma is SqlitePragma.JOURNAL_MODE
        ]
        self.assertIs(setting.enforcement, PragmaEnforcement.BEST_EFFORT)


class ConnectionPragmaEffectTests(unittest.TestCase):
    """The pragmas take effect on a real connection."""

    def test_file_database_uses_wal_journal_mode(self) -> None:
        # WAL needs a real file (shared memory) — it cannot apply to an
        # in-memory database, so this is the file-backed case.
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "wal.db")
            self.addCleanup(db.close)
            mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, SqliteJournalMode.WAL.value)

    def test_in_memory_database_reports_memory_mode(self) -> None:
        # The best-effort journal-mode request must not raise on :memory:,
        # which always reports "memory" — this is what keeps the whole
        # storage test suite (all in-memory) working unchanged.
        db = Database.in_memory()
        self.addCleanup(db.close)
        mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, SqliteJournalMode.MEMORY.value)

    def test_busy_timeout_is_set_from_defaults(self) -> None:
        db = Database.in_memory()
        self.addCleanup(db.close)
        timeout = db.connection.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, SQLITE_BUSY_TIMEOUT_MS)

    def test_wal_persists_across_reopen_without_rerequest(self) -> None:
        # Journal mode is stored in the database file header, so a file
        # opened once in WAL stays WAL on the next open — the pragma is a
        # one-time migration of the file, not a per-launch necessity.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "persist.db"
            first = Database(path)
            _create_test_table(first)
            first.close()

            second = Database(path)
            self.addCleanup(second.close)
            mode = second.connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            self.assertEqual(mode, SqliteJournalMode.WAL.value)

    def test_wal_sidecars_are_removed_on_close(self) -> None:
        # The -wal/-shm sidecars exist while a connection is open and are
        # checkpointed away when the last connection closes. This is the
        # property that makes closing the database on shutdown matter.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sidecar.db"
            wal = path.with_name(path.name + "-wal")
            db = Database(path)
            _create_test_table(db)
            with db.transaction() as conn:
                conn.execute("INSERT INTO items VALUES (1, 'a')")
            self.assertTrue(wal.exists())

            db.close()
            self.assertFalse(wal.exists())


# ---------------------------------------------------------------------------
# Transaction shape — outermost
# ---------------------------------------------------------------------------


class OutermostTransactionTests(unittest.TestCase):
    db: Database

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        _create_test_table(self.db)

    def test_commit_on_normal_exit(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO items VALUES (1, 'a')")
        self.assertEqual(_count_rows(self.db), 1)

    def test_rollback_on_exception(self) -> None:
        class _Sentinel(RuntimeError):
            pass

        with self.assertRaises(_Sentinel):
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO items VALUES (1, 'a')")
                raise _Sentinel("kaboom")
        self.assertEqual(_count_rows(self.db), 0)

    def test_yields_the_wrapped_connection(self) -> None:
        with self.db.transaction() as connection:
            self.assertIs(connection, self.db.connection)

    def test_in_transaction_property_tracks_state(self) -> None:
        self.assertFalse(self.db.in_transaction)
        with self.db.transaction():
            self.assertTrue(self.db.in_transaction)
        self.assertFalse(self.db.in_transaction)

    def test_in_transaction_resets_after_exception(self) -> None:
        class _Sentinel(RuntimeError):
            pass

        with self.assertRaises(_Sentinel):
            with self.db.transaction():
                raise _Sentinel
        self.assertFalse(self.db.in_transaction)

    def test_underlying_connection_in_transaction_during_block(self) -> None:
        # Sanity that we issued BEGIN and SQLite sees us inside a
        # transaction, not just our own depth counter.
        with self.db.transaction():
            self.assertTrue(self.db.connection.in_transaction)
        self.assertFalse(self.db.connection.in_transaction)


# ---------------------------------------------------------------------------
# Transaction shape — nested (savepoints)
# ---------------------------------------------------------------------------


class NestedTransactionTests(unittest.TestCase):
    db: Database

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        _create_test_table(self.db)

    def test_inner_commit_persists_via_outer_commit(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO items VALUES (1, 'outer')")
            with self.db.transaction() as inner:
                inner.execute("INSERT INTO items VALUES (2, 'inner')")
        self.assertEqual(_count_rows(self.db), 2)

    def test_inner_rollback_keeps_outer_intact(self) -> None:
        class _Sentinel(RuntimeError):
            pass

        with self.db.transaction() as connection:
            connection.execute("INSERT INTO items VALUES (1, 'outer')")
            with self.assertRaises(_Sentinel):
                with self.db.transaction() as inner:
                    inner.execute("INSERT INTO items VALUES (2, 'inner')")
                    raise _Sentinel
            # We're back inside the outer transaction with the inner
            # rolled back — exactly one row visible at this point.
            self.assertEqual(_count_rows(self.db), 1)
        # Outer commits; still exactly one row.
        self.assertEqual(_count_rows(self.db), 1)

    def test_outer_rollback_drops_committed_inner(self) -> None:
        # Even though the inner block committed (released its savepoint),
        # the outer rollback must take it down with everything else —
        # transactions compose; intermediate savepoint releases don't
        # escape the parent.
        class _Sentinel(RuntimeError):
            pass

        with self.assertRaises(_Sentinel):
            with self.db.transaction() as connection:
                with self.db.transaction() as inner:
                    inner.execute("INSERT INTO items VALUES (1, 'inner')")
                connection.execute("INSERT INTO items VALUES (2, 'outer')")
                raise _Sentinel
        self.assertEqual(_count_rows(self.db), 0)

    def test_depth_tracks_nesting(self) -> None:
        self.assertEqual(self.db.in_transaction, False)
        with self.db.transaction():
            self.assertTrue(self.db.in_transaction)
            with self.db.transaction():
                self.assertTrue(self.db.in_transaction)
            self.assertTrue(self.db.in_transaction)
        self.assertFalse(self.db.in_transaction)

    def test_three_deep_savepoints_all_release(self) -> None:
        with self.db.transaction() as outer:
            outer.execute("INSERT INTO items VALUES (1, 'lvl1')")
            with self.db.transaction() as mid:
                mid.execute("INSERT INTO items VALUES (2, 'lvl2')")
                with self.db.transaction() as inner:
                    inner.execute("INSERT INTO items VALUES (3, 'lvl3')")
        self.assertEqual(_count_rows(self.db), 3)

    def test_savepoint_names_are_unique_per_depth(self) -> None:
        # If savepoint names collided across a single nested stack,
        # SQLite would error on the inner SAVEPOINT statement. This
        # nested test pattern is the simplest way to assert that.
        with self.db.transaction():
            with self.db.transaction():
                with self.db.transaction():
                    pass

    def test_depth_resets_after_nested_exception(self) -> None:
        class _Sentinel(RuntimeError):
            pass

        with self.assertRaises(_Sentinel):
            with self.db.transaction():
                with self.db.transaction():
                    raise _Sentinel
        self.assertFalse(self.db.in_transaction)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class DatabaseLifecycleTests(unittest.TestCase):
    def test_close_is_idempotent(self) -> None:
        db = Database.in_memory()
        db.close()
        # A second close() must not raise; sqlite3.Connection.close()
        # is itself idempotent and our wrapper just delegates.
        db.close()

    def test_context_manager_closes_on_exit(self) -> None:
        with Database.in_memory() as db:
            _create_test_table(db)
        # After exit the connection is closed; using it raises
        # ProgrammingError.
        with self.assertRaises(sqlite3.ProgrammingError):
            db.connection.execute("SELECT 1")

    def test_context_manager_closes_on_exception(self) -> None:
        class _Sentinel(RuntimeError):
            pass

        captured: Database | None = None
        with self.assertRaises(_Sentinel):
            with Database.in_memory() as db:
                captured = db
                raise _Sentinel
        self.assertIsNotNone(captured)
        assert captured is not None
        with self.assertRaises(sqlite3.ProgrammingError):
            captured.connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
