"""Stable typing surface for the storage and rendering layers.

Principles & invariants
-----------------------
* This module is **pure typing** — it defines :class:`typing.Protocol`
  interfaces, two type aliases for renderer resolvers, and the
  exception that those protocols' contracts mention. It never imports
  from a higher layer (controllers, ui), and at runtime it never imports
  ``gi`` or ``sqlite3``. Concrete implementations live in sibling modules
  (``note_repository.py``, ``attachment_store.py``,
  ``ui/note_render/textbuffer_renderer.py``) and depend on this module —
  never the other way round.
* Every method signature uses **specific** parameter and return types —
  no ``Any``, no ``object``. The protocol *is* the contract; vague types
  here propagate vagueness to every call site.
* :class:`AttachmentRejected` is defined here, not in a separate
  exceptions module, because it is part of the call surface that
  callers need to catch. Putting it next to the protocols means
  controllers, repositories, and tests have a single import for
  "everything you need to talk to storage".
* The rendering protocol references :class:`Gtk.TextBuffer` for type
  checking but never imports it at runtime. GTK is not a runtime
  dependency of this module — that arrives later in the build (step 8).
  This is achieved with the canonical ``if TYPE_CHECKING`` guard plus
  ``from __future__ import annotations`` so the name is only resolved by
  static checkers.
* Resolver aliases (:data:`ImageBytesResolver`, :data:`ColumnWidthResolver`)
  are defined with PEP 695 ``type`` statements. They name the construction
  -time dependencies of the concrete renderer; the protocol itself does
  not expose them because protocols describe call surfaces, not
  ``__init__`` shapes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from enums import AttachmentRejectionReason
from models.attachment import Attachment
from models.note import Note

if TYPE_CHECKING:
    # GTK is only a runtime dependency from build step 8 onwards. Pulling
    # it in for static type-checking only keeps this module importable on
    # any machine — including CI runs that never spin up a display server.
    from gi.repository import Gtk


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


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class NoteRepositoryProtocol(Protocol):
    """The set of operations the controllers need on the notes table.

    Every method is atomic with respect to the database. Returns are
    plain :class:`Note` dataclasses; ``sqlite3.Row`` objects never escape
    the implementation. The query methods below pre-filter by smart-filter
    / substring on the database side; further composition (tag AND,
    live search query, sort dropdown) happens in :mod:`search`.

    :meth:`list_tags` returns every distinct tag currently in use,
    paired with its note count, alphabetically ordered. This is the
    surface the sidebar's *Tags* section reads.
    """

    def get(self, note_id: str) -> Note: ...

    def list_modified_since(self, since: datetime) -> list[Note]:
        """Notes modified at/after ``since``. No longer on any UI path
        after the write-through model migration — retained for legacy
        callers and tests; do not add new consumers."""

    def list_all(self) -> list[Note]: ...

    def search(self, query: str) -> list[Note]:
        """Substring search across title/snippet/source. No longer on
        any UI path after the write-through model migration (the note
        list filters in memory) — retained for legacy callers and tests;
        do not add new consumers."""

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

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        """Distinct tags with note counts, alphabetically. No longer on
        any UI path after the write-through model migration (the sidebar
        derives tag counts from the in-memory store) — retained for
        legacy callers and tests; do not add new consumers."""


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

        * the source file's :meth:`pathlib.Path.stat` size exceeds
          :data:`config.defaults.MAX_ATTACHMENT_BYTES`
          (``EXCEEDS_SIZE_LIMIT``) — checked before any bytes enter
          memory;
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

    def count_for_note(self, note_id: str) -> int:
        """Return how many attachments ``note_id`` has.

        A pure ``SELECT COUNT(*)`` — it materialises neither
        :class:`Attachment` objects nor BLOBs, so the note-list pane can
        surface a per-note attachment badge cheaply without touching the
        metadata/bytes split that the rest of this protocol enforces.
        """


class RendererProtocol(Protocol):
    """The high-level surface controllers and the note view depend on.

    Concrete renderers (currently
    :mod:`ui.note_render.textbuffer_renderer`) take an
    :data:`ImageBytesResolver` and a :data:`ColumnWidthResolver` at
    construction so this protocol does not have to expose them — the
    protocol describes the call surface, not ``__init__``.
    """

    def render_into(
        self,
        source: str,
        buffer: Gtk.TextBuffer,
        *,
        note_id: str,
    ) -> None:
        """Parse ``source`` and populate ``buffer`` with the rendered
        AST.

        The buffer is cleared and repopulated on every call — callers
        re-invoke after every source change, never patch the buffer in
        place. Raises :class:`models.parse_error.ParseError`
        for any input outside the supported AsciiDoc subset; the caller
        is responsible for keeping the previously valid render visible
        while the source is broken.
        """
