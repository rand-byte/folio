"""The in-memory, write-through source of truth for the note list.

Principles & invariants
-----------------------
* :class:`NoteListStore` is a :class:`Gio.ListStore` of
  :class:`NoteItem` — the UI's in-memory truth for the **full** note,
  body included. Views bind to it through ``FilterListModel`` /
  ``SortListModel`` / ``ListView``; the derived
  :class:`controllers.tag_counts_model.TagCountsModel` wraps it too.
  SQLite remains the durable truth, reached only by the one-time
  :meth:`load` at startup and by write-through on every mutation.
* **Write-through, DB-first.** :meth:`create` / :meth:`update` /
  :meth:`delete` persist through the repository *first*; only on a
  successful write do they commit the in-memory change and let
  ``Gio.ListStore`` emit ``items-changed``. The store deliberately does
  **not** catch storage errors — it lets them propagate so it can never
  get ahead of disk. The controller wraps store calls in
  :func:`controllers._storage_errors.capturing_storage_errors`, exactly
  as it used to wrap repository calls.
* **An edit is a replace, never an in-place mutation.** Insert →
  append; delete → remove; edit → ``splice(pos, 1, [new_item])``.
  ``items-changed`` never fires for a property mutating, so both the
  filter/sort chain and the tag aggregator depend on edits surfacing as
  a replace at the same position.
* **The store cannot derive.** ``controllers`` must not import
  :mod:`asciidoc`, so the store never calls ``derive_summary``. Instead
  the repository's :meth:`insert` / :meth:`update_source` *return* the
  derived :class:`Note`, and the store wraps exactly that value. This is
  why those two methods widened their return type in the storage layer.
* :attr:`_index` maps ``note_id -> position`` and is kept current
  across every mutation. :meth:`get_note` raises :class:`KeyError` on an
  unknown id (matching the repository contract), so the editor's and
  view's existing ``except KeyError:`` clear-buffer paths keep working.
* The clock and id-generator are injected callables, owned here (moved
  off :class:`NoteController`) so test determinism is preserved at the
  layer that now creates notes.
* This module imports :mod:`gi` for ``Gio`` / ``GObject`` only — both
  are GLib, available headless — and never imports :mod:`Gtk`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from gi.repository import Gio

from config.defaults import UNTITLED
from giruntime.controllers.note_item import NoteItem
from models.note import Note
from storage.protocols import NoteRepositoryProtocol


type ClockFn = Callable[[], datetime]
type IdFactory = Callable[[], str]

_BLANK_TITLE: Final[str] = UNTITLED
"""Advisory title for a freshly-created blank note.

Advisory only: the repository re-derives ``title`` from ``source`` on
insert and returns the persisted note, which is the value the store
actually wraps. Kept here so the draft is a complete :class:`Note`.
"""


def _default_clock() -> datetime:
    """Production clock — timezone-aware UTC."""
    return datetime.now(UTC)


def _default_id_factory() -> str:
    """Production id generator — UUID4 with a stable prefix."""
    return f"note-{uuid.uuid4().hex[:12]}"


class NoteListStore(Gio.ListStore):
    """Write-through in-memory list of full notes (the UI's truth)."""

    __gtype_name__ = "FolioNoteListStore"

    _repository: NoteRepositoryProtocol
    _index: dict[str, int]
    _clock: ClockFn
    _id_factory: IdFactory

    def __init__(
        self,
        *,
        repository: NoteRepositoryProtocol,
        clock: ClockFn = _default_clock,
        id_factory: IdFactory = _default_id_factory,
    ) -> None:
        super().__init__(item_type=NoteItem)
        self._repository = repository
        self._clock = clock
        self._id_factory = id_factory
        self._index = {}

    # ------------------------------------------------------------------
    # Startup population
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Populate the store once from the durable backing store.

        Reads every full note (body included) via
        :meth:`NoteRepositoryProtocol.list_all` and appends a
        :class:`NoteItem` per note, building :attr:`_index` as it goes.
        Called exactly once at application startup; mutations thereafter
        flow through :meth:`create` / :meth:`update` / :meth:`delete`.
        """
        for note in self._repository.list_all():
            self._index[note.id] = self.get_n_items()
            self.append(NoteItem(note))

    # ------------------------------------------------------------------
    # Body reads
    # ------------------------------------------------------------------

    def get_note(self, note_id: str) -> Note:
        """Return the full :class:`Note` for ``note_id`` from memory.

        Raises :class:`KeyError` on an unknown id — the same contract as
        :meth:`NoteRepositoryProtocol.get`, so the editor and view keep
        their existing clear-buffer-on-``KeyError`` behaviour. No disk
        read: the body is resident.
        """
        item = self.get_item(self._index[note_id])
        if not isinstance(item, NoteItem):
            # ``_index`` and the store are kept in lock-step, so this is
            # unreachable; the narrowing keeps the return precisely typed.
            raise KeyError(note_id)
        return item.note

    # ------------------------------------------------------------------
    # Mutations (DB-first, then in-memory commit + items-changed)
    # ------------------------------------------------------------------

    def create(self, source: str) -> Note:
        """Persist a new note with ``source`` and append it to the store."""
        now = self._clock()
        draft = Note(
            id=self._id_factory(),
            title=_BLANK_TITLE,
            source=source,
            snippet="",
            tags=(),
            created_at=now,
            modified_at=now,
        )
        return self._persist_then_place(
            lambda: self._repository.insert(draft),
            self._commit_append,
        )

    def update(self, note_id: str, source: str) -> Note:
        """Persist a new ``source`` for ``note_id`` and replace its row."""
        return self._persist_then_place(
            lambda: self._repository.update_source(
                note_id, source, self._clock(),
            ),
            self._commit_replace,
        )

    def delete(self, note_id: str) -> None:
        """Delete ``note_id`` from disk, then remove it from the store.

        The repository write raises before any in-memory change, so a
        storage failure cannot leave the store ahead of disk.
        """
        self._repository.delete(note_id)
        pos = self._index.pop(note_id)
        self.remove(pos)
        self._reindex_from(pos)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _persist_then_place(
        self,
        write: Callable[[], Note],
        place: Callable[[Note], None],
    ) -> Note:
        """Run the DB-first write, then commit the returned note in memory.

        A storage error raised by ``write`` propagates here, so ``place``
        (the in-memory commit + ``items-changed``) never runs on failure
        — the store stays in lock-step with disk.
        """
        persisted = write()
        place(persisted)
        return persisted

    def _commit_append(self, note: Note) -> None:
        self._index[note.id] = self.get_n_items()
        self.append(NoteItem(note))

    def _commit_replace(self, note: Note) -> None:
        pos = self._index[note.id]
        self.splice(pos, 1, [NoteItem(note)])

    def _reindex_from(self, removed_pos: int) -> None:
        """Decrement every cached position that shifted left on a removal."""
        for note_id, pos in self._index.items():
            if pos > removed_pos:
                self._index[note_id] = pos - 1
