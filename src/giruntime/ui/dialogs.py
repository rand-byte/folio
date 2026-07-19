"""Reusable confirm-delete dialog used across the application.

Principles & invariants
-----------------------
* This module is the single home for the modal dialogs the design
  reuses: today, just the confirm-delete prompt (note delete from the
  toolbar's More menu). Centralising means the user-facing dialog
  text lives in one place and every consumer tests its own click
  handler against the same injected fakes.
* The confirm-delete surface is exposed as a **callable**, not a
  class. The :data:`ConfirmDialogPresenter` type alias is the public
  contract; :func:`default_confirm_dialog_presenter` is the
  production implementation that wraps :class:`Gtk.AlertDialog`.
  Tests pass a synchronous fake matching the alias and drive the
  result callback directly. This mirrors the
  :data:`FileDialogOpener` pattern from
  :mod:`ui._file_picker`: production wraps an asynchronous
  GTK dialog; tests run synchronously.
* :class:`Gtk.AlertDialog` is the GTK 4.10+ idiomatic choice for
  alert-style modal prompts. The pre-4.10 :class:`Gtk.MessageDialog`
  is deprecated and is not used. The dialog's
  :meth:`Gtk.AlertDialog.choose` method is asynchronous; the
  production presenter packages its callback-based result into the
  presenter's simpler ``on_result(bool)`` shape — ``True`` means the
  user clicked the destructive button, ``False`` covers every other
  outcome (cancel, dismissal, backend error). Treating dismissal as
  cancellation matches the conservative choice for destructive
  prompts (clicking the scrim does nothing destructive).
* GTK 4 currency: :class:`Gtk.AlertDialog` (4.10+).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from gi.repository import GLib, Gio, GObject, Gtk


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
    prompt.
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
            chosen = source.choose_finish(result)
        except GLib.Error:
            on_result(False)
            return
        on_result(chosen == _CONFIRM_BUTTON_INDEX)

    dialog.choose(parent_window, None, _on_chosen)
