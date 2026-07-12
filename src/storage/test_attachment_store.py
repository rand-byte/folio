"""Tests for :mod:`storage.attachment_store`.

These tests run against a real :class:`Database` opened in-memory and
the v1 schema applied via :func:`apply_pending`. The database is the
unit under test alongside the repository — neither is interesting
without the other, and the in-memory backend is fast enough that
mocking the cursor would buy nothing while losing fidelity to the
foreign-key cascade behaviour we depend on.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from config.defaults import (
    MAX_ATTACHMENT_BYTES,
    SEED_WELCOME_NOTE_ID,
)
from enums import AttachmentExportFailureReason, AttachmentRejectionReason
from models.note import Note
from storage.attachment_store import (
    AttachmentStore,
    _default_id_factory,
)
from storage.database import Database
from storage.migrations import apply_pending
from storage.note_repository import NoteRepository
from storage.protocols import AttachmentExportFailed, AttachmentRejected


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)

# A 1×1 transparent PNG — the smallest valid PNG. We use it as fixture
# bytes whenever a test needs *some* payload; the attachment store
# treats every file as an opaque blob (no decode, no type gate), but
# the size and bytes round-trip tests are clearer when the bytes are
# obviously image-shaped.
_PNG_1X1: bytes = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db4"
    "0000000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _TempFileFactory:
    """Build small fixture files for tests.

    Wraps a :class:`TemporaryDirectory` so each test gets a clean
    workspace and so cleanup is automatic.
    """

    _dir: TemporaryDirectory[str]
    root: Path

    def __init__(self) -> None:
        # ``consider-using-with`` is appropriate when the temp dir's
        # lifetime is the surrounding scope. Here the factory's
        # owner (a TestCase) closes via tearDown, so a context-
        # manager `with` block would not match the test-fixture
        # lifecycle.
        # pylint: disable-next=consider-using-with
        self._dir = TemporaryDirectory()
        self.root = Path(self._dir.name)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def close(self) -> None:
        self._dir.cleanup()


def _build_database_with_note(
    note_id: str = "note-1",
) -> Database:
    """Open a fresh in-memory database, apply migrations, insert one note."""
    db = Database.in_memory()
    apply_pending(db, now=_FIXED_NOW)
    repo = NoteRepository(db)
    repo.insert(
        Note(
            id=note_id,
            title="Title",
            source="= Title\n\nbody.\n",
            snippet="body.",
            tags=(),
            created_at=_FIXED_NOW,
            modified_at=_FIXED_NOW,
        )
    )
    return db


class _IdSequence:
    """Deterministic id generator for assertions.

    Returns ``att-0001``, ``att-0002``, … in order. Stored on the
    test as a default :data:`IdFactory` so tests don't have to
    juggle UUIDs in expected values.
    """

    counter: int

    def __init__(self) -> None:
        self.counter = 0

    def __call__(self) -> str:
        self.counter += 1
        return f"att-{self.counter:04d}"


# ---------------------------------------------------------------------------
# _default_id_factory
# ---------------------------------------------------------------------------


class DefaultIdFactoryTests(unittest.TestCase):
    def test_default_id_has_att_prefix(self) -> None:
        self.assertTrue(_default_id_factory().startswith("att-"))

    def test_default_ids_are_unique(self) -> None:
        # Two consecutive calls must not collide. UUID4 makes this
        # statistically certain; we check a handful for confidence.
        ids = {_default_id_factory() for _ in range(10)}
        self.assertEqual(len(ids), 10)


# ---------------------------------------------------------------------------
# add_for_note — happy path
# ---------------------------------------------------------------------------


class AddForNoteHappyPathTests(unittest.TestCase):
    db: Database
    files: _TempFileFactory
    ids: _IdSequence
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.ids = _IdSequence()
        self.store = AttachmentStore(self.db, id_factory=self.ids)

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_add_round_trips_bytes_and_metadata(self) -> None:
        path = self.files.write("photo.png", _PNG_1X1)
        attachment = self.store.add_for_note("note-1", path)

        self.assertEqual(attachment.id, "att-0001")
        self.assertEqual(attachment.note_id, "note-1")
        self.assertEqual(attachment.filename, "photo.png")
        self.assertEqual(attachment.byte_size, len(_PNG_1X1))

        # Metadata round-trips via list_for_note.
        listed = self.store.list_for_note("note-1")
        self.assertEqual(listed, [attachment])

        # Bytes round-trip via get_bytes.
        self.assertEqual(self.store.get_bytes(attachment.id), _PNG_1X1)

    def test_non_image_extension_is_accepted(self) -> None:
        # Attachments are opaque blobs: the old MimeKind allow-list is
        # gone, so a PDF (a real MIME type, formerly rejected) attaches
        # like anything else.
        path = self.files.write("doc.pdf", b"%PDF-1.4")
        attachment = self.store.add_for_note("note-1", path)
        self.assertEqual(attachment.filename, "doc.pdf")
        self.assertEqual(self.store.get_bytes(attachment.id), b"%PDF-1.4")

    def test_unknown_extension_is_accepted(self) -> None:
        # ``.xyz`` maps to no MIME type at all — formerly an
        # UNSUPPORTED_MIME_TYPE rejection, now a plain add.
        path = self.files.write("notes.xyz", b"some bytes")
        attachment = self.store.add_for_note("note-1", path)
        self.assertEqual(attachment.filename, "notes.xyz")

    def test_file_without_extension_is_accepted(self) -> None:
        path = self.files.write("plainfile", b"data")
        attachment = self.store.add_for_note("note-1", path)
        self.assertEqual(attachment.filename, "plainfile")
        self.assertEqual(attachment.byte_size, 4)

    def test_add_two_files_yields_two_attachments(self) -> None:
        first = self.files.write("a.png", _PNG_1X1)
        second = self.files.write("b.png", _PNG_1X1 + b"\x00")  # different bytes
        att_a = self.store.add_for_note("note-1", first)
        att_b = self.store.add_for_note("note-1", second)
        self.assertNotEqual(att_a.id, att_b.id)

        listed = self.store.list_for_note("note-1")
        self.assertEqual(
            {a.id for a in listed},
            {att_a.id, att_b.id},
        )

        # Bytes are kept distinct per attachment — the BLOB store does
        # not deduplicate.
        self.assertEqual(self.store.get_bytes(att_a.id), _PNG_1X1)
        self.assertEqual(
            self.store.get_bytes(att_b.id),
            _PNG_1X1 + b"\x00",
        )

    def test_add_records_byte_size_equal_to_blob_length(self) -> None:
        # The plan's invariant: byte_size in metadata equals the actual
        # length of the BLOB. After insert, this must hold.
        payload = _PNG_1X1 * 4
        path = self.files.write("big.png", payload)
        attachment = self.store.add_for_note("note-1", path)
        self.assertEqual(attachment.byte_size, len(payload))
        self.assertEqual(
            len(self.store.get_bytes(attachment.id)),
            attachment.byte_size,
        )


# ---------------------------------------------------------------------------
# count_for_note
# ---------------------------------------------------------------------------


class CountForNoteTests(unittest.TestCase):
    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_zero_when_note_has_no_attachments(self) -> None:
        self.assertEqual(self.store.count_for_note("note-1"), 0)

    def test_counts_only_the_target_notes_attachments(self) -> None:
        self.store.add_for_note("note-1", self.files.write("a.png", _PNG_1X1))
        self.store.add_for_note(
            "note-1", self.files.write("b.png", _PNG_1X1 + b"\x00")
        )
        self.assertEqual(self.store.count_for_note("note-1"), 2)

    def test_unknown_note_id_is_zero_not_an_error(self) -> None:
        self.assertEqual(self.store.count_for_note("does-not-exist"), 0)

    def test_count_drops_after_remove(self) -> None:
        att = self.store.add_for_note(
            "note-1", self.files.write("a.png", _PNG_1X1)
        )
        self.assertEqual(self.store.count_for_note("note-1"), 1)
        self.store.remove(att.id)
        self.assertEqual(self.store.count_for_note("note-1"), 0)


# ---------------------------------------------------------------------------
# add_for_note — size cap
# ---------------------------------------------------------------------------


class AddForNoteSizeCapTests(unittest.TestCase):
    """The 10 MB cap is enforced via ``stat()`` *before* any bytes are
    read — verified by patching :func:`open` to fail if it is called
    for an over-limit input.
    """

    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_over_limit_file_raises_with_correct_reason(self) -> None:
        # We don't actually need to write 10+ MB of bytes — we lie via
        # a stat() patch. This keeps the test fast and avoids touching
        # tmpfs limits in constrained CI environments.
        path = self.files.write("huge.png", _PNG_1X1)

        original_stat = Path.stat

        def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
            real = original_stat(self, *args, **kwargs)  # type: ignore[arg-type]
            if self == path:
                # Build a tiny shim object exposing st_size only —
                # AttachmentStore reads no other field.
                class _ShimStat:  # pylint: disable=too-few-public-methods
                    st_size = MAX_ATTACHMENT_BYTES + 1
                return _ShimStat()
            return real

        with patch.object(Path, "stat", fake_stat):
            with self.assertRaises(AttachmentRejected) as ctx:
                self.store.add_for_note("note-1", path)

        self.assertIs(
            ctx.exception.reason,
            AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT,
        )

    def test_over_limit_file_does_not_open_the_source(self) -> None:
        # The stronger property: an over-limit file's bytes never enter
        # memory. We patch :meth:`Path.open` to fail loudly if it is
        # called on the test file. AttachmentStore must short-circuit
        # in the size check before reaching that line.
        path = self.files.write("huge.png", _PNG_1X1)

        original_stat = Path.stat
        original_open = Path.open

        def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
            real = original_stat(self, *args, **kwargs)  # type: ignore[arg-type]
            if self == path:
                class _ShimStat:  # pylint: disable=too-few-public-methods
                    st_size = MAX_ATTACHMENT_BYTES + 100
                return _ShimStat()
            return real

        open_calls: list[Path] = []

        def fake_open(
            self: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            open_calls.append(self)
            if self == path:
                raise AssertionError(
                    "Path.open() must not be called on an over-limit file"
                )
            return original_open(self, *args, **kwargs)  # type: ignore[call-overload]

        with patch.object(Path, "stat", fake_stat), \
             patch.object(Path, "open", fake_open):
            with self.assertRaises(AttachmentRejected):
                self.store.add_for_note("note-1", path)

        # Belt-and-braces: the calls list confirms no open targeted
        # the file in question.
        self.assertNotIn(path, open_calls)

    def test_at_exactly_the_limit_is_accepted(self) -> None:
        # The cap is "exceeds 10 MB" — strictly greater than. A file
        # of exactly MAX_ATTACHMENT_BYTES bytes is allowed.
        # We synthesise via a stat patch rather than writing 10 MB.
        path = self.files.write("ok.png", _PNG_1X1)

        original_stat = Path.stat

        def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
            real = original_stat(self, *args, **kwargs)  # type: ignore[arg-type]
            if self == path:
                class _ShimStat:  # pylint: disable=too-few-public-methods
                    st_size = MAX_ATTACHMENT_BYTES
                return _ShimStat()
            return real

        with patch.object(Path, "stat", fake_stat):
            attachment = self.store.add_for_note("note-1", path)
        # The actual bytes on disk are smaller; byte_size reflects the
        # bytes we read, not the lying stat. That's intentional — we
        # want the recorded size to match the BLOB length.
        self.assertEqual(attachment.byte_size, len(_PNG_1X1))


# ---------------------------------------------------------------------------
# add_for_note — unreadable source
# ---------------------------------------------------------------------------


class AddForNoteUnreadableSourceTests(unittest.TestCase):
    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_missing_file_is_unreadable(self) -> None:
        path = self.files.root / "does-not-exist.png"
        with self.assertRaises(AttachmentRejected) as ctx:
            self.store.add_for_note("note-1", path)
        self.assertIs(
            ctx.exception.reason,
            AttachmentRejectionReason.UNREADABLE_SOURCE,
        )

    def test_open_failure_after_passing_stat_is_unreadable(self) -> None:
        # If stat succeeds but open fails (e.g. the file is unlinked
        # in the gap between the two calls), the rejection reason is
        # still UNREADABLE_SOURCE — no half-attached row.
        path = self.files.write("photo.png", _PNG_1X1)

        original_open = Path.open

        def fake_open(
            self: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            if self == path:
                raise PermissionError("no read for you")
            return original_open(self, *args, **kwargs)  # type: ignore[call-overload]

        with patch.object(Path, "open", fake_open):
            with self.assertRaises(AttachmentRejected) as ctx:
                self.store.add_for_note("note-1", path)
        self.assertIs(
            ctx.exception.reason,
            AttachmentRejectionReason.UNREADABLE_SOURCE,
        )

        # No row was inserted on the failed read.
        self.assertEqual(self.store.list_for_note("note-1"), [])


# ---------------------------------------------------------------------------
# list_for_note — metadata-only
# ---------------------------------------------------------------------------


class ListForNoteMetadataOnlyTests(unittest.TestCase):
    """The plan's central invariant: ``list_for_note`` must not load
    BLOB bytes. We assert this via a query log around the call.
    """

    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_listing_does_not_select_data_column(self) -> None:
        # Insert one attachment so list_for_note has work to do.
        path = self.files.write("a.png", _PNG_1X1)
        self.store.add_for_note("note-1", path)

        # SQLite's set_trace_callback hands us every SQL string the
        # connection executes. We capture them across the listing
        # call and assert no SELECT ever pulled the ``data`` column.
        captured: list[str] = []
        self.db.connection.set_trace_callback(captured.append)
        try:
            self.store.list_for_note("note-1")
        finally:
            self.db.connection.set_trace_callback(None)

        # Every SELECT statement issued during the call must be
        # column-listed and must not reference ``data``. We look for
        # the literal ``data`` token between SELECT and FROM — any
        # ``SELECT * FROM`` (which would *also* drag the BLOB) shows
        # up here as a substring match against the wildcard form.
        select_statements = [
            stmt for stmt in captured
            if stmt.lstrip().upper().startswith("SELECT")
        ]
        self.assertGreater(len(select_statements), 0)
        for stmt in select_statements:
            head = stmt.split("FROM", 1)[0].lower()
            self.assertNotIn(
                " data",
                head,
                f"listing path leaked the BLOB column: {stmt!r}",
            )
            self.assertNotIn(
                "*",
                head,
                f"listing path used SELECT *: {stmt!r}",
            )

    def test_empty_for_note_with_no_attachments(self) -> None:
        self.assertEqual(self.store.list_for_note("note-1"), [])

    def test_listing_only_returns_target_notes_attachments(self) -> None:
        # Insert a second note and an attachment for each.
        repo = NoteRepository(self.db)
        repo.insert(
            Note(
                id="note-2",
                title="Other",
                source="= Other\n",
                snippet="",
                tags=(),
                created_at=_FIXED_NOW,
                modified_at=_FIXED_NOW,
            )
        )
        a = self.store.add_for_note(
            "note-1",
            self.files.write("a.png", _PNG_1X1),
        )
        b = self.store.add_for_note(
            "note-2",
            self.files.write("b.png", _PNG_1X1),
        )

        self.assertEqual(self.store.list_for_note("note-1"), [a])
        self.assertEqual(self.store.list_for_note("note-2"), [b])


# ---------------------------------------------------------------------------
# get_bytes — the hot path
# ---------------------------------------------------------------------------


class GetBytesTests(unittest.TestCase):
    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_round_trip_returns_exact_bytes(self) -> None:
        path = self.files.write("a.png", _PNG_1X1)
        att = self.store.add_for_note("note-1", path)
        self.assertEqual(self.store.get_bytes(att.id), _PNG_1X1)

    def test_unknown_id_raises_key_error(self) -> None:
        # Matches the dict-like contract used elsewhere in the storage
        # layer (NoteRepository, NotebookRepository).
        with self.assertRaises(KeyError):
            self.store.get_bytes("att-does-not-exist")

    def test_returns_bytes_type_not_memoryview(self) -> None:
        # SQLite's BLOB read returns a ``bytes`` value; we re-wrap to
        # be defensive in case a future driver hands back a
        # memoryview. Tests pin that callers can compare against
        # ``bytes`` literals.
        path = self.files.write("a.png", _PNG_1X1)
        att = self.store.add_for_note("note-1", path)
        result = self.store.get_bytes(att.id)
        self.assertIsInstance(result, bytes)


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class RemoveTests(unittest.TestCase):
    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_remove_drops_metadata_and_bytes(self) -> None:
        path = self.files.write("a.png", _PNG_1X1)
        att = self.store.add_for_note("note-1", path)

        self.store.remove(att.id)
        self.assertEqual(self.store.list_for_note("note-1"), [])
        with self.assertRaises(KeyError):
            self.store.get_bytes(att.id)

    def test_remove_unknown_id_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.store.remove("att-does-not-exist")


# ---------------------------------------------------------------------------
# Cascade-on-note-delete
# ---------------------------------------------------------------------------


class CascadeOnNoteDeleteTests(unittest.TestCase):
    """The schema's ``ON DELETE CASCADE`` must wipe attachments when
    their owning note is removed.
    """

    db: Database
    files: _TempFileFactory
    store: AttachmentStore
    repo: NoteRepository

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())
        self.repo = NoteRepository(self.db)

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_note_deletion_removes_attachments(self) -> None:
        # The seed welcome note is also present from migrations; we
        # use the explicit note we inserted.
        path = self.files.write("a.png", _PNG_1X1)
        att = self.store.add_for_note("note-1", path)

        self.repo.delete("note-1")

        self.assertEqual(self.store.list_for_note("note-1"), [])
        with self.assertRaises(KeyError):
            self.store.get_bytes(att.id)

    def test_note_deletion_does_not_touch_other_notes_attachments(self) -> None:
        # Two notes, one attachment each. Deleting one note must
        # leave the other note's attachment intact.
        self.repo.insert(
            Note(
                id="note-2",
                title="Other",
                source="= Other\n",
                snippet="",
                tags=(),
                created_at=_FIXED_NOW,
                modified_at=_FIXED_NOW,
            )
        )
        att_keep = self.store.add_for_note(
            "note-2",
            self.files.write("keep.png", _PNG_1X1),
        )
        self.store.add_for_note(
            "note-1",
            self.files.write("gone.png", _PNG_1X1),
        )

        self.repo.delete("note-1")

        self.assertEqual(self.store.list_for_note("note-2"), [att_keep])


# ---------------------------------------------------------------------------
# Welcome-note interaction
# ---------------------------------------------------------------------------


class WelcomeNoteInteractionTests(unittest.TestCase):
    """The seed welcome note exists from migration time and is a
    legitimate note id for attachments. Checking this guards against
    a regression where an attachment add against the seed note id
    might accidentally fail FK validation.
    """

    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = Database.in_memory()
        apply_pending(self.db, now=_FIXED_NOW)
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_can_attach_to_welcome_note(self) -> None:
        path = self.files.write("a.png", _PNG_1X1)
        att = self.store.add_for_note(SEED_WELCOME_NOTE_ID, path)
        self.assertEqual(att.note_id, SEED_WELCOME_NOTE_ID)


# ---------------------------------------------------------------------------
# Foreign-key enforcement
# ---------------------------------------------------------------------------


class ForeignKeyEnforcementTests(unittest.TestCase):
    """An attachment whose ``note_id`` does not exist must be rejected
    by SQLite's foreign-key enforcement. The Database class enables
    ``PRAGMA foreign_keys = ON``; this test pins that the AttachmentStore
    inherits that behaviour rather than silently inserting an orphan.
    """

    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def test_unknown_note_id_raises_integrity_error(self) -> None:
        path = self.files.write("a.png", _PNG_1X1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_for_note("note-does-not-exist", path)


class ExportToTests(unittest.TestCase):
    """``export_to`` — the outbound mirror of ``add_for_note``."""

    db: Database
    files: _TempFileFactory
    store: AttachmentStore

    def setUp(self) -> None:
        self.db = _build_database_with_note()
        self.files = _TempFileFactory()
        self.store = AttachmentStore(self.db, id_factory=_IdSequence())

    def tearDown(self) -> None:
        self.files.close()
        self.db.close()

    def _add(self, name: str = "photo.png", data: bytes = _PNG_1X1) -> str:
        path = self.files.write(name, data)
        return self.store.add_for_note("note-1", path).id

    def test_written_file_is_byte_identical(self) -> None:
        attachment_id = self._add()
        destination = self.files.root / "out.png"
        self.store.export_to(attachment_id, destination)
        self.assertEqual(destination.read_bytes(), _PNG_1X1)

    def test_export_signature_returns_no_bytes(self) -> None:
        # The metadata/bytes split: only ``get_bytes`` hands a caller
        # bytes, so the export's annotated return type must be None —
        # mypy rejects assigning its result, and at runtime the write is
        # the only observable effect.
        annotations = AttachmentStore.export_to.__annotations__
        self.assertEqual(annotations["return"], "None")

    def test_existing_destination_is_overwritten(self) -> None:
        attachment_id = self._add()
        destination = self.files.write("out.png", b"stale")
        self.store.export_to(attachment_id, destination)
        self.assertEqual(destination.read_bytes(), _PNG_1X1)

    def test_unknown_attachment_raises_with_its_reason(self) -> None:
        with self.assertRaises(AttachmentExportFailed) as ctx:
            self.store.export_to("att-nope", self.files.root / "out.png")
        self.assertEqual(
            ctx.exception.reason,
            AttachmentExportFailureReason.UNKNOWN_ATTACHMENT,
        )

    def test_unknown_attachment_writes_nothing(self) -> None:
        destination = self.files.root / "out.png"
        with self.assertRaises(AttachmentExportFailed):
            self.store.export_to("att-nope", destination)
        self.assertFalse(destination.exists())

    def test_unwritable_destination_raises_with_its_reason(self) -> None:
        attachment_id = self._add()
        # A directory that does not exist — the write raises OSError,
        # which the store translates rather than letting escape.
        destination = self.files.root / "missing-dir" / "out.png"
        with self.assertRaises(AttachmentExportFailed) as ctx:
            self.store.export_to(attachment_id, destination)
        self.assertEqual(
            ctx.exception.reason,
            AttachmentExportFailureReason.DESTINATION_UNWRITABLE,
        )


if __name__ == "__main__":
    unittest.main()
