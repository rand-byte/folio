"""Install the ``src/`` tree into the package's private data directory.

Principles & invariants
-----------------------
* This is the Meson counterpart of :mod:`build_pyz`: the ``.deb`` installs the
  *same* files the zipapp archives, so **"what ships" has exactly one
  definition** — :func:`build_pyz._included`. This script imports that predicate
  rather than restating it; ``meson``'s ``install_subdir`` cannot express the
  ``test_*.py`` exclusion, which is why an install script exists at all.
* One extra exclusion applies here and not to the zipapp: the **compiled
  GResource bundle is a build output, never a source file**. The zipapp copies
  the in-tree bundle that ``make resource`` produced; Meson compiles its own in
  the build directory and installs it to the same in-package location, so
  copying a source-tree bundle over it would install a stale artifact. Compiled
  bundles are therefore skipped wholesale (see :data:`_BUILD_OUTPUTS`).
* The destination layout is a **byte-for-byte mirror** of ``src/``: the runtime
  loads its data as in-package resources (``importlib.resources`` from
  ``giruntime.ui`` / ``system_docs``), so relative paths inside the tree must
  survive installation exactly.
* :mod:`build_pyz` lives at the repo root, so ``meson.build`` runs this script
  with ``PYTHONPATH`` set to the source root — that keeps the import a plain
  top-level one, with no ``sys.path`` surgery here.
* Meson runs install scripts with ``MESON_SOURCE_ROOT`` and
  ``MESON_INSTALL_DESTDIR_PREFIX`` in the environment; the destination is
  derived from the latter so staged installs (``DESTDIR=…``, as
  ``dh_auto_install`` performs) land in the staging tree, not on the host.
* The script **adds to** the install directory, never clears it: Meson runs
  install scripts *after* its own install steps, so the compiled GResource is
  already sitting in there by the time this runs. Removing the destination
  first would delete it.
* Build tooling, not shipped code — it lives outside ``src/`` and is never
  installed. It is still covered by ``make type`` / ``make lint``.
"""
from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

from build_pyz import _included

_SOURCE_ROOT_VAR = "MESON_SOURCE_ROOT"
_DESTDIR_PREFIX_VAR = "MESON_INSTALL_DESTDIR_PREFIX"

_BUILD_OUTPUTS = frozenset({".gresource"})
"""Suffixes of generated artifacts Meson installs itself (never copied)."""


def _environment_path(name: str) -> Path:
    value = os.environ.get(name)
    if value is None:
        raise SystemExit(f"{name} is not set: this script must be run by meson")
    return Path(value)


def _shippable(source_root: Path, path: Path) -> bool:
    if path.suffix in _BUILD_OUTPUTS:
        return False
    return _included(path.relative_to(source_root))


def _sources(tree: Path) -> Iterable[Path]:
    return (path for path in sorted(tree.rglob("*")) if path.is_file())


def main(source_subdir: str, pkgdatadir: str) -> int:
    if Path(pkgdatadir).is_absolute():
        raise SystemExit(f"pkgdatadir must be relative to the prefix: {pkgdatadir}")

    tree = _environment_path(_SOURCE_ROOT_VAR) / source_subdir
    destination = _environment_path(_DESTDIR_PREFIX_VAR) / pkgdatadir

    for source in _sources(tree):
        if not _shippable(tree, source):
            continue
        target = destination / source.relative_to(tree)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
