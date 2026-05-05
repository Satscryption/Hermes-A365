"""hermes a365 publish — wrap ``a365 publish`` to package the agent manifest.

The real CLI's ``publish`` command updates manifest IDs and produces a
zip file that the operator uploads to the Microsoft 365 Admin Centre.
Channel deployment in v0.2 is **operator-side**: the CLI doesn't push to
Teams / Outlook / Copilot — the admin signs in to the centre, uploads
the zip, and approves the agent for users in the desired DLP scope.

This wrapper composes the right argv from operator inputs and surfaces
the resulting package path + admin-centre URL hint. Default mode is
dry-run; ``--apply`` runs ``a365 publish`` for real.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass, field

from mutator import AADSTSError, CliInvocationError, Mutator, RunResult, get_mutator

ADMIN_CENTRE_URL = "https://admin.microsoft.com/"

# Defensive parser: when the CLI emits a "Created package:" / "Wrote zip:" /
# similar line, grab the path. The exact wording isn't pinned in v1.1.171 yet,
# so we accept several phrasings.
_PACKAGE_PATH_RE = re.compile(
    r"(?:created package|wrote zip|package(?: created)?)[\s:]+(\S+\.zip)",
    re.IGNORECASE,
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
        if self.inputs.use_blueprint:
            lines.append("  flow:    blueprint-based non-DW (Agent Instance Graph API)")
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


def build_publish_plan(inputs: PublishInputs) -> PublishPlan:
    return PublishPlan(
        inputs=inputs,
        step=PublishStep(
            argv=_build_argv(inputs),
            description="package the agent manifest for M365 Admin Centre upload",
        ),
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    plan: PublishPlan
    raw: RunResult
    package_path: str | None  # parsed from CLI stdout if visible
    messages: list[str] = field(default_factory=list)


def _extract_package_path(output: str) -> str | None:
    """Best-effort grep for a `*.zip` path in the CLI's stdout/stderr."""
    match = _PACKAGE_PATH_RE.search(output)
    return match.group(1) if match else None


def apply_publish_plan(
    plan: PublishPlan,
    *,
    mutator: Mutator,
) -> PublishResult:
    """Run ``a365 publish`` and surface the produced package path."""
    run = mutator.run(plan.step.argv, timeout=180.0)
    package_path = _extract_package_path(run.combined)

    messages: list[str] = [f"[apply] {plan.step.description} — done"]
    if package_path:
        messages.append(f"[apply] package: {package_path}")
    messages.append(
        f"[apply] next: upload the package to the M365 Admin Centre at {ADMIN_CENTRE_URL}"
    )

    return PublishResult(plan=plan, raw=run, package_path=package_path, messages=messages)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main())
