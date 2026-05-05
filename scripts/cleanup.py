"""hermes a365 cleanup — destructive teardown of an A365 agent.

v0.2 design: drives the real ``a365 cleanup`` subcommands. The CLI ships
three granular kinds plus a bare ``cleanup`` that does everything:

- ``a365 cleanup blueprint``  — Entra blueprint app + service principal
- ``a365 cleanup instance``   — agent instance identity + user
- ``a365 cleanup azure``      — Azure App Service + App Service Plan

The v0.1 ``deployment`` and ``app`` kinds are gone (no per-tier app
split, no separate deploy command in the GA CLI). Channel deployment in
v0.2 is operator-side via the M365 Admin Centre — there's nothing
cloud-side for us to "undeploy" except the Azure infrastructure.

Order of operations (safe → unsafe): ``azure`` → ``instance`` →
``blueprint``. We tear the App Service down first so the agent's
runtime stops, then revoke the agent's Entra identity, then remove the
blueprint. After all cloud steps succeed, local artefacts under
``~/.hermes/agents/<slug>/`` are removed.

Safety: ``--confirm`` is required and must equal the agent name. The
plan is always printed (even without ``--apply``) so the operator can
audit before mutating. Each CLI invocation is run with ``--yes`` to
skip the CLI's own confirmation prompt — our gate is ``--confirm``.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from _common import slugify
from mutator import AADSTSError, CliInvocationError, Mutator, get_mutator

CleanupKind = Literal["azure", "instance", "blueprint"]
CLEANUP_KINDS: tuple[CleanupKind, ...] = ("azure", "instance", "blueprint")

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CleanupError(RuntimeError):
    """Raised when cleanup can't proceed (missing confirm, bad kind)."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _agent_dir(hermes_home: Path, slug: str) -> Path:
    return hermes_home / "agents" / slug


def _local_artefacts(hermes_home: Path, slug: str) -> list[Path]:
    """Return existing local files we'd remove after cloud cleanup succeeds."""
    candidates = [
        _agent_dir(hermes_home, slug) / ".env",
        # Legacy v0.1 caches that may linger on a fresh checkout.
        _agent_dir(hermes_home, slug) / "blueprint.json",
        _agent_dir(hermes_home, slug) / "bridge.pid",
        _agent_dir(hermes_home, slug) / "bridge.log",
    ]
    return [p for p in candidates if p.exists()]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class CleanupInputs:
    agent_name: str
    tenant_id: str | None = None
    kinds: tuple[CleanupKind, ...] = CLEANUP_KINDS  # default: all three
    slug: str | None = None  # local-dir slug; defaults to slugify(agent_name)

    def __post_init__(self) -> None:
        if not self.agent_name:
            raise ValueError("agent_name must be non-empty")
        for k in self.kinds:
            if k not in CLEANUP_KINDS:
                raise ValueError(f"unknown cleanup kind: {k!r}; allowed: {list(CLEANUP_KINDS)}")

    @property
    def resolved_slug(self) -> str:
        """Slug used for ``~/.hermes/agents/<slug>/`` lookup.

        Operator override (``--slug``) wins; otherwise we slugify the
        ``agent_name``. This is the fix for the 2026-05-05 walkthrough's
        bug #12 — the wrapper used to look at
        ``~/.hermes/agents/Hermes Inbox Helper/`` literally.
        """
        if self.slug:
            return self.slug
        return slugify(self.agent_name)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class CleanupStep:
    kind: CleanupKind
    argv: list[str]
    description: str


@dataclass
class CleanupPlan:
    inputs: CleanupInputs
    steps: list[CleanupStep]
    local_paths: list[Path] = field(default_factory=list)

    def render_human(self) -> str:
        lines = [f"[plan] hermes a365 cleanup {self.inputs.agent_name}"]
        if self.inputs.tenant_id:
            lines.append(f"  tenant: {self.inputs.tenant_id}")
        else:
            lines.append("  tenant: (auto-detect from `az account show`)")
        lines.append(f"  local slug: {self.inputs.resolved_slug}")
        lines.append("  cloud steps (in order):")
        if not self.steps:
            lines.append("    (none)")
        else:
            for s in self.steps:
                lines.append(f"    - {s.kind:<10} {s.description}")
                # shlex.join (slice 18p, bug #7) keeps multi-word values
                # like `--agent-name "Hermes Inbox Helper"` quoted.
                lines.append(f"      $ {shlex.join(s.argv)}")
        lines.append("  local files to remove (after cloud cleanup succeeds):")
        if not self.local_paths:
            lines.append("    (none)")
        else:
            for p in self.local_paths:
                lines.append(f"    - {p}")
        return "\n".join(lines)


_DESCRIPTIONS: dict[CleanupKind, str] = {
    "azure": "remove Azure App Service + App Service Plan",
    "instance": "remove agent instance identity + user from Entra ID",
    "blueprint": "remove Entra blueprint app + service principal",
}


def _step_argv(kind: CleanupKind, inputs: CleanupInputs) -> list[str]:
    # `-y` lives on the parent `cleanup` verb in the GA help text, but
    # empirically it does NOT propagate to subcommands — slice 18w's
    # round-2 walkthrough caught `a365 cleanup -y blueprint ...` still
    # prompting "Continue with blueprint cleanup? (y/N):" and exiting
    # with rc=1 on empty stdin. So we keep `-y` (harmless redundancy +
    # documented intent) AND `apply_cleanup_plan` pipes `y\n` to stdin.
    argv = ["a365", "cleanup", "-y", kind, "--agent-name", inputs.agent_name]
    if inputs.tenant_id:
        argv.extend(["--tenant-id", inputs.tenant_id])
    return argv


def build_cleanup_plan(
    inputs: CleanupInputs,
    *,
    hermes_home: Path | None = None,
) -> CleanupPlan:
    """Compose the ordered list of CLI cleanup steps + local artefact list.

    Order is canonical (azure → instance → blueprint) regardless of the
    order in ``inputs.kinds`` — we always tear down the runtime infra
    before revoking the identity.
    """
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()

    requested = set(inputs.kinds) or set(CLEANUP_KINDS)
    steps: list[CleanupStep] = []
    for kind in CLEANUP_KINDS:  # canonical order
        if kind in requested:
            steps.append(
                CleanupStep(
                    kind=kind,
                    argv=_step_argv(kind, inputs),
                    description=_DESCRIPTIONS[kind],
                )
            )

    return CleanupPlan(
        inputs=inputs,
        steps=steps,
        local_paths=_local_artefacts(hermes_home, inputs.resolved_slug),
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


# Slice 19g: GA CLI v1.1.171.4547 hits Graph DELETE on a non-existent
# `/beta/agentUsers/<id>` segment when tearing down a published AI
# Teammate agent (round-3 walkthrough, 2026-05-05). The CLI logs both
# an inline failure and a final summary line; we accept either form so
# we still catch orphans if Microsoft tweaks one of the strings.
_ORPHAN_USER_RE = re.compile(
    r"(?:Failed to delete agentic user|Orphaned agentic user:)\s+"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def _parse_orphan_user_ids(stdout: str) -> list[str]:
    """Extract agentic-user object IDs the CLI couldn't delete.

    The agentic user is a regular Entra user reachable via
    `/beta/users/<id>`, so ``az ad user delete --id <id>`` cleans it up
    (driven by ``--purge-orphans``).
    """
    seen: list[str] = []
    for match in _ORPHAN_USER_RE.finditer(stdout):
        oid = match.group(1).lower()
        if oid not in seen:
            seen.append(oid)
    return seen


@dataclass
class CleanupResult:
    plan: CleanupPlan
    completed: list[CleanupKind] = field(default_factory=list)
    local_paths_removed: list[Path] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    # Slice 19g: orphan agentic users surfaced from CLI output.
    orphan_user_ids: list[str] = field(default_factory=list)
    orphans_purged: list[str] = field(default_factory=list)
    orphans_remaining: list[str] = field(default_factory=list)


def apply_cleanup_plan(
    plan: CleanupPlan,
    *,
    mutator: Mutator,
    hermes_home: Path,
    purge_orphans: bool = False,
) -> CleanupResult:
    """Run each cloud step in order; on success, remove local artefacts.

    Any AADSTS / CliInvocationError aborts the run — local files stay on
    disk so a re-run can pick up where we left off.

    Slice 19g: parses orphan agentic-user IDs from each CLI step's
    stdout. With ``purge_orphans=True`` the wrapper additionally runs
    ``az ad user delete --id <id>`` for each orphan after the CLI
    steps complete. Off by default — keeps cleanup pure-CLI in the
    happy case so we're not silently using a different identity than
    the CLI.
    """
    result = CleanupResult(plan=plan)

    for step in plan.steps:
        # Pre-feed `y\n` for the GA CLI's "Continue with X cleanup? (y/N):"
        # prompt that `-y` on the parent verb doesn't actually suppress
        # for subcommands. Slice 18w (corrects the gap left by 18l).
        run = mutator.run(step.argv, stdin_input="y\n")
        result.completed.append(step.kind)
        result.messages.append(f"[apply] {step.kind}: {step.description} — done")
        for oid in _parse_orphan_user_ids(run.stdout):
            if oid not in result.orphan_user_ids:
                result.orphan_user_ids.append(oid)

    for path in plan.local_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        result.local_paths_removed.append(path)
        result.messages.append(f"[apply] removed {path}")

    # Best-effort agent dir reaper.
    agent_dir = _agent_dir(hermes_home, plan.inputs.resolved_slug)
    if agent_dir.exists() and not any(agent_dir.iterdir()):
        agent_dir.rmdir()
        result.messages.append(f"[apply] removed empty dir {agent_dir}")

    # Slice 18x: `a365 cleanup -y` defensively backs the generated config
    # (which contains the blueprint client secret in plaintext on macOS /
    # Linux) into `*.backup-<timestamp>.json` before deleting the
    # original. The CLI writes those backups with default umask (644 —
    # world-readable). Tighten to 0600 so a stray operator-machine
    # incident doesn't leak the secret. Operators can still delete them
    # manually; we don't auto-delete because they have audit value.
    cwd = Path.cwd()
    for backup in sorted(cwd.glob("a365.*config.backup-*.json")):
        try:
            os.chmod(backup, 0o600)
            result.messages.append(f"[apply] chmod 600 {backup}")
        except OSError:
            # Don't fail cleanup over a chmod that didn't take.
            continue

    # Slice 19g: optionally purge orphan agentic users via az ad user
    # delete. The CLI's own delete path (`/beta/agentUsers/<id>`) 404s
    # — likely a Microsoft GA CLI defect — so the wrapper has to do the
    # cleanup itself when asked. Off by default; opt-in with
    # ``--purge-orphans`` so the CLI invocation set stays auditable.
    for oid in result.orphan_user_ids:
        if not purge_orphans:
            result.orphans_remaining.append(oid)
            result.messages.append(
                f"[apply] orphaned agentic user: {oid}; "
                f"recover with: az ad user delete --id {oid}"
            )
            continue
        try:
            mutator.run(["az", "ad", "user", "delete", "--id", oid])
        except CliInvocationError as e:
            result.orphans_remaining.append(oid)
            result.messages.append(
                f"[apply] purge failed for {oid}: {e}; "
                f"recover with: az ad user delete --id {oid}"
            )
            continue
        result.orphans_purged.append(oid)
        result.messages.append(f"[apply] purged orphan agentic user {oid}")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _validate_confirm(agent_name: str, confirm: str | None) -> None:
    if confirm is None:
        raise CleanupError(
            f"--confirm is required for --apply and must be the agent name literal "
            f"(e.g. --confirm={agent_name})"
        )
    if confirm != agent_name:
        raise CleanupError(
            f"--confirm value {confirm!r} does not match agent-name {agent_name!r}; "
            "refusing to proceed"
        )


def _parse_kinds(value: str | None) -> tuple[CleanupKind, ...]:
    """Parse ``--kinds=azure,instance`` into a validated tuple."""
    if not value:
        return CLEANUP_KINDS
    parts = [p.strip() for p in value.split(",") if p.strip()]
    for p in parts:
        if p not in CLEANUP_KINDS:
            raise CleanupError(f"unknown cleanup kind: {p!r}; allowed: {list(CLEANUP_KINDS)}")
    return tuple(parts)  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 cleanup — destructive teardown of an A365 agent.",
    )
    parser.add_argument("--agent-name", required=True, help="agent base name")
    parser.add_argument(
        "--tenant-id",
        help="tenant id; default auto-detects via `az account show`",
    )
    parser.add_argument(
        "--kinds",
        help=(
            "comma-separated subset of "
            f"{list(CLEANUP_KINDS)}; default = all (azure → instance → blueprint)"
        ),
    )
    parser.add_argument(
        "--slug",
        help=(
            "local slug under ~/.hermes/agents/<slug>/; "
            "defaults to slugify(--agent-name) — pass explicitly if you used a "
            "custom slug at `instance create` time"
        ),
    )
    parser.add_argument(
        "--confirm",
        help="must equal --agent-name for the apply path to proceed",
    )
    parser.add_argument("--apply", action="store_true", help="execute the plan; default is dry-run")
    # Slice 19g
    parser.add_argument(
        "--purge-orphans",
        action="store_true",
        help=(
            "after the CLI cleanup steps, run `az ad user delete --id <id>` "
            "for each agentic user the GA CLI failed to delete (its delete "
            "path `/beta/agentUsers/<id>` 404s). Off by default — surface "
            "the orphans only and exit 1 instead."
        ),
    )
    args = parser.parse_args(argv)

    try:
        kinds = _parse_kinds(args.kinds)
        inputs = CleanupInputs(
            agent_name=args.agent_name,
            tenant_id=args.tenant_id,
            kinds=kinds,
            slug=args.slug,
        )
    except (ValueError, CleanupError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    plan = build_cleanup_plan(inputs)
    sys.stdout.write(plan.render_human() + "\n")

    if not args.apply:
        sys.stdout.write(
            f"\nNo mutations. Re-run with --apply --confirm={args.agent_name} to tear down.\n"
        )
        return 0

    try:
        _validate_confirm(args.agent_name, args.confirm)
    except CleanupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        result = apply_cleanup_plan(
            plan,
            mutator=get_mutator(),
            hermes_home=_resolve_hermes_home(),
            purge_orphans=args.purge_orphans,
        )
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except CliInvocationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    # Slice 19g: exit 1 (partial) if we know an agentic user was left
    # behind — CI / scripted teardown should notice.
    if result.orphans_remaining:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
