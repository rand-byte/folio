"""GTK 4 widgets — the only layer that imports ``gi``.

UI modules wire the three-pane layout (sidebar, note list, note pane)
plus the toolbar and status bar. They contain no business logic: every
data operation goes through ``controllers/``, and every storage operation
goes through the repository ``Protocol`` interfaces. Widgets are thin and
unit-testable with fake controllers.

Modules
-------
* :mod:`ui.application` (step 8; rewritten step 10) — the
  :class:`Gtk.Application` subclass that opens the database, runs
  migrations, builds repositories, :class:`AppState`, and the
  :class:`NoteController` the editor depends on, then presents the
  main window.
* :mod:`ui.main_window` (step 8 stub; rewritten step 9;
  rewritten step 10) — the single top-level :class:`Gtk.ApplicationWindow`.
  Composes sidebar, note list, and a :class:`Gtk.Stack` switching
  between :class:`NoteView` and :class:`NoteEditor` keyed on
  :attr:`AppState.view_mode`.
* :mod:`ui.note_view` (step 8) — the rendered-note pane.
  Owns :class:`ArticleContainer` (the fixed-width column that absorbs
  wide-window slack as side margins) and the :class:`Gtk.ScrolledWindow`
  + read-only :class:`Gtk.TextView` stack the renderer populates.
* :mod:`ui.sidebar` (step 9) — the library / notebooks tree
  on the left.
* :mod:`ui.note_list` (step 9) — the middle pane listing
  notes for the current selection / smart filter.
* :mod:`ui.note_editor` (step 10) — the source editor.
  GtkSourceView 5 over the bundled ``notes-asciidoc`` language, a
  toolbar exposing the step-4 core constructs (heading / bold /
  italic / strikethrough / underline / lists / code block / image
  macro), and a debounced auto-save that flushes through
  :meth:`NoteController.update_source` 300 ms after the user pauses.

Build steps still to populate this layer: 11 (image flow against the
real attachment store), 12 (toolbar + dialogs), and 16 (link handler).
"""
