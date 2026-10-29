"""Client secret expiry checks for ``doctor`` and ``status`` (#191).

Reads only ``passwordCredentials[].endDateTime`` (and ``keyId`` for
disambiguation) via Graph — never secret values, hints, or custom keys.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from . import bot_service
from ._common import parse_env
from .entra_state import (
    GraphGet,
    _is_denied,
    _is_not_found,
    _public_error,
    application_client_secret_expiry_url,
    graph_get_from_mutator_for_secret_expiry,
)
from .mutator import Mutator, get_mutator

DEFAULT_CLIENT_SECRET_EXPIRY_WARN_DAYS = 30

CheckState = Literal["ok", "warn", "error", "skipped"]

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_graph_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _days_remaining(*, end: datetime, now: datetime) -> int:
    delta = end - now
    if delta.total_seconds() <= 0:
        return 0
    return delta.days


def select_nearest_active_credential_end(
    credentials: list[dict[str, Any]],
    *,
    now: datetime,
) -> tuple[datetime | None, str | None, str | None]:
    """Return ``(end, key_id, failure_reason)``.

    Among credentials whose ``endDateTime`` is strictly after ``now``, choose
    the minimum ``endDateTime`` (soonest expiry). When none qualify, ``end``
    is ``None`` and ``failure_reason`` explains why.
    """
    parsed: list[tuple[datetime, str | None]] = []
    for item in credentials:
        if not isinstance(item, dict):
            continue
        end = _parse_graph_datetime(item.get("endDateTime"))
        if end is None:
            continue
        key_id = item.get("keyId")
        key_label = str(key_id) if isinstance(key_id, str) and key_id else None
        parsed.append((end, key_label))
    if not parsed:
        return None, None, "passwordCredentials entries lacked parseable endDateTime"
    active = [(end, key) for end, key in parsed if end > now]
    if not active:
        return None, None, "no active client secret (all credentials expired)"
    chosen_end, chosen_key = min(active, key=lambda pair: pair[0])
    return chosen_end, chosen_key, None


def classify_client_secret_expiry(
    *,
    end: datetime | None,
    now: datetime,
    warn_days: int,
    unreadable_reason: str | None = None,
) -> tuple[CheckState, str, dict[str, Any]]:
    data: dict[str, Any] = {"warn_days": warn_days}
    if unreadable_reason:
        data["check"] = "unavailable"
        return (
            "warn",
            f"could not check client secret expiry ({unreadable_reason})",
            data,
        )
    if end is None:
        data["check"] = "missing"
        return (
            "error",
            "no valid client secret found (add or rotate credentials in Entra)",
            data,
        )
    data["end_date_time"] = end.isoformat().replace("+00:00", "Z")
    days = _days_remaining(end=end, now=now)
    data["days_remaining"] = days
    if end <= now:
        data["check"] = "expired"
        return (
            "error",
            f"client secret expired on {data['end_date_time']} ({days} days remaining)",
            data,
        )
    if days <= warn_days:
        data["check"] = "expiring"
        return (
            "warn",
            f"client secret expires on {data['end_date_time']} ({days} day(s) remaining)",
            data,
        )
    data["check"] = "healthy"
    return (
        "ok",
        f"client secret expires on {data['end_date_time']} ({days} day(s) remaining)",
        data,
    )


def _sanitize_credential_list(raw: object) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                key: value
                for key, value in item.items()
                if key in {"endDateTime", "startDateTime", "keyId", "displayName"}
            }
        )
    return cleaned


def check_app_client_secret_expiry(
    app_id: str,
    *,
    graph_get: GraphGet,
    now: datetime,
    warn_days: int = DEFAULT_CLIENT_SECRET_EXPIRY_WARN_DAYS,
    role: str = "application",
) -> tuple[CheckState, str, dict[str, Any]]:
    try:
        canonical = str(UUID(app_id.strip()))
    except ValueError:
        return (
            "warn",
            f"could not check {role} client secret expiry (app id is not a GUID)",
            {"role": role, "check": "unavailable"},
        )
    url = application_client_secret_expiry_url(canonical)
    payload = graph_get(url)
    if payload is None:
        return (
            "warn",
            f"could not check {role} client secret expiry (Graph read failed)",
            {"role": role, "app_id": canonical, "check": "unavailable"},
        )
    if _is_denied(payload):
        return (
            "warn",
            f"could not check {role} client secret expiry ({_public_error(payload)})",
            {"role": role, "app_id": canonical, "check": "unavailable"},
        )
    if _is_not_found(payload):
        return (
            "error",
            f"{role} Entra application not found (appId={canonical[:8]}…)",
            {"role": role, "app_id": canonical, "check": "missing_app"},
        )
    if "error" in payload and isinstance(payload.get("error"), dict):
        return (
            "warn",
            f"could not check {role} client secret expiry ({_public_error(payload)})",
            {"role": role, "app_id": canonical, "check": "unavailable"},
        )
    creds = _sanitize_credential_list(payload.get("passwordCredentials"))
    if creds is None:
        return (
            "warn",
            f"could not check {role} client secret expiry (malformed Graph response)",
            {"role": role, "app_id": canonical, "check": "unavailable"},
        )
    if not creds:
        return (
            "error",
            f"no client secret configured for {role} application",
            {"role": role, "app_id": canonical, "check": "missing"},
        )
    end, key_id, failure = select_nearest_active_credential_end(creds, now=now)
    if end is None:
        reason = failure or "no active client secret"
        return (
            "error",
            f"no valid client secret found ({reason}); rotate credentials in Entra",
            {"role": role, "app_id": canonical, "check": "missing", "reason": reason},
        )
    state, detail, data = classify_client_secret_expiry(
        end=end,
        now=now,
        warn_days=warn_days,
    )
    data = {**data, "role": role, "app_id": canonical}
    if key_id:
        data["key_id"] = key_id
    return state, detail, data


_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        return parse_env(path.read_text())
    except OSError:
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
    blueprint = (os.environ.get("A365_APP_ID") or "").strip() or (
        home_env.get("A365_APP_ID") or ""
    ).strip()
    if agent_name:
        agent_env = _read_env_file(hermes_home / "agents" / agent_name / ".env")
        blueprint = (agent_env.get("A365_APP_ID") or "").strip() or blueprint
    if not blueprint:
        blueprint = _generated_blueprint_id(hermes_home, generated_config_path)
    return blueprint.strip()


def resolve_bf_app_id(
    *,
    hermes_home: Path,
    sidecar_path: Path,
    operator_env: dict[str, str] | None = None,
) -> str:
    env = operator_env if operator_env is not None else _read_env_file(hermes_home / ".env")
    bf = (os.environ.get("A365_BF_APP_ID") or "").strip() or (
        env.get("A365_BF_APP_ID") or ""
    ).strip()
    if bf:
        return bf
    if not sidecar_path.is_file():
        return ""
    try:
        config = bot_service.BotServiceConfig.from_file(sidecar_path)
    except (OSError, ValueError):
        return ""
    return (config.msaAppId or "").strip()


@dataclass(frozen=True)
class ClientSecretExpiryTargets:
    blueprint_app_id: str
    bf_app_id: str
    bf_configured: bool


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
    bf_configured = sidecar_path.is_file()
    bf = resolve_bf_app_id(
        hermes_home=hermes_home,
        sidecar_path=sidecar_path,
        operator_env=operator_env,
    )
    return ClientSecretExpiryTargets(
        blueprint_app_id=blueprint,
        bf_app_id=bf,
        bf_configured=bf_configured,
    )


def build_graph_get(mutator: Mutator | None = None) -> GraphGet | None:
    mut = mutator if mutator is not None else get_mutator()
    if not getattr(mut, "available", True):
        return None
    return graph_get_from_mutator_for_secret_expiry(mut)


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
) -> list[tuple[str, CheckState, str, dict[str, Any]]]:
    """Return per-app rows: ``(probe_name, state, detail, data)``."""
    clock = now or _utc_now
    instant = clock()
    targets = resolve_secret_expiry_targets(
        hermes_home=hermes_home,
        agent_name=agent_name,
        sidecar_path=sidecar_path,
        generated_config_path=generated_config_path,
        operator_env=operator_env,
    )
    rows: list[tuple[str, CheckState, str, dict[str, Any]]] = []

    def skip_row(name: str, detail: str) -> None:
        rows.append((name, "skipped", detail, {"check": "skipped"}))

    if skip_reason:
        skip_row("blueprint_client_secret", f"skipped; {skip_reason}")
        skip_row("bf_client_secret", f"skipped; {skip_reason}")
        return rows

    getter = graph_get if graph_get is not None else build_graph_get(mutator)
    if getter is None:
        unavailable = "a365/az Graph read unavailable"
        for name, role in (
            ("blueprint_client_secret", "blueprint"),
            ("bf_client_secret", "Bot Framework"),
        ):
            rows.append(
                (
                    name,
                    "warn",
                    f"could not check {role} client secret expiry ({unavailable})",
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
        )
        rows.append(("blueprint_client_secret", state, detail, data))
    else:
        skip_row(
            "blueprint_client_secret",
            "skipped; no blueprint app id configured (A365_APP_ID)",
        )

    if not targets.bf_configured:
        skip_row("bf_client_secret", "skipped; Path B sidecar absent")
    elif not targets.bf_app_id:
        rows.append(
            (
                "bf_client_secret",
                "warn",
                "could not check Bot Framework client secret expiry "
                "(A365_BF_APP_ID or sidecar msaAppId missing)",
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
        )
        rows.append(("bf_client_secret", state, detail, data))
    return rows


def worst_check_state(states: list[CheckState]) -> CheckState:
    if any(state == "error" for state in states):
        return "error"
    if any(state == "warn" for state in states):
        return "warn"
    if any(state == "skipped" for state in states) and all(
        state in ("ok", "skipped") for state in states
    ):
        return "ok"
    return "ok"
