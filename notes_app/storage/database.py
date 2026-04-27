"""Owns the ``sqlite3.Connection`` and provides composable transactions.

Principles & invariants
-----------------------
* This module is the sole owner of the ``sqlite3.Connection`` for the
  application. Repositories hold a reference to a :class:`Database`
  instance; they never open a connection of their own. There is exactly
  one connection per running process — we are single-threaded (the GTK
  main loop), so :func:`sqlite3.connect` is called with the default
  ``check_same_thread=True``.
* The connection is opened with ``autocommit=True`` (Python 3.13's
  explicit form). The driver issues no implicit ``BEGIN``/``COMMIT``;
  every transaction starts inside :meth:`transaction`. Reads outside a
  transaction therefore execute under SQLite's autocommit mode, which is
  the cheapest read path available.
* Foreign keys are enabled at construction time
  (``PRAGMA foreign_keys = ON``). Without this, ``ON DELETE CASCADE`` and
  ``ON DELETE RESTRICT`` are silently ignored — a critical correctness
  property of the schema.
* :meth:`transaction` composes. Calling it inside another active
  ``transaction()`` issues a ``SAVEPOINT`` rather than a fresh ``BEGIN``,
  so the caller's outer transaction stays in control. This implements
  §8's *"never opens a transaction the caller didn't ask for; composes
  inside a parent transaction when present"* property: a repository can
  wrap each public method in ``with self._db.transaction()`` and still
  participate in a larger caller-provided transaction.
* The class deliberately does not host any business logic. It owns the
  connection lifecycle and the transaction shape and nothing else;
  adding domain methods here would break the separation that lets
  repositories be unit-tested with in-memory protocol fakes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self


_BEGIN_SQL: str = "BEGIN"
_COMMIT_SQL: str = "COMMIT"
_ROLLBACK_SQL: str = "ROLLBACK"
_PRAGMA_FK_ON: str = "PRAGMA foreign_keys = ON"
_IN_MEMORY_PATH: str = ":memory:"


class Database:
    """Wraps a single ``sqlite3.Connection``.

    Construction opens the connection, switches it into autocommit mode,
    enables ``sqlite3.Row`` as the row factory, and turns on foreign-key
    enforcement.
    """

    _connection: sqlite3.Connection
    _depth: int

    def __init__(self, path: Path | str) -> None:
        # ``str(path)`` lets callers pass either ``Path`` or the literal
        # ``":memory:"`` — sqlite3 already special-cases that string.
        self._connection = sqlite3.connect(
            str(path),
            autocommit=True,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(_PRAGMA_FK_ON)
        self._depth = 0

    @classmethod
    def in_memory(cls) -> Self:
        """Open a fresh in-memory database (used by tests).

        The returned instance is independent of every other in-memory
        ``Database`` — SQLite gives each ``:memory:`` connection its own
        private database.
        """
        return cls(_IN_MEMORY_PATH)

    @property
    def connection(self) -> sqlite3.Connection:
        """The wrapped connection.

        Repositories use this for ``SELECT`` queries (no transaction is
        opened by SQLite for read-only statements in autocommit mode)
        and within a ``with self.transaction()`` block for writes.
        """
        return self._connection

    @property
    def in_transaction(self) -> bool:
        """``True`` while a :meth:`transaction` block is active."""
        return self._depth > 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run the body of the ``with`` block inside a single transaction.

        The first (outermost) entry issues ``BEGIN`` and ``COMMIT``
        (or ``ROLLBACK`` on exception). Re-entering while a transaction
        is already open issues a ``SAVEPOINT`` and the corresponding
        release (or rollback-to-savepoint on exception). The yielded
        connection is the same object returned by :attr:`connection`;
        callers may therefore mix ``self._db.connection.execute(...)``
        and ``conn.execute(...)`` interchangeably inside the block.
        """
        is_outermost = self._depth == 0
        savepoint: str | None = None
        if is_outermost:
            self._connection.execute(_BEGIN_SQL)
        else:
            savepoint = f"sp_{self._depth}"
            self._connection.execute(f"SAVEPOINT {savepoint}")
        self._depth += 1

        try:
            yield self._connection
        except BaseException:
            # Rolling back the partial work is the whole reason the
            # transaction context manager exists; we deliberately catch
            # ``BaseException`` so the rollback also runs on
            # ``KeyboardInterrupt`` / ``SystemExit`` and never leaves the
            # database in a half-applied state.
            try:
                if is_outermost:
                    self._connection.execute(_ROLLBACK_SQL)
                else:
                    assert savepoint is not None
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            finally:
                self._depth -= 1
            raise
        # No exception path. We deliberately use a flat `try/except`
        # without an `else:` (and the corresponding non-exceptional
        # finalisation below). The two shapes are equivalent —
        # `except` ends in `raise`, so anything after it executes only
        # when no exception was raised — and pylint is happier with
        # this form.
        try:
            if is_outermost:
                self._connection.execute(_COMMIT_SQL)
            else:
                assert savepoint is not None
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        finally:
            self._depth -= 1

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Standard context manager: the type / value / traceback aren't
        # needed for cleanup (we don't suppress the exception).
        del exc_type, exc_value, traceback
        self.close()
