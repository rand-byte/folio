"""Builds the shared :class:`Gtk.TextTagTable` used by the rendered view.

Principles & invariants
-----------------------
* This module is the single owner of every styling tag that appears in
  the rendered view. Other modules apply tags by *name* (looking them up
  on the table) so the visual definitions live in exactly one place. A
  tweak to "what bold looks like" is one edit here, not a hunt across
  the renderer.
* Tag *names* are exposed as :class:`TagName` enum members. The renderer
  and tests reference :data:`TagName.BOLD` rather than the string
  ``"bold"`` — the style rule against magic strings applies inside this
  package as much as anywhere else.
* Tags are scoped to the **AsciiDoc subset implemented up to this
  build step**: bold, italic, strikethrough, underline, monospace,
  link, plus one tag per heading level produced by the parser. The
  parser produces a level-0 heading (``Document.title``) and levels
  2–6 (``Section.level``); level 1 is intentionally absent because
  the parser rejects mid-document level-1 headings as
  ``UNKNOWN_BLOCK``. Step 13 added ``MONOSPACE`` and ``LINK``;
  later build steps may extend the table further.
* All sizing for headings is expressed via ``scale`` (a multiplier on
  the inherited font size) rather than absolute point sizes. This keeps
  the user's font preferences and OS accessibility settings composable —
  a user with a larger base font sees proportionally larger headings
  without any extra wiring.
* :func:`build_tag_table` returns a fresh :class:`Gtk.TextTagTable` on
  every call. Tag tables can only be associated with one
  :class:`Gtk.TextBuffer` at a time in some situations, and a fresh
  instance per buffer avoids accidental cross-buffer aliasing in tests.
* This module imports ``gi`` because the tag table *is* a GTK object —
  there is no useful pure-Python representation of a tag. The renderer
  is the only other place in :mod:`notes_app.asciidoc` that imports
  ``gi``; everything else stays display-agnostic.
"""

from __future__ import annotations

from enum import StrEnum

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk, Pango  # noqa: E402


class TagName(StrEnum):
    """Names of every shared tag the rendered-view tag table contains.

    Values are the strings the underlying :class:`Gtk.TextTag` carries
    as its ``name`` property — :meth:`Gtk.TextTagTable.lookup` accepts a
    plain :class:`str`, so each enum member is usable directly as the
    lookup key.

    The heading members map to AST levels: :data:`HEADING_0` is the
    document title, and :data:`HEADING_2` … :data:`HEADING_6` are
    section levels. There is no :data:`HEADING_1` member because the
    parser does not produce level-1 headings outside of the document
    title — a mid-document ``=`` heading is rejected as an
    ``UNKNOWN_BLOCK``.

    :data:`MONOSPACE` and :data:`LINK` provide the *visual* styling
    for those constructs. The link's *URL identity* is carried by
    a separate, anonymous (unnamed) :class:`Gtk.TextTag` per link,
    managed by the renderer — only one shared :data:`LINK` tag
    appears in this table because every link looks the same.
    """

    BOLD = "bold"
    ITALIC = "italic"
    STRIKETHROUGH = "strikethrough"
    UNDERLINE = "underline"
    MONOSPACE = "monospace"
    LINK = "link"
    HEADING_0 = "heading_0"
    HEADING_2 = "heading_2"
    HEADING_3 = "heading_3"
    HEADING_4 = "heading_4"
    HEADING_5 = "heading_5"
    HEADING_6 = "heading_6"


# ---------------------------------------------------------------------------
# Scale multipliers for heading levels
# ---------------------------------------------------------------------------
#
# Indexed by AST heading level. The numbers follow a roughly geometric
# progression that matches typical web typography: the document title is
# 2× the body size, h2 is 1.6×, and h6 lands just at body size with bold
# weight to differentiate it from running text. Level 1 is absent on
# purpose (see TagName).

_HEADING_SCALES: dict[int, float] = {
    0: 2.0,
    2: 1.6,
    3: 1.4,
    4: 1.2,
    5: 1.1,
    6: 1.0,
}


def heading_tag_name(level: int) -> TagName:
    """Return the :class:`TagName` for a given heading level.

    Raises :class:`KeyError` for levels the parser never produces (1, or
    anything outside 0..6) — a misuse from the renderer's side that
    deserves to fail loudly rather than silently fall back to a default.
    """
    return _LEVEL_TO_TAG_NAME[level]


_LEVEL_TO_TAG_NAME: dict[int, TagName] = {
    0: TagName.HEADING_0,
    2: TagName.HEADING_2,
    3: TagName.HEADING_3,
    4: TagName.HEADING_4,
    5: TagName.HEADING_5,
    6: TagName.HEADING_6,
}


# ---------------------------------------------------------------------------
# Visual constants for monospace and link styling
# ---------------------------------------------------------------------------
#
# These are not exposed as enum values because they describe *visual*
# settings rather than categorical concepts — there's no closed set of
# legal monospace families or link colours, only one current choice
# each. They live as module constants so a one-line edit changes the
# look across every rendered note.

_MONOSPACE_FAMILY: str = "monospace"

# A blue close to the GTK Adwaita "accent" colour. Encoded as a CSS-style
# RGB string because :class:`Gtk.TextTag`'s ``foreground`` property
# accepts that form directly. If the renderer ever gains a dark-mode
# variant we'll switch this to a callable that picks per-theme.
_LINK_FOREGROUND: str = "#1a73e8"


def build_tag_table() -> Gtk.TextTagTable:
    """Construct the rendered-view tag table for the current subset.

    The returned table contains exactly one tag per :class:`TagName`
    member. Tag names are unique within a table, so callers that need
    a tag by name use :meth:`Gtk.TextTagTable.lookup` with the
    corresponding :class:`TagName` value.

    Note that link *identity* (which URL each link points at) is
    carried by a separate, anonymous :class:`Gtk.TextTag` per link,
    added to the table at render time and tracked by the renderer.
    The shared :data:`TagName.LINK` tag in this table only contributes
    the visual appearance — colour and underline — that every link
    shares.
    """
    table = Gtk.TextTagTable.new()
    table.add(_make_inline_tag(TagName.BOLD, weight=Pango.Weight.BOLD))
    table.add(_make_inline_tag(TagName.ITALIC, style=Pango.Style.ITALIC))
    table.add(_make_inline_tag(TagName.STRIKETHROUGH, strikethrough=True))
    table.add(_make_inline_tag(TagName.UNDERLINE, underline=Pango.Underline.SINGLE))
    table.add(_make_inline_tag(TagName.MONOSPACE, family=_MONOSPACE_FAMILY))
    table.add(
        _make_inline_tag(
            TagName.LINK,
            foreground=_LINK_FOREGROUND,
            underline=Pango.Underline.SINGLE,
        )
    )
    for level, scale in _HEADING_SCALES.items():
        table.add(_make_heading_tag(_LEVEL_TO_TAG_NAME[level], scale=scale))
    return table


def _make_inline_tag(  # pylint: disable=too-many-arguments
    name: TagName,
    *,
    weight: Pango.Weight | None = None,
    style: Pango.Style | None = None,
    strikethrough: bool | None = None,
    underline: Pango.Underline | None = None,
    family: str | None = None,
    foreground: str | None = None,
) -> Gtk.TextTag:
    """Build a single inline-style tag with the requested visual rule.

    Only the property the caller passes is set; the rest are left at
    their inherited defaults so multiple tags on the same range
    compose without one tag erasing another's contribution. This is
    why ``LINK`` (foreground + underline) and ``UNDERLINE`` (just
    underline) coexist cleanly when both apply to the same range.

    The argument list grows one element each time we add an inline
    construct, which is unavoidable: each is a distinct
    :class:`Gtk.TextTag` property. Refactoring to a single ``props``
    mapping would lose the type-checked keyword surface — and there
    are only six properties total in the closed AsciiDoc subset, so
    the explicit list stays readable.
    """
    tag = Gtk.TextTag.new(name.value)
    if weight is not None:
        tag.set_property("weight", weight)
    if style is not None:
        tag.set_property("style", style)
    if strikethrough is not None:
        tag.set_property("strikethrough", strikethrough)
    if underline is not None:
        tag.set_property("underline", underline)
    if family is not None:
        tag.set_property("family", family)
    if foreground is not None:
        tag.set_property("foreground", foreground)
    return tag


def _make_heading_tag(name: TagName, *, scale: float) -> Gtk.TextTag:
    """Build a heading-style tag: bold weight at the given scale."""
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("weight", Pango.Weight.BOLD)
    tag.set_property("scale", scale)
    return tag
