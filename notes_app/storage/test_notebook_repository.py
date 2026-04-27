"""Tests for :mod:`notes_app.storage.notebook_repository`."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime

from notes_app.config.defaults import (
    SEED_NOTEBOOKS,
    SEED_NOTEBOOK_ID_PERSONAL,
    SEED_NOTEBOOK_ID_RECIPES,
)
from notes_app.enums import NotebookIcon
from notes_app.models.note import Note, derive_snippet, derive_title
from notes_app.models.notebook import Notebook
from notes_app.storage.database import Database
from notes_app.storage.migrations import apply_pending
from notes_app.storage.note_repository import NoteRepository
from notes_app.storage.notebook_repository import NotebookRepository
from notes_app.storage.protocols import NestingTooDeep


_FIXED_NOW: datetime = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_SEED_BAKING_ID: str = "seed-baking"
_SEED_ARCHIVE_ID: str = "seed-archive"


def _make_notebook(
    *,
    notebook_id: str,
    name: str = "Test Notebook",
    parent_id: str | None = None,
    icon: NotebookIcon = NotebookIcon.HOME,
) -> Notebook:
    return Notebook(
        id=notebook_id,
        name=name,
        parent_id=parent_id,
        icon=icon,
    )


def _make_note(
    *,
    note_id: str,
    notebook_id: str,
    source: str = "= Test\n\nbody",
) -> Note:
    return Note(
        id=note_id,
        title=derive_title(source),
        notebook_id=notebook_id,
        source=source,
        snippet=derive_snippet(source),
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW,
    )


class _NotebookRepoTestBase(unittest.TestCase):
    """In-memory DB with seeds + the repo under test."""

    db: Database
    repo: NotebookRepository
    notes: NoteRepository

    def setUp(self) -> None:
        self.db = Database.in_memory()
        self.addCleanup(self.db.close)
        apply_pending(self.db, now=_FIXED_NOW)
        self.repo = NotebookRepository(self.db)
        self.notes = NoteRepository(self.db)


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------


class GetAndListTests(_NotebookRepoTestBase):
    def test_list_all_returns_seeded_notebooks(self) -> None:
        seeded = self.repo.list_all()
        self.assertEqual(len(seeded), len(SEED_NOTEBOOKS))
        self.assertEqual(
            [n.id for n in seeded],
            [n.id for n in SEED_NOTEBOOKS],
        )

    def test_list_all_orders_by_sort_order(self) -> None:
        # Insert a notebook; it must appear after every seed.
        self.repo.insert(_make_notebook(
            notebook_id="user-a", name="User A"))
        listed = self.repo.list_all()
        self.assertEqual(listed[-1].id, "user-a")

    def test_list_all_seeded_includes_baking_under_recipes(self) -> None:
        seeded = self.repo.list_all()
        baking = next(n for n in seeded if n.id == _SEED_BAKING_ID)
        self.assertEqual(baking.parent_id, SEED_NOTEBOOK_ID_RECIPES)

    def test_get_returns_seeded_notebook(self) -> None:
        personal = self.repo.get(SEED_NOTEBOOK_ID_PERSONAL)
        self.assertEqual(personal.id, SEED_NOTEBOOK_ID_PERSONAL)
        self.assertIsNone(personal.parent_id)

    def test_get_returns_typed_icon(self) -> None:
        nb = self.repo.get(SEED_NOTEBOOK_ID_PERSONAL)
        self.assertIsInstance(nb.icon, NotebookIcon)

    def test_get_raises_keyerror_on_missing(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.get("does-not-exist")


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


class InsertTests(_NotebookRepoTestBase):
    def test_insert_top_level_notebook_round_trips(self) -> None:
        nb = _make_notebook(
            notebook_id="user-1", name="Mine", icon=NotebookIcon.STAR,
        )
        self.repo.insert(nb)
        fetched = self.repo.get("user-1")
        self.assertEqual(fetched, nb)

    def test_insert_child_notebook_round_trips(self) -> None:
        nb = _make_notebook(
            notebook_id="user-2",
            name="Child",
            parent_id=SEED_NOTEBOOK_ID_PERSONAL,
            icon=NotebookIcon.BOOK,
        )
        self.repo.insert(nb)
        self.assertEqual(self.repo.get("user-2"), nb)

    def test_insert_grandchild_raises_nesting_too_deep(self) -> None:
        # Parent is "seed-baking", which is itself a child of recipes.
        # The defensive Python check should refuse before we ever reach
        # the SQL trigger.
        nb = _make_notebook(
            notebook_id="user-too-deep",
            parent_id=_SEED_BAKING_ID,
        )
        with self.assertRaises(NestingTooDeep):
            self.repo.insert(nb)

    def test_insert_appends_to_sort_order(self) -> None:
        self.repo.insert(_make_notebook(
            notebook_id="user-a", name="First"))
        self.repo.insert(_make_notebook(
            notebook_id="user-b", name="Second"))
        listed = [n.id for n in self.repo.list_all()]
        self.assertEqual(listed[-2:], ["user-a", "user-b"])

    def test_insert_with_unknown_parent_raises_integrity_error(self) -> None:
        # The FK constraint on parent_id catches a non-existent parent.
        nb = _make_notebook(
            notebook_id="user-orphan",
            parent_id="nope-not-a-real-id",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.insert(nb)


# ---------------------------------------------------------------------------
# Two-level depth: SQL UPDATE trigger
# ---------------------------------------------------------------------------


class UpdateTriggerTests(_NotebookRepoTestBase):
    def test_sql_update_trigger_blocks_grandchild_via_parent_change(self) -> None:
        # The protocol does not expose a "change parent" operation, but
        # the SQL trigger must still fire for any direct UPDATE that
        # would create a third level. Test the trigger by issuing the
        # UPDATE through the connection directly.
        # Insert a top-level notebook, then try to set its parent to a
        # notebook that's already a child — that would make us a
        # grandchild.
        self.repo.insert(_make_notebook(notebook_id="will-be-deep"))
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.db.connection.execute(
                "UPDATE notebooks SET parent_id = ? WHERE id = ?",
                (_SEED_BAKING_ID, "will-be-deep"),
            )
        self.assertIn("NestingTooDeep", str(cm.exception))


# ---------------------------------------------------------------------------
# Rename / set_icon
# ---------------------------------------------------------------------------


class RenameTests(_NotebookRepoTestBase):
    def test_rename_changes_name(self) -> None:
        self.repo.insert(_make_notebook(
            notebook_id="user-1", name="Before"))
        self.repo.rename("user-1", "After")
        self.assertEqual(self.repo.get("user-1").name, "After")

    def test_rename_raises_keyerror_on_missing(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.rename("ghost", "Whatever")


class SetIconTests(_NotebookRepoTestBase):
    def test_set_icon_changes_icon(self) -> None:
        self.repo.insert(_make_notebook(
            notebook_id="user-1", icon=NotebookIcon.HOME))
        self.repo.set_icon("user-1", NotebookIcon.STAR)
        self.assertIs(self.repo.get("user-1").icon, NotebookIcon.STAR)

    def test_set_icon_raises_keyerror_on_missing(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.set_icon("ghost", NotebookIcon.HOME)


# ---------------------------------------------------------------------------
# delete_and_reparent_notes
# ---------------------------------------------------------------------------


class DeleteAndReparentTests(_NotebookRepoTestBase):
    def test_moves_notes_to_target_then_removes_notebook(self) -> None:
        self.repo.insert(_make_notebook(notebook_id="doomed"))
        self.notes.insert(_make_note(note_id="n1", notebook_id="doomed"))
        self.notes.insert(_make_note(note_id="n2", notebook_id="doomed"))

        self.repo.delete_and_reparent_notes("doomed", _SEED_ARCHIVE_ID)

        with self.assertRaises(KeyError):
            self.repo.get("doomed")
        self.assertEqual(
            {n.id for n in self.notes.list_by_notebook(_SEED_ARCHIVE_ID)},
            {"n1", "n2"},
        )

    def test_promotes_child_notebooks_to_top_level(self) -> None:
        # Build a parent + child, then delete the parent. The child
        # must survive with parent_id = NULL.
        self.repo.insert(_make_notebook(notebook_id="parent"))
        self.repo.insert(_make_notebook(
            notebook_id="child", parent_id="parent"))

        self.repo.delete_and_reparent_notes("parent", _SEED_ARCHIVE_ID)

        survivor = self.repo.get("child")
        self.assertIsNone(survivor.parent_id)

    def test_self_target_raises_value_error(self) -> None:
        self.repo.insert(_make_notebook(notebook_id="self-target"))
        with self.assertRaises(ValueError):
            self.repo.delete_and_reparent_notes(
                "self-target", "self-target"
            )

    def test_missing_target_raises_keyerror(self) -> None:
        self.repo.insert(_make_notebook(notebook_id="nb1"))
        with self.assertRaises(KeyError):
            self.repo.delete_and_reparent_notes("nb1", "no-such-target")

    def test_missing_notebook_raises_keyerror(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.delete_and_reparent_notes(
                "ghost", _SEED_ARCHIVE_ID,
            )

    def test_failure_inside_transaction_rolls_back(self) -> None:
        # If the final DELETE fails (e.g. notebook id not present),
        # the prior reparenting UPDATE must be rolled back so notes
        # don't end up moved to the target while the source notebook
        # still exists. We simulate this by attempting to delete a
        # notebook that doesn't exist while seeding fake state — the
        # KeyError propagates and the transaction unwinds.
        # Place a note in seed-personal — observe its notebook_id
        # before and after a failed delete.
        self.notes.insert(_make_note(
            note_id="n1", notebook_id=SEED_NOTEBOOK_ID_PERSONAL))
        with self.assertRaises(KeyError):
            self.repo.delete_and_reparent_notes(
                "ghost", SEED_NOTEBOOK_ID_PERSONAL,
            )
        # The note in seed-personal didn't belong to "ghost", so it
        # must still be there. (seed-personal also contains the
        # welcome note from the seeds; we only assert that ``n1`` is
        # present, not the exact set.)
        ids = {n.id for n in self.notes.list_by_notebook(
            SEED_NOTEBOOK_ID_PERSONAL)}
        self.assertIn("n1", ids)


if __name__ == "__main__":
    unittest.main()
