"""hermes a365 deploy — bind/unbind agent instances to M365 channels.

Spec: SPEC.md §6.9. Wraps ``a365 deploy --instance=<id> --channels=<list>``
with idempotent set-diff reconciliation:

- Reads ``AA_INSTANCE_ID`` from ``~/.hermes/agents/<slug>/.env``.
- Queries the current channel set via ``QuerySource.query_instance``.
- Compares against the desired set; same set → noop, otherwise compute
  ``+`` / ``-`` and (when ``--apply``) hand the desired set to the
  mutator. A365 reconciles channels server-side from the absolute set.

Channels currently supported: ``teams``, ``outlook``, ``m365copilot``.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from _common import parse_env
from register import AADSTSError, Mutator, get_mutator
from status import QuerySource, get_query_source

SUPPORTED_CHANNELS: frozenset[str] = frozenset({"teams", "outlook", "m365copilot"})

PlanAction = Literal["create", "noop", "patch"]

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DeployError(RuntimeError):
    """Raised when deploy can't proceed (bad config, missing instance, …)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _agent_env_path(hermes_home: Path, slug: str) -> Path:
    return hermes_home / "agents" / slug / ".env"


def _load_aa_instance_id(hermes_home: Path, slug: str) -> str:
    """Return ``AA_INSTANCE_ID`` from the agent .env or raise ``DeployError``."""
    env_path = _agent_env_path(hermes_home, slug)
    if not env_path.exists():
        raise DeployError(
            f"{env_path} does not exist; run `hermes a365 instance create {slug}` first"
        )
    env = parse_env(env_path.read_text())
    instance_id = env.get("AA_INSTANCE_ID", "").strip()
    if not instance_id:
        raise DeployError(
            f"{env_path} has no AA_INSTANCE_ID; re-run `hermes a365 instance create {slug}`"
        )
    return instance_id


def _bound_channels(payload: dict[str, Any] | None) -> set[str]:
    """Channels currently bound on the instance — i.e. with state ``ok``."""
    if not payload:
        return set()
    channels = payload.get("channels") or {}
    if not isinstance(channels, dict):
        return set()
    return {name for name, state in channels.items() if state == "ok"}


def normalize_channels(values: list[str] | None) -> list[str]:
    """Deduplicate, sort, validate the channel list.

    Empty or ``None`` is allowed — it represents 'unbind everything'.
    Unknown channel names raise :class:`ValueError`.
    """
    raw = values or []
    seen: set[str] = set()
    for name in raw:
        if name not in SUPPORTED_CHANNELS:
            raise ValueError(f"unsupported channel {name!r}; allowed: {sorted(SUPPORTED_CHANNELS)}")
        seen.add(name)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class DeployPlan:
    slug: str
    aa_instance_id: str
    desired: list[str]
    current: list[str]
    additions: list[str]
    removals: list[str]
    action: PlanAction

    def render_human(self) -> str:
        lines = [f"[plan] deploy {self.slug}"]
        lines.append(f"  current channels:  {', '.join(self.current) or '(none)'}")
        lines.append(f"  desired channels:  {', '.join(self.desired) or '(none)'}")
        if self.action == "noop":
            lines.append("  delta:             (none — already converged)")
            return "\n".join(lines)
        delta_parts = [f"+{c}" for c in self.additions] + [f"-{c}" for c in self.removals]
        lines.append(f"  delta:             {', '.join(delta_parts)}")
        return "\n".join(lines)


def build_deploy_plan(
    slug: str,
    desired: list[str],
    *,
    hermes_home: Path | None = None,
    query_source: QuerySource | None = None,
) -> DeployPlan:
    """Resolve identifiers, query current channels, compute the plan."""
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    qs = query_source or get_query_source()

    desired_norm = normalize_channels(desired)
    aa_instance_id = _load_aa_instance_id(hermes_home, slug)

    current_set: set[str] = set()
    if qs.available:
        payload = qs.query_instance(instance_id=aa_instance_id)
        current_set = _bound_channels(payload)
    current = sorted(current_set)

    desired_set = set(desired_norm)
    additions = sorted(desired_set - current_set)
    removals = sorted(current_set - desired_set)

    if not additions and not removals:
        action: PlanAction = "noop"
    elif not current_set:
        action = "create"
    else:
        action = "patch"

    return DeployPlan(
        slug=slug,
        aa_instance_id=aa_instance_id,
        desired=desired_norm,
        current=current,
        additions=additions,
        removals=removals,
        action=action,
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


@dataclass
class DeployResult:
    slug: str
    aa_instance_id: str
    plan: DeployPlan
    mutator_called: bool
    channel_results: dict[str, str] = field(default_factory=dict)
    deep_links: dict[str, str] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


def apply_deploy_plan(
    plan: DeployPlan,
    *,
    mutator: Mutator,
) -> DeployResult:
    """Execute the deploy plan; idempotent — noop bypasses the mutator."""
    if plan.action == "noop":
        return DeployResult(
            slug=plan.slug,
            aa_instance_id=plan.aa_instance_id,
            plan=plan,
            mutator_called=False,
            messages=[
                f"[apply] deploy {plan.slug}: channel set already matches — no change",
            ],
        )

    response = mutator.deploy(instance_id=plan.aa_instance_id, channels=plan.desired)

    channels_payload = response.get("channels") or {}
    deep_links_payload = response.get("deep_links") or response.get("deepLinks") or {}
    if not isinstance(channels_payload, dict):
        channels_payload = {}
    if not isinstance(deep_links_payload, dict):
        deep_links_payload = {}

    messages: list[str] = [
        f"[apply] a365 deploy --instance={plan.aa_instance_id} "
        f"--channels={','.join(plan.desired) or '(empty)'}",
    ]
    for ch in plan.desired:
        state = str(channels_payload.get(ch) or "ok")
        link = deep_links_payload.get(ch)
        if link:
            messages.append(f"[apply] {ch}: {state} ({link})")
        else:
            messages.append(f"[apply] {ch}: {state}")
    for ch in plan.removals:
        messages.append(f"[apply] {ch}: unbound")

    return DeployResult(
        slug=plan.slug,
        aa_instance_id=plan.aa_instance_id,
        plan=plan,
        mutator_called=True,
        channel_results={k: str(v) for k, v in channels_payload.items()},
        deep_links={k: str(v) for k, v in deep_links_payload.items()},
        messages=messages,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 deploy — bind/unbind agent channels (Teams/Outlook/Copilot).",
    )
    parser.add_argument("slug", help="agent slug")
    parser.add_argument(
        "--channels",
        default="",
        help=f"desired channel set, comma-separated. Allowed: {sorted(SUPPORTED_CHANNELS)}. "
        "Empty = unbind everything.",
    )
    parser.add_argument("--apply", action="store_true", help="execute the plan; default is dry-run")
    args = parser.parse_args(argv)

    try:
        desired = normalize_channels(_split_csv(args.channels))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        plan = build_deploy_plan(args.slug, desired)
    except DeployError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")

    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to deploy.\n")
        return 0

    try:
        result = apply_deploy_plan(plan, mutator=get_mutator())
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
