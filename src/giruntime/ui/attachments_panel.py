"""The per-note attachments panel: list, add, and remove attachments.

Principles & invariants
-----------------------
* :class:`AttachmentsPanel` is the editor pane's attachment-management
  strip — an ``ATTACHMENTS · N`` header with an *Add file* button and
  one card per attachment (generic icon, filename, human-readable
  size, remove button). It is a self-contained :class:`Gtk.Box` so
  :class:`NoteEditor` stays focused and under pylint's line ceiling.
* **Adding a file never touches the note body.** The Add button routes
  the picked path through :meth:`NoteController.add_attachment` and
  refreshes the card list; it inserts no ``image::``/link macro.
  References in the body stay author-typed.
* Attachments are opaque blobs: every card shows the **same generic
  icon** — no per-type badge or styling — because the model carries no
  content-type classification.
* The panel is **stateless with respect to notes**: every change to
  :attr:`AppState.selected_note_id` reloads the card list from the
  injected ``AttachmentStoreProtocol.list_for_note``. With no note
  selected the panel is hidden entirely (mirroring the old image
  button's disabled state — there is nothing to attach to).
* Refresh is driven by the controller's narrow ``attachments-changed``
  signal (re-loading when the changed note is the selected one), which
  keeps the panel correct even when another observer mutates the same
  note's attachments. The panel *also* reloads synchronously after its
  own add/remove calls — the reload is idempotent, so the overlap with
  the signal-driven path is harmless belt-and-braces.
* A rejected add (size cap, unreadable file) inserts nothing and adds
  no card: the controller has already emitted ``attachment-rejected``
  for the outer toast layer; the panel stays silent.
* Removal is immediate — no confirmation dialog. Re-adding a file is
  cheap, so a modal would cost more friction than it saves.
* The dialog *opener* is injected as a :data:`FileDialogOpener`
  callable so tests can drive the post-pick code path synchronously;
  production defaults to :func:`default_file_dialog_opener` (which,
  per the no-classification rule, offers all files).
* GTK 4 currency: :meth:`Gtk.Box.append`, :class:`Gtk.Button`,
  :class:`Gtk.Image` from an icon name. No deprecated-in-4.18 calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from gi.repository import GObject, Gtk, Pango

from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.ui._file_picker import (
    FileDialogOpener,
    default_file_dialog_opener,
)
from giruntime.ui._filesize import format_byte_size
from models.attachment import Attachment
from storage.protocols import AttachmentStoreProtocol


_HEADER_TEMPLATE: Final[str] = "ATTACHMENTS · {n}"
"""Header text — the selected note's attachment count."""

_ADD_BUTTON_LABEL: Final[str] = "Add file"
_ADD_BUTTON_TOOLTIP: Final[str] = "Attach a file to this note"

_REMOVE_BUTTON_TOOLTIP: Final[str] = "Remove attachment"
_REMOVE_BUTTON_ICON_NAME: Final[str] = "user-trash-symbolic"

_ATTACHMENT_ICON_NAME: Final[str] = "mail-attachment-symbolic"
"""The single generic per-card icon — attachments carry no type."""

_PANEL_SPACING_PX: Final[int] = 4
_PANEL_PADDING_PX: Final[int] = 8
_CARD_SPACING_PX: Final[int] = 6

_HEADER_CSS_CLASS: Final[str] = "attachments-header"
_SIZE_CSS_CLASS: Final[str] = "dim-label"


class AttachmentsPanel(Gtk.Box):  # pylint: disable=too-many-instance-attributes
    """Attachment management strip for the currently selected note.

    The instance-attribute count exceeds pylint's default ceiling of
    seven by one because the panel genuinely depends on four injected
    collaborators (controller, app state, attachment store, dialog
    opener) plus three widget references it must mutate on reload
    (header label, Add button, cards box) and the current-note id.
    Hiding any of them behind a bundle object would obscure rather
    than clarify — every field is read or written from at least two
    methods.
    """

    _note_controller: NoteController
    _app_state: AppState
    _attachments: AttachmentStoreProtocol | None
    _open_file_dialog: FileDialogOpener

    _header_label: Gtk.Label
    _add_button: Gtk.Button
    _cards_box: Gtk.Box

    _current_note_id: str | None
    """Id of the note whose attachments are presently listed.

    ``None`` means no note is selected — the panel is hidden and the
    Add button has no target. Updated only inside :meth:`_reload` so
    the relationship between the visible cards and this id is
    invariant.
    """

    def __init__(
        self,
        *,
        note_controller: NoteController,
        app_state: AppState,
        attachments: AttachmentStoreProtocol | None,
        file_dialog_opener: FileDialogOpener = default_file_dialog_opener,
    ) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=_PANEL_SPACING_PX,
        )
        self._note_controller = note_controller
        self._app_state = app_state
        self._attachments = attachments
        self._open_file_dialog = file_dialog_opener
        self._current_note_id = None

        self.set_margin_top(_PANEL_PADDING_PX)
        self.set_margin_bottom(_PANEL_PADDING_PX)
        self.set_margin_start(_PANEL_PADDING_PX)
        self.set_margin_end(_PANEL_PADDING_PX)
        # The editor's ScrolledWindow is the vexpand child; the panel
        # takes only its natural height so it cannot starve the
        # editing area on small windows.
        self.set_vexpand(False)

        self.append(self._build_header())

        self._cards_box = Gtk.Box.new(
            Gtk.Orientation.VERTICAL,
            _PANEL_SPACING_PX,
        )
        self.append(self._cards_box)

        # Wire up signals last so handlers don't fire mid-construction.
        self._app_state.connect(
            "notify::selected-note-id",
            self._on_selected_note_changed,
        )
        self._note_controller.connect(
            "attachments-changed",
            self._on_attachments_changed,
        )

        # Pick up whatever ``selected_note_id`` is set to before the
        # panel was constructed — same pattern as the sibling panes.
        self._reload()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _build_header(self) -> Gtk.Box:
        """Build the header row: ``ATTACHMENTS · N`` + the Add button."""
        header = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _CARD_SPACING_PX)

        self._header_label = Gtk.Label.new(_HEADER_TEMPLATE.format(n=0))
        self._header_label.set_halign(Gtk.Align.START)
        self._header_label.set_hexpand(True)
        self._header_label.add_css_class(_HEADER_CSS_CLASS)
        self._header_label.add_css_class(_SIZE_CSS_CLASS)
        header.append(self._header_label)

        self._add_button = Gtk.Button.new_with_label(_ADD_BUTTON_LABEL)
        self._add_button.set_tooltip_text(_ADD_BUTTON_TOOLTIP)
        self._add_button.connect(
            "clicked",
            lambda _b: self._on_add_button_clicked(),
        )
        header.append(self._add_button)

        return header

    def _make_card(self, attachment: Attachment) -> Gtk.Box:
        """Build one attachment card: icon, filename, size, remove."""
        card = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _CARD_SPACING_PX)

        icon = Gtk.Image.new_from_icon_name(_ATTACHMENT_ICON_NAME)
        card.append(icon)

        name_label = Gtk.Label.new(attachment.filename)
        name_label.set_halign(Gtk.Align.START)
        name_label.set_hexpand(True)
        name_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        card.append(name_label)

        size_label = Gtk.Label.new(format_byte_size(attachment.byte_size))
        size_label.add_css_class(_SIZE_CSS_CLASS)
        card.append(size_label)

        remove_button = Gtk.Button.new_from_icon_name(
            _REMOVE_BUTTON_ICON_NAME,
        )
        remove_button.set_tooltip_text(_REMOVE_BUTTON_TOOLTIP)
        remove_button.connect(
            "clicked",
            lambda _b: self._on_remove_clicked(attachment),
        )
        card.append(remove_button)

        return card

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        """Rebuild the header and card list for the current selection.

        A ``None`` selection hides the whole panel (there is nothing
        to attach to); a selected note shows the header (``· 0`` when
        empty) plus the Add button, with one card per attachment from
        ``list_for_note``. A missing attachment store (``None`` — the
        same optional-injection contract the note list and view
        follow) lists as empty rather than raising.
        """
        note_id = self._app_state.selected_note_id
        self._current_note_id = note_id

        if note_id is None:
            self.set_visible(False)
            self._clear_cards()
            self._header_label.set_text(_HEADER_TEMPLATE.format(n=0))
            return

        self.set_visible(True)
        listed = (
            []
            if self._attachments is None
            else self._attachments.list_for_note(note_id)
        )
        self._header_label.set_text(_HEADER_TEMPLATE.format(n=len(listed)))
        self._clear_cards()
        for attachment in listed:
            self._cards_box.append(self._make_card(attachment))

    def _clear_cards(self) -> None:
        """Remove every card (GTK 4 has no ``remove_all`` on Box)."""
        child = self._cards_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._cards_box.remove(child)
            child = nxt

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_selected_note_changed(
        self,
        _app_state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._reload()

    def _on_attachments_changed(
        self,
        _controller: NoteController,
        note_id: str,
    ) -> None:
        """Reload when the changed note is the one being displayed.

        The panel already reloads synchronously after its own calls;
        this handler is what keeps it correct when *another* observer
        (or a future second entry point) mutates the same note's
        attachments.
        """
        if note_id == self._current_note_id:
            self._reload()

    def _on_add_button_clicked(self) -> None:
        """Open the file dialog and queue the post-pick handler.

        Bails when no note is selected — the panel is hidden in that
        state, but a programmatic ``emit("clicked")`` could bypass it.
        """
        if self._current_note_id is None:
            return
        # ``self`` is the parent widget for the dialog; the default
        # opener walks up to the root window.
        self._open_file_dialog(self, self._on_file_picked)

    def _on_file_picked(self, source_path: Path | None) -> None:
        """Handle the file-dialog result.

        ``source_path`` is :data:`None` when the user cancelled or the
        dialog backend reported an error — silence is the correct UX
        for "user changed their mind". A selection cleared between the
        dialog opening and the callback likewise bails: without a note
        id the attachment cannot be associated with anything.

        On a real path, route through
        :meth:`NoteController.add_attachment` and refresh. A rejection
        returns ``None`` from the controller (which has already
        emitted ``attachment-rejected`` for the toast layer); the
        panel adds no card and inserts nothing into the note body.
        """
        if source_path is None:
            return
        if self._current_note_id is None:
            return
        attachment = self._note_controller.add_attachment(
            self._current_note_id,
            source_path,
        )
        if attachment is None:
            return
        self._reload()

    def _on_remove_clicked(self, attachment: Attachment) -> None:
        """Remove ``attachment`` immediately and refresh — no modal."""
        self._note_controller.remove_attachment(
            attachment.id,
            attachment.note_id,
        )
        self._reload()
