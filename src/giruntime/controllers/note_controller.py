"""Orchestrates note-level user gestures across the store and app state.

Principles & invariants
-----------------------
* :class:`NoteController` is the single mediator between widgets that
  request a note-level action (create, duplicate, delete, save,
  add/remove attachment) and the layer that performs it. Widgets never
  reach past it; doing so would scatter the signal-emission and
  error-handling discipline below across every call site.
* Persistence is delegated to the injected
  :class:`controllers.note_list_store.NoteListStore`, which is the UI's
  in-memory source of truth and writes through to storage DB-first. The
  controller no longer holds a repository, a clock, or an id-generator —
  those moved onto the store (the layer that now creates notes), so test
  determinism is configured there.
* :class:`AppState` is injected. The controller mutates app state
  (selecting a freshly created note, clearing the selection when the
  displayed note is deleted) so widgets that subscribe to app-state
  signals see a coherent picture.
* Database errors surface here via
  :func:`controllers._storage_errors.capturing_storage_errors`, wrapping
  the **store** call exactly as it used to wrap the repository call: the
  store does not swallow storage errors, so a failed write propagates
  out of the store, the toast fires, and the exception re-raises. The
  store's DB-first ordering guarantees no in-memory commit happened.
* :class:`AttachmentRejected` is caught and surfaced as the
  ``attachment-rejected`` signal carrying the
  :class:`AttachmentRejectionReason`; the method returns ``None``
  rather than re-raising.
* There is **no** ``notes-changed`` signal. The note list, the rendered
  view, and the sidebar all update by observing the store's
  ``items-changed`` (directly or through the derived
  :class:`controllers.tag_counts_model.TagCountsModel`), so the old
  coarse fan-out is gone. The note-list attachment badge stays live
  because adding an image inserts an image macro into the editor buffer,
  which autosaves → :meth:`update_source` → a store replace → factory
  re-bind.
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

from pathlib import Path
from typing import Final

from gi.repository import GObject

from giruntime.controllers._storage_errors import capturing_storage_errors
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_list_store import NoteListStore
from models.attachment import Attachment
from models.note import Note
from search.note_filter import Selection, SmartSelection, TagSelection
from storage.protocols import (
    AttachmentRejected,
    AttachmentStoreProtocol,
)


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
    attachment-rejected
        Fired when :meth:`add_attachment` declines a file.
    storage-error
        Fired when a database operation raises. Carries a single
        :class:`str`.
    """

    __gsignals__ = {
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

    _store: NoteListStore
    _attachments: AttachmentStoreProtocol
    _app_state: AppState

    def __init__(
        self,
        *,
        note_store: NoteListStore,
        attachments: AttachmentStoreProtocol,
        app_state: AppState,
    ) -> None:
        super().__init__()
        self._store = note_store
        self._attachments = attachments
        self._app_state = app_state

    def _emit_storage_error(self, message: str) -> None:
        self.emit("storage-error", message)

    # ------------------------------------------------------------------
    # Note CRUD
    # ------------------------------------------------------------------

    def create_note(self, initial_source: str) -> Note:
        """Create a note with ``initial_source`` as its body and select it.

        The caller (typically the toolbar's *New* handler) builds
        ``initial_source`` via :func:`make_initial_source` against the
        current :data:`Selection`. Persistence and id / clock assignment
        live in the store, which writes through DB-first and returns the
        derived note; the controller only wraps the call for the toast
        signal and then tells :class:`AppState` to select the new note.
        """
        with capturing_storage_errors(self._emit_storage_error, "create note"):
            note = self._store.create(initial_source)
        self._app_state.set_selected_note_id(note.id)
        return note

    def duplicate_note(self, note_id: str) -> Note:
        """Duplicate ``note_id`` with " (copy)" suffixed to its title."""
        original = self._store.get_note(note_id)
        new_source = _suffix_title_in_source(
            original.source,
            _DUPLICATE_TITLE_SUFFIX,
        )
        with capturing_storage_errors(self._emit_storage_error, "duplicate note"):
            duplicate = self._store.create(new_source)
        self._app_state.set_selected_note_id(duplicate.id)
        return duplicate

    def request_delete(self, note_id: str) -> None:
        """Delete ``note_id``; clear the selection if it matched."""
        with capturing_storage_errors(self._emit_storage_error, "delete note"):
            self._store.delete(note_id)
        if self._app_state.selected_note_id == note_id:
            self._app_state.set_selected_note_id(None)

    def update_source(self, note_id: str, source: str) -> None:
        """Persist a new source for ``note_id`` with a fresh modified-at."""
        with capturing_storage_errors(self._emit_storage_error, "save note"):
            self._store.update(note_id, source)

    # ------------------------------------------------------------------
    # Attachment management
    # ------------------------------------------------------------------

    def add_attachment(
        self,
        note_id: str,
        source_path: Path,
    ) -> Attachment | None:
        """Attach the file at ``source_path`` to ``note_id``.

        On success the note-list 📎 badge refreshes via the editor's
        autosave path: the image macro the caller inserts into the
        buffer triggers an :meth:`update_source`, which replaces the
        note's row in the store and re-binds the factory.
        """
        try:
            attachment = self._attachments.add_for_note(note_id, source_path)
        except AttachmentRejected as exc:
            self.emit("attachment-rejected", exc.reason)
            return None
        return attachment

    def remove_attachment(self, attachment_id: str) -> None:
        """Remove an attachment by id."""
        with capturing_storage_errors(self._emit_storage_error, "remove attachment"):
            self._attachments.remove(attachment_id)
