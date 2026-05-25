"""AsciiDoc lexer, parser, AST, and summary — a pure format library.

The ``lexer``, ``inline_parser``, ``parser``, ``ast``, and ``summary``
sub-modules are pure: they turn text into an AST (and the AST into a
note-list summary) with no GTK and no storage dependency. The package
imports only ``enums``, ``models``, and ``config``, which makes it
reusable by any non-view consumer — the note-list summary today, a
future export or structure-aware index tomorrow.

The GTK ``TextBuffer`` renderer and its tag table, which used to live
here as the package's only ``gi`` consumers, now live under
:mod:`notes_app.ui.note_render`; the GtkSourceView editor grammar lives
under :mod:`notes_app.ui`. Nothing in this package imports ``gi`` or
``storage``.
"""

from __future__ import annotations
