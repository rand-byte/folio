"""Tests for :mod:`giruntime.controllers.note_item`.

:class:`NoteItem` needs no display — it wraps a :class:`Note` in a
:class:`GObject.Object`, and GObject is GLib, not GTK. The surface is
small by design: the wrapped value read back as-is, and three scalar
properties exposed ``READABLE``-only so an edit cannot be applied in
place (the model chain depends on edits arriving as a replace).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from giruntime.controllers.note_item import NoteItem
from models.note import Note


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _note(
    note_id: str = "n1",
    *,
    title: str = "Alpha",
    source: str = "= Alpha\n\nbody\n",
    snippet: str = "body",
    tags: tuple[str, ...] = (),
) -> Note:
    return Note(
        id=note_id,
        title=title,
        source=source,
        snippet=snippet,
        tags=tags,
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW,
    )


class NoteItemExposureTests(unittest.TestCase):
    """The wrapped note is readable as a value and as GObject props."""

    def test_note_property_returns_the_wrapped_value(self) -> None:
        note = _note()
        self.assertIs(NoteItem(note).note, note)

    def test_scalar_properties_mirror_the_note(self) -> None:
        item = NoteItem(_note("n7", title="Beta", snippet="preview"))
        self.assertEqual(item.get_property("note-id"), "n7")
        self.assertEqual(item.get_property("title"), "Beta")
        self.assertEqual(item.get_property("snippet"), "preview")


class NoteItemImmutabilityTests(unittest.TestCase):
    """The scalar properties are READABLE-only, by design."""

    def test_scalar_properties_have_no_setters(self) -> None:
        item = NoteItem(_note())
        for name in ("note-id", "title", "snippet"):
            with self.subTest(prop=name):
                with self.assertRaises(TypeError):
                    item.set_property(name, "mutated")
