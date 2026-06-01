"""Tests for :mod:`controllers.note_controller`."""

from __future__ import annotations

import sqlite3
import unittest
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import (
    NoteController,
    make_initial_source,
)
from enums import AttachmentRejectionReason, MimeKind, SmartFilter
from models.attachment import Attachment
from models.note import Note
from search.note_filter import SmartSelection, TagSelection
from storage.protocols import AttachmentRejected


_FIXED_NOW: datetime = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    """Conforms to :class:`NoteRepositoryProtocol`.

    Mirrors :meth:`update_source`'s real behaviour by re-deriving
    cached fields from the new source — the tests assert on that.
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
            self.notes.values(),
            key=lambda n: n.modified_at,
            reverse=True,
        )

    def search(self, query: str) -> list[Note]:
        needle = query.lower()
        return [
            n for n in self.notes.values()
            if needle in n.title.lower() or needle in n.source.lower()
        ]

    def insert(self, note: Note) -> None:
        if note.id in self.notes:
            raise sqlite3.IntegrityError(
                f"duplicate id {note.id}",
            )
        self.notes[note.id] = note

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> None:
        existing = self.notes[note_id]
        self.notes[note_id] = Note(
            id=existing.id,
            title=existing.title,
            source=source,
            snippet=existing.snippet,
            tags=existing.tags,
            created_at=existing.created_at,
            modified_at=modified_at,
        )

    def delete(self, note_id: str) -> None:
        del self.notes[note_id]

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for note in self.notes.values():
            for tag in note.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return tuple(sorted(counts.items()))


class _FakeAttachmentStore:
    """Conforms to :class:`AttachmentStoreProtocol`."""

    attachments: dict[str, Attachment]
    reject_with: AttachmentRejectionReason | None
    raise_on_remove: sqlite3.DatabaseError | None

    def __init__(self) -> None:
        self.attachments = {}
        self.reject_with = None
        self.raise_on_remove = None

    def add_for_note(self, note_id: str, source_path: Path) -> Attachment:
        if self.reject_with is not None:
            raise AttachmentRejected(self.reject_with)
        att = Attachment(
            id=f"att-{len(self.attachments) + 1}",
            note_id=note_id,
            filename=source_path.name,
            byte_size=1,
            mime_type=MimeKind.PNG,
        )
        self.attachments[att.id] = att
        return att

    def remove(self, attachment_id: str) -> None:
        if self.raise_on_remove is not None:
            raise self.raise_on_remove
        self.attachments.pop(attachment_id, None)

    def list_for_note(self, note_id: str) -> list[Attachment]:
        return [a for a in self.attachments.values() if a.note_id == note_id]

    def get_bytes(self, _attachment_id: str) -> bytes:
        return b""

    def count_for_note(self, note_id: str) -> int:
        return sum(1 for a in self.attachments.values() if a.note_id == note_id)


class _Recorder:
    """Captures controller signal emissions."""

    events: list[tuple[str, tuple[object, ...]]]

    def __init__(self, controller: NoteController) -> None:
        self.events = []
        for signal in ("notes-changed", "attachment-rejected", "storage-error"):
            controller.connect(signal, self._make_handler(signal))

    def _make_handler(self, signal: str):  # type: ignore[no-untyped-def]
        def handler(_controller: NoteController, *args: object) -> None:
            self.events.append((signal, args))
        return handler

    def names(self) -> list[str]:
        return [e[0] for e in self.events]


def _id_sequence(prefix: str = "note-") -> Iterator[str]:
    n = 0
    while True:
        n += 1
        yield f"{prefix}{n}"


def _build_controller(
    *,
    repository: _FakeNoteRepository | None = None,
    attachments: _FakeAttachmentStore | None = None,
    app_state: AppState | None = None,
    clock_value: datetime = _FIXED_NOW,
) -> tuple[
    NoteController,
    _FakeNoteRepository,
    _FakeAttachmentStore,
    AppState,
]:
    repo = repository if repository is not None else _FakeNoteRepository()
    atts = attachments if attachments is not None else _FakeAttachmentStore()
    state = app_state if app_state is not None else AppState()
    ids = _id_sequence()
    controller = NoteController(
        repository=repo,
        attachments=atts,
        app_state=state,
        clock=lambda: clock_value,
        id_factory=lambda: next(ids),
    )
    return controller, repo, atts, state


# ---------------------------------------------------------------------------
# make_initial_source
# ---------------------------------------------------------------------------


class MakeInitialSourceTests(unittest.TestCase):
    def test_smart_all_yields_title_only(self) -> None:
        out = make_initial_source(SmartSelection(smart_filter=SmartFilter.ALL))
        self.assertEqual(out, "= Untitled\n\n")

    def test_smart_untagged_yields_title_only(self) -> None:
        out = make_initial_source(
            SmartSelection(smart_filter=SmartFilter.UNTAGGED),
        )
        self.assertEqual(out, "= Untitled\n\n")

    def test_single_tag_selection_pre_fills(self) -> None:
        out = make_initial_source(TagSelection(tags=frozenset({"baking"})))
        self.assertEqual(out, "= Untitled\n:tags: baking\n\n")

    def test_multi_tag_selection_sorted(self) -> None:
        out = make_initial_source(
            TagSelection(tags=frozenset({"bread", "baking"})),
        )
        self.assertEqual(out, "= Untitled\n:tags: baking, bread\n\n")


# ---------------------------------------------------------------------------
# create_note
# ---------------------------------------------------------------------------


class CreateNoteTests(unittest.TestCase):
    def test_creates_note_with_provided_initial_source(self) -> None:
        controller, repo, _, state = _build_controller()
        note = controller.create_note("= Untitled\n:tags: foo\n\n")
        self.assertEqual(note.source, "= Untitled\n:tags: foo\n\n")
        self.assertIn(note.id, repo.notes)
        self.assertEqual(state.selected_note_id, note.id)

    def test_uses_clock_for_both_timestamps(self) -> None:
        controller, _, _, _ = _build_controller()
        note = controller.create_note("= x\n\n")
        self.assertEqual(note.created_at, _FIXED_NOW)
        self.assertEqual(note.modified_at, _FIXED_NOW)

    def test_emits_notes_changed_then_selects(self) -> None:
        controller, _, _, state = _build_controller()
        recorder = _Recorder(controller)
        controller.create_note("= x\n\n")
        # The signal sequence: notes-changed (then AppState's own
        # notify::selected-note-id fires elsewhere — not in our recorder).
        self.assertIn("notes-changed", recorder.names())
        self.assertIsNotNone(state.selected_note_id)

    def test_database_error_emits_storage_error_and_reraises(self) -> None:
        controller, repo, _, _ = _build_controller()
        # Pre-populate so the next insert collides (a fake-defined
        # IntegrityError stand-in for the real one).
        controller.create_note("= a\n\n")  # id "note-1"
        # Force the next id to collide:
        controller._id_factory = lambda: "note-1"  # type: ignore[method-assign]
        recorder = _Recorder(controller)
        with self.assertRaises(sqlite3.DatabaseError):
            controller.create_note("= b\n\n")
        self.assertIn("storage-error", recorder.names())
        # Repository still has just the one note.
        self.assertEqual(len(repo.notes), 1)


# ---------------------------------------------------------------------------
# duplicate_note
# ---------------------------------------------------------------------------


class DuplicateNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller, self.repo, _, self.state = _build_controller()
        # Seed with one note.
        self.repo.insert(Note(
            id="seed-1",
            title="Original",
            source="= Original\n\nbody",
            snippet="body",
            tags=("baking",),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            modified_at=datetime(2025, 1, 1, tzinfo=UTC),
        ))

    def test_duplicate_appends_copy_suffix(self) -> None:
        duplicate = self.controller.duplicate_note("seed-1")
        self.assertEqual(duplicate.title, "Original (copy)")
        self.assertIn("= Original (copy)", duplicate.source)

    def test_duplicate_inherits_tags_from_original(self) -> None:
        duplicate = self.controller.duplicate_note("seed-1")
        self.assertEqual(duplicate.tags, ("baking",))

    def test_duplicate_gets_fresh_id_and_timestamps(self) -> None:
        duplicate = self.controller.duplicate_note("seed-1")
        self.assertNotEqual(duplicate.id, "seed-1")
        self.assertEqual(duplicate.created_at, _FIXED_NOW)
        self.assertEqual(duplicate.modified_at, _FIXED_NOW)

    def test_duplicate_selects_new_note(self) -> None:
        duplicate = self.controller.duplicate_note("seed-1")
        self.assertEqual(self.state.selected_note_id, duplicate.id)


# ---------------------------------------------------------------------------
# request_delete
# ---------------------------------------------------------------------------


class RequestDeleteTests(unittest.TestCase):
    def test_delete_removes_and_clears_selection_when_matching(self) -> None:
        controller, repo, _, state = _build_controller()
        controller.create_note("= a\n\n")  # selects new note
        target_id = state.selected_note_id
        assert target_id is not None
        controller.request_delete(target_id)
        self.assertNotIn(target_id, repo.notes)
        self.assertIsNone(state.selected_note_id)

    def test_delete_keeps_selection_when_unrelated(self) -> None:
        controller, repo, _, state = _build_controller()
        repo.insert(Note(
            id="other",
            title="Other",
            source="= Other\n",
            snippet="",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        ))
        controller.create_note("= a\n\n")
        sel = state.selected_note_id
        controller.request_delete("other")
        self.assertEqual(state.selected_note_id, sel)


# ---------------------------------------------------------------------------
# update_source
# ---------------------------------------------------------------------------


class UpdateSourceTests(unittest.TestCase):
    def test_update_source_writes_to_repo_and_emits(self) -> None:
        controller, repo, _, _ = _build_controller()
        controller.create_note("= a\n\n")
        target_id = next(iter(repo.notes))
        recorder = _Recorder(controller)
        controller.update_source(target_id, "= new\n\nbody")
        self.assertEqual(repo.notes[target_id].source, "= new\n\nbody")
        self.assertIn("notes-changed", recorder.names())


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


class AddAttachmentTests(unittest.TestCase):
    def test_success_emits_notes_changed(self) -> None:
        controller, _, atts, _ = _build_controller()
        recorder = _Recorder(controller)
        result = controller.add_attachment("n1", Path("/tmp/x.png"))
        self.assertIsNotNone(result)
        self.assertIn("notes-changed", recorder.names())
        self.assertEqual(len(atts.attachments), 1)

    def test_rejection_emits_attachment_rejected_signal(self) -> None:
        controller, _, atts, _ = _build_controller()
        atts.reject_with = AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT
        recorder = _Recorder(controller)
        result = controller.add_attachment("n1", Path("/tmp/x.png"))
        self.assertIsNone(result)
        self.assertIn("attachment-rejected", recorder.names())


class RemoveAttachmentTests(unittest.TestCase):
    def test_remove_emits_notes_changed(self) -> None:
        controller, _, atts, _ = _build_controller()
        atts.attachments["att-1"] = Attachment(
            id="att-1", note_id="n", filename="x.png",
            byte_size=1, mime_type=MimeKind.PNG,
        )
        recorder = _Recorder(controller)
        controller.remove_attachment("att-1")
        self.assertNotIn("att-1", atts.attachments)
        self.assertIn("notes-changed", recorder.names())


if __name__ == "__main__":
    unittest.main()
