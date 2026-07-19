"""Tests for :mod:`controllers.tag_counts_model`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from gi.repository import Gio

from giruntime.controllers.note_item import NoteItem
from giruntime.controllers.tag_counts_model import TagCountsModel, TagItem
from models.note import Note


_NOW: datetime = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)


def _item(note_id: str, tags: tuple[str, ...]) -> NoteItem:
    return NoteItem(Note(
        id=note_id,
        title=note_id,
        source="",
        snippet="",
        tags=tags,
        created_at=_NOW,
        modified_at=_NOW,
    ))


def _counts(model: TagCountsModel) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(model.get_n_items()):
        row = model.get_item(i)
        assert isinstance(row, TagItem)
        out[row.name] = row.count
    return out


class _ItemsChangedRecorder:
    events: list[tuple[int, int, int]]

    def __init__(self, model: TagCountsModel) -> None:
        self.events = []
        model.connect("items-changed", self._on_changed)

    def _on_changed(
        self, _m: TagCountsModel, pos: int, removed: int, added: int,
    ) -> None:
        self.events.append((pos, removed, added))


class InitialSeedTests(unittest.TestCase):
    def test_seeds_counts_from_prepopulated_source(self) -> None:
        source = Gio.ListStore.new(NoteItem)
        source.append(_item("a", ("red", "blue")))
        source.append(_item("b", ("red",)))
        model = TagCountsModel(source)
        self.assertEqual(_counts(model), {"red": 2, "blue": 1})

    def test_empty_source_yields_no_rows(self) -> None:
        source = Gio.ListStore.new(NoteItem)
        model = TagCountsModel(source)
        self.assertEqual(model.get_n_items(), 0)


class AppendTests(unittest.TestCase):
    def test_new_tag_appends_membership(self) -> None:
        source = Gio.ListStore.new(NoteItem)
        source.append(_item("a", ("red",)))
        model = TagCountsModel(source)
        recorder = _ItemsChangedRecorder(model)
        source.append(_item("b", ("green",)))
        self.assertEqual(_counts(model), {"red": 1, "green": 1})
        # One insertion at the end of the tag list.
        self.assertEqual(recorder.events, [(1, 0, 1)])

    def test_existing_tag_increments_count_only(self) -> None:
        source = Gio.ListStore.new(NoteItem)
        source.append(_item("a", ("red",)))
        model = TagCountsModel(source)
        # Find the existing TagItem and watch its count notifications.
        red = model.get_item(0)
        assert isinstance(red, TagItem)
        notifications: list[int] = []
        red.connect("notify::count", lambda *_: notifications.append(red.count))
        recorder = _ItemsChangedRecorder(model)
        source.append(_item("b", ("red",)))
        # No list churn — just a count bump on the existing row.
        self.assertEqual(recorder.events, [])
        self.assertEqual(_counts(model), {"red": 2})
        self.assertEqual(notifications, [2])


class RemoveTests(unittest.TestCase):
    def test_tag_membership_removed_on_last_note(self) -> None:
        source = Gio.ListStore.new(NoteItem)
        source.append(_item("a", ("red",)))
        source.append(_item("b", ("green",)))
        model = TagCountsModel(source)
        recorder = _ItemsChangedRecorder(model)
        source.remove(0)  # drops note "a", red -> 0
        self.assertEqual(_counts(model), {"green": 1})
        # red was at position 0 in the tag list.
        self.assertEqual(recorder.events, [(0, 1, 0)])

    def test_count_decrements_when_tag_survives(self) -> None:
        source = Gio.ListStore.new(NoteItem)
        source.append(_item("a", ("red",)))
        source.append(_item("b", ("red",)))
        model = TagCountsModel(source)
        recorder = _ItemsChangedRecorder(model)
        source.remove(0)  # red -> 1, still present
        self.assertEqual(recorder.events, [])
        self.assertEqual(_counts(model), {"red": 1})


class EditTests(unittest.TestCase):
    """An edit is a splice(pos, 1, [new]) — subtract old, add new."""

    def test_edit_swaps_tag_sets(self) -> None:
        source = Gio.ListStore.new(NoteItem)
        source.append(_item("a", ("red", "blue")))
        source.append(_item("b", ("blue",)))
        model = TagCountsModel(source)
        # Replace "a"'s tags red+blue with green+blue.
        source.splice(0, 1, [_item("a", ("green", "blue"))])
        # red dropped to 0 (removed), green added, blue unchanged at 2.
        self.assertEqual(_counts(model), {"blue": 2, "green": 1})

    def test_edit_that_keeps_multiply_held_tag_does_count_only(self) -> None:
        source = Gio.ListStore.new(NoteItem)
        source.append(_item("a", ("red",)))
        source.append(_item("b", ("red",)))  # red is held by two notes
        model = TagCountsModel(source)
        red = next(
            model.get_item(i) for i in range(model.get_n_items())
            if model.get_item(i).name == "red"
        )
        assert isinstance(red, TagItem)
        # Edit "a" keeping red and adding blue. Because red is also on
        # "b" it never hits 0, so its row instance survives (count-only).
        source.splice(0, 1, [_item("a", ("red", "blue"))])
        self.assertEqual(_counts(model), {"red": 2, "blue": 1})
        survivor = next(
            model.get_item(i) for i in range(model.get_n_items())
            if model.get_item(i).name == "red"
        )
        self.assertIs(survivor, red)

    def test_shadow_decrements_correct_removed_row(self) -> None:
        # Three rows; removing the middle must subtract only its tags.
        source = Gio.ListStore.new(NoteItem)
        source.append(_item("a", ("x",)))
        source.append(_item("b", ("y",)))
        source.append(_item("c", ("x",)))
        model = TagCountsModel(source)
        self.assertEqual(_counts(model), {"x": 2, "y": 1})
        source.remove(1)  # remove "b" (y)
        self.assertEqual(_counts(model), {"x": 2})


if __name__ == "__main__":
    unittest.main()
