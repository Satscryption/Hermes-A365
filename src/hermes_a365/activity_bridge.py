"""hermes a365 activity-bridge — A365 / MCP-Platform webhook adapter daemon.

Three subcommands:

- ``verify`` (slice 19a): one-shot diagnostic. Validates the registered
  blueprint's client secret can acquire an OAuth token, that AAD +
  Graph are reachable, and that the per-agent config + generated-config
  files parse cleanly. Useful for CI gates and pre-deploy smoke.
- ``serve`` (slice 19b): long-running webhook adapter. FastAPI app on a
  local port that operators expose via ``cloudflared`` / reverse proxy.
  Receives A365 activities, forwards each one to
  ``HERMES_BRIDGE_WEBHOOK`` with our stable JSON envelope, renders the
  webhook's JSON response as an Adaptive Card or plain text, and
  replies via ``serviceUrl`` using the agentic three-stage user-FIC
  token chain.
- ``update-endpoint`` (slice 19b): thin wrapper around
  ``a365 setup blueprint --m365 --update-endpoint <url>`` so operators
  can pin the agent's messaging endpoint to a tunnel URL with one
  command. Includes the cleanup-then-recreate fallback for the known
  duplicate-name error (Agent365-devTools issue #140).

Per-agent state files (serve mode)::

    ~/.hermes/agents/<slug>/bridge.pid    # daemon PID, removed on shutdown
    ~/.hermes/agents/<slug>/bridge.log    # append-only operational log

Auth wiring (validated end-to-end against the satscryption tenant
2026-05-05, round-4 walkthrough):

- **Inbound JWT** (slice 19f, supersedes the pre-19f BF-issuer
  expectation): A365 / MCP Platform issues AAD v2.0 tokens directly
  to the bot endpoint. Validator pulls JWKS from the AAD v2.0 OIDC
  document for the configured tenant
  (``https://login.microsoftonline.com/<tid>/v2.0/.well-known/openid-configuration``),
  enforces ``iss == https://login.microsoftonline.com/<tid>/v2.0``,
  ``aud == <blueprint app id>``, RS256, 5-min skew. There is **no**
  ``serviceUrl`` claim on A365 tokens (it's BF-specific) — the
  pre-19f claim check is gone.
- **`azp` allowlist** (slice 19f, defense-in-depth): a valid AAD-v2
  token from a different sender SP must not be accepted just because
  it carries our ``aud``. ``BridgeConfig.inbound_azp_allowlist``
  defaults to ``(5a807f24-c9de-44ee-a3a7-329e88a00ffc,)`` — the
  Messaging Bot API SP, the same SP we already target for outbound.
- **Inbound idempotency** (slice 19i): TTL-keyed dedupe on
  ``(conversationId, activityId)`` short-circuits BF / A365 connector
  retries. Default TTL 1h via ``BridgeConfig.idempotency_ttl_seconds``.
  Activities without an ``id`` (some channel-control flows) bypass
  dedupe — better to over-deliver than to silently drop on missing id.
- **Inbound serviceUrl gate** (slice 19j): the inbound activity's
  ``serviceUrl`` must be HTTPS with a hostname ending in one of
  ``BridgeConfig.trusted_service_url_suffixes`` before the bridge
  mints any outbound bearer. Default suffixes:
  ``.trafficmanager.net`` (the load-bearing one — observed on real
  Teams traffic), ``.botframework.com``, ``.botframework.us``,
  ``.cloud.microsoft``, ``.azure.com``. Empty allowlist refuses all
  (treated as a config bug).
- **Outbound auth** (slice 19e, supersedes the pre-19e BF
  ``client_credentials`` flow that AADSTS82001'd for A365 agentic
  apps — see issue #6 for the upstream defect): three-stage agentic
  user-FIC chain per
  https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow.
  T1 = blueprint impersonates the agent identity via FMI; T2 = agent
  identity asserts itself; final = user-context token at the
  Messaging Bot API resource (``5a807f24-…/.default``). Two-tier
  cache (T1/T2 shared across users; final per-user). Reply POSTs to
  ``{serviceUrl}/v3/conversations/{conv}/activities/{activity}``.

CLI use::

    hermes-a365 activity-bridge verify --slug inbox-helper
    hermes-a365 activity-bridge serve  --slug inbox-helper --port 3978
    hermes-a365 activity-bridge update-endpoint --slug X --url https://...
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

from ._common import parse_env, tcp_reachable

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

# Resource appIds for token acquisition. The blueprint SP only ever
# gets one S2S app-role assignment (``Agent365Observability``) — this
# is intended behaviour per Microsoft's 2026-05-05 reply on
# microsoft/Agent365-devTools#402, NOT a CLI defect. The Messaging
# Bot API and Power Platform API resources are configured via
# delegated OAuth2 grants only. Probing Observability with
# ``client_credentials`` validates the secret + the only S2S grant
# the GA CLI assigns. ``client_credentials`` to other resources is
# expected to fail with ``AADSTS7000218`` (no app role) — that's
# still positive evidence the secret works, not a missing-grant
# signal.
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


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
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

    return parser


def run(args: argparse.Namespace) -> int:
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

    print(f"ERROR: unknown command: {args.cmd}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


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

# Path B outbound auth — classic Bot Framework S2S (#33, slice 20e).
# Standard `client_credentials` grant against the bot's own tenant token
# endpoint (SingleTenant bot resources use the per-tenant URL per
# https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication
# "Authenticate requests from your bot to the Bot Connector service" §single-tenant).
# Scope is the Bot Connector resource's `.default` — same audience the
# inbound Connector→Bot tokens claim, so we're talking back to the same
# service that called us.
BF_S2S_SCOPE = "https://api.botframework.com/.default"
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

# Slice 19i: how long the bridge remembers a delivered (conversationId,
# activityId) pair to short-circuit BF / A365 retries. 1h matches the
# upper bound on Microsoft's connector retry window we've observed in
# the wild. Configurable via ``BridgeConfig.idempotency_ttl_seconds``.
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 3600.0

# Slice 19j: only POST outbound replies to ``serviceUrl`` hosts whose
# DNS suffix is on this list. The bridge mints a user-FIC bearer for
# the Messaging Bot API SP and ships it to whatever URL the inbound
# activity carries; without an allowlist a forged activity could
# steer that bearer's traffic anywhere. Suffix list adapted from
# NousResearch/hermes-agent#10037's ``TRUSTED_SERVICE_URL_HOST_SUFFIXES``;
# the round-3 walkthrough confirmed real Teams traffic lands on
# ``smba.trafficmanager.net`` so ``.trafficmanager.net`` is the
# load-bearing entry. ``.cloud.microsoft`` and ``.botframework.com``
# are kept for cross-channel coverage; ``.azure.com`` covers Bot
# Service-hosted endpoints.
DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES: tuple[str, ...] = (
    ".trafficmanager.net",
    ".botframework.com",
    ".botframework.us",
    ".cloud.microsoft",
    ".azure.com",
)

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

    ``bf_app_id`` / ``bf_client_secret`` (#36) are the optional
    separate non-agentic Entra app used as the Azure Bot Service
    identity for Path B. The blueprint app's Agentic-application
    policy class refuses ``client_credentials`` for BF resources
    (AADSTS82001), so Path B outbound and inbound both need a
    non-agentic identity when the operator configures Path B end-to-end.
    Leaving these empty means the wrapper falls back to the blueprint
    credentials for Path B, which fails AADSTS82001 at outbound mint
    — the gateway logs that explicitly pointing at #36's operator
    walk.
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
    # Slice 19i: TTL for in-memory dedupe of inbound activities.
    idempotency_ttl_seconds: float = DEFAULT_IDEMPOTENCY_TTL_SECONDS
    # Slice 19j: DNS suffixes acceptable on the inbound `serviceUrl`.
    trusted_service_url_suffixes: tuple[str, ...] = (
        DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES
    )
    # #36: optional separate Path B identity (non-agentic). When set,
    # used as `expected_app_id` for inbound BF JWT validation AND as
    # the `client_id` / `client_secret` for outbound BF S2S mint.
    bf_app_id: str = ""
    bf_client_secret: str = ""


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
    # #36: optional Path B identity. Empty defaults match Path A-only
    # operators; setting both fields enables the BF S2S mint against
    # a non-agentic Entra app.
    bf_app_id = agent_env.get("A365_BF_APP_ID", "")
    bf_client_secret = agent_env.get("A365_BF_CLIENT_SECRET", "")
    return BridgeConfig(
        slug=slug,
        tenant_id=agent_env["A365_TENANT_ID"],
        blueprint_client_id=blueprint_client_id,
        blueprint_client_secret=blueprint_secret,
        webhook_url=resolved_webhook,
        log_path=agent_dir / "bridge.log",
        pid_path=agent_dir / "bridge.pid",
        skip_jwt_validation=skip_jwt_validation,
        bf_app_id=bf_app_id,
        bf_client_secret=bf_client_secret,
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


# Slice 19i: in-memory dedupe of inbound BF / A365 activity deliveries.
# BF's connector retries on 5xx and slow ACK; without dedupe we forward
# the same user message to the operator webhook multiple times. Pattern
# adapted from NousResearch/hermes-agent#10037.
@dataclass
class _IdempotencyCache:
    """TTL-keyed dedupe of ``conversationId:activityId`` pairs."""

    seen: dict[str, float] = field(default_factory=dict)
    ttl_seconds: float = DEFAULT_IDEMPOTENCY_TTL_SECONDS

    def is_duplicate(self, delivery_id: str, *, now: float | None = None) -> bool:
        """Return True if ``delivery_id`` was seen within the TTL.

        Side effect: records ``delivery_id`` as seen on first call and
        prunes expired entries opportunistically. Pure-function callers
        should use :meth:`peek` instead.
        """
        import time as _time

        cur = now if now is not None else _time.time()
        # Prune-on-check keeps the dict bounded even on long-running
        # bridges with many short conversations.
        self.seen = {
            key: seen_at
            for key, seen_at in self.seen.items()
            if cur - seen_at < self.ttl_seconds
        }
        if delivery_id in self.seen:
            return True
        self.seen[delivery_id] = cur
        return False


def _activity_delivery_id(activity: dict[str, Any]) -> str | None:
    """Compose the dedupe key from an inbound activity.

    Returns ``None`` when either id is missing — channel-control
    activities (``conversationUpdate``, ``typing``) sometimes lack
    ``id``, and we'd rather always-deliver them than risk dropping
    legitimate traffic.
    """
    conv = (activity.get("conversation") or {}).get("id") if isinstance(
        activity.get("conversation"), dict
    ) else None
    activity_id = activity.get("id")
    if not conv or not activity_id:
        return None
    return f"{conv}:{activity_id}"


def _is_trusted_service_url(url: str, suffixes: tuple[str, ...]) -> bool:
    """Slice 19j: True iff ``url`` is https + hostname ends with one of
    the configured DNS suffixes.

    Empty or missing URL → False (caller dispatches the 4xx). Empty
    ``suffixes`` is a config bug; caller checks for it separately so
    the failure mode is explicit ("refusing to ship outbound" rather
    than "silently accepted").
    """
    if not url or not suffixes:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    for suffix in suffixes:
        if not suffix:
            continue
        if hostname.endswith(suffix.lower()):
            return True
    return False


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
# Path B inbound auth — classic Bot Framework S2S tokens (#34, slice 20-pre)
# ---------------------------------------------------------------------------
#
# Microsoft's Bot Connector signs activities with classic BF S2S tokens. The
# validation rules are fixed and documented at
# https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication
# (Connector-to-Bot section):
#
#   - iss = https://api.botframework.com  (pinned; not tenant-scoped)
#   - aud = bot's MSA App ID              (the blueprint app id for us)
#   - alg = RS256
#   - serviceUrl claim must match the activity's serviceUrl field
#   - JWKS via the BF OpenID Connect discovery URL below (static; cache 24h)
#
# Differences from the A365 path (slice 19f):
#   - issuer is BF's static `api.botframework.com`, not the AAD-v2 tenant URL
#   - JWKS lives at a different discovery URL (login.botframework.com)
#   - no `azp` check — issuer pin already proves Microsoft signed it; classic
#     BF tokens don't carry a meaningful `azp` for connector→bot direction
#   - `serviceUrl` claim match IS required (A365 tokens don't carry that
#     claim — slice 19f explicitly removed it on the A365 path).
#
# Phase 2 walk 2026-05-14 against Hermes-A365#28 confirmed Path B inbound
# fails A365-shape validation deterministically — Direct Line probe returns
# `BotError / Failed to send activity / 403` from BF service after our gateway
# rejects every token. This validator is the fix.
BF_OPENID_CONFIG_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BF_ISSUER = "https://api.botframework.com"


async def _fetch_bf_jwks_keys(client: Any) -> dict[str, Any]:
    """Fetch the Bot Framework Connector JWKS via OpenID discovery.

    Returns ``{kid: PyJWK}``. Pure function — caching handled by the
    caller so tests can substitute a fixed JWKS via httpx MockTransport
    the same way slice 19f's A365 fetcher tests do.

    Unlike the A365 path, the BF discovery URL is static (not
    tenant-scoped) and the issuer is hard-coded
    (``https://api.botframework.com``). Microsoft documents this as
    safe to hardcode.
    """
    if _jwt is None:
        raise BridgeConfigError("pyjwt not installed; run `uv sync --extra bridge`")
    config_resp = await client.get(BF_OPENID_CONFIG_URL, timeout=10.0)
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


async def _ensure_bf_jwks_loaded(
    client: Any,
    cache: _JwksCache,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Mirror of ``_ensure_jwks_loaded`` for the BF JWKS. Separate cache
    instance because BF and AAD-v2 have different issuers + tenants and
    we don't want to merge two key sources behind one ``kid``-keyed
    dict (a kid collision across issuers is improbable but the kind of
    failure shape we'd rather not have).
    """
    import time as _time

    cur = now if now is not None else _time.time()
    if cache.keys_by_kid and (cur - cache.fetched_at) < cache.ttl_seconds:
        return cache.keys_by_kid
    cache.keys_by_kid = await _fetch_bf_jwks_keys(client)
    cache.fetched_at = cur
    return cache.keys_by_kid


def peek_unverified_iss(token: str) -> str | None:
    """Read the JWT's ``iss`` claim without verifying the signature.

    Used by the route dispatcher to pick A365 vs BF validator without
    paying for two full JWKS fetches. Returns ``None`` for any
    parse failure (caller falls through to the A365 path, which will
    do a real signature check and reject malformed tokens itself —
    this peek is a routing hint, not a security gate).
    """
    if _jwt is None:
        return None
    try:
        claims = _jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iss": False,
            },
        )
    except Exception:
        # Any decode failure → no signal; caller falls through to A365 path.
        return None
    iss = claims.get("iss")
    if isinstance(iss, str):
        return iss
    return None


async def validate_inbound_jwt_bf(
    *,
    token: str,
    expected_app_id: str,
    expected_service_url: str,
    client: Any,
    cache: _JwksCache,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate a classic Bot Framework Connector-to-Bot S2S token.

    Path B inbound (#34, slice 20-pre). Mirrors the shape of
    :func:`validate_inbound_jwt` (slice 19f) but against the BF
    issuer + BF JWKS + a `serviceUrl` claim match instead of an
    `azp` allowlist.

    Raises :class:`JwtValidationError` on any failure with a short
    reason. Caller dispatches HTTP 401/403 from there.
    """
    if _jwt is None:
        raise BridgeConfigError("pyjwt not installed; run `uv sync --extra bridge`")
    keys = await _ensure_bf_jwks_loaded(client, cache, now=now)
    try:
        unverified_header = _jwt.get_unverified_header(token)
    except _jwt.PyJWTError as e:
        raise JwtValidationError(f"unverified header parse failed: {e}") from e
    kid = unverified_header.get("kid")
    if not kid or kid not in keys:
        raise JwtValidationError(f"signing key (kid={kid!r}) not in BF JWKS")
    try:
        claims = _jwt.decode(
            token,
            key=keys[kid].key,
            algorithms=["RS256"],
            audience=expected_app_id,
            issuer=BF_ISSUER,
            leeway=300,
        )
    except _jwt.PyJWTError as e:
        raise JwtValidationError(f"BF signature/aud/iss check failed: {e}") from e
    # Microsoft's BF docs (requirement 7 at
    # /azure/bot-service/rest-api/bot-framework-rest-connector-authentication)
    # say "the token contains a 'serviceUrl' claim with value that
    # matches the `serviceUrl` property at the root of the Activity
    # object", and the bot must 403 on mismatch. **Reality differs:**
    # the 2026-05-15 Phase 2 walk (#34) found that real
    # Connector→Bot tokens issued by Microsoft's BF service for
    # Direct Line traffic to a SingleTenant Bot Service registration
    # don't carry the `serviceUrl` claim at all — only `aud`, `iss`,
    # `exp`, `nbf`, signing-key-id, with no `serviceUrl` field.
    #
    # The defensible posture is: validate the claim IF it's present
    # (defend against forged-routing attacks where the claim *is*
    # set but doesn't match), but don't reject on absence (which
    # would block every real BF Connector inbound). The issuer pin
    # (`api.botframework.com`) + signature check already prove the
    # token came from Microsoft's BF service; if Microsoft tightens
    # the protocol later to require the claim, this validator picks
    # up the stricter shape automatically.
    token_service_url = claims.get("serviceUrl")
    if (
        isinstance(token_service_url, str)
        and token_service_url
        and token_service_url != expected_service_url
    ):
        raise JwtValidationError(
            f"BF token serviceUrl {token_service_url!r} does not match "
            f"activity serviceUrl {expected_service_url!r}"
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


@dataclass
class _BfTokenCache:
    """Cache for the Path B (classic Bot Framework S2S) outbound token (#33).

    Path B uses a simpler ``client_credentials`` flow than Path A's
    user-FIC chain — bot identity is shared across every conversation,
    so the cache key is just ``(tenant_id, scope)`` with no per-user
    dimension. Microsoft's BF Connector accepts the same bearer for
    every conversation the bot is registered for, so this cache is
    typically a single entry per process.
    """

    by_key: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)
    """``{(tenant_id, scope): (access_token, expires_at)}``"""


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
# Path B outbound — classic Bot Framework S2S `client_credentials` (#33)
# ---------------------------------------------------------------------------


async def acquire_bf_s2s_token(
    *,
    client: Any,
    tenant_id: str,
    blueprint_client_id: str,
    blueprint_client_secret: str,
    bf_cache: _BfTokenCache,
    scope: str = BF_S2S_SCOPE,
    now: float | None = None,
) -> str:
    """Mint a classic Bot Framework S2S bearer for Path B outbound.

    Standard ``client_credentials`` against the bot's tenant token
    endpoint (SingleTenant bot resources). Cached per
    ``(tenant_id, scope)``; refresh inside
    ``TOKEN_REFRESH_SKEW_SECONDS`` of expiry. No per-user dimension —
    Path B uses the bot's own identity for every conversation, so one
    cached entry serves the whole gateway process.

    ⚠️ **AADSTS82001 caveat (#33 walk, 2026-05-15).** If the Entra
    app you pass as ``blueprint_client_id`` is classified as an
    Agentic application by Microsoft's policy class (every blueprint
    app registered via Path A's ``setup blueprint`` flow falls into
    this category), Microsoft refuses to issue app-only tokens for
    any BF-family resource — `AADSTS82001: Agentic application is
    not permitted to request app-only tokens`. The token mint fails
    with HTTP 400. **Workaround**: register a SEPARATE non-agentic
    Entra app, grant it admin consent for ``Bot.Connector``, and pass
    that app's client id + secret here instead. The wrapper's
    follow-up issue tracks the operator-side identity registration
    walk; until that lands, Path B outbound is wrapper-code-complete
    but runtime-blocked on the identity shape.

    Phase 2 walk 2026-05-15 (#33) confirmed the BF S2S mint argv is
    correct end-to-end against Microsoft's token endpoint — it just
    fails the Entra-side policy check on agentic apps.
    """
    import time as _time

    cur = now if now is not None else _time.time()
    cache_key = (tenant_id, scope)
    cached = bf_cache.by_key.get(cache_key)
    if cached and (cur + TOKEN_REFRESH_SKEW_SECONDS) < cached[1]:
        return cached[0]

    # Use the lower-level post + parse explicitly so we can detect
    # AADSTS82001 and re-raise with a clear pointer for operators.
    if _httpx is None:
        raise BridgeConfigError(
            "httpx not installed; run `uv sync --extra bridge`"
        )
    resp = await client.post(
        TENANT_TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id),
        data={
            "client_id": blueprint_client_id,
            "scope": scope,
            "grant_type": "client_credentials",
            "client_secret": blueprint_client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    if resp.status_code >= 400:
        # Try to surface AADSTS82001 specifically — it has a fix that
        # 'just a 400' doesn't.
        try:
            body = resp.json()
            aadsts = body.get("error_codes") or []
            desc = body.get("error_description") or ""
        except Exception:
            body = None
            aadsts = []
            desc = ""
        if 82001 in aadsts:
            raise TokenAcquisitionError(
                "AADSTS82001",
                f"BF S2S mint failed: app {blueprint_client_id!r} "
                "is classified as an Agentic application by Microsoft's "
                "policy and cannot mint app-only tokens for BF resources. "
                "Path B outbound needs a SEPARATE non-agentic Entra app — "
                "register one per #36 and set `A365_BF_APP_ID` + "
                "`A365_BF_CLIENT_SECRET` in your operator .env. "
                f"Microsoft response: {desc!r}",
            )
        resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    ttl = float(payload.get("expires_in", 3600))
    bf_cache.by_key[cache_key] = (token, cur + ttl)
    return token


def _inbound_path_tag(
    activity: dict[str, Any],
    *,
    trusted_bf_suffixes: tuple[str, ...] = (
        ".botframework.com",
        ".trafficmanager.net",
    ),
) -> str:
    """Classify an inbound activity as ``"A"``, ``"B"``, or ``"unknown"``.

    Path A: recipient carries both ``agenticAppId`` and ``agenticUserId``
    (Microsoft A365 agentic-user routing signal) + a tenantId is
    derivable. Anything that ``_agentic_ids_from_activity`` would accept.

    Path B: agentic ids absent AND ``serviceUrl`` host suffix matches a
    classic Bot Framework destination (``.botframework.com`` or
    ``.trafficmanager.net`` — the Direct Line, Test in Web Chat, and
    Teams channel serviceUrl shapes Microsoft uses today).

    ``"unknown"``: the safe fallback. Callers raise rather than guess.
    """
    recipient = activity.get("recipient") or {}
    conversation = activity.get("conversation") or {}
    has_agentic = bool(
        (recipient.get("tenantId") or conversation.get("tenantId"))
        and recipient.get("agenticAppId")
        and recipient.get("agenticUserId")
    )
    if has_agentic:
        return "A"
    service_url = activity.get("serviceUrl") or ""
    if isinstance(service_url, str) and service_url:
        parsed = urlparse(service_url)
        host = (parsed.hostname or "").lower()
        for suffix in trusted_bf_suffixes:
            if host.endswith(suffix.lower()):
                return "B"
    return "unknown"


async def acquire_reply_token(
    *,
    client: Any,
    cfg: BridgeConfig,
    activity: dict[str, Any],
    fmi_cache: _FmiCache,
    user_cache: _UserTokenCache,
    bf_cache: _BfTokenCache,
    scope_a: str = APX_PRODUCTION_SCOPE,
    scope_b: str = BF_S2S_SCOPE,
    now: float | None = None,
) -> tuple[str, str]:
    """Dispatch outbound token mint by inbound path.

    Returns ``(bearer_token, path_tag)`` where ``path_tag`` is ``"A"``
    or ``"B"``. ``"unknown"`` raises — callers must not POST a reply
    with the wrong audience or BF will silently drop it.

    Path A → ``acquire_outbound_token`` (three-stage user-FIC chain
    against the Messaging Bot API SP).
    Path B → ``acquire_bf_s2s_token`` (``client_credentials`` against
    the Bot Connector resource).

    This is the single dispatch site for outbound token minting. All
    five caller surfaces — ``send_reply`` (serve mode),
    ``Agent365Adapter.send`` cached-inbound branch,
    ``_send_proactive``, ``_send_stream_start`` /
    ``_post_activity`` / ``edit_message`` — funnel through here so
    Path A vs Path B is decided in exactly one place.
    """
    path_tag = _inbound_path_tag(activity)
    if path_tag == "A":
        token = await acquire_outbound_token(
            client=client,
            cfg=cfg,
            activity=activity,
            fmi_cache=fmi_cache,
            user_cache=user_cache,
            scope=scope_a,
            now=now,
        )
        return token, "A"
    if path_tag == "B":
        # #36: use the separate non-agentic identity when set,
        # else fall back to the (agentic) blueprint creds — which
        # then fail AADSTS82001 with an operator-actionable error
        # pointing at #36's identity-registration walk.
        if cfg.bf_app_id and cfg.bf_client_secret:
            bf_client_id = cfg.bf_app_id
            bf_secret = cfg.bf_client_secret
        else:
            bf_client_id = cfg.blueprint_client_id
            bf_secret = cfg.blueprint_client_secret
        token = await acquire_bf_s2s_token(
            client=client,
            tenant_id=cfg.tenant_id,
            blueprint_client_id=bf_client_id,
            blueprint_client_secret=bf_secret,
            bf_cache=bf_cache,
            scope=scope_b,
            now=now,
        )
        return token, "B"
    raise RuntimeError(
        "cannot classify inbound activity as Path A or Path B: "
        f"recipient={activity.get('recipient')!r} "
        f"serviceUrl={activity.get('serviceUrl')!r}. "
        "Path A requires recipient.agenticAppId + recipient.agenticUserId; "
        "Path B requires a classic-BF serviceUrl host suffix "
        "(.botframework.com or .trafficmanager.net)."
    )


# ---------------------------------------------------------------------------
# TokenFactory — slice 19w-b (#18)
# ---------------------------------------------------------------------------


class TokenFactory:
    """Mint OUTBOUND TOOL tokens (Graph / another app) a per-invoke-name
    handler needs — distinct from the reply/serviceUrl token
    ``acquire_reply_token`` mints.

    Path A vs Path B selection reuses ``_inbound_path_tag`` — the SAME
    classifier ``acquire_reply_token`` uses — so it does NOT re-derive the
    token chain. In v0.8.0 no shipped invoke handler needs a tool token
    (``task/fetch`` is a pure card echo), so this is the landed *foundation
    seam* that v0.8.1 tool-backed names (``signin/tokenExchange``,
    ``fileConsent/invoke``, a Graph-backed ``search``) plug into without
    re-plumbing. ``for_graph`` is implemented — Graph is the one resource that
    works on both paths (it is exempt from the AADSTS82001 app-only limit);
    ``for_app`` and ``for_workiq`` are hooks their consuming slices implement.
    """

    def __init__(
        self,
        *,
        client: Any,
        cfg: BridgeConfig,
        fmi_cache: _FmiCache,
        user_cache: _UserTokenCache,
        bf_cache: _BfTokenCache,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._fmi_cache = fmi_cache
        self._user_cache = user_cache
        self._bf_cache = bf_cache

    async def for_graph(
        self, activity: dict[str, Any], *, now: float | None = None
    ) -> str:
        """Mint a Microsoft Graph bearer for the inbound activity's identity.

        Path A → the three-stage user-FIC chain (delegated, user-context) at
        the Graph scope. Path B → an app-only ``client_credentials`` token
        (Graph is exempt from AADSTS82001, so the Path B identity mints it
        directly). Raises for an unclassifiable activity rather than guessing.
        """
        path_tag = _inbound_path_tag(activity)
        if path_tag == "A":
            return await acquire_outbound_token(
                client=self._client,
                cfg=self._cfg,
                activity=activity,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                scope=f"{GRAPH_RESOURCE}/.default",
                now=now,
            )
        if path_tag == "B":
            payload = acquire_token(
                tenant_id=self._cfg.tenant_id,
                client_id=self._cfg.bf_app_id or self._cfg.blueprint_client_id,
                client_secret=(
                    self._cfg.bf_client_secret or self._cfg.blueprint_client_secret
                ),
                resource=GRAPH_RESOURCE,
            )
            return str(payload["access_token"])
        raise RuntimeError(
            "cannot classify inbound activity as Path A or Path B for a Graph "
            f"token: recipient={activity.get('recipient')!r} "
            f"serviceUrl={activity.get('serviceUrl')!r}"
        )

    async def for_app(
        self,
        activity: dict[str, Any],
        target_resource: str,
        *,
        now: float | None = None,
    ) -> str:
        """Mint a token for an arbitrary tool app — lands with the v0.8.1
        tool-backed invoke names (signin/tokenExchange, fileConsent) that need
        it. A Path B non-Graph resource hits AADSTS82001 on the agentic app,
        which that slice surfaces as an operator-actionable error."""
        raise NotImplementedError(
            "TokenFactory.for_app lands with the v0.8.1 tool-backed invoke names"
        )

    async def for_workiq(self, server: Any) -> str:
        """Work IQ V2 token bootstrap — a #21 hook, not implemented in v0.8.0."""
        raise NotImplementedError(
            "TokenFactory.for_workiq (Work IQ V2 token bootstrap) is #21"
        )


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


# #73(a): the BF/Teams "AI generated" content label. Attached to every
# outbound *message* activity this adapter emits (replies, coalesced
# Copilot-Chat flush, cards, streaming-final, proactive). It is inert on
# channels that don't render it, and the inbound is shape-indistinguishable
# between Teams group chat and Copilot Chat, so it is always-on rather than
# channel-gated. NOT attached to typing/intermediate-streaming activities.
# Append a fresh ``dict(...)`` copy per activity to avoid shared-mutable
# aliasing across sends.
AI_GENERATED_CONTENT_ENTITY: dict[str, Any] = {
    "type": "https://schema.org/Message",
    "@type": "Message",
    "@context": "https://schema.org",
    "additionalType": ["AIGeneratedContent"],
}


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
    # #73(a): label the reply as AI-generated content.
    reply["entities"] = [dict(AI_GENERATED_CONTENT_ENTITY)]
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


_REPLY_ERROR_EXCERPT_CHARS = 500


class ReplyPostError(RuntimeError):
    """Raised when the Bot Framework reply endpoint rejects a reply POST."""

    def __init__(
        self,
        *,
        status_code: int,
        url: str,
        body_excerpt: str,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.body_excerpt = body_excerpt
        detail = f"reply POST failed: HTTP {status_code} from {url}"
        if body_excerpt:
            detail += f"; response body: {body_excerpt}"
        super().__init__(detail)


def _response_text_excerpt(response: Any) -> str:
    try:
        text = str(response.text)
    except Exception:
        text = ""
    if len(text) > _REPLY_ERROR_EXCERPT_CHARS:
        return text[:_REPLY_ERROR_EXCERPT_CHARS] + "..."
    return text


async def send_reply(
    *,
    inbound: dict[str, Any],
    reply: dict[str, Any],
    cfg: BridgeConfig,
    client: Any,
    fmi_cache: _FmiCache,
    user_cache: _UserTokenCache,
    bf_cache: _BfTokenCache | None = None,
) -> Any:
    """POST a reply Activity to the inbound's serviceUrl.

    Auth: dispatched via ``acquire_reply_token`` — Path A (agentic
    user-FIC chain) or Path B (BF S2S ``client_credentials``)
    depending on the inbound shape. The reply path itself is the
    standard Bot Framework REST shape; only the bearer changes.

    ``bf_cache`` is optional for backwards-compatibility with existing
    serve-mode callers that don't yet pass it; a fresh cache is
    constructed when None (#33).
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
    if bf_cache is None:
        bf_cache = _BfTokenCache()
    token, _path = await acquire_reply_token(
        client=client,
        cfg=cfg,
        activity=inbound,
        fmi_cache=fmi_cache,
        user_cache=user_cache,
        bf_cache=bf_cache,
    )
    response = await client.post(
        url,
        json=reply,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise ReplyPostError(
            status_code=status_code,
            url=url,
            body_excerpt=_response_text_excerpt(response),
        )
    return response


def _reply_failed_response(error: Exception) -> Any:
    return _JSONResponse(
        {"status": "reply_failed", "error": str(error)},
        status_code=502,
    )


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def make_app(
    cfg: BridgeConfig,
    *,
    http_client: Any | None = None,
    jwks_cache: _JwksCache | None = None,
    bf_jwks_cache: _JwksCache | None = None,
    fmi_cache: _FmiCache | None = None,
    user_cache: _UserTokenCache | None = None,
    bf_cache: _BfTokenCache | None = None,
    idempotency_cache: _IdempotencyCache | None = None,
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
    if bf_jwks_cache is None:
        bf_jwks_cache = _JwksCache()
    if fmi_cache is None:
        fmi_cache = _FmiCache()
    if user_cache is None:
        user_cache = _UserTokenCache()
    if bf_cache is None:
        bf_cache = _BfTokenCache()
    if idempotency_cache is None:
        idempotency_cache = _IdempotencyCache(ttl_seconds=cfg.idempotency_ttl_seconds)

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
        # Slice 19j: refuse to act on an activity whose `serviceUrl`
        # isn't on the trusted-host suffix allowlist. Without this a
        # forged activity could redirect our outbound user-FIC bearer
        # to an attacker-controlled URL. Empty allowlist is a config
        # bug — refuse all rather than silently accept.
        service_url = activity.get("serviceUrl") or ""
        if not cfg.trusted_service_url_suffixes:
            raise _HTTPException(
                status_code=403,
                detail=(
                    "trusted_service_url_suffixes is empty — refusing to "
                    "process inbound activity. This is a config bug."
                ),
            )
        if not _is_trusted_service_url(service_url, cfg.trusted_service_url_suffixes):
            raise _HTTPException(
                status_code=403,
                detail=f"untrusted serviceUrl: {service_url!r}",
            )

        # JWT validation — the inbound `Authorization: Bearer <token>` header.
        # Slice 19f: AAD-v2 issuer + JWKS for the bridge's tenant; azp
        # allowlist replaces the BF-specific serviceUrl claim check.
        if not cfg.skip_jwt_validation:
            if not authorization or not authorization.lower().startswith("bearer "):
                raise _HTTPException(status_code=401, detail="missing bearer token")
            token = authorization.split(None, 1)[1]
            # Slice 19w-a (#18, review finding 3): dual inbound-JWT dispatch,
            # matching the plugin adapter. Previously serve validated ONLY the
            # AAD-v2 shape and would reject legitimate Path B BF Connector
            # tokens (Direct Line / Teams via Bot Service / Copilot fabric).
            # Peek the unverified issuer to pick the validator; BF-issuer tokens
            # go through validate_inbound_jwt_bf, A365/AAD-v2 through
            # validate_inbound_jwt. Both do full signature checks, so a
            # malformed token is rejected either way; default to AAD-v2 on peek
            # failure to preserve pre-fix behaviour.
            try:
                if peek_unverified_iss(token) == BF_ISSUER:
                    await validate_inbound_jwt_bf(
                        token=token,
                        expected_app_id=cfg.bf_app_id or cfg.blueprint_client_id,
                        expected_service_url=service_url,
                        client=http_client,
                        cache=bf_jwks_cache,
                    )
                else:
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

        # Slice 19i: dedupe by (conversationId, activityId) so connector
        # retries don't double-fire the operator webhook. Channel-control
        # activities that lack `id` skip dedupe — better to always-deliver
        # them than to drop legitimate traffic on a missing id.
        delivery_id = _activity_delivery_id(activity)
        if delivery_id is not None and idempotency_cache.is_duplicate(delivery_id):
            return _JSONResponse({"status": "duplicate"})

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
            try:
                await send_reply(
                    inbound=activity,
                    reply=error_reply,
                    cfg=cfg,
                    client=http_client,
                    fmi_cache=fmi_cache,
                    user_cache=user_cache,
                    bf_cache=bf_cache,
                )
            except Exception as reply_error:
                return _reply_failed_response(reply_error)
            return _JSONResponse({"status": "webhook_error"}, status_code=200)

        # Invoke replies must be synchronous. BF wire: the HTTP body is the
        # invokeResponse *body* and the HTTP status is its status — NOT a
        # {status, body} wrapper (that is an SDK abstraction the transport
        # unwraps; sending the wrapper makes Teams reject it as "Unable to
        # reach app"). The operator webhook still returns {status, body}; we
        # unwrap it here.
        if activity_type == "invoke":
            invoke_response = webhook_resp.get("invokeResponse") or {
                "status": 200,
                "body": webhook_resp,
            }
            return _JSONResponse(
                invoke_response.get("body"),
                status_code=int(invoke_response.get("status", 200) or 200),
            )

        # Standard message: render reply and send asynchronously via serviceUrl.
        if not webhook_resp.get("text") and not webhook_resp.get("card"):
            return _JSONResponse({"status": "no_reply"})
        reply = render_reply_activity(activity, webhook_resp)
        try:
            await send_reply(
                inbound=activity,
                reply=reply,
                cfg=cfg,
                client=http_client,
                fmi_cache=fmi_cache,
                user_cache=user_cache,
                bf_cache=bf_cache,
            )
        except Exception as e:
            return _reply_failed_response(e)
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
    from .mutator import AADSTSError, CliInvocationError, get_mutator

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
