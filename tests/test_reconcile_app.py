"""Tests for scripts/reconcile_app.py."""

from __future__ import annotations

import pytest
from reconcile_app import (
    ActualAppRegistration,
    AppPlan,
    DesiredAppRegistration,
    reconcile_app,
)


class TestDesiredAppRegistrationValidation:
    def test_valid(self) -> None:
        d = DesiredAppRegistration(name="X", tier=1)
        assert d.tier == 1

    @pytest.mark.parametrize("tier", [0, 3, -1])
    def test_invalid_tier(self, tier: int) -> None:
        with pytest.raises(ValueError, match="tier must be 1 or 2"):
            DesiredAppRegistration(name="X", tier=tier)

    def test_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            DesiredAppRegistration(name="", tier=1)

    def test_fic_only_on_tier2(self) -> None:
        with pytest.raises(ValueError, match="fic_required only applies to tier-2"):
            DesiredAppRegistration(name="X", tier=1, fic_required=True)
        # Allowed on tier 2
        DesiredAppRegistration(name="X", tier=2, fic_required=True)


class TestActualAppRegistrationFromQueryJson:
    def test_basic(self) -> None:
        payload = {
            "appId": "abc",
            "displayName": "Hermes Inbox Agent",
            "tier": 1,
            "isMultiTenant": True,
            "ficConfigured": False,
        }
        a = ActualAppRegistration.from_query_json(payload)
        assert a.app_id == "abc"
        assert a.display_name == "Hermes Inbox Agent"
        assert a.tier == 1
        assert a.is_multi_tenant is True
        assert a.fic_configured is False
        assert a.extra == {}

    def test_extras_collected(self) -> None:
        payload = {"appId": "abc", "displayName": "X", "tier": 2, "extraField": 42}
        a = ActualAppRegistration.from_query_json(payload)
        assert a.extra == {"extraField": 42}

    def test_missing_fields_default(self) -> None:
        a = ActualAppRegistration.from_query_json({})
        assert a.app_id == ""
        assert a.tier == 0
        assert a.is_multi_tenant is False


class TestReconcileApp:
    def test_create_when_no_actual(self) -> None:
        desired = DesiredAppRegistration(name="X", tier=1)
        plan = reconcile_app(desired, None)
        assert plan.action == "create"
        assert plan.actual is None
        assert plan.diff == {}
        assert plan.abort_reason is None

    def test_noop_when_identical(self) -> None:
        desired = DesiredAppRegistration(name="X", tier=1, is_multi_tenant=True)
        actual = ActualAppRegistration(
            app_id="abc",
            display_name="X",
            tier=1,
            is_multi_tenant=True,
        )
        plan = reconcile_app(desired, actual)
        assert plan.action == "noop"
        assert plan.diff == {}

    def test_patch_when_display_name_differs(self) -> None:
        desired = DesiredAppRegistration(name="New Name", tier=1)
        actual = ActualAppRegistration(
            app_id="abc",
            display_name="Old Name",
            tier=1,
            is_multi_tenant=True,
        )
        plan = reconcile_app(desired, actual)
        assert plan.action == "patch"
        assert plan.diff == {"display_name": ("Old Name", "New Name")}

    def test_patch_when_multi_tenant_differs(self) -> None:
        desired = DesiredAppRegistration(name="X", tier=1, is_multi_tenant=False)
        actual = ActualAppRegistration(
            app_id="abc",
            display_name="X",
            tier=1,
            is_multi_tenant=True,
        )
        plan = reconcile_app(desired, actual)
        assert plan.action == "patch"
        assert plan.diff == {"is_multi_tenant": (True, False)}

    def test_patch_when_fic_required_but_not_configured(self) -> None:
        desired = DesiredAppRegistration(name="X", tier=2, fic_required=True)
        actual = ActualAppRegistration(
            app_id="abc",
            display_name="X",
            tier=2,
            is_multi_tenant=True,
            fic_configured=False,
        )
        plan = reconcile_app(desired, actual)
        assert plan.action == "patch"
        assert plan.diff == {"fic_configured": (False, True)}

    def test_noop_when_fic_already_configured(self) -> None:
        desired = DesiredAppRegistration(name="X", tier=2, fic_required=True)
        actual = ActualAppRegistration(
            app_id="abc",
            display_name="X",
            tier=2,
            is_multi_tenant=True,
            fic_configured=True,
        )
        plan = reconcile_app(desired, actual)
        assert plan.action == "noop"

    def test_abort_on_tier_mismatch(self) -> None:
        desired = DesiredAppRegistration(name="X", tier=2)
        actual = ActualAppRegistration(
            app_id="abc",
            display_name="X",
            tier=1,
            is_multi_tenant=True,
        )
        plan = reconcile_app(desired, actual)
        assert plan.action == "abort"
        assert plan.abort_reason is not None
        assert "tier 1" in plan.abort_reason
        assert "tier 2" in plan.abort_reason

    def test_render_human_create(self) -> None:
        plan = reconcile_app(DesiredAppRegistration(name="X", tier=1), None)
        text = plan.render_human()
        assert "CREATE" in text
        assert "tier=1" in text

    def test_render_human_patch(self) -> None:
        desired = DesiredAppRegistration(name="New", tier=1)
        actual = ActualAppRegistration(
            app_id="abc",
            display_name="Old",
            tier=1,
            is_multi_tenant=True,
        )
        plan = reconcile_app(desired, actual)
        text = plan.render_human()
        assert "PATCH" in text
        assert "display_name" in text
        assert "Old" in text and "New" in text

    def test_render_human_abort(self) -> None:
        desired = DesiredAppRegistration(name="X", tier=2)
        actual = ActualAppRegistration(
            app_id="abc",
            display_name="X",
            tier=1,
            is_multi_tenant=True,
        )
        plan = reconcile_app(desired, actual)
        text = plan.render_human()
        assert "ABORT" in text
        assert "reason:" in text


class TestPlanShape:
    """Light structural sanity checks on AppPlan itself."""

    def test_default_diff_is_empty(self) -> None:
        plan = AppPlan(
            action="noop",
            desired=DesiredAppRegistration(name="X", tier=1),
            actual=None,
        )
        assert plan.diff == {}
