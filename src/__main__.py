"""Entry point so the app can be launched with ``python3 -B src/__main__.py``.

Principles & invariants
-----------------------
* This module's only job is to construct :class:`NotesApplication`,
  forward the process's command-line arguments to its ``run`` method,
  and propagate the return code as the process exit code. There is no
  application logic here — moving any would split "what the app does"
  across two modules.
* The module deliberately does not import anything beyond
  :mod:`sys` and :mod:`ui.application`. Keeping it tiny is
  what lets ``python3 -B src/__main__.py --help`` and similar boot paths
  fail fast with a clear error if a downstream import is broken,
  rather than crashing midway through application startup.
* This file doubles as the zipapp entry point: ``zipapp`` archives the
  contents of ``src/`` (so this file lands at the archive root) and uses
  ``__main__.py`` as the implicit entry, which is why the distributed
  app launches with ``python folio.pyz``. The
  ``if __name__ == "__main__"`` / ``raise SystemExit(main())`` idiom
  below works identically whether the file is run directly from a source
  checkout or from inside the zip.
* :func:`main` returns an :class:`int` (the GTK application's exit
  code) rather than calling :func:`sys.exit` directly. The
  ``raise SystemExit(main())`` form below is the standard idiom and
  makes :func:`main` callable from tests without the test process
  itself exiting.
"""

from __future__ import annotations

import sys

from ui.application import NotesApplication


def main(argv: list[str] | None = None) -> int:
    """Run the application and return its exit code.

    ``argv`` defaults to :data:`sys.argv` when ``None``; tests pass an
    explicit list to avoid depending on the surrounding process
    invocation. The return value is the GTK application's exit code,
    suitable for ``raise SystemExit(...)`` at the call site.
    """
    effective_argv = list(sys.argv) if argv is None else list(argv)
    exit_code: int = NotesApplication().run(effective_argv)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
