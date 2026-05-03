"""Tests for scripts/status.py.

All tests use either a tmp_path-based ``HERMES_HOME`` for local components
or a ``FakeQuerySource`` for cloud components. The real macOS keychain
and the real ``a365`` CLI are never touched.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import status as status_mod
from status import (
    QuerySource,
    StatusComponent,
    StatusReport,
    collect_status,
    gather_activity_bridge,
    gather_blueprint,
    gather_channels,
    gather_fic,
    gather_instance,
    gather_license,
    gather_local_config,
    gather_t1_app,
    gather_t2_app,
    gather_telemetry,
    overall_to_exit_code,
    render_human,
    render_json,
)

# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@dataclass
class FakeQuerySource:
    """In-memory QuerySource used by every cloud-driven test."""

    available: bool = True
    license_payload: dict[str, Any] | None = None
    apps: dict[str, dict[str, Any]] = field(default_factory=dict)
    apps_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    consents: dict[str, dict[str, Any]] = field(default_factory=dict)
    blueprints: dict[str, dict[str, Any]] = field(default_factory=dict)
    instances: dict[str, dict[str, Any]] = field(default_factory=dict)
    telemetry: dict[str, dict[str, Any]] = field(default_factory=dict)
    fics: dict[str, dict[str, Any]] = field(default_factory=dict)

    def query_license(self) -> dict[str, Any] | None:
        return self.license_payload

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return self.apps.get(app_id)

    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None:
        return self.apps_by_name.get(name)

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        return self.consents.get(app_id)

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return self.blueprints.get(slug)

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return self.instances.get(instance_id)

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return self.telemetry.get(instance_id)

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return self.fics.get(app_id)


# Static check: FakeQuerySource satisfies the QuerySource Protocol.
_: QuerySource = FakeQuerySource()


# ---------------------------------------------------------------------------
# Aggregation + exit codes
# ---------------------------------------------------------------------------


def _component(name: str, state: str, detail: str = "") -> StatusComponent:
    return StatusComponent(name=name, state=state, detail=detail)  # type: ignore[arg-type]


class TestStatusReportOverall:
    def test_uninitialized_when_local_config_missing(self) -> None:
        rep = StatusReport(
            agent_slug=None,
            components=[_component("local_config", "missing")],
        )
        assert rep.overall == "uninitialized"

    def test_ok_when_all_components_ok(self) -> None:
        rep = StatusReport(
            agent_slug="x",
            components=[
                _component("local_config", "ok"),
                _component("license", "ok"),
            ],
        )
        assert rep.overall == "ok"

    def test_skipped_components_dont_break_ok(self) -> None:
        rep = StatusReport(
            agent_slug="x",
            components=[
                _component("local_config", "ok"),
                _component("license", "skipped"),
                _component("blueprint", "skipped"),
            ],
        )
        assert rep.overall == "ok"

    def test_partial_when_warn(self) -> None:
        rep = StatusReport(
            agent_slug="x",
            components=[_component("local_config", "ok"), _component("fic", "warn")],
        )
        assert rep.overall == "partial"

    def test_partial_when_missing(self) -> None:
        rep = StatusReport(
            agent_slug="x",
            components=[
                _component("local_config", "ok"),
                _component("blueprint", "missing"),
            ],
        )
        assert rep.overall == "partial"

    def test_broken_when_error_dominates(self) -> None:
        rep = StatusReport(
            agent_slug="x",
            components=[
                _component("local_config", "ok"),
                _component("activity_bridge", "error"),
                _component("fic", "warn"),
            ],
        )
        assert rep.overall == "broken"


class TestExitCodeMapping:
    def test_mapping(self) -> None:
        assert overall_to_exit_code("ok") == 0
        assert overall_to_exit_code("partial") == 1
        assert overall_to_exit_code("broken") == 2
        assert overall_to_exit_code("uninitialized") == 3


# ---------------------------------------------------------------------------
# Local-only gatherers
# ---------------------------------------------------------------------------


class TestGatherLocalConfig:
    def test_missing_when_env_absent(self, tmp_path: Path) -> None:
        result = gather_local_config(tmp_path, agent_slug=None)
        assert result.state == "missing"
        assert "register" in result.detail

    def test_warn_when_keys_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("A365_TENANT_ID=contoso\n")
        result = gather_local_config(tmp_path, agent_slug=None)
        assert result.state == "warn"
        assert "A365_APP_ID" in result.detail

    def test_ok_skill_wide(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            "A365_TENANT_ID=contoso.onmicrosoft.com\nA365_APP_ID=appabc\n"
        )
        result = gather_local_config(tmp_path, agent_slug=None)
        assert result.state == "ok"
        assert result.data["tenant_id"] == "contoso.onmicrosoft.com"
        assert result.data["app_id"] == "appabc"

    def test_warn_when_agent_env_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("A365_TENANT_ID=contoso\nA365_APP_ID=appabc\n")
        result = gather_local_config(tmp_path, agent_slug="missing-agent")
        assert result.state == "warn"
        assert "agent .env missing" in result.detail

    def test_ok_with_agent_env(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("A365_TENANT_ID=contoso\nA365_APP_ID=appabc\n")
        agent_dir = tmp_path / "agents" / "inbox-helper"
        agent_dir.mkdir(parents=True)
        (agent_dir / ".env").write_text(
            "AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\nAGENT_IDENTITY=inbox-helper\n"
        )
        result = gather_local_config(tmp_path, agent_slug="inbox-helper")
        assert result.state == "ok"
        assert result.data["aa_instance_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert "inbox-helper" in result.detail


class TestGatherActivityBridge:
    def test_missing_when_no_pidfile(self, tmp_path: Path) -> None:
        result = gather_activity_bridge(tmp_path, "agent")
        assert result.state == "missing"
        assert "bridge.pid not found" in result.detail

    def test_error_on_garbage_pidfile(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agents" / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "bridge.pid").write_text("not-a-pid\n")
        result = gather_activity_bridge(tmp_path, "agent")
        assert result.state == "error"

    def test_ok_when_pid_alive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "agents" / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "bridge.pid").write_text("12345\n")
        monkeypatch.setattr(status_mod, "_process_alive", lambda _pid: True)
        result = gather_activity_bridge(tmp_path, "agent")
        assert result.state == "ok"
        assert result.data["pid"] == 12345
        assert result.data["alive"] is True

    def test_error_on_stale_pidfile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "agents" / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "bridge.pid").write_text("99999\n")
        monkeypatch.setattr(status_mod, "_process_alive", lambda _pid: False)
        result = gather_activity_bridge(tmp_path, "agent")
        assert result.state == "error"
        assert "stale pidfile" in result.detail

    def test_process_alive_uses_signal_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Smoke: _process_alive uses os.kill(pid, 0) under the hood."""
        seen = []

        def fake_kill(pid: int, sig: int) -> None:
            seen.append((pid, sig))

        monkeypatch.setattr(os, "kill", fake_kill)
        assert status_mod._process_alive(12345) is True
        assert seen == [(12345, 0)]


# ---------------------------------------------------------------------------
# Cloud-driven gatherers (FakeQuerySource)
# ---------------------------------------------------------------------------


class TestGatherLicense:
    def test_skipped_when_unavailable(self) -> None:
        qs = FakeQuerySource(available=False)
        assert gather_license(qs).state == "skipped"

    def test_missing_when_no_license(self) -> None:
        qs = FakeQuerySource(license_payload=None)
        result = gather_license(qs)
        assert result.state == "missing"

    def test_ok_with_seats(self) -> None:
        qs = FakeQuerySource(
            license_payload={"model": "per_agent", "seats_used": 12, "seats_total": 25}
        )
        result = gather_license(qs)
        assert result.state == "ok"
        assert "12 of 25" in result.detail
        assert "per_agent" in result.detail


class TestGatherT1App:
    def test_skipped_when_unavailable(self) -> None:
        qs = FakeQuerySource(available=False)
        assert gather_t1_app(qs, app_id="x").state == "skipped"

    def test_missing_no_app_id(self) -> None:
        qs = FakeQuerySource()
        assert gather_t1_app(qs, app_id=None).state == "missing"

    def test_missing_when_app_not_in_tenant(self) -> None:
        qs = FakeQuerySource()
        result = gather_t1_app(qs, app_id="00000000-aaaa-bbbb-cccc-dddddddddddd")
        assert result.state == "missing"

    def test_ok(self) -> None:
        app_id = "00000000-aaaa-bbbb-cccc-dddddddddddd"
        qs = FakeQuerySource(apps={app_id: {"appId": app_id, "displayName": "X"}})
        result = gather_t1_app(qs, app_id=app_id)
        assert result.state == "ok"
        assert app_id[:8] in result.detail


class TestGatherT2App:
    def test_warn_when_consent_missing(self) -> None:
        app_id = "AAA"
        qs = FakeQuerySource(
            apps={app_id: {"appId": app_id}},
            consents={app_id: {"granted": False}},
        )
        result = gather_t2_app(qs, app_id=app_id)
        assert result.state == "warn"
        assert "consent=missing" in result.detail

    def test_ok_with_granted_consent(self) -> None:
        app_id = "AAA"
        qs = FakeQuerySource(
            apps={app_id: {"appId": app_id}},
            consents={app_id: {"granted": True, "granted_date": "2026-04-30"}},
        )
        result = gather_t2_app(qs, app_id=app_id)
        assert result.state == "ok"
        assert "granted" in result.detail
        assert "2026-04-30" in result.detail


class TestGatherBlueprint:
    def test_missing(self) -> None:
        qs = FakeQuerySource()
        result = gather_blueprint(qs, slug="inbox-helper")
        assert result.state == "missing"

    def test_ok_with_last_patched(self) -> None:
        qs = FakeQuerySource(blueprints={"inbox-helper": {"last_patched": "2026-05-02"}})
        result = gather_blueprint(qs, slug="inbox-helper")
        assert result.state == "ok"
        assert "2026-05-02" in result.detail


class TestGatherInstance:
    def test_missing_no_id(self) -> None:
        qs = FakeQuerySource()
        result = gather_instance(qs, instance_id=None)
        assert result.state == "missing"

    def test_ok(self) -> None:
        iid = "550e8400-e29b-41d4-a716-446655440000"
        qs = FakeQuerySource(instances={iid: {"id": iid}})
        result = gather_instance(qs, instance_id=iid)
        assert result.state == "ok"


class TestGatherChannels:
    def test_missing_no_channels(self) -> None:
        iid = "iid"
        qs = FakeQuerySource(instances={iid: {"channels": {}}})
        result = gather_channels(qs, instance_id=iid)
        assert result.state == "missing"

    def test_ok_all_channels_ok(self) -> None:
        iid = "iid"
        qs = FakeQuerySource(instances={iid: {"channels": {"teams": "ok", "outlook": "ok"}}})
        result = gather_channels(qs, instance_id=iid)
        assert result.state == "ok"
        assert "teams=ok" in result.detail
        assert "outlook=ok" in result.detail

    def test_warn_when_any_channel_not_ok(self) -> None:
        iid = "iid"
        qs = FakeQuerySource(
            instances={iid: {"channels": {"teams": "ok", "m365copilot": "missing"}}}
        )
        result = gather_channels(qs, instance_id=iid)
        assert result.state == "warn"


class TestGatherTelemetry:
    def test_missing(self) -> None:
        qs = FakeQuerySource()
        result = gather_telemetry(qs, instance_id="iid")
        assert result.state == "missing"

    def test_warn_no_spans(self) -> None:
        qs = FakeQuerySource(
            telemetry={"iid": {"sampler": "parent_based"}}  # no last_span
        )
        result = gather_telemetry(qs, instance_id="iid")
        assert result.state == "warn"

    def test_ok_with_span(self) -> None:
        qs = FakeQuerySource(
            telemetry={
                "iid": {
                    "last_span": "2026-05-03T14:22:00Z",
                    "sampler": "parent_based",
                }
            }
        )
        result = gather_telemetry(qs, instance_id="iid")
        assert result.state == "ok"
        assert "2026-05-03T14:22:00Z" in result.detail
        assert "parent_based" in result.detail


class TestGatherFic:
    def test_missing(self) -> None:
        qs = FakeQuerySource()
        result = gather_fic(qs, app_id="A")
        assert result.state == "missing"

    def test_ok_when_far_from_expiry(self) -> None:
        qs = FakeQuerySource(fics={"A": {"expires": "2026-08-30", "days_until_expiry": 60}})
        result = gather_fic(qs, app_id="A")
        assert result.state == "ok"

    def test_warn_within_7_days(self) -> None:
        qs = FakeQuerySource(fics={"A": {"expires": "2026-05-08", "days_until_expiry": 5}})
        result = gather_fic(qs, app_id="A")
        assert result.state == "warn"

    def test_error_when_expired(self) -> None:
        qs = FakeQuerySource(fics={"A": {"expires": "2026-05-01", "days_until_expiry": -2}})
        result = gather_fic(qs, app_id="A")
        assert result.state == "error"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class TestCollectStatus:
    def _bootstrap(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            "A365_TENANT_ID=contoso.onmicrosoft.com\nA365_APP_ID=APPID\n"
        )

    def _bootstrap_agent(self, tmp_path: Path, slug: str) -> None:
        agent_dir = tmp_path / "agents" / slug
        agent_dir.mkdir(parents=True)
        (agent_dir / ".env").write_text(
            f"AA_INSTANCE_ID=550e8400-e29b-41d4-a716-446655440000\nAGENT_IDENTITY={slug}\n"
        )

    def test_uninitialized_when_no_env(self, tmp_path: Path) -> None:
        report = collect_status(
            agent_slug=None,
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        assert report.overall == "uninitialized"
        assert overall_to_exit_code(report.overall) == 3

    def test_skill_wide_partial_when_a365_unavailable(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path)
        report = collect_status(
            agent_slug=None,
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        # local_config ok, everything cloud is skipped → overall ok
        assert report.overall == "ok"
        skipped = [c for c in report.components if c.state == "skipped"]
        assert len(skipped) >= 3

    def test_per_agent_full_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._bootstrap(tmp_path)
        self._bootstrap_agent(tmp_path, "inbox-helper")
        # Stand up a fake bridge.pid that the test marks alive
        (tmp_path / "agents" / "inbox-helper" / "bridge.pid").write_text("12345\n")
        monkeypatch.setattr(status_mod, "_process_alive", lambda _pid: True)

        iid = "550e8400-e29b-41d4-a716-446655440000"
        qs = FakeQuerySource(
            license_payload={"model": "per_agent", "seats_used": 1, "seats_total": 25},
            apps={"APPID": {"appId": "APPID"}},
            consents={"APPID": {"granted": True, "granted_date": "2026-04-30"}},
            blueprints={"inbox-helper": {"last_patched": "2026-05-02"}},
            instances={iid: {"id": iid, "channels": {"teams": "ok"}}},
            telemetry={iid: {"last_span": "2026-05-03T14:22:00Z", "sampler": "parent_based"}},
            fics={"APPID": {"expires": "2026-08-30", "days_until_expiry": 60}},
        )
        report = collect_status(
            agent_slug="inbox-helper",
            hermes_home=tmp_path,
            query_source=qs,
        )
        assert report.overall == "ok"
        names = [c.name for c in report.components]
        assert "activity_bridge" in names
        assert "blueprint" in names
        assert "channels" in names

    def test_per_agent_broken_when_bridge_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._bootstrap(tmp_path)
        self._bootstrap_agent(tmp_path, "inbox-helper")
        (tmp_path / "agents" / "inbox-helper" / "bridge.pid").write_text("99999\n")
        monkeypatch.setattr(status_mod, "_process_alive", lambda _pid: False)
        report = collect_status(
            agent_slug="inbox-helper",
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        assert report.overall == "broken"
        assert overall_to_exit_code(report.overall) == 2


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def _report(self) -> StatusReport:
        return StatusReport(
            agent_slug="inbox-helper",
            components=[
                _component("local_config", "ok", "tenant=contoso | app_id=APPID…"),
                _component("license", "ok", "per_agent, 1 of 25 seats used"),
                _component("activity_bridge", "ok", "pid=12345 (alive)"),
            ],
        )

    def test_human_includes_header_and_overall(self) -> None:
        text = render_human(self._report())
        assert "hermes a365 status — inbox-helper" in text
        assert "Component" in text and "State" in text and "Detail" in text
        assert "overall: ok" in text

    def test_human_includes_skipped_note(self) -> None:
        rep = StatusReport(
            agent_slug=None,
            components=[
                _component("local_config", "ok"),
                _component("license", "skipped", "a365 CLI unavailable"),
            ],
        )
        text = render_human(rep)
        assert "skipped" in text
        assert "a365 CLI unavailable" in text

    def test_json_round_trip(self) -> None:
        text = render_json(self._report())
        payload = json.loads(text)
        assert payload["agent_slug"] == "inbox-helper"
        assert payload["overall"] == "ok"
        assert len(payload["components"]) == 3
