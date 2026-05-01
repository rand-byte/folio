"""Reusable confirm-delete dialog and icon picker popover.

Principles & invariants
-----------------------
* This module is the single home for the modal dialogs the design
  reuses across notes and notebooks: the confirm-delete prompt (note
  delete from the toolbar's More menu, notebook delete from the
  sidebar context menu) and the icon picker popover (notebook icon
  change from the sidebar context menu). Centralising both keeps the
  user-facing dialog text in one place and lets every consumer test
  its own click handler against the same injected fakes.
* The confirm-delete surface is exposed as a **callable**, not a
  class. The :data:`ConfirmDialogPresenter` type alias is the public
  contract; :func:`default_confirm_dialog_presenter` is the
  production implementation that wraps :class:`Gtk.AlertDialog`.
  Tests pass a synchronous fake matching the alias and drive the
  result callback directly. This mirrors the
  :data:`FileDialogOpener` pattern from
  :mod:`notes_app.ui._image_picker`: production wraps an asynchronous
  GTK dialog; tests run synchronously.
* :class:`Gtk.AlertDialog` is the GTK 4.10+ idiomatic choice for
  alert-style modal prompts. The pre-4.10 :class:`Gtk.MessageDialog`
  is deprecated and is not used. The dialog's
  :meth:`Gtk.AlertDialog.choose` method is asynchronous; the
  production presenter packages its callback-based result into the
  presenter's simpler ``on_result(bool)`` shape — ``True`` means the
  user clicked the destructive button, ``False`` covers every other
  outcome (cancel, dismissal, backend error). Treating dismissal as
  cancellation matches the React reference (clicking the scrim does
  nothing destructive).
* The icon picker is a :class:`Gtk.Popover` subclass rather than a
  free function so it has somewhere to anchor (``set_parent``) and a
  natural "close on selection" behaviour via :meth:`popdown`. The
  caller anchors it to whatever widget triggered the picker (a
  context-menu button in the sidebar, a toolbar button in a future
  build) by calling :meth:`Gtk.Popover.set_parent`. The popover then
  invokes a single ``on_icon_picked`` callback with the chosen
  :class:`NotebookIcon` and pops itself down.
* The icon set rendered by the popover is **the entire**
  :class:`NotebookIcon` enum, in declaration order. Adding a member
  to the enum automatically extends the picker; removing one
  automatically shrinks it. The mapping from enum to FreeDesktop
  icon name is defined here for the picker; the sidebar maintains
  its own copy for its row rendering. Both reference the same enum,
  so they cannot disagree on which symbols are valid — only on which
  themed icon represents each one. A unit test pins the two
  mappings to the same enum members.
* Icon buttons inside the picker are :class:`Gtk.ToggleButton`s in a
  shared group, so the currently-selected icon is visually
  highlighted ("aria-pressed" in the design's HTML). A click on any
  button activates the group, fires the callback, and pops the
  popover down. The popover does not retain selection state across
  appearances — every show is a fresh layout, current-icon-driven.
* GTK 4 currency: :class:`Gtk.AlertDialog` (4.10+),
  :meth:`Gtk.Popover.set_parent` (the GTK 4 way to anchor a popover;
  the deprecated ``Gtk.Popover.popup`` overload taking a parent is
  not used), :meth:`Gtk.Widget.get_root` (rather than the deprecated
  :meth:`Gtk.Widget.get_toplevel`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import gi

gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import GLib, Gio, GObject, Gtk  # noqa: E402

from notes_app.enums import NotebookIcon


# ---------------------------------------------------------------------------
# Confirm-delete dialog
# ---------------------------------------------------------------------------


type ConfirmDialogPresenter = Callable[
    [
        Gtk.Window | None,  # parent_window — None acceptable but unparents
        str,                # title — primary message
        str,                # detail — secondary message
        str,                # confirm_label — destructive button label
        Callable[[bool], None],  # on_result(True=confirm, False=cancel)
    ],
    None,
]
"""Open a confirm-delete dialog and call ``on_result`` with the answer.

Production wiring: :func:`default_confirm_dialog_presenter`. Test
wiring: a synchronous fake that captures the parameters and lets the
test invoke the result callback explicitly.

The boolean argument to ``on_result`` distinguishes user confirmation
(``True``) from every other outcome (``False``: cancel, dialog
dismissal, backend error). The presenter never raises — failure
modes are folded into ``False``.
"""


_CANCEL_BUTTON_LABEL: Final[str] = "Cancel"
"""Label of the non-destructive button. Constant rather than a
parameter because every confirm-delete in the design uses the same
word — and a future translation lookup will change it in one place."""

_CANCEL_BUTTON_INDEX: Final[int] = 0
"""Position of the cancel button in the buttons array. The cancel
button comes first so it has the focused-by-default position
(the safest choice for destructive prompts)."""

_CONFIRM_BUTTON_INDEX: Final[int] = 1
"""Position of the destructive button. ``choose_finish`` returns this
index when the user picks Delete; the presenter compares against it
to derive the boolean returned to ``on_result``."""


def default_confirm_dialog_presenter(
    parent_window: Gtk.Window | None,
    title: str,
    detail: str,
    confirm_label: str,
    on_result: Callable[[bool], None],
) -> None:
    """Production presenter — wraps :class:`Gtk.AlertDialog`.

    Builds an alert dialog with the cancel button at index
    :data:`_CANCEL_BUTTON_INDEX` (focused by default — the safe
    default for destructive prompts) and the destructive button at
    :data:`_CONFIRM_BUTTON_INDEX`. The dialog's asynchronous
    ``choose`` is wrapped so the caller sees a single callback that
    fires once with ``True`` (confirmed) or ``False`` (anything else).

    A :class:`GLib.Error` raised by ``choose_finish`` — which GTK
    raises when the user dismisses the dialog without picking a
    button (Escape, scrim click, window close) — is caught and
    surfaced as ``on_result(False)``. Treating dismissal as
    cancellation is the conservative choice for a destructive
    prompt and matches the React reference's scrim behaviour.
    """
    dialog = Gtk.AlertDialog()
    dialog.set_message(title)
    dialog.set_detail(detail)
    dialog.set_buttons([_CANCEL_BUTTON_LABEL, confirm_label])
    dialog.set_default_button(_CANCEL_BUTTON_INDEX)
    dialog.set_cancel_button(_CANCEL_BUTTON_INDEX)
    # AlertDialog automatically handles modality once a parent is set;
    # an unparented dialog is allowed (GTK falls back to a transient-
    # less window), but the presenter prefers a parent when available.
    dialog.set_modal(True)

    def _on_chosen(
        source: GObject.Object,
        result: Gio.AsyncResult,
    ) -> None:
        # The first arg is the dialog itself; we accept it as
        # GObject.Object because that is the static type of the
        # GIO async-callback's first arg. Use ``source`` for the
        # finish call to mirror the GIO convention.
        try:
            chosen = source.choose_finish(result)  # type: ignore[attr-defined]
        except GLib.Error:
            on_result(False)
            return
        on_result(chosen == _CONFIRM_BUTTON_INDEX)

    dialog.choose(parent_window, None, _on_chosen)


# ---------------------------------------------------------------------------
# Icon picker popover
# ---------------------------------------------------------------------------


_PICKER_HEADER_TEXT: Final[str] = "Choose an icon"
"""Heading shown at the top of the icon-picker popover.

Matches the design's ``<div className="hd">Choose an icon</div>`` so
the picker reads identically across implementations.
"""

_PICKER_GRID_COLUMNS: Final[int] = 4
"""Number of icon buttons per row in the picker grid.

Four columns is wide enough that the popover stays compact for the
v1 :class:`NotebookIcon` enum (eleven members → three rows) and
matches the visual rhythm of the design's grid (which uses a
``grid-template-columns: repeat(6, 1fr)`` against a 22-icon set —
proportionally similar density per row).
"""

_PICKER_BUTTON_SPACING_PX: Final[int] = 4
"""Pixel spacing between icon buttons inside the picker grid."""

_PICKER_OUTER_PADDING_PX: Final[int] = 8
"""Padding around the picker contents inside the popover."""

_PICKER_FALLBACK_ICON_NAME: Final[str] = "folder-symbolic"
"""FreeDesktop icon shown when a :class:`NotebookIcon` has no entry
in :data:`_NOTEBOOK_ICON_NAMES`. Mirrors the equivalent constant
inside :mod:`notes_app.ui.sidebar`; the two are independently
defined (both reference the same enum so there is no shared
mutable state) and a unit test pins the mapping for parity."""

_NOTEBOOK_ICON_NAMES: Final[dict[NotebookIcon, str]] = {
    NotebookIcon.HOME: "user-home-symbolic",
    NotebookIcon.BOOK: "accessories-text-editor-symbolic",
    NotebookIcon.MAP: "mark-location-symbolic",
    NotebookIcon.BRAIN: "applications-science-symbolic",
    NotebookIcon.ARCHIVE: "package-x-generic-symbolic",
    NotebookIcon.BRIEFCASE: "system-run-symbolic",
    NotebookIcon.HEART: "emblem-favorite-symbolic",
    NotebookIcon.STAR: "starred-symbolic",
    NotebookIcon.FOLDER: "folder-symbolic",
    NotebookIcon.INBOX: "mail-inbox-symbolic",
    NotebookIcon.GRADUATION_CAP: "preferences-desktop-display-symbolic",
}
"""Mapping from :class:`NotebookIcon` to FreeDesktop icon names.

Independent of the equivalent mapping inside
:mod:`notes_app.ui.sidebar`. The redundancy is deliberate: each
module owns its own UI presentation. A test in
``test_dialogs.py`` verifies that every enum member resolves to a
non-empty icon name here, so a future enum addition that forgets
this map raises a clear failure rather than silently degrading to
the fallback for the picker."""


def _icon_name_for(icon: NotebookIcon) -> str:
    """Look up the FreeDesktop icon name with fallback."""
    return _NOTEBOOK_ICON_NAMES.get(icon, _PICKER_FALLBACK_ICON_NAME)


type IconPickedCallback = Callable[[NotebookIcon], None]
"""Callable invoked when the user clicks an icon in the picker.

The callback receives the chosen :class:`NotebookIcon`. The popover
itself pops down before the callback fires, so the callback is free
to immediately re-anchor the popover (or another popover) without
fighting GTK's "only one popup at a time" invariant.
"""


class IconPickerPopover(Gtk.Popover):
    """A popover containing a grid of notebook icons.

    Each icon is a :class:`Gtk.ToggleButton` in a shared group. The
    button corresponding to the popover's ``current_icon`` is
    pre-pressed so the user sees which icon the notebook already has.
    Clicking any button:

    1. invokes the constructor's ``on_icon_picked`` with the chosen
       :class:`NotebookIcon`;
    2. pops the popover down (:meth:`popdown`).

    The popover does not perform the actual notebook update — that
    is the caller's job. This separation lets the same popover be
    reused for any "pick a notebook icon" flow (sidebar context
    menu today, possibly a toolbar control later) without the
    popover knowing about repositories.

    The instance-attribute count is below pylint's default ceiling
    so no per-class ``too-many-instance-attributes`` waiver is
    needed: only the callback and the buttons dictionary are kept
    on ``self``.
    """

    _on_icon_picked: IconPickedCallback
    _icon_buttons: dict[NotebookIcon, Gtk.ToggleButton]

    def __init__(
        self,
        *,
        on_icon_picked: IconPickedCallback,
        current_icon: NotebookIcon | None = None,
    ) -> None:
        super().__init__()
        self._on_icon_picked = on_icon_picked
        self._icon_buttons = {}

        # Outer container: header label above the icon grid.
        outer = Gtk.Box.new(
            Gtk.Orientation.VERTICAL,
            _PICKER_BUTTON_SPACING_PX,
        )
        outer.set_margin_top(_PICKER_OUTER_PADDING_PX)
        outer.set_margin_bottom(_PICKER_OUTER_PADDING_PX)
        outer.set_margin_start(_PICKER_OUTER_PADDING_PX)
        outer.set_margin_end(_PICKER_OUTER_PADDING_PX)

        header = Gtk.Label.new(_PICKER_HEADER_TEXT)
        header.set_halign(Gtk.Align.START)
        outer.append(header)

        grid = Gtk.Grid.new()
        grid.set_row_spacing(_PICKER_BUTTON_SPACING_PX)
        grid.set_column_spacing(_PICKER_BUTTON_SPACING_PX)

        # First button is the "group leader". Every subsequent
        # button joins its group via ``set_group``, which makes
        # them mutually exclusive (radio-button semantics) — only
        # one stays pressed at a time, which is the right
        # behaviour for "the currently-selected icon".
        group_leader: Gtk.ToggleButton | None = None

        for index, icon in enumerate(NotebookIcon):
            row, column = divmod(index, _PICKER_GRID_COLUMNS)
            button = Gtk.ToggleButton.new()
            image = Gtk.Image.new_from_icon_name(_icon_name_for(icon))
            button.set_child(image)
            button.set_tooltip_text(icon.value)

            if group_leader is None:
                group_leader = button
            else:
                button.set_group(group_leader)

            if icon == current_icon:
                button.set_active(True)

            button.connect(
                "clicked",
                self._on_icon_button_clicked,
                icon,
            )
            self._icon_buttons[icon] = button
            grid.attach(button, column, row, 1, 1)

        outer.append(grid)
        self.set_child(outer)

    def _on_icon_button_clicked(
        self,
        _button: Gtk.ToggleButton,
        icon: NotebookIcon,
    ) -> None:
        """User clicked an icon. Notify and pop down.

        ``set_group`` guarantees at most one button is active at a
        time; we don't need to manually un-press peers. Popping down
        before invoking the callback ensures that if the callback
        re-opens the popover (or another popover) GTK does not see
        two popups racing for the same parent.
        """
        self.popdown()
        self._on_icon_picked(icon)

    @property
    def icon_buttons(self) -> dict[NotebookIcon, Gtk.ToggleButton]:
        """Read-only access to the per-icon buttons.

        Tests use this to verify that every enum member produced a
        button, that the active button matches ``current_icon``, and
        to drive a synthetic click without relying on GTK's signal
        emission machinery. Returning the live dict (rather than a
        copy) is fine here: the dict is populated once at
        construction and never mutated.
        """
        return self._icon_buttons
