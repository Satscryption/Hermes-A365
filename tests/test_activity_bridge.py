"""Tests for hermes_a365.activity_bridge — slice 19a (verify mode).

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
import urllib.parse
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt as _jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hermes_a365.activity_bridge import (
    APX_PRODUCTION_SCOPE,
    BF_ISSUER,
    BF_OPENID_CONFIG_URL,
    BF_S2S_SCOPE,
    DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES,
    FMI_TOKEN_SCOPE,
    GRAPH_RESOURCE,
    OBSERVABILITY_RESOURCE_APPID,
    TENANT_TOKEN_URL_TEMPLATE,
    BridgeConfig,
    BridgeConfigError,
    JwtValidationError,
    TokenAcquisitionError,
    VerifyReport,
    _activity_delivery_id,
    _agentic_ids_from_activity,
    _BfTokenCache,
    _FmiCache,
    _IdempotencyCache,
    _inbound_path_tag,
    _is_trusted_service_url,
    _JwksCache,
    _UserTokenCache,
    acquire_bf_s2s_token,
    acquire_outbound_token,
    acquire_reply_token,
    acquire_t1_token,
    acquire_t2_token,
    acquire_token,
    acquire_user_fic_token,
    build_ai_message_entity,
    build_webhook_envelope,
    load_agent_env,
    load_bridge_config,
    load_generated_config,
    main,
    make_app,
    peek_unverified_iss,
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
    validate_inbound_jwt_bf,
)

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

    @pytest.mark.parametrize("slug", ["../../tmp/x", "/etc", "..", "a/b", "x\x00y"])
    def test_traversal_slug_refused(self, slug: str, tmp_path: Path) -> None:
        # #103/M9: a traversal-shaped slug is refused (ValueError) before the
        # agents-path join, so nothing outside the agents root is read. Plant a
        # secret where a "../.." would land and confirm it's never returned.
        victim = tmp_path.parent / "victim.env"
        victim.write_text("SECRET=leak\n")
        try:
            with pytest.raises(ValueError):
                load_agent_env(tmp_path, slug)
        finally:
            victim.unlink(missing_ok=True)


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
        with patch("hermes_a365.activity_bridge.urllib.request.urlopen") as urlopen:
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

        with patch("hermes_a365.activity_bridge.urllib.request.urlopen", side_effect=fake_urlopen):
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
            "hermes_a365.activity_bridge.urllib.request.urlopen",
            side_effect=_aad_http_error(401, body),
        ), pytest.raises(TokenAcquisitionError) as excinfo:
            acquire_token(tenant_id="t", client_id="c", client_secret="s")
        assert excinfo.value.code == "AADSTS7000222"
        assert "expired" in excinfo.value.message

    def test_non_aadsts_error_falls_back_to_error_field(self) -> None:
        with patch(
            "hermes_a365.activity_bridge.urllib.request.urlopen",
            side_effect=_aad_http_error(400, {"error": "bad_request", "error_description": "bad"}),
        ), pytest.raises(TokenAcquisitionError) as excinfo:
            acquire_token(tenant_id="t", client_id="c", client_secret="s")
        assert excinfo.value.code == "bad_request"

    def test_url_error_surfaces_as_network_error(self) -> None:
        with patch(
            "hermes_a365.activity_bridge.urllib.request.urlopen",
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
            "hermes_a365.activity_bridge.acquire_token",
            return_value={"token_type": "Bearer", "expires_in": 3599, "access_token": "x"},
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "ok"
        assert "expires_in=3599" in r.detail

    def test_invalid_secret_yields_error(self) -> None:
        with patch(
            "hermes_a365.activity_bridge.acquire_token",
            side_effect=TokenAcquisitionError("AADSTS7000215", "invalid secret"),
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "error"
        assert "rotate" in r.detail

    def test_other_aadsts_yields_error_with_code(self) -> None:
        # Slice 19e: this probe now targets Graph (which works for
        # blueprint apps). Anything other than the secret-rejection
        # codes is reported as a generic error so operators can look
        # the AADSTS code up.
        with patch(
            "hermes_a365.activity_bridge.acquire_token",
            side_effect=TokenAcquisitionError("AADSTS90002", "tenant not found"),
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "error"
        assert "AADSTS90002" in r.detail


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
        with patch(
            "hermes_a365.activity_bridge.socket.gethostbyname",
            side_effect=OSError("no DNS"),
        ):
            r = probe_otlp_endpoint("https://otel.example.invalid/")
        assert r.state == "warn"
        assert "DNS" in r.detail

    def test_dns_resolves_yields_ok(self) -> None:
        with patch("hermes_a365.activity_bridge.socket.gethostbyname", return_value="1.2.3.4"):
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
        # Slice 19e: verify orchestration now includes fmi_exchange
        # between token_acquisition and reachability. Mock the urllib
        # call probe_fmi_exchange makes so this test stays hermetic.
        _seed_agent_env(tmp_path)
        gen_path = _seed_generated_config(tmp_path)
        ok_body = b'{"access_token": "T1", "token_type": "Bearer", "expires_in": 3599}'
        urlopen_ctx = patch(
            "hermes_a365.activity_bridge.urllib.request.urlopen"
        ).start()
        urlopen_ctx.return_value.__enter__.return_value.read.return_value = ok_body
        try:
            with (
                patch(
                    "hermes_a365.activity_bridge.acquire_token",
                    return_value={
                        "token_type": "Bearer",
                        "expires_in": 3599,
                        "access_token": "x",
                    },
                ),
                patch("hermes_a365.activity_bridge.tcp_reachable", return_value=True),
                patch("hermes_a365.activity_bridge.socket.gethostbyname", return_value="1.2.3.4"),
            ):
                report = run_verify(
                    slug="inbox-helper",
                    hermes_home=tmp_path,
                    generated_config_path=gen_path,
                )
        finally:
            patch.stopall()
        assert report.overall == "ok"
        names = [p.name for p in report.probes]
        assert names == [
            "local_config",
            "generated_config",
            "token_acquisition",
            "fmi_exchange",
            "reachability",
            "otlp_endpoint",
        ]


# ---------------------------------------------------------------------------
# Rendering + CLI
# ---------------------------------------------------------------------------


class TestRender:
    def _green_report(self) -> VerifyReport:
        from hermes_a365.activity_bridge import ProbeResult

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
        # Slice 19e: mock the urlopen call probe_fmi_exchange uses too.
        ok_body = b'{"access_token": "T1", "token_type": "Bearer", "expires_in": 3599}'
        urlopen_p = patch("hermes_a365.activity_bridge.urllib.request.urlopen").start()
        urlopen_p.return_value.__enter__.return_value.read.return_value = ok_body
        try:
            with (
                patch(
                    "hermes_a365.activity_bridge.acquire_token",
                    return_value={
                        "token_type": "Bearer",
                        "expires_in": 3599,
                        "access_token": "x",
                    },
                ),
                patch("hermes_a365.activity_bridge.tcp_reachable", return_value=True),
                patch("hermes_a365.activity_bridge.socket.gethostbyname", return_value="1.2.3.4"),
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
        finally:
            patch.stopall()
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


# Slice 19f: tokens are AAD-v2 (issuer = login.microsoftonline.com/<tid>/v2.0,
# `azp` claim names the calling Microsoft SP, no `serviceUrl` claim).
TEST_TENANT_ID = "11111111-1111-1111-1111-111111111111"
TEST_AAD_ISSUER = f"https://login.microsoftonline.com/{TEST_TENANT_ID}/v2.0"
TEST_APX_AZP = "5a807f24-c9de-44ee-a3a7-329e88a00ffc"  # Messaging Bot API SP


def _make_token(
    priv: rsa.RSAPrivateKey,
    *,
    aud: str,
    iss: str = TEST_AAD_ISSUER,
    azp: str = TEST_APX_AZP,
    exp_offset: int = 600,
    extra: dict[str, Any] | None = None,
) -> str:
    payload = {
        "aud": aud,
        "iss": iss,
        "azp": azp,
        "azpacr": "2",
        "tid": TEST_TENANT_ID,
        "ver": "2.0",
        "iat": int(_time.time()),
        "nbf": int(_time.time()),
        "exp": int(_time.time()) + exp_offset,
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
    """httpx transport that serves a fixed JWKS at the AAD-v2 discovery URLs."""
    config = {
        "issuer": TEST_AAD_ISSUER,
        "jwks_uri": (
            f"https://login.microsoftonline.com/{TEST_TENANT_ID}/discovery/v2.0/keys"
        ),
    }
    keys = {"keys": [jwk]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
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
        token = _make_token(priv, aud="bot-app-id")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            claims = await validate_inbound_jwt(
                token=token,
                tenant_id=TEST_TENANT_ID,
                expected_app_id="bot-app-id",
                client=client,
                cache=_JwksCache(),
            )
        assert claims["aud"] == "bot-app-id"
        assert claims["azp"] == TEST_APX_AZP
        assert claims["iss"] == TEST_AAD_ISSUER

    async def test_wrong_audience_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(priv, aud="other-app")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError):
                await validate_inbound_jwt(
                    token=token,
                    tenant_id=TEST_TENANT_ID,
                    expected_app_id="bot-app-id",
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_wrong_tenant_in_issuer_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        """A token whose `iss` names a different tenant than the bridge
        is configured for must 403, even if the signing key happens to
        be reachable. Slice 19f."""
        priv, jwk = rsa_keypair
        # Token claims it was issued by *our* tenant…
        token = _make_token(priv, aud="bot-app-id")
        # …but the bridge is configured for a different one.
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError):
                await validate_inbound_jwt(
                    token=token,
                    tenant_id="22222222-2222-2222-2222-222222222222",
                    expected_app_id="bot-app-id",
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_azp_not_in_allowlist_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        """A valid AAD-v2 token issued by a different Microsoft SP than
        we accept (`azp` mismatch) must 403. Replaces the pre-19f
        serviceUrl claim check. Slice 19f."""
        priv, jwk = rsa_keypair
        # Some other tenant SP that happens to know our app id.
        token = _make_token(priv, aud="bot-app-id", azp="cafebabe-dead-beef-cafe-babecafebabe")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="azp"):
                await validate_inbound_jwt(
                    token=token,
                    tenant_id=TEST_TENANT_ID,
                    expected_app_id="bot-app-id",
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_empty_azp_allowlist_refuses_all(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        """An empty allowlist is a config bug — the validator must not
        silently accept every otherwise-valid token. Slice 19f."""
        priv, jwk = rsa_keypair
        token = _make_token(priv, aud="bot-app-id")
        async with httpx.AsyncClient(transport=_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="azp allowlist is empty"):
                await validate_inbound_jwt(
                    token=token,
                    tenant_id=TEST_TENANT_ID,
                    expected_app_id="bot-app-id",
                    azp_allowlist=(),
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
                    tenant_id=TEST_TENANT_ID,
                    expected_app_id="bot-app-id",
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
            if request.url.path.endswith("openid-configuration"):
                return httpx.Response(
                    200,
                    json={
                        "issuer": TEST_AAD_ISSUER,
                        "jwks_uri": (
                            f"https://login.microsoftonline.com/"
                            f"{TEST_TENANT_ID}/discovery/v2.0/keys"
                        ),
                    },
                )
            return httpx.Response(200, json={"keys": [jwk]})

        cache = _JwksCache()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(3):
                await validate_inbound_jwt(
                    token=token,
                    tenant_id=TEST_TENANT_ID,
                    expected_app_id="bot-app-id",
                    client=client,
                    cache=cache,
                )
        # First call hits both URLs (2 requests). Subsequent calls use the cache (0 each).
        assert request_count["n"] == 2


# ---------------------------------------------------------------------------
# #34 — Path B (classic Bot Framework) JWT validation
# ---------------------------------------------------------------------------


BF_TEST_SERVICE_URL = "https://smba.trafficmanager.net/emea/"


def _bf_jwks_transport(jwk: dict[str, Any]) -> httpx.MockTransport:
    """httpx transport that serves a fixed JWKS at the BF discovery URLs.

    Microsoft documents these as static (not tenant-scoped); we hit the
    discovery URL first, then follow ``jwks_uri`` to the keys document.
    """
    config = {
        "issuer": BF_ISSUER,
        "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    keys = {"keys": [jwk]}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BF_OPENID_CONFIG_URL:
            return httpx.Response(200, json=config)
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json=keys)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestValidateInboundJwtBf:
    """Path B (#34): classic Bot Framework Connector-to-Bot S2S tokens.

    Mirrors the slice 19f ``TestValidateInboundJwt`` shape so failures
    pin to the divergence between A365 and BF validation rules:
    - issuer is ``https://api.botframework.com`` (static, not tenant)
    - JWKS via BF discovery URL (not AAD-v2)
    - ``serviceUrl`` claim must match activity.serviceUrl
    - no ``azp`` allowlist (issuer pin is the strong identity signal)
    """

    async def test_valid_token_returns_claims(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(
            priv,
            aud="bot-app-id",
            iss=BF_ISSUER,
            extra={"serviceUrl": BF_TEST_SERVICE_URL},
        )
        async with httpx.AsyncClient(transport=_bf_jwks_transport(jwk)) as client:
            claims = await validate_inbound_jwt_bf(
                token=token,
                expected_app_id="bot-app-id",
                expected_service_url=BF_TEST_SERVICE_URL,
                client=client,
                cache=_JwksCache(),
            )
        assert claims["aud"] == "bot-app-id"
        assert claims["iss"] == BF_ISSUER
        assert claims["serviceUrl"] == BF_TEST_SERVICE_URL

    async def test_wrong_audience_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(
            priv,
            aud="other-app",
            iss=BF_ISSUER,
            extra={"serviceUrl": BF_TEST_SERVICE_URL},
        )
        async with httpx.AsyncClient(transport=_bf_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="BF signature/aud/iss"):
                await validate_inbound_jwt_bf(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url=BF_TEST_SERVICE_URL,
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_wrong_issuer_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        """A token issued by an AAD tenant (Path A shape) must NOT
        pass the BF validator — even if the signature happens to be
        verifiable. The dispatcher in adapter.py routes by issuer so
        this branch shouldn't normally fire, but the validator must
        still defence-in-depth check."""
        priv, jwk = rsa_keypair
        token = _make_token(
            priv,
            aud="bot-app-id",
            iss=TEST_AAD_ISSUER,
            extra={"serviceUrl": BF_TEST_SERVICE_URL},
        )
        async with httpx.AsyncClient(transport=_bf_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="BF signature/aud/iss"):
                await validate_inbound_jwt_bf(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url=BF_TEST_SERVICE_URL,
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_missing_service_url_claim_accepted(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        """Microsoft's BF docs say the token MUST carry a serviceUrl
        claim, but real Connector→Bot tokens issued for Direct Line +
        Test in Web Chat against a SingleTenant Bot Service
        registration don't include it (Phase 2 walk 2026-05-15, #34).
        The validator accepts tokens missing the claim — issuer pin
        (api.botframework.com) + signing-key check already prove
        Microsoft signed it. If Microsoft tightens the protocol
        later, ``test_mismatched_service_url_rejected`` still pins
        the defence-in-depth path."""
        priv, jwk = rsa_keypair
        token = _make_token(priv, aud="bot-app-id", iss=BF_ISSUER)  # no serviceUrl extra
        async with httpx.AsyncClient(transport=_bf_jwks_transport(jwk)) as client:
            claims = await validate_inbound_jwt_bf(
                token=token,
                expected_app_id="bot-app-id",
                expected_service_url=BF_TEST_SERVICE_URL,
                client=client,
                cache=_JwksCache(),
            )
        assert claims["aud"] == "bot-app-id"
        assert claims["iss"] == BF_ISSUER
        assert "serviceUrl" not in claims

    async def test_mismatched_service_url_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        """When the serviceUrl claim IS present but doesn't match the
        activity's serviceUrl, that's a replay against a different
        bot — 403 (defence-in-depth even though real BF tokens
        observed 2026-05-15 don't carry the claim)."""
        priv, jwk = rsa_keypair
        token = _make_token(
            priv,
            aud="bot-app-id",
            iss=BF_ISSUER,
            extra={"serviceUrl": "https://attacker.example/"},
        )
        async with httpx.AsyncClient(transport=_bf_jwks_transport(jwk)) as client:
            with pytest.raises(JwtValidationError, match="does not match"):
                await validate_inbound_jwt_bf(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url=BF_TEST_SERVICE_URL,
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_unknown_kid_rejected(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, good_jwk = rsa_keypair
        bad_jwk = {**good_jwk, "kid": "different-kid"}
        token = _make_token(
            priv,
            aud="bot-app-id",
            iss=BF_ISSUER,
            extra={"serviceUrl": BF_TEST_SERVICE_URL},
        )
        async with httpx.AsyncClient(transport=_bf_jwks_transport(bad_jwk)) as client:
            with pytest.raises(JwtValidationError, match="not in BF JWKS"):
                await validate_inbound_jwt_bf(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url=BF_TEST_SERVICE_URL,
                    client=client,
                    cache=_JwksCache(),
                )

    async def test_jwks_cache_hits_on_second_call(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, jwk = rsa_keypair
        token = _make_token(
            priv,
            aud="bot-app-id",
            iss=BF_ISSUER,
            extra={"serviceUrl": BF_TEST_SERVICE_URL},
        )
        request_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            request_count["n"] += 1
            if str(request.url) == BF_OPENID_CONFIG_URL:
                return httpx.Response(
                    200,
                    json={
                        "issuer": BF_ISSUER,
                        "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                        "id_token_signing_alg_values_supported": ["RS256"],
                    },
                )
            return httpx.Response(200, json={"keys": [jwk]})

        cache = _JwksCache()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(3):
                await validate_inbound_jwt_bf(
                    token=token,
                    expected_app_id="bot-app-id",
                    expected_service_url=BF_TEST_SERVICE_URL,
                    client=client,
                    cache=cache,
                )
        # First call hits discovery + keys (2 requests). Subsequent calls hit the cache.
        assert request_count["n"] == 2


class TestPeekUnverifiedIss:
    """The dispatcher peeks the JWT's ``iss`` claim without verifying
    the signature to pick between Path A and Path B validators. Peek
    is a routing hint — the actual validator does the real signature
    check, so peek must be tolerant of malformed input (return None;
    caller defaults to A365 path)."""

    def test_returns_bf_issuer(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, _jwk = rsa_keypair
        token = _make_token(priv, aud="bot-app-id", iss=BF_ISSUER)
        assert peek_unverified_iss(token) == BF_ISSUER

    def test_returns_aad_issuer(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        priv, _jwk = rsa_keypair
        token = _make_token(priv, aud="bot-app-id")  # defaults to TEST_AAD_ISSUER
        assert peek_unverified_iss(token) == TEST_AAD_ISSUER

    def test_returns_none_on_garbage(self) -> None:
        # Not a JWT at all — caller falls through to A365 path (which
        # will reject the token in its own real validation step).
        assert peek_unverified_iss("not-a-token") is None
        assert peek_unverified_iss("") is None


# ---------------------------------------------------------------------------
# Outbound auth — three-stage agentic-user-FIC chain (slice 19e)
# ---------------------------------------------------------------------------


def _agentic_token_handler(
    *,
    capture: list[dict[str, Any]] | None = None,
    allowed_user_id: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build an httpx mock handler that fakes all three stages of the
    A365 agentic chain. Each request appends a small dict to
    ``capture`` documenting (url, scope, grant_type) so tests can
    assert the right things were posted in the right order.
    """
    if capture is None:
        capture = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        params: dict[str, str] = {}
        for kv in body.split("&"):
            k, _, v = kv.partition("=")
            params[k] = urllib.parse.unquote_plus(v)
        capture.append(
            {
                "url": str(req.url),
                "grant_type": params.get("grant_type"),
                "scope": params.get("scope"),
                "fmi_path": params.get("fmi_path"),
                "user_id": params.get("user_id"),
            }
        )
        # Each stage returns its own opaque "token". The next stage
        # echoes it back as `client_assertion` / `user_federated_identity_credential`,
        # so we can't tell the three apart from the response alone —
        # the order of arrival is what tests assert on.
        if params.get("fmi_path") and params.get("grant_type") == "client_credentials":
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 3600})
        if (
            params.get("grant_type") == "client_credentials"
            and params.get("client_assertion")
        ):
            return httpx.Response(200, json={"access_token": "T2", "expires_in": 3600})
        if params.get("grant_type") == "user_fic":
            if allowed_user_id is not None and params.get("user_id") != allowed_user_id:
                return httpx.Response(
                    401,
                    json={"error": "unauthorized_client", "error_description": "denied"},
                )
            return httpx.Response(
                200, json={"access_token": "FINAL", "expires_in": 3600}
            )
        return httpx.Response(400, json={"error": "test_unhandled_token_request"})

    return handler


class TestAgenticIdsFromActivity:
    def test_extracts_all_three(self) -> None:
        tenant, instance, user = _agentic_ids_from_activity(_inbound_message_activity())
        assert tenant == "tenant-1"
        assert instance == "blueprint-app-id"
        assert user == "agentic-user-1"

    def test_falls_back_to_conversation_tenant(self) -> None:
        a = _inbound_message_activity()
        a["recipient"].pop("tenantId")
        # conversation.tenantId is the secondary source.
        tenant, _, _ = _agentic_ids_from_activity(a)
        assert tenant == "tenant-1"

    def test_missing_fields_raise(self) -> None:
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        with pytest.raises(RuntimeError, match="agentic identifiers"):
            _agentic_ids_from_activity(a)


class TestAcquireOutboundToken:
    async def test_three_stage_chain_runs_in_order(self) -> None:
        """First call exercises all three stages in order: T1 (FMI) →
        T2 (instance assertion) → final (user_fic)."""
        capture: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler(capture=capture))
        ) as client:
            tok = await acquire_outbound_token(
                client=client,
                cfg=_cfg(),
                activity=_inbound_message_activity(),
                fmi_cache=_FmiCache(),
                user_cache=_UserTokenCache(),
            )
        assert tok == "FINAL"
        # Three POSTs in the right order, each at the tenant token endpoint.
        assert [c["grant_type"] for c in capture] == [
            "client_credentials",  # T1: blueprint impersonates instance via fmi_path
            "client_credentials",  # T2: instance asserts itself via client_assertion
            "user_fic",            # Final: user-context token at messaging scope
        ]
        # First request is FMI step — fmi_path present.
        assert capture[0]["fmi_path"] == "blueprint-app-id"
        assert capture[0]["scope"] == FMI_TOKEN_SCOPE
        # Final request carries the agentic_user_id.
        assert capture[2]["user_id"] == "agentic-user-1"
        assert capture[2]["scope"] == APX_PRODUCTION_SCOPE
        # All three POSTs hit the tenant-specific endpoint, not the BF one.
        for c in capture:
            assert c["url"] == TENANT_TOKEN_URL_TEMPLATE.format(tenant_id="tenant-1")

    async def test_caches_final_token_per_user(self) -> None:
        """Same user, two calls — only one round of three POSTs."""
        capture: list[dict[str, Any]] = []
        fmi = _FmiCache()
        user = _UserTokenCache()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler(capture=capture))
        ) as client:
            await acquire_outbound_token(
                client=client,
                cfg=_cfg(),
                activity=_inbound_message_activity(),
                fmi_cache=fmi,
                user_cache=user,
            )
            await acquire_outbound_token(
                client=client,
                cfg=_cfg(),
                activity=_inbound_message_activity(),
                fmi_cache=fmi,
                user_cache=user,
            )
        assert len(capture) == 3  # not 6

    async def test_distinct_users_share_t1_t2_but_mint_separate_finals(self) -> None:
        """Two activities → same tenant + agent → shared FMI; per-user
        final tokens. Should result in 3 + 1 POSTs (not 6, not 4)."""
        capture: list[dict[str, Any]] = []
        fmi = _FmiCache()
        user = _UserTokenCache()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler(capture=capture))
        ) as client:
            await acquire_outbound_token(
                client=client,
                cfg=_cfg(),
                activity=_inbound_message_activity(agentic_user_id="user-A"),
                fmi_cache=fmi,
                user_cache=user,
            )
            await acquire_outbound_token(
                client=client,
                cfg=_cfg(),
                activity=_inbound_message_activity(agentic_user_id="user-B"),
                fmi_cache=fmi,
                user_cache=user,
            )
        # 3 stages for user-A, just the final stage for user-B.
        assert len(capture) == 4
        assert capture[3]["grant_type"] == "user_fic"
        assert capture[3]["user_id"] == "user-B"

    async def test_individual_stages(self) -> None:
        """Smoke: each stage helper drives a single POST with the right body."""
        capture: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler(capture=capture))
        ) as client:
            t1, _ = await acquire_t1_token(
                client=client,
                tenant_id="t",
                blueprint_client_id="bp",
                blueprint_client_secret="sek",
                agent_app_instance_id="agent-1",
            )
            t2, _ = await acquire_t2_token(
                client=client, tenant_id="t", agent_app_instance_id="agent-1", t1=t1
            )
            final, _ = await acquire_user_fic_token(
                client=client,
                tenant_id="t",
                agent_app_instance_id="agent-1",
                t1=t1,
                t2=t2,
                agentic_user_id="user-1",
                scope=APX_PRODUCTION_SCOPE,
            )
        assert (t1, t2, final) == ("T1", "T2", "FINAL")
        assert len(capture) == 3

    async def test_h1_rejects_foreign_agent_app_id(self) -> None:
        # H1 (#100) — recipient.agenticAppId is an unauthenticated body field; a
        # value differing from the configured blueprint must be refused BEFORE any
        # FMI mint, never trusted to name the identity we mint under.
        a = _inbound_message_activity()
        a["recipient"]["agenticAppId"] = "some-other-tenant-app"
        capture: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler(capture=capture))
        ) as client:
            with pytest.raises(RuntimeError, match="does not match the configured"):
                await acquire_outbound_token(
                    client=client,
                    cfg=_cfg(),
                    activity=a,
                    fmi_cache=_FmiCache(),
                    user_cache=_UserTokenCache(),
                )
        # Refused before any token POST — no partial mint.
        assert capture == []

    async def test_h1_rejects_foreign_tenant(self) -> None:
        # H1/tenant (#100, #106 review) — tenant_id is a body field
        # (recipient.tenantId / conversation.tenantId); a value != the configured
        # tenant must be refused before minting against a foreign token endpoint.
        a = _inbound_message_activity()
        a["recipient"]["tenantId"] = "other-tenant"
        a["conversation"]["tenantId"] = "other-tenant"
        capture: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler(capture=capture))
        ) as client:
            with pytest.raises(RuntimeError, match="does not match the configured tenant"):
                await acquire_outbound_token(
                    client=client,
                    cfg=_cfg(),
                    activity=a,
                    fmi_cache=_FmiCache(),
                    user_cache=_UserTokenCache(),
                )
        assert capture == []

    async def test_m1_user_cache_key_is_full_identity_tuple(self) -> None:
        # M1 (#100) — the per-user cache key is (tenant, agent_app_instance, user,
        # scope), matching _FmiCache, so a (user, scope)-only collision cannot hand
        # one caller a bearer minted under another agent identity.
        user = _UserTokenCache()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler())
        ) as client:
            await acquire_outbound_token(
                client=client,
                cfg=_cfg(),
                activity=_inbound_message_activity(agentic_user_id="user-A"),
                fmi_cache=_FmiCache(),
                user_cache=user,
            )
        (key,) = user.by_key
        assert key == ("tenant-1", "blueprint-app-id", "user-A", APX_PRODUCTION_SCOPE)

    async def test_107_mint_maps_body_user_to_user_fic_request(self) -> None:
        # Client-side characterization only: no documented validated claim maps
        # to agenticUserId, so the mint sends the body value to Entra as user_id.
        # The live positive/negative authorization proof remains in #107/#123.
        a = _inbound_message_activity(agentic_user_id="body-supplied-user-x")
        capture: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler(capture=capture))
        ) as client:
            token = await acquire_outbound_token(
                client=client,
                cfg=_cfg(),
                activity=a,
                fmi_cache=_FmiCache(),
                user_cache=_UserTokenCache(),
            )
        user_fic = [c for c in capture if c["grant_type"] == "user_fic"]
        assert len(user_fic) == 1
        assert user_fic[0]["user_id"] == "body-supplied-user-x"
        assert all(c["user_id"] is None for c in capture if c not in user_fic)
        assert token == "FINAL"

    async def test_107_user_fic_rejection_propagates_fail_closed(self) -> None:
        # This mock does not prove Entra's policy; it verifies the local half of
        # the boundary: a denied user_fic exchange never yields a bearer token.
        capture: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                _agentic_token_handler(
                    capture=capture, allowed_user_id="configured-agentic-user"
                )
            )
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await acquire_outbound_token(
                    client=client,
                    cfg=_cfg(),
                    activity=_inbound_message_activity(
                        agentic_user_id="unauthorized-agentic-user"
                    ),
                    fmi_cache=_FmiCache(),
                    user_cache=_UserTokenCache(),
                )
        user_fic = [c for c in capture if c["grant_type"] == "user_fic"]
        assert len(user_fic) == 1
        assert user_fic[0]["user_id"] == "unauthorized-agentic-user"


# ---------------------------------------------------------------------------
# #33 — Path B outbound: BF S2S client_credentials + dispatcher
# ---------------------------------------------------------------------------


def _bf_s2s_token_handler(
    *,
    capture: list[dict[str, Any]] | None = None,
    access_token: str = "BF-S2S",
    expires_in: int = 3600,
) -> Callable[[httpx.Request], httpx.Response]:
    """Mock the BF S2S ``client_credentials`` POST. Captures the
    posted form fields so tests can assert audience / grant / scope
    are correct. Anything that's NOT a ``client_credentials`` with
    the BF scope returns 400 so we surface unexpected callers."""
    if capture is None:
        capture = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        params: dict[str, str] = {}
        for kv in body.split("&"):
            k, _, v = kv.partition("=")
            params[k] = urllib.parse.unquote_plus(v)
        capture.append(
            {
                "url": str(req.url),
                "grant_type": params.get("grant_type"),
                "scope": params.get("scope"),
                "client_id": params.get("client_id"),
                "client_secret": params.get("client_secret"),
            }
        )
        if (
            params.get("grant_type") == "client_credentials"
            and params.get("scope") == BF_S2S_SCOPE
        ):
            return httpx.Response(
                200, json={"access_token": access_token, "expires_in": expires_in}
            )
        return httpx.Response(400, json={"error": "test_unhandled_token_request"})

    return handler


class TestAcquireBfS2sToken:
    """Path B outbound (#33): simple ``client_credentials`` against the
    tenant token endpoint with BF audience scope."""

    async def test_mints_token_with_bf_scope(self) -> None:
        capture: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_bf_s2s_token_handler(capture=capture))
        ) as client:
            tok = await acquire_bf_s2s_token(
                client=client,
                tenant_id="tenant-1",
                blueprint_client_id="blueprint-app-id",
                blueprint_client_secret="sek",
                bf_cache=_BfTokenCache(),
            )
        assert tok == "BF-S2S"
        assert len(capture) == 1
        req = capture[0]
        assert req["grant_type"] == "client_credentials"
        assert req["scope"] == BF_S2S_SCOPE
        assert req["client_id"] == "blueprint-app-id"
        assert req["client_secret"] == "sek"
        # POSTs against the tenant-scoped endpoint (SingleTenant bot).
        assert req["url"] == TENANT_TOKEN_URL_TEMPLATE.format(tenant_id="tenant-1")

    async def test_caches_token_per_tenant_and_scope(self) -> None:
        """One POST then one cache hit — second call doesn't re-mint."""
        capture: list[dict[str, Any]] = []
        cache = _BfTokenCache()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_bf_s2s_token_handler(capture=capture))
        ) as client:
            for _ in range(3):
                await acquire_bf_s2s_token(
                    client=client,
                    tenant_id="tenant-1",
                    blueprint_client_id="blueprint-app-id",
                    blueprint_client_secret="sek",
                    bf_cache=cache,
                )
        assert len(capture) == 1

    async def test_refreshes_when_within_skew_of_expiry(self) -> None:
        """If the cached entry is within ``TOKEN_REFRESH_SKEW_SECONDS``
        of expiry, we re-mint rather than serve a stale token."""
        from hermes_a365.activity_bridge import TOKEN_REFRESH_SKEW_SECONDS

        capture: list[dict[str, Any]] = []
        cache = _BfTokenCache()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                _bf_s2s_token_handler(capture=capture, expires_in=1)
            )
        ) as client:
            # First call mints at now=0, expires at 1.
            await acquire_bf_s2s_token(
                client=client,
                tenant_id="tenant-1",
                blueprint_client_id="blueprint-app-id",
                blueprint_client_secret="sek",
                bf_cache=cache,
                now=0.0,
            )
            # Second call at now=0 should be cached (within freshness).
            # ...but the skew window is 5 min, so it should refresh
            # immediately because exp - skew < now.
            await acquire_bf_s2s_token(
                client=client,
                tenant_id="tenant-1",
                blueprint_client_id="blueprint-app-id",
                blueprint_client_secret="sek",
                bf_cache=cache,
                now=0.0,
            )
        # 1s ttl < 300s skew → refresh on every call.
        assert len(capture) == 2
        _ = TOKEN_REFRESH_SKEW_SECONDS  # silence unused-import lint

    async def test_aadsts82001_re_raises_with_operator_hint(self) -> None:
        """When Microsoft returns AADSTS82001 (the agentic-app policy
        denial), we re-raise with a clear pointer to the separate-identity
        follow-up rather than passing through the bare 400."""

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": "unauthorized_client",
                    "error_description": (
                        "AADSTS82001: Agentic application 'app-id' is "
                        "not permitted to request app-only tokens for "
                        "resource '8d2d3342-cf29-4959-9577-0e0eafbd16bc'."
                    ),
                    "error_codes": [82001],
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(TokenAcquisitionError) as exc_info:
                await acquire_bf_s2s_token(
                    client=client,
                    tenant_id="tenant-1",
                    blueprint_client_id="blueprint-app-id",
                    blueprint_client_secret="sek",
                    bf_cache=_BfTokenCache(),
                )
        msg = str(exc_info.value)
        assert "Agentic application" in msg
        # #36: error references the env vars operators set to fix it.
        assert "A365_BF_APP_ID" in msg
        assert "A365_BF_CLIENT_SECRET" in msg
        assert "#36" in msg

    async def test_other_400_re_raises_as_http_error(self) -> None:
        """A non-82001 4xx surfaces as the regular httpx HTTPStatusError —
        we only special-case the AADSTS82001 path."""

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"error": "invalid_request", "error_codes": [70002]}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await acquire_bf_s2s_token(
                    client=client,
                    tenant_id="tenant-1",
                    blueprint_client_id="blueprint-app-id",
                    blueprint_client_secret="sek",
                    bf_cache=_BfTokenCache(),
                )

    async def test_distinct_tenants_get_separate_cache_entries(self) -> None:
        capture: list[dict[str, Any]] = []
        cache = _BfTokenCache()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_bf_s2s_token_handler(capture=capture))
        ) as client:
            await acquire_bf_s2s_token(
                client=client,
                tenant_id="tenant-A",
                blueprint_client_id="bp",
                blueprint_client_secret="s",
                bf_cache=cache,
            )
            await acquire_bf_s2s_token(
                client=client,
                tenant_id="tenant-B",
                blueprint_client_id="bp",
                blueprint_client_secret="s",
                bf_cache=cache,
            )
        # Different tenants → different cache keys → both minted.
        assert len(capture) == 2


class TestInboundPathTag:
    """The dispatcher's path classifier (#33). Pure function over the
    inbound activity shape."""

    def test_path_a_when_agentic_ids_present(self) -> None:
        assert _inbound_path_tag(_inbound_message_activity()) == "A"

    def test_path_a_when_tenant_falls_back_to_conversation(self) -> None:
        a = _inbound_message_activity()
        a["recipient"].pop("tenantId")
        # conversation.tenantId still set
        assert _inbound_path_tag(a) == "A"

    def test_path_b_when_no_agentic_but_bf_service_url(self) -> None:
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["serviceUrl"] = "https://smba.trafficmanager.net/teams/"
        assert _inbound_path_tag(a) == "B"

    def test_path_b_via_botframework_com_suffix(self) -> None:
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["serviceUrl"] = "https://directline.botframework.com/"
        assert _inbound_path_tag(a) == "B"

    def test_unknown_when_no_agentic_and_non_bf_service_url(self) -> None:
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["serviceUrl"] = "https://attacker.example/"
        assert _inbound_path_tag(a) == "unknown"

    def test_unknown_when_only_agentic_app_id_present(self) -> None:
        """One of the two agentic fields isn't enough — defence in
        depth against partial/forged shapes."""
        a = _inbound_message_activity()
        a["recipient"].pop("agenticUserId")
        a["serviceUrl"] = "https://attacker.example/"
        assert _inbound_path_tag(a) == "unknown"

    def test_unknown_when_no_service_url(self) -> None:
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["serviceUrl"] = ""
        assert _inbound_path_tag(a) == "unknown"


class TestAcquireReplyTokenDispatcher:
    """The single dispatch site for outbound token mints (#33).
    Routes Path A → user-FIC chain, Path B → BF S2S, raises on
    unknown."""

    async def test_path_a_routes_to_user_fic_chain(self) -> None:
        capture: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_agentic_token_handler(capture=capture))
        ) as client:
            token, path = await acquire_reply_token(
                client=client,
                cfg=_cfg(),
                activity=_inbound_message_activity(),
                fmi_cache=_FmiCache(),
                user_cache=_UserTokenCache(),
                bf_cache=_BfTokenCache(),
                validated_path=None,
            )
        assert (token, path) == ("FINAL", "A")
        # Three POSTs (T1, T2, user_fic) — the full Path A chain.
        assert [c["grant_type"] for c in capture] == [
            "client_credentials",
            "client_credentials",
            "user_fic",
        ]

    async def test_l4_validated_path_b_overrides_body_agentic_ids(self) -> None:
        # L4 (#100) — a BF-validated (Path B) inbound carrying INJECTED agentic ids
        # must NOT mint via the Path A user-FIC chain. validated_path="B" is
        # authoritative over the body-derived tag, so no user-context bearer is
        # minted from a body the JWT never bound.
        capture: list[dict[str, Any]] = []
        # Body LOOKS like Path A (agentic ids present) — but the JWT validated B.
        a = _inbound_message_activity()
        a["serviceUrl"] = "https://smba.trafficmanager.net/emea/"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_bf_s2s_token_handler(capture=capture))
        ) as client:
            token, path = await acquire_reply_token(
                client=client,
                cfg=_cfg(),
                activity=a,
                fmi_cache=_FmiCache(),
                user_cache=_UserTokenCache(),
                bf_cache=_BfTokenCache(),
                validated_path="B",
            )
        assert (token, path) == ("BF-S2S", "B")
        # Single BF S2S client_credentials POST — NOT the Path A user-FIC chain.
        assert len(capture) == 1
        assert capture[0]["grant_type"] == "client_credentials"
        assert capture[0]["scope"] == BF_S2S_SCOPE

    async def test_path_b_routes_to_bf_s2s_mint(self) -> None:
        capture: list[dict[str, Any]] = []
        # Build a Path B inbound (no agentic ids, BF serviceUrl).
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["recipient"].pop("tenantId")
        a["conversation"].pop("tenantId")
        a["serviceUrl"] = "https://smba.trafficmanager.net/emea/"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_bf_s2s_token_handler(capture=capture))
        ) as client:
            token, path = await acquire_reply_token(
                client=client,
                cfg=_cfg(),
                activity=a,
                fmi_cache=_FmiCache(),
                user_cache=_UserTokenCache(),
                bf_cache=_BfTokenCache(),
                validated_path=None,
            )
        assert (token, path) == ("BF-S2S", "B")
        # One POST: client_credentials with BF audience.
        assert len(capture) == 1
        assert capture[0]["grant_type"] == "client_credentials"
        assert capture[0]["scope"] == BF_S2S_SCOPE

    async def test_path_b_uses_bf_app_id_when_set(self) -> None:
        """#36: when ``cfg.bf_app_id`` and ``cfg.bf_client_secret`` are
        set, the dispatcher mints against the separate non-agentic
        identity instead of the blueprint creds (which would 401 with
        AADSTS82001 in production)."""
        capture: list[dict[str, Any]] = []
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["recipient"].pop("tenantId")
        a["conversation"].pop("tenantId")
        a["serviceUrl"] = "https://smba.trafficmanager.net/emea/"

        cfg = BridgeConfig(
            slug="inbox-helper",
            tenant_id="tenant-1",
            blueprint_client_id="blueprint-app-id",
            blueprint_client_secret="blueprint-sek",
            webhook_url="http://hook.test/responder",
            log_path=Path("/tmp/x.log"),
            pid_path=Path("/tmp/x.pid"),
            bf_app_id="bf-app-id",
            bf_client_secret="bf-sek",
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_bf_s2s_token_handler(capture=capture))
        ) as client:
            token, path = await acquire_reply_token(
                client=client,
                cfg=cfg,
                activity=a,
                fmi_cache=_FmiCache(),
                user_cache=_UserTokenCache(),
                bf_cache=_BfTokenCache(),
                validated_path=None,
            )
        assert (token, path) == ("BF-S2S", "B")
        # Critical: the form-encoded body uses the BF (not blueprint) creds.
        assert capture[0]["client_id"] == "bf-app-id"
        assert capture[0]["client_secret"] == "bf-sek"

    async def test_path_b_falls_back_to_blueprint_when_bf_unset(self) -> None:
        """#36: when ``bf_app_id`` is empty (default — Path A-only
        operators), the dispatcher falls back to blueprint creds.
        Production would then 401 AADSTS82001 with the operator-actionable
        error pointing at #36; the test just confirms the credential
        plumbing (which creds get posted)."""
        capture: list[dict[str, Any]] = []
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["recipient"].pop("tenantId")
        a["conversation"].pop("tenantId")
        a["serviceUrl"] = "https://smba.trafficmanager.net/emea/"

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_bf_s2s_token_handler(capture=capture))
        ) as client:
            token, path = await acquire_reply_token(
                client=client,
                cfg=_cfg(),  # bf_app_id / bf_client_secret default to ""
                activity=a,
                fmi_cache=_FmiCache(),
                user_cache=_UserTokenCache(),
                bf_cache=_BfTokenCache(),
                validated_path=None,
            )
        assert (token, path) == ("BF-S2S", "B")
        # Falls back to blueprint creds.
        assert capture[0]["client_id"] == "blueprint-app-id"
        assert capture[0]["client_secret"] == "sek"

    async def test_path_b_falls_back_when_only_one_bf_field_set(self) -> None:
        """Defence-in-depth: both ``bf_app_id`` AND ``bf_client_secret``
        must be set; a half-configured operator state falls back to
        blueprint (which fails with AADSTS82001 + the actionable error)
        rather than minting against a half-shape that would 401 with a
        confusing 'invalid_client' instead."""
        capture: list[dict[str, Any]] = []
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["recipient"].pop("tenantId")
        a["conversation"].pop("tenantId")
        a["serviceUrl"] = "https://smba.trafficmanager.net/emea/"

        cfg = BridgeConfig(
            slug="inbox-helper",
            tenant_id="tenant-1",
            blueprint_client_id="blueprint-app-id",
            blueprint_client_secret="blueprint-sek",
            webhook_url="http://hook.test/responder",
            log_path=Path("/tmp/x.log"),
            pid_path=Path("/tmp/x.pid"),
            bf_app_id="bf-app-id",
            bf_client_secret="",  # half-configured
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_bf_s2s_token_handler(capture=capture))
        ) as client:
            await acquire_reply_token(
                client=client,
                cfg=cfg,
                activity=a,
                fmi_cache=_FmiCache(),
                user_cache=_UserTokenCache(),
                bf_cache=_BfTokenCache(),
                validated_path=None,
            )
        # Half-config falls back to blueprint creds.
        assert capture[0]["client_id"] == "blueprint-app-id"
        assert capture[0]["client_secret"] == "blueprint-sek"

    async def test_unknown_path_raises(self) -> None:
        """An inbound that's neither Path A nor Path B must raise —
        no guessing at audience. The dispatcher refuses to mint
        rather than POST a bearer for the wrong resource."""
        a = _inbound_message_activity()
        a["recipient"].pop("agenticAppId")
        a["recipient"].pop("agenticUserId")
        a["serviceUrl"] = "https://attacker.example/"
        async with httpx.AsyncClient() as client:
            with pytest.raises(RuntimeError, match="cannot classify"):
                await acquire_reply_token(
                    client=client,
                    cfg=_cfg(),
                    activity=a,
                    fmi_cache=_FmiCache(),
                    user_cache=_UserTokenCache(),
                    bf_cache=_BfTokenCache(),
                    validated_path=None,
                )


# ---------------------------------------------------------------------------
# Webhook envelope + reply rendering
# ---------------------------------------------------------------------------


def _cfg(webhook_url: str = "http://hook.test/responder") -> BridgeConfig:
    return BridgeConfig(
        slug="inbox-helper",
        tenant_id="tenant-1",
        blueprint_client_id="blueprint-app-id",
        blueprint_client_secret="sek",
        webhook_url=webhook_url,
        log_path=Path("/tmp/x.log"),
        pid_path=Path("/tmp/x.pid"),
    )


def _inbound_message_activity(
    *, conv_id: str = "conv-1", agentic_user_id: str = "agentic-user-1"
) -> dict[str, Any]:
    """Inbound BF activity with the agentic recipient fields the bridge
    needs to mint outbound tokens (slice 19e).

    For A365 the blueprint Entra app *is* the agent identity, so
    ``agenticAppId`` here matches the blueprint client id used in
    ``_cfg``.
    """
    return {
        "type": "message",
        "id": "1234",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
        "conversation": {"id": conv_id, "tenantId": "tenant-1"},
        "from": {"id": "user-1", "name": "Sadiq"},
        "recipient": {
            "id": "bot-1",
            "name": "Inbox Helper",
            "tenantId": "tenant-1",
            "agenticAppId": "blueprint-app-id",
            "agenticUserId": agentic_user_id,
        },
        "text": "hi",
    }


class TestEnvelope:
    def test_includes_agent_metadata(self) -> None:
        env = build_webhook_envelope(_inbound_message_activity(), _cfg())
        assert env["version"] == "1"
        assert env["agent"]["slug"] == "inbox-helper"
        assert env["agent"]["blueprint_client_id"] == "blueprint-app-id"
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

    def test_reply_carries_ai_generated_content_label(self) -> None:
        # #73(a): every outbound reply is labelled AI-generated content.
        reply = render_reply_activity(
            _inbound_message_activity(), {"text": "hello back"}
        )
        assert reply["entities"] == [
            {
                "type": "https://schema.org/Message",
                "@type": "Message",
                "@context": "https://schema.org",
                "additionalType": ["AIGeneratedContent"],
            }
        ]

    def test_card_reply_carries_ai_label_alongside_attachment(self) -> None:
        card = {"type": "AdaptiveCard", "version": "1.6", "body": []}
        reply = render_reply_activity(
            _inbound_message_activity(), {"text": "see card", "card": card}
        )
        # Entity and attachment coexist on the same activity.
        assert "attachments" in reply
        assert reply["entities"][0]["additionalType"] == ["AIGeneratedContent"]

    # ── #73(b) citations ──────────────────────────────────────────────────

    def test_no_citations_leaves_bare_ai_entity(self) -> None:
        reply = render_reply_activity(_inbound_message_activity(), {"text": "hi"})
        assert "citation" not in reply["entities"][0]

    def test_citations_render_on_message_entity(self) -> None:
        reply = render_reply_activity(
            _inbound_message_activity(),
            {
                "text": "See [1] and [2].",
                "citations": [
                    {"title": "Doc A", "url": "https://a", "abstract": "aa"},
                    {"name": "Doc B", "keywords": ["k1", "k2"]},
                ],
            },
        )
        cits = reply["entities"][0]["citation"]
        assert [c["position"] for c in cits] == [1, 2]
        assert cits[0]["@type"] == "Claim"
        assert cits[0]["appearance"] == {
            "@type": "DigitalDocument",
            "name": "Doc A",
            "url": "https://a",
            "abstract": "aa",
        }
        assert cits[1]["appearance"]["name"] == "Doc B"
        assert cits[1]["appearance"]["keywords"] == ["k1", "k2"]

    def test_citations_capped_at_20_and_skip_non_dict(self) -> None:
        entity = build_ai_message_entity(
            [{"title": f"S{i}"} for i in range(25)] + ["bad"]  # type: ignore[list-item]
        )
        assert len(entity["citation"]) == 20
        assert entity["additionalType"] == ["AIGeneratedContent"]

    # ── #73(c) feedback loop ──────────────────────────────────────────────

    def test_feedback_opt_in_adds_channel_data(self) -> None:
        reply = render_reply_activity(
            _inbound_message_activity(), {"text": "hi", "feedback": True}
        )
        assert reply["channelData"] == {"feedbackLoop": {"type": "default"}}

    def test_feedback_absent_by_default(self) -> None:
        reply = render_reply_activity(_inbound_message_activity(), {"text": "hi"})
        assert "channelData" not in reply

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
    def test_missing_secret_errors_with_actionable_hint(
        self, tmp_path: Path
    ) -> None:
        """Slice 19e: secret missing usually means `a365 publish`
        clobbered the local config. Error should point at the fix."""
        _seed_agent_env(tmp_path)
        path = tmp_path / "a365.generated.config.json"
        path.write_text(
            json.dumps(
                {
                    "agentBlueprintId": "bp-id",
                    "agentBlueprintClientSecret": None,
                }
            )
        )
        with pytest.raises(BridgeConfigError, match="credential reset"):
            load_bridge_config(
                slug="inbox-helper",
                webhook_url="http://hook",
                hermes_home=tmp_path,
                generated_config_path=path,
            )

    @pytest.mark.parametrize("slug", ["../../tmp/x", "/etc", "..", "a/b", "x\x00y"])
    def test_traversal_slug_refused(self, slug: str, tmp_path: Path) -> None:
        # #103/M9: refuse a traversal slug before any path join (agent_dir /
        # bridge.log / bridge.pid). No pid/log file is created anywhere.
        with pytest.raises(ValueError):
            load_bridge_config(
                slug=slug,
                webhook_url="http://hook",
                hermes_home=tmp_path,
                generated_config_path=tmp_path / "a365.generated.config.json",
            )
        assert not list(tmp_path.rglob("bridge.pid"))
        assert not list(tmp_path.rglob("bridge.log"))

    def test_happy_path(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        path = tmp_path / "a365.generated.config.json"
        path.write_text(
            json.dumps(
                {
                    "agentBlueprintId": "blueprint-app-id",
                    "agentBlueprintClientSecret": "sek",
                }
            )
        )
        cfg = load_bridge_config(
            slug="inbox-helper",
            webhook_url="http://hook",
            hermes_home=tmp_path,
            generated_config_path=path,
        )
        assert cfg.blueprint_client_id == "blueprint-app-id"
        assert cfg.blueprint_client_secret == "sek"
        assert cfg.webhook_url == "http://hook"
        # #36 defaults — empty when not set in agent .env.
        assert cfg.bf_app_id == ""
        assert cfg.bf_client_secret == ""

    def test_path_b_identity_loaded_from_agent_env(
        self, tmp_path: Path
    ) -> None:
        """#36: ``A365_BF_APP_ID`` and ``A365_BF_CLIENT_SECRET`` in the
        per-agent .env populate the Path B identity fields. Empty/unset
        defaults to "" (Path A-only operators unaffected)."""
        _seed_agent_env(
            tmp_path,
            A365_BF_APP_ID="bf-app-id",
            A365_BF_CLIENT_SECRET="bf-secret",
        )
        path = tmp_path / "a365.generated.config.json"
        path.write_text(
            json.dumps(
                {
                    "agentBlueprintId": "blueprint-app-id",
                    "agentBlueprintClientSecret": "sek",
                }
            )
        )
        cfg = load_bridge_config(
            slug="inbox-helper",
            webhook_url="http://hook",
            hermes_home=tmp_path,
            generated_config_path=path,
        )
        assert cfg.bf_app_id == "bf-app-id"
        assert cfg.bf_client_secret == "bf-secret"
        # Blueprint creds untouched — both identities coexist.
        assert cfg.blueprint_client_id == "blueprint-app-id"
        assert cfg.blueprint_client_secret == "sek"

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
    reply_status: int = 200,
    reply_body: str = "",
    capture: dict[str, Any] | None = None,
    jwk: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    """Build a transport that handles ALL outbound HTTP the bridge makes:
    - operator's webhook (POST http://hook.test/responder)
    - BF outbound calls (POST {serviceUrl}/v3/conversations/.../activities/...)
    - AAD token endpoint
    - AAD-v2 JWKS discovery + keys (when jwk is provided, for JWT-validation tests)
    """

    if capture is None:
        capture = {"webhook": [], "reply": [], "token": []}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        # Slice 19e: outbound auth is now the 3-stage agentic chain
        # at the tenant token endpoint. We answer all stages
        # generically — tests assert on capture["token"] for the
        # request bodies.
        if "/oauth2/v2.0/token" in url:
            body = req.content.decode()
            params: dict[str, str] = {}
            for kv in body.split("&"):
                k, _, v = kv.partition("=")
                params[k] = urllib.parse.unquote_plus(v)
            capture["token"].append(params)
            grant = params.get("grant_type", "")
            if params.get("fmi_path"):
                return httpx.Response(
                    200, json={"access_token": "T1", "expires_in": 3600}
                )
            if grant == "user_fic":
                return httpx.Response(
                    200, json={"access_token": "FINAL", "expires_in": 3600}
                )
            return httpx.Response(
                200, json={"access_token": "T2", "expires_in": 3600}
            )
        if url.startswith("http://hook.test/responder"):
            capture["webhook"].append(json.loads(req.content))
            if webhook_status != 200:
                return httpx.Response(webhook_status, json={"error": "boom"})
            return httpx.Response(200, json=webhook_response or {})
        if "/v3/conversations/" in url:
            capture["reply"].append({"url": url, "body": json.loads(req.content)})
            if reply_status == 200:
                return httpx.Response(200, json={})
            return httpx.Response(reply_status, text=reply_body)
        # AAD-v2 JWKS discovery + keys (slice 19f).
        if url.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": TEST_AAD_ISSUER,
                    "jwks_uri": (
                        f"https://login.microsoftonline.com/"
                        f"{TEST_TENANT_ID}/discovery/v2.0/keys"
                    ),
                },
            )
        if url.endswith("/discovery/v2.0/keys"):
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
    reply_status: int = 200,
    reply_body: str = "",
    jwk: dict[str, Any] | None = None,
) -> TestClient:
    transport = _serve_handler_factory(
        webhook_response=webhook_response,
        webhook_status=webhook_status,
        reply_status=reply_status,
        reply_body=reply_body,
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

    def test_reply_url_percent_encodes_inbound_ids(self) -> None:
        # #103/M4: conv id / activity id come from the inbound webhook body and
        # are interpolated into the outbound BF URL. A Teams-shaped conv id and
        # a hostile activity id must be percent-encoded so they can't inject
        # path structure, a query, or a fragment.
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity(conv_id="19:abc@thread.tacv2")
        activity["id"] = "../x?y#z"
        with _client_for(
            cfg, capture=capture, webhook_response={"text": "hi back"}
        ) as client:
            r = client.post("/api/messages", json=activity)
        assert r.status_code == 200
        assert len(capture["reply"]) == 1
        reply_url = capture["reply"][0]["url"]
        # Encoded forms present for both segments.
        assert "19%3Aabc%40thread.tacv2" in reply_url
        assert "..%2Fx%3Fy%23z" in reply_url
        # No raw path/query/fragment injection after the conversations/ segment.
        after = reply_url.split("/v3/conversations/", 1)[1]
        assert "../" not in after
        assert "?" not in after
        assert "#" not in after

    @pytest.mark.parametrize("reply_status", [401, 500])
    def test_message_reply_post_failure_does_not_report_replied(
        self, reply_status: int
    ) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        body = "microsoft rejected the reply"
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={"text": "hi back"},
            reply_status=reply_status,
            reply_body=body,
        ) as client:
            r = client.post("/api/messages", json=_inbound_message_activity())

        assert r.status_code == 502
        payload = r.json()
        assert payload["status"] == "reply_failed"
        assert f"HTTP {reply_status}" in payload["error"]
        assert body in payload["error"]
        assert len(capture["reply"]) == 1

    def test_message_reply_post_failure_bounds_response_excerpt(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        long_body = "x" * 600
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={"text": "hi back"},
            reply_status=500,
            reply_body=long_body,
        ) as client:
            r = client.post("/api/messages", json=_inbound_message_activity())

        error = r.json()["error"]
        assert "x" * 500 in error
        assert "x" * 501 not in error
        assert error.endswith("...")

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
        # BF wire: HTTP body is the invokeResponse BODY (unwrapped), and the
        # HTTP status is the invoke status. (v0.8.0 walk fix — Teams rejects
        # the {status,body} wrapper as "Unable to reach app".)
        assert r.json() == {"text": "thanks"}
        # No serviceUrl reply for invoke.
        assert capture["reply"] == []

    def test_invoke_non_numeric_status_coerces_to_200(self) -> None:
        # #97 — a non-numeric operator-webhook status must degrade to 200, not
        # raise a ValueError that becomes an unhandled HTTP 500.
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        invoke = {
            **_inbound_message_activity(),
            "type": "invoke",
            "name": "adaptiveCard/action",
        }
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={
                "invokeResponse": {"status": "not-a-number", "body": {"ok": True}}
            },
        ) as client:
            r = client.post("/api/messages", json=invoke)
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    @pytest.mark.parametrize(
        "invoke_response",
        [
            ["not", "a", "dict"],  # non-dict envelope -> AttributeError on .get()
            "a-string-envelope",  # non-dict envelope
            {"status": float("inf"), "body": {}},  # int(inf) -> OverflowError
            {"status": float("-inf"), "body": {}},  # int(-inf) -> OverflowError
            {"status": float("nan"), "body": {}},  # int(nan) -> ValueError
            {"status": [500], "body": {}},  # int([500]) -> TypeError
        ],
    )
    def test_invoke_malformed_operator_response_degrades_to_200(
        self, invoke_response: Any
    ) -> None:
        # #97 (red-team) — the operator webhook response is arbitrary operator-
        # controlled JSON. A non-dict invokeResponse envelope, or a status that
        # is Infinity / NaN / wrong-type, must degrade to a 200 ack — never an
        # unhandled HTTP 500 (AttributeError / OverflowError / TypeError / ...).
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        invoke = {
            **_inbound_message_activity(),
            "type": "invoke",
            "name": "adaptiveCard/action",
        }
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={"invokeResponse": invoke_response},
        ) as client:
            r = client.post("/api/messages", json=invoke)
        assert r.status_code == 200

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
# Slice 19i — inbound idempotency
# ---------------------------------------------------------------------------


class TestActivityDeliveryId:
    def test_extracts_conv_and_activity_id(self) -> None:
        a = _inbound_message_activity()
        # #103/L7: key is length-prefixed on conv ("conv-1" is 6 chars).
        assert _activity_delivery_id(a) == "6:conv-1:1234"

    def test_missing_conversation_returns_none(self) -> None:
        a = _inbound_message_activity()
        a.pop("conversation")
        assert _activity_delivery_id(a) is None

    def test_missing_activity_id_returns_none(self) -> None:
        a = _inbound_message_activity()
        a.pop("id")
        assert _activity_delivery_id(a) is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [("conversation", {"id": 123}), ("id", 123)],
    )
    def test_non_string_identity_returns_none(self, field: str, value: Any) -> None:
        a = _inbound_message_activity()
        a[field] = value
        assert _activity_delivery_id(a) is None

    def test_non_dict_conversation_returns_none(self) -> None:
        a = _inbound_message_activity()
        a["conversation"] = "not-a-dict"
        assert _activity_delivery_id(a) is None

    def test_colon_in_conv_id_does_not_collide(self) -> None:
        # #103/L7: Teams conv ids carry literal ':'. Without length-prefixing,
        # ("19:abc","123") and ("19","abc:123") would both key "19:abc:123".
        a1 = {"conversation": {"id": "19:abc"}, "id": "123"}
        a2 = {"conversation": {"id": "19"}, "id": "abc:123"}
        k1 = _activity_delivery_id(a1)
        k2 = _activity_delivery_id(a2)
        assert k1 == "6:19:abc:123"
        assert k2 == "2:19:abc:123"
        assert k1 != k2


class TestIdempotencyCache:
    def test_first_call_records_and_returns_false(self) -> None:
        cache = _IdempotencyCache()
        assert cache.is_duplicate("conv-1:abc", now=100.0) is False
        assert "conv-1:abc" not in cache.seen
        assert len(cache.seen) == 1
        assert len(next(iter(cache.seen))) == 64

    def test_second_call_within_ttl_returns_true(self) -> None:
        cache = _IdempotencyCache(ttl_seconds=60.0)
        cache.is_duplicate("k", now=100.0)
        assert cache.is_duplicate("k", now=130.0) is True

    def test_call_after_ttl_returns_false(self) -> None:
        cache = _IdempotencyCache(ttl_seconds=60.0)
        cache.is_duplicate("k", now=100.0)
        # 60s exactly is the boundary; pyjwt-style strict-less-than means
        # ttl must elapse, not just match.
        assert cache.is_duplicate("k", now=160.0) is False

    def test_prune_drops_expired_entries_on_check(self) -> None:
        import hashlib

        cache = _IdempotencyCache(ttl_seconds=60.0)
        cache.is_duplicate("old", now=100.0)
        cache.is_duplicate("fresh", now=190.0)
        # Time has moved well past the old entry's TTL by the third call.
        cache.is_duplicate("probe", now=200.0)
        assert hashlib.sha256(b"old").hexdigest() not in cache.seen
        assert len(cache.seen) == 2

    def test_oversized_delivery_id_is_stored_as_fixed_size_digest(self) -> None:
        cache = _IdempotencyCache()
        raw = "conv:" + "x" * 40_000
        assert cache.is_duplicate(raw, now=100.0) is False
        stored = next(iter(cache.seen))
        assert stored != raw
        assert len(stored) == 64
        assert cache.is_duplicate(raw, now=101.0) is True

    def test_entry_cap_evicts_oldest_digest(self) -> None:
        cache = _IdempotencyCache(ttl_seconds=60.0, max_entries=2)
        assert cache.is_duplicate("a", now=100.0) is False
        assert cache.is_duplicate("b", now=101.0) is False
        assert cache.is_duplicate("c", now=102.0) is False
        assert len(cache.seen) == 2
        # The oldest entry was evicted, so it is admitted as new.
        assert cache.is_duplicate("a", now=103.0) is False
        assert len(cache.seen) == 2

    def test_is_duplicate_mutates_in_place_without_rebuilding(self) -> None:
        # #105 review: the check runs on the inbound request coroutine in both
        # runtimes, so it must not rebuild/scan the whole cache per call (the
        # prior prune-on-check comprehension reassigned self.seen every time,
        # making a full cache fill quadratic). Dict identity is a deterministic
        # proxy for "mutated in place, never re-created".
        cache = _IdempotencyCache(ttl_seconds=60.0)
        original = cache.seen
        for i in range(50):
            cache.is_duplicate(f"k{i}", now=100.0 + i)
        assert cache.seen is original

    def test_filling_to_capacity_is_not_quadratic(self) -> None:
        # #105 review perf guard: the prior O(n)-per-call prune + O(n) min()
        # eviction made filling 20k entries take ~10s. The amortized-O(1) path
        # is milliseconds; a generous ceiling catches a quadratic regression
        # without being timing-flaky.
        import time as _time

        cache = _IdempotencyCache(ttl_seconds=3600.0, max_entries=20_000)
        started = _time.monotonic()
        for i in range(20_000):
            cache.is_duplicate(f"k{i}", now=1000.0 + i * 0.001)
        assert _time.monotonic() - started < 5.0
        assert len(cache.seen) == 20_000

    def test_implicit_clock_is_monotonic_across_wall_clock_rollback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Front-only expiry requires insertion order to match timestamp order.
        # Wall time can roll backwards; make any wall-clock access fail so this
        # regression proves the production path depends only on monotonic time.
        import time as _time

        def _wall_clock_must_not_be_used() -> float:
            raise AssertionError("idempotency cache used wall time")

        monkeypatch.setattr(_time, "time", _wall_clock_must_not_be_used)
        cache = _IdempotencyCache(ttl_seconds=60.0, max_entries=2)

        assert cache.is_duplicate("a") is False
        assert cache.is_duplicate("a") is True

    def test_non_positive_cap_disables_dedupe_loudly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # #105 review: max_entries<=0 DISABLES dedupe (it is a cap, not an
        # "unlimited" knob). Behaviour is retained but must never be silent.
        caplog.set_level("WARNING")
        cache = _IdempotencyCache(ttl_seconds=60.0, max_entries=0)
        assert "DISABLED" in caplog.text
        # Nothing is retained, so a genuine retry re-delivers.
        assert cache.is_duplicate("k", now=100.0) is False
        assert cache.is_duplicate("k", now=101.0) is False
        assert len(cache.seen) == 0

    def test_oversized_cap_is_clamped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # #105 review: a fat-fingered operator cap is clamped in the one place
        # both runtimes construct the cache, and the clamp is logged.
        from hermes_a365.activity_bridge import MAX_IDEMPOTENCY_MAX_ENTRIES

        caplog.set_level("WARNING")
        cache = _IdempotencyCache(max_entries=MAX_IDEMPOTENCY_MAX_ENTRIES + 1)
        assert cache.max_entries == MAX_IDEMPOTENCY_MAX_ENTRIES
        assert "clamping" in caplog.text

    def test_non_integer_cap_falls_back_to_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # #105 review: a non-int cap can arrive from either config path (a
        # malformed `extra` value, or a config object whose attribute lookup
        # always succeeds). Fall back instead of raising on first comparison.
        from hermes_a365.activity_bridge import DEFAULT_IDEMPOTENCY_MAX_ENTRIES

        caplog.set_level("WARNING")
        cache = _IdempotencyCache(max_entries="lots")  # type: ignore[arg-type]
        assert cache.max_entries == DEFAULT_IDEMPOTENCY_MAX_ENTRIES
        assert "not an integer" in caplog.text
        # Still functions as a normal bounded cache.
        assert cache.is_duplicate("k", now=100.0) is False
        assert cache.is_duplicate("k", now=101.0) is True

    def test_bool_cap_is_not_treated_as_one(self) -> None:
        # bool is an int subclass; True must not silently mean "cap of 1".
        from hermes_a365.activity_bridge import DEFAULT_IDEMPOTENCY_MAX_ENTRIES

        cache = _IdempotencyCache(max_entries=True)  # type: ignore[arg-type]
        assert cache.max_entries == DEFAULT_IDEMPOTENCY_MAX_ENTRIES


class TestServeAppUnroutable:
    """#105 review — serve must not fail open on an un-dedupable activity.

    ``_activity_delivery_id`` returns None for a missing/non-string
    conversation or activity id, which SKIPS dedupe. The plugin route got a
    compensating ``unroutable`` ack; without the same guard here every BF
    connector retry would re-fire the (non-idempotent) operator webhook.
    """

    def test_non_string_activity_id_acked_without_forwarding(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity()
        activity["id"] = {"nested": "not-a-string"}
        with _client_for(
            cfg, capture=capture, webhook_response={"text": "hi back"}
        ) as client:
            r1 = client.post("/api/messages", json=activity)
            r2 = client.post("/api/messages", json=activity)
        assert r1.status_code == 200
        assert r1.json() == {"status": "acked", "reason": "unroutable"}
        assert r2.json() == {"status": "acked", "reason": "unroutable"}
        # Never forwarded — so a retry storm can't re-fire the webhook.
        assert capture["webhook"] == []
        assert capture["reply"] == []

    def test_invoke_is_exempt_from_the_unroutable_guard(self) -> None:
        # #105 review: an invoke's response is the HTTP body of this same POST,
        # so it needs neither a delivery key nor a replyToActivity target. The
        # exemption is a deliberate trade-off in serve: the arbitrary operator
        # webhook may be non-idempotent, but blank-acking here would break the
        # interaction outright. The plugin also services invokes ahead of its
        # guard, though its local registry is idempotent. An id-less invoke must
        # still return the real invokeResponse body, not a blank ack.
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity()
        activity["type"] = "invoke"
        activity.pop("id", None)
        with _client_for(
            cfg, capture=capture, webhook_response={"text": "hi back"}
        ) as client:
            r = client.post("/api/messages", json=activity)
        assert r.status_code == 200
        assert len(capture["webhook"]) == 1  # forwarded, not dropped
        assert r.json() is not None

    def test_invoke_webhook_error_degrades_to_benign_ack(self) -> None:
        # #105 review: an invoke's reply is the HTTP body of THIS request, so
        # the out-of-band error card is wrong for it — send_reply hard-requires
        # serviceUrl/conversation.id/id and raised for an id-less invoke,
        # returning 502 (which Teams reads as "unable to reach app") with the
        # serviceUrl echoed in the body. Degrade to the benign empty ack.
        # (Pre-dates this branch; the invoke exemption makes it load-bearing.)
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity()
        activity["type"] = "invoke"
        activity.pop("id", None)
        with _client_for(
            cfg, capture=capture, webhook_status=500
        ) as client:
            r = client.post("/api/messages", json=activity)
        assert r.status_code == 200
        assert r.json() is None
        # No unsolicited error card posted into the conversation.
        assert capture["reply"] == []

    def test_invoke_with_ids_webhook_error_posts_no_card_and_no_marker(self) -> None:
        # The same handler previously POSTed an unsolicited error card AND
        # returned the {"status": "webhook_error"} wrapper as the invokeResponse
        # body — the exact shape the dedupe branch exists to avoid (#96/#97).
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        activity = _inbound_message_activity()
        activity["type"] = "invoke"
        with _client_for(
            cfg, capture=capture, webhook_status=500
        ) as client:
            r = client.post("/api/messages", json=activity)
        assert r.status_code == 200
        assert r.json() is None
        assert capture["reply"] == []

    def test_routable_activity_still_forwards(self) -> None:
        # Guard against over-blocking: a normal activity is unaffected.
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(
            cfg, capture=capture, webhook_response={"text": "hi back"}
        ) as client:
            r = client.post("/api/messages", json=_inbound_message_activity())
        assert r.json()["status"] == "replied"
        assert len(capture["webhook"]) == 1


class TestServeAppDedupe:
    def test_duplicate_delivery_short_circuits_webhook(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(
            cfg, capture=capture, webhook_response={"text": "hi back"}
        ) as client:
            r1 = client.post("/api/messages", json=_inbound_message_activity())
            r2 = client.post("/api/messages", json=_inbound_message_activity())
        assert r1.status_code == 200
        assert r1.json()["status"] == "replied"
        assert r2.status_code == 200
        assert r2.json()["status"] == "duplicate"
        # Webhook + reply only fired once across both POSTs.
        assert len(capture["webhook"]) == 1
        assert len(capture["reply"]) == 1

    def test_m3_authenticated_preseed_suppresses_later_delivery__known_residual(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, dict[str, Any]]
    ) -> None:
        # #100/M3 is an unresolved availability residual owned by #86. Drive
        # the real route with a valid platform token: two different activities
        # sharing the body-derived (conversation.id, activity.id) key collide,
        # so the later legitimate delivery never reaches the webhook.
        priv, jwk = rsa_keypair
        cfg = _cfg()
        cfg.tenant_id = TEST_TENANT_ID
        cfg.blueprint_client_id = "bot-app-id"
        preseed_token = _make_token(
            priv, aud=cfg.blueprint_client_id, extra={"jti": "preseed-token"}
        )
        victim_token = _make_token(
            priv, aud=cfg.blueprint_client_id, extra={"jti": "victim-token"}
        )

        preseed = _inbound_message_activity(conv_id="conv-victim")
        victim = _inbound_message_activity(
            conv_id="conv-victim", agentic_user_id="victim-agentic-user"
        )
        for activity in (preseed, victim):
            activity["id"] = "predicted-activity-id"
            activity["conversation"]["tenantId"] = TEST_TENANT_ID
            activity["recipient"]["tenantId"] = TEST_TENANT_ID
            activity["recipient"]["agenticAppId"] = cfg.blueprint_client_id
        preseed["type"] = "conversationUpdate"
        preseed["from"] = {"id": "preseed-caller"}
        victim["from"] = {"id": "victim-caller"}
        victim["text"] = "legitimate delivery"

        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture, jwk=jwk) as client:
            unauthenticated = client.post("/api/messages", json=preseed)
            first = client.post(
                "/api/messages",
                json=preseed,
                headers={"Authorization": f"Bearer {preseed_token}"},
            )
            second = client.post(
                "/api/messages",
                json=victim,
                headers={"Authorization": f"Bearer {victim_token}"},
            )

        assert unauthenticated.status_code == 401
        assert first.status_code == 200
        assert first.json()["status"] == "acked"
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert capture == {"webhook": [], "reply": [], "token": []}

    def test_deduped_invoke_returns_benign_ack_not_marker(self) -> None:
        # #96 — a deduped *invoke* must not return the {status:duplicate} marker
        # as its body (Teams reads an invoke's HTTP body as the invokeResponse
        # body). Serve does NOT re-forward — the webhook may be non-idempotent —
        # so it returns a benign empty 200 ack; the webhook still fires once.
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        invoke = {
            **_inbound_message_activity(),
            "type": "invoke",
            "name": "adaptiveCard/action",
        }
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={
                "invokeResponse": {"status": 200, "body": {"text": "ok"}}
            },
        ) as client:
            r1 = client.post("/api/messages", json=invoke)
            r2 = client.post("/api/messages", json=invoke)
        # First turn: the real unwrapped invokeResponse body.
        assert r1.status_code == 200
        assert r1.json() == {"text": "ok"}
        # Retry: benign empty ack (null body), NOT the {status:duplicate} marker.
        assert r2.status_code == 200
        assert r2.json() is None
        # Webhook fired once — the retry did not re-forward.
        assert len(capture["webhook"]) == 1

    def test_distinct_activities_both_processed(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        a1 = _inbound_message_activity()
        a2 = {**_inbound_message_activity(), "id": "5678"}
        with _client_for(
            cfg, capture=capture, webhook_response={"text": "echo"}
        ) as client:
            client.post("/api/messages", json=a1)
            client.post("/api/messages", json=a2)
        assert len(capture["webhook"]) == 2

    def test_activity_without_id_is_not_deduped(self) -> None:
        """Channel-control activities can lack an `id` — better to over-
        deliver them than silently drop the second one."""
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        a = {**_inbound_message_activity(), "type": "conversationUpdate"}
        a.pop("id")
        with _client_for(cfg, capture=capture) as client:
            r1 = client.post("/api/messages", json=a)
            r2 = client.post("/api/messages", json=a)
        # Both ack — neither short-circuits as a duplicate.
        assert r1.json()["status"] == "acked"
        assert r2.json()["status"] == "acked"


# ---------------------------------------------------------------------------
# Slice 19j — serviceUrl host suffix allowlist
# ---------------------------------------------------------------------------


class TestIsTrustedServiceUrl:
    def test_real_teams_service_url_accepted(self) -> None:
        # The exact shape captured during the round-3 walkthrough.
        url = "https://smba.trafficmanager.net/amer/2699fca3.../"
        assert _is_trusted_service_url(url, DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES)

    def test_arbitrary_host_rejected(self) -> None:
        assert not _is_trusted_service_url(
            "https://attacker.example/", DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES
        )

    def test_http_rejected_even_on_trusted_host(self) -> None:
        # Plain HTTP must never be accepted — bearer would ride
        # unencrypted.
        assert not _is_trusted_service_url(
            "http://smba.trafficmanager.net/teams/",
            DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES,
        )

    def test_empty_url_rejected(self) -> None:
        assert not _is_trusted_service_url("", DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES)

    def test_empty_suffix_list_rejected(self) -> None:
        assert not _is_trusted_service_url(
            "https://smba.trafficmanager.net/teams/", ()
        )

    def test_suffix_match_is_dns_boundary_not_substring(self) -> None:
        # `evil-trafficmanager.net` must not slip through a naive
        # endswith on `trafficmanager.net`. The `.` prefix on each
        # suffix is the load-bearing detail.
        assert not _is_trusted_service_url(
            "https://evil-trafficmanager.net/", (".trafficmanager.net",)
        )

    def test_m2_registrable_trafficmanager_subdomain_rejected(self) -> None:
        # M2 (#100) — .trafficmanager.net is customer-registrable (any tenant can
        # stand up <label>.trafficmanager.net and receive our outbound bearer), so
        # a registrable subdomain must NOT pass the default allowlist even though
        # it sits under trafficmanager.net. .azure.com is dropped for the same
        # reason.
        assert not _is_trusted_service_url(
            "https://attacker.trafficmanager.net/x/",
            DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES,
        )
        assert not _is_trusted_service_url(
            "https://evil.azure.com/x/", DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES
        )

    def test_m2_exact_host_pin_vs_suffix_semantics(self) -> None:
        # A bare allowlist entry is an EXACT host match (used to pin one Microsoft
        # host inside a registrable zone); a leading-dot entry is a subdomain-
        # suffix match on a non-registrable Microsoft zone.
        suffixes = ("smba.trafficmanager.net", ".botframework.com")
        assert _is_trusted_service_url("https://smba.trafficmanager.net/amer/", suffixes)
        assert _is_trusted_service_url("https://x.botframework.com/", suffixes)
        # The exact-host pin must not admit a registrable sibling or a suffix-
        # spoof host that merely ends with the pinned string.
        assert not _is_trusted_service_url("https://evilsmba.trafficmanager.net/", suffixes)
        assert not _is_trusted_service_url(
            "https://smba.trafficmanager.net.evil.com/", suffixes
        )

    def test_m2_trailing_dot_fqdn_accepted(self) -> None:
        # #106 review — "smba.trafficmanager.net." (absolute/trailing-dot FQDN) is
        # the same host to DNS; the exact-host pin must not miss it.
        assert _is_trusted_service_url(
            "https://smba.trafficmanager.net./amer/",
            DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES,
        )

    def test_m2_malformed_url_fails_closed(self) -> None:
        # #106 review — a urlparse ValueError (bad IPv6) must fail closed (False),
        # never propagate as an unhandled 500.
        assert not _is_trusted_service_url(
            "https://[::1/x", DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES
        )


class TestServeAppServiceUrlGate:
    def test_untrusted_service_url_returns_403(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        a = {**_inbound_message_activity(), "serviceUrl": "https://attacker.example/"}
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            r = client.post("/api/messages", json=a)
        assert r.status_code == 403
        assert "untrusted serviceUrl" in r.json()["detail"]
        # Webhook never fired — the gate sits before any forwarding.
        assert capture["webhook"] == []

    def test_empty_suffix_list_returns_403_config_bug(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        cfg.trusted_service_url_suffixes = ()
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(cfg, capture=capture) as client:
            r = client.post("/api/messages", json=_inbound_message_activity())
        assert r.status_code == 403
        assert "config bug" in r.json()["detail"]

    def test_trusted_service_url_proceeds_normally(self) -> None:
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        with _client_for(
            cfg, capture=capture, webhook_response={"text": "echo"}
        ) as client:
            r = client.post("/api/messages", json=_inbound_message_activity())
        assert r.status_code == 200
        assert r.json()["status"] == "replied"


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


def _unverifiable_token(iss: str) -> str:
    """A JWT parseable enough for peek_unverified_iss; signature is fake (the
    real validators are monkeypatched so it never matters)."""
    import base64
    import json

    def _seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    header = _seg({"alg": "RS256", "typ": "JWT", "kid": "fake"})
    payload = _seg({"iss": iss, "aud": "x", "exp": 9999999999})
    return f"{header}.{payload}.AAAA"


class TestServeDualJwtDispatch:
    """#18 finding 3 — serve now dispatches BF-issuer tokens to
    validate_inbound_jwt_bf, not only the A365/AAD-v2 validator."""

    def _run(
        self, monkeypatch: pytest.MonkeyPatch, iss: str
    ) -> tuple[AsyncMock, AsyncMock, Any]:
        import hermes_a365.activity_bridge as ab

        cfg = _cfg()
        cfg.skip_jwt_validation = False
        a365 = AsyncMock(return_value={"iss": "aad"})
        bf = AsyncMock(return_value={"iss": ab.BF_ISSUER})
        monkeypatch.setattr(ab, "validate_inbound_jwt", a365)
        monkeypatch.setattr(ab, "validate_inbound_jwt_bf", bf)
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        # conversationUpdate acks right after the JWT gate — no webhook needed.
        activity = {**_inbound_message_activity(), "type": "conversationUpdate"}
        with _client_for(cfg, capture=capture) as client:
            r = client.post(
                "/api/messages",
                json=activity,
                headers={"Authorization": f"Bearer {_unverifiable_token(iss)}"},
            )
        assert r.status_code == 200, r.text
        return a365, bf, activity, cfg

    def test_bf_issuer_routes_to_bf_validator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.activity_bridge as ab

        a365, bf, activity, cfg = self._run(monkeypatch, ab.BF_ISSUER)
        bf.assert_awaited_once()
        a365.assert_not_awaited()
        assert bf.await_args.kwargs["expected_service_url"] == activity["serviceUrl"]
        # Parity with the plugin adapter: expected_app_id falls back to the
        # blueprint id when bf_app_id is unset.
        assert bf.await_args.kwargs["expected_app_id"] == (
            cfg.bf_app_id or cfg.blueprint_client_id
        )

    def test_aad_issuer_routes_to_aad_validator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a365, bf, _, _ = self._run(
            monkeypatch, "https://login.microsoftonline.com/tid/v2.0"
        )
        a365.assert_awaited_once()
        bf.assert_not_awaited()


class TestTokenFactory:
    """#18 / 19w-b — TokenFactory mints tool tokens via the existing
    Path-A/Path-B dispatch, without re-deriving the chain."""

    def _factory(self) -> Any:
        import hermes_a365.activity_bridge as ab

        return ab.TokenFactory(
            client=MagicMock(),
            cfg=_cfg(),
            fmi_cache=MagicMock(),
            user_cache=MagicMock(),
            bf_cache=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_for_graph_path_a_uses_user_fic_at_graph_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.activity_bridge as ab

        mint = AsyncMock(return_value="tokenA")
        monkeypatch.setattr(ab, "acquire_outbound_token", mint)
        activity = {
            "recipient": {"agenticAppId": "aa", "agenticUserId": "au", "tenantId": "t"},
            "conversation": {"tenantId": "t"},
        }
        assert await self._factory().for_graph(activity) == "tokenA"
        assert mint.await_args.kwargs["scope"] == f"{ab.GRAPH_RESOURCE}/.default"

    @pytest.mark.asyncio
    async def test_for_graph_path_b_uses_client_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_a365.activity_bridge as ab

        mint = MagicMock(return_value={"access_token": "tokenB"})
        monkeypatch.setattr(ab, "acquire_token", mint)
        activity = {
            "serviceUrl": "https://smba.trafficmanager.net/amer/x/",
            "recipient": {},
            "conversation": {},
        }
        assert await self._factory().for_graph(activity) == "tokenB"
        assert mint.call_args.kwargs["resource"] == ab.GRAPH_RESOURCE

    @pytest.mark.asyncio
    async def test_for_graph_unknown_path_raises(self) -> None:
        with pytest.raises(RuntimeError, match="Path A or Path B"):
            await self._factory().for_graph(
                {"recipient": {}, "conversation": {}, "serviceUrl": "https://x.example/y"}
            )

    @pytest.mark.asyncio
    async def test_for_app_and_for_workiq_not_implemented(self) -> None:
        tf = self._factory()
        with pytest.raises(NotImplementedError):
            await tf.for_app({"recipient": {}}, "some-resource")
        with pytest.raises(NotImplementedError):
            await tf.for_workiq(object())
