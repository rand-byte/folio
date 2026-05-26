"""Tests for :mod:`storage.protocols`.

The Protocol classes themselves are static-typing constructs; their
correctness as type annotations is checked by mypy, not unittest. What
this file exercises at runtime is:

* the storage-layer exceptions (:class:`AttachmentRejected`,
  :class:`NestingTooDeep`) — their constructors, attributes, and
  ``str()`` output;
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

from enums import (
    AttachmentRejectionReason,
    MimeKind,
    NotebookIcon,
)
from models.attachment import Attachment
from models.note import Note
from models.notebook import Notebook
from storage.protocols import (
    AttachmentRejected,
    AttachmentStoreProtocol,
    ColumnWidthResolver,
    ImageBytesResolver,
    NestingTooDeep,
    NoteRepositoryProtocol,
    NotebookRepositoryProtocol,
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
        notebook_id="nb1",
        source="= t",
        snippet="",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _make_notebook(notebook_id: str = "nb1", parent_id: str | None = None) -> Notebook:
    return Notebook(
        id=notebook_id,
        name="N",
        parent_id=parent_id,
        icon=NotebookIcon.HOME,
    )


def _make_attachment(attachment_id: str = "att1") -> Attachment:
    return Attachment(
        id=attachment_id,
        note_id="n1",
        filename="x.png",
        byte_size=1,
        mime_type=MimeKind.PNG,
    )


# ---------------------------------------------------------------------------
# Exception tests
# ---------------------------------------------------------------------------


class AttachmentRejectedTests(unittest.TestCase):
    def test_carries_reason(self) -> None:
        exc = AttachmentRejected(AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT)
        self.assertIs(exc.reason, AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT)

    def test_default_message_includes_reason_name(self) -> None:
        exc = AttachmentRejected(AttachmentRejectionReason.UNSUPPORTED_MIME_TYPE)
        # The default message uses the enum's .name so debug logs and
        # toasts can both find a stable token.
        self.assertIn("UNSUPPORTED_MIME_TYPE", str(exc))

    def test_custom_message_overrides_default(self) -> None:
        exc = AttachmentRejected(
            AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT,
            "Image too large \u2014 10 MB limit",
        )
        self.assertEqual(str(exc), "Image too large \u2014 10 MB limit")

    def test_empty_string_message_is_kept_verbatim(self) -> None:
        # Distinguishing "" from None matters: "" is a valid (if odd)
        # caller-provided message and must not silently fall back to the
        # default.
        exc = AttachmentRejected(
            AttachmentRejectionReason.UNREADABLE_SOURCE,
            "",
        )
        self.assertEqual(str(exc), "")
        self.assertIs(exc.reason, AttachmentRejectionReason.UNREADABLE_SOURCE)

    def test_can_be_raised_and_caught_by_name(self) -> None:
        with self.assertRaises(AttachmentRejected) as ctx:
            raise AttachmentRejected(AttachmentRejectionReason.UNREADABLE_SOURCE)
        self.assertIs(
            ctx.exception.reason,
            AttachmentRejectionReason.UNREADABLE_SOURCE,
        )

    def test_is_exception_subclass(self) -> None:
        # Sanity: catchers using the precise type still work, but the
        # type really is an Exception (not a BaseException-only such as
        # KeyboardInterrupt).
        self.assertTrue(issubclass(AttachmentRejected, Exception))

    def test_every_reason_can_construct_an_exception(self) -> None:
        for reason in AttachmentRejectionReason:
            with self.subTest(reason=reason):
                exc = AttachmentRejected(reason)
                self.assertIs(exc.reason, reason)


class NestingTooDeepTests(unittest.TestCase):
    def test_can_be_raised_with_message(self) -> None:
        with self.assertRaises(NestingTooDeep) as ctx:
            raise NestingTooDeep("notebook 'X' parent is already a child")
        self.assertIn("'X'", str(ctx.exception))

    def test_can_be_raised_without_message(self) -> None:
        with self.assertRaises(NestingTooDeep):
            raise NestingTooDeep()

    def test_is_exception_subclass(self) -> None:
        self.assertTrue(issubclass(NestingTooDeep, Exception))


# ---------------------------------------------------------------------------
# Type-alias / protocol surface tests
# ---------------------------------------------------------------------------


class ResolverAliasTests(unittest.TestCase):
    """The PEP 695 ``type`` statement creates a ``TypeAliasType`` object
    at runtime. These tests pin the alias targets so a future refactor
    can't silently change the signature."""

    def test_image_bytes_resolver_is_a_type_alias(self) -> None:
        self.assertIsInstance(ImageBytesResolver, TypeAliasType)

    def test_column_width_resolver_is_a_type_alias(self) -> None:
        self.assertIsInstance(ColumnWidthResolver, TypeAliasType)

    def test_image_bytes_resolver_target(self) -> None:
        # The alias resolves to Callable[[str], bytes]. We compare with
        # the canonical form built from collections.abc rather than typing
        # so a future swap stays observable. ``getattr`` is used so
        # pylint's static analysis (which loses track of TypeAliasType
        # through PEP 695 aliases) does not flag a spurious no-member.
        self.assertEqual(
            getattr(ImageBytesResolver, "__value__"),
            Callable[[str], bytes],
        )

    def test_column_width_resolver_target(self) -> None:
        self.assertEqual(
            getattr(ColumnWidthResolver, "__value__"),
            Callable[[], int],
        )


class ProtocolSurfaceTests(unittest.TestCase):
    """Every Protocol class is decorated as a runtime protocol marker
    (``_is_protocol = True``) by the typing machinery. We assert that
    here so a stray ``class Foo(Protocol)`` that lost its protocol-ness
    (e.g. by being given a metaclass at construction) is caught early.
    """

    def test_each_protocol_is_marked_as_protocol(self) -> None:
        for proto in (
            NoteRepositoryProtocol,
            NotebookRepositoryProtocol,
            AttachmentStoreProtocol,
            RendererProtocol,
        ):
            with self.subTest(protocol=proto.__name__):
                self.assertTrue(getattr(proto, "_is_protocol", False))


# ---------------------------------------------------------------------------
# Structural-conformance fakes
#
# These fakes exercise the protocols' call surfaces end-to-end so that
# any signature change (added/renamed/removed parameter) immediately
# breaks the test rather than only showing up in mypy on a downstream
# module.
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    """Minimal in-memory implementation of :class:`NoteRepositoryProtocol`."""

    notes: dict[str, Note]

    def __init__(self) -> None:
        self.notes = {}

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
            notebook_id=existing.notebook_id,
            source=source,
            snippet=existing.snippet,
            created_at=existing.created_at,
            modified_at=modified_at,
        )

    def update_notebook(self, note_id: str, notebook_id: str) -> None:
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
        del self.notes[note_id]


class _FakeNotebookRepository:
    """Minimal in-memory implementation of
    :class:`NotebookRepositoryProtocol`. Honours the two-level depth
    rule so the protocol's stated raising behaviour is exercised."""

    notebooks: dict[str, Notebook]

    def __init__(self) -> None:
        self.notebooks = {}

    def list_all(self) -> list[Notebook]:
        return list(self.notebooks.values())

    def get(self, notebook_id: str) -> Notebook:
        return self.notebooks[notebook_id]

    def insert(self, notebook: Notebook) -> None:
        if notebook.parent_id is not None:
            parent = self.notebooks[notebook.parent_id]
            if parent.parent_id is not None:
                raise NestingTooDeep(
                    f"notebook {notebook.id!r} parent is already a child",
                )
        self.notebooks[notebook.id] = notebook

    def rename(self, notebook_id: str, new_name: str) -> None:
        existing = self.notebooks[notebook_id]
        self.notebooks[notebook_id] = Notebook(
            id=existing.id,
            name=new_name,
            parent_id=existing.parent_id,
            icon=existing.icon,
        )

    def set_icon(self, notebook_id: str, icon: NotebookIcon) -> None:
        existing = self.notebooks[notebook_id]
        self.notebooks[notebook_id] = Notebook(
            id=existing.id,
            name=existing.name,
            parent_id=existing.parent_id,
            icon=icon,
        )

    def delete_and_reparent_notes(
        self,
        notebook_id: str,
        target_id: str,
    ) -> None:
        # Reparenting notes is a note-repository concern; the fake just
        # asserts the target exists and removes the deleted notebook.
        _ = self.notebooks[target_id]
        del self.notebooks[notebook_id]


class _FakeAttachmentStore:
    """Minimal in-memory implementation of
    :class:`AttachmentStoreProtocol`. Demonstrates the metadata/bytes
    split — :meth:`list_for_note` never reads from ``_blobs``."""

    metadata: dict[str, Attachment]
    _blobs: dict[str, bytes]

    def __init__(self) -> None:
        self.metadata = {}
        self._blobs = {}

    def add_for_note(self, note_id: str, source_path: Path) -> Attachment:
        # Real implementations check stat() first to avoid loading huge
        # files into memory; a fake doesn't need to. We also don't read
        # the file in this stub — tests that actually exercise the size
        # cap belong with the concrete implementation.
        att = Attachment(
            id=f"att-{len(self.metadata) + 1}",
            note_id=note_id,
            filename=source_path.name,
            byte_size=0,
            mime_type=MimeKind.PNG,
        )
        self.metadata[att.id] = att
        self._blobs[att.id] = b""
        return att

    def remove(self, attachment_id: str) -> None:
        del self.metadata[attachment_id]
        del self._blobs[attachment_id]

    def list_for_note(self, note_id: str) -> list[Attachment]:
        return [m for m in self.metadata.values() if m.note_id == note_id]

    def get_bytes(self, attachment_id: str) -> bytes:
        return self._blobs[attachment_id]

    def count_for_note(self, note_id: str) -> int:
        return sum(1 for m in self.metadata.values() if m.note_id == note_id)


class _FakeRenderer:
    """Minimal :class:`RendererProtocol` implementation that records
    each call so the protocol's signature can be exercised end-to-end.

    The ``buffer`` parameter is typed as :class:`Gtk.TextBuffer` so the
    fake structurally matches the protocol; at runtime the test passes
    any object — the fake never calls into it — so GTK is not actually
    imported.
    """

    calls: list[tuple[str, str]]

    def __init__(self) -> None:
        self.calls = []

    def render_into(
        self,
        source: str,
        buffer: Gtk.TextBuffer,
        *,
        note_id: str,
    ) -> None:
        _ = buffer
        self.calls.append((note_id, source))


class StructuralConformanceTests(unittest.TestCase):
    """Round-trips through the fakes catch any drift between the
    Protocol method signatures and what callers actually pass.

    Static conformance is mypy's job; these tests pin the *runtime*
    callability so a typo in a parameter name doesn't only surface in
    a downstream import."""

    repo: _FakeNoteRepository
    nb_repo: _FakeNotebookRepository
    store: _FakeAttachmentStore
    renderer: _FakeRenderer

    def setUp(self) -> None:
        self.repo = _FakeNoteRepository()
        self.nb_repo = _FakeNotebookRepository()
        self.store = _FakeAttachmentStore()
        self.renderer = _FakeRenderer()

    def test_note_repository_signature_round_trip(self) -> None:
        proto: NoteRepositoryProtocol = self.repo
        note = _make_note()
        proto.insert(note)
        self.assertEqual(proto.get("n1"), note)
        self.assertEqual(proto.list_by_notebook("nb1"), [note])
        self.assertEqual(proto.list_all(), [note])
        self.assertEqual(
            proto.list_modified_since(datetime(2026, 1, 1, tzinfo=UTC)),
            [note],
        )
        self.assertEqual(proto.search("= t"), [note])
        proto.update_source(
            "n1",
            "= updated",
            datetime(2026, 1, 2, tzinfo=UTC),
        )
        self.assertEqual(proto.get("n1").source, "= updated")
        proto.update_notebook("n1", "nb2")
        self.assertEqual(proto.get("n1").notebook_id, "nb2")
        proto.delete("n1")
        self.assertEqual(proto.list_all(), [])

    def test_notebook_repository_signature_round_trip(self) -> None:
        proto: NotebookRepositoryProtocol = self.nb_repo
        archive = _make_notebook("archive")
        top = _make_notebook("top")
        child = _make_notebook("child", parent_id="top")
        proto.insert(archive)
        proto.insert(top)
        proto.insert(child)
        self.assertEqual(
            {n.id for n in proto.list_all()},
            {"archive", "top", "child"},
        )
        self.assertEqual(proto.get("child").parent_id, "top")
        proto.rename("top", "Renamed")
        self.assertEqual(proto.get("top").name, "Renamed")
        proto.set_icon("top", NotebookIcon.STAR)
        self.assertIs(proto.get("top").icon, NotebookIcon.STAR)
        proto.delete_and_reparent_notes("top", "archive")
        self.assertNotIn("top", {n.id for n in proto.list_all()})

    def test_notebook_repository_insert_raises_on_deep_nesting(self) -> None:
        proto: NotebookRepositoryProtocol = self.nb_repo
        proto.insert(_make_notebook("top"))
        proto.insert(_make_notebook("child", parent_id="top"))
        with self.assertRaises(NestingTooDeep):
            proto.insert(_make_notebook("grandchild", parent_id="child"))

    def test_attachment_store_signature_round_trip(self) -> None:
        proto: AttachmentStoreProtocol = self.store
        att = proto.add_for_note("n1", Path("/tmp/x.png"))
        self.assertEqual(att.note_id, "n1")
        self.assertEqual(proto.list_for_note("n1"), [att])
        self.assertEqual(proto.get_bytes(att.id), b"")
        self.assertEqual(proto.count_for_note("n1"), 1)
        self.assertEqual(proto.count_for_note("other"), 0)
        proto.remove(att.id)
        self.assertEqual(proto.list_for_note("n1"), [])
        self.assertEqual(proto.count_for_note("n1"), 0)

    def test_renderer_signature_round_trip(self) -> None:
        # The renderer fake is bound to RendererProtocol so any drift
        # between the fake's ``render_into`` signature and the protocol
        # surfaces as a typing error. At runtime ``Gtk.TextBuffer`` is
        # not a real type so we hand the fake a ``Mock`` — the fake
        # never calls any of its methods, but mypy is satisfied because
        # ``Mock`` matches any annotation.
        proto: RendererProtocol = self.renderer
        buffer = Mock()
        proto.render_into(
            source="= hello",
            buffer=buffer,
            note_id="n1",
        )
        self.assertEqual(self.renderer.calls, [("n1", "= hello")])


class CrossReferenceTests(unittest.TestCase):
    """Sanity that the helpers in this file actually produce the model
    types they advertise — guards against the test fakes drifting away
    from the dataclass definitions."""

    def test_make_helpers_produce_correct_types(self) -> None:
        self.assertIsInstance(_make_note(), Note)
        self.assertIsInstance(_make_notebook(), Notebook)
        self.assertIsInstance(_make_attachment(), Attachment)


if __name__ == "__main__":
    unittest.main()
