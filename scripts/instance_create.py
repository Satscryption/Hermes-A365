"""hermes a365 instance create — write the per-agent runtime .env file.

Pure local config-file writer. No cloud step. The server-side agent
identity is created by ``a365 setup blueprint`` (driven by
``hermes a365 register``); this command only produces the ``.env``
file that runtime consumers (the activity bridge, telemetry pipeline,
etc.) read for slug / owner / OTLP endpoint / business-hours metadata.

Inherits required values (``A365_APP_ID``, ``A365_TENANT_ID``,
``HERMES_OTLP_ENDPOINT``) from ``~/.hermes/.env``. An existing
``AA_INSTANCE_ID`` in the agent .env is preserved across re-runs;
business-hours fields from a prior run are also preserved unless
explicitly overridden on the command line.

Secrets policy unchanged from v0.1: this file never contains the T2
client secret. Runtime consumers fetch it from the OS keychain.

Default mode is dry-run; ``--apply`` performs the atomic write.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from _common import parse_env
from render_instance_env import InstanceEnvInputs, render_instance_env

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

_REQUIRED_PARENT_KEYS = ("A365_APP_ID", "A365_TENANT_ID")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InstanceCreateError(RuntimeError):
    """Raised when instance create can't proceed (missing parent .env, bad inputs)."""


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


def write_text_atomic(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` via tmp + rename. Creates parents.

    ``mode`` defaults to 0o600 (slice 18x — owner-only) so we don't leak
    operator metadata (email, AAD object id, instance id, OTLP endpoint)
    via world-readable umask defaults. The agent .env doesn't contain
    secrets by design, but tightening it here also protects any future
    secret-bearing file the activity bridge writes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class InstanceCreateInputs:
    """User-supplied arguments for the local-runtime .env writer."""

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
# Plan
# ---------------------------------------------------------------------------


@dataclass
class InstancePlan:
    """The local file write the apply phase will perform.

    ``aa_instance_id`` is ``None`` when no prior ``AA_INSTANCE_ID`` exists
    in the agent .env — in that case the apply phase generates a fresh
    UUID. Plan-time UUID generation was removed in slice 18n (bug #10
    from the live walkthrough) because the dry-run UUID was discarded
    by ``--apply``, which then minted its own; operators saw two
    different IDs for the same operation.
    """

    slug: str
    aa_instance_id: str | None
    desired_env_inputs: InstanceEnvInputs
    env_path: Path
    will_create: bool  # True if the agent .env doesn't yet exist

    @property
    def aa_instance_id_was_existing(self) -> bool:
        return self.aa_instance_id is not None

    def render_human(self) -> str:
        if self.aa_instance_id is None:
            id_line = "AA_INSTANCE_ID: (generated at apply)"
        else:
            id_line = f"AA_INSTANCE_ID: {self.aa_instance_id}  (preserved from existing .env)"
        action = "create" if self.will_create else "update"
        return "\n".join(
            [
                f"[plan] hermes a365 instance create {self.slug}",
                f"  {id_line}",
                f"  agent .env:    {self.env_path}  ({action})",
                "  cloud step:    none — server-side identity is managed by `setup blueprint`",
            ]
        )


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


def build_instance_plan(
    inputs: InstanceCreateInputs,
    *,
    hermes_home: Path | None = None,
) -> InstancePlan:
    """Resolve identifiers, gather inherited values, return the file-write plan.

    The ``AA_INSTANCE_ID`` is preserved from any existing agent .env;
    otherwise the plan defers UUID generation to apply (so dry-run and
    apply don't disagree). See ``InstancePlan.aa_instance_id``.
    """
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()

    parent_env = _load_skill_env(hermes_home)
    existing_agent = _load_existing_agent_env(hermes_home, inputs.slug)
    env_path = _agent_env_path(hermes_home, inputs.slug)

    existing_id = existing_agent.get("AA_INSTANCE_ID", "").strip() or None

    desired_inputs = InstanceEnvInputs(
        agent_identity=inputs.slug,
        owner=inputs.owner,
        owner_aad_id=inputs.owner_aad_id,
        a365_app_id=parent_env["A365_APP_ID"],
        a365_tenant_id=parent_env["A365_TENANT_ID"],
        hermes_otlp_endpoint=_resolve_otlp_endpoint(inputs, parent_env),
        # When existing_id is None, InstanceEnvInputs.__post_init__ mints a
        # fresh UUID — but we won't materialise that until apply time.
        aa_instance_id=existing_id,
        # Preserve prior business-hours values unless the caller overrode them.
        business_hours_tz=(inputs.business_hours_tz or existing_agent.get("BUSINESS_HOURS_TZ")),
        business_hours_start=(
            inputs.business_hours_start or existing_agent.get("BUSINESS_HOURS_START")
        ),
        business_hours_end=(inputs.business_hours_end or existing_agent.get("BUSINESS_HOURS_END")),
    )

    return InstancePlan(
        slug=inputs.slug,
        aa_instance_id=existing_id,
        desired_env_inputs=desired_inputs,
        env_path=env_path,
        will_create=not env_path.exists(),
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class InstanceCreateResult:
    slug: str
    aa_instance_id: str
    env_path: Path
    env_written: bool
    messages: list[str] = field(default_factory=list)


def apply_instance_plan(plan: InstancePlan) -> InstanceCreateResult:
    """Render the desired .env and atomically write it to the agent dir.

    Pulls the realised ``AA_INSTANCE_ID`` off ``desired_env_inputs`` —
    that's where the freshly-minted UUID lives when ``plan.aa_instance_id``
    is ``None`` (no prior id existed; UUID was generated by
    ``InstanceEnvInputs.__post_init__``).
    """
    rendered = render_instance_env(plan.desired_env_inputs)
    write_text_atomic(plan.env_path, rendered)
    realised_id = plan.desired_env_inputs.aa_instance_id
    assert realised_id is not None  # __post_init__ guarantees this
    return InstanceCreateResult(
        slug=plan.slug,
        aa_instance_id=realised_id,
        env_path=plan.env_path,
        env_written=True,
        messages=[f"[apply] wrote {plan.env_path}"],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description="hermes a365 instance create — write the per-agent runtime .env file.",
        )
    parser.add_argument("slug", help="agent slug (also the agent-name passed to `a365 setup`)")
    parser.add_argument("--owner", required=True, help="owner email")
    parser.add_argument("--owner-aad-id", required=True, help="owner Entra (AAD) object id")
    parser.add_argument(
        "--otlp-endpoint",
        help="override HERMES_OTLP_ENDPOINT; defaults to value from ~/.hermes/.env",
    )
    parser.add_argument("--business-hours-tz")
    parser.add_argument("--business-hours-start")
    parser.add_argument("--business-hours-end")
    parser.add_argument(
        "--apply", action="store_true", help="execute the file write; default is dry-run"
    )
    return parser


def run(args: argparse.Namespace) -> int:
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
        sys.stdout.write("\nNo mutations. Re-run with --apply to write the agent .env.\n")
        return 0

    result = apply_instance_plan(plan)
    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
