"""hermes a365 instance create — register an A365 instance and write per-agent .env.

Spec: SPEC.md §6.5. Per-agent runtime config:

- Inputs: ``agent-slug``, ``owner``, ``owner-aad-id``, optional business hours.
- Inherits ``A365_APP_ID``, ``A365_TENANT_ID``, ``A365_CLI_VARIANT``, and
  ``HERMES_OTLP_ENDPOINT`` from ``~/.hermes/.env`` (written by ``register``).
- Idempotency: if ``~/.hermes/agents/<slug>/.env`` already records an
  ``AA_INSTANCE_ID``, that UUID is preserved; otherwise a fresh one is
  generated. Business-hours fields from a prior run are preserved unless
  explicitly overridden on the command line.
- Cloud step: ``a365 create-instance --blueprint=<slug> --instance=<UUID>``.
  Skipped if the cloud already reports the instance (re-run noop).
- Secrets policy: ``A365_APP_PASSWORD`` is *never* written to the agent .env
  — runtime consumers (e.g. activity bridge) pull it from the OS keychain.

Default mode is dry-run; ``--apply`` executes both the local .env write and
the cloud registration call.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from _common import parse_env
from register import AADSTSError, Mutator, get_mutator
from render_instance_env import CLI_VARIANTS, InstanceEnvInputs, render_instance_env
from status import QuerySource, get_query_source

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

_REQUIRED_PARENT_KEYS = ("A365_APP_ID", "A365_TENANT_ID")

PlanAction = Literal["create", "noop", "create-cloud-only"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InstanceCreateError(RuntimeError):
    """Raised when instance create's apply path can't proceed."""


# ---------------------------------------------------------------------------
# Path + env helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _agent_env_path(hermes_home: Path, slug: str) -> Path:
    return hermes_home / "agents" / slug / ".env"


def _load_skill_env(hermes_home: Path) -> dict[str, str]:
    """Return parsed ``~/.hermes/.env``. Raises if missing or incomplete."""
    env_file = hermes_home / ".env"
    if not env_file.exists():
        raise InstanceCreateError(f"{env_file} does not exist; run `hermes a365 register` first")
    env = parse_env(env_file.read_text())
    missing = [k for k in _REQUIRED_PARENT_KEYS if not env.get(k)]
    if missing:
        raise InstanceCreateError(
            f"{env_file} missing required keys: {missing}; re-run `hermes a365 register`"
        )
    return env


def _load_existing_agent_env(hermes_home: Path, slug: str) -> dict[str, str]:
    """Return parsed agent .env, or {} if it doesn't exist yet."""
    path = _agent_env_path(hermes_home, slug)
    if not path.exists():
        return {}
    return parse_env(path.read_text())


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via tmp + rename. Creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class InstanceCreateInputs:
    """User-supplied arguments for instance create.

    The slug doubles as the blueprint identifier (per SPEC §6.5 / §6.4).
    """

    slug: str
    owner: str
    owner_aad_id: str
    otlp_endpoint: str | None = None  # falls back to parent .env
    business_hours_tz: str | None = None
    business_hours_start: str | None = None
    business_hours_end: str | None = None

    def __post_init__(self) -> None:
        if not self.slug:
            raise ValueError("slug must be non-empty")
        if not self.owner:
            raise ValueError("owner must be non-empty")
        if not self.owner_aad_id:
            raise ValueError("owner_aad_id must be non-empty")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class InstancePlan:
    """Composite plan: local .env write + cloud registration."""

    slug: str
    aa_instance_id: str
    aa_instance_id_was_existing: bool
    action: PlanAction
    desired_env_inputs: InstanceEnvInputs
    cloud_actual: dict[str, Any] | None = None

    def render_human(self) -> str:
        lines = [
            f"[plan] hermes a365 instance create {self.slug}",
            f"  AA_INSTANCE_ID: {self.aa_instance_id}"
            f"  ({'existing' if self.aa_instance_id_was_existing else 'new'})",
            f"  agent .env:    {'will be written / merged'}",
            f"  cloud step:    {self._cloud_phrase()}",
        ]
        return "\n".join(lines)

    def _cloud_phrase(self) -> str:
        if self.action == "noop":
            return "instance already registered — no change"
        if self.action == "create":
            return f"would call a365 create-instance --blueprint={self.slug} --instance=<UUID>"
        return "would only register cloud (local .env already complete)"


def _resolve_otlp_endpoint(
    inputs: InstanceCreateInputs,
    parent_env: dict[str, str],
) -> str:
    if inputs.otlp_endpoint:
        return inputs.otlp_endpoint
    inherited = parent_env.get("HERMES_OTLP_ENDPOINT", "").strip()
    if inherited:
        return inherited
    raise InstanceCreateError(
        "HERMES_OTLP_ENDPOINT is not set in ~/.hermes/.env and --otlp-endpoint was not given. "
        "Either set the parent env or pass --otlp-endpoint."
    )


def _resolve_cli_variant(parent_env: dict[str, str]) -> str:
    variant = parent_env.get("A365_CLI_VARIANT", "").strip()
    if variant in CLI_VARIANTS:
        return variant
    # Sensible default — SPEC §6.2 lists both variants; pick .NET for parity
    # with the GA Microsoft package feed. The doctor flags drift.
    return "a365-dotnet"


def build_instance_plan(
    inputs: InstanceCreateInputs,
    *,
    hermes_home: Path | None = None,
    query_source: QuerySource | None = None,
) -> InstancePlan:
    """Resolve identifiers, query cloud actual, return the composite plan."""
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    qs = query_source or get_query_source()

    parent_env = _load_skill_env(hermes_home)
    existing_agent = _load_existing_agent_env(hermes_home, inputs.slug)

    existing_id = existing_agent.get("AA_INSTANCE_ID", "").strip()
    aa_instance_id = existing_id or str(uuid.uuid4())

    desired_inputs = InstanceEnvInputs(
        agent_identity=inputs.slug,
        owner=inputs.owner,
        owner_aad_id=inputs.owner_aad_id,
        a365_app_id=parent_env["A365_APP_ID"],
        a365_tenant_id=parent_env["A365_TENANT_ID"],
        a365_cli_variant=_resolve_cli_variant(parent_env),
        hermes_otlp_endpoint=_resolve_otlp_endpoint(inputs, parent_env),
        aa_instance_id=aa_instance_id,
        # Preserve prior business-hours values unless the caller overrode them.
        business_hours_tz=inputs.business_hours_tz or existing_agent.get("BUSINESS_HOURS_TZ"),
        business_hours_start=(
            inputs.business_hours_start or existing_agent.get("BUSINESS_HOURS_START")
        ),
        business_hours_end=inputs.business_hours_end or existing_agent.get("BUSINESS_HOURS_END"),
    )

    cloud_actual: dict[str, Any] | None = None
    if qs.available:
        cloud_actual = qs.query_instance(instance_id=aa_instance_id)

    if cloud_actual is not None:
        action: PlanAction = "noop"
    elif existing_id:
        action = "create-cloud-only"
    else:
        action = "create"

    return InstancePlan(
        slug=inputs.slug,
        aa_instance_id=aa_instance_id,
        aa_instance_id_was_existing=bool(existing_id),
        action=action,
        desired_env_inputs=desired_inputs,
        cloud_actual=cloud_actual,
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


@dataclass
class InstanceCreateResult:
    slug: str
    aa_instance_id: str
    env_path: Path
    env_written: bool
    cloud_registered: bool
    messages: list[str] = field(default_factory=list)


def apply_instance_plan(
    plan: InstancePlan,
    *,
    mutator: Mutator,
    hermes_home: Path,
) -> InstanceCreateResult:
    """Write the agent .env atomically and (when needed) register the cloud instance."""
    env_path = _agent_env_path(hermes_home, plan.slug)
    rendered = render_instance_env(plan.desired_env_inputs)
    write_text_atomic(env_path, rendered)

    messages: list[str] = [f"[apply] wrote {env_path}"]
    cloud_registered = False

    if plan.action in ("create", "create-cloud-only"):
        mutator.create_instance(blueprint_slug=plan.slug, instance_id=plan.aa_instance_id)
        cloud_registered = True
        messages.append(
            f"[apply] a365 create-instance --blueprint={plan.slug} --instance={plan.aa_instance_id}"
        )
    else:
        messages.append("[apply] cloud instance already registered — no change")

    return InstanceCreateResult(
        slug=plan.slug,
        aa_instance_id=plan.aa_instance_id,
        env_path=env_path,
        env_written=True,
        cloud_registered=cloud_registered,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "hermes a365 instance create — write per-agent .env and register an A365 instance."
        ),
    )
    parser.add_argument("slug", help="agent slug (also the blueprint id)")
    parser.add_argument("--owner", required=True, help="owner email")
    parser.add_argument("--owner-aad-id", required=True, help="owner Entra (AAD) object id")
    parser.add_argument(
        "--otlp-endpoint",
        help="override HERMES_OTLP_ENDPOINT; defaults to value from ~/.hermes/.env",
    )
    parser.add_argument("--business-hours-tz")
    parser.add_argument("--business-hours-start")
    parser.add_argument("--business-hours-end")
    parser.add_argument("--apply", action="store_true", help="execute the plan; default is dry-run")
    args = parser.parse_args(argv)

    try:
        inputs = InstanceCreateInputs(
            slug=args.slug,
            owner=args.owner,
            owner_aad_id=args.owner_aad_id,
            otlp_endpoint=args.otlp_endpoint,
            business_hours_tz=args.business_hours_tz,
            business_hours_start=args.business_hours_start,
            business_hours_end=args.business_hours_end,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        plan = build_instance_plan(inputs)
    except InstanceCreateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")

    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to register.\n")
        return 0

    try:
        result = apply_instance_plan(
            plan,
            mutator=get_mutator(),
            hermes_home=_resolve_hermes_home(),
        )
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except InstanceCreateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
