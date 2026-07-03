"""The :class:`SessionState` dataclass and its default value.

Principles & invariants
------------------------
* :class:`SessionState` is the in-memory shape of everything the app
  restores about the *previous run* on the next launch: the last-open
  note and the main window's last size/maximized state. It is a domain
  value alongside :class:`Note` and :class:`Attachment` — this module
  carries no I/O and no GTK; :mod:`storage.session_state_store` reads
  and writes it to disk, :mod:`giruntime.ui.application` and
  :mod:`giruntime.ui.main_window` consume it.
* The dataclass is frozen, matching every other domain value in
  ``models`` — a restored session is a snapshot, not something a caller
  mutates in place.
* ``window_size`` is ``tuple[int, int] | None`` rather than two separate
  ``int | None`` fields. Width and height are only ever meaningful
  together (there is no such thing as a saved width with no saved
  height), so pairing them in one optional field makes "no saved size
  yet" a single state instead of two fields that could disagree.
  ``None`` means exactly that: no prior run has saved a size, so the
  caller falls back to its own computed default (see
  :mod:`giruntime.ui.main_window`, whose default width is measured from
  the rendered article column and therefore cannot be a constant this
  module could supply).
* ``selected_note_id`` is ``str | None`` for the same reason it already
  is on :class:`AppState`: no note is a real, reachable state (an empty
  library), not an error.
* GTK 4 has no window-position API (Wayland compositors own placement,
  not the app), so there is deliberately no ``window_x`` / ``window_y``
  field here — there is nothing to save.
* :data:`DEFAULT_SESSION_STATE` is the single "nothing was restored"
  value, used both when no state file exists yet (first launch) and
  when a saved one fails to parse. Every consumer compares against or
  falls back to this one constant rather than each inventing its own
  all-``None`` literal, so "no saved state" has exactly one shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class SessionState:
    """Everything restored from the previous run.

    Fields
    ------
    selected_note_id:
        The note that was open when the app last closed, or ``None`` if
        none was selected (an empty library). Restoring it is
        best-effort: the caller is expected to fall back to its normal
        initial-selection logic when the id no longer exists.
    window_size:
        The main window's last ``(width, height)`` in pixels, or
        ``None`` if no prior run has saved one yet.
    window_maximized:
        Whether the main window was maximized when it last closed.
    """

    selected_note_id: str | None
    window_size: tuple[int, int] | None
    window_maximized: bool


DEFAULT_SESSION_STATE: Final[SessionState] = SessionState(
    selected_note_id=None,
    window_size=None,
    window_maximized=False,
)
"""The "nothing restored" value — first launch, or a failed restore."""
