"""The middle pane: a header + a sortable, filtered list of notes.

Principles & invariants
-----------------------
* :class:`NoteList` is the navigation pane between the sidebar and
  the rendered article. It mirrors :class:`AppState`: every change
  to :attr:`AppState.selection`, :attr:`AppState.query`, or the
  local sort key triggers a full :meth:`refresh`. The widget never
  caches a derived list that could drift out of sync with the
  signals that drive it.
* Selection input flows in one direction: a click on a row → an
  ``app_state.set_selected_note_id`` mutation → an
  ``app_state.selected-note-changed`` signal → the row is highlighted
  here and the rendered view reacts in :class:`NoteView`. We use
  ``row-activated`` for input (which only fires for *user* activation,
  not programmatic ``select_row`` calls), so the highlight update we
  make in the signal handler cannot loop back into a second selection.
* The list-display computation is extracted as a free function,
  :func:`compute_display_notes`, so tests can call it without
  constructing GTK widgets at all. The function takes both
  repositories and the runtime knobs (selection, query, sort,
  clock) by keyword; nothing is read from a global. This is the
  only place that knows how to expand a notebook selection to
  include its children — a piece of logic the
  :mod:`search.note_filter` layer deliberately keeps
  itself free of, per its module docstring.
* Sort key is a *local* concern of this widget (the design's
  ``[sort, setSort]`` lives in ``notelist.jsx``, not in shared
  state). Stored on the widget rather than on :class:`AppState`
  because the sort dropdown is only visible from the note list and
  no other pane needs to react to it. If this ever changes — e.g. a
  status bar that displays the active sort — we promote it to
  :class:`AppState` then; today it is widget-local.
* Hierarchy expansion for notebook selection (the design's
  *Recipes → Baking + Weeknight dinners*) happens at the boundary
  between this widget and the storage layer. When the selection is
  a :class:`NotebookSelection`, we read every notebook id in the
  selected subtree (parent + direct children, max depth 1 under the
  two-level rule) and concatenate the per-notebook lists. For a
  :class:`SmartSelection`, we read :meth:`list_all` once and let
  :func:`filter_by_selection` do the date predicate.
* CRUD on notes (Open / Duplicate / Delete from the design's
  context menu) is **not delivered in step 9**. Delete needs the
  confirm dialog (step 12) and Duplicate routes through
  :class:`NoteController`, which in turn needs
  :class:`AttachmentStoreProtocol` (step 11). The widget surface
  in step 9 is read-only navigation; the right-click menu lands
  alongside :mod:`ui.dialogs`.
* Date formatting is intentionally minimal — month abbreviation +
  day. The design's ``formatDateShort`` does the same. Locale-aware
  formatting is a future polish item; today the goal is to keep
  the row dense and readable in the column widths the design uses.
* GTK 4 currency: :meth:`Gtk.Box.append`, :meth:`Gtk.ListBox.append`,
  :class:`Gtk.DropDown` (not the deprecated :class:`Gtk.ComboBoxText`),
  ``row-activated`` (not the deprecated ``row-selected``-as-input
  pattern). No deprecated GTK 4.18 calls.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk, Pango  # noqa: E402

from controllers.app_state import AppState
from enums import NoteSortKey, SmartFilter
from models.note import Note
from models.notebook import Notebook
from search.note_filter import (
    NotebookSelection,
    Selection,
    SmartSelection,
    filter_by_query,
    filter_by_selection,
    sort_notes,
)
from storage.protocols import (
    AttachmentStoreProtocol,
    NoteRepositoryProtocol,
    NotebookRepositoryProtocol,
)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


type ClockFn = Callable[[], datetime]
"""Callable returning a timezone-aware ``datetime`` representing 'now'.

Used by the ``Recent`` smart-filter so the displayed list and the
underlying filter agree on what "now" means. Injected so tests can
pin it.
"""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_SMART_FILTER_LABELS: Final[dict[SmartFilter, str]] = {
    SmartFilter.ALL: "All notes",
    SmartFilter.RECENT: "Recent",
}
"""Header titles for each smart-filter selection.

The same strings appear in :mod:`ui.sidebar`. The two
copies are kept in sync deliberately rather than abstracted to a
shared module: the strings are short, the duplication is two lines,
and a "ui label registry" module would be more friction than the
duplication it avoids.
"""

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
"""Order the sort options appear in the dropdown.

``MODIFIED`` is first because it is also the default — newest-first
ordering is what the design's note list shows on launch. ``TITLE``
is last because it is the only ascending-by-text sort, and grouping
the two date sorts together reads as a clearer affordance.
"""

_DEFAULT_SORT_KEY: Final[NoteSortKey] = NoteSortKey.MODIFIED
"""Initial sort applied to a freshly constructed :class:`NoteList`."""

_HEADER_SPACING_PX: Final[int] = 6
_ROW_SPACING_PX: Final[int] = 4
_ROW_PADDING_PX: Final[int] = 8
_META_SPACING_PX: Final[int] = 4
"""Horizontal gap between the meta line's paperclip, separator, and date."""
_DEFAULT_PANE_WIDTH_PX: Final[int] = 320
"""Initial width hint for the note-list pane in :class:`MainWindow`."""

_TITLE_CSS_CLASS: Final[str] = "note-title"
"""CSS class bolding the row title (font weight only — palette-safe)."""

_SNIPPET_CSS_CLASS: Final[str] = "note-snippet"
"""CSS class dimming the two-line row snippet (opacity only)."""

_META_CSS_CLASS: Final[str] = "note-meta"
"""CSS class dimming the meta line (count + separator + date), opacity only."""

_META_SEPARATOR_CSS_CLASS: Final[str] = "note-meta-separator"
"""Extra CSS class for the meta separator's own low-opacity treatment."""

_PAPERCLIP: Final[str] = "\U0001f4ce"
"""Paperclip glyph (📎) prefixing the attachment count when positive."""

_META_SEPARATOR: Final[str] = "|"
"""Literal separator drawn between the attachment count and the date."""

_MONTH_ABBREVIATIONS: Final[tuple[str, ...]] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
"""ASCII month names used by :func:`format_date_short`.

Hard-coded rather than computed via ``datetime.strftime("%b")`` so
the format is locale-independent — a French user's note list still
reads ``Apr 14`` rather than the French abbreviation. Locale-aware
formatting is a future polish task.
"""

_SELECTION_TITLE_FALLBACK: Final[str] = ""
"""Title shown when the current selection refers to something that
no longer exists (e.g. a notebook deleted out from under the
sidebar). The note list would simultaneously reduce to zero rows;
the fallback is just the empty string so the header doesn't display
a stale name."""


# ---------------------------------------------------------------------------
# Default factories
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """Production clock — UTC, full resolution preserved."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def format_date_short(value: datetime) -> str:
    """Return a short, locale-independent date string like ``Apr 14``.

    Mirrors ``formatDateShort`` from ``notelist.jsx`` in the design
    reference. The current year is omitted; if a future iteration
    wants ``Apr 14, 2025`` for older notes it can compose this value
    with the year by reading :attr:`datetime.year` itself.
    """
    return f"{_MONTH_ABBREVIATIONS[value.month - 1]} {value.day}"


def title_for_selection(
    selection: Selection,
    notebook_repository: NotebookRepositoryProtocol,
) -> str:
    """Return the user-facing header title for ``selection``.

    For a :class:`SmartSelection`, the title is the literal label
    from :data:`_SMART_FILTER_LABELS`. For a
    :class:`NotebookSelection`, the title is the notebook's name
    looked up via the protocol; a :class:`KeyError` (stale id) is
    swallowed and the empty-string fallback is returned, since the
    list of notes will simultaneously reduce to zero rows and the
    header is not the right place to surface the staleness.
    """
    match selection:
        case SmartSelection(smart_filter=sf):
            return _SMART_FILTER_LABELS[sf]
        case NotebookSelection(notebook_id=nb_id):
            try:
                return notebook_repository.get(nb_id).name
            except KeyError:
                return _SELECTION_TITLE_FALLBACK


def expand_notebook_selection_to_ids(
    notebook_id: str,
    all_notebooks: list[Notebook],
) -> list[str]:
    """Return the selected notebook id plus its direct children.

    Per the strict two-level rule in the plan, recursion is at most
    one level deep — children of children do not exist. The
    implementation is a single comprehension over the materialised
    notebook list for clarity over the (premature) optimisation of
    keeping a parent→children index in memory.
    """
    return [
        notebook_id,
        *(nb.id for nb in all_notebooks if nb.parent_id == notebook_id),
    ]


def compute_display_notes(  # pylint: disable=too-many-arguments
    *,
    note_repository: NoteRepositoryProtocol,
    notebook_repository: NotebookRepositoryProtocol,
    selection: Selection,
    query: str,
    sort_key: NoteSortKey,
    now: datetime,
) -> list[Note]:
    """Compute the filtered, sorted note list shown in the middle pane.

    Pure with respect to its inputs: every value the function reads
    is either a parameter or returned by a method on a repository
    parameter. No global clock, no module-level state. Tests pass
    fakes for both repositories and assert directly on the returned
    list.

    Procedure:

    * If the selection is a :class:`NotebookSelection`, read every
      note in the selected notebook *and its children*. The
      hierarchy expansion lives here, deliberately, rather than in
      :func:`filter_by_selection` (see that module's docstring on
      why it stays free of notebook-graph knowledge).
    * If the selection is a :class:`SmartSelection`, read
      :meth:`list_all` once and let :func:`filter_by_selection`
      apply the date predicate for ``RECENT`` (``ALL`` is a
      passthrough).
    * Apply :func:`filter_by_query` against the live query string —
      empty / whitespace-only is a passthrough.
    * Apply :func:`sort_notes` for the current sort key.
    """
    match selection:
        case NotebookSelection(notebook_id=nb_id):
            all_notebooks = notebook_repository.list_all()
            ids = expand_notebook_selection_to_ids(nb_id, all_notebooks)
            notes: list[Note] = []
            for sub_id in ids:
                notes.extend(note_repository.list_by_notebook(sub_id))
        case SmartSelection() as smart:
            notes = filter_by_selection(
                note_repository.list_all(),
                smart,
                now=now,
            )
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
    """A note-list row carrying its note id as a typed payload.

    The payload is what :class:`NoteList`'s ``row-activated``
    handler reads to decide which note to select. Subclassing
    :class:`Gtk.ListBoxRow` lets us attach the payload as a Python
    attribute without using the deprecated ``Gtk.Widget.set_data``
    API.
    """

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

    1. Header row — selection title + count badge + sort dropdown.
    2. A :class:`Gtk.ScrolledWindow` wrapping a :class:`Gtk.ListBox`
       of note rows.
    3. An empty-state label that becomes visible when the filtered
       list is empty.

    The instance-attribute count is above pylint's default of 7
    because the widget needs handles to every header subwidget
    (title, count, sort dropdown), the scrolling list-box, the
    empty-state label, the three injected dependencies (repositories,
    app state, clock), the local sort key, and the note-id-to-row
    index used to apply highlights. Each of those is referenced from
    at least two methods; storing them on ``self`` is the simplest
    correct shape.
    """

    _note_repository: NoteRepositoryProtocol
    _notebook_repository: NotebookRepositoryProtocol
    _app_state: AppState
    _clock: ClockFn
    _attachment_store: AttachmentStoreProtocol | None

    _title_label: Gtk.Label
    _count_label: Gtk.Label
    _sort_dropdown: Gtk.DropDown
    _list_box: Gtk.ListBox
    _empty_label: Gtk.Label

    _sort_key: NoteSortKey
    _row_for_note_id: dict[str, _NoteListRow]

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        note_repository: NoteRepositoryProtocol,
        notebook_repository: NotebookRepositoryProtocol,
        app_state: AppState,
        clock: ClockFn = _default_clock,
        attachment_store: AttachmentStoreProtocol | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_repository = note_repository
        self._notebook_repository = notebook_repository
        self._app_state = app_state
        self._clock = clock
        self._attachment_store = attachment_store
        self._sort_key = _DEFAULT_SORT_KEY
        self._row_for_note_id = {}

        # Header: title + count + sort dropdown.
        header = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _HEADER_SPACING_PX)
        header.set_margin_start(_ROW_PADDING_PX)
        header.set_margin_end(_ROW_PADDING_PX)
        header.set_margin_top(_HEADER_SPACING_PX)
        header.set_margin_bottom(_HEADER_SPACING_PX)

        self._title_label = Gtk.Label.new("")
        self._title_label.set_halign(Gtk.Align.START)
        self._title_label.set_hexpand(True)
        self._title_label.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self._title_label)

        self._count_label = Gtk.Label.new("0")
        self._count_label.set_halign(Gtk.Align.END)
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

        # Scrolled list of note rows.
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

        # Empty-state label, shown only when the filtered list is empty.
        # Always present in the widget tree; visibility is what we
        # toggle. This avoids the "build a label only when needed"
        # special case that complicates :meth:`refresh`.
        self._empty_label = Gtk.Label.new("No notes here yet.")
        self._empty_label.set_margin_top(_ROW_PADDING_PX * 4)
        self._empty_label.set_margin_bottom(_ROW_PADDING_PX * 4)
        self._empty_label.set_visible(False)
        self.append(self._empty_label)

        self.set_size_request(_DEFAULT_PANE_WIDTH_PX, -1)

        self._app_state.connect(
            "selection-changed",
            self._on_app_state_selection_changed,
        )
        self._app_state.connect(
            "query-changed",
            self._on_app_state_query_changed,
        )
        self._app_state.connect(
            "selected-note-changed",
            self._on_app_state_selected_note_changed,
        )

        # Initial population.
        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Recompute and re-render the displayed list.

        Sets the header title, sets the count, rebuilds the rows in
        the list box, toggles the empty-state label, and re-applies
        the highlight for the currently-selected note id (which may
        no longer exist in the filtered set).

        Cheap to call. Triggered automatically on selection / query
        changes; future controllers (step 11+) will call this from
        ``notes-changed`` / ``notebooks-changed`` handlers in
        :class:`MainWindow`.
        """
        self._title_label.set_text(
            title_for_selection(
                self._app_state.selection,
                self._notebook_repository,
            )
        )

        notes = compute_display_notes(
            note_repository=self._note_repository,
            notebook_repository=self._notebook_repository,
            selection=self._app_state.selection,
            query=self._app_state.query,
            sort_key=self._sort_key,
            now=self._clock(),
        )

        self._count_label.set_text(str(len(notes)))

        # Per-note attachment counts for the row meta line. A single
        # COUNT(*) per visible note keeps the badge off the BLOB path
        # entirely; with no store wired (some tests) every count is 0
        # and no paperclip is shown.
        counts = self._attachment_counts(notes)

        # Rebuild rows from scratch. ``remove_all`` clears the
        # ListBox; the row-for-note-id index is rebuilt as we go.
        self._list_box.remove_all()
        self._row_for_note_id = {}
        for note in notes:
            row = _make_note_row(note, counts[note.id])
            self._row_for_note_id[note.id] = row
            self._list_box.append(row)

        # Empty-state visibility depends only on the filtered count.
        self._empty_label.set_visible(not notes)
        self._list_box.set_visible(bool(notes))

        # Re-apply highlight: a note that is no longer in the list
        # must lose its selected state.
        self._apply_highlight()

    @property
    def sort_key(self) -> NoteSortKey:
        """Read access for tests and for future widgets that surface
        the active sort (e.g. a future status-bar indicator).
        """
        return self._sort_key

    def _attachment_counts(self, notes: Iterable[Note]) -> dict[str, int]:
        """Map each note id to its attachment count for the meta line.

        Sourced through the store the widget already holds rather than
        by threading a new parameter into :func:`compute_display_notes`
        (which keeps that free function's signature — and its existing
        ``too-many-arguments`` suppression — untouched). With no store
        wired, every count is 0.
        """
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
        """User clicked a note row — record the new selected id."""
        if not isinstance(row, _NoteListRow):
            return
        self._app_state.set_selected_note_id(row.payload.note_id)

    def _on_sort_changed(
        self,
        _dropdown: Gtk.DropDown,
        _pspec: object,
    ) -> None:
        """Sort dropdown changed — re-render with the new key."""
        index = self._sort_dropdown.get_selected()
        if 0 <= index < len(_SORT_KEY_DROPDOWN_ORDER):
            self._sort_key = _SORT_KEY_DROPDOWN_ORDER[index]
        self.refresh()

    def _on_app_state_selection_changed(self, _state: AppState) -> None:
        """Sidebar (or anything else) changed the selection — refresh."""
        self.refresh()

    def _on_app_state_query_changed(self, _state: AppState) -> None:
        """Live search query changed — re-filter the visible list."""
        self.refresh()

    def _on_app_state_selected_note_changed(self, _state: AppState) -> None:
        """The currently displayed note id changed — re-highlight."""
        self._apply_highlight()

    # ------------------------------------------------------------------
    # Highlight application
    # ------------------------------------------------------------------

    def _apply_highlight(self) -> None:
        """Make the row matching :attr:`AppState.selected_note_id`
        the visually-selected one in the list-box.

        If the id is ``None``, or refers to a note that is not in
        the currently displayed (filtered) set, the list-box is
        unselected. ``unselect_all`` and ``select_row`` do not
        emit ``row-activated``, so this method is safe to call
        from any signal handler without causing a feedback loop.
        """
        note_id = self._app_state.selected_note_id
        if note_id is None:
            self._list_box.unselect_all()
            return
        row = self._row_for_note_id.get(note_id)
        if row is None:
            self._list_box.unselect_all()
            return
        self._list_box.select_row(row)


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def _make_note_row(note: Note, attachment_count: int) -> _NoteListRow:
    """Build a single note-list row.

    Layout per row (top to bottom):

    * Title — bold, single line, end-ellipsised (``.note-title``).
    * Snippet — up to two wrapped lines, dimmed, end-ellipsised
      (``.note-snippet``); omitted entirely when the note has none.
    * Meta line — right-aligned ``📎 N | <date>``. The paperclip and
      count appear only when ``attachment_count`` is positive; the date
      always shows. The count, separator, and date are dimmed via CSS
      (palette-safe — opacity only, no colour).

    ``attachment_count`` is the note's number of attachments, surfaced
    by :class:`NoteList` from the attachment store. The row carries a
    :class:`_NoteRowPayload` so the click handler on the list-box can
    recover the note id without walking back through the model.
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
        # Two wrapped lines with an end ellipsis: enough to preview the
        # lead sentence without letting the row height run away.
        snippet_label.set_wrap(True)
        snippet_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        snippet_label.set_lines(2)
        snippet_label.set_ellipsize(Pango.EllipsizeMode.END)
        snippet_label.add_css_class(_SNIPPET_CSS_CLASS)
        box.append(snippet_label)

    box.append(_make_meta_line(note, attachment_count))

    return _NoteListRow(
        payload=_NoteRowPayload(note_id=note.id),
        child=box,
    )


def _make_meta_line(note: Note, attachment_count: int) -> Gtk.Box:
    """Build the right-aligned ``📎 N | <date>`` meta line for a row.

    When ``attachment_count`` is zero the line is just the date (no
    leading separator). The paperclip-count and separator share the
    date's dim treatment; the separator additionally carries its own
    low-opacity class.
    """
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
