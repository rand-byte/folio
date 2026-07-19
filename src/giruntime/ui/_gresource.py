"""Access to the compiled ``folio.gresource`` bundle, by named subtree.

Principles & invariants
-----------------------
* ``folio.gresource`` is a single **generated artifact**
  (``src/giruntime/ui/folio.gresource``, gitignored) compiled by
  ``glib-compile-resources`` from the committed ``folio.gresource.xml``
  manifest. Every entry point (``run``, ``make test``, ``make pyz``)
  builds it before launch. It bundles more than one thing this
  application needs at runtime — today the GtkSourceView grammar
  (:mod:`giruntime.ui.note_editor`) and the application icon
  (:mod:`giruntime.ui.application`) — each published under its own
  ``resource://`` subtree, named by :class:`enums.GResourceSubtree`.
* :func:`resource_path` is the **only** way a caller obtains a path
  into the bundle, and registration is not a separate step a caller
  can forget: the function registers the compiled bytes as a
  process-global :class:`Gio.Resource` (idempotent — a module-level
  guard makes every call after the first a no-op) and only then
  returns the requested subtree's path. A caller cannot end up with a
  path whose contents were never registered, because nothing in this
  module hands out a path any other way — a bare "ensure registered"
  call with no return value would let a caller separately hardcode a
  path and register-and-look-up could silently drift out of order.
* A missing ``folio.gresource`` is a hard :class:`FileNotFoundError` —
  never a silent fallback to "no grammar" / "no icon". The fix is
  always "build the resource first" (``make resource`` / ``./run``).
"""

from __future__ import annotations

import importlib.resources

from gi.repository import Gio, GLib

from enums import GResourceSubtree

_GRESOURCE_PACKAGE: str = "giruntime.ui"
"""Package the compiled bundle ships next to, read via :mod:`importlib.resources`."""

_GRESOURCE_NAME: str = "folio.gresource"
"""File name of the compiled GResource bundle within :data:`_GRESOURCE_PACKAGE`."""

_GRESOURCE_REGISTERED: bool = False
"""Module-level guard so registration happens at most once per process.

Mirrors the memoisation :mod:`giruntime.ui.note_editor` previously did
itself via its cached ``LanguageManager`` — the guard now lives here so
every caller shares the one registration instead of each module needing
its own cache.
"""


def resource_path(subtree: GResourceSubtree) -> str:
    """Register the compiled bundle if needed, then return ``subtree``'s path.

    This is the single entry point into the bundle: obtaining a path
    is what triggers registration, so a caller cannot look up a
    ``resource://`` location whose bytes were never loaded into the
    process. Registration itself runs at most once per process — the
    first call pays the cost of reading and registering the bundle;
    every later call (including ones for a different ``subtree``)
    just returns the requested value.

    A missing bundle raises :class:`FileNotFoundError` via
    :meth:`read_bytes` — the intended failure mode: a hard, obvious
    error pointing at "build the resource first" rather than a silent
    fallback with some resources missing.
    """
    _ensure_registered()
    return subtree.value


def _ensure_registered() -> None:
    """Register the compiled GResource bundle, exactly once per process.

    Private: :func:`resource_path` is the only supported way to reach
    the bundle's contents, so registration is not exposed on its own —
    see the module docstring for why that matters.
    """
    global _GRESOURCE_REGISTERED  # pylint: disable=global-statement
    if _GRESOURCE_REGISTERED:
        return
    # ``GLib.Bytes.new`` is provided by the gi metaclass.
    blob = GLib.Bytes.new(
        importlib.resources.files(_GRESOURCE_PACKAGE)
        .joinpath(_GRESOURCE_NAME)
        .read_bytes()
    )
    Gio.resources_register(Gio.Resource.new_from_data(blob))
    _GRESOURCE_REGISTERED = True
