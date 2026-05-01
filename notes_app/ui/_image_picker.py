"""Image picker — file-dialog opener for the note editor.

Principles & invariants
-----------------------
* This module exists to keep :mod:`notes_app.ui.note_editor` under
  pylint's ``max-module-lines`` limit while preserving the editor's
  injection-friendly shape. The single public surface is
  :data:`FileDialogOpener` plus the production
  :func:`default_file_dialog_opener` that satisfies it.
* The opener's contract is *callback-style*: parameters are
  ``(parent: Gtk.Widget, on_result: Callable[[Path | None], None])``,
  return is ``None``. The result arrives asynchronously via
  ``on_result`` because :meth:`Gtk.FileDialog.open` is itself
  asynchronous in GTK 4.10+. Wrapping the asynchronous shape in
  the alias means callers do not need to know whether the picker
  is sync or async — both behave identically from the call site.
* MIME filters mirror :class:`MimeKind` exactly. The filter is a
  UI affordance only — the authoritative validation happens inside
  :meth:`AttachmentStore.add_for_note`. A user who bypasses the
  filter (by typing a path) still gets the typed rejection if the
  extension does not map to a supported MIME type.
* GTK currency: :class:`Gtk.FileDialog` (introduced in 4.10),
  :meth:`Gtk.Widget.get_root` (the modern way to find the
  enclosing window). The deprecated
  :class:`Gtk.FileChooserDialog` and :meth:`Gtk.Widget.get_toplevel`
  are not used.
* The opener gracefully handles three "no path" outcomes by
  forwarding :data:`None` to the result callback:
  user cancellation, dialog backend error, or a remote URI that
  has no local :class:`Path` representation. The editor's
  post-pick handler treats all three identically — "do nothing".
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import GLib, Gio, GObject, Gtk  # noqa: E402


type FileDialogOpener = Callable[
    [Gtk.Widget, Callable[[Path | None], None]],
    None,
]
"""Open a file picker, then call back with the chosen :class:`Path`.

Parameters: the *parent widget* (the editor — :class:`Gtk.FileDialog`
walks up to the window for modal anchoring) and a *result callback*
that receives the chosen path or :data:`None` if the user cancelled.

Production wiring: :func:`default_file_dialog_opener`. Test wiring: a
synchronous fake that captures the callback and lets the test invoke
it explicitly with a fake path.
"""


_IMAGE_FILTER_NAME: Final[str] = "Images"
"""User-facing label for the image MIME filter in the file dialog."""

_IMAGE_FILTER_MIME_TYPES: Final[tuple[str, ...]] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)
"""MIME types the editor's image dialog accepts.

Mirrors :class:`MimeKind` exactly. Rather than importing
:class:`MimeKind` and reading ``.value`` for every member (which
couples the filter list to the order of declarations), the values
are listed inline. The unit test pins that the two stay in sync.
"""

_DIALOG_TITLE: Final[str] = "Insert image"
"""Title shown in the file dialog's window decoration."""


def default_file_dialog_opener(
    parent: Gtk.Widget,
    on_result: Callable[[Path | None], None],
) -> None:
    """Production opener — wraps :class:`Gtk.FileDialog`.

    Builds the dialog, applies an image-only filter, and invokes
    :meth:`Gtk.FileDialog.open`. The async result callback unpacks
    the chosen :class:`Gio.File` to a :class:`Path` (or :data:`None`
    on cancellation / error / non-local URI) and forwards it to
    ``on_result``.

    The parent widget walks up to its top-level window via
    :meth:`Gtk.Widget.get_root` — the modern GTK 4 way (predecessor
    :meth:`Gtk.Widget.get_toplevel` is deprecated). A :data:`None`
    root is acceptable; the dialog will still open, just unparented.
    """
    dialog = Gtk.FileDialog.new()
    dialog.set_title(_DIALOG_TITLE)
    dialog.set_modal(True)

    image_filter = Gtk.FileFilter.new()
    image_filter.set_name(_IMAGE_FILTER_NAME)
    for mime in _IMAGE_FILTER_MIME_TYPES:
        image_filter.add_mime_type(mime)
    dialog.set_default_filter(image_filter)
    # Wrap the single filter in a ``Gio.ListStore`` so the dialog's
    # filter dropdown exposes it. ``Gio.ListStore`` is the GTK 4
    # collection type FileDialog accepts via ``set_filters``.
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(image_filter)
    dialog.set_filters(filters)

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
            # picked" — forward None and let the editor do nothing.
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
