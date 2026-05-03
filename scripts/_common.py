"""Shared helpers for hermes-a365 scripts.

Path conventions:
- Repo root contains ``scripts/``, ``templates/``, ``references/`` as siblings.
- When running from the installed Hermes skill location
  (``~/.hermes/hermes-agent/optional-skills/cloud-platforms/hermes-a365/``)
  the layout is the same.
- ``skill_root()`` resolves to the parent of this file's ``scripts/`` directory.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import jinja2


def skill_root() -> Path:
    """Return the directory that contains scripts/, templates/, references/."""
    return Path(__file__).resolve().parent.parent


def templates_dir() -> Path:
    return skill_root() / "templates"


def safe_run(argv: list[str], *, timeout: float = 5.0) -> str | None:
    """Run a subprocess; return combined stdout+stderr on success, ``None`` on any failure.

    Used by probes and reconcilers that need to shell out without raising.
    Captures both streams so a tool that prints version info to stderr
    (some `--version` implementations do) is still surfaced.
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
    return (proc.stdout + proc.stderr).strip() or None


def tcp_reachable(host: str, *, port: int = 443, timeout: float = 3.0) -> bool:
    """Return True if a TCP connection to ``(host, port)`` succeeds within ``timeout``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


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
