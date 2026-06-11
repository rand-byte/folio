"""SQLite-backed implementation of :class:`NoteRepositoryProtocol`.

Principles & invariants
-----------------------
* Every public method is atomic with respect to the database. Reads
  use the connection directly (autocommit means no implicit transaction
  is opened); writes wrap their statements in
  :meth:`Database.transaction` so they compose with any outer
  transaction the caller has already opened.
* Conversion between :class:`Note` dataclasses and SQLite rows happens
  in exactly one place: :func:`_assemble_notes` (read) and the parameter
  tuples in each insert/update method (write). ``sqlite3.Row`` objects
  never escape this module.
* Timestamps are persisted as ISO-8601 strings (timezone-aware UTC).
  Round-trip is :meth:`datetime.isoformat` / :meth:`datetime.fromisoformat`.
* Title, snippet, and tags are derived from the source via
  :func:`asciidoc.summary.derive_summary` in both :meth:`insert` and
  :meth:`update_source`, so this repository is the single owner of the
  ``source -> (cached columns + tag rows)`` mapping. The dataclass's
  own ``title`` / ``snippet`` / ``tags`` fields are advisory only on
  insert — the columns always reflect a fresh derive from ``source``.
  On update, the note's ``note_tags`` rows are replaced (deleted, then
  re-inserted) in the same transaction; partial states are never
  visible to other readers.
* :meth:`insert` and :meth:`update_source` **return the note as
  persisted** — the caller's id / source / timestamps combined with
  the freshly-derived ``title`` / ``snippet`` / ``tags``. This lets the
  write-through in-memory model commit the exact row that hit disk
  without a re-read or a re-derive (it cannot call ``derive_summary``
  itself — the controllers layer must not import :mod:`asciidoc`).
  :meth:`update_source` recovers the one field it does not already hold,
  ``created_at``, via ``UPDATE ... RETURNING created_at`` in the same
  round trip.
* :meth:`search`, :meth:`list_modified_since`, and :meth:`list_tags`
  are **no longer on any UI path** after the write-through model
  migration (the note list filters in memory; the sidebar derives tag
  counts from the in-memory store). They remain for legacy callers and
  their own tests; do not add new consumers.
* :meth:`get`, :meth:`update_source`, :meth:`delete` raise
  :class:`KeyError` on an unknown id, matching the dict-like in-memory
  fake used by controller tests so production and test code paths are
  interchangeable.
* Listing methods sort by ``modified_at DESC`` (the indexed column) so
  the most-recently-edited note is first. Further sorting (created date,
  title) is the responsibility of :mod:`search.note_filter`, which
  composes on the materialised list.
* Tags are read by an outer join (``LEFT JOIN note_tags``) so a note
  with zero tags still appears, and the list-builder gets all rows in
  one round trip rather than firing an N+1 per-note query.
* :meth:`list_tags` returns ``((tag, count), ...)`` for every distinct
  tag in use, alphabetically ordered. Empty when no note has any tags.
* Search is a substring match across ``title``, ``snippet``, and
  ``source``, case-insensitive for ASCII (SQLite ``LIKE`` default).
  User input is escape-quoted via :func:`_escape_like` so a literal
  ``%`` or ``_`` in the query does not turn into a SQL wildcard. Tags
  are not searched; the *Tags* sidebar provides direct selection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Final

from asciidoc.summary import derive_summary
from models.note import Note
from storage.database import Database


_LIKE_ESCAPE_CHAR: Final[str] = "\\"
"""Escape character announced via ``ESCAPE`` clauses in LIKE queries."""


_NOTE_FIELDS: Final[str] = (
    "n.id, n.title, n.source, n.snippet, n.created_at, n.modified_at"
)
"""Column list for the ``notes`` half of every read query.

The tag column is added via a left join in :func:`_join_with_tags`.
"""


def _join_with_tags(where_clause: str, order_clause: str) -> str:
    """Compose a query that joins ``notes`` with ``note_tags``.

    The shape is: select the note columns plus the joined tag (or
    NULL), apply ``WHERE``, and order by ``modified_at`` first (so
    multi-tagged notes still cluster correctly under their note row in
    insertion order).
    """
    return (
        f"SELECT {_NOTE_FIELDS}, nt.tag AS tag "
        "FROM notes AS n "
        "LEFT JOIN note_tags AS nt ON nt.note_id = n.id "
        f"{where_clause} "
        f"{order_clause}"
    )


class NoteRepository:
    """Concrete implementation of :class:`NoteRepositoryProtocol`."""

    _db: Database

    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, note_id: str) -> Note:
        cursor = self._db.connection.execute(
            _join_with_tags(
                "WHERE n.id = ?",
                "ORDER BY tag ASC",
            ),
            (note_id,),
        )
        notes = _assemble_notes(cursor.fetchall())
        if not notes:
            raise KeyError(note_id)
        return notes[0]

    def list_modified_since(self, since: datetime) -> list[Note]:
        cursor = self._db.connection.execute(
            _join_with_tags(
                "WHERE n.modified_at >= ?",
                "ORDER BY n.modified_at DESC, n.id, tag ASC",
            ),
            (since.isoformat(),),
        )
        return _assemble_notes(cursor.fetchall())

    def list_all(self) -> list[Note]:
        cursor = self._db.connection.execute(
            _join_with_tags(
                "",
                "ORDER BY n.modified_at DESC, n.id, tag ASC",
            )
        )
        return _assemble_notes(cursor.fetchall())

    def search(self, query: str) -> list[Note]:
        pattern = f"%{_escape_like(query)}%"
        cursor = self._db.connection.execute(
            _join_with_tags(
                "WHERE n.title LIKE ? ESCAPE ? "
                "OR n.snippet LIKE ? ESCAPE ? "
                "OR n.source LIKE ? ESCAPE ?",
                "ORDER BY n.modified_at DESC, n.id, tag ASC",
            ),
            (
                pattern, _LIKE_ESCAPE_CHAR,
                pattern, _LIKE_ESCAPE_CHAR,
                pattern, _LIKE_ESCAPE_CHAR,
            ),
        )
        return _assemble_notes(cursor.fetchall())

    def insert(self, note: Note) -> Note:
        # Title, snippet, and tags are derived here from the note's
        # source, not taken from the dataclass fields, so the repository
        # is the one and only place that maps source to cached state
        # (the same invariant ``update_source`` upholds).
        # ``derive_summary`` never raises, so an unparseable in-progress
        # note is still insertable.
        summary = derive_summary(note.source)
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO notes "
                "(id, title, source, snippet, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    note.id,
                    summary.title,
                    note.source,
                    summary.snippet,
                    note.created_at.isoformat(),
                    note.modified_at.isoformat(),
                ),
            )
            for tag in summary.tags:
                connection.execute(
                    "INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                    (note.id, tag),
                )
        # Return the note exactly as persisted: the caller's ``source``,
        # timestamps, and id combined with the freshly-derived cached
        # fields. ``summary.tags`` is already sorted / lowercased /
        # deduplicated, matching what :meth:`get` would reassemble, so
        # no extra query is needed.
        return Note(
            id=note.id,
            title=summary.title,
            source=note.source,
            snippet=summary.snippet,
            tags=summary.tags,
            created_at=note.created_at,
            modified_at=note.modified_at,
        )

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        # Title, snippet, and tags are derived here, not in the
        # controller, so there is exactly one place that owns the
        # "source -> cached state" mapping. ``derive_summary`` parses
        # once and never raises, so a mid-edit unparseable note stays
        # saveable.
        summary = derive_summary(source)
        with self._db.transaction() as connection:
            # ``RETURNING created_at`` (SQLite >= 3.35) hands back the
            # one field not already in the caller's hand in the same
            # round trip, so the derived :class:`Note` can be returned
            # without a follow-up SELECT. A ``None`` row means no note
            # matched the id.
            cursor = connection.execute(
                "UPDATE notes "
                "SET source = ?, title = ?, snippet = ?, modified_at = ? "
                "WHERE id = ? RETURNING created_at",
                (
                    source,
                    summary.title,
                    summary.snippet,
                    modified_at.isoformat(),
                    note_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(note_id)
            created_at = datetime.fromisoformat(row["created_at"])
            # Tag rows are *replaced* on update so a removed ``:tags:``
            # entry actually disappears from the junction. The DELETE
            # + INSERT pair is atomic with the source UPDATE because
            # all three statements sit inside the same transaction.
            connection.execute(
                "DELETE FROM note_tags WHERE note_id = ?",
                (note_id,),
            )
            for tag in summary.tags:
                connection.execute(
                    "INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                    (note_id, tag),
                )
        return Note(
            id=note_id,
            title=summary.title,
            source=source,
            snippet=summary.snippet,
            tags=summary.tags,
            created_at=created_at,
            modified_at=modified_at,
        )

    def delete(self, note_id: str) -> None:
        # ``note_tags.note_id`` is ``ON DELETE CASCADE``, so deleting
        # the row in ``notes`` drops every tag pairing in one go — no
        # extra DELETE here.
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM notes WHERE id = ?",
                (note_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(note_id)

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        """Return every distinct tag with its note count, alphabetically.

        Driven by a plain ``GROUP BY`` on the junction table. The
        sidebar reads this directly to populate its *Tags* section.
        """
        cursor = self._db.connection.execute(
            "SELECT tag, COUNT(*) AS n "
            "FROM note_tags "
            "GROUP BY tag "
            "ORDER BY tag ASC"
        )
        return tuple((row["tag"], int(row["n"])) for row in cursor.fetchall())


def _assemble_notes(rows: Iterable[sqlite3.Row]) -> list[Note]:
    """Group joined ``(note × tag)`` rows into :class:`Note` instances.

    The query in :func:`_join_with_tags` is a left join, so a note with
    zero tags appears as exactly one row with ``tag`` IS NULL, and a
    note with N tags appears as N rows (the note columns are repeated).
    This function walks the rows in order, accumulating tags onto the
    in-progress note, and emits one :class:`Note` per distinct note id.

    The outer query orders by ``modified_at DESC, n.id, tag ASC`` so
    the per-note row groups arrive contiguously in display order with
    tags already alphabetical.
    """
    notes: list[Note] = []
    current_id: str | None = None
    current_row: sqlite3.Row | None = None
    current_tags: list[str] = []
    for row in rows:
        row_id = row["id"]
        if row_id != current_id:
            if current_row is not None:
                notes.append(_row_to_note(current_row, current_tags))
            current_id = row_id
            current_row = row
            current_tags = []
        tag = row["tag"]
        if tag is not None:
            current_tags.append(tag)
    if current_row is not None:
        notes.append(_row_to_note(current_row, current_tags))
    return notes


def _row_to_note(row: sqlite3.Row, tags: list[str]) -> Note:
    """Build a :class:`Note` from a database row plus its tag list.

    Tags arrive already sorted (the SQL ``ORDER BY tag ASC`` clause).
    """
    return Note(
        id=row["id"],
        title=row["title"],
        source=row["source"],
        snippet=row["snippet"],
        tags=tuple(tags),
        created_at=datetime.fromisoformat(row["created_at"]),
        modified_at=datetime.fromisoformat(row["modified_at"]),
    )


def _escape_like(text: str) -> str:
    """Escape SQL ``LIKE`` wildcards so user input is treated as literal."""
    return (
        text
        .replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR + _LIKE_ESCAPE_CHAR)
        .replace("%", _LIKE_ESCAPE_CHAR + "%")
        .replace("_", _LIKE_ESCAPE_CHAR + "_")
    )
