"""Tests for :mod:`controllers.note_controller`."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from asciidoc.summary import derive_summary
from controllers.app_state import AppState
from controllers.note_controller import (
    NoteController,
    _DUPLICATE_TITLE_SUFFIX,
    _suffix_title_in_source,
)
from enums import (
    AttachmentRejectionReason,
    MimeKind,
    SmartFilter,
)
from models.attachment import Attachment
from models.note import Note
from search.note_filter import (
    NotebookSelection,
    SmartSelection,
)
from storage.protocols import (
    AttachmentRejected,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _later(offset_seconds: int) -> datetime:
    """Return a deterministic timestamp ``offset_seconds`` after
    :data:`_FIXED_NOW` for tests that need a distinct "modified at"."""
    return _FIXED_NOW + timedelta(seconds=offset_seconds)


class _CountingClock:
    """Returns a sequence of pre-baked timestamps in order.

    Tests that need to distinguish "creation time" from "modification
    time" populate the queue with two distinct values; calls beyond
    the queue's end fall back to the final value (so a controller
    that calls ``clock()`` more than expected does not raise — it
    just gets the last timestamp, which the test can detect via
    assertions on later observations).
    """

    _values: list[datetime]

    def __init__(self, values: list[datetime]) -> None:
        if not values:
            raise ValueError("clock needs at least one value")
        self._values = values
        self._index = 0

    def __call__(self) -> datetime:
        index = min(self._index, len(self._values) - 1)
        self._index += 1
        return self._values[index]


class _CountingIdFactory:
    """Returns ``"note-1"``, ``"note-2"``, … on successive calls."""

    _counter: int

    def __init__(self) -> None:
        self._counter = 0

    def __call__(self) -> str:
        self._counter += 1
        return f"note-{self._counter}"


class _FakeNoteRepository:
    """In-memory :class:`NoteRepositoryProtocol` implementation.

    Implements the dataclass-based contract that production code
    follows: returns frozen :class:`Note` instances, raises
    :class:`KeyError` for missing ids, recomputes ``title`` and
    ``snippet`` inside :meth:`update_source` via ``derive_summary`` so
    the storage-layer invariant is honoured here too.
    """

    notes: dict[str, Note]
    insert_calls: int
    fail_next_insert: BaseException | None
    fail_next_update: BaseException | None
    fail_next_delete: BaseException | None
    fail_next_update_notebook: BaseException | None

    def __init__(self) -> None:
        self.notes = {}
        self.insert_calls = 0
        self.fail_next_insert = None
        self.fail_next_update = None
        self.fail_next_delete = None
        self.fail_next_update_notebook = None

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]

    def list_by_notebook(self, notebook_id: str) -> list[Note]:
        return [n for n in self.notes.values() if n.notebook_id == notebook_id]

    def list_modified_since(self, since: datetime) -> list[Note]:
        return [n for n in self.notes.values() if n.modified_at >= since]

    def list_all(self) -> list[Note]:
        return list(self.notes.values())

    def search(self, query: str) -> list[Note]:
        return [n for n in self.notes.values() if query in n.source]

    def insert(self, note: Note) -> None:
        if self.fail_next_insert is not None:
            failure = self.fail_next_insert
            self.fail_next_insert = None
            raise failure
        self.insert_calls += 1
        self.notes[note.id] = note

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> None:
        if self.fail_next_update is not None:
            failure = self.fail_next_update
            self.fail_next_update = None
            raise failure
        if note_id not in self.notes:
            raise KeyError(note_id)
        # Storage layer recomputes title/snippet; the fake mirrors that
        # so callers see the same observable behaviour.
        existing = self.notes[note_id]
        summary = derive_summary(source)
        self.notes[note_id] = Note(
            id=existing.id,
            title=summary.title,
            notebook_id=existing.notebook_id,
            source=source,
            snippet=summary.snippet,
            created_at=existing.created_at,
            modified_at=modified_at,
        )

    def update_notebook(self, note_id: str, notebook_id: str) -> None:
        if self.fail_next_update_notebook is not None:
            failure = self.fail_next_update_notebook
            self.fail_next_update_notebook = None
            raise failure
        existing = self.notes[note_id]
        self.notes[note_id] = Note(
            id=existing.id,
            title=existing.title,
            notebook_id=notebook_id,
            source=existing.source,
            snippet=existing.snippet,
            created_at=existing.created_at,
            modified_at=existing.modified_at,
        )

    def delete(self, note_id: str) -> None:
        if self.fail_next_delete is not None:
            failure = self.fail_next_delete
            self.fail_next_delete = None
            raise failure
        del self.notes[note_id]


class _FakeAttachmentStore:
    """In-memory :class:`AttachmentStoreProtocol` implementation."""

    metadata: dict[str, Attachment]
    add_calls: list[tuple[str, Path]]
    next_add_failure: AttachmentRejected | BaseException | None
    next_remove_failure: BaseException | None
    next_attachment: Attachment | None

    def __init__(self) -> None:
        self.metadata = {}
        self.add_calls = []
        self.next_add_failure = None
        self.next_remove_failure = None
        self.next_attachment = None

    def add_for_note(self, note_id: str, source_path: Path) -> Attachment:
        self.add_calls.append((note_id, source_path))
        if self.next_add_failure is not None:
            failure = self.next_add_failure
            self.next_add_failure = None
            raise failure
        if self.next_attachment is not None:
            attachment = self.next_attachment
            self.next_attachment = None
        else:
            attachment = Attachment(
                id=f"att-{len(self.metadata) + 1}",
                note_id=note_id,
                filename=source_path.name,
                byte_size=1,
                mime_type=MimeKind.PNG,
            )
        self.metadata[attachment.id] = attachment
        return attachment

    def remove(self, attachment_id: str) -> None:
        if self.next_remove_failure is not None:
            failure = self.next_remove_failure
            self.next_remove_failure = None
            raise failure
        del self.metadata[attachment_id]

    def list_for_note(self, note_id: str) -> list[Attachment]:
        return [m for m in self.metadata.values() if m.note_id == note_id]

    def get_bytes(self, attachment_id: str) -> bytes:
        _ = attachment_id
        return b""

    def count_for_note(self, note_id: str) -> int:
        return sum(1 for m in self.metadata.values() if m.note_id == note_id)


class _SignalRecorder:
    """Records every signal a :class:`NoteController` fires.

    Connects to ``notes-changed``, ``attachment-rejected``, and
    ``storage-error`` so tests can assert on the sequence of events
    without per-test signal wiring.
    """

    events: list[tuple[str, tuple[object, ...]]]

    def __init__(self, controller: NoteController) -> None:
        self.events = []
        controller.connect("notes-changed", self._on_notes_changed)
        controller.connect("attachment-rejected", self._on_attachment_rejected)
        controller.connect("storage-error", self._on_storage_error)

    def _on_notes_changed(self, _obj: NoteController) -> None:
        self.events.append(("notes-changed", ()))

    def _on_attachment_rejected(
        self,
        _obj: NoteController,
        reason: object,
    ) -> None:
        self.events.append(("attachment-rejected", (reason,)))

    def _on_storage_error(
        self,
        _obj: NoteController,
        message: str,
    ) -> None:
        self.events.append(("storage-error", (message,)))

    def names(self) -> list[str]:
        return [event[0] for event in self.events]

    def first_payload_str(self, signal: str) -> str:
        """Return the first :class:`str`-payloaded event's first arg.

        ``storage-error`` is the only string-payloaded signal here;
        callers use this helper to keep mypy from widening the
        ``tuple[object, ...]`` payload to ``object`` at the
        assertion site.
        """
        for name, args in self.events:
            if name == signal:
                payload = args[0]
                if isinstance(payload, str):
                    return payload
                raise TypeError(
                    f"signal {signal!r} payload was {type(payload).__name__}, not str"
                )
        raise AssertionError(f"no {signal!r} event recorded")


def _make_controller(
    *,
    repository: _FakeNoteRepository | None = None,
    attachments: _FakeAttachmentStore | None = None,
    app_state: AppState | None = None,
    clock_values: list[datetime] | None = None,
    id_factory: _CountingIdFactory | None = None,
) -> tuple[
    NoteController,
    _FakeNoteRepository,
    _FakeAttachmentStore,
    AppState,
    _CountingIdFactory,
]:
    """Build a controller plus its dependencies in one place.

    The factory returns the live components alongside the
    controller so individual tests can directly observe repository
    state, attachment-store state, and app state without re-
    constructing them.
    """
    repo = repository if repository is not None else _FakeNoteRepository()
    store = attachments if attachments is not None else _FakeAttachmentStore()
    state = app_state if app_state is not None else AppState()
    ids = id_factory if id_factory is not None else _CountingIdFactory()
    clock = _CountingClock(clock_values if clock_values is not None else [_FIXED_NOW])
    controller = NoteController(
        repository=repo,
        attachments=store,
        app_state=state,
        clock=clock,
        id_factory=ids,
    )
    return controller, repo, store, state, ids


# ---------------------------------------------------------------------------
# create_note
# ---------------------------------------------------------------------------


class CreateNoteTests(unittest.TestCase):
    def test_creates_blank_note_in_target_notebook(self) -> None:
        controller, repo, _, _, _ = _make_controller()
        note = controller.create_note(notebook_id="nb-personal")
        self.assertEqual(note.notebook_id, "nb-personal")
        self.assertEqual(note.id, "note-1")
        self.assertIn("note-1", repo.notes)
        self.assertEqual(repo.insert_calls, 1)

    def test_uses_clock_for_both_timestamps(self) -> None:
        controller, _, _, _, _ = _make_controller(clock_values=[_FIXED_NOW])
        note = controller.create_note(notebook_id="nb-1")
        self.assertEqual(note.created_at, _FIXED_NOW)
        self.assertEqual(note.modified_at, _FIXED_NOW)

    def test_initial_source_is_blank_template_with_title(self) -> None:
        controller, _, _, _, _ = _make_controller()
        note = controller.create_note(notebook_id="nb-1")
        # The blank source must contain a level-0 heading so the note
        # has a real title, not the "Untitled" fallback derived from
        # an empty source.
        self.assertTrue(note.source.startswith("= "))
        self.assertEqual(note.title, "Untitled")

    def test_emits_notes_changed_then_selects_note(self) -> None:
        controller, _, _, state, _ = _make_controller()
        recorder = _SignalRecorder(controller)
        note_events: list[str | None] = []
        state.connect(
            "selected-note-changed",
            lambda obj: note_events.append(obj.selected_note_id),
        )

        controller.create_note(notebook_id="nb-1")

        # notes-changed fires before the app-state mutation.
        self.assertEqual(recorder.names(), ["notes-changed"])
        self.assertEqual(note_events, ["note-1"])
        self.assertEqual(state.selected_note_id, "note-1")

    def test_database_error_emits_storage_error_and_reraises(self) -> None:
        controller, repo, _, state, _ = _make_controller()
        repo.fail_next_insert = sqlite3.OperationalError("disk full")
        recorder = _SignalRecorder(controller)

        with self.assertRaises(sqlite3.OperationalError):
            controller.create_note(notebook_id="nb-1")

        # Toast fired; the post-success effects (notes-changed,
        # selecting the new note) did not.
        self.assertEqual(recorder.names(), ["storage-error"])
        self.assertIn("create note", recorder.first_payload_str("storage-error"))
        self.assertIsNone(state.selected_note_id)
        self.assertEqual(repo.notes, {})


# ---------------------------------------------------------------------------
# duplicate_note
# ---------------------------------------------------------------------------


class DuplicateNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _FakeNoteRepository()
        self.repo.notes["n-orig"] = Note(
            id="n-orig",
            title="Apple pie",
            notebook_id="nb-recipes",
            source="= Apple pie\n\nFlour, butter, apples.\n",
            snippet="Flour, butter, apples.",
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )

    def test_duplicates_with_copy_suffix(self) -> None:
        controller, _, _, _, _ = _make_controller(
            repository=self.repo,
            clock_values=[_later(60)],
        )
        new_note = controller.duplicate_note("n-orig")
        self.assertEqual(new_note.title, "Apple pie (copy)")
        self.assertIn("= Apple pie (copy)", new_note.source)
        self.assertEqual(new_note.notebook_id, "nb-recipes")

    def test_duplicate_gets_fresh_id_and_timestamps(self) -> None:
        controller, _, _, _, _ = _make_controller(
            repository=self.repo,
            clock_values=[_later(60)],
        )
        new_note = controller.duplicate_note("n-orig")
        self.assertEqual(new_note.id, "note-1")
        self.assertNotEqual(new_note.id, "n-orig")
        self.assertEqual(new_note.created_at, _later(60))
        self.assertEqual(new_note.modified_at, _later(60))

    def test_duplicate_persists_and_selects(self) -> None:
        controller, repo, _, state, _ = _make_controller(
            repository=self.repo,
        )
        recorder = _SignalRecorder(controller)
        controller.duplicate_note("n-orig")
        self.assertEqual(recorder.names(), ["notes-changed"])
        self.assertEqual(repo.insert_calls, 1)
        self.assertEqual(state.selected_note_id, "note-1")

    def test_duplicate_unknown_note_raises_key_error(self) -> None:
        controller, _, _, _, _ = _make_controller(repository=self.repo)
        with self.assertRaises(KeyError):
            controller.duplicate_note("does-not-exist")

    def test_duplicate_database_error_does_not_select(self) -> None:
        controller, repo, _, state, _ = _make_controller(
            repository=self.repo,
        )
        repo.fail_next_insert = sqlite3.IntegrityError("constraint")
        recorder = _SignalRecorder(controller)

        with self.assertRaises(sqlite3.IntegrityError):
            controller.duplicate_note("n-orig")

        self.assertEqual(recorder.names(), ["storage-error"])
        self.assertIsNone(state.selected_note_id)


class SuffixTitleInSourceTests(unittest.TestCase):
    """The ``_suffix_title_in_source`` helper is the duplicate-title
    rewriter; tests pin its behaviour at module level since it is
    public-facing within the package."""

    def test_appends_to_first_level_zero_heading(self) -> None:
        result = _suffix_title_in_source(
            "= Apple pie\n\nbody\n",
            _DUPLICATE_TITLE_SUFFIX,
        )
        self.assertEqual(result, "= Apple pie (copy)\n\nbody\n")

    def test_skips_blank_lines_before_heading(self) -> None:
        result = _suffix_title_in_source(
            "\n\n= Apple pie\n",
            _DUPLICATE_TITLE_SUFFIX,
        )
        self.assertEqual(result, "\n\n= Apple pie (copy)\n")

    def test_only_modifies_first_heading(self) -> None:
        # Section headings (==, ===, …) are NOT level-0; they must
        # not get the suffix. Subsequent ``= …`` headings (rare but
        # not impossible) are also untouched — the helper is a
        # one-shot title patcher, not a global rewriter.
        result = _suffix_title_in_source(
            "= First\n\n= Second\n",
            _DUPLICATE_TITLE_SUFFIX,
        )
        self.assertEqual(result, "= First (copy)\n\n= Second\n")

    def test_returns_unchanged_when_no_level_zero_heading(self) -> None:
        # A paragraph-first source has no title to suffix; the
        # helper returns the input verbatim and the caller falls
        # back to the cached title from the original Note row.
        original = "Just some text without a heading.\n"
        self.assertEqual(
            _suffix_title_in_source(original, _DUPLICATE_TITLE_SUFFIX),
            original,
        )

    def test_returns_unchanged_when_first_line_is_section(self) -> None:
        # Mid-document level-1 (==) headings are rejected by the
        # parser; here we just verify the helper doesn't apply the
        # suffix to anything other than ``= `` lines.
        original = "== Section\n"
        self.assertEqual(
            _suffix_title_in_source(original, _DUPLICATE_TITLE_SUFFIX),
            original,
        )

    def test_preserves_line_terminator(self) -> None:
        # Even with no trailing newline, the helper must not invent
        # one — the input shape is preserved.
        result = _suffix_title_in_source(
            "= Apple pie",
            _DUPLICATE_TITLE_SUFFIX,
        )
        self.assertEqual(result, "= Apple pie (copy)")

    def test_empty_source_returns_empty(self) -> None:
        self.assertEqual(_suffix_title_in_source("", _DUPLICATE_TITLE_SUFFIX), "")


# ---------------------------------------------------------------------------
# request_delete
# ---------------------------------------------------------------------------


class RequestDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _FakeNoteRepository()
        self.repo.notes["n-1"] = Note(
            id="n-1",
            title="t1",
            notebook_id="nb-1",
            source="= t1",
            snippet="",
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
        self.repo.notes["n-2"] = Note(
            id="n-2",
            title="t2",
            notebook_id="nb-1",
            source="= t2",
            snippet="",
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )

    def test_removes_note_from_repository(self) -> None:
        controller, _, _, _, _ = _make_controller(repository=self.repo)
        controller.request_delete("n-1")
        self.assertNotIn("n-1", self.repo.notes)
        self.assertIn("n-2", self.repo.notes)

    def test_emits_notes_changed(self) -> None:
        controller, _, _, _, _ = _make_controller(repository=self.repo)
        recorder = _SignalRecorder(controller)
        controller.request_delete("n-1")
        self.assertEqual(recorder.names(), ["notes-changed"])

    def test_clears_selected_note_when_deleted(self) -> None:
        controller, _, _, state, _ = _make_controller(repository=self.repo)
        state.set_selected_note_id("n-1")
        controller.request_delete("n-1")
        self.assertIsNone(state.selected_note_id)

    def test_keeps_selected_note_when_other_deleted(self) -> None:
        controller, _, _, state, _ = _make_controller(repository=self.repo)
        state.set_selected_note_id("n-2")
        controller.request_delete("n-1")
        self.assertEqual(state.selected_note_id, "n-2")

    def test_unknown_note_raises_key_error(self) -> None:
        controller, _, _, _, _ = _make_controller(repository=self.repo)
        with self.assertRaises(KeyError):
            controller.request_delete("does-not-exist")

    def test_database_error_emits_and_reraises(self) -> None:
        controller, repo, _, state, _ = _make_controller(repository=self.repo)
        state.set_selected_note_id("n-1")
        repo.fail_next_delete = sqlite3.OperationalError("locked")
        recorder = _SignalRecorder(controller)

        with self.assertRaises(sqlite3.OperationalError):
            controller.request_delete("n-1")

        self.assertEqual(recorder.names(), ["storage-error"])
        # Selection unchanged because we never reached the clear-
        # selection branch.
        self.assertEqual(state.selected_note_id, "n-1")


# ---------------------------------------------------------------------------
# update_source
# ---------------------------------------------------------------------------


class UpdateSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _FakeNoteRepository()
        self.repo.notes["n-1"] = Note(
            id="n-1",
            title="Old",
            notebook_id="nb-1",
            source="= Old\n",
            snippet="",
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )

    def test_persists_new_source(self) -> None:
        controller, _, _, _, _ = _make_controller(
            repository=self.repo,
            clock_values=[_later(60)],
        )
        controller.update_source("n-1", "= New title\n\nbody\n")
        self.assertEqual(self.repo.notes["n-1"].source, "= New title\n\nbody\n")

    def test_emits_notes_changed(self) -> None:
        controller, _, _, _, _ = _make_controller(repository=self.repo)
        recorder = _SignalRecorder(controller)
        controller.update_source("n-1", "= updated\n")
        self.assertEqual(recorder.names(), ["notes-changed"])

    def test_uses_clock_for_modified_at(self) -> None:
        controller, _, _, _, _ = _make_controller(
            repository=self.repo,
            clock_values=[_later(120)],
        )
        controller.update_source("n-1", "= updated\n")
        self.assertEqual(self.repo.notes["n-1"].modified_at, _later(120))

    def test_unknown_note_raises_key_error(self) -> None:
        controller, _, _, _, _ = _make_controller(repository=self.repo)
        with self.assertRaises(KeyError):
            controller.update_source("does-not-exist", "= x\n")

    def test_database_error_does_not_emit_notes_changed(self) -> None:
        controller, repo, _, _, _ = _make_controller(repository=self.repo)
        repo.fail_next_update = sqlite3.OperationalError("disk full")
        recorder = _SignalRecorder(controller)

        with self.assertRaises(sqlite3.OperationalError):
            controller.update_source("n-1", "= updated\n")

        self.assertEqual(recorder.names(), ["storage-error"])
        self.assertNotIn("notes-changed", recorder.names())


# ---------------------------------------------------------------------------
# move_to_notebook
# ---------------------------------------------------------------------------


class MoveToNotebookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _FakeNoteRepository()
        self.repo.notes["n-1"] = Note(
            id="n-1",
            title="t",
            notebook_id="nb-source",
            source="= t",
            snippet="",
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )

    def test_changes_notebook_id(self) -> None:
        controller, _, _, _, _ = _make_controller(repository=self.repo)
        controller.move_to_notebook("n-1", "nb-target")
        self.assertEqual(self.repo.notes["n-1"].notebook_id, "nb-target")

    def test_emits_notes_changed(self) -> None:
        controller, _, _, _, _ = _make_controller(repository=self.repo)
        recorder = _SignalRecorder(controller)
        controller.move_to_notebook("n-1", "nb-target")
        self.assertEqual(recorder.names(), ["notes-changed"])

    def test_database_error_emits_and_reraises(self) -> None:
        controller, repo, _, _, _ = _make_controller(repository=self.repo)
        repo.fail_next_update_notebook = sqlite3.OperationalError("locked")
        recorder = _SignalRecorder(controller)

        with self.assertRaises(sqlite3.OperationalError):
            controller.move_to_notebook("n-1", "nb-target")

        self.assertEqual(recorder.names(), ["storage-error"])


# ---------------------------------------------------------------------------
# add_attachment
# ---------------------------------------------------------------------------


class AddAttachmentTests(unittest.TestCase):
    def test_returns_attachment_on_success(self) -> None:
        controller, _, store, _, _ = _make_controller()
        store.next_attachment = Attachment(
            id="att-7",
            note_id="n-1",
            filename="x.png",
            byte_size=42,
            mime_type=MimeKind.PNG,
        )
        result = controller.add_attachment("n-1", Path("/tmp/x.png"))
        self.assertIsNotNone(result)
        assert result is not None  # for mypy; covered by previous assertion
        self.assertEqual(result.id, "att-7")
        self.assertEqual(store.add_calls, [("n-1", Path("/tmp/x.png"))])

    def test_emits_notes_changed_on_success(self) -> None:
        controller, _, _, _, _ = _make_controller()
        recorder = _SignalRecorder(controller)
        controller.add_attachment("n-1", Path("/tmp/x.png"))
        self.assertEqual(recorder.names(), ["notes-changed"])

    def test_rejection_emits_signal_returns_none(self) -> None:
        controller, _, store, _, _ = _make_controller()
        store.next_add_failure = AttachmentRejected(
            AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT,
        )
        recorder = _SignalRecorder(controller)

        result = controller.add_attachment("n-1", Path("/tmp/big.png"))

        self.assertIsNone(result)
        self.assertEqual(recorder.names(), ["attachment-rejected"])
        # Payload is the typed reason — UI uses it to pick a toast.
        self.assertEqual(
            recorder.events[0][1],
            (AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT,),
        )
        # No notes-changed because nothing actually changed.
        self.assertNotIn("notes-changed", recorder.names())

    def test_each_rejection_reason_passes_through(self) -> None:
        # All three reasons should arrive at the listener verbatim;
        # the controller does not coerce or rewrite them.
        for reason in AttachmentRejectionReason:
            with self.subTest(reason=reason):
                controller, _, store, _, _ = _make_controller()
                store.next_add_failure = AttachmentRejected(reason)
                recorder = _SignalRecorder(controller)
                controller.add_attachment("n-1", Path("/tmp/x.png"))
                self.assertEqual(
                    recorder.events,
                    [("attachment-rejected", (reason,))],
                )

    def test_rejection_does_not_propagate_exception(self) -> None:
        # Per the controller's contract: AttachmentRejected is a
        # validation failure, not a system fault — the call returns
        # None instead of re-raising.
        controller, _, store, _, _ = _make_controller()
        store.next_add_failure = AttachmentRejected(
            AttachmentRejectionReason.UNSUPPORTED_MIME_TYPE,
        )
        # Implicitly asserts no exception by reaching the next line.
        result = controller.add_attachment("n-1", Path("/tmp/x.bmp"))
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# remove_attachment
# ---------------------------------------------------------------------------


class RemoveAttachmentTests(unittest.TestCase):
    def test_removes_metadata_and_emits(self) -> None:
        controller, _, store, _, _ = _make_controller()
        store.metadata["att-1"] = Attachment(
            id="att-1",
            note_id="n-1",
            filename="x.png",
            byte_size=10,
            mime_type=MimeKind.PNG,
        )
        recorder = _SignalRecorder(controller)
        controller.remove_attachment("att-1")
        self.assertNotIn("att-1", store.metadata)
        self.assertEqual(recorder.names(), ["notes-changed"])

    def test_database_error_emits_and_reraises(self) -> None:
        controller, _, store, _, _ = _make_controller()
        store.next_remove_failure = sqlite3.OperationalError("locked")
        recorder = _SignalRecorder(controller)

        with self.assertRaises(sqlite3.OperationalError):
            controller.remove_attachment("att-1")

        self.assertEqual(recorder.names(), ["storage-error"])


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class ConstructionTests(unittest.TestCase):
    """Sanity tests on the constructor surface — every dependency is
    keyword-only so a future positional change is loud."""

    def test_keyword_only_construction(self) -> None:
        controller, _, _, _, _ = _make_controller()
        self.assertIsInstance(controller, NoteController)

    def test_default_clock_used_when_none_supplied(self) -> None:
        # When no clock is injected the controller must still
        # function — just that timestamps come from the wall clock.
        # We don't assert an exact value; we assert the resulting
        # note's timestamp is at least timezone-aware UTC.
        repo = _FakeNoteRepository()
        store = _FakeAttachmentStore()
        state = AppState(initial_selection=SmartSelection(SmartFilter.ALL))
        controller = NoteController(
            repository=repo,
            attachments=store,
            app_state=state,
        )
        note = controller.create_note(notebook_id="nb-1")
        self.assertEqual(note.created_at.tzinfo, UTC)

    def test_id_factory_default_yields_unique_ids(self) -> None:
        repo = _FakeNoteRepository()
        store = _FakeAttachmentStore()
        state = AppState()
        controller = NoteController(
            repository=repo,
            attachments=store,
            app_state=state,
        )
        a = controller.create_note(notebook_id="nb-1")
        b = controller.create_note(notebook_id="nb-1")
        self.assertNotEqual(a.id, b.id)

    def test_initial_selection_is_unaffected_by_controller(self) -> None:
        # Constructing the controller must not mutate app state — the
        # caller's notion of "selection" survives unchanged.
        state = AppState(
            initial_selection=NotebookSelection(notebook_id="nb-x"),
        )
        _make_controller(app_state=state)
        self.assertEqual(
            state.selection,
            NotebookSelection(notebook_id="nb-x"),
        )


if __name__ == "__main__":
    unittest.main()
