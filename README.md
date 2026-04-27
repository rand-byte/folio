# notes-app

A desktop AsciiDoc note-taking application. See `plan.md` for the full
implementation plan.

## Status

Foundation in place — step 1 of the build order:

- `notes_app.enums` — every categorical type the app uses
- `notes_app.models.note` — `Note` dataclass + `derive_title` /
  `derive_snippet` helpers
- `notes_app.models.notebook` — `Notebook` dataclass
- `notes_app.models.attachment` — `Attachment` dataclass (metadata only,
  no BLOB)
- `notes_app.models.parse_error` — `ParseError` exception
- `notes_app.config.paths` — XDG-compliant database path resolution
- `notes_app.config.defaults` — runtime constants and seed library data

No I/O, no GTK, no SQLite at this stage.

## Requirements

- Python 3.13

## Running the tests

From the repository root:

    python -m unittest discover -s notes_app -p 'test_*.py' -v

Tests live next to the modules they cover (e.g.
`notes_app/models/test_note.py`).
