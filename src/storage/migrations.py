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
  releases without losing data. v1 (notebooks + welcome note) and v2
  (cached-column backfill) are byte-identical to their shipped form;
  v3 demolishes the notebook schema and introduces the ``note_tags``
  junction table; v4 drops the unused ``attachments.mime_type`` column.
* :func:`apply_pending` is idempotent: invoking it on a database that
  is already at the latest version is a no-op. Each migration runs
  inside its own transaction (composed via :meth:`Database.transaction`),
  so a partial failure leaves the database at the last successfully-
  applied version.
* The seed welcome note is part of the v1 migration and is applied
  **exactly once**: the ``schema_version`` table records that v1 has
  run, and v1 is never replayed. A user who deletes the welcome note
  will not see it reappear on the next launch. v1 reads the welcome
  *source* directly from the ``system_docs`` package
  (:data:`enums.SystemDocument.WELCOME`) via the gi-free
  :func:`system_docs.load_text` loader — the same config-tier home the
  help window reads from — rather than a ``config`` constant; ``storage``
  stays gi-free because that loader uses only :func:`importlib.resources`.
* This module derives cached note columns through
  :func:`asciidoc.summary.derive_summary` (the v1 seed, the v2
  backfill, and the v3 tag backfill). ``storage`` is allowed to import
  the pure ``asciidoc`` core; the edge is acyclic because ``asciidoc``
  imports nothing from ``storage``.
* The migration runner does not import from
  :mod:`storage.note_repository`. The repository depends on the schema
  being in place; the schema is set up here. Going the other way would
  create a cycle.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from asciidoc.summary import derive_summary
from config.defaults import SEED_WELCOME_NOTE_ID
from enums import SystemDocument
from storage.database import Database
from system_docs import load_text


# ---------------------------------------------------------------------------
# v1 schema — every CREATE statement executed on a fresh database
# ---------------------------------------------------------------------------
#
# NOTE: v1 ships forever as-is. v3 demolishes the notebooks table and
# the ``notes.notebook_id`` column. We keep v1's original DDL intact so
# upgrade paths from older databases run the same statements they
# originally did, then have them undone by v3.

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


# Notebook seed data is a v1 fixture; it lives here (not in
# config/defaults.py) because every higher layer should have no
# knowledge of notebooks. The v1 migration is the only consumer.
_V1_SEED_NOTEBOOK_ID_PERSONAL: str = "seed-personal"
_V1_SEED_NOTEBOOK_ROWS: tuple[
    tuple[str, str, str | None, str], ...,
] = (
    (_V1_SEED_NOTEBOOK_ID_PERSONAL, "Personal", None, "home"),
    ("seed-recipes", "Recipes", None, "book"),
    ("seed-baking", "Baking", "seed-recipes", "book"),
    ("seed-weeknight-dinners", "Weeknight dinners", "seed-recipes", "book"),
    ("seed-travel", "Travel", None, "map"),
    ("seed-learning", "Learning", None, "brain"),
    ("seed-archive", "Archive", None, "archive"),
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

    # Seeds: top-level notebooks first, children second (already in
    # that order); ``enumerate`` gives every notebook a stable
    # sort_order. v3 will drop all of this; the rows must be inserted
    # under v1 so existing databases that upgrade across v1 → v3 see
    # the same v1 state.
    for sort_order, row in enumerate(_V1_SEED_NOTEBOOK_ROWS):
        notebook_id, name, parent_id, icon = row
        connection.execute(
            "INSERT INTO notebooks "
            "(id, name, parent_id, icon, sort_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (notebook_id, name, parent_id, icon, sort_order),
        )

    timestamp = now.isoformat()
    # The welcome source is config-tier package data in ``system_docs``,
    # read gi-free via ``importlib.resources`` — the same mechanism the
    # help window uses for its own source and image. v1 is a frozen
    # migration: reading the bytes from a file instead of a module-level
    # constant preserves its data behaviour exactly (a golden test pins
    # the seeded bytes against an accidental future edit to welcome.adoc).
    welcome_source = load_text(SystemDocument.WELCOME)
    welcome_summary = derive_summary(welcome_source)
    connection.execute(
        "INSERT INTO notes "
        "(id, title, notebook_id, source, snippet, created_at, modified_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            SEED_WELCOME_NOTE_ID,
            welcome_summary.title,
            _V1_SEED_NOTEBOOK_ID_PERSONAL,
            welcome_source,
            welcome_summary.snippet,
            timestamp,
            timestamp,
        ),
    )


# ---------------------------------------------------------------------------
# v2 migration body — backfill title/snippet from derive_summary
# ---------------------------------------------------------------------------


def _apply_v2(connection: sqlite3.Connection, now: datetime) -> None:
    """Rewrite every note's cached ``title`` / ``snippet`` columns."""
    _ = now
    cursor = connection.execute("SELECT id, source FROM notes")
    rows = cursor.fetchall()
    for row in rows:
        summary = derive_summary(row["source"])
        connection.execute(
            "UPDATE notes SET title = ?, snippet = ? WHERE id = ?",
            (summary.title, summary.snippet, row["id"]),
        )


# ---------------------------------------------------------------------------
# v3 migration body — drop notebooks, introduce note_tags junction
# ---------------------------------------------------------------------------


_V3_DDL: tuple[str, ...] = (
    # SQLite refuses to drop a column or table participating in a live
    # FK relationship while ``PRAGMA foreign_keys = ON`` is enforcing
    # each statement individually. The defer pragma postpones FK checks
    # to COMMIT time; by COMMIT the FK column and the notebooks table
    # are gone, so there is nothing left to enforce. The pragma is
    # transaction-scoped (it resets to 0 when this transaction ends),
    # so connection-wide FK behaviour outside the migration is
    # unchanged.
    "PRAGMA defer_foreign_keys = 1",
    # Drop the notebook depth-enforcing triggers (created in v1).
    "DROP TRIGGER IF EXISTS notebooks_no_deep_nesting_insert",
    "DROP TRIGGER IF EXISTS notebooks_no_deep_nesting_update",
    # Drop the notebook index on the notes table before dropping its column.
    "DROP INDEX IF EXISTS idx_notes_notebook",
    # Drop the notebook FK column on notes. SQLite >= 3.35 supports
    # DROP COLUMN; the project ships against a recent SQLite.
    "ALTER TABLE notes DROP COLUMN notebook_id",
    # Drop the notebooks table itself.
    "DROP TABLE IF EXISTS notebooks",
    # Junction table: note ↔ tag. ``ON DELETE CASCADE`` removes a
    # note's tag rows when the note row is removed.
    """
    CREATE TABLE note_tags (
        note_id TEXT NOT NULL
            REFERENCES notes(id) ON DELETE CASCADE,
        tag     TEXT NOT NULL,
        PRIMARY KEY (note_id, tag)
    )
    """,
    "CREATE INDEX idx_note_tags_tag ON note_tags(tag)",
)


def _apply_v3(connection: sqlite3.Connection, now: datetime) -> None:
    """Demolish the notebook schema; introduce ``note_tags`` + backfill.

    After running the DDL we re-derive every existing note's tag set
    via :func:`derive_summary` and insert one row per (note, tag) into
    ``note_tags``. ``derive_summary`` never raises, so notes whose
    ``:tags:`` header line is malformed quietly backfill with zero
    tags — matching the rest of the cached-derivation contract.

    ``now`` is unused: a tag backfill does not touch timestamps.
    """
    _ = now
    for statement in _V3_DDL:
        connection.execute(statement)
    cursor = connection.execute("SELECT id, source FROM notes")
    rows = cursor.fetchall()
    for row in rows:
        summary = derive_summary(row["source"])
        for tag in summary.tags:
            connection.execute(
                "INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                (row["id"], tag),
            )


# ---------------------------------------------------------------------------
# v4 migration body — drop the unused attachments.mime_type column
# ---------------------------------------------------------------------------


def _apply_v4(connection: sqlite3.Connection, now: datetime) -> None:
    """Drop ``attachments.mime_type``.

    The column was written and read back but consumed by nothing: the
    renderer sniffs bytes via ``Gdk.Texture`` and the add-time type
    allow-list it once backed is gone (attachments are opaque blobs;
    the size cap is the only remaining gate). SQLite >= 3.35 supports
    ``DROP COLUMN`` — the same facility v3 already relies on. Existing
    rows (including their BLOBs) are preserved.

    ``now`` is unused: a column drop does not touch timestamps.
    """
    _ = now
    connection.execute("ALTER TABLE attachments DROP COLUMN mime_type")


# ---------------------------------------------------------------------------
# Migration registry — append-only
# ---------------------------------------------------------------------------

ALL_MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, apply=_apply_v1),
    Migration(version=2, apply=_apply_v2),
    Migration(version=3, apply=_apply_v3),
    Migration(version=4, apply=_apply_v4),
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
    every domain table, index, and trigger, then seeds the welcome
    note. Subsequent calls on a current database are a no-op.
    """
    effective_now = datetime.now(UTC) if now is None else now

    # Bootstrap.
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
    """Return the highest applied schema version, or 0 if none."""
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
