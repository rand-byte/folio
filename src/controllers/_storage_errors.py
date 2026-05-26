"""Shared context manager for emitting and re-raising storage errors.

Principles & invariants
-----------------------
* Lives as a sibling private helper because both
  :class:`NoteController` and :class:`NotebookController` need
  identical "catch :class:`sqlite3.DatabaseError`, emit a toast
  signal, re-raise" semantics. Defining it once removes a
  duplicate-code pylint warning and, more importantly, removes the
  drift hazard — a controller that fails to update its copy of the
  helper after a refactor would silently diverge from the other.
* The helper does not own the signal — it accepts an ``emit``
  callable. This keeps the two controllers' signals genuinely
  separate (``NoteController.storage-error`` and
  ``NotebookController.storage-error`` are two distinct GObject
  signals on two distinct objects) while sharing the catching
  discipline. Having the helper hard-code a single signal source
  would force the two signals to merge and break listener
  isolation.
* The exception is **always** re-raised after the signal fires.
  Callers depend on this: they use the context manager to wrap
  exactly the storage call so post-success side-effects (emitting
  ``notes-changed`` etc.) only run when the call returned cleanly.
* Only :class:`sqlite3.DatabaseError` is caught — its subclasses
  (:class:`OperationalError`, :class:`IntegrityError`,
  :class:`DataError`, etc.) come along for free. Subclasses that
  represent bugs rather than runtime failures
  (:class:`ProgrammingError`, :class:`NotSupportedError`) are also
  ``DatabaseError``s; in normal use they should not fire, and if
  they do the toast plus the propagated exception together make
  the failure visible.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager


type StorageErrorEmitter = Callable[[str], None]
"""Callable invoked with a single human-readable message string when
a database error is caught. Each controller passes a closure that
emits its own ``storage-error`` signal so listeners can subscribe
per-controller."""


@contextmanager
def capturing_storage_errors(
    emit: StorageErrorEmitter,
    action: str,
) -> Iterator[None]:
    """Catch :class:`sqlite3.DatabaseError`, emit a toast, re-raise.

    ``action`` is a short verb-led phrase (e.g. ``"create note"``,
    ``"delete notebook"``) that becomes part of the toast message:
    ``"Could not <action>: <exception>"``. Keeping it short and
    verb-led is what makes the toasts uniform across controllers.

    The exception propagates after the signal fires, so the caller
    can rely on the post-success code in the surrounding ``with``
    block being unreachable on failure — no ``notes-changed`` will
    be emitted, no :class:`AppState` mutation will fire.
    """
    try:
        yield
    except sqlite3.DatabaseError as exc:
        emit(f"Could not {action}: {exc}")
        raise
