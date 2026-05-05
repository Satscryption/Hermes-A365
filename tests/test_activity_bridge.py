"""Tests for scripts/activity_bridge.py — slice 19a (verify mode).

Covers config loading, the AAD token request shape (mocked), the
probes individually, and the verify orchestration end-to-end.
``serve`` mode lands in slice 19b after Microsoft's BF subscription
contract is validated against documentation.
"""

from __future__ import annotations

import json
import os
import time as _time
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import jwt as _jwt
import pytest
from activity_bridge import (
    BF_TOKEN_URL,
    GRAPH_RESOURCE,
    OBSERVABILITY_RESOURCE_APPID,
    BridgeConfig,
    BridgeConfigError,
    JwtValidationError,
    TokenAcquisitionError,
    VerifyReport,
    _BotTokenCache,
    _JwksCache,
    acquire_bot_token,
    acquire_token,
    build_webhook_envelope,
    load_agent_env,
    load_bridge_config,
    load_generated_config,
    main,
    make_app,
    probe_generated_config,
    probe_local_config,
    probe_otlp_endpoint,
    probe_token_acquisition,
    render_error_card,
    render_human,
    render_json,
    render_reply_activity,
    run_verify,
    validate_inbound_jwt,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_agent_env(home: Path, slug: str = "inbox-helper", **overrides: str) -> Path:
    base: dict[str, str] = {
        "AGENT_IDENTITY": slug,
        "OWNER": "sadiq@contoso.com",
        "OWNER_AAD_ID": "00000000-0000-0000-0000-000000000001",
        "A365_APP_ID": "8b563a20-2fac-4210-8210-df139c61e8b7",
        "A365_TENANT_ID": "2699fca3-dac6-40a2-bcea-62ce05e2ee9b",
        "AA_INSTANCE_ID": "550e8400-e29b-41d4-a716-446655440000",
        "HERMES_OTLP_ENDPOINT": "https://contoso.otel.agent365.microsoft.com",
    }
    base.update(overrides)
    agent_dir = home / "agents" / slug
    agent_dir.mkdir(parents=True, exist_ok=True)
    env_path = agent_dir / ".env"
    env_path.write_text("".join(f"{k}={v}\n" for k, v in base.items()))
    return env_path


def _seed_generated_config(
    cwd: Path,
    *,
    secret: str = "test-client-secret-redacted",
    blueprint_id: str = "8b563a20-2fac-4210-8210-df139c61e8b7",
    mode: int = 0o600,
) -> Path:
    path = cwd / "a365.generated.config.json"
    path.write_text(
        json.dumps(
            {
                "agentBlueprintId": blueprint_id,
                "agentBlueprintClientSecret": secret,
                "agentBlueprintObjectId": "obj-id",
                "agentBlueprintServicePrincipalObjectId": "sp-id",
            }
        )
    )
    os.chmod(path, mode)
    return path


def _aad_http_error(status: int, body: dict[str, Any]) -> urllib.error.HTTPError:
    """Build a stub HTTPError that behaves like one returned by AAD."""
    return urllib.error.HTTPError(
        url="https://login.microsoftonline.com/x/oauth2/v2.0/token",
        code=status,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(json.dumps(body).encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# load_agent_env
# ---------------------------------------------------------------------------


class TestLoadAgentEnv:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BridgeConfigError, match="instance create"):
            load_agent_env(tmp_path, "ghost")

    def test_happy_path(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        env = load_agent_env(tmp_path, "inbox-helper")
        assert env["A365_APP_ID"] == "8b563a20-2fac-4210-8210-df139c61e8b7"
        assert env["AA_INSTANCE_ID"] == "550e8400-e29b-41d4-a716-446655440000"


# ---------------------------------------------------------------------------
# load_generated_config
# ---------------------------------------------------------------------------


class TestLoadGeneratedConfig:
    def test_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BridgeConfigError, match="register --apply"):
            load_generated_config(tmp_path / "a365.generated.config.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "a365.generated.config.json"
        path.write_text("not json {{")
        with pytest.raises(BridgeConfigError, match="not JSON"):
            load_generated_config(path)

    def test_happy_path(self, tmp_path: Path) -> None:
        path = _seed_generated_config(tmp_path)
        cfg = load_generated_config(path)
        assert cfg["agentBlueprintClientSecret"]
        assert cfg["agentBlueprintId"]


# ---------------------------------------------------------------------------
# acquire_token
# ---------------------------------------------------------------------------


class TestAcquireToken:
    def test_success_returns_payload(self) -> None:
        sample = {
            "token_type": "Bearer",
            "expires_in": 3599,
            "access_token": "eyJ0eXAi…",
        }
        # Stub urlopen to return our payload.
        with patch("activity_bridge.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
                sample
            ).encode("utf-8")
            out = acquire_token(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert out["access_token"] == "eyJ0eXAi…"
        assert out["expires_in"] == 3599

    def test_request_body_shape(self) -> None:
        captured: dict[str, Any] = {}

        def fake_urlopen(req: Any, timeout: float = 0) -> Any:
            captured["url"] = req.full_url
            captured["body"] = req.data.decode("utf-8")
            captured["method"] = req.get_method()
            captured["content_type"] = req.headers.get("Content-type")

            class _Ctx:
                def __enter__(self_inner) -> Any:
                    return _Ctx()

                def __exit__(self_inner, *_: Any) -> None:
                    pass

                def read(self_inner) -> bytes:
                    return json.dumps({"access_token": "x", "expires_in": 60}).encode("utf-8")

            return _Ctx()

        with patch("activity_bridge.urllib.request.urlopen", side_effect=fake_urlopen):
            acquire_token(
                tenant_id="t-123",
                client_id="appid-abc",
                client_secret="sek/ret with+chars",
                resource=GRAPH_RESOURCE,
            )
        assert captured["url"] == "https://login.microsoftonline.com/t-123/oauth2/v2.0/token"
        assert captured["method"] == "POST"
        assert captured["content_type"] == "application/x-www-form-urlencoded"
        # Body should be form-urlencoded with proper quoting.
        body = captured["body"]
        assert "grant_type=client_credentials" in body
        assert "client_id=appid-abc" in body
        # Spaces and `+` and `/` get percent-encoded; `+` becomes `%2B`.
        assert "client_secret=sek%2Fret+with%2Bchars" in body
        # urlencode percent-encodes `:` and `/` in the value too.
        assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body

    def test_aadsts_code_extracted_from_error(self) -> None:
        body = {
            "error": "invalid_client",
            "error_description": (
                "AADSTS7000222: The provided client secret keys "
                "for app are expired"
            ),
        }
        with patch(
            "activity_bridge.urllib.request.urlopen",
            side_effect=_aad_http_error(401, body),
        ), pytest.raises(TokenAcquisitionError) as excinfo:
            acquire_token(tenant_id="t", client_id="c", client_secret="s")
        assert excinfo.value.code == "AADSTS7000222"
        assert "expired" in excinfo.value.message

    def test_non_aadsts_error_falls_back_to_error_field(self) -> None:
        with patch(
            "activity_bridge.urllib.request.urlopen",
            side_effect=_aad_http_error(400, {"error": "bad_request", "error_description": "bad"}),
        ), pytest.raises(TokenAcquisitionError) as excinfo:
            acquire_token(tenant_id="t", client_id="c", client_secret="s")
        assert excinfo.value.code == "bad_request"

    def test_url_error_surfaces_as_network_error(self) -> None:
        with patch(
            "activity_bridge.urllib.request.urlopen",
            side_effect=urllib.error.URLError("name resolution failed"),
        ), pytest.raises(TokenAcquisitionError) as excinfo:
            acquire_token(tenant_id="t", client_id="c", client_secret="s")
        assert excinfo.value.code == "network_error"


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


class TestProbeLocalConfig:
    def test_missing_env_yields_error(self, tmp_path: Path) -> None:
        probe, env = probe_local_config(tmp_path, "ghost")
        assert probe.state == "error"
        assert env == {}

    def test_missing_required_keys_yields_error(self, tmp_path: Path) -> None:
        # AGENT_IDENTITY is fine but A365_APP_ID is required.
        agent_dir = tmp_path / "agents" / "ghost"
        agent_dir.mkdir(parents=True)
        (agent_dir / ".env").write_text("AGENT_IDENTITY=ghost\n")
        probe, _env = probe_local_config(tmp_path, "ghost")
        assert probe.state == "error"
        assert "missing keys" in probe.detail

    def test_happy_path(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        probe, env = probe_local_config(tmp_path, "inbox-helper")
        assert probe.state == "ok"
        assert env["A365_APP_ID"]


class TestProbeGeneratedConfig:
    def test_missing_yields_error(self, tmp_path: Path) -> None:
        probe, data = probe_generated_config(tmp_path / "a365.generated.config.json")
        assert probe.state == "error"
        assert data == {}

    def test_warns_on_world_readable_perms(self, tmp_path: Path) -> None:
        # Slice 18x policy: secret-bearing files must be 0600. Verify
        # the bridge surfaces a warning if the operator's filesystem
        # left them looser.
        path = _seed_generated_config(tmp_path, mode=0o644)
        probe, data = probe_generated_config(path)
        assert probe.state == "warn"
        assert "chmod 600" in probe.detail
        # Secret was still extracted — the probe is a warning, not a hard error.
        assert data["client_secret"]

    def test_happy_path_at_0600(self, tmp_path: Path) -> None:
        path = _seed_generated_config(tmp_path, mode=0o600)
        probe, data = probe_generated_config(path)
        assert probe.state == "ok"
        assert data["blueprint_id"]
        assert data["client_secret"]


class TestProbeTokenAcquisition:
    def test_ok_on_successful_token(self) -> None:
        with patch(
            "activity_bridge.acquire_token",
            return_value={"token_type": "Bearer", "expires_in": 3599, "access_token": "x"},
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "ok"
        assert "expires_in=3599" in r.detail

    def test_invalid_secret_yields_error(self) -> None:
        with patch(
            "activity_bridge.acquire_token",
            side_effect=TokenAcquisitionError("AADSTS7000215", "invalid secret"),
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "error"
        assert "rotate" in r.detail

    def test_no_role_grants_yields_warn_not_error(self) -> None:
        # AADSTS7000218 is "the app has no permissions on this resource"
        # — diagnostic-positive: the secret WORKS, just the scope is wrong.
        with patch(
            "activity_bridge.acquire_token",
            side_effect=TokenAcquisitionError("AADSTS7000218", "no role"),
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "warn"
        assert "secret valid but no role" in r.detail


class TestProbeOtlpEndpoint:
    def test_unset_yields_warn(self) -> None:
        r = probe_otlp_endpoint(None)
        assert r.state == "warn"

    def test_no_host_yields_error(self) -> None:
        # urlparse is lenient — "htp://" still yields a parseable URL
        # with no host. The probe should report this as an error since
        # there's nothing to DNS-resolve.
        r = probe_otlp_endpoint("htp:///no-scheme-no-host")
        assert r.state == "error"
        assert "no host" in r.detail

    def test_dns_lookup_failure_yields_warn(self) -> None:
        with patch("activity_bridge.socket.gethostbyname", side_effect=OSError("no DNS")):
            r = probe_otlp_endpoint("https://otel.example.invalid/")
        assert r.state == "warn"
        assert "DNS" in r.detail

    def test_dns_resolves_yields_ok(self) -> None:
        with patch("activity_bridge.socket.gethostbyname", return_value="1.2.3.4"):
            r = probe_otlp_endpoint("https://otel.example.com/")
        assert r.state == "ok"


# ---------------------------------------------------------------------------
# run_verify orchestration
# ---------------------------------------------------------------------------


class TestRunVerify:
    def test_skips_token_when_local_config_missing(self, tmp_path: Path) -> None:
        # No agent .env. Token probe should be skipped (not crashed).
        report = run_verify(slug="ghost", hermes_home=tmp_path)
        token_probes = [p for p in report.probes if p.name == "token_acquisition"]
        assert len(token_probes) == 1
        assert token_probes[0].state == "warn"
        assert "skipped" in token_probes[0].detail

    def test_full_happy_path(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        gen_path = _seed_generated_config(tmp_path)
        with (
            patch(
                "activity_bridge.acquire_token",
                return_value={"token_type": "Bearer", "expires_in": 3599, "access_token": "x"},
            ),
            patch("activity_bridge.tcp_reachable", return_value=True),
            patch("activity_bridge.socket.gethostbyname", return_value="1.2.3.4"),
        ):
            report = run_verify(
                slug="inbox-helper",
                hermes_home=tmp_path,
                generated_config_path=gen_path,
            )
        assert report.overall == "ok"
        names = [p.name for p in report.probes]
        assert names == [
            "local_config",
            "generated_config",
            "token_acquisition",
            "reachability",
            "otlp_endpoint",
        ]


# ---------------------------------------------------------------------------
# Rendering + CLI
# ---------------------------------------------------------------------------


class TestRender:
    def _green_report(self) -> VerifyReport:
        from activity_bridge import ProbeResult

        return VerifyReport(
            slug="x",
            probes=[
                ProbeResult("local_config", "ok", "ok"),
                ProbeResult("token_acquisition", "ok", "ok"),
            ],
        )

    def test_human_includes_slug_and_overall(self) -> None:
        text = render_human(self._green_report())
        assert "verify — x" in text
        assert "overall: ok" in text

    def test_json_is_parseable(self) -> None:
        out = render_json(self._green_report())
        parsed = json.loads(out)
        assert parsed["slug"] == "x"
        assert parsed["overall"] == "ok"
        assert len(parsed["probes"]) == 2


class TestCli:
    def test_verify_exit_codes_track_overall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_agent_env(tmp_path)
        gen_path = _seed_generated_config(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with (
            patch(
                "activity_bridge.acquire_token",
                return_value={"token_type": "Bearer", "expires_in": 3599, "access_token": "x"},
            ),
            patch("activity_bridge.tcp_reachable", return_value=True),
            patch("activity_bridge.socket.gethostbyname", return_value="1.2.3.4"),
        ):
            rc = main(
                [
                    "verify",
                    "--slug",
                    "inbox-helper",
                    "--generated-config",
                    str(gen_path),
                    "--human",
                ]
            )
        assert rc == 0
        out = capsys.readouterr().out
        assert "overall: ok" in out

    def test_verify_returns_2_when_secret_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_agent_env(tmp_path)
        # No generated-config file → probe error → overall=error → exit 2.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        rc = main(
            [
                "verify",
                "--slug",
                "inbox-helper",
                "--generated-config",
                str(tmp_path / "missing.json"),
            ]
        )
        assert rc == 2


# ---------------------------------------------------------------------------
# Pinned constants
# ---------------------------------------------------------------------------


def test_observability_resource_pinned() -> None:
    # Verified in the 2026-05-05 round-2 walkthrough as the
    # `Agent365Observability` resource appId. Pin so a future
    # refactor surfaces the change here first.
    assert OBSERVABILITY_RESOURCE_APPID == "9b975845-388f-4429-889e-eab1ef63949c"


# ===========================================================================
# Slice 19b — serve mode
# ===========================================================================
#
# Tests below cover the FastAPI app via TestClient with mocked HTTP
# (httpx.MockTransport) for both inbound JWKS and outbound BF connector
# / webhook calls. JWT validation is exercised against an ephemeral
# RSA keypair we publish via a fake JWKS document.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rsa_keypair() -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    """An ephemeral 2048-bit RSA key + matching JWKS entry."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_numbers = priv.public_key().public_numbers()
    import base64

    def _b64u(n: int) -> str:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "test-kid-1",
        "n": _b64u(pub_numbers.n),
        "e": _b64u(pub_numbers.e),
    }
    return priv, jwk


def _make_token(
    priv: rsa.RSAPrivateKey,
    *,
    aud: str,
    iss: str = "https://api.botframework.com",
    service_url: str = "https://smba.trafficmanager.net/teams/",
    exp_offset: int = 600,
    extra: dict[str, Any] | None = None,
) -> str:
    payload = {
        "aud": aud,
        "iss": iss,
        "iat": int(_time.time()),
        "exp": int(_time.time()) + exp_offset,
        "serviceUrl": service_url,
    }
    if extra:
        payload.update(extra)
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return _jwt.encode(payload, pem, algorithm="RS256", headers={"kid": "test-kid-1"})


def _jwks_transport(jwk: dict[str, Any]) -> httpx.MockTransport:
    """httpx transport that serves a fixed JWKS at the BF discovery URLs."""
    config = {
        "issuer": "https://api.botframework.com",
        "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
    }
    keys = {"keys": [jwk]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openidconfiguration"):
            return httpx.Response(200, json=config)
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json=keys)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# JWT validation
# ---------------------------------------------------------------------------


class TestValidateInboundJwt:
    async def test_valid_token_returns_claims(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(priv, aud="bot-app-id", service_url="https://svc/")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            claims = await validate_inbound_jwt(
                token=token,
                expected_app_id="bot-app-id",
                expected_service_url="https://svc/",
                client=client,
                cache=_JwksCache(),
            )
        assert claims["aud"] == "bot-app-id"
        assert claims["serviceUrl"] == "https://svc/"

    async def test_wrong_audience_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(priv, aud="other-app")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError):
                await validate_inbound_jwt(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url="https://smba.trafficmanager.net/teams/",
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_service_url_mismatch_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(priv, aud="bot-app-id", service_url="https://A/")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="serviceUrl claim mismatch"):
                await validate_inbound_jwt(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url="https://B/",
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_unknown_kid_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, _good_jwk = rsa_keypair
        # Publish a different jwk so the kid in the token is unknown.
        bad_jwk = {**_good_jwk, "kid": "different-kid"}
        token = _make_token(priv, aud="bot-app-id")
        async with httpx.AsyncClient(transport=_jwks_transport(bad_jwk)) as client:
            with pytest.raises(JwtValidationError, match="not in JWKS"):
                await validate_inbound_jwt(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url="https://smba.trafficmanager.net/teams/",
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_jwks_cache_hits_on_second_call(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(priv, aud="bot-app-id")
        request_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            request_count["n"] += 1
            if request.url.path.endswith("/openidconfiguration"):
                return httpx.Response(
                    200,
                    json={
                        "issuer": "https://api.botframework.com",
                        "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                    },
                )
            return httpx.Response(200, json={"keys": [jwk]})

        cache = _JwksCache()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(3):
                await validate_inbound_jwt(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url="https://smba.trafficmanager.net/teams/",
                    client=client,
                    cache=cache,
                )
        # First call hits both URLs (2 requests). Subsequent calls use the cache (0 each).
        assert request_count["n"] == 2


# ---------------------------------------------------------------------------
# Outbound BF token cache
# ---------------------------------------------------------------------------


class TestAcquireBotToken:
    async def test_first_call_fetches_token(self) -> None:
        cfg = BridgeConfig(
            slug="x",
            tenant_id="t",
            bot_app_id="bot",
            bot_app_secret="sek",
            webhook_url="http://hook",
            log_path=Path("/tmp/x.log"),
            pid_path=Path("/tmp/x.pid"),
        )

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url == BF_TOKEN_URL
            assert b"client_id=bot" in req.content
            assert b"scope=https%3A%2F%2Fapi.botframework.com%2F.default" in req.content
            return httpx.Response(
                200, json={"access_token": "tok-abc", "expires_in": 3600}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = _BotTokenCache()
            t = await acquire_bot_token(client=client, cfg=cfg, cache=cache)
        assert t == "tok-abc"
        assert cache.access_token == "tok-abc"

    async def test_cached_token_reused(self) -> None:
        cfg = BridgeConfig(
            slug="x",
            tenant_id="t",
            bot_app_id="bot",
            bot_app_secret="sek",
            webhook_url="http://hook",
            log_path=Path("/tmp/x.log"),
            pid_path=Path("/tmp/x.pid"),
        )
        call_count = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(
                200, json={"access_token": "tok-abc", "expires_in": 3600}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = _BotTokenCache()
            await acquire_bot_token(client=client, cfg=cfg, cache=cache)
            await acquire_bot_token(client=client, cfg=cfg, cache=cache)
            await acquire_bot_token(client=client, cfg=cfg, cache=cache)
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Webhook envelope + reply rendering
# ---------------------------------------------------------------------------


def _cfg(webhook_url: str = "http://hook.test/responder") -> BridgeConfig:
    return BridgeConfig(
        slug="inbox-helper",
        tenant_id="2699fca3-...",
        bot_app_id="bot-app-id",
        bot_app_secret="sek",
        webhook_url=webhook_url,
        log_path=Path("/tmp/x.log"),
        pid_path=Path("/tmp/x.pid"),
    )


def _inbound_message_activity() -> dict[str, Any]:
    return {
        "type": "message",
        "id": "1234",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
        "conversation": {"id": "conv-1"},
        "from": {"id": "user-1", "name": "Sadiq"},
        "recipient": {"id": "bot-1", "name": "Inbox Helper"},
        "text": "hi",
    }


class TestEnvelope:
    def test_includes_agent_metadata(self) -> None:
        env = build_webhook_envelope(_inbound_message_activity(), _cfg())
        assert env["version"] == "1"
        assert env["agent"]["slug"] == "inbox-helper"
        assert env["agent"]["bot_app_id"] == "bot-app-id"
        # Activity passed through verbatim — includes serviceUrl, channelId, etc.
        assert env["activity"]["serviceUrl"].startswith("https://smba")
        assert env["activity"]["text"] == "hi"


class TestRenderReply:
    def test_text_only_response(self) -> None:
        reply = render_reply_activity(
            _inbound_message_activity(), {"text": "hello back"}
        )
        assert reply["type"] == "message"
        assert reply["text"] == "hello back"
        # from/recipient must swap per BF reply convention.
        assert reply["from"]["id"] == "bot-1"
        assert reply["recipient"]["id"] == "user-1"
        assert reply["replyToId"] == "1234"
        assert "attachments" not in reply

    def test_card_attached_with_correct_content_type(self) -> None:
        card = {"type": "AdaptiveCard", "version": "1.6", "body": []}
        reply = render_reply_activity(
            _inbound_message_activity(), {"text": "see card", "card": card}
        )
        assert reply["attachments"][0] == {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }

    def test_error_card_shape(self) -> None:
        card = render_error_card("oops")
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == "1.6"
        # Error message included verbatim.
        assert any("oops" in (b.get("text") or "") for b in card["body"])


# ---------------------------------------------------------------------------
# load_bridge_config
# ---------------------------------------------------------------------------


class TestLoadBridgeConfig:
    def test_missing_botMsaAppId_errors_with_actionable_hint(
        self, tmp_path: Path
    ) -> None:
        _seed_agent_env(tmp_path)
        # Generated config without botMsaAppId — the round-2 walkthrough's
        # actual state. The error must point operators at the fix.
        path = tmp_path / "a365.generated.config.json"
        path.write_text(
            json.dumps(
                {
                    "agentBlueprintId": "bp-id",
                    "agentBlueprintClientSecret": "sek",
                    "botMsaAppId": None,
                }
            )
        )
        with pytest.raises(BridgeConfigError, match="--m365"):
            load_bridge_config(
                slug="inbox-helper",
                webhook_url="http://hook",
                hermes_home=tmp_path,
                generated_config_path=path,
            )

    def test_happy_path(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        path = tmp_path / "a365.generated.config.json"
        path.write_text(
            json.dumps(
                {
                    "agentBlueprintId": "bp-id",
                    "agentBlueprintClientSecret": "sek",
                    "botMsaAppId": "bot-app-id",
                }
            )
        )
        cfg = load_bridge_config(
            slug="inbox-helper",
            webhook_url="http://hook",
            hermes_home=tmp_path,
            generated_config_path=path,
        )
        assert cfg.bot_app_id == "bot-app-id"
        assert cfg.bot_app_secret == "sek"
        assert cfg.webhook_url == "http://hook"

    def test_falls_back_to_env_var_when_no_webhook_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_agent_env(tmp_path)
        path = tmp_path / "a365.generated.config.json"
        path.write_text(
            json.dumps(
                {
                    "agentBlueprintId": "bp-id",
                    "agentBlueprintClientSecret": "sek",
                    "botMsaAppId": "bot",
                }
            )
        )
        monkeypatch.setenv("HERMES_BRIDGE_WEBHOOK", "http://from-env")
        cfg = load_bridge_config(
            slug="inbox-helper",
            webhook_url=None,
            hermes_home=tmp_path,
            generated_config_path=path,
        )
        assert cfg.webhook_url == "http://from-env"


# ---------------------------------------------------------------------------
# FastAPI app via TestClient
# ---------------------------------------------------------------------------


def _serve_handler_factory(
    *,
    webhook_response: dict[str, Any] | None = None,
    webhook_status: int = 200,
    capture: dict[str, Any] | None = None,
    jwk: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    """Build a transport that handles ALL outbound HTTP the bridge makes:
    - operator's webhook (POST http://hook.test/responder)
    - BF outbound calls (POST {serviceUrl}/v3/conversations/.../activities/...)
    - AAD token endpoint
    - BF JWKS discovery + keys (when jwk is provided, for JWT-validation tests)
    """

    if capture is None:
        capture = {"webhook": [], "reply": [], "token": []}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == BF_TOKEN_URL:
            capture["token"].append(req.content)
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
        if url.startswith("http://hook.test/responder"):
            capture["webhook"].append(json.loads(req.content))
            if webhook_status != 200:
                return httpx.Response(webhook_status, json={"error": "boom"})
            return httpx.Response(200, json=webhook_response or {})
        if "/v3/conversations/" in url:
            capture["reply"].append({"url": url, "body": json.loads(req.content)})
            return httpx.Response(200, json={})
        # JWKS discovery + keys.
        if url.endswith("/openidconfiguration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://api.botframework.com",
                    "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                },
            )
        if url.endswith("/.well-known/keys"):
            keys = [jwk] if jwk else []
            return httpx.Response(200, json={"keys": keys})
        return httpx.Response(404, text=f"unhandled {url}")

    return httpx.MockTransport(handler)


def _client_for(
    cfg: BridgeConfig,
    *,
    capture: dict[str, Any],
    webhook_response: dict[str, Any] | None = None,
    webhook_status: int = 200,
    jwk: dict[str, Any] | None = None,
) -> TestClient:
    transport = _serve_handler_factory(
        webhook_response=webhook_response,
        webhook_status=webhook_status,
        capture=capture,
        jwk=jwk,
    )
    http_client = httpx.AsyncClient(transport=transport)
    app = make_app(cfg, http_client=http_client)
    return TestClient(app)


class TestServeApp:
    def test_healthz(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["slug"] == "inbox-helper"

    def test_message_forwards_to_webhook_and_replies(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True  # JWT path tested separately
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={"text": "hi back"},
        ) as client:
            r = client.post("/api/messages", json=_inbound_message_activity())
        assert r.status_code == 200
        assert r.json()["status"] == "replied"
        # Webhook was called with the envelope.
        assert capture["webhook"][0]["agent"]["slug"] == "inbox-helper"
        assert capture["webhook"][0]["activity"]["text"] == "hi"
        # Reply was POSTed to the right URL with the right body.
        assert len(capture["reply"]) == 1
        reply_url = capture["reply"][0]["url"]
        assert "/v3/conversations/conv-1/activities/1234" in reply_url
        assert capture["reply"][0]["body"]["text"] == "hi back"

    def test_message_with_card_response(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        card = {"type": "AdaptiveCard", "version": "1.6", "body": []}
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={"text": "see card", "card": card},
        ) as client:
            client.post("/api/messages", json=_inbound_message_activity())
        body = capture["reply"][0]["body"]
        assert body["attachments"][0]["content"] == card

    def test_invoke_returns_inline_response(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        invoke = {**_inbound_message_activity(), "type": "invoke", "name": "adaptiveCard/action"}
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={
                "invokeResponse": {"status": 200, "body": {"text": "thanks"}}
            },
        ) as client:
            r = client.post("/api/messages", json=invoke)
        assert r.status_code == 200
        # Invoke replies are SYNC: response body is the invokeResponse.
        assert r.json() == {"status": 200, "body": {"text": "thanks"}}
        # No serviceUrl reply for invoke.
        assert capture["reply"] == []

    def test_conversation_update_acked_no_webhook(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        update = {**_inbound_message_activity(), "type": "conversationUpdate"}
        with _client_for(cfg, capture=capture) as client:
            r = client.post("/api/messages", json=update)
        assert r.status_code == 200
        assert r.json()["status"] == "acked"
        assert capture["webhook"] == []

    def test_webhook_error_surfaces_error_card(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture, webhook_status=500) as client:
            r = client.post("/api/messages", json=_inbound_message_activity())
        assert r.status_code == 200
        assert r.json()["status"] == "webhook_error"
        # An error card was sent back to the user via serviceUrl reply.
        assert len(capture["reply"]) == 1
        attachments = capture["reply"][0]["body"]["attachments"]
        assert attachments[0]["content"]["type"] == "AdaptiveCard"

    def test_jwt_missing_returns_401(self) -> None:
        cfg = _cfg()  # JWT validation enabled
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            r = client.post("/api/messages", json=_inbound_message_activity())
        assert r.status_code == 401

    def test_jwt_invalid_returns_403(self) -> None:
        cfg = _cfg()  # JWT validation enabled
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            r = client.post(
                "/api/messages",
                json=_inbound_message_activity(),
                headers={"Authorization": "Bearer not-a-valid-jwt"},
            )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# update-endpoint
# ---------------------------------------------------------------------------


class TestUpdateEndpointCli:
    def test_dry_run_renders_argv_with_m365(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "update-endpoint",
                "--agent-name",
                "Hermes Inbox Helper",
                "--url",
                "https://example.trycloudflare.com/api/messages",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "--m365" in out
        assert "--update-endpoint https://example.trycloudflare.com/api/messages" in out

    def test_no_m365_omits_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "update-endpoint",
                "--agent-name",
                "X",
                "--url",
                "https://x.example/api/messages",
                "--no-m365",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "--m365" not in out

    def test_non_https_url_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "update-endpoint",
                "--agent-name",
                "X",
                "--url",
                "http://insecure.example/api/messages",
            ]
        )
        assert rc == 2
        assert "must be HTTPS" in capsys.readouterr().err
