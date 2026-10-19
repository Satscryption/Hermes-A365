"""Release-integrity preflight, distribution checks, and GitHub Release finalizer."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "release_integrity.py"
_ROOT = Path(__file__).parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("release_integrity", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

_CHANGELOG = """# Changelog

## Unreleased / 1.2.3

- not a release section

## [1.2.3] — 2026-01-02

First sentence stays intact even with 1.0 inside.
Second sentence starts here.

### Added

- something

## [1.2.2] — 2026-01-01

Older.
"""


def _repo(
    tmp_path: Path,
    *,
    project: str = "1.2.3",
    module: str = "1.2.3",
    changelog: str = _CHANGELOG,
) -> Path:
    root = tmp_path / "repo"
    package = root / "src" / "hermes_a365"
    package.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "hermes-a365"\nversion = "{project}"\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(f'__version__ = "{module}"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return root


def _metadata(version: str, *, requires: str = ">=3.11", name: str = "hermes-a365") -> str:
    return (
        "Metadata-Version: 2.4\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        "Summary: Integrate Hermes agents into the Microsoft 365 ecosystem.\n"
        f"Requires-Python: {requires}\n"
        "Description-Content-Type: text/markdown\n"
        "\n"
        "Hello.\n"
    )


def _dist(tmp_path: Path, metadata: str, version: str = "1.2.3") -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    wheel = dist / f"hermes_a365-{version}-py3-none-any.whl"
    info = f"hermes_a365-{version}.dist-info/METADATA"
    with mod.zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(info, metadata)
    sdist = dist / f"hermes_a365-{version}.tar.gz"
    with mod.tarfile.open(sdist, "w:gz") as archive:
        payload = metadata.encode()
        member = mod.tarfile.TarInfo(name=f"hermes_a365-{version}/PKG-INFO")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    lines = []
    for path in (wheel, sdist):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dist


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


TAG_SHA = "a" * 40


class FakeGitHub:
    def __init__(
        self,
        *,
        tag: str = "v1.2.3",
        tag_exists: bool = True,
        tag_sha: str = TAG_SHA,
        tag_type: str = "commit",
        peeled_sha: str | None = None,
        release: dict | None = None,
        others: list[dict] | None = None,
        release_pages: list[list[dict]] | None = None,
        token: str = "super-secret-token",
        fail: bool = False,
    ) -> None:
        self.tag = tag
        self.tag_exists = tag_exists
        self.tag_sha = tag_sha
        self.tag_type = tag_type
        self.peeled_sha = peeled_sha
        self.release = release
        self.others = list(others or [])
        self.release_pages = release_pages
        self.token = token
        self.fail = fail
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.assets: dict[int, list[dict]] = {}
        if release is not None:
            self.assets[int(release["id"])] = list(release.get("assets") or [])

    def _release_page(self, parsed: urllib.parse.ParseResult) -> mod.HttpResult:
        query = urllib.parse.parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        index = page - 1
        pages = self.release_pages or []
        if index < 0 or index >= len(pages):
            return mod.HttpResult(200, {}, b"[]")
        headers: dict[str, str] = {}
        if index + 1 < len(pages):
            next_url = (
                "https://api.github.com/repos/octo/repo/releases?per_page=100&page="
                f"{page + 1}"
            )
            headers["Link"] = f'<{next_url}>; rel="next"'
        return mod.HttpResult(200, headers, json.dumps(pages[index]).encode())

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, dict(headers)))
        assert headers["Authorization"] == f"Bearer {self.token}"
        parsed = urllib.parse.urlparse(url)
        assert parsed.hostname in {"api.github.com", "uploads.github.com"}
        if self.fail:
            return mod.HttpResult(500, {}, f"boom {self.token}".encode())
        path = parsed.path
        if "/git/ref/tags/" in path:
            if method != "GET":
                raise AssertionError(method)
            if not self.tag_exists:
                return mod.HttpResult(404, {}, b'{"message":"Not Found"}')
            payload = {"object": {"type": self.tag_type, "sha": self.tag_sha}}
            return mod.HttpResult(200, {}, json.dumps(payload).encode())
        if "/git/tags/" in path and method == "GET":
            payload = {
                "object": {"type": "commit", "sha": self.peeled_sha or self.tag_sha},
            }
            return mod.HttpResult(200, {}, json.dumps(payload).encode())
        if path.endswith("/releases") and method == "GET":
            if self.release_pages is not None:
                return self._release_page(parsed)
            payload = self.others + ([] if self.release is None else [self.release])
            return mod.HttpResult(200, {}, json.dumps(payload).encode())
        if "/releases/tags/" in path and method == "GET":
            wanted = path.rsplit("/", 1)[-1]
            if self.release is not None and wanted == urllib.parse.quote(self.tag, safe=""):
                return mod.HttpResult(200, {}, json.dumps(self.release).encode())
            candidates = list(self.others)
            for page in self.release_pages or []:
                candidates.extend(page)
            winner = next(
                (
                    item
                    for item in candidates
                    if wanted == urllib.parse.quote(item["tag_name"], safe="")
                ),
                None,
            )
            if winner is None:
                return mod.HttpResult(404, {}, b'{"message":"Not Found"}')
            return mod.HttpResult(200, {}, json.dumps(winner).encode())
        if path.endswith("/releases") and method == "POST":
            created = {
                "id": 99,
                "tag_name": self.tag,
                "html_url": f"https://github.com/octo/repo/releases/tag/{self.tag}",
                "assets": [],
            }
            self.release = created
            self.assets[99] = []
            return mod.HttpResult(201, {}, json.dumps(created).encode())
        if "/releases/assets/" in path and method == "DELETE":
            return mod.HttpResult(204, {}, b"")
        if "/releases/" in path and method == "GET" and parsed.hostname == "api.github.com":
            release_id = int(path.rstrip("/").rsplit("/", 1)[1])
            payload = dict(self.release or {})
            payload["assets"] = self.assets.get(release_id, [])
            payload["id"] = release_id
            return mod.HttpResult(200, {}, json.dumps(payload).encode())
        if "/releases/" in path and method == "PATCH":
            sent = json.loads(body.decode())
            if self.release is not None and path.endswith(f"/{self.release['id']}"):
                self.release = {**self.release, **sent}
                return mod.HttpResult(200, {}, json.dumps(self.release).encode())
            patched = {"id": int(path.rsplit("/", 1)[1]), **sent}
            return mod.HttpResult(200, {}, json.dumps(patched).encode())
        if parsed.hostname == "uploads.github.com" and method == "POST":
            return mod.HttpResult(201, {}, b'{"state":"uploaded"}')
        raise AssertionError((method, url))


class FakePyPI:
    def __init__(
        self,
        digests: dict[str, str] | None = None,
        *,
        status: int = 200,
        fail: bool = False,
    ) -> None:
        self.digests = dict(digests or {})
        self.status = status
        self.fail = fail
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method, url, body, headers):
        del body
        self.calls.append((method, url, dict(headers)))
        assert "Authorization" not in headers
        assert urllib.parse.urlparse(url).hostname == "pypi.org"
        if self.fail:
            raise RuntimeError("pypi unreachable")
        if self.status != 200:
            return mod.HttpResult(self.status, {}, b'{"message":"no"}')
        urls = [
            {"filename": name, "digests": {"sha256": digest}}
            for name, digest in self.digests.items()
        ]
        return mod.HttpResult(200, {}, json.dumps({"urls": urls}).encode())


def _matching_pypi(dist: Path) -> FakePyPI:
    digests = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in dist.iterdir()
        if path.name != "SHA256SUMS"
    }
    return FakePyPI(digests)


def _client(github: FakeGitHub, dist: Path, pypi: FakePyPI | None = None) -> mod.GitHubReleases:
    return mod.GitHubReleases(
        "octo/repo",
        github.token,
        github,
        pypi_transport=pypi or _matching_pypi(dist),
    )


def _notes(tag: str = "v1.2.3", *, prerelease: bool = False) -> mod.ReleaseNotes:
    version = tag[1:]
    return mod.ReleaseNotes(
        tag=tag,
        version=version,
        title=f"{tag} \u2014 Theme.",
        body=f"## [{version}] \u2014 2026-01-02\n\nTheme.\n",
        prerelease=prerelease,
    )


def _remote(tag: str, release_id: int, *, prerelease: bool = False, draft: bool = False) -> dict:
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [],
        "html_url": f"https://github.com/octo/repo/releases/tag/{tag}",
    }


def test_matching_tag_versions_and_changelog_agree(tmp_path: Path) -> None:
    notes = mod.evaluate(_repo(tmp_path), "refs/tags/v1.2.3")

    assert notes.tag == "v1.2.3"
    assert notes.version == "1.2.3"
    assert notes.prerelease is False
    assert notes.title == "v1.2.3 \u2014 First sentence stays intact even with 1.0 inside."
    assert notes.body.startswith("## [1.2.3] \u2014 2026-01-02\n")
    assert "Unreleased" not in notes.body
    assert "Older." not in notes.body


def test_live_0_9_0_section_still_parses() -> None:
    text = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading, body = mod.changelog_section(text, "0.9.0")

    assert heading.startswith("## [0.9.0] \u2014 2026-10-16")
    assert "reviewer-gated PyPI publishing" in body


def test_live_0_9_1_section_still_parses() -> None:
    notes = mod.evaluate(_ROOT, "refs/tags/v0.9.1")

    assert notes.version == "0.9.1"
    assert notes.title == (
        "v0.9.1 \u2014 Operator walk and release integrity (#124)."
    )
    assert len(notes.title) <= mod.TITLE_LIMIT
    assert "main@24126496" in notes.body


def test_unreleased_heading_is_not_a_release_section(tmp_path: Path) -> None:
    changelog = "## Unreleased / 9.9.9\n\n- pending\n"
    with pytest.raises(mod.ReleaseIntegrityError, match="exactly one"):
        mod.evaluate(
            _repo(tmp_path, project="9.9.9", module="9.9.9", changelog=changelog),
            "v9.9.9",
        )


def test_reports_every_disagreement(tmp_path: Path) -> None:
    root = _repo(tmp_path, project="0.1.0", module="0.2.0", changelog="# none\n")

    with pytest.raises(mod.ReleaseIntegrityError) as caught:
        mod.evaluate(root, "refs/tags/v1.2.3")

    message = str(caught.value)
    assert "project.version" in message
    assert "hermes_a365.__version__" in message
    assert "CHANGELOG.md" in message


def test_duplicate_changelog_section_fails(tmp_path: Path) -> None:
    changelog = _CHANGELOG + "\n## [1.2.3] — 2026-02-02\n\nAgain.\n"
    with pytest.raises(mod.ReleaseIntegrityError, match="2 sections"):
        mod.evaluate(_repo(tmp_path, changelog=changelog), "v1.2.3")


def test_hyphen_heading_and_bad_date_are_rejected(tmp_path: Path) -> None:
    hyphen = "## [1.2.3] - 2026-01-02\n\nNotes.\n"
    with pytest.raises(mod.ReleaseIntegrityError, match="exactly one"):
        mod.evaluate(_repo(tmp_path, changelog=hyphen), "v1.2.3")
    bad_date = "## [1.2.3] — 2026-99-99\n\nNotes.\n"
    with pytest.raises(mod.ReleaseIntegrityError, match="invalid date"):
        mod.evaluate(_repo(tmp_path, changelog=bad_date), "v1.2.3")


@pytest.mark.parametrize(
    "tag_ref",
    ["v1", "v1.2", "v01.2.3", "v1.2.3.4", "v1.2.3+build", "refs/heads/v1.2.3", "1.2.3"],
)
def test_non_semver_tags_are_rejected(tag_ref: str) -> None:
    with pytest.raises(mod.ReleaseIntegrityError, match="non-semver"):
        mod.tag_from_ref(tag_ref)


def test_prerelease_tag_is_classified_and_ordered() -> None:
    notes = mod.ReleaseNotes("v1.2.3-rc.1", "1.2.3-rc.1", "t", "b", True)
    assert mod.parse_semver("1.2.3-rc.1").is_prerelease is True
    assert mod.parse_semver("1.0.0-alpha") < mod.parse_semver("1.0.0-alpha.1")
    assert mod.parse_semver("1.0.0-alpha.1") < mod.parse_semver("1.0.0-alpha.beta")
    assert mod.parse_semver("1.0.0-1") < mod.parse_semver("1.0.0-alpha")
    assert mod.parse_semver("1.0.0-rc.1") < mod.parse_semver("1.0.0")
    assert not (mod.parse_semver("1.2.3") < mod.parse_semver("1.2.3"))
    plan = mod.plan_release(
        notes,
        [
            mod.RemoteRelease("v1.2.0", mod.parse_semver("1.2.0"), False, False, 1),
        ],
    )
    assert plan.prerelease is True
    assert plan.make_latest is False
    assert plan.move_latest_to == "v1.2.0"


def test_backfilled_patch_does_not_steal_latest() -> None:
    plan = mod.plan_release(
        _notes("v0.8.6"),
        [mod.RemoteRelease("v0.9.0", mod.parse_semver("0.9.0"), False, False, 4)],
    )

    assert plan.make_latest is False
    assert plan.move_latest_to == "v0.9.0"
    assert plan.prerelease is False


def test_newer_stable_release_becomes_latest() -> None:
    plan = mod.plan_release(
        _notes("v0.9.1"),
        [mod.RemoteRelease("v0.9.0", mod.parse_semver("0.9.0"), False, False, 4)],
    )

    assert plan == mod.ReleasePlan(False, True, None)


def test_ancestor_check_accepts_main_and_rejects_side_commits(tmp_path: Path) -> None:
    repo = tmp_path / "git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "release-integrity@example.com")
    _git(repo, "config", "user.name", "release-integrity")
    (repo / "file").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "file")
    _git(repo, "commit", "-m", "one")
    main_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", main_sha)
    _git(repo, "checkout", "-b", "side")
    (repo / "file").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-am", "two")
    side_sha = _git(repo, "rev-parse", "HEAD")

    mod.assert_reachable(repo, main_sha)
    with pytest.raises(mod.ReleaseIntegrityError, match="not reachable from origin/main"):
        mod.assert_reachable(repo, side_sha)


def test_sha_must_be_full_hex_before_git_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_git(*_args, **_kwargs):
        raise AssertionError("git should not run")

    monkeypatch.setattr(mod.subprocess, "run", fail_git)
    with pytest.raises(mod.ReleaseIntegrityError, match="40-character"):
        mod.assert_reachable(Path("."), "HEAD; touch pwned")
    with pytest.raises(mod.ReleaseIntegrityError, match="40-character"):
        mod.assert_worktree(Path("."), "HEAD; touch pwned")


def test_check_binds_the_worktree_to_the_requested_sha(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "release-integrity@example.com")
    _git(root, "config", "user.name", "release-integrity")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "release")
    sha = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", sha)

    mod.check(root, "refs/tags/v1.2.3", sha)

    (root / "CHANGELOG.md").write_text(
        (root / "CHANGELOG.md").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(mod.ReleaseIntegrityError, match="worktree differs"):
        mod.check(root, "refs/tags/v1.2.3", sha)

    _git(root, "checkout", "--", "CHANGELOG.md")
    (root / "file").write_text("next\n", encoding="utf-8")
    _git(root, "add", "file")
    _git(root, "commit", "-m", "next")
    _git(root, "update-ref", "refs/remotes/origin/main", _git(root, "rev-parse", "HEAD"))
    with pytest.raises(mod.ReleaseIntegrityError, match="worktree HEAD is not"):
        mod.check(root, "refs/tags/v1.2.3", sha)


def test_check_combines_version_and_ancestry_failures(tmp_path: Path) -> None:
    root = _repo(tmp_path, project="9.9.9")
    with pytest.raises(mod.ReleaseIntegrityError) as caught:
        mod.check(root, "refs/tags/v1.2.3", "not-a-sha")

    message = str(caught.value)
    assert "project.version" in message
    assert "40-character" in message


def test_cli_writes_notes_and_rejects_a_bad_tag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out"
    assert (
        mod.main(
            [
                "check",
                "--repo-root",
                str(_repo(tmp_path)),
                "--tag-ref",
                "refs/tags/v1.2.3",
                "--output-dir",
                str(out),
            ]
        )
        == 0
    )
    metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["version"] == "1.2.3"
    assert metadata["prerelease"] is False
    assert (out / "notes.md").read_text(encoding="utf-8").startswith("## [1.2.3]")

    assert mod.main(["version-from-tag", "--tag-ref", "v01.2.3"]) == 1
    assert "non-semver" in capsys.readouterr().err


def test_distribution_metadata_must_match_and_keep_python_3_14(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3"))
    mod.check_distributions(dist, "1.2.3")

    excluded = _dist(tmp_path / "old", _metadata("1.2.3", requires=">=3.11,<3.14"))
    with pytest.raises(mod.ReleaseIntegrityError, match=r"3\.14"):
        mod.check_distributions(excluded, "1.2.3")

    mismatched = _dist(tmp_path / "mismatch", _metadata("1.2.2"), version="1.2.2")
    with pytest.raises(mod.ReleaseIntegrityError, match="Version"):
        mod.check_distributions(mismatched, "1.2.3")


_RC_CHANGELOG = """# Changelog

## [1.2.3-rc.1] — 2026-01-02

Release candidate.
"""


def test_prerelease_artifact_accepts_pep440_spelling(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3rc1"), version="1.2.3rc1")
    mod.check_distributions(dist, "1.2.3-rc.1")


def test_prerelease_artifact_rejects_a_different_version(tmp_path: Path) -> None:
    different = _dist(tmp_path, _metadata("1.2.3rc2"), version="1.2.3rc2")
    with pytest.raises(mod.ReleaseIntegrityError, match="Version"):
        mod.check_distributions(different, "1.2.3-rc.1")

    renamed = _dist(
        tmp_path / "name",
        _metadata("1.2.3rc1"),
        version="1.2.3rc2",
    )
    with pytest.raises(mod.ReleaseIntegrityError, match="Version"):
        mod.check_distributions(renamed, "1.2.3-rc.1")


def test_source_version_keeps_the_semver_spelling(tmp_path: Path) -> None:
    normalized = _repo(
        tmp_path,
        project="1.2.3rc1",
        module="1.2.3rc1",
        changelog=_RC_CHANGELOG,
    )
    with pytest.raises(mod.ReleaseIntegrityError, match=r"project\.version"):
        mod.evaluate(normalized, "refs/tags/v1.2.3-rc.1")

    matching = _repo(
        tmp_path / "semver",
        project="1.2.3-rc.1",
        module="1.2.3-rc.1",
        changelog=_RC_CHANGELOG,
    )
    notes = mod.evaluate(matching, "refs/tags/v1.2.3-rc.1")
    assert notes.version == "1.2.3-rc.1"
    assert notes.tag == "v1.2.3-rc.1"
    assert notes.prerelease is True


def test_checksum_file_uses_basenames_and_checks_beside_the_assets(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3"))
    text = (dist / "SHA256SUMS").read_text(encoding="utf-8")
    names = [line.split(maxsplit=1)[1] for line in text.splitlines() if line.strip()]

    assert names
    assert all("/" not in name and name == Path(name).name for name in names)
    mod.verify_checksums(dist)

    folder = tmp_path / "download"
    folder.mkdir()
    for path in dist.iterdir():
        (folder / path.name).write_bytes(path.read_bytes())
    completed = subprocess.run(
        ["sha256sum", "--check", "--strict", "SHA256SUMS"],
        cwd=folder,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_checksum_file_cannot_escape_dist(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3"))
    sums = dist / "SHA256SUMS"
    digest = sums.read_text(encoding="utf-8").split()[0]
    sums.write_text(f"{digest}  ../outside.whl\n", encoding="utf-8")

    with pytest.raises(mod.ReleaseIntegrityError, match="unsafe SHA256SUMS path"):
        mod.verify_checksums(dist)

    prefixed = _dist(tmp_path / "prefixed", _metadata("1.2.3"))
    prefixed_sums = prefixed / "SHA256SUMS"
    rewritten = [
        f"{line.split(maxsplit=1)[0]}  dist/{line.split(maxsplit=1)[1]}"
        for line in prefixed_sums.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    prefixed_sums.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    with pytest.raises(mod.ReleaseIntegrityError, match="unsafe SHA256SUMS path"):
        mod.verify_checksums(prefixed)


def test_smoke_accepts_the_development_install() -> None:
    version = mod.project_version(_ROOT)
    mod.smoke_installed(version)


def test_smoke_accepts_a_normalized_prerelease_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_a365

    monkeypatch.setattr(hermes_a365, "__version__", "1.2.3rc1")
    monkeypatch.setattr(mod.importlib.metadata, "version", lambda _name: "1.2.3rc1")
    mod.smoke_installed("1.2.3-rc.1")


def test_smoke_rejects_a_different_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_a365

    monkeypatch.setattr(hermes_a365, "__version__", "1.2.3rc2")
    monkeypatch.setattr(mod.importlib.metadata, "version", lambda _name: "1.2.3rc2")
    with pytest.raises(mod.ReleaseIntegrityError, match="__version__"):
        mod.smoke_installed("1.2.3-rc.1")


def test_missing_tag_does_not_create_a_release_or_ref(tmp_path: Path) -> None:
    github = FakeGitHub(tag_exists=False)
    dist = _dist(tmp_path, _metadata("1.2.3"))
    client = _client(github, dist)

    with pytest.raises(mod.ReleaseIntegrityError, match="does not create or push tags"):
        client.publish(_notes(), dist, expected_sha=TAG_SHA)

    assert [call[0] for call in github.calls] == ["GET"]
    assert "/git/ref/tags/" in github.calls[0][1]


def test_existing_release_is_updated_without_touching_the_tag(tmp_path: Path) -> None:
    github = FakeGitHub(
        tag="v1.2.4",
        release=_remote("v1.2.4", 7, prerelease=True),
        others=[_remote("v1.2.3", 4)],
    )
    github.assets[7] = [{"id": 11, "name": "hermes_a365-1.2.4-py3-none-any.whl"}]
    dist = _dist(tmp_path, _metadata("1.2.4"), version="1.2.4")
    client = _client(github, dist)

    client.publish(_notes("v1.2.4"), dist, expected_sha=TAG_SHA)

    methods = [call[0] for call in github.calls]
    assert "POST" in methods  # asset upload only
    release_writes = [
        json.loads(call[2].decode())
        for call in github.calls
        if call[0] == "PATCH" and call[2] is not None and "/assets" not in call[1]
    ]
    assert release_writes
    assert "target_commitish" not in release_writes[0]
    assert "tag_name" not in release_writes[0]
    assert release_writes[0]["prerelease"] is False
    assert release_writes[0]["make_latest"] == "true"
    assert release_writes[0]["body"].startswith("## [1.2.4]")
    release_posts = [
        call
        for call in github.calls
        if call[0] == "POST" and call[1].rstrip("/").endswith("/releases")
    ]
    assert release_posts == []
    assert any(call[0] == "DELETE" for call in github.calls)


def test_create_uses_the_existing_tag_and_a_rerun_updates(tmp_path: Path) -> None:
    github = FakeGitHub(release=None, others=[_remote("v1.2.2", 3)])
    dist = _dist(tmp_path, _metadata("1.2.3"))
    client = _client(github, dist)

    client.publish(_notes(), dist, expected_sha=TAG_SHA)
    created = [
        json.loads(call[2].decode())
        for call in github.calls
        if call[0] == "POST" and urllib.parse.urlparse(call[1]).path.endswith("/releases")
    ]
    assert len(created) == 1
    assert created[0]["tag_name"] == "v1.2.3"
    assert "target_commitish" not in created[0]
    assert created[0]["generate_release_notes"] is False

    client.publish(_notes(), dist, expected_sha=TAG_SHA)
    created_again = [
        call
        for call in github.calls
        if call[0] == "POST" and urllib.parse.urlparse(call[1]).path.endswith("/releases")
    ]
    assert len(created_again) == 1


def test_older_release_moves_latest_back(tmp_path: Path) -> None:
    github = FakeGitHub(tag="v0.8.6", release=_remote("v0.8.6", 8), others=[_remote("v0.9.0", 4)])
    dist = _dist(tmp_path, _metadata("0.8.6"), version="0.8.6")
    client = _client(github, dist)

    client.publish(_notes("v0.8.6"), dist, expected_sha=TAG_SHA)

    patches = [
        (call[1], json.loads(call[2].decode()))
        for call in github.calls
        if call[0] == "PATCH" and call[2] is not None
    ]
    assert patches[0][1]["make_latest"] == "false"
    assert patches[1][0].endswith("/releases/4")
    assert patches[1][1] == {"make_latest": "true"}


def test_api_errors_do_not_print_the_token(tmp_path: Path) -> None:
    github = FakeGitHub(fail=True, token="super-secret-token")
    dist = _dist(tmp_path, _metadata("1.2.3"))
    client = _client(github, dist)

    with pytest.raises(mod.ReleaseIntegrityError) as caught:
        client.publish(_notes(), dist, expected_sha=TAG_SHA)

    assert "super-secret-token" not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_credentials_are_not_sent_off_github() -> None:
    with pytest.raises(mod.ReleaseIntegrityError, match=r"evil\.example"):
        mod._guard_url("https://evil.example/repos")
    with pytest.raises(mod.ReleaseIntegrityError, match="credentials"):
        mod._guard_url("https://user:super-secret-token@api.github.com/repos")
    with pytest.raises(mod.ReleaseIntegrityError, match=r"pypi\.org"):
        mod._guard_url("https://pypi.org/pypi/hermes-a365/1.2.3/json")


def test_tag_must_resolve_to_the_workflow_sha(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3"))
    github = FakeGitHub(tag_sha="b" * 40)
    client = _client(github, dist)

    with pytest.raises(mod.ReleaseIntegrityError, match="not the workflow commit"):
        client.publish(_notes(), dist, expected_sha=TAG_SHA)

    assert [call[0] for call in github.calls] == ["GET"]
    assert not any(call[0] in {"POST", "PATCH", "DELETE"} for call in github.calls)


def test_annotated_tag_is_peeled_before_the_sha_check(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3"))
    github = FakeGitHub(tag_type="tag", tag_sha="c" * 40, peeled_sha=TAG_SHA)
    client = _client(github, dist)

    client.publish(_notes(), dist, expected_sha=TAG_SHA)

    assert any("/git/tags/" in call[1] for call in github.calls)


def test_pypi_digest_mismatch_does_not_replace_assets(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3"))
    github = FakeGitHub(release=_remote("v1.2.3", 7))
    pypi = FakePyPI(
        {
            "hermes_a365-1.2.3-py3-none-any.whl": "0" * 64,
            "hermes_a365-1.2.3.tar.gz": "1" * 64,
        }
    )
    client = _client(github, dist, pypi)

    with pytest.raises(mod.ReleaseIntegrityError, match="does not match PyPI"):
        client.publish(_notes(), dist, expected_sha=TAG_SHA)

    assert pypi.calls
    assert all("Authorization" not in call[2] for call in pypi.calls)
    assert not any(call[0] in {"POST", "PATCH", "DELETE"} for call in github.calls)


def test_missing_pypi_file_refuses_before_upload(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3"))
    github = FakeGitHub()
    wheel = next(path.name for path in dist.iterdir() if path.suffix == ".whl")
    pypi = FakePyPI({wheel: hashlib.sha256((dist / wheel).read_bytes()).hexdigest()})
    client = _client(github, dist, pypi)

    with pytest.raises(mod.ReleaseIntegrityError, match="PyPI has no"):
        client.publish(_notes(), dist, expected_sha=TAG_SHA)

    assert not any(call[0] in {"POST", "PATCH", "DELETE"} for call in github.calls)


def test_unreachable_pypi_refuses_before_upload(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("1.2.3"))
    github = FakeGitHub()
    client = _client(github, dist, FakePyPI(fail=True))

    with pytest.raises(mod.ReleaseIntegrityError, match="PyPI digest lookup failed"):
        client.publish(_notes(), dist, expected_sha=TAG_SHA)

    assert not any(call[0] in {"POST", "PATCH", "DELETE"} for call in github.calls)


def test_newer_stable_release_past_the_first_page_blocks_latest(tmp_path: Path) -> None:
    dist = _dist(tmp_path, _metadata("0.8.6"), version="0.8.6")
    github = FakeGitHub(
        tag="v0.8.6",
        release=_remote("v0.8.6", 8),
        release_pages=[
            [_remote("v0.0.1", 1)],
            [_remote("v9.9.9", 50)],
        ],
    )
    client = _client(github, dist)

    client.publish(_notes("v0.8.6"), dist, expected_sha=TAG_SHA)

    patches = [
        json.loads(call[2].decode())
        for call in github.calls
        if call[0] == "PATCH" and call[2] is not None
    ]
    assert patches[0]["make_latest"] == "false"
    assert patches[1] == {"make_latest": "true"}
    assert any(call[1].endswith("/releases/50") for call in github.calls)


def test_incomplete_release_pagination_refuses_to_choose_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "_MAX_RELEASE_PAGES", 1)
    dist = _dist(tmp_path, _metadata("1.2.3"))
    github = FakeGitHub(
        release=_remote("v1.2.3", 7),
        release_pages=[[_remote("v0.0.1", 1)], [_remote("v9.9.9", 50)]],
    )
    client = _client(github, dist)

    with pytest.raises(mod.ReleaseIntegrityError, match="still paginated"):
        client.publish(_notes(), dist, expected_sha=TAG_SHA)

    assert not any(call[0] in {"POST", "PATCH", "DELETE"} for call in github.calls)


def test_workflow_and_script_do_not_push_tags() -> None:
    script = _SCRIPT.read_text(encoding="utf-8")
    workflow = (_ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    for text in (script, workflow):
        assert "git push" not in text
        assert "git tag" not in text
    assert '["git", "merge-base", "--is-ancestor"' in script
