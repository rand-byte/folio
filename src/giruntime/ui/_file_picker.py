"""File picker — the file-dialog openers the widgets inject.

Principles & invariants
-----------------------
* This module exists to keep the widgets that open a file dialog small
  while preserving their injection-friendly shape. There are two public
  surfaces — one per direction the bytes travel:
  :data:`FileDialogOpener` + :func:`default_file_dialog_opener` (pick a
  file to *attach*), and :data:`FileSaveDialogOpener` +
  :func:`default_file_save_dialog_opener` (pick where to *save* an
  attachment back out). The two are deliberately symmetric: same
  callback-style contract, same three "no path" outcomes, same test
  wiring.
* The opener's contract is *callback-style*: parameters are
  ``(parent: Gtk.Widget, on_result: Callable[[Path | None], None])``,
  return is ``None``. The result arrives asynchronously via
  ``on_result`` because :meth:`Gtk.FileDialog.open` is itself
  asynchronous in GTK 4.10+. Wrapping the asynchronous shape in
  the alias means callers do not need to know whether the picker
  is sync or async — both behave identically from the call site.
* The dialog offers **all files** — attachments are opaque blobs with
  no content-type allow-list, so there is no MIME filter to mirror.
  The authoritative validation (the size cap) happens inside
  :meth:`AttachmentStore.add_for_note`; a user whose pick exceeds the
  cap still gets the typed rejection.
* GTK currency: :class:`Gtk.FileDialog` (introduced in 4.10),
  :meth:`Gtk.Widget.get_root` (the modern way to find the
  enclosing window). The deprecated
  :class:`Gtk.FileChooserDialog` and :meth:`Gtk.Widget.get_toplevel`
  are not used.
* The opener gracefully handles three "no path" outcomes by
  forwarding :data:`None` to the result callback:
  user cancellation, dialog backend error, or a remote URI that
  has no local :class:`Path` representation. The caller's
  post-pick handler treats all three identically — "do nothing".
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

from gi.repository import GLib, Gio, GObject, Gtk


type FileDialogOpener = Callable[
    [Gtk.Widget, Callable[[Path | None], None]],
    None,
]
"""Open a file picker, then call back with the chosen :class:`Path`.

Parameters: the *parent widget* (:class:`Gtk.FileDialog`
walks up to the window for modal anchoring) and a *result callback*
that receives the chosen path or :data:`None` if the user cancelled.

Production wiring: :func:`default_file_dialog_opener`. Test wiring: a
synchronous fake that captures the callback and lets the test invoke
it explicitly with a fake path.
"""


type FileSaveDialogOpener = Callable[
    [Gtk.Widget, str, Callable[[Path | None], None]],
    None,
]
"""Open a *save* dialog pre-filled with a name, then call back with the path.

Parameters: the *parent widget* (walked up to the window for modal
anchoring, exactly as :data:`FileDialogOpener` does), the *suggested
name* the dialog pre-fills (production passes the attachment's
filename), and a *result callback* that receives the chosen path — or
:data:`None` when the user cancelled, the backend errored, or the target
is a non-local URI.

Production wiring: :func:`default_file_save_dialog_opener`. Test wiring:
a synchronous fake that records the suggested name and lets the test
invoke the callback with a temporary path.
"""


_DIALOG_TITLE: Final[str] = "Attach file"
"""Title shown in the open dialog's window decoration."""

_SAVE_DIALOG_TITLE: Final[str] = "Save attachment"
"""Title shown in the save dialog's window decoration."""


def default_file_dialog_opener(
    parent: Gtk.Widget,
    on_result: Callable[[Path | None], None],
) -> None:
    """Production opener — wraps :class:`Gtk.FileDialog`.

    Builds the dialog (no file filter — any file may be attached) and
    invokes :meth:`Gtk.FileDialog.open`. The async result callback
    unpacks the chosen :class:`Gio.File` to a :class:`Path` (or
    :data:`None` on cancellation / error / non-local URI) and forwards
    it to ``on_result``.

    The parent widget walks up to its top-level window via
    :meth:`Gtk.Widget.get_root` — the modern GTK 4 way (predecessor
    :meth:`Gtk.Widget.get_toplevel` is deprecated). A :data:`None`
    root is acceptable; the dialog will still open, just unparented.
    """
    dialog = Gtk.FileDialog.new()
    dialog.set_title(_DIALOG_TITLE)
    dialog.set_modal(True)

    root = parent.get_root()
    parent_window = root if isinstance(root, Gtk.Window) else None

    def _on_open_finished(
        source: GObject.Object,
        result: Gio.AsyncResult,
    ) -> None:
        # ``source`` is the dialog itself (Gtk.FileDialog). We accept
        # it as ``GObject.Object`` because that is the static type of
        # the GIO async-callback's first arg, but we know its runtime
        # shape is the dialog.
        del source
        try:
            chosen = dialog.open_finish(result)
        except GLib.Error:
            # User cancelled or the dialog backend reported an error.
            # Either way the user-facing semantics are "no path
            # picked" — forward None and let the caller do nothing.
            on_result(None)
            return
        if chosen is None:
            on_result(None)
            return
        path_str = chosen.get_path()
        if path_str is None:
            # A non-local URI (gvfs / portal mounts) — out of scope
            # for v1; surface as no-pick so the user can try again
            # with a local file.
            on_result(None)
            return
        on_result(Path(path_str))

    dialog.open(parent_window, None, _on_open_finished)


def default_file_save_dialog_opener(
    parent: Gtk.Widget,
    suggested_name: str,
    on_result: Callable[[Path | None], None],
) -> None:
    """Production save-opener — wraps :class:`Gtk.FileDialog`.

    Pre-fills the dialog with ``suggested_name``
    (:meth:`Gtk.FileDialog.set_initial_name`) and invokes
    :meth:`Gtk.FileDialog.save`. The async result callback unpacks the
    chosen :class:`Gio.File` to a :class:`Path` (or :data:`None` on
    cancellation / backend error / non-local URI) and forwards it to
    ``on_result`` — the same three "no path" outcomes the open-opener
    already collapses to :data:`None`, so both call sites treat "no
    path" identically: do nothing.

    ``Gtk.FileDialog`` is the GTK 4.10+ API; nothing here is deprecated
    in 4.18.
    """
    dialog = Gtk.FileDialog.new()
    dialog.set_title(_SAVE_DIALOG_TITLE)
    dialog.set_modal(True)
    dialog.set_initial_name(suggested_name)

    root = parent.get_root()
    parent_window = root if isinstance(root, Gtk.Window) else None

    def _on_save_finished(
        source: GObject.Object,
        result: Gio.AsyncResult,
    ) -> None:
        # ``source`` is the dialog itself; the parameter exists because
        # that is the static shape of the GIO async callback.
        del source
        try:
            chosen = dialog.save_finish(result)
        except GLib.Error:
            on_result(None)
            return
        if chosen is None:
            on_result(None)
            return
        path_str = chosen.get_path()
        if path_str is None:
            # A non-local URI (gvfs / portal mounts) — out of scope, as
            # on the inbound path.
            on_result(None)
            return
        on_result(Path(path_str))

    dialog.save(parent_window, None, _on_save_finished)
