"""Orchestrates notebook-level user gestures (create, rename, etc.).

Principles & invariants
-----------------------
* :class:`NotebookController` is the single mediator between sidebar
  widgets that request a notebook-level action (create, rename, change
  icon, delete-with-reparent) and the :class:`NotebookRepositoryProtocol`
  that performs it. Widgets do not call the repository directly; the
  controller is the place that owns the signal-emission and error-
  handling discipline below.
* Storage is injected as a protocol, never as a concrete class. Tests
  use a dataclass-backed in-memory fake; production wires the SQLite-
  backed repository. The controller code is identical in both cases.
* The id-generator is injected as a callable so tests can use a
  deterministic counter. The default factory uses a UUID4 prefixed
  with ``notebook-`` so user-created notebooks are visually
  distinguishable from the seed-data ids in diagnostics.
* Database errors (:class:`sqlite3.DatabaseError` and its subclasses)
  are caught here, emitted on the ``storage-error`` signal as a
  human-readable message, and **re-raised**. The catch-and-emit
  pattern is shared with :class:`NoteController` via
  :func:`notes_app.controllers._storage_errors.capturing_storage_errors`,
  which exists for exactly this de-duplication. Re-raising is
  identical to the convention in :class:`NoteController` and
  exists for the same reason: the toast satisfies the "never
  silently swallowed" rule, propagation lets the caller skip the
  post-success side-effects that would otherwise leave widgets
  reading stale state.
* :class:`NestingTooDeep` (the two-level-hierarchy guard) is caught
  in :meth:`create_notebook` and surfaced as the dedicated
  ``nesting-too-deep`` signal (no payload — the rule is binary).
  The method returns ``None`` rather than re-raising; in normal use
  the sidebar greys out the *Add child notebook* action on any
  notebook that already has a parent, so the catch is defence in
  depth against bugs that bypass the UI guard.
* Successful mutations emit ``notebooks-changed``. Listeners (the
  sidebar tree view, primarily) re-read
  :meth:`NotebookRepositoryProtocol.list_all`. The signal is
  payload-free — the listener pulls a fresh list, just like the
  matching pattern in :class:`NoteController`.
* Signal-emission ordering is: persist first, then emit. The
  handler that re-reads the repository must see the post-mutation
  state, never the pre-mutation one.
* Deletion of a notebook re-parents its notes to a target notebook
  that the caller chooses. The controller does not pick the target
  for the caller (e.g. it does not silently fall back to the
  Archive notebook): which notebook to absorb the orphans is a UX
  decision the sidebar owns. Promoting child notebooks of the
  deleted notebook to top-level is the repository's job — see
  :meth:`NotebookRepositoryProtocol.delete_and_reparent_notes`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Final

import gi

gi.require_version("GObject", "2.0")
# pylint: disable=wrong-import-position
from gi.repository import GObject  # noqa: E402

from notes_app.controllers._storage_errors import capturing_storage_errors
from notes_app.enums import NotebookIcon
from notes_app.models.notebook import Notebook
from notes_app.storage.protocols import (
    NestingTooDeep,
    NotebookRepositoryProtocol,
)


type IdFactory = Callable[[], str]
"""Callable producing a fresh, unique notebook id.

Same role as the equivalent alias in :mod:`notes_app.controllers.
note_controller`: tests pin it to a counter, production uses
:func:`_default_id_factory` (UUID4 with a stable prefix).
"""


_NOTEBOOK_ID_PREFIX: Final[str] = "notebook-"
"""Stable prefix on user-created notebook ids.

The seed notebooks defined in :mod:`notes_app.config.defaults` use a
``seed-…`` prefix; using a different prefix here means the two
populations are visually distinguishable in diagnostics and cannot
collide with each other.
"""


def _default_id_factory() -> str:
    """Production id generator — UUID4 with the ``notebook-`` prefix."""
    return f"{_NOTEBOOK_ID_PREFIX}{uuid.uuid4().hex[:12]}"


class NotebookController(GObject.Object):
    """Orchestrates notebook-level user actions.

    Signals
    -------
    notebooks-changed
        Fired after any successful create / rename / set-icon /
        delete. Listeners re-read the repository.
    nesting-too-deep
        Fired (no payload) when :meth:`create_notebook` rejects an
        attempt to put a notebook under a parent that already has a
        parent of its own.
    storage-error
        Fired with a human-readable :class:`str` message when a
        database operation raises. The originating exception is also
        re-raised, so this signal is purely a notification channel.
    """

    __gsignals__ = {
        "notebooks-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "nesting-too-deep": (GObject.SignalFlags.RUN_LAST, None, ()),
        "storage-error": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (str,),
        ),
    }

    _repository: NotebookRepositoryProtocol
    _id_factory: IdFactory

    def __init__(
        self,
        *,
        repository: NotebookRepositoryProtocol,
        id_factory: IdFactory = _default_id_factory,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._id_factory = id_factory

    def _emit_storage_error(self, message: str) -> None:
        """Closure-friendly emitter passed to
        :func:`capturing_storage_errors`. Mirrors the same-named
        helper on :class:`NoteController`."""
        self.emit("storage-error", message)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_notebook(
        self,
        *,
        name: str,
        parent_id: str | None,
        icon: NotebookIcon,
    ) -> Notebook | None:
        """Create a notebook and return it on success, ``None`` on a
        nesting-too-deep rejection.

        ``parent_id`` is ``None`` for a top-level notebook, otherwise
        the id of an existing top-level notebook. Passing the id of
        a notebook that already has a parent triggers the protocol's
        :class:`NestingTooDeep` guard; the controller emits
        ``nesting-too-deep`` and returns ``None`` so the caller can
        keep its UI state consistent without having to handle an
        exception.
        """
        notebook = Notebook(
            id=self._id_factory(),
            name=name,
            parent_id=parent_id,
            icon=icon,
        )
        try:
            with capturing_storage_errors(self._emit_storage_error, "create notebook"):
                self._repository.insert(notebook)
        except NestingTooDeep:
            self.emit("nesting-too-deep")
            return None
        self.emit("notebooks-changed")
        return notebook

    def rename(self, notebook_id: str, new_name: str) -> None:
        """Rename ``notebook_id`` in place; the id and notes survive."""
        with capturing_storage_errors(self._emit_storage_error, "rename notebook"):
            self._repository.rename(notebook_id, new_name)
        self.emit("notebooks-changed")

    def set_icon(self, notebook_id: str, icon: NotebookIcon) -> None:
        """Change the symbolic icon shown next to ``notebook_id``."""
        with capturing_storage_errors(self._emit_storage_error, "change notebook icon"):
            self._repository.set_icon(notebook_id, icon)
        self.emit("notebooks-changed")

    def delete(self, notebook_id: str, target_notebook_id: str) -> None:
        """Delete ``notebook_id``, re-parenting its notes to ``target``.

        The caller picks ``target_notebook_id`` (the sidebar
        typically uses the Archive notebook). The repository
        promotes any *child* notebooks of the deleted notebook to
        top-level inside the same transaction — the controller does
        not need to handle that case explicitly.
        """
        with capturing_storage_errors(self._emit_storage_error, "delete notebook"):
            self._repository.delete_and_reparent_notes(
                notebook_id,
                target_notebook_id,
            )
        self.emit("notebooks-changed")
