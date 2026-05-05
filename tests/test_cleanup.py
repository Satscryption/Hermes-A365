"""Tests for scripts/cleanup.py — v0.2 around the real CLI cleanup subs."""

from __future__ import annotations

import json
import os
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
    # Slice 18w: record stdin_input per call so cleanup tests can assert
    # we're feeding `y\n` to answer the GA CLI's "Continue with X
    # cleanup? (y/N):" prompt that `-y` doesn't suppress.
    stdin_inputs: list[str | None] = field(default_factory=list)
    scripted: list[RunResult | Exception] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 60.0,
        stdin_input: str | None = None,
    ) -> RunResult:
        self.calls.append(list(argv))
        self.stdin_inputs.append(stdin_input)
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
        # Slice 18w (bug #11 round-2): each step must be fed `y\n` so
        # the GA CLI's "Continue with X cleanup? (y/N):" prompt gets
        # answered. `-y` on the parent verb does NOT propagate to
        # subcommands — empirically verified during the 2026-05-05
        # round-2 walkthrough.
        assert mutator.stdin_inputs == ["y\n", "y\n", "y\n"]
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

    def test_chmods_secret_bearing_backups_to_600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slice 18x: `a365 cleanup -y` writes `*.backup-*.json` files
        with default umask (644). The `a365.generated.config.backup-*`
        copy carries the blueprint client secret in plaintext on
        macOS / Linux — tighten to 0600 after cleanup runs."""
        _seed_agent_dir(tmp_path)
        # Cleanup uses Path.cwd() to find the backups; chdir into a
        # tempdir so we don't see anything from the real repo.
        monkeypatch.chdir(tmp_path)
        gen_backup = tmp_path / "a365.generated.config.backup-20260101-000000.json"
        gen_backup.write_text('{"agentBlueprintClientSecret": "redacted"}\n')
        cfg_backup = tmp_path / "a365.config.backup-20260101-000000.json"
        cfg_backup.write_text("{}\n")
        # Both start world-readable (default umask).
        os.chmod(gen_backup, 0o644)
        os.chmod(cfg_backup, 0o644)

        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        result = apply_cleanup_plan(plan, mutator=FakeMutator(), hermes_home=tmp_path)

        assert (gen_backup.stat().st_mode & 0o777) == 0o600
        assert (cfg_backup.stat().st_mode & 0o777) == 0o600
        assert any("chmod 600" in m for m in result.messages)

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
# Slice 19g — orphan agentic-user surfacing + --purge-orphans
# ---------------------------------------------------------------------------


# Excerpt from a real round-3 walkthrough (2026-05-05) — the GA CLI's
# delete path `/beta/agentUsers/<id>` 404s and the agentic user is left
# orphaned. We accept either the inline failure line or the final
# summary line.
_REAL_ORPHAN_OUTPUT = (
    "Deleting agentic user: b8dab9ca-afa7-48aa-bb22-520eeabcd4f7\n"
    "ERROR: Graph DELETE https://graph.microsoft.com/beta/agentUsers/"
    "b8dab9ca-afa7-48aa-bb22-520eeabcd4f7 failed 400: "
    "Resource not found for the segment 'agentUsers'.\n"
    "ERROR: Failed to delete agentic user: b8dab9ca-afa7-48aa-bb22-520eeabcd4f7\n"
    "Failed to delete agentic user b8dab9ca-afa7-48aa-bb22-520eeabcd4f7"
    " -- will continue\n"
    "Blueprint cleanup encountered warnings.\n"
    "The following resources could not be deleted and remain orphaned in Entra ID:\n"
    "  Orphaned agentic user: b8dab9ca-afa7-48aa-bb22-520eeabcd4f7\n"
)


def _scripted_run(stdout: str = "") -> RunResult:
    return RunResult(argv=[], returncode=0, stdout=stdout, stderr="")


class TestOrphanParser:
    def test_extracts_guid_from_real_output(self) -> None:
        from cleanup import _parse_orphan_user_ids

        ids = _parse_orphan_user_ids(_REAL_ORPHAN_OUTPUT)
        assert ids == ["b8dab9ca-afa7-48aa-bb22-520eeabcd4f7"]

    def test_dedupes_repeats_within_single_step(self) -> None:
        from cleanup import _parse_orphan_user_ids

        # Same GUID hits both the inline 'Failed to delete' line and the
        # final 'Orphaned agentic user:' summary — count it once.
        assert len(_parse_orphan_user_ids(_REAL_ORPHAN_OUTPUT)) == 1

    def test_clean_output_yields_no_ids(self) -> None:
        from cleanup import _parse_orphan_user_ids

        assert _parse_orphan_user_ids("Cleanup successful.\n") == []


class TestApplyCleanupOrphans:
    def test_orphan_surfaced_with_recovery_hint_and_no_purge_by_default(
        self, tmp_path: Path
    ) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        # azure: clean, instance: clean, blueprint: leaves an orphan.
        mutator = FakeMutator(
            scripted=[
                _scripted_run(""),
                _scripted_run(""),
                _scripted_run(_REAL_ORPHAN_OUTPUT),
            ]
        )
        result = apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)

        assert result.orphan_user_ids == ["b8dab9ca-afa7-48aa-bb22-520eeabcd4f7"]
        assert result.orphans_purged == []
        assert result.orphans_remaining == ["b8dab9ca-afa7-48aa-bb22-520eeabcd4f7"]
        # The recovery line is the "ready-to-paste" affordance — anyone
        # tailing logs should see exactly what to run.
        assert any(
            "az ad user delete --id b8dab9ca-afa7-48aa-bb22-520eeabcd4f7" in m
            for m in result.messages
        )
        # No `az ad user delete` was actually invoked — purge was off.
        assert not any(c[:3] == ["az", "ad", "user"] for c in mutator.calls)

    def test_purge_orphans_runs_az_user_delete_and_clears_remaining(
        self, tmp_path: Path
    ) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        # 3 cleanup steps + 1 az purge call.
        mutator = FakeMutator(
            scripted=[
                _scripted_run(""),
                _scripted_run(""),
                _scripted_run(_REAL_ORPHAN_OUTPUT),
                _scripted_run(""),  # az ad user delete success
            ]
        )
        result = apply_cleanup_plan(
            plan, mutator=mutator, hermes_home=tmp_path, purge_orphans=True
        )

        assert result.orphans_purged == ["b8dab9ca-afa7-48aa-bb22-520eeabcd4f7"]
        assert result.orphans_remaining == []
        # The 4th call is `az ad user delete --id <guid>`.
        assert mutator.calls[-1] == [
            "az",
            "ad",
            "user",
            "delete",
            "--id",
            "b8dab9ca-afa7-48aa-bb22-520eeabcd4f7",
        ]

    def test_purge_orphans_failure_keeps_orphan_in_remaining(
        self, tmp_path: Path
    ) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        mutator = FakeMutator(
            scripted=[
                _scripted_run(""),
                _scripted_run(""),
                _scripted_run(_REAL_ORPHAN_OUTPUT),
                CliInvocationError(["az"], 1, "user delete failed"),
            ]
        )
        result = apply_cleanup_plan(
            plan, mutator=mutator, hermes_home=tmp_path, purge_orphans=True
        )

        assert result.orphans_purged == []
        assert result.orphans_remaining == ["b8dab9ca-afa7-48aa-bb22-520eeabcd4f7"]
        assert any("purge failed" in m for m in result.messages)
        # Recovery line still emitted so the operator can retry by hand.
        assert any(
            "az ad user delete --id b8dab9ca-afa7-48aa-bb22-520eeabcd4f7" in m
            for m in result.messages
        )

    def test_clean_run_records_no_orphans(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        result = apply_cleanup_plan(plan, mutator=FakeMutator(), hermes_home=tmp_path)
        assert result.orphan_user_ids == []
        assert result.orphans_purged == []
        assert result.orphans_remaining == []


# ---------------------------------------------------------------------------
# Slice 19h — orphan agentRegistry/agentInstances surfacing + purge
# ---------------------------------------------------------------------------


_TEST_INSTANCE_ID = "62433e08-c737-47d5-a53f-5b2f5bcd40ce"


def _seed_generated_config(
    cwd: Path, *, instance_id: str | None = _TEST_INSTANCE_ID
) -> Path:
    path = cwd / "a365.generated.config.json"
    payload: dict[str, str] = {
        "agentBlueprintId": "blueprint-app-id",
        "agentBlueprintClientSecret": "redacted",
    }
    if instance_id is not None:
        payload["agentInstanceId"] = instance_id
    path.write_text(json.dumps(payload))
    return path


class TestSnapshotAgentInstanceId:
    def test_returns_id_when_present(self, tmp_path: Path) -> None:
        from cleanup import _snapshot_agent_instance_id

        cfg = _seed_generated_config(tmp_path)
        assert _snapshot_agent_instance_id(cfg) == _TEST_INSTANCE_ID

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        from cleanup import _snapshot_agent_instance_id

        assert _snapshot_agent_instance_id(tmp_path / "nope.json") is None

    def test_returns_none_when_id_empty(self, tmp_path: Path) -> None:
        from cleanup import _snapshot_agent_instance_id

        cfg = _seed_generated_config(tmp_path, instance_id="")
        assert _snapshot_agent_instance_id(cfg) is None

    def test_returns_none_on_invalid_json(self, tmp_path: Path) -> None:
        from cleanup import _snapshot_agent_instance_id

        cfg = tmp_path / "a365.generated.config.json"
        cfg.write_text("{not valid json")
        assert _snapshot_agent_instance_id(cfg) is None


class TestApplyCleanupOrphanInstance:
    def test_orphan_instance_surfaced_with_recovery_hint(
        self, tmp_path: Path
    ) -> None:
        _seed_agent_dir(tmp_path)
        cfg_path = _seed_generated_config(tmp_path)
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        result = apply_cleanup_plan(
            plan,
            mutator=FakeMutator(),
            hermes_home=tmp_path,
            generated_config_path=cfg_path,
        )

        assert result.orphan_instance_ids == [_TEST_INSTANCE_ID]
        assert result.orphan_instances_purged == []
        assert result.orphan_instances_remaining == [_TEST_INSTANCE_ID]
        # Recovery hint is the exact `az rest` line a human can paste.
        assert any(
            f"az rest --method DELETE --uri https://graph.microsoft.com/beta/"
            f"agentRegistry/agentInstances/{_TEST_INSTANCE_ID}"
            in m
            for m in result.messages
        )

    def test_purge_orphans_runs_az_rest_delete(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        cfg_path = _seed_generated_config(tmp_path)
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        # 3 cleanup steps + 1 az rest DELETE.
        mutator = FakeMutator()
        result = apply_cleanup_plan(
            plan,
            mutator=mutator,
            hermes_home=tmp_path,
            purge_orphans=True,
            generated_config_path=cfg_path,
        )

        assert result.orphan_instances_purged == [_TEST_INSTANCE_ID]
        assert result.orphan_instances_remaining == []
        assert mutator.calls[-1] == [
            "az",
            "rest",
            "--method",
            "DELETE",
            "--uri",
            f"https://graph.microsoft.com/beta/agentRegistry/agentInstances/"
            f"{_TEST_INSTANCE_ID}",
        ]

    def test_purge_failure_keeps_orphan_in_remaining(self, tmp_path: Path) -> None:
        _seed_agent_dir(tmp_path)
        cfg_path = _seed_generated_config(tmp_path)
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        # The az rest call is the 4th invocation (after the 3 cleanup
        # steps). Only it raises — common in the field on accounts that
        # lack AgentRegistry.ReadWrite.All on the delegated token.
        mutator = FakeMutator(
            scripted=[
                RunResult(argv=[], returncode=0, stdout="", stderr=""),
                RunResult(argv=[], returncode=0, stdout="", stderr=""),
                RunResult(argv=[], returncode=0, stdout="", stderr=""),
                CliInvocationError(["az"], 3, "Insufficient privileges"),
            ]
        )
        result = apply_cleanup_plan(
            plan,
            mutator=mutator,
            hermes_home=tmp_path,
            purge_orphans=True,
            generated_config_path=cfg_path,
        )

        assert result.orphan_instances_purged == []
        assert result.orphan_instances_remaining == [_TEST_INSTANCE_ID]
        assert any("purge failed for instance" in m for m in result.messages)

    def test_no_generated_config_means_no_orphan_instance(
        self, tmp_path: Path
    ) -> None:
        _seed_agent_dir(tmp_path)
        # No generated config → nothing to snapshot → no orphan claimed.
        plan = build_cleanup_plan(
            CleanupInputs(agent_name="inbox-helper"), hermes_home=tmp_path
        )
        result = apply_cleanup_plan(
            plan,
            mutator=FakeMutator(),
            hermes_home=tmp_path,
            generated_config_path=tmp_path / "nope.json",
        )
        assert result.orphan_instance_ids == []
        assert result.orphan_instances_remaining == []


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_kinds_constant_pinned() -> None:
    # Real CLI has exactly these three subs (verified 2026-05-04 v1.1.171).
    assert CLEANUP_KINDS == ("azure", "instance", "blueprint")
