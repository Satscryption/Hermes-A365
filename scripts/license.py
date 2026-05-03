"""hermes a365 license — license-model recommendation.

Spec: SPEC.md §6.1. Read-only; never purchases. Inputs are user count,
agent count, current M365 plan, and (optionally) whether the operator
wants the bundled Copilot+Defender+Purview offering. Output is a
recommendation (``per_agent`` add-on or ``e7``), the rationale, monthly
and annual cost estimates, and a link to the admin-center catalog where
a tenant admin can complete the purchase.

Programmatic use::

    from license import LicenseInputs, recommend, render_human
    rec = recommend(LicenseInputs(users=12, agents=3, plan="E5"))

CLI use::

    python scripts/license.py --users 12 --agents 3 --plan E5
    python scripts/license.py --users 250 --agents 40 --plan E5 --bundled-security
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Literal

PRICE_ADDON_PER_USER_MONTHLY = 15  # USD
PRICE_E7_PER_USER_MONTHLY = 99  # USD
ADDON_THRESHOLD_USERS = 25
ADMIN_CENTER_CATALOG_URL = "https://admin.microsoft.com/Adminportal/Home#/catalog"

LicenseModel = Literal["per_agent", "e7"]

# M365 plan tier ordering. Spec only references E3 / E5 / E7 explicitly.
PLAN_TIERS: dict[str, int] = {"E3": 3, "E5": 5, "E7": 7}


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LicenseInputs:
    users: int
    agents: int
    plan: str  # M365 plan: E3 / E5 / E7
    bundled_security_wanted: bool = False  # E7 bundles Copilot+Defender+Purview


@dataclass(frozen=True)
class LicenseRecommendation:
    model: LicenseModel
    monthly_cost: int  # total $/month
    annual_cost: int  # total $/year
    rationale: str
    alternative_annual_cost: int


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------


def recommend(inputs: LicenseInputs) -> LicenseRecommendation:
    """Apply the §6.1 decision rule and return a typed recommendation."""
    if inputs.users < 0 or inputs.agents < 0:
        raise ValueError("users and agents must be non-negative")
    if inputs.plan not in PLAN_TIERS:
        raise ValueError(f"unknown plan {inputs.plan!r}; expected one of {sorted(PLAN_TIERS)}")

    plan_tier = PLAN_TIERS[inputs.plan]

    if inputs.users < ADDON_THRESHOLD_USERS or plan_tier < PLAN_TIERS["E5"]:
        model: LicenseModel = "per_agent"
        rationale = f"users={inputs.users} < {ADDON_THRESHOLD_USERS} or plan={inputs.plan} < E5"
    elif inputs.bundled_security_wanted:
        model = "e7"
        rationale = (
            f"users={inputs.users} >= {ADDON_THRESHOLD_USERS}, plan={inputs.plan}, "
            "and bundled Copilot+Defender+Purview wanted"
        )
    else:
        model = "per_agent"
        rationale = (
            f"users={inputs.users} >= {ADDON_THRESHOLD_USERS}, plan={inputs.plan}, "
            "no bundled-security need — add-on is cheaper"
        )

    addon_monthly = inputs.users * PRICE_ADDON_PER_USER_MONTHLY
    e7_monthly = inputs.users * PRICE_E7_PER_USER_MONTHLY

    if model == "per_agent":
        return LicenseRecommendation(
            model=model,
            monthly_cost=addon_monthly,
            annual_cost=addon_monthly * 12,
            rationale=rationale,
            alternative_annual_cost=e7_monthly * 12,
        )
    return LicenseRecommendation(
        model=model,
        monthly_cost=e7_monthly,
        annual_cost=e7_monthly * 12,
        rationale=rationale,
        alternative_annual_cost=addon_monthly * 12,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _model_label(model: LicenseModel) -> str:
    if model == "per_agent":
        return f"Agent 365 add-on (${PRICE_ADDON_PER_USER_MONTHLY}/user/mo)"
    return f"Microsoft 365 E7 (${PRICE_E7_PER_USER_MONTHLY}/user/mo)"


def render_human(inputs: LicenseInputs, rec: LicenseRecommendation) -> str:
    """Render a markdown-aligned recommendation block to stdout-friendly text."""
    title = "A365 license recommendation"
    lines = [
        title,
        "=" * len(title),
        f"Users:    {inputs.users}",
        f"Agents:   {inputs.agents}",
        f"M365:     {inputs.plan}",
    ]
    if inputs.bundled_security_wanted:
        lines.append("Bundled:  Copilot+Defender+Purview wanted")
    lines.append("")

    lines.append(f"Recommendation: {_model_label(rec.model)}")
    lines.append(f"  Reason:      {rec.rationale}")
    per_user = (
        PRICE_ADDON_PER_USER_MONTHLY if rec.model == "per_agent" else PRICE_E7_PER_USER_MONTHLY
    )
    lines.append(f"  Monthly:     ${rec.monthly_cost:,} ({inputs.users} x ${per_user})")
    lines.append(f"  Annual:      ${rec.annual_cost:,}")

    alt_label = "E7" if rec.model == "per_agent" else "Agent 365 add-on"
    lines.append(f"  Alternative: {alt_label} → ${rec.alternative_annual_cost:,}/yr")
    lines.append("")
    lines.append("Next step (manual; this command never purchases):")
    lines.append(f"  Open admin centre: {ADMIN_CENTER_CATALOG_URL}")
    lines.append("  Then re-run: python scripts/doctor.py")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recommend an A365 license model. Read-only; never purchases.",
    )
    parser.add_argument("--users", type=int, required=True)
    parser.add_argument("--agents", type=int, required=True)
    parser.add_argument("--plan", required=True, choices=sorted(PLAN_TIERS))
    parser.add_argument(
        "--bundled-security",
        action="store_true",
        help="user wants Copilot+Defender+Purview bundled (favours E7)",
    )
    args = parser.parse_args(argv)

    try:
        inputs = LicenseInputs(
            users=args.users,
            agents=args.agents,
            plan=args.plan,
            bundled_security_wanted=args.bundled_security,
        )
        rec = recommend(inputs)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(render_human(inputs, rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
