"""Shared SQL helpers for inserting notebook rows.

Principles & invariants
-----------------------
* This module exists exclusively to factor out the
  ``INSERT INTO notebooks ...`` statement and its parameter tuple, which
  are needed by both the migration runner (seeding the design's notebook
  set on a fresh DB) and the notebook repository (user-driven inserts).
* It is intentionally tiny and stateless. Moving the helper here keeps
  :mod:`notes_app.storage.migrations` from having to import the
  repository — which would invert the layering rule that *the schema is
  installed before any repository touches it*.
* Module name uses a leading underscore to mark it as a private
  implementation detail of the storage package. Code outside
  :mod:`notes_app.storage` should not import from here.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from notes_app.models.notebook import Notebook


INSERT_NOTEBOOK_SQL: Final[str] = (
    "INSERT INTO notebooks (id, name, parent_id, icon, sort_order) "
    "VALUES (?, ?, ?, ?, ?)"
)
"""The shared INSERT statement used by both call sites."""


def insert_notebook_row(
    connection: sqlite3.Connection,
    notebook: Notebook,
    sort_order: int,
) -> None:
    """Insert a single notebook row at the given ``sort_order`` slot.

    The caller controls the sort order: the migration runner uses
    enumerate-style indices on the seed list; the repository computes
    ``MAX(sort_order) + 1``. Centralising the parameter tuple here is
    what lets the column order, parameter order, and ``.value`` access
    on the icon enum be defined in exactly one place.
    """
    connection.execute(
        INSERT_NOTEBOOK_SQL,
        (
            notebook.id,
            notebook.name,
            notebook.parent_id,
            notebook.icon.value,
            sort_order,
        ),
    )
