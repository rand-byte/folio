"""The :class:`Attachment` dataclass — attachment metadata without the bytes.

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
* Attachments carry **no content-type classification**. Any file may be
  attached; the only add-time gate is the size cap. The renderer's
  ``Gdk.Texture`` decode is what decides whether bytes referenced by an
  ``image::`` macro display as an image (falling back to the placeholder
  otherwise), and the ``filename`` extension preserves the ability to
  re-derive a content type if a future feature ever needs one.
* ``byte_size`` records the size of the bytes as they were measured at add
  time. It is the value used by quota / display logic and must equal the
  actual length of ``attachments.data``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Attachment:
    """Metadata for a single attached file.

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
        Length of the BLOB in bytes. Always positive — a zero-byte file
        is not accepted.
    """

    id: str
    note_id: str
    filename: str
    byte_size: int
