from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

from check_version import (
    VersionParseError,
    VersionSource,
    debian_upstream_of,
    main,
    mismatches,
    read_versions,
    to_debian_upstream,
)

_PYPROJECT = """\
[project]
name = "folio"
version = "{version}"
"""

_MESON = """\
project(
  'folio',
  version: '{version}',
  license: 'GPL-3.0-only',
  meson_version: '>= 1.0.0',
)
"""

_METAINFO = """\
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>io.github.rand_byte.Folio</id>
  <releases>
    <release version="{version}" type="development" date="2026-07-11">
      <description><p>Release candidate.</p></description>
    </release>
  </releases>
</component>
"""

_CHANGELOG = """\
folio ({version}) unstable; urgency=medium

  * Initial Debian packaging.

 -- rand-byte <nobody@example.com>  Sat, 11 Jul 2026 00:00:00 +0000
"""

_TEMPLATES = {
    VersionSource.PYPROJECT: _PYPROJECT,
    VersionSource.MESON: _MESON,
    VersionSource.METAINFO: _METAINFO,
    VersionSource.CHANGELOG: _CHANGELOG,
}


def _write_tree(root: Path, versions: dict[VersionSource, str]) -> None:
    for source, version in versions.items():
        path = source.path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATES[source].format(version=version), encoding="utf-8")


def _agreeing(upstream: str, debian: str) -> dict[VersionSource, str]:
    return {
        VersionSource.PYPROJECT: upstream,
        VersionSource.MESON: upstream,
        VersionSource.METAINFO: upstream,
        VersionSource.CHANGELOG: debian,
    }


class DebianMappingTests(unittest.TestCase):
    def test_final_release_is_unchanged(self) -> None:
        self.assertEqual(to_debian_upstream("0.9.2"), "0.9.2")

    def test_pre_release_marker_gains_a_tilde(self) -> None:
        self.assertEqual(to_debian_upstream("0.9.2rc1"), "0.9.2~rc1")
        self.assertEqual(to_debian_upstream("1.0a2"), "1.0~a2")
        self.assertEqual(to_debian_upstream("1.2.3b10"), "1.2.3~b10")

    def test_unsupported_pep_440_forms_raise(self) -> None:
        for version in ("0.9.2.post1", "0.9.2.dev0", "0.9.2-rc1", "v0.9.2", "rc1", ""):
            with self.subTest(version=version):
                with self.assertRaises(VersionParseError):
                    to_debian_upstream(version)

    def test_revision_and_epoch_are_stripped(self) -> None:
        self.assertEqual(debian_upstream_of("0.9.2~rc1-1"), "0.9.2~rc1")
        self.assertEqual(debian_upstream_of("1:2.0-3"), "2.0")

    def test_missing_revision_raises(self) -> None:
        with self.assertRaises(VersionParseError):
            debian_upstream_of("0.9.2~rc1")


class ReadVersionsTests(unittest.TestCase):
    def test_every_site_is_read_in_its_own_dialect(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, _agreeing("0.9.2rc1", "0.9.2~rc1-1"))
            self.assertEqual(
                dict(read_versions(root)), _agreeing("0.9.2rc1", "0.9.2~rc1-1")
            )

    def test_missing_meson_version_raises(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, _agreeing("0.9.2rc1", "0.9.2~rc1-1"))
            VersionSource.MESON.path(root).write_text(
                "project(\n  'folio',\n)\n", encoding="utf-8"
            )
            with self.assertRaises(VersionParseError):
                read_versions(root)

    def test_malformed_changelog_line_raises(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, _agreeing("0.9.2rc1", "0.9.2~rc1-1"))
            VersionSource.CHANGELOG.path(root).write_text(
                "folio 0.9.2~rc1-1 unstable\n", encoding="utf-8"
            )
            with self.assertRaises(VersionParseError):
                read_versions(root)

    def test_metainfo_without_a_release_raises(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, _agreeing("0.9.2rc1", "0.9.2~rc1-1"))
            VersionSource.METAINFO.path(root).write_text(
                "<component><releases/></component>", encoding="utf-8"
            )
            with self.assertRaises(VersionParseError):
                read_versions(root)

    def test_malformed_metainfo_xml_raises(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, _agreeing("0.9.2rc1", "0.9.2~rc1-1"))
            VersionSource.METAINFO.path(root).write_text("<component>", encoding="utf-8")
            with self.assertRaises(VersionParseError):
                read_versions(root)

    def test_pyproject_without_a_version_raises(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, _agreeing("0.9.2rc1", "0.9.2~rc1-1"))
            VersionSource.PYPROJECT.path(root).write_text(
                '[project]\nname = "folio"\n', encoding="utf-8"
            )
            with self.assertRaises(VersionParseError):
                read_versions(root)


class MismatchTests(unittest.TestCase):
    def test_agreeing_sites_report_nothing(self) -> None:
        self.assertEqual(list(mismatches(_agreeing("0.9.2rc1", "0.9.2~rc1-1"))), [])
        self.assertEqual(list(mismatches(_agreeing("0.9.2", "0.9.2-1"))), [])

    def test_a_debian_revision_bump_is_not_a_mismatch(self) -> None:
        versions = _agreeing("0.9.2rc1", "0.9.2~rc1-3")
        self.assertEqual(list(mismatches(versions)), [])

    def test_one_line_per_disagreeing_site(self) -> None:
        versions = _agreeing("0.9.2rc1", "0.9.3~rc1-1")
        versions[VersionSource.MESON] = "0.9.1rc1"
        reported = list(mismatches(versions))
        self.assertEqual(len(reported), 2)
        self.assertTrue(any(str(VersionSource.MESON) in line for line in reported))
        self.assertTrue(any(str(VersionSource.CHANGELOG) in line for line in reported))

    def test_a_hyphenated_pre_release_in_the_changelog_is_a_mismatch(self) -> None:
        # `0.9.2-rc1-1` would sort *after* 0.9.2, so apt would never offer the
        # upgrade to the final release: the tilde is the whole point.
        versions = _agreeing("0.9.2rc1", "0.9.2-rc1-1")
        self.assertEqual(len(list(mismatches(versions))), 1)


class MainTests(unittest.TestCase):
    """``main`` reports to stderr, which the tests capture rather than print."""

    def _exit_code(self, root: Path) -> int:
        with redirect_stderr(io.StringIO()):
            return main(root)
    def test_exit_zero_when_every_site_agrees(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, _agreeing("0.9.2rc1", "0.9.2~rc1-1"))
            self.assertEqual(self._exit_code(root), 0)

    def test_exit_one_on_a_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            versions = _agreeing("0.9.2rc1", "0.9.2~rc1-1")
            versions[VersionSource.METAINFO] = "0.9.1"
            _write_tree(root, versions)
            self.assertEqual(self._exit_code(root), 1)

    def test_exit_two_on_malformed_input(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, _agreeing("0.9.2rc1", "0.9.2~rc1-1"))
            VersionSource.PYPROJECT.path(root).write_text(
                '[project]\nversion = "0.9.2.post1"\n', encoding="utf-8"
            )
            self.assertEqual(self._exit_code(root), 2)

    def test_the_repository_itself_is_consistent(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(list(mismatches(read_versions(root))), [])


if __name__ == "__main__":
    unittest.main()
