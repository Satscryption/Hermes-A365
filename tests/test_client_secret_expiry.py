"""Mock-based tests for client secret expiry checks (#191)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from hermes_a365.client_secret_expiry import (
    DEFAULT_CLIENT_SECRET_EXPIRY_WARN_DAYS,
    check_app_client_secret_expiry,
    classify_client_secret_expiry,
    gather_client_secret_expiry_checks,
    select_nearest_active_credential_end,
)
from hermes_a365.entra_state import (
    EntraStateError,
    application_client_secret_expiry_url,
    assert_client_secret_expiry_read_url,
    assert_registration_read_url,
)

_BLUEPRINT = "11111111-1111-1111-1111-111111111111"
_BF = "22222222-2222-2222-2222-222222222222"
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _graph_get(payload: dict[str, Any]):
    def getter(_url: str) -> dict[str, Any]:
        return payload

    return getter


class TestSelectNearestActive:
    def test_picks_soonest_active_among_several(self) -> None:
        later = _NOW + timedelta(days=400)
        sooner = _NOW + timedelta(days=10)
        creds = [
            {"keyId": "a", "endDateTime": _iso(later)},
            {"keyId": "b", "endDateTime": _iso(sooner)},
        ]
        end, key_id, failure = select_nearest_active_credential_end(creds, now=_NOW)
        assert failure is None
        assert end == sooner
        assert key_id == "b"

    def test_ignores_expired_when_a_future_exists(self) -> None:
        creds = [
            {"endDateTime": _iso(_NOW - timedelta(days=1))},
            {"endDateTime": _iso(_NOW + timedelta(days=5))},
        ]
        end, _, failure = select_nearest_active_credential_end(creds, now=_NOW)
        assert failure is None
        assert end == _NOW + timedelta(days=5)


class TestClassify:
    def test_expired_is_error(self) -> None:
        end = _NOW - timedelta(hours=1)
        state, _, data = classify_client_secret_expiry(end=end, now=_NOW, warn_days=30)
        assert state == "error"
        assert data["days_remaining"] == 0

    def test_within_warn_window(self) -> None:
        end = _NOW + timedelta(days=10)
        state, _, data = classify_client_secret_expiry(end=end, now=_NOW, warn_days=30)
        assert state == "warn"
        assert data["days_remaining"] == 10

    def test_healthy(self) -> None:
        end = _NOW + timedelta(days=400)
        state, _, data = classify_client_secret_expiry(end=end, now=_NOW, warn_days=30)
        assert state == "ok"
        assert data["days_remaining"] == 400


class TestCheckApp:
    def test_expired_graph_payload(self) -> None:
        payload = {
            "passwordCredentials": [{"endDateTime": _iso(_NOW - timedelta(days=2))}],
        }
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
        )
        assert state == "error"
        assert "no valid client secret" in detail
        assert data["check"] == "missing"

    def test_expiring_warns(self) -> None:
        payload = {
            "passwordCredentials": [{"endDateTime": _iso(_NOW + timedelta(days=10))}],
        }
        state, _, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            warn_days=30,
        )
        assert state == "warn"
        assert data["days_remaining"] == 10

    def test_healthy_ok(self) -> None:
        payload = {
            "passwordCredentials": [{"endDateTime": _iso(_NOW + timedelta(days=400))}],
        }
        state, _, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
        )
        assert state == "ok"
        assert data["days_remaining"] == 400

    def test_no_credentials(self) -> None:
        state, detail, _ = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get({"passwordCredentials": []}),
            now=_NOW,
        )
        assert state == "error"
        assert "no client secret configured" in detail

    def test_graph_permission_error_degrades(self) -> None:
        payload = {
            "error": {
                "code": "Authorization_RequestDenied",
                "message": "Insufficient privileges",
            }
        }
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
        )
        assert state == "warn"
        assert "could not check" in detail
        assert data["check"] == "unavailable"

    def test_malformed_response(self) -> None:
        state, detail, data = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get({"passwordCredentials": "not-a-list"}),
            now=_NOW,
        )
        assert state == "warn"
        assert "malformed" in detail
        assert data["check"] == "unavailable"

    def test_honours_warn_days_flag(self) -> None:
        payload = {
            "passwordCredentials": [{"endDateTime": _iso(_NOW + timedelta(days=10))}],
        }
        state, _, _ = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            warn_days=15,
        )
        assert state == "warn"
        state_ok, _, _ = check_app_client_secret_expiry(
            _BLUEPRINT,
            graph_get=_graph_get(payload),
            now=_NOW,
            warn_days=5,
        )
        assert state_ok == "ok"


class TestEntraUrl:
    def test_expiry_url_is_allowlisted(self) -> None:
        url = application_client_secret_expiry_url(_BLUEPRINT)
        assert_client_secret_expiry_read_url(url)
        assert UUID(url.split("appId='")[1].split("'")[0])

    def test_registration_assert_rejects_same_select(self) -> None:
        url = application_client_secret_expiry_url(_BLUEPRINT)
        with pytest.raises(EntraStateError):
            assert_registration_read_url(url)


class TestGather:
    def test_doctor_rows_include_json_fields(self, tmp_path: Path) -> None:
        home = tmp_path / "hermes"
        home.mkdir()
        (home / ".env").write_text(f"A365_APP_ID={_BLUEPRINT}\n")
        sidecar = tmp_path / "missing-sidecar.json"
        end = _NOW + timedelta(days=10)

        def router(url: str) -> dict[str, Any]:
            if _BLUEPRINT in url:
                return {"passwordCredentials": [{"endDateTime": _iso(end)}]}
            return {"passwordCredentials": []}

        rows = gather_client_secret_expiry_checks(
            hermes_home=home,
            agent_name=None,
            sidecar_path=sidecar,
            generated_config_path=tmp_path / "missing.json",
            graph_get=router,
            now=lambda: _NOW,
        )
        by_name = {name: (state, data) for name, state, _, data in rows}
        assert by_name["blueprint_client_secret"][0] == "warn"
        assert by_name["blueprint_client_secret"][1]["days_remaining"] == 10
        assert "end_date_time" in by_name["blueprint_client_secret"][1]
        assert by_name["bf_client_secret"][0] == "skipped"

    def test_default_warn_days_constant(self) -> None:
        assert DEFAULT_CLIENT_SECRET_EXPIRY_WARN_DAYS == 30
