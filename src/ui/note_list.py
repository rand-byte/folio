"""The middle pane: a header + a sortable, filtered list of notes.

Principles & invariants
-----------------------
* :class:`NoteList` is the navigation pane between the sidebar and the
  rendered article. It mirrors :class:`AppState` by observing
  ``notify::selection``, ``notify::query``, and the local sort key.
  Selection and sort changes are discrete and trigger a synchronous
  :meth:`refresh`. Query changes arrive per keystroke (the search entry
  is bound to :attr:`AppState.query`), so the query handler is
  **throttled**: it schedules a single coalesced :meth:`refresh` per
  :data:`_QUERY_REFRESH_DEBOUNCE_MS` window rather than re-filtering on
  every character. The widget never caches a derived list that could
  drift out of sync with the notifications that drive it.
* The query refresh is a *throttle*, not a true debounce: it fires once
  per window measured from the first keystroke of a burst, and
  :meth:`refresh` re-reads the *current* :attr:`AppState.query` when the
  timer fires (rather than capturing a snapshot), so intermediate
  keystrokes are skipped and only the latest state renders. The pending
  GLib source is cancelled at teardown so a queued refresh cannot fire
  into a finalized widget. This mirrors the editor's autosave idiom
  (a module constant plus a pending-id field), not a new mechanism.
* Selection input flows in one direction: a click on a row → an
  ``app_state.set_selected_note_id`` mutation → an
  ``app_state`` ``notify::selected-note-id`` → the row is highlighted
  here and the rendered view reacts in :class:`NoteView`.
* The list-display computation is extracted as a free function,
  :func:`compute_display_notes`, so tests can call it without
  constructing GTK widgets at all. Tag-aware filtering happens in
  :mod:`search.note_filter` against the materialised note list; this
  widget only orchestrates the calls.
* Sort key is a *local* concern of this widget. Stored on the widget
  rather than on :class:`AppState` because the sort dropdown is only
  visible from the note list.
* The header reads ``"{N} notes"`` on the left and the sort dropdown
  on the right — no notebook-name lead-in, no filter chips.
* Each row carries an optional third line of dim ``#tag`` labels —
  rendered only when ``note.tags`` is non-empty. The chip row is a
  horizontal :class:`Gtk.Box` of :class:`Gtk.Label` widgets keyed off
  the ``.tag-chip-row`` CSS class for the dim opacity treatment.
* Date formatting is intentionally minimal — month abbreviation +
  day — and lives in the shared :mod:`ui._dates` helper
  (:func:`ui._dates.format_date_short`) so the note-list meta line and
  the rendered-view metadata line agree on it without cross-importing.
  Locale-aware formatting is a future polish item.
* GTK 4 currency: :meth:`Gtk.Box.append`, :meth:`Gtk.ListBox.append`,
  :class:`Gtk.DropDown`, ``row-activated``, ``notify::<prop>``
  observation, and :func:`GLib.timeout_add` / :func:`GLib.source_remove`
  for the query-refresh throttle.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import gi

gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import GLib, GObject, Gtk, Pango  # noqa: E402

from controllers.app_state import AppState
from enums import NoteSortKey
from models.note import Note
from ui._dates import format_date_short
from search.note_filter import (
    Selection,
    filter_by_query,
    filter_by_selection,
    sort_notes,
)
from storage.protocols import (
    AttachmentStoreProtocol,
    NoteRepositoryProtocol,
)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


type ClockFn = Callable[[], datetime]
"""Callable returning a timezone-aware ``datetime`` representing 'now'.

Kept for API parity with other panes; the tag migration removed the
``Recent`` smart filter so the clock is no longer used to compute the
displayed set. The constructor still accepts one for forward-compat
and to keep the construction signature stable.
"""


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

_QUERY_REFRESH_DEBOUNCE_MS: Final[int] = 150
"""Throttle window for the query-driven refresh.

The search entry is bound to :attr:`AppState.query` and notifies per
keystroke; coalescing the resulting :meth:`NoteList.refresh` over this
window keeps the expensive re-filter off the typing hot path. 150 ms
matches the delay the old ``Gtk.SearchEntry:search-changed`` debounce
carried before the binding moved the per-keystroke truth into
:class:`AppState`.
"""

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
"""Header text on the left — ``"N notes"`` regardless of selection.

Replaces the former selection-name lead-in: with a flat tag system the
note-list header no longer mirrors the sidebar choice, only its size.
"""


# ---------------------------------------------------------------------------
# Default factories
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def compute_display_notes(
    *,
    note_repository: NoteRepositoryProtocol,
    selection: Selection,
    query: str,
    sort_key: NoteSortKey,
) -> list[Note]:
    """Compute the filtered, sorted note list shown in the middle pane.

    Pure with respect to its inputs (except for the call to
    :meth:`note_repository.list_all`, which is the obvious storage
    edge). No notebook-graph knowledge — the tag migration moved
    grouping into :func:`filter_by_selection`.

    Procedure:

    * Read every note via :meth:`list_all`.
    * Apply :func:`filter_by_selection` (handles ``ALL``, ``UNTAGGED``,
      and tag-AND).
    * Apply :func:`filter_by_query` against the live query string.
    * Apply :func:`sort_notes` for the current sort key.
    """
    notes = filter_by_selection(note_repository.list_all(), selection)
    notes = filter_by_query(notes, query)
    return sort_notes(notes, sort_key)


# ---------------------------------------------------------------------------
# Row payload + row class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NoteRowPayload:
    """The note id this row stands in for."""

    note_id: str


class _NoteListRow(Gtk.ListBoxRow):
    """A note-list row carrying its note id as a typed payload."""

    payload: _NoteRowPayload

    def __init__(
        self,
        *,
        payload: _NoteRowPayload,
        child: Gtk.Widget,
    ) -> None:
        super().__init__()
        self.payload = payload
        self.set_child(child)


# ---------------------------------------------------------------------------
# NoteList
# ---------------------------------------------------------------------------


class NoteList(Gtk.Box):  # pylint: disable=too-many-instance-attributes
    """The middle navigation pane: header + scrolled list of notes.

    Layout (top to bottom):

    1. Header — ``"N notes"`` label + sort dropdown.
    2. A :class:`Gtk.ScrolledWindow` wrapping a :class:`Gtk.ListBox`
       of note rows.
    3. An empty-state label that becomes visible when the filtered
       list is empty.
    """

    _note_repository: NoteRepositoryProtocol
    _app_state: AppState
    _clock: ClockFn
    _attachment_store: AttachmentStoreProtocol | None

    _count_label: Gtk.Label
    _sort_dropdown: Gtk.DropDown
    _list_box: Gtk.ListBox
    _empty_label: Gtk.Label

    _sort_key: NoteSortKey
    _row_for_note_id: dict[str, _NoteListRow]
    _pending_refresh_id: int | None

    def __init__(
        self,
        *,
        note_repository: NoteRepositoryProtocol,
        app_state: AppState,
        clock: ClockFn = _default_clock,
        attachment_store: AttachmentStoreProtocol | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_repository = note_repository
        self._app_state = app_state
        self._clock = clock
        self._attachment_store = attachment_store
        self._sort_key = _DEFAULT_SORT_KEY
        self._row_for_note_id = {}
        self._pending_refresh_id = None

        header = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _HEADER_SPACING_PX)
        header.set_margin_start(_ROW_PADDING_PX)
        header.set_margin_end(_ROW_PADDING_PX)
        header.set_margin_top(_HEADER_SPACING_PX)
        header.set_margin_bottom(_HEADER_SPACING_PX)

        self._count_label = Gtk.Label.new(
            _NOTES_LABEL_TEMPLATE.format(n=0),
        )
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

        self.append(header)

        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)

        self._list_box = Gtk.ListBox.new()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.connect("row-activated", self._on_row_activated)
        scrolled.set_child(self._list_box)

        self.append(scrolled)

        self._empty_label = Gtk.Label.new("No notes here yet.")
        self._empty_label.set_margin_top(_ROW_PADDING_PX * 4)
        self._empty_label.set_margin_bottom(_ROW_PADDING_PX * 4)
        self._empty_label.set_visible(False)
        self.append(self._empty_label)

        self.set_size_request(_DEFAULT_PANE_WIDTH_PX, -1)

        self._app_state.connect(
            "notify::selection",
            self._on_app_state_selection_changed,
        )
        self._app_state.connect(
            "notify::query",
            self._on_app_state_query_changed,
        )
        self._app_state.connect(
            "notify::selected-note-id",
            self._on_app_state_selected_note_changed,
        )

        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Recompute and re-render the displayed list."""
        notes = compute_display_notes(
            note_repository=self._note_repository,
            selection=self._app_state.selection,
            query=self._app_state.query,
            sort_key=self._sort_key,
        )

        self._count_label.set_text(
            _NOTES_LABEL_TEMPLATE.format(n=len(notes)),
        )

        counts = self._attachment_counts(notes)

        self._list_box.remove_all()
        self._row_for_note_id = {}
        for note in notes:
            row = _make_note_row(note, counts[note.id])
            self._row_for_note_id[note.id] = row
            self._list_box.append(row)

        self._empty_label.set_visible(not notes)
        self._list_box.set_visible(bool(notes))

        self._apply_highlight()

    @property
    def sort_key(self) -> NoteSortKey:
        return self._sort_key

    def _attachment_counts(self, notes: Iterable[Note]) -> dict[str, int]:
        store = self._attachment_store
        if store is None:
            return {note.id: 0 for note in notes}
        return {note.id: store.count_for_note(note.id) for note in notes}

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_row_activated(
        self,
        _listbox: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        if not isinstance(row, _NoteListRow):
            return
        self._app_state.set_selected_note_id(row.payload.note_id)

    def _on_sort_changed(
        self,
        _dropdown: Gtk.DropDown,
        _pspec: object,
    ) -> None:
        index = self._sort_dropdown.get_selected()
        if 0 <= index < len(_SORT_KEY_DROPDOWN_ORDER):
            self._sort_key = _SORT_KEY_DROPDOWN_ORDER[index]
        self.refresh()

    def _on_app_state_selection_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self.refresh()

    def _on_app_state_query_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """Coalesce per-keystroke query notifications into one refresh.

        The search entry is bound to :attr:`AppState.query`, so this
        fires on every character. Scheduling a single pending source per
        burst (and re-reading the current query when it flushes) keeps
        the expensive re-filter off the typing hot path while always
        rendering the latest state. See the module throttle invariant.
        """
        if self._pending_refresh_id is not None:
            return  # a refresh is already scheduled for this burst
        self._pending_refresh_id = GLib.timeout_add(
            _QUERY_REFRESH_DEBOUNCE_MS,
            self._flush_pending_refresh,
        )

    def _on_app_state_selected_note_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._apply_highlight()

    def _flush_pending_refresh(self) -> bool:
        """GLib timer callback — run the coalesced query refresh.

        Clears the pending id first, then refreshes (which re-reads the
        *current* :attr:`AppState.query`), then returns
        :data:`GLib.SOURCE_REMOVE` so the one-shot source does not
        re-fire. Safe to call directly from tests, which drive the
        throttle without a running main loop.
        """
        self._pending_refresh_id = None
        self.refresh()
        result: bool = GLib.SOURCE_REMOVE
        return result

    def _cancel_pending_refresh(self) -> None:
        """Drop any scheduled refresh so it cannot fire after teardown.

        Idempotent and self-guarding: removes the GLib source only when
        one is pending, so the two teardown hooks (:meth:`do_unroot` and
        :meth:`__del__`) can both call it without double-removal.
        """
        if self._pending_refresh_id is not None:
            GLib.source_remove(self._pending_refresh_id)
            self._pending_refresh_id = None

    # ------------------------------------------------------------------
    # Highlight application
    # ------------------------------------------------------------------

    def _apply_highlight(self) -> None:
        note_id = self._app_state.selected_note_id
        if note_id is None:
            self._list_box.unselect_all()
            return
        row = self._row_for_note_id.get(note_id)
        if row is None:
            self._list_box.unselect_all()
            return
        self._list_box.select_row(row)

    # ------------------------------------------------------------------
    # Teardown — cancel the throttle source
    # ------------------------------------------------------------------

    def do_unroot(self) -> None:  # pylint: disable=arguments-differ
        """Cancel a pending refresh when leaving the widget tree.

        GTK invokes this synchronously while tearing the window's widget
        tree down, so it is the reliable hook for a *rooted* list. The
        :meth:`__del__` below is the companion net for a never-rooted
        instance (e.g. a standalone test widget).
        """
        self._cancel_pending_refresh()
        Gtk.Box.do_unroot(self)

    def __del__(self) -> None:
        """Cancel a pending refresh for a list finalized un-rooted.

        :meth:`do_unroot` only fires for a list that was added to a
        window; one built in isolation and dropped (as the unit tests
        do) is finalized without ever being rooted, so the cancel has to
        happen here. The :meth:`_cancel_pending_refresh` guard makes this
        a no-op when :meth:`do_unroot` already ran.
        """
        self._cancel_pending_refresh()


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def _make_note_row(note: Note, attachment_count: int) -> _NoteListRow:
    """Build a single note-list row.

    Layout (top to bottom):

    * Title — bold, single line, end-ellipsised (``.note-title``).
    * Snippet — up to two wrapped lines, dimmed (``.note-snippet``);
      omitted when the note has none.
    * Chip row — ``#tag`` labels (``.tag-chip-row``), one per entry in
      ``note.tags``; omitted entirely when ``note.tags`` is empty.
    * Meta line — right-aligned ``📎 N | <date>``.
    """
    box = Gtk.Box.new(Gtk.Orientation.VERTICAL, _ROW_SPACING_PX)
    box.set_margin_start(_ROW_PADDING_PX)
    box.set_margin_end(_ROW_PADDING_PX)
    box.set_margin_top(_ROW_PADDING_PX)
    box.set_margin_bottom(_ROW_PADDING_PX)

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

    return _NoteListRow(
        payload=_NoteRowPayload(note_id=note.id),
        child=box,
    )


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
