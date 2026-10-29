"""Read Entra registration state and decide whether register may mutate.

``a365 setup blueprint`` looks an application up by display name and, on a
hit, finishes configuration: admin consent, an OBO scope patch, and a new
client secret when the stored one is missing. That is not a safe re-run.
Register therefore reads Graph itself before any ``a365`` call (#127).

Contract (the behaviour #117 will document):

- The only local ownership anchor is per-agent ``A365_APP_ID`` in
  ``~/.hermes/agents/<slug>/.env``. A cwd ``a365.generated.config.json``
  GUID is not an anchor (#159 / 871b978): it can authorize a read-back
  after *this* process created the app, and nothing else.
- No existing application → create (blueprint, then MCP and bot permissions).
- Exactly one blueprint application whose appId is that anchor, which Graph
  shows is an agent-identity blueprint, whose display name and slug match,
  and whose service principal already has both permission-step markers →
  no-op. No ``a365`` call, so nothing is duplicated, consented, or rotated.
  The markers are an Observability app-role assignment (``setup permissions
  bot``) and a delegated grant for a resource other than Microsoft Graph,
  Messaging Bot, Observability, or Power Platform (``setup permissions
  mcp``). Microsoft Graph admin consent, which ``setup blueprint`` writes,
  is not one of those markers.
- The same owned blueprint with a service principal but a missing marker →
  run only the permission step that has not landed. That finishes a
  consent-deferred register; it does not re-enter ``setup blueprint`` and
  it does not re-grant a step that already landed.
- Anything else (unowned name, several matches, a paged or unreadable
  result, a missing anchor app, a tier or slug mismatch, audience drift,
  a missing service principal, unreadable grants) → fail closed with an
  error that names what to fix. ``--update-permissions`` is the only flag
  that may re-grant on an already-owned blueprint, and it does not override
  an ownership, tier, or name refusal.
- Graph calls are GET-only, on ``graph.microsoft.com``, with an explicit
  ``$select`` that excludes credentials, secrets, and tokens.

Desired tier 1 means "agent identity blueprint" (what ``setup blueprint``
creates). Any other application class is tier 2, which ``reconcile_app``
aborts on. When the blueprint cast returns ``uniqueName``, that value is
the slug. A different slug aborts via ``reconcile_blueprint`` (SPEC §6.13).
A missing ``uniqueName`` is not filled in from the display name:
``a365 setup blueprint`` does not set the property, so requiring it would
refuse a finished registration and the confirmation after create.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID

from ._common import (
    ensure_contained,
    parse_env,
    slugify,
    validate_slug,
    write_owner_only_text_atomic,
)
from .mutator import AADSTSError, CliInvocationError, Mutator
from .reconcile_app import (
    ActualAppRegistration,
    AppPlan,
    DesiredAppRegistration,
    reconcile_app,
)
from .reconcile_blueprint import BlueprintPlan, reconcile_blueprint

GraphGet = Callable[[str], dict[str, Any] | None]

StepName = Literal["blueprint", "permissions-mcp", "permissions-bot"]
Action = Literal["create", "noop", "repair", "abort"]
SpState = Literal["not-queried", "missing", "found", "ambiguous", "unreadable"]
GrantState = Literal["not-queried", "absent", "present", "unreadable"]

CREATE_STEPS: tuple[StepName, ...] = ("blueprint", "permissions-mcp", "permissions-bot")
PERMISSION_STEPS: tuple[StepName, ...] = ("permissions-mcp", "permissions-bot")

_GRAPH_HOST = "graph.microsoft.com"
_APP_SELECT = "id,appId,displayName,signInAudience"
# ``uniqueName`` is the immutable Graph slug. It is selected only on the
# agent-identity-blueprint cast, not on the directory list.
_CAST_SELECT = "id,appId,displayName,signInAudience,uniqueName"
_SP_SELECT = "id,appId"
_ROLE_SELECT = "id,resourceId,appRoleId"
_OAUTH_SELECT = "id,resourceId"
_RESOURCE_SELECT = "appId"
# Resource appIds. Graph admin consent is what ``setup blueprint`` writes.
# The other three are what ``setup permissions bot`` writes. A delegated
# grant for any other resource is the MCP (or custom) marker.
_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
_MESSAGING_BOT_APP_ID = "5a807f24-c9de-44ee-a3a7-329e88a00ffc"
_POWER_PLATFORM_APP_ID = "8578e004-a5c6-46e7-913e-12f58912df43"
_OBSERVABILITY_APP_IDS = frozenset(
    {
        "9b975845-388f-4429-889e-eab1ef63949c",
        "2c672ad5-b104-44ed-8069-bb68dd138546",
        "009c6bd0-82e4-4466-95b3-4c996521f3d7",
        "a9e04047-c6a7-430b-a7ae-faf8f8eed1b7",
    }
)
_BOT_FIXED_APP_IDS = (
    frozenset({_GRAPH_APP_ID, _MESSAGING_BOT_APP_ID, _POWER_PLATFORM_APP_ID})
    | _OBSERVABILITY_APP_IDS
)
_BLUEPRINT_ODATA_CAST = "microsoft.graph.agentIdentityBlueprint"
_MULTI_TENANT_AUDIENCE = "AzureADMultipleOrgs"
_DESIRED_TIER = 1  # agent identity blueprint; other classes are tier 2
_ACCESS_DENIED_CODES = frozenset(
    {
        "Authorization_RequestDenied",
        "AccessDenied",
        "Forbidden",
        "InsufficientPrivileges",
    }
)
_NOT_FOUND_CODES = frozenset(
    {
        "Request_ResourceNotFound",
        "ResourceNotFound",
        "ErrorItemNotFound",
        "itemNotFound",
    }
)
_SELECT_ALLOWLIST: dict[str, frozenset[str]] = {
    "application": frozenset({"id", "appId", "displayName", "signInAudience", "uniqueName"}),
    "servicePrincipal": frozenset({"id", "appId"}),
    "grant": frozenset({"id", "resourceId", "appRoleId"}),
}
_FORBIDDEN_URL_RE = re.compile(
    r"(?i)(password|secret|credential|token|keyCredentials|passwordCredentials)"
)
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}")
_JSON_SECRET_RE = re.compile(
    r"(?i)(\"(?:password|secret|client_secret|access_token|refresh_token|token)\"\s*:\s*\")[^\"]*\""
)

GRAPH_READ_PERMISSION_HINT = (
    "Graph rejected the registration state read (insufficient privileges). "
    "The az CLI session needs delegated permission to read applications "
    "(Application.Read.All). Reconciling an existing blueprint also reads "
    "its service principal, app role assignments, and delegated permission "
    "grants (AppRoleAssignment.Read.All and DelegatedPermissionGrant.Read.All). "
    "Directory.Read.All covers those reads. Refusing to provision without a "
    "trustworthy read."
)


class EntraStateError(RuntimeError):
    """Graph or local state could not be read safely. Callers must not mutate."""


@dataclass(frozen=True)
class GraphApp:
    """Fields register is allowed to read from an application object."""

    object_id: str
    app_id: str
    display_name: str
    sign_in_audience: str
    is_agent_blueprint: bool | None = None
    blueprint_slug: str | None = None


@dataclass(frozen=True)
class RegistrationSnapshot:
    """Entra state for one agent name, after a successful read.

    A snapshot only exists when every query we attempted returned a
    trustworthy body. Logical conflicts (unowned, collision, drift) are
    decisions, not read failures.
    """

    agent_name: str
    anchor_app_id: str
    blueprint_apps: tuple[GraphApp, ...]
    identity_apps: tuple[GraphApp, ...]
    anchor_app: GraphApp | None
    anchor_missing: bool
    service_principal_id: str
    service_principal_state: SpState
    grant_state: GrantState
    grant_detail: str = ""
    mcp_permission_configured: bool = False
    bot_permission_configured: bool = False


@dataclass
class RegistrationDecision:
    """What register may do with a snapshot. ``abort`` means zero mutations."""

    action: Action
    step_names: tuple[StepName, ...]
    reason: str | None
    messages: tuple[str, ...]
    app_plan: AppPlan | None = None
    blueprint_plan: BlueprintPlan | None = None


def blueprint_display_name(agent_name: str) -> str:
    return f"{agent_name} Blueprint"


def identity_display_name(agent_name: str) -> str:
    return f"{agent_name} Identity"


def redact(text: str) -> str:
    """Strip bearer tokens, JWTs, and JSON secret fields from operator text."""
    redacted = _JWT_RE.sub("[redacted]", text)
    redacted = _BEARER_RE.sub("Bearer [redacted]", redacted)
    redacted = _JSON_SECRET_RE.sub(r'\1[redacted]"', redacted)
    if len(redacted) > 400:
        return redacted[:400] + "…"
    return redacted


def registration_slug(agent_name: str) -> str:
    """Slug used for the per-agent ownership anchor directory."""
    slug = slugify(agent_name)
    if not slug:
        raise EntraStateError(
            f"agent name {agent_name!r} does not produce a slug; refusing to "
            "look up or create an Entra application."
        )
    try:
        return validate_slug(slug)
    except ValueError as exc:
        raise EntraStateError(str(exc)) from exc


def load_anchor_app_id(hermes_home: Path, agent_name: str) -> str:
    """Return the per-agent ``A365_APP_ID``, or ``""`` when the file is absent.

    A non-GUID value is a hard error. Treating it as "unset" would let
    register create a second blueprint beside a corrupt anchor.
    """
    slug = registration_slug(agent_name)
    root = hermes_home / "agents"
    env_path = root / slug / ".env"
    # ``Path.exists()`` follows links and is false for a dangling symlink, so
    # the symlink check has to come first. A symlink is never "no anchor".
    if root.is_symlink() or env_path.parent.is_symlink() or env_path.is_symlink():
        raise EntraStateError(
            "refusing to read the ownership anchor: the per-agent env path is a "
            f"symlink ({env_path})."
        )
    if not env_path.exists():
        return ""
    if not env_path.is_file():
        raise EntraStateError(
            "refusing to read the ownership anchor: the per-agent env is not "
            f"a regular file ({env_path})."
        )
    try:
        ensure_contained(env_path, root)
    except ValueError as exc:
        raise EntraStateError(f"refusing to read the ownership anchor: {exc}") from exc
    try:
        text = env_path.read_text()
    except OSError as exc:
        raise EntraStateError(
            f"could not read the per-agent ownership anchor at {env_path}: {exc}"
        ) from exc
    raw = parse_env(text).get("A365_APP_ID", "").strip()
    if not raw:
        return ""
    try:
        return str(UUID(raw))
    except ValueError as exc:
        raise EntraStateError(
            "per-agent A365_APP_ID is not a GUID; refusing to provision. "
            f"Fix {env_path} before re-running register."
        ) from exc


def record_ownership_anchor(hermes_home: Path, agent_name: str, app_id: str) -> Path:
    """Record ``A365_APP_ID`` after Graph has confirmed an app this run created.

    Does not overwrite a different anchor and does not log env values. The
    file stays owner-only because it may already hold a Bot Framework secret.
    """
    try:
        canonical = str(UUID(app_id))
    except ValueError as exc:
        raise EntraStateError("refusing to record an ownership anchor that is not a GUID.") from exc
    slug = registration_slug(agent_name)
    root = hermes_home / "agents"
    env_path = root / slug / ".env"
    if root.is_symlink() or env_path.parent.is_symlink() or env_path.is_symlink():
        raise EntraStateError(
            "refusing to record A365_APP_ID: the per-agent env path is a symlink."
        )
    ensure_contained(env_path, root)
    existing: dict[str, str] = {}
    if env_path.exists():
        if env_path.is_symlink() or not env_path.is_file():
            raise EntraStateError(
                "refusing to record A365_APP_ID: the per-agent env is not a regular file."
            )
        try:
            existing = parse_env(env_path.read_text())
        except OSError as exc:
            raise EntraStateError(
                f"could not read the per-agent env before recording A365_APP_ID: {exc}"
            ) from exc
    current = existing.get("A365_APP_ID", "").strip()
    if current:
        try:
            current_id = str(UUID(current))
        except ValueError as exc:
            raise EntraStateError(
                "refusing to overwrite a non-GUID A365_APP_ID in the per-agent env."
            ) from exc
        if current_id != canonical:
            raise EntraStateError(
                "refusing to overwrite per-agent A365_APP_ID "
                f"{current_id} with the blueprint just created ({canonical})."
            )
        return env_path
    existing["A365_APP_ID"] = canonical
    # Stable key order, anchor last only in the sense that we don't sort
    # secrets ahead of anything else — parse order is preserved by dict.
    rendered = "".join(f"{key}={value}\n" for key, value in existing.items())
    env_path.parent.mkdir(parents=True, exist_ok=True)
    write_owner_only_text_atomic(env_path, rendered)
    os.chmod(env_path.parent, 0o700)
    return env_path


# ---------------------------------------------------------------------------
# Least-privilege URL construction
# ---------------------------------------------------------------------------


def _odata_string(value: str) -> str:
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise EntraStateError(
            "agent name contains a control character that cannot be placed "
            "in a Graph filter; refusing to query."
        )
    return value.replace("'", "''")


def _select_query(fields: str) -> str:
    return "$select=" + quote(fields, safe=",")


def applications_by_display_name_url(*display_names: str) -> str:
    clauses = " or ".join(f"displayName eq '{_odata_string(name)}'" for name in display_names)
    filter_query = "$filter=" + quote(f"({clauses})", safe="")
    return f"https://{_GRAPH_HOST}/beta/applications?{filter_query}&{_select_query(_APP_SELECT)}"


def application_by_app_id_url(app_id: str) -> str:
    canonical = str(UUID(app_id))
    return (
        f"https://{_GRAPH_HOST}/beta/applications(appId='{canonical}')?{_select_query(_APP_SELECT)}"
    )


_SECRET_EXPIRY_SELECT = frozenset({"passwordCredentials"})


def application_client_secret_expiry_url(app_id: str) -> str:
    """Read only ``passwordCredentials`` metadata (``endDateTime``); never secret values."""
    canonical = str(UUID(app_id))
    return (
        f"https://{_GRAPH_HOST}/beta/applications(appId='{canonical}')"
        f"?{_select_query('passwordCredentials')}"
    )


def assert_client_secret_expiry_read_url(url: str) -> None:
    """Allow only the narrow client-secret expiry metadata read."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != _GRAPH_HOST:
        raise EntraStateError("refusing a non-Graph URL during client secret expiry read.")
    if parts.fragment or not parts.query:
        raise EntraStateError("refusing a Graph client secret expiry read without a query.")
    path = parts.path
    if not path.startswith("/beta/applications(appId='") or not path.endswith("')"):
        raise EntraStateError(
            "refusing a Graph client secret expiry read outside applications(appId=…)."
        )
    inner = path[len("/beta/applications(appId='") : -2]
    if not _is_guid(inner):
        raise EntraStateError("refusing a Graph client secret expiry read with a non-GUID appId.")
    select = _query_select(parts.query)
    if select is None:
        raise EntraStateError("refusing a Graph client secret expiry read without $select.")
    if select != _SECRET_EXPIRY_SELECT:
        raise EntraStateError(
            "refusing a Graph client secret expiry read whose $select is not "
            "passwordCredentials only."
        )


def blueprint_cast_url(object_id: str) -> str:
    canonical = str(UUID(object_id))
    return (
        f"https://{_GRAPH_HOST}/beta/applications/{canonical}/{_BLUEPRINT_ODATA_CAST}"
        f"?{_select_query(_CAST_SELECT)}"
    )


def service_principals_by_app_id_url(app_id: str) -> str:
    canonical = str(UUID(app_id))
    filter_query = "$filter=" + quote(f"appId eq '{canonical}'", safe="")
    return (
        f"https://{_GRAPH_HOST}/v1.0/servicePrincipals?{filter_query}&{_select_query(_SP_SELECT)}"
    )


def app_role_assignments_url(service_principal_id: str) -> str:
    canonical = str(UUID(service_principal_id))
    return (
        f"https://{_GRAPH_HOST}/v1.0/servicePrincipals/{canonical}/appRoleAssignments"
        f"?{_select_query(_ROLE_SELECT)}"
    )


def oauth2_permission_grants_url(service_principal_id: str) -> str:
    canonical = str(UUID(service_principal_id))
    filter_query = "$filter=" + quote(f"clientId eq '{canonical}'", safe="")
    return (
        f"https://{_GRAPH_HOST}/v1.0/oauth2PermissionGrants?{filter_query}"
        f"&{_select_query(_OAUTH_SELECT)}"
    )


def service_principal_by_id_url(object_id: str) -> str:
    """Read a resource service principal's appId. No credentials."""
    canonical = str(UUID(object_id))
    return (
        f"https://{_GRAPH_HOST}/v1.0/servicePrincipals/{canonical}"
        f"?{_select_query(_RESOURCE_SELECT)}"
    )


def assert_registration_read_url(url: str) -> None:
    """Reject any Graph URL that is not a registration read with a tight ``$select``.

    The display-name filter is not scanned: an agent may legitimately be
    named "Token" or "Secret". The path and the ``$select`` list are.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != _GRAPH_HOST:
        raise EntraStateError("refusing a non-Graph URL during registration state read.")
    if parts.fragment or not parts.query:
        raise EntraStateError("refusing a Graph registration read without a query.")
    path = parts.path
    if _FORBIDDEN_URL_RE.search(path):
        raise EntraStateError(
            "refusing a Graph registration read that asks for credentials or tokens."
        )
    kind = _url_kind(path)
    if kind is None:
        raise EntraStateError(f"refusing an unexpected Graph path during registration read: {path}")
    select = _query_select(parts.query)
    if select is None:
        raise EntraStateError("refusing a Graph registration read without $select.")
    if any(_FORBIDDEN_URL_RE.search(field) for field in select):
        raise EntraStateError(
            "refusing a Graph registration read that asks for credentials or tokens."
        )
    if not select <= _SELECT_ALLOWLIST[kind]:
        raise EntraStateError(
            "refusing a Graph registration read whose $select is wider than "
            f"{sorted(_SELECT_ALLOWLIST[kind])}."
        )


def _url_kind(path: str) -> str | None:
    if path == "/beta/applications":
        return "application"
    if path.startswith("/beta/applications(appId='") and path.endswith("')"):
        inner = path[len("/beta/applications(appId='") : -2]
        if _is_guid(inner):
            return "application"
        return None
    prefix = "/beta/applications/"
    cast_suffix = f"/{_BLUEPRINT_ODATA_CAST}"
    if path.startswith(prefix) and path.endswith(cast_suffix):
        object_id = path[len(prefix) : -len(cast_suffix)]
        if _is_guid(object_id):
            return "application"
        return None
    if path == "/v1.0/servicePrincipals":
        return "servicePrincipal"
    marker = "/v1.0/servicePrincipals/"
    if path.startswith(marker) and path.endswith("/appRoleAssignments"):
        object_id = path[len(marker) : -len("/appRoleAssignments")]
        if _is_guid(object_id):
            return "grant"
        return None
    if path.startswith(marker):
        object_id = path[len(marker) :]
        if _is_guid(object_id):
            return "servicePrincipal"
        return None
    if path == "/v1.0/oauth2PermissionGrants":
        return "grant"
    return None


def _query_select(query: str) -> frozenset[str] | None:
    # The query is already percent-encoded by the builders. Decode just the
    # select list so the allowlist compares field names.
    parsed = parse_qs(query, keep_blank_values=True)
    values = parsed.get("$select")
    if not values or len(values) != 1 or not values[0]:
        return None
    return frozenset(part for part in values[0].split(",") if part)


def _is_guid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Graph parsing
# ---------------------------------------------------------------------------


def _parse_graph_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _error_code(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return ""


def _public_error(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return "response was not a JSON object"
    error = payload.get("error")
    if not isinstance(error, dict):
        return "response had no Graph error object"
    code = str(error.get("code") or "unknown")
    message = redact(str(error.get("message") or ""))
    return f"{code}: {message}"


def _is_denied(payload: Mapping[str, Any] | None) -> bool:
    return _error_code(payload) in _ACCESS_DENIED_CODES


def _is_not_found(payload: Mapping[str, Any] | None) -> bool:
    return _error_code(payload) in _NOT_FOUND_CODES


def graph_get_from_mutator(mutator: Mutator) -> GraphGet:
    """GET one allowlisted Graph URL via ``az rest``. Errors stay structured.

    ``az rest`` non-zero exits still carry a JSON body (including 403 and
    404). That body is returned so the caller can fail closed with the Graph
    code instead of treating the failure as "no such app".
    """

    def graph_get(url: str) -> dict[str, Any] | None:
        assert_registration_read_url(url)
        try:
            # sensitive=True captures the body instead of streaming it. A Graph
            # error can contain a bearer token; redact() runs before any of that
            # text is shown to the operator.
            result = mutator.run(
                ["az", "rest", "--method", "GET", "--uri", url],
                sensitive=True,
            )
        except CliInvocationError as exc:
            raw = exc.captured_output if exc.captured_output is not None else (exc.output or "")
            parsed = _parse_graph_json(raw)
            if parsed is not None:
                return parsed
            return {
                "error": {
                    "code": "UnreadableGraphResponse",
                    "message": redact(raw or "az rest failed"),
                }
            }
        except (AADSTSError, OSError):
            return None
        return _parse_graph_json(result.stdout or "")

    return graph_get


def graph_get_from_mutator_for_secret_expiry(mutator: Mutator) -> GraphGet:
    """GET passwordCredentials expiry metadata via ``az rest`` (no secret values)."""

    def graph_get(url: str) -> dict[str, Any] | None:
        assert_client_secret_expiry_read_url(url)
        try:
            result = mutator.run(
                ["az", "rest", "--method", "GET", "--uri", url],
                sensitive=True,
            )
        except CliInvocationError as exc:
            raw = exc.captured_output if exc.captured_output is not None else (exc.output or "")
            parsed = _parse_graph_json(raw)
            if parsed is not None:
                return parsed
            return {
                "error": {
                    "code": "UnreadableGraphResponse",
                    "message": redact(raw or "az rest failed"),
                }
            }
        except (AADSTSError, OSError):
            return None
        return _parse_graph_json(result.stdout or "")

    return graph_get


def _require_guid(value: object, *, what: str) -> str:
    if not isinstance(value, str):
        raise EntraStateError(f"Graph {what} was missing; refusing to reconcile.")
    try:
        return str(UUID(value.strip()))
    except ValueError as exc:
        raise EntraStateError(f"Graph {what} was not a GUID; refusing to reconcile.") from exc


def _parse_app(item: Mapping[str, Any], *, is_agent_blueprint: bool | None) -> GraphApp:
    display_name = item.get("displayName")
    audience = item.get("signInAudience")
    if not isinstance(display_name, str) or not display_name:
        raise EntraStateError("Graph application is missing displayName; refusing to reconcile.")
    if not isinstance(audience, str) or not audience:
        raise EntraStateError("Graph application is missing signInAudience; refusing to reconcile.")
    blueprint_slug: str | None = None
    if "uniqueName" in item and item.get("uniqueName") not in (None, ""):
        slug = item.get("uniqueName")
        if (
            not isinstance(slug, str)
            or slug.strip() == ""
            or any(char in slug for char in ("\x00", "\r", "\n"))
        ):
            raise EntraStateError(
                "Graph uniqueName was not a usable blueprint slug; refusing to "
                "derive one from the display name."
            )
        blueprint_slug = slug.strip()
    return GraphApp(
        object_id=_require_guid(item.get("id"), what="application object id"),
        app_id=_require_guid(item.get("appId"), what="application appId"),
        display_name=display_name,
        sign_in_audience=audience,
        is_agent_blueprint=is_agent_blueprint,
        blueprint_slug=blueprint_slug,
    )


def _reject_partial_page(payload: Mapping[str, Any], *, what: str) -> None:
    if payload.get("@odata.nextLink"):
        raise EntraStateError(
            f"Graph returned a paged {what} result; refusing to reconcile a partial page."
        )
    count = payload.get("@odata.count")
    if count is None:
        return
    value = payload.get("value")
    try:
        expected = int(count)
    except (TypeError, ValueError) as exc:
        raise EntraStateError(
            f"Graph {what} @odata.count was not an integer; refusing to reconcile."
        ) from exc
    if not isinstance(value, list) or expected != len(value):
        raise EntraStateError(
            f"Graph {what} @odata.count did not match the returned rows; "
            "refusing to reconcile a partial result."
        )


def _collection(payload: Mapping[str, Any] | None, *, what: str) -> list[dict[str, Any]]:
    if payload is None:
        raise EntraStateError(
            f"could not read {what} (Graph response was not trustworthy). Refusing to provision."
        )
    if payload.get("error"):
        if _is_denied(payload):
            raise EntraStateError(f"{GRAPH_READ_PERMISSION_HINT} {_public_error(payload)}")
        raise EntraStateError(
            f"could not read {what} ({_public_error(payload)}). Refusing to provision."
        )
    _reject_partial_page(payload, what=what)
    value = payload.get("value")
    if not isinstance(value, list):
        raise EntraStateError(f"Graph {what} response had no value array; refusing to reconcile.")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise EntraStateError(f"Graph {what} returned a non-object row; refusing to reconcile.")
        rows.append(item)
    return rows


def _read(graph_get: GraphGet, url: str) -> dict[str, Any] | None:
    assert_registration_read_url(url)
    try:
        payload = graph_get(url)
    except EntraStateError:
        raise
    except Exception as exc:
        raise EntraStateError(
            f"Graph read failed ({redact(str(exc))}). Refusing to provision."
        ) from exc
    if payload is not None and not isinstance(payload, dict):
        raise EntraStateError("Graph read returned a non-object; refusing to provision.")
    return payload


def _apps_by_name(graph_get: GraphGet, *display_names: str) -> tuple[GraphApp, ...]:
    payload = _read(graph_get, applications_by_display_name_url(*display_names))
    rows = _collection(payload, what="applications")
    expected = set(display_names)
    apps: list[GraphApp] = []
    for row in rows:
        app = _parse_app(row, is_agent_blueprint=None)
        if app.display_name not in expected:
            raise EntraStateError(
                "Graph returned an application whose display name was not requested "
                f"({app.display_name!r}, appId {app.app_id}); refusing to reconcile."
            )
        apps.append(app)
    return tuple(apps)


def _app_by_id(graph_get: GraphGet, app_id: str) -> GraphApp | None:
    """Return the application, ``None`` when Graph says it does not exist."""
    payload = _read(graph_get, application_by_app_id_url(app_id))
    if payload is None:
        raise EntraStateError(
            f"could not read application {app_id} (Graph response was not trustworthy). "
            "Refusing to provision."
        )
    if payload.get("error"):
        if _is_not_found(payload):
            return None
        if _is_denied(payload):
            raise EntraStateError(f"{GRAPH_READ_PERMISSION_HINT} {_public_error(payload)}")
        raise EntraStateError(
            f"could not read application {app_id} ({_public_error(payload)}). "
            "Refusing to provision."
        )
    app = _parse_app(payload, is_agent_blueprint=None)
    if app.app_id != str(UUID(app_id)):
        raise EntraStateError(
            f"Graph application appId {app.app_id} did not match the requested {app_id}. "
            "Refusing to reconcile."
        )
    return app


def _confirm_blueprint(graph_get: GraphGet, app: GraphApp) -> GraphApp:
    payload = _read(graph_get, blueprint_cast_url(app.object_id))
    if payload is None:
        raise EntraStateError(
            "could not tell whether the application is an agent identity blueprint "
            "(Graph response was not trustworthy). Refusing to provision."
        )
    if payload.get("error"):
        if _is_not_found(payload):
            return GraphApp(
                object_id=app.object_id,
                app_id=app.app_id,
                display_name=app.display_name,
                sign_in_audience=app.sign_in_audience,
                is_agent_blueprint=False,
            )
        if _is_denied(payload):
            raise EntraStateError(f"{GRAPH_READ_PERMISSION_HINT} {_public_error(payload)}")
        raise EntraStateError(
            "could not tell whether the application is an agent identity blueprint "
            f"({_public_error(payload)}). Refusing to provision."
        )
    cast = _parse_app(payload, is_agent_blueprint=True)
    if cast.app_id != app.app_id or cast.object_id != app.object_id:
        raise EntraStateError(
            "agent-identity-blueprint cast returned a different application than "
            f"{app.app_id}. Refusing to reconcile."
        )
    if cast.display_name != app.display_name or cast.sign_in_audience != app.sign_in_audience:
        raise EntraStateError(
            "agent-identity-blueprint cast disagreed with the application read "
            f"for {app.app_id}. Refusing to reconcile inconsistent state."
        )
    return cast


def _service_principal_id(graph_get: GraphGet, app_id: str) -> tuple[str, SpState, str]:
    payload = _read(graph_get, service_principals_by_app_id_url(app_id))
    try:
        rows = _collection(payload, what="service principals")
    except EntraStateError as exc:
        if str(exc).startswith("Graph rejected"):
            raise
        return "", "unreadable", str(exc)
    parsed: list[str] = []
    for row in rows:
        row_app = _require_guid(row.get("appId"), what="service principal appId")
        if row_app != str(UUID(app_id)):
            raise EntraStateError(
                "Graph returned a service principal for a different appId. Refusing to reconcile."
            )
        parsed.append(_require_guid(row.get("id"), what="service principal id"))
    if not parsed:
        return "", "missing", ""
    if len(parsed) > 1:
        return "", "ambiguous", ""
    return parsed[0], "found", ""


def _grant_rows(
    graph_get: GraphGet, url: str, *, what: str
) -> tuple[list[dict[str, Any]] | None, str]:
    payload = _read(graph_get, url)
    try:
        rows = _collection(payload, what=what)
    except EntraStateError as exc:
        return None, str(exc)
    return rows, ""


def _resource_app_id(graph_get: GraphGet, object_id: str, cache: dict[str, str]) -> str:
    cached = cache.get(object_id)
    if cached is not None:
        return cached
    payload = _read(graph_get, service_principal_by_id_url(object_id))
    if payload is None:
        raise EntraStateError(
            "could not read the resource service principal for a permission grant. "
            "Refusing to treat the grant as absent."
        )
    if payload.get("error"):
        if _is_denied(payload):
            raise EntraStateError(f"{GRAPH_READ_PERMISSION_HINT} {_public_error(payload)}")
        raise EntraStateError(
            "could not read the resource service principal for a permission grant "
            f"({_public_error(payload)}). Refusing to treat the grant as absent."
        )
    app_id = _require_guid(payload.get("appId"), what="resource service principal appId")
    cache[object_id] = app_id
    return app_id


def _row_resource_id(row: Mapping[str, Any], *, what: str) -> str:
    try:
        return _require_guid(row.get("resourceId"), what=f"{what} resourceId")
    except EntraStateError as exc:
        raise EntraStateError(
            f"Graph {what} row had no resourceId. Refusing to treat it as absent "
            "or as a finished permission step."
        ) from exc


def _permission_state(
    graph_get: GraphGet, service_principal_id: str
) -> tuple[GrantState, str, bool, bool]:
    """Classify grants. Graph admin consent is not MCP or bot setup.

    ``setup blueprint`` writes Microsoft Graph delegated consent. ``setup
    permissions mcp`` writes a delegated grant for some other resource.
    ``setup permissions bot`` writes the Observability app-role assignment.
    A row we cannot classify fails closed instead of looking finished.
    """
    roles, role_detail = _grant_rows(
        graph_get,
        app_role_assignments_url(service_principal_id),
        what="app role assignments",
    )
    grants, grant_detail = _grant_rows(
        graph_get,
        oauth2_permission_grants_url(service_principal_id),
        what="delegated permission grants",
    )
    if roles is None or grants is None:
        return "unreadable", role_detail or grant_detail, False, False
    cache: dict[str, str] = {}
    try:
        bot = False
        for row in roles:
            resource = _row_resource_id(row, what="app role assignment")
            if _resource_app_id(graph_get, resource, cache) in _OBSERVABILITY_APP_IDS:
                bot = True
        mcp = False
        for row in grants:
            resource = _row_resource_id(row, what="delegated permission grant")
            app_id = _resource_app_id(graph_get, resource, cache)
            if app_id not in _BOT_FIXED_APP_IDS:
                mcp = True
    except EntraStateError as exc:
        return "unreadable", str(exc), False, False
    if mcp or bot or roles or grants:
        return "present", "", mcp, bot
    return "absent", "", False, False


def confirmed_created_app_id(
    graph_get: GraphGet,
    generated_config_path: Path,
    *,
    expected_display_name: str,
) -> str | None:
    """Return the appId Graph confirmed after ``setup blueprint``.

    ``None`` means the generated config recorded no ``agentBlueprintId``
    (the caller continues, but must not invent an anchor). A present id
    that Graph does not confirm raises. The client secret is never read
    into the error text.
    """
    if not generated_config_path.exists():
        return None
    if generated_config_path.is_symlink() or not generated_config_path.is_file():
        raise EntraStateError(
            "refusing to read a generated config that is not a regular file "
            "while recording the ownership anchor."
        )
    try:
        payload = json.loads(generated_config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EntraStateError(
            "generated config is unreadable after setup blueprint; refusing to "
            f"grant permissions or record an ownership anchor ({exc.__class__.__name__})."
        ) from exc
    if not isinstance(payload, dict):
        raise EntraStateError(
            "generated config was not a JSON object; refusing to grant permissions "
            "or record an ownership anchor."
        )
    raw_id = payload.get("agentBlueprintId")
    if raw_id is None or raw_id == "":
        return None
    if not isinstance(raw_id, str) or not _is_guid(raw_id):
        # Do not echo the value: a mis-written secret is not a GUID either.
        raise EntraStateError(
            "agentBlueprintId in the generated config is not a GUID; refusing to "
            "grant permissions or record an ownership anchor."
        )
    app_id = str(UUID(raw_id))
    app = _app_by_id(graph_get, app_id)
    if app is None or app.display_name != expected_display_name:
        raise EntraStateError(
            f"setup blueprint recorded app {app_id}, but Graph did not confirm an "
            f"application with that appId and display name {expected_display_name!r}. "
            "Refusing to grant permissions or record A365_APP_ID. If the app is still "
            "replicating, retry once it is readable. If you already own it, set "
            f"A365_APP_ID={app_id} in the per-agent env and re-run."
        )
    confirmed = _confirm_blueprint(graph_get, app)
    if not confirmed.is_agent_blueprint:
        raise EntraStateError(
            f"application {app_id} is not an agent identity blueprint. "
            "Refusing to grant permissions or record A365_APP_ID."
        )
    return app_id


def read_registration_snapshot(
    graph_get: GraphGet,
    *,
    agent_name: str,
    anchor_app_id: str,
) -> RegistrationSnapshot:
    """Read the Entra objects register could create or modify. GET only."""
    registration_slug(agent_name)
    blueprint_name = blueprint_display_name(agent_name)
    identity_name = identity_display_name(agent_name)
    found = _apps_by_name(graph_get, blueprint_name, identity_name)
    blueprint_apps = tuple(app for app in found if app.display_name == blueprint_name)
    identity_apps = tuple(app for app in found if app.display_name == identity_name)

    anchor_app: GraphApp | None = None
    anchor_missing = False
    if anchor_app_id:
        anchor_app = _app_by_id(graph_get, anchor_app_id)
        anchor_missing = anchor_app is None

    service_principal_id = ""
    service_principal_state: SpState = "not-queried"
    grant_state: GrantState = "not-queried"
    grant_detail = ""
    mcp_permission_configured = False
    bot_permission_configured = False
    owned = (
        anchor_app is not None
        and len(blueprint_apps) == 1
        and blueprint_apps[0].app_id == anchor_app.app_id
        and blueprint_apps[0].object_id == anchor_app.object_id
        and blueprint_apps[0].display_name == blueprint_name
        and blueprint_apps[0].sign_in_audience == anchor_app.sign_in_audience
    )
    if owned:
        confirmed = _confirm_blueprint(graph_get, blueprint_apps[0])
        blueprint_apps = (confirmed,)
        if confirmed.is_agent_blueprint:
            service_principal_id, service_principal_state, grant_detail = _service_principal_id(
                graph_get, confirmed.app_id
            )
            if service_principal_state == "found":
                (
                    grant_state,
                    grant_detail,
                    mcp_permission_configured,
                    bot_permission_configured,
                ) = _permission_state(graph_get, service_principal_id)
    elif anchor_app is not None and not blueprint_apps:
        # The name query missed this app. The cast supplies uniqueName when
        # Graph has one, so a renamed anchor is not compared to a slug
        # invented from its display name.
        anchor_app = _confirm_blueprint(graph_get, anchor_app)

    return RegistrationSnapshot(
        agent_name=agent_name,
        anchor_app_id=str(UUID(anchor_app_id)) if anchor_app_id else "",
        blueprint_apps=blueprint_apps,
        identity_apps=identity_apps,
        anchor_app=anchor_app,
        anchor_missing=anchor_missing,
        service_principal_id=service_principal_id,
        service_principal_state=service_principal_state,
        grant_state=grant_state,
        grant_detail=grant_detail,
        mcp_permission_configured=mcp_permission_configured,
        bot_permission_configured=bot_permission_configured,
    )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _blueprint_body(display_name: str, slug: str) -> dict[str, Any]:
    """Desired or actual blueprint fields reconcile_blueprint compares.

    ``slug`` is the registration slug for the desired body, and Graph
    ``uniqueName`` for the actual body when Graph returned one. It is not
    derived from the display name.
    """
    return {
        "displayName": display_name,
        "agentIdentity": {"slug": slug},
    }


def _blueprint_plan_for(
    *,
    desired_name: str,
    desired_slug: str,
    actual_name: str,
    actual_slug: str | None,
) -> BlueprintPlan:
    """Compare the display name, and ``uniqueName`` when Graph returned it.

    ``a365 setup blueprint`` does not assign ``uniqueName``. Deriving a
    slug from the display name would hide a real Graph slug, and refusing
    a missing value would abort a finished registration. When Graph omits
    it, both sides use the registration slug so only the display name can
    differ. That placeholder is not a display-name slug.
    """
    compared = actual_slug if actual_slug else desired_slug
    return reconcile_blueprint(
        _blueprint_body(desired_name, desired_slug),
        _blueprint_body(actual_name, compared),
    )


def _abort(reason: str, *, messages: tuple[str, ...] = ()) -> RegistrationDecision:
    text = messages if messages else (reason,)
    return RegistrationDecision(
        action="abort",
        step_names=(),
        reason=reason,
        messages=text,
    )


def decide_registration(
    snapshot: RegistrationSnapshot,
    *,
    update_permissions: bool = False,
) -> RegistrationDecision:
    """Turn a snapshot into a create, no-op, repair, or abort. Never mutates."""
    agent_name = snapshot.agent_name
    blueprint_name = blueprint_display_name(agent_name)
    identity_name = identity_display_name(agent_name)
    slug = registration_slug(agent_name)
    anchor = snapshot.anchor_app_id

    if snapshot.anchor_missing:
        return _abort(
            "per-agent A365_APP_ID "
            f"{anchor} was not found in Entra. Refusing to create another "
            f"blueprint named {blueprint_name!r}. Confirm the anchor, or remove "
            f"it from ~/.hermes/agents/{slug}/.env if that application was deleted."
        )

    if len(snapshot.blueprint_apps) > 1:
        ids = ", ".join(app.app_id for app in snapshot.blueprint_apps)
        return _abort(
            f"multiple Entra applications are named {blueprint_name!r} ({ids}). "
            "Refusing to pick one, modify one, or create another."
        )
    if len(snapshot.identity_apps) > 1:
        ids = ", ".join(app.app_id for app in snapshot.identity_apps)
        return _abort(
            f"multiple Entra applications are named {identity_name!r} ({ids}). "
            "Refusing to provision beside an ambiguous identity name."
        )

    blueprint = snapshot.blueprint_apps[0] if snapshot.blueprint_apps else None
    identity = snapshot.identity_apps[0] if snapshot.identity_apps else None

    if snapshot.anchor_app is not None and blueprint is not None:
        same_object = (
            snapshot.anchor_app.app_id == blueprint.app_id
            and snapshot.anchor_app.object_id == blueprint.object_id
            and snapshot.anchor_app.display_name == blueprint.display_name
            and snapshot.anchor_app.sign_in_audience == blueprint.sign_in_audience
        )
        if not same_object:
            return _abort(
                "the display-name read and the A365_APP_ID read disagree for "
                f"{anchor} ({snapshot.anchor_app.display_name!r} / {blueprint.display_name!r}). "
                "Refusing to modify either application or to create a duplicate."
            )
    if snapshot.anchor_app is not None and blueprint is None:
        if snapshot.anchor_app.is_agent_blueprint is False:
            return _abort(
                f"per-agent A365_APP_ID {anchor} ({snapshot.anchor_app.display_name!r}) "
                "is not an agent identity blueprint. Refusing to create another "
                "application or to modify this one."
            )
        blueprint_plan = _blueprint_plan_for(
            desired_name=blueprint_name,
            desired_slug=slug,
            actual_name=snapshot.anchor_app.display_name,
            actual_slug=snapshot.anchor_app.blueprint_slug,
        )
        if blueprint_plan.action == "abort":
            return RegistrationDecision(
                action="abort",
                step_names=(),
                reason=(
                    "refusing to rename application "
                    f"{snapshot.anchor_app.app_id}: {blueprint_plan.abort_reason} "
                    "No Entra object was changed."
                ),
                messages=(f"entra: abort — {blueprint_plan.abort_reason}",),
                blueprint_plan=blueprint_plan,
            )
        return _abort(
            f"per-agent A365_APP_ID {anchor} is named "
            f"{snapshot.anchor_app.display_name!r}, not {blueprint_name!r}, and the "
            "display-name query did not return it. Refusing to create another "
            "application or to patch this one."
        )
    if blueprint is None:
        if identity is not None:
            return _abort(
                f"an Entra application named {identity_name!r} already exists "
                f"(appId {identity.app_id}) and no blueprint named {blueprint_name!r} "
                "is owned by this agent. Refusing to create a blueprint beside it."
            )
        if anchor:
            return _abort(
                f"per-agent A365_APP_ID {anchor} did not resolve to "
                f"{blueprint_name!r}. Refusing to create a blueprint while an "
                "ownership anchor is set."
            )
        return RegistrationDecision(
            action="create",
            step_names=CREATE_STEPS,
            reason=None,
            messages=(
                "entra: no existing application named "
                f"{blueprint_name!r} or {identity_name!r}; registration will create.",
            ),
        )

    if not anchor or snapshot.anchor_app is None:
        return _abort(
            f"an Entra application named {blueprint_name!r} already exists "
            f"(appId {blueprint.app_id}) but this agent has no per-agent A365_APP_ID. "
            "Refusing to modify it or to create a duplicate. If you own that "
            f"blueprint, set A365_APP_ID={blueprint.app_id} in "
            f"~/.hermes/agents/{slug}/.env and re-run. Otherwise choose a different "
            "--agent-name."
        )

    if blueprint.is_agent_blueprint is None:
        return _abort(
            "could not tell whether application "
            f"{blueprint.app_id} is an agent identity blueprint. Refusing to provision."
        )

    desired_app = DesiredAppRegistration(
        name=blueprint_name,
        tier=_DESIRED_TIER,
        is_multi_tenant=True,
        fic_required=False,
    )
    actual_tier = _DESIRED_TIER if blueprint.is_agent_blueprint else 2
    actual_app = ActualAppRegistration(
        app_id=blueprint.app_id,
        display_name=blueprint.display_name,
        tier=actual_tier,
        is_multi_tenant=blueprint.sign_in_audience == _MULTI_TENANT_AUDIENCE,
        fic_configured=False,
    )
    app_plan = reconcile_app(desired_app, actual_app)
    blueprint_plan = _blueprint_plan_for(
        desired_name=blueprint_name,
        desired_slug=slug,
        actual_name=blueprint.display_name,
        actual_slug=blueprint.blueprint_slug,
    )
    if app_plan.action == "abort" or blueprint_plan.action == "abort":
        reason = app_plan.abort_reason or blueprint_plan.abort_reason or "reconciliation aborted"
        return RegistrationDecision(
            action="abort",
            step_names=(),
            reason=(
                f"refusing to modify application {blueprint.app_id}: {reason} "
                "No Entra object was changed."
            ),
            messages=(f"entra: abort — {reason}",),
            app_plan=app_plan,
            blueprint_plan=blueprint_plan,
        )
    if app_plan.action == "patch" or blueprint_plan.action == "patch":
        diff = {**blueprint_plan.diff, **app_plan.diff}
        return RegistrationDecision(
            action="abort",
            step_names=(),
            reason=(
                f"owned blueprint {blueprint.app_id} does not match the desired "
                f"registration ({diff}). Refusing to patch display name, slug, or "
                "sign-in audience, and refusing to re-run setup blueprint (that "
                "grants consent and may mint a client secret). No Entra object was changed."
            ),
            messages=(f"entra: abort — drift {diff}",),
            app_plan=app_plan,
            blueprint_plan=blueprint_plan,
        )

    identity_note = ""
    if identity is not None and identity.app_id != blueprint.app_id:
        identity_note = (
            f" An application named {identity_name!r} also exists "
            f"(appId {identity.app_id}) and was not modified."
        )

    if snapshot.service_principal_state == "ambiguous":
        return _abort(
            f"application {blueprint.app_id} has more than one service principal. "
            "Refusing to grant permissions against an ambiguous principal." + identity_note
        )
    if snapshot.service_principal_state == "unreadable":
        detail = snapshot.grant_detail or "the service principal lookup was not trustworthy"
        if "insufficient privileges" in detail or "Application.Read.All" in detail:
            return _abort(detail)
        return _abort(
            f"could not read the service principal for {blueprint.app_id}: {detail} "
            "Refusing to provision."
        )
    if snapshot.service_principal_state != "found":
        reason = (
            f"owned blueprint {blueprint.app_id} has no service principal. "
            "Refusing to re-run `a365 setup blueprint` because that grants admin "
            "consent and may mint a client secret. Re-run with --update-permissions "
            "to repair this owned blueprint." + identity_note
        )
        if not update_permissions:
            return _abort(reason)
        return RegistrationDecision(
            action="repair",
            step_names=CREATE_STEPS,
            reason=None,
            messages=(
                "entra: --update-permissions set; re-running blueprint and permission "
                f"setup for owned app {blueprint.app_id}. This grants consent and may "
                "mint a client secret." + identity_note,
            ),
            app_plan=app_plan,
            blueprint_plan=blueprint_plan,
        )

    if snapshot.grant_state == "unreadable":
        detail = snapshot.grant_detail or GRAPH_READ_PERMISSION_HINT
        return _abort(
            "could not tell whether MCP and bot permission setup already exist on "
            f"{blueprint.app_id}: {detail} Refusing to run permission setup."
        )
    if snapshot.grant_state == "not-queried":
        return _abort(
            f"permission grants for {blueprint.app_id} were not read. "
            "Refusing to run permission setup."
        )

    mcp_done = snapshot.mcp_permission_configured
    bot_done = snapshot.bot_permission_configured
    if mcp_done and bot_done and not update_permissions:
        return RegistrationDecision(
            action="noop",
            step_names=(),
            reason=None,
            messages=(
                f"entra: blueprint {blueprint.app_id} matches desired state and "
                "MCP and bot permission setup have already landed. No Entra changes."
                + identity_note,
            ),
            app_plan=app_plan,
            blueprint_plan=blueprint_plan,
        )
    if mcp_done and bot_done and update_permissions:
        return RegistrationDecision(
            action="repair",
            step_names=PERMISSION_STEPS,
            reason=None,
            messages=(
                "entra: --update-permissions set; re-running permission setup for "
                f"owned blueprint {blueprint.app_id}. setup blueprint will not run. "
                "This can add consent." + identity_note,
            ),
            app_plan=app_plan,
            blueprint_plan=blueprint_plan,
        )

    # A missing marker is unfinished permission setup. Microsoft Graph admin
    # consent (what setup blueprint writes) is not a marker, so it does not
    # skip these steps. A marker that already landed is not run again.
    step_names: tuple[StepName, ...] = tuple(
        name
        for name, done in (
            ("permissions-mcp", mcp_done),
            ("permissions-bot", bot_done),
        )
        if not done
    )
    missing = [label for label, done in (("MCP", mcp_done), ("bot", bot_done)) if not done]
    joined = " and ".join(missing)
    return RegistrationDecision(
        action="repair",
        step_names=step_names,
        reason=None,
        messages=(
            f"entra: blueprint {blueprint.app_id} matches. {joined} permission "
            "setup has not landed (Microsoft Graph admin consent from "
            "`a365 setup blueprint` does not count). "
            + " ".join(step_names)
            + " will run. setup blueprint will not run."
            + identity_note,
        ),
        app_plan=app_plan,
        blueprint_plan=blueprint_plan,
    )
