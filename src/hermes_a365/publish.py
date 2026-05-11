"""hermes a365 publish — wrap ``a365 publish``.

The GA CLI's ``publish`` does **two different things** depending on the
agent flavour:

- **AI Teammate** (``--aiteammate``): updates manifest IDs and emits a
  zip the operator uploads to the M365 Admin Centre. Channel
  deployment is operator-side after that.
- **Blueprint-only** (default): calls the Agent Instance Graph API
  (``POST /beta/agentRegistry/agentInstances``) to register the agent
  instance. No zip; nothing to upload. The
  resulting ``agentInstanceId`` lands in ``a365.generated.config.json``.

The 2026-05-05 walkthrough caught the wrapper rendering the Admin
Centre upload language for both flows, which misled operators in
blueprint-only mode (slice 18t / bug #14 fixes that — the plan and
post-apply messages now branch on ``aiteammate``).

Default mode is dry-run; ``--apply`` runs ``a365 publish`` for real.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass, field

from .mutator import AADSTSError, CliInvocationError, Mutator, RunResult, get_mutator

ADMIN_CENTRE_URL = "https://admin.microsoft.com/"

# Defensive parser: when the CLI emits a "Created package:" / "Wrote zip:" /
# similar line, grab the path. The exact wording isn't pinned in v1.1.171 yet,
# so we accept several phrasings.
_PACKAGE_PATH_RE = re.compile(
    r"(?:created package|wrote zip|package(?: created)?)[\s:]+(\S+\.zip)",
    re.IGNORECASE,
)

# Slice 18t (bug #14): blueprint-only flow registers an instance via
# Graph and prints "Agent instance registered: <guid>" — extract the
# id so the post-apply message is concrete.
_INSTANCE_ID_RE = re.compile(
    r"Agent instance registered:\s*([0-9a-fA-F-]{8,})",
)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class PublishInputs:
    agent_name: str
    tenant_id: str | None = None
    aiteammate: bool = False  # blueprint-only by default per CLI default
    use_blueprint: bool = False  # blueprint-based non-DW flow (only with aiteammate=False)
    verbose: bool = False

    def __post_init__(self) -> None:
        if not self.agent_name:
            raise ValueError("agent_name must be non-empty")
        if self.use_blueprint and self.aiteammate:
            raise ValueError("--use-blueprint is only meaningful with --aiteammate false")


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class PublishStep:
    argv: list[str]
    description: str


@dataclass
class PublishPlan:
    inputs: PublishInputs
    step: PublishStep

    def render_human(self) -> str:
        lines = [f"[plan] hermes a365 publish {self.inputs.agent_name}"]
        if self.inputs.tenant_id:
            lines.append(f"  tenant: {self.inputs.tenant_id}")
        else:
            lines.append("  tenant: (auto-detect from `az account show`)")
        flavour = "AI Teammate" if self.inputs.aiteammate else "blueprint-only"
        lines.append(f"  flavour: {flavour}")
        # Slice 18t (bug #14): be explicit about what this run will produce
        # so operators know whether to wait for an admin-centre upload step.
        if self.inputs.aiteammate:
            lines.append("  output:  manifest zip for M365 Admin Centre upload")
        else:
            lines.append("  output:  Graph API instance registration (no zip)")
        if self.inputs.use_blueprint:
            lines.append("  flow:    blueprint-based non-DW (explicit)")
        lines.append(f"  step:    {self.step.description}")
        # shlex.join (slice 18p, bug #7) keeps multi-word values quoted
        # so the printed line is shell-pasteable verbatim.
        lines.append(f"           $ {shlex.join(self.step.argv)}")
        return "\n".join(lines)


def _build_argv(inputs: PublishInputs) -> list[str]:
    argv = ["a365", "publish", "--agent-name", inputs.agent_name]
    if inputs.tenant_id:
        argv.extend(["--tenant-id", inputs.tenant_id])
    if inputs.aiteammate:
        argv.append("--aiteammate")
    if inputs.use_blueprint:
        argv.append("--use-blueprint")
    if inputs.verbose:
        argv.append("--verbose")
    return argv


def _step_description(inputs: PublishInputs) -> str:
    if inputs.aiteammate:
        return "package the agent manifest for M365 Admin Centre upload"
    return "register agent instance via Microsoft Graph (no zip emitted)"


def build_publish_plan(inputs: PublishInputs) -> PublishPlan:
    return PublishPlan(
        inputs=inputs,
        step=PublishStep(
            argv=_build_argv(inputs),
            description=_step_description(inputs),
        ),
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    plan: PublishPlan
    raw: RunResult
    package_path: str | None  # set on AI Teammate flow if a zip was produced
    instance_id: str | None  # set on blueprint-only flow if registration succeeded
    messages: list[str] = field(default_factory=list)


def _extract_package_path(output: str) -> str | None:
    """Best-effort grep for a `*.zip` path in the CLI's stdout/stderr."""
    match = _PACKAGE_PATH_RE.search(output)
    return match.group(1) if match else None


def _extract_instance_id(output: str) -> str | None:
    """Grep for the registered instance id in blueprint-only flow output."""
    match = _INSTANCE_ID_RE.search(output)
    return match.group(1) if match else None


def apply_publish_plan(
    plan: PublishPlan,
    *,
    mutator: Mutator,
) -> PublishResult:
    """Run ``a365 publish``; surface the produced artefact (zip or
    registered instance) appropriate to the flow."""
    run = mutator.run(plan.step.argv, timeout=180.0)
    package_path: str | None = None
    instance_id: str | None = None
    messages: list[str] = [f"[apply] {plan.step.description} — done"]

    if plan.inputs.aiteammate:
        package_path = _extract_package_path(run.combined)
        if package_path:
            messages.append(f"[apply] package: {package_path}")
        messages.append(
            f"[apply] next: upload the package to the M365 Admin Centre at {ADMIN_CENTRE_URL}"
        )
    else:
        instance_id = _extract_instance_id(run.combined)
        if instance_id:
            messages.append(f"[apply] agent instance registered: {instance_id}")
        messages.append(
            "[apply] no upload needed — the instance is registered server-side via Graph"
        )

    return PublishResult(
        plan=plan,
        raw=run,
        package_path=package_path,
        instance_id=instance_id,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description="hermes a365 publish — package the agent manifest for admin-centre upload.",
        )
    parser.add_argument("--agent-name", required=True, help="agent base name")
    parser.add_argument(
        "--tenant-id",
        help="tenant id; default auto-detects via `az account show`",
    )
    parser.add_argument(
        "--aiteammate",
        action="store_true",
        help="treat as AI Teammate (creates Entra user); default is blueprint-only",
    )
    parser.add_argument(
        "--use-blueprint",
        action="store_true",
        help="use blueprint-based non-DW flow (only with --aiteammate false)",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--apply", action="store_true", help="execute the plan; default is dry-run")
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        inputs = PublishInputs(
            agent_name=args.agent_name,
            tenant_id=args.tenant_id,
            aiteammate=args.aiteammate,
            use_blueprint=args.use_blueprint,
            verbose=args.verbose,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    plan = build_publish_plan(inputs)
    sys.stdout.write(plan.render_human() + "\n")

    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to package.\n")
        return 0

    try:
        result = apply_publish_plan(plan, mutator=get_mutator())
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except CliInvocationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
