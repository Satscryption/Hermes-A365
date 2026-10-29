"""Client secret expiry checks for ``doctor`` and ``status`` (#191).

Reads only ``passwordCredentials`` ``startDateTime`` / ``endDateTime`` metadata
via Graph — never secret values, key ids, or hints.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from . import bot_service
from ._common import parse_env, slugify, validate_slug
from .bot_service import BotServiceError
from .entra_state import (
    _ACCESS_DENIED_CODES,
    GraphGet,
    _is_denied,
    _is_not_found,
    _parse_expiry_response_bytes,
    application_client_secret_expiry_url,
    assert_client_secret_expiry_read_url,
    graph_error_code,
    graph_get_from_mutator_for_secret_expiry,
)
from .mutator import Mutator, get_mutator

DEFAULT_CLIENT_SECRET_EXPIRY_WARN_DAYS = 30
GRAPH_SECRET_EXPIRY_REST_TIMEOUT_SEC = 15.0

CheckState = Literal["ok", "warn", "error", "skipped"]

Clock = Callable[[], datetime]

_ALLOWLISTED_GRAPH_ERROR_CODES = _ACCESS_DENIED_CODES | frozenset(
    {
        "UnreadableGraphResponse",
        "Request_ResourceNotFound",
        "ResourceNotFound",
        "ErrorItemNotFound",
        "itemNotFound",
        "UnknownError",
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_end_z(end: datetime) -> str:
    return end.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_graph_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None


def _days_remaining(*, end: datetime, now: datetime) -> int:
    return (end.astimezone(UTC) - now.astimezone(UTC)).days


def _unavailable_state(*, for_status: bool) -> CheckState:
    return "skipped" if for_status else "warn"


def _unavailable_detail(*, role: str, code: str | None = None) -> str:
    if code in _ACCESS_DENIED_CODES:
        return f"could not check {role} client secret expiry (Graph access denied)"
    if code and code in _ALLOWLISTED_GRAPH_ERROR_CODES:
        return f"could not check {role} client secret expiry (Graph {code})"
    return f"could not check {role} client secret expiry"


def _parse_password_credential_windows(
    raw: object,
) -> tuple[list[tuple[datetime, datetime]] | None, bool]:
    """Return ``(windows, malformed)``. ``malformed`` is True when unreadable."""
    if not isinstance(raw, list):
        return None, True
    windows: list[tuple[datetime, datetime]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None, True
        start = _parse_graph_datetime(item.get("startDateTime"))
        end = _parse_graph_datetime(item.get("endDateTime"))
        if start is None or end is None:
            return None, True
        windows.append((start, end))
    return windows, False


def select_latest_active_credential_end(
    windows: list[tuple[datetime, datetime]],
    *,
    now: datetime,
) -> tuple[datetime | None, datetime | None, str | None, int]:
    """Return ``(active_end, latest_past_end, failure_reason, active_count)``."""
    active = [(start, end) for start, end in windows if start <= now < end]
    if active:
        return max(end for _start, end in active), None, None, len(active)
    if any(end > now for _start, end in windows):
        return None, None, "no_active_yet", 0
    past_ends = [end for start, end in windows if end <= now]
    if past_ends:
        return None, max(past_ends), "all client secrets expired", 0
    return None, None, "no client secret in its active window", 0


def _healthy_detail(*, end_z: str, days: int, active_count: int) -> str:
    count_label = f" ({active_count} active passwords)" if active_count > 1 else ""
    return (
        f"latest active client secret{count_label} expires on {end_z} "
        f"({days} day(s) remaining); does not confirm which credential the "
        "deployment uses; older active secrets may expire sooner"
    )


def classify_client_secret_expiry(
    *,
    end: datetime,
    now: datetime,
    warn_days: int,
    active_count: int = 1,
) -> tuple[CheckState, str, dict[str, Any]]:
    data: dict[str, Any] = {
        "warn_days": warn_days,
        "end_date_time": _format_end_z(end),
    }
    if active_count > 1:
        data["active_password_count"] = active_count
    days = _days_remaining(end=end, now=now)
    data["days_remaining"] = days
    end_z = data["end_date_time"]
    if end <= now:
        data["check"] = "expired"
        return (
            "error",
            f"last active client secret expired on {end_z} ({days} days remaining)",
            data,
        )
    if days <= warn_days:
        data["check"] = "expiring"
        return (
            "warn",
            _healthy_detail(end_z=end_z, days=days, active_count=active_count),
            data,
        )
    data["check"] = "healthy"
    return (
        "ok",
        _healthy_detail(end_z=end_z, days=days, active_count=active_count),
        data,
    )


def _az_rest_graph_get(url: str) -> dict[str, Any] | None:
    assert_client_secret_expiry_read_url(url)
    if not shutil.which("az"):
        return None
    if urllib.parse.urlparse(url).hostname != "graph.microsoft.com":
        return None
    try:
        proc = subprocess.run(
            [
                "az",
                "rest",
                "--method",
                "GET",
                "--url",
                url,
                "--headers",
                "ConsistencyLevel=eventual",
            ],
            capture_output=True,
            text=False,
            timeout=GRAPH_SECRET_EXPIRY_REST_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout or b""
    parsed = _parse_expiry_response_bytes(raw)
    if parsed is None:
        return None
    if isinstance(parsed.get("error"), dict):
        code = str(parsed["error"].get("code") or "")
        if code == "UnreadableGraphResponse":
            return parsed
        return None
    return parsed


def graph_get_from_az_rest() -> GraphGet:
    def graph_get(url: str) -> dict[str, Any] | None:
        return _az_rest_graph_get(url)

    return graph_get


def build_graph_get(mutator: Mutator | None = None) -> GraphGet | None:
    mut = mutator if mutator is not None else get_mutator()
    if getattr(mut, "available", True):
        return graph_get_from_mutator_for_secret_expiry(
            mut, timeout=GRAPH_SECRET_EXPIRY_REST_TIMEOUT_SEC
        )
    if shutil.which("az"):
        return graph_get_from_az_rest()
    return None


def check_app_client_secret_expiry(
    app_id: str,
    *,
    graph_get: GraphGet,
    now: datetime,
    warn_days: int = DEFAULT_CLIENT_SECRET_EXPIRY_WARN_DAYS,
    role: str = "application",
    for_status: bool = False,
) -> tuple[CheckState, str, dict[str, Any]]:
    try:
        canonical = str(UUID(app_id.strip()))
    except ValueError:
        state = _unavailable_state(for_status=for_status)
        return (
            state,
            _unavailable_detail(role=role),
            {"role": role, "check": "unavailable"},
        )
    url = application_client_secret_expiry_url(canonical)
    payload = graph_get(url)
    if payload is None:
        state = _unavailable_state(for_status=for_status)
        return (
            state,
            _unavailable_detail(role=role),
            {"role": role, "check": "unavailable"},
        )
    if _is_denied(payload):
        code = graph_error_code(payload)
        state = _unavailable_state(for_status=for_status)
        return (
            state,
            _unavailable_detail(role=role, code=code or "Authorization_RequestDenied"),
            {"role": role, "check": "unavailable"},
        )
    if _is_not_found(payload):
        return (
            "error",
            f"{role} Entra application not found",
            {"role": role, "check": "missing_app"},
        )
    if isinstance(payload.get("error"), dict):
        code = graph_error_code(payload)
        state = _unavailable_state(for_status=for_status)
        return (
            state,
            _unavailable_detail(role=role, code=code or "UnknownError"),
            {"role": role, "check": "unavailable"},
        )
    if "passwordCredentials" not in payload:
        state = _unavailable_state(for_status=for_status)
        return (
            state,
            _unavailable_detail(role=role),
            {"role": role, "check": "unavailable"},
        )
    windows, malformed = _parse_password_credential_windows(payload.get("passwordCredentials"))
    if malformed or windows is None:
        state = _unavailable_state(for_status=for_status)
        return (
            state,
            _unavailable_detail(role=role),
            {"role": role, "check": "unavailable"},
        )
    if not windows:
        return (
            "skipped",
            f"no client secret to monitor for {role} application",
            {"role": role, "check": "no_password_secret"},
        )
    active_end, latest_past, failure, active_count = select_latest_active_credential_end(
        windows, now=now
    )
    if active_end is not None:
        state, detail, data = classify_client_secret_expiry(
            end=active_end,
            now=now,
            warn_days=warn_days,
            active_count=active_count,
        )
        return state, detail, {**data, "role": role}
    if failure == "no_active_yet":
        state: CheckState = "skipped" if for_status else "warn"
        return (
            state,
            f"no active client secret yet for {role} application",
            {"role": role, "check": "no_active_yet"},
        )
    if latest_past is not None:
        data = {
            "warn_days": warn_days,
            "end_date_time": _format_end_z(latest_past),
            "days_remaining": _days_remaining(end=latest_past, now=now),
            "check": "expired",
            "role": role,
        }
        return (
            "error",
            f"last active client secret expired on {data['end_date_time']} "
            f"({data['days_remaining']} days remaining)",
            data,
        )
    reason = failure or "no client secret in its active window"
    return (
        "error",
        f"no valid client secret found ({reason})",
        {"role": role, "check": "missing", "reason": reason},
    )


_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        return parse_env(path.read_text())
    except OSError:
        return {}


def _read_agent_env(hermes_home: Path, agent_name: str) -> dict[str, str]:
    """Match ``status`` local_config: original name first, then slugified path."""
    candidates = [agent_name]
    derived = slugify(agent_name)
    if derived and derived != agent_name:
        candidates.append(derived)
    for candidate in candidates:
        try:
            validate_slug(candidate)
        except ValueError:
            continue
        probe = hermes_home / "agents" / candidate / ".env"
        if probe.is_file():
            return _read_env_file(probe)
    return {}


def _generated_blueprint_id(home: Path, generated_config_path: Path) -> str:
    candidates = [
        os.environ.get("A365_GENERATED_CONFIG_PATH") or "",
        str(generated_config_path),
        str(home / "a365.generated.config.json"),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(os.path.expanduser(raw))
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            blueprint = str(payload.get("agentBlueprintId") or "").strip()
            if blueprint:
                return blueprint
    return ""


def resolve_blueprint_app_id(
    *,
    hermes_home: Path,
    agent_name: str | None,
    generated_config_path: Path,
) -> str:
    home_env = _read_env_file(hermes_home / ".env")
    gateway = (os.environ.get("A365_APP_ID") or "").strip() or (
        home_env.get("A365_APP_ID") or ""
    ).strip()
    if agent_name:
        agent_env = _read_agent_env(hermes_home, agent_name)
        blueprint = (agent_env.get("A365_APP_ID") or "").strip() or gateway
    else:
        blueprint = gateway
    if not blueprint:
        blueprint = _generated_blueprint_id(hermes_home, generated_config_path)
    return blueprint.strip()


def resolve_bf_app_id(
    *,
    hermes_home: Path,
    sidecar_path: Path,
    agent_name: str | None = None,
    operator_env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Return ``(app_id, sidecar_unreadable)``."""
    env = dict(operator_env or _read_env_file(hermes_home / ".env"))
    if agent_name:
        env = {**env, **_read_agent_env(hermes_home, agent_name)}
    bf = (os.environ.get("A365_BF_APP_ID") or "").strip() or (
        env.get("A365_BF_APP_ID") or ""
    ).strip()
    if bf:
        return bf, False
    if not sidecar_path.is_file():
        return "", False
    try:
        config = bot_service.BotServiceConfig.from_file(sidecar_path)
    except BotServiceError:
        return "", True
    except (OSError, ValueError):
        return "", True
    return (config.msaAppId or "").strip(), False


@dataclass(frozen=True)
class ClientSecretExpiryTargets:
    blueprint_app_id: str
    bf_app_id: str
    bf_configured: bool
    bf_sidecar_unreadable: bool


def resolve_secret_expiry_targets(
    *,
    hermes_home: Path,
    agent_name: str | None,
    sidecar_path: Path,
    generated_config_path: Path,
    operator_env: dict[str, str] | None = None,
) -> ClientSecretExpiryTargets:
    blueprint = resolve_blueprint_app_id(
        hermes_home=hermes_home,
        agent_name=agent_name,
        generated_config_path=generated_config_path,
    )
    bf, unreadable = resolve_bf_app_id(
        hermes_home=hermes_home,
        sidecar_path=sidecar_path,
        agent_name=agent_name,
        operator_env=operator_env,
    )
    bf_configured = bool(bf) or sidecar_path.is_file()
    return ClientSecretExpiryTargets(
        blueprint_app_id=blueprint,
        bf_app_id=bf,
        bf_configured=bf_configured,
        bf_sidecar_unreadable=unreadable,
    )


def gather_client_secret_expiry_checks(
    *,
    hermes_home: Path,
    agent_name: str | None,
    sidecar_path: Path,
    generated_config_path: Path,
    warn_days: int = DEFAULT_CLIENT_SECRET_EXPIRY_WARN_DAYS,
    graph_get: GraphGet | None = None,
    mutator: Mutator | None = None,
    now: Clock | None = None,
    skip_reason: str | None = None,
    operator_env: dict[str, str] | None = None,
    for_status: bool = False,
) -> list[tuple[str, CheckState, str, dict[str, Any]]]:
    """Return per-app rows: ``(probe_name, state, detail, data)``."""
    clock = now or _utc_now
    instant = clock()
    try:
        targets = resolve_secret_expiry_targets(
            hermes_home=hermes_home,
            agent_name=agent_name,
            sidecar_path=sidecar_path,
            generated_config_path=generated_config_path,
            operator_env=operator_env,
        )
    except ValueError:
        unavailable = _unavailable_state(for_status=for_status)
        detail = _unavailable_detail(role="blueprint")
        return [
            ("blueprint_client_secret", unavailable, detail, {"check": "unavailable"}),
            ("bf_client_secret", unavailable, detail, {"check": "unavailable"}),
        ]

    rows: list[tuple[str, CheckState, str, dict[str, Any]]] = []

    def skip_row(name: str, detail: str) -> None:
        rows.append((name, "skipped", detail, {"check": "skipped"}))

    if skip_reason:
        skip_row("blueprint_client_secret", f"skipped; {skip_reason}")
        skip_row("bf_client_secret", f"skipped; {skip_reason}")
        return rows

    getter = graph_get if graph_get is not None else build_graph_get(mutator)
    if getter is None:
        state = _unavailable_state(for_status=for_status)
        for name, role in (
            ("blueprint_client_secret", "blueprint"),
            ("bf_client_secret", "Bot Framework"),
        ):
            rows.append(
                (
                    name,
                    state,
                    _unavailable_detail(role=role),
                    {"role": role, "check": "unavailable"},
                )
            )
        return rows

    if targets.blueprint_app_id:
        state, detail, data = check_app_client_secret_expiry(
            targets.blueprint_app_id,
            graph_get=getter,
            now=instant,
            warn_days=warn_days,
            role="blueprint",
            for_status=for_status,
        )
        rows.append(("blueprint_client_secret", state, detail, data))
    else:
        skip_row(
            "blueprint_client_secret",
            "skipped; no blueprint app id configured (A365_APP_ID)",
        )

    if not targets.bf_configured:
        skip_row("bf_client_secret", "skipped; Bot Framework app not configured")
    elif targets.bf_sidecar_unreadable or not targets.bf_app_id:
        state = _unavailable_state(for_status=for_status)
        rows.append(
            (
                "bf_client_secret",
                state,
                _unavailable_detail(role="Bot Framework"),
                {"role": "Bot Framework", "check": "unavailable"},
            )
        )
    else:
        state, detail, data = check_app_client_secret_expiry(
            targets.bf_app_id,
            graph_get=getter,
            now=instant,
            warn_days=warn_days,
            role="Bot Framework",
            for_status=for_status,
        )
        rows.append(("bf_client_secret", state, detail, data))
    return rows


def worst_check_state(states: list[CheckState]) -> CheckState:
    actionable = [state for state in states if state != "skipped"]
    if any(state == "error" for state in actionable):
        return "error"
    if any(state == "warn" for state in actionable):
        return "warn"
    return "ok"


def status_component_state(
    rows: list[tuple[str, CheckState, str, dict[str, Any]]],
) -> CheckState:
    """Map per-app rows to one status component state (exit code unchanged on skip)."""
    overall = worst_check_state([state for _, state, _, _ in rows])
    if overall == "error":
        return "error"
    if overall == "warn":
        return "warn"
    if any(payload.get("check") in ("unavailable", "no_active_yet") for _, _, _, payload in rows):
        return "skipped"
    if all(
        state == "skipped" and payload.get("check") == "skipped" for _, state, _, payload in rows
    ):
        return "skipped"
    return "ok"


def status_component_detail(
    rows: list[tuple[str, CheckState, str, dict[str, Any]]],
) -> str:
    return "; ".join(f"{name}: {detail}" for name, _, detail, _ in rows)
