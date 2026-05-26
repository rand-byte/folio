"""Build folio.pyz from the src/ tree via zipapp's API filter.

Principles & invariants
-----------------------
* The archive is created directly from ``src/``; there is no staging copy, so
  the only omitted files are those :func:`_included` rejects.
* Omitted: ``__pycache__`` at any depth, ``test_*.py``, and the grammar
  *sources* (``language_spec.lang``, ``folio.gresource.xml``) that the compiled
  ``folio.gresource`` supersedes at runtime. Everything else (incl. the compiled
  GResource and ``css/*.css``) is shipped.
* ``__main__.py`` already sits at the archive root (it is ``src/__main__.py``),
  so :func:`zipapp.create_archive` uses it as the implicit entry point and
  ``main=`` is deliberately *not* passed — passing it would conflict with the
  existing ``__main__.py``.
* This is build tooling, not shipped code: it lives at the repo root rather than
  under ``src/`` so it is neither swept by the rename nor archived into the zip.
  It is still a non-test source file, so it is covered by the same mypy / pylint
  runs as the rest of the tree (see the ``PY_SRC`` glob in the ``Makefile``).
"""
from __future__ import annotations

import sys
import zipapp
from pathlib import Path

_GRAMMAR_SOURCES = frozenset({"language_spec.lang", "folio.gresource.xml"})


def _included(path: Path) -> bool:
    if "__pycache__" in path.parts:
        return False
    if path.name.startswith("test_") and path.suffix == ".py":
        return False
    return path.name not in _GRAMMAR_SOURCES


def main(source: str, target: str, interpreter: str) -> int:
    zipapp.create_archive(source, target, interpreter=interpreter, filter=_included)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
