"""Tests for :mod:`config.paths`."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import paths


class DataDirectoryTests(unittest.TestCase):
    """Cover the XDG override, the home-relative fallback, and mkdir."""

    def test_uses_xdg_data_home_when_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: str(tmp)},
                clear=False,
            ):
                directory = paths.data_directory()
            self.assertEqual(directory, tmp / paths.APP_DIRECTORY_NAME)
            self.assertTrue(directory.is_dir())

    def test_falls_back_to_home_when_xdg_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            fake_home = Path(tmp_str)
            new_env = {k: v for k, v in os.environ.items()
                       if k != paths.XDG_DATA_HOME_ENV}
            with mock.patch.dict(os.environ, new_env, clear=True), \
                 mock.patch.object(Path, "home", return_value=fake_home):
                directory = paths.data_directory()
            self.assertEqual(
                directory,
                fake_home / ".local" / "share" / paths.APP_DIRECTORY_NAME,
            )
            self.assertTrue(directory.is_dir())

    def test_falls_back_to_home_when_xdg_empty(self) -> None:
        # Per the XDG spec, an empty value counts as unset.
        with tempfile.TemporaryDirectory() as tmp_str:
            fake_home = Path(tmp_str)
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: ""},
                clear=False,
            ), mock.patch.object(Path, "home", return_value=fake_home):
                directory = paths.data_directory()
            self.assertEqual(
                directory,
                fake_home / ".local" / "share" / paths.APP_DIRECTORY_NAME,
            )

    def test_creates_missing_intermediate_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            # Use a deeply nested base to exercise parents=True.
            base = Path(tmp_str) / "deeply" / "nested" / "xdg"
            self.assertFalse(base.exists())
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: str(base)},
                clear=False,
            ):
                directory = paths.data_directory()
            self.assertTrue(directory.is_dir())

    def test_idempotent_when_directory_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                first = paths.data_directory()
                second = paths.data_directory()
            self.assertEqual(first, second)
            self.assertTrue(first.is_dir())

    def test_returns_pathlib_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                directory = paths.data_directory()
            self.assertIsInstance(directory, Path)


class DatabasePathTests(unittest.TestCase):
    def test_returns_db_filename_inside_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                db = paths.database_path()
                expected = (
                    Path(tmp_str)
                    / paths.APP_DIRECTORY_NAME
                    / paths.DATABASE_FILENAME
                )
            self.assertEqual(db, expected)

    def test_parent_directory_exists_after_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                db = paths.database_path()
            self.assertTrue(db.parent.is_dir())

    def test_does_not_create_database_file(self) -> None:
        # The helper only ensures the directory; opening / creating the
        # SQLite file is the caller's job.
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                db = paths.database_path()
            self.assertFalse(db.exists())


class SessionStatePathTests(unittest.TestCase):
    def test_returns_state_filename_inside_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                state_path = paths.session_state_path()
                expected = (
                    Path(tmp_str)
                    / paths.APP_DIRECTORY_NAME
                    / paths.SESSION_STATE_FILENAME
                )
            self.assertEqual(state_path, expected)

    def test_parent_directory_exists_after_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                state_path = paths.session_state_path()
            self.assertTrue(state_path.parent.is_dir())

    def test_does_not_create_state_file(self) -> None:
        # The helper only ensures the directory; a first launch has no
        # prior session, and SessionStateStore treats that as "use
        # defaults", not an error.
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                state_path = paths.session_state_path()
            self.assertFalse(state_path.exists())

    def test_distinct_from_database_path(self) -> None:
        # Both files live in the same directory but must not collide.
        with tempfile.TemporaryDirectory() as tmp_str:
            with mock.patch.dict(
                os.environ,
                {paths.XDG_DATA_HOME_ENV: tmp_str},
                clear=False,
            ):
                db = paths.database_path()
                state_path = paths.session_state_path()
            self.assertEqual(db.parent, state_path.parent)
            self.assertNotEqual(db.name, state_path.name)


if __name__ == "__main__":
    unittest.main()
