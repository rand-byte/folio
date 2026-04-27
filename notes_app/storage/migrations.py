"""Versioned schema migrations for the SQLite database.

Principles & invariants
-----------------------
* This module owns the schema. The only place a ``CREATE TABLE`` /
  ``CREATE INDEX`` / ``CREATE TRIGGER`` is issued anywhere in the app is
  inside one of the migration functions defined here. Repositories
  accept the schema as a given; they never patch it at runtime.
* Migrations are append-only. Each :class:`Migration` has a stable,
  monotonically-increasing :attr:`Migration.version`. A shipped
  migration is never edited, removed, or re-numbered — only superseded
  by a later one. This is the contract that lets users upgrade across
  releases without losing data.
* :func:`apply_pending` is idempotent: invoking it on a database that
  is already at the latest version is a no-op. Each migration runs
  inside its own transaction (composed via :meth:`Database.transaction`),
  so a partial failure leaves the database at the last successfully-
  applied version.
* The seed data (notebooks + welcome note) is part of the v1 migration
  and is applied **exactly once**: the ``schema_version`` table records
  that v1 has run, and v1 is never replayed. A user who deletes the
  welcome note will not see it reappear on the next launch.
* The migration runner does not import from :mod:`notes_app.storage.note_repository`
  or :mod:`notes_app.storage.notebook_repository`. The repositories
  depend on the schema being in place; the schema is set up here. Going
  the other way would create a cycle.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from notes_app.config.defaults import (
    SEED_NOTEBOOKS,
    SEED_WELCOME_NOTE_ID,
    SEED_WELCOME_NOTE_NOTEBOOK_ID,
    SEED_WELCOME_NOTE_SOURCE,
)
from notes_app.models.note import derive_snippet, derive_title
from notes_app.storage._notebook_writes import insert_notebook_row
from notes_app.storage.database import Database


# ---------------------------------------------------------------------------
# v1 schema — every CREATE statement executed on a fresh database
# ---------------------------------------------------------------------------

_V1_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE notebooks (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        parent_id   TEXT REFERENCES notebooks(id) ON DELETE RESTRICT,
        icon        TEXT NOT NULL,
        sort_order  INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Triggers enforce the two-level depth invariant on both INSERT and
    # UPDATE OF parent_id. SQLite CHECK constraints can't reference other
    # rows, so triggers are the right shape here.
    """
    CREATE TRIGGER notebooks_no_deep_nesting_insert
    BEFORE INSERT ON notebooks
    WHEN NEW.parent_id IS NOT NULL
      AND (SELECT parent_id FROM notebooks WHERE id = NEW.parent_id) IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'NestingTooDeep');
    END
    """,
    """
    CREATE TRIGGER notebooks_no_deep_nesting_update
    BEFORE UPDATE OF parent_id ON notebooks
    WHEN NEW.parent_id IS NOT NULL
      AND (SELECT parent_id FROM notebooks WHERE id = NEW.parent_id) IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'NestingTooDeep');
    END
    """,
    """
    CREATE TABLE notes (
        id           TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        notebook_id  TEXT NOT NULL REFERENCES notebooks(id) ON DELETE RESTRICT,
        source       TEXT NOT NULL,
        snippet      TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        modified_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_notes_notebook ON notes(notebook_id)",
    "CREATE INDEX idx_notes_modified ON notes(modified_at DESC)",
    # Attachments table is created here, in v1, even though the
    # AttachmentStore implementation arrives in build step 11. Creating
    # it now means we don't need a v2 migration just to add a table —
    # and the rest of the schema (notes.id with ON DELETE CASCADE) can
    # rely on its existence from day one.
    """
    CREATE TABLE attachments (
        id          TEXT PRIMARY KEY,
        note_id     TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        filename    TEXT NOT NULL,
        byte_size   INTEGER NOT NULL,
        mime_type   TEXT NOT NULL,
        data        BLOB NOT NULL
    )
    """,
    "CREATE INDEX idx_attachments_note ON attachments(note_id)",
)


_SCHEMA_VERSION_TABLE_SQL: str = (
    "CREATE TABLE IF NOT EXISTS schema_version "
    "(version INTEGER NOT NULL PRIMARY KEY)"
)


# ---------------------------------------------------------------------------
# Migration container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """A single migration step.

    Migrations are addressed by :attr:`version`. The :attr:`apply`
    callable receives the active connection (already inside a
    transaction managed by :func:`apply_pending`) and a timestamp the
    runner uses for any time-dependent seed data — passing it in keeps
    the migration deterministic for tests.
    """

    version: int
    apply: Callable[[sqlite3.Connection, datetime], None]


# ---------------------------------------------------------------------------
# v1 migration body
# ---------------------------------------------------------------------------


def _apply_v1(connection: sqlite3.Connection, now: datetime) -> None:
    for statement in _V1_SCHEMA:
        connection.execute(statement)

    # Seeds: top-level notebooks first, children second. The tuple in
    # ``defaults`` is already in this order; ``enumerate`` gives every
    # notebook a stable sort_order so the sidebar matches the design.
    for sort_order, notebook in enumerate(SEED_NOTEBOOKS):
        insert_notebook_row(connection, notebook, sort_order)

    timestamp = now.isoformat()
    connection.execute(
        "INSERT INTO notes "
        "(id, title, notebook_id, source, snippet, created_at, modified_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            SEED_WELCOME_NOTE_ID,
            derive_title(SEED_WELCOME_NOTE_SOURCE),
            SEED_WELCOME_NOTE_NOTEBOOK_ID,
            SEED_WELCOME_NOTE_SOURCE,
            derive_snippet(SEED_WELCOME_NOTE_SOURCE),
            timestamp,
            timestamp,
        ),
    )


# ---------------------------------------------------------------------------
# Migration registry — append-only
# ---------------------------------------------------------------------------

ALL_MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, apply=_apply_v1),
)


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------


def apply_pending(
    database: Database,
    *,
    now: datetime | None = None,
) -> None:
    """Apply every migration whose version is above the current DB version.

    The first call on a fresh database creates ``schema_version`` and
    every domain table, index, and trigger, then seeds notebooks and
    the welcome note. Subsequent calls on a current database are a
    no-op.

    Parameters
    ----------
    database:
        An open :class:`Database`. Migrations run in transactions
        managed via :meth:`Database.transaction`.
    now:
        The timestamp used for any migration-generated timestamps —
        currently the welcome note's ``created_at`` and ``modified_at``.
        Defaults to ``datetime.now(UTC)``; tests pass a fixed value so
        their assertions are deterministic.
    """
    effective_now = datetime.now(UTC) if now is None else now

    # Bootstrap: the version table is created outside a transaction so
    # we can read it consistently below. ``IF NOT EXISTS`` keeps this
    # idempotent even if a previous run died after creating the table
    # but before applying v1.
    database.connection.execute(_SCHEMA_VERSION_TABLE_SQL)
    cursor = database.connection.execute(
        "SELECT MAX(version) FROM schema_version"
    )
    fetched = cursor.fetchone()
    current = fetched[0] if fetched is not None and fetched[0] is not None else 0

    for migration in ALL_MIGRATIONS:
        if migration.version <= current:
            continue
        with database.transaction() as connection:
            migration.apply(connection, effective_now)
            connection.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (migration.version,),
            )


def current_schema_version(database: Database) -> int:
    """Return the highest applied schema version, or 0 if none.

    Useful for diagnostics and tests. If ``schema_version`` does not
    exist yet (no migration ever applied), returns 0 without creating
    the table.
    """
    cursor = database.connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'schema_version'"
    )
    if cursor.fetchone() is None:
        return 0
    cursor = database.connection.execute(
        "SELECT MAX(version) FROM schema_version"
    )
    fetched = cursor.fetchone()
    if fetched is None or fetched[0] is None:
        return 0
    return int(fetched[0])
