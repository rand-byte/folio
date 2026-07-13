"""The per-note attachments panel: list, add, and remove attachments.

Principles & invariants
-----------------------
* :class:`AttachmentsPanel` is the editor pane's attachment-management
  strip — an ``ATTACHMENTS · N`` header with an *Add file* button and
  one card per attachment (generic icon, filename above its
  human-readable size, remove button). It is a self-contained
  :class:`Gtk.Box` so :class:`NoteEditor` stays focused and under
  pylint's line ceiling.
* A card is **two lines, not one row**: the filename sits directly
  above the size inside a shared vertical box, so the size always
  reads as metadata *about that file* however wide the window grows.
* A card wears a **border-only frame** (the ``attachment-card`` class
  styled in ``css/app.css``): the border and padding are what make its
  icon, name, size and remove button read as *one object* instead of
  four widgets adrift in the panel's whitespace. No fill — the frame
  bounds the card, the pane behind it stays the background. The remove
  button is **flat** inside that frame (no frame within a frame); it
  grows a wash on hover. Like every other rule in that stylesheet the
  colours are theme-derived, never named here.
* The cards are a :class:`Gtk.FlowBox` **grid inside a height-capped**
  :class:`Gtk.ScrolledWindow`. Attachments therefore cost *rows*, not
  one row each, and the panel's height is **bounded** at
  :data:`_VISIBLE_ROWS` card rows however many attachments a note has
  — the editing area above it can no longer be starved.
* The cap is **2.5 rows, measured, not hard-coded**: the half row is
  the overflow affordance (a clipped card announces that scrolling
  reveals more), and deriving it from a card's measured natural height
  (:func:`scroll_cap_height`, re-applied on every reload) keeps it
  correct across themes and font sizes.
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
* GTK 4 currency: :meth:`Gtk.Box.append`, :meth:`Gtk.FlowBox.append`,
  :class:`Gtk.ScrolledWindow`, :class:`Gtk.Button`, :class:`Gtk.Image`
  from an icon name. No deprecated-in-4.18 calls.
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

_CARD_MIN_WIDTH_PX: Final[int] = 200
"""Width floor for one card, so a long filename cannot squeeze it."""

_MIN_CARDS_PER_LINE: Final[int] = 1
_MAX_CARDS_PER_LINE: Final[int] = 4
"""Grid bounds: reflow down to one column, never past four."""

_VISIBLE_ROWS: Final[float] = 2.5
"""Card rows visible before the panel scrolls.

The fractional row is deliberate: a clipped row is the affordance that
tells the reader more attachments exist below the fold.
"""

_HEADER_CSS_CLASS: Final[str] = "attachments-header"
_SIZE_CSS_CLASS: Final[str] = "dim-label"
_CARD_CSS_CLASS: Final[str] = "attachment-card"
"""Border-only frame for one card — the class ``css/app.css`` styles.

The frame is what makes a card read as a single object; its padding
also feeds the measured card height the scroll cap is derived from.
"""

_REMOVE_BUTTON_CSS_CLASS: Final[str] = "attachment-card-remove"
"""Flattens the per-card remove button — no frame inside the frame."""


def scroll_cap_height(
    card_height: int,
    row_spacing: int,
    visible_rows: float = _VISIBLE_ROWS,
) -> int:
    """Pixel height showing ``visible_rows`` rows of ``card_height`` cards.

    Full rows contribute their height plus the spacing that follows
    them; a trailing fractional row contributes that fraction of a
    card height and no spacing (nothing follows it). A card height of
    zero (or less) yields zero: with no card to show there is no cap
    to compute, and a bare stack of spacings would be nonsense.

    Pure arithmetic — no GTK, no display.
    """
    if card_height <= 0:
        return 0
    full_rows = int(visible_rows)
    fractional_row = visible_rows - full_rows
    return round(
        full_rows * (card_height + row_spacing)
        + fractional_row * card_height,
    )


class AttachmentsPanel(Gtk.Box):  # pylint: disable=too-many-instance-attributes
    """Attachment management strip for the currently selected note.

    The instance-attribute count exceeds pylint's default ceiling of
    seven by two because the panel genuinely depends on four injected
    collaborators (controller, app state, attachment store, dialog
    opener) plus four widget references it must mutate on reload
    (header label, Add button, the cards flow box and the scroller
    whose height cap is recomputed from a measured card) and the
    current-note id. Hiding any of them behind a bundle object would
    obscure rather than clarify — every field is read or written from
    at least two methods.
    """

    _note_controller: NoteController
    _app_state: AppState
    _attachments: AttachmentStoreProtocol | None
    _open_file_dialog: FileDialogOpener

    _header_label: Gtk.Label
    _add_button: Gtk.Button
    _cards_flow: Gtk.FlowBox
    _cards_scroller: Gtk.ScrolledWindow

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
        # takes its natural height, and that height is *bounded* — the
        # cards grid is capped at _VISIBLE_ROWS rows and scrolls past
        # them — so it cannot starve the editing area however many
        # attachments the note carries.
        self.set_vexpand(False)

        self.append(self._build_header())
        self.append(self._build_cards_scroller())

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

    def _build_cards_scroller(self) -> Gtk.ScrolledWindow:
        """Build the cards grid inside its height-capped scroller.

        ``propagate_natural_height`` + a ``max_content_height`` set in
        :meth:`_reload` is exactly "grow to fit, then stop and
        scroll": below the cap the panel is as tall as it needs to be
        and shows no scrollbar. ``hscrollbar_policy=NEVER`` keeps the
        grid reflowing horizontally rather than scrolling sideways.
        """
        self._cards_flow = Gtk.FlowBox()
        self._cards_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._cards_flow.set_homogeneous(True)
        self._cards_flow.set_min_children_per_line(_MIN_CARDS_PER_LINE)
        self._cards_flow.set_max_children_per_line(_MAX_CARDS_PER_LINE)
        self._cards_flow.set_row_spacing(_CARD_SPACING_PX)
        self._cards_flow.set_column_spacing(_CARD_SPACING_PX)

        self._cards_scroller = Gtk.ScrolledWindow()
        self._cards_scroller.set_child(self._cards_flow)
        self._cards_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self._cards_scroller.set_propagate_natural_height(True)
        self._cards_scroller.set_vexpand(False)
        return self._cards_scroller

    def _make_card(self, attachment: Attachment) -> Gtk.Box:
        """Build one attachment card: icon, name-over-size, remove.

        The filename and the size share a vertical box, so the pair
        moves together: no window width can exile the size to the far
        edge of the editor pane. Middle-ellipsis on the name preserves
        the extension, which is what the ``attachment:`` / ``image::``
        macros are typed against.
        """
        card = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _CARD_SPACING_PX)
        card.set_size_request(_CARD_MIN_WIDTH_PX, -1)
        card.add_css_class(_CARD_CSS_CLASS)

        icon = Gtk.Image.new_from_icon_name(_ATTACHMENT_ICON_NAME)
        icon.set_valign(Gtk.Align.CENTER)
        card.append(icon)

        text_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        text_box.set_hexpand(True)

        name_label = Gtk.Label.new(attachment.filename)
        name_label.set_halign(Gtk.Align.START)
        name_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        text_box.append(name_label)

        size_label = Gtk.Label.new(format_byte_size(attachment.byte_size))
        size_label.set_halign(Gtk.Align.START)
        size_label.add_css_class(_SIZE_CSS_CLASS)
        text_box.append(size_label)

        card.append(text_box)

        remove_button = Gtk.Button.new_from_icon_name(
            _REMOVE_BUTTON_ICON_NAME,
        )
        remove_button.set_tooltip_text(_REMOVE_BUTTON_TOOLTIP)
        remove_button.add_css_class(_REMOVE_BUTTON_CSS_CLASS)
        remove_button.set_valign(Gtk.Align.CENTER)
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
            self._cards_flow.append(self._make_card(attachment))
        self._apply_scroll_cap()

    def _apply_scroll_cap(self) -> None:
        """Cap the scroller at :data:`_VISIBLE_ROWS` measured card rows.

        The cap is derived from the natural height of a real card
        rather than a pixel constant, so a theme or font-size change
        carries into it for free. Measuring on every reload costs one
        measure — noise next to the store round-trip that precedes it.
        The cap does **not** depend on the attachment count: that is
        the whole point, and the empty case leaves the previous cap in
        place because there is nothing to clip.
        """
        first_child = self._cards_flow.get_first_child()
        if first_child is None:
            return
        natural = first_child.measure(Gtk.Orientation.VERTICAL, -1).natural
        self._cards_scroller.set_max_content_height(
            scroll_cap_height(natural, _CARD_SPACING_PX),
        )

    def _clear_cards(self) -> None:
        """Remove every card (GTK 4 has no bulk removal on FlowBox).

        The walk yields the :class:`Gtk.FlowBoxChild` wrappers GTK
        creates implicitly on ``append``, which is exactly what
        ``remove`` takes.
        """
        child = self._cards_flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._cards_flow.remove(child)
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
