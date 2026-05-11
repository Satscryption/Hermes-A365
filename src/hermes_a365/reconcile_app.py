"""Reconcile desired vs actual Entra app registration state.

Used by the ``register`` command to produce idempotent plans: same
desired-and-actual = ``noop``; differences = ``patch``;
no actual state at all = ``create``; a tier or app-id mismatch on the same
display name = ``abort`` (refuse to silently mutate someone else's app).

This module is **library-only** for the higher-level commands. A small
``--desired/--actual`` CLI is provided as a debugging aid for ad-hoc diffing
against a captured ``a365 query-entra`` JSON blob.

The ``ActualAppRegistration`` shape is **approximate** — Microsoft does not
publish a JSON Schema for this surface. We document the fields we read and
leave room to grow.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ._common import render_diff_human

PlanAction = Literal["create", "noop", "patch", "abort"]


@dataclass(frozen=True)
class DesiredAppRegistration:
    """Desired Entra app state, as the operator wants it after `register`."""

    name: str  # display name
    tier: int  # 1 (first-party multi-tenant) or 2 (confidential client)
    is_multi_tenant: bool = True
    fic_required: bool = False  # T2: configure user-FIC

    def __post_init__(self) -> None:
        if self.tier not in (1, 2):
            raise ValueError(f"tier must be 1 or 2, got {self.tier}")
        if not self.name:
            raise ValueError("name must be non-empty")
        if self.fic_required and self.tier != 2:
            raise ValueError("fic_required only applies to tier-2 apps")


@dataclass(frozen=True)
class ActualAppRegistration:
    """Actual Entra app state, parsed from `a365 query-entra --by-name <name>`.

    Field shape is approximate — see module docstring.
    """

    app_id: str
    display_name: str
    tier: int
    is_multi_tenant: bool
    fic_configured: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_query_json(cls, payload: dict[str, Any]) -> ActualAppRegistration:
        """Parse the JSON shape we (speculatively) expect from `a365 query-entra`.

        Tolerant of missing fields — anything we don't recognise lands in ``extra``.
        """
        known = {"appId", "displayName", "tier", "isMultiTenant", "ficConfigured"}
        extra = {k: v for k, v in payload.items() if k not in known}
        return cls(
            app_id=payload.get("appId", ""),
            display_name=payload.get("displayName", ""),
            tier=int(payload.get("tier", 0)),
            is_multi_tenant=bool(payload.get("isMultiTenant", False)),
            fic_configured=bool(payload.get("ficConfigured", False)),
            extra=extra,
        )


@dataclass
class AppPlan:
    """Reconciliation plan for a single Entra app registration."""

    action: PlanAction
    desired: DesiredAppRegistration
    actual: ActualAppRegistration | None
    diff: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    abort_reason: str | None = None

    def render_human(self) -> str:
        header = f"{self.action.upper()} app {self.desired.name!r} (tier={self.desired.tier})"
        if self.action == "abort":
            return f"{header}\n  reason: {self.abort_reason}"
        if self.action == "create":
            return f"{header}\n  (no actual state — would create)"
        if self.action == "noop":
            return f"{header}\n  (matches actual; no change)"
        # patch
        return f"{header}\n{render_diff_human(self.diff)}"


def reconcile_app(
    desired: DesiredAppRegistration,
    actual: ActualAppRegistration | None,
) -> AppPlan:
    """Compute the plan to converge ``actual`` to ``desired``.

    - ``actual is None`` → action ``create``.
    - Tier mismatch on same name → ``abort`` (refuse to silently mutate).
    - All fields equal → ``noop``.
    - Some fields differ → ``patch`` with the diff dict populated.
    """
    if actual is None:
        return AppPlan(action="create", desired=desired, actual=None)

    if actual.tier != desired.tier:
        return AppPlan(
            action="abort",
            desired=desired,
            actual=actual,
            abort_reason=(
                f"existing app named {actual.display_name!r} is tier {actual.tier} "
                f"with appId {actual.app_id!r}; desired is tier {desired.tier}. "
                "refusing to mutate; rename or remove the existing app first."
            ),
        )

    diff: dict[str, tuple[Any, Any]] = {}
    if actual.display_name != desired.name:
        diff["display_name"] = (actual.display_name, desired.name)
    if actual.is_multi_tenant != desired.is_multi_tenant:
        diff["is_multi_tenant"] = (actual.is_multi_tenant, desired.is_multi_tenant)
    if desired.fic_required and not actual.fic_configured:
        diff["fic_configured"] = (False, True)

    if not diff:
        return AppPlan(action="noop", desired=desired, actual=actual)
    return AppPlan(action="patch", desired=desired, actual=actual, diff=diff)


# ---------------------------------------------------------------------------
# Demo CLI (debugging aid)
# ---------------------------------------------------------------------------


def _load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff a desired Entra app spec against an actual a365 query-entra JSON.",
    )
    parser.add_argument("--name", required=True, help="desired display name")
    parser.add_argument("--tier", required=True, type=int, choices=[1, 2])
    parser.add_argument("--multi-tenant", action="store_true", default=True)
    parser.add_argument("--no-multi-tenant", dest="multi_tenant", action="store_false")
    parser.add_argument("--fic-required", action="store_true")
    parser.add_argument(
        "--actual",
        type=Path,
        help="path to a JSON file with the actual state (omit for create plan)",
    )
    args = parser.parse_args(argv)

    desired = DesiredAppRegistration(
        name=args.name,
        tier=args.tier,
        is_multi_tenant=args.multi_tenant,
        fic_required=args.fic_required,
    )
    actual: ActualAppRegistration | None = None
    if args.actual:
        actual = ActualAppRegistration.from_query_json(_load_json_file(args.actual))

    plan = reconcile_app(desired, actual)
    sys.stdout.write(plan.render_human() + "\n")
    return {"create": 0, "noop": 0, "patch": 0, "abort": 2}[plan.action]


if __name__ == "__main__":
    raise SystemExit(main())
