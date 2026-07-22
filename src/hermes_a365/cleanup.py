"""hermes a365 cleanup — destructive teardown of an A365 agent.

Drives the real ``a365 cleanup`` subcommands. The CLI ships three
granular kinds plus a bare ``cleanup`` that does everything:

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
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import bot_service
from ._common import (
    TenantResolutionError,
    active_az_tenant,
    ensure_contained,
    resolve_expected_tenant,
    slugify,
    tenant_ids_match,
    validate_slug,
)
from .mutator import AADSTSError, CliInvocationError, Mutator, get_mutator

CleanupKind = Literal["bot-service", "azure", "instance", "blueprint"]
CLEANUP_KINDS: tuple[CleanupKind, ...] = ("bot-service", "azure", "instance", "blueprint")

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


def _local_artefacts(
    hermes_home: Path,
    slug: str,
    kinds: tuple[CleanupKind, ...] = CLEANUP_KINDS,
) -> list[Path]:
    """Return existing local files a cleanup scoped to ``kinds`` may remove.

    #102 M8: the agent ``.env`` is a CROSS-KIND identity file — it carries
    ``AA_INSTANCE_ID`` (instance), ``A365_APP_ID`` (blueprint), and the
    Path B ``A365_BF_*`` pair (bot-service) — so no single kind owns it.
    Removing it on a scoped run (e.g. ``--kinds=blueprint``) destroys the ids
    for still-live, still-billing infra with no local trace. It (and the
    runtime bridge files) therefore only leave with a FULL teardown;
    ``blueprint.json`` is the one artefact with a single owner."""
    agent_dir = _agent_dir(hermes_home, slug)
    full_teardown = set(kinds) >= set(CLEANUP_KINDS)
    candidates: list[Path] = []
    if full_teardown:
        candidates.append(agent_dir / ".env")
    if "blueprint" in kinds:
        # Legacy v0.1 cache that may linger on a fresh checkout.
        candidates.append(agent_dir / "blueprint.json")
    if full_teardown:
        candidates.append(agent_dir / "bridge.pid")
        candidates.append(agent_dir / "bridge.log")
    return [p for p in candidates if p.exists()]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class CleanupInputs:
    agent_name: str
    tenant_id: str | None = None
    kinds: tuple[CleanupKind, ...] = CLEANUP_KINDS  # default: all four
    slug: str | None = None  # local-dir slug; defaults to slugify(agent_name)
    bot_service_sidecar_path: Path = field(
        default_factory=lambda: Path.cwd() / bot_service.SIDECAR_FILENAME
    )

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

        #103/M9: the explicit ``--slug`` is returned VERBATIM and then
        joined into ``~/.hermes/agents/<slug>/`` for local-artefact
        deletion, so a traversal-shaped value would steer unlink/rmdir
        outside the agents root. Validate it here (the slugify fallback
        can never produce separators, so only the explicit branch needs
        the gate). Raises :class:`ValueError` on an unsafe slug.
        """
        if self.slug:
            return validate_slug(self.slug)
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
    bot_service_plan: bot_service.BotServiceCleanupPlan | None = None

    def bot_service_target_summary(self) -> str | None:
        if self.bot_service_plan is None or self.bot_service_plan.config is None:
            return None
        config = self.bot_service_plan.config
        binding = config.agentName if config.agentName is not None else "unbound legacy sidecar"
        return (
            f"bot {config.botName!r} in resource group {config.resourceGroup!r} "
            f"(subscription {config.subscriptionId}; provisioned for {binding!r})"
        )

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
                if s.kind == "bot-service":
                    target = self.bot_service_target_summary()
                    if target is not None:
                        lines.append(f"      sidecar target: {target}")
        lines.append("  local files to remove (after cloud cleanup succeeds):")
        if not self.local_paths:
            lines.append("    (none)")
        else:
            for p in self.local_paths:
                lines.append(f"    - {p}")
        return "\n".join(lines)


_DESCRIPTIONS: dict[CleanupKind, str] = {
    "bot-service": "remove Path B Azure Bot Service resource + sidecar",
    "azure": "remove Azure App Service + App Service Plan",
    "instance": "remove agent instance identity + user from Entra ID",
    "blueprint": "remove Entra blueprint app + service principal",
}


def _step_argv(kind: CleanupKind, inputs: CleanupInputs) -> list[str]:
    if kind == "bot-service":
        # Render-only: top-level apply calls bot_service.apply_cleanup_plan()
        # directly so it can share the in-process runner/test seam.
        return [
            "hermes-a365",
            "bot-service",
            "cleanup",
            "--agent-name",
            inputs.agent_name,
            "--sidecar",
            str(inputs.bot_service_sidecar_path),
        ]
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

    Order is canonical (bot-service → azure → instance → blueprint) regardless of the
    order in ``inputs.kinds`` — we always tear down the runtime infra
    before revoking the identity.
    """
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()

    # #102 review: normalize ONCE — an empty kinds tuple means "all four" for
    # the cloud steps, so the local-artefact scope must see the same
    # normalization or a full teardown would remove zero local files.
    requested = set(inputs.kinds) or set(CLEANUP_KINDS)
    normalized_kinds = tuple(k for k in CLEANUP_KINDS if k in requested)
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

    bot_service_plan = None
    if "bot-service" in requested:
        bot_service_plan = bot_service.build_cleanup_plan(
            bot_service.BotServiceCleanupInputs(
                agent_name=inputs.agent_name,
                sidecar_path=inputs.bot_service_sidecar_path,
            )
        )

    return CleanupPlan(
        inputs=inputs,
        steps=steps,
        # #102 M8: the plan (and so the dry-run listing) only names the
        # artefacts this run's kinds may actually remove.
        local_paths=_local_artefacts(hermes_home, inputs.resolved_slug, normalized_kinds),
        bot_service_plan=bot_service_plan,
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


# Slice 19h: GA CLI v1.1.171.4547 deletes the blueprint app + agent
# identity SP but leaves the `agentRegistry/agentInstances/<id>`
# registry entry orphaned (round-3 walkthrough finding, 2026-05-05).
# Tenant-wide instance count drifts up by one per cleanup. Snapshot
# the id from `a365.generated.config.json` *before* running the CLI
# steps (the CLI wipes the local config as part of cleanup), and
# surface — or DELETE via `az rest` — afterwards.
_AGENT_INSTANCES_GRAPH_URL = (
    "https://graph.microsoft.com/beta/agentRegistry/agentInstances/{instance_id}"
)


def _snapshot_agent_instance_id(generated_config_path: Path) -> str | None:
    """Read ``agentInstanceId`` from the generated config, if any.

    Returns ``None`` when the file is missing, unreadable, or carries
    no instance id. Cleanup proceeds either way — we just don't have
    an orphan to chase down.
    """
    try:
        raw = generated_config_path.read_text()
    except OSError:
        return None
    try:
        gen = json.loads(raw)
    except json.JSONDecodeError:
        return None
    instance_id = gen.get("agentInstanceId") if isinstance(gen, dict) else None
    if isinstance(instance_id, str) and instance_id.strip():
        return instance_id.strip()
    return None


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
    # Slice 19h: orphan agentRegistry/agentInstances entries snapshotted
    # from the generated config before the CLI wipes it. Distinct from
    # orphan_user_ids because the cleanup mechanism differs (Graph
    # DELETE via `az rest`, not `az ad user delete`).
    orphan_instance_ids: list[str] = field(default_factory=list)
    orphan_instances_purged: list[str] = field(default_factory=list)
    orphan_instances_remaining: list[str] = field(default_factory=list)


def apply_cleanup_plan(
    plan: CleanupPlan,
    *,
    mutator: Mutator,
    hermes_home: Path,
    purge_orphans: bool = False,
    generated_config_path: Path | None = None,
    additional_orphan_instance_ids: tuple[str, ...] = (),
    confirmed_orphan_instance_ids: tuple[str, ...] = (),
    bot_service_runner: bot_service.CommandRunner | None = None,
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

    Slice 19h: also snapshots ``agentInstanceId`` from
    ``a365.generated.config.json`` *before* the CLI wipes the local
    config, and surfaces the orphan agentRegistry entry (or
    ``--purge-orphans`` issues a Graph DELETE via ``az rest``).
    ``additional_orphan_instance_ids`` (driven by
    ``--orphan-instance-id``) plumbs in ids the operator knows about
    but that aren't in the local config — the AI Teammate flow
    creates the instance server-side via admin-centre activation, so
    the snapshot path can't see those (round-4 walkthrough finding,
    2026-05-05).
    """
    result = CleanupResult(plan=plan)

    if generated_config_path is None:
        generated_config_path = Path.cwd() / "a365.generated.config.json"
    snapshot_instance_id = _snapshot_agent_instance_id(generated_config_path)
    # Dedupe + canonical-case the union of snapshot and operator-supplied ids.
    # #102 L6: every candidate requires --confirm-orphan before deletion. The
    # snapshot file is selected by working directory and has no agent binding,
    # so treating it as trusted would preserve the wrong-directory footgun.
    candidate_instance_ids: list[str] = []
    if snapshot_instance_id is not None:
        candidate_instance_ids.append(snapshot_instance_id.lower())
    for oid in additional_orphan_instance_ids:
        normalised = oid.strip().lower()
        if not normalised:
            continue
        if normalised not in candidate_instance_ids:
            candidate_instance_ids.append(normalised)
    confirmed_instance_ids = {
        oid.strip().lower() for oid in confirmed_orphan_instance_ids if oid.strip()
    }

    # #102 M7: bind the WHOLE teardown to the persisted tenant BEFORE any
    # destructive step. The a365 subcommands auto-detect their tenant from the
    # ambient az session, and the two az orphan paths (`az ad user delete`,
    # `az rest` Graph DELETE) have no per-call tenant flag at all — so the only
    # sound enforcement is up-front: assert the active az tenant matches the
    # expected one (explicit --tenant-id, else the operator env's
    # A365_TENANT_ID), and refuse on mismatch or when the session can't be
    # verified. With no pin available anywhere we proceed — a fresh setup has
    # nothing to compare against — but say so loudly. (The bot-service step is
    # independently safe: its az calls pin the sidecar's subscriptionId, and a
    # subscription belongs to exactly one tenant.)
    try:
        expected_tenant, tenant_source = resolve_expected_tenant(
            hermes_home, plan.inputs.tenant_id
        )
    except TenantResolutionError as e:
        raise CleanupError(str(e)) from e
    if expected_tenant is None:
        result.messages.append(
            "[apply] WARNING: no tenant pin (--tenant-id absent and no "
            f"A365_TENANT_ID in {hermes_home / '.env'}) — proceeding against "
            "the ambient az/a365 session tenant"
        )
    else:
        active = active_az_tenant(lambda argv: mutator.run(argv))
        if active is None:
            raise CleanupError(
                "could not verify the active az tenant (az account show "
                f"failed or returned no tenantId) while a tenant pin exists "
                f"({tenant_source} = {expected_tenant}); refusing to run "
                "destructive cleanup against an unverifiable session. "
                "Run `az login --tenant <tenant>` and re-try."
            )
        try:
            tenant_matches = tenant_ids_match(
                active, expected_tenant, source=tenant_source
            )
        except TenantResolutionError as e:
            raise CleanupError(str(e)) from e
        if not tenant_matches:
            raise CleanupError(
                f"active az tenant {active} does not match the pinned tenant "
                f"{expected_tenant} ({tenant_source}); refusing to tear down "
                "against the wrong tenant. Run `az login --tenant "
                f"{expected_tenant}` (or pass --tenant-id) and re-try."
            )
        result.messages.append(
            f"[apply] tenant pin OK: active az tenant matches {tenant_source}"
        )

    for step in plan.steps:
        if step.kind == "bot-service":
            bs_plan = plan.bot_service_plan
            if bs_plan is None:
                raise CleanupError("bot-service cleanup step is missing its parsed target plan")
            bs_result = bot_service.apply_cleanup_plan(bs_plan, runner=bot_service_runner)
            result.completed.append(step.kind)
            blueprint_teardown_requested = "blueprint" in plan.inputs.kinds
            preserved_message = (
                bs_result.blueprint_preserved_message
                if blueprint_teardown_requested and bs_result.blueprint_preserved
                else None
            )
            for message in bs_result.messages:
                if message == preserved_message:
                    continue
                result.messages.append(message)
            if blueprint_teardown_requested:
                result.messages.append(
                    "[apply] bot-service cleanup complete before blueprint teardown"
                )
            else:
                result.messages.append("[apply] bot-service cleanup complete")
            continue
        # Pre-feed `y\n` for the GA CLI's "Continue with X cleanup? (y/N):"
        # prompt that `-y` on the parent verb doesn't actually suppress
        # for subcommands. Slice 18w (corrects the gap left by 18l).
        run = mutator.run(step.argv, stdin_input="y\n")
        result.completed.append(step.kind)
        result.messages.append(f"[apply] {step.kind}: {step.description} — done")
        for oid in _parse_orphan_user_ids(run.stdout):
            if oid not in result.orphan_user_ids:
                result.orphan_user_ids.append(oid)

    # #103/M9: every unlink/rmdir target must resolve inside the agents root.
    # ``resolved_slug`` already rejects traversal slugs, so this is
    # belt-and-braces (also catches a symlinked agent dir pointing outside):
    # fail closed with a clear operator error rather than deleting an
    # arbitrary path the CLI cleanup should never touch.
    agents_root = hermes_home / "agents"
    for path in plan.local_paths:
        try:
            ensure_contained(path, agents_root)
        except ValueError as e:
            raise CleanupError(f"refusing to delete a path outside the agents root: {e}") from e
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        result.local_paths_removed.append(path)
        result.messages.append(f"[apply] removed {path}")

    # Best-effort agent dir reaper. #102 M8: only when EVERY kind was torn
    # down in this run — keyed on ``result.completed`` (what actually
    # succeeded), not on what was requested, so a scoped run can never reap
    # the directory that still anchors live-infra identity for other kinds.
    agent_dir = _agent_dir(hermes_home, plan.inputs.resolved_slug)
    try:
        ensure_contained(agent_dir, agents_root)
    except ValueError as e:
        raise CleanupError(f"refusing to rmdir a path outside the agents root: {e}") from e
    all_kinds_torn_down = set(result.completed) >= set(CLEANUP_KINDS)
    if all_kinds_torn_down and agent_dir.exists() and not any(agent_dir.iterdir()):
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

    # Slice 19h: orphan agentRegistry entry. ``cleanup blueprint`` deletes
    # the Entra app + agent identity SP but leaves the
    # ``agentRegistry/agentInstances/<id>`` registry record behind. The
    # operator's account typically lacks ``AgentRegistry.ReadWrite.All``
    # so the DELETE may 403 — surface a recovery hint either way.
    for instance_id in candidate_instance_ids:
        result.orphan_instance_ids.append(instance_id)
        recovery = (
            f"az rest --method DELETE --uri "
            f"{_AGENT_INSTANCES_GRAPH_URL.format(instance_id=instance_id)}"
        )
        if not purge_orphans:
            result.orphan_instances_remaining.append(instance_id)
            result.messages.append(
                f"[apply] orphaned agentRegistry instance: "
                f"{instance_id}; recover with: {recovery}"
            )
            continue
        # #102 L6: every id must be re-typed to be DELETED. A snapshot from
        # ./a365.generated.config.json is selected by working directory and is
        # not cryptographically or structurally bound to --agent-name, so it
        # carries the same wrong-directory risk as an operator-supplied id.
        if instance_id not in confirmed_instance_ids:
            result.orphan_instances_remaining.append(instance_id)
            result.messages.append(
                f"[apply] refusing to purge agentRegistry instance "
                f"{instance_id} without --confirm-orphan {instance_id} "
                "(double-entry guard: a typo or stale generated config would "
                "delete an unrelated instance). Re-run with confirmation to purge it."
            )
            continue
        try:
            mutator.run(
                [
                    "az",
                    "rest",
                    "--method",
                    "DELETE",
                    "--uri",
                    _AGENT_INSTANCES_GRAPH_URL.format(instance_id=instance_id),
                ]
            )
        except CliInvocationError as e:
            result.orphan_instances_remaining.append(instance_id)
            result.messages.append(
                f"[apply] purge failed for instance "
                f"{instance_id}: {e}; recover with: {recovery}"
            )
            continue
        result.orphan_instances_purged.append(instance_id)
        result.messages.append(
            f"[apply] purged orphan agentRegistry instance {instance_id}"
        )

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


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
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
            f"{list(CLEANUP_KINDS)}; default = all "
            "(bot-service → azure → instance → blueprint)"
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
            "after the CLI cleanup steps, also (1) run `az ad user delete "
            "--id <id>` for each agentic user the GA CLI failed to delete "
            "(its delete path `/beta/agentUsers/<id>` 404s), and (2) issue "
            "a Graph DELETE on the orphaned agentRegistry instance the CLI "
            "leaves behind (slice 19h). Off by default — surface orphans "
            "only and exit 1 instead. Requires AgentRegistry.ReadWrite.All "
            "on the az CLI token for (2) to succeed."
        ),
    )
    parser.add_argument(
        "--orphan-instance-id",
        action="append",
        default=[],
        metavar="GUID",
        help=(
            "agentRegistry instance id known to be orphaned. The "
            "AI Teammate flow creates the instance server-side via "
            "admin-centre activation (round-4 walkthrough finding), "
            "so the snapshot-from-config path can't see the id; pass "
            "it explicitly here. May be repeated. Combined with "
            "`--purge-orphans` to issue the Graph DELETE."
        ),
    )
    # #102 L6
    parser.add_argument(
        "--confirm-orphan",
        action="append",
        default=[],
        metavar="GUID",
        help=(
            "re-type an orphan instance id to authorise its Graph DELETE "
            "under --purge-orphans. Double-entry guard: a typo'd "
            "instance id would delete an UNRELATED agentRegistry instance, "
            "and the wrapper has no Graph read with which to verify "
            "ownership. Snapshot-derived ids from a365.generated.config.json "
            "also require confirmation because that file is selected by the "
            "working directory, not bound to --agent-name. May be repeated."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        kinds = _parse_kinds(args.kinds)
        # #102 M7 belt-and-braces: resolve the pinned tenant up-front so the
        # a365 subcommands carry an explicit --tenant-id (visible in the plan)
        # instead of auto-detecting from the ambient session. The apply-time
        # pre-flight in apply_cleanup_plan remains the enforcement — it also
        # covers the az orphan paths that have no per-call tenant flag.
        pinned_tenant, _source = resolve_expected_tenant(
            _resolve_hermes_home(), args.tenant_id
        )
        inputs = CleanupInputs(
            agent_name=args.agent_name,
            tenant_id=pinned_tenant,
            kinds=kinds,
            slug=args.slug,
        )
    except (ValueError, CleanupError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        # #103/M9: building the plan resolves ``resolved_slug`` (which rejects
        # traversal-shaped ``--slug`` values); surface that as a clean rc=2.
        plan = build_cleanup_plan(inputs)
    except (ValueError, bot_service.BotServiceError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(plan.render_human() + "\n")

    if not args.apply:
        target = plan.bot_service_target_summary()
        target_text = f" Bot Service target: {target}." if target is not None else ""
        sys.stdout.write(
            f"\nNo mutations.{target_text} Re-run with --apply "
            f"--confirm={args.agent_name} to tear down.\n"
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
            additional_orphan_instance_ids=tuple(args.orphan_instance_id),
            confirmed_orphan_instance_ids=tuple(args.confirm_orphan),
        )
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except CliInvocationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except bot_service.BotServiceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except CleanupError as e:
        # #103/M9: ensure_contained refused an unlink/rmdir outside the agents root.
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    # Exit 1 (partial) if any orphan was left behind — CI / scripted
    # teardown should notice. Slice 19g (agentic users) and 19h
    # (agentRegistry instances) both feed this.
    if result.orphans_remaining or result.orphan_instances_remaining:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
