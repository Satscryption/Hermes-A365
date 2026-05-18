"""Read-only Path B Bot Service diagnostics shared by doctor and status."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import activity_bridge, bot_service
from ._common import parse_env

DiagnosticState = Literal["ok", "warn", "error", "skipped"]

_OK = "ok"
_WARN = "warn"
_ERROR = "error"

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"
_REQUIRED_SIDECAR_FIELDS = (
    "subscriptionId",
    "resourceGroup",
    "botName",
    "msaAppId",
    "messagingEndpoint",
    "armResourceId",
)


@dataclass
class DiagnosticResult:
    name: str
    state: DiagnosticState
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


RuntimeAuthProbe = Callable[[bot_service.BotServiceConfig], DiagnosticResult]


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _load_operator_env(hermes_home: Path | None = None) -> dict[str, str]:
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    env_file = hermes_home / ".env"
    if not env_file.exists():
        return {}
    return parse_env(env_file.read_text())


def _load_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = json.loads(path.read_text())
    except OSError as e:
        return None, f"unreadable: {e}"
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(raw, dict):
        return None, f"JSON {type(raw).__name__}, expected object"
    return raw, None


def probe_bot_service_config(
    sidecar_path: Path,
) -> tuple[DiagnosticResult, bot_service.BotServiceConfig | None]:
    raw, error = _load_json_object(sidecar_path)
    if error is not None or raw is None:
        return (
            DiagnosticResult(
                "bot_service_config",
                _ERROR,
                f"{sidecar_path} {error}",
                {"sidecar": str(sidecar_path)},
            ),
            None,
        )

    missing = [key for key in _REQUIRED_SIDECAR_FIELDS if not str(raw.get(key) or "").strip()]
    if missing:
        return (
            DiagnosticResult(
                "bot_service_config",
                _ERROR,
                f"{sidecar_path} missing required fields: {missing}",
                {"sidecar": str(sidecar_path), "missing_fields": missing},
            ),
            None,
        )
    try:
        config = bot_service.BotServiceConfig.from_file(sidecar_path)
    except bot_service.BotServiceError as e:
        return (
            DiagnosticResult(
                "bot_service_config",
                _ERROR,
                str(e),
                {"sidecar": str(sidecar_path)},
            ),
            None,
        )
    return (
        DiagnosticResult(
            "bot_service_config",
            _OK,
            f"{sidecar_path} parsed ({config.resourceGroup}/{config.botName})",
            {
                "sidecar": str(sidecar_path),
                "resource_group": config.resourceGroup,
                "bot_name": config.botName,
                "msa_app_id": config.msaAppId,
                "endpoint": config.messagingEndpoint,
            },
        ),
        config,
    )


def _expected_msa_app_id(
    generated_config_path: Path,
    *,
    operator_env: dict[str, str],
) -> tuple[str | None, str | None, dict[str, Any]]:
    generated: dict[str, Any] = {}
    if generated_config_path.exists():
        loaded, _ = _load_json_object(generated_config_path)
        generated = loaded or {}

    candidates = (
        ("a365.generated.config.json::botMsaAppId", generated.get("botMsaAppId")),
        ("a365.generated.config.json::botId", generated.get("botId")),
        ("~/.hermes/.env A365_BF_APP_ID", operator_env.get("A365_BF_APP_ID")),
        ("a365.generated.config.json::agentBlueprintId", generated.get("agentBlueprintId")),
    )
    for source, value in candidates:
        text = str(value or "").strip()
        if text:
            return text, source, generated
    return None, None, generated


def probe_bot_service_msa_app_id(
    config: bot_service.BotServiceConfig,
    *,
    generated_config_path: Path,
    operator_env: dict[str, str] | None = None,
) -> DiagnosticResult:
    if operator_env is None:
        operator_env = _load_operator_env()
    expected, source, generated = _expected_msa_app_id(
        generated_config_path,
        operator_env=operator_env,
    )
    data = {
        "sidecar_msa_app_id": config.msaAppId,
        "generated_config": str(generated_config_path),
        "expected_source": source,
    }
    if expected is None:
        return DiagnosticResult(
            "bot_service_msa_app_id",
            _WARN,
            "could not find botMsaAppId, botId, A365_BF_APP_ID, or agentBlueprintId to compare",
            data,
        )
    data["expected_msa_app_id"] = expected
    if config.msaAppId.lower() != expected.lower():
        return DiagnosticResult(
            "bot_service_msa_app_id",
            _ERROR,
            f"sidecar msaAppId={config.msaAppId}; expected {expected} from {source}",
            data,
        )
    return DiagnosticResult(
        "bot_service_msa_app_id",
        _OK,
        f"sidecar msaAppId matches {source}",
        {**data, "generated_keys": sorted(generated)},
    )


def probe_bot_service_az_subscription(
    config: bot_service.BotServiceConfig,
    *,
    runner: bot_service.CommandRunner,
) -> DiagnosticResult:
    result = runner.run(["az", "account", "show", "-o", "json"])
    if result.returncode != 0:
        return DiagnosticResult(
            "bot_service_az_subscription",
            _WARN,
            result.output or "az account show failed (run `az login`)",
        )
    try:
        account = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return DiagnosticResult(
            "bot_service_az_subscription",
            _WARN,
            f"az account show returned non-JSON output: {e}",
        )
    subscription_id = str(account.get("id") or "").strip()
    if not subscription_id:
        return DiagnosticResult(
            "bot_service_az_subscription",
            _WARN,
            "az account show did not return a subscription id",
        )
    if config.subscriptionId and subscription_id.lower() != config.subscriptionId.lower():
        return DiagnosticResult(
            "bot_service_az_subscription",
            _ERROR,
            f"active subscription={subscription_id}; sidecar expects {config.subscriptionId}",
            {"active_subscription": subscription_id, "sidecar_subscription": config.subscriptionId},
        )
    return DiagnosticResult(
        "bot_service_az_subscription",
        _OK,
        f"active subscription matches sidecar ({subscription_id})",
        {"active_subscription": subscription_id},
    )


def _bot_show(
    config: bot_service.BotServiceConfig,
    *,
    runner: bot_service.CommandRunner,
) -> dict[str, Any] | None:
    result = runner.run(
        [
            "az",
            "bot",
            "show",
            "--resource-group",
            config.resourceGroup,
            "--name",
            config.botName,
            "-o",
            "json",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _bot_properties(bot: dict[str, Any]) -> dict[str, Any]:
    props = bot.get("properties")
    return props if isinstance(props, dict) else {}


def _bot_msa_app_id(bot: dict[str, Any]) -> str:
    props = _bot_properties(bot)
    return str(props.get("msaAppId") or bot.get("msaAppId") or "")


def _bot_endpoint(bot: dict[str, Any]) -> str:
    props = _bot_properties(bot)
    return str(props.get("endpoint") or bot.get("endpoint") or "")


def probe_bot_service_resource(
    config: bot_service.BotServiceConfig,
    *,
    runner: bot_service.CommandRunner,
) -> DiagnosticResult:
    bot = _bot_show(config, runner=runner)
    if bot is None:
        return DiagnosticResult(
            "bot_service_resource",
            _ERROR,
            f"az bot show could not find {config.resourceGroup}/{config.botName}",
        )
    actual_msa = _bot_msa_app_id(bot)
    endpoint = _bot_endpoint(bot)
    data = {"actual_msa_app_id": actual_msa, "actual_endpoint": endpoint}
    if actual_msa.lower() != config.msaAppId.lower():
        return DiagnosticResult(
            "bot_service_resource",
            _ERROR,
            f"Azure bot msaAppId={actual_msa}; sidecar expects {config.msaAppId}",
            data,
        )
    if endpoint.rstrip("/") != config.messagingEndpoint.rstrip("/"):
        return DiagnosticResult(
            "bot_service_resource",
            _WARN,
            f"Azure endpoint={endpoint}; sidecar expects {config.messagingEndpoint}",
            data,
        )
    return DiagnosticResult(
        "bot_service_resource",
        _OK,
        f"bot exists and matches sidecar ({config.botName})",
        data,
    )


def probe_bot_service_channel_msteams(
    config: bot_service.BotServiceConfig,
    *,
    runner: bot_service.CommandRunner,
) -> DiagnosticResult:
    result = runner.run(
        [
            "az",
            "bot",
            "msteams",
            "show",
            "--resource-group",
            config.resourceGroup,
            "--name",
            config.botName,
            "-o",
            "json",
        ]
    )
    if result.returncode != 0:
        return DiagnosticResult(
            "bot_service_channel_msteams",
            _WARN,
            "Microsoft Teams channel is not enabled",
        )
    try:
        channel = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return DiagnosticResult(
            "bot_service_channel_msteams",
            _WARN,
            f"az bot msteams show returned non-JSON output: {e}",
        )
    props = channel.get("properties") if isinstance(channel, dict) else {}
    nested = props.get("properties") if isinstance(props, dict) else {}
    if isinstance(nested, dict):
        accepted = bool(nested.get("acceptedTerms"))
        enabled = bool(nested.get("isEnabled", True))
    else:
        accepted = bool(props.get("acceptedTerms")) if isinstance(props, dict) else False
        enabled = bool(props.get("isEnabled", True)) if isinstance(props, dict) else False
    if not enabled or not accepted:
        return DiagnosticResult(
            "bot_service_channel_msteams",
            _WARN,
            "Microsoft Teams channel exists but acceptedTerms/isEnabled is false",
            {"acceptedTerms": accepted, "isEnabled": enabled},
        )
    return DiagnosticResult(
        "bot_service_channel_msteams",
        _OK,
        "Microsoft Teams channel enabled with acceptedTerms=true",
        {"acceptedTerms": accepted, "isEnabled": enabled},
    )


def default_runtime_auth_probe(config: bot_service.BotServiceConfig) -> DiagnosticResult:
    has_bf_validator = hasattr(activity_bridge, "validate_inbound_jwt_bf")
    has_dispatcher = hasattr(activity_bridge, "acquire_reply_token")
    has_bf_token = hasattr(activity_bridge, "acquire_bf_s2s_token")
    data = {
        "has_validate_inbound_jwt_bf": has_bf_validator,
        "has_acquire_reply_token": has_dispatcher,
        "has_acquire_bf_s2s_token": has_bf_token,
        "msa_app_id": config.msaAppId,
    }
    if has_bf_validator and has_dispatcher and has_bf_token:
        return DiagnosticResult(
            "bot_service_runtime_auth",
            _OK,
            "Path B BF-token validator and reply-token dispatcher are present",
            data,
        )
    return DiagnosticResult(
        "bot_service_runtime_auth",
        _ERROR,
        "Path B runtime auth path is incomplete; standalone activity-bridge serve must "
        "validate BF Connector tokens and dispatch replies through the BF-token path",
        data,
    )


def collect_bot_service_diagnostics(
    *,
    sidecar_path: Path,
    generated_config_path: Path,
    no_network: bool = False,
    runner: bot_service.CommandRunner | None = None,
    operator_env: dict[str, str] | None = None,
    runtime_auth_probe: RuntimeAuthProbe | None = None,
) -> list[DiagnosticResult]:
    if not sidecar_path.exists():
        return []

    config_result, config = probe_bot_service_config(sidecar_path)
    results = [config_result]
    if config is None:
        return results

    results.append(
        probe_bot_service_msa_app_id(
            config,
            generated_config_path=generated_config_path,
            operator_env=operator_env,
        )
    )
    if no_network:
        return results

    if runner is None:
        runner = bot_service.SubprocessRunner()
    if runtime_auth_probe is None:
        runtime_auth_probe = default_runtime_auth_probe

    results.append(probe_bot_service_az_subscription(config, runner=runner))
    results.append(probe_bot_service_resource(config, runner=runner))
    results.append(probe_bot_service_channel_msteams(config, runner=runner))
    results.append(runtime_auth_probe(config))
    return results
