"""Pure filtering and sorting over already-materialised note lists.

The repository pre-filters by notebook in SQL; this layer composes the
result with the live query string and the user's sort preference. Every
function here is pure: it takes a ``list[Note]`` and an injected ``now``
where dates matter, and returns a new list. No I/O, no GTK.

Populated by build-order step 5.
"""
