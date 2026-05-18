"""hermes a365 bot-service — provision and verify Path B Azure Bot Service.

Slice 20a covers the Azure-side registration that makes the Custom
Engine Agent reachable through Bot Framework / Copilot fabric:

- create the resource group if needed
- auto-register the ``Microsoft.BotService`` resource provider
- create or reuse the Azure Bot resource bound to the Path B BF app id
- enable the Teams channel and set the load-bearing ``acceptedTerms`` flag
- write a local ``a365.bot-service.config.json`` sidecar (0600)
- verify the Bot Service resource, channel state, and optional runtime probe

The operator-facing default is dry-run; ``--apply`` performs Azure and
local file mutations.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import MISSING, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib import error, request
from urllib.parse import quote

from ._common import parse_env, slugify

SIDECAR_FILENAME = "a365.bot-service.config.json"
SIDECAR_SCHEMA_VERSION = 1
_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"
_BOT_SERVICE_NAMESPACE = "Microsoft.BotService"
_BOT_API_VERSION = "2022-09-15"
_DEFAULT_REGION = "westeurope"
_DEFAULT_SKU = "F0"


class BotServiceError(RuntimeError):
    """Raised when bot-service create/verify cannot proceed."""


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


class CommandRunner(Protocol):
    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult: ...


class SubprocessRunner:
    """Run Azure CLI commands without involving the GA ``a365`` mutator."""

    def run(self, argv: list[str], *, timeout: float = 120.0) -> CommandResult:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except OSError as e:
            raise BotServiceError(f"failed to run {argv[0]!r}: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise BotServiceError(f"{' '.join(argv)} timed out after {timeout:.0f}s") from e
        return CommandResult(
            argv=list(argv),
            returncode=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
        )


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


def _write_text_atomic(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _normalize_endpoint(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise BotServiceError("--endpoint must be non-empty")
    if not value.startswith(("http://", "https://")):
        raise BotServiceError("--endpoint must be an absolute http(s) URL")
    trimmed = value.rstrip("/")
    if trimmed.endswith("/api/messages"):
        return trimmed
    return f"{trimmed}/api/messages"


def derive_bot_name(agent_name: str) -> str:
    slug = slugify(agent_name)
    if not slug:
        raise BotServiceError("--agent-name must contain at least one alphanumeric character")
    suffix = "-bot"
    max_slug = 42 - len(suffix)
    name = f"{slug[:max_slug].rstrip('-')}{suffix}"
    if len(name) < 4:
        name = f"{name}0000"[:4]
    return name


@dataclass
class BotServiceCreateInputs:
    agent_name: str
    resource_group: str
    endpoint: str
    region: str = _DEFAULT_REGION
    sku: str = _DEFAULT_SKU
    tenant_id: str | None = None
    app_id: str | None = None
    subscription_id: str | None = None
    bot_name: str | None = None
    sidecar_path: Path = field(default_factory=lambda: Path.cwd() / SIDECAR_FILENAME)

    def __post_init__(self) -> None:
        if not self.agent_name.strip():
            raise ValueError("agent_name must be non-empty")
        if not self.resource_group.strip():
            raise ValueError("resource_group must be non-empty")
        self.endpoint = _normalize_endpoint(self.endpoint)
        self.bot_name = self.bot_name or derive_bot_name(self.agent_name)


@dataclass
class BotServiceConfig:
    schemaVersion: int
    subscriptionId: str
    resourceGroup: str
    botName: str
    armResourceId: str
    msaAppId: str
    tenantId: str
    messagingEndpoint: str
    channelsEnabled: list[str]
    createdAt: str
    resourceGroupManaged: bool = False

    @classmethod
    def from_file(cls, path: Path) -> BotServiceConfig:
        if not path.exists():
            raise BotServiceError(f"{path} does not exist; run `bot-service create --apply` first")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise BotServiceError(f"{path} is not valid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise BotServiceError(f"{path} is JSON {type(raw).__name__}, expected object")
        raw.setdefault("resourceGroupManaged", False)
        missing = [
            f.name
            for f in fields(cls)
            if f.name not in raw and f.default is MISSING and f.default_factory is MISSING
        ]
        if missing:
            raise BotServiceError(f"{path} missing required keys: {missing}")
        if raw.get("schemaVersion") != SIDECAR_SCHEMA_VERSION:
            raise BotServiceError(
                f"{path} schemaVersion={raw.get('schemaVersion')!r}; "
                f"expected {SIDECAR_SCHEMA_VERSION}"
            )
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"


@dataclass
class BotServiceCreatePlan:
    inputs: BotServiceCreateInputs
    app_id_source: str
    tenant_id_source: str

    @property
    def bot_name(self) -> str:
        assert self.inputs.bot_name is not None
        return self.inputs.bot_name

    def render_human(self) -> str:
        return "\n".join(
            [
                f"[plan] hermes a365 bot-service create {self.inputs.agent_name}",
                f"  resource group: {self.inputs.resource_group} ({self.inputs.region})",
                f"  bot resource:   {self.bot_name} (location global, sku {self.inputs.sku})",
                f"  app id:         {self.app_id_source}",
                f"  tenant id:      {self.tenant_id_source}",
                f"  endpoint:       {self.inputs.endpoint}",
                f"  sidecar:        {self.inputs.sidecar_path}",
                "  azure steps:",
                f"    - az provider register --namespace {_BOT_SERVICE_NAMESPACE} --wait",
                "    - az group create --name <resource-group> --location <region>",
                "    - az bot create (or no-op if existing bot matches app id)",
                "    - az bot msteams create + acceptedTerms ARM PATCH",
            ]
        )


@dataclass
class BotServiceCreateResult:
    config: BotServiceConfig
    sidecar_path: Path
    created_bot: bool
    created_teams_channel: bool
    patched_teams_terms: bool
    messages: list[str] = field(default_factory=list)


@dataclass
class BotServiceEnableChannelInputs:
    agent_name: str
    channel: str = "msteams"
    sidecar_path: Path = field(default_factory=lambda: Path.cwd() / SIDECAR_FILENAME)

    def __post_init__(self) -> None:
        if not self.agent_name.strip():
            raise ValueError("agent_name must be non-empty")
        self.channel = self.channel.lower().strip()
        if self.channel != "msteams":
            raise ValueError("only --channel msteams is supported in slice 20b")


@dataclass
class BotServiceEnableChannelPlan:
    inputs: BotServiceEnableChannelInputs
    config: BotServiceConfig

    def render_human(self) -> str:
        return "\n".join(
            [
                f"[plan] hermes a365 bot-service enable-channel {self.inputs.agent_name}",
                f"  channel:        {self.inputs.channel}",
                f"  resource group: {self.config.resourceGroup}",
                f"  bot resource:   {self.config.botName}",
                f"  sidecar:        {self.inputs.sidecar_path}",
                "  azure steps:",
                "    - az bot msteams show",
                "    - az bot msteams create (skip if already enabled)",
                "    - acceptedTerms ARM PATCH if terms are not accepted",
            ]
        )


@dataclass
class BotServiceEnableChannelResult:
    config: BotServiceConfig
    sidecar_path: Path
    channel_created: bool
    patched_teams_terms: bool
    messages: list[str] = field(default_factory=list)


@dataclass
class BotServiceUpdateEndpointInputs:
    agent_name: str
    url: str
    sidecar_path: Path = field(default_factory=lambda: Path.cwd() / SIDECAR_FILENAME)

    def __post_init__(self) -> None:
        if not self.agent_name.strip():
            raise ValueError("agent_name must be non-empty")
        self.url = _normalize_endpoint(self.url)


@dataclass
class BotServiceUpdateEndpointPlan:
    inputs: BotServiceUpdateEndpointInputs
    config: BotServiceConfig

    def render_human(self) -> str:
        return "\n".join(
            [
                f"[plan] hermes a365 bot-service update-endpoint {self.inputs.agent_name}",
                f"  resource group: {self.config.resourceGroup}",
                f"  bot resource:   {self.config.botName}",
                f"  current URL:     {self.config.messagingEndpoint}",
                f"  new URL:         {self.inputs.url}",
                f"  sidecar:        {self.inputs.sidecar_path}",
                "  azure step:",
                "    - az bot update --endpoint <new-url> (skip if already current)",
                "  note:",
                "    - Path A uses activity-bridge update-endpoint; run both",
                "      when operating both paths.",
            ]
        )


@dataclass
class BotServiceUpdateEndpointResult:
    config: BotServiceConfig
    sidecar_path: Path
    endpoint_updated: bool
    messages: list[str] = field(default_factory=list)


@dataclass
class BotServiceCleanupInputs:
    agent_name: str
    sidecar_path: Path = field(default_factory=lambda: Path.cwd() / SIDECAR_FILENAME)
    purge_resource_group: bool = False

    def __post_init__(self) -> None:
        if not self.agent_name.strip():
            raise ValueError("agent_name must be non-empty")


@dataclass
class BotServiceCleanupPlan:
    inputs: BotServiceCleanupInputs
    config: BotServiceConfig | None
    sidecar_exists: bool

    def render_human(self) -> str:
        lines = [f"[plan] hermes a365 bot-service cleanup {self.inputs.agent_name}"]
        lines.append(f"  sidecar:        {self.inputs.sidecar_path}")
        if self.config is None:
            lines.append("  bot resource:   (none; sidecar missing)")
            lines.append("  azure steps:    (none)")
        else:
            lines.append(f"  resource group: {self.config.resourceGroup}")
            lines.append(f"  bot resource:   {self.config.botName}")
            lines.append("  azure steps:")
            lines.append("    - az bot msteams delete (best effort)")
            lines.append("    - az bot delete (skip if already gone)")
            if self.inputs.purge_resource_group:
                if self.config.resourceGroupManaged:
                    lines.append("    - az group delete (resourceGroupManaged=true)")
                else:
                    lines.append("    - skip az group delete (resourceGroupManaged=false)")
            else:
                lines.append("    - skip az group delete (--purge-resource-group not set)")
        lines.append("  local steps:")
        lines.append("    - back up and remove a365.bot-service.config.json when present")
        lines.append("  preserved:")
        lines.append("    - Blueprint Entra app + service principal (Path A still depends on it)")
        return "\n".join(lines)


@dataclass
class BotServiceCleanupResult:
    sidecar_path: Path
    bot_deleted: bool = False
    resource_group_deleted: bool = False
    sidecar_backup_path: Path | None = None
    sidecar_removed: bool = False
    messages: list[str] = field(default_factory=list)


Status = Literal["OK", "WARN", "ERROR"]


@dataclass
class ProbeResult:
    name: str
    status: Status
    detail: str

    def render(self) -> str:
        return f"[{self.status:<5}] {self.name}: {self.detail}"


@dataclass
class BotServiceVerifyReport:
    sidecar_path: Path
    results: list[ProbeResult]

    @property
    def ok(self) -> bool:
        return all(r.status != "ERROR" for r in self.results)

    def render_human(self) -> str:
        lines = [f"[verify] hermes a365 bot-service verify {self.sidecar_path}"]
        lines.extend(r.render() for r in self.results)
        return "\n".join(lines)


def build_create_plan(
    inputs: BotServiceCreateInputs,
    *,
    operator_env: dict[str, str] | None = None,
) -> BotServiceCreatePlan:
    env = operator_env if operator_env is not None else _load_operator_env()
    app_source = "--appid/--bf-app-id" if inputs.app_id else "~/.hermes/.env A365_BF_APP_ID"
    tenant_source = "--tenant-id" if inputs.tenant_id else "~/.hermes/.env A365_TENANT_ID"
    if not inputs.app_id and not env.get("A365_BF_APP_ID"):
        app_source = "missing (set --appid or A365_BF_APP_ID)"
    if not inputs.tenant_id and not env.get("A365_TENANT_ID"):
        tenant_source = "az account show (apply-time)"
    return BotServiceCreatePlan(
        inputs=inputs,
        app_id_source=app_source,
        tenant_id_source=tenant_source,
    )


def build_enable_channel_plan(
    inputs: BotServiceEnableChannelInputs,
) -> BotServiceEnableChannelPlan:
    return BotServiceEnableChannelPlan(
        inputs=inputs,
        config=BotServiceConfig.from_file(inputs.sidecar_path),
    )


def build_update_endpoint_plan(
    inputs: BotServiceUpdateEndpointInputs,
) -> BotServiceUpdateEndpointPlan:
    return BotServiceUpdateEndpointPlan(
        inputs=inputs,
        config=BotServiceConfig.from_file(inputs.sidecar_path),
    )


def build_cleanup_plan(inputs: BotServiceCleanupInputs) -> BotServiceCleanupPlan:
    if not inputs.sidecar_path.exists():
        return BotServiceCleanupPlan(inputs=inputs, config=None, sidecar_exists=False)
    return BotServiceCleanupPlan(
        inputs=inputs,
        config=BotServiceConfig.from_file(inputs.sidecar_path),
        sidecar_exists=True,
    )


def _require_success(result: CommandResult, action: str) -> CommandResult:
    if result.returncode != 0:
        detail = result.output or f"exit code {result.returncode}"
        raise BotServiceError(f"{action} failed: {detail}")
    return result


def _json_from_result(result: CommandResult, action: str) -> dict[str, Any]:
    _require_success(result, action)
    if not result.stdout:
        return {}
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise BotServiceError(f"{action} returned non-JSON output: {e}") from e
    if not isinstance(parsed, dict):
        raise BotServiceError(f"{action} returned JSON {type(parsed).__name__}, expected object")
    return parsed


_NOT_FOUND_MARKERS = ("not found", "could not be found", "resourcenotfound", "was not found")


def _is_not_found(result: CommandResult) -> bool:
    text = result.output.lower()
    return any(marker in text for marker in _NOT_FOUND_MARKERS)


def _bot_show(runner: CommandRunner, resource_group: str, bot_name: str) -> dict[str, Any] | None:
    result = runner.run(
        ["az", "bot", "show", "--resource-group", resource_group, "--name", bot_name, "-o", "json"]
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        _require_success(result, "az bot show")
    return _json_from_result(result, "az bot show")


def _group_show(runner: CommandRunner, resource_group: str) -> dict[str, Any] | None:
    result = runner.run(["az", "group", "show", "--name", resource_group, "-o", "json"])
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        _require_success(result, "az group show")
    return _json_from_result(result, "az group show")


def _msteams_show(
    runner: CommandRunner,
    resource_group: str,
    bot_name: str,
) -> dict[str, Any] | None:
    result = runner.run(
        [
            "az",
            "bot",
            "msteams",
            "show",
            "--resource-group",
            resource_group,
            "--name",
            bot_name,
            "-o",
            "json",
        ]
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        _require_success(result, "az bot msteams show")
    return _json_from_result(result, "az bot msteams show")


def _msteams_delete(runner: CommandRunner, resource_group: str, bot_name: str) -> bool:
    result = runner.run(
        [
            "az",
            "bot",
            "msteams",
            "delete",
            "--resource-group",
            resource_group,
            "--name",
            bot_name,
        ]
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return False
        _require_success(result, "az bot msteams delete")
    return True


def _bot_properties(bot: dict[str, Any]) -> dict[str, Any]:
    props = bot.get("properties")
    return props if isinstance(props, dict) else {}


def _bot_app_id(bot: dict[str, Any]) -> str:
    props = _bot_properties(bot)
    return str(props.get("msaAppId") or bot.get("msaAppId") or "")


def _bot_endpoint(bot: dict[str, Any]) -> str:
    props = _bot_properties(bot)
    return str(props.get("endpoint") or bot.get("endpoint") or "")


def _bot_resource_id(
    bot: dict[str, Any],
    subscription_id: str,
    resource_group: str,
    bot_name: str,
) -> str:
    rid = str(bot.get("id") or "")
    if rid:
        return rid
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.BotService/botServices/{bot_name}"
    )


def _enabled_channels(bot: dict[str, Any]) -> list[str]:
    props = _bot_properties(bot)
    raw = props.get("enabledChannels") or bot.get("enabledChannels") or []
    if not isinstance(raw, list):
        return []
    return sorted({str(c).lower() for c in raw if c})


def _with_channel(channels: list[str], channel: str) -> list[str]:
    return sorted({*(str(c).lower() for c in channels if c), channel.lower()})


def _teams_terms_accepted(channel: dict[str, Any] | None) -> bool:
    if not channel:
        return False
    props = channel.get("properties")
    if not isinstance(props, dict):
        return False
    nested = props.get("properties")
    if isinstance(nested, dict):
        return bool(nested.get("acceptedTerms") and nested.get("isEnabled", True))
    return bool(props.get("acceptedTerms") and props.get("isEnabled", True))


def _account_show(runner: CommandRunner) -> dict[str, Any]:
    return _json_from_result(
        runner.run(["az", "account", "show", "-o", "json"]),
        "az account show",
    )


def _resolve_app_id(inputs: BotServiceCreateInputs, operator_env: dict[str, str]) -> str:
    app_id = (inputs.app_id or operator_env.get("A365_BF_APP_ID") or "").strip()
    if app_id:
        return app_id
    raise BotServiceError(
        "Path B Bot Service must use the separate non-agentic BF app id. "
        "Pass --appid/--bf-app-id or set A365_BF_APP_ID in ~/.hermes/.env."
    )


def _resolve_tenant_id(
    inputs: BotServiceCreateInputs,
    operator_env: dict[str, str],
    account: dict[str, Any],
) -> str:
    tenant_id = (
        inputs.tenant_id
        or operator_env.get("A365_TENANT_ID")
        or account.get("tenantId")
        or ""
    )
    tenant_id = str(tenant_id).strip()
    if not tenant_id:
        raise BotServiceError("tenant id not found; pass --tenant-id or sign in with `az login`")
    return tenant_id


def _resolve_subscription_id(inputs: BotServiceCreateInputs, account: dict[str, Any]) -> str:
    subscription_id = str(inputs.subscription_id or account.get("id") or "").strip()
    if not subscription_id:
        raise BotServiceError(
            "subscription id not found; pass --subscription-id or select an Azure subscription"
        )
    return subscription_id


def _teams_channel_url(subscription_id: str, resource_group: str, bot_name: str) -> str:
    rg = quote(resource_group, safe="")
    bot = quote(bot_name, safe="")
    return (
        "https://management.azure.com/subscriptions/"
        f"{quote(subscription_id, safe='')}/resourceGroups/{rg}"
        f"/providers/Microsoft.BotService/botServices/{bot}/channels/MsTeamsChannel"
        f"?api-version={_BOT_API_VERSION}"
    )


def _patch_teams_terms(
    runner: CommandRunner,
    *,
    subscription_id: str,
    resource_group: str,
    bot_name: str,
) -> None:
    body = {
        "location": "global",
        "properties": {
            "channelName": "MsTeamsChannel",
            "properties": {
                "acceptedTerms": True,
                "isEnabled": True,
                "deploymentEnvironment": "CommercialDeployment",
            },
        },
    }
    _require_success(
        runner.run(
            [
                "az",
                "rest",
                "--method",
                "PATCH",
                "--url",
                _teams_channel_url(subscription_id, resource_group, bot_name),
                "--headers",
                "Content-Type=application/json",
                "--body",
                json.dumps(body, separators=(",", ":")),
            ]
        ),
        "az rest acceptedTerms PATCH",
    )


def _write_bot_service_config(path: Path, config: BotServiceConfig) -> None:
    _write_text_atomic(path, config.to_json(), mode=0o600)


def _backup_sidecar_path(path: Path, *, now: datetime) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    if path.name.endswith(".json"):
        return path.with_name(f"{path.name[:-5]}.backup-{stamp}.json")
    return path.with_name(f"{path.name}.backup-{stamp}")


def apply_create_plan(
    plan: BotServiceCreatePlan,
    *,
    runner: CommandRunner | None = None,
    operator_env: dict[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> BotServiceCreateResult:
    if runner is None:
        runner = SubprocessRunner()
    if operator_env is None:
        operator_env = _load_operator_env()
    if now is None:
        def now() -> datetime:
            return datetime.now(UTC)

    inputs = plan.inputs
    bot_name = plan.bot_name
    app_id = _resolve_app_id(inputs, operator_env)
    account = _account_show(runner)
    tenant_id = _resolve_tenant_id(inputs, operator_env, account)
    subscription_id = _resolve_subscription_id(inputs, account)

    messages: list[str] = []
    _require_success(
        runner.run(
            ["az", "provider", "register", "--namespace", _BOT_SERVICE_NAMESPACE, "--wait"],
            timeout=300.0,
        ),
        "az provider register",
    )
    messages.append(f"[apply] registered provider {_BOT_SERVICE_NAMESPACE}")

    existing_group = _group_show(runner, inputs.resource_group)
    resource_group_managed = existing_group is None
    _require_success(
        runner.run(
            [
                "az",
                "group",
                "create",
                "--name",
                inputs.resource_group,
                "--location",
                inputs.region,
                "-o",
                "json",
            ]
        ),
        "az group create",
    )
    if resource_group_managed:
        messages.append(f"[apply] created resource group {inputs.resource_group}")
    else:
        messages.append(f"[apply] reused resource group {inputs.resource_group}")

    created_bot = False
    bot = _bot_show(runner, inputs.resource_group, bot_name)
    if bot is None:
        bot = _json_from_result(
            runner.run(
                [
                    "az",
                    "bot",
                    "create",
                    "--resource-group",
                    inputs.resource_group,
                    "--name",
                    bot_name,
                    "--app-type",
                    "SingleTenant",
                    "--appid",
                    app_id,
                    "--tenant-id",
                    tenant_id,
                    "--endpoint",
                    inputs.endpoint,
                    "--sku",
                    inputs.sku,
                    "--location",
                    "global",
                    "-o",
                    "json",
                ],
                timeout=300.0,
            ),
            "az bot create",
        )
        created_bot = True
        messages.append(f"[apply] created bot resource {bot_name}")
    else:
        existing_app_id = _bot_app_id(bot)
        if existing_app_id and existing_app_id.lower() != app_id.lower():
            raise BotServiceError(
                f"existing bot {bot_name} is bound to msaAppId={existing_app_id}, "
                f"but Path B expects {app_id}. Azure cannot change --appid in-place; "
                "delete/recreate the bot resource deliberately."
            )
        if _bot_endpoint(bot).rstrip("/") != inputs.endpoint.rstrip("/"):
            bot = _json_from_result(
                runner.run(
                    [
                        "az",
                        "bot",
                        "update",
                        "--resource-group",
                        inputs.resource_group,
                        "--name",
                        bot_name,
                        "--endpoint",
                        inputs.endpoint,
                        "-o",
                        "json",
                    ]
                ),
                "az bot update",
            )
            messages.append(f"[apply] updated bot endpoint for {bot_name}")
        else:
            messages.append(f"[apply] bot resource {bot_name} already matches")

    created_teams_channel = False
    teams = _msteams_show(runner, inputs.resource_group, bot_name)
    if teams is None:
        _require_success(
            runner.run(
                [
                    "az",
                    "bot",
                    "msteams",
                    "create",
                    "--resource-group",
                    inputs.resource_group,
                    "--name",
                    bot_name,
                ]
            ),
            "az bot msteams create",
        )
        created_teams_channel = True
        messages.append("[apply] created Microsoft Teams channel")
        teams = _msteams_show(runner, inputs.resource_group, bot_name)

    patched_teams_terms = False
    if not _teams_terms_accepted(teams):
        _patch_teams_terms(
            runner,
            subscription_id=subscription_id,
            resource_group=inputs.resource_group,
            bot_name=bot_name,
        )
        patched_teams_terms = True
        messages.append("[apply] accepted Microsoft Teams channel terms")

    refreshed = _bot_show(runner, inputs.resource_group, bot_name) or bot
    channels = _enabled_channels(refreshed)
    if "msteams" not in channels:
        channels.append("msteams")
    cfg = BotServiceConfig(
        schemaVersion=SIDECAR_SCHEMA_VERSION,
        subscriptionId=subscription_id,
        resourceGroup=inputs.resource_group,
        botName=bot_name,
        armResourceId=_bot_resource_id(refreshed, subscription_id, inputs.resource_group, bot_name),
        msaAppId=app_id,
        tenantId=tenant_id,
        messagingEndpoint=inputs.endpoint,
        channelsEnabled=sorted(set(channels)),
        createdAt=now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        resourceGroupManaged=resource_group_managed,
    )
    _write_bot_service_config(inputs.sidecar_path, cfg)
    messages.append(f"[apply] wrote {inputs.sidecar_path} (mode 0600)")
    return BotServiceCreateResult(
        config=cfg,
        sidecar_path=inputs.sidecar_path,
        created_bot=created_bot,
        created_teams_channel=created_teams_channel,
        patched_teams_terms=patched_teams_terms,
        messages=messages,
    )


def apply_enable_channel_plan(
    plan: BotServiceEnableChannelPlan,
    *,
    runner: CommandRunner | None = None,
) -> BotServiceEnableChannelResult:
    if runner is None:
        runner = SubprocessRunner()

    config = plan.config
    inputs = plan.inputs
    messages: list[str] = []
    channel_created = False

    teams = _msteams_show(runner, config.resourceGroup, config.botName)
    if teams is None:
        _require_success(
            runner.run(
                [
                    "az",
                    "bot",
                    "msteams",
                    "create",
                    "--resource-group",
                    config.resourceGroup,
                    "--name",
                    config.botName,
                ]
            ),
            "az bot msteams create",
        )
        channel_created = True
        messages.append("[apply] created Microsoft Teams channel")
        teams = _msteams_show(runner, config.resourceGroup, config.botName)
    else:
        messages.append("[apply] Microsoft Teams channel already enabled")

    patched_teams_terms = False
    if not _teams_terms_accepted(teams):
        _patch_teams_terms(
            runner,
            subscription_id=config.subscriptionId,
            resource_group=config.resourceGroup,
            bot_name=config.botName,
        )
        patched_teams_terms = True
        messages.append("[apply] accepted Microsoft Teams channel terms")

    updated = BotServiceConfig(
        **{
            **config.__dict__,
            "channelsEnabled": _with_channel(config.channelsEnabled, inputs.channel),
        }
    )
    _write_bot_service_config(inputs.sidecar_path, updated)
    messages.append(f"[apply] wrote {inputs.sidecar_path} (mode 0600)")
    return BotServiceEnableChannelResult(
        config=updated,
        sidecar_path=inputs.sidecar_path,
        channel_created=channel_created,
        patched_teams_terms=patched_teams_terms,
        messages=messages,
    )


def apply_update_endpoint_plan(
    plan: BotServiceUpdateEndpointPlan,
    *,
    runner: CommandRunner | None = None,
) -> BotServiceUpdateEndpointResult:
    if runner is None:
        runner = SubprocessRunner()

    config = plan.config
    inputs = plan.inputs
    messages: list[str] = []

    bot = _bot_show(runner, config.resourceGroup, config.botName)
    if bot is None:
        raise BotServiceError(f"{config.botName} not found in {config.resourceGroup}")

    current = _bot_endpoint(bot)
    endpoint_updated = False
    if current.rstrip("/") != inputs.url.rstrip("/"):
        bot = _json_from_result(
            runner.run(
                [
                    "az",
                    "bot",
                    "update",
                    "--resource-group",
                    config.resourceGroup,
                    "--name",
                    config.botName,
                    "--endpoint",
                    inputs.url,
                    "-o",
                    "json",
                ]
            ),
            "az bot update",
        )
        endpoint_updated = True
        messages.append(f"[apply] updated Bot Service endpoint to {inputs.url}")
    else:
        messages.append("[apply] Bot Service endpoint already current")

    channels = sorted({*_enabled_channels(bot), *config.channelsEnabled})
    updated = BotServiceConfig(
        **{
            **config.__dict__,
            "messagingEndpoint": inputs.url,
            "channelsEnabled": sorted({str(c).lower() for c in channels if c}),
        }
    )
    _write_bot_service_config(inputs.sidecar_path, updated)
    messages.append(f"[apply] wrote {inputs.sidecar_path} (mode 0600)")
    return BotServiceUpdateEndpointResult(
        config=updated,
        sidecar_path=inputs.sidecar_path,
        endpoint_updated=endpoint_updated,
        messages=messages,
    )


def apply_cleanup_plan(
    plan: BotServiceCleanupPlan,
    *,
    runner: CommandRunner | None = None,
    now: Callable[[], datetime] | None = None,
) -> BotServiceCleanupResult:
    if runner is None:
        runner = SubprocessRunner()
    if now is None:
        def now() -> datetime:
            return datetime.now(UTC)

    inputs = plan.inputs
    result = BotServiceCleanupResult(sidecar_path=inputs.sidecar_path)
    config = plan.config
    if config is None:
        result.messages.append(
            f"[apply] no bot-service sidecar at {inputs.sidecar_path}; nothing to clean up"
        )
        result.messages.append(
            "[apply] Blueprint Entra app + service principal preserved — Path A still depends on it"
        )
        return result

    bot = _bot_show(runner, config.resourceGroup, config.botName)
    if bot is None:
        result.messages.append(
            f"[apply] no bot resource found: {config.resourceGroup}/{config.botName}"
        )
    else:
        if _msteams_delete(runner, config.resourceGroup, config.botName):
            result.messages.append("[apply] deleted Microsoft Teams channel")
        _require_success(
            runner.run(
                [
                    "az",
                    "bot",
                    "delete",
                    "--resource-group",
                    config.resourceGroup,
                    "--name",
                    config.botName,
                    "--yes",
                ]
            ),
            "az bot delete",
        )
        result.bot_deleted = True
        result.messages.append(f"[apply] deleted bot resource {config.botName}")

    if inputs.purge_resource_group:
        if config.resourceGroupManaged:
            _require_success(
                runner.run(["az", "group", "delete", "--name", config.resourceGroup, "--yes"]),
                "az group delete",
            )
            result.resource_group_deleted = True
            result.messages.append(f"[apply] deleted managed resource group {config.resourceGroup}")
        else:
            result.messages.append(
                f"[apply] skipped resource group purge for {config.resourceGroup}: "
                "sidecar resourceGroupManaged=false"
            )

    if inputs.sidecar_path.exists():
        backup = _backup_sidecar_path(inputs.sidecar_path, now=now())
        _write_text_atomic(backup, inputs.sidecar_path.read_text(), mode=0o600)
        inputs.sidecar_path.unlink()
        result.sidecar_backup_path = backup
        result.sidecar_removed = True
        result.messages.append(f"[apply] backed up sidecar to {backup}")
        result.messages.append(f"[apply] removed {inputs.sidecar_path}")

    result.messages.append(
        "[apply] Blueprint Entra app + service principal preserved — Path A still depends on it"
    )
    return result


RuntimeProbe = Callable[[BotServiceConfig, CommandRunner], ProbeResult]


def _provider_probe(runner: CommandRunner) -> ProbeResult:
    result = runner.run(
        [
            "az",
            "provider",
            "show",
            "--namespace",
            _BOT_SERVICE_NAMESPACE,
            "--query",
            "registrationState",
            "-o",
            "tsv",
        ]
    )
    if result.returncode != 0:
        return ProbeResult("provider", "ERROR", result.output or "az provider show failed")
    state = result.stdout.strip()
    if state != "Registered":
        return ProbeResult(
            "provider",
            "ERROR",
            f"{_BOT_SERVICE_NAMESPACE} registrationState={state!r}",
        )
    return ProbeResult("provider", "OK", f"{_BOT_SERVICE_NAMESPACE} Registered")


def _extract_directline_secret(data: dict[str, Any]) -> str:
    candidates: list[Any] = []
    props = data.get("properties")
    if isinstance(props, dict):
        candidates.extend([props.get("key"), props.get("key1"), props.get("key2")])
        sites = props.get("sites")
        if isinstance(sites, list):
            for site in sites:
                if isinstance(site, dict):
                    candidates.extend([site.get("key"), site.get("key1"), site.get("key2")])
    candidates.extend([data.get("key"), data.get("key1"), data.get("key2")])
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise BotServiceError("Direct Line channel secret was not present in az output")


def _http_json(
    url: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict[str, Any]]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else {}
    except error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(text) if text else {}
        except json.JSONDecodeError:
            return e.code, {"error": text}
    except OSError as e:
        raise BotServiceError(f"Direct Line probe failed before reaching Bot Service: {e}") from e


def directline_runtime_probe(config: BotServiceConfig, runner: CommandRunner) -> ProbeResult:
    """Send one Direct Line activity to catch Path B auth rejections."""
    secret_data = _json_from_result(
        runner.run(
            [
                "az",
                "bot",
                "directline",
                "show",
                "--resource-group",
                config.resourceGroup,
                "--name",
                config.botName,
                "--with-secrets",
                "true",
                "-o",
                "json",
            ]
        ),
        "az bot directline show",
    )
    secret = _extract_directline_secret(secret_data)
    status, conversation = _http_json(
        "https://directline.botframework.com/v3/directline/conversations",
        token=secret,
    )
    if status >= 400:
        return ProbeResult(
            "runtime_auth",
            "ERROR",
            f"Direct Line conversation start returned HTTP {status}: {conversation}",
        )
    conversation_id = str(conversation.get("conversationId") or "")
    token = str(conversation.get("token") or secret)
    if not conversation_id:
        return ProbeResult(
            "runtime_auth",
            "ERROR",
            f"Direct Line omitted conversationId: {conversation}",
        )
    status, response = _http_json(
        f"https://directline.botframework.com/v3/directline/conversations/{conversation_id}/activities",
        token=token,
        body={
            "type": "message",
            "from": {"id": "hermes-a365-verify"},
            "text": "hermes-a365 bot-service verify",
        },
    )
    if status >= 400:
        detail = json.dumps(response, sort_keys=True)
        if "403" in detail or "Failed to send activity" in detail or "BotError" in detail:
            return ProbeResult(
                "runtime_auth",
                "ERROR",
                "configured endpoint rejected a Path B BF Connector token "
                f"(HTTP {status}): {detail}",
            )
        return ProbeResult(
            "runtime_auth",
            "ERROR",
            f"Direct Line activity returned HTTP {status}: {detail}",
        )
    return ProbeResult("runtime_auth", "OK", "Direct Line activity accepted by Bot Service")


def _path_endpoint_parity_probe(
    config: BotServiceConfig,
    generated_config_path: Path,
    *,
    path_b_endpoint: str | None = None,
) -> ProbeResult:
    if not generated_config_path.exists():
        return ProbeResult(
            "path_endpoint_parity",
            "OK",
            f"skipped; {generated_config_path} not found",
        )
    try:
        generated = json.loads(generated_config_path.read_text())
    except json.JSONDecodeError as e:
        return ProbeResult(
            "path_endpoint_parity",
            "WARN",
            f"{generated_config_path} is not valid JSON: {e}",
        )
    if not isinstance(generated, dict):
        return ProbeResult(
            "path_endpoint_parity",
            "WARN",
            f"{generated_config_path} is JSON {type(generated).__name__}, expected object",
        )
    path_a_endpoint = str(generated.get("messagingEndpoint") or "").strip()
    if not path_a_endpoint:
        return ProbeResult(
            "path_endpoint_parity",
            "OK",
            f"skipped; {generated_config_path} has no messagingEndpoint",
        )
    bot_service_endpoint = path_b_endpoint or config.messagingEndpoint
    if path_a_endpoint.rstrip("/") != bot_service_endpoint.rstrip("/"):
        return ProbeResult(
            "path_endpoint_parity",
            "WARN",
            "Path A activity-bridge endpoint differs from Path B Bot Service endpoint: "
            f"{path_a_endpoint} != {bot_service_endpoint}. "
            "Run both activity-bridge update-endpoint and bot-service update-endpoint "
            "when operating both paths.",
        )
    return ProbeResult("path_endpoint_parity", "OK", "Path A and Path B endpoints match")


def verify_bot_service(
    sidecar_path: Path,
    *,
    runner: CommandRunner | None = None,
    runtime_probe: RuntimeProbe | None = None,
    generated_config_path: Path | None = None,
) -> BotServiceVerifyReport:
    if runner is None:
        runner = SubprocessRunner()
    if generated_config_path is None:
        generated_config_path = Path.cwd() / "a365.generated.config.json"
    config = BotServiceConfig.from_file(sidecar_path)
    results: list[ProbeResult] = [_provider_probe(runner)]

    bot = _bot_show(runner, config.resourceGroup, config.botName)
    actual_bot_endpoint: str | None = None
    if bot is None:
        results.append(
            ProbeResult(
                "bot",
                "ERROR",
                f"{config.botName} not found in {config.resourceGroup}",
            )
        )
    else:
        app_id = _bot_app_id(bot)
        endpoint = _bot_endpoint(bot)
        actual_bot_endpoint = endpoint
        if app_id.lower() != config.msaAppId.lower():
            results.append(
                ProbeResult(
                    "bot_msa_app_id",
                    "ERROR",
                    f"Azure bot msaAppId={app_id}; sidecar expects {config.msaAppId}",
                )
            )
        else:
            results.append(ProbeResult("bot_msa_app_id", "OK", f"msaAppId={app_id}"))
        if endpoint.rstrip("/") != config.messagingEndpoint.rstrip("/"):
            results.append(
                ProbeResult(
                    "bot_endpoint",
                    "WARN",
                    f"Azure endpoint={endpoint}; sidecar expects {config.messagingEndpoint}",
                )
            )
        else:
            results.append(ProbeResult("bot_endpoint", "OK", endpoint))
        channels = _enabled_channels(bot)
        missing_auto = [c for c in ("webchat", "directline") if c not in channels]
        if missing_auto:
            results.append(
                ProbeResult(
                    "auto_channels",
                    "WARN",
                    f"missing expected auto channels: {missing_auto}",
                )
            )
        else:
            results.append(ProbeResult("auto_channels", "OK", "webchat + directline present"))

    teams = _msteams_show(runner, config.resourceGroup, config.botName)
    if teams is None:
        results.append(
            ProbeResult(
                "msteams_channel",
                "ERROR",
                "Microsoft Teams channel is not enabled",
            )
        )
    elif not _teams_terms_accepted(teams):
        results.append(
            ProbeResult(
                "msteams_channel",
                "ERROR",
                "Microsoft Teams channel exists but acceptedTerms/isEnabled is false; "
                "Path B traffic will be held by Microsoft",
            )
        )
    else:
        results.append(ProbeResult("msteams_channel", "OK", "enabled with acceptedTerms=true"))

    results.append(
        _path_endpoint_parity_probe(
            config,
            generated_config_path,
            path_b_endpoint=actual_bot_endpoint,
        )
    )

    if runtime_probe is None:
        results.append(
            ProbeResult(
                "runtime_auth",
                "WARN",
                "skipped; pass --directline-probe to send a live BF Connector-token activity",
            )
        )
    else:
        try:
            results.append(runtime_probe(config, runner))
        except BotServiceError as e:
            results.append(ProbeResult("runtime_auth", "ERROR", str(e)))

    return BotServiceVerifyReport(sidecar_path=sidecar_path, results=results)


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description="hermes a365 bot-service — manage Path B Azure Bot Service resources.",
        )
    subs = parser.add_subparsers(dest="bot_service_command")

    create = subs.add_parser("create", help="Create or reconcile the Path B Azure Bot resource")
    create.add_argument("--agent-name", required=True)
    create.add_argument("--resource-group", required=True)
    create.add_argument(
        "--endpoint",
        required=True,
        help="Bot endpoint; /api/messages is appended if omitted",
    )
    create.add_argument(
        "--region",
        default=_DEFAULT_REGION,
        help=f"resource group region (default: {_DEFAULT_REGION})",
    )
    create.add_argument(
        "--sku",
        default=_DEFAULT_SKU,
        help=f"Bot Service sku (default: {_DEFAULT_SKU})",
    )
    create.add_argument("--tenant-id")
    create.add_argument("--appid", "--bf-app-id", dest="app_id")
    create.add_argument("--subscription-id")
    create.add_argument("--bot-name", help="override derived <agent-slug>-bot name")
    create.add_argument("--sidecar", type=Path, default=Path.cwd() / SIDECAR_FILENAME)
    create.add_argument("--apply", action="store_true", help="execute Azure + sidecar mutations")

    enable = subs.add_parser(
        "enable-channel",
        help="Enable a Bot Framework channel on the existing Path B bot",
    )
    enable.add_argument("--agent-name", required=True)
    enable.add_argument(
        "--channel",
        default="msteams",
        choices=["msteams"],
        help="Bot Framework channel to enable (slice 20b supports msteams)",
    )
    enable.add_argument("--sidecar", type=Path, default=Path.cwd() / SIDECAR_FILENAME)
    enable.add_argument("--apply", action="store_true", help="execute Azure + sidecar mutations")

    endpoint = subs.add_parser(
        "update-endpoint",
        help=(
            "Update Azure Bot Service Path B endpoint; Path A uses "
            "`activity-bridge update-endpoint`"
        ),
    )
    endpoint.add_argument("--agent-name", required=True)
    endpoint.add_argument(
        "--url",
        required=True,
        help="HTTPS endpoint; /api/messages is appended if omitted",
    )
    endpoint.add_argument("--sidecar", type=Path, default=Path.cwd() / SIDECAR_FILENAME)
    endpoint.add_argument("--apply", action="store_true", help="execute Azure + sidecar mutations")

    cleanup = subs.add_parser(
        "cleanup",
        help="Delete the Path B Azure Bot resource and back up/remove the sidecar",
    )
    cleanup.add_argument("--agent-name", required=True)
    cleanup.add_argument("--sidecar", type=Path, default=Path.cwd() / SIDECAR_FILENAME)
    cleanup.add_argument(
        "--purge-resource-group",
        action="store_true",
        help="delete the resource group only when the sidecar marks it as wrapper-managed",
    )
    cleanup.add_argument(
        "--confirm",
        help="must equal --agent-name for the apply path to proceed",
    )
    cleanup.add_argument("--apply", action="store_true", help="execute Azure + sidecar mutations")

    verify = subs.add_parser("verify", help="Verify the Path B Azure Bot resource from the sidecar")
    verify.add_argument(
        "--agent-name",
        help="accepted for operator symmetry; sidecar remains source of truth",
    )
    verify.add_argument("--sidecar", type=Path, default=Path.cwd() / SIDECAR_FILENAME)
    verify.add_argument(
        "--generated-config",
        type=Path,
        default=Path.cwd() / "a365.generated.config.json",
        help="Path A generated config for endpoint parity check",
    )
    verify.add_argument(
        "--directline-probe",
        action="store_true",
        help="send a live Direct Line activity to catch BF Connector-token auth failures",
    )
    return parser


def _run_create(args: argparse.Namespace) -> int:
    try:
        inputs = BotServiceCreateInputs(
            agent_name=args.agent_name,
            resource_group=args.resource_group,
            endpoint=args.endpoint,
            region=args.region,
            sku=args.sku,
            tenant_id=args.tenant_id,
            app_id=args.app_id,
            subscription_id=args.subscription_id,
            bot_name=args.bot_name,
            sidecar_path=args.sidecar,
        )
        plan = build_create_plan(inputs)
    except (ValueError, BotServiceError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")
    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to create/reconcile Bot Service.\n")
        return 0

    try:
        result = apply_create_plan(plan)
    except BotServiceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


def _run_enable_channel(args: argparse.Namespace) -> int:
    try:
        inputs = BotServiceEnableChannelInputs(
            agent_name=args.agent_name,
            channel=args.channel,
            sidecar_path=args.sidecar,
        )
        plan = build_enable_channel_plan(inputs)
    except (ValueError, BotServiceError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")
    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to enable the channel.\n")
        return 0

    try:
        result = apply_enable_channel_plan(plan)
    except BotServiceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


def _run_update_endpoint(args: argparse.Namespace) -> int:
    try:
        inputs = BotServiceUpdateEndpointInputs(
            agent_name=args.agent_name,
            url=args.url,
            sidecar_path=args.sidecar,
        )
        plan = build_update_endpoint_plan(inputs)
    except (ValueError, BotServiceError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")
    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to update Azure Bot Service.\n")
        return 0

    try:
        result = apply_update_endpoint_plan(plan)
    except BotServiceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


def _validate_confirm(agent_name: str, confirm: str | None) -> None:
    if confirm is None:
        raise BotServiceError(
            f"--confirm is required for --apply and must be the agent name literal "
            f"(e.g. --confirm={agent_name})"
        )
    if confirm != agent_name:
        raise BotServiceError(
            f"--confirm value {confirm!r} does not match agent-name {agent_name!r}; "
            "refusing to proceed"
        )


def _run_cleanup(args: argparse.Namespace) -> int:
    try:
        inputs = BotServiceCleanupInputs(
            agent_name=args.agent_name,
            sidecar_path=args.sidecar,
            purge_resource_group=args.purge_resource_group,
        )
        plan = build_cleanup_plan(inputs)
    except (ValueError, BotServiceError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")
    if not args.apply:
        sys.stdout.write(
            f"\nNo mutations. Re-run with --apply --confirm={args.agent_name} "
            "to clean up Bot Service.\n"
        )
        return 0

    try:
        _validate_confirm(args.agent_name, args.confirm)
    except BotServiceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        result = apply_cleanup_plan(plan)
    except BotServiceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    probe = directline_runtime_probe if args.directline_probe else None
    try:
        report = verify_bot_service(
            args.sidecar,
            runtime_probe=probe,
            generated_config_path=args.generated_config,
        )
    except BotServiceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(report.render_human() + "\n")
    return 0 if report.ok else 1


def run(args: argparse.Namespace) -> int:
    sub = getattr(args, "bot_service_command", None)
    if sub == "create":
        return _run_create(args)
    if sub == "enable-channel":
        return _run_enable_channel(args)
    if sub == "update-endpoint":
        return _run_update_endpoint(args)
    if sub == "cleanup":
        return _run_cleanup(args)
    if sub == "verify":
        return _run_verify(args)
    print(
        "usage: hermes-a365 bot-service "
        "{create,enable-channel,update-endpoint,cleanup,verify}",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
