"""Tests for scripts/reconcile_blueprint.py."""

from __future__ import annotations

import pytest
from reconcile_blueprint import BlueprintPlan, reconcile_blueprint


def _blueprint(slug: str = "inbox-helper", **overrides: object) -> dict[str, object]:
    """Build a representative blueprint payload for tests."""
    payload: dict[str, object] = {
        "displayName": slug,
        "agentIdentity": {
            "slug": slug,
            "description": "Summarises unread mail",
            "purpose": "productivity",
        },
        "appRoles": ["User"],
        "functions": [],
        "optionalClaims": {"idToken": ["oid", "tid", "preferred_username"]},
        "policies": {
            "dlp": "default-restricted",
            "externalAccess": "tenant-only",
            "logging": "verbose",
        },
        "workIqTools": [],
    }
    for k, v in overrides.items():
        payload[k] = v
    return payload


class TestReconcileBlueprint:
    def test_create_when_no_actual(self) -> None:
        desired = _blueprint()
        plan = reconcile_blueprint(desired, None)
        assert plan.action == "create"
        assert plan.slug == "inbox-helper"
        assert plan.actual is None
        assert plan.diff == {}

    def test_noop_when_identical(self) -> None:
        desired = _blueprint()
        plan = reconcile_blueprint(desired, dict(desired))
        assert plan.action == "noop"
        assert plan.diff == {}

    def test_patch_when_dlp_changes(self) -> None:
        actual = _blueprint()
        desired = _blueprint()
        desired_policies = dict(desired["policies"])  # type: ignore[arg-type]
        desired_policies["dlp"] = "default-strict"
        desired["policies"] = desired_policies
        plan = reconcile_blueprint(desired, actual)
        assert plan.action == "patch"
        assert plan.diff == {"policies/dlp": ("default-restricted", "default-strict")}

    def test_patch_when_workiq_added(self) -> None:
        actual = _blueprint()
        desired = _blueprint(workIqTools=["mail", "calendar"])
        plan = reconcile_blueprint(desired, actual)
        assert plan.action == "patch"
        # Length differs → root-level diff for the list
        assert "workIqTools" in plan.diff

    def test_patch_when_function_appended(self) -> None:
        actual = _blueprint()
        desired = _blueprint(functions=["summarise-mail"])
        plan = reconcile_blueprint(desired, actual)
        assert plan.action == "patch"
        assert "functions" in plan.diff

    def test_abort_on_slug_mismatch(self) -> None:
        actual = _blueprint(slug="something-else")
        actual_id = dict(actual["agentIdentity"])  # type: ignore[arg-type]
        actual_id["slug"] = "something-else"
        actual["agentIdentity"] = actual_id

        desired = _blueprint(slug="inbox-helper")
        plan = reconcile_blueprint(desired, actual)
        assert plan.action == "abort"
        assert plan.abort_reason is not None
        assert "something-else" in plan.abort_reason
        assert "inbox-helper" in plan.abort_reason

    def test_desired_must_have_slug(self) -> None:
        bad = {"displayName": "x"}  # no agentIdentity
        with pytest.raises(ValueError, match="missing agentIdentity/slug"):
            reconcile_blueprint(bad, None)

    def test_actual_with_missing_slug_treated_as_match(self) -> None:
        """If query-entra returns a payload without a recognisable slug field,
        we don't abort — fall through to deep_diff and let the diff speak."""
        actual = _blueprint()
        del actual["agentIdentity"]  # type: ignore[arg-type]

        desired = _blueprint()
        plan = reconcile_blueprint(desired, actual)
        assert plan.action == "patch"
        # The missing agentIdentity shows up in the diff
        assert "agentIdentity" in plan.diff


class TestRenderHuman:
    def test_create(self) -> None:
        plan = reconcile_blueprint(_blueprint(), None)
        text = plan.render_human()
        assert "CREATE" in text
        assert "inbox-helper" in text

    def test_noop(self) -> None:
        actual = _blueprint()
        plan = reconcile_blueprint(_blueprint(), dict(actual))
        text = plan.render_human()
        assert "NOOP" in text

    def test_patch_lists_paths(self) -> None:
        actual = _blueprint()
        desired = _blueprint()
        desired["policies"] = {**actual["policies"], "dlp": "default-strict"}  # type: ignore[dict-item]
        plan = reconcile_blueprint(desired, actual)
        text = plan.render_human()
        assert "PATCH" in text
        assert "policies/dlp" in text


class TestPlanShape:
    def test_default_diff_is_empty(self) -> None:
        plan = BlueprintPlan(
            action="noop",
            slug="x",
            desired=_blueprint(),
            actual=_blueprint(),
        )
        assert plan.diff == {}
