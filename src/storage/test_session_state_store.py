"""Tests for :mod:`storage.session_state_store`."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from models.session_state import DEFAULT_SESSION_STATE, SessionState
from storage.session_state_store import SessionStateStore


class LoadMissingOrUnreadableTests(unittest.TestCase):
    """A missing or unreadable file resolves to the default — never
    raises."""

    def test_missing_file_returns_default(self) -> None:
        with TemporaryDirectory() as tmp_str:
            store = SessionStateStore(Path(tmp_str) / "state.json")
            self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_path_pointing_at_a_directory_returns_default(self) -> None:
        # Reading a directory as a file raises ``OSError`` (specifically
        # ``IsADirectoryError``), which ``load`` must also swallow.
        with TemporaryDirectory() as tmp_str:
            directory_as_path = Path(tmp_str) / "a-directory"
            directory_as_path.mkdir()
            store = SessionStateStore(directory_as_path)
            self.assertEqual(store.load(), DEFAULT_SESSION_STATE)


class LoadMalformedContentTests(unittest.TestCase):
    """Any syntactically- or semantically-invalid content resolves to
    the default — never raises, never coerces a guess."""

    def _store_with_content(self, raw_text: str) -> SessionStateStore:
        # See storage/test_attachment_store.py's _TempAttachmentDir for
        # why consider-using-with is a false positive here: the temp
        # dir must outlive this helper (the returned store reads from
        # it later), so cleanup is deferred to the TestCase.
        # pylint: disable-next=consider-using-with
        tmp_dir = TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        path = Path(tmp_dir.name) / "state.json"
        path.write_text(raw_text, encoding="utf-8")
        return SessionStateStore(path)

    def test_empty_file_returns_default(self) -> None:
        store = self._store_with_content("")
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_invalid_json_returns_default(self) -> None:
        store = self._store_with_content("{not: valid json")
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_json_array_instead_of_object_returns_default(self) -> None:
        store = self._store_with_content("[1, 2, 3]")
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_missing_version_returns_default(self) -> None:
        document = {
            "selected_note_id": None,
            "window_maximized": False,
        }
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_unknown_version_returns_default(self) -> None:
        document = {
            "version": 999,
            "selected_note_id": None,
            "window_maximized": False,
        }
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_missing_selected_note_id_key_returns_default(self) -> None:
        document = {"version": 1, "window_maximized": False}
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_wrong_type_selected_note_id_returns_default(self) -> None:
        document = {
            "version": 1,
            "selected_note_id": 42,
            "window_maximized": False,
        }
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_wrong_type_window_maximized_returns_default(self) -> None:
        document = {
            "version": 1,
            "selected_note_id": None,
            "window_maximized": "yes",
        }
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_width_without_height_returns_default(self) -> None:
        # Width and height are only ever accepted together.
        document = {
            "version": 1,
            "selected_note_id": None,
            "window_maximized": False,
            "window_width": 1200,
        }
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_non_integer_window_width_returns_default(self) -> None:
        document = {
            "version": 1,
            "selected_note_id": None,
            "window_maximized": False,
            "window_width": "wide",
            "window_height": 800,
        }
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_boolean_window_width_returns_default(self) -> None:
        # ``bool`` is a subclass of ``int`` in Python; ``true``/``false``
        # must not silently parse as ``1``/``0``.
        document = {
            "version": 1,
            "selected_note_id": None,
            "window_maximized": False,
            "window_width": True,
            "window_height": 800,
        }
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)

    def test_non_positive_window_width_returns_default(self) -> None:
        document = {
            "version": 1,
            "selected_note_id": None,
            "window_maximized": False,
            "window_width": 0,
            "window_height": 800,
        }
        store = self._store_with_content(json.dumps(document))
        self.assertEqual(store.load(), DEFAULT_SESSION_STATE)


class RoundTripTests(unittest.TestCase):
    """``save`` then ``load`` reproduces the same value."""

    def _store(self, tmp_str: str) -> SessionStateStore:
        return SessionStateStore(Path(tmp_str) / "state.json")

    def test_round_trips_full_state(self) -> None:
        with TemporaryDirectory() as tmp_str:
            store = self._store(tmp_str)
            state = SessionState(
                selected_note_id="n1",
                window_size=(1234, 987),
                window_maximized=True,
            )
            store.save(state)
            self.assertEqual(store.load(), state)

    def test_round_trips_no_selection_and_no_size(self) -> None:
        with TemporaryDirectory() as tmp_str:
            store = self._store(tmp_str)
            state = SessionState(
                selected_note_id=None,
                window_size=None,
                window_maximized=False,
            )
            store.save(state)
            self.assertEqual(store.load(), state)

    def test_save_overwrites_a_previous_value(self) -> None:
        with TemporaryDirectory() as tmp_str:
            store = self._store(tmp_str)
            store.save(
                SessionState(
                    selected_note_id="n1",
                    window_size=(1000, 700),
                    window_maximized=False,
                )
            )
            second = SessionState(
                selected_note_id="n2",
                window_size=(1500, 900),
                window_maximized=True,
            )
            store.save(second)
            self.assertEqual(store.load(), second)

    def test_save_creates_the_file(self) -> None:
        with TemporaryDirectory() as tmp_str:
            path = Path(tmp_str) / "state.json"
            store = SessionStateStore(path)
            self.assertFalse(path.exists())
            store.save(DEFAULT_SESSION_STATE)
            self.assertTrue(path.exists())

    def test_save_does_not_leave_a_temp_file_behind(self) -> None:
        with TemporaryDirectory() as tmp_str:
            path = Path(tmp_str) / "state.json"
            store = SessionStateStore(path)
            store.save(DEFAULT_SESSION_STATE)
            leftover = list(Path(tmp_str).glob("*.tmp"))
            self.assertEqual(leftover, [])

    def test_saved_file_carries_the_schema_version(self) -> None:
        with TemporaryDirectory() as tmp_str:
            path = Path(tmp_str) / "state.json"
            store = SessionStateStore(path)
            store.save(DEFAULT_SESSION_STATE)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["version"], 1)


if __name__ == "__main__":
    unittest.main()
