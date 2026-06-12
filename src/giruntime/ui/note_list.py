"""The middle pane: a header + a sortable, filtered list of notes.

Principles & invariants
-----------------------
* :class:`NoteList` is the navigation pane between the sidebar and the
  rendered article. It binds a :class:`Gtk.ListView` to a model chain
  layered over the in-memory
  :class:`controllers.note_list_store.NoteListStore`::

      store (NoteItem)
        -> Gtk.FilterListModel(custom selection+query filter)
        -> Gtk.SortListModel(custom sort-key sorter)
        -> Gtk.SingleSelection
        -> Gtk.ListView(SignalListItemFactory)

  The list never re-reads the database and never materialises its own
  Python list: selection, query, and sort changes invalidate the
  ``Gtk.CustomFilter`` / ``Gtk.CustomSorter``, and the model chain
  recomputes incrementally. Create / edit / delete in the store flow in
  automatically because the chain observes the store's
  ``items-changed``.
* The filter and sorter reuse the per-item predicates in
  :mod:`search.note_filter` (:func:`matches_selection`,
  :func:`matches_query`, :func:`comparator_for`) so the "what shows" /
  "what order" rules live in exactly one place, shared with the legacy
  list API. The query needle is normalised once per query change via
  :func:`normalize_query`, then the filter is invalidated; re-filtering
  the resident list per keystroke is cheap, so no throttle is needed.
* Selection is one source of truth: :class:`AppState`. A row click moves
  the :class:`Gtk.SingleSelection`, whose ``notify::selected`` writes
  through to ``app_state.set_selected_note_id``; a programmatic
  selection change (e.g. the controller selecting a freshly created
  note) arrives as ``notify::selected-note-id`` and is mirrored back
  onto the ``SingleSelection``. A re-entrancy guard breaks the echo.
* The header reads ``"{N} notes"`` on the left — the count of the
  *filtered* model, kept live by observing the sorted model's
  ``items-changed`` — and the sort dropdown on the right.
* Each row shows title (bold, ellipsised), an optional snippet (two
  wrapped lines), an optional ``#tag`` chip row (only when
  ``note.tags`` is non-empty), and a right-aligned ``📎 N | <date>``
  meta line. The 📎 count is read per-bind from the attachment store, so
  it tracks edits (an edit replaces the row → re-bind). Attachment
  add/remove never touches the note source, so no ``items-changed``
  re-bind would fire for it; the list instead subscribes to the
  controller's narrow ``attachments-changed`` signal and re-populates
  the affected note's *bound* row (tracked via the factory's
  ``bind``/``unbind`` pair) so the badge recomputes via
  ``count_for_note`` without lying to the model chain.
* Date formatting lives in the shared :mod:`ui._dates` helper
  (:func:`ui._dates.format_date_short`) so the note-list meta line and
  the rendered-view metadata line agree without cross-importing.
* GTK 4 currency: :class:`Gtk.ListView`, :class:`Gtk.SignalListItemFactory`,
  :class:`Gtk.FilterListModel`, :class:`Gtk.SortListModel`,
  :class:`Gtk.SingleSelection`, :class:`Gtk.CustomFilter`,
  :class:`Gtk.CustomSorter`, and ``notify::<prop>`` observation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from gi.repository import GObject, Gtk, Pango

from enums import NoteSortKey
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_item import NoteItem
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui._dates import format_date_short
from models.note import Note
from search.note_filter import (
    comparator_for,
    matches_query,
    matches_selection,
    normalize_query,
)
from storage.protocols import AttachmentStoreProtocol


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_SORT_KEY_LABELS: Final[dict[NoteSortKey, str]] = {
    NoteSortKey.MODIFIED: "Modified",
    NoteSortKey.CREATED: "Created",
    NoteSortKey.TITLE: "Title",
}
"""Visible labels in the sort dropdown."""

_SORT_KEY_DROPDOWN_ORDER: Final[tuple[NoteSortKey, ...]] = (
    NoteSortKey.MODIFIED,
    NoteSortKey.CREATED,
    NoteSortKey.TITLE,
)

_DEFAULT_SORT_KEY: Final[NoteSortKey] = NoteSortKey.MODIFIED

_HEADER_SPACING_PX: Final[int] = 6
_ROW_SPACING_PX: Final[int] = 4
_ROW_PADDING_PX: Final[int] = 8
_META_SPACING_PX: Final[int] = 4
_CHIP_SPACING_PX: Final[int] = 6
"""Horizontal gap between adjacent #tag labels on the third row."""
_DEFAULT_PANE_WIDTH_PX: Final[int] = 320

_TITLE_CSS_CLASS: Final[str] = "note-title"
_SNIPPET_CSS_CLASS: Final[str] = "note-snippet"
_META_CSS_CLASS: Final[str] = "note-meta"
_META_SEPARATOR_CSS_CLASS: Final[str] = "note-meta-separator"
_CHIP_CSS_CLASS: Final[str] = "tag-chip-row"

_PAPERCLIP: Final[str] = "\U0001f4ce"
_META_SEPARATOR: Final[str] = "|"

_NOTES_LABEL_TEMPLATE: Final[str] = "{n} notes"
"""Header text on the left — ``"N notes"`` (count of the filtered set)."""


# ---------------------------------------------------------------------------
# NoteList
# ---------------------------------------------------------------------------


class NoteList(Gtk.Box):  # pylint: disable=too-many-instance-attributes
    """The middle navigation pane: header + scrolled ListView of notes."""

    _app_state: AppState
    _attachment_store: AttachmentStoreProtocol | None

    _bound_rows: dict[str, tuple[Gtk.Box, Note]]
    """The factory's currently-bound rows, keyed by note id.

    Maintained by the ``bind``/``unbind`` factory handlers so the
    ``attachments-changed`` handler can re-populate exactly the
    affected row's box (GTK exposes no "re-bind one item" API and
    emitting a synthetic ``items-changed`` on the store would lie to
    every other observer). A note id is present iff its row is
    currently realised by the ``ListView``.
    """

    _count_label: Gtk.Label
    _sort_dropdown: Gtk.DropDown
    _list_view: Gtk.ListView
    _empty_label: Gtk.Label

    _filter: Gtk.CustomFilter
    _sorter: Gtk.CustomSorter
    _filter_model: Gtk.FilterListModel
    _sort_model: Gtk.SortListModel
    _selection_model: Gtk.SingleSelection

    _sort_key: NoteSortKey
    _comparator: Callable[[Note, Note], int]
    _needle: str
    _syncing_selection: bool

    def __init__(
        self,
        *,
        note_store: NoteListStore,
        note_controller: NoteController,
        app_state: AppState,
        attachment_store: AttachmentStoreProtocol | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._app_state = app_state
        self._attachment_store = attachment_store
        self._bound_rows = {}
        self._sort_key = _DEFAULT_SORT_KEY
        self._comparator = comparator_for(self._sort_key)
        self._needle = normalize_query(app_state.query)
        self._syncing_selection = False

        self.append(self._build_header())
        self._build_model_chain(note_store)
        self.append(self._build_list_view())

        self._empty_label = Gtk.Label.new("No notes here yet.")
        self._empty_label.set_margin_top(_ROW_PADDING_PX * 4)
        self._empty_label.set_margin_bottom(_ROW_PADDING_PX * 4)
        self._empty_label.set_visible(False)
        self.append(self._empty_label)

        self.set_size_request(_DEFAULT_PANE_WIDTH_PX, -1)

        self._app_state.connect(
            "notify::selection", self._on_app_state_selection_changed,
        )
        self._app_state.connect(
            "notify::query", self._on_app_state_query_changed,
        )
        self._app_state.connect(
            "notify::selected-note-id",
            self._on_app_state_selected_note_changed,
        )
        note_controller.connect(
            "attachments-changed",
            self._on_attachments_changed,
        )

        self._update_count()
        self._sync_selection_from_app_state()

    def _build_header(self) -> Gtk.Box:
        """Build the header row: filtered count on the left, sort on right."""
        header = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _HEADER_SPACING_PX)
        header.set_margin_start(_ROW_PADDING_PX)
        header.set_margin_end(_ROW_PADDING_PX)
        header.set_margin_top(_HEADER_SPACING_PX)
        header.set_margin_bottom(_HEADER_SPACING_PX)

        self._count_label = Gtk.Label.new(_NOTES_LABEL_TEMPLATE.format(n=0))
        self._count_label.set_halign(Gtk.Align.START)
        self._count_label.set_hexpand(True)
        header.append(self._count_label)

        self._sort_dropdown = Gtk.DropDown.new_from_strings(
            [_SORT_KEY_LABELS[key] for key in _SORT_KEY_DROPDOWN_ORDER]
        )
        self._sort_dropdown.set_selected(
            _SORT_KEY_DROPDOWN_ORDER.index(_DEFAULT_SORT_KEY)
        )
        self._sort_dropdown.connect("notify::selected", self._on_sort_changed)
        header.append(self._sort_dropdown)
        return header

    def _build_model_chain(self, note_store: NoteListStore) -> None:
        """Layer Filter → Sort → SingleSelection over the note store."""
        self._filter = Gtk.CustomFilter.new(self._match)
        self._sorter = Gtk.CustomSorter.new(self._compare)
        self._filter_model = Gtk.FilterListModel.new(note_store, self._filter)
        self._sort_model = Gtk.SortListModel.new(
            self._filter_model, self._sorter,
        )
        self._selection_model = Gtk.SingleSelection.new(self._sort_model)
        self._selection_model.set_autoselect(False)
        self._selection_model.set_can_unselect(True)
        self._selection_model.connect(
            "notify::selected", self._on_selection_notify,
        )
        # The filtered/sorted count drives the header and a re-sync of
        # the highlight (positions shift when the set changes).
        self._sort_model.connect("items-changed", self._on_model_items_changed)

    def _build_list_view(self) -> Gtk.ScrolledWindow:
        """Build the scrolled ``ListView`` bound to the selection model."""
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        factory.connect("unbind", self._on_factory_unbind)

        self._list_view = Gtk.ListView.new(self._selection_model, factory)
        self._list_view.add_css_class("note-list-view")

        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_child(self._list_view)
        return scrolled

    # ------------------------------------------------------------------
    # Filter / sorter callbacks
    # ------------------------------------------------------------------

    def _match(
        self,
        item: GObject.Object,
        _user_data: object = None,
    ) -> bool:
        if not isinstance(item, NoteItem):
            return False
        note = item.note
        return (
            matches_selection(note, self._app_state.selection)
            and matches_query(note, self._needle)
        )

    def _compare(
        self,
        left: GObject.Object,
        right: GObject.Object,
        _user_data: object = None,
    ) -> int:
        if not (isinstance(left, NoteItem) and isinstance(right, NoteItem)):
            return 0
        return self._comparator(left.note, right.note)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    def _on_factory_setup(
        self,
        _factory: Gtk.SignalListItemFactory,
        list_item: Gtk.ListItem,
    ) -> None:
        box = Gtk.Box.new(Gtk.Orientation.VERTICAL, _ROW_SPACING_PX)
        box.set_margin_start(_ROW_PADDING_PX)
        box.set_margin_end(_ROW_PADDING_PX)
        box.set_margin_top(_ROW_PADDING_PX)
        box.set_margin_bottom(_ROW_PADDING_PX)
        list_item.set_child(box)

    def _on_factory_bind(
        self,
        _factory: Gtk.SignalListItemFactory,
        list_item: Gtk.ListItem,
    ) -> None:
        box = list_item.get_child()
        item = list_item.get_item()
        if not isinstance(box, Gtk.Box) or not isinstance(item, NoteItem):
            return
        self._bound_rows[item.note.id] = (box, item.note)
        _clear_box(box)
        _populate_row_box(box, item.note, self._attachment_count(item.note.id))

    def _on_factory_unbind(
        self,
        _factory: Gtk.SignalListItemFactory,
        list_item: Gtk.ListItem,
    ) -> None:
        """Forget the row mapping when the ``ListView`` recycles it.

        ``unbind`` still sees the item that was bound, so the note id
        can be popped directly; GTK always unbinds before re-binding a
        recycled list item, so the mapping cannot go stale.
        """
        item = list_item.get_item()
        if isinstance(item, NoteItem):
            self._bound_rows.pop(item.note.id, None)

    def _attachment_count(self, note_id: str) -> int:
        if self._attachment_store is None:
            return 0
        return self._attachment_store.count_for_note(note_id)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_sort_changed(
        self,
        _dropdown: Gtk.DropDown,
        _pspec: object,
    ) -> None:
        index = self._sort_dropdown.get_selected()
        if 0 <= index < len(_SORT_KEY_DROPDOWN_ORDER):
            self._sort_key = _SORT_KEY_DROPDOWN_ORDER[index]
            self._comparator = comparator_for(self._sort_key)
            self._sorter.changed(Gtk.SorterChange.DIFFERENT)

    def _on_app_state_selection_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._filter.changed(Gtk.FilterChange.DIFFERENT)

    def _on_app_state_query_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """Re-normalise the needle and invalidate the filter.

        In-memory re-filtering is cheap, so the per-keystroke query
        notification invalidates the filter directly — no throttle.
        """
        self._needle = normalize_query(self._app_state.query)
        self._filter.changed(Gtk.FilterChange.DIFFERENT)

    def _on_app_state_selected_note_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._sync_selection_from_app_state()

    def _on_model_items_changed(
        self,
        _model: Gtk.SortListModel,
        _position: int,
        _removed: int,
        _added: int,
    ) -> None:
        self._update_count()
        # Positions shifted; keep the highlight on the app-state note.
        self._sync_selection_from_app_state()

    def _on_attachments_changed(
        self,
        _controller: NoteController,
        note_id: str,
    ) -> None:
        """Recompute the 📎 badge of the affected note's bound row.

        Attachment mutations leave the note source untouched, so the
        model chain emits no ``items-changed`` and no factory re-bind
        happens. Re-populating the bound box directly recomputes the
        badge through :meth:`_attachment_count` (``count_for_note``).
        A note whose row is not currently realised needs nothing — its
        next ``bind`` reads the fresh count anyway.
        """
        entry = self._bound_rows.get(note_id)
        if entry is None:
            return
        box, note = entry
        _clear_box(box)
        _populate_row_box(box, note, self._attachment_count(note.id))

    def _on_selection_notify(
        self,
        _selection: Gtk.SingleSelection,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """Write a user-driven row click through to :class:`AppState`."""
        if self._syncing_selection:
            return
        item = self._selection_model.get_selected_item()
        if isinstance(item, NoteItem):
            self._app_state.set_selected_note_id(item.note.id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_count(self) -> None:
        count = self._sort_model.get_n_items()
        self._count_label.set_text(_NOTES_LABEL_TEMPLATE.format(n=count))
        self._empty_label.set_visible(count == 0)
        self._list_view.set_visible(count > 0)

    def _sync_selection_from_app_state(self) -> None:
        """Mirror :attr:`AppState.selected_note_id` onto the model.

        Guarded against the ``notify::selected`` echo so the round-trip
        click → app-state → here does not loop.
        """
        note_id = self._app_state.selected_note_id
        target = Gtk.INVALID_LIST_POSITION
        if note_id is not None:
            for position in range(self._sort_model.get_n_items()):
                candidate = self._sort_model.get_item(position)
                if isinstance(candidate, NoteItem) and candidate.note.id == note_id:
                    target = position
                    break
        self._syncing_selection = True
        try:
            self._selection_model.set_selected(target)
        finally:
            self._syncing_selection = False

    @property
    def sort_key(self) -> NoteSortKey:
        return self._sort_key


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def _clear_box(box: Gtk.Box) -> None:
    """Remove every child of ``box`` (GTK 4 has no ``remove_all`` on Box)."""
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _populate_row_box(box: Gtk.Box, note: Note, attachment_count: int) -> None:
    """Fill ``box`` with the title / snippet / chips / meta of ``note``.

    Layout (top to bottom):

    * Title — bold, single line, end-ellipsised (``.note-title``).
    * Snippet — up to two wrapped lines, dimmed (``.note-snippet``);
      omitted when the note has none.
    * Chip row — ``#tag`` labels (``.tag-chip-row``); omitted entirely
      when ``note.tags`` is empty.
    * Meta line — right-aligned ``📎 N | <date>``.
    """
    title_label = Gtk.Label.new(note.title)
    title_label.set_halign(Gtk.Align.START)
    title_label.set_hexpand(True)
    title_label.set_ellipsize(Pango.EllipsizeMode.END)
    title_label.add_css_class(_TITLE_CSS_CLASS)
    box.append(title_label)

    if note.snippet:
        snippet_label = Gtk.Label.new(note.snippet)
        snippet_label.set_halign(Gtk.Align.START)
        snippet_label.set_xalign(0.0)
        snippet_label.set_hexpand(True)
        snippet_label.set_wrap(True)
        snippet_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        snippet_label.set_lines(2)
        snippet_label.set_ellipsize(Pango.EllipsizeMode.END)
        snippet_label.add_css_class(_SNIPPET_CSS_CLASS)
        box.append(snippet_label)

    if note.tags:
        box.append(_make_chip_row(note.tags))

    box.append(_make_meta_line(note, attachment_count))


def _make_chip_row(tags: tuple[str, ...]) -> Gtk.Box:
    """Build the third-line row of ``#tag`` chip labels."""
    chip_row = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _CHIP_SPACING_PX)
    chip_row.set_halign(Gtk.Align.START)
    for tag in tags:
        label = Gtk.Label.new(f"#{tag}")
        label.set_halign(Gtk.Align.START)
        label.add_css_class(_CHIP_CSS_CLASS)
        chip_row.append(label)
    return chip_row


def _make_meta_line(note: Note, attachment_count: int) -> Gtk.Box:
    """Build the right-aligned ``📎 N | <date>`` meta line for a row."""
    meta = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _META_SPACING_PX)
    meta.set_halign(Gtk.Align.END)

    if attachment_count > 0:
        clip_label = Gtk.Label.new(f"{_PAPERCLIP} {attachment_count}")
        clip_label.add_css_class(_META_CSS_CLASS)
        meta.append(clip_label)

        separator_label = Gtk.Label.new(_META_SEPARATOR)
        separator_label.add_css_class(_META_CSS_CLASS)
        separator_label.add_css_class(_META_SEPARATOR_CSS_CLASS)
        meta.append(separator_label)

    date_label = Gtk.Label.new(format_date_short(note.modified_at))
    date_label.add_css_class(_META_CSS_CLASS)
    meta.append(date_label)

    return meta
