"""The application's top header bar — New, search, mode switch, More menu.

Principles & invariants
-----------------------
* :class:`Toolbar` is the application's top bar — a
  :class:`Gtk.HeaderBar` populated with the controls the design
  shows in its titlebar:

  * a *New* button on the left that creates a fresh note pre-filled
    with the currently selected tag set (when the sidebar has a tag
    selection) and selects it for editing;
  * a global search entry that mirrors :attr:`AppState.query`
    bidirectionally;
  * an empty centre title widget (no breadcrumb — the tag-based
    library has no hierarchy to surface);
  * a View / Source segmented toggle on the right that mirrors
    :attr:`AppState.view_mode` bidirectionally;
  * a *More* menu button whose popover surfaces the *Duplicate note*
    and *Delete note* actions matching the design's three-dot menu.

  The toolbar sits in the window via :meth:`Gtk.Window.set_titlebar`
  on :class:`MainWindow`.

* The +New button's seed source is produced by
  :func:`controllers.note_controller.make_initial_source`, which
  inspects the current :class:`AppState` selection and returns a
  ``:tags: a, b`` pre-fill when a non-empty
  :class:`TagSelection` is active. The toolbar's click handler is a
  thin shim over the helper plus a switch to
  :data:`ViewMode.EDIT`.

* The search entry and the mode toggle are bound *bidirectionally*
  to :class:`AppState`. To prevent the obvious update cycle (user
  types → search-changed → set_query → query-changed → set_text →
  search-changed) the toolbar uses a guard flag pattern matching
  the editor's ``_loading`` field.

* The More menu is a :class:`Gtk.MenuButton` with a hand-built
  :class:`Gtk.Popover` containing :class:`Gtk.Button` rows.
  The menu button is **disabled** when no note is selected.

* The Delete action goes through an injected
  :data:`ConfirmDialogPresenter`. Production wires
  :func:`default_confirm_dialog_presenter`.

* GTK 4 currency: :class:`Gtk.HeaderBar`, :class:`Gtk.SearchEntry`,
  :class:`Gtk.MenuButton`, :class:`Gtk.ToggleButton.set_group`,
  :meth:`Gtk.Widget.get_root`.
"""

from __future__ import annotations

from typing import Final

import gi

gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk  # noqa: E402

from controllers.app_state import AppState
from controllers.note_controller import NoteController, make_initial_source
from enums import ViewMode
from storage.protocols import NoteRepositoryProtocol
from ui.dialogs import (
    ConfirmDialogPresenter,
    default_confirm_dialog_presenter,
)


# ---------------------------------------------------------------------------
# Visible labels and tooltips
# ---------------------------------------------------------------------------


_NEW_BUTTON_LABEL: Final[str] = "New"
_NEW_BUTTON_TOOLTIP: Final[str] = "New note (Ctrl+N)"

_SEARCH_PLACEHOLDER: Final[str] = "Search notes\u2026"

_MODE_VIEW_LABEL: Final[str] = "View"
_MODE_SOURCE_LABEL: Final[str] = "Source"

_MORE_BUTTON_TOOLTIP: Final[str] = "More"
_MORE_BUTTON_ICON: Final[str] = "view-more-symbolic"

_NEW_BUTTON_ICON: Final[str] = "list-add-symbolic"

_MENU_DUPLICATE_LABEL: Final[str] = "Duplicate note"
_MENU_DELETE_LABEL: Final[str] = "Delete note"

_DELETE_DIALOG_TITLE_FORMAT: Final[str] = 'Delete "{title}"?'
_DELETE_DIALOG_DETAIL: Final[str] = (
    "This note and its attachments will be removed. "
    "This cannot be undone."
)
_DELETE_DIALOG_CONFIRM_LABEL: Final[str] = "Delete"

_TOOLBAR_INNER_SPACING_PX: Final[int] = 6
"""Spacing between sibling widgets inside packed-start / packed-end boxes."""


# ---------------------------------------------------------------------------
# Toolbar widget
# ---------------------------------------------------------------------------


class Toolbar(  # pylint: disable=too-many-instance-attributes
    Gtk.HeaderBar,
):
    """The application's top header bar."""

    _note_repository: NoteRepositoryProtocol
    _note_controller: NoteController
    _app_state: AppState
    _confirm_dialog_presenter: ConfirmDialogPresenter

    _search_entry: Gtk.SearchEntry
    _view_button: Gtk.ToggleButton
    _source_button: Gtk.ToggleButton
    _more_menu_button: Gtk.MenuButton
    _more_popover: Gtk.Popover

    _suppress_signal_writeback: bool

    def __init__(
        self,
        *,
        note_repository: NoteRepositoryProtocol,
        note_controller: NoteController,
        app_state: AppState,
        confirm_dialog_presenter: ConfirmDialogPresenter = (
            default_confirm_dialog_presenter
        ),
    ) -> None:
        super().__init__()
        self._note_repository = note_repository
        self._note_controller = note_controller
        self._app_state = app_state
        self._confirm_dialog_presenter = confirm_dialog_presenter
        self._suppress_signal_writeback = False

        # ---------- left side: New button + search entry ----------
        new_button = self._build_new_button()
        self._search_entry = self._build_search_entry()
        left_box = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX,
        )
        left_box.append(new_button)
        left_box.append(self._search_entry)
        self.pack_start(left_box)

        # ---------- centre: intentionally empty ----------
        # No breadcrumb in the tag-based library. An empty label is set
        # as the title widget so GTK does not auto-fill the centre slot
        # with the window title (which would duplicate the OS title bar).
        self.set_title_widget(Gtk.Label.new(""))

        # ---------- right side: View/Source segmented + More ----------
        self._view_button, self._source_button = self._build_mode_toggle()
        self._more_popover = self._build_more_popover()
        self._more_menu_button = self._build_more_menu_button(self._more_popover)

        right_box = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX,
        )
        mode_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        mode_box.append(self._view_button)
        mode_box.append(self._source_button)
        right_box.append(mode_box)
        right_box.append(self._more_menu_button)
        self.pack_end(right_box)

        # ---------- AppState subscriptions ----------
        self._app_state.connect(
            "selected-note-changed",
            self._on_selected_note_changed,
        )
        self._app_state.connect(
            "query-changed",
            self._on_app_state_query_changed,
        )
        self._app_state.connect(
            "view-mode-changed",
            self._on_app_state_view_mode_changed,
        )

        # ---------- initial state ----------
        self._refresh_more_menu_sensitivity()
        self._sync_search_entry_from_app_state()
        self._sync_mode_toggle_from_app_state()

    # ------------------------------------------------------------------
    # Construction helpers — one method per child widget
    # ------------------------------------------------------------------

    def _build_new_button(self) -> Gtk.Button:
        button = Gtk.Button.new()
        content = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX // 2,
        )
        content.append(Gtk.Image.new_from_icon_name(_NEW_BUTTON_ICON))
        content.append(Gtk.Label.new(_NEW_BUTTON_LABEL))
        button.set_child(content)
        button.set_tooltip_text(_NEW_BUTTON_TOOLTIP)
        button.connect("clicked", self._on_new_clicked)
        return button

    def _build_search_entry(self) -> Gtk.SearchEntry:
        entry = Gtk.SearchEntry.new()
        entry.set_placeholder_text(_SEARCH_PLACEHOLDER)
        entry.connect("search-changed", self._on_search_changed)
        return entry

    def _build_mode_toggle(
        self,
    ) -> tuple[Gtk.ToggleButton, Gtk.ToggleButton]:
        view_button = Gtk.ToggleButton.new_with_label(_MODE_VIEW_LABEL)
        source_button = Gtk.ToggleButton.new_with_label(_MODE_SOURCE_LABEL)
        source_button.set_group(view_button)
        view_button.connect("toggled", self._on_view_toggle_changed)
        source_button.connect("toggled", self._on_source_toggle_changed)
        return view_button, source_button

    def _build_more_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover.new()
        contents = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)

        duplicate_button = Gtk.Button.new_with_label(_MENU_DUPLICATE_LABEL)
        duplicate_button.set_has_frame(False)
        duplicate_button.connect("clicked", self._on_duplicate_clicked)
        contents.append(duplicate_button)

        delete_button = Gtk.Button.new_with_label(_MENU_DELETE_LABEL)
        delete_button.set_has_frame(False)
        delete_button.add_css_class("destructive-action")
        delete_button.connect("clicked", self._on_delete_clicked)
        contents.append(delete_button)

        popover.set_child(contents)
        return popover

    def _build_more_menu_button(
        self,
        popover: Gtk.Popover,
    ) -> Gtk.MenuButton:
        button = Gtk.MenuButton.new()
        button.set_icon_name(_MORE_BUTTON_ICON)
        button.set_tooltip_text(_MORE_BUTTON_TOOLTIP)
        button.set_popover(popover)
        return button

    # ------------------------------------------------------------------
    # User-driven event handlers — write into AppState / controllers
    # ------------------------------------------------------------------

    def _on_new_clicked(self, _button: Gtk.Button) -> None:
        """Create a note pre-filled from the current selection."""
        initial = make_initial_source(self._app_state.selection)
        self._note_controller.create_note(initial)
        self._app_state.set_view_mode(ViewMode.EDIT)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        if self._suppress_signal_writeback:
            return
        self._app_state.set_query(entry.get_text())

    def _on_view_toggle_changed(self, button: Gtk.ToggleButton) -> None:
        if self._suppress_signal_writeback:
            return
        if button.get_active():
            self._app_state.set_view_mode(ViewMode.VIEW)

    def _on_source_toggle_changed(self, button: Gtk.ToggleButton) -> None:
        if self._suppress_signal_writeback:
            return
        if button.get_active():
            self._app_state.set_view_mode(ViewMode.EDIT)

    def _on_duplicate_clicked(self, _button: Gtk.Button) -> None:
        self._more_popover.popdown()
        note_id = self._app_state.selected_note_id
        if note_id is None:
            return
        self._note_controller.duplicate_note(note_id)

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        self._more_popover.popdown()
        note_id = self._app_state.selected_note_id
        if note_id is None:
            return
        try:
            note = self._note_repository.get(note_id)
        except KeyError:
            return

        captured_note_id = note.id

        def on_confirm_result(confirmed: bool) -> None:
            if confirmed:
                self._note_controller.request_delete(captured_note_id)

        self._confirm_dialog_presenter(
            self._parent_window(),
            _DELETE_DIALOG_TITLE_FORMAT.format(title=note.title),
            _DELETE_DIALOG_DETAIL,
            _DELETE_DIALOG_CONFIRM_LABEL,
            on_confirm_result,
        )

    # ------------------------------------------------------------------
    # AppState-driven handlers — programmatic widget refreshes
    # ------------------------------------------------------------------

    def _on_selected_note_changed(self, _state: AppState) -> None:
        self._refresh_more_menu_sensitivity()

    def _on_app_state_query_changed(self, _state: AppState) -> None:
        self._sync_search_entry_from_app_state()

    def _on_app_state_view_mode_changed(self, _state: AppState) -> None:
        self._sync_mode_toggle_from_app_state()

    # ------------------------------------------------------------------
    # Refresh / sync helpers
    # ------------------------------------------------------------------

    def _refresh_more_menu_sensitivity(self) -> None:
        self._more_menu_button.set_sensitive(
            self._app_state.selected_note_id is not None
        )

    def _sync_search_entry_from_app_state(self) -> None:
        self._suppress_signal_writeback = True
        try:
            self._search_entry.set_text(self._app_state.query)
        finally:
            self._suppress_signal_writeback = False

    def _sync_mode_toggle_from_app_state(self) -> None:
        self._suppress_signal_writeback = True
        try:
            mode = self._app_state.view_mode
            self._view_button.set_active(mode == ViewMode.VIEW)
            self._source_button.set_active(mode == ViewMode.EDIT)
        finally:
            self._suppress_signal_writeback = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parent_window(self) -> Gtk.Window | None:
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    # ------------------------------------------------------------------
    # Read-only properties exposed for tests
    # ------------------------------------------------------------------

    @property
    def search_entry(self) -> Gtk.SearchEntry:
        return self._search_entry

    @property
    def view_button(self) -> Gtk.ToggleButton:
        return self._view_button

    @property
    def source_button(self) -> Gtk.ToggleButton:
        return self._source_button

    @property
    def more_menu_button(self) -> Gtk.MenuButton:
        return self._more_menu_button

    @property
    def more_popover(self) -> Gtk.Popover:
        return self._more_popover
