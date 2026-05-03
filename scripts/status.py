"""hermes a365 status — orchestrate per-component status into a single report.

Spec: SPEC.md §6.11. Aggregates state across nine components — license, T1
app, T2 app, blueprint, instance, channels, activity bridge, telemetry, FIC —
into one report with exit code semantics:

- ``0`` — all components ``ok`` (skipped components don't fail the overall)
- ``1`` — at least one ``warn`` or ``missing`` (partial)
- ``2`` — at least one ``error`` (broken)
- ``3`` — skill not yet bootstrapped (no ``~/.hermes/.env``)

Local components (``local_config``, ``activity_bridge``) read the filesystem
directly. Cloud components are sourced via the ``QuerySource`` protocol;
``A365CliQuerySource`` shells out to ``a365 query-entra``. Tests substitute
``FakeQuerySource``.

The ``a365 query-entra`` JSON shapes are **speculative** — Microsoft does not
publish them. Parsers are defensive (every ``payload.get(...)``); refinement
happens once we test against a live tenant.
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

from _common import parse_env, safe_run

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
    """A complete status snapshot. ``overall`` is derived, not stored."""

    agent_slug: str | None
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
# QuerySource — abstraction over `a365 query-entra` calls
# ---------------------------------------------------------------------------


class QuerySource(Protocol):
    """Each method returns parsed JSON on success, ``None`` on miss/failure."""

    available: bool

    def query_license(self) -> dict[str, Any] | None: ...
    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None: ...
    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None: ...
    def query_consent(self, *, app_id: str) -> dict[str, Any] | None: ...
    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None: ...
    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None: ...
    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None: ...
    def query_fic(self, *, app_id: str) -> dict[str, Any] | None: ...


class _UnavailableQuerySource:
    """Returned when ``a365`` CLI is not on PATH; every method returns None."""

    name = "unavailable"
    available = False

    def query_license(self) -> dict[str, Any] | None:
        return None

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None:
        return None

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return None

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return None


class A365CliQuerySource:
    """Shells out to ``a365 query-entra`` for each query.

    Each ``--<flag>`` shape below is **speculative** based on SPEC §3.3 / §6.x.
    Refinement happens once we test against a live tenant.
    """

    name = "a365-cli"

    def __init__(self) -> None:
        self.available = shutil.which("a365") is not None

    def _run(self, *flags: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        out = safe_run(["a365", "query-entra", *flags], timeout=10.0)
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    def query_license(self) -> dict[str, Any] | None:
        return self._run("--license")

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return self._run("--by-app-id", app_id)

    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None:
        return self._run("--by-name", name)

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        return self._run("--consent-status", "--app", app_id)

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return self._run("--blueprint", slug)

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return self._run("--instance", instance_id)

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return self._run("--telemetry", "--instance", instance_id)

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return self._run("--fic", "--app", app_id)


def get_query_source() -> QuerySource:
    """Return a CLI-backed QuerySource if available, else an unavailable stub."""
    cli = A365CliQuerySource()
    if cli.available:
        return cli
    return _UnavailableQuerySource()


# ---------------------------------------------------------------------------
# Local-only gatherers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def gather_local_config(hermes_home: Path, agent_slug: str | None) -> StatusComponent:
    """Read ``~/.hermes/.env`` and (if slug given) ``~/.hermes/agents/<slug>/.env``."""
    env_file = hermes_home / ".env"
    if not env_file.exists():
        return StatusComponent(
            "local_config",
            _MISSING,
            f"{env_file} does not exist; run `hermes a365 register` first",
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
        "license_model": env.get("A365_LICENSE_MODEL"),
        "cli_variant": env.get("A365_CLI_VARIANT"),
    }
    detail_parts = [
        f"tenant={env.get('A365_TENANT_ID')}",
        f"app_id={(env.get('A365_APP_ID') or '')[:8]}…",
    ]

    # Optional per-agent .env
    if agent_slug:
        agent_env_file = hermes_home / "agents" / agent_slug / ".env"
        if not agent_env_file.exists():
            return StatusComponent(
                "local_config",
                _WARN,
                f"agent .env missing: {agent_env_file}",
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
        data["aa_instance_id"] = agent_env.get("AA_INSTANCE_ID")
        detail_parts.append(f"agent={agent_slug} ({len(agent_env)} keys)")

    return StatusComponent("local_config", _OK, " | ".join(detail_parts), data)


def _process_alive(pid: int) -> bool:
    """Return True iff the given pid is a live process visible to the caller."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours — still alive
        return True
    return True


def gather_activity_bridge(hermes_home: Path, agent_slug: str) -> StatusComponent:
    """Inspect the activity-bridge PID file and probe the process."""
    pid_file = hermes_home / "agents" / agent_slug / "bridge.pid"
    if not pid_file.exists():
        return StatusComponent(
            "activity_bridge",
            _MISSING,
            "bridge.pid not found (bridge not started)",
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


def _skipped(name: str) -> StatusComponent:
    return StatusComponent(name, _SKIPPED, "a365 CLI unavailable")


def gather_license(qs: QuerySource) -> StatusComponent:
    if not qs.available:
        return _skipped("license")
    payload = qs.query_license()
    if payload is None:
        return StatusComponent("license", _MISSING, "no license assigned to tenant")
    model = payload.get("model", "unknown")
    used = payload.get("seats_used")
    total = payload.get("seats_total")
    detail = str(model)
    if used is not None and total is not None:
        detail += f", {used} of {total} seats used"
    return StatusComponent("license", _OK, detail, payload)


def gather_t1_app(qs: QuerySource, *, app_id: str | None) -> StatusComponent:
    """T1 (first-party) app status. ``app_id`` is captured during register."""
    if not qs.available:
        return _skipped("app (T1)")
    if not app_id:
        return StatusComponent(
            "app (T1)",
            _MISSING,
            "no T1 appId recorded locally; run register",
        )
    payload = qs.query_app_by_id(app_id=app_id)
    if payload is None:
        return StatusComponent(
            "app (T1)",
            _MISSING,
            f"appId {app_id[:8]}… not found in tenant",
            {"app_id": app_id},
        )
    return StatusComponent(
        "app (T1)",
        _OK,
        f"appId={app_id[:8]}…",
        {"app_id": app_id, "payload": payload},
    )


def gather_t2_app(qs: QuerySource, *, app_id: str) -> StatusComponent:
    """T2 (confidential client) app status with consent state folded in."""
    if not qs.available:
        return _skipped("app (T2)")
    if not app_id:
        return StatusComponent(
            "app (T2)",
            _MISSING,
            "no T2 appId recorded locally; run register",
        )
    app = qs.query_app_by_id(app_id=app_id)
    if app is None:
        return StatusComponent(
            "app (T2)",
            _MISSING,
            f"appId {app_id[:8]}… not found in tenant",
            {"app_id": app_id},
        )
    consent = qs.query_consent(app_id=app_id) or {}
    granted = bool(consent.get("granted"))
    granted_date = consent.get("granted_date") or consent.get("grantedAt")
    if not granted:
        return StatusComponent(
            "app (T2)",
            _WARN,
            f"appId={app_id[:8]}…, consent=missing",
            {"app_id": app_id, "consent": consent, "payload": app},
        )
    detail = f"appId={app_id[:8]}…, consent=granted"
    if granted_date:
        detail += f" {granted_date}"
    return StatusComponent(
        "app (T2)",
        _OK,
        detail,
        {"app_id": app_id, "consent": consent, "payload": app},
    )


def gather_blueprint(qs: QuerySource, *, slug: str) -> StatusComponent:
    if not qs.available:
        return _skipped("blueprint")
    payload = qs.query_blueprint(slug=slug)
    if payload is None:
        return StatusComponent(
            "blueprint",
            _MISSING,
            f"blueprint {slug!r} not registered",
            {"slug": slug},
        )
    last_patched = payload.get("last_patched") or payload.get("lastPatched")
    detail = slug
    if last_patched:
        detail += f", last patched {last_patched}"
    return StatusComponent("blueprint", _OK, detail, payload)


def gather_instance(qs: QuerySource, *, instance_id: str | None) -> StatusComponent:
    if not qs.available:
        return _skipped("instance")
    if not instance_id:
        return StatusComponent(
            "instance",
            _MISSING,
            "no AA_INSTANCE_ID in agent .env; run `hermes a365 instance create`",
        )
    payload = qs.query_instance(instance_id=instance_id)
    if payload is None:
        return StatusComponent(
            "instance",
            _MISSING,
            f"instance {instance_id[:8]}… not found",
            {"instance_id": instance_id},
        )
    return StatusComponent(
        "instance",
        _OK,
        f"AA_INSTANCE_ID={instance_id[:8]}…",
        {"instance_id": instance_id, "payload": payload},
    )


def gather_channels(qs: QuerySource, *, instance_id: str | None) -> StatusComponent:
    if not qs.available:
        return _skipped("channels")
    if not instance_id:
        return StatusComponent("channels", _MISSING, "no instance to query")
    payload = qs.query_instance(instance_id=instance_id) or {}
    channels = payload.get("channels", {}) or {}
    if not channels:
        return StatusComponent(
            "channels",
            _MISSING,
            "no channels deployed",
            {"instance_id": instance_id},
        )
    # Expected shape: {"teams": "ok", "outlook": "ok", "m365copilot": "missing"}
    parts = [f"{name}={state}" for name, state in sorted(channels.items())]
    state: ComponentState = _OK
    if any(v != "ok" for v in channels.values()):
        state = _WARN
    return StatusComponent(
        "channels",
        state,
        " ".join(parts),
        {"instance_id": instance_id, "channels": channels},
    )


def gather_telemetry(qs: QuerySource, *, instance_id: str | None) -> StatusComponent:
    if not qs.available:
        return _skipped("telemetry")
    if not instance_id:
        return StatusComponent("telemetry", _MISSING, "no instance to query")
    payload = qs.query_telemetry(instance_id=instance_id)
    if payload is None:
        return StatusComponent(
            "telemetry",
            _MISSING,
            "no telemetry data available",
            {"instance_id": instance_id},
        )
    last_span = payload.get("last_span") or payload.get("lastSpan")
    sampler = payload.get("sampler", "")
    if not last_span:
        return StatusComponent(
            "telemetry",
            _WARN,
            f"no spans seen yet (sampler={sampler})",
            payload,
        )
    detail = f"last span {last_span}"
    if sampler:
        detail += f", sampler={sampler}"
    return StatusComponent("telemetry", _OK, detail, payload)


def gather_fic(qs: QuerySource, *, app_id: str | None) -> StatusComponent:
    if not qs.available:
        return _skipped("fic")
    if not app_id:
        return StatusComponent("fic", _MISSING, "no T2 appId recorded locally")
    payload = qs.query_fic(app_id=app_id)
    if payload is None:
        return StatusComponent(
            "fic",
            _MISSING,
            "no FIC configured",
            {"app_id": app_id},
        )
    expires = payload.get("expires") or payload.get("expiresAt")
    days_until = payload.get("days_until_expiry")
    if expires is None:
        return StatusComponent(
            "fic",
            _WARN,
            "FIC present but no expiry recorded",
            payload,
        )
    detail = f"expires {expires}"
    state: ComponentState = _OK
    if isinstance(days_until, int):
        detail += f" ({days_until} days)"
        if days_until <= 0:
            state = _ERROR
        elif days_until <= 7:
            state = _WARN
    return StatusComponent("fic", state, detail, payload)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def collect_status(
    agent_slug: str | None,
    *,
    hermes_home: Path | None = None,
    query_source: QuerySource | None = None,
) -> StatusReport:
    """Build a full status report for a single agent (or skill-wide if slug=None)."""
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    if query_source is None:
        query_source = get_query_source()

    components: list[StatusComponent] = []

    local = gather_local_config(hermes_home, agent_slug)
    components.append(local)

    if local.state == _MISSING:
        return StatusReport(agent_slug=None, components=components)

    tenant_id = local.data.get("tenant_id", "")
    app_id_t2 = local.data.get("app_id", "")
    aa_instance_id = local.data.get("aa_instance_id")

    # Tenant-wide
    components.append(gather_license(query_source))
    components.append(gather_t1_app(query_source, app_id=(tenant_id and app_id_t2) or None))
    components.append(gather_t2_app(query_source, app_id=app_id_t2))

    # Per-agent
    if agent_slug:
        components.append(gather_blueprint(query_source, slug=agent_slug))
        components.append(gather_instance(query_source, instance_id=aa_instance_id))
        components.append(gather_channels(query_source, instance_id=aa_instance_id))
        components.append(gather_telemetry(query_source, instance_id=aa_instance_id))
        components.append(gather_activity_bridge(hermes_home, agent_slug))

    components.append(gather_fic(query_source, app_id=app_id_t2))

    return StatusReport(agent_slug=agent_slug, components=components)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_human(report: StatusReport) -> str:
    """Render the status report as a markdown-style aligned table."""
    if not report.components:
        return "(no components gathered)\n"

    name_w = max(len(c.name) for c in report.components)
    state_w = max(len(c.state) for c in report.components)
    name_w = max(name_w, len("Component"))
    state_w = max(state_w, len("State"))

    lines = []
    if report.agent_slug:
        lines.append(f"hermes a365 status — {report.agent_slug}")
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
        lines.append(f"note: {skipped} component(s) skipped (a365 CLI unavailable)")
    return "\n".join(lines) + "\n"


def render_json(report: StatusReport) -> str:
    payload = {
        "agent_slug": report.agent_slug,
        "overall": report.overall,
        "components": [asdict(c) for c in report.components],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 status — per-component status report.",
    )
    parser.add_argument(
        "agent_slug",
        nargs="?",
        help="agent to report on; omit for skill-wide status",
    )
    parser.add_argument("--human", action="store_true", help="markdown-aligned output")
    args = parser.parse_args(argv)

    report = collect_status(args.agent_slug)
    sys.stdout.write(render_human(report) if args.human else render_json(report))
    return overall_to_exit_code(report.overall)


if __name__ == "__main__":
    raise SystemExit(main())
