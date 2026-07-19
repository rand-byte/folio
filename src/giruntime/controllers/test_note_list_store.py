"""Tests for :mod:`controllers.note_list_store`."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime

from giruntime.controllers.note_item import NoteItem
from giruntime.controllers.note_list_store import NoteListStore
from models.note import Note


_NOW: datetime = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
_LATER: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


class _FakeRepository:
    """In-memory :class:`NoteRepositoryProtocol` for store tests.

    ``insert`` / ``update_source`` return the persisted note, mirroring
    the real repository's widened return contract; a tiny tag derivation
    is faked so update assertions can observe a tag change.
    """

    notes: dict[str, Note]

    def __init__(self) -> None:
        self.notes = {}

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]

    def list_modified_since(self, since: datetime) -> list[Note]:
        return [n for n in self.notes.values() if n.modified_at >= since]

    def list_all(self) -> list[Note]:
        return sorted(
            self.notes.values(), key=lambda n: n.modified_at, reverse=True,
        )

    def search(self, _query: str) -> list[Note]:
        return list(self.notes.values())

    def insert(self, note: Note) -> Note:
        self.notes[note.id] = note
        return note

    def update_source(
        self, note_id: str, source: str, modified_at: datetime,
    ) -> Note:
        existing = self.notes[note_id]
        updated = Note(
            id=existing.id,
            title=existing.title,
            source=source,
            snippet=existing.snippet,
            tags=existing.tags,
            created_at=existing.created_at,
            modified_at=modified_at,
        )
        self.notes[note_id] = updated
        return updated

    def delete(self, note_id: str) -> None:
        del self.notes[note_id]

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return ()


class _RaisingOnInsertRepository(_FakeRepository):
    def insert(self, note: Note) -> Note:
        raise sqlite3.OperationalError("simulated failure")


class _RaisingOnUpdateRepository(_FakeRepository):
    def update_source(
        self, note_id: str, source: str, modified_at: datetime,
    ) -> Note:
        raise sqlite3.OperationalError("simulated failure")


class _RaisingOnDeleteRepository(_FakeRepository):
    def delete(self, note_id: str) -> None:
        raise sqlite3.OperationalError("simulated failure")


class _ItemsChangedRecorder:
    """Captures ``items-changed(position, removed, added)`` emissions."""

    events: list[tuple[int, int, int]]

    def __init__(self, store: NoteListStore) -> None:
        self.events = []
        store.connect("items-changed", self._on_changed)

    def _on_changed(
        self,
        _model: NoteListStore,
        position: int,
        removed: int,
        added: int,
    ) -> None:
        self.events.append((position, removed, added))


def _note(note_id: str, *, source: str = "", tags: tuple[str, ...] = (),
          modified_at: datetime = _NOW) -> Note:
    return Note(
        id=note_id,
        title=note_id.upper(),
        source=source if source else f"= {note_id}",
        snippet="",
        tags=tags,
        created_at=_NOW,
        modified_at=modified_at,
    )


def _build_store(
    repository: _FakeRepository | None = None,
    *,
    clock_value: datetime = _NOW,
    next_id: str = "new-1",
) -> tuple[NoteListStore, _FakeRepository]:
    repo = repository if repository is not None else _FakeRepository()
    store = NoteListStore(
        repository=repo,
        clock=lambda: clock_value,
        id_factory=lambda: next_id,
    )
    return store, repo


class LoadTests(unittest.TestCase):
    def test_load_populates_items_and_positions(self) -> None:
        repo = _FakeRepository()
        # Newest first ordering from list_all: c, b, a.
        repo.insert(_note("a", modified_at=datetime(2026, 1, 1, tzinfo=UTC)))
        repo.insert(_note("b", modified_at=datetime(2026, 1, 2, tzinfo=UTC)))
        repo.insert(_note("c", modified_at=datetime(2026, 1, 3, tzinfo=UTC)))
        store, _ = _build_store(repo)
        store.load()
        self.assertEqual(store.get_n_items(), 3)
        ids = [store.get_item(i).note.id for i in range(store.get_n_items())]
        self.assertEqual(ids, ["c", "b", "a"])
        # get_note resolves each by id regardless of position.
        for nid in ("a", "b", "c"):
            self.assertEqual(store.get_note(nid).id, nid)

    def test_loaded_item_is_note_item_carrying_source(self) -> None:
        repo = _FakeRepository()
        repo.insert(_note("a", source="= A\n\nbody text"))
        store, _ = _build_store(repo)
        store.load()
        item = store.get_item(0)
        self.assertIsInstance(item, NoteItem)
        self.assertEqual(item.note.source, "= A\n\nbody text")


class GetNoteTests(unittest.TestCase):
    def test_get_note_returns_full_note(self) -> None:
        repo = _FakeRepository()
        repo.insert(_note("a", source="= A\n\nbody", tags=("red",)))
        store, _ = _build_store(repo)
        store.load()
        note = store.get_note("a")
        self.assertEqual(note.source, "= A\n\nbody")
        self.assertEqual(note.tags, ("red",))

    def test_get_note_raises_keyerror_on_unknown_id(self) -> None:
        store, _ = _build_store()
        store.load()
        with self.assertRaises(KeyError):
            store.get_note("ghost")


class CreateTests(unittest.TestCase):
    def test_create_appends_and_emits_items_changed(self) -> None:
        store, repo = _build_store(next_id="new-1")
        store.load()
        recorder = _ItemsChangedRecorder(store)
        created = store.create("= New\n\nbody")
        self.assertEqual(created.id, "new-1")
        self.assertIn("new-1", repo.notes)
        self.assertEqual(store.get_n_items(), 1)
        # Appended at position 0 (empty store): added 1, removed 0.
        self.assertEqual(recorder.events, [(0, 0, 1)])
        self.assertEqual(store.get_note("new-1").source, "= New\n\nbody")

    def test_create_uses_injected_clock(self) -> None:
        store, _ = _build_store(clock_value=_LATER, next_id="new-1")
        store.load()
        created = store.create("= x")
        self.assertEqual(created.created_at, _LATER)
        self.assertEqual(created.modified_at, _LATER)

    def test_create_index_tracks_appended_position(self) -> None:
        repo = _FakeRepository()
        repo.insert(_note("a"))
        store, _ = _build_store(repo, next_id="new-1")
        store.load()
        store.create("= new")
        # The new note lands at position 1 (after the loaded "a").
        self.assertEqual(store.get_item(1).note.id, "new-1")
        self.assertEqual(store.get_note("new-1").id, "new-1")


class UpdateTests(unittest.TestCase):
    def test_update_replaces_in_place_and_emits_replace(self) -> None:
        repo = _FakeRepository()
        repo.insert(_note("a"))
        repo.insert(_note("b"))
        store, _ = _build_store(repo)
        store.load()
        pos = next(
            i for i in range(store.get_n_items())
            if store.get_item(i).note.id == "a"
        )
        recorder = _ItemsChangedRecorder(store)
        store.update("a", "= A2\n\nrewritten")
        # A replace at the same position: removed 1, added 1.
        self.assertEqual(recorder.events, [(pos, 1, 1)])
        self.assertEqual(store.get_note("a").source, "= A2\n\nrewritten")
        # The id->position mapping is unchanged by a replace.
        self.assertEqual(store.get_item(pos).note.id, "a")

    def test_update_uses_injected_clock(self) -> None:
        repo = _FakeRepository()
        repo.insert(_note("a"))
        store, _ = _build_store(repo, clock_value=_LATER)
        store.load()
        updated = store.update("a", "= changed")
        self.assertEqual(updated.modified_at, _LATER)


class DeleteTests(unittest.TestCase):
    def test_delete_removes_and_emits_items_changed(self) -> None:
        repo = _FakeRepository()
        repo.insert(_note("a", modified_at=datetime(2026, 1, 2, tzinfo=UTC)))
        repo.insert(_note("b", modified_at=datetime(2026, 1, 1, tzinfo=UTC)))
        store, _ = _build_store(repo)
        store.load()  # order: a (pos 0), b (pos 1)
        recorder = _ItemsChangedRecorder(store)
        store.delete("a")
        self.assertEqual(recorder.events, [(0, 1, 0)])
        self.assertEqual(store.get_n_items(), 1)
        self.assertEqual(store.get_item(0).note.id, "b")

    def test_delete_reindexes_remaining_positions(self) -> None:
        repo = _FakeRepository()
        repo.insert(_note("a", modified_at=datetime(2026, 1, 3, tzinfo=UTC)))
        repo.insert(_note("b", modified_at=datetime(2026, 1, 2, tzinfo=UTC)))
        repo.insert(_note("c", modified_at=datetime(2026, 1, 1, tzinfo=UTC)))
        store, _ = _build_store(repo)
        store.load()  # a@0, b@1, c@2
        store.delete("b")
        # c shifted from 2 to 1; the index must follow so update lands
        # the replace at the right slot.
        self.assertEqual(store.get_item(1).note.id, "c")
        store.update("c", "= C2")
        self.assertEqual(store.get_item(1).note.source, "= C2")
        self.assertEqual(store.get_note("c").source, "= C2")

    def test_delete_unknown_id_raises_keyerror(self) -> None:
        store, _ = _build_store()
        store.load()
        with self.assertRaises(KeyError):
            store.delete("ghost")


class DbFirstTests(unittest.TestCase):
    """A storage error must leave the in-memory store untouched."""

    def test_create_failure_does_not_commit(self) -> None:
        store, _ = _build_store(_RaisingOnInsertRepository())
        store.load()
        recorder = _ItemsChangedRecorder(store)
        with self.assertRaises(sqlite3.DatabaseError):
            store.create("= x")
        self.assertEqual(store.get_n_items(), 0)
        self.assertEqual(recorder.events, [])

    def test_update_failure_does_not_commit(self) -> None:
        repo = _RaisingOnUpdateRepository()
        repo.insert(_note("a", source="= original"))
        store, _ = _build_store(repo)
        store.load()
        recorder = _ItemsChangedRecorder(store)
        with self.assertRaises(sqlite3.DatabaseError):
            store.update("a", "= changed")
        # The in-memory note is unchanged.
        self.assertEqual(store.get_note("a").source, "= original")
        self.assertEqual(recorder.events, [])

    def test_delete_failure_does_not_commit(self) -> None:
        repo = _RaisingOnDeleteRepository()
        repo.insert(_note("a"))
        store, _ = _build_store(repo)
        store.load()
        recorder = _ItemsChangedRecorder(store)
        with self.assertRaises(sqlite3.DatabaseError):
            store.delete("a")
        self.assertEqual(store.get_n_items(), 1)
        self.assertEqual(store.get_note("a").id, "a")
        self.assertEqual(recorder.events, [])


if __name__ == "__main__":
    unittest.main()
