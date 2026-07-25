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
* Connection-level settings are applied from a single declarative table,
  :data:`_CONNECTION_PRAGMAS`, at construction time — one ordered
  ``tuple`` of ``(pragma, value, enforcement)`` triples rather than a
  scattering of ad-hoc ``execute`` calls. Each is written, then read back
  and compared; the *enforcement* field says what a disagreement means:

  - **Foreign keys** (:data:`enums.SqlitePragma.FOREIGN_KEYS`,
    ``REQUIRED``). Without this, ``ON DELETE CASCADE`` and
    ``ON DELETE RESTRICT`` are silently ignored — a critical correctness
    property of the schema — so a read-back that is not ``1`` raises.
  - **Journal mode** (:data:`enums.SqlitePragma.JOURNAL_MODE`,
    ``BEST_EFFORT``). :data:`config.defaults.SQLITE_JOURNAL_MODE` (WAL)
    is *requested*, not required: an in-memory database always reports
    ``memory`` and a filesystem without shared-memory support keeps its
    existing mode. Both are legitimate, so a disagreement here is
    accepted silently. WAL is persisted in the database file header, so
    the request is effectively a one-time migration of the user's file
    and a no-op on every launch thereafter.
  - **Busy timeout** (:data:`enums.SqlitePragma.BUSY_TIMEOUT`,
    ``REQUIRED``). Set from :data:`config.defaults.SQLITE_BUSY_TIMEOUT_MS`.

  Order is a contract, which is why the table is a ``tuple`` and not a
  mapping: ``journal_mode`` cannot be set inside a transaction (there is
  none open at construction) and ``foreign_keys`` must be on before any
  FK-dependent statement runs. Toggles are written in their numeric form
  (:class:`enums.SqliteToggle`) so the value written and the value read
  back are the same string, letting one comparison rule verify every
  pragma uniformly. Pragma names/values cannot be bound parameters, so
  they are interpolated into the SQL text — safe because every value
  comes from an enum member or an ``int`` constant, never user input.
* :meth:`transaction` composes. Calling it inside another active
  ``transaction()`` issues a ``SAVEPOINT`` rather than a fresh ``BEGIN``,
  so the caller's outer transaction stays in control. This implements
  §8's *"never opens a transaction the caller didn't ask for; composes
  inside a parent transaction when present"* property: a repository can
  wrap each public method in ``with self._db.transaction()`` and still
  participate in a larger caller-provided transaction.
* :meth:`close` is an **owner responsibility**, not merely a test
  affordance. It is idempotent and backs the context-manager protocol,
  but the running application must call it on shutdown: under WAL the
  ``-wal`` / ``-shm`` sidecar files are checkpointed and removed only
  when the last connection closes, so a process that just exits leaves
  them behind (SQLite recovers them on next open — the data is safe, but
  the lifecycle is unfinished). The single production caller is the
  application's ``do_shutdown`` (see
  :mod:`giruntime.ui.application`); this module only guarantees that
  ``close`` is safe to call once, more than once, or never.
* The class deliberately does not host any business logic. It owns the
  connection lifecycle and the transaction shape and nothing else;
  adding domain methods here would break the separation that lets
  repositories be unit-tested with in-memory protocol fakes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from config.defaults import SQLITE_BUSY_TIMEOUT_MS, SQLITE_JOURNAL_MODE
from enums import PragmaEnforcement, SqlitePragma, SqliteToggle


_BEGIN_SQL: str = "BEGIN"
_COMMIT_SQL: str = "COMMIT"
_ROLLBACK_SQL: str = "ROLLBACK"
_IN_MEMORY_PATH: str = ":memory:"


@dataclass(frozen=True)
class _PragmaSetting:
    """One connection pragma to write, verify, and its enforcement level.

    ``value`` is the *string* form written into the SQL text and compared
    against the read-back (see :class:`enums.SqliteToggle` for why the
    numeric form is used for boolean pragmas). ``enforcement`` decides
    what a read-back that disagrees with ``value`` means — see
    :class:`enums.PragmaEnforcement`.
    """

    pragma: SqlitePragma
    value: str
    enforcement: PragmaEnforcement


_CONNECTION_PRAGMAS: Final[tuple[_PragmaSetting, ...]] = (
    _PragmaSetting(
        SqlitePragma.FOREIGN_KEYS,
        SqliteToggle.ON.value,
        PragmaEnforcement.REQUIRED,
    ),
    _PragmaSetting(
        SqlitePragma.JOURNAL_MODE,
        SQLITE_JOURNAL_MODE.value,
        PragmaEnforcement.BEST_EFFORT,
    ),
    _PragmaSetting(
        SqlitePragma.BUSY_TIMEOUT,
        str(SQLITE_BUSY_TIMEOUT_MS),
        PragmaEnforcement.REQUIRED,
    ),
)
"""The connection-level pragmas, applied in this order at construction.

Order is a contract (``journal_mode`` cannot run inside a transaction;
``foreign_keys`` must precede any FK-dependent statement), hence a
``tuple``. ``foreign_keys`` is a fixed, ``REQUIRED`` entry rather than a
tunable in :mod:`config.defaults`: a silently-ignored value would break
``ON DELETE CASCADE``. The journal mode and busy timeout come from
:mod:`config.defaults`, the documented home for tunables.
"""


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
        self._apply_connection_pragmas()
        self._depth = 0

    def _apply_connection_pragmas(self) -> None:
        """Write, then verify, each pragma in :data:`_CONNECTION_PRAGMAS`.

        For every setting: interpolate its value into a
        ``PRAGMA <name> = <value>`` statement and execute it, then read the
        pragma back and compare. A ``REQUIRED`` setting whose read-back
        does not match the value written raises :class:`sqlite3.DatabaseError`
        (the engine ignored a setting the schema's correctness depends on);
        a ``BEST_EFFORT`` setting is allowed to differ (the engine may
        legitimately decline — e.g. WAL on an in-memory database).

        The read-back is always a separate ``PRAGMA <name>`` query because
        the value a pragma *write* returns is not uniform across pragmas
        (a toggle write yields nothing, ``journal_mode`` echoes the mode),
        whereas the read-back is: a single-column, single-row result whose
        value, stringified, equals the numeric/lower-case form written.
        """
        for setting in _CONNECTION_PRAGMAS:
            self._connection.execute(
                f"PRAGMA {setting.pragma.value} = {setting.value}"
            )
            row = self._connection.execute(
                f"PRAGMA {setting.pragma.value}"
            ).fetchone()
            effective = str(row[0])
            if (
                setting.enforcement is PragmaEnforcement.REQUIRED
                and effective != setting.value
            ):
                raise sqlite3.DatabaseError(
                    f"PRAGMA {setting.pragma.value} could not be set to "
                    f"{setting.value!r}: connection reports {effective!r}"
                )

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
