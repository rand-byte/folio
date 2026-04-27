"""SQLite-backed implementation of :class:`NoteRepositoryProtocol`.

Principles & invariants
-----------------------
* Every public method is atomic with respect to the database. Reads
  use the connection directly (autocommit means no implicit transaction
  is opened); writes wrap their statements in
  :meth:`Database.transaction` so they compose with any outer
  transaction the caller has already opened.
* Conversion between :class:`Note` dataclasses and SQLite rows happens
  in exactly one place: :func:`_row_to_note` (read) and the parameter
  tuples in each insert/update method (write). ``sqlite3.Row`` objects
  never escape this module.
* Timestamps are persisted as ISO-8601 strings (timezone-aware UTC).
  Round-trip is :meth:`datetime.isoformat` / :meth:`datetime.fromisoformat`.
* Title and snippet are derived from the source in :meth:`update_source`
  so the cached columns track the source. :meth:`insert` accepts pre-
  derived title and snippet from the caller — typically the controller,
  which already had to compute them while constructing the dataclass.
* Methods that target a specific note (:meth:`get`, :meth:`update_source`,
  :meth:`update_notebook`, :meth:`delete`) raise :class:`KeyError` when
  the id is unknown. This matches the dict-like in-memory fake used by
  controller tests so production and test code paths are interchangeable.
* Listing methods sort by ``modified_at DESC`` (the indexed column) so
  the most-recently-edited note is first — matching the design's note
  list. Further sorting (created date, title) is the responsibility of
  :mod:`notes_app.search.note_filter`, which composes on the
  materialised list.
* Search is a substring match across ``title``, ``snippet``, and
  ``source``, case-insensitive for ASCII (SQLite ``LIKE`` default).
  User input is escape-quoted via :func:`_escape_like` so a literal
  ``%`` or ``_`` in the query does not turn into a SQL wildcard.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Final

from notes_app.models.note import Note, derive_snippet, derive_title
from notes_app.storage.database import Database


_SELECT_FIELDS: Final[str] = (
    "id, title, notebook_id, source, snippet, created_at, modified_at"
)
"""Column list reused by every read query.

Defined once so changes propagate uniformly and so the queries below
remain narrowly scoped to the columns the dataclass actually carries.
"""

_LIKE_ESCAPE_CHAR: Final[str] = "\\"
"""Escape character announced via ``ESCAPE`` clauses in LIKE queries."""


class NoteRepository:
    """Concrete implementation of :class:`NoteRepositoryProtocol`."""

    _db: Database

    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, note_id: str) -> Note:
        cursor = self._db.connection.execute(
            f"SELECT {_SELECT_FIELDS} FROM notes WHERE id = ?",
            (note_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(note_id)
        return _row_to_note(row)

    def list_by_notebook(self, notebook_id: str) -> list[Note]:
        cursor = self._db.connection.execute(
            f"SELECT {_SELECT_FIELDS} FROM notes "
            "WHERE notebook_id = ? ORDER BY modified_at DESC",
            (notebook_id,),
        )
        return [_row_to_note(row) for row in cursor.fetchall()]

    def list_modified_since(self, since: datetime) -> list[Note]:
        cursor = self._db.connection.execute(
            f"SELECT {_SELECT_FIELDS} FROM notes "
            "WHERE modified_at >= ? ORDER BY modified_at DESC",
            (since.isoformat(),),
        )
        return [_row_to_note(row) for row in cursor.fetchall()]

    def list_all(self) -> list[Note]:
        cursor = self._db.connection.execute(
            f"SELECT {_SELECT_FIELDS} FROM notes ORDER BY modified_at DESC"
        )
        return [_row_to_note(row) for row in cursor.fetchall()]

    def search(self, query: str) -> list[Note]:
        pattern = f"%{_escape_like(query)}%"
        cursor = self._db.connection.execute(
            f"SELECT {_SELECT_FIELDS} FROM notes "
            "WHERE title LIKE ? ESCAPE ? "
            "OR snippet LIKE ? ESCAPE ? "
            "OR source LIKE ? ESCAPE ? "
            "ORDER BY modified_at DESC",
            (
                pattern, _LIKE_ESCAPE_CHAR,
                pattern, _LIKE_ESCAPE_CHAR,
                pattern, _LIKE_ESCAPE_CHAR,
            ),
        )
        return [_row_to_note(row) for row in cursor.fetchall()]

    def insert(self, note: Note) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO notes "
                "(id, title, notebook_id, source, snippet, "
                " created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    note.id,
                    note.title,
                    note.notebook_id,
                    note.source,
                    note.snippet,
                    note.created_at.isoformat(),
                    note.modified_at.isoformat(),
                ),
            )

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> None:
        # Title and snippet are derived here, not in the controller, so
        # there is exactly one place that owns the "source -> cached
        # columns" mapping. A controller change can never forget to
        # update one of them.
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE notes "
                "SET source = ?, title = ?, snippet = ?, modified_at = ? "
                "WHERE id = ?",
                (
                    source,
                    derive_title(source),
                    derive_snippet(source),
                    modified_at.isoformat(),
                    note_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(note_id)

    def update_notebook(self, note_id: str, notebook_id: str) -> None:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE notes SET notebook_id = ? WHERE id = ?",
                (notebook_id, note_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(note_id)

    def delete(self, note_id: str) -> None:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM notes WHERE id = ?",
                (note_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(note_id)


def _row_to_note(row: sqlite3.Row) -> Note:
    """Build a :class:`Note` from a database row.

    The conversion is the only place ISO-8601 timestamps become
    :class:`datetime` and the only place ``sqlite3.Row`` is read. Both
    inversions live in the public methods above.
    """
    return Note(
        id=row["id"],
        title=row["title"],
        notebook_id=row["notebook_id"],
        source=row["source"],
        snippet=row["snippet"],
        created_at=datetime.fromisoformat(row["created_at"]),
        modified_at=datetime.fromisoformat(row["modified_at"]),
    )


def _escape_like(text: str) -> str:
    """Escape SQL ``LIKE`` wildcards so user input is treated as literal.

    Escapes the escape character itself first to avoid double-processing,
    then ``%`` (any-chars) and ``_`` (single-char). Used in conjunction
    with an ``ESCAPE '\\\\'`` clause in the query.
    """
    return (
        text
        .replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR + _LIKE_ESCAPE_CHAR)
        .replace("%", _LIKE_ESCAPE_CHAR + "%")
        .replace("_", _LIKE_ESCAPE_CHAR + "_")
    )
