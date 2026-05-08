"""hermes a365 status — per-component status report.

Narrowed to the four components the verified GA CLI surface actually
supports.

- ``local_config``     — parent ``~/.hermes/.env`` (and agent .env if a slug
  is given) parseable + required keys present.
- ``blueprint_scopes`` — ``a365 query-entra blueprint-scopes`` for the
  agent's blueprint.
- ``instance_scopes``  — ``a365 query-entra instance-scopes`` for the
  agent's instance.
- ``activity_bridge``  — local PID-file probe (only when agent_name given).
  Reports ``ok`` when ``bridge.pid`` exists and the recorded PID is alive,
  ``missing`` when no pidfile is present (bridge not currently running),
  and ``error`` on stale pidfile / unreadable contents. The bridge itself
  has shipped — runtime walkthroughs are in ``references/live-tenant-test.md``
  §9c (standalone serve) and §9d (Hermes plugin path).

Components dropped vs v0.1:
- ``license`` — A365 SKUs are queried via Microsoft Graph subscribedSkus,
  not via a365 CLI. Future v0.3 may add a Graph-based component.
- ``app (T1)`` / ``app (T2)`` — no per-tier app split in v0.2.
- ``blueprint`` / ``instance`` — folded into the new ``*_scopes`` reads.
- ``channels`` — channel deployment is operator-side (M365 Admin Centre)
  and has no CLI query surface.
- ``telemetry`` — ``a365 query-entra --telemetry`` does not exist.
- ``fic`` — ``a365 query-entra --fic`` does not exist.

Exit codes (unchanged from v0.1):
- ``0`` — overall ``ok``
- ``1`` — at least one ``warn`` / ``missing`` (partial)
- ``2`` — at least one ``error`` (broken)
- ``3`` — not yet bootstrapped (parent .env absent)

The cloud reads gracefully degrade to ``skipped`` when ``a365`` isn't on
PATH OR when the read returns nothing within the timeout (typically
because the CLI is waiting on interactive auth).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from _common import parse_env, safe_run, slugify

ComponentState = Literal["ok", "warn", "error", "missing", "skipped"]
OverallState = Literal["ok", "partial", "broken", "uninitialized"]

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

_OK = "ok"
_WARN = "warn"
_ERROR = "error"
_MISSING = "missing"
_SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class StatusComponent:
    name: str
    state: ComponentState
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class StatusReport:
    agent_name: str | None
    components: list[StatusComponent]

    @property
    def overall(self) -> OverallState:
        if self._is_uninitialized():
            return "uninitialized"
        if any(c.state == _ERROR for c in self.components):
            return "broken"
        if any(c.state in (_WARN, _MISSING) for c in self.components):
            return "partial"
        return "ok"

    def _is_uninitialized(self) -> bool:
        return any(c.name == "local_config" and c.state == _MISSING for c in self.components)


def overall_to_exit_code(o: OverallState) -> int:
    return {"ok": 0, "partial": 1, "broken": 2, "uninitialized": 3}[o]


# ---------------------------------------------------------------------------
# QuerySource — the v0.2 query-entra surface
# ---------------------------------------------------------------------------


class QuerySource(Protocol):
    """Two methods, matching the GA ``a365 query-entra`` surface."""

    available: bool

    def query_blueprint_scopes(
        self, *, agent_name: str, tenant_id: str | None = None
    ) -> str | None: ...

    def query_instance_scopes(
        self, *, agent_name: str, tenant_id: str | None = None
    ) -> str | None: ...


class _UnavailableQuerySource:
    name = "unavailable"
    available = False

    def query_blueprint_scopes(
        self, *, agent_name: str, tenant_id: str | None = None
    ) -> str | None:
        return None

    def query_instance_scopes(self, *, agent_name: str, tenant_id: str | None = None) -> str | None:
        return None


class A365CliQuerySource:
    """Shells out to ``a365 query-entra`` and returns raw stdout text.

    The CLI emits human-readable text (no JSON option in v1.1.171), so
    callers grep / pattern-match the returned string. ``None`` means the
    call failed or timed out — typically because the CLI is waiting on
    interactive auth (device-code flow on macOS).
    """

    name = "a365-cli"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.available = shutil.which("a365") is not None
        self._timeout = timeout

    def _query_scopes(self, sub: str, *, agent_name: str, tenant_id: str | None) -> str | None:
        if not self.available:
            return None
        argv = ["a365", "query-entra", sub, "--agent-name", agent_name]
        if tenant_id:
            argv.extend(["--tenant-id", tenant_id])
        return safe_run(argv, timeout=self._timeout)

    def query_blueprint_scopes(
        self, *, agent_name: str, tenant_id: str | None = None
    ) -> str | None:
        return self._query_scopes("blueprint-scopes", agent_name=agent_name, tenant_id=tenant_id)

    def query_instance_scopes(self, *, agent_name: str, tenant_id: str | None = None) -> str | None:
        return self._query_scopes("instance-scopes", agent_name=agent_name, tenant_id=tenant_id)


def get_query_source() -> QuerySource:
    cli = A365CliQuerySource()
    return cli if cli.available else _UnavailableQuerySource()


# ---------------------------------------------------------------------------
# Local-only gatherers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def gather_local_config(hermes_home: Path, agent_name: str | None) -> StatusComponent:
    """Read ``~/.hermes/.env`` and (if name given) ``~/.hermes/agents/<name>/.env``."""
    env_file = hermes_home / ".env"
    if not env_file.exists():
        return StatusComponent(
            "local_config",
            _MISSING,
            f"{env_file} does not exist; run `hermes a365 register --apply` first",
            {"hermes_home": str(hermes_home)},
        )
    try:
        env = parse_env(env_file.read_text())
    except OSError as e:
        return StatusComponent(
            "local_config",
            _ERROR,
            f"~/.hermes/.env unreadable: {e}",
            {"hermes_home": str(hermes_home)},
        )

    needed = {"A365_TENANT_ID", "A365_APP_ID"}
    missing = needed - env.keys()
    if missing:
        return StatusComponent(
            "local_config",
            _WARN,
            f"~/.hermes/.env missing keys: {sorted(missing)}",
            {"hermes_home": str(hermes_home), "missing_keys": sorted(missing)},
        )

    data: dict[str, Any] = {
        "hermes_home": str(hermes_home),
        "tenant_id": env.get("A365_TENANT_ID"),
        "app_id": env.get("A365_APP_ID"),
    }
    detail_parts = [
        f"tenant={env.get('A365_TENANT_ID')}",
        f"app_id={(env.get('A365_APP_ID') or '')[:8]}…",
    ]

    if agent_name:
        # The positional ``agent_name`` may be either a CLI display name
        # ("Hermes Inbox Helper") or a slug ("inbox-helper"). Try the raw
        # value first (operators who already know the slug), then fall back
        # to slugify(). Closes 2026-05-05 walkthrough bug #12.
        candidates = [agent_name]
        derived = slugify(agent_name)
        if derived and derived != agent_name:
            candidates.append(derived)

        agent_env_file: Path | None = None
        for candidate in candidates:
            probe = hermes_home / "agents" / candidate / ".env"
            if probe.exists():
                agent_env_file = probe
                break

        if agent_env_file is None:
            tried = " or ".join(
                str(hermes_home / "agents" / c / ".env") for c in candidates
            )
            return StatusComponent(
                "local_config",
                _WARN,
                f"agent .env missing: tried {tried}",
                data,
            )
        try:
            agent_env = parse_env(agent_env_file.read_text())
        except OSError as e:
            return StatusComponent(
                "local_config",
                _ERROR,
                f"agent .env unreadable: {e}",
                data,
            )
        data["agent_env"] = agent_env
        data["agent_dir"] = str(agent_env_file.parent)
        data["aa_instance_id"] = agent_env.get("AA_INSTANCE_ID")
        detail_parts.append(
            f"agent={agent_env_file.parent.name} ({len(agent_env)} keys)"
        )

    return StatusComponent("local_config", _OK, " | ".join(detail_parts), data)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def gather_activity_bridge(hermes_home: Path, agent_name: str) -> StatusComponent:
    """Inspect the activity-bridge PID file (if any)."""
    pid_file = hermes_home / "agents" / agent_name / "bridge.pid"
    if not pid_file.exists():
        return StatusComponent(
            "activity_bridge",
            _MISSING,
            (
                "bridge.pid not found (bridge not currently running; "
                "start via §9c or §9d in references/live-tenant-test.md)"
            ),
            {"pid_file": str(pid_file)},
        )
    try:
        text = pid_file.read_text().strip()
        pid = int(text)
    except (OSError, ValueError) as e:
        return StatusComponent(
            "activity_bridge",
            _ERROR,
            f"bridge.pid unreadable: {e}",
            {"pid_file": str(pid_file)},
        )
    if _process_alive(pid):
        return StatusComponent(
            "activity_bridge",
            _OK,
            f"pid={pid} (alive)",
            {"pid": pid, "alive": True, "pid_file": str(pid_file)},
        )
    return StatusComponent(
        "activity_bridge",
        _ERROR,
        f"pid={pid} not running (stale pidfile at {pid_file})",
        {"pid": pid, "alive": False, "pid_file": str(pid_file)},
    )


# ---------------------------------------------------------------------------
# Cloud-driven gatherers (via QuerySource)
# ---------------------------------------------------------------------------


def _skipped(name: str, *, reason: str = "a365 CLI unavailable") -> StatusComponent:
    return StatusComponent(name, _SKIPPED, reason)


# Slice 18v: the GA `a365 query-entra blueprint-scopes` doesn't actually
# print "consented" or "granted" — it prints "Successfully retrieved
# inheritable permissions from Graph API" then lists each resource under
# an `Inheritable Scopes:` header. Slice 18q tightened the hint list
# (dropped "ok" because of substring false-positives) but didn't replace
# it with a hint that matches real CLI output, so the classifier
# returned "warn" for actually-consented blueprints. The hints below
# are pinned against verified v1.1.171 output (round-2 walkthrough,
# 2026-05-05).
_CONSENTED_HINTS = (
    "consented",
    "granted",
    "inheritable scopes",
    "successfully retrieved",
)
_NOT_CONSENTED_HINTS = ("not consented", "missing", "not granted", "consent required")

# Lines the CLI emits as progress markers rather than data. The 2026-05-05
# walkthrough caught the unclassifiable-warn path latching onto
# "Querying Entra ID for agent blueprint inheritable permissions..." (the
# first thing the CLI prints) and showing it as the result detail (bug
# #13). We strip these before picking a representative line.
_PROGRESS_LINE_PREFIXES = (
    "querying ",
    "checking ",
    "resolving ",
    "authenticating",
    "loading ",
    "[debug]",
    "[info]",
)


def _meaningful_line(text: str) -> str:
    """Return the first non-progress, non-blank line of ``text``.

    Used as the fall-through detail when no consent-state hint matches.
    Skips lines that end with `…`/`...` or start with a present-progressive
    verb the CLI uses for status messages. Returns ``""`` if everything
    looks like progress.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith("...") or line.endswith("…"):
            continue
        if line.lower().startswith(_PROGRESS_LINE_PREFIXES):
            continue
        return line
    return ""


def _classify_scopes_output(text: str) -> tuple[ComponentState, str]:
    """Heuristic classifier over the CLI's human-readable scope output.

    The exact phrasing isn't pinned in v1.1.171; we look for common
    consent-state hints. When unclassifiable, default to ``warn`` with
    the first non-progress line as detail — the operator can re-run with
    ``--verbose`` for full text.
    """
    lower = text.lower()
    if any(hint in lower for hint in _NOT_CONSENTED_HINTS):
        return (_WARN, "scopes present but at least one not consented")
    if any(hint in lower for hint in _CONSENTED_HINTS):
        return (_OK, "scopes consented")
    representative = _meaningful_line(text)
    return (_WARN, representative[:80] or "unclassifiable scope output")


def gather_blueprint_scopes(
    qs: QuerySource, *, agent_name: str | None, tenant_id: str | None
) -> StatusComponent:
    if not qs.available:
        return _skipped("blueprint_scopes")
    if not agent_name:
        return StatusComponent(
            "blueprint_scopes",
            _MISSING,
            "no agent name given; pass <agent-name> to query",
        )
    text = qs.query_blueprint_scopes(agent_name=agent_name, tenant_id=tenant_id)
    if text is None:
        return StatusComponent(
            "blueprint_scopes",
            _SKIPPED,
            "query-entra returned nothing (likely awaiting interactive auth)",
        )
    state, detail = _classify_scopes_output(text)
    return StatusComponent("blueprint_scopes", state, detail, {"raw": text})


def gather_instance_scopes(
    qs: QuerySource, *, agent_name: str | None, tenant_id: str | None
) -> StatusComponent:
    if not qs.available:
        return _skipped("instance_scopes")
    if not agent_name:
        return StatusComponent(
            "instance_scopes",
            _MISSING,
            "no agent name given; pass <agent-name> to query",
        )
    text = qs.query_instance_scopes(agent_name=agent_name, tenant_id=tenant_id)
    if text is None:
        return StatusComponent(
            "instance_scopes",
            _SKIPPED,
            "query-entra returned nothing (likely awaiting interactive auth)",
        )
    state, detail = _classify_scopes_output(text)
    return StatusComponent("instance_scopes", state, detail, {"raw": text})


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def collect_status(
    agent_name: str | None,
    *,
    hermes_home: Path | None = None,
    query_source: QuerySource | None = None,
    tenant_id: str | None = None,
) -> StatusReport:
    """Build the full status report."""
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    if query_source is None:
        query_source = get_query_source()

    components: list[StatusComponent] = []

    local = gather_local_config(hermes_home, agent_name)
    components.append(local)

    # If we're not bootstrapped at all, skip the cloud probes — they'd
    # all return missing/skipped anyway and the result is just noise.
    if local.state == _MISSING:
        return StatusReport(agent_name=None, components=components)

    # Tenant id for cloud reads — prefer caller-supplied, fall back to
    # the parent .env value, then to None (CLI auto-detects via az).
    if tenant_id is None:
        tenant_id = local.data.get("tenant_id") or None

    components.append(
        gather_blueprint_scopes(query_source, agent_name=agent_name, tenant_id=tenant_id)
    )
    components.append(
        gather_instance_scopes(query_source, agent_name=agent_name, tenant_id=tenant_id)
    )

    if agent_name:
        components.append(gather_activity_bridge(hermes_home, agent_name))

    return StatusReport(agent_name=agent_name, components=components)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_human(report: StatusReport) -> str:
    if not report.components:
        return "(no components gathered)\n"

    name_w = max(len(c.name) for c in report.components)
    state_w = max(len(c.state) for c in report.components)
    name_w = max(name_w, len("Component"))
    state_w = max(state_w, len("State"))

    lines = []
    if report.agent_name:
        lines.append(f"hermes a365 status — {report.agent_name}")
    else:
        lines.append("hermes a365 status — (skill-wide)")
    lines.append("-" * 60)
    lines.append(f"{'Component':<{name_w}}  {'State':<{state_w}}  Detail")
    lines.append(f"{'-' * name_w}  {'-' * state_w}  ------")
    for c in report.components:
        lines.append(f"{c.name:<{name_w}}  {c.state:<{state_w}}  {c.detail}")
    skipped = sum(1 for c in report.components if c.state == _SKIPPED)
    lines.append("")
    lines.append(f"overall: {report.overall}")
    if skipped:
        lines.append(
            f"note: {skipped} component(s) skipped (a365 CLI unavailable or awaiting auth)"
        )
    return "\n".join(lines) + "\n"


def render_json(report: StatusReport) -> str:
    payload = {
        "agent_name": report.agent_name,
        "overall": report.overall,
        "components": [asdict(c) for c in report.components],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description="hermes a365 status — per-component status report.",
        )
    parser.add_argument(
        "agent_name",
        nargs="?",
        help="agent name to report on; omit for skill-wide status",
    )
    parser.add_argument("--tenant-id", help="override tenant id used for cloud reads")
    parser.add_argument("--human", action="store_true", help="markdown-aligned output")
    return parser


def run(args: argparse.Namespace) -> int:
    report = collect_status(args.agent_name, tenant_id=args.tenant_id)
    sys.stdout.write(render_human(report) if args.human else render_json(report))
    return overall_to_exit_code(report.overall)


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
