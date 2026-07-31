"""Stable typing surface for the storage layer and the renderer's seams.

Principles & invariants
-----------------------
* This module is **pure typing** — it defines :class:`typing.Protocol`
  interfaces, three type aliases for renderer resolvers, and the
  exceptions that those protocols' contracts mention. It never imports
  from a higher layer (controllers, ui), and at runtime it never imports
  ``gi`` or ``sqlite3``. Concrete implementations live in sibling modules
  (``note_repository.py``, ``attachment_store.py``) and depend on this
  module — never the other way round.
* **A protocol lives here only while something is typed against it.**
  This is the typing surface higher layers *import*, not a catalogue of
  every storage-shaped class in the tree: a Protocol nothing annotates
  is dead weight that still has to be kept in step with an
  implementation, and it drifts silently because no checker compares
  the two. ``SessionStateProtocol`` and ``RendererProtocol`` were
  removed for exactly that reason — ``application.py`` names the
  concrete :class:`~storage.session_state_store.SessionStateStore`, and
  the note view names the concrete renderer, so neither protocol was
  ever a call-site contract. Add one back when a call site is annotated
  with it, not before.
* Consequently this module imports **no** ``gi`` at all, not even under
  ``if TYPE_CHECKING``: the only signature that named a GTK type
  (``RendererProtocol.render_into``) is gone. A widget-facing surface
  that needs a GTK type belongs next to its consumer — see
  ``ui/link_handler.py``'s ``TagTargetResolverProtocol``, which is
  declared where it is used.
* Every method signature uses **specific** parameter and return types —
  no ``Any``, no ``object``. The protocol *is* the contract; vague types
  here propagate vagueness to every call site.
* :class:`AttachmentRejected` is defined here, not in a separate
  exceptions module, because it is part of the call surface that
  callers need to catch. Putting it next to the protocols means
  controllers, repositories, and tests have a single import for
  "everything you need to talk to storage".
* Resolver aliases (:data:`ImageBytesResolver`,
  :data:`AttachmentListResolver`, :data:`ColumnWidthResolver`) are
  defined with PEP 695 ``type`` statements. They name the
  construction-time dependencies of the concrete renderer, which is why
  they are aliases rather than protocol methods: they describe
  ``__init__`` shapes, and a protocol describes a call surface.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from enums import AttachmentExportFailureReason, AttachmentRejectionReason
from models.attachment import Attachment
from models.note import Note

# ---------------------------------------------------------------------------
# Resolver type aliases (PEP 695)
# ---------------------------------------------------------------------------

type ImageBytesResolver = Callable[[str], bytes]
"""Resolves an image identifier (filename or attachment id, as agreed
between the renderer and its caller) to the raw image bytes.

Injected at construction of the concrete renderer so tests can pass a
fake (e.g. a function returning a 1x1 PNG) and production can wire
:meth:`AttachmentStoreProtocol.get_bytes` through a closure that captures
the current note context.
"""

type AttachmentListResolver = Callable[[], tuple[Attachment, ...]]
"""Returns the attachment **metadata** of the note currently rendered.

The renderer calls this once per render to expand an
``attachments::[]`` macro into an ordinary table. It is deliberately a
metadata-only surface — no BLOB is touched to *draw* the table, which
is the whole point of the metadata/bytes split; bytes are pulled only
when the reader actually saves an attachment.

Injected at construction of the concrete renderer, like
:data:`ImageBytesResolver`: production wires it to
:meth:`AttachmentStoreProtocol.list_for_note` through a closure that
captures the current note context, the help window wires it to a static
demo list, and tests pass a literal tuple.
"""


type ColumnWidthResolver = Callable[[], int]
"""Returns the live pixel width of the rendered article column.

The concrete renderer calls this when computing ``max-width-chars`` for
table cell labels so wrapping tracks the user's window size. Tests pass
a closure returning a fixed integer; production wires it to
``ArticleContainer.target_column_width()``.
"""


# ---------------------------------------------------------------------------
# Storage-layer exceptions
# ---------------------------------------------------------------------------


class AttachmentRejected(Exception):
    """Raised by :meth:`AttachmentStoreProtocol.add_for_note` when the
    source file cannot be accepted.

    The :attr:`reason` discriminator lets the controller pick a specific
    user-facing toast (e.g. "Image too large — 10 MB limit") without
    parsing the human-readable message. The caller should catch this
    exception by name; it must never be silently swallowed by a broader
    ``except`` clause.
    """

    reason: AttachmentRejectionReason

    def __init__(
        self,
        reason: AttachmentRejectionReason,
        message: str | None = None,
    ) -> None:
        super().__init__(
            message if message is not None else f"Attachment rejected: {reason.name}"
        )
        self.reason = reason


class AttachmentExportFailed(Exception):
    """Raised by :meth:`AttachmentStoreProtocol.export_to` when the bytes
    cannot be written to the destination the user chose.

    The mirror image of :class:`AttachmentRejected` (which guards the
    *inbound* path): same shape, same discipline. The :attr:`reason`
    discriminator lets the controller pick a specific user-facing toast
    without parsing the human-readable message, and the caller catches
    this exception by name — never via a broader ``except``.
    """

    reason: AttachmentExportFailureReason

    def __init__(
        self,
        reason: AttachmentExportFailureReason,
        message: str | None = None,
    ) -> None:
        super().__init__(
            message
            if message is not None
            else f"Attachment export failed: {reason.name}"
        )
        self.reason = reason


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class NoteRepositoryProtocol(Protocol):
    """The set of operations the controllers need on the notes table.

    Every method is atomic with respect to the database. Returns are
    plain :class:`Note` dataclasses; ``sqlite3.Row`` objects never escape
    the implementation. :meth:`list_all` materialises the whole table in
    ``modified_at DESC`` order; all further composition (filtering, tag
    AND, live search query, sort dropdown, tag counts) happens in the
    controllers/:mod:`search` layers over the in-memory list, not here.
    """

    def get(self, note_id: str) -> Note: ...

    def list_all(self) -> list[Note]: ...

    def insert(self, note: Note) -> Note:
        """Persist ``note`` and return it **as stored** — i.e. with
        ``title`` / ``snippet`` / ``tags`` freshly derived from
        ``source`` by :func:`asciidoc.summary.derive_summary`. The
        returned value is the write-through model's in-memory source of
        truth for the new row, so callers never re-read or re-derive.
        """

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        """Persist a new ``source`` for ``note_id`` and return the
        updated, derived :class:`Note`. Raises :class:`KeyError` on an
        unknown id. ``created_at`` is preserved from the existing row;
        every other field is the freshly-derived state."""

    def delete(self, note_id: str) -> None: ...


class AttachmentStoreProtocol(Protocol):
    """Read/write surface for attachment BLOBs.

    The split between :meth:`list_for_note` (metadata only — never
    selects the BLOB column) and :meth:`get_bytes` (the only path that
    materialises bytes) is a schema-level invariant from §6 of the plan.
    Adding a third method that returns metadata-plus-bytes would
    re-introduce the hazard of BLOBs leaking into the note-list query
    path; do not do that.
    """

    def add_for_note(self, note_id: str, source_path: Path) -> Attachment:
        """Copy a file's bytes into the store and return its metadata.

        Attachments are opaque blobs — there is no content-type
        allow-list. Raises :class:`AttachmentRejected` with the
        corresponding :class:`AttachmentRejectionReason` when:

        * the source file exceeds
          :data:`config.defaults.MAX_ATTACHMENT_BYTES`
          (``EXCEEDS_SIZE_LIMIT``) — enforced by a
          :meth:`pathlib.Path.stat` check before any bytes enter memory
          *and* re-checked against a bounded read, so a file that grows
          between the stat and the read is still rejected;
        * the source file cannot be stat'd, opened, or read
          (``UNREADABLE_SOURCE``).
        """

    def remove(self, attachment_id: str) -> None: ...

    def list_for_note(self, note_id: str) -> list[Attachment]:
        """Return the metadata for every attachment of ``note_id``.

        This call must **never** select the ``data`` BLOB column. The
        concrete implementation uses an explicit column list rather than
        ``SELECT *`` so the property holds even if the schema later grows
        another column.
        """

    def get_bytes(self, attachment_id: str) -> bytes:
        """Return the raw bytes of a single attachment.

        The hot path for image rendering: only the renderer should call
        this, and only when the image is actually about to be displayed.
        Listing notes or browsing attachment metadata must use
        :meth:`list_for_note` instead.
        """

    def export_to(self, attachment_id: str, destination: Path) -> None:
        """Write the attachment's bytes to ``destination`` (overwriting).

        The mirror image of :meth:`add_for_note`: the *outbound* file
        I/O belongs to the same layer that owns the inbound file I/O,
        not to a widget. Raises :class:`AttachmentExportFailed` carrying
        :data:`AttachmentExportFailureReason.UNKNOWN_ATTACHMENT` for a
        missing row, or
        :data:`AttachmentExportFailureReason.DESTINATION_UNWRITABLE`
        when the write itself fails.

        Returns **no bytes to the caller** — which is what keeps the
        metadata/bytes split intact: :meth:`get_bytes` remains the only
        method that materialises a BLOB to a caller.
        """

    def count_for_note(self, note_id: str) -> int:
        """Return how many attachments ``note_id`` has.

        A pure ``SELECT COUNT(*)`` — it materialises neither
        :class:`Attachment` objects nor BLOBs, so the note-list pane can
        surface a per-note attachment badge cheaply without touching the
        metadata/bytes split that the rest of this protocol enforces.
        """
