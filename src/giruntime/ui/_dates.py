"""Locale-independent date-formatting helpers shared across UI widgets.

Principles & invariants
-----------------------
* This module is the single home of the app's human-facing date
  formatting. Two sibling widgets render dates — the note-list row meta
  line (:mod:`ui.note_list`) and the rendered-view metadata line
  (:mod:`ui.note_view`) — and both import from here rather than from
  each other, so neither widget depends on the other's presentation
  helpers.
* Formatting is deliberately **locale-independent**: the month names
  come from the fixed :data:`_MONTH_ABBREVIATIONS` tuple, not from the
  C locale. This keeps the rendered strings stable across machines and
  test environments. Localisation is a future polish item, and when it
  lands it lands here, once.
* Two formats are provided. :func:`format_date_short` (``"Apr 14"``) is
  the compact note-list meta form; :func:`format_date_long`
  (``"Apr 14, 2026"``) adds the year for the rendered-view metadata
  line, where the extra context is wanted. Both share the same month
  table so a change to the month spelling is one edit.
* Pure functions — no GTK, no clock. Each takes a single
  :class:`datetime.datetime` and returns a :class:`str`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

_MONTH_ABBREVIATIONS: Final[tuple[str, ...]] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
"""Three-letter month abbreviations, indexed ``month - 1``.

The single source of month spelling for every date the UI renders.
"""


def format_date_short(value: datetime) -> str:
    """Return a short, locale-independent date string like ``Apr 14``."""
    return f"{_MONTH_ABBREVIATIONS[value.month - 1]} {value.day}"


def format_date_long(value: datetime) -> str:
    """Return a long, locale-independent date string like ``Apr 14, 2026``.

    Same month/day form as :func:`format_date_short`, with the year
    appended after a comma. Used by the rendered-view metadata line,
    where the year adds useful context the compact note-list form
    omits.
    """
    return (
        f"{_MONTH_ABBREVIATIONS[value.month - 1]} {value.day}, {value.year}"
    )
