"""The display guard: one probe, and a tripwire that makes it mandatory.

Principles & invariants
-----------------------
* **Single home for "can we open a display?"** Every widget-level test
  module gates its cases on :func:`display_available`. The probe lives
  here and nowhere else, so what counts as "a display" cannot drift
  between modules.
* **Skipping is the default, failing is opt-in.** Running the suite by
  hand — against your own session, or with no compositor at all — keeps
  the historical behaviour: display-gated tests skip and the run is
  green. Nothing about a developer's local loop changes.
* **``make test`` opts in.** The recipe exports
  :data:`REQUIRE_DISPLAY_ENV_VAR` because it launches ``weston`` itself
  and therefore *knows* a display should exist. If one does not, the
  compositor failed to start and the 90-odd display-gated test classes
  would skip silently — a green run that proves nothing about the
  widgets. The tripwire below turns that into a named failure.
* **Probing beats inspecting the environment.** ``make test`` exports
  ``WAYLAND_DISPLAY`` unconditionally, before it knows whether weston
  came up, so the variable's presence says nothing. Only opening a
  display answers the question.
* **This module ships nothing.** It is named ``test_*`` so
  ``build_pyz`` excludes it from ``folio.pyz`` and the ``.deb``, which
  is also why the environment-variable constants live here rather than
  in :mod:`enums` — they are test scaffolding, not application
  vocabulary.
"""

from __future__ import annotations

import os
import unittest
from enum import StrEnum
from typing import Final

from gi.repository import Gdk, Gtk

REQUIRE_DISPLAY_ENV_VAR: Final[str] = "FOLIO_REQUIRE_DISPLAY"
"""Environment variable through which ``make test`` demands a display.

Set by the ``test`` recipe alongside ``WAYLAND_DISPLAY``,
``GDK_BACKEND`` and ``GSK_RENDERER``; unset in an ordinary hand-run.
"""


class DisplayRequirement(StrEnum):
    """Values :data:`REQUIRE_DISPLAY_ENV_VAR` is read for.

    Only :attr:`REQUIRED` is acted on — any other value, including the
    variable being absent, leaves the historical skip behaviour in
    place. :attr:`OPTIONAL` exists so a caller can express "explicitly
    not required" without resorting to a bare string.
    """

    REQUIRED = "1"
    OPTIONAL = "0"


def display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction.

    ``Gtk.init_check`` is idempotent and safe to call from every test
    module at import time, which is when the ``skipUnless`` decorators
    that consume this evaluate it.
    """
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def display_is_required() -> bool:
    """True iff this run was told a display must be available."""
    return (
        os.environ.get(REQUIRE_DISPLAY_ENV_VAR)
        == DisplayRequirement.REQUIRED
    )


class DisplayRequirementTests(unittest.TestCase):
    """Fail loudly when a run that demanded a display did not get one."""

    def test_display_is_available_when_required(self) -> None:
        if not display_is_required():
            self.skipTest(
                f"{REQUIRE_DISPLAY_ENV_VAR} is not set to "
                f"{DisplayRequirement.REQUIRED} — display-gated tests may skip"
            )
        self.assertTrue(
            display_available(),
            f"{REQUIRE_DISPLAY_ENV_VAR}="
            f"{DisplayRequirement.REQUIRED} but no GDK display could be "
            "opened, so every widget-level test in this run skipped "
            "silently and the suite proves nothing about the GTK widgets. "
            "Is weston installed and did it start? See dev-environment.md.",
        )


if __name__ == "__main__":
    unittest.main()
