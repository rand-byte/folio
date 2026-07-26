"""The injected one-shot timeout seam shared by the debouncing panes.

Principles & invariants
-----------------------
* Two panes debounce work behind a GLib timer — the editor's auto-save
  (:data:`giruntime.ui.note_editor.AUTOSAVE_DEBOUNCE_MS`) and the note
  list's search filter
  (:data:`giruntime.ui.note_list.SEARCH_DEBOUNCE_MS`). Both schedule
  and cancel through the :data:`TimeoutScheduler` /
  :data:`TimeoutCanceller` callables defined here rather than calling
  :mod:`GLib` directly, so a test can drive the debounce synchronously
  with a fake and never spin a real main loop.
* This module owns the *seam*, not the *delays*. Each pane keeps its
  own debounce interval next to the behaviour it belongs to: the
  intervals have exactly one consumer each, so centralising them would
  widen their audience for no gain (compare
  :mod:`config.defaults`, which by its own contract holds values
  "the app reuses **across modules**").
* It is the single place in the UI layer that names GLib's timeout
  primitives. :data:`TIMEOUT_REMOVE` is re-exported so a callback can
  state "do not re-fire" without every pane importing :mod:`GLib` for
  one constant.
* The two production functions are free functions wrapping
  :func:`GLib.timeout_add` / :func:`GLib.source_remove` rather than the
  GLib callables themselves, so a caller's parameter annotation can be
  the explicit alias — PyGObject's introspected signatures do not
  always satisfy mypy against an arbitrary callable type.
* This module imports :mod:`gi` for :class:`GLib` only. GLib is
  available headless, so importing it costs no display and the module's
  own tests need none.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from gi.repository import GLib


type TimeoutScheduler = Callable[[int, Callable[[], bool]], int]
"""Schedule ``callback`` to run once after ``delay_ms`` ms; return a
cancellable handle.

The callback's :class:`bool` return value follows GLib semantics:
returning :data:`TIMEOUT_REMOVE` (``False``) means "do not re-fire".
The debouncing panes never return :data:`GLib.SOURCE_CONTINUE` — each
debounce cycle is one-shot.

Production wiring: :func:`default_timeout_scheduler`. Test wiring: a
fake that records the call and returns a synthetic integer handle, plus
a ``fire`` helper that invokes the callback synchronously."""

type TimeoutCanceller = Callable[[int], None]
"""Cancel a previously-scheduled :data:`TimeoutScheduler` handle.

Production wiring: :func:`default_timeout_canceller`. Test wiring: a
fake that records the cancelled handle so assertions can verify the
debounce really did cancel before rescheduling."""

TIMEOUT_REMOVE: Final[bool] = GLib.SOURCE_REMOVE
"""Return this from a scheduled callback so the timer does not re-fire.

An alias for :data:`GLib.SOURCE_REMOVE` (``False``), re-exported so a
pane that only needs the constant does not import :mod:`GLib` for it.
"""


def default_timeout_scheduler(
    delay_ms: int,
    callback: Callable[[], bool],
) -> int:
    """Production scheduler — wraps :func:`GLib.timeout_add`."""
    handle: int = GLib.timeout_add(delay_ms, callback)
    return handle


def default_timeout_canceller(handle: int) -> None:
    """Production canceller — wraps :func:`GLib.source_remove`."""
    GLib.source_remove(handle)
