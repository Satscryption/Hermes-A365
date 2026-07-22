"""Shared helpers for hermes_a365 modules.

Packaged Jinja templates live under ``hermes_a365/_data/templates/`` and are
resolved via :func:`templates_dir`, which uses ``importlib.resources`` so the
lookup works for both editable installs and wheels.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from urllib.parse import quote

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


def write_owner_only_text_atomic(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Atomically write ``text`` to ``path``, owner-only (``mode``) from birth.

    Secret-safe ordering (#112 / CS-004): the temp file is created with
    ``O_CREAT | O_EXCL`` at ``mode`` (default 0600) *before* any bytes are
    written, so under a permissive umask (e.g. 022) neither the temp file
    nor the final path is ever group/world-readable while it holds secret
    material — ``os.replace`` carries the mode to the final path.
    ``O_EXCL`` also refuses to write through a pre-planted temp file or
    symlink (the write fails closed instead); a stale temp left by a
    crashed prior run is removed first. Parent directories are created if
    absent. This is the single owner-only atomic writer every
    secret-bearing file in the package should route through.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        # O_EXCL created the file at `mode`, but the umask may have cleared
        # bits (e.g. request 0640 under umask 027 → 0600); force it exact.
        os.fchmod(fd, mode)
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


def quote_path_segment(value: str) -> str:
    """Percent-encode ``value`` as a single, inert URL path segment (#103 / M4).

    Inbound conversation / activity ids are interpolated into outbound Bot
    Framework REST URLs. ``quote(safe="")`` neutralises ``/``, ``?``, ``#``
    — but ``.`` is RFC-3986 *unreserved*, so a bare ``.`` or ``..`` id
    survives encoding and still renders a live dot-segment that URL
    normalisation collapses (``…/conversations/../activities`` →
    ``…/activities``), shifting the request target on the trusted host.
    Percent-encode those dots too so no id can contribute a dot-segment.
    """
    seg = quote(value, safe="")
    if seg in (".", ".."):
        seg = seg.replace(".", "%2E")
    return seg


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


def resolve_expected_tenant(
    hermes_home: Path, explicit: str | None
) -> tuple[str | None, str]:
    """#102 M7: which tenant destructive/provisioning CLI runs must bind to.

    Precedence: an explicit ``--tenant-id`` wins; else the persisted
    ``A365_TENANT_ID`` from ``<hermes_home>/.env`` (the operator env
    ``status``/``doctor`` already treat as authoritative); else ``None`` —
    meaning the caller has nothing to pin against and must say so rather than
    silently trusting the ambient az/a365 session. Returns ``(tenant, source)``
    where ``source`` is operator-readable provenance for messages."""
    if explicit and explicit.strip():
        return explicit.strip(), "--tenant-id"
    env_file = hermes_home / ".env"
    if env_file.exists():
        try:
            tenant = parse_env(env_file.read_text()).get("A365_TENANT_ID", "").strip()
        except OSError:
            tenant = ""
        if tenant:
            return tenant, f"{env_file} A365_TENANT_ID"
    return None, "unpinned"


def active_az_tenant(run_fn: Callable[[list[str]], object]) -> str | None:
    """#102 M7: the ambient az session's ``tenantId``, or ``None`` when it
    cannot be determined (az missing, not logged in, unparseable output).

    ``run_fn`` executes an argv and returns an object with ``stdout`` (the
    caller's mutator/runner seam — kept as a callable so this module stays
    free of the mutator dependency). Callers enforcing a tenant pin MUST
    treat ``None`` as fail-closed: an unverifiable session is not a match.

    Parsing is TOLERANT of leading noise (#102 review): the production
    mutator merges stderr into stdout, so ``az -o json`` output is routinely
    prefixed by benign az notices (upgrade-available WARNING, preview
    banners). A strict ``json.loads`` would fail-close a session whose
    tenant actually matches. ``--only-show-errors`` suppresses most of the
    noise at the source; the first-balanced-object decode handles the rest
    (mirroring ``register._extract_first_json_object``'s documented lesson
    about this exact stream shape)."""
    try:
        result = run_fn(["az", "account", "show", "-o", "json", "--only-show-errors"])
    except Exception:
        return None
    stdout = str(getattr(result, "stdout", "") or "")
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(stdout[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tenant = str(data.get("tenantId") or "").strip()
    return tenant or None


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
