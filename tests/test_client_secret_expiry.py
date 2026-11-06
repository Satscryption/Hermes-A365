"""Mock-based tests for client secret expiry checks (#191)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from hermes_a365 import client_secret_expiry, doctor
from hermes_a365.client_secret_expiry import (
    check_app_client_secret_expiry,
    classify_client_secret_expiry,
    gather_client_secret_expiry_checks,
    graph_get_from_az_rest,
    resolve_bf_app_id,
    select_latest_active_credential_end,
    status_component_detail,
    status_component_state,
)
from hermes_a365.doctor import (
    ProbeResult,
    overall_to_exit_code,
    probe_local_config,
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
from hermes_a365.mutator import A365CliMutator, CliInvocationError, RunResult
from hermes_a365.status import _UnavailableQuerySource, collect_status
from hermes_a365.status import overall_to_exit_code as status_exit

_TENANT = "00000000-0000-0000-0000-000000000001"
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


@pytest.fixture
def pinned_tenant(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(client_secret_expiry, "active_az_tenant", lambda _run: _TENANT)
    return _TENANT


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
    @pytest.mark.parametrize("elapsed", [timedelta(days=3), timedelta(hours=1), timedelta(0)])
    @pytest.mark.parametrize("for_status", [False, True])
    def test_all_expired_reports_latest_past_end(
        self, elapsed: timedelta, for_status: bool
    ) -> None:
        past = _NOW - elapsed
        payload = {
            "passwordCredentials": [
                _cred(start=past - timedelta(days=400), end=past - timedelta(days=1)),
                _cred(start=past - timedelta(days=400), end=past),
            ],
        }
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            for_status=for_status,
        )
        assert state == "error"
        assert detail == f"last active client secret expired on {_iso(past)}"
        assert data["check"] == "expired"
        assert data["end_date_time"] == _iso(past)
        assert data["days_remaining"] == (-elapsed).days

    @pytest.mark.parametrize("elapsed", [timedelta(days=3), timedelta(hours=1), timedelta(0)])
    def test_classify_expired_detail(self, elapsed: timedelta) -> None:
        end = _NOW - elapsed
        state, detail, data = classify_client_secret_expiry(end=end, now=_NOW, warn_days=30)
        assert state == "error"
        assert detail == f"last active client secret expired on {_iso(end)}"
        assert data["check"] == "expired"
        assert data["days_remaining"] == (-elapsed).days

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


class TestAdapterArgv:
    def test_mutator_requests_json(self) -> None:
        mut = A365CliMutator()
        url = application_client_secret_expiry_url(_BLUEPRINT)
        argv = [
            "az",
            "rest",
            "--method",
            "GET",
            "--uri",
            url,
            "--output",
            "json",
            "--only-show-errors",
        ]
        result = RunResult(argv=argv, returncode=0, stdout='{"passwordCredentials":[]}', stderr="")
        with patch.object(mut, "run", return_value=result) as run:
            getter = graph_get_from_mutator_for_secret_expiry(mut)
            assert getter(url) == {"passwordCredentials": []}
        run.assert_called_once_with(argv, sensitive=True, timeout=15.0)

    def test_fallback_requests_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
        url = application_client_secret_expiry_url(_BLUEPRINT)
        argv = [
            "az",
            "rest",
            "--method",
            "GET",
            "--url",
            url,
            "--headers",
            "ConsistencyLevel=eventual",
            "--output",
            "json",
            "--only-show-errors",
        ]
        result = subprocess.CompletedProcess(
            argv, 0, stdout=b'{"passwordCredentials":[]}', stderr=b""
        )
        with patch.object(subprocess, "run", return_value=result) as run:
            assert graph_get_from_az_rest()(url) == {"passwordCredentials": []}
        run.assert_called_once_with(
            argv, capture_output=True, text=False, timeout=15.0, check=False
        )

    @pytest.mark.parametrize("adapter", ["mutator", "subprocess"])
    def test_warning_stream_matches_adapter_capture(
        self, adapter: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "passwordCredentials": [
                _cred(start=_NOW - timedelta(days=100), end=_NOW - timedelta(days=1)),
            ],
        }
        body = json.dumps(payload)
        warning = "WARNING: upgrade available\n"
        run = Mock()
        if adapter == "mutator":
            run.return_value = (0, warning + body)
            mut = A365CliMutator()
            monkeypatch.setattr(mut, "available", True)
            monkeypatch.setattr("hermes_a365.mutator._run_sensitive_streaming", run)
            getter = graph_get_from_mutator_for_secret_expiry(mut)
        else:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=body.encode(), stderr=warning.encode()
            )
            monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
            monkeypatch.setattr(subprocess, "run", run)
            getter = graph_get_from_az_rest()

        result = getter(application_client_secret_expiry_url(_BLUEPRINT))
        if adapter == "mutator":
            assert result["error"]["code"] == "UnreadableGraphResponse"
        else:
            assert result == payload
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT, graph_get=getter, now=_NOW, for_status=True
        )
        assert state == ("skipped" if adapter == "mutator" else "error")
        assert data["check"] == ("unavailable" if adapter == "mutator" else "expired")
        assert ("could not check" if adapter == "mutator" else "expired") in detail
        assert all("--only-show-errors" in call.args[0] for call in run.call_args_list)


class TestTenantSelection:
    _TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    @pytest.mark.parametrize("adapter", ["mutator", "subprocess"])
    @pytest.mark.parametrize(
        ("selected", "account_body", "account_rc", "expected"),
        [
            (_TENANT, {"tenantId": _OTHER}, 0, "unavailable"),
            (_TENANT, {}, 0, "unavailable"),
            (_TENANT, {"tenantId": _TENANT}, 1, "unavailable"),
            (_TENANT, {"tenantId": _TENANT.upper()}, 0, "expired"),
            (None, {}, 1, "skipped"),
            ("   ", {}, 1, "skipped"),
        ],
    )
    def test_validates_session_before_graph_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        adapter: str,
        selected: str | None,
        account_body: dict[str, str],
        account_rc: int,
        expected: str,
    ) -> None:
        home = _seed_home(tmp_path)
        calls: list[list[str]] = []
        payload = {
            "passwordCredentials": [
                _cred(start=_NOW - timedelta(days=100), end=_NOW - timedelta(days=1)),
            ],
        }

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ["az", "account", "show"]:
                assert argv == ["az", "account", "show", "-o", "json", "--only-show-errors"]
                rc, body = account_rc, account_body
                if adapter == "subprocess" and rc:
                    raise subprocess.CalledProcessError(rc, argv)
                if adapter == "mutator" and rc:
                    raise CliInvocationError(argv, rc, "not logged in")
            else:
                assert argv[:2] == ["az", "rest"]
                assert expected == "expired", "Graph must not read an unverified tenant"
                rc, body = 0, payload
            if adapter == "mutator":
                assert kwargs == {"sensitive": True, "timeout": 15.0}
                return RunResult(argv, rc, json.dumps(body), "")
            text = json.dumps(body)
            return subprocess.CompletedProcess(
                argv, rc, stdout=text if kwargs["text"] else text.encode(), stderr=""
            )

        mut = A365CliMutator()
        monkeypatch.setattr(mut, "available", adapter == "mutator")
        monkeypatch.setattr(mut, "run", run)
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
        rows = gather_client_secret_expiry_checks(
            hermes_home=home,
            agent_name=None,
            sidecar_path=tmp_path / "missing-sidecar.json",
            generated_config_path=tmp_path / "missing.json",
            tenant_id=selected,
            mutator=mut,
            now=lambda: _NOW,
            for_status=True,
        )
        bp = next(row for row in rows if row[0] == "blueprint_client_secret")
        assert bp[3]["check"] == expected
        assert bp[1] == ("error" if expected == "expired" else "skipped")
        assert len(calls) == (int(bool(selected and selected.strip())) + int(expected == "expired"))

    @pytest.mark.parametrize("explicit", [True, False])
    @pytest.mark.parametrize("active", [_OTHER, None])
    def test_status_forwards_resolved_tenant_and_does_not_report_missing_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool, active: str | None
    ) -> None:
        home = _seed_home(tmp_path)
        (home / ".env").write_text(
            f"A365_TENANT_ID={self._OTHER if explicit else self._TENANT}\n"
            f"A365_APP_ID={_BLUEPRINT}\n"
        )
        mut = A365CliMutator()
        monkeypatch.setattr(mut, "available", True)
        run = Mock(return_value=RunResult([], 0, json.dumps({"tenantId": active}), ""))
        monkeypatch.setattr(mut, "run", run)
        monkeypatch.setattr(client_secret_expiry, "get_mutator", lambda: mut)
        graph_get = Mock(return_value={"error": {"code": "Request_ResourceNotFound"}})
        with patch(
            "hermes_a365.status.gather_client_secret_expiry_checks",
            wraps=gather_client_secret_expiry_checks,
        ) as gather:
            report = collect_status(
                None,
                hermes_home=home,
                tenant_id=self._TENANT if explicit else None,
                query_source=_UnavailableQuerySource(),
                graph_get=graph_get,
                bot_service_sidecar_path=tmp_path / "missing.json",
            )
        assert gather.call_args.kwargs["tenant_id"] == self._TENANT
        run.assert_called_once_with(
            ["az", "account", "show", "-o", "json", "--only-show-errors"],
            sensitive=True,
            timeout=15.0,
        )
        graph_get.assert_not_called()
        component = next(c for c in report.components if c.name == "client_secret_expiry")
        assert component.state == "skipped"
        assert component.data["apps"]["blueprint_client_secret"]["check"] == "unavailable"
        assert "missing_app" not in json.dumps(component.data)
        assert report.overall != "broken"

    @pytest.mark.parametrize("command", ["doctor", "status"])
    @pytest.mark.parametrize("tenant_id", [None, "", "   "])
    @pytest.mark.parametrize("graph_missing", [False, True])
    def test_unpinned_tenant_skips_without_trusting_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: str,
        tenant_id: str | None,
        graph_missing: bool,
    ) -> None:
        home = _seed_home(tmp_path)
        (home / ".env").write_text(
            f"A365_TENANT_ID={tenant_id or ''}\n"
            f"A365_APP_ID={_BLUEPRINT}\nA365_BF_APP_ID={_BLUEPRINT}\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        active = Mock(return_value=self._OTHER)
        monkeypatch.setattr(client_secret_expiry, "active_az_tenant", active)
        graph_get = Mock(
            return_value={"error": {"code": "Request_ResourceNotFound"}}
            if graph_missing
            else {
                "passwordCredentials": [
                    _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=400)),
                ],
            }
        )
        if command == "doctor":
            # Isolate expiry's rollup from the separate local-config pin requirement.
            with _minimal_doctor_patches(tenant_id=tenant_id):
                report = run_all_probes(
                    graph_get=graph_get,
                    bot_service_sidecar_path=tmp_path / "missing.json",
                )
            expiry = [
                p
                for p in report.probes
                if p.name in ("blueprint_client_secret", "bf_client_secret")
            ]
            assert len(expiry) == 2
            assert all(p.state == "skipped" and p.data["check"] == "skipped" for p in expiry)
            assert all("no tenant pinned" in p.detail for p in expiry)
            assert report.overall == "ok"
            assert "missing_app" not in render_json(report)
        else:
            status = collect_status(
                None,
                hermes_home=home,
                query_source=_UnavailableQuerySource(),
                graph_get=graph_get,
                bot_service_sidecar_path=tmp_path / "missing.json",
            )
            component = next(c for c in status.components if c.name == "client_secret_expiry")
            assert component.state == "skipped"
            assert all(app["check"] == "skipped" for app in component.data["apps"].values())
            assert "no tenant pinned" in component.detail
            assert "missing_app" not in json.dumps(component.data)
            assert status.overall != "broken"
        graph_get.assert_not_called()
        active.assert_not_called()

    @pytest.mark.parametrize(
        ("configured", "active", "payload_check", "expected"),
        [
            (True, _OTHER, "missing_app", "unavailable"),
            (True, None, "missing_app", "unavailable"),
            (True, _TENANT.upper(), "healthy", "healthy"),
            (True, _TENANT, "missing_app", "missing_app"),
            (False, None, "healthy", "skipped"),
            (False, None, "missing_app", "skipped"),
        ],
    )
    def test_doctor_forwards_configured_tenant_and_guards_expiry_reads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured: bool,
        active: str | None,
        payload_check: str,
        expected: str,
    ) -> None:
        home = _seed_home(tmp_path)
        tenant_line = f"A365_TENANT_ID= {self._TENANT} \n" if configured else ""
        (home / ".env").write_text(
            f"{tenant_line}A365_APP_ID={_BLUEPRINT}\nA365_BF_APP_ID={_BLUEPRINT}\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        mut = A365CliMutator()
        monkeypatch.setattr(mut, "available", True)
        run = Mock(return_value=RunResult([], 0, json.dumps({"tenantId": active}), ""))
        monkeypatch.setattr(mut, "run", run)
        monkeypatch.setattr(client_secret_expiry, "get_mutator", lambda: mut)
        payload = (
            {
                "passwordCredentials": [
                    _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=400)),
                ],
            }
            if payload_check == "healthy"
            else {"error": {"code": "Request_ResourceNotFound"}}
        )
        graph_get = Mock(return_value=payload)
        with (
            _minimal_doctor_patches(),
            patch("hermes_a365.doctor.probe_local_config", wraps=probe_local_config),
            patch(
                "hermes_a365.doctor.gather_client_secret_expiry_checks",
                wraps=gather_client_secret_expiry_checks,
            ) as gather,
        ):
            report = run_all_probes(
                graph_get=graph_get,
                secret_expiry_now=lambda: _NOW,
                bot_service_sidecar_path=tmp_path / "missing.json",
            )

        assert gather.call_args.kwargs["tenant_id"] == (self._TENANT if configured else None)
        assert gather.call_args.kwargs["for_status"] is False
        expiry = [
            p for p in report.probes if p.name in ("blueprint_client_secret", "bf_client_secret")
        ]
        assert len(expiry) == 2
        assert all(p.data["check"] == expected for p in expiry)
        if configured:
            run.assert_called_once_with(
                ["az", "account", "show", "-o", "json", "--only-show-errors"],
                sensitive=True,
                timeout=15.0,
            )
        else:
            run.assert_not_called()
        if expected in ("unavailable", "skipped"):
            graph_get.assert_not_called()
            state = "warn" if configured and active is None else "skipped"
            assert all(p.state == state for p in expiry)
            assert "missing_app" not in render_json(report)
            if configured:
                assert report.overall == ("warn" if active is None else "ok")
        else:
            assert graph_get.call_count == 2
            assert all(p.state == ("ok" if expected == "healthy" else "error") for p in expiry)


class TestGraphErrors:
    @pytest.fixture(
        params=["mutator", "mutator_graph_error", "az_stdout", "az_stderr", "az_stderr_prefix"]
    )
    def error_getter(self, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
        def build(code: str):
            body = json.dumps({"error": {"code": code, "message": _GRAPH_MSG}})
            if request.param == "mutator_graph_error":
                mut = A365CliMutator()

                def fake_run(argv, *, sensitive, timeout):
                    assert sensitive is True
                    raise CliInvocationError(
                        argv,
                        1,
                        "<output suppressed>",
                        sensitive=True,
                        graph_error=(code, "Graph request failed"),
                    )

                monkeypatch.setattr(mut, "run", fake_run)
                return graph_get_from_mutator_for_secret_expiry(mut)
            if request.param == "mutator":
                mut = A365CliMutator()
                monkeypatch.setattr(mut, "available", True)

                def fake_streaming(argv, *, timeout, stdin_input):
                    # Exercise A365CliMutator.run's production sensitive-error handling.
                    return 1, f"ERROR: {body}"

                monkeypatch.setattr("hermes_a365.mutator._run_sensitive_streaming", fake_streaming)
                return graph_get_from_mutator_for_secret_expiry(mut)

            stdout = body.encode() if request.param == "az_stdout" else b""
            stderr = b"" if request.param == "az_stdout" else body.encode()
            if request.param == "az_stderr_prefix":
                stderr = b"ERROR: " + stderr
            monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
            monkeypatch.setattr(
                subprocess,
                "run",
                lambda *_a, **_k: subprocess.CompletedProcess([], 1, stdout=stdout, stderr=stderr),
            )
            return graph_get_from_az_rest()

        return build

    @pytest.mark.parametrize("code", ["Request_ResourceNotFound", "Authorization_RequestDenied"])
    @pytest.mark.parametrize("for_status", [False, True])
    def test_adapters_preserve_error_classification(self, error_getter, code, for_status) -> None:
        getter = error_getter(code)
        payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        assert payload is not None
        assert payload["error"]["code"] == code
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT, graph_get=getter, now=_NOW, for_status=for_status
        )
        if code == "Request_ResourceNotFound":
            assert state == "error"
            assert data["check"] == "missing_app"
            assert "Entra application not found" in detail
        else:
            assert state == ("skipped" if for_status else "warn")
            assert data["check"] == "unavailable"
            assert "Graph access denied" in detail
        _assert_no_leaks(detail)
        _assert_no_leaks(json.dumps(data))

    def test_missing_app_makes_status_fail(
        self, tmp_path: Path, error_getter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            client_secret_expiry,
            "active_az_tenant",
            lambda _run: "00000000-0000-0000-0000-000000000001",
        )
        (tmp_path / ".env").write_text(
            f"A365_TENANT_ID=00000000-0000-0000-0000-000000000001\nA365_APP_ID={_BLUEPRINT}\n"
        )
        report = collect_status(
            None,
            hermes_home=tmp_path,
            query_source=_UnavailableQuerySource(),
            graph_get=error_getter("Request_ResourceNotFound"),
            secret_expiry_now=lambda: _NOW,
            bot_service_sidecar_path=tmp_path / "missing-sidecar.json",
        )
        comp = next(c for c in report.components if c.name == "client_secret_expiry")
        assert comp.state == "error"
        assert comp.data["apps"]["blueprint_client_secret"]["check"] == "missing_app"
        assert "blueprint Entra application not found" in comp.detail
        assert status_exit(report.overall) == 2
        _assert_no_leaks(comp.detail)
        _assert_no_leaks(json.dumps(comp.data))

    def test_missing_app_makes_doctor_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_getter
    ) -> None:
        home = _seed_home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        active = Mock(return_value=_TENANT)
        monkeypatch.setattr(client_secret_expiry, "active_az_tenant", active)
        with (
            _minimal_doctor_patches(),
            patch("hermes_a365.doctor.probe_local_config", wraps=probe_local_config),
        ):
            report = run_all_probes(
                no_network=False,
                graph_get=error_getter("Request_ResourceNotFound"),
                secret_expiry_now=lambda: _NOW,
                bot_service_sidecar_path=tmp_path / "missing-sidecar.json",
            )
        active.assert_called_once()
        probe = next(p for p in report.probes if p.name == "blueprint_client_secret")
        assert probe.state == "error"
        assert probe.data["check"] == "missing_app"
        assert "blueprint Entra application not found" in probe.detail
        assert overall_to_exit_code(report.overall) == 2
        _assert_no_leaks(probe.detail)
        _assert_no_leaks(json.dumps(probe.data))


class TestStrictJson:
    @pytest.mark.parametrize("adapter", ["mutator", "az"])
    @pytest.mark.parametrize("containers", ["objects", "arrays", "mixed"])
    @pytest.mark.parametrize("depth", [64, 65])
    def test_nesting_boundary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        adapter: str,
        containers: str,
        depth: int,
    ) -> None:
        # Include structural characters, escaped quotes and escaped backslashes
        # in a string at the deepest level; none count towards container depth.
        body = json.dumps('"' + "[{]}" * 65 + "\\")
        for level in range(depth - 1):
            if containers == "arrays" or (containers == "mixed" and level % 2):
                body = "[" + body + "]"
            else:
                body = '{"a":' + body + "}"
        body = '{"a":' + body + "}"
        expected = json.loads(body)
        if adapter == "mutator":
            mut = A365CliMutator()

            def fake_run(argv, **kwargs):
                return RunResult(argv=list(argv), returncode=0, stdout=body, stderr="")

            monkeypatch.setattr(mut, "run", fake_run)
            getter = graph_get_from_mutator_for_secret_expiry(mut)
        else:
            monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
            monkeypatch.setattr(
                subprocess,
                "run",
                lambda *_a, **_k: subprocess.CompletedProcess(
                    [], 0, stdout=body.encode(), stderr=b""
                ),
            )
            getter = graph_get_from_az_rest()

        with patch("hermes_a365.entra_state.json.loads", wraps=json.loads) as loads:
            payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
            if depth == 64:
                assert payload == expected
                loads.assert_called_once()
            else:
                loads.assert_not_called()
                assert payload["error"]["code"] == "UnreadableGraphResponse"
                assert "depth limit of 64" in payload["error"]["message"]

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
    @pytest.mark.parametrize("code", ["Request_ResourceNotFound", "Authorization_RequestDenied"])
    def test_structured_error_on_zero_exit(
        self, monkeypatch: pytest.MonkeyPatch, code: str
    ) -> None:
        body = json.dumps({"error": {"code": code, "message": _GRAPH_MSG}}).encode()
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=body, stderr=b""),
        )
        getter = graph_get_from_az_rest()
        payload = getter(application_client_secret_expiry_url(_BLUEPRINT))
        assert payload is not None
        assert payload["error"]["code"] == code

    @pytest.mark.parametrize(
        "body",
        [
            b"not JSON",
            b'{"passwordCredentials": []}',
            b'{"error": {"code": "Request_ResourceNotFound"}} trailing',
            b"invalid utf-8: \xff",
        ],
    )
    def test_nonzero_exit_without_structured_error_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/az")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], 1, stdout=body, stderr=body),
        )
        getter = graph_get_from_az_rest()
        assert getter(application_client_secret_expiry_url(_BLUEPRINT)) is None

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

    @pytest.mark.parametrize(
        ("agent_name", "agent_value", "process_value", "operator_value", "expected"),
        [
            ("Agent.One", "agent", "process", None, "agent"),
            ("Agent.One", "agent", None, None, "agent"),
            ("Agent.One", "agent", "process", "operator", "agent"),
            ("Agent.One", None, "process", None, "process"),
            ("Agent.One", None, None, None, "home"),
            ("Agent.One", "", None, None, "home"),
            ("Agent.One", "   ", None, "operator", "operator"),
            ("Agent.One", "", "process", "operator", "process"),
            (None, "agent", "process", "operator", "process"),
            (None, "agent", None, "operator", "operator"),
            (None, "agent", None, None, "home"),
        ],
    )
    def test_bf_app_id_precedence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        agent_name: str | None,
        agent_value: str | None,
        process_value: str | None,
        operator_value: str | None,
        expected: str,
    ) -> None:
        home = tmp_path / "hermes"
        agent = home / "agents" / "Agent.One"
        agent.mkdir(parents=True)
        (home / ".env").write_text("A365_BF_APP_ID=home\n")
        (agent / ".env").write_text(
            "" if agent_value is None else f'A365_BF_APP_ID="{agent_value}"\n'
        )
        monkeypatch.delenv("A365_BF_APP_ID", raising=False)
        if process_value is not None:
            monkeypatch.setenv("A365_BF_APP_ID", process_value)
        operator_env = None if operator_value is None else {"A365_BF_APP_ID": operator_value}

        assert resolve_bf_app_id(
            hermes_home=home,
            agent_name=agent_name,
            sidecar_path=tmp_path / "missing-sidecar.json",
            operator_env=operator_env,
        ) == (expected, False)

    def test_agent_one_uses_agent_env_not_gateway(self, tmp_path: Path, pinned_tenant: str) -> None:
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
            tenant_id=pinned_tenant,
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
    def test_bf_env_without_sidecar_runs_check(self, tmp_path: Path, pinned_tenant: str) -> None:
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
            tenant_id=pinned_tenant,
            graph_get=router,
            now=lambda: _NOW,
        )
        by_name = {name: state for name, state, _, _ in rows}
        assert by_name["bf_client_secret"] == "ok"


class TestStatusRollup:
    def test_healthy_plus_denied_skipped_with_both_details(
        self, tmp_path: Path, pinned_tenant: str
    ) -> None:
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
            tenant_id=pinned_tenant,
            graph_get=router,
            now=lambda: _NOW,
            for_status=True,
        )
        assert status_component_state(rows) == "skipped"
        detail = status_component_detail(rows)
        assert "could not check Bot Framework" in detail
        assert "older active secrets may expire sooner" in detail
        _assert_no_leaks(detail)

    def test_no_active_yet_does_not_roll_up_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            client_secret_expiry,
            "active_az_tenant",
            lambda _run: "00000000-0000-0000-0000-000000000001",
        )
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
            tenant_id="00000000-0000-0000-0000-000000000001",
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
        assert comp.data["apps"]["blueprint_client_secret"]["check"] == "no_active_yet"
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
    (home / ".env").write_text(f"A365_TENANT_ID={_TENANT}\nA365_APP_ID={app_id}\n")
    return home


def _minimal_doctor_patches(*, tenant_id: str | None = _TENANT):
    return patch.multiple(
        "hermes_a365.doctor",
        probe_python_runtime=lambda: ProbeResult("python", "ok", ""),
        probe_host_platform=lambda: ProbeResult("host_platform", "ok", ""),
        probe_a365_cli=lambda: ProbeResult("a365_cli", "ok", ""),
        probe_dotnet_runtime=lambda: ProbeResult("dotnet_runtime", "ok", ""),
        probe_az_cli=lambda: ProbeResult("az_cli", "ok", ""),
        probe_powershell=lambda: ProbeResult("powershell", "ok", ""),
        probe_custom_client_app=lambda: ProbeResult("custom_client_app", "ok", ""),
        probe_network=lambda: ProbeResult("network", "ok", ""),
        probe_keychain=lambda: ProbeResult("keychain", "ok", ""),
        probe_local_config=lambda: ProbeResult("local_config", "ok", "", {"tenant_id": tenant_id}),
        probe_hermes_harness=lambda: ProbeResult("hermes_harness", "ok", ""),
        probe_agent_instance_allowlist=lambda **_kwargs: ProbeResult(
            "agent_instance_allowlist", "ok", ""
        ),
        probe_file_transfer=lambda: ProbeResult("file_transfer", "ok", ""),
    )


class TestDoctorStatusIntegration:
    @pytest.fixture(autouse=True)
    def _block_live_graph(self, monkeypatch: pytest.MonkeyPatch, pinned_tenant: str) -> None:
        monkeypatch.setattr(
            "hermes_a365.client_secret_expiry.build_graph_get",
            lambda *_a, **_k: None,
        )

    @pytest.mark.parametrize("command", ["doctor", "status"])
    def test_agent_bf_secret_expiry_uses_agent_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        home = _seed_home(tmp_path)
        gateway_bf = "22222222-2222-2222-2222-222222222222"
        agent_bf = "33333333-3333-3333-3333-333333333333"
        (home / ".env").write_text(
            f"A365_TENANT_ID={_TENANT}\nA365_APP_ID={_BLUEPRINT}\nA365_BF_APP_ID={gateway_bf}\n"
        )
        agent = home / "agents" / "Agent.One"
        agent.mkdir(parents=True)
        (agent / ".env").write_text(f"A365_BF_APP_ID={agent_bf}\n")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("A365_BF_APP_ID", gateway_bf)
        seen: list[str] = []

        def router(url: str) -> dict[str, Any]:
            seen.append(url)
            days = 5 if agent_bf in url else 400
            return {
                "passwordCredentials": [
                    _cred(start=_NOW - timedelta(days=1), end=_NOW + timedelta(days=days)),
                ],
            }

        if command == "doctor":
            with _minimal_doctor_patches():
                report = run_all_probes(
                    agent_name="Agent.One",
                    graph_get=router,
                    secret_expiry_now=lambda: _NOW,
                    bot_service_sidecar_path=tmp_path / "missing.json",
                )
            bf_state = next(p.state for p in report.probes if p.name == "bf_client_secret")
        else:
            status = collect_status(
                "Agent.One",
                hermes_home=home,
                query_source=_UnavailableQuerySource(),
                graph_get=router,
                secret_expiry_now=lambda: _NOW,
                bot_service_sidecar_path=tmp_path / "missing.json",
            )
            component = next(c for c in status.components if c.name == "client_secret_expiry")
            assert component.state == "warn"
            bf_state = component.data["apps"]["bf_client_secret"]["state"]

        assert bf_state == "warn"
        assert seen == [
            application_client_secret_expiry_url(_BLUEPRINT),
            application_client_secret_expiry_url(agent_bf),
        ]

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

    def test_status_could_not_check_keeps_exit_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            client_secret_expiry,
            "active_az_tenant",
            lambda _run: "00000000-0000-0000-0000-000000000001",
        )
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
