"""AsciiDoc lexer, parser, AST, and TextBuffer renderer.

The ``lexer``, ``inline_parser``, ``parser``, and ``ast`` sub-modules are
pure (text → AST, no GTK, no storage). The ``textbuffer_renderer`` is the
one place in this package that imports ``gi`` because it has to populate a
``Gtk.TextBuffer`` and instantiate child widgets — it still does no I/O,
since image bytes are supplied by an injected resolver.

Populated by build-order steps 4 (core grammar), 6 (renderer), 13 (links
and monospace), 14 (tables), and 15 (admonitions and blockquotes).
"""
