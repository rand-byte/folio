"""The :class:`Attachment` dataclass — image metadata without the bytes.

Principles & invariants
-----------------------
* :class:`Attachment` deliberately has **no ``data`` field**. The bytes
  live only in the ``attachments.data`` BLOB column and are loaded
  exclusively by ``AttachmentStoreProtocol.get_bytes`` — never as part of
  listing or browsing. Storing them on the dataclass would create an
  in-memory shape that lets BLOBs leak into the note-list query path. The
  schema is the contract; this dataclass is the type-level proof that the
  contract is honoured.
* All fields are immutable once written: an attachment is never renamed,
  never re-encoded, never re-pointed to a different note. To change any of
  these the caller deletes the attachment and adds a new one.
* ``mime_type`` is a :class:`MimeKind` enum, not a raw string. This makes
  the allow-list a type-level constraint and means widgets that switch on
  type cannot be silently broken by a new format slipping through.
* ``byte_size`` records the size of the bytes as they were measured at add
  time. It is the value used by quota / display logic and must equal the
  actual length of ``attachments.data``.
"""

from __future__ import annotations

from dataclasses import dataclass

from notes_app.enums import MimeKind


@dataclass(frozen=True)
class Attachment:
    """Metadata for a single attached image.

    Fields
    ------
    id:
        Stable identifier of this attachment.
    note_id:
        The note this attachment belongs to. ``ON DELETE CASCADE`` removes
        the attachment when the owning note is deleted.
    filename:
        The user-facing name (used in image macros and as the fallback in
        the placeholder shown when bytes fail to decode). Not unique within
        a note.
    byte_size:
        Length of the BLOB in bytes. Always positive — a zero-byte image
        is not accepted.
    mime_type:
        One of the formats the renderer knows how to display.
    """

    id: str
    note_id: str
    filename: str
    byte_size: int
    mime_type: MimeKind
