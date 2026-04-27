"""GTK 4 widgets — the only layer that imports ``gi``.

UI modules wire the three-pane layout (sidebar, note list, note pane)
plus the toolbar and status bar. They contain no business logic: every
data operation goes through ``controllers/``, and every storage operation
goes through the repository ``Protocol`` interfaces. Widgets are thin and
unit-testable with fake controllers.

Populated by build-order steps 8 (skeleton main window + note view), 9
(sidebar + note list), 10 (editor), 11 (image flow), 12 (toolbar +
dialogs), and 16 (link handler).
"""
