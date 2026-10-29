#!/usr/bin/env python3
"""Release integrity preflight and GitHub Release finalizer.

The ``Protect release tags`` ruleset on ``refs/tags/v*`` has no bypass, so
neither a git client nor this workflow can create the tag. Publishing a
GitHub Release in the web UI is what mints it. ``publish.yml`` then checks
that the tag, ``project.version``, ``hermes_a365.__version__``, and the
matching CHANGELOG section agree, publishes the already-built distributions
to PyPI, and only afterwards rewrites that one release (notes, title,
Latest / prerelease, artifacts). This module never creates or moves a git ref.

Operator steps live in ``references/releasing.md``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path

PACKAGE_NAME = "hermes-a365"
ENTRY_POINT_GROUP = "hermes_agent.plugins"
ENTRY_POINT_NAME = "agent365"
TITLE_LIMIT = 250
NOTES_LIMIT = 125_000
ALLOWED_HOSTS = frozenset({"api.github.com", "uploads.github.com"})
PYPI_HOSTS = frozenset({"pypi.org"})
_MAX_RELEASE_PAGES = 100
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+\b")
_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\] \u2014 (?P<date>\d{4}-\d{2}-\d{2})[ \t]*$")
_VERSION_ASSIGN = re.compile(
    r"^__version__\s*=\s*(?P<quote>['\"])(?P<version>.*?)(?P=quote)\s*$",
    re.MULTILINE,
)
# Strict semver, no build metadata. Leading zeros are rejected.
_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?$"
)
_REQUIRED_TEMPLATES = (
    "blueprint.json.j2",
    "consent-url.txt.j2",
    "instance.env.j2",
    "adaptive-cards/confirmation.json.j2",
    "adaptive-cards/error.json.j2",
    "adaptive-cards/greeting.json.j2",
)

Transport = Callable[[str, str, bytes | None, Mapping[str, str]], "HttpResult"]


class ReleaseIntegrityError(Exception):
    """One or more fail-closed release checks did not pass."""

    def __init__(self, problems: str | Sequence[str]) -> None:
        if isinstance(problems, str):
            items = [problems]
        else:
            items = [str(item) for item in problems if str(item)]
        self.problems = items or ["release integrity check failed"]
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes


@total_ordering
@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return core < other_core
        if self.prerelease == other.prerelease:
            return False
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        return _prerelease_lt(self.prerelease, other.prerelease)


@dataclass(frozen=True)
class ReleaseNotes:
    tag: str
    version: str
    title: str
    body: str
    prerelease: bool

    def metadata(self) -> dict[str, str | bool]:
        return {
            "tag": self.tag,
            "version": self.version,
            "title": self.title,
            "prerelease": self.prerelease,
        }


@dataclass(frozen=True)
class RemoteRelease:
    tag: str
    version: SemVer | None
    prerelease: bool
    draft: bool
    release_id: int


@dataclass(frozen=True)
class ReleasePlan:
    prerelease: bool
    make_latest: bool
    move_latest_to: str | None


def scrub(text: str, token: str | None = None) -> str:
    """Remove credential material from a string that may be printed."""
    if token:
        text = text.replace(token, "[redacted]")
    return _TOKEN_RE.sub("[redacted]", text)


def version_from_tag(tag_ref: str) -> str:
    """Return the package version encoded by a ``v*`` tag ref."""
    tag = tag_from_ref(tag_ref)
    return tag[1:]


def tag_from_ref(tag_ref: str) -> str:
    ref = tag_ref.strip()
    tag = ref.removeprefix("refs/tags/") if ref.startswith("refs/tags/") else ref
    if not tag.startswith("v") or _SEMVER.fullmatch(tag[1:]) is None:
        raise ReleaseIntegrityError(
            f"rejecting non-semver release tag {tag_ref!r}; "
            "expected refs/tags/vMAJOR.MINOR.PATCH or a semver prerelease"
        )
    return tag


def parse_semver(version: str) -> SemVer:
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ReleaseIntegrityError(f"rejecting non-semver version {version!r}")
    pre = match.group("pre")
    return SemVer(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        tuple(pre.split(".")) if pre else (),
    )


def project_version(repo: Path) -> str:
    path = repo / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseIntegrityError(f"could not read {path}: {exc}") from exc
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - CI is 3.11+
        raise ReleaseIntegrityError("reading pyproject.toml requires Python 3.11+") from exc
    try:
        data = tomllib.loads(text)
        version = data["project"]["version"]
    except (KeyError, tomllib.TOMLDecodeError, TypeError) as exc:
        raise ReleaseIntegrityError(f"pyproject.toml has no project.version: {exc}") from exc
    if not isinstance(version, str) or not version:
        raise ReleaseIntegrityError("project.version must be a non-empty string")
    return version


def module_version(repo: Path) -> str:
    path = repo / "src" / "hermes_a365" / "__init__.py"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseIntegrityError(f"could not read {path}: {exc}") from exc
    found = _VERSION_ASSIGN.findall(text)
    versions = [item[1] for item in found]
    if len(versions) != 1 or not versions[0]:
        raise ReleaseIntegrityError(
            "hermes_a365.__version__ must be exactly one string literal "
            f"in {path}, found {len(versions)}"
        )
    return versions[0]


def changelog_section(text: str, version: str) -> tuple[str, str]:
    """Return the heading line and body of the single matching release section."""
    matches: list[tuple[int, str]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading is None or heading.group("version") != version:
            continue
        try:
            dt.date.fromisoformat(heading.group("date"))
        except ValueError as exc:
            raise ReleaseIntegrityError(
                f"CHANGELOG.md heading has an invalid date: {line}"
            ) from exc
        matches.append((index, line))
    if not matches:
        raise ReleaseIntegrityError(
            "CHANGELOG.md needs exactly one "
            f"'## [{version}] \u2014 YYYY-MM-DD' section. "
            "An 'Unreleased / …' heading is not a release section."
        )
    if len(matches) > 1:
        raise ReleaseIntegrityError(
            f"CHANGELOG.md has {len(matches)} sections for version {version}"
        )
    start, heading_line = matches[0]
    body_lines: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body:
        raise ReleaseIntegrityError(f"CHANGELOG.md section {version} is empty")
    return heading_line, body


def first_paragraph(body: str) -> str:
    lines = body.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    paragraph: list[str] = []
    while index < len(lines) and lines[index].strip() and not lines[index].startswith("#"):
        paragraph.append(lines[index].strip())
        index += 1
    return " ".join(paragraph)


def first_sentence(paragraph: str) -> str:
    """First sentence of a collapsed paragraph.

    A sentence ends at ``.``, ``!``, or ``?`` when whitespace and an uppercase
    letter follow. Periods inside versions (``1.0 ladder``) stay in the sentence.
    """
    collapsed = " ".join(paragraph.split())
    if not collapsed:
        raise ReleaseIntegrityError("CHANGELOG section has no theme sentence")
    match = re.search(r"[.!?](?=\s+[A-Z])", collapsed)
    if match is None:
        return collapsed
    return collapsed[: match.end()].strip()


def release_title(tag: str, body: str) -> str:
    theme = first_sentence(first_paragraph(body))
    prefix = f"{tag} \u2014 "
    if len(prefix) + len(theme) <= TITLE_LIMIT:
        return prefix + theme
    room = TITLE_LIMIT - len(prefix) - 3
    if room < 1:
        raise ReleaseIntegrityError("release title prefix exceeds the title limit")
    return prefix + theme[:room].rstrip() + "..."


def evaluate(repo: Path, tag_ref: str) -> ReleaseNotes:
    """Compare the tag with both version sources and the CHANGELOG section."""
    tag = tag_from_ref(tag_ref)
    version = tag[1:]
    problems: list[str] = []
    try:
        found_project = project_version(repo)
        if found_project != version:
            problems.append(f"project.version is {found_project!r}, tag {tag} requires {version!r}")
    except ReleaseIntegrityError as exc:
        problems.extend(exc.problems)
    try:
        found_module = module_version(repo)
        if found_module != version:
            problems.append(
                f"hermes_a365.__version__ is {found_module!r}, tag {tag} requires {version!r}"
            )
    except ReleaseIntegrityError as exc:
        problems.extend(exc.problems)
    body = ""
    heading = ""
    changelog_path = repo / "CHANGELOG.md"
    try:
        text = changelog_path.read_text(encoding="utf-8")
        heading, body = changelog_section(text, version)
    except OSError as exc:
        problems.append(f"could not read {changelog_path}: {exc}")
    except ReleaseIntegrityError as exc:
        problems.extend(exc.problems)
    if problems:
        raise ReleaseIntegrityError(problems)
    parsed = parse_semver(version)
    notes = heading + "\n\n" + body.strip() + "\n"
    if len(notes) > NOTES_LIMIT:
        raise ReleaseIntegrityError("CHANGELOG section exceeds the GitHub Release body limit")
    return ReleaseNotes(
        tag=tag,
        version=version,
        title=release_title(tag, body),
        body=notes,
        prerelease=parsed.is_prerelease,
    )


def _git(repo: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if not args or args[0] != "git":
        raise ReleaseIntegrityError("refusing to run a non-git command")
    try:
        return subprocess.run(
            list(args),
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ReleaseIntegrityError(f"could not run git: {exc}") from exc


def assert_reachable(repo: Path, sha: str) -> None:
    """Fail unless ``sha`` is contained in ``origin/main``."""
    if _SHA.fullmatch(sha) is None:
        raise ReleaseIntegrityError("release sha must be a 40-character hex commit id")
    completed = _git(repo, ["git", "merge-base", "--is-ancestor", sha, "origin/main"])
    if completed.returncode != 0:
        raise ReleaseIntegrityError(f"{sha} is not reachable from origin/main; refusing to publish")


def assert_worktree(repo: Path, sha: str) -> None:
    """Fail unless HEAD is ``sha`` and the worktree matches that commit.

    ``evaluate`` reads the working tree. Ancestry alone would let a dirty
    tree, or a checkout of a different commit, describe files that are not
    the tagged commit.
    """
    if _SHA.fullmatch(sha) is None:
        raise ReleaseIntegrityError("release sha must be a 40-character hex commit id")
    head = _git(repo, ["git", "rev-parse", "HEAD"])
    resolved = head.stdout.strip()
    if head.returncode != 0 or resolved != sha:
        raise ReleaseIntegrityError(
            f"worktree HEAD is not {sha}; refusing to publish a tree other than the tagged commit"
        )
    status = _git(repo, ["git", "status", "--porcelain", "--untracked-files=no"])
    if status.returncode != 0 or status.stdout.strip():
        raise ReleaseIntegrityError("worktree differs from the tagged commit; refusing to publish")


def check(repo: Path, tag_ref: str, sha: str | None = None) -> ReleaseNotes:
    """Compare versions and, when ``sha`` is set, bind the worktree to it."""
    problems: list[str] = []
    notes: ReleaseNotes | None = None
    try:
        notes = evaluate(repo, tag_ref)
    except ReleaseIntegrityError as exc:
        problems.extend(exc.problems)
    if sha is not None:
        try:
            assert_worktree(repo, sha)
        except ReleaseIntegrityError as exc:
            problems.extend(exc.problems)
        try:
            assert_reachable(repo, sha)
        except ReleaseIntegrityError as exc:
            problems.extend(exc.problems)
    if problems:
        raise ReleaseIntegrityError(problems)
    if notes is None:  # pragma: no cover - problems would already have been raised
        raise ReleaseIntegrityError("release integrity check produced no result")
    return notes


def write_notes(notes: ReleaseNotes, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metadata.json").write_text(
        json.dumps(notes.metadata(), indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "notes.md").write_text(notes.body, encoding="utf-8")


def plan_release(notes: ReleaseNotes, releases: Sequence[RemoteRelease]) -> ReleasePlan:
    """Choose Latest / prerelease so an older patch cannot steal Latest."""
    this = parse_semver(notes.version)
    stable = [
        release
        for release in releases
        if release.tag != notes.tag
        and not release.draft
        and not release.prerelease
        and release.version is not None
        and not release.version.is_prerelease
    ]
    if this.is_prerelease or notes.prerelease:
        winner = max(stable, key=lambda release: release.version or SemVer(0, 0, 0), default=None)
        return ReleasePlan(
            prerelease=True,
            make_latest=False,
            move_latest_to=None if winner is None else winner.tag,
        )
    higher_or_equal = [
        release for release in stable if release.version is not None and release.version >= this
    ]
    if higher_or_equal:
        winner = max(higher_or_equal, key=lambda release: release.version or SemVer(0, 0, 0))
        return ReleasePlan(prerelease=False, make_latest=False, move_latest_to=winner.tag)
    return ReleasePlan(prerelease=False, make_latest=True, move_latest_to=None)


def iter_dist_files(dist: Path) -> tuple[Path, Path, Path]:
    """Return the wheel, sdist, and checksum file after a layout check."""
    if not dist.is_dir():
        raise ReleaseIntegrityError(f"dist directory does not exist: {dist}")
    entries = [path for path in dist.iterdir() if path.name != "SHA256SUMS"]
    unsafe = [path for path in dist.iterdir() if path.is_symlink() or not path.is_file()]
    if unsafe:
        raise ReleaseIntegrityError(
            "dist entries must be regular files: " + ", ".join(path.name for path in unsafe)
        )
    wheels = [path for path in entries if path.suffix == ".whl"]
    sdists = [path for path in entries if path.name.endswith(".tar.gz")]
    sums = dist / "SHA256SUMS"
    if len(wheels) != 1 or len(sdists) != 1 or len(entries) != 2 or not sums.is_file():
        raise ReleaseIntegrityError(
            "dist must contain exactly one wheel, one sdist, and SHA256SUMS"
        )
    return wheels[0], sdists[0], sums


def _basename_in_dist(dist: Path, name: str, sums_name: str) -> Path:
    """Accept only a bare filename, so ``sha256sum --check`` works beside the assets."""
    if (
        not name
        or name in {".", "..", sums_name}
        or name != Path(name).name
        or "/" in name
        or "\\" in name
        or "\n" in name
        or "\r" in name
    ):
        raise ReleaseIntegrityError(f"unsafe SHA256SUMS path: {name}")
    return dist / name


def verify_checksums(dist: Path) -> tuple[Path, Path, Path]:
    wheel, sdist, sums = iter_dist_files(dist)
    expected: dict[Path, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ReleaseIntegrityError("malformed SHA256SUMS entry")
        try:
            int(parts[0], 16)
        except ValueError as exc:
            raise ReleaseIntegrityError("malformed SHA256SUMS digest") from exc
        name = parts[1].lstrip("*")
        path = _basename_in_dist(dist, name, sums.name)
        if path in expected:
            raise ReleaseIntegrityError(f"duplicate SHA256SUMS path: {name}")
        expected[path] = parts[0].lower()
    actual = {wheel, sdist}
    if set(expected) != actual:
        raise ReleaseIntegrityError("SHA256SUMS does not match the wheel and sdist")
    for path, digest in expected.items():
        found = hashlib.sha256(path.read_bytes()).hexdigest()
        if found != digest:
            raise ReleaseIntegrityError(f"checksum mismatch for {path.name}")
    return wheel, sdist, sums


def _pep440(value: str):
    """Parse a package version. SemVer ``1.2.3-rc.1`` and PEP 440 ``1.2.3rc1`` match.

    Tags, changelog headings, and the source version strings stay SemVer.
    Distribution filenames, core metadata, and the installed version use the
    build backend's PEP 440 spelling.
    """
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError as exc:  # pragma: no cover - present in the build and smoke envs
        raise ReleaseIntegrityError(
            "packaging is not installed; version comparison needs the hash-pinned packaging"
        ) from exc
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise ReleaseIntegrityError(f"rejecting non-PEP-440 version {value!r}") from exc


def _require_same_version(found: str, expected: str, label: str) -> None:
    if _pep440(found) != _pep440(expected):
        raise ReleaseIntegrityError(f"{label} is {found!r}, expected {expected!r}")


def _require_filename_version(path: Path, expected_version: str) -> None:
    try:
        from packaging.utils import (
            InvalidSdistFilename,
            InvalidWheelFilename,
            parse_sdist_filename,
            parse_wheel_filename,
        )
    except ImportError as exc:  # pragma: no cover - present in the build env
        raise ReleaseIntegrityError(
            "packaging is not installed; version comparison needs the hash-pinned packaging"
        ) from exc
    try:
        if path.suffix == ".whl":
            _name, found, _build, _tags = parse_wheel_filename(path.name)
        elif path.name.endswith(".tar.gz"):
            _name, found = parse_sdist_filename(path.name)
        else:
            raise ReleaseIntegrityError(f"{path.name} is not a wheel or sdist")
    except (InvalidSdistFilename, InvalidWheelFilename) as exc:
        raise ReleaseIntegrityError(f"{path.name} has no PEP 440 version") from exc
    if found != _pep440(expected_version):
        raise ReleaseIntegrityError(
            f"{path.name} Version is {found}, expected {expected_version!r}"
        )


def check_distributions(dist: Path, expected_version: str) -> None:
    """Validate wheel and sdist core metadata with the pinned packaging library."""
    try:
        from packaging.metadata import Metadata
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
    except ImportError as exc:  # pragma: no cover - present in the pinned build env
        raise ReleaseIntegrityError(
            "packaging is not installed; check-dists runs inside the hash-pinned build env"
        ) from exc
    tag_from_ref(f"v{expected_version}")
    wheel, sdist, _sums = verify_checksums(dist)
    for path, metadata_text in (
        (wheel, _wheel_metadata(wheel)),
        (sdist, _sdist_metadata(sdist)),
    ):
        headers = _metadata_headers(metadata_text)
        if headers.get("Name") != PACKAGE_NAME:
            raise ReleaseIntegrityError(
                f"{path.name} Name is {headers.get('Name')!r}, expected {PACKAGE_NAME!r}"
            )
        found_version = headers.get("Version")
        if not isinstance(found_version, str):
            raise ReleaseIntegrityError(f"{path.name} is missing Version")
        _require_same_version(found_version, expected_version, f"{path.name} Version")
        _require_filename_version(path, expected_version)
        requires = headers.get("Requires-Python")
        if not requires:
            raise ReleaseIntegrityError(f"{path.name} is missing Requires-Python")
        spec = SpecifierSet(requires)
        if Version("3.11") not in spec or Version("3.14") not in spec:
            raise ReleaseIntegrityError(
                f"{path.name} Requires-Python {requires!r} must include 3.11 and 3.14"
            )
        if not headers.get("Summary"):
            raise ReleaseIntegrityError(f"{path.name} is missing Summary")
        license_expression = headers.get("License-Expression")
        if license_expression != "MIT":
            raise ReleaseIntegrityError(
                f"{path.name} License-Expression is {license_expression!r}, expected 'MIT'"
            )
        license_file = headers.get("License-File")
        if license_file != "LICENSE":
            raise ReleaseIntegrityError(
                f"{path.name} License-File is {license_file!r}, expected 'LICENSE'"
            )
        content_type = headers.get("Description-Content-Type", "")
        if not content_type.startswith("text/markdown"):
            raise ReleaseIntegrityError(
                f"{path.name} Description-Content-Type must be text/markdown"
            )
        try:
            Metadata.from_email(metadata_text, validate=True)
        except Exception as exc:
            raise ReleaseIntegrityError(
                f"{path.name} failed core-metadata validation: {exc}"
            ) from exc


def smoke_installed(expected_version: str) -> None:
    """Import the installed wheel and exercise the CLI, entry point, and data."""
    tag_from_ref(f"v{expected_version}")
    from importlib import resources

    import jinja2
    import yaml

    import hermes_a365

    _require_same_version(
        hermes_a365.__version__,
        expected_version,
        "installed hermes_a365.__version__",
    )
    _require_same_version(
        importlib.metadata.version(PACKAGE_NAME),
        expected_version,
        "installed distribution version",
    )
    _smoke_cli()
    _smoke_entry_point()
    plugin = resources.files("hermes_a365.plugin").joinpath("plugin.yaml")
    plugin_text = plugin.read_text(encoding="utf-8")
    loaded = yaml.safe_load(plugin_text)
    if not isinstance(loaded, dict) or loaded.get("name") != "agent365":
        raise ReleaseIntegrityError("installed plugin.yaml is missing name: agent365")
    for key in ("version", "description", "requires_env"):
        if key not in loaded:
            raise ReleaseIntegrityError(f"installed plugin.yaml is missing {key}")
    from hermes_a365._common import templates_dir

    root = templates_dir()
    found = {path.relative_to(root).as_posix() for path in root.rglob("*.j2")}
    missing = [name for name in _REQUIRED_TEMPLATES if name not in found]
    if missing:
        raise ReleaseIntegrityError("installed templates missing: " + ", ".join(missing))
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(root),
        autoescape=False,
    )
    for name in sorted(found):
        environment.get_template(name)


def _smoke_cli() -> None:
    cli = Path(sys.executable).with_name("hermes-a365")
    if os.name == "nt":  # pragma: no cover - CI is Linux
        cli = cli.with_suffix(".exe")
    if not cli.is_file():
        located = shutil.which("hermes-a365")
        if located is None:
            raise ReleaseIntegrityError("hermes-a365 console script is not installed")
        cli = Path(located)
    completed = subprocess.run(
        [str(cli), "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0 or "hermes-a365" not in completed.stdout:
        detail = scrub((completed.stderr or completed.stdout)[-500:])
        raise ReleaseIntegrityError(f"hermes-a365 --help failed: {detail}")


def _smoke_entry_point() -> None:
    matches = [
        entry
        for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        if entry.name == ENTRY_POINT_NAME
    ]
    if len(matches) != 1:
        raise ReleaseIntegrityError(
            f"expected one {ENTRY_POINT_GROUP} entry named {ENTRY_POINT_NAME}"
        )
    value = matches[0].value
    if "hermes_a365.plugin" not in value:
        raise ReleaseIntegrityError(f"plugin entry point target is {value!r}")
    loaded = matches[0].load()
    register = loaded if callable(loaded) else getattr(loaded, "register", None)
    if not callable(register):
        raise ReleaseIntegrityError("plugin entry point does not expose register")


def _wheel_metadata(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = []
        for info in archive.infolist():
            if _unsafe_archive_name(info.filename):
                raise ReleaseIntegrityError(f"unsafe path in wheel: {info.filename}")
            if info.filename.endswith(".dist-info/METADATA") and not info.is_dir():
                names.append(info.filename)
        if len(names) != 1:
            raise ReleaseIntegrityError(f"{path.name} does not contain exactly one METADATA")
        return archive.read(names[0]).decode("utf-8")


def _sdist_metadata(path: Path) -> str:
    with tarfile.open(path, "r:gz") as archive:
        chosen: list[tarfile.TarInfo] = []
        for member in archive.getmembers():
            if _unsafe_archive_name(member.name):
                raise ReleaseIntegrityError(f"unsafe path in sdist: {member.name}")
            parts = Path(member.name).parts
            if member.isfile() and len(parts) == 2 and parts[1] == "PKG-INFO":
                chosen.append(member)
        if len(chosen) != 1:
            raise ReleaseIntegrityError(f"{path.name} does not contain exactly one PKG-INFO")
        extracted = archive.extractfile(chosen[0])
        if extracted is None:
            raise ReleaseIntegrityError(f"{path.name} PKG-INFO could not be read")
        return extracted.read().decode("utf-8")


def _unsafe_archive_name(name: str) -> bool:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        return True
    return "\\" in name


def _metadata_headers(text: str) -> dict[str, str]:
    header_text = text.split("\n\n", 1)[0]
    message = email.message_from_string(header_text)
    return {key: str(value) for key, value in message.items()}


def _prerelease_lt(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    for lhs, rhs in zip(left, right, strict=False):
        if lhs == rhs:
            continue
        return _ident_lt(lhs, rhs)
    return len(left) < len(right)


def _ident_lt(left: str, right: str) -> bool:
    left_num = left.isdigit()
    right_num = right.isdigit()
    if left_num and right_num:
        return int(left) < int(right)
    if left_num:
        return True
    if right_num:
        return False
    return left < right


def _guard_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        host = parsed.hostname or "unknown"
        raise ReleaseIntegrityError(f"refusing to send credentials to {host}")
    if parsed.username or parsed.password:
        raise ReleaseIntegrityError("refusing a URL that embeds credentials")


def _guard_pypi_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in PYPI_HOSTS:
        host = parsed.hostname or "unknown"
        raise ReleaseIntegrityError(f"refusing PyPI digest lookup to {host}")
    if parsed.username or parsed.password:
        raise ReleaseIntegrityError("refusing a URL that embeds credentials")


def _pypi_json_url(version: str) -> str:
    tag_from_ref(f"v{version}")
    quoted = urllib.parse.quote(version, safe="")
    return f"https://pypi.org/pypi/{PACKAGE_NAME}/{quoted}/json"


def pypi_sha256(version: str, transport: Transport) -> dict[str, str]:
    """Return filename → sha256 from the PyPI JSON API. No credentials are sent."""
    url = _pypi_json_url(version)
    _guard_pypi_url(url)
    headers = {
        "Accept": "application/json",
        "User-Agent": "hermes-a365-release-integrity",
    }
    try:
        result = transport("GET", url, None, headers)
    except ReleaseIntegrityError:
        raise
    except Exception as exc:
        raise ReleaseIntegrityError(f"PyPI digest lookup failed: {exc}") from None
    if result.status == 404:
        raise ReleaseIntegrityError(
            f"PyPI has no {PACKAGE_NAME} {version}; refusing to upload release assets"
        )
    if result.status != 200:
        raise ReleaseIntegrityError(
            f"PyPI digest lookup returned {result.status}; refusing to upload release assets"
        )
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseIntegrityError("PyPI digest response was not JSON") from exc
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list):
        raise ReleaseIntegrityError("PyPI digest response has no file list")
    digests: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, dict):
            raise ReleaseIntegrityError("PyPI file entry was not an object")
        filename = item.get("filename")
        digest_map = item.get("digests")
        if not isinstance(filename, str) or not filename:
            raise ReleaseIntegrityError("PyPI file entry is missing a filename")
        if not isinstance(digest_map, dict):
            raise ReleaseIntegrityError(f"PyPI file {filename} has no digests")
        digest = digest_map.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ReleaseIntegrityError(f"PyPI file {filename} has no sha256 digest")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ReleaseIntegrityError(f"PyPI file {filename} has a malformed sha256") from exc
        normalized = digest.lower()
        previous = digests.get(filename)
        if previous is not None and previous != normalized:
            raise ReleaseIntegrityError(f"PyPI returned conflicting sha256 values for {filename}")
        digests[filename] = normalized
    return digests


def assert_pypi_digests(
    version: str,
    paths: Sequence[Path],
    transport: Transport,
) -> None:
    """Refuse unless each file's sha256 is the digest PyPI publishes for that name."""
    digests = pypi_sha256(version, transport)
    for path in paths:
        remote = digests.get(path.name)
        if remote is None:
            raise ReleaseIntegrityError(
                f"PyPI has no {path.name} for {version}; refusing to upload release assets"
            )
        local = hashlib.sha256(path.read_bytes()).hexdigest()
        if remote != local:
            raise ReleaseIntegrityError(
                f"sha256 of {path.name} does not match PyPI; refusing to upload release assets"
            )


def _default_pypi_transport() -> Transport:
    def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> HttpResult:
        _guard_pypi_url(url)
        if any(key.lower() == "authorization" for key in headers):
            raise ReleaseIntegrityError("refusing to send credentials to pypi.org")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=120) as response:
                raw_headers = {key: value for key, value in response.headers.items()}
                return HttpResult(response.status, raw_headers, response.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            raw_headers = {key: value for key, value in exc.headers.items()}
            return HttpResult(exc.code, raw_headers, payload)
        except urllib.error.URLError as exc:
            raise ReleaseIntegrityError(f"PyPI digest lookup failed: {exc.reason}") from None

    return transport


def _default_transport(token: str) -> Transport:
    def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> HttpResult:
        _guard_url(url)
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=120) as response:
                raw_headers = {key: value for key, value in response.headers.items()}
                return HttpResult(response.status, raw_headers, response.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            raw_headers = {key: value for key, value in exc.headers.items()}
            return HttpResult(exc.code, raw_headers, payload)
        except urllib.error.URLError as exc:
            message = scrub(f"GitHub request failed: {exc.reason}", token)
            raise ReleaseIntegrityError(message) from None

    return transport


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        host = urllib.parse.urlparse(newurl).hostname or "unknown"
        raise ReleaseIntegrityError(f"refusing redirect to {host}")


class GitHubReleases:
    """Update one release for an existing tag. Never creates a git ref."""

    def __init__(
        self,
        repository: str,
        token: str,
        transport: Transport,
        pypi_transport: Transport | None = None,
    ) -> None:
        if _REPO.fullmatch(repository) is None:
            raise ReleaseIntegrityError(f"unexpected repository id {repository!r}")
        if not token:
            raise ReleaseIntegrityError("GITHUB_TOKEN is not set")
        self.repository = repository
        self.token = token
        self.transport = transport
        self.pypi_transport = pypi_transport or _default_pypi_transport()

    def publish(self, notes: ReleaseNotes, dist: Path, *, expected_sha: str) -> str:
        wheel, sdist, sums = verify_checksums(dist)
        self._require_tag(notes.tag, expected_sha)
        # Compare to the files PyPI holds before any release write. A mismatch
        # leaves the GitHub Release unchanged.
        assert_pypi_digests(notes.version, [wheel, sdist], self.pypi_transport)
        existing = self._get_release(notes.tag)
        plan = plan_release(notes, self._list_releases())
        if existing is None:
            release = self._create_release(notes, plan)
            action = "created"
        else:
            release = self._update_release(int(existing["id"]), notes, plan)
            action = "updated"
        if plan.move_latest_to and plan.move_latest_to != notes.tag:
            self._mark_latest(plan.move_latest_to)
        self._upload_assets(int(release["id"]), [wheel, sdist, sums])
        url = scrub(str(release.get("html_url") or ""), self.token)
        print(f"GitHub Release {notes.tag} {action}: {url}")
        return url

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Mapping[str, object] | None = None,
        raw: bytes | None = None,
        content_type: str | None = None,
    ) -> HttpResult:
        _guard_url(url)
        body: bytes | None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        else:
            body = raw
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "hermes-a365-release-integrity",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if content_type:
            headers["Content-Type"] = content_type
        try:
            result = self.transport(method, url, body, headers)
        except ReleaseIntegrityError:
            raise
        except Exception as exc:
            raise ReleaseIntegrityError(scrub(str(exc), self.token)) from None
        if result.status >= 400:
            self._fail(result)
        return result

    def _fail(self, result: HttpResult) -> None:
        detail = result.body.decode("utf-8", errors="replace")[:500]
        try:
            payload = json.loads(result.body.decode("utf-8"))
            message = payload.get("message")
            if isinstance(message, str):
                detail = message
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise ReleaseIntegrityError(
            scrub(f"GitHub API returned {result.status}: {detail}", self.token)
        )

    def _api(self, path: str) -> str:
        return f"https://api.github.com/repos/{self.repository}{path}"

    def _require_tag(self, tag: str, expected_sha: str) -> None:
        if _SHA.fullmatch(expected_sha) is None:
            raise ReleaseIntegrityError("release sha must be a 40-character hex commit id")
        tag_from_ref(tag)
        quoted = urllib.parse.quote(tag, safe="")
        result = self._request_status("GET", self._api(f"/git/ref/tags/{quoted}"))
        if result.status == 404:
            raise ReleaseIntegrityError(
                f"tag {tag} does not exist. This workflow does not create or push tags. "
                "Publish a GitHub Release in the web UI to mint the v* tag "
                "(the Protect release tags ruleset has no bypass), then rerun."
            )
        if result.status >= 400:
            self._fail(result)
        resolved = self._tag_commit_sha(tag, self._json_object(result))
        if resolved != expected_sha:
            raise ReleaseIntegrityError(
                f"tag {tag} resolves to {resolved}, not the workflow commit {expected_sha}"
            )

    def _tag_commit_sha(self, tag: str, payload: Mapping[str, object]) -> str:
        obj = payload.get("object")
        if not isinstance(obj, dict):
            raise ReleaseIntegrityError(f"tag {tag} response did not include an object")
        obj_sha = obj.get("sha")
        obj_type = obj.get("type")
        if not isinstance(obj_sha, str) or _SHA.fullmatch(obj_sha) is None:
            raise ReleaseIntegrityError(f"tag {tag} is missing a commit sha")
        if obj_type == "commit":
            return obj_sha
        if obj_type != "tag":
            raise ReleaseIntegrityError(f"tag {tag} does not resolve to a commit")
        peeled = self._request("GET", self._api(f"/git/tags/{obj_sha}"))
        peeled_obj = self._json_object(peeled).get("object")
        if not isinstance(peeled_obj, dict) or peeled_obj.get("type") != "commit":
            raise ReleaseIntegrityError(f"annotated tag {tag} did not peel to a commit")
        commit_sha = peeled_obj.get("sha")
        if not isinstance(commit_sha, str) or _SHA.fullmatch(commit_sha) is None:
            raise ReleaseIntegrityError(f"annotated tag {tag} is missing a commit sha")
        return commit_sha

    def _get_release(self, tag: str) -> dict[str, object] | None:
        quoted = urllib.parse.quote(tag, safe="")
        result = self._request_status("GET", self._api(f"/releases/tags/{quoted}"))
        if result.status == 404:
            return None
        if result.status >= 400:
            self._fail(result)
        payload = self._json_object(result)
        if payload.get("tag_name") != tag:
            raise ReleaseIntegrityError("GitHub returned a release for a different tag")
        return payload

    def _list_releases(self) -> list[RemoteRelease]:
        releases: list[RemoteRelease] = []
        url: str | None = self._api("/releases?per_page=100")
        pages = 0
        while url is not None:
            if pages >= _MAX_RELEASE_PAGES:
                raise ReleaseIntegrityError(
                    "GitHub release list is still paginated after "
                    f"{_MAX_RELEASE_PAGES} pages; refusing to choose Latest"
                )
            pages += 1
            result = self._request("GET", url)
            payload = json.loads(result.body.decode("utf-8"))
            if not isinstance(payload, list):
                raise ReleaseIntegrityError("GitHub release list was not a list")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                releases.append(_remote_release(item))
            url = _next_link(result.headers)
            if url is not None:
                _guard_url(url)
        return releases

    def _create_release(self, notes: ReleaseNotes, plan: ReleasePlan) -> dict[str, object]:
        # tag_name refers to the tag _require_tag already observed. target_commitish
        # is omitted so GitHub is not asked to create a ref.
        body = {
            "tag_name": notes.tag,
            "name": notes.title,
            "body": notes.body,
            "draft": False,
            "prerelease": plan.prerelease,
            "make_latest": "true" if plan.make_latest else "false",
            "generate_release_notes": False,
        }
        result = self._request("POST", self._api("/releases"), json_body=body)
        return self._json_object(result)

    def _update_release(
        self,
        release_id: int,
        notes: ReleaseNotes,
        plan: ReleasePlan,
    ) -> dict[str, object]:
        # Do not send tag_name or target_commitish. Either one can move a ref.
        body = {
            "name": notes.title,
            "body": notes.body,
            "draft": False,
            "prerelease": plan.prerelease,
            "make_latest": "true" if plan.make_latest else "false",
        }
        result = self._request("PATCH", self._api(f"/releases/{release_id}"), json_body=body)
        return self._json_object(result)

    def _mark_latest(self, tag: str) -> None:
        release = self._get_release(tag)
        if release is None:
            raise ReleaseIntegrityError(f"cannot mark missing release {tag} as Latest")
        self._request(
            "PATCH",
            self._api(f"/releases/{int(release['id'])}"),
            json_body={"make_latest": "true"},
        )

    def _upload_assets(self, release_id: int, paths: Sequence[Path]) -> None:
        current = self._get_release_by_id(release_id)
        assets = current.get("assets")
        if not isinstance(assets, list):
            assets = []
        names = {path.name for path in paths}
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("name") not in names:
                continue
            asset_id = int(asset["id"])
            result = self._request_status(
                "DELETE",
                self._api(f"/releases/assets/{asset_id}"),
            )
            if result.status not in {204, 404}:
                self._fail(result)
        for path in paths:
            quoted = urllib.parse.quote(path.name, safe="")
            url = (
                "https://uploads.github.com/repos/"
                f"{self.repository}/releases/{release_id}/assets?name={quoted}"
            )
            self._request(
                "POST",
                url,
                raw=path.read_bytes(),
                content_type="application/octet-stream",
            )

    def _get_release_by_id(self, release_id: int) -> dict[str, object]:
        result = self._request("GET", self._api(f"/releases/{release_id}"))
        return self._json_object(result)

    def _request_status(self, method: str, url: str) -> HttpResult:
        _guard_url(url)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "hermes-a365-release-integrity",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            return self.transport(method, url, None, headers)
        except ReleaseIntegrityError:
            raise
        except Exception as exc:
            raise ReleaseIntegrityError(scrub(str(exc), self.token)) from None

    @staticmethod
    def _json_object(result: HttpResult) -> dict[str, object]:
        payload = json.loads(result.body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ReleaseIntegrityError("GitHub response was not an object")
        return payload


def _remote_release(item: Mapping[str, object]) -> RemoteRelease:
    tag = str(item.get("tag_name") or "")
    version: SemVer | None
    try:
        version = parse_semver(tag[1:]) if tag.startswith("v") else None
    except ReleaseIntegrityError:
        version = None
    release_id = item.get("id")
    if not isinstance(release_id, int):
        raise ReleaseIntegrityError("GitHub release is missing an id")
    return RemoteRelease(
        tag=tag,
        version=version,
        prerelease=bool(item.get("prerelease")),
        draft=bool(item.get("draft")),
        release_id=release_id,
    )


def _next_link(headers: Mapping[str, str]) -> str | None:
    link = ""
    for key, value in headers.items():
        if key.lower() == "link":
            link = value
            break
    if not link:
        return None
    for part in link.split(","):
        if 'rel="next"' not in part:
            continue
        url = part.split(";", 1)[0].strip()
        if url.startswith("<") and url.endswith(">"):
            return url[1:-1]
    return None


def _load_notes(metadata_path: Path, notes_path: Path) -> ReleaseNotes:
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        body = notes_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseIntegrityError(f"could not read release metadata: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReleaseIntegrityError("release metadata must be an object")
    tag = raw.get("tag")
    version = raw.get("version")
    title = raw.get("title")
    prerelease = raw.get("prerelease")
    if not all(isinstance(item, str) and item for item in (tag, version, title)):
        raise ReleaseIntegrityError("release metadata is missing tag, version, or title")
    if not isinstance(prerelease, bool):
        raise ReleaseIntegrityError("release metadata prerelease must be a boolean")
    if tag != tag_from_ref(str(tag)) or version != tag[1:]:
        raise ReleaseIntegrityError("release metadata tag and version disagree")
    if not body.strip():
        raise ReleaseIntegrityError("release notes are empty")
    return ReleaseNotes(
        tag=str(tag),
        version=str(version),
        title=str(title),
        body=body,
        prerelease=prerelease,
    )


def _add_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, default=Path("."))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser("check", help="compare the tag, versions, and CHANGELOG")
    _add_repo(check_parser)
    check_parser.add_argument("--tag-ref", required=True)
    check_parser.add_argument("--sha")
    check_parser.add_argument("--output-dir", type=Path, required=True)

    tag_parser = sub.add_parser("version-from-tag", help="print the version encoded by a tag")
    tag_parser.add_argument("--tag-ref", required=True)

    project_parser = sub.add_parser(
        "version-from-project",
        help="print project.version when it matches hermes_a365.__version__",
    )
    _add_repo(project_parser)

    dists = sub.add_parser("check-dists", help="validate wheel and sdist core metadata")
    dists.add_argument("--dist", type=Path, required=True)
    dists.add_argument("--expected-version", required=True)

    smoke = sub.add_parser("smoke", help="smoke-test the installed package")
    smoke.add_argument("--expected-version", required=True)

    publish = sub.add_parser(
        "publish-release",
        help="create or update the GitHub Release for an existing tag",
    )
    publish.add_argument("--metadata", type=Path, required=True)
    publish.add_argument("--notes", type=Path, required=True)
    publish.add_argument("--dist", type=Path, required=True)
    publish.add_argument("--sha", required=True)

    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    try:
        if args.command == "check":
            notes = check(args.repo_root, args.tag_ref, args.sha)
            write_notes(notes, args.output_dir)
            print(f"release integrity ok for {notes.tag}")
        elif args.command == "version-from-tag":
            print(version_from_tag(args.tag_ref))
        elif args.command == "version-from-project":
            found_project = project_version(args.repo_root)
            found_module = module_version(args.repo_root)
            if found_project != found_module:
                raise ReleaseIntegrityError(
                    f"project.version {found_project!r} != hermes_a365.__version__ {found_module!r}"
                )
            print(found_project)
        elif args.command == "check-dists":
            check_distributions(args.dist, args.expected_version)
            print(f"distribution metadata ok for {args.expected_version}")
        elif args.command == "smoke":
            smoke_installed(args.expected_version)
            print(f"wheel smoke ok for {args.expected_version}")
        elif args.command == "publish-release":
            repository = os.environ.get("GITHUB_REPOSITORY", "")
            notes = _load_notes(args.metadata, args.notes)
            client = GitHubReleases(
                repository,
                token or "",
                _default_transport(token or ""),
            )
            client.publish(notes, args.dist, expected_sha=args.sha)
        else:  # pragma: no cover - argparse requires a known command
            raise ReleaseIntegrityError(f"unknown command {args.command}")
    except ReleaseIntegrityError as exc:
        print(scrub(str(exc), token), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
