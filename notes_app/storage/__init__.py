"""SQLite-backed persistence for notes, notebooks, and attachments.

Everything the application needs to outlive a process — note bodies,
notebook hierarchy, and image BLOBs — is held in a single SQLite file
under the XDG data directory. Concrete repository classes implement the
``Protocol`` interfaces in :mod:`notes_app.storage.protocols`, which are
the only types ``controllers/`` and ``ui/`` import from this layer.

Populated by build-order steps 2 (protocols), 3 (database, migrations,
note and notebook repositories), and 11 (attachment store).
"""
