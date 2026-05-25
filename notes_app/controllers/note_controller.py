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
  :class:`AttachmentStoreProtocol`), never as concrete classes. This
  is what lets the controller's tests run on dataclass-backed in-
  memory fakes — no GTK display, no SQLite file, no temp directories.
* :class:`AppState` is also injected. The controller mutates app
  state (selecting a freshly created note, clearing the selection
  when the displayed note is deleted) so widgets that subscribe to
  app-state signals see a coherent picture without having to listen
  to two sources at once.
* The clock and id-generator are injected as callables so tests can
  pin both to deterministic values. Production wires
  :func:`datetime.now` and a uuid-based factory; tests pass closures
  that yield fixed timestamps and counter-based ids.
* Database errors (:class:`sqlite3.DatabaseError` and its subclasses
  — :class:`OperationalError`, :class:`IntegrityError`, etc.) are
  caught here, surfaced as a ``storage-error`` signal carrying a
  human-readable message, and **re-raised**. The catch-and-emit
  pattern is shared with :class:`NotebookController` via
  :func:`notes_app.controllers._storage_errors.capturing_storage_errors`.
  Re-raising keeps the call chain honest: a UI button handler that
  called :meth:`create_note` learns the operation failed and does
  not proceed to e.g. focus the editor on a note that was never
  written. The toast already fired by signal emission satisfies
  the rule that database errors are never silently swallowed.
* :class:`AttachmentRejected` is caught and surfaced as the
  ``attachment-rejected`` signal carrying the
  :class:`AttachmentRejectionReason`; the method returns ``None``
  rather than re-raising. The distinction matters: a rejection is a
  validation failure that the user can address by picking a
  different file, not a system-level fault, so callers don't need to
  unwind their work.
* Successful mutations emit ``notes-changed`` so listeners (the note-
  list widget, primarily) re-query the repository. The signal is
  payload-free; the listener pulls a fresh list. This keeps the
  controller free of the per-listener "what subset do you care
  about?" question.
* Signal-emission ordering is consistent across methods: persist
  first, then emit ``notes-changed`` (the note list reloads), then
  update :class:`AppState` (the right pane reacts to the new
  selection). Reordering would briefly leave widgets reading state
  that doesn't match the database.
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

from notes_app.controllers._storage_errors import capturing_storage_errors
from notes_app.controllers.app_state import AppState
from notes_app.models.attachment import Attachment
from notes_app.models.note import Note
from notes_app.storage.protocols import (
    AttachmentRejected,
    AttachmentStoreProtocol,
    NoteRepositoryProtocol,
)


type ClockFn = Callable[[], datetime]
"""Callable returning a timezone-aware ``datetime`` representing 'now'.

Injected so tests can fix the clock and so production has a single
explicit place that depends on the wall clock. The default in this
module is :func:`_default_clock`, which returns
``datetime.now(UTC)``.
"""

type IdFactory = Callable[[], str]
"""Callable producing a fresh, unique identifier string for a row.

Injected so tests can use a counter and have stable ids in
assertions. The default factory uses a UUID4 to guarantee global
uniqueness, prefixed by ``note-`` so seed-data and user-data ids
remain visually distinguishable in diagnostics.
"""


_BLANK_NOTE_TITLE: Final[str] = "Untitled"
"""Title placed at the top of a freshly-created blank note.

Matches the design (``app.jsx``: ``"= Untitled\\n\\n"``) so the
"select-the-title" flow the editor performs after creation has a
consistent string to highlight.
"""

_BLANK_NOTE_SOURCE: Final[str] = f"= {_BLANK_NOTE_TITLE}\n\n"
"""Source body inserted on note creation. The trailing blank line
gives the cursor a place to land when the editor opens."""

_DUPLICATE_TITLE_SUFFIX: Final[str] = " (copy)"
"""Suffix appended to a duplicated note's title.

Mirrors ``app.jsx``'s duplication behaviour. Applied both to the
:attr:`Note.title` cache and to the level-0 heading inside
:attr:`Note.source` so the rendered view shows the new title without
the user having to edit the source.
"""


def _default_clock() -> datetime:
    """Production clock — UTC, seconds resolution preserved."""
    return datetime.now(UTC)


def _default_id_factory() -> str:
    """Production id generator — UUID4 with a stable prefix."""
    return f"note-{uuid.uuid4().hex[:12]}"


def _suffix_title_in_source(source: str, suffix: str) -> str:
    """Append ``suffix`` to the first level-0 heading line in ``source``.

    Operates on a *prefix* of the source — only the first non-blank
    line is examined, mirroring the level-0 title rule — so
    later headings are untouched. If the source has no level-0
    heading the original string is returned unchanged; the caller
    falls back to whatever cached title the duplicate already has.

    Pure, deterministic, never raises. Public for testability inside
    this module — keeps the duplicate-flow logic in one place rather
    than duplicating (no pun intended) the prefix-walk inside
    :meth:`NoteController.duplicate_note`.
    """
    lines = source.splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("= "):
            # Preserve trailing newline if there was one — splitlines
            # with keepends keeps the line terminator on the line.
            terminator = ""
            content = raw_line
            for end in ("\r\n", "\n", "\r"):
                if content.endswith(end):
                    terminator = end
                    content = content[: -len(end)]
                    break
            lines[index] = content.rstrip() + suffix + terminator
        # Either we patched the title or this first non-blank line
        # was not a level-0 heading — either way we are done.
        return "".join(lines)
    return source


class NoteController(GObject.Object):
    """Orchestrates note-level user actions.

    Signals
    -------
    notes-changed
        Fired after any successful create / duplicate / delete /
        update / attachment add / attachment remove. Listeners re-
        read the repository.
    attachment-rejected
        Fired when :meth:`add_attachment` declines a file. Carries an
        :class:`AttachmentRejectionReason` so the UI can pick the
        right toast string.
    storage-error
        Fired when a database operation raises. Carries a single
        :class:`str` — a short human-readable message of the form
        ``"Could not <action>: <exception>"``. The originating
        exception is *also* re-raised, so this signal is purely a
        notification channel.
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
        """Closure-friendly emitter passed to
        :func:`capturing_storage_errors`.

        Defining the bound-method version explicitly (rather than
        relying on ``self.emit`` partials at every call site) keeps
        the call signature of the helper a single string and gives
        type-checkers a stable target.
        """
        self.emit("storage-error", message)

    # ------------------------------------------------------------------
    # Note CRUD
    # ------------------------------------------------------------------

    def create_note(self, notebook_id: str) -> Note:
        """Create a blank note inside ``notebook_id`` and select it.

        The new note's source is :data:`_BLANK_NOTE_SOURCE`; its
        title and snippet are derived from that so the cached
        columns are valid the moment the note hits the database.
        Both timestamps are set to the injected clock's "now".

        On success the controller emits ``notes-changed`` and tells
        :class:`AppState` to select the new note. On a database
        error it emits ``storage-error`` and re-raises — neither the
        ``notes-changed`` signal nor the selection update fires, so
        widgets observing app state see no half-applied effect.
        """
        now = self._clock()
        # The blank source's derived summary is known statically: its
        # only heading flattens to ``_BLANK_NOTE_TITLE`` and it has no
        # body, so the snippet is empty. The repository re-derives both
        # columns from ``source`` on insert (it is the single owner of
        # that mapping); the values here describe the returned in-memory
        # note and match what the repository writes.
        note = Note(
            id=self._id_factory(),
            title=_BLANK_NOTE_TITLE,
            notebook_id=notebook_id,
            source=_BLANK_NOTE_SOURCE,
            snippet="",
            created_at=now,
            modified_at=now,
        )
        with capturing_storage_errors(self._emit_storage_error, "create note"):
            self._repository.insert(note)
        self.emit("notes-changed")
        self._app_state.set_selected_note_id(note.id)
        return note

    def duplicate_note(self, note_id: str) -> Note:
        """Duplicate ``note_id`` with " (copy)" suffixed to its title.

        The copy keeps the original's notebook and content but gets a
        fresh id, fresh timestamps (both set to "now"), and an
        adjusted title — both in the cached :attr:`Note.title` field
        and in the level-0 heading inside :attr:`Note.source`, so the
        rendered view immediately shows the new title.
        """
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
            notebook_id=original.notebook_id,
            source=new_source,
            # The body is copied verbatim, so the prose snippet is
            # unchanged from the original; only the title gains the
            # suffix. The repository re-derives both columns from
            # ``new_source`` on insert — it owns that mapping — so these
            # values only describe the returned in-memory note.
            snippet=original.snippet,
            created_at=now,
            modified_at=now,
        )
        with capturing_storage_errors(self._emit_storage_error, "duplicate note"):
            self._repository.insert(duplicate)
        self.emit("notes-changed")
        self._app_state.set_selected_note_id(duplicate.id)
        return duplicate

    def request_delete(self, note_id: str) -> None:
        """Delete ``note_id``; clear the selection if it matched.

        The method is named ``request_delete`` rather than ``delete``
        because the UI's confirmation-dialog flow lives upstream of
        this call — by the time the controller is invoked the user
        has already confirmed the destructive action.

        If the deleted note was the currently displayed one,
        :class:`AppState`'s :attr:`selected_note_id` is set to
        ``None``. The note-list widget reacts to that and picks a
        neighbour, mirroring ``app.jsx``'s post-delete behaviour
        without the controller having to know about the filtered
        list.
        """
        with capturing_storage_errors(self._emit_storage_error, "delete note"):
            self._repository.delete(note_id)
        if self._app_state.selected_note_id == note_id:
            self._app_state.set_selected_note_id(None)
        self.emit("notes-changed")

    def update_source(self, note_id: str, source: str) -> None:
        """Persist a new source for ``note_id`` with a fresh
        modified-at timestamp.

        The repository derives :attr:`Note.title` and
        :attr:`Note.snippet` from the new source — there is exactly
        one place that owns the source-to-cached-columns mapping. The
        controller does not re-derive here, even though it could; a
        single owner is the better invariant.
        """
        now = self._clock()
        with capturing_storage_errors(self._emit_storage_error, "save note"):
            self._repository.update_source(note_id, source, now)
        self.emit("notes-changed")

    def move_to_notebook(self, note_id: str, notebook_id: str) -> None:
        """Reassign ``note_id`` to ``notebook_id``.

        Used by the sidebar's drag-drop / context-menu "move to…"
        flows. The note's source and timestamps are unchanged — a
        move is a categorisation change, not an edit.
        """
        with capturing_storage_errors(self._emit_storage_error, "move note"):
            self._repository.update_notebook(note_id, notebook_id)
        self.emit("notes-changed")

    # ------------------------------------------------------------------
    # Attachment management
    # ------------------------------------------------------------------

    def add_attachment(
        self,
        note_id: str,
        source_path: Path,
    ) -> Attachment | None:
        """Attach the file at ``source_path`` to ``note_id``.

        Validation (size cap, MIME allow-list, readability) lives in
        :meth:`AttachmentStoreProtocol.add_for_note`. The controller's
        only job is to translate an :class:`AttachmentRejected`
        exception into a typed signal and a ``None`` return; on
        success it emits ``notes-changed`` and returns the new
        :class:`Attachment` metadata so the caller can immediately
        refer to the attachment by id (e.g. to insert an
        ``image::filename[]`` macro into the source).
        """
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
