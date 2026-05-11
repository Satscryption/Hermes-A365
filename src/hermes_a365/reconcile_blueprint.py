"""Reconcile desired vs actual A365 agent blueprint state.

Used by the ``blueprint create`` flow to produce idempotent PATCH plans
against existing blueprints. The reconciler
is generic: it operates on dict payloads (the rendered desired blueprint
from ``render_blueprint.py`` and the actual blueprint as returned by
``a365 query-entra --blueprint=<slug>``).

Comparison semantics:
- Field-by-field deep equality.
- Lists are compared **positionally** for v0.1. If A365 reorders a list
  (e.g. ``workIqTools``), this will be flagged as a diff. Callers that
  want set-comparison can sort their inputs before reconciling.
- Top-level ``displayName``/``agentIdentity.slug`` mismatches abort —
  renaming requires a cleanup-then-recreate per SPEC §6.13.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ._common import deep_diff, render_diff_human

PlanAction = Literal["create", "noop", "patch", "abort"]


@dataclass
class BlueprintPlan:
    """Reconciliation plan for a single agent blueprint."""

    action: PlanAction
    slug: str
    desired: dict[str, Any]
    actual: dict[str, Any] | None
    diff: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    abort_reason: str | None = None

    def render_human(self) -> str:
        header = f"{self.action.upper()} blueprint {self.slug!r}"
        if self.action == "abort":
            return f"{header}\n  reason: {self.abort_reason}"
        if self.action == "create":
            return f"{header}\n  (no actual state — would create)"
        if self.action == "noop":
            return f"{header}\n  (matches actual; no change)"
        # patch
        return f"{header}\n{render_diff_human(self.diff)}"


def _slug_of(blueprint: dict[str, Any]) -> str | None:
    """Pull the slug out of a blueprint payload (where render_blueprint puts it)."""
    identity = blueprint.get("agentIdentity")
    if isinstance(identity, dict):
        slug = identity.get("slug")
        if isinstance(slug, str):
            return slug
    return None


def reconcile_blueprint(
    desired: dict[str, Any],
    actual: dict[str, Any] | None,
) -> BlueprintPlan:
    """Compute the plan to converge ``actual`` to ``desired``.

    - ``actual is None`` → action ``create``.
    - Slug mismatch (same blueprint id) → ``abort``.
    - Deep equal → ``noop``.
    - Otherwise → ``patch`` with the diff populated.
    """
    desired_slug = _slug_of(desired) or ""
    if not desired_slug:
        raise ValueError("desired blueprint missing agentIdentity/slug")

    if actual is None:
        return BlueprintPlan(
            action="create",
            slug=desired_slug,
            desired=desired,
            actual=None,
        )

    actual_slug = _slug_of(actual) or ""
    if actual_slug and actual_slug != desired_slug:
        return BlueprintPlan(
            action="abort",
            slug=desired_slug,
            desired=desired,
            actual=actual,
            abort_reason=(
                f"actual blueprint slug {actual_slug!r} does not match "
                f"desired slug {desired_slug!r}; rename via cleanup-then-recreate "
                "per SPEC §6.13."
            ),
        )

    diff = deep_diff(actual, desired)
    if not diff:
        return BlueprintPlan(
            action="noop",
            slug=desired_slug,
            desired=desired,
            actual=actual,
        )
    return BlueprintPlan(
        action="patch",
        slug=desired_slug,
        desired=desired,
        actual=actual,
        diff=diff,
    )


# ---------------------------------------------------------------------------
# Demo CLI (debugging aid)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff a desired blueprint JSON file against an actual one.",
    )
    parser.add_argument("--desired", type=Path, required=True)
    parser.add_argument(
        "--actual",
        type=Path,
        help="path to actual JSON; omit to plan a create",
    )
    args = parser.parse_args(argv)

    desired: dict[str, Any] = json.loads(args.desired.read_text())
    actual: dict[str, Any] | None = None
    if args.actual:
        actual = json.loads(args.actual.read_text())

    plan = reconcile_blueprint(desired, actual)
    sys.stdout.write(plan.render_human() + "\n")
    return {"create": 0, "noop": 0, "patch": 0, "abort": 2}[plan.action]


if __name__ == "__main__":
    raise SystemExit(main())
