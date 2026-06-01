"""Mediators between UI events and the domain layer.

Controllers hold no widget references and emit no GTK signals directly.
They own the mutable :class:`AppState` (single source of truth for
selection / mode / query), accept storage repositories by Protocol
injection, and translate user gestures into repository calls plus state
mutations. Widgets subscribe to ``AppState`` via GObject signals — they
never read from another widget.

Modules
-------
* :mod:`controllers.app_state` — the :class:`AppState`
  GObject carrying selection, selected note id, view mode, and query,
  with a payload-free signal per field.
* :mod:`controllers.note_controller` — orchestrates note
  CRUD plus attachment add/remove on top of
  :class:`NoteRepositoryProtocol` and :class:`AttachmentStoreProtocol`.
* :mod:`controllers.notebook_controller` — orchestrates
  notebook CRUD on top of :class:`NotebookRepositoryProtocol`.
* :mod:`controllers._storage_errors` — private helper
  shared by the two controllers above; encapsulates the
  catch-:class:`sqlite3.DatabaseError`-emit-toast-re-raise pattern
  exactly once so the two controllers cannot drift.

Populated by build-order step 7.
"""
