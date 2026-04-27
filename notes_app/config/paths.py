"""Filesystem path resolution for the application's persistent data.

Principles & invariants
-----------------------
* All persistent state lives under a single per-user directory beneath the
  XDG data root. There is no second location the app reads from, so a
  user's entire library can be backed up or migrated by copying that
  directory.
* Every path-returning function is pure with respect to its inputs and
  module-level state — it does not read or mutate any module-level
  variables. The single side effect any helper performs is calling
  :meth:`pathlib.Path.mkdir` with ``parents=True, exist_ok=True`` so the
  directory exists by the time the caller opens a file inside it.
* The XDG override is read from ``os.environ`` exactly once per call. We
  do not cache the value because tests need to vary it via
  :func:`unittest.mock.patch.dict`.
* The fallback when ``XDG_DATA_HOME`` is unset matches the XDG Base
  Directory specification: ``$HOME/.local/share``. Falling back to a
  hardcoded ``/tmp`` or to the working directory would silently lose data
  on uprooted home directories.
"""

from __future__ import annotations

import os
from pathlib import Path


APP_DIRECTORY_NAME: str = "notes-app"
"""The single subdirectory under XDG_DATA_HOME the app owns."""

DATABASE_FILENAME: str = "notes.db"
"""The single SQLite file inside the app directory."""

XDG_DATA_HOME_ENV: str = "XDG_DATA_HOME"
"""Environment variable consulted before falling back to ~/.local/share."""

_XDG_DATA_HOME_FALLBACK: tuple[str, ...] = (".local", "share")
"""Path components, relative to the user's home, used when XDG is unset."""


def data_directory() -> Path:
    """Return the directory the app stores everything under, creating it.

    The directory is ``$XDG_DATA_HOME/notes-app`` if the environment
    variable is set and non-empty, otherwise ``$HOME/.local/share/notes-app``.
    The directory and any missing parents are created on disk before the
    function returns.
    """
    base = _xdg_data_root()
    directory = base / APP_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def database_path() -> Path:
    """Return the absolute path to the SQLite database file.

    The parent directory is guaranteed to exist on return; the database
    file itself may or may not exist (callers are responsible for opening
    or creating it).
    """
    return data_directory() / DATABASE_FILENAME


def _xdg_data_root() -> Path:
    """Return the XDG data root for the current user.

    Honours an explicit, non-empty ``XDG_DATA_HOME``; otherwise falls back
    to the spec-defined ``$HOME/.local/share``. Empty strings count as
    unset, matching the XDG specification.
    """
    override = os.environ.get(XDG_DATA_HOME_ENV)
    if override:
        return Path(override)
    return Path.home().joinpath(*_XDG_DATA_HOME_FALLBACK)
