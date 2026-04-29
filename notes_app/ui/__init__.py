"""GTK 4 widgets — the only layer that imports ``gi``.

UI modules wire the three-pane layout (sidebar, note list, note pane)
plus the toolbar and status bar. They contain no business logic: every
data operation goes through ``controllers/``, and every storage operation
goes through the repository ``Protocol`` interfaces. Widgets are thin and
unit-testable with fake controllers.

Modules
-------
* :mod:`notes_app.ui.application` (step 8) — the
  :class:`Gtk.Application` subclass that opens the database, runs
  migrations, builds repositories and :class:`AppState`, and presents
  the main window.
* :mod:`notes_app.ui.main_window` (step 8 stub; rewritten step 9) —
  the single top-level :class:`Gtk.ApplicationWindow`. Today it hosts
  one :class:`NoteView`; step 9 turns it into the three-pane shell.
* :mod:`notes_app.ui.note_view` (step 8) — the rendered-note pane.
  Owns :class:`ArticleContainer` (the fixed-width column that absorbs
  wide-window slack as side margins) and the :class:`Gtk.ScrolledWindow`
  + read-only :class:`Gtk.TextView` stack the renderer populates.

Build steps still to populate this layer: 9 (sidebar + note list +
real three-pane shell), 10 (editor with ``GtkSourceView``), 11 (image
flow against the real attachment store), 12 (toolbar + dialogs), and
16 (link handler).
"""
