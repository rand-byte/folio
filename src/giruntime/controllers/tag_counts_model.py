"""A derived :class:`Gio.ListModel` of tag counts over the note store.

Principles & invariants
-----------------------
* :class:`TagCountsModel` is a *derived* ``Gio.ListModel`` that wraps a
  source note model (the :class:`controllers.note_list_store.NoteListStore`)
  and exposes one :class:`TagItem` per distinct tag currently in use,
  each carrying a live ``count``. The sidebar binds a ``ListView`` to a
  ``SortListModel`` over it; this model itself imposes no order (the
  alphabetical sort is the sorter's job).
* It reads **only** ``tags``, which is resident on every note, so the
  body-in-memory pivot does not affect it. It never touches the
  database.
* Because ``items-changed(pos, removed, added)`` does **not** carry the
  *removed* items, the model keeps a :attr:`_shadow` of each source
  row's tag set, parallel to the source positions. On a source change it
  decrements the shadow slice being removed, drops it, then inserts the
  added rows' tag sets and increments. A create / edit / delete in the
  store therefore reduces to "subtract the old tag set, add the new one".
* Count bookkeeping is incremental: :meth:`_inc` appends a
  :class:`TagItem` on a ``0 -> 1`` transition (an ``items-changed``
  insertion); :meth:`_dec` removes it on ``1 -> 0`` (an
  ``items-changed`` removal); any other change is a count-only
  ``notify::count`` on the existing row with no list churn.
* :class:`TagItem` exposes ``name`` (``READABLE``) so a
  ``StringSorter`` / ``PropertyExpression`` can order rows, and
  ``count`` (read/write) so the factory binds it and a count-only update
  notifies without rebuilding the row.
* This module imports :mod:`gi` for ``Gio`` / ``GObject`` only (both
  GLib, headless-safe) and never imports :mod:`Gtk`.
"""

from __future__ import annotations

from gi.repository import Gio, GObject

from giruntime.controllers.note_item import NoteItem


class TagItem(GObject.Object):
    """A single tag row: an immutable ``name`` and a live ``count``."""

    __gtype_name__ = "FolioTagItem"

    _name: str
    count: int = GObject.Property(type=int, default=0)

    def __init__(self, *, name: str, count: int) -> None:
        super().__init__()
        self._name = name
        self.count = count

    @GObject.Property(type=str, flags=GObject.ParamFlags.READABLE)
    def name(self) -> str:
        return self._name


class TagCountsModel(GObject.Object, Gio.ListModel):
    """Derived, incrementally-maintained list of :class:`TagItem`."""

    __gtype_name__ = "FolioTagCountsModel"

    _source: Gio.ListModel
    _shadow: list[frozenset[str]]
    _counts: dict[str, int]
    _rows: dict[str, TagItem]
    _order: list[str]

    def __init__(self, source: Gio.ListModel) -> None:
        super().__init__()
        self._source = source
        self._shadow = []
        self._counts = {}
        self._rows = {}
        self._order = []
        self._source.connect("items-changed", self._on_source_items_changed)
        # Seed from whatever the source already holds (the store is
        # populated by ``load()`` before this model is constructed): a
        # full-length "added" run starting at position 0.
        self._on_source_items_changed(self._source, 0, 0, self.get_n_items_of_source())

    def get_n_items_of_source(self) -> int:
        """Current item count of the wrapped source model."""
        return int(self._source.get_n_items())

    # ------------------------------------------------------------------
    # Gio.ListModel interface
    # ------------------------------------------------------------------

    def do_get_item_type(self) -> GObject.GType:
        # ``__gtype__`` is injected by the GObject metaclass; pylint
        # cannot see it statically.
        return TagItem.__gtype__  # pylint: disable=no-member

    def do_get_n_items(self) -> int:
        return len(self._order)

    def do_get_item(self, position: int) -> GObject.Object | None:
        if 0 <= position < len(self._order):
            return self._rows[self._order[position]]
        return None

    # ------------------------------------------------------------------
    # Source subscription
    # ------------------------------------------------------------------

    def _on_source_items_changed(
        self,
        source: Gio.ListModel,
        position: int,
        removed: int,
        added: int,
    ) -> None:
        """Re-aggregate the tag counts for one source ``items-changed``.

        Subtract the tag sets of the removed slice (read from the
        shadow, since the source no longer holds them), then add the tag
        sets of the newly-present rows (read live from the source).
        """
        old_slice = self._shadow[position:position + removed]
        for tag_set in old_slice:
            for tag in tag_set:
                self._dec(tag)

        new_sets: list[frozenset[str]] = []
        for offset in range(added):
            item = source.get_item(position + offset)
            if not isinstance(item, NoteItem):
                new_sets.append(frozenset())
                continue
            tag_set = frozenset(item.note.tags)
            new_sets.append(tag_set)
            for tag in tag_set:
                self._inc(tag)

        self._shadow[position:position + removed] = new_sets

    # ------------------------------------------------------------------
    # Count bookkeeping
    # ------------------------------------------------------------------

    def _inc(self, tag: str) -> None:
        new_count = self._counts.get(tag, 0) + 1
        self._counts[tag] = new_count
        if new_count == 1:
            item = TagItem(name=tag, count=1)
            self._rows[tag] = item
            self._order.append(tag)
            self.items_changed(len(self._order) - 1, 0, 1)
        else:
            self._rows[tag].count = new_count

    def _dec(self, tag: str) -> None:
        new_count = self._counts[tag] - 1
        if new_count == 0:
            pos = self._order.index(tag)
            del self._order[pos]
            del self._rows[tag]
            del self._counts[tag]
            self.items_changed(pos, 1, 0)
        else:
            self._counts[tag] = new_count
            self._rows[tag].count = new_count
