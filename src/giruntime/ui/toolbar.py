"""The application's top header bar — New, Delete, search, mode switch, Help.

Principles & invariants
-----------------------
* :class:`Toolbar` is the application's top bar — a
  :class:`Gtk.HeaderBar` populated with the controls the design
  shows in its titlebar, **all surfaced directly with no overflow
  menus**:

  * a *New* button on the left that creates a fresh note pre-filled
    with the currently selected tag set (when the sidebar has a tag
    selection) and selects it for editing;
  * a *Delete* button immediately after *New* — a standalone
    trash-icon button (icon-only, tooltip-labelled) so the two
    note-lifecycle actions sit together. It is **separate** from the
    *New* button (its own border, a gap between them) rather than
    linked, so the pair does not read as a single split/dropdown
    button;
  * a *Search* :class:`Gtk.ToggleButton` after *Delete* — a raised
    icon button styled like its neighbours (never ``flat``, so it
    reads as a button and not a glyph). Its pressed state mirrors
    whether the centre search is open, giving search an explicit,
    discoverable close affordance in addition to :kbd:`Escape`;
  * a **two-page centre stack** as the title widget, with pages named
    by :class:`enums.HeaderCentrePage`: ``TITLE`` shows the selected
    note's title (ellipsized; empty when no note is selected) and
    ``SEARCH`` shows the global search entry that mirrors
    :attr:`AppState.query` bidirectionally. The swap is a quick
    crossfade;
  * a View / Source segmented toggle on the right that mirrors
    :attr:`AppState.view_mode` bidirectionally;
  * a *Help* button on the right (icon + ``Syntax`` label) that opens
    the AsciiDoc syntax reference by activating the app-scoped
    ``app.help`` action.

  The toolbar sits in the window via :meth:`Gtk.Window.set_titlebar`
  on :class:`MainWindow`.

* **The search toggle's handler is the single page switcher.** Active
  swaps the stack to ``SEARCH`` and focuses the entry; inactive clears
  the entry (which clears :attr:`AppState.query` through the binding)
  and swaps back to ``TITLE``. The entry's ``stop-search`` signal
  (:kbd:`Escape`) unpresses the toggle programmatically; that
  writeback, like the View / Source one, is fenced by the shared
  ``_suppress_signal_writeback`` guard.

* **A non-empty query is never hidden behind the title.** Collapsing
  always clears the query first, and any write that makes ``query``
  non-empty while collapsed (the construction-time ``SYNC_CREATE``
  seed or a later programmatic write) expands the search. The two
  rules compose: there is no state where the note list is filtered
  and the header shows only a title.

* **The expanded entry spans the whole middle slot.**
  :class:`Gtk.HeaderBar` grants its title widget up to the widget's
  *natural* width, capped by the space between the packed button
  groups — expand flags are ignored there. So the entry requests a
  small minimum (:data:`_SEARCH_ENTRY_MIN_WIDTH_CHARS`, keeping the
  window freely shrinkable) and a deliberately huge natural size
  (:data:`_SEARCH_ENTRY_NATURAL_WIDTH_CHARS`); the cap then makes its
  left edge land one toolbar gap from the search toggle at any window
  width.

* **The title label mirrors the selected note.** It refreshes on
  ``AppState:notify::selected-note-id`` and on the note store's
  ``items-changed`` (the same channel every pane observes, so an
  edited title updates live), showing the empty string when no note
  is selected. It is ellipsized with a width cap so a long title can
  never push the packed boxes around.

* There are **no menu buttons**. An earlier design split actions across
  a note-scoped *More* popover (Duplicate / Delete) and an app-scoped
  primary (hamburger) menu (Help); both are gone. *Duplicate* was
  dropped, and *Delete* and *Help* were promoted to first-class
  toolbar buttons so every action is visible and one click away.

* The +New button's seed source is produced by
  :func:`controllers.note_controller.make_initial_source`, which
  inspects the current :class:`AppState` selection and returns a
  ``:tags: a, b`` pre-fill when a non-empty
  :class:`TagSelection` is active. The toolbar's click handler is a
  thin shim over the helper plus a switch to
  :data:`ViewMode.EDIT`.

* The search entry's ``text`` is bound *bidirectionally* to
  :attr:`AppState.query` through a :meth:`GObject.Object.bind_property`
  binding established at construction (with
  :data:`GObject.BindingFlags.SYNC_CREATE` for the initial
  ``query → text`` copy). GObject's own reverse-echo suppression breaks
  the update cycle without a hand-rolled guard, and — crucially —
  avoids the re-entrant ``set_text`` that used to reset the entry's
  cursor and reverse typed characters. The binding's correctness relies
  on ``query`` being stored verbatim (see the :class:`AppState`
  invariants); any normalisation there would reintroduce the cursor bug.

* The View / Source toggle maps the :class:`ViewMode` enum onto two
  :class:`Gtk.ToggleButton` ``active`` booleans, which is not a clean
  single-property bind, so it keeps explicit handlers. To prevent its
  update cycle (programmatic ``set_active`` → ``toggled`` →
  ``set_view_mode`` → ``notify::view-mode`` → ``set_active``) those
  handlers are fenced by the ``_suppress_signal_writeback`` guard flag,
  matching the editor's ``_loading`` field. The two grouped toggles are
  wrapped in a ``linked`` box (:data:`_MODE_TOGGLE_CSS_CLASS`) so they
  render as a **single segmented widget** — the natural shape for one
  two-way mode switch. This is the deliberate opposite of the *New* /
  *Delete* pair, which stays unlinked so it does not read as a split
  button.

* The *Delete* button is **note-scoped**: it is insensitive when no
  note is selected (the sensitivity rule the removed *More* menu
  carried) and goes through an injected
  :data:`ConfirmDialogPresenter`. Production wires
  :func:`default_confirm_dialog_presenter`. It is styled quietly (no
  ``destructive-action`` accent) — the confirmation dialog, not the
  toolbar icon, is where the destructive weight belongs.

* The *Help* button is **app-scoped**: it carries no click handler and
  is never note-dependent. It points at the ``app.help`` action via
  :meth:`Gtk.Actionable.set_action_name` — the same action the ``F1``
  accelerator triggers — so GTK activates it when the button is clicked.
  The action and its accelerator are registered by
  :class:`giruntime.ui.application.NotesApplication`; the button only
  references them. GTK exposes application actions to every widget in
  the window under the ``app.`` prefix, so the reference resolves once
  the toolbar is in the window's hierarchy.

* **Keyboard accelerators reuse the button behaviours, they do not copy
  them.** *New*, the search toggle, and *Delete* are also reachable from
  the keyboard (``Ctrl+N``, ``Ctrl+F``, and a focus-local ``Delete`` on the
  note list). The accelerators are wired as ``win.*`` actions by
  :class:`giruntime.ui.main_window.MainWindow`, whose handlers call the
  public :meth:`create_note`, :meth:`focus_search`, and
  :meth:`delete_selected` on this toolbar — the very methods the button
  ``clicked`` handlers call — so a key and its button can never diverge.
  Mode toggling (``Ctrl+E``) instead writes :attr:`AppState.view_mode`
  directly, which this toolbar already observes to keep the View / Source
  segmented toggle in sync.

* GTK 4 currency: :class:`Gtk.HeaderBar`, :class:`Gtk.SearchEntry`,
  :class:`Gtk.Stack`, :class:`Gtk.Button`,
  :meth:`Gtk.Button.set_icon_name`, :meth:`Gtk.Editable.set_width_chars`,
  :meth:`Gtk.Editable.set_max_width_chars`,
  :meth:`Gtk.Actionable.set_action_name`,
  :meth:`Gtk.ToggleButton.set_group`, :meth:`Gtk.Widget.get_root`.
"""

from __future__ import annotations

from typing import Final

from gi.repository import GObject, Gtk, Pango

from enums import HeaderCentrePage, ViewMode
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController, make_initial_source
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui.dialogs import (
    ConfirmDialogPresenter,
    default_confirm_dialog_presenter,
)


# ---------------------------------------------------------------------------
# Visible labels and tooltips
# ---------------------------------------------------------------------------


_NEW_BUTTON_LABEL: Final[str] = "New"
_NEW_BUTTON_TOOLTIP: Final[str] = "New note (Ctrl+N)"
_NEW_BUTTON_ICON: Final[str] = "list-add-symbolic"

_DELETE_BUTTON_TOOLTIP: Final[str] = "Delete note"
_DELETE_BUTTON_ICON: Final[str] = "user-trash-symbolic"

_SEARCH_PLACEHOLDER: Final[str] = "Search notes\u2026"

_SEARCH_TOGGLE_TOOLTIP: Final[str] = "Search notes"
_SEARCH_TOGGLE_ICON: Final[str] = "system-search-symbolic"

_SEARCH_ENTRY_MIN_WIDTH_CHARS: Final[int] = 16
"""Minimum width request of the search entry — kept small so the entry
never dictates the window's minimum width."""

_SEARCH_ENTRY_NATURAL_WIDTH_CHARS: Final[int] = 512
"""Natural width request of the search entry, deliberately far larger
than any realistic header bar. :class:`Gtk.HeaderBar` grants its title
widget up to natural width, capped by the space between the packed
button groups (expand flags are ignored for the title widget), so this
is what makes the expanded entry span the whole middle slot at any
window width."""

_TITLE_MAX_WIDTH_CHARS: Final[int] = 40
"""Width cap of the centre title label. Combined with end-ellipsizing
this keeps a long note title from pushing the packed boxes around."""

_TITLE_CSS_CLASS: Final[str] = "title"
"""GTK style class giving the centre label the standard header-bar
title look (bold). Standard Adwaita style class — no project CSS rule
backs it."""

_NO_NOTE_TITLE: Final[str] = ""
"""Centre title shown when no note is selected — deliberately empty, as
the old empty title widget was: the tag-based library has nothing to
surface there."""

_CENTRE_TRANSITION_DURATION_MS: Final[int] = 250
"""Duration of the title ↔ search crossfade — long enough to register,
short enough to never delay typing (the entry accepts keystrokes while
the fade runs). GTK's global ``gtk-enable-animations`` setting turns it
into an instant swap on reduced-motion setups."""

_MODE_VIEW_LABEL: Final[str] = "View"
_MODE_SOURCE_LABEL: Final[str] = "Source"
_MODE_TOGGLE_CSS_CLASS: Final[str] = "linked"
"""GTK style class that fuses the View / Source toggle pair into a single
segmented control (shared borders, only the outer corners rounded). Applied
to the box that holds the two grouped :class:`Gtk.ToggleButton`s so the mode
switch reads as one widget rather than two adjacent buttons. Standard
Adwaita style class — no project CSS rule backs it."""

_HELP_BUTTON_LABEL: Final[str] = "Syntax"
_HELP_BUTTON_TOOLTIP: Final[str] = "AsciiDoc syntax help (F1)"
_HELP_BUTTON_ICON: Final[str] = "help-about-symbolic"
_HELP_ACTION_DETAILED_NAME: Final[str] = "app.help"
"""Detailed name of the application-level help action the *Help* button
activates. The action and its ``F1`` accelerator are registered by
:class:`giruntime.ui.application.NotesApplication`; the button only
points at it via :meth:`Gtk.Actionable.set_action_name`, keeping Help
app-scoped (always available) rather than note-scoped like *Delete*."""

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

    _note_store: NoteListStore
    _note_controller: NoteController
    _app_state: AppState
    _confirm_dialog_presenter: ConfirmDialogPresenter

    _search_entry: Gtk.SearchEntry
    _search_toggle: Gtk.ToggleButton
    _centre_stack: Gtk.Stack
    _title_label: Gtk.Label
    _query_binding: GObject.Binding
    _view_button: Gtk.ToggleButton
    _source_button: Gtk.ToggleButton
    _delete_button: Gtk.Button
    _help_button: Gtk.Button

    _suppress_signal_writeback: bool

    def __init__(
        self,
        *,
        note_store: NoteListStore,
        note_controller: NoteController,
        app_state: AppState,
        confirm_dialog_presenter: ConfirmDialogPresenter = (
            default_confirm_dialog_presenter
        ),
    ) -> None:
        super().__init__()
        self._note_store = note_store
        self._note_controller = note_controller
        self._app_state = app_state
        self._confirm_dialog_presenter = confirm_dialog_presenter
        self._suppress_signal_writeback = False

        # ---------- left side: New + Delete + Search toggle ----------
        # New and Delete are the two note-lifecycle actions, kept
        # adjacent but as separate buttons (not a linked pair) so they
        # do not read as one split button. The Search toggle joins them
        # as a third raised button; its pressed state mirrors whether
        # the centre search is open.
        new_button = self._build_new_button()
        self._delete_button = self._build_delete_button()
        self._search_toggle = self._build_search_toggle()
        left_box = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX,
        )
        left_box.append(new_button)
        left_box.append(self._delete_button)
        left_box.append(self._search_toggle)
        self.pack_start(left_box)

        # ---------- centre: title ↔ search stack ----------
        # The centre swaps between the selected note's title and the
        # global search entry. Setting the stack as the title widget
        # also keeps GTK from auto-filling the centre slot with the
        # window title (which would duplicate the OS title bar).
        self._centre_stack = self._build_centre_stack()
        self.set_title_widget(self._centre_stack)

        # ---------- right side: View/Source segmented + Help ----------
        self._view_button, self._source_button = self._build_mode_toggle()
        self._help_button = self._build_help_button()

        right_box = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX,
        )
        mode_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        # `linked` fuses the two grouped toggles into one segmented
        # control — the natural representation of a single two-way mode
        # switch (unlike New/Delete, which stay separate so they do not
        # read as a split button).
        mode_box.add_css_class(_MODE_TOGGLE_CSS_CLASS)
        mode_box.append(self._view_button)
        mode_box.append(self._source_button)
        right_box.append(mode_box)
        right_box.append(self._help_button)
        self.pack_end(right_box)

        # ---------- AppState bindings & subscriptions ----------
        # The search entry round-trips with AppState.query through a
        # bidirectional property binding. SYNC_CREATE performs the
        # initial query -> text copy, so no explicit sync call is needed.
        # GObject suppresses the reverse echo within a propagation cycle,
        # which is what removes the re-entrant set_text() that used to
        # reset the cursor and reverse typed characters.
        self._query_binding = self._app_state.bind_property(
            "query",
            self._search_entry,
            "text",
            GObject.BindingFlags.BIDIRECTIONAL
            | GObject.BindingFlags.SYNC_CREATE,
        )
        self._app_state.connect(
            "notify::selected-note-id",
            self._on_selected_note_changed,
        )
        self._app_state.connect(
            "notify::view-mode",
            self._on_app_state_view_mode_changed,
        )
        # A query write while the search is collapsed (programmatic —
        # the entry itself is only editable while expanded) must never
        # leave a hidden filter behind the title.
        self._app_state.connect("notify::query", self._on_query_changed)
        # An edited note title arrives as items-changed, the same
        # channel every other pane observes.
        self._note_store.connect(
            "items-changed",
            self._on_store_items_changed,
        )

        # ---------- initial state ----------
        # The search entry is seeded by the binding's SYNC_CREATE above;
        # if that seed was non-empty, the search must start expanded
        # (never a hidden filter).
        self._refresh_delete_sensitivity()
        self._sync_mode_toggle_from_app_state()
        self._refresh_title()
        self._ensure_search_visible_if_query()

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

    def _build_delete_button(self) -> Gtk.Button:
        """Build the standalone, note-scoped *Delete* button.

        Icon-only (a trash can) with a tooltip carrying the label, so it
        is compact yet accessible. It is **not** given the
        ``destructive-action`` accent: the toolbar icon stays quiet and
        the confirmation dialog supplies the destructive weight.
        """
        button = Gtk.Button.new()
        button.set_icon_name(_DELETE_BUTTON_ICON)
        button.set_tooltip_text(_DELETE_BUTTON_TOOLTIP)
        button.connect("clicked", self._on_delete_clicked)
        return button

    def _build_search_toggle(self) -> Gtk.ToggleButton:
        """Build the raised search toggle in the note-actions group.

        Styled like its *Delete* neighbour (icon-only, tooltip-labelled,
        never ``flat``) so it reads as a real button. Being a toggle
        gives search an explicit close affordance — click again — in
        addition to :kbd:`Escape` in the entry.
        """
        toggle = Gtk.ToggleButton.new()
        toggle.set_icon_name(_SEARCH_TOGGLE_ICON)
        toggle.set_tooltip_text(_SEARCH_TOGGLE_TOOLTIP)
        toggle.connect("toggled", self._on_search_toggled)
        return toggle

    def _build_centre_stack(self) -> Gtk.Stack:
        """Build the two-page title ↔ search centre stack.

        Pages are named by :class:`HeaderCentrePage`. The transition is
        a quick crossfade — non-directional, reading as "same place,
        new mode" (a slide would imply spatial navigation, which a mode
        swap is not).
        """
        stack = Gtk.Stack.new()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(_CENTRE_TRANSITION_DURATION_MS)

        self._title_label = Gtk.Label.new(_NO_NOTE_TITLE)
        self._title_label.add_css_class(_TITLE_CSS_CLASS)
        self._title_label.set_max_width_chars(_TITLE_MAX_WIDTH_CHARS)
        self._title_label.set_ellipsize(Pango.EllipsizeMode.END)
        stack.add_named(self._title_label, HeaderCentrePage.TITLE)

        self._search_entry = self._build_search_entry()
        stack.add_named(self._search_entry, HeaderCentrePage.SEARCH)

        stack.set_visible_child_name(HeaderCentrePage.TITLE)
        return stack

    def _build_search_entry(self) -> Gtk.SearchEntry:
        entry = Gtk.SearchEntry.new()
        entry.set_placeholder_text(_SEARCH_PLACEHOLDER)
        # Small minimum + huge natural: the header bar caps the natural
        # size to the space between the packed button groups, which is
        # what makes the expanded entry fill the whole middle slot (see
        # the module invariants).
        entry.set_width_chars(_SEARCH_ENTRY_MIN_WIDTH_CHARS)
        entry.set_max_width_chars(_SEARCH_ENTRY_NATURAL_WIDTH_CHARS)
        entry.connect("stop-search", self._on_stop_search)
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

    def _build_help_button(self) -> Gtk.Button:
        """Build the app-scoped *Help* button.

        An icon + ``Syntax`` label that points at the application-level
        ``app.help`` action — the same action the ``F1`` accelerator
        triggers — via :meth:`Gtk.Actionable.set_action_name`. The button
        carries no click handler: GTK activates the referenced action on
        click and resolves ``app.help`` against the window's application.
        Because it targets an always-enabled app action rather than a
        per-note one, it is never note-scoped.
        """
        button = Gtk.Button.new()
        content = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX // 2,
        )
        content.append(Gtk.Image.new_from_icon_name(_HELP_BUTTON_ICON))
        content.append(Gtk.Label.new(_HELP_BUTTON_LABEL))
        button.set_child(content)
        button.set_tooltip_text(_HELP_BUTTON_TOOLTIP)
        button.set_action_name(_HELP_ACTION_DETAILED_NAME)
        return button

    # ------------------------------------------------------------------
    # User-driven event handlers — write into AppState / controllers
    # ------------------------------------------------------------------

    def _on_new_clicked(self, _button: Gtk.Button) -> None:
        self.create_note()

    def create_note(self) -> None:
        """Create a note pre-filled from the current selection and edit it.

        The single implementation behind both the *New* button and the
        ``win.new-note`` accelerator
        (:class:`giruntime.ui.main_window.MainWindow` routes the action
        here), so the two entry points cannot diverge. The seed source is
        :func:`make_initial_source`, which pre-fills ``:tags:`` from an
        active :class:`TagSelection`; the new note is then switched into
        :data:`ViewMode.EDIT`.
        """
        initial = make_initial_source(self._app_state.selection)
        self._note_controller.create_note(initial)
        self._app_state.set_view_mode(ViewMode.EDIT)

    def focus_search(self) -> None:
        """Open the header search and focus its entry.

        Behind the ``win.focus-search`` accelerator. Idempotent: if search
        is already expanded it just re-focuses the entry; otherwise it
        presses the toggle, whose handler expands the centre stack and
        grabs focus. Routing through the toggle keeps its pressed state and
        the visible centre page in lock-step, exactly as a pointer click
        would.
        """
        if self._search_toggle.get_active():
            self._expand_search()
        else:
            self._search_toggle.set_active(True)

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

    def _on_search_toggled(self, button: Gtk.ToggleButton) -> None:
        """The single page switcher — driven by the toggle's state."""
        if self._suppress_signal_writeback:
            return
        if button.get_active():
            self._expand_search()
        else:
            self._collapse_search()

    def _on_stop_search(self, _entry: Gtk.SearchEntry) -> None:
        """:kbd:`Escape` in the entry: unpress the toggle, collapse.

        The programmatic ``set_active(False)`` is fenced so the toggle
        handler does not fire a second, redundant collapse.
        """
        self._set_search_toggle_active(False)
        self._collapse_search()

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        self.delete_selected()

    def delete_selected(self) -> None:
        """Confirm, then delete the selected note.

        The single implementation behind both the *Delete* button and the
        note list's focus-local ``Delete`` shortcut (routed through
        ``win.delete-note`` on
        :class:`giruntime.ui.main_window.MainWindow`), so the key and the
        button share one confirm-then-delete path. A no-op when nothing is
        selected, so it is safe to invoke from the accelerator even though
        the action is also disabled in that state.
        """
        note_id = self._app_state.selected_note_id
        if note_id is None:
            return
        try:
            note = self._note_store.get_note(note_id)
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

    def _on_selected_note_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._refresh_delete_sensitivity()
        self._refresh_title()

    def _on_query_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._ensure_search_visible_if_query()

    def _on_store_items_changed(
        self,
        _store: NoteListStore,
        _position: int,
        _removed: int,
        _added: int,
    ) -> None:
        self._refresh_title()

    def _on_app_state_view_mode_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._sync_mode_toggle_from_app_state()

    # ------------------------------------------------------------------
    # Refresh / sync helpers
    # ------------------------------------------------------------------

    def _refresh_delete_sensitivity(self) -> None:
        self._delete_button.set_sensitive(
            self._app_state.selected_note_id is not None
        )

    def _refresh_title(self) -> None:
        """Mirror the selected note's title into the centre label."""
        self._title_label.set_text(self._selected_note_title())

    def _selected_note_title(self) -> str:
        note_id = self._app_state.selected_note_id
        if note_id is None:
            return _NO_NOTE_TITLE
        try:
            return self._note_store.get_note(note_id).title
        except KeyError:
            # The selected id can transiently point at a note the store
            # no longer holds (deletion races the selection refresh);
            # the note list auto-corrects the selection right after.
            return _NO_NOTE_TITLE

    def _expand_search(self) -> None:
        """Swap the centre to the search entry and focus it."""
        self._centre_stack.set_visible_child_name(HeaderCentrePage.SEARCH)
        self._search_entry.grab_focus()

    def _collapse_search(self) -> None:
        """Clear the query and swap the centre back to the title.

        Clearing goes through the entry so the bidirectional binding
        propagates the empty string into :attr:`AppState.query` — the
        note list un-filters exactly as if the user had cleared the
        box. This is what keeps a collapsed search from hiding a live
        filter.
        """
        self._search_entry.set_text("")
        self._centre_stack.set_visible_child_name(HeaderCentrePage.TITLE)

    def _ensure_search_visible_if_query(self) -> None:
        """Expand the search when a non-empty query would be hidden.

        Covers the construction-time ``SYNC_CREATE`` seed and any later
        programmatic :attr:`AppState.query` write. Never collapses —
        collapsing is owned by the toggle / :kbd:`Escape` paths, which
        clear the query first, so this cannot loop.
        """
        if self._app_state.query and not self._search_toggle.get_active():
            self._set_search_toggle_active(True)
            self._expand_search()

    def _set_search_toggle_active(self, active: bool) -> None:
        self._suppress_signal_writeback = True
        try:
            self._search_toggle.set_active(active)
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
    def search_toggle(self) -> Gtk.ToggleButton:
        return self._search_toggle

    @property
    def title_label(self) -> Gtk.Label:
        return self._title_label

    @property
    def centre_page(self) -> HeaderCentrePage:
        """The currently visible page of the centre stack."""
        return HeaderCentrePage(
            self._centre_stack.get_visible_child_name()
        )

    @property
    def view_button(self) -> Gtk.ToggleButton:
        return self._view_button

    @property
    def source_button(self) -> Gtk.ToggleButton:
        return self._source_button

    @property
    def delete_button(self) -> Gtk.Button:
        return self._delete_button

    @property
    def help_button(self) -> Gtk.Button:
        return self._help_button
