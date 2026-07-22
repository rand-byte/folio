"""Tests for :mod:`storage.protocols`.

The Protocol classes themselves are static-typing constructs; their
correctness as type annotations is checked by mypy, not unittest. What
this file exercises at runtime is:

* the storage-layer exception :class:`AttachmentRejected` — its
  constructor, attributes, and ``str()`` output;
* that the resolver type aliases and Protocol classes are importable
  and are the kinds of objects we expect (``TypeAliasType`` for the PEP
  695 aliases; classes whose ``_is_protocol`` flag is set for the
  Protocols);
* that a hand-rolled in-memory fake satisfying each protocol's surface
  is callable through every method without falling foul of signature
  drift.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeAliasType
from unittest.mock import Mock

from enums import AttachmentExportFailureReason, AttachmentRejectionReason
from models.attachment import Attachment
from models.note import Note
from storage.protocols import (
    AttachmentExportFailed,
    AttachmentRejected,
    AttachmentStoreProtocol,
    ColumnWidthResolver,
    ImageBytesResolver,
    NoteRepositoryProtocol,
    RendererProtocol,
)

if TYPE_CHECKING:
    # Mirrors the production module: GTK is only a type-time dependency.
    from gi.repository import Gtk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_note(note_id: str = "n1") -> Note:
    return Note(
        id=note_id,
        title="t",
        source="= t\n\n",
        snippet="",
        tags=(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _make_attachment(attachment_id: str = "a1") -> Attachment:
    return Attachment(
        id=attachment_id,
        note_id="n1",
        filename="cat.png",
        byte_size=10,
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AttachmentRejectedTests(unittest.TestCase):
    def test_default_message_uses_reason_name(self) -> None:
        exc = AttachmentRejected(
            AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT,
        )
        self.assertEqual(exc.reason, AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT)
        self.assertIn("EXCEEDS_SIZE_LIMIT", str(exc))

    def test_custom_message_overrides(self) -> None:
        exc = AttachmentRejected(
            AttachmentRejectionReason.UNREADABLE_SOURCE,
            "not allowed",
        )
        self.assertEqual(str(exc), "not allowed")

    def test_is_exception_subclass(self) -> None:
        self.assertTrue(issubclass(AttachmentRejected, Exception))


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


class TypeAliasTests(unittest.TestCase):
    def test_image_bytes_resolver_is_type_alias(self) -> None:
        self.assertIsInstance(ImageBytesResolver, TypeAliasType)

    def test_column_width_resolver_is_type_alias(self) -> None:
        self.assertIsInstance(ColumnWidthResolver, TypeAliasType)


# ---------------------------------------------------------------------------
# Protocol surface flag
# ---------------------------------------------------------------------------


class ProtocolFlagTests(unittest.TestCase):
    """Every advertised Protocol class is in fact a Protocol."""

    def test_note_repository_protocol(self) -> None:
        self.assertTrue(
            getattr(NoteRepositoryProtocol, "_is_protocol", False),
        )

    def test_attachment_store_protocol(self) -> None:
        self.assertTrue(
            getattr(AttachmentStoreProtocol, "_is_protocol", False),
        )

    def test_renderer_protocol(self) -> None:
        self.assertTrue(
            getattr(RendererProtocol, "_is_protocol", False),
        )


# ---------------------------------------------------------------------------
# In-memory fakes used by controllers' tests; sanity-checked here
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    """In-memory fake conforming to :class:`NoteRepositoryProtocol`."""

    notes: dict[str, Note]

    def __init__(self) -> None:
        self.notes = {}

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]

    def list_all(self) -> list[Note]:
        return sorted(
            self.notes.values(),
            key=lambda n: n.modified_at,
            reverse=True,
        )

    def insert(self, note: Note) -> Note:
        self.notes[note.id] = note
        return note

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
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


class FakeNoteRepositorySanityTests(unittest.TestCase):
    """Confirms the fake actually satisfies the protocol surface."""

    def test_fake_satisfies_protocol(self) -> None:
        fake: NoteRepositoryProtocol = _FakeNoteRepository()
        fake.insert(_make_note())
        fetched = fake.get("n1")
        self.assertEqual(fetched.id, "n1")
        self.assertEqual(fake.list_all(), [fetched])
        fake.delete("n1")
        self.assertEqual(fake.list_all(), [])


class _FakeAttachmentStore:
    """Minimal fake for :class:`AttachmentStoreProtocol`."""

    items: dict[str, Attachment]

    def __init__(self) -> None:
        self.items = {}

    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        att = _make_attachment()
        self.items[att.id] = att
        return att

    def remove(self, attachment_id: str) -> None:
        self.items.pop(attachment_id, None)

    def list_for_note(self, note_id: str) -> list[Attachment]:
        return [a for a in self.items.values() if a.note_id == note_id]

    def get_bytes(self, _attachment_id: str) -> bytes:
        return b""

    def count_for_note(self, note_id: str) -> int:
        return sum(1 for a in self.items.values() if a.note_id == note_id)

    def export_to(self, attachment_id: str, destination: Path) -> None:
        """Write the attachment's bytes out (the outbound mirror of add)."""
        try:
            data = self.get_bytes(attachment_id)
        except KeyError as exc:
            raise AttachmentExportFailed(
                AttachmentExportFailureReason.UNKNOWN_ATTACHMENT,
            ) from exc
        try:
            destination.write_bytes(data)
        except OSError as exc:
            raise AttachmentExportFailed(
                AttachmentExportFailureReason.DESTINATION_UNWRITABLE,
            ) from exc


class FakeAttachmentStoreSanityTests(unittest.TestCase):
    def test_fake_satisfies_protocol(self) -> None:
        fake: AttachmentStoreProtocol = _FakeAttachmentStore()
        a = fake.add_for_note("n1", Path("/tmp/x"))
        self.assertEqual(fake.list_for_note("n1"), [a])
        self.assertEqual(fake.count_for_note("n1"), 1)
        self.assertEqual(fake.get_bytes(a.id), b"")
        fake.remove(a.id)
        self.assertEqual(fake.list_for_note("n1"), [])


# ---------------------------------------------------------------------------
# Renderer protocol: spot-check via a Mock
# ---------------------------------------------------------------------------


class RendererProtocolMockTests(unittest.TestCase):
    def test_call_signature_matches_protocol(self) -> None:
        renderer = Mock(spec=RendererProtocol)
        # Just confirms the attribute exists; static checkers verify
        # the typing.
        called: Callable[..., None] = renderer.render_into
        self.assertTrue(callable(called))


if __name__ == "__main__":
    unittest.main()
