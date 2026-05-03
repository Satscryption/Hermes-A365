"""hermes a365 workiq — toggle Work IQ MCP exposure for an agent.

Spec: SPEC.md §6.6. Config-only; no local MCP server runs. The set of
exposed Work IQ tools is stored on the blueprint, so changes flow through
the blueprint reconciler rather than a dedicated A365 endpoint.

Reads the cached blueprint at ``~/.hermes/agents/<slug>/blueprint.json``
(written by ``hermes a365 blueprint create``), applies the requested
``--enable``/``--disable``/``--set`` change to ``workIqTools``, and hands
the updated input set to :mod:`blueprint_create`'s pipeline. The reconciler
takes care of the create-or-patch decision.

Default mode is dry-run; ``--apply`` executes the underlying blueprint
update.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blueprint_create import (
    BlueprintCreateError,
    apply_blueprint_plan,
    build_blueprint_plan,
)
from register import AADSTSError, get_mutator
from render_blueprint import (
    DEFAULT_APP_ROLES,
    DEFAULT_DLP,
    DEFAULT_EXTERNAL_ACCESS,
    DEFAULT_LOGGING,
    DEFAULT_OPTIONAL_CLAIMS,
    WORKIQ_TOOLS,
    BlueprintInputs,
)

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkIqError(RuntimeError):
    """Raised when workiq can't proceed (no cached blueprint, bad args)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _cache_path(hermes_home: Path, slug: str) -> Path:
    return hermes_home / "agents" / slug / "blueprint.json"


def reconstitute_inputs(payload: dict[str, Any]) -> BlueprintInputs:
    """Rebuild ``BlueprintInputs`` from a cached blueprint JSON payload.

    Defensive about missing fields: anything absent falls back to the same
    defaults ``render_blueprint`` uses, so a manually-edited cache file
    still produces a sensible reconstitution.
    """
    identity = payload.get("agentIdentity") or {}
    policies = payload.get("policies") or {}
    optional_claims_block = payload.get("optionalClaims") or {}
    optional_claims = (
        optional_claims_block.get("idToken") if isinstance(optional_claims_block, dict) else None
    ) or list(DEFAULT_OPTIONAL_CLAIMS)
    return BlueprintInputs(
        slug=identity.get("slug", "") or "",
        description=identity.get("description", "") or "",
        purpose=identity.get("purpose", "") or "",
        functions=list(payload.get("functions") or []),
        app_roles=list(payload.get("appRoles") or DEFAULT_APP_ROLES),
        optional_claims=list(optional_claims),
        dlp_policy=policies.get("dlp") or DEFAULT_DLP,
        external_access_policy=policies.get("externalAccess") or DEFAULT_EXTERNAL_ACCESS,
        logging_policy=policies.get("logging") or DEFAULT_LOGGING,
        workiq_tools=list(payload.get("workIqTools") or []),
        display_name=payload.get("displayName"),
    )


def compute_desired_workiq(
    current: list[str],
    *,
    enable: list[str] | None,
    disable: list[str] | None,
    set_to: list[str] | None,
) -> list[str]:
    """Apply the requested change to the current Work IQ tool list.

    ``set_to`` replaces the whole list; ``enable``/``disable`` perform
    set-add / set-remove. Mixing modes is rejected. Unknown tool names
    raise :class:`WorkIqError`.
    """
    if set_to is not None and (enable or disable):
        raise WorkIqError("--set is mutually exclusive with --enable/--disable")

    if set_to is not None:
        desired_set = set(set_to)
    else:
        desired_set = set(current)
        if enable:
            desired_set.update(enable)
        if disable:
            desired_set.difference_update(disable)

    unknown = desired_set - WORKIQ_TOOLS
    if unknown:
        raise WorkIqError(
            f"unknown workiq tools: {sorted(unknown)}; allowed: {sorted(WORKIQ_TOOLS)}"
        )
    return sorted(desired_set)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class WorkIqResult:
    slug: str
    before: list[str]
    after: list[str]
    blueprint_id: str | None
    mutated: bool
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def update_workiq(
    slug: str,
    *,
    enable: list[str] | None = None,
    disable: list[str] | None = None,
    set_to: list[str] | None = None,
    apply: bool = False,
    hermes_home: Path | None = None,
    mutator=None,
    query_source=None,
) -> WorkIqResult:
    """Read the cached blueprint, compute the new Work IQ list, reconcile.

    When ``apply`` is False the returned result describes the prospective
    change but no mutation is performed; the underlying blueprint plan
    action (``create``/``noop``/``patch``) is included in the messages.
    """
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    cache = _cache_path(hermes_home, slug)
    if not cache.exists():
        raise WorkIqError(f"{cache} not found; run `hermes a365 blueprint create {slug}` first")
    try:
        payload = json.loads(cache.read_text())
    except json.JSONDecodeError as e:
        raise WorkIqError(f"{cache} is not valid JSON: {e}") from e

    current_inputs = reconstitute_inputs(payload)
    if not current_inputs.slug:
        raise WorkIqError(f"{cache} is missing agentIdentity.slug")
    before = sorted(current_inputs.workiq_tools)

    after = compute_desired_workiq(before, enable=enable, disable=disable, set_to=set_to)

    new_inputs = BlueprintInputs(
        slug=current_inputs.slug,
        description=current_inputs.description,
        purpose=current_inputs.purpose,
        functions=current_inputs.functions,
        app_roles=current_inputs.app_roles,
        optional_claims=current_inputs.optional_claims,
        dlp_policy=current_inputs.dlp_policy,
        external_access_policy=current_inputs.external_access_policy,
        logging_policy=current_inputs.logging_policy,
        workiq_tools=after,
        display_name=current_inputs.display_name,
    )

    ctx = build_blueprint_plan(new_inputs, query_source=query_source)

    diff_phrase = (
        f"workiq: {before or '(none)'} -> {after or '(none)'}"
        if before != after
        else f"workiq unchanged: {after or '(none)'}"
    )

    if not apply:
        return WorkIqResult(
            slug=slug,
            before=before,
            after=after,
            blueprint_id=ctx.existing_blueprint_id,
            mutated=False,
            messages=[
                f"[plan] hermes a365 workiq {slug}",
                f"  {diff_phrase}",
                f"  blueprint plan: {ctx.plan.action}",
            ],
        )

    result = apply_blueprint_plan(
        new_inputs,
        ctx,
        mutator=mutator if mutator is not None else get_mutator(),
        hermes_home=hermes_home,
    )
    return WorkIqResult(
        slug=slug,
        before=before,
        after=after,
        blueprint_id=result.blueprint_id,
        mutated=result.mutated,
        messages=[f"[apply] hermes a365 workiq {slug}", f"  {diff_phrase}", *result.messages],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 workiq — toggle Work IQ MCP exposure on an agent's blueprint.",
    )
    parser.add_argument("slug", help="agent slug")
    parser.add_argument("--enable", default="", help="comma-separated tools to add")
    parser.add_argument("--disable", default="", help="comma-separated tools to remove")
    parser.add_argument(
        "--set",
        default=None,
        help="comma-separated tools to set as the absolute list (replaces existing)",
    )
    parser.add_argument("--apply", action="store_true", help="execute the plan; default is dry-run")
    args = parser.parse_args(argv)

    enable = _split_csv(args.enable) or None
    disable = _split_csv(args.disable) or None
    set_to = _split_csv(args.set) if args.set is not None else None

    try:
        result = update_workiq(
            args.slug,
            enable=enable,
            disable=disable,
            set_to=set_to,
            apply=args.apply,
        )
    except WorkIqError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except BlueprintCreateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2

    sys.stdout.write("\n".join(result.messages) + "\n")
    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to update the blueprint.\n")
    else:
        sys.stdout.write("done.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
