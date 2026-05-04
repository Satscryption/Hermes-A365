"""Tests for scripts/status.py — v0.2 narrowed component set."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from status import (
    A365CliQuerySource,
    QuerySource,
    StatusComponent,
    StatusReport,
    _classify_scopes_output,
    _UnavailableQuerySource,
    collect_status,
    gather_activity_bridge,
    gather_blueprint_scopes,
    gather_instance_scopes,
    gather_local_config,
    overall_to_exit_code,
    render_human,
    render_json,
)

# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@dataclass
class FakeQuerySource:
    """Records calls and returns scripted text-or-None per sub."""

    available: bool = True
    blueprint_text: str | None = None
    instance_text: str | None = None
    calls: list[tuple[str, dict[str, str | None]]] = field(default_factory=list)

    def query_blueprint_scopes(
        self, *, agent_name: str, tenant_id: str | None = None
    ) -> str | None:
        self.calls.append(("blueprint_scopes", {"agent_name": agent_name, "tenant_id": tenant_id}))
        return self.blueprint_text

    def query_instance_scopes(self, *, agent_name: str, tenant_id: str | None = None) -> str | None:
        self.calls.append(("instance_scopes", {"agent_name": agent_name, "tenant_id": tenant_id}))
        return self.instance_text


# Static check: FakeQuerySource satisfies the v0.2 QuerySource Protocol.
_: QuerySource = FakeQuerySource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_skill_env(home: Path, **overrides: str) -> None:
    base = {
        "A365_TENANT_ID": "contoso.onmicrosoft.com",
        "A365_APP_ID": "00000000-0000-0000-0000-00000000aaa1",
    }
    base.update(overrides)
    text = "".join(f"{k}={v}\n" for k, v in sorted(base.items()))
    (home / ".env").write_text(text)


# ---------------------------------------------------------------------------
# overall + exit-code mapping
# ---------------------------------------------------------------------------


def _component(name: str, state: str, detail: str = "") -> StatusComponent:
    return StatusComponent(name=name, state=state, detail=detail)  # type: ignore[arg-type]


class TestOverallAndExitCode:
    @pytest.mark.parametrize(
        "states,expected",
        [
            (["ok", "ok", "ok"], "ok"),
            (["ok", "warn", "ok"], "partial"),
            (["ok", "missing", "ok"], "partial"),
            (["ok", "error", "warn"], "broken"),
            (["error", "error"], "broken"),
            (["skipped", "skipped"], "ok"),  # skipped doesn't fail overall
        ],
    )
    def test_overall(self, states: list[str], expected: str) -> None:
        report = StatusReport(
            agent_name=None,
            components=[_component(f"c{i}", s) for i, s in enumerate(states)],
        )
        assert report.overall == expected

    def test_uninitialised_when_local_config_missing(self) -> None:
        report = StatusReport(
            agent_name=None,
            components=[_component("local_config", "missing")],
        )
        assert report.overall == "uninitialized"

    @pytest.mark.parametrize(
        "overall,code",
        [("ok", 0), ("partial", 1), ("broken", 2), ("uninitialized", 3)],
    )
    def test_exit_code_mapping(self, overall: str, code: int) -> None:
        assert overall_to_exit_code(overall) == code  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# gather_local_config
# ---------------------------------------------------------------------------


class TestLocalConfig:
    def test_missing_when_env_absent(self, tmp_path: Path) -> None:
        result = gather_local_config(tmp_path, None)
        assert result.state == "missing"
        assert "register" in result.detail

    def test_warn_when_required_keys_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("OTHER=x\n")
        result = gather_local_config(tmp_path, None)
        assert result.state == "warn"
        assert "missing keys" in result.detail

    def test_ok_at_skill_scope(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        result = gather_local_config(tmp_path, None)
        assert result.state == "ok"
        assert "tenant=" in result.detail

    def test_warn_when_agent_env_missing(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        result = gather_local_config(tmp_path, "inbox-helper")
        assert result.state == "warn"
        assert "agent .env missing" in result.detail

    def test_ok_at_agent_scope(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=550e8400\n")
        result = gather_local_config(tmp_path, "inbox-helper")
        assert result.state == "ok"
        assert "agent=inbox-helper" in result.detail


# ---------------------------------------------------------------------------
# _classify_scopes_output
# ---------------------------------------------------------------------------


class TestClassifyScopes:
    def test_consented_text_yields_ok(self) -> None:
        state, _ = _classify_scopes_output("Mail.Read: consented\nCalendars.Read: consented\n")
        assert state == "ok"

    def test_missing_consent_text_yields_warn(self) -> None:
        state, _ = _classify_scopes_output("Mail.Read: consented\nFiles.Read.All: not consented\n")
        assert state == "warn"

    def test_unclassifiable_yields_warn_with_first_line(self) -> None:
        state, detail = _classify_scopes_output("Some opaque output\n")
        assert state == "warn"
        assert detail.startswith("Some opaque output")


# ---------------------------------------------------------------------------
# gather_blueprint_scopes / gather_instance_scopes
# ---------------------------------------------------------------------------


class TestCloudScopeGatherers:
    def test_unavailable_query_source_skipped(self) -> None:
        qs = FakeQuerySource(available=False)
        result = gather_blueprint_scopes(qs, agent_name="x", tenant_id=None)
        assert result.state == "skipped"

    def test_no_agent_name_marks_missing(self) -> None:
        qs = FakeQuerySource(available=True, blueprint_text="ok")
        result = gather_blueprint_scopes(qs, agent_name=None, tenant_id=None)
        assert result.state == "missing"

    def test_none_returned_means_skipped(self) -> None:
        qs = FakeQuerySource(available=True, blueprint_text=None)
        result = gather_blueprint_scopes(qs, agent_name="x", tenant_id=None)
        assert result.state == "skipped"
        assert "interactive auth" in result.detail

    def test_consented_text_propagates_to_ok(self) -> None:
        qs = FakeQuerySource(available=True, blueprint_text="all scopes consented")
        result = gather_blueprint_scopes(qs, agent_name="x", tenant_id=None)
        assert result.state == "ok"

    def test_query_args_passed_through(self) -> None:
        qs = FakeQuerySource(available=True, instance_text="consented")
        gather_instance_scopes(qs, agent_name="inbox", tenant_id="t-1")
        assert qs.calls == [("instance_scopes", {"agent_name": "inbox", "tenant_id": "t-1"})]


# ---------------------------------------------------------------------------
# gather_activity_bridge (PID file probe — kept from v0.1)
# ---------------------------------------------------------------------------


class TestActivityBridge:
    def test_missing_pid_file(self, tmp_path: Path) -> None:
        result = gather_activity_bridge(tmp_path, "inbox-helper")
        assert result.state == "missing"
        assert "SPEC §10 Q1" in result.detail

    def test_alive_pid(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agents" / "inbox-helper"
        agent_dir.mkdir(parents=True)
        # Use this test process's pid — guaranteed alive.
        import os as _os

        (agent_dir / "bridge.pid").write_text(str(_os.getpid()))
        result = gather_activity_bridge(tmp_path, "inbox-helper")
        assert result.state == "ok"
        assert "(alive)" in result.detail

    def test_stale_pid(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agents" / "inbox-helper"
        agent_dir.mkdir(parents=True)
        # PID 1 belongs to launchd / systemd; if running as non-root we can
        # send signal 0 to it (raises PermissionError → "alive"). To force
        # the stale case use an absurdly high pid that won't exist.
        (agent_dir / "bridge.pid").write_text("99999999")
        result = gather_activity_bridge(tmp_path, "inbox-helper")
        assert result.state == "error"
        assert "stale" in result.detail


# ---------------------------------------------------------------------------
# collect_status orchestration
# ---------------------------------------------------------------------------


class TestCollectStatus:
    def test_uninitialised_short_circuits_cloud_probes(self, tmp_path: Path) -> None:
        report = collect_status(
            None, hermes_home=tmp_path, query_source=FakeQuerySource(available=True)
        )
        assert report.overall == "uninitialized"
        # Only local_config probed; cloud probes skipped.
        assert [c.name for c in report.components] == ["local_config"]

    def test_skill_scope_runs_blueprint_and_instance_probes(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        report = collect_status(
            None,
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        names = [c.name for c in report.components]
        assert "blueprint_scopes" in names
        assert "instance_scopes" in names
        # Skill-wide → no activity_bridge component.
        assert "activity_bridge" not in names

    def test_agent_scope_adds_activity_bridge(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=x\n")
        report = collect_status(
            "inbox-helper",
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        names = [c.name for c in report.components]
        assert names == [
            "local_config",
            "blueprint_scopes",
            "instance_scopes",
            "activity_bridge",
        ]

    def test_explicit_tenant_id_overrides_local(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        qs = FakeQuerySource(available=True, blueprint_text="consented")
        collect_status(
            "x",
            hermes_home=tmp_path,
            query_source=qs,
            tenant_id="override-tenant",
        )
        # Both calls saw the override.
        assert all(c[1]["tenant_id"] == "override-tenant" for c in qs.calls)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_human_skill_wide(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        report = collect_status(
            None, hermes_home=tmp_path, query_source=FakeQuerySource(available=False)
        )
        text = render_human(report)
        assert "hermes a365 status — (skill-wide)" in text
        assert "overall:" in text

    def test_human_agent_scope(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "x" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("\n")
        report = collect_status(
            "x", hermes_home=tmp_path, query_source=FakeQuerySource(available=False)
        )
        assert "hermes a365 status — x" in render_human(report)

    def test_json_round_trip(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        report = collect_status(
            None, hermes_home=tmp_path, query_source=FakeQuerySource(available=False)
        )
        payload = json.loads(render_json(report))
        assert payload["agent_name"] is None
        assert payload["overall"] in {"ok", "partial", "broken", "uninitialized"}
        assert any(c["name"] == "blueprint_scopes" for c in payload["components"])


# ---------------------------------------------------------------------------
# Concrete query sources
# ---------------------------------------------------------------------------


class TestConcreteSources:
    def test_unavailable_returns_none(self) -> None:
        qs = _UnavailableQuerySource()
        assert qs.query_blueprint_scopes(agent_name="x") is None
        assert qs.query_instance_scopes(agent_name="x") is None

    def test_a365_cli_marks_available_when_binary_present(self, tmp_path, monkeypatch) -> None:
        # Stub shutil.which for predictable behaviour.
        import shutil as _shutil

        monkeypatch.setattr(
            _shutil, "which", lambda name: "/usr/bin/a365" if name == "a365" else None
        )
        qs = A365CliQuerySource()
        assert qs.available is True
