"""Tests for hermes_a365.status — v0.2 narrowed component set."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hermes_a365.bot_service import BotServiceConfig, CommandResult
from hermes_a365.bot_service_diagnostics import DiagnosticResult
from hermes_a365.status import (
    A365CliQuerySource,
    QuerySource,
    StatusComponent,
    StatusReport,
    _classify_scopes_output,
    _UnavailableQuerySource,
    collect_status,
    gather_activity_bridge,
    gather_blueprint_scopes,
    gather_bot_service,
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


class FakeBotServiceRunner:
    def __init__(
        self,
        *,
        bot_app_id: str = "11111111-1111-1111-1111-111111111111",
        endpoint: str = "https://example.test/api/messages",
        teams: dict[str, Any] | None = None,
    ) -> None:
        self.bot_app_id = bot_app_id
        self.endpoint = endpoint
        self.teams = teams or self._teams()

    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        if argv[:3] == ["az", "account", "show"]:
            return CommandResult(
                argv,
                0,
                stdout=json.dumps({"id": "33333333-3333-3333-3333-333333333333"}),
            )
        if argv[:3] == ["az", "bot", "show"]:
            return CommandResult(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "properties": {
                            "msaAppId": self.bot_app_id,
                            "endpoint": self.endpoint,
                        }
                    }
                ),
            )
        if argv[:4] == ["az", "bot", "msteams", "show"]:
            if self.teams is None:
                return CommandResult(argv, 3, stderr="Channel not found")
            return CommandResult(argv, 0, stdout=json.dumps(self.teams))
        raise AssertionError(f"unexpected command: {argv}")

    @staticmethod
    def _teams(*, accepted: bool = True, enabled: bool = True) -> dict[str, Any]:
        return {
            "properties": {
                "properties": {
                    "acceptedTerms": accepted,
                    "isEnabled": enabled,
                }
            }
        }


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


def _write_bot_service_sidecar(tmp_path: Path) -> Path:
    sidecar = tmp_path / "a365.bot-service.config.json"
    sidecar.write_text(
        BotServiceConfig(
            schemaVersion=1,
            subscriptionId="33333333-3333-3333-3333-333333333333",
            resourceGroup="hermes-a365-bots",
            botName="hermes-inbox-helper-bot",
            armResourceId=(
                "/subscriptions/33333333-3333-3333-3333-333333333333"
                "/resourceGroups/hermes-a365-bots/providers/Microsoft.BotService"
                "/botServices/hermes-inbox-helper-bot"
            ),
            msaAppId="11111111-1111-1111-1111-111111111111",
            tenantId="22222222-2222-2222-2222-222222222222",
            messagingEndpoint="https://example.test/api/messages",
            channelsEnabled=["msteams"],
            createdAt="2026-05-18T12:30:00Z",
        ).to_json()
    )
    return sidecar


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

    def test_ok_at_agent_scope_via_slugified_display_name(self, tmp_path: Path) -> None:
        # Slice 18l (bug #12): operators tend to type the display name
        # ("Hermes Inbox Helper") not the slug. Wrapper now slugifies on
        # lookup before giving up.
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "hermes-inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=550e8400\n")
        result = gather_local_config(tmp_path, "Hermes Inbox Helper")
        assert result.state == "ok"
        assert "agent=hermes-inbox-helper" in result.detail

    def test_warn_message_lists_both_paths_when_agent_dir_missing(
        self, tmp_path: Path
    ) -> None:
        _seed_skill_env(tmp_path)
        result = gather_local_config(tmp_path, "Hermes Inbox Helper")
        assert result.state == "warn"
        # Both the literal-name and the slugified path should appear in the
        # detail so operators see exactly where we looked.
        assert "Hermes Inbox Helper" in result.detail
        assert "hermes-inbox-helper" in result.detail

    def test_traversal_agent_name_does_not_read_outside_root(self, tmp_path: Path) -> None:
        # #103/M9: a read-only probe must never be steered outside the agents
        # root. Plant a victim .env where "../../victim/.env" would land and
        # confirm status never reads/returns it — the raw candidate is skipped
        # while the benign slugified fallback ("victim") is probed harmlessly.
        _seed_skill_env(tmp_path)
        # The agents root MUST exist or the OS can't resolve the ".." probe
        # path (stat through a missing dir → ENOENT), which would make this
        # test pass even with the validate_slug guard removed. With it
        # present, the traversal genuinely resolves to the planted victim, so
        # a neutered guard here would leak and fail the test.
        (tmp_path / "agents").mkdir(exist_ok=True)
        victim_dir = tmp_path.parent / "victim"
        victim_dir.mkdir(exist_ok=True)
        (victim_dir / ".env").write_text("AA_INSTANCE_ID=leak\n")
        try:
            result = gather_local_config(tmp_path, "../../victim")
            # Does not crash; does not surface the out-of-root secret.
            assert result.state in {"warn", "ok"}
            assert "leak" not in result.detail
            assert "agent_env" not in result.data
            assert result.data.get("aa_instance_id") != "leak"
        finally:
            (victim_dir / ".env").unlink(missing_ok=True)
            victim_dir.rmdir()

    def test_pure_dotdot_agent_name_is_rejected_cleanly(self, tmp_path: Path) -> None:
        # slugify("..") == "" so no benign fallback exists; status must warn
        # about an invalid slug rather than probe "~/.hermes/agents/../.env".
        _seed_skill_env(tmp_path)
        result = gather_local_config(tmp_path, "..")
        assert result.state == "warn"
        assert "not a valid agent slug" in result.detail


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

    def test_progress_messages_skipped_when_picking_detail(self) -> None:
        """Slice 18q (bug #13): the warn-detail used to latch onto the
        CLI's progress message ("Querying Entra ID for…"), making the
        operator think the query had hung. Skip progress lines and pick
        the first real content."""
        text = (
            "Querying Entra ID for agent blueprint inheritable permissions...\n"
            "Resolving client app by display name 'Agent 365 CLI'...\n"
            "Mail.ReadWrite: pending review\n"
        )
        state, detail = _classify_scopes_output(text)
        assert state == "warn"
        assert detail == "Mail.ReadWrite: pending review"

    def test_all_progress_falls_back_to_placeholder(self) -> None:
        text = "Querying Entra ID...\nLoading scopes...\n"
        state, detail = _classify_scopes_output(text)
        assert state == "warn"
        assert detail == "unclassifiable scope output"

    def test_debug_lines_skipped(self) -> None:
        text = (
            "[DEBUG] Initializing MSAL token cache\n"
            "[INFO] Resolving tenant id...\n"
            "Mail.Send: pending review\n"
        )
        _, detail = _classify_scopes_output(text)
        assert detail == "Mail.Send: pending review"

    def test_real_ga_blueprint_scopes_output_classifies_ok(self) -> None:
        """Slice 18v regression — pinned against verified GA v1.1.171 output
        from the 2026-05-05 round-2 walkthrough. The real CLI doesn't
        print "consented" or "granted"; it prints "Successfully retrieved
        inheritable permissions from Graph API" then `Inheritable Scopes:`
        sections. Slice 18q's hint-list tightening (dropping "ok") had
        regressed the classifier into reporting these as warn."""
        text = (
            "Querying Entra ID for agent blueprint inheritable permissions...\n"
            "Loaded resource IDs from /tmp/a365.generated.config.json\n"
            "Agent Blueprint ID: 8b563a20-2fac-4210-8210-df139c61e8b7\n"
            "\n"
            "Querying Microsoft Graph API for blueprint inheritable permissions...\n"
            "Successfully retrieved inheritable permissions from Graph API\n"
            "Blueprint Inheritable Permissions:\n"
            "==================================\n"
            "Resource: Microsoft Graph (00000003-0000-0000-c000-000000000000)\n"
            "  Scope Kind: enumerated\n"
            "  Inheritable Scopes:\n"
            "      Mail.ReadWrite\n"
            "      Mail.Send\n"
            "Total resources with inheritable permissions: 1\n"
        )
        state, _ = _classify_scopes_output(text)
        assert state == "ok"


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
# gather_bot_service
# ---------------------------------------------------------------------------


class TestBotServiceStatus:
    def test_absent_sidecar_is_skipped(self, tmp_path: Path) -> None:
        result = gather_bot_service(
            sidecar_path=tmp_path / "missing.json",
            generated_config_path=tmp_path / "a365.generated.config.json",
        )

        assert result.state == "skipped"
        assert result.name == "bot_service"

    def test_clean_path_b_install_reports_ok(self, tmp_path: Path) -> None:
        sidecar = _write_bot_service_sidecar(tmp_path)

        result = gather_bot_service(
            sidecar_path=sidecar,
            generated_config_path=tmp_path / "a365.generated.config.json",
            runner=FakeBotServiceRunner(),
            operator_env={"A365_BF_APP_ID": "11111111-1111-1111-1111-111111111111"},
            runtime_auth_probe=lambda _config: DiagnosticResult(
                "bot_service_runtime_auth", "ok", "runtime ok"
            ),
        )

        assert result.state == "ok"
        assert result.name == "bot_service"
        assert "Path B probe(s) ok" in result.detail

    def test_runtime_auth_failure_reports_error(self, tmp_path: Path) -> None:
        sidecar = _write_bot_service_sidecar(tmp_path)

        result = gather_bot_service(
            sidecar_path=sidecar,
            generated_config_path=tmp_path / "a365.generated.config.json",
            runner=FakeBotServiceRunner(),
            operator_env={"A365_BF_APP_ID": "11111111-1111-1111-1111-111111111111"},
            runtime_auth_probe=lambda _config: DiagnosticResult(
                "bot_service_runtime_auth",
                "error",
                "standalone activity-bridge serve must dispatch BF-token replies",
            ),
        )

        assert result.state == "error"
        assert "activity-bridge serve" in result.detail
        assert "BF-token" in result.detail

    def test_channel_disabled_reports_warn(self, tmp_path: Path) -> None:
        sidecar = _write_bot_service_sidecar(tmp_path)

        result = gather_bot_service(
            sidecar_path=sidecar,
            generated_config_path=tmp_path / "a365.generated.config.json",
            runner=FakeBotServiceRunner(
                teams=FakeBotServiceRunner._teams(accepted=False, enabled=False)
            ),
            operator_env={"A365_BF_APP_ID": "11111111-1111-1111-1111-111111111111"},
            runtime_auth_probe=lambda _config: DiagnosticResult(
                "bot_service_runtime_auth", "ok", "runtime ok"
            ),
        )

        assert result.state == "warn"
        assert "acceptedTerms/isEnabled" in result.detail


# ---------------------------------------------------------------------------
# gather_activity_bridge (PID file probe — kept from v0.1)
# ---------------------------------------------------------------------------


class TestActivityBridge:
    def test_missing_pid_file(self, tmp_path: Path) -> None:
        result = gather_activity_bridge(tmp_path, "inbox-helper")
        assert result.state == "missing"
        assert "bridge.pid not found" in result.detail
        assert "live-tenant-test.md" in result.detail

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

    @pytest.mark.parametrize("agent_name", ["../../etc", "..", "a/b", "x\x00y"])
    def test_traversal_agent_name_refused_without_probe(
        self, agent_name: str, tmp_path: Path
    ) -> None:
        # #103/M9: read-only probe — a traversal-shaped agent_name must not be
        # joined into the pidfile path; status reports missing, no crash.
        result = gather_activity_bridge(tmp_path, agent_name)
        assert result.state == "missing"
        assert "not a valid agent slug" in result.detail


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
            "bot_service",
            "activity_bridge",
        ]
        bot_service = next(c for c in report.components if c.name == "bot_service")
        assert bot_service.state == "skipped"

    def test_agent_env_secrets_redacted_in_status_data(self, tmp_path: Path) -> None:
        # H2 (#101) — secret-valued keys must be redacted from the status
        # component data. Default (non---human) output is JSON, which lands in
        # shell history / CI logs / support tickets.
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(
            "AA_INSTANCE_ID=x\nA365_BF_CLIENT_SECRET=supersekret\nA365_TENANT_ID=tenant-1\n"
        )
        report = collect_status(
            "inbox-helper",
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        local = next(c for c in report.components if c.name == "local_config")
        env = local.data["agent_env"]
        assert env["A365_BF_CLIENT_SECRET"] == "***redacted***"
        # The secret value must not appear anywhere in the component data.
        assert "supersekret" not in str(local.data)
        # Non-secret values are preserved (key presence + value still visible).
        assert env["A365_TENANT_ID"] == "tenant-1"
        assert env["AA_INSTANCE_ID"] == "x"

    def test_agent_env_redacts_widened_secret_markers(self, tmp_path: Path) -> None:
        # H2 (#106 review) — normalized-key matching catches APIKEY (no
        # underscore), connection strings, *_PAT, and CREDENTIAL that the earlier
        # `_KEY`/`SECRET` substring markers missed.
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text(
            "AA_INSTANCE_ID=keep\nAPIKEY=a\nGRAPH_CONNECTIONSTRING=b\n"
            "GITHUB_PAT=c\nMY_CREDENTIAL=d\nA365_TENANT_ID=keep2\n"
        )
        report = collect_status(
            "inbox-helper",
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        env = next(
            c for c in report.components if c.name == "local_config"
        ).data["agent_env"]
        for k in ("APIKEY", "GRAPH_CONNECTIONSTRING", "GITHUB_PAT", "MY_CREDENTIAL"):
            assert env[k] == "***redacted***", k
        # Non-secret keys still pass through untouched.
        assert env["AA_INSTANCE_ID"] == "keep"
        assert env["A365_TENANT_ID"] == "keep2"

    def test_path_b_sidecar_row_sits_before_activity_bridge(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        agent_env = tmp_path / "agents" / "inbox-helper" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("AA_INSTANCE_ID=x\n")
        sidecar = _write_bot_service_sidecar(tmp_path)

        report = collect_status(
            "inbox-helper",
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
            bot_service_sidecar_path=sidecar,
            generated_config_path=tmp_path / "a365.generated.config.json",
            bot_service_runner=FakeBotServiceRunner(),
            bot_service_operator_env={
                "A365_BF_APP_ID": "11111111-1111-1111-1111-111111111111"
            },
            bot_service_runtime_auth_probe=lambda _config: DiagnosticResult(
                "bot_service_runtime_auth", "ok", "runtime ok"
            ),
        )

        names = [c.name for c in report.components]
        assert names == [
            "local_config",
            "blueprint_scopes",
            "instance_scopes",
            "bot_service",
            "activity_bridge",
        ]
        assert report.components[3].state == "ok"

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
