"""GTK/GObject-dependent layers — the one place GI versions are pinned.

Principles & invariants
-----------------------
* This package contains every module that imports ``gi`` at runtime
  (``giruntime.ui`` and ``giruntime.controllers``). All other layers are
  GI-free and live at the source root.
* ``gi.require_version`` is called here, exactly once per process, and
  nowhere else. Importing any submodule runs this ``__init__`` first
  (Python imports a package before its submodules), so the versions are
  pinned before any ``from gi.repository import …`` executes — on the app
  entry path and on every test that imports a ``giruntime.*`` module.
* This module pins versions only; it must not import a ``gi.repository``
  namespace, so merely importing the package does not load typelibs.
"""
from __future__ import annotations

import gi

gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GtkSource", "5")
