"""Mock-based tests for client secret expiry checks (#191)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from hermes_a365 import doctor
from hermes_a365.client_secret_expiry import (
    check_app_client_secret_expiry,
    gather_client_secret_expiry_checks,
    graph_get_from_az_rest,
    select_latest_active_credential_end,
    status_component_detail,
    status_component_state,
)
from hermes_a365.doctor import (
    ProbeResult,
    overall_to_exit_code,
    render_human,
    render_json,
    run_all_probes,
)
from hermes_a365.entra_state import (
    EntraStateError,
    _parse_strict_graph_json_object,
    application_client_secret_expiry_url,
    assert_client_secret_expiry_read_url,
    assert_registration_read_url,
    graph_get_from_mutator_for_secret_expiry,
    redact,
)
from hermes_a365.mutator import A365CliMutator, RunResult
from hermes_a365.status import _UnavailableQuerySource, collect_status
from hermes_a365.status import overall_to_exit_code as status_exit

_BLUEPRINT = "11111111-1111-1111-1111-111111111111"
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_LEAK_KEY = "00000000-0000-4000-8000-leakMarker1"
_LEAK_HINT = "hint-leakMarker9"
_LEAK_SECRET = "secretText-leakMarker9"
_GRAPH_MSG = "Bearer leakMarker9 and more"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _cred(*, start: datetime, end: datetime, leak: bool = False) -> dict[str, str]:
    row = {
        "startDateTime": _iso(start),
        "endDateTime": _iso(end),
    }
    if leak:
        row["keyId"] = _LEAK_KEY
        row["hint"] = _LEAK_HINT
        row["secretText"] = _LEAK_SECRET
    return row


def _graph_get(payload: dict[str, Any]):
    def getter(_url: str) -> dict[str, Any]:
        return payload

    return getter


def _assert_no_leaks(text: str) -> None:
    for marker in (_LEAK_KEY, _LEAK_HINT, _LEAK_SECRET, "leakMarker9"):
        assert marker not in text


class TestSelectLatestActive:
    def test_picks_latest_end_among_overlapping_active(self) -> None:
        windows = [
            (_NOW - timedelta(days=1), _NOW + timedelta(days=10)),
            (_NOW - timedelta(days=1), _NOW + timedelta(days=400)),
        ]
        end, past, failure, count = select_latest_active_credential_end(windows, now=_NOW)
        assert failure is None and past is None
        assert end == _NOW + timedelta(days=400)
        assert count == 2

    def test_ten_day_window_still_expiring_when_latest_is_400(self) -> None:
        payload = {
            "passwordCredentials": [
                _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=10)),
                _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=400)),
            ],
            "keyCredentials": [],
            "federatedIdentityCredentials": [],
        }
        state, _, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            warn_days=30,
        )
        assert state == "ok"
        assert data["days_remaining"] == 400


class TestCheckApp:
    def test_all_expired_reports_latest_past_end(self) -> None:
        past = _NOW - timedelta(days=3)
        payload = {
            "passwordCredentials": [
                _cred(start=past - timedelta(days=400), end=past),
            ],
        }
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
        )
        assert state == "error"
        assert "last active client secret expired" in detail
        assert data["days_remaining"] < 0

    def test_no_password_credentials_skips(self) -> None:
        payload = {
            "passwordCredentials": [],
            "keyCredentials": [{"keyId": "x"}],
        }
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
        )
        assert state == "skipped"
        assert "no client secret to monitor" in detail
        assert data["check"] == "no_password_secret"

    def test_graph_denied_doctor_warns_status_skips(self) -> None:
        payload = {
            "error": {"code": "Authorization_RequestDenied", "message": _GRAPH_MSG},
        }
        doc_state, doc_detail, _ = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            for_status=False,
        )
        stat_state, stat_detail, _ = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            for_status=True,
        )
        assert doc_state == "warn"
        assert stat_state == "skipped"
        _assert_no_leaks(doc_detail)
        _assert_no_leaks(stat_detail)

    def test_malformed_date_makes_check_unavailable(self) -> None:
        payload = {
            "passwordCredentials": [
                {
                    "startDateTime": "2026-01-01T00:00:00",
                    "endDateTime": _iso(_NOW + timedelta(days=5)),
                }
            ],
        }
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
        )
        assert state == "warn"
        assert data["check"] == "unavailable"
        _assert_no_leaks(detail)

    def test_non_dict_credential_malformed(self) -> None:
        state, _, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get({"passwordCredentials": ["bad"]}),
            now=_NOW,
        )
        assert state == "warn"
        assert data["check"] == "unavailable"

    def test_future_start_is_not_expired(self) -> None:
        payload = {
            "passwordCredentials": [
                _cred(start=_NOW + timedelta(days=5), end=_NOW + timedelta(days=400)),
            ],
        }
        doc_state, doc_detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            for_status=False,
        )
        stat_state, _, _ = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            for_status=True,
        )
        assert doc_state == "warn"
        assert stat_state == "skipped"
        assert "no active client secret yet" in doc_detail
        assert data["check"] == "no_active_yet"


class TestStrictJson:
    def test_rejects_trailing_garbage(self) -> None:
        body = '{"passwordCredentials": []} trailing'
        assert _parse_strict_graph_json_object(body) is None

    def test_rejects_array_body(self) -> None:
        assert _parse_strict_graph_json_object("[{}]") is None

    def test_mutator_adapter_rejects_non_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mut = A365CliMutator()
        monkeypatch.setattr(mut, "available", True)

        def fake_run(argv, **kwargs):
            return RunResult(
                argv=list(argv), returncode=0, stdout='{"passwordCredentials":[]} x', stderr=""
            )

        monkeypatch.setattr(mut, "run", fake_run)
        getter = graph_get_from_mutator_for_secret_expiry(mut)
        payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        assert payload is not None
        assert payload.get("error", {}).get("code") == "UnreadableGraphResponse"
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=getter,
            now=_NOW,
        )
        assert state == "warn"
        assert data["check"] == "unavailable"
        _assert_no_leaks(detail)

    def test_huge_integer_unavailable_mutator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mut = A365CliMutator()
        monkeypatch.setattr(mut, "available", True)
        huge = "1" + "0" * 10000

        def fake_run(argv, **kwargs):
            return RunResult(
                argv=list(argv),
                returncode=0,
                stdout=f'{{"passwordCredentials": [], "n": {huge}}}',
                stderr="",
            )

        monkeypatch.setattr(mut, "run", fake_run)
        getter = graph_get_from_mutator_for_secret_expiry(mut)
        payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        assert payload is not None
        assert payload.get("error", {}).get("code") == "UnreadableGraphResponse"
        _, _, data = check_app_client_secret_expiry(_BLUEPRINT, graph_get=getter, now=_NOW)
        assert data["check"] == "unavailable"

    def test_deep_nesting_unavailable_mutator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mut = A365CliMutator()
        monkeypatch.setattr(mut, "available", True)
        body = '{"a":' * 50000 + "null" + "}" * 50000

        def fake_run(argv, **kwargs):
            return RunResult(argv=list(argv), returncode=0, stdout=body, stderr="")

        monkeypatch.setattr(mut, "run", fake_run)
        getter = graph_get_from_mutator_for_secret_expiry(mut)
        payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        assert payload.get("error", {}).get("code") == "UnreadableGraphResponse"


class TestAzAdapterStrictJson:
    def test_huge_integer_unavailable_az(self, monkeypatch: pytest.MonkeyPatch) -> None:
        huge = "1" + "0" * 10000
        body = f'{{"passwordCredentials": [], "n": {huge}}}'

        def fake_run(*_a, **_k):
            return subprocess.CompletedProcess([], 0, stdout=body.encode(), stderr=b"")

        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
        monkeypatch.setattr(subprocess, "run", fake_run)
        getter = graph_get_from_az_rest()
        payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        assert payload is not None
        assert payload.get("error", {}).get("code") == "UnreadableGraphResponse"

    def test_deep_nesting_unavailable_az(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = '{"a":' * 50000 + "null" + "}" * 50000

        def fake_run(*_a, **_k):
            return subprocess.CompletedProcess([], 0, stdout=body.encode(), stderr=b"")

        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
        monkeypatch.setattr(subprocess, "run", fake_run)
        getter = graph_get_from_az_rest()
        payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        assert payload.get("error", {}).get("code") == "UnreadableGraphResponse"


class TestInvalidUtf8Capture:
    _MARKER = b"opaqueUtf8LeakMarker9\xff\xfe"

    def test_mutator_unicode_decode_error_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mut = A365CliMutator()
        monkeypatch.setattr(mut, "available", True)

        def bad_run(argv, **kwargs):
            raise UnicodeDecodeError("utf-8", self._MARKER, 0, 1, "invalid")

        monkeypatch.setattr(mut, "run", bad_run)
        getter = graph_get_from_mutator_for_secret_expiry(mut)
        try:
            payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        except UnicodeDecodeError as exc:
            pytest.fail(f"decode error escaped adapter: {exc!r}")
        assert payload is not None
        blob = json.dumps(payload)
        assert self._MARKER.decode("latin-1") not in blob
        assert "opaqueUtf8LeakMarker9" not in blob
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=getter,
            now=_NOW,
        )
        assert state == "warn"
        assert data["check"] == "unavailable"
        _assert_no_leaks(detail)

    def test_az_subprocess_invalid_utf8_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        marker = self._MARKER

        def fake_run(*_a, **_k):
            return subprocess.CompletedProcess([], 0, stdout=marker, stderr=b"")

        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
        monkeypatch.setattr(subprocess, "run", fake_run)
        getter = graph_get_from_az_rest()
        payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        assert payload is not None
        assert payload.get("error", {}).get("code") == "UnreadableGraphResponse"
        blob = json.dumps(payload)
        assert "opaqueUtf8LeakMarker9" not in blob
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=getter,
            now=_NOW,
        )
        assert state == "warn"
        assert data["check"] == "unavailable"
        _assert_no_leaks(detail)


class TestAgentEnvLookup:
    _PARENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _AGENT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def test_agent_one_uses_agent_env_not_gateway(self, tmp_path: Path) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        agents = home / "agents" / "Agent.One"
        agents.mkdir(parents=True)
        (home / ".env").write_text(f"A365_APP_ID={self._PARENT}\n")
        (agents / ".env").write_text(f"A365_APP_ID={self._AGENT}\n")
        seen: list[str] = []

        def router(url: str) -> dict[str, Any]:
            seen.append(url)
            if self._AGENT in url:
                return {
                    "passwordCredentials": [
                        _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=5)),
                    ],
                }
            if self._PARENT in url:
                return {
                    "passwordCredentials": [
                        _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=400)),
                    ],
                }
            return {"passwordCredentials": []}

        rows = gather_client_secret_expiry_checks(
            hermes_home=home,
            agent_name="Agent.One",
            sidecar_path=tmp_path / "missing-sidecar.json",
            generated_config_path=tmp_path / "missing.json",
            graph_get=router,
            now=lambda: _NOW,
            warn_days=30,
        )
        bp = next(r for r in rows if r[0] == "blueprint_client_secret")
        assert self._AGENT in "".join(seen)
        assert bp[1] == "warn"
        assert "day(s) remaining" in bp[2]


class TestMutatorTimeout:
    _MARKER = "timeoutPartialLeakMarker9"

    def test_timeout_returns_sanitized_without_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mut = A365CliMutator()
        monkeypatch.setattr(mut, "available", True)

        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1, output=self._MARKER)

        monkeypatch.setattr(mut, "run", boom)
        getter = graph_get_from_mutator_for_secret_expiry(mut)
        try:
            payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        except subprocess.TimeoutExpired as exc:
            pytest.fail(f"timeout leaked to caller: {exc!r}")
        assert payload is not None
        assert self._MARKER not in json.dumps(payload)
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=getter,
            now=_NOW,
        )
        assert state == "warn"
        assert data["check"] == "unavailable"
        _assert_no_leaks(detail)


class TestBfConfiguredWithoutSidecar:
    def test_bf_env_without_sidecar_runs_check(self, tmp_path: Path) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        bf = "22222222-2222-2222-2222-222222222222"
        (home / ".env").write_text(f"A365_APP_ID={_BLUEPRINT}\nA365_BF_APP_ID={bf}\n")
        sidecar = tmp_path / "absent-sidecar.json"

        def router(url: str) -> dict[str, Any]:
            if bf in url:
                return {
                    "passwordCredentials": [
                        _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=400)),
                    ],
                }
            return {"passwordCredentials": []}

        rows = gather_client_secret_expiry_checks(
            hermes_home=home,
            agent_name=None,
            sidecar_path=sidecar,
            generated_config_path=tmp_path / "missing.json",
            graph_get=router,
            now=lambda: _NOW,
        )
        by_name = {name: state for name, state, _, _ in rows}
        assert by_name["bf_client_secret"] == "ok"


class TestStatusRollup:
    def test_healthy_plus_denied_skipped_with_both_details(self, tmp_path: Path) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        bf = "22222222-2222-2222-2222-222222222222"
        (home / ".env").write_text(
            f"A365_APP_ID={_BLUEPRINT}\nA365_BF_APP_ID={bf}\n",
        )

        def router(url: str) -> dict[str, Any]:
            if _BLUEPRINT in url:
                return {
                    "passwordCredentials": [
                        _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=400)),
                    ],
                }
            return {"error": {"code": "Authorization_RequestDenied", "message": _GRAPH_MSG}}

        rows = gather_client_secret_expiry_checks(
            hermes_home=home,
            agent_name=None,
            sidecar_path=tmp_path / "missing-sidecar.json",
            generated_config_path=tmp_path / "missing.json",
            graph_get=router,
            now=lambda: _NOW,
            for_status=True,
        )
        assert status_component_state(rows) == "skipped"
        detail = status_component_detail(rows)
        assert "could not check Bot Framework" in detail
        assert "older active secrets may expire sooner" in detail
        _assert_no_leaks(detail)

    def test_no_active_yet_does_not_roll_up_ok(self, tmp_path: Path) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        (home / ".env").write_text(
            f"A365_TENANT_ID=00000000-0000-0000-0000-000000000001\nA365_APP_ID={_BLUEPRINT}\n"
        )
        payload = {
            "passwordCredentials": [
                _cred(start=_NOW - timedelta(days=400), end=_NOW - timedelta(days=1)),
                _cred(start=_NOW + timedelta(days=2), end=_NOW + timedelta(days=400)),
            ],
        }

        rows = gather_client_secret_expiry_checks(
            hermes_home=home,
            agent_name=None,
            sidecar_path=tmp_path / "missing-sidecar.json",
            generated_config_path=tmp_path / "missing.json",
            graph_get=_graph_get(payload),
            now=lambda: _NOW,
            for_status=True,
        )
        bp = next(r for r in rows if r[0] == "blueprint_client_secret")
        assert bp[3].get("check") == "no_active_yet"
        assert status_component_state(rows) == "skipped"
        report = collect_status(
            None,
            hermes_home=home,
            query_source=_UnavailableQuerySource(),
            graph_get=_graph_get(payload),
            secret_expiry_now=lambda: _NOW,
            bot_service_sidecar_path=tmp_path / "missing-sidecar.json",
        )
        comp = next(c for c in report.components if c.name == "client_secret_expiry")
        assert comp.state == "skipped"
        assert status_exit(report.overall) == 0


class TestEntraUrl:
    def test_expiry_url_allowlisted(self) -> None:
        url = application_client_secret_expiry_url(_BLUEPRINT)
        assert_client_secret_expiry_read_url(url)
        assert UUID(url.split("appId='")[1].split("'")[0])

    def test_rejects_extra_query_keys(self) -> None:
        url = application_client_secret_expiry_url(_BLUEPRINT) + "&$filter=x"
        with pytest.raises(EntraStateError):
            assert_client_secret_expiry_read_url(url)

    def test_registration_assert_rejects_expiry_select(self) -> None:
        with pytest.raises(EntraStateError):
            assert_registration_read_url(application_client_secret_expiry_url(_BLUEPRINT))

    def test_redact_strips_credential_metadata(self) -> None:
        raw = json.dumps(
            {
                "keyId": _LEAK_KEY,
                "hint": _LEAK_HINT,
                "secretText": _LEAK_SECRET,
            }
        )
        cleaned = redact(raw)
        _assert_no_leaks(cleaned)


def _seed_home(tmp_path: Path, *, app_id: str = _BLUEPRINT) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(f"A365_APP_ID={app_id}\n")
    return home


def _minimal_doctor_patches():
    return patch.multiple(
        "hermes_a365.doctor",
        probe_python_runtime=lambda: ProbeResult("python", "ok", ""),
        probe_host_platform=lambda: ProbeResult("host_platform", "ok", ""),
        probe_a365_cli=lambda: ProbeResult("a365_cli", "ok", ""),
        probe_dotnet_runtime=lambda: ProbeResult("dotnet_runtime", "ok", ""),
        probe_az_cli=lambda: ProbeResult("az_cli", "ok", ""),
        probe_powershell=lambda: ProbeResult("powershell", "ok", ""),
        probe_custom_client_app=lambda: ProbeResult("custom_client_app", "ok", ""),
        probe_keychain=lambda: ProbeResult("keychain", "ok", ""),
        probe_local_config=lambda: ProbeResult("local_config", "ok", ""),
        probe_hermes_harness=lambda: ProbeResult("hermes_harness", "ok", ""),
        probe_agent_instance_allowlist=lambda **_kwargs: ProbeResult(
            "agent_instance_allowlist", "ok", ""
        ),
        probe_file_transfer=lambda: ProbeResult("file_transfer", "ok", ""),
    )


class TestDoctorStatusIntegration:
    @pytest.fixture(autouse=True)
    def _block_live_graph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hermes_a365.client_secret_expiry.build_graph_get",
            lambda *_a, **_k: None,
        )

    def test_doctor_ok_healthy_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _seed_home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        end = _NOW + timedelta(days=400)
        payload = {
            "passwordCredentials": [
                _cred(start=_NOW - timedelta(days=1), end=end, leak=True),
            ],
        }

        with _minimal_doctor_patches():
            report = run_all_probes(
                no_network=False,
                graph_get=_graph_get(payload),
                secret_expiry_now=lambda: _NOW,
                bot_service_sidecar_path=tmp_path / "missing.json",
            )
        probe = next(p for p in report.probes if p.name == "blueprint_client_secret")
        assert probe.state == "ok"
        assert overall_to_exit_code(report.overall) == 0
        human = render_human(report)
        blob = render_json(report)
        _assert_no_leaks(human)
        _assert_no_leaks(blob)

    def test_doctor_warn_in_window(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _seed_home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        payload = {
            "passwordCredentials": [
                _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=10)),
            ],
        }
        with _minimal_doctor_patches():
            report = run_all_probes(
                no_network=False,
                graph_get=_graph_get(payload),
                secret_expiry_now=lambda: _NOW,
                bot_service_sidecar_path=tmp_path / "missing.json",
            )
        probe = next(p for p in report.probes if p.name == "blueprint_client_secret")
        assert probe.state == "warn"
        assert overall_to_exit_code(report.overall) == 1

    def test_status_could_not_check_keeps_exit_ok(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            f"A365_TENANT_ID=00000000-0000-0000-0000-000000000001\nA365_APP_ID={_BLUEPRINT}\n"
        )
        report = collect_status(
            None,
            hermes_home=tmp_path,
            query_source=_UnavailableQuerySource(),
            graph_get=lambda _url: None,
            secret_expiry_now=lambda: _NOW,
            bot_service_sidecar_path=tmp_path / "missing-sidecar.json",
        )
        comp = next(c for c in report.components if c.name == "client_secret_expiry")
        assert comp.state == "skipped"
        assert "could not check blueprint" in comp.detail
        assert status_exit(report.overall) == 0

    def test_no_network_skips_expiry_without_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _seed_home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        with _minimal_doctor_patches(), patch("subprocess.run") as proc_run:
            report = run_all_probes(no_network=True, bot_service_sidecar_path=tmp_path / "x.json")
        proc_run.assert_not_called()
        bp = next(p for p in report.probes if p.name == "blueprint_client_secret")
        assert bp.state == "skipped"
        assert "--no-network" in bp.detail

    def test_negative_warn_days_argparse_exit_2(self) -> None:
        with pytest.raises(SystemExit) as exc:
            doctor.build_parser().parse_args(["--secret-expiry-warn-days", "-1"])
        assert exc.value.code == 2


# Mutation table (revert guard locally → named test fails):
# no_network skip_reason → test_no_network_skips_expiry_without_subprocess
# status unavailable rollup → test_healthy_plus_denied_skipped_with_both_details
# future-start handling → test_future_start_is_not_expired
# mutator TimeoutExpired → test_timeout_returns_sanitized_without_marker
# strict json.loads → test_rejects_trailing_garbage / test_rejects_array_body
# bf env configured → test_bf_env_without_sidecar_runs_check
# max(end) selection → test_picks_latest_end_among_overlapping_active
