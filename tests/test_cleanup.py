"""Tests for scripts/cleanup.py — v0.2 around the real CLI cleanup subs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cleanup import (
    CLEANUP_KINDS,
    CleanupError,
    CleanupInputs,
    CleanupResult,
    _parse_kinds,
    _validate_confirm,
    apply_cleanup_plan,
    build_cleanup_plan,
)
from mutator import AADSTSError, CliInvocationError, RunResult

# ---------------------------------------------------------------------------
# FakeMutator (records argv lists)
# ---------------------------------------------------------------------------


@dataclass
class FakeMutator:
    available: bool = True
    calls: list[list[str]] = field(default_factory=list)
    scripted: list[RunResult | Exception] = field(default_factory=list)

    def run(self, argv: list[str], *, timeout: float = 60.0) -> RunResult:
        self.calls.append(list(argv))
        if self.scripted:
            nxt = self.scripted.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return RunResult(argv=list(argv), returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_agent_dir(
    tmp_path: Path,
    *,
    agent_name: str = "inbox-helper",
    with_env: bool = True,
    with_blueprint_cache: bool = False,
) -> Path:
    agent_dir = tmp_path / "agents" / agent_name
    agent_dir.mkdir(parents=True)
    if with_env:
        (agent_dir / ".env").write_text("AA_INSTANCE_ID=550e8400\n")
    if with_blueprint_cache:
        (agent_dir / "blueprint.json").write_text("{}")
    return agent_dir


# ---------------------------------------------------------------------------
# CleanupInputs validation
# ---------------------------------------------------------------------------


class TestCleanupInputs:
    def test_minimal_valid(self) -> None:
        inp = CleanupInputs(agent_name="x")
        assert inp.kinds == CLEANUP_KINDS

    def test_empty_agent_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="agent_name"):
            CleanupInputs(agent_name="")

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown cleanup kind"):
            CleanupInputs(agent_name="x", kinds=("bogus",))  # type: ignore[arg-type]

    def test_resolved_slug_defaults_to_slugified_agent_name(self) -> None:
        # Slice 18l (bug #12): closes the gap between CLI display name and
        # local-dir slug.
        assert CleanupInputs(agent_name="Hermes Inbox Helper").resolved_slug == (
            "hermes-inbox-helper"
        )

    def test_resolved_slug_explicit_override_wins(self) -> None:
        inp = CleanupInputs(agent_name="Hermes Inbox Helper", slug="custom-slug")
        assert inp.resolved_slug == "custom-slug"


# ---------------------------------------------------------------------------
# _parse_kinds
# ---------------------------------------------------------------------------


class TestParseKinds:
    def test_empty_means_all(self) -> None:
        assert _parse_kinds(None) == CLEANUP_KINDS
        assert _parse_kinds("") == CLEANUP_KINDS

    def test_subset(self) -> None:
        assert _parse_kinds("azure,instance") == ("azure", "instance")

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(CleanupError, match="unknown cleanup kind"):
            _parse_kinds("azure,bogus")


# ---------------------------------------------------------------------------
# _validate_confirm
# ---------------------------------------------------------------------------


class TestConfirm:
    def test_missing_confirm_rejected(self) -> None:
        with pytest.raises(CleanupError, match="--confirm is required"):
            _validate_confirm("inbox-helper", None)

    def test_mismatched_confirm_rejected(self) -> None:
        with pytest.raises(CleanupError, match="does not match"):
            _validate_confirm("inbox-helper", "wrong")

    def test_matching_confirm_accepted(self) -> None:
        _validate_confirm("inbox-helper", "inbox-helper")  # no exception


# ---------------------------------------------------------------------------
# build_cleanup_plan
# ---------------------------------------------------------------------------


class TestBuildCleanupPlan:
    def test_default_plans_all_three_in_canonical_order(self, tmp_path: Path) -> None:
        plan = build_cleanup_plan(CleanupInputs(agent_name="x"), hermes_home=tmp_path)
        kinds = [s.kind for s in plan.steps]
        assert kinds == ["azure", "instance", "blueprint"]

    def test_subset_preserves_canonical_order(self, tmp_path: Path) -> None:
        # Even when caller orders the kinds differently, we emit canonical.
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="x", kinds=("blueprint", "azure")),
            hermes_home=tmp_path,
        )
        kinds = [s.kind for s in plan.steps]
        assert kinds == ["azure", "blueprint"]

    def test_argv_shape_minimal(self, tmp_path: Path) -> None:
        plan = build_cleanup_plan(CleanupInputs(agent_name="x"), hermes_home=tmp_path)
        # Slice 18l (bug #11): -y is a parent flag — `a365 cleanup -y <kind>`,
        # not `a365 cleanup <kind> --yes`.
        assert plan.steps[0].argv == [
            "a365",
            "cleanup",
            "-y",
            "azure",
            "--agent-name",
            "x",
        ]

    def test_argv_shape_with_tenant(self, tmp_path: Path) -> None:
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="x", tenant_id="contoso.onmicrosoft.com"),
            hermes_home=tmp_path,
        )
        assert plan.steps[0].argv == [
            "a365",
            "cleanup",
            "-y",
            "azure",
            "--agent-name",
            "x",
            "--tenant-id",
            "contoso.onmicrosoft.com",
        ]

    def test_local_paths_picked_up(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path, with_env=True, with_blueprint_cache=True)
        plan = build_cleanup_plan(CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path)
        names = sorted(p.name for p in plan.local_paths)
        assert ".env" in names
        assert "blueprint.json" in names  # legacy v0.1 cache picked up too

    def test_no_local_paths_when_agent_dir_missing(self, tmp_path: Path) -> None:
        plan = build_cleanup_plan(CleanupInputs(agent_name="ghost"), hermes_home=tmp_path)
        assert plan.local_paths == []

    def test_local_paths_resolved_via_slugify_from_display_name(self, tmp_path: Path) -> None:
        # Bug #12 regression: passing a CLI display name (with spaces) used
        # to make local lookup miss the actual slugged dir.
        _seed_agent_dir(tmp_path, agent_name="hermes-inbox-helper")
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="Hermes Inbox Helper"),
            hermes_home=tmp_path,
        )
        names = [p.name for p in plan.local_paths]
        assert ".env" in names

    def test_local_paths_use_explicit_slug_override(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path, agent_name="custom-slug")
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="Whatever Display", slug="custom-slug"),
            hermes_home=tmp_path,
        )
        names = [p.name for p in plan.local_paths]
        assert ".env" in names


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------


class TestPlanRender:
    def test_human_lists_steps_and_argv_and_local_paths(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path)
        text = plan.render_human()
        assert "[plan] hermes a365 cleanup inbox-helper" in text
        assert "azure" in text
        assert "instance" in text
        assert "blueprint" in text
        assert "$ a365 cleanup -y azure" in text
        assert ".env" in text

    def test_human_shell_quotes_multi_word_agent_name(self, tmp_path: Path) -> None:
        """Slice 18p (bug #7): printed `$` line must be shell-pasteable."""
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="Hermes Inbox Helper"), hermes_home=tmp_path
        )
        text = plan.render_human()
        assert "--agent-name 'Hermes Inbox Helper'" in text

    def test_human_says_none_when_no_local_files(self, tmp_path: Path) -> None:
        plan = build_cleanup_plan(CleanupInputs(agent_name="ghost"), hermes_home=tmp_path)
        assert "(none)" in plan.render_human()


# ---------------------------------------------------------------------------
# apply_cleanup_plan
# ---------------------------------------------------------------------------


class TestApplyCleanup:
    def test_runs_three_cloud_steps_then_removes_local(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path)
        mutator = FakeMutator()
        result = apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)

        assert isinstance(result, CleanupResult)
        assert result.completed == ["azure", "instance", "blueprint"]
        # Mutator received argv lists matching plan order.
        # Index 3 = subcommand (after `a365 cleanup -y`).
        assert [argv[3] for argv in mutator.calls] == ["azure", "instance", "blueprint"]
        # Local .env was removed; agent dir reaped.
        assert not (tmp_path / "agents" / "inbox-helper" / ".env").exists()
        assert not (tmp_path / "agents" / "inbox-helper").exists()

    def test_subset_only_runs_selected_kinds(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper", kinds=("blueprint",)),
            hermes_home=tmp_path,
        )
        mutator = FakeMutator()
        result = apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)
        assert result.completed == ["blueprint"]
        assert [argv[3] for argv in mutator.calls] == ["blueprint"]

    def test_aadsts_error_propagates_and_local_files_remain(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path)
        mutator = FakeMutator(scripted=[AADSTSError("AADSTS65001", "no perms")])
        with pytest.raises(AADSTSError) as excinfo:
            apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)
        assert excinfo.value.code == "AADSTS65001"
        # Local .env stays — re-run can pick up.
        assert (tmp_path / "agents" / "inbox-helper" / ".env").exists()

    def test_cli_invocation_error_propagates(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path)
        mutator = FakeMutator(scripted=[CliInvocationError(["a365"], 7, "boom")])
        with pytest.raises(CliInvocationError):
            apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)

    def test_idempotent_re_run_after_full_cleanup(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path)
        apply_cleanup_plan(plan, mutator=FakeMutator(), hermes_home=tmp_path)

        # Second run: agent dir is gone, plan still has all three cloud steps
        # (we don't probe to check), but local_paths is empty.
        plan2 = build_cleanup_plan(CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path)
        assert plan2.local_paths == []

    def test_no_local_files_means_no_local_removal(self, tmp_path: Path) -> None:
        plan = build_cleanup_plan(CleanupInputs(agent_name="ghost"), hermes_home=tmp_path)
        result = apply_cleanup_plan(plan, mutator=FakeMutator(), hermes_home=tmp_path)
        assert result.local_paths_removed == []


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_kinds_constant_pinned() -> None:
    # Real CLI has exactly these three subs (verified 2026-05-04 v1.1.171).
    assert CLEANUP_KINDS == ("azure", "instance", "blueprint")
