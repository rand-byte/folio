"""Orchestrates note-level user gestures across storage and app state.

Principles & invariants
-----------------------
* :class:`NoteController` is the single mediator between widgets that
  request a note-level action (create, duplicate, delete, save,
  add/remove attachment) and the storage layer that performs it.
  Widgets never call repositories directly; doing so would scatter the
  signal-emission and error-handling discipline below across every
  call site.
* Storage dependencies are injected as protocols
  (:class:`NoteRepositoryProtocol`,
  :class:`AttachmentStoreProtocol`), never as concrete classes.
* :class:`AppState` is also injected. The controller mutates app
  state (selecting a freshly created note, clearing the selection
  when the displayed note is deleted) so widgets that subscribe to
  app-state signals see a coherent picture.
* The clock and id-generator are injected as callables.
* Database errors are caught here via
  :func:`controllers._storage_errors.capturing_storage_errors`,
  surfaced as a ``storage-error`` signal carrying a human-readable
  message, and **re-raised**.
* :class:`AttachmentRejected` is caught and surfaced as the
  ``attachment-rejected`` signal carrying the
  :class:`AttachmentRejectionReason`; the method returns ``None``
  rather than re-raising.
* Successful mutations emit ``notes-changed`` so listeners re-query.
* :func:`make_initial_source` is a free function (not a method) that
  builds the initial ``:tags:`` line for a freshly-created note. The
  toolbar's *New* handler calls it against the current sidebar
  selection: a :class:`TagSelection` pre-fills the new note's
  ``:tags:`` header with the selected tags; a :class:`SmartSelection`
  yields an empty prefix (no ``:tags:`` line). Keeping it a free
  function means the toolbar can build the source-string without
  reaching through the controller, and tests can pin every branch with
  literal :data:`Selection` values.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import gi

gi.require_version("GObject", "2.0")
# pylint: disable=wrong-import-position
from gi.repository import GObject  # noqa: E402

from controllers._storage_errors import capturing_storage_errors
from controllers.app_state import AppState
from models.attachment import Attachment
from models.note import Note
from search.note_filter import Selection, SmartSelection, TagSelection
from storage.protocols import (
    AttachmentRejected,
    AttachmentStoreProtocol,
    NoteRepositoryProtocol,
)


type ClockFn = Callable[[], datetime]
type IdFactory = Callable[[], str]


_BLANK_NOTE_TITLE: Final[str] = "Untitled"
"""Title placed at the top of a freshly-created blank note."""

_TITLE_LINE: Final[str] = f"= {_BLANK_NOTE_TITLE}\n"

_DUPLICATE_TITLE_SUFFIX: Final[str] = " (copy)"
"""Suffix appended to a duplicated note's title."""


def make_initial_source(selection: Selection) -> str:
    """Return the seed source for a brand-new note under ``selection``.

    A :class:`TagSelection` produces ``"= Untitled\\n:tags: foo, bar\\n\\n"``
    where the tag list is the alphabetically-sorted selected tags; the
    trailing blank line lands the cursor below the header. A
    :class:`SmartSelection` produces only the title line + a blank
    body — no ``:tags:`` prefix (the user hasn't expressed a tag
    intent yet).

    Pure, deterministic, no GTK.
    """
    title_block = f"{_TITLE_LINE}\n"
    match selection:
        case TagSelection(tags=tags):
            tag_csv = ", ".join(sorted(tags))
            return f"{_TITLE_LINE}:tags: {tag_csv}\n\n"
        case SmartSelection():
            return title_block


def _default_clock() -> datetime:
    """Production clock — UTC, seconds resolution preserved."""
    return datetime.now(UTC)


def _default_id_factory() -> str:
    """Production id generator — UUID4 with a stable prefix."""
    return f"note-{uuid.uuid4().hex[:12]}"


def _suffix_title_in_source(source: str, suffix: str) -> str:
    """Append ``suffix`` to the first level-0 heading line in ``source``."""
    lines = source.splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("= "):
            terminator = ""
            content = raw_line
            for end in ("\r\n", "\n", "\r"):
                if content.endswith(end):
                    terminator = end
                    content = content[: -len(end)]
                    break
            lines[index] = content.rstrip() + suffix + terminator
        return "".join(lines)
    return source


class NoteController(GObject.Object):
    """Orchestrates note-level user actions.

    Signals
    -------
    notes-changed
        Fired after any successful create / duplicate / delete /
        update / attachment add / attachment remove.
    attachment-rejected
        Fired when :meth:`add_attachment` declines a file.
    storage-error
        Fired when a database operation raises. Carries a single
        :class:`str`.
    """

    __gsignals__ = {
        "notes-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "attachment-rejected": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (object,),
        ),
        "storage-error": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (str,),
        ),
    }

    _repository: NoteRepositoryProtocol
    _attachments: AttachmentStoreProtocol
    _app_state: AppState
    _clock: ClockFn
    _id_factory: IdFactory

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        repository: NoteRepositoryProtocol,
        attachments: AttachmentStoreProtocol,
        app_state: AppState,
        clock: ClockFn = _default_clock,
        id_factory: IdFactory = _default_id_factory,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._attachments = attachments
        self._app_state = app_state
        self._clock = clock
        self._id_factory = id_factory

    def _emit_storage_error(self, message: str) -> None:
        self.emit("storage-error", message)

    # ------------------------------------------------------------------
    # Note CRUD
    # ------------------------------------------------------------------

    def create_note(self, initial_source: str) -> Note:
        """Create a note with ``initial_source`` as its body and select it.

        The caller (typically the toolbar's *New* handler) builds
        ``initial_source`` via :func:`make_initial_source` against
        the current :data:`Selection`. The repository derives the
        cached columns and the junction-table tag rows from
        ``initial_source`` on insert — the controller does not
        re-derive.

        On success the controller emits ``notes-changed`` and tells
        :class:`AppState` to select the new note.
        """
        now = self._clock()
        note = Note(
            id=self._id_factory(),
            title=_BLANK_NOTE_TITLE,
            source=initial_source,
            snippet="",
            # ``tags`` is advisory on insert; the repository re-derives
            # from ``source``. We pass ``()`` rather than try to parse
            # the initial source here.
            tags=(),
            created_at=now,
            modified_at=now,
        )
        with capturing_storage_errors(self._emit_storage_error, "create note"):
            self._repository.insert(note)
        self.emit("notes-changed")
        self._app_state.set_selected_note_id(note.id)
        return note

    def duplicate_note(self, note_id: str) -> Note:
        """Duplicate ``note_id`` with " (copy)" suffixed to its title."""
        original = self._repository.get(note_id)
        now = self._clock()
        new_title = original.title + _DUPLICATE_TITLE_SUFFIX
        new_source = _suffix_title_in_source(
            original.source,
            _DUPLICATE_TITLE_SUFFIX,
        )
        duplicate = Note(
            id=self._id_factory(),
            title=new_title,
            source=new_source,
            snippet=original.snippet,
            tags=original.tags,
            created_at=now,
            modified_at=now,
        )
        with capturing_storage_errors(self._emit_storage_error, "duplicate note"):
            self._repository.insert(duplicate)
        self.emit("notes-changed")
        self._app_state.set_selected_note_id(duplicate.id)
        return duplicate

    def request_delete(self, note_id: str) -> None:
        """Delete ``note_id``; clear the selection if it matched."""
        with capturing_storage_errors(self._emit_storage_error, "delete note"):
            self._repository.delete(note_id)
        if self._app_state.selected_note_id == note_id:
            self._app_state.set_selected_note_id(None)
        self.emit("notes-changed")

    def update_source(self, note_id: str, source: str) -> None:
        """Persist a new source for ``note_id`` with a fresh modified-at."""
        now = self._clock()
        with capturing_storage_errors(self._emit_storage_error, "save note"):
            self._repository.update_source(note_id, source, now)
        self.emit("notes-changed")

    # ------------------------------------------------------------------
    # Attachment management
    # ------------------------------------------------------------------

    def add_attachment(
        self,
        note_id: str,
        source_path: Path,
    ) -> Attachment | None:
        """Attach the file at ``source_path`` to ``note_id``."""
        try:
            attachment = self._attachments.add_for_note(note_id, source_path)
        except AttachmentRejected as exc:
            self.emit("attachment-rejected", exc.reason)
            return None
        self.emit("notes-changed")
        return attachment

    def remove_attachment(self, attachment_id: str) -> None:
        """Remove an attachment by id and notify listeners."""
        with capturing_storage_errors(self._emit_storage_error, "remove attachment"):
            self._attachments.remove(attachment_id)
        self.emit("notes-changed")
