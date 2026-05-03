"""Tests for scripts/consent.py.

Polling tests monkeypatch ``time.sleep``/``time.monotonic`` so the suite
remains hermetic and fast. URL rendering uses the real Jinja env against
``templates/consent-url.txt.j2``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import consent as consent_mod
import pytest
from consent import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    load_tenant_and_app,
    main,
    poll_for_consent,
    render_consent_url,
)

# ---------------------------------------------------------------------------
# Test double for QuerySource
# ---------------------------------------------------------------------------


class _StubQuerySource:
    """Minimal QuerySource — only ``query_consent`` is used by polling tests.

    ``responses`` is a list whose first len(responses)-1 elements are returned
    in order; the last element is repeated forever.
    """

    def __init__(
        self,
        responses: list[dict[str, Any] | None],
        *,
        available: bool = True,
    ) -> None:
        self.responses = list(responses)
        self.available = available
        self.calls = 0

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        self.calls += 1
        idx = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[idx]

    # Stubs to satisfy the Protocol — never called in these tests.
    def query_license(self) -> dict[str, Any] | None:
        return None

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return None

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return None


# ---------------------------------------------------------------------------
# URL rendering
# ---------------------------------------------------------------------------


class TestRenderConsentUrl:
    def test_basic(self) -> None:
        url = render_consent_url("contoso.onmicrosoft.com", "9e2d1f73-3c5b-49a1-bf2d-77a812f5c4e0")
        assert url.startswith("https://login.microsoftonline.com/")
        assert "contoso.onmicrosoft.com" in url
        assert "client_id=9e2d1f73-3c5b-49a1-bf2d-77a812f5c4e0" in url

    def test_uses_v1_adminconsent_endpoint(self) -> None:
        # v0.1 uses the v1 endpoint per template comments.
        url = render_consent_url("t", "a")
        assert "/adminconsent" in url
        assert "/v2.0/" not in url

    def test_no_trailing_newline(self) -> None:
        url = render_consent_url("t", "a")
        assert not url.endswith("\n")

    @pytest.mark.parametrize("tenant,app_id", [("", "x"), ("x", ""), ("", "")])
    def test_empty_inputs_rejected(self, tenant: str, app_id: str) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            render_consent_url(tenant, app_id)


# ---------------------------------------------------------------------------
# load_tenant_and_app
# ---------------------------------------------------------------------------


class TestLoadTenantAndApp:
    def test_missing_env_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="register"):
            load_tenant_and_app(tmp_path)

    def test_missing_keys_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("A365_TENANT_ID=t\n")  # no app id
        with pytest.raises(KeyError, match="A365_APP_ID"):
            load_tenant_and_app(tmp_path)

    def test_happy_path(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("A365_TENANT_ID=contoso\nA365_APP_ID=appid-123\n")
        tenant, app_id = load_tenant_and_app(tmp_path)
        assert tenant == "contoso"
        assert app_id == "appid-123"


# ---------------------------------------------------------------------------
# poll_for_consent
# ---------------------------------------------------------------------------


class TestPollForConsent:
    def _patch_time(
        self, monkeypatch: pytest.MonkeyPatch, *, ticks: list[float] | None = None
    ) -> list[float]:
        """Monkeypatch ``time.sleep`` to advance a virtual clock instead of waiting."""
        sleep_calls: list[float] = []
        clock = {"now": 0.0}

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock["now"] += seconds

        def fake_monotonic() -> float:
            if ticks:
                return ticks.pop(0)
            return clock["now"]

        monkeypatch.setattr(consent_mod.time, "sleep", fake_sleep)
        monkeypatch.setattr(consent_mod.time, "monotonic", fake_monotonic)
        return sleep_calls

    def test_unavailable_raises_runtime(self) -> None:
        qs = _StubQuerySource([], available=False)
        with pytest.raises(RuntimeError, match="unavailable"):
            poll_for_consent(qs, app_id="x")

    def test_invalid_interval(self) -> None:
        qs = _StubQuerySource([{"granted": True}])
        with pytest.raises(ValueError, match="interval"):
            poll_for_consent(qs, app_id="x", interval=0)

    def test_invalid_timeout(self) -> None:
        qs = _StubQuerySource([{"granted": True}])
        with pytest.raises(ValueError, match="timeout"):
            poll_for_consent(qs, app_id="x", timeout=0)

    def test_granted_on_first_call_returns_true_no_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps = self._patch_time(monkeypatch)
        qs = _StubQuerySource([{"granted": True}])
        assert poll_for_consent(qs, app_id="x") is True
        assert qs.calls == 1
        assert sleeps == []  # never slept

    def test_granted_on_third_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps = self._patch_time(monkeypatch)
        qs = _StubQuerySource(
            [
                {"granted": False},
                {"granted": False},
                {"granted": True},
            ]
        )
        assert poll_for_consent(qs, app_id="x", interval=5, timeout=300) is True
        assert qs.calls == 3
        assert sleeps == [5, 5]  # slept twice between three queries

    def test_timeout_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_time(monkeypatch)
        qs = _StubQuerySource([{"granted": False}])  # always pending
        # Tight timeout: 5 seconds, interval 5 → at most one query and a final check.
        assert poll_for_consent(qs, app_id="x", interval=5, timeout=5) is False
        # At least one call to query_consent
        assert qs.calls >= 1

    def test_consent_with_no_payload_treated_as_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_time(monkeypatch)
        qs = _StubQuerySource([None, None, {"granted": True}])
        assert poll_for_consent(qs, app_id="x", interval=1, timeout=100) is True

    def test_default_interval_and_timeout_constants(self) -> None:
        # Sanity: spec says 5s / 5min defaults.
        assert DEFAULT_POLL_INTERVAL_SECONDS == 5.0
        assert DEFAULT_TIMEOUT_SECONDS == 300.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def _bootstrap_env(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("A365_TENANT_ID=contoso\nA365_APP_ID=APPID\n")

    def test_print_url_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._bootstrap_env(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        rc = main(["--print-url-only"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith("https://login.microsoftonline.com/")
        assert "contoso" in out
        assert "client_id=APPID" in out

    def test_no_env_exits_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # tmp_path has no .env
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        rc = main(["--print-url-only"])
        assert rc == 2
        assert "register" in capsys.readouterr().err

    def test_no_open_no_a365_warns_and_returns_1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._bootstrap_env(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Force unavailable QuerySource
        monkeypatch.setattr(
            consent_mod,
            "get_query_source",
            lambda: _StubQuerySource([], available=False),
        )
        rc = main(["--no-open"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "Admin consent URL" in captured.out
        assert "unavailable" in captured.err

    def test_full_flow_grants_immediately(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._bootstrap_env(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(
            consent_mod,
            "get_query_source",
            lambda: _StubQuerySource([{"granted": True}], available=True),
        )
        # Disable browser launch to avoid env-dependent flakiness.
        rc = main(["--no-open"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Consent granted" in out
