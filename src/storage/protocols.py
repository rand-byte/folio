"""Stable typing surface for the storage and rendering layers.

Principles & invariants
-----------------------
* This module is **pure typing** — it defines :class:`typing.Protocol`
  interfaces, two type aliases for renderer resolvers, and the two
  exceptions that those protocols' contracts mention. It never imports
  from a higher layer (controllers, ui), and at runtime it never imports
  ``gi`` or ``sqlite3``. Concrete implementations live in sibling modules
  (``note_repository.py``, ``notebook_repository.py``,
  ``attachment_store.py``, ``ui/note_render/textbuffer_renderer.py``) and depend
  on this module — never the other way round.
* Every method signature uses **specific** parameter and return types —
  no ``Any``, no ``object``. The protocol *is* the contract; vague types
  here propagate vagueness to every call site.
* :class:`AttachmentRejected` and :class:`NestingTooDeep` are defined
  here, not in a separate exceptions module, because they are part of the
  call surface that callers need to catch. Putting them next to the
  protocols means controllers, repositories, and tests have a single
  import for "everything you need to talk to storage".
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

from enums import AttachmentRejectionReason, NotebookIcon
from models.attachment import Attachment
from models.note import Note
from models.notebook import Notebook

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


class NestingTooDeep(Exception):
    """Raised by :meth:`NotebookRepositoryProtocol.insert` (and the SQL
    triggers behind it) when a notebook's proposed ``parent_id`` refers
    to a notebook that is itself a child.

    The two-level hierarchy is a hard rule. UI code disables the *Add
    child notebook* action on any notebook that already has a parent, so
    in normal use this exception is unreachable; it is the defence-in-
    depth for direct repository misuse and for bugs that bypass the UI
    guard.
    """


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class NoteRepositoryProtocol(Protocol):
    """The set of operations the controllers need on the notes table.

    Every method is atomic with respect to the database. Returns are
    plain :class:`Note` dataclasses; ``sqlite3.Row`` objects never escape
    the implementation. The query methods below pre-filter by notebook /
    smart-filter / substring on the database side; further composition
    (live search query, sort dropdown) happens in :mod:`search`.
    """

    def get(self, note_id: str) -> Note: ...

    def list_by_notebook(self, notebook_id: str) -> list[Note]: ...

    def list_modified_since(self, since: datetime) -> list[Note]: ...

    def list_all(self) -> list[Note]: ...

    def search(self, query: str) -> list[Note]: ...

    def insert(self, note: Note) -> None: ...

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> None: ...

    def update_notebook(self, note_id: str, notebook_id: str) -> None: ...

    def delete(self, note_id: str) -> None: ...


class NotebookRepositoryProtocol(Protocol):
    """The set of operations the controllers need on the notebooks table.

    The two-level depth rule is enforced inside :meth:`insert` (and by
    SQL triggers covering both ``INSERT`` and ``UPDATE OF parent_id``):
    a notebook whose proposed parent already has a parent is rejected
    with :class:`NestingTooDeep`. UI code never reaches that branch in
    normal use because the *Add child notebook* action is greyed out on
    children — this protocol method's contract is the defence-in-depth.
    """

    def list_all(self) -> list[Notebook]: ...

    def get(self, notebook_id: str) -> Notebook: ...

    def insert(self, notebook: Notebook) -> None:
        """Persist a new notebook.

        Raises :class:`NestingTooDeep` when ``notebook.parent_id`` refers
        to a notebook that already has a non-``None`` ``parent_id``. The
        check happens inside the same transaction as the insert, so
        rejection is atomic with respect to the rest of the row.
        """

    def rename(self, notebook_id: str, new_name: str) -> None: ...

    def set_icon(self, notebook_id: str, icon: NotebookIcon) -> None: ...

    def delete_and_reparent_notes(
        self,
        notebook_id: str,
        target_id: str,
    ) -> None: ...


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
        """Copy an image file's bytes into the store and return its
        metadata.

        Raises :class:`AttachmentRejected` with the corresponding
        :class:`AttachmentRejectionReason` when:

        * the source file's :meth:`pathlib.Path.stat` size exceeds
          :data:`config.defaults.MAX_ATTACHMENT_BYTES`
          (``EXCEEDS_SIZE_LIMIT``) — checked before any bytes enter
          memory;
        * the file's MIME type is outside
          :class:`enums.MimeKind` (``UNSUPPORTED_MIME_TYPE``);
        * the source file cannot be opened or read (``UNREADABLE_SOURCE``).
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
