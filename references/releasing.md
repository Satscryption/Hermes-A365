# Releasing hermes-a365

Operator flow for a PyPI release and its GitHub Release. The package
version, `hermes_a365.__version__`, and `CHANGELOG.md` stay in lockstep
with the tag. Nothing in this flow weakens the `Protect release tags`
ruleset or the `pypi` environment reviewer.

## Why the release is created first

`Protect release tags` (ruleset `21134661`) targets `refs/tags/v*` and
restricts creation, update, and deletion. It has no bypass actors.
`git push` of a `v*` tag is rejected, and the publish workflow's
`GITHUB_TOKEN` cannot create one either. Publishing a GitHub Release in
the web UI is the path that mints the tag. That is how `v0.9.0` was cut:
the release was published, the tag appeared, and `publish.yml` started.

The workflow therefore does not create tags. It checks the tag that the
UI just minted, publishes the already-built files to PyPI after the
reviewer approves the `pypi` environment, and only then rewrites that
one release with the changelog, title, Latest/prerelease flags, and
artifacts. If PyPI fails, the UI release stays a prerelease and is not
promoted.

## Before you mint the tag

1. Merge to protected `main`. The commit that will be tagged has to be
   reachable from `origin/main`. Target `main` in the Release UI. The
   preflight rejects a commit that is not an ancestor of `origin/main`
   when the tagged commit still contains this workflow. A tag cannot be
   moved or deleted afterwards. Tag pushes run the workflow file that is
   in the tagged commit, so a side-branch commit that removes the
   preflight does not fail closed by itself. See the approval note below.
2. Set `project.version` in `pyproject.toml` and `__version__` in
   `src/hermes_a365/__init__.py` to the same `MAJOR.MINOR.PATCH`.
3. Rename this file's `## Unreleased / X.Y.Z` heading to exactly:

   ```markdown
   ## [X.Y.Z] — YYYY-MM-DD
   ```

   The dash is an em dash (`—`), and the date has to be a real
   `YYYY-MM-DD`. One section per version. The first sentence of the
   first paragraph becomes the GitHub Release theme. An
   `Unreleased / …` heading is not a release section.
4. Run the preflight locally, from a checkout of that commit, before
   opening the Release UI. A failed preflight after the tag exists
   leaves an immutable tag and no PyPI release; the next attempt needs
   a new version.

   ```bash
   git fetch --no-tags origin main
   python scripts/release_integrity.py check \
     --tag-ref refs/tags/vX.Y.Z \
     --sha "$(git rev-parse HEAD)" \
     --output-dir /tmp/hermes-a365-release-check
   ```

   `--sha` checks that `HEAD` is that commit, that the worktree matches
   it, and that the commit is contained in `origin/main`. Inspect
   `/tmp/hermes-a365-release-check/notes.md` and `metadata.json`. Do not
   publish those files yourself.

## Mint the tag

On GitHub: **Releases → Draft a new release**.

- **Choose a tag:** `vX.Y.Z` (create it on publish).
- **Target:** `main`, at the commit checked above.
- **Title and description:** placeholders. The workflow replaces both.
- Check **Set as a pre-release**.
- Leave **Set as the latest release** unchecked.
- Publish the release. Do not use `git push` to create the tag, and do
  not add a ruleset bypass so that Actions can.

Publishing the release creates `refs/tags/vX.Y.Z` and starts
**Publish to PyPI**.

## What the workflow does

1. **integrity** (no PyPI credential): strict semver, `origin/main`
   ancestry, and agreement between the tag, `project.version`,
   `__version__`, and the changelog section. A mismatch fails here,
   before the `pypi` environment is queued.
2. **test:** the same hermetic ruff and pytest suite as pull requests,
   on Python 3.11 and 3.14, against the tagged commit.
3. **build** (no PyPI credential): hash-pinned `build` / hatchling,
   with `SOURCE_DATE_EPOCH` set from the tagged commit's committer time,
   sdist and wheel, core-metadata validation (name, version,
   `Requires-Python` covering 3.11 and 3.14, markdown description),
   and a fresh-environment install of that wheel (`import hermes_a365`,
   `hermes-a365 --help`, the `hermes_agent.plugins` entry point,
   `plugin.yaml`, and the packaged templates). Digests are recorded
   before that install and checked again afterwards.
4. **publish:** waits for the required `pypi` environment reviewer,
   checks the artifact checksums, and uploads those same files.
   `skip-existing` makes a rerun skip a file PyPI already has. The
   approval-test dispatch still enters this environment and still skips
   the PyPI action. It does not continue on to rewrite the GitHub Release.
5. **release** (only after PyPI succeeds, only for a `v*` tag, and not
   for an approval-test dispatch): updates the UI release in place, or
   creates the release object if the tag already exists and the release
   object does not. The tag must resolve to the workflow commit. Title is
   `vX.Y.Z — <first sentence>`. Notes are the changelog section.
   A semver prerelease stays a prerelease and is not Latest. A stable
   version is Latest only when no other stable release is newer or
   equal, so a back-filled patch cannot steal Latest. Before any asset
   is replaced, the wheel and sdist sha256 must match the digests the
   PyPI JSON API publishes for that version. The wheel, sdist, and
   `SHA256SUMS` are then attached, replacing any previous copies of
   those names. A mismatch fails and leaves the release unchanged.

## Before you approve the pypi deployment

The `pypi` environment has no deployment branch policy, and this
repository's settings are left that way. A tag push runs
`.github/workflows/publish.yml` from the tagged commit, not from the
tip of `main`. Before approving the deployment, confirm the tagged
commit is on `main` (the run's SHA is an ancestor of `main`). A tag
that points at a side-branch commit which deletes the preflight still
reaches this reviewer.

There is still one release for the tag. The workflow never creates a
second one and never creates a tag.

## Reruns

- Preflight or test or build failed: the tag already exists and cannot
  be deleted. Fix forward with a new patch version. Do not point the
  tag at a different commit.
- PyPI succeeded and **Finalize the GitHub Release** failed: use
  **Re-run failed jobs**. That reruns the release job only. It reuses
  the original artifact, does not ask for a second PyPI approval, and
  does not upload the distributions to PyPI again. It still refuses to
  replace GitHub Release assets unless their sha256 matches PyPI.
- **Re-run all jobs** rebuilds the tagged commit with
  `SOURCE_DATE_EPOCH` set to that commit's committer time, waits for
  the `pypi` reviewer again, and skips files PyPI already has. The
  release job then compares the rebuilt wheel and sdist to the sha256
  digests PyPI publishes. It uploads those assets only when every
  digest matches. If the rebuilt bytes differ, the finalizer fails and
  does not replace the GitHub Release files.

## What not to change

- Do not add a bypass actor to `Protect release tags` so the workflow
  can create tags.
- Do not remove the required reviewer on the `pypi` environment, and
  do not give the build, test, integrity, or release jobs
  `id-token: write`.
- Do not rebuild after approval. The publish job uploads the artifact
  the build job already checked.
