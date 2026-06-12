"""Tests for :mod:`controllers.note_controller`."""

from __future__ import annotations

import sqlite3
import unittest
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from gi.repository import GObject

from asciidoc.summary import derive_summary
from enums import AttachmentRejectionReason, SmartFilter
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import (
    NoteController,
    make_initial_source,
)
from giruntime.controllers.note_list_store import NoteListStore
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

    Mirrors the real repository: :meth:`insert` and :meth:`update_source`
    re-derive ``title`` / ``snippet`` / ``tags`` from ``source`` and
    return the persisted note, so the store wraps the derived value the
    way the production repository hands it back.
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

    def insert(self, note: Note) -> Note:
        if note.id in self.notes:
            raise sqlite3.IntegrityError(f"duplicate id {note.id}")
        summary = derive_summary(note.source)
        persisted = Note(
            id=note.id,
            title=summary.title,
            source=note.source,
            snippet=summary.snippet,
            tags=summary.tags,
            created_at=note.created_at,
            modified_at=note.modified_at,
        )
        self.notes[note.id] = persisted
        return persisted

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        existing = self.notes[note_id]
        summary = derive_summary(source)
        updated = Note(
            id=existing.id,
            title=summary.title,
            source=source,
            snippet=summary.snippet,
            tags=summary.tags,
            created_at=existing.created_at,
            modified_at=modified_at,
        )
        self.notes[note_id] = updated
        return updated

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
    """Captures controller signal emissions.

    The controller no longer has a ``notes-changed`` signal — propagation
    is via the store's ``items-changed`` — so the recorder watches the
    two toast signals plus the narrow per-note ``attachments-changed``.
    """

    events: list[tuple[str, tuple[object, ...]]]

    def __init__(self, controller: NoteController) -> None:
        self.events = []
        for signal in (
            "attachment-rejected",
            "attachments-changed",
            "storage-error",
        ):
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
    id_factory: object = None,
) -> tuple[
    NoteController,
    NoteListStore,
    _FakeNoteRepository,
    _FakeAttachmentStore,
    AppState,
]:
    repo = repository if repository is not None else _FakeNoteRepository()
    atts = attachments if attachments is not None else _FakeAttachmentStore()
    state = app_state if app_state is not None else AppState()
    if id_factory is None:
        ids = _id_sequence()

        def factory() -> str:
            return next(ids)
    else:
        factory = id_factory  # type: ignore[assignment]
    store = NoteListStore(
        repository=repo,
        clock=lambda: clock_value,
        id_factory=factory,
    )
    store.load()
    controller = NoteController(
        note_store=store,
        attachments=atts,
        app_state=state,
    )
    return controller, store, repo, atts, state


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
# No notes-changed signal
# ---------------------------------------------------------------------------


class SignalSurfaceTests(unittest.TestCase):
    def test_controller_has_no_notes_changed_signal(self) -> None:
        controller, _, _, _, _ = _build_controller()
        gtype = type(controller)
        self.assertEqual(GObject.signal_lookup("notes-changed", gtype), 0)
        self.assertNotEqual(
            GObject.signal_lookup("attachment-rejected", gtype), 0,
        )
        self.assertNotEqual(
            GObject.signal_lookup("storage-error", gtype), 0,
        )


# ---------------------------------------------------------------------------
# create_note
# ---------------------------------------------------------------------------


class CreateNoteTests(unittest.TestCase):
    def test_creates_note_with_provided_initial_source(self) -> None:
        controller, store, repo, _, state = _build_controller()
        note = controller.create_note("= Untitled\n:tags: foo\n\n")
        self.assertEqual(note.source, "= Untitled\n:tags: foo\n\n")
        self.assertIn(note.id, repo.notes)
        # The note lands in the in-memory store too.
        self.assertEqual(store.get_note(note.id).source, note.source)
        self.assertEqual(state.selected_note_id, note.id)

    def test_uses_clock_for_both_timestamps(self) -> None:
        controller, _, _, _, _ = _build_controller()
        note = controller.create_note("= x\n\n")
        self.assertEqual(note.created_at, _FIXED_NOW)
        self.assertEqual(note.modified_at, _FIXED_NOW)

    def test_selects_created_note(self) -> None:
        controller, _, _, _, state = _build_controller()
        note = controller.create_note("= x\n\n")
        self.assertEqual(state.selected_note_id, note.id)

    def test_database_error_emits_storage_error_and_leaves_store(self) -> None:
        # An id_factory that always collides forces the second insert to
        # raise; the DB-first store must not commit the failed note.
        controller, store, repo, _, _ = _build_controller(
            id_factory=lambda: "dup",
        )
        controller.create_note("= a\n\n")  # id "dup"
        recorder = _Recorder(controller)
        with self.assertRaises(sqlite3.DatabaseError):
            controller.create_note("= b\n\n")
        self.assertIn("storage-error", recorder.names())
        # Neither the repo nor the store grew.
        self.assertEqual(len(repo.notes), 1)
        self.assertEqual(store.get_n_items(), 1)


# ---------------------------------------------------------------------------
# duplicate_note
# ---------------------------------------------------------------------------


class DuplicateNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller, self.store, self.repo, _, self.state = (
            _build_controller()
        )
        # Seed with one note (tags live in the source so the duplicate
        # inherits them through the re-derive on insert).
        self.repo.insert(Note(
            id="seed-1",
            title="Original",
            source="= Original\n:tags: baking\n\nbody",
            snippet="body",
            tags=("baking",),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            modified_at=datetime(2025, 1, 1, tzinfo=UTC),
        ))
        self.store.load()

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

    def test_duplicate_lands_in_store(self) -> None:
        duplicate = self.controller.duplicate_note("seed-1")
        self.assertEqual(self.store.get_note(duplicate.id).id, duplicate.id)


# ---------------------------------------------------------------------------
# request_delete
# ---------------------------------------------------------------------------


class RequestDeleteTests(unittest.TestCase):
    def test_delete_removes_and_clears_selection_when_matching(self) -> None:
        controller, store, repo, _, state = _build_controller()
        controller.create_note("= a\n\n")  # selects new note
        target_id = state.selected_note_id
        assert target_id is not None
        controller.request_delete(target_id)
        self.assertNotIn(target_id, repo.notes)
        with self.assertRaises(KeyError):
            store.get_note(target_id)
        self.assertIsNone(state.selected_note_id)

    def test_delete_keeps_selection_when_unrelated(self) -> None:
        repo = _FakeNoteRepository()
        repo.insert(Note(
            id="other",
            title="Other",
            source="= Other\n",
            snippet="",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        ))
        # Build over the pre-seeded repo so the store loads "other".
        controller, _, _, _, state = _build_controller(repository=repo)
        controller.create_note("= a\n\n")
        sel = state.selected_note_id
        controller.request_delete("other")
        self.assertEqual(state.selected_note_id, sel)


# ---------------------------------------------------------------------------
# update_source
# ---------------------------------------------------------------------------


class UpdateSourceTests(unittest.TestCase):
    def test_update_source_writes_through_store(self) -> None:
        controller, store, repo, _, _ = _build_controller()
        created = controller.create_note("= a\n\n")
        controller.update_source(created.id, "= new\n\nbody")
        self.assertEqual(repo.notes[created.id].source, "= new\n\nbody")
        self.assertEqual(store.get_note(created.id).source, "= new\n\nbody")


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


class AddAttachmentTests(unittest.TestCase):
    def test_success_returns_attachment(self) -> None:
        controller, _, _, atts, _ = _build_controller()
        result = controller.add_attachment("n1", Path("/tmp/x.png"))
        self.assertIsNotNone(result)
        self.assertEqual(len(atts.attachments), 1)

    def test_success_emits_attachments_changed_with_note_id(self) -> None:
        # Adding never touches the note source, so this narrow signal
        # is the only thing that tells the panel and the 📎 badge to
        # refresh. It must carry the affected note's id.
        controller, _, _, _, _ = _build_controller()
        recorder = _Recorder(controller)
        controller.add_attachment("n1", Path("/tmp/x.png"))
        self.assertEqual(
            recorder.events,
            [("attachments-changed", ("n1",))],
        )

    def test_rejection_emits_attachment_rejected_signal(self) -> None:
        controller, _, _, atts, _ = _build_controller()
        atts.reject_with = AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT
        recorder = _Recorder(controller)
        result = controller.add_attachment("n1", Path("/tmp/x.png"))
        self.assertIsNone(result)
        self.assertIn("attachment-rejected", recorder.names())

    def test_rejection_does_not_emit_attachments_changed(self) -> None:
        # A rejected add changed nothing, so no observer should
        # refresh: the rejected reason rides its own toast signal.
        controller, _, _, atts, _ = _build_controller()
        atts.reject_with = AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT
        recorder = _Recorder(controller)
        controller.add_attachment("n1", Path("/tmp/x.png"))
        self.assertNotIn("attachments-changed", recorder.names())


class RemoveAttachmentTests(unittest.TestCase):
    def test_remove_drops_attachment(self) -> None:
        controller, _, _, atts, _ = _build_controller()
        atts.attachments["att-1"] = Attachment(
            id="att-1", note_id="n", filename="x.png",
            byte_size=1,
        )
        controller.remove_attachment("att-1", "n")
        self.assertNotIn("att-1", atts.attachments)

    def test_remove_emits_attachments_changed_with_note_id(self) -> None:
        controller, _, _, atts, _ = _build_controller()
        atts.attachments["att-1"] = Attachment(
            id="att-1", note_id="n", filename="x.png",
            byte_size=1,
        )
        recorder = _Recorder(controller)
        controller.remove_attachment("att-1", "n")
        self.assertEqual(
            recorder.events,
            [("attachments-changed", ("n",))],
        )

    def test_failed_remove_does_not_emit_attachments_changed(self) -> None:
        # A storage error propagates out of capturing_storage_errors
        # before the emit, so observers never refresh against a state
        # that did not change — only the storage-error toast fires.
        controller, _, _, atts, _ = _build_controller()
        atts.attachments["att-1"] = Attachment(
            id="att-1", note_id="n", filename="x.png",
            byte_size=1,
        )
        atts.raise_on_remove = sqlite3.OperationalError("locked")
        recorder = _Recorder(controller)
        with self.assertRaises(sqlite3.OperationalError):
            controller.remove_attachment("att-1", "n")
        self.assertEqual(
            recorder.names(),
            ["storage-error"],
        )


if __name__ == "__main__":
    unittest.main()
