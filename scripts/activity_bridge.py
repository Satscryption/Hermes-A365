"""hermes a365 activity-bridge — Bot Framework adapter daemon for A365 agents.

Three subcommands:

- ``verify`` (slice 19a): one-shot diagnostic. Validates the registered
  blueprint's client secret can acquire an OAuth token, that AAD +
  Graph are reachable, and that the per-agent config + generated-config
  files parse cleanly. Useful for CI gates and pre-deploy smoke.
- ``serve`` (slice 19b): long-running BF webhook adapter. FastAPI app
  on a local port that operators expose via ``cloudflared`` / reverse
  proxy. Receives BF activities (JWT-validated against the public
  Bot Framework JWKS), forwards each one to ``HERMES_BRIDGE_WEBHOOK``
  with our stable JSON envelope, renders the webhook's JSON response
  as an Adaptive Card or plain text, and replies via the public BF
  connector at ``api.botframework.com`` using the bot's MSA client
  credentials (``botMsaAppId`` + ``agentBlueprintClientSecret`` from
  ``a365.generated.config.json``).
- ``update-endpoint`` (slice 19b): thin wrapper around
  ``a365 setup blueprint --m365 --update-endpoint <url>`` so operators
  can pin the agent's messaging endpoint to a tunnel URL with one
  command. Includes the cleanup-then-recreate fallback for the known
  duplicate-name error (Agent365-devTools issue #140).

Per-agent state files (serve mode):

    ~/.hermes/agents/<slug>/bridge.pid    # daemon PID, removed on shutdown
    ~/.hermes/agents/<slug>/bridge.log    # append-only operational log

Auth wiring (verified against Microsoft Learn 2026-05-05):

- Inbound JWT validation uses Microsoft's public BF metadata at
  ``https://login.botframework.com/v1/.well-known/openidconfiguration``.
  Issuer ``https://api.botframework.com``, audience = bot's appId,
  RS256, 5-min skew, ``serviceUrl`` claim must match the activity's.
  JWKS cached for 24h.
- Outbound replies acquire a token at
  ``https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token``
  with scope ``https://api.botframework.com/.default``. Token cached
  until 5 min before expiry. POST to
  ``{serviceUrl}/v3/conversations/{conv}/activities/{activity}``.

CLI use::

    python scripts/activity_bridge.py verify --slug inbox-helper
    python scripts/activity_bridge.py serve  --slug inbox-helper --port 3978
    python scripts/activity_bridge.py update-endpoint --slug X --url https://...
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from _common import parse_env, tcp_reachable

# Slice 19b — `serve` mode dependencies. Optional extras: operators
# who only need `verify` can install without them. We bind the names
# to ``None`` if missing so the verify path imports cleanly; serve
# mode raises a helpful error at startup.
try:
    import httpx as _httpx
    import jwt as _jwt
    from fastapi import Body as _Body
    from fastapi import FastAPI as _FastAPI
    from fastapi import Header as _Header
    from fastapi import HTTPException as _HTTPException
    from fastapi.responses import JSONResponse as _JSONResponse
except ImportError:  # pragma: no cover — exercised by integration tests
    _httpx = None  # type: ignore[assignment]
    _jwt = None  # type: ignore[assignment]
    _Body = None  # type: ignore[assignment]
    _FastAPI = None  # type: ignore[assignment]
    _Header = None  # type: ignore[assignment]
    _HTTPException = None  # type: ignore[assignment]
    _JSONResponse = None  # type: ignore[assignment]

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

# Resource appIds for token acquisition. The 2026-05-05 walkthrough
# confirmed our blueprint has the ``Agent365Observability`` S2S
# app-role assignment (bug #18 — only this one was set, even though
# the CLI claimed bot+power-platform also got configured). Probing
# this resource validates the secret + the one S2S grant we know
# exists. ``client_credentials`` to other resources may legitimately
# fail with ``AADSTS7000218`` (no app role) — that's still positive
# evidence the secret itself works.
OBSERVABILITY_RESOURCE_APPID = "9b975845-388f-4429-889e-eab1ef63949c"
GRAPH_RESOURCE = "https://graph.microsoft.com"

# Hosts the bridge needs reachable at runtime.
_REACHABILITY_HOSTS: tuple[str, ...] = (
    "login.microsoftonline.com",
    "graph.microsoft.com",
)

ProbeState = Literal["ok", "warn", "error"]
_OK: ProbeState = "ok"
_WARN: ProbeState = "warn"
_ERROR: ProbeState = "error"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BridgeConfigError(RuntimeError):
    """Raised when the bridge can't load its config (missing files / keys)."""


class TokenAcquisitionError(RuntimeError):
    """Raised when the AAD token endpoint rejects our credentials."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    name: str
    state: ProbeState
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyReport:
    slug: str
    probes: list[ProbeResult] = field(default_factory=list)

    @property
    def overall(self) -> ProbeState:
        if any(p.state == _ERROR for p in self.probes):
            return _ERROR
        if any(p.state == _WARN for p in self.probes):
            return _WARN
        return _OK


# ---------------------------------------------------------------------------
# Path / env helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def load_agent_env(hermes_home: Path, slug: str) -> dict[str, str]:
    """Read ``~/.hermes/agents/<slug>/.env`` into a dict.

    Raises :class:`BridgeConfigError` if the file is absent or unparseable.
    """
    path = hermes_home / "agents" / slug / ".env"
    if not path.exists():
        raise BridgeConfigError(
            f"agent .env missing: {path}; run `hermes a365 instance create {slug} --apply`"
        )
    try:
        return parse_env(path.read_text())
    except OSError as e:
        raise BridgeConfigError(f"agent .env unreadable: {e}") from e


def load_generated_config(path: Path) -> dict[str, Any]:
    """Read ``a365.generated.config.json`` and return the parsed dict.

    Raises :class:`BridgeConfigError` on missing file or JSON error.
    """
    if not path.exists():
        raise BridgeConfigError(
            f"{path} missing; run `hermes a365 register --apply` to create the blueprint"
        )
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BridgeConfigError(f"{path} unreadable / not JSON: {e}") from e


# ---------------------------------------------------------------------------
# AAD token acquisition (client_credentials)
# ---------------------------------------------------------------------------


def acquire_token(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    resource: str = OBSERVABILITY_RESOURCE_APPID,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Acquire an OAuth token via the client_credentials grant.

    ``resource`` is the resource appId (a GUID) or full resource URI
    (e.g. ``https://graph.microsoft.com``). The function suffixes
    ``/.default`` automatically.

    Returns the parsed JSON token response (with ``access_token``,
    ``token_type``, ``expires_in`` keys). Raises
    :class:`TokenAcquisitionError` on AAD-side failures with the
    surfaced ``AADSTS<code>`` for the operator to look up.
    """
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"{resource}/.default",
        }
    ).encode("utf-8")
    req = urllib.request.Request(        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # AAD always returns JSON for errors; surface the code.
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body_text)
            code = err.get("error", "unknown_error")
            description = err.get("error_description", body_text)
        except json.JSONDecodeError:
            code = f"http_{e.code}"
            description = body_text
        # AADSTS code lives inside error_description as "AADSTS\d+:".
        aadsts = ""
        if "AADSTS" in description:
            for tok in description.split():
                if tok.startswith("AADSTS"):
                    aadsts = tok.rstrip(":,")
                    break
        raise TokenAcquisitionError(aadsts or code, description) from e
    except urllib.error.URLError as e:
        raise TokenAcquisitionError("network_error", str(e.reason)) from e
    return payload


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_local_config(hermes_home: Path, slug: str) -> tuple[ProbeResult, dict[str, str]]:
    try:
        env = load_agent_env(hermes_home, slug)
    except BridgeConfigError as e:
        return (
            ProbeResult("local_config", _ERROR, str(e)),
            {},
        )
    needed = ("A365_TENANT_ID", "A365_APP_ID", "AA_INSTANCE_ID")
    missing = [k for k in needed if not env.get(k)]
    if missing:
        return (
            ProbeResult(
                "local_config",
                _ERROR,
                f"~/.hermes/agents/{slug}/.env missing keys: {missing}",
                {"missing": missing},
            ),
            env,
        )
    return (
        ProbeResult(
            "local_config",
            _OK,
            f"slug={slug} | tenant={env['A365_TENANT_ID']} | app_id={env['A365_APP_ID'][:8]}…",
            {"slug": slug, "tenant_id": env["A365_TENANT_ID"]},
        ),
        env,
    )


def probe_generated_config(path: Path) -> tuple[ProbeResult, dict[str, Any]]:
    try:
        cfg = load_generated_config(path)
    except BridgeConfigError as e:
        return (ProbeResult("generated_config", _ERROR, str(e)), {})
    secret = cfg.get("agentBlueprintClientSecret")
    blueprint_id = cfg.get("agentBlueprintId")
    if not secret:
        return (
            ProbeResult(
                "generated_config",
                _ERROR,
                f"{path} has no agentBlueprintClientSecret; re-run `hermes a365 register --apply`",
            ),
            {},
        )
    if not blueprint_id:
        return (
            ProbeResult(
                "generated_config",
                _ERROR,
                f"{path} has no agentBlueprintId",
            ),
            {},
        )
    # Permission audit: the file should be 0600. Slice 18i / 18x policy.
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = -1
    extra: dict[str, Any] = {"blueprint_id": blueprint_id, "mode": f"0{mode:o}"}
    detail = f"appId={blueprint_id[:8]}… secret loaded (mode=0{mode:o})"
    if mode >= 0 and (mode & 0o077):
        return (
            ProbeResult(
                "generated_config",
                _WARN,
                f"{detail}; world-/group-readable — chmod 600 {path}",
                extra,
            ),
            {"blueprint_id": blueprint_id, "client_secret": secret},
        )
    return (
        ProbeResult("generated_config", _OK, detail, extra),
        {"blueprint_id": blueprint_id, "client_secret": secret},
    )


def probe_token_acquisition(
    *, tenant_id: str, client_id: str, client_secret: str
) -> ProbeResult:
    """Probe ``client_credentials`` against Microsoft Graph.

    Slice 19e (issue #6) reframed this probe. Originally it targeted
    the Observability resource — but the GA Microsoft "Agentic
    application" policy now blocks ``client_credentials`` for
    blueprint apps on every messaging-related resource (returns
    ``AADSTS82001``). Graph still works, so it remains the single
    auth-validity smoke test verify mode runs. The full outbound
    chain (T1 / T2 / user_fic) is exercised by ``probe_fmi_exchange``.
    """
    try:
        token = acquire_token(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            resource=GRAPH_RESOURCE,
        )
    except TokenAcquisitionError as e:
        if e.code in ("AADSTS7000215", "AADSTS7000222"):
            return ProbeResult(
                "token_acquisition",
                _ERROR,
                f"client secret rejected ({e.code}); re-run register --apply to rotate",
                {"aadsts": e.code},
            )
        return ProbeResult(
            "token_acquisition",
            _ERROR,
            f"token request failed: {e.code}: {e.message[:120]}",
            {"aadsts": e.code},
        )
    return ProbeResult(
        "token_acquisition",
        _OK,
        f"Graph token acquired (expires_in={token.get('expires_in', '?')}s)",
        {"token_type": token.get("token_type", "?")},
    )


def probe_fmi_exchange(
    *,
    tenant_id: str,
    blueprint_client_id: str,
    blueprint_client_secret: str,
    agent_app_instance_id: str,
) -> ProbeResult:
    """Probe step 1 of the agentic chain: blueprint impersonates the agent
    identity instance via FMI.

    On success the bridge can mint outbound tokens (modulo the
    user-context final stage, which can't be exercised without a real
    inbound activity). On failure, the blueprint→instance binding is
    likely misconfigured — the most common cause is `a365 publish`
    having clobbered the local secret (round-3 finding).
    """
    body = urllib.parse.urlencode(
        {
            "client_id": blueprint_client_id,
            "scope": FMI_TOKEN_SCOPE,
            "fmi_path": agent_app_instance_id,
            "grant_type": "client_credentials",
            "client_secret": blueprint_client_secret,
        }
    ).encode("utf-8")
    url = TENANT_TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body_text)
            description = err.get("error_description", body_text)
        except json.JSONDecodeError:
            description = body_text
        aadsts = ""
        if "AADSTS" in description:
            for tok in description.split():
                if tok.startswith("AADSTS"):
                    aadsts = tok.rstrip(":,")
                    break
        return ProbeResult(
            "fmi_exchange",
            _ERROR,
            f"FMI step 1 failed: {aadsts or 'http_error'}: {description[:120]}",
            {"aadsts": aadsts},
        )
    except urllib.error.URLError as e:
        return ProbeResult(
            "fmi_exchange", _ERROR, f"network error: {e.reason}"
        )
    return ProbeResult(
        "fmi_exchange",
        _OK,
        (
            f"T1 acquired (expires_in={payload.get('expires_in', '?')}s); "
            "blueprint→instance binding live"
        ),
        {"token_type": payload.get("token_type", "?")},
    )


def probe_reachability(hosts: tuple[str, ...] = _REACHABILITY_HOSTS) -> ProbeResult:
    unreachable = [h for h in hosts if not tcp_reachable(h)]
    if unreachable:
        return ProbeResult(
            "reachability",
            _ERROR,
            f"unreachable: {unreachable}",
            {"unreachable": unreachable},
        )
    return ProbeResult(
        "reachability", _OK, f"reachable: {list(hosts)}", {"hosts": list(hosts)}
    )


def probe_otlp_endpoint(endpoint: str | None) -> ProbeResult:
    if not endpoint:
        return ProbeResult(
            "otlp_endpoint",
            _WARN,
            "HERMES_OTLP_ENDPOINT not set in agent .env; spans won't be exported",
        )
    try:
        host = urlparse(endpoint).hostname
    except ValueError:
        return ProbeResult(
            "otlp_endpoint", _ERROR, f"unparseable URL: {endpoint!r}"
        )
    if not host:
        return ProbeResult(
            "otlp_endpoint",
            _ERROR,
            f"no host extracted from {endpoint!r}",
        )
    try:
        socket.gethostbyname(host)
    except OSError as e:
        return ProbeResult(
            "otlp_endpoint",
            _WARN,
            f"DNS lookup failed for {host}: {e}; spans may not export at runtime",
            {"host": host},
        )
    return ProbeResult(
        "otlp_endpoint",
        _OK,
        f"resolves: {host} (DNS only — actual POST happens at runtime)",
        {"host": host, "endpoint": endpoint},
    )


# ---------------------------------------------------------------------------
# Verify orchestration
# ---------------------------------------------------------------------------


def run_verify(
    *,
    slug: str,
    hermes_home: Path | None = None,
    generated_config_path: Path | None = None,
) -> VerifyReport:
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    if generated_config_path is None:
        generated_config_path = Path.cwd() / "a365.generated.config.json"

    report = VerifyReport(slug=slug)

    local_probe, agent_env = probe_local_config(hermes_home, slug)
    report.probes.append(local_probe)

    gen_probe, gen_data = probe_generated_config(generated_config_path)
    report.probes.append(gen_probe)

    if local_probe.state == _ERROR or gen_probe.state == _ERROR:
        # Skip auth probes — we don't have what we need.
        for name in ("token_acquisition", "fmi_exchange"):
            report.probes.append(
                ProbeResult(
                    name,
                    _WARN,
                    "skipped — local_config or generated_config probe failed",
                )
            )
    else:
        report.probes.append(
            probe_token_acquisition(
                tenant_id=agent_env["A365_TENANT_ID"],
                client_id=agent_env["A365_APP_ID"],
                client_secret=gen_data["client_secret"],
            )
        )
        # Slice 19e (issue #6): FMI step-1 exchange. For A365 the blueprint
        # Entra app *is* the agent identity (round-3 confirmed
        # ``botMsaAppId == agentBlueprintId == botId``), so the agent
        # app instance id we pass as ``fmi_path`` is the same blueprint id.
        report.probes.append(
            probe_fmi_exchange(
                tenant_id=agent_env["A365_TENANT_ID"],
                blueprint_client_id=agent_env["A365_APP_ID"],
                blueprint_client_secret=gen_data["client_secret"],
                agent_app_instance_id=gen_data["blueprint_id"],
            )
        )

    report.probes.append(probe_reachability())
    report.probes.append(probe_otlp_endpoint(agent_env.get("HERMES_OTLP_ENDPOINT")))

    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_STATE_BADGE = {_OK: "[ ok ]", _WARN: "[WARN]", _ERROR: "[ERR ]"}


def render_human(report: VerifyReport) -> str:
    lines = [f"hermes a365 activity-bridge verify — {report.slug}", "-" * 60]
    for p in report.probes:
        badge = _STATE_BADGE.get(p.state, f"[{p.state}]")
        lines.append(f"  {badge}  {p.name:<19}  {p.detail}")
    lines.append("")
    lines.append(f"overall: {report.overall}")
    if report.overall != _OK:
        lines.append("")
        lines.append("Re-run with --json for raw probe data.")
    return "\n".join(lines) + "\n"


def render_json(report: VerifyReport) -> str:
    return json.dumps(
        {
            "slug": report.slug,
            "overall": report.overall,
            "probes": [asdict(p) for p in report.probes],
        },
        indent=2,
    ) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _overall_to_exit_code(overall: ProbeState) -> int:
    return {_OK: 0, _WARN: 1, _ERROR: 2}[overall]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 activity-bridge — Bot Framework adapter daemon.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    verify = sub.add_parser(
        "verify",
        help="one-shot diagnostic: validate config + auth + reachability",
    )
    verify.add_argument(
        "--slug", required=True, help="agent slug (matches ~/.hermes/agents/<slug>/)"
    )
    verify.add_argument(
        "--generated-config",
        type=Path,
        help="path to a365.generated.config.json (default: ./a365.generated.config.json)",
    )
    verify.add_argument(
        "--human", action="store_true", help="formatted output (default: JSON)"
    )

    serve = sub.add_parser(
        "serve",
        help="long-running BF webhook adapter (slice 19b)",
    )
    serve.add_argument("--slug", required=True)
    serve.add_argument(
        "--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)"
    )
    serve.add_argument("--port", type=int, default=3978)
    serve.add_argument(
        "--webhook",
        help=(
            "operator's responder URL (default: HERMES_BRIDGE_WEBHOOK env var). "
            "Bridge POSTs each inbound activity to this URL and renders the "
            "response as a reply."
        ),
    )
    serve.add_argument(
        "--generated-config",
        type=Path,
        help="path to a365.generated.config.json (default: ./a365.generated.config.json)",
    )
    serve.add_argument(
        "--no-jwt-validation",
        action="store_true",
        help=(
            "DEV ONLY: skip Bot Framework JWT validation on inbound activities. "
            "Useful when testing locally without going through real BF infra. "
            "Never set this in production — incoming /api/messages will accept "
            "unauthenticated requests."
        ),
    )

    update_ep = sub.add_parser(
        "update-endpoint",
        help="re-point the agent's messaging endpoint at a tunnel URL",
    )
    update_ep.add_argument("--agent-name", required=True)
    update_ep.add_argument(
        "--url", required=True, help="HTTPS endpoint A365 should POST activities to"
    )
    update_ep.add_argument("--tenant-id")
    update_ep.add_argument(
        "--no-m365",
        action="store_true",
        help="omit --m365 (default behaviour passes --m365 so Teams routes through MCP)",
    )
    update_ep.add_argument("--apply", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "verify":
        try:
            report = run_verify(
                slug=args.slug,
                generated_config_path=args.generated_config,
            )
        except (BridgeConfigError, TokenAcquisitionError) as e:
            # These shouldn't escape run_verify (each probe catches them),
            # but defend against future regressions.
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        sys.stdout.write(render_human(report) if args.human else render_json(report))
        return _overall_to_exit_code(report.overall)

    if args.cmd == "serve":
        return cmd_serve(args)

    if args.cmd == "update-endpoint":
        return cmd_update_endpoint(args)

    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable; argparse exits


# ===========================================================================
# Slice 19b — serve mode
# ===========================================================================
#
# All serve-mode code is below this line. It depends on the optional
# `bridge` extras (fastapi, uvicorn, httpx, pyjwt[crypto]); imports are
# deferred so the verify path keeps working with no extras installed.

# A365 inbound auth — AAD v2.0 tokens (slice 19f, 2026-05-05).
# A365 / MCP Platform issues AAD-v2 tokens directly to the bot
# endpoint, not classic Bot Framework tokens. The pre-19f validator
# expected `iss = https://api.botframework.com` and a BF JWKS, which
# 403'd every real activity. Real captured token (round-3 walkthrough)
# carried:
#   iss = https://login.microsoftonline.com/<tid>/v2.0
#   aud = <blueprint app id>
#   azp = 5a807f24-c9de-44ee-a3a7-329e88a00ffc  (Messaging Bot API SP)
#   ver = 2.0
# Discovery / JWKS now come from the AAD v2.0 OIDC document for the
# bridge's configured tenant.
AAD_V2_OPENID_CONFIG_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
)
AAD_V2_ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/v2.0"

# A365 outbound auth — three-stage `user_fic` chain. Discovered during the
# 2026-05-05 round-3 walkthrough (issue #6) when the standard BF
# `client_credentials` against api.botframework.com returned AADSTS82001
# ("Agentic application not permitted to request app-only tokens"). The
# correct flow is documented at
# https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow
# and implemented in
# microsoft-agents-authentication-msal:MsalAuth.get_agentic_user_token.
TENANT_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
FMI_TOKEN_SCOPE = "api://AzureADTokenExchange/.default"
APX_PRODUCTION_APP_ID = "5a807f24-c9de-44ee-a3a7-329e88a00ffc"  # Messaging Bot API SP
APX_PRODUCTION_SCOPE = f"{APX_PRODUCTION_APP_ID}/.default"
# Default allowlist for inbound `azp` (slice 19f). Treat the same SP we
# get outbound tokens *for* as the only sender we accept inbound *from*.
# Other A365 services may join this list as we observe them.
DEFAULT_INBOUND_AZP_ALLOWLIST: tuple[str, ...] = (APX_PRODUCTION_APP_ID,)
USER_FIC_GRANT = "user_fic"
JWT_BEARER_ASSERTION_TYPE = (
    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
)

# JWKS cache TTL per Microsoft's published guidance (refresh at least daily).
JWKS_CACHE_TTL_SECONDS = 24 * 3600

# Refresh outbound token 5 min before expiry to avoid mid-flight 401s.
TOKEN_REFRESH_SKEW_SECONDS = 300

# Webhook envelope schema version — bumped when we make a breaking change.
WEBHOOK_ENVELOPE_VERSION = "1"

# Default 10s budget on the operator webhook so a stuck responder
# doesn't tie up BF (which itself times out around 15s).
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Bridge config (loaded once at serve start)
# ---------------------------------------------------------------------------


@dataclass
class BridgeConfig:
    """Runtime config the FastAPI handlers depend on.

    Threading this through DI rather than reaching into module-level
    globals keeps the test surface small.

    ``blueprint_client_id`` and ``blueprint_client_secret`` are the
    blueprint Entra app's id + secret — used as the *bootstrap*
    credential at the start of the three-stage agentic-user token
    chain. The agent app instance id (``recipient.agentic_app_id`` on
    the inbound activity) is what the chain swaps in for steps 2-3.
    """

    slug: str
    tenant_id: str
    blueprint_client_id: str
    blueprint_client_secret: str
    webhook_url: str
    log_path: Path
    pid_path: Path
    skip_jwt_validation: bool = False
    webhook_timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS
    # Slice 19f: SPs allowed in the `azp` claim of inbound JWTs.
    inbound_azp_allowlist: tuple[str, ...] = DEFAULT_INBOUND_AZP_ALLOWLIST


def load_bridge_config(
    *,
    slug: str,
    webhook_url: str | None,
    hermes_home: Path,
    generated_config_path: Path,
    skip_jwt_validation: bool = False,
) -> BridgeConfig:
    """Resolve all runtime inputs needed to start the serve loop.

    The blueprint client id + secret live in
    ``a365.generated.config.json``. ``a365 publish`` clobbers the
    secret in the local file (CLI quirk caught in round-3); operators
    may need to re-run ``register --apply`` or
    ``az ad app credential reset`` to get a working secret back.
    """
    agent_env = load_agent_env(hermes_home, slug)
    gen = load_generated_config(generated_config_path)

    blueprint_client_id = gen.get("agentBlueprintId") or ""
    blueprint_secret = gen.get("agentBlueprintClientSecret") or ""
    if not blueprint_client_id:
        raise BridgeConfigError(
            f"{generated_config_path} has no agentBlueprintId; "
            "re-run `hermes a365 register --apply` first."
        )
    if not blueprint_secret:
        raise BridgeConfigError(
            f"{generated_config_path} has no agentBlueprintClientSecret. "
            "Common cause: `a365 publish` was run after `register` and "
            "clobbered the local secret. Recover by either re-running "
            "register --apply (with cleanup first) or `az ad app "
            "credential reset --id <blueprint-app-id>` and patching the "
            "new secret into the generated config."
        )

    resolved_webhook = webhook_url or os.environ.get("HERMES_BRIDGE_WEBHOOK") or ""
    if not resolved_webhook:
        raise BridgeConfigError(
            "HERMES_BRIDGE_WEBHOOK is not set and --webhook was not given. "
            "Point this at the operator's responder URL "
            "(see references/webhook-contract.md)."
        )

    agent_dir = hermes_home / "agents" / slug
    return BridgeConfig(
        slug=slug,
        tenant_id=agent_env["A365_TENANT_ID"],
        blueprint_client_id=blueprint_client_id,
        blueprint_client_secret=blueprint_secret,
        webhook_url=resolved_webhook,
        log_path=agent_dir / "bridge.log",
        pid_path=agent_dir / "bridge.pid",
        skip_jwt_validation=skip_jwt_validation,
    )


# ---------------------------------------------------------------------------
# JWKS / JWT validation
# ---------------------------------------------------------------------------


@dataclass
class _JwksCache:
    """Tiny TTL cache for the BF OpenID JWKS document."""

    keys_by_kid: dict[str, Any] = field(default_factory=dict)
    fetched_at: float = 0.0
    ttl_seconds: float = JWKS_CACHE_TTL_SECONDS


async def _fetch_aad_v2_keys(client: Any, *, tenant_id: str) -> dict[str, Any]:
    """Fetch the AAD v2.0 tenant JWKS via OpenID discovery.

    Returns ``{kid: PyJWK}``. Pure function — caching is handled by the
    caller so tests can substitute a fixed JWKS without touching the
    cache structure.

    Slice 19f: replaced the pre-19f BF discovery URL after a real
    A365 / MCP Platform inbound token was captured (round-3
    walkthrough, 2026-05-05) and shown to be AAD-v2-issued, not
    BF-issued.
    """
    if _jwt is None:
        raise BridgeConfigError("pyjwt not installed; run `uv sync --extra bridge`")
    config_url = AAD_V2_OPENID_CONFIG_URL_TEMPLATE.format(tenant_id=tenant_id)
    config_resp = await client.get(config_url, timeout=10.0)
    config_resp.raise_for_status()
    jwks_uri = config_resp.json()["jwks_uri"]
    jwks_resp = await client.get(jwks_uri, timeout=10.0)
    jwks_resp.raise_for_status()
    keys: dict[str, Any] = {}
    for jwk in jwks_resp.json().get("keys", []):
        kid = jwk.get("kid")
        if not kid:
            continue
        keys[kid] = _jwt.PyJWK(jwk)
    return keys


async def _ensure_jwks_loaded(
    client: Any,
    cache: _JwksCache,
    *,
    tenant_id: str,
    now: float | None = None,
) -> dict[str, Any]:
    import time as _time

    cur = now if now is not None else _time.time()
    if cache.keys_by_kid and (cur - cache.fetched_at) < cache.ttl_seconds:
        return cache.keys_by_kid
    cache.keys_by_kid = await _fetch_aad_v2_keys(client, tenant_id=tenant_id)
    cache.fetched_at = cur
    return cache.keys_by_kid


class JwtValidationError(RuntimeError):
    pass


async def validate_inbound_jwt(
    *,
    token: str,
    tenant_id: str,
    expected_app_id: str,
    azp_allowlist: tuple[str, ...] = DEFAULT_INBOUND_AZP_ALLOWLIST,
    client: Any,
    cache: _JwksCache,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate an A365 / MCP-Platform inbound JWT and return its claims.

    Slice 19f rewrite. The A365 platform issues AAD-v2 tokens directly
    to the bot endpoint:

    - ``iss`` = ``https://login.microsoftonline.com/<tenant_id>/v2.0``
    - ``aud`` = the bot's blueprint Entra app id
    - ``azp`` = the calling Microsoft service principal (e.g.
      ``5a807f24-…`` for the Messaging Bot API)
    - ``ver`` = ``2.0``

    The pre-19f validator's ``serviceUrl`` claim check is gone — A365
    tokens don't carry that claim (it's BF-specific). In its place we
    validate ``azp`` against an allowlist so a stray AAD-v2 token from
    any other Microsoft service (or a different tenant SP that happens
    to be granted our scope) cannot impersonate the platform.

    Raises :class:`JwtValidationError` on any failure with a short reason.
    Caller dispatches HTTP 401/403 from there.
    """
    if _jwt is None:
        raise BridgeConfigError("pyjwt not installed; run `uv sync --extra bridge`")
    keys = await _ensure_jwks_loaded(client, cache, tenant_id=tenant_id, now=now)
    try:
        unverified_header = _jwt.get_unverified_header(token)
    except _jwt.PyJWTError as e:
        raise JwtValidationError(f"unverified header parse failed: {e}") from e
    kid = unverified_header.get("kid")
    if not kid or kid not in keys:
        raise JwtValidationError(f"signing key (kid={kid!r}) not in JWKS")
    expected_issuer = AAD_V2_ISSUER_TEMPLATE.format(tenant_id=tenant_id)
    try:
        claims = _jwt.decode(
            token,
            key=keys[kid].key,
            algorithms=["RS256"],
            audience=expected_app_id,
            issuer=expected_issuer,
            leeway=300,
        )
    except _jwt.PyJWTError as e:
        raise JwtValidationError(f"signature/aud/iss check failed: {e}") from e
    azp = claims.get("azp")
    if not azp_allowlist:
        raise JwtValidationError(
            "azp allowlist is empty — refusing to accept any inbound token. "
            "This is a config bug; populate inbound_azp_allowlist."
        )
    if azp not in azp_allowlist:
        raise JwtValidationError(
            f"azp {azp!r} not in allowlist {list(azp_allowlist)!r}"
        )
    return claims


# ---------------------------------------------------------------------------
# Outbound auth — three-stage agentic-user-FIC chain
# ---------------------------------------------------------------------------
#
# Why we do this instead of standard `client_credentials` to api.botframework.com:
# the GA Microsoft "Agentic application" policy class (which A365 blueprint
# Entra apps land in) returns ``AADSTS82001`` for app-only tokens against any
# messaging-related resource. The canonical flow used by the
# `microsoft-agents-authentication-msal` SDK is a custom `user_fic` chain
# documented at
# https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow.
#
# Flow:
#   T1: blueprint impersonates the agent identity instance via FMI
#       (``client_credentials`` + ``fmi_path=<agent-app-instance-id>``);
#       audience ``api://AzureADTokenExchange``.
#   T2: agent identity uses T1 as its own client_assertion to mint another
#       FMI token (used as the federated-identity-credential in step 3).
#   Final: agent identity mints a user-context token at the messaging
#       resource via ``grant_type=user_fic``, with T1 as client_assertion,
#       T2 as user_federated_identity_credential, and ``user_id`` = the
#       opaque ``recipient.agentic_user_id`` from the inbound activity.
#
# Cache structure: T1/T2 are shared across users (keyed by
# ``(tenant_id, agent_app_instance_id)``); the final user-context token is
# per-user (keyed by ``(agentic_user_id, scope)``).


@dataclass
class _FmiCache:
    """Cache for the (T1, T2) pair, shared across users for one agent."""

    by_key: dict[tuple[str, str], tuple[str, str, float]] = field(default_factory=dict)
    """``{(tenant_id, agent_app_instance_id): (t1, t2, expires_at)}``"""


@dataclass
class _UserTokenCache:
    """Cache for the final per-user access token at the messaging resource."""

    by_key: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)
    """``{(agentic_user_id, scope): (access_token, expires_at)}``"""


def _agentic_ids_from_activity(activity: dict[str, Any]) -> tuple[str, str, str]:
    """Pull (tenant_id, agent_app_instance_id, agentic_user_id) from inbound.

    Microsoft's `microsoft-agents-activity` package treats these as
    fields on `recipient`; tenant_id can also live on `conversation`.
    Raise if any is missing — the bridge cannot mint an outbound token
    without all three.
    """
    recipient = activity.get("recipient") or {}
    conversation = activity.get("conversation") or {}
    tenant_id = recipient.get("tenantId") or conversation.get("tenantId") or ""
    agent_app_instance_id = recipient.get("agenticAppId") or ""
    agentic_user_id = recipient.get("agenticUserId") or ""
    missing = [
        name
        for name, val in (
            ("recipient.tenantId or conversation.tenantId", tenant_id),
            ("recipient.agenticAppId", agent_app_instance_id),
            ("recipient.agenticUserId", agentic_user_id),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"inbound activity missing agentic identifiers: {missing}. "
            "This activity wasn't routed via A365 — outbound replies "
            "require the agentic recipient fields."
        )
    return tenant_id, agent_app_instance_id, agentic_user_id


async def _post_token_request(
    client: Any, *, tenant_id: str, body: dict[str, str]
) -> dict[str, Any]:
    """POST a form-encoded token request and return the parsed JSON."""
    resp = await client.post(
        TENANT_TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id),
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


async def acquire_t1_token(
    *,
    client: Any,
    tenant_id: str,
    blueprint_client_id: str,
    blueprint_client_secret: str,
    agent_app_instance_id: str,
) -> tuple[str, float]:
    """T1: blueprint impersonates the agent identity (FMI exchange)."""
    payload = await _post_token_request(
        client,
        tenant_id=tenant_id,
        body={
            "client_id": blueprint_client_id,
            "scope": FMI_TOKEN_SCOPE,
            "fmi_path": agent_app_instance_id,
            "grant_type": "client_credentials",
            "client_secret": blueprint_client_secret,
        },
    )
    return payload["access_token"], float(payload.get("expires_in", 3600))


async def acquire_t2_token(
    *,
    client: Any,
    tenant_id: str,
    agent_app_instance_id: str,
    t1: str,
) -> tuple[str, float]:
    """T2: agent identity asserts itself using T1 as client_assertion."""
    payload = await _post_token_request(
        client,
        tenant_id=tenant_id,
        body={
            "client_id": agent_app_instance_id,
            "scope": FMI_TOKEN_SCOPE,
            "grant_type": "client_credentials",
            "client_assertion_type": JWT_BEARER_ASSERTION_TYPE,
            "client_assertion": t1,
        },
    )
    return payload["access_token"], float(payload.get("expires_in", 3600))


async def acquire_user_fic_token(
    *,
    client: Any,
    tenant_id: str,
    agent_app_instance_id: str,
    t1: str,
    t2: str,
    agentic_user_id: str,
    scope: str = APX_PRODUCTION_SCOPE,
) -> tuple[str, float]:
    """Final stage: user-context token via ``grant_type=user_fic``."""
    payload = await _post_token_request(
        client,
        tenant_id=tenant_id,
        body={
            "client_id": agent_app_instance_id,
            "scope": scope,
            "grant_type": USER_FIC_GRANT,
            "client_assertion_type": JWT_BEARER_ASSERTION_TYPE,
            "client_assertion": t1,
            "user_federated_identity_credential": t2,
            "user_id": agentic_user_id,
        },
    )
    return payload["access_token"], float(payload.get("expires_in", 3600))


async def acquire_outbound_token(
    *,
    client: Any,
    cfg: BridgeConfig,
    activity: dict[str, Any],
    fmi_cache: _FmiCache,
    user_cache: _UserTokenCache,
    scope: str = APX_PRODUCTION_SCOPE,
    now: float | None = None,
) -> str:
    """Return a bearer token for replying to ``activity`` at ``scope``.

    Runs the three-stage chain, cached two-tier (T1/T2 shared across
    users for the same agent; final per-user). Single-flight is
    deliberately not implemented yet — at scale, swap in an asyncio
    Lock keyed on the cache key.
    """
    import time as _time

    cur = now if now is not None else _time.time()
    tenant_id, agent_app_instance_id, agentic_user_id = _agentic_ids_from_activity(
        activity
    )

    # Tier-2: per-user access token at the target scope.
    user_key = (agentic_user_id, scope)
    cached = user_cache.by_key.get(user_key)
    if cached and (cur + TOKEN_REFRESH_SKEW_SECONDS) < cached[1]:
        return cached[0]

    # Tier-1: shared T1/T2.
    fmi_key = (tenant_id, agent_app_instance_id)
    fmi_cached = fmi_cache.by_key.get(fmi_key)
    if fmi_cached and (cur + TOKEN_REFRESH_SKEW_SECONDS) < fmi_cached[2]:
        t1, t2, _ = fmi_cached
    else:
        t1, t1_ttl = await acquire_t1_token(
            client=client,
            tenant_id=tenant_id,
            blueprint_client_id=cfg.blueprint_client_id,
            blueprint_client_secret=cfg.blueprint_client_secret,
            agent_app_instance_id=agent_app_instance_id,
        )
        t2, t2_ttl = await acquire_t2_token(
            client=client,
            tenant_id=tenant_id,
            agent_app_instance_id=agent_app_instance_id,
            t1=t1,
        )
        fmi_cache.by_key[fmi_key] = (t1, t2, cur + min(t1_ttl, t2_ttl))

    final, final_ttl = await acquire_user_fic_token(
        client=client,
        tenant_id=tenant_id,
        agent_app_instance_id=agent_app_instance_id,
        t1=t1,
        t2=t2,
        agentic_user_id=agentic_user_id,
        scope=scope,
    )
    user_cache.by_key[user_key] = (final, cur + final_ttl)
    return final


# ---------------------------------------------------------------------------
# Webhook forwarding
# ---------------------------------------------------------------------------


def build_webhook_envelope(activity: dict[str, Any], cfg: BridgeConfig) -> dict[str, Any]:
    """Wrap an inbound activity in our stable JSON envelope.

    Schema is pinned at ``references/webhook-contract.md``. We pass the
    activity through whole rather than picking fields — operators can
    rely on getting whatever the BF protocol gives us.
    """
    return {
        "version": WEBHOOK_ENVELOPE_VERSION,
        "agent": {
            "slug": cfg.slug,
            "tenant_id": cfg.tenant_id,
            "blueprint_client_id": cfg.blueprint_client_id,
        },
        "activity": activity,
    }


async def forward_to_webhook(
    *, envelope: dict[str, Any], cfg: BridgeConfig, client: Any
) -> dict[str, Any]:
    """POST the envelope to the operator's responder. Returns the parsed
    response. Raises ``RuntimeError`` on non-2xx so the caller can render
    a fallback Adaptive Card."""
    resp = await client.post(
        cfg.webhook_url,
        json=envelope,
        timeout=cfg.webhook_timeout_seconds,
    )
    resp.raise_for_status()
    if not resp.content:
        return {}
    return resp.json()


# ---------------------------------------------------------------------------
# Reply rendering
# ---------------------------------------------------------------------------


def render_reply_activity(
    inbound: dict[str, Any], webhook_response: dict[str, Any]
) -> dict[str, Any]:
    """Build a BF reply Activity from the operator's webhook response.

    Webhook response contract (see ``references/webhook-contract.md``):

        { "text": "<plain text>", "card": { ... }, "metadata": { ... } }

    Either ``text`` or ``card`` must be present (or both). For replies
    the bridge stamps ``type=message``, mirrors the conversation /
    recipient / from triple per BF reply convention, and forwards
    optional attachments.
    """
    reply_text = webhook_response.get("text", "")
    card = webhook_response.get("card")
    attachments = []
    if card:
        attachments.append(
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        )
    reply: dict[str, Any] = {
        "type": "message",
        "from": inbound.get("recipient", {}),
        "recipient": inbound.get("from", {}),
        "conversation": inbound.get("conversation", {}),
        "replyToId": inbound.get("id"),
    }
    if reply_text:
        reply["text"] = reply_text
    if attachments:
        reply["attachments"] = attachments
    return reply


def render_error_card(message: str) -> dict[str, Any]:
    """Adaptive Card we send when the operator's webhook errors out."""
    return {
        "type": "AdaptiveCard",
        "version": "1.6",
        "body": [
            {
                "type": "TextBlock",
                "text": "The agent backend returned an error.",
                "weight": "Bolder",
                "color": "Attention",
            },
            {"type": "TextBlock", "text": message, "wrap": True},
        ],
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    }


# ---------------------------------------------------------------------------
# Send the reply via BF connector
# ---------------------------------------------------------------------------


async def send_reply(
    *,
    inbound: dict[str, Any],
    reply: dict[str, Any],
    cfg: BridgeConfig,
    client: Any,
    fmi_cache: _FmiCache,
    user_cache: _UserTokenCache,
) -> Any:
    """POST a reply Activity to the inbound's serviceUrl.

    Auth: agentic user-FIC token (per-inbound-user) — see
    ``acquire_outbound_token``. The reply path itself is the standard
    Bot Framework REST shape; only the bearer changes vs classic BF.
    """
    service_url = inbound.get("serviceUrl", "").rstrip("/")
    conv_id = inbound.get("conversation", {}).get("id", "")
    activity_id = inbound.get("id", "")
    if not service_url or not conv_id or not activity_id:
        raise RuntimeError(
            f"reply target incomplete: serviceUrl={service_url!r}, "
            f"conversationId={conv_id!r}, activityId={activity_id!r}"
        )
    url = f"{service_url}/v3/conversations/{conv_id}/activities/{activity_id}"
    token = await acquire_outbound_token(
        client=client,
        cfg=cfg,
        activity=inbound,
        fmi_cache=fmi_cache,
        user_cache=user_cache,
    )
    return await client.post(
        url,
        json=reply,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def make_app(
    cfg: BridgeConfig,
    *,
    http_client: Any | None = None,
    jwks_cache: _JwksCache | None = None,
    fmi_cache: _FmiCache | None = None,
    user_cache: _UserTokenCache | None = None,
) -> Any:
    """Build the FastAPI app for the serve loop.

    Dependencies are passed in for testability — callers (tests) inject
    fakes; production passes ``None`` and we construct real ones.
    """
    if _FastAPI is None or _httpx is None:
        raise BridgeConfigError(
            "serve mode requires the bridge extras: `uv sync --extra bridge`"
        )

    if http_client is None:
        http_client = _httpx.AsyncClient()
    if jwks_cache is None:
        jwks_cache = _JwksCache()
    if fmi_cache is None:
        fmi_cache = _FmiCache()
    if user_cache is None:
        user_cache = _UserTokenCache()

    app = _FastAPI(title=f"hermes a365 activity-bridge — {cfg.slug}")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "slug": cfg.slug,
            "blueprint_client_id": cfg.blueprint_client_id[:8] + "…",
        }

    @app.post("/api/messages")
    async def messages(
        activity: dict[str, Any] = _Body(...),  # noqa: B008 — FastAPI idiom
        authorization: str | None = _Header(default=None),
    ) -> Any:
        # JWT validation — the inbound `Authorization: Bearer <token>` header.
        # Slice 19f: AAD-v2 issuer + JWKS for the bridge's tenant; azp
        # allowlist replaces the BF-specific serviceUrl claim check.
        if not cfg.skip_jwt_validation:
            if not authorization or not authorization.lower().startswith("bearer "):
                raise _HTTPException(status_code=401, detail="missing bearer token")
            token = authorization.split(None, 1)[1]
            try:
                await validate_inbound_jwt(
                    token=token,
                    tenant_id=cfg.tenant_id,
                    expected_app_id=cfg.blueprint_client_id,
                    azp_allowlist=cfg.inbound_azp_allowlist,
                    client=http_client,
                    cache=jwks_cache,
                )
            except JwtValidationError as e:
                raise _HTTPException(status_code=403, detail=str(e)) from e

        activity_type = activity.get("type", "message")

        # Channel-control activities — ack and bail.
        if activity_type in ("conversationUpdate", "typing", "endOfConversation"):
            return _JSONResponse({"status": "acked"})

        # Forward to operator webhook.
        envelope = build_webhook_envelope(activity, cfg)
        try:
            webhook_resp = await forward_to_webhook(
                envelope=envelope, cfg=cfg, client=http_client
            )
        except Exception as e:
            error_reply = render_reply_activity(
                activity,
                {"text": "", "card": render_error_card(f"Webhook error: {e}")},
            )
            await send_reply(
                inbound=activity,
                reply=error_reply,
                cfg=cfg,
                client=http_client,
                fmi_cache=fmi_cache,
                user_cache=user_cache,
            )
            return _JSONResponse({"status": "webhook_error"}, status_code=200)

        # Invoke replies must be synchronous: return the invokeResponse body
        # in this HTTP turn.
        if activity_type == "invoke":
            invoke_response = webhook_resp.get("invokeResponse") or {
                "status": 200,
                "body": webhook_resp,
            }
            return _JSONResponse(invoke_response)

        # Standard message: render reply and send asynchronously via serviceUrl.
        if not webhook_resp.get("text") and not webhook_resp.get("card"):
            return _JSONResponse({"status": "no_reply"})
        reply = render_reply_activity(activity, webhook_resp)
        await send_reply(
            inbound=activity,
            reply=reply,
            cfg=cfg,
            client=http_client,
            fmi_cache=fmi_cache,
            user_cache=user_cache,
        )
        return _JSONResponse({"status": "replied"})

    return app


# ---------------------------------------------------------------------------
# PID file lifecycle
# ---------------------------------------------------------------------------


def write_pid_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))
    os.chmod(path, 0o600)


def remove_pid_file(path: Path) -> None:
    import contextlib

    with contextlib.suppress(FileNotFoundError):
        path.unlink()


# ---------------------------------------------------------------------------
# `serve` and `update-endpoint` CLI handlers
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        cfg = load_bridge_config(
            slug=args.slug,
            webhook_url=args.webhook,
            hermes_home=_resolve_hermes_home(),
            generated_config_path=(
                args.generated_config or Path.cwd() / "a365.generated.config.json"
            ),
            skip_jwt_validation=bool(args.no_jwt_validation),
        )
    except BridgeConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    import uvicorn  # type: ignore[import-not-found]

    write_pid_file(cfg.pid_path)
    sys.stdout.write(
        f"hermes a365 activity-bridge serving {cfg.slug} "
        f"on http://{args.host}:{args.port}\n"
        f"  webhook: {cfg.webhook_url}\n"
        f"  pid:     {cfg.pid_path}\n"
        f"  log:     {cfg.log_path}\n"
        f"  jwt:     {'DISABLED — DEV ONLY' if cfg.skip_jwt_validation else 'enabled'}\n"
        f"  outbound auth: agentic user-FIC (3-stage chain) → "
        f"{APX_PRODUCTION_SCOPE.split('/')[0][:8]}…\n"
    )
    sys.stdout.flush()

    app = make_app(cfg)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        remove_pid_file(cfg.pid_path)
    return 0


def cmd_update_endpoint(args: argparse.Namespace) -> int:
    """Wrap ``a365 setup blueprint --m365 --update-endpoint <url>``."""
    if not args.url.startswith("https://"):
        print(
            f"ERROR: --url must be HTTPS (got {args.url!r}); BF requires TLS",
            file=sys.stderr,
        )
        return 2

    argv = ["a365", "setup", "blueprint", "--agent-name", args.agent_name]
    if not args.no_m365:
        argv.append("--m365")
    argv.extend(["--update-endpoint", args.url])
    if args.tenant_id:
        argv.extend(["--tenant-id", args.tenant_id])

    sys.stdout.write(f"[plan] $ {' '.join(argv)}\n")
    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to execute.\n")
        return 0

    # Lazy import — keeps the verify path mutator-free.
    from mutator import AADSTSError, CliInvocationError, get_mutator

    mutator = get_mutator()
    if not mutator.available:
        print("ERROR: a365 CLI not on PATH", file=sys.stderr)
        return 2
    try:
        mutator.run(argv)
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except CliInvocationError as e:
        # Issue #140 cleanup-then-recreate path.
        if "already exists" in (e.output or ""):
            sys.stdout.write(
                "\n[recover] update-endpoint hit a duplicate-name error "
                "(Agent365-devTools issue #140). Falling back to "
                "cleanup --endpoint-only then re-applying.\n"
            )
            try:
                mutator.run(
                    [
                        "a365",
                        "cleanup",
                        "blueprint",
                        "--agent-name",
                        args.agent_name,
                        "--endpoint-only",
                    ],
                    stdin_input="y\n",
                )
                mutator.run(argv)
            except (AADSTSError, CliInvocationError) as e2:
                print(f"ERROR: fallback failed: {e2}", file=sys.stderr)
                return 2
        else:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    sys.stdout.write("done.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
