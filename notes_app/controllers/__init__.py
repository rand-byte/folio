"""Mediators between UI events and the domain layer.

Controllers hold no widget references and emit no GTK signals directly.
They own the mutable :class:`AppState` (single source of truth for
selection / mode / query), accept storage repositories by Protocol
injection, and translate user gestures into repository calls plus state
mutations. Widgets subscribe to ``AppState`` via GObject signals — they
never read from another widget.

Populated by build-order step 7.
"""
