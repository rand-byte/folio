"""JSON-file-backed implementation of :class:`SessionStateProtocol`.

Principles & invariants
------------------------
* The session state lives in a single plain JSON file
  (:func:`config.paths.session_state_path`), not GSettings: the app
  ships as an installer-less zipapp with no step that compiles and
  installs a GSettings schema, and a plain file keeps the state inside
  the one directory :func:`config.paths.data_directory` promises holds
  everything a user's install needs to back up.
* :meth:`SessionStateStore.load` **never raises**. A missing file (first
  launch), an unreadable one (``OSError`` — permissions, a directory in
  the way, etc.), and a malformed one (invalid JSON, or valid JSON with
  a shape or types that do not match :class:`SessionState`) all resolve
  to :data:`models.session_state.DEFAULT_SESSION_STATE`. Losing saved
  window geometry or the last-open note is a minor inconvenience, never
  worth blocking startup over — this mirrors how
  :meth:`giruntime.ui.application.NotesApplication._select_initial_note`
  already treats a missing welcome note as "fall back", not "crash".
* Malformed content is still a **parsing error**, not silently accepted
  data: :func:`_state_from_json` raises :class:`_SessionStateParseError`
  for any JSON value that does not decode to the exact shape a
  :class:`SessionState` needs (wrong top-level type, missing key, wrong
  field type, an unknown/missing ``"version"``). ``load`` is the single
  place that turns that raised error into "use the default" — the
  parser itself never coerces or guesses.
* :meth:`SessionStateStore.save` writes atomically: the new content
  lands in a sibling ``.tmp`` file first, then :func:`os.replace` swaps
  it into place in one filesystem operation. A crash or power loss
  mid-write therefore either leaves the previous file intact or the new
  one complete — never a half-written file for the next launch to choke
  on.
* The on-disk shape carries a top-level ``"version"`` field
  (:data:`_SCHEMA_VERSION`) from day one, the same append-only-migration
  spirit :mod:`storage.migrations` uses for the database, so a future
  field addition or rename has a place to hang a version bump rather
  than silently misreading an older file. ``load`` treats any version
  other than the one this module writes as unparsable (see above) —
  there is only one version so far, so there is nothing yet to migrate
  *from*, only a documented seam for when there is.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from models.session_state import DEFAULT_SESSION_STATE, SessionState


_SCHEMA_VERSION: Final[int] = 1
"""On-disk schema version this module reads and writes.

Bump alongside a migration in :func:`_state_from_json` whenever the
on-disk shape changes; a file written with a different version is
treated as unparsable rather than guessed at.
"""

_VERSION_KEY: Final[str] = "version"
_SELECTED_NOTE_ID_KEY: Final[str] = "selected_note_id"
_WINDOW_WIDTH_KEY: Final[str] = "window_width"
_WINDOW_HEIGHT_KEY: Final[str] = "window_height"
_WINDOW_MAXIMIZED_KEY: Final[str] = "window_maximized"


class _SessionStateParseError(ValueError):
    """Raised by :func:`_state_from_json` for any syntactically-valid
    JSON value that is not a well-formed session-state document.

    Purely internal: :meth:`SessionStateStore.load` is the only catcher,
    and it never lets this type escape to its own caller.
    """


class SessionStateStore:
    """Reads and writes :class:`SessionState` as JSON at a fixed path."""

    _path: Path

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> SessionState:
        """Return the persisted state, or :data:`DEFAULT_SESSION_STATE`
        if the file is missing, unreadable, or malformed. Never raises."""
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except OSError:
            return DEFAULT_SESSION_STATE
        try:
            return _state_from_json(raw_text)
        except (json.JSONDecodeError, _SessionStateParseError):
            return DEFAULT_SESSION_STATE

    def save(self, state: SessionState) -> None:
        """Persist ``state``, replacing whatever was previously saved.

        Writes to a sibling ``.tmp`` file and swaps it into place with
        :func:`os.replace`, so a write that is interrupted partway
        through never corrupts the previously-saved file.
        """
        payload = _state_to_json(state)
        tmp_path = self._path.with_name(f"{self._path.name}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self._path)


def _state_from_json(raw_text: str) -> SessionState:
    """Parse ``raw_text`` into a :class:`SessionState`.

    Raises :class:`json.JSONDecodeError` for text that is not valid
    JSON, and :class:`_SessionStateParseError` for JSON that is valid
    but does not decode to the exact document shape this module writes
    (wrong top-level type, a missing or wrong-version ``"version"``, a
    missing key, or a field of the wrong type). Never coerces a
    close-but-wrong value into a guessed one.
    """
    document = json.loads(raw_text)
    if not isinstance(document, dict):
        raise _SessionStateParseError(
            f"expected a JSON object, got {type(document).__name__}"
        )

    version = document.get(_VERSION_KEY)
    if version != _SCHEMA_VERSION:
        raise _SessionStateParseError(
            f"unsupported {_VERSION_KEY!r}: {version!r}"
        )

    selected_note_id = _require_str_or_none(document, _SELECTED_NOTE_ID_KEY)
    window_size = _require_window_size(document)
    window_maximized = _require_bool(document, _WINDOW_MAXIMIZED_KEY)

    return SessionState(
        selected_note_id=selected_note_id,
        window_size=window_size,
        window_maximized=window_maximized,
    )


def _require_str_or_none(document: dict[str, object], key: str) -> str | None:
    if key not in document:
        raise _SessionStateParseError(f"missing key: {key!r}")
    value = document[key]
    if value is not None and not isinstance(value, str):
        raise _SessionStateParseError(
            f"{key!r} must be a string or null, got {type(value).__name__}"
        )
    return value


def _require_bool(document: dict[str, object], key: str) -> bool:
    if key not in document:
        raise _SessionStateParseError(f"missing key: {key!r}")
    value = document[key]
    if not isinstance(value, bool):
        raise _SessionStateParseError(
            f"{key!r} must be a boolean, got {type(value).__name__}"
        )
    return value


def _require_window_size(document: dict[str, object]) -> tuple[int, int] | None:
    # Width and height are stored as two sibling keys (plain JSON has no
    # tuple type) but only ever accepted or rejected together: either
    # both are present as positive integers, or both are absent — one
    # present without the other is malformed, not a partial size.
    has_width = _WINDOW_WIDTH_KEY in document
    has_height = _WINDOW_HEIGHT_KEY in document
    if not has_width and not has_height:
        return None
    if has_width != has_height:
        raise _SessionStateParseError(
            f"{_WINDOW_WIDTH_KEY!r} and {_WINDOW_HEIGHT_KEY!r} must be "
            "both present or both absent"
        )
    width = _require_positive_int(document, _WINDOW_WIDTH_KEY)
    height = _require_positive_int(document, _WINDOW_HEIGHT_KEY)
    return (width, height)


def _require_positive_int(document: dict[str, object], key: str) -> int:
    value = document[key]
    # ``bool`` is a subclass of ``int`` in Python; excluding it keeps
    # ``true``/``false`` from silently parsing as ``1``/``0``.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _SessionStateParseError(
            f"{key!r} must be an integer, got {type(value).__name__}"
        )
    if value <= 0:
        raise _SessionStateParseError(f"{key!r} must be positive, got {value}")
    return value


def _state_to_json(state: SessionState) -> str:
    """Serialise ``state`` to the on-disk JSON document shape."""
    document: dict[str, object] = {
        _VERSION_KEY: _SCHEMA_VERSION,
        _SELECTED_NOTE_ID_KEY: state.selected_note_id,
        _WINDOW_MAXIMIZED_KEY: state.window_maximized,
    }
    if state.window_size is not None:
        width, height = state.window_size
        document[_WINDOW_WIDTH_KEY] = width
        document[_WINDOW_HEIGHT_KEY] = height
    return json.dumps(document, indent=2)
