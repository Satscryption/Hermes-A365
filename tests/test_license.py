"""Tests for hermes_a365.license — pure functions, no I/O."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_a365.license import (
    ADDON_THRESHOLD_USERS,
    ADMIN_CENTER_CATALOG_URL,
    PRICE_ADDON_PER_USER_MONTHLY,
    PRICE_E7_PER_USER_MONTHLY,
    LicenseInputs,
    main,
    recommend,
    render_human,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "license"


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


def _check_golden(name: str, actual: str, *, update: bool) -> None:
    path = GOLDEN_DIR / name
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    expected = path.read_text()
    assert actual == expected, (
        f"golden mismatch: {name}\n--- expected ---\n{expected}\n--- actual ---\n{actual}"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestRecommendValidation:
    @pytest.mark.parametrize("users", [-1, -100])
    def test_negative_users_rejected(self, users: int) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            recommend(LicenseInputs(users=users, agents=1, plan="E5"))

    def test_negative_agents_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            recommend(LicenseInputs(users=10, agents=-1, plan="E5"))

    def test_unknown_plan_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown plan"):
            recommend(LicenseInputs(users=10, agents=1, plan="X9"))


# ---------------------------------------------------------------------------
# Decision rule
# ---------------------------------------------------------------------------


class TestDecisionRule:
    """The §6.1 decision rule branches:

    - users < 25 OR plan < E5 → per_agent
    - users ≥ 25 AND plan ≥ E5 AND bundled-security wanted → e7
    - users ≥ 25 AND plan ≥ E5 AND bundled-security NOT wanted → per_agent (cheaper)
    """

    def test_small_team_e3(self) -> None:
        rec = recommend(LicenseInputs(users=10, agents=2, plan="E3"))
        assert rec.model == "per_agent"
        # Slice 18o (bug #4): both predicates fire for users=10/plan=E3 —
        # rationale joins them with " and " rather than the old " or "
        # template that produced nonsense like "plan=E5 < E5".
        assert "users=10 < 25" in rec.rationale
        assert "plan=E3 below E5" in rec.rationale
        assert " and " in rec.rationale

    def test_small_team_e5(self) -> None:
        rec = recommend(LicenseInputs(users=10, agents=2, plan="E5"))
        assert rec.model == "per_agent"
        # Slice 18o (bug #4): only the user-threshold predicate is true;
        # rationale must NOT mention the (false) plan predicate.
        assert "users=10 < 25" in rec.rationale
        assert "below E5" not in rec.rationale

    def test_large_team_e5_no_security_bundle(self) -> None:
        rec = recommend(LicenseInputs(users=200, agents=10, plan="E5"))
        assert rec.model == "per_agent"
        assert "no bundled-security" in rec.rationale

    def test_large_team_e5_wants_security_bundle(self) -> None:
        rec = recommend(
            LicenseInputs(
                users=200,
                agents=10,
                plan="E5",
                bundled_security_wanted=True,
            )
        )
        assert rec.model == "e7"
        assert "bundled" in rec.rationale.lower()

    def test_threshold_boundary_24_users(self) -> None:
        # Just under threshold → per_agent
        rec = recommend(
            LicenseInputs(
                users=ADDON_THRESHOLD_USERS - 1,
                agents=1,
                plan="E5",
                bundled_security_wanted=True,
            )
        )
        assert rec.model == "per_agent"

    def test_threshold_boundary_25_users(self) -> None:
        # At threshold → eligible for e7 if security wanted
        rec = recommend(
            LicenseInputs(
                users=ADDON_THRESHOLD_USERS,
                agents=1,
                plan="E5",
                bundled_security_wanted=True,
            )
        )
        assert rec.model == "e7"


# ---------------------------------------------------------------------------
# Cost arithmetic
# ---------------------------------------------------------------------------


class TestCostArithmetic:
    def test_addon_costs(self) -> None:
        rec = recommend(LicenseInputs(users=12, agents=3, plan="E5"))
        assert rec.monthly_cost == 12 * PRICE_ADDON_PER_USER_MONTHLY  # 180
        assert rec.annual_cost == 12 * PRICE_ADDON_PER_USER_MONTHLY * 12  # 2160
        # Alternative cost = E7 annual
        assert rec.alternative_annual_cost == 12 * PRICE_E7_PER_USER_MONTHLY * 12

    def test_e7_costs(self) -> None:
        rec = recommend(
            LicenseInputs(
                users=250,
                agents=40,
                plan="E5",
                bundled_security_wanted=True,
            )
        )
        assert rec.model == "e7"
        assert rec.monthly_cost == 250 * PRICE_E7_PER_USER_MONTHLY
        assert rec.annual_cost == 250 * PRICE_E7_PER_USER_MONTHLY * 12
        # Alternative = add-on annual
        assert rec.alternative_annual_cost == 250 * PRICE_ADDON_PER_USER_MONTHLY * 12

    def test_zero_users_renders_without_error(self) -> None:
        # 0 users isn't useful but shouldn't crash
        rec = recommend(LicenseInputs(users=0, agents=0, plan="E3"))
        assert rec.monthly_cost == 0
        assert rec.annual_cost == 0


# ---------------------------------------------------------------------------
# Rendering (golden-file)
# ---------------------------------------------------------------------------


class TestRenderHuman:
    def test_addon_recommendation_golden(self, update_golden: bool) -> None:
        inputs = LicenseInputs(users=12, agents=3, plan="E5")
        text = render_human(inputs, recommend(inputs))
        _check_golden("addon_small_team.txt", text, update=update_golden)

    def test_e7_recommendation_golden(self, update_golden: bool) -> None:
        inputs = LicenseInputs(
            users=250,
            agents=40,
            plan="E5",
            bundled_security_wanted=True,
        )
        text = render_human(inputs, recommend(inputs))
        _check_golden("e7_large_team.txt", text, update=update_golden)

    def test_render_includes_admin_url(self) -> None:
        inputs = LicenseInputs(users=5, agents=1, plan="E3")
        text = render_human(inputs, recommend(inputs))
        assert ADMIN_CENTER_CATALOG_URL in text

    def test_render_never_says_purchase_executed(self) -> None:
        # The output should reinforce that this command never purchases.
        inputs = LicenseInputs(users=5, agents=1, plan="E3")
        text = render_human(inputs, recommend(inputs))
        assert "never purchases" in text.lower() or "manual" in text.lower()

    def test_render_surfaces_subscribed_skus_partnumber(self) -> None:
        # Slice 18o (bug #6): operators look at `subscribedSkus` to verify
        # what's installed; the recommendation must name the actual
        # partNumber so that lookup is unambiguous.
        addon = render_human(
            LicenseInputs(users=5, agents=1, plan="E3"),
            recommend(LicenseInputs(users=5, agents=1, plan="E3")),
        )
        assert "MICROSOFT_AGENT_365_TIER_3" in addon

        e7_inputs = LicenseInputs(
            users=200, agents=10, plan="E5", bundled_security_wanted=True
        )
        e7 = render_human(e7_inputs, recommend(e7_inputs))
        assert "MICROSOFT_365_E7" in e7


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_happy_path(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--users", "12", "--agents", "3", "--plan", "E5"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Recommendation:" in out
        assert "Agent 365 add-on" in out

    def test_negative_users_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--users", "-1", "--agents", "1", "--plan", "E5"])
        assert rc == 2
        assert "non-negative" in capsys.readouterr().err

    def test_e7_via_cli(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "--users",
                "200",
                "--agents",
                "30",
                "--plan",
                "E5",
                "--bundled-security",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "E7" in out
