"""Application-wide constants and the seed data used on first launch.

Principles & invariants
-----------------------
* Every numeric or string value the app reuses across modules lives here
  if it is plausibly tunable. This avoids the dual problem of magic
  numbers scattered through implementation files and of a single
  "settings" module that ends up importing half the application.
* ``MAX_ATTACHMENT_BYTES`` is the only quota the storage layer enforces.
  It is checked via :meth:`pathlib.Path.stat` before any bytes are read,
  so an over-limit file never enters memory. Changing the value affects
  in-flight rejections but never invalidates already-stored attachments.
* ``TARGET_CHARS_PER_LINE`` parameterises the rendered-view text column
  width. The pixel width is computed by the UI layer once per font as
  ``TARGET_CHARS_PER_LINE`` × measured glyph width and cached. Changing
  this value is a typography decision, not a runtime tuning knob, so it is
  intentionally not exposed in any settings panel in v1.
* :data:`SEED_NOTEBOOKS` and :data:`SEED_WELCOME_NOTE_SOURCE` are written
  to a fresh database by the v1 migration. They are never re-applied: a
  user who deletes the welcome note must not see it reappear on the next
  launch. The migration's own version-tracking enforces this.
* Seed notebook ids use a stable ``seed-…`` prefix so they remain
  identifiable in the database for diagnostics and so they cannot collide
  with the UUID-shaped ids the repository generates for user-created
  notebooks.
"""

from __future__ import annotations

from notes_app.enums import NotebookIcon
from notes_app.models.notebook import Notebook


# ---------------------------------------------------------------------------
# Tunable numeric constants
# ---------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES: int = 10 * 1024 * 1024
"""Hard upper bound on the size of a single image attachment, in bytes."""

TARGET_CHARS_PER_LINE: int = 66
"""Target text-column width in characters for the rendered view.

The 66-character target follows the typography literature's 45–75 range
for comfortable prose reading. The :class:`ArticleContainer` widget uses
this constant, multiplied by the measured glyph width of the current
font, as the fixed pixel width of the article column.
"""


# ---------------------------------------------------------------------------
# Seed identifiers
# ---------------------------------------------------------------------------

SEED_NOTEBOOK_ID_PERSONAL: str = "seed-personal"
SEED_NOTEBOOK_ID_RECIPES: str = "seed-recipes"
SEED_NOTEBOOK_ID_BAKING: str = "seed-baking"
SEED_NOTEBOOK_ID_WEEKNIGHT: str = "seed-weeknight-dinners"
SEED_NOTEBOOK_ID_TRAVEL: str = "seed-travel"
SEED_NOTEBOOK_ID_LEARNING: str = "seed-learning"
SEED_NOTEBOOK_ID_ARCHIVE: str = "seed-archive"

SEED_WELCOME_NOTE_ID: str = "seed-welcome-note"


# ---------------------------------------------------------------------------
# Seed notebooks (top-level first, then children — preserves the SQL
# insertion order so foreign-key references resolve)
# ---------------------------------------------------------------------------

SEED_NOTEBOOKS: tuple[Notebook, ...] = (
    Notebook(
        id=SEED_NOTEBOOK_ID_PERSONAL,
        name="Personal",
        parent_id=None,
        icon=NotebookIcon.HOME,
    ),
    Notebook(
        id=SEED_NOTEBOOK_ID_RECIPES,
        name="Recipes",
        parent_id=None,
        icon=NotebookIcon.BOOK,
    ),
    Notebook(
        id=SEED_NOTEBOOK_ID_BAKING,
        name="Baking",
        parent_id=SEED_NOTEBOOK_ID_RECIPES,
        icon=NotebookIcon.BOOK,
    ),
    Notebook(
        id=SEED_NOTEBOOK_ID_WEEKNIGHT,
        name="Weeknight dinners",
        parent_id=SEED_NOTEBOOK_ID_RECIPES,
        icon=NotebookIcon.BOOK,
    ),
    Notebook(
        id=SEED_NOTEBOOK_ID_TRAVEL,
        name="Travel",
        parent_id=None,
        icon=NotebookIcon.MAP,
    ),
    Notebook(
        id=SEED_NOTEBOOK_ID_LEARNING,
        name="Learning",
        parent_id=None,
        icon=NotebookIcon.BRAIN,
    ),
    Notebook(
        id=SEED_NOTEBOOK_ID_ARCHIVE,
        name="Archive",
        parent_id=None,
        icon=NotebookIcon.ARCHIVE,
    ),
)


# ---------------------------------------------------------------------------
# Seed welcome note
# ---------------------------------------------------------------------------

SEED_WELCOME_NOTE_NOTEBOOK_ID: str = SEED_NOTEBOOK_ID_PERSONAL
"""Notebook the welcome note is dropped into on a fresh database."""

SEED_WELCOME_NOTE_SOURCE: str = """\
= Welcome to your notes

This is your first note. You can keep it, edit it, or delete it.

Notes are written in *AsciiDoc* — a plain-text format. The toolbar above
gives you formatting buttons for the most common things, but you can also
type the markup directly.

== A few features to try

* Type _italic_ or *bold* text inline
* Mark something as [.line-through]#done# or [.underline]#important#
* Group related ideas under a heading like the one above this list

== Step-by-step lists

. Click *New note* on the toolbar to create a note
. Pick a notebook from the sidebar to organise it
. Use the search box to find any note across the whole library

== Code blocks

----
def hello():
    print("Hello!")
----

Code is rendered verbatim — the editor highlights AsciiDoc itself, but
the rendered view shows the code as you wrote it.

When you are ready to start your own notes, you can safely delete this
one. It will not come back.
"""
