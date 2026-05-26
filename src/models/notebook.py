"""The :class:`Notebook` dataclass — the in-memory shape of a notebook row.

Principles & invariants
-----------------------
* A notebook is identified by its opaque ``id`` string and never by its
  ``name`` — names are user-editable and not unique.
* ``parent_id`` is either ``None`` (a top-level notebook) or refers to the
  ``id`` of another notebook that is itself top-level. The two-level depth
  rule is enforced by the storage layer (SQL triggers on
  ``INSERT``/``UPDATE``, plus a defensive check in
  ``notebook_repository.insert``) and by the UI (the *Add child notebook*
  action is disabled on any notebook that already has a parent). This
  dataclass cannot enforce the rule itself because it has no access to
  other rows; it merely carries the field.
* The dataclass is frozen so a notebook reference handed to a UI widget
  cannot be mutated underneath the controller. Renames and icon changes
  flow through the repository, which produces a fresh instance.
* ``icon`` is the :class:`NotebookIcon` enum, never a raw string. The
  storage layer is responsible for converting to and from the textual
  column value at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from enums import NotebookIcon


@dataclass(frozen=True)
class Notebook:
    """A notebook in the library.

    Fields
    ------
    id:
        Stable identifier. Equality and hashing of notebooks is by id only.
    name:
        Display name as the user typed it. Not unique. Renames preserve
        ``id`` so notes inside the notebook stay attached.
    parent_id:
        ``None`` for top-level notebooks; otherwise the id of the parent.
        The parent must itself be top-level — see the SQL triggers in
        :mod:`storage.migrations`.
    icon:
        Symbolic icon shown in the sidebar. The full set of allowed values
        lives in :class:`enums.NotebookIcon`.
    """

    id: str
    name: str
    parent_id: str | None
    icon: NotebookIcon
