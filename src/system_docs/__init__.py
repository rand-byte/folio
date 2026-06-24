"""Bundled "system documents" and the one gi-free loader that reads them.

Principles & invariants
-----------------------
* This package is the single, config-tier home for the application's
  *system documents*: the seed welcome note inserted on first launch and
  the AsciiDoc help reference (plus the small image the help's
  ``image::`` example demonstrates). They are content the application
  ships, not content the user authors, so they live here as package data
  rather than in the database or the editor grammar bundle.
* The documents are **plain package data, read gi-free** via
  :func:`importlib.resources` — exactly the way ``giruntime/ui/css/app.css``
  ships and is read today. A compiled GResource could only be read back
  through :mod:`gi` (``Gio``), which would lock the content behind the UI
  layer; plain text and a plain image have no such requirement, so they
  skip the gresource entirely. Only the *editor grammar* stays in
  ``folio.gresource`` (it has a hard ``resource:///`` requirement).
* Because the read needs no :mod:`gi`, the **same loader serves both
  layers**: :mod:`storage.migrations` reads :data:`SystemDocument.WELCOME`
  to seed the first-launch note, and :mod:`giruntime.ui.help_window`
  reads :data:`SystemDocument.HELP` and
  :data:`SystemDocument.HELP_DEMO_IMAGE`. ``storage`` stays gi-free by
  construction — it imports this loader, never a widget.
* The package imports only :mod:`enums` (for :class:`SystemDocument`) and
  the standard library. It must **not** import :mod:`gi`, :mod:`storage`,
  or :mod:`giruntime` — keeping it a leaf that any layer above ``enums``
  may depend on, mirroring how ``storage`` already reads ``config``.
* The two readers split by payload, not by document: :func:`load_text`
  decodes UTF-8 for the ``.adoc`` sources, :func:`load_bytes` returns the
  raw bytes for the demo image. Passing a member to the wrong reader is a
  caller bug; the functions do not second-guess the member.
* The files ship in the zipapp with **no** ``build_pyz.py`` change: the
  build archives ``src/`` directly minus ``__pycache__`` / ``test_*.py``
  / the grammar sources, so ``system_docs/*`` (this package's data files)
  rides along like ``app.css`` does. Only this package's own
  ``test_*.py`` files are excluded by the existing filter.
"""

from __future__ import annotations

import importlib.resources

from enums import SystemDocument


_PACKAGE: str = "system_docs"
"""The import-package name this loader reads its data files from.

Passed to :func:`importlib.resources.files` so the lookup resolves
identically from a source checkout (``src/system_docs``) and from inside
the ``folio.pyz`` zipapp (where ``system_docs`` sits at the archive
root). Each :class:`SystemDocument` member's value is the filename joined
onto this package.
"""


def load_text(document: SystemDocument) -> str:
    """Return the UTF-8 text of a system document's ``.adoc`` source.

    Reads the file named by ``document``'s value from the
    ``system_docs`` package via :func:`importlib.resources`. Used for the
    welcome and help sources; passing :data:`SystemDocument.HELP_DEMO_IMAGE`
    (a binary file) is a caller misuse — use :func:`load_bytes` for the
    image.
    """
    return (
        importlib.resources
        .files(_PACKAGE)
        .joinpath(document.value)
        .read_text(encoding="utf-8")
    )


def load_bytes(document: SystemDocument) -> bytes:
    """Return the raw bytes of a system document.

    Reads the file named by ``document``'s value from the
    ``system_docs`` package via :func:`importlib.resources`. Used for the
    help's demo image, whose bytes the help window's
    :data:`storage.protocols.ImageBytesResolver` serves to the renderer.
    """
    return (
        importlib.resources
        .files(_PACKAGE)
        .joinpath(document.value)
        .read_bytes()
    )
