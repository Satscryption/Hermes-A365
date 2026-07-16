"""Shared helpers for hermes_a365 modules.

Packaged Jinja templates live under ``hermes_a365/_data/templates/`` and are
resolved via :func:`templates_dir`, which uses ``importlib.resources`` so the
lookup works for both editable installs and wheels.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from importlib import resources
from pathlib import Path

import jinja2


def templates_dir() -> Path:
    """Filesystem path to the packaged ``_data/templates/`` directory."""
    return Path(str(resources.files("hermes_a365._data").joinpath("templates")))


def safe_run(argv: list[str], *, timeout: float = 5.0) -> str | None:
    """Run a subprocess; return combined stdout+stderr on success, ``None`` on failure.

    "Failure" means: ``OSError`` from spawning (binary not on PATH /
    permission denied), :class:`subprocess.TimeoutExpired`, or a
    non-zero exit code. Successful invocations return the combined
    output string — **including the empty string** when the process
    exited cleanly with no output. Slice 18m fixed the older
    ``... or None`` contract that conflated empty-success with
    failure (caused doctor's ``probe_custom_client_app`` to misread
    "app not found" as "az not signed in?").

    Used by probes and reconcilers that need to shell out without
    raising. Captures both streams so a tool that prints version
    info to stderr (some `--version` implementations do) is still
    surfaced.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout + proc.stderr).strip()


def tcp_reachable(host: str, *, port: int = 443, timeout: float = 3.0) -> bool:
    """Return True if a TCP connection to ``(host, port)`` succeeds within ``timeout``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def write_owner_only_text_atomic(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path``, owner-only (0600) from birth.

    Secret-safe ordering (#112 / CS-004): the temp file is created with
    ``O_CREAT | O_EXCL`` at mode 0600 *before* any bytes are written, so
    under a permissive umask (e.g. 022) neither the temp file nor the
    final path is ever group/world-readable while it holds secret
    material — ``os.replace`` carries the 0600 mode to the final path.
    ``O_EXCL`` also refuses to write through a pre-planted temp file or
    symlink (the write fails closed instead); a stale temp left by a
    crashed prior run is removed first.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    try:
        with fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Convert an agent display name to its canonical local-dir slug.

    Lowercase, runs of any non-alphanumeric character collapse to a
    single hyphen, leading/trailing hyphens trimmed. Matches the slug
    convention operators are expected to pass to ``hermes a365 instance
    create <slug>`` — so cleanup / status can locate the local agent
    dir without the operator having to repeat the slug manually.

    Examples:
        ``slugify("Hermes Inbox Helper")`` → ``"hermes-inbox-helper"``
        ``slugify("Foo_Bar 99")``           → ``"foo-bar-99"``
        ``slugify("---")``                   → ``""`` (empty — caller
        should reject)
    """
    return _SLUG_NON_ALNUM_RE.sub("-", name.lower()).strip("-")


def validate_slug(slug: str) -> str:
    """Return ``slug`` iff it is safe to join under ``~/.hermes/agents/``.

    Guards the agent-dir path joins against traversal (#103 / M9): the
    slug must be a non-empty single relative path component — no
    separators, no ``.``/``..``, no NUL. Raises :class:`ValueError`
    otherwise. Deliberately looser than :func:`slugify` (existing
    operator slugs may carry case or dots); this is a safety gate at the
    filesystem boundary, not a normalizer.
    """
    if not slug:
        raise ValueError("agent slug must be non-empty")
    if slug in (".", ".."):
        raise ValueError(f"agent slug must not be a dot component: {slug!r}")
    if "/" in slug or "\\" in slug or "\x00" in slug:
        raise ValueError(
            f"agent slug must not contain path separators or NUL: {slug!r}"
        )
    return slug


def ensure_contained(path: Path, root: Path) -> None:
    """Raise :class:`ValueError` unless ``path`` resolves inside ``root``.

    Belt-and-braces companion to :func:`validate_slug` (#103 / M9) for
    the destructive primitives (env writes, cleanup unlink/rmdir):
    resolves symlinks on both sides, so a traversal-shaped or
    symlinked path that escapes the agents root fails closed before
    any write or delete happens.
    """
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise ValueError(f"{path} escapes {root}")


def parse_env(text: str) -> dict[str, str]:
    """Parse a simple ``KEY=value`` env file into a dict.

    Skips blank lines and ``#`` comments. Strips matched single/double quotes
    from values. Does not support multi-line values, escapes, or interpolation.
    Sufficient for the ``.env`` format this skill produces.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


def deep_diff(
    actual: object,
    desired: object,
    *,
    path: str = "",
) -> dict[str, tuple[object, object]]:
    """Compare two JSON-like structures; return differing leaf paths.

    Returns a mapping ``{path: (actual_value, desired_value)}`` for every leaf
    that differs. Used by the reconcilers to produce idempotent PATCH plans
    against A365's blueprint and Entra app state.

    Path notation:
    - ``""`` for the root
    - ``"foo"`` for a top-level dict key
    - ``"foo/bar"`` for a nested dict key
    - ``"items[3]"`` for a list index

    Comparison semantics:
    - Type mismatch is treated as a single root-level diff (no recursion).
    - Lists are compared **positionally**; reordered lists report each
      differing index. Callers that want set-comparison semantics for
      specific paths should sort their inputs before calling.
    - ``bool`` is *not* equal to its int form: ``True != 1`` here, even
      though Python's ``==`` says otherwise. This matters for JSON round-trips.
    """
    # bool is a subclass of int in Python but they're distinct in JSON.
    if isinstance(actual, bool) != isinstance(desired, bool):
        key = path or "$"
        return {key: (actual, desired)}

    # Different container types (e.g. dict vs list, list vs str) → root diff.
    if type(actual) is not type(desired):
        key = path or "$"
        return {key: (actual, desired)}

    if isinstance(desired, dict):
        assert isinstance(actual, dict)
        keys = sorted(set(actual.keys()) | set(desired.keys()))
        out: dict[str, tuple[object, object]] = {}
        for k in keys:
            child = f"{path}/{k}" if path else str(k)
            if k not in actual:
                out[child] = (None, desired[k])
            elif k not in desired:
                out[child] = (actual[k], None)
            else:
                out.update(deep_diff(actual[k], desired[k], path=child))
        return out

    if isinstance(desired, list):
        assert isinstance(actual, list)
        if len(actual) != len(desired):
            key = path or "$"
            return {key: (actual, desired)}
        out = {}
        for i, (a, d) in enumerate(zip(actual, desired, strict=True)):
            out.update(deep_diff(a, d, path=f"{path}[{i}]"))
        return out

    if actual != desired:
        key = path or "$"
        return {key: (actual, desired)}
    return {}


def render_diff_human(diff: dict[str, tuple[object, object]]) -> str:
    """Render a deep_diff result as a human-friendly multi-line string.

    Empty diff renders as ``"(no differences)"``. Each line is one path with
    the actual → desired transition.
    """
    if not diff:
        return "(no differences)"
    lines = []
    width = max(len(p) for p in diff)
    for path in sorted(diff):
        actual, desired = diff[path]
        lines.append(f"  {path:<{width}}  {actual!r} -> {desired!r}")
    return "\n".join(lines)


def jinja_env(*, extra_searchpaths: list[Path] | None = None) -> jinja2.Environment:
    """Construct a Jinja environment rooted at ``templates/``.

    StrictUndefined: any unset variable raises rather than rendering empty.
    autoescape=False: we render JSON/.env/text, not HTML.
    keep_trailing_newline=True: deterministic output for golden-file tests.
    """
    searchpaths = [str(templates_dir())]
    if extra_searchpaths:
        searchpaths.extend(str(p) for p in extra_searchpaths)
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpaths),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
