"""Tests for :mod:`notes_app.controllers._storage_errors`."""

from __future__ import annotations

import sqlite3
import unittest

from notes_app.controllers._storage_errors import capturing_storage_errors


class CapturingStorageErrorsHappyPathTests(unittest.TestCase):
    def test_no_exception_emits_nothing(self) -> None:
        emitted: list[str] = []
        with capturing_storage_errors(emitted.append, "do thing"):
            pass
        self.assertEqual(emitted, [])

    def test_returned_value_visible_outside_context(self) -> None:
        # The context manager doesn't intercept normal control flow:
        # values bound inside the ``with`` are visible afterwards.
        emitted: list[str] = []
        result: int
        with capturing_storage_errors(emitted.append, "do thing"):
            result = 42
        self.assertEqual(result, 42)
        self.assertEqual(emitted, [])


class CapturingStorageErrorsCatchesDatabaseErrors(unittest.TestCase):
    def test_operational_error_emits_and_reraises(self) -> None:
        emitted: list[str] = []
        with self.assertRaises(sqlite3.OperationalError):
            with capturing_storage_errors(emitted.append, "create note"):
                raise sqlite3.OperationalError("disk full")
        self.assertEqual(len(emitted), 1)
        self.assertIn("Could not create note", emitted[0])
        self.assertIn("disk full", emitted[0])

    def test_integrity_error_emits_and_reraises(self) -> None:
        # IntegrityError is a sibling subclass of DatabaseError; the
        # helper must catch it through the parent. This pins that the
        # narrowing on ``except`` is on the parent class, not the
        # operational subclass.
        emitted: list[str] = []
        with self.assertRaises(sqlite3.IntegrityError):
            with capturing_storage_errors(emitted.append, "rename notebook"):
                raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        self.assertEqual(len(emitted), 1)
        self.assertIn("rename notebook", emitted[0])
        self.assertIn("FOREIGN KEY", emitted[0])

    def test_database_error_directly_emits_and_reraises(self) -> None:
        emitted: list[str] = []
        with self.assertRaises(sqlite3.DatabaseError):
            with capturing_storage_errors(emitted.append, "save note"):
                raise sqlite3.DatabaseError("database is locked")
        self.assertEqual(len(emitted), 1)
        self.assertIn("save note", emitted[0])

    def test_message_format(self) -> None:
        # Pinned format keeps every controller's toast wording uniform.
        emitted: list[str] = []
        with self.assertRaises(sqlite3.OperationalError):
            with capturing_storage_errors(emitted.append, "delete note"):
                raise sqlite3.OperationalError("boom")
        self.assertEqual(emitted, ["Could not delete note: boom"])


class CapturingStorageErrorsDoesNotCatchOthers(unittest.TestCase):
    """The helper deliberately catches only :class:`sqlite3.DatabaseError`.

    Catching :class:`Exception` would silently swallow programming
    bugs and other unrelated faults; the rule is to be narrow and
    explicit.
    """

    def test_value_error_propagates_without_emit(self) -> None:
        emitted: list[str] = []
        with self.assertRaises(ValueError):
            with capturing_storage_errors(emitted.append, "do thing"):
                raise ValueError("not a db error")
        self.assertEqual(emitted, [])

    def test_key_error_propagates_without_emit(self) -> None:
        # Repositories raise KeyError when an id doesn't exist. That
        # is *not* a database fault — it's a normal "missing row"
        # signal — so the helper must NOT emit a toast for it.
        emitted: list[str] = []
        with self.assertRaises(KeyError):
            with capturing_storage_errors(emitted.append, "save note"):
                raise KeyError("n-99")
        self.assertEqual(emitted, [])


class CapturingStorageErrorsEmitsBeforeReraise(unittest.TestCase):
    """The signal must fire before the exception propagates so that a
    listener that runs synchronously sees the toast even if the
    caller's stack is about to unwind."""

    def test_emit_runs_before_exception_unwinds(self) -> None:
        emitted_at: list[str] = []
        try:
            with capturing_storage_errors(
                lambda msg: emitted_at.append(f"emit:{msg}"),
                "delete note",
            ):
                raise sqlite3.OperationalError("boom")
        except sqlite3.OperationalError:
            emitted_at.append("after-raise")
        # The "emit:" entry comes first; the "after-raise" sentinel
        # is appended only once the exception has been caught
        # outside the context manager.
        self.assertEqual(len(emitted_at), 2)
        self.assertTrue(emitted_at[0].startswith("emit:"))
        self.assertEqual(emitted_at[1], "after-raise")


if __name__ == "__main__":
    unittest.main()
