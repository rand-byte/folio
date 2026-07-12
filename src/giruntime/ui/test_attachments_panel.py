"""Tests for :mod:`ui.attachments_panel`.

The panel builds its card list eagerly into a plain :class:`Gtk.Box`,
so the tests inspect children directly — no window needs presenting.
Construction follows the editor suite's shape: a fake attachment
store, a fake file-dialog opener driven synchronously, and a real
:class:`NoteController` over an in-memory store.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from gi.repository import Gdk, Gtk

from enums import AttachmentExportFailureReason, AttachmentRejectionReason
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui.attachments_panel import AttachmentsPanel
from models.attachment import Attachment
from models.note import Note
from storage.protocols import AttachmentExportFailed, AttachmentRejected


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for any
    :class:`Gtk.Widget` subclass construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def _make_note(note_id: str) -> Note:
    return Note(
        id=note_id,
        title="Hello",
        source="= Hello\n\nbody.\n",
        snippet="body.",
        tags=(),
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeNoteRepository:
    """Minimal repository: the panel never writes notes."""

    notes: dict[str, Note]

    def __init__(self) -> None:
        self.notes = {}

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]

    def list_modified_since(self, _since: datetime) -> list[Note]:
        raise NotImplementedError

    def list_all(self) -> list[Note]:
        return list(self.notes.values())

    def search(self, _query: str) -> list[Note]:
        raise NotImplementedError

    def insert(self, _note: Note) -> Note:
        raise NotImplementedError

    def update_source(
        self,
        _note_id: str,
        _source: str,
        _modified_at: datetime,
    ) -> Note:
        raise NotImplementedError

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return ()


class _FakeAttachmentStore:
    """Configurable :class:`AttachmentStoreProtocol` fake.

    Holds attachments in memory; ``add_for_note`` appends one whose
    filename echoes the source path's name (or raises
    :class:`AttachmentRejected` when :attr:`reject_with` is set), and
    ``list_for_note`` / ``remove`` operate over the same dict, so the
    panel's refresh-after-mutation path is observable end-to-end.
    ``get_bytes`` fails loudly — the panel must never pull BLOBs.
    """

    attachments: dict[str, Attachment]
    reject_with: AttachmentRejectionReason | None
    list_calls: list[str]
    next_id: int

    def __init__(self) -> None:
        self.attachments = {}
        self.reject_with = None
        self.list_calls = []
        self.next_id = 1

    def seed(
        self,
        note_id: str,
        filename: str,
        byte_size: int,
    ) -> Attachment:
        attachment = Attachment(
            id=f"att-{self.next_id}",
            note_id=note_id,
            filename=filename,
            byte_size=byte_size,
        )
        self.next_id += 1
        self.attachments[attachment.id] = attachment
        return attachment

    def add_for_note(self, note_id: str, source_path: Path) -> Attachment:
        if self.reject_with is not None:
            raise AttachmentRejected(self.reject_with)
        return self.seed(note_id, source_path.name, byte_size=42)

    def remove(self, attachment_id: str) -> None:
        del self.attachments[attachment_id]

    def list_for_note(self, note_id: str) -> list[Attachment]:
        self.list_calls.append(note_id)
        return [
            a for a in self.attachments.values() if a.note_id == note_id
        ]

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError

    def count_for_note(self, note_id: str) -> int:
        return len(self.list_for_note(note_id))

    def export_to(self, attachment_id: str, destination: Path) -> None:
        """Write the attachment's bytes out (the outbound mirror of add)."""
        try:
            data = self.get_bytes(attachment_id)
        except KeyError as exc:
            raise AttachmentExportFailed(
                AttachmentExportFailureReason.UNKNOWN_ATTACHMENT,
            ) from exc
        try:
            destination.write_bytes(data)
        except OSError as exc:
            raise AttachmentExportFailed(
                AttachmentExportFailureReason.DESTINATION_UNWRITABLE,
            ) from exc


class _FakeFileDialogOpener:
    """Synchronous stand-in for :data:`FileDialogOpener`.

    Captures the most recent callback so tests can drive the
    post-pick code path explicitly: :meth:`deliver` with a
    :class:`Path` simulates a successful pick, ``deliver(None)`` a
    cancellation. Until then the click is "in flight" and the test
    can assert on the intermediate state.
    """

    open_calls: list[Gtk.Widget]
    pending_callback: Callable[[Path | None], None] | None

    def __init__(self) -> None:
        self.open_calls = []
        self.pending_callback = None

    def __call__(
        self,
        parent: Gtk.Widget,
        on_result: Callable[[Path | None], None],
    ) -> None:
        self.open_calls.append(parent)
        self.pending_callback = on_result

    def deliver(self, path: Path | None) -> None:
        callback = self.pending_callback
        if callback is None:
            raise AssertionError(
                "FakeFileDialogOpener.deliver() called with no pending callback"
            )
        self.pending_callback = None
        callback(path)


# ---------------------------------------------------------------------------
# Fixture builder + widget-walking helpers
# ---------------------------------------------------------------------------


def _build_panel(
    *,
    select_note: bool = True,
) -> tuple[
    AttachmentsPanel,
    _FakeAttachmentStore,
    _FakeFileDialogOpener,
    NoteController,
    AppState,
]:
    repo = _FakeNoteRepository()
    repo.notes["n1"] = _make_note("n1")
    repo.notes["n2"] = _make_note("n2")
    attachment_store = _FakeAttachmentStore()
    state = AppState()
    if select_note:
        state.set_selected_note_id("n1")
    note_store = NoteListStore(repository=repo)
    note_store.load()
    controller = NoteController(
        note_store=note_store,
        attachments=attachment_store,
        app_state=state,
    )
    opener = _FakeFileDialogOpener()
    panel = AttachmentsPanel(
        note_controller=controller,
        app_state=state,
        attachments=attachment_store,
        file_dialog_opener=opener,
    )
    return panel, attachment_store, opener, controller, state


def _card_boxes(panel: AttachmentsPanel) -> list[Gtk.Box]:
    cards: list[Gtk.Box] = []
    child = panel._cards_box.get_first_child()
    while child is not None:
        assert isinstance(child, Gtk.Box)
        cards.append(child)
        child = child.get_next_sibling()
    return cards


def _card_labels(card: Gtk.Box) -> list[str]:
    """The text of every label on a card, left to right."""
    labels: list[str] = []
    child = card.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            labels.append(child.get_text())
        child = child.get_next_sibling()
    return labels


def _card_remove_button(card: Gtk.Box) -> Gtk.Button:
    child = card.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            return child
        child = child.get_next_sibling()
    raise AssertionError("no remove button on the card")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class AttachmentsPanelRenderingTests(unittest.TestCase):
    def test_renders_one_card_per_attachment(self) -> None:
        panel, store, _, _, state = _build_panel(select_note=False)
        store.seed("n1", "photo.png", byte_size=1024)
        store.seed("n1", "report.pdf", byte_size=2048)
        store.seed("n2", "other.txt", byte_size=1)
        state.set_selected_note_id("n1")

        cards = _card_boxes(panel)
        self.assertEqual(len(cards), 2)
        # Only n1's attachments are listed.
        self.assertEqual(_card_labels(cards[0])[0], "photo.png")
        self.assertEqual(_card_labels(cards[1])[0], "report.pdf")

    def test_header_shows_count(self) -> None:
        panel, store, _, _, state = _build_panel(select_note=False)
        store.seed("n1", "a.png", byte_size=1)
        store.seed("n1", "b.png", byte_size=1)
        state.set_selected_note_id("n1")
        self.assertEqual(panel._header_label.get_text(), "ATTACHMENTS · 2")

    def test_card_shows_human_readable_size(self) -> None:
        panel, store, _, _, state = _build_panel(select_note=False)
        store.seed("n1", "big.bin", byte_size=int(2.3 * 1024 * 1024))
        state.set_selected_note_id("n1")
        cards = _card_boxes(panel)
        self.assertEqual(len(cards), 1)
        self.assertIn("2.3 MB", _card_labels(cards[0]))

    def test_zero_attachments_shows_header_and_add_only(self) -> None:
        # Selected note with no attachments: panel visible, header
        # reads "· 0", no cards, Add button present.
        panel, _, _, _, _ = _build_panel()
        self.assertTrue(panel.get_visible())
        self.assertEqual(panel._header_label.get_text(), "ATTACHMENTS · 0")
        self.assertEqual(_card_boxes(panel), [])
        self.assertTrue(panel._add_button.get_visible())

    def test_hidden_when_no_note_selected(self) -> None:
        panel, _, _, _, _ = _build_panel(select_note=False)
        self.assertFalse(panel.get_visible())

    def test_becomes_visible_on_selection(self) -> None:
        panel, _, _, _, state = _build_panel(select_note=False)
        state.set_selected_note_id("n1")
        self.assertTrue(panel.get_visible())

    def test_hides_again_when_selection_clears(self) -> None:
        panel, _, _, _, state = _build_panel()
        state.set_selected_note_id(None)
        self.assertFalse(panel.get_visible())

    def test_selection_change_reloads_from_the_new_note(self) -> None:
        panel, store, _, _, state = _build_panel()
        store.seed("n2", "two.png", byte_size=1)
        state.set_selected_note_id("n2")
        cards = _card_boxes(panel)
        self.assertEqual(len(cards), 1)
        self.assertEqual(_card_labels(cards[0])[0], "two.png")
        self.assertEqual(panel._header_label.get_text(), "ATTACHMENTS · 1")

    def test_missing_attachment_store_lists_empty(self) -> None:
        # ``attachments=None`` follows the note list / view contract:
        # the panel renders the empty state instead of raising.
        repo = _FakeNoteRepository()
        repo.notes["n1"] = _make_note("n1")
        state = AppState()
        state.set_selected_note_id("n1")
        note_store = NoteListStore(repository=repo)
        note_store.load()
        controller = NoteController(
            note_store=note_store,
            attachments=_FakeAttachmentStore(),
            app_state=state,
        )
        panel = AttachmentsPanel(
            note_controller=controller,
            app_state=state,
            attachments=None,
            file_dialog_opener=_FakeFileDialogOpener(),
        )
        self.assertTrue(panel.get_visible())
        self.assertEqual(panel._header_label.get_text(), "ATTACHMENTS · 0")
        self.assertEqual(_card_boxes(panel), [])


# ---------------------------------------------------------------------------
# Add flow
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class AttachmentsPanelAddTests(unittest.TestCase):
    def test_add_button_opens_the_dialog(self) -> None:
        panel, _, opener, _, _ = _build_panel()
        panel._add_button.emit("clicked")
        self.assertEqual(len(opener.open_calls), 1)
        self.assertIs(opener.open_calls[0], panel)

    def test_successful_pick_routes_to_add_attachment_and_refreshes(
        self,
    ) -> None:
        panel, store, opener, _, _ = _build_panel()
        panel._add_button.emit("clicked")
        opener.deliver(Path("/tmp/notes.pdf"))

        # The store gained the attachment for the selected note …
        stored = list(store.attachments.values())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].note_id, "n1")
        self.assertEqual(stored[0].filename, "notes.pdf")
        # … and the panel refreshed: one card, header count bumped.
        cards = _card_boxes(panel)
        self.assertEqual(len(cards), 1)
        self.assertEqual(_card_labels(cards[0])[0], "notes.pdf")
        self.assertEqual(panel._header_label.get_text(), "ATTACHMENTS · 1")

    def test_cancelled_pick_changes_nothing(self) -> None:
        panel, store, opener, _, _ = _build_panel()
        panel._add_button.emit("clicked")
        opener.deliver(None)
        self.assertEqual(store.attachments, {})
        self.assertEqual(_card_boxes(panel), [])

    def test_rejected_add_adds_no_card(self) -> None:
        panel, store, opener, controller, _ = _build_panel()
        store.reject_with = AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT
        rejections: list[AttachmentRejectionReason] = []
        controller.connect(
            "attachment-rejected",
            lambda _c, reason: rejections.append(reason),
        )

        panel._add_button.emit("clicked")
        opener.deliver(Path("/tmp/huge.bin"))

        # No card, count unchanged; the toast layer got its signal.
        self.assertEqual(_card_boxes(panel), [])
        self.assertEqual(panel._header_label.get_text(), "ATTACHMENTS · 0")
        self.assertEqual(
            rejections,
            [AttachmentRejectionReason.EXCEEDS_SIZE_LIMIT],
        )

    def test_click_with_no_selection_does_not_open_dialog(self) -> None:
        # The panel is hidden in this state, but a programmatic
        # ``emit("clicked")`` bypasses visibility; the handler must
        # still bail rather than opening a dialog with no note id.
        panel, _, opener, _, _ = _build_panel(select_note=False)
        panel._add_button.emit("clicked")
        self.assertEqual(opener.open_calls, [])

    def test_selection_clearing_during_dialog_drops_the_pick(self) -> None:
        # The dialog is asynchronous in production. Between opening
        # and the user picking, the selection might clear (e.g. the
        # displayed note was deleted). The post-pick handler must bail
        # rather than attaching against a stale id.
        panel, store, opener, _, state = _build_panel()
        panel._add_button.emit("clicked")
        state.set_selected_note_id(None)
        opener.deliver(Path("/tmp/photo.png"))
        self.assertEqual(store.attachments, {})


# ---------------------------------------------------------------------------
# Remove flow
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class AttachmentsPanelRemoveTests(unittest.TestCase):
    def test_remove_routes_to_controller_and_refreshes(self) -> None:
        panel, store, _, _, state = _build_panel(select_note=False)
        keep = store.seed("n1", "keep.png", byte_size=1)
        drop = store.seed("n1", "drop.png", byte_size=1)
        state.set_selected_note_id("n1")

        cards = _card_boxes(panel)
        drop_card = next(
            c for c in cards if _card_labels(c)[0] == "drop.png"
        )
        _card_remove_button(drop_card).emit("clicked")

        # Gone from the store, panel refreshed to the surviving card.
        self.assertNotIn(drop.id, store.attachments)
        self.assertIn(keep.id, store.attachments)
        cards = _card_boxes(panel)
        self.assertEqual(len(cards), 1)
        self.assertEqual(_card_labels(cards[0])[0], "keep.png")
        self.assertEqual(panel._header_label.get_text(), "ATTACHMENTS · 1")


# ---------------------------------------------------------------------------
# attachments-changed subscription
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class AttachmentsPanelSignalTests(unittest.TestCase):
    def test_attachments_changed_for_selected_note_reloads(self) -> None:
        # Another observer mutates the selected note's attachments
        # through the controller; the panel must pick it up via the
        # signal even though the panel itself made no call.
        panel, store, _, controller, _ = _build_panel()
        self.assertEqual(_card_boxes(panel), [])
        attachment = controller.add_attachment("n1", Path("/tmp/elsewhere.png"))
        self.assertIsNotNone(attachment)
        cards = _card_boxes(panel)
        self.assertEqual(len(cards), 1)
        self.assertEqual(_card_labels(cards[0])[0], "elsewhere.png")
        # Direct store mutation + signal also reloads (remove path).
        assert attachment is not None
        controller.remove_attachment(attachment.id, attachment.note_id)
        self.assertEqual(_card_boxes(panel), [])
        self.assertEqual(store.attachments, {})

    def test_attachments_changed_for_other_note_is_ignored(self) -> None:
        panel, store, _, controller, _ = _build_panel()
        store.list_calls.clear()
        controller.add_attachment("n2", Path("/tmp/other.png"))
        # No reload happened: the panel never re-listed, and no card
        # for the other note's attachment leaked in.
        self.assertEqual(store.list_calls, [])
        self.assertEqual(_card_boxes(panel), [])


if __name__ == "__main__":
    unittest.main()
