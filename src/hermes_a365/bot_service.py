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
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import MISSING, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib import error, request
from urllib.parse import quote, urlparse

from ._common import (
    parse_env,
    quote_path_segment,
    slugify,
    write_owner_only_text_atomic,
)

SIDECAR_FILENAME = "a365.bot-service.config.json"
SIDECAR_SCHEMA_VERSION = 2
_SUPPORTED_SIDECAR_SCHEMA_VERSIONS = (1, SIDECAR_SCHEMA_VERSION)
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
    # #112/CS-004: O_EXCL-first owner-only write (no permissive-umask window).
    # These files carry the bot's msaAppId + config, not a secret, but they
    # route through the same hardened writer for consistency.
    write_owner_only_text_atomic(path, text, mode=mode)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _normalize_endpoint(raw: str, *, allow_local: bool = False) -> str:
    value = raw.strip()
    if not value:
        raise BotServiceError("--endpoint must be non-empty")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BotServiceError("--endpoint must be an absolute http(s) URL")
    # ``hostname`` is lowercased and bracket-stripped by urlparse, so an
    # exact set-membership test is correct (and avoids mis-bucketing
    # ``localhost.example.com`` as loopback).
    host = (parsed.hostname or "").lower()
    is_loopback = host in _LOOPBACK_HOSTS
    if is_loopback and not allow_local:
        raise BotServiceError(
            f"endpoint refuses localhost/loopback host ({value}); "
            "pass --allow-local for a local dev tunnel"
        )
    # HTTPS is required for any real (non-loopback) endpoint — the BF /
    # Copilot Bot Service fabric requires TLS. ``--allow-local`` relaxes
    # this ONLY for loopback hosts (the http://localhost dev-tunnel case);
    # a remote http:// URL is always refused.
    if parsed.scheme != "https" and not is_loopback:
        raise BotServiceError(
            f"endpoint must be HTTPS ({value}); BF Bot Service requires TLS"
        )
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


def resolve_default_region(*, runner: CommandRunner | None = None) -> tuple[str, str]:
    """Return the create-region default and where it came from.

    Operators often configure ``az config set defaults.location=<region>``.
    Respect that before falling back to the historical satscryption
    walkthrough default.
    """
    if runner is None:
        runner = SubprocessRunner()
    try:
        result = runner.run(
            ["az", "config", "get", "defaults.location", "--query", "value", "-o", "tsv"],
            timeout=10.0,
        )
    except BotServiceError:
        return _DEFAULT_REGION, "built-in fallback"
    if result.returncode == 0:
        value = result.stdout.strip()
        if value:
            return value, "az config defaults.location"
    return _DEFAULT_REGION, "built-in fallback"


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
    allow_local: bool = False

    def __post_init__(self) -> None:
        if not self.agent_name.strip():
            raise ValueError("agent_name must be non-empty")
        if not self.resource_group.strip():
            raise ValueError("resource_group must be non-empty")
        self.endpoint = _normalize_endpoint(self.endpoint, allow_local=self.allow_local)
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
    # #102 M6: which agent this sidecar was provisioned for. Optional in the
    # in-memory model so pre-existing v1 sidecars still load; newly written v2
    # sidecars require it. Old binaries reject v2 instead of silently ignoring
    # the binding, while this reader keeps the v1 warning/migration path.
    agentName: str | None = None

    @classmethod
    def from_file(cls, path: Path) -> BotServiceConfig:
        if not path.exists():
            raise BotServiceError(f"{path} does not exist; run `bot-service create --apply` first")
        try:
            text = path.read_text()
        except OSError as e:
            raise BotServiceError(f"could not read {path}: {e}") from e
        return cls.from_json_text(text, source=str(path))

    @classmethod
    def from_json_text(cls, text: str, *, source: str) -> BotServiceConfig:
        """Parse one already-bound sidecar snapshot."""
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise BotServiceError(f"{source} is not valid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise BotServiceError(f"{source} is JSON {type(raw).__name__}, expected object")
        raw.setdefault("resourceGroupManaged", False)
        missing = [
            f.name
            for f in fields(cls)
            if f.name not in raw and f.default is MISSING and f.default_factory is MISSING
        ]
        if missing:
            raise BotServiceError(f"{source} missing required keys: {missing}")
        schema_version = raw.get("schemaVersion")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version not in _SUPPORTED_SIDECAR_SCHEMA_VERSIONS
        ):
            raise BotServiceError(
                f"{source} schemaVersion={schema_version!r}; expected one of "
                f"{sorted(_SUPPORTED_SIDECAR_SCHEMA_VERSIONS)}"
            )
        if schema_version == SIDECAR_SCHEMA_VERSION:
            agent_name = raw.get("agentName")
            if not isinstance(agent_name, str) or not agent_name.strip():
                raise BotServiceError(
                    f"{source} schemaVersion={SIDECAR_SCHEMA_VERSION} requires a non-empty "
                    "agentName binding; refusing to load"
                )
        if not isinstance(raw.get("resourceGroupManaged"), bool):
            raise BotServiceError(
                f"{source} resourceGroupManaged must be true or false, got "
                f"{raw.get('resourceGroupManaged')!r}; refusing to infer purge authority"
            )
        for key in (
            "subscriptionId",
            "resourceGroup",
            "botName",
            "armResourceId",
            "msaAppId",
            "tenantId",
            "messagingEndpoint",
            "createdAt",
        ):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                if key == "subscriptionId":
                    raise BotServiceError(
                        f"{source} has a blank subscriptionId; refusing to load"
                    )
                raise BotServiceError(f"{source} requires a non-empty string {key}")
        channels = raw.get("channelsEnabled")
        if not isinstance(channels, list) or any(
            not isinstance(channel, str) or not channel.strip() for channel in channels
        ):
            raise BotServiceError(
                f"{source} channelsEnabled must be a list of non-empty strings"
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
    legacy_binding_confirmation: str | None = None
    target_confirmation: str | None = None

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
    sidecar_binding: tuple[int, int, int, str]
    sidecar_snapshot: bytes = field(repr=False)

    def render_human(self) -> str:
        legacy_note = (
            []
            if self.config.schemaVersion == SIDECAR_SCHEMA_VERSION
            else [
                "  legacy binding:",
                "    - apply requires "
                f"--confirm-legacy-binding={self.inputs.agent_name!r}",
            ]
        )
        return "\n".join(
            [
                f"[plan] hermes a365 bot-service enable-channel {self.inputs.agent_name}",
                f"  channel:        {self.inputs.channel}",
                f"  resource group: {self.config.resourceGroup}",
                f"  bot resource:   {self.config.botName}",
                f"  confirm target: {self.config.armResourceId}",
                f"  sidecar:        {self.inputs.sidecar_path}",
                *legacy_note,
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
    allow_local: bool = False
    legacy_binding_confirmation: str | None = None
    target_confirmation: str | None = None

    def __post_init__(self) -> None:
        if not self.agent_name.strip():
            raise ValueError("agent_name must be non-empty")
        self.url = _normalize_endpoint(self.url, allow_local=self.allow_local)


@dataclass
class BotServiceUpdateEndpointPlan:
    inputs: BotServiceUpdateEndpointInputs
    config: BotServiceConfig
    sidecar_binding: tuple[int, int, int, str]
    sidecar_snapshot: bytes = field(repr=False)

    def render_human(self) -> str:
        legacy_note = (
            []
            if self.config.schemaVersion == SIDECAR_SCHEMA_VERSION
            else [
                "  legacy binding:",
                "    - apply requires "
                f"--confirm-legacy-binding={self.inputs.agent_name!r}",
            ]
        )
        return "\n".join(
            [
                f"[plan] hermes a365 bot-service update-endpoint {self.inputs.agent_name}",
                f"  resource group: {self.config.resourceGroup}",
                f"  bot resource:   {self.config.botName}",
                f"  confirm target: {self.config.armResourceId}",
                f"  current URL:     {self.config.messagingEndpoint}",
                f"  new URL:         {self.inputs.url}",
                f"  sidecar:        {self.inputs.sidecar_path}",
                *legacy_note,
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
    target_confirmation: str | None = None
    legacy_binding_confirmation: str | None = None

    def __post_init__(self) -> None:
        if not self.agent_name.strip():
            raise ValueError("agent_name must be non-empty")


@dataclass
class BotServiceCleanupPlan:
    inputs: BotServiceCleanupInputs
    config: BotServiceConfig | None
    sidecar_exists: bool
    # #102 M5: best-effort plan-time enumeration of the group's contents so
    # the operator sees the purge blast radius BEFORE --apply. None == not
    # enumerated (no runner given / purge not requested / listing failed);
    # the authoritative content re-check happens again at apply time.
    resource_group_contents: list[str] | None = None
    sidecar_binding: tuple[int, int, int, str] | None = None
    sidecar_snapshot: bytes | None = field(default=None, repr=False)

    def render_human(self) -> str:
        lines = [f"[plan] hermes a365 bot-service cleanup {self.inputs.agent_name}"]
        lines.append(f"  sidecar:        {self.inputs.sidecar_path}")
        if self.config is None:
            lines.append("  bot resource:   (none; sidecar missing)")
            lines.append("  azure steps:    (none)")
        else:
            # #102 M6: surface the full deletion target, not just the names —
            # the sidecar is what picks these, so the operator must see them
            # before typing --apply --confirm.
            lines.append(f"  resource group: {self.config.resourceGroup}")
            lines.append(f"  bot resource:   {self.config.botName}")
            lines.append(f"  subscription:   {self.config.subscriptionId}")
            lines.append(f"  confirm target: {self.config.armResourceId}")
            if self.config.agentName is not None:
                lines.append(f"  provisioned for: {self.config.agentName}")
            else:
                lines.append("  provisioned for: (unbound pre-M6 sidecar; no agentName)")
                lines.append(
                    f"  legacy confirm: --confirm-legacy-binding={self.inputs.agent_name!r}"
                )
            lines.append("  azure steps:")
            lines.append("    - az bot msteams delete (best effort)")
            lines.append("    - az bot delete (skip if already gone)")
            if self.inputs.purge_resource_group:
                if self.config.resourceGroupManaged:
                    lines.append(
                        "    - enumerate the managed resource group and print a manual "
                        "delete command (never auto-delete the group)"
                    )
                    if self.resource_group_contents is not None:
                        if self.resource_group_contents:
                            lines.append("      group contents now:")
                            lines.extend(
                                f"        - {item}" for item in self.resource_group_contents
                            )
                        else:
                            lines.append("      group contents now: (empty)")
                else:
                    lines.append("    - skip az group delete (resourceGroupManaged=false)")
            else:
                lines.append("    - skip az group delete (--purge-resource-group not set)")
        lines.append("  local steps:")
        lines.append(
            "    - back up the bound sidecar snapshot and preserve the live sidecar "
            "for cloud-state readback"
        )
        lines.append("  preserved:")
        lines.append("    - Blueprint Entra app + service principal (Path A still depends on it)")
        return "\n".join(lines)


@dataclass
class BotServiceCleanupResult:
    sidecar_path: Path
    target_missing: bool = False
    bot_deleted: bool = False
    resource_group_deleted: bool = False
    resource_group_purge_pending: bool = False
    sidecar_backup_path: Path | None = None
    sidecar_removed: bool = False
    blueprint_preserved: bool = False
    blueprint_preserved_message: str | None = None
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
    config, binding, snapshot = _read_bound_sidecar(inputs.sidecar_path)
    if _sidecar_file_binding(inputs.sidecar_path) != binding:
        raise BotServiceError(f"{inputs.sidecar_path} changed while the plan was read")
    return BotServiceEnableChannelPlan(
        inputs=inputs,
        config=config,
        sidecar_binding=binding,
        sidecar_snapshot=snapshot,
    )


def build_update_endpoint_plan(
    inputs: BotServiceUpdateEndpointInputs,
) -> BotServiceUpdateEndpointPlan:
    config, binding, snapshot = _read_bound_sidecar(inputs.sidecar_path)
    if _sidecar_file_binding(inputs.sidecar_path) != binding:
        raise BotServiceError(f"{inputs.sidecar_path} changed while the plan was read")
    return BotServiceUpdateEndpointPlan(
        inputs=inputs,
        config=config,
        sidecar_binding=binding,
        sidecar_snapshot=snapshot,
    )


def build_cleanup_plan(
    inputs: BotServiceCleanupInputs,
    *,
    runner: CommandRunner | None = None,
) -> BotServiceCleanupPlan:
    """Build the (dry-run) cleanup plan.

    ``runner`` is optional (#102 M5): when given AND a managed-group purge is
    requested, the plan enumerates the group's current contents (one read-only
    az call) so the operator sees the blast radius before ``--apply``. Failure
    to enumerate degrades to no listing — the apply-time guard re-checks
    authoritatively either way. Existing callers that pass no runner (the
    top-level cleanup orchestrator) are unchanged."""
    if not inputs.sidecar_path.exists():
        return BotServiceCleanupPlan(inputs=inputs, config=None, sidecar_exists=False)
    config, binding, snapshot = _read_bound_sidecar(inputs.sidecar_path)
    if _sidecar_file_binding(inputs.sidecar_path) != binding:
        raise BotServiceError(f"{inputs.sidecar_path} changed while the cleanup plan was read")
    contents: list[str] | None = None
    if runner is not None and inputs.purge_resource_group and config.resourceGroupManaged:
        listed = _resource_list(
            runner, config.resourceGroup, subscription_id=config.subscriptionId
        )
        if listed is not None:
            contents = [
                f"{item.get('type') or '(unknown type)'}/{item.get('name') or '(unnamed)'}"
                for item in listed
            ]
    return BotServiceCleanupPlan(
        inputs=inputs,
        config=config,
        sidecar_exists=True,
        resource_group_contents=contents,
        sidecar_binding=binding,
        sidecar_snapshot=snapshot,
    )


def _read_sidecar_bytes(path: Path) -> tuple[bytes, tuple[int, int, int, str]]:
    """Read one regular sidecar revision and derive its binding from those bytes."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or path.is_symlink():
            raise BotServiceError(f"{path} must be a regular non-symlink file")
        raw = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise BotServiceError(f"could not bind cleanup sidecar {path}: {exc}") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size)
    identity_after = (after.st_dev, after.st_ino, after.st_size)
    if identity_after != identity_before:
        raise BotServiceError(f"{path} changed while it was being read")
    return raw, (*identity_after, hashlib.sha256(raw).hexdigest())


def _read_bound_sidecar(
    path: Path,
) -> tuple[BotServiceConfig, tuple[int, int, int, str], bytes]:
    raw, binding = _read_sidecar_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BotServiceError(f"{path} is not valid UTF-8: {exc}") from exc
    return BotServiceConfig.from_json_text(text, source=str(path)), binding, raw


def _sidecar_file_binding(path: Path) -> tuple[int, int, int, str]:
    """Return the binding for the exact bytes read from a sidecar."""
    _, binding = _read_sidecar_bytes(path)
    return binding


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


def _sub_args(subscription_id: str | None) -> list[str]:
    """#102 H3/L5: explicit ``--subscription`` for an az invocation.

    Every az call that reads or mutates ARM resources must bind to the
    RESOLVED subscription (create path: ``--subscription-id``/account; cleanup
    path: the sidecar's persisted ``subscriptionId``) — never the CLI's ambient
    default, which can silently differ and land resources in (or delete them
    from) the wrong subscription. Appended at the END of argv so the
    ``argv[:3]``/``argv[:4]`` prefix dispatch in tests and any operator
    eyeballing of the leading verb stay stable. Empty/None yields no args
    (az rejects ``--subscription ''``) — but note no PRODUCTION caller may
    rely on that as a fallback: create resolves-or-raises
    (``_resolve_subscription_id``) and cleanup/verify load-or-raise
    (``from_file`` rejects a blank ``subscriptionId``), so an un-pinned call
    can only arise from a caller that passed nothing at all."""
    sub = str(subscription_id or "").strip()
    return ["--subscription", sub] if sub else []


def _bot_show(
    runner: CommandRunner,
    resource_group: str,
    bot_name: str,
    *,
    subscription_id: str | None = None,
) -> dict[str, Any] | None:
    result = runner.run(
        [
            "az", "bot", "show", "--resource-group", resource_group, "--name", bot_name, "-o",
            "json",
            *_sub_args(subscription_id),
        ]
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        _require_success(result, "az bot show")
    return _json_from_result(result, "az bot show")


def _group_show(
    runner: CommandRunner,
    resource_group: str,
    *,
    subscription_id: str | None = None,
) -> dict[str, Any] | None:
    result = runner.run(
        ["az", "group", "show", "--name", resource_group, "-o", "json", *_sub_args(subscription_id)]
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        _require_success(result, "az group show")
    return _json_from_result(result, "az group show")


def _resource_list(
    runner: CommandRunner,
    resource_group: str,
    *,
    subscription_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """#102 M5: enumerate a resource group's top-level contents.

    Returns ``None`` when the listing FAILS — callers must treat that as
    "contents unknown" and fail closed (skip the purge), never as "empty".
    Failure includes RAISED failures, not just nonzero exits:
    ``SubprocessRunner.run`` raises ``BotServiceError`` for a missing az
    binary or a timeout, and letting that propagate would either crash a
    dry-run that used to work offline or abort a cleanup mid-way after the
    bot was already deleted."""
    try:
        result = runner.run(
            [
                "az", "resource", "list", "--resource-group", resource_group, "-o", "json",
                *_sub_args(subscription_id),
            ]
        )
    except BotServiceError:
        return None
    if result.returncode != 0:
        return None
    if not result.stdout.strip():
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    if any(not isinstance(item, dict) for item in parsed):
        return None
    return parsed


def _foreign_resources(
    resources: list[dict[str, Any]], config: BotServiceConfig
) -> list[str]:
    """#102 M5: which of a managed group's resources are NOT ours.

    Provisioning puts exactly ONE top-level resource in a managed group — the
    bot (``Microsoft.BotService/botServices/<botName>``; the Teams channel is a
    child of it, not a group member). Everything else appeared after we created
    the group and must not be destroyed by ``--purge-resource-group``. Matched
    case-insensitively by the full ARM id against the sidecar's
    ``armResourceId``. Type/name is display-only: it cannot override a missing
    or mismatching id and accidentally bless an unrelated resource. Returns
    human-readable ``type/name`` labels for the refusal message."""
    managed_id = (config.armResourceId or "").lower()
    foreign: list[str] = []
    for item in resources:
        arm_id = str(item.get("id") or "").lower()
        rtype = str(item.get("type") or "")
        name = str(item.get("name") or "")
        is_managed_bot = bool(managed_id and arm_id == managed_id)
        if not is_managed_bot:
            foreign.append(f"{rtype or '(unknown type)'}/{name or '(unnamed)'}")
    return foreign


def _msteams_show(
    runner: CommandRunner,
    resource_group: str,
    bot_name: str,
    *,
    subscription_id: str | None = None,
) -> dict[str, Any] | None:
    result = runner.run(
        [
            "az", "bot", "msteams", "show", "--resource-group", resource_group, "--name", bot_name,
            "-o", "json",
            *_sub_args(subscription_id),
        ]
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        _require_success(result, "az bot msteams show")
    return _json_from_result(result, "az bot msteams show")


def _msteams_delete(
    runner: CommandRunner,
    resource_group: str,
    bot_name: str,
    *,
    subscription_id: str | None = None,
) -> bool:
    result = runner.run(
        [
            "az", "bot", "msteams", "delete", "--resource-group", resource_group, "--name",
            bot_name,
            *_sub_args(subscription_id),
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


def _verify_sidecar_target(
    runner: CommandRunner,
    config: BotServiceConfig,
    *,
    agent_name: str,
    require_live: bool = True,
) -> dict[str, Any] | None:
    """Bind every mutation to the persisted tenant and immutable live identities."""
    if config.agentName is not None and config.agentName != agent_name:
        raise BotServiceError(
            f"sidecar belongs to agent {config.agentName!r}, not {agent_name!r}; "
            f"refusing target {config.resourceGroup}/{config.botName}"
        )
    account = _account_show(runner, subscription_id=config.subscriptionId)
    selected_subscription = str(account.get("id") or "").strip().lower()
    if selected_subscription != config.subscriptionId.strip().lower():
        raise BotServiceError(
            "Azure returned a different subscription than the sidecar binding; "
            "refusing mutation"
        )
    active_tenant = str(account.get("tenantId") or "").strip().lower()
    if not active_tenant or active_tenant != config.tenantId.strip().lower():
        raise BotServiceError(
            "active Azure tenant does not match the sidecar tenant; refusing mutation"
        )
    bot = _bot_show(
        runner,
        config.resourceGroup,
        config.botName,
        subscription_id=config.subscriptionId,
    )
    if bot is None:
        if require_live:
            raise BotServiceError(
                f"bound bot {config.resourceGroup}/{config.botName} was not found"
            )
        return None
    live_id = str(bot.get("id") or "").strip()
    if not live_id:
        raise BotServiceError(
            "live bot response omitted its ARM resource id; refusing mutation"
        )
    if live_id.lower() != config.armResourceId.strip().lower():
        raise BotServiceError("live bot ARM resource id does not match the sidecar binding")
    if _bot_app_id(bot).lower() != config.msaAppId.strip().lower():
        raise BotServiceError("live bot msaAppId does not match the sidecar binding")
    return bot


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


def _account_show(
    runner: CommandRunner, *, subscription_id: str | None = None
) -> dict[str, Any]:
    return _json_from_result(
        runner.run(
            ["az", "account", "show", "-o", "json", *_sub_args(subscription_id)]
        ),
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


def _app_id_drift_recovery_commands(
    inputs: BotServiceCreateInputs,
    *,
    bot_name: str,
    app_id: str,
    tenant_id: str,
    subscription_id: str,
) -> str:
    create_argv = [
        "hermes-a365",
        "bot-service",
        "create",
        "--agent-name",
        inputs.agent_name,
        "--resource-group",
        inputs.resource_group,
        "--endpoint",
        inputs.endpoint,
        "--appid",
        app_id,
        "--tenant-id",
        tenant_id,
        "--subscription-id",
        subscription_id,
        "--bot-name",
        bot_name,
        "--region",
        inputs.region,
        "--sku",
        inputs.sku,
        "--sidecar",
        str(inputs.sidecar_path),
        "--apply",
    ]
    commands = [
        # #102 H3: paste-ready recovery must pin the subscription too — the
        # operator pasting these is exactly the person whose ambient az
        # default may point elsewhere.
        [
            "az", "bot", "msteams", "delete", "--resource-group", inputs.resource_group, "--name",
            bot_name,
            *_sub_args(subscription_id),
        ],
        [
            "az", "bot", "delete", "--resource-group", inputs.resource_group, "--name", bot_name,
            *_sub_args(subscription_id),
        ],
        create_argv,
    ]
    return "\n".join(shlex.join(argv) for argv in commands)


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


@contextmanager
def _exclusive_sidecar_lock(path: Path) -> Iterator[None]:
    """Serialize every Hermes writer for one Bot Service sidecar."""
    lock_path = path.with_name(f"{path.name}.lock")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise BotServiceError(
            f"another Bot Service operation holds {lock_path}; refusing before "
            "Azure mutation. If no operation is active, inspect and remove the "
            "stale lock deliberately."
        ) from exc
    except OSError as exc:
        raise BotServiceError(f"could not acquire sidecar lock {lock_path}: {exc}") from exc
    locked_stat = os.fstat(fd)
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(fd)
        try:
            current = lock_path.lstat()
            if (current.st_dev, current.st_ino) == (
                locked_stat.st_dev,
                locked_stat.st_ino,
            ):
                lock_path.unlink()
        except FileNotFoundError:
            pass


def _backup_sidecar_path(path: Path, *, now: datetime) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    if path.name.endswith(".json"):
        return path.with_name(f"{path.name[:-5]}.backup-{stamp}.json")
    return path.with_name(f"{path.name}.backup-{stamp}")


def _bind_legacy_sidecar(
    config: BotServiceConfig,
    *,
    agent_name: str,
    path: Path,
    messages: list[str],
    confirmation: str | None,
    snapshot: bytes,
) -> BotServiceConfig:
    """Upgrade verified v1 state immediately before its first successful rewrite."""
    if config.schemaVersion == SIDECAR_SCHEMA_VERSION:
        return config
    _validate_legacy_binding_confirmation(
        config, agent_name=agent_name, confirmation=confirmation
    )
    backup = _backup_sidecar_path(path, now=datetime.now(UTC))
    _write_text_atomic(backup, snapshot.decode("utf-8"), mode=0o600)
    messages.append(f"[apply] backed up legacy sidecar to {backup}")
    messages.append(f"[apply] migrated legacy sidecar binding to agent {agent_name}")
    return BotServiceConfig(
        **{
            **config.__dict__,
            "schemaVersion": SIDECAR_SCHEMA_VERSION,
            "agentName": agent_name,
        }
    )


def _validate_legacy_binding_confirmation(
    config: BotServiceConfig,
    *,
    agent_name: str,
    confirmation: str | None,
) -> None:
    """Require an explicit name acknowledgement before mutating a v1 target."""
    if config.schemaVersion == SIDECAR_SCHEMA_VERSION:
        return
    if confirmation != agent_name:
        target = (
            f"{config.subscriptionId}/{config.resourceGroup}/{config.botName} "
            f"(app {config.msaAppId})"
        )
        raise BotServiceError(
            "schema-v1 sidecar has no agent-name binding; live identity checks "
            f"resolved target {target}. Re-run with "
            f"--confirm-legacy-binding={agent_name!r} to bind it to this agent"
        )


def _validate_cleanup_target_confirmation(
    config: BotServiceConfig, confirmation: str | None
) -> None:
    """Require exact double entry of the immutable ARM target before cleanup."""
    if confirmation != config.armResourceId:
        raise BotServiceError(
            "cleanup requires exact target acknowledgement before any Azure read or "
            f"mutation. Re-run with --confirm-bot-target={config.armResourceId!r}"
        )


def _validate_sidecar_mutation_plan(
    *,
    config: BotServiceConfig,
    agent_name: str,
    target_confirmation: str | None,
    legacy_binding_confirmation: str | None,
    sidecar_path: Path,
    sidecar_binding: tuple[int, int, int, str],
) -> None:
    """Validate every local binding before the first Azure read or mutation."""
    if config.agentName is not None and config.agentName != agent_name:
        raise BotServiceError(
            f"sidecar belongs to agent {config.agentName!r}, not {agent_name!r}; "
            f"refusing target {config.resourceGroup}/{config.botName}"
        )
    _validate_cleanup_target_confirmation(config, target_confirmation)
    _validate_legacy_binding_confirmation(
        config,
        agent_name=agent_name,
        confirmation=legacy_binding_confirmation,
    )
    if _sidecar_file_binding(sidecar_path) != sidecar_binding:
        raise BotServiceError(f"{sidecar_path} changed after planning; refusing mutation")


def apply_create_plan(
    plan: BotServiceCreatePlan,
    *,
    runner: CommandRunner | None = None,
    operator_env: dict[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> BotServiceCreateResult:
    with _exclusive_sidecar_lock(plan.inputs.sidecar_path):
        return _apply_create_plan_locked(
            plan, runner=runner, operator_env=operator_env, now=now
        )


def _apply_create_plan_locked(
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

    # Resolve the selected subscription itself before the first provider/RG
    # mutation. An explicit subscription can belong to a different tenant than
    # the tenant pin; merely adding --subscription to later commands would
    # otherwise discover that mismatch after provisioning had started.
    selected_account = _account_show(runner, subscription_id=subscription_id)
    selected_subscription = str(selected_account.get("id") or "").strip()
    selected_tenant = str(selected_account.get("tenantId") or "").strip()
    if selected_subscription.lower() != subscription_id.lower():
        raise BotServiceError(
            "az account show returned a different subscription than the resolved "
            "--subscription-id; refusing to provision"
        )
    if selected_tenant.lower() != tenant_id.lower():
        raise BotServiceError(
            f"subscription {subscription_id} belongs to tenant {selected_tenant or '(unknown)'}, "
            f"not the resolved tenant {tenant_id}; refusing before mutation"
        )

    messages: list[str] = []
    # #102 H3: every ARM read/mutate below carries the RESOLVED subscription —
    # provider registration is per-subscription, and group/bot creation would
    # otherwise land in the CLI's ambient default while config/ARM ids
    # reference the resolved one (orphaned cross-subscription resources).
    _require_success(
        runner.run(
            [
                "az", "provider", "register", "--namespace", _BOT_SERVICE_NAMESPACE, "--wait",
                *_sub_args(subscription_id),
            ],
            timeout=300.0,
        ),
        "az provider register",
    )
    messages.append(f"[apply] registered provider {_BOT_SERVICE_NAMESPACE}")

    existing_group = _group_show(
        runner, inputs.resource_group, subscription_id=subscription_id
    )
    resource_group_managed = existing_group is None
    _require_success(
        runner.run(
            [
                "az", "group", "create", "--name", inputs.resource_group, "--location",
                inputs.region, "-o", "json",
                *_sub_args(subscription_id),
            ]
        ),
        "az group create",
    )
    if resource_group_managed:
        messages.append(f"[apply] created resource group {inputs.resource_group}")
    else:
        messages.append(f"[apply] reused resource group {inputs.resource_group}")

    created_bot = False
    bot = _bot_show(runner, inputs.resource_group, bot_name, subscription_id=subscription_id)
    if bot is None:
        bot = _json_from_result(
            runner.run(
                [
                    "az", "bot", "create", "--resource-group", inputs.resource_group, "--name",
                    bot_name, "--app-type", "SingleTenant", "--appid", app_id, "--tenant-id",
                    tenant_id, "--endpoint", inputs.endpoint, "--sku", inputs.sku, "--location",
                    "global", "-o", "json",
                    *_sub_args(subscription_id),
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
                "delete/recreate the bot resource deliberately.\n\n"
                "Paste-ready recovery:\n"
                + _app_id_drift_recovery_commands(
                    inputs,
                    bot_name=bot_name,
                    app_id=app_id,
                    tenant_id=tenant_id,
                    subscription_id=subscription_id,
                )
            )
        if _bot_endpoint(bot).rstrip("/") != inputs.endpoint.rstrip("/"):
            bot = _json_from_result(
                runner.run(
                    [
                        "az", "bot", "update", "--resource-group", inputs.resource_group, "--name",
                        bot_name, "--endpoint", inputs.endpoint, "-o", "json",
                        *_sub_args(subscription_id),
                    ]
                ),
                "az bot update",
            )
            messages.append(f"[apply] updated bot endpoint for {bot_name}")
        else:
            messages.append(f"[apply] bot resource {bot_name} already matches")

    created_teams_channel = False
    teams = _msteams_show(runner, inputs.resource_group, bot_name, subscription_id=subscription_id)
    if teams is None:
        _require_success(
            runner.run(
                [
                    "az", "bot", "msteams", "create", "--resource-group", inputs.resource_group,
                    "--name", bot_name,
                    *_sub_args(subscription_id),
                ]
            ),
            "az bot msteams create",
        )
        created_teams_channel = True
        messages.append("[apply] created Microsoft Teams channel")
        teams = _msteams_show(
            runner, inputs.resource_group, bot_name, subscription_id=subscription_id
        )

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

    refreshed = (
        _bot_show(runner, inputs.resource_group, bot_name, subscription_id=subscription_id) or bot
    )
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
        # #102 M6: bind the sidecar to the agent it was provisioned for, so a
        # later cleanup can refuse a sidecar that names a different agent.
        agentName=inputs.agent_name,
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
    with _exclusive_sidecar_lock(plan.inputs.sidecar_path):
        return _apply_enable_channel_plan_locked(plan, runner=runner)


def _apply_enable_channel_plan_locked(
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

    _validate_sidecar_mutation_plan(
        config=config,
        agent_name=inputs.agent_name,
        target_confirmation=inputs.target_confirmation,
        legacy_binding_confirmation=inputs.legacy_binding_confirmation,
        sidecar_path=inputs.sidecar_path,
        sidecar_binding=plan.sidecar_binding,
    )
    _verify_sidecar_target(
        runner, config, agent_name=inputs.agent_name, require_live=True
    )

    teams = _msteams_show(
        runner, config.resourceGroup, config.botName, subscription_id=config.subscriptionId
    )
    if teams is None:
        _require_success(
            runner.run(
                [
                    "az", "bot", "msteams", "create", "--resource-group", config.resourceGroup,
                    "--name", config.botName,
                    *_sub_args(config.subscriptionId),
                ]
            ),
            "az bot msteams create",
        )
        channel_created = True
        messages.append("[apply] created Microsoft Teams channel")
        teams = _msteams_show(
            runner, config.resourceGroup, config.botName, subscription_id=config.subscriptionId
        )
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

    if _sidecar_file_binding(inputs.sidecar_path) != plan.sidecar_binding:
        raise BotServiceError(
            f"{inputs.sidecar_path} changed during channel update; refusing local rewrite"
        )
    bound = _bind_legacy_sidecar(
        config,
        agent_name=inputs.agent_name,
        path=inputs.sidecar_path,
        messages=messages,
        confirmation=inputs.legacy_binding_confirmation,
        snapshot=plan.sidecar_snapshot,
    )
    updated = BotServiceConfig(
        **{
            **bound.__dict__,
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
    with _exclusive_sidecar_lock(plan.inputs.sidecar_path):
        return _apply_update_endpoint_plan_locked(plan, runner=runner)


def _apply_update_endpoint_plan_locked(
    plan: BotServiceUpdateEndpointPlan,
    *,
    runner: CommandRunner | None = None,
) -> BotServiceUpdateEndpointResult:
    if runner is None:
        runner = SubprocessRunner()

    config = plan.config
    inputs = plan.inputs
    messages: list[str] = []

    _validate_sidecar_mutation_plan(
        config=config,
        agent_name=inputs.agent_name,
        target_confirmation=inputs.target_confirmation,
        legacy_binding_confirmation=inputs.legacy_binding_confirmation,
        sidecar_path=inputs.sidecar_path,
        sidecar_binding=plan.sidecar_binding,
    )
    bot = _verify_sidecar_target(
        runner, config, agent_name=inputs.agent_name, require_live=True
    )
    assert bot is not None

    current = _bot_endpoint(bot)
    endpoint_updated = False
    if current.rstrip("/") != inputs.url.rstrip("/"):
        bot = _json_from_result(
            runner.run(
                [
                    "az", "bot", "update", "--resource-group", config.resourceGroup, "--name",
                    config.botName, "--endpoint", inputs.url, "-o", "json",
                    *_sub_args(config.subscriptionId),
                ]
            ),
            "az bot update",
        )
        endpoint_updated = True
        messages.append(f"[apply] updated Bot Service endpoint to {inputs.url}")
    else:
        messages.append("[apply] Bot Service endpoint already current")

    channels = sorted({*_enabled_channels(bot), *config.channelsEnabled})
    if _sidecar_file_binding(inputs.sidecar_path) != plan.sidecar_binding:
        raise BotServiceError(
            f"{inputs.sidecar_path} changed during endpoint update; refusing local rewrite"
        )
    bound = _bind_legacy_sidecar(
        config,
        agent_name=inputs.agent_name,
        path=inputs.sidecar_path,
        messages=messages,
        confirmation=inputs.legacy_binding_confirmation,
        snapshot=plan.sidecar_snapshot,
    )
    updated = BotServiceConfig(
        **{
            **bound.__dict__,
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
    with _exclusive_sidecar_lock(plan.inputs.sidecar_path):
        return _apply_cleanup_plan_locked(plan, runner=runner, now=now)


def _apply_cleanup_plan_locked(
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

    def record_blueprint_preserved() -> None:
        message = (
            "[apply] Blueprint Entra app + service principal preserved — "
            "Path A still depends on it"
        )
        result.blueprint_preserved = True
        result.blueprint_preserved_message = message
        result.messages.append(message)

    config = plan.config
    if config is None:
        result.target_missing = True
        result.messages.append(
            f"[apply] no bot-service sidecar at {inputs.sidecar_path}; nothing to clean up"
        )
        record_blueprint_preserved()
        return result

    if config.agentName is not None and config.agentName != inputs.agent_name:
        raise BotServiceError(
            f"sidecar belongs to agent {config.agentName!r}, not {inputs.agent_name!r}; "
            f"refusing target {config.resourceGroup}/{config.botName}"
        )
    _validate_cleanup_target_confirmation(config, inputs.target_confirmation)
    _validate_legacy_binding_confirmation(
        config,
        agent_name=inputs.agent_name,
        confirmation=inputs.legacy_binding_confirmation,
    )
    if plan.sidecar_binding is None:
        raise BotServiceError("cleanup plan is missing its sidecar file binding")
    if _sidecar_file_binding(inputs.sidecar_path) != plan.sidecar_binding:
        raise BotServiceError(
            f"{inputs.sidecar_path} changed after planning; refusing cleanup"
        )

    # Refuse BEFORE any deletion when the sidecar is legacy or belongs to
    # another agent. The classic footgun is running cleanup for agent X from a
    # directory holding agent Y's sidecar, which would delete Y's bot and, with
    # --purge-resource-group, potentially Y's whole managed group.
    # #102 L5: every cleanup az call pins the sidecar's persisted
    # subscriptionId — deletes must target the subscription the resources were
    # provisioned in, never the CLI's ambient default.
    subscription_id = config.subscriptionId
    bot = _verify_sidecar_target(
        runner,
        config,
        agent_name=inputs.agent_name,
        require_live=False,
    )
    if bot is None:
        result.messages.append(
            f"[apply] no bot resource found: {config.resourceGroup}/{config.botName}"
        )
    else:
        if _msteams_delete(
            runner, config.resourceGroup, config.botName, subscription_id=subscription_id
        ):
            result.messages.append("[apply] deleted Microsoft Teams channel")
        # `az bot delete` is non-interactive (no confirm prompt) and rejects
        # `--yes`; only `--name` and `--resource-group` are accepted.
        _require_success(
            runner.run(
                [
                    "az", "bot", "delete", "--resource-group", config.resourceGroup, "--name",
                    config.botName,
                    *_sub_args(subscription_id),
                ]
            ),
            "az bot delete",
        )
        result.bot_deleted = True
        result.messages.append(f"[apply] deleted bot resource {config.botName}")

    if inputs.purge_resource_group:
        if config.resourceGroupManaged:
            group = _group_show(
                runner, config.resourceGroup, subscription_id=subscription_id
            )
            if group is None:
                result.messages.append(
                    f"[apply] managed resource group {config.resourceGroup} is already absent"
                )
            else:
                # #102 M5: re-check the group's contents at apply time. We created
                # this group holding exactly one top-level resource. Azure has
                # no conditional group delete, so even a clean inventory
                # cannot be coupled atomically to deletion. Always leave the
                # final group delete to the operator and retain the sidecar
                # until a subsequent run reads the group back as absent.
                leftovers = _resource_list(
                    runner, config.resourceGroup, subscription_id=subscription_id
                )
                foreign = (
                    None if leftovers is None else _foreign_resources(leftovers, config)
                )
                result.resource_group_purge_pending = True
                if leftovers is None:
                    result.messages.append(
                        f"[apply] resource group purge remains pending for "
                        f"{config.resourceGroup}: could not enumerate its contents; "
                        "refusing to delete a group with unknown contents"
                    )
                elif foreign:
                    listing = ", ".join(foreign)
                    result.messages.append(
                        f"[apply] resource group purge remains pending for "
                        f"{config.resourceGroup}: it holds {len(foreign)} "
                        f"non-Hermes-managed or unverified resource(s) [{listing}]"
                    )
                else:
                    manual = shlex.join(
                        [
                            "az",
                            "group",
                            "delete",
                            "--name",
                            config.resourceGroup,
                            "--yes",
                            *_sub_args(subscription_id),
                        ]
                    )
                    result.messages.append(
                        f"[apply] resource group purge remains pending for "
                        f"{config.resourceGroup}: no non-Hermes top-level resources "
                        "were found, but Azure cannot make the inventory check and "
                        "group deletion atomic; review and run manually, then re-run "
                        f"cleanup for readback: {manual}"
                    )
        else:
            result.messages.append(
                f"[apply] skipped resource group purge for {config.resourceGroup}: "
                "sidecar resourceGroupManaged=false"
            )

    if inputs.sidecar_path.exists() and not result.resource_group_purge_pending:
        if _sidecar_file_binding(inputs.sidecar_path) != plan.sidecar_binding:
            raise BotServiceError(
                f"{inputs.sidecar_path} changed during cleanup; refusing to back up or remove it"
            )
        if plan.sidecar_snapshot is None:
            raise BotServiceError("cleanup plan is missing its bound sidecar snapshot")
        backup = _backup_sidecar_path(inputs.sidecar_path, now=now())
        _write_text_atomic(backup, plan.sidecar_snapshot.decode("utf-8"), mode=0o600)
        result.sidecar_backup_path = backup
        result.messages.append(f"[apply] backed up sidecar to {backup}")
        result.messages.append(
            f"[apply] preserved {inputs.sidecar_path} as post-cleanup provenance; "
            "remove it manually only after cloud-state readback"
        )
    elif result.resource_group_purge_pending:
        result.messages.append(
            f"[apply] preserved {inputs.sidecar_path} until the requested managed "
            "resource-group purge is read back as complete"
        )

    record_blueprint_preserved()
    return result


RuntimeProbe = Callable[[BotServiceConfig, CommandRunner], ProbeResult]


def _provider_probe(
    runner: CommandRunner, *, subscription_id: str | None = None
) -> ProbeResult:
    result = runner.run(
        [
            "az", "provider", "show", "--namespace", _BOT_SERVICE_NAMESPACE, "--query",
            "registrationState", "-o", "tsv",
            *_sub_args(subscription_id),
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
    # `az bot directline show --with-secrets` returns the channel as
    # `data.properties.properties.sites[].key` (note: doubly-nested
    # `properties`). Some versions also expose a sibling `data.resource.
    # properties.sites[]` copy. Older / mocked shapes use the simpler
    # `data.properties.sites[]`. Walk all known paths and keep the
    # top-level fallback so we degrade gracefully if az adds another
    # wrapper.
    candidates: list[Any] = []

    def _collect_from_props(props: Any) -> None:
        if not isinstance(props, dict):
            return
        candidates.extend([props.get("key"), props.get("key1"), props.get("key2")])
        sites = props.get("sites")
        if isinstance(sites, list):
            for site in sites:
                if isinstance(site, dict):
                    candidates.extend([site.get("key"), site.get("key1"), site.get("key2")])

    outer = data.get("properties")
    _collect_from_props(outer)
    if isinstance(outer, dict):
        _collect_from_props(outer.get("properties"))
    resource = data.get("resource")
    if isinstance(resource, dict):
        _collect_from_props(resource.get("properties"))
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
                "az", "bot", "directline", "show", "--resource-group", config.resourceGroup,
                "--name", config.botName, "--with-secrets", "true", "-o", "json",
                *_sub_args(config.subscriptionId),
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
        # #103/L8: conversation_id comes from the Direct Line start-conversation
        # response; percent-encode it so it can't break out of the path segment
        # or smuggle a query/fragment into the probe URL.
        "https://directline.botframework.com/v3/directline/conversations/"
        f"{quote_path_segment(conversation_id)}/activities",
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
    # #102 H3/L5: verify reads pin the sidecar's subscription too — probing
    # the ambient subscription would report bogus missing/OK for the wrong
    # target.
    results: list[ProbeResult] = [
        _provider_probe(runner, subscription_id=config.subscriptionId)
    ]

    bot = _bot_show(
        runner, config.resourceGroup, config.botName, subscription_id=config.subscriptionId
    )
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

    teams = _msteams_show(
        runner, config.resourceGroup, config.botName, subscription_id=config.subscriptionId
    )
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
        help=(
            "resource group region (default: az config defaults.location, "
            f"then {_DEFAULT_REGION})"
        ),
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
    create.add_argument(
        "--allow-local",
        action="store_true",
        help="permit a localhost/loopback endpoint over http (local dev tunnels)",
    )

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
    enable.add_argument(
        "--confirm-legacy-binding",
        metavar="AGENT_NAME",
        help="required to bind a verified schema-v1 sidecar; must equal --agent-name",
    )
    enable.add_argument(
        "--confirm-bot-target",
        metavar="ARM_RESOURCE_ID",
        help="must exactly match the sidecar ARM resource id for apply",
    )

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
    endpoint.add_argument(
        "--confirm-legacy-binding",
        metavar="AGENT_NAME",
        help="required to bind a verified schema-v1 sidecar; must equal --agent-name",
    )
    endpoint.add_argument(
        "--confirm-bot-target",
        metavar="ARM_RESOURCE_ID",
        help="must exactly match the sidecar ARM resource id for apply",
    )
    endpoint.add_argument(
        "--allow-local",
        action="store_true",
        help="permit a localhost/loopback endpoint over http (local dev tunnels)",
    )

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
    cleanup.add_argument(
        "--confirm-bot-target",
        metavar="ARM_RESOURCE_ID",
        help="must exactly match the sidecar ARM resource id for cleanup apply",
    )
    cleanup.add_argument(
        "--confirm-legacy-binding",
        metavar="AGENT_NAME",
        help="required with a schema-v1 sidecar; must equal --agent-name",
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
        help=(
            "Path A generated config for endpoint parity check "
            "(default: ./a365.generated.config.json in the current working "
            "directory; pass this when running verify from another cwd)"
        ),
    )
    verify.add_argument(
        "--directline-probe",
        action="store_true",
        help="send a live Direct Line activity to catch BF Connector-token auth failures",
    )
    return parser


def _run_create(args: argparse.Namespace) -> int:
    try:
        region = args.region
        if region is None:
            region, region_source = resolve_default_region()
            sys.stdout.write(f"[info] defaulting --region to {region} ({region_source})\n")
        inputs = BotServiceCreateInputs(
            agent_name=args.agent_name,
            resource_group=args.resource_group,
            endpoint=args.endpoint,
            region=region,
            sku=args.sku,
            tenant_id=args.tenant_id,
            app_id=args.app_id,
            subscription_id=args.subscription_id,
            bot_name=args.bot_name,
            sidecar_path=args.sidecar,
            allow_local=args.allow_local,
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
            legacy_binding_confirmation=args.confirm_legacy_binding,
            target_confirmation=args.confirm_bot_target,
        )
        plan = build_enable_channel_plan(inputs)
    except (ValueError, BotServiceError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")
    if not args.apply:
        apply_args = ["--apply", f"--confirm-bot-target={plan.config.armResourceId}"]
        if plan.config.agentName is None:
            apply_args.append(f"--confirm-legacy-binding={args.agent_name}")
        sys.stdout.write(
            f"\nNo mutations. Re-run with {shlex.join(apply_args)} "
            "to enable the channel.\n"
        )
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
            allow_local=args.allow_local,
            legacy_binding_confirmation=args.confirm_legacy_binding,
            target_confirmation=args.confirm_bot_target,
        )
        plan = build_update_endpoint_plan(inputs)
    except (ValueError, BotServiceError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")
    if not args.apply:
        apply_args = ["--apply", f"--confirm-bot-target={plan.config.armResourceId}"]
        if plan.config.agentName is None:
            apply_args.append(f"--confirm-legacy-binding={args.agent_name}")
        sys.stdout.write(
            f"\nNo mutations. Re-run with {shlex.join(apply_args)} "
            "to update Azure Bot Service.\n"
        )
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
            target_confirmation=args.confirm_bot_target,
            legacy_binding_confirmation=args.confirm_legacy_binding,
        )
        # #102 M5: give the plan a real runner so a purge dry-run can
        # enumerate the group's contents (build_cleanup_plan only issues the
        # read-only listing when a managed-group purge is actually requested).
        plan = build_cleanup_plan(inputs, runner=SubprocessRunner())
    except (ValueError, BotServiceError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(plan.render_human() + "\n")
    if not args.apply:
        # #102 M6: restate the sidecar-selected deletion target next to the
        # confirm instruction — the sidecar picks the target, so the operator
        # must see it at the moment they are told what to type.
        target = ""
        if plan.config is not None:
            target = (
                f" This will delete bot {plan.config.botName!r} in resource group "
                f"{plan.config.resourceGroup!r} (subscription "
                f"{plan.config.subscriptionId})."
            )
        apply_args = [
            "--apply",
            f"--confirm={args.agent_name}",
            (
                f"--confirm-bot-target={plan.config.armResourceId}"
                if plan.config is not None
                else "--confirm-bot-target=<ARM-ID>"
            ),
        ]
        if plan.config is not None and plan.config.agentName is None:
            apply_args.append(f"--confirm-legacy-binding={args.agent_name}")
        sys.stdout.write(
            f"\nNo mutations.{target} Re-run with {shlex.join(apply_args)} "
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
    return 1 if result.target_missing or result.resource_group_purge_pending else 0


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
