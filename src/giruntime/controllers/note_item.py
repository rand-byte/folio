"""A :class:`GObject.Object` view of one immutable :class:`Note`.

Principles & invariants
-----------------------
* :class:`NoteItem` is the element type stored inside
  :class:`controllers.note_list_store.NoteListStore` — a thin
  :class:`GObject.Object` wrapping a single frozen :class:`Note`. It
  carries no behaviour beyond exposing that note two ways: as scalar
  ``READABLE`` GObject properties for a list-row factory to bind
  (``note-id`` / ``title`` / ``snippet``), and as a plain Python
  :attr:`note` property returning the whole value for the in-memory
  filter / sort callbacks and for body reads (editor / rendered view).
* The wrapped :class:`Note` is **immutable and never mutated in place**.
  An edit produces a *new* :class:`NoteItem` that replaces the old one
  in the store via ``splice`` — never an in-place property change. This
  is load-bearing: ``Gio.ListModel::items-changed`` does not fire for a
  property mutation, so the filter / sort chain and the tag aggregator
  depend on every edit surfacing as a replace. Exposing the scalar
  fields as ``READABLE``-only (no setters) enforces that at the type
  level.
* Only the three fields a row factory binds are GObject properties;
  ``tags`` and ``modified_at`` are read through :attr:`note` in the
  filter / sort callbacks, so they need no property of their own.
* This module imports :mod:`gi` for :class:`GObject.Object` only — it
  is part of GLib, available headless, so the controller-layer unit
  tests run without a display. It does not import :mod:`Gtk`.
"""

from __future__ import annotations

from gi.repository import GObject

from models.note import Note


class NoteItem(GObject.Object):
    """GObject view of one immutable :class:`Note` for a ``Gio.ListModel``."""

    __gtype_name__ = "FolioNoteItem"

    _note: Note

    def __init__(self, note: Note) -> None:
        super().__init__()
        self._note = note

    @property
    def note(self) -> Note:
        """The full wrapped value — for filter / sort / body reads."""
        return self._note

    @GObject.Property(type=str, flags=GObject.ParamFlags.READABLE)
    def note_id(self) -> str:
        return self._note.id

    @GObject.Property(type=str, flags=GObject.ParamFlags.READABLE)
    def title(self) -> str:
        return self._note.title

    @GObject.Property(type=str, flags=GObject.ParamFlags.READABLE)
    def snippet(self) -> str:
        return self._note.snippet
