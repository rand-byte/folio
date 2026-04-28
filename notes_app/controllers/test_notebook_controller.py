"""Tests for :mod:`notes_app.controllers.notebook_controller`."""

from __future__ import annotations

import sqlite3
import unittest

from notes_app.controllers.notebook_controller import NotebookController
from notes_app.enums import NotebookIcon
from notes_app.models.notebook import Notebook
from notes_app.storage.protocols import (
    NestingTooDeep,
    NotebookRepositoryProtocol,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CountingIdFactory:
    """Returns ``"nb-1"``, ``"nb-2"``, … on successive calls."""

    _counter: int

    def __init__(self) -> None:
        self._counter = 0

    def __call__(self) -> str:
        self._counter += 1
        return f"nb-{self._counter}"


class _FakeNotebookRepository:
    """In-memory :class:`NotebookRepositoryProtocol` implementation.

    Honours the two-level depth rule (raises :class:`NestingTooDeep`
    when a notebook's proposed parent already has a parent of its
    own) so the controller's catch-and-emit branch is exercised by
    real behaviour rather than mocked exceptions.
    """

    notebooks: dict[str, Notebook]
    notes_reparented: list[tuple[str, str]]
    fail_next_insert: BaseException | None
    fail_next_rename: BaseException | None
    fail_next_set_icon: BaseException | None
    fail_next_delete: BaseException | None

    def __init__(self) -> None:
        self.notebooks = {}
        self.notes_reparented = []
        self.fail_next_insert = None
        self.fail_next_rename = None
        self.fail_next_set_icon = None
        self.fail_next_delete = None

    def list_all(self) -> list[Notebook]:
        return list(self.notebooks.values())

    def get(self, notebook_id: str) -> Notebook:
        return self.notebooks[notebook_id]

    def insert(self, notebook: Notebook) -> None:
        if self.fail_next_insert is not None:
            failure = self.fail_next_insert
            self.fail_next_insert = None
            raise failure
        if notebook.parent_id is not None:
            parent = self.notebooks[notebook.parent_id]
            if parent.parent_id is not None:
                raise NestingTooDeep(
                    f"notebook {notebook.id!r} parent already has a parent",
                )
        self.notebooks[notebook.id] = notebook

    def rename(self, notebook_id: str, new_name: str) -> None:
        if self.fail_next_rename is not None:
            failure = self.fail_next_rename
            self.fail_next_rename = None
            raise failure
        existing = self.notebooks[notebook_id]
        self.notebooks[notebook_id] = Notebook(
            id=existing.id,
            name=new_name,
            parent_id=existing.parent_id,
            icon=existing.icon,
        )

    def set_icon(self, notebook_id: str, icon: NotebookIcon) -> None:
        if self.fail_next_set_icon is not None:
            failure = self.fail_next_set_icon
            self.fail_next_set_icon = None
            raise failure
        existing = self.notebooks[notebook_id]
        self.notebooks[notebook_id] = Notebook(
            id=existing.id,
            name=existing.name,
            parent_id=existing.parent_id,
            icon=icon,
        )

    def delete_and_reparent_notes(
        self,
        notebook_id: str,
        target_id: str,
    ) -> None:
        if self.fail_next_delete is not None:
            failure = self.fail_next_delete
            self.fail_next_delete = None
            raise failure
        # Sanity check matches the real repo's first-line guard.
        if target_id not in self.notebooks:
            raise KeyError(target_id)
        self.notes_reparented.append((notebook_id, target_id))
        # Promote any child notebooks of the doomed parent to top-level.
        for nb_id, nb in list(self.notebooks.items()):
            if nb.parent_id == notebook_id:
                self.notebooks[nb_id] = Notebook(
                    id=nb.id,
                    name=nb.name,
                    parent_id=None,
                    icon=nb.icon,
                )
        del self.notebooks[notebook_id]


class _SignalRecorder:
    """Records every signal a :class:`NotebookController` fires."""

    events: list[tuple[str, tuple[object, ...]]]

    def __init__(self, controller: NotebookController) -> None:
        self.events = []
        controller.connect("notebooks-changed", self._on_notebooks_changed)
        controller.connect("nesting-too-deep", self._on_nesting_too_deep)
        controller.connect("storage-error", self._on_storage_error)

    def _on_notebooks_changed(self, _obj: NotebookController) -> None:
        self.events.append(("notebooks-changed", ()))

    def _on_nesting_too_deep(self, _obj: NotebookController) -> None:
        self.events.append(("nesting-too-deep", ()))

    def _on_storage_error(
        self,
        _obj: NotebookController,
        message: str,
    ) -> None:
        self.events.append(("storage-error", (message,)))

    def names(self) -> list[str]:
        return [event[0] for event in self.events]

    def first_payload_str(self, signal: str) -> str:
        """Return the first :class:`str`-payloaded event's first arg.

        ``storage-error`` is the only string-payloaded signal here;
        callers use this helper to keep mypy from widening the
        ``tuple[object, ...]`` payload to ``object`` at the
        assertion site.
        """
        for name, args in self.events:
            if name == signal:
                payload = args[0]
                if isinstance(payload, str):
                    return payload
                raise TypeError(
                    f"signal {signal!r} payload was {type(payload).__name__}, not str"
                )
        raise AssertionError(f"no {signal!r} event recorded")


def _make_controller(
    *,
    repository: _FakeNotebookRepository | None = None,
    id_factory: _CountingIdFactory | None = None,
) -> tuple[NotebookController, _FakeNotebookRepository, _CountingIdFactory]:
    repo = repository if repository is not None else _FakeNotebookRepository()
    ids = id_factory if id_factory is not None else _CountingIdFactory()
    controller = NotebookController(repository=repo, id_factory=ids)
    return controller, repo, ids


# ---------------------------------------------------------------------------
# create_notebook
# ---------------------------------------------------------------------------


class CreateNotebookTests(unittest.TestCase):
    def test_creates_top_level_notebook(self) -> None:
        controller, repo, _ = _make_controller()
        notebook = controller.create_notebook(
            name="Recipes",
            parent_id=None,
            icon=NotebookIcon.BOOK,
        )
        self.assertIsNotNone(notebook)
        assert notebook is not None  # for mypy
        self.assertEqual(notebook.id, "nb-1")
        self.assertEqual(notebook.name, "Recipes")
        self.assertIsNone(notebook.parent_id)
        self.assertIs(notebook.icon, NotebookIcon.BOOK)
        self.assertIn("nb-1", repo.notebooks)

    def test_creates_child_under_top_level(self) -> None:
        controller, repo, _ = _make_controller()
        controller.create_notebook(
            name="Recipes",
            parent_id=None,
            icon=NotebookIcon.BOOK,
        )
        child = controller.create_notebook(
            name="Baking",
            parent_id="nb-1",
            icon=NotebookIcon.BOOK,
        )
        self.assertIsNotNone(child)
        assert child is not None  # for mypy
        self.assertEqual(child.parent_id, "nb-1")
        self.assertEqual(repo.notebooks["nb-2"].parent_id, "nb-1")

    def test_emits_notebooks_changed(self) -> None:
        controller, _, _ = _make_controller()
        recorder = _SignalRecorder(controller)
        controller.create_notebook(
            name="Travel",
            parent_id=None,
            icon=NotebookIcon.MAP,
        )
        self.assertEqual(recorder.names(), ["notebooks-changed"])

    def test_nested_too_deep_emits_signal_and_returns_none(self) -> None:
        controller, repo, _ = _make_controller()
        # Build top-level + child; attempt grandchild = NestingTooDeep.
        controller.create_notebook(
            name="Top",
            parent_id=None,
            icon=NotebookIcon.FOLDER,
        )
        controller.create_notebook(
            name="Child",
            parent_id="nb-1",
            icon=NotebookIcon.FOLDER,
        )
        recorder = _SignalRecorder(controller)
        result = controller.create_notebook(
            name="Grandchild",
            parent_id="nb-2",
            icon=NotebookIcon.FOLDER,
        )
        self.assertIsNone(result)
        # Only the rejection signal fired; no notebooks-changed.
        self.assertEqual(recorder.names(), ["nesting-too-deep"])
        # The grandchild was NOT inserted into the repo.
        self.assertEqual(set(repo.notebooks), {"nb-1", "nb-2"})

    def test_nested_too_deep_does_not_propagate_exception(self) -> None:
        controller, _, _ = _make_controller()
        controller.create_notebook(
            name="Top",
            parent_id=None,
            icon=NotebookIcon.FOLDER,
        )
        controller.create_notebook(
            name="Child",
            parent_id="nb-1",
            icon=NotebookIcon.FOLDER,
        )
        # The bare call would raise NestingTooDeep at the repository
        # boundary; the controller's job is to swallow that and
        # return None instead.
        result = controller.create_notebook(
            name="Grandchild",
            parent_id="nb-2",
            icon=NotebookIcon.FOLDER,
        )
        self.assertIsNone(result)

    def test_database_error_emits_and_reraises(self) -> None:
        controller, repo, _ = _make_controller()
        repo.fail_next_insert = sqlite3.OperationalError("disk full")
        recorder = _SignalRecorder(controller)
        with self.assertRaises(sqlite3.OperationalError):
            controller.create_notebook(
                name="Recipes",
                parent_id=None,
                icon=NotebookIcon.BOOK,
            )
        self.assertEqual(recorder.names(), ["storage-error"])
        # No notebooks-changed because the row never landed.
        self.assertNotIn("notebooks-changed", recorder.names())
        # Toast message format matches the controller's verb-led
        # convention.
        self.assertIn(
            "create notebook",
            recorder.first_payload_str("storage-error"),
        )


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


class RenameNotebookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _FakeNotebookRepository()
        self.repo.notebooks["nb-1"] = Notebook(
            id="nb-1",
            name="Old name",
            parent_id=None,
            icon=NotebookIcon.HOME,
        )

    def test_renames_notebook(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        controller.rename("nb-1", "New name")
        self.assertEqual(self.repo.notebooks["nb-1"].name, "New name")

    def test_emits_notebooks_changed(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        recorder = _SignalRecorder(controller)
        controller.rename("nb-1", "New name")
        self.assertEqual(recorder.names(), ["notebooks-changed"])

    def test_id_and_other_fields_preserved(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        controller.rename("nb-1", "New name")
        # Renames do NOT change the id (notes still attach correctly)
        # nor the icon.
        self.assertEqual(self.repo.notebooks["nb-1"].id, "nb-1")
        self.assertIs(self.repo.notebooks["nb-1"].icon, NotebookIcon.HOME)

    def test_unknown_id_raises_key_error(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        with self.assertRaises(KeyError):
            controller.rename("nope", "anything")

    def test_database_error_emits_and_reraises(self) -> None:
        controller, repo, _ = _make_controller(repository=self.repo)
        repo.fail_next_rename = sqlite3.OperationalError("locked")
        recorder = _SignalRecorder(controller)
        with self.assertRaises(sqlite3.OperationalError):
            controller.rename("nb-1", "New")
        self.assertEqual(recorder.names(), ["storage-error"])


# ---------------------------------------------------------------------------
# set_icon
# ---------------------------------------------------------------------------


class SetIconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _FakeNotebookRepository()
        self.repo.notebooks["nb-1"] = Notebook(
            id="nb-1",
            name="Recipes",
            parent_id=None,
            icon=NotebookIcon.BOOK,
        )

    def test_changes_icon(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        controller.set_icon("nb-1", NotebookIcon.STAR)
        self.assertIs(self.repo.notebooks["nb-1"].icon, NotebookIcon.STAR)

    def test_emits_notebooks_changed(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        recorder = _SignalRecorder(controller)
        controller.set_icon("nb-1", NotebookIcon.HEART)
        self.assertEqual(recorder.names(), ["notebooks-changed"])

    def test_unknown_id_raises_key_error(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        with self.assertRaises(KeyError):
            controller.set_icon("nope", NotebookIcon.STAR)

    def test_database_error_emits_and_reraises(self) -> None:
        controller, repo, _ = _make_controller(repository=self.repo)
        repo.fail_next_set_icon = sqlite3.OperationalError("locked")
        recorder = _SignalRecorder(controller)
        with self.assertRaises(sqlite3.OperationalError):
            controller.set_icon("nb-1", NotebookIcon.STAR)
        self.assertEqual(recorder.names(), ["storage-error"])


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class DeleteNotebookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _FakeNotebookRepository()
        self.repo.notebooks["nb-recipes"] = Notebook(
            id="nb-recipes",
            name="Recipes",
            parent_id=None,
            icon=NotebookIcon.BOOK,
        )
        self.repo.notebooks["nb-archive"] = Notebook(
            id="nb-archive",
            name="Archive",
            parent_id=None,
            icon=NotebookIcon.ARCHIVE,
        )

    def test_calls_repository_with_target(self) -> None:
        controller, repo, _ = _make_controller(repository=self.repo)
        controller.delete("nb-recipes", "nb-archive")
        self.assertEqual(
            repo.notes_reparented,
            [("nb-recipes", "nb-archive")],
        )
        self.assertNotIn("nb-recipes", repo.notebooks)

    def test_emits_notebooks_changed(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        recorder = _SignalRecorder(controller)
        controller.delete("nb-recipes", "nb-archive")
        self.assertEqual(recorder.names(), ["notebooks-changed"])

    def test_promotes_children_to_top_level(self) -> None:
        # Defence-in-depth: ensure that the controller delegates to
        # the repository (which is responsible for promoting child
        # notebooks). The fake's promotion logic mirrors the real
        # one — selecting that against this test pins both layers.
        self.repo.notebooks["nb-baking"] = Notebook(
            id="nb-baking",
            name="Baking",
            parent_id="nb-recipes",
            icon=NotebookIcon.BOOK,
        )
        controller, repo, _ = _make_controller(repository=self.repo)
        controller.delete("nb-recipes", "nb-archive")
        self.assertIsNone(repo.notebooks["nb-baking"].parent_id)

    def test_unknown_target_raises_key_error(self) -> None:
        controller, _, _ = _make_controller(repository=self.repo)
        with self.assertRaises(KeyError):
            controller.delete("nb-recipes", "does-not-exist")

    def test_database_error_emits_and_reraises(self) -> None:
        controller, repo, _ = _make_controller(repository=self.repo)
        repo.fail_next_delete = sqlite3.OperationalError("locked")
        recorder = _SignalRecorder(controller)
        with self.assertRaises(sqlite3.OperationalError):
            controller.delete("nb-recipes", "nb-archive")
        self.assertEqual(recorder.names(), ["storage-error"])


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class ConstructionTests(unittest.TestCase):
    def test_keyword_only_construction(self) -> None:
        controller, _, _ = _make_controller()
        self.assertIsInstance(controller, NotebookController)

    def test_default_id_factory_yields_unique_ids(self) -> None:
        # The default UUID-based factory is deterministically unique
        # — two consecutive create_notebook calls yield distinct ids.
        repo = _FakeNotebookRepository()
        controller = NotebookController(repository=repo)
        a = controller.create_notebook(
            name="A",
            parent_id=None,
            icon=NotebookIcon.HOME,
        )
        b = controller.create_notebook(
            name="B",
            parent_id=None,
            icon=NotebookIcon.HOME,
        )
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        assert a is not None and b is not None  # for mypy
        self.assertNotEqual(a.id, b.id)

    def test_default_id_factory_uses_notebook_prefix(self) -> None:
        # Pinning the prefix matters because the seed notebooks use
        # ``seed-…`` ids; a future refactor that drops or renames
        # the prefix would silently let user-created and seed
        # notebooks collide.
        repo = _FakeNotebookRepository()
        controller = NotebookController(repository=repo)
        nb = controller.create_notebook(
            name="A",
            parent_id=None,
            icon=NotebookIcon.HOME,
        )
        assert nb is not None  # for mypy
        self.assertTrue(nb.id.startswith("notebook-"))

    def test_protocol_assignment_compatible(self) -> None:
        # Sanity that the fake satisfies the protocol the controller
        # expects — a structural-typing check that runs at import
        # time of the test, exposing signature drift early.
        repo: NotebookRepositoryProtocol = _FakeNotebookRepository()
        controller = NotebookController(repository=repo)
        self.assertIsInstance(controller, NotebookController)


if __name__ == "__main__":
    unittest.main()
