"""Mock-based tests for client secret expiry checks (#191)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from hermes_a365 import doctor
from hermes_a365.client_secret_expiry import (
    check_app_client_secret_expiry,
    select_latest_active_credential_end,
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
    application_client_secret_expiry_url,
    assert_client_secret_expiry_read_url,
    assert_registration_read_url,
    redact,
)
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
        end, past, failure = select_latest_active_credential_end(windows, now=_NOW)
        assert failure is None and past is None
        assert end == _NOW + timedelta(days=400)

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
                no_network=True,
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
                no_network=True,
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
        assert comp.state == "ok"
        assert status_exit(report.overall) == 0

    def test_negative_warn_days_argparse_exit_2(self) -> None:
        with pytest.raises(SystemExit) as exc:
            doctor.build_parser().parse_args(["--secret-expiry-warn-days", "-1"])
        assert exc.value.code == 2


# Mutation table (revert guard locally → named test fails):
# max(end) selection → test_picks_latest_end_among_overlapping_active
# empty password list → test_no_password_credentials_skips
# for_status unavailable → test_graph_denied_doctor_warns_status_skips
# naive datetime → test_malformed_date_makes_check_unavailable
# query key allowlist → test_rejects_extra_query_keys
# status exit on unavailable → test_status_could_not_check_keeps_exit_ok
# warn-days argparse → test_negative_warn_days_argparse_exit_2
