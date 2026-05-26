"""Tests for :mod:`models.notebook`."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields

from enums import NotebookIcon
from models.notebook import Notebook


class NotebookDataclassTests(unittest.TestCase):
    def test_top_level_construction(self) -> None:
        nb = Notebook(
            id="nb1",
            name="Personal",
            parent_id=None,
            icon=NotebookIcon.HOME,
        )
        self.assertEqual(nb.id, "nb1")
        self.assertEqual(nb.name, "Personal")
        self.assertIsNone(nb.parent_id)
        self.assertIs(nb.icon, NotebookIcon.HOME)

    def test_child_construction(self) -> None:
        nb = Notebook(
            id="nb-baking",
            name="Baking",
            parent_id="nb-recipes",
            icon=NotebookIcon.BOOK,
        )
        self.assertEqual(nb.parent_id, "nb-recipes")

    def test_is_frozen(self) -> None:
        nb = Notebook(
            id="nb1", name="X", parent_id=None, icon=NotebookIcon.HOME,
        )
        with self.assertRaises(FrozenInstanceError):
            nb.name = "Y"  # type: ignore[misc]

    def test_field_set_is_exact(self) -> None:
        names = {f.name for f in fields(Notebook)}
        self.assertEqual(names, {"id", "name", "parent_id", "icon"})

    def test_equality_by_value(self) -> None:
        a = Notebook(id="x", name="X", parent_id=None, icon=NotebookIcon.HOME)
        b = Notebook(id="x", name="X", parent_id=None, icon=NotebookIcon.HOME)
        c = Notebook(id="y", name="X", parent_id=None, icon=NotebookIcon.HOME)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_hashable_for_use_in_sets(self) -> None:
        a = Notebook(id="x", name="X", parent_id=None, icon=NotebookIcon.HOME)
        b = Notebook(id="x", name="X", parent_id=None, icon=NotebookIcon.HOME)
        self.assertEqual({a, b}, {a})

    def test_icon_is_typed_as_enum(self) -> None:
        # The icon field accepts NotebookIcon members. We don't enforce
        # rejection of bare strings at construction time (Python's
        # dataclasses don't run runtime type checks) — but every persisted
        # path must round-trip through NotebookIcon.
        nb = Notebook(
            id="nb1", name="X", parent_id=None, icon=NotebookIcon.ARCHIVE,
        )
        self.assertIsInstance(nb.icon, NotebookIcon)


if __name__ == "__main__":
    unittest.main()
