"""Check that every file stating the release version agrees with the others.

Principles & invariants
-----------------------
* ``pyproject.toml`` is the **single source of truth** for the version. The
  other three sites (:mod:`meson.build`, the AppStream metainfo, and
  ``debian/changelog``) *mirror* it; this script reports where they do not.
* The script **reads and reports, never rewrites**. A mismatch is a human
  decision (which site is wrong?), so the only outputs are a report and an
  exit status.
* The set of version sites is **closed and enumerated** — :class:`VersionSource`
  carries each site's repo-relative path, so the read loop and the mismatch
  report are enum-driven and a new site is added in exactly one place.
* **One release, three dialects.** PEP 440 (`0.9.2rc1`) is upstream's spelling;
  Debian's is `0.9.2~rc1`, and the changelog additionally carries a `-<revision>`
  suffix that is packaging-only and therefore not this script's business. The
  ``~`` is load-bearing: it is the only character sorting *before* the empty
  string in dpkg's comparison, so ``0.9.2~rc1`` < ``0.9.2`` and an RC upgrades
  to the final release. :func:`to_debian_upstream` is the one place that mapping
  lives.
* **Parsing never silently skips.** A missing, duplicated, or unparseable
  version line is a :class:`VersionParseError`, not a pass — a check that
  quietly reads nothing would report agreement it never verified.
* **No Debian tooling dependency.** The changelog's first line is parsed
  directly rather than through ``dpkg-parsechangelog``, so this script (and
  therefore ``make type`` / ``make lint`` / ``make test``) runs on any host.
* Build tooling, not shipped code — it lives outside ``src/`` and is never
  installed (the same contract as :mod:`install_python_tree`). It is still
  covered by ``make type`` / ``make lint`` / ``make test``.
"""
from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping
from enum import StrEnum
from pathlib import Path
from xml.etree import ElementTree

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


class VersionParseError(ValueError):
    """A version site is missing, duplicated, or malformed."""


class PreReleaseMarker(StrEnum):
    """The PEP 440 pre-release segments this project uses.

    Each maps to Debian by gaining a ``~`` prefix (``rc1`` -> ``~rc1``).
    """

    ALPHA = "a"
    BETA = "b"
    RELEASE_CANDIDATE = "rc"


class VersionSource(StrEnum):
    """A file that states the release version; the value is its path in the repo."""

    PYPROJECT = "pyproject.toml"
    MESON = "meson.build"
    METAINFO = "data/io.github.rand_byte.Folio.metainfo.xml"
    CHANGELOG = "debian/changelog"

    def path(self, root: Path) -> Path:
        return root / self.value


_MESON_VERSION = re.compile(r"^[ \t]*version:[ \t]*'(?P<version>[^']*)'", re.MULTILINE)
"""``project(...)``'s ``version:`` keyword. ``meson_version:`` cannot match: the
line-start anchor plus the horizontal-whitespace class leaves no way to skip its
``meson_`` prefix."""

_CHANGELOG_HEADER = re.compile(
    r"\A(?P<source>[a-z0-9][a-z0-9+.-]*) \((?P<version>[^()\s]+)\)"
)
"""The changelog's first line: ``<source> (<version>) <distribution>; …``."""

_PEP_440_VERSION = re.compile(
    rf"\A(?P<release>\d+(?:\.\d+)*)"
    rf"(?:(?P<marker>{'|'.join(PreReleaseMarker)})(?P<serial>\d+))?\Z"
)
"""The PEP 440 subset this project releases: a release segment plus an optional
pre-release. Anything else (``.post``, ``.dev``, a local version) is a parse
error rather than a silently unmapped version."""


def to_debian_upstream(version: str) -> str:
    """Map a PEP 440 version to its Debian *upstream* spelling (no revision)."""
    match = _PEP_440_VERSION.fullmatch(version)
    if match is None:
        raise VersionParseError(f"not a supported PEP 440 version: {version!r}")
    marker = match["marker"]
    if marker is None:
        return match["release"]
    return f"{match['release']}~{marker}{match['serial']}"


def debian_upstream_of(debian_version: str) -> str:
    """Strip the epoch and the ``-<revision>`` suffix from a Debian version."""
    _, _, without_epoch = debian_version.rpartition(":")
    upstream, separator, _ = without_epoch.rpartition("-")
    if not separator:
        raise VersionParseError(
            f"Debian version carries no -<revision>: {debian_version!r}"
        )
    return upstream


def _parse_pyproject(text: str) -> str:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise VersionParseError(f"pyproject.toml is not valid TOML: {error}") from error
    project = document.get("project")
    if not isinstance(project, dict):
        raise VersionParseError("pyproject.toml has no [project] table")
    version = project.get("version")
    if not isinstance(version, str):
        raise VersionParseError("pyproject.toml has no project.version string")
    return version


def _parse_meson(text: str) -> str:
    versions = _MESON_VERSION.findall(text)
    if len(versions) != 1:
        raise VersionParseError(
            f"meson.build must state exactly one version:, found {len(versions)}"
        )
    return str(versions[0])


def _parse_metainfo(text: str) -> str:
    try:
        component = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise VersionParseError(f"metainfo is not valid XML: {error}") from error
    release = component.find("releases/release")
    if release is None:
        raise VersionParseError("metainfo has no <releases>/<release> element")
    version = release.get("version")
    if version is None:
        raise VersionParseError("metainfo <release> has no version attribute")
    return version


def _parse_changelog(text: str) -> str:
    match = _CHANGELOG_HEADER.match(text)
    if match is None:
        raise VersionParseError("debian/changelog: unparseable first line")
    return match["version"]


_PARSERS: Mapping[VersionSource, Callable[[str], str]] = {
    VersionSource.PYPROJECT: _parse_pyproject,
    VersionSource.MESON: _parse_meson,
    VersionSource.METAINFO: _parse_metainfo,
    VersionSource.CHANGELOG: _parse_changelog,
}


def read_versions(root: Path) -> Mapping[VersionSource, str]:
    """Read the version *as written* at every site, in that site's own dialect."""
    return {
        source: _PARSERS[source](source.path(root).read_text(encoding="utf-8"))
        for source in VersionSource
    }


def mismatches(versions: Mapping[VersionSource, str]) -> Iterator[str]:
    """Yield one report line per site disagreeing with ``pyproject.toml``."""
    expected = versions[VersionSource.PYPROJECT]
    debian_expected = to_debian_upstream(expected)

    for source in (VersionSource.MESON, VersionSource.METAINFO):
        found = versions[source]
        if found != expected:
            yield f"{source}: {found!r} does not match {expected!r} (pyproject.toml)"

    found_debian = debian_upstream_of(versions[VersionSource.CHANGELOG])
    if found_debian != debian_expected:
        yield (
            f"{VersionSource.CHANGELOG}: upstream version {found_debian!r} "
            f"does not match {debian_expected!r} (pyproject.toml {expected!r})"
        )


def main(root: Path) -> int:
    try:
        reported = tuple(mismatches(read_versions(root)))
    except VersionParseError as error:
        print(f"version check failed: {error}", file=sys.stderr)
        return 2
    for line in reported:
        print(line, file=sys.stderr)
    return 1 if reported else 0


if __name__ == "__main__":
    raise SystemExit(main(_REPOSITORY_ROOT))
