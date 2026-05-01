"""SQLite-BLOB-backed implementation of :class:`AttachmentStoreProtocol`.

Principles & invariants
-----------------------
* Attachment bytes are stored as ``BLOB`` values in the ``attachments``
  table — the schema lives in :mod:`notes_app.storage.migrations` and
  was created in v1, so this module never issues DDL. It only reads
  and writes rows.
* The 10 MB hard cap from :data:`MAX_ATTACHMENT_BYTES` is enforced via
  :meth:`pathlib.Path.stat` *before* any bytes are read into memory.
  The ordering matters: an over-limit file must not enter the process
  even briefly. The unit tests assert this by patching :func:`open` to
  fail if it is called for an over-limit input.
* Three reasons can reject an add and each maps to a distinct
  :class:`AttachmentRejectionReason`: ``EXCEEDS_SIZE_LIMIT`` (cap),
  ``UNSUPPORTED_MIME_TYPE`` (extension does not map to a
  :class:`MimeKind` member), and ``UNREADABLE_SOURCE`` (the file
  refuses to be stat'd or opened — :class:`OSError` and its subclasses).
  Each rejection raises :class:`AttachmentRejected` carrying the
  corresponding reason; the caller (controller) catches the exception
  and surfaces the right toast.
* :meth:`list_for_note` and :meth:`get_bytes` honour the metadata /
  bytes split that is the central reason BLOBs live in SQLite at all
  rather than on disk: the listing query has an explicit column list
  excluding ``data`` so a future schema growth cannot accidentally
  drag the BLOB into the listing path. ``get_bytes`` is the single
  hot path that pulls the BLOB, called only by the renderer when the
  image is actually about to be displayed.
* MIME detection uses :mod:`mimetypes` from the standard library —
  a filename-extension lookup. This is the same shape the design
  uses (filename → mime type) and avoids reading the file's header
  to sniff the content. The cost of the simpler approach is that a
  ``.png`` file with JPEG bytes would be accepted; that is a user
  error the renderer's :class:`Gdk.Texture.new_from_bytes` decode
  step catches at display time, falling back to the
  ``[Image: filename]`` placeholder. We deliberately avoid magic-byte
  sniffing because every alternative we considered (Pillow, magic,
  hand-rolled signatures) either pulled in a dependency we don't
  need or made the rejection rules harder to test.
* Id generation is injected as :data:`IdFactory` so tests can pin
  ids deterministically. The default produces UUID4-shaped ids with
  a stable ``att-`` prefix so seed data and user data remain visually
  distinguishable in diagnostics, mirroring the same convention used
  by :class:`NoteController`.
* Every public method that mutates the database does so inside a
  :meth:`Database.transaction` block. Reads execute in autocommit
  mode (no implicit transaction) so they remain cheap.
* :meth:`remove` raises :class:`KeyError` when the id is unknown,
  matching the dict-like contract the rest of the storage layer
  honours. The cascade-on-note-delete path (``ON DELETE CASCADE``
  on the ``attachments.note_id`` foreign key) is the schema's
  responsibility and is verified by tests against the v1 migration.
"""

from __future__ import annotations

import mimetypes
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Final

from notes_app.config.defaults import MAX_ATTACHMENT_BYTES
from notes_app.enums import AttachmentRejectionReason, MimeKind
from notes_app.models.attachment import Attachment
from notes_app.storage.database import Database
from notes_app.storage.protocols import AttachmentRejected


type IdFactory = Callable[[], str]
"""Callable producing a fresh, unique attachment id string.

Injected so tests can use a counter and have stable ids in
assertions. The default factory produces UUID4-shaped ids prefixed
with ``att-`` so the rows are identifiable in diagnostic queries
and so the seed-data ids cannot collide with user-data ids.
"""


_METADATA_FIELDS: Final[str] = "id, note_id, filename, byte_size, mime_type"
"""Column list reused by :meth:`list_for_note` and :meth:`_load_metadata`.

Defined once so the *exclusion* of ``data`` is enforced at a single
grep target. A future schema change that introduces another column
either updates this constant (deliberate) or remains invisible to
the metadata path (which is what we want for any future BLOB-shaped
column).
"""


def _default_id_factory() -> str:
    """Production id generator — UUID4 with a stable prefix."""
    return f"att-{uuid.uuid4().hex[:12]}"


def _mime_kind_for_path(source_path: Path) -> MimeKind | None:
    """Return the :class:`MimeKind` for ``source_path`` or ``None``.

    Resolves via :mod:`mimetypes`, which is a filename-extension
    lookup. Returning ``None`` rather than raising lets the caller
    decide whether the absence of a recognised type is an error
    (it is, in :meth:`AttachmentStore.add_for_note`) without forcing
    an exception-handling shape on every caller.
    """
    guessed, _encoding = mimetypes.guess_type(source_path.name)
    if guessed is None:
        return None
    try:
        return MimeKind(guessed)
    except ValueError:
        return None


class AttachmentStore:
    """Concrete implementation of :class:`AttachmentStoreProtocol`."""

    _db: Database
    _id_factory: IdFactory

    def __init__(
        self,
        database: Database,
        *,
        id_factory: IdFactory = _default_id_factory,
    ) -> None:
        self._db = database
        self._id_factory = id_factory

    def add_for_note(self, note_id: str, source_path: Path) -> Attachment:
        """Copy ``source_path``'s bytes into the store for ``note_id``.

        Order of validation, in this exact sequence:

        1. ``stat()`` the file. :class:`OSError` (file missing,
           permissions, symlink loop, …) → :class:`AttachmentRejected`
           with ``UNREADABLE_SOURCE``. The ``stat()`` precedes any
           open so an unreadable file never leaves a partial read in
           memory.
        2. Compare the stat-reported size against
           :data:`MAX_ATTACHMENT_BYTES`. Over-limit →
           ``EXCEEDS_SIZE_LIMIT``, *without* the bytes ever being
           loaded. The unit test patches :func:`open` to verify this.
        3. Resolve the file's MIME type by extension. Unknown or
           outside :class:`MimeKind` → ``UNSUPPORTED_MIME_TYPE``.
        4. Read the bytes (:class:`OSError` on read maps to
           ``UNREADABLE_SOURCE`` — the file passed stat but the read
           itself failed, e.g. mid-read disk error).
        5. Insert the row and return the metadata.
        """
        try:
            stat_result = source_path.stat()
        except OSError as exc:
            raise AttachmentRejected(
                AttachmentRejectionReason.UNREADABLE_SOURCE,
                f"could not stat {source_path}: {exc}",
            ) from exc

        if stat_result.st_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentRejected(
                AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT,
                f"{source_path.name} is {stat_result.st_size} bytes "
                f"(limit {MAX_ATTACHMENT_BYTES})",
            )

        mime_kind = _mime_kind_for_path(source_path)
        if mime_kind is None:
            raise AttachmentRejected(
                AttachmentRejectionReason.UNSUPPORTED_MIME_TYPE,
                f"{source_path.name}: not a supported image format",
            )

        try:
            with source_path.open("rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise AttachmentRejected(
                AttachmentRejectionReason.UNREADABLE_SOURCE,
                f"could not read {source_path}: {exc}",
            ) from exc

        # ``stat`` and the actual read can disagree if the file was
        # truncated between calls; the BLOB column gets the bytes we
        # have, the byte_size metadata reflects the same value.
        byte_size = len(data)

        attachment = Attachment(
            id=self._id_factory(),
            note_id=note_id,
            filename=source_path.name,
            byte_size=byte_size,
            mime_type=mime_kind,
        )

        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO attachments "
                "(id, note_id, filename, byte_size, mime_type, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    attachment.id,
                    attachment.note_id,
                    attachment.filename,
                    attachment.byte_size,
                    attachment.mime_type.value,
                    data,
                ),
            )

        return attachment

    def remove(self, attachment_id: str) -> None:
        """Remove a single attachment row.

        Raises :class:`KeyError` if no row matches — the dict-like
        contract the rest of the storage layer follows.
        """
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM attachments WHERE id = ?",
                (attachment_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(attachment_id)

    def list_for_note(self, note_id: str) -> list[Attachment]:
        """Return metadata for every attachment of ``note_id``.

        The query's column list is :data:`_METADATA_FIELDS` —
        explicitly excluding ``data``. This is the schema-level
        invariant from §6 of the plan: "lazy BLOB loading is a
        schema-level invariant, not a convention". A future column
        added to the table is invisible to this query unless it is
        added to the constant deliberately.
        """
        cursor = self._db.connection.execute(
            f"SELECT {_METADATA_FIELDS} FROM attachments "
            "WHERE note_id = ? "
            "ORDER BY id ASC",
            (note_id,),
        )
        return [
            Attachment(
                id=row["id"],
                note_id=row["note_id"],
                filename=row["filename"],
                byte_size=row["byte_size"],
                mime_type=MimeKind(row["mime_type"]),
            )
            for row in cursor.fetchall()
        ]

    def get_bytes(self, attachment_id: str) -> bytes:
        """Return the raw bytes of a single attachment.

        The single hot path that ``SELECT``s the ``data`` column.
        Raises :class:`KeyError` when the id is unknown — same
        contract as :meth:`remove`.
        """
        cursor = self._db.connection.execute(
            "SELECT data FROM attachments WHERE id = ?",
            (attachment_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(attachment_id)
        return bytes(row["data"])
