"""GTK rendering of a parsed AsciiDoc document into a ``Gtk.TextBuffer``.

This sub-package holds the two modules that turn an
:class:`~asciidoc.ast.Document` into on-screen text: the
:mod:`~ui.note_render.tag_table` (every visual style, defined
exactly once) and the :mod:`~ui.note_render.textbuffer_renderer`
(the buffer builder). They live under ``ui`` because they are the only
consumers that need ``gi`` and ``storage.protocols``; keeping them here
lets :mod:`asciidoc` stay a pure, GTK-free format library.

Both modules are consumed by :mod:`ui.note_view` and
:mod:`ui.link_handler`. The "tag table and note view must not
drift" invariant that used to span packages is now an intra-``ui``
concern.
"""

from __future__ import annotations
