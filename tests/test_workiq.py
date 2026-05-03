"""Tests for scripts/workiq.py."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from blueprint_create import build_blueprint_plan
from render_blueprint import BlueprintInputs
from workiq import (
    WorkIqError,
    compute_desired_workiq,
    reconstitute_inputs,
    update_workiq,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeQuerySource:
    available: bool = True
    blueprints: dict[str, dict[str, Any]] = field(default_factory=dict)

    def query_license(self) -> dict[str, Any] | None:
        return None

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None:
        return None

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return self.blueprints.get(slug)

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return None


@dataclass
class FakeMutator:
    available: bool = True
    setup_blueprint_response: dict[str, Any] = field(
        default_factory=lambda: {"blueprintId": "bp-x"}
    )
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def fic_configure(self, *, app_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def setup_blueprint(self, *, file_path: Path) -> dict[str, Any]:
        self.calls.append(("setup_blueprint", {"file_path": file_path}))
        return self.setup_blueprint_response

    def create_instance(  # pragma: no cover
        self, *, blueprint_slug: str, instance_id: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    def deploy(  # pragma: no cover
        self, *, instance_id: str, channels: list[str]
    ) -> dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_cached_blueprint(
    tmp_path: Path,
    slug: str = "inbox-helper",
    workiq: list[str] | None = None,
) -> Path:
    """Render a real blueprint via build_blueprint_plan and write it to the cache path."""
    inputs = BlueprintInputs(
        slug=slug,
        description="Summarises mail",
        purpose="productivity",
        workiq_tools=workiq or [],
    )
    ctx = build_blueprint_plan(inputs, query_source=FakeQuerySource())
    cache = tmp_path / "agents" / slug / "blueprint.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(ctx.rendered, indent=2, sort_keys=True) + "\n")
    return cache


# ---------------------------------------------------------------------------
# reconstitute_inputs
# ---------------------------------------------------------------------------


class TestReconstituteInputs:
    def test_round_trip_via_render(self, tmp_path: Path) -> None:
        # Render with a known set of inputs, then reconstitute and verify
        # the re-render matches.
        original = BlueprintInputs(
            slug="inbox-helper",
            description="Summarises mail",
            purpose="productivity",
            workiq_tools=["mail", "calendar"],
            functions=["fetch", "draft"],
            dlp_policy="default-strict",
        )
        ctx = build_blueprint_plan(original, query_source=FakeQuerySource())
        rebuilt = reconstitute_inputs(ctx.rendered)
        assert rebuilt.slug == "inbox-helper"
        assert rebuilt.description == "Summarises mail"
        assert rebuilt.purpose == "productivity"
        assert sorted(rebuilt.workiq_tools) == ["calendar", "mail"]
        assert rebuilt.functions == ["fetch", "draft"]
        assert rebuilt.dlp_policy == "default-strict"

    def test_defensive_against_missing_fields(self) -> None:
        # An almost-empty payload still produces a valid object (with sane
        # defaults), apart from slug/description/purpose which the reconciler
        # will catch separately.
        rebuilt = reconstitute_inputs({"agentIdentity": {"slug": "x"}})
        assert rebuilt.slug == "x"
        assert rebuilt.workiq_tools == []
        # Defaults applied.
        assert rebuilt.dlp_policy == "default-restricted"
        assert rebuilt.external_access_policy == "tenant-only"
        assert rebuilt.logging_policy == "verbose"


# ---------------------------------------------------------------------------
# compute_desired_workiq
# ---------------------------------------------------------------------------


class TestComputeDesiredWorkiq:
    def test_set_to_replaces(self) -> None:
        assert compute_desired_workiq(["mail"], enable=None, disable=None, set_to=["calendar"]) == [
            "calendar"
        ]

    def test_enable_adds(self) -> None:
        assert compute_desired_workiq(["mail"], enable=["calendar"], disable=None, set_to=None) == [
            "calendar",
            "mail",
        ]

    def test_disable_removes(self) -> None:
        assert compute_desired_workiq(
            ["mail", "calendar"], enable=None, disable=["mail"], set_to=None
        ) == ["calendar"]

    def test_enable_and_disable_can_combine(self) -> None:
        assert compute_desired_workiq(
            ["mail"], enable=["calendar"], disable=["mail"], set_to=None
        ) == ["calendar"]

    def test_set_to_mutually_exclusive_with_enable(self) -> None:
        with pytest.raises(WorkIqError, match="mutually exclusive"):
            compute_desired_workiq([], enable=["mail"], disable=None, set_to=[])

    def test_set_to_mutually_exclusive_with_disable(self) -> None:
        with pytest.raises(WorkIqError, match="mutually exclusive"):
            compute_desired_workiq(["mail"], enable=None, disable=["mail"], set_to=[])

    def test_unknown_tool_rejected(self) -> None:
        with pytest.raises(WorkIqError, match="unknown workiq tools"):
            compute_desired_workiq([], enable=["slack"], disable=None, set_to=None)

    def test_returns_sorted(self) -> None:
        result = compute_desired_workiq([], enable=None, disable=None, set_to=["teams", "mail"])
        assert result == ["mail", "teams"]


# ---------------------------------------------------------------------------
# update_workiq — preconditions
# ---------------------------------------------------------------------------


class TestUpdateWorkiqPreconditions:
    def test_missing_cached_blueprint_fails_clean(self, tmp_path: Path) -> None:
        with pytest.raises(WorkIqError, match="blueprint create"):
            update_workiq(
                "inbox-helper",
                enable=["mail"],
                hermes_home=tmp_path,
                query_source=FakeQuerySource(),
            )

    def test_corrupt_cache_fails_clean(self, tmp_path: Path) -> None:
        cache = tmp_path / "agents" / "inbox-helper" / "blueprint.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("{ not json")
        with pytest.raises(WorkIqError, match="not valid JSON"):
            update_workiq(
                "inbox-helper",
                enable=["mail"],
                hermes_home=tmp_path,
                query_source=FakeQuerySource(),
            )

    def test_cache_missing_slug_fails_clean(self, tmp_path: Path) -> None:
        cache = tmp_path / "agents" / "inbox-helper" / "blueprint.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("{}")
        with pytest.raises(WorkIqError, match=r"missing agentIdentity\.slug"):
            update_workiq(
                "inbox-helper",
                enable=["mail"],
                hermes_home=tmp_path,
                query_source=FakeQuerySource(),
            )


# ---------------------------------------------------------------------------
# update_workiq — dry-run
# ---------------------------------------------------------------------------


class TestUpdateWorkiqDryRun:
    def test_enable_dry_run(self, tmp_path: Path) -> None:
        _seed_cached_blueprint(tmp_path, workiq=["mail"])
        result = update_workiq(
            "inbox-helper",
            enable=["calendar"],
            apply=False,
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
        )
        assert result.before == ["mail"]
        assert result.after == ["calendar", "mail"]
        assert result.mutated is False
        assert any("workiq:" in m for m in result.messages)
        assert any("blueprint plan: create" in m for m in result.messages)

    def test_noop_when_set_unchanged(self, tmp_path: Path) -> None:
        # Cached blueprint has mail; we ask to enable mail again — same set.
        _seed_cached_blueprint(tmp_path, workiq=["mail"])
        # Plant the desired blueprint as the cloud actual so blueprint plan = noop.
        rendered = json.loads((tmp_path / "agents" / "inbox-helper" / "blueprint.json").read_text())
        qs = FakeQuerySource(blueprints={"inbox-helper": rendered})
        result = update_workiq(
            "inbox-helper",
            enable=["mail"],
            apply=False,
            hermes_home=tmp_path,
            query_source=qs,
        )
        assert result.before == result.after == ["mail"]
        assert any("workiq unchanged" in m for m in result.messages)
        assert any("blueprint plan: noop" in m for m in result.messages)


# ---------------------------------------------------------------------------
# update_workiq — apply
# ---------------------------------------------------------------------------


class TestUpdateWorkiqApply:
    def test_apply_calls_setup_blueprint_with_new_workiq(self, tmp_path: Path) -> None:
        _seed_cached_blueprint(tmp_path, workiq=["mail"])
        mutator = FakeMutator()
        result = update_workiq(
            "inbox-helper",
            enable=["calendar"],
            apply=True,
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
            mutator=mutator,
        )
        assert result.mutated is True
        assert result.after == ["calendar", "mail"]
        assert mutator.calls and mutator.calls[0][0] == "setup_blueprint"
        # Verify the JSON written to disk reflects the new workiq list.
        cache = tmp_path / "agents" / "inbox-helper" / "blueprint.json"
        cached = json.loads(cache.read_text())
        assert cached["workIqTools"] == ["calendar", "mail"]

    def test_apply_disable_removes_tool(self, tmp_path: Path) -> None:
        _seed_cached_blueprint(tmp_path, workiq=["mail", "calendar"])
        mutator = FakeMutator()
        result = update_workiq(
            "inbox-helper",
            disable=["calendar"],
            apply=True,
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
            mutator=mutator,
        )
        assert result.after == ["mail"]
        cache = tmp_path / "agents" / "inbox-helper" / "blueprint.json"
        cached = json.loads(cache.read_text())
        assert cached["workIqTools"] == ["mail"]

    def test_apply_set_replaces_entirely(self, tmp_path: Path) -> None:
        _seed_cached_blueprint(tmp_path, workiq=["mail", "calendar"])
        mutator = FakeMutator()
        result = update_workiq(
            "inbox-helper",
            set_to=["teams"],
            apply=True,
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
            mutator=mutator,
        )
        assert result.after == ["teams"]
