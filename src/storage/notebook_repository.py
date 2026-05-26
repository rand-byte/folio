"""SQLite-backed implementation of :class:`NotebookRepositoryProtocol`.

Principles & invariants
-----------------------
* The two-level depth rule is enforced at three layers and this module
  is the lowest two of them: a defensive Python-side check inside
  :meth:`insert`, *and* the SQL triggers from
  :mod:`storage.migrations`. Either layer alone would suffice;
  both together catch direct repository misuse and any future code path
  that bypasses :meth:`insert`.
* Trigger-raised aborts surface as :class:`sqlite3.IntegrityError` whose
  message is the literal we passed to ``RAISE(ABORT, ...)``. This module
  recognises that token and re-raises as :class:`NestingTooDeep` so the
  caller never has to know the SQL detail.
* :meth:`delete_and_reparent_notes` runs in a single transaction: notes
  are moved to ``target_id``, any child notebooks become top-level
  (``parent_id = NULL``), and the now-empty notebook row is removed.
  Any failure rolls everything back; there is no intermediate state in
  which notes can orphan.
* Promoting child notebooks to top-level when their parent is deleted
  is the only behaviour that satisfies the two-level invariant when
  there is no obvious new parent — the alternatives (cascade-delete the
  children and their notes, or refuse the operation) either lose data
  silently or strand the user with an undeletable folder.
* New user-created notebooks are appended to the end of the sort order
  (``MAX(sort_order) + 1``). The seed notebooks reserve the first N
  slots; user-created ones follow them and stay in creation order
  unless an explicit reorder operation moves them (not in the v1
  protocol surface).
* Every method that targets a specific notebook raises :class:`KeyError`
  when the id does not exist, matching the dict-like in-memory fake
  used in controller tests.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from enums import NotebookIcon
from models.notebook import Notebook
from storage._notebook_writes import insert_notebook_row
from storage.database import Database
from storage.protocols import NestingTooDeep


_SELECT_FIELDS: Final[str] = "id, name, parent_id, icon"
"""Column list reused by every read query."""

_NESTING_TRIGGER_TOKEN: Final[str] = "NestingTooDeep"
"""The literal token used in ``RAISE(ABORT, ...)`` by both triggers.

Matches what :mod:`storage.migrations` emits in
``notebooks_no_deep_nesting_insert`` and
``notebooks_no_deep_nesting_update``.
"""


class NotebookRepository:
    """Concrete implementation of :class:`NotebookRepositoryProtocol`."""

    _db: Database

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_all(self) -> list[Notebook]:
        cursor = self._db.connection.execute(
            f"SELECT {_SELECT_FIELDS} FROM notebooks "
            "ORDER BY sort_order ASC, name ASC"
        )
        return [_row_to_notebook(row) for row in cursor.fetchall()]

    def get(self, notebook_id: str) -> Notebook:
        cursor = self._db.connection.execute(
            f"SELECT {_SELECT_FIELDS} FROM notebooks WHERE id = ?",
            (notebook_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(notebook_id)
        return _row_to_notebook(row)

    def insert(self, notebook: Notebook) -> None:
        # Defensive Python-side check: refuse before issuing SQL when we
        # can already see that the proposed parent is itself a child.
        # The trigger remains as belt-and-braces; the explicit check is
        # what the tests pin and what gives the controller a precise
        # exception to catch.
        if notebook.parent_id is not None:
            cursor = self._db.connection.execute(
                "SELECT parent_id FROM notebooks WHERE id = ?",
                (notebook.parent_id,),
            )
            parent_row = cursor.fetchone()
            if parent_row is not None and parent_row["parent_id"] is not None:
                raise NestingTooDeep(
                    f"notebook {notebook.id!r} parent {notebook.parent_id!r} "
                    "is itself a child"
                )

        with self._db.transaction() as connection:
            sort_order_cursor = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM notebooks"
            )
            sort_order = sort_order_cursor.fetchone()[0]
            try:
                insert_notebook_row(connection, notebook, sort_order)
            except sqlite3.IntegrityError as exc:
                if _NESTING_TRIGGER_TOKEN in str(exc):
                    raise NestingTooDeep(str(exc)) from exc
                raise

    def rename(self, notebook_id: str, new_name: str) -> None:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE notebooks SET name = ? WHERE id = ?",
                (new_name, notebook_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(notebook_id)

    def set_icon(self, notebook_id: str, icon: NotebookIcon) -> None:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE notebooks SET icon = ? WHERE id = ?",
                (icon.value, notebook_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(notebook_id)

    def delete_and_reparent_notes(
        self,
        notebook_id: str,
        target_id: str,
    ) -> None:
        if notebook_id == target_id:
            raise ValueError(
                "delete_and_reparent_notes: notebook_id and target_id "
                "must differ"
            )
        with self._db.transaction() as connection:
            # Verify the target exists up front. Without this the FK
            # constraint on ``notes.notebook_id`` fires only when we
            # attempt the UPDATE, which produces a confusing
            # IntegrityError that points at notes rather than at the
            # missing notebook.
            target_cursor = connection.execute(
                "SELECT 1 FROM notebooks WHERE id = ?",
                (target_id,),
            )
            if target_cursor.fetchone() is None:
                raise KeyError(target_id)

            # Move every note out of the doomed notebook into the target.
            connection.execute(
                "UPDATE notes SET notebook_id = ? WHERE notebook_id = ?",
                (target_id, notebook_id),
            )
            # Promote any child notebooks to top-level. parent_id=NULL
            # cannot violate the two-level invariant (the UPDATE trigger
            # only fires when NEW.parent_id IS NOT NULL).
            connection.execute(
                "UPDATE notebooks SET parent_id = NULL WHERE parent_id = ?",
                (notebook_id,),
            )
            cursor = connection.execute(
                "DELETE FROM notebooks WHERE id = ?",
                (notebook_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(notebook_id)


def _row_to_notebook(row: sqlite3.Row) -> Notebook:
    """Build a :class:`Notebook` from a database row.

    The icon string is normalised through :class:`NotebookIcon` so an
    unrecognised value raises :class:`ValueError` here, at the boundary
    — never as a confusing failure deep inside a UI widget.
    """
    return Notebook(
        id=row["id"],
        name=row["name"],
        parent_id=row["parent_id"],
        icon=NotebookIcon(row["icon"]),
    )
