"""hermes a365 doctor — read-only environment probe.

Targets the .NET ``a365`` CLI (the only variant that ships at GA —
verified 2026-05-04) and checks the prereqs the real CLI's own
``setup requirements`` enforces:

- ``a365`` CLI present + version
- ``az`` CLI present, version ≥ matrix floor, signed in
- ``pwsh`` (PowerShell 7+) on PATH — the CLI shells out to it for some
  steps; missing pwsh causes ``setup requirements`` to fail.
- Custom Entra client app (named ``Agent 365 CLI`` by Microsoft's
  convention) discoverable via the signed-in ``az`` context.
- Network reachability for ``login.microsoftonline.com`` and
  ``graph.microsoft.com``.
- OS keychain backend.
- Local config (``~/.hermes/.env``).
- Hermes harness version.
- Entra agentIdentity app ids of the configured blueprint that are
  absent from ``A365_AGENT_IDENTITY_IDS`` (hint only; a failed Graph
  lookup does not warn).

Frontier Preview Program enrollment is not auto-verifiable; doctor
mentions it in prose. Deeper, auth-requiring checks (license posture,
tenant-side Azure subscription role) are deferred to ``a365 setup
requirements``, which the operator runs separately when bootstrapping.

Exit codes:
- 0 — all probes ``ok``
- 1 — at least one ``warn``, no ``error``
- 2 — at least one ``error``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from . import bot_service, bot_service_diagnostics
from ._common import parse_env, safe_run, tcp_reachable
from .activity_bridge import parse_agent_identity_ids
from .compatibility import (
    A365_CLI_LIVE_VERIFIED,
    AZURE_CLI_MIN,
    HERMES_GATEWAY_PYTHON,
    HERMES_MULTIPLEX_MIN_CALENDAR,
    HERMES_PLUGIN_MIN_CALENDAR,
    HERMES_SESSION_PIN_RESOLUTION_MIN_CALENDAR,
    POWERSHELL_MIN_MAJOR,
    PYTHON_VERSIONS_CI,
    REQUIRES_PYTHON,
    format_calendar_tag,
    hermes_meets_multiplex_floor,
    hermes_meets_plugin_floor,
    hermes_meets_session_pin_resolution_floor,
    host_platform_supported,
    parse_hermes_version_text,
    python_in_ci_matrix,
    python_in_supported_range,
    python_version_tuple,
)

ProbeState = Literal["ok", "warn", "error"]

_OK = "ok"
_WARN = "warn"
_ERROR = "error"

_DEFAULT_NETWORK_HOSTS = ("login.microsoftonline.com", "graph.microsoft.com")
_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

# Microsoft's docs use this display name for the operator-managed custom
# client app the A365 CLI runs as. Operators can name theirs differently
# but the spec / docs default is this string; doctor reports a warning if
# nothing matches, with the URL the operator follows to fix it.
DEFAULT_CLIENT_APP_NAME = "Agent 365 CLI"

# Slice 18r (bug #3): operators with a non-default client-app name can
# set this env var to skip the rename. Looked up at probe time so the
# shell `export` route works without re-importing.
CLIENT_APP_NAME_ENV = "A365_CLIENT_APP_NAME"
CUSTOM_CLIENT_APP_DOCS = (
    "https://learn.microsoft.com/microsoft-agent-365/developer/custom-client-app-registration"
)
FRONTIER_PROGRAM_URL = "https://adoption.microsoft.com/copilot/frontier-program/"

# Slice 20 / issue #35: microsoft/Agent365-devTools#408 (macOS/Linux
# `agentBlueprintClientSecret: null` after `setup blueprint`) was marked
# fixed upstream, but a 2026-05-15 live R9 walkthrough still reproduced
# the persistence gap on 1.1.181. Keep this probe conservative until a
# later CLI build is live-verified.
A365_CLI_SECRET_LATEST_AFFECTED_VERSION = A365_CLI_LIVE_VERIFIED
A365_CLI_SECRET_LATEST_AFFECTED_VERSION_TEXT = ".".join(
    str(part) for part in A365_CLI_LIVE_VERIFIED
)
A365_CLI_NUGET_URL = "https://www.nuget.org/packages/Microsoft.Agents.A365.DevTools.Cli"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    name: str
    state: ProbeState
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_a365_cli() -> ProbeResult:
    """v0.2 only ships the .NET CLI; no variant detection."""
    binary = shutil.which("a365")
    if not binary:
        return ProbeResult(
            "a365_cli",
            _ERROR,
            "a365 not found on PATH (install via "
            "`dotnet tool install -g Microsoft.Agents.A365.DevTools.Cli --prerelease`)",
        )
    version_text = safe_run([binary, "--version"], timeout=10.0) or ""
    version_first = version_text.splitlines()[0] if version_text else "version unknown"
    parsed = parse_a365_cli_version(version_text)
    data: dict[str, Any] = {"path": binary, "version_raw": version_text}
    if parsed is None:
        return ProbeResult(
            "a365_cli",
            _WARN,
            (
                f"present at {binary}; {version_first}; could not confirm "
                "whether this CLI build still has the Microsoft#408 secret "
                "persistence regression; keep `register --auto-recover-secret` "
                "enabled for live setup"
            ),
            data,
        )
    data["version"] = ".".join(str(part) for part in parsed)
    if parsed <= A365_CLI_SECRET_LATEST_AFFECTED_VERSION:
        return ProbeResult(
            "a365_cli",
            _WARN,
            (
                f"present at {binary}; {version_first}; Microsoft#408 "
                "secret persistence is live-verified affected through "
                f"{A365_CLI_SECRET_LATEST_AFFECTED_VERSION_TEXT}; pass "
                "`register --auto-recover-secret` for live setup"
            ),
            {
                **data,
                "latest_known_affected_version": A365_CLI_SECRET_LATEST_AFFECTED_VERSION_TEXT,
                "upgrade": (
                    "dotnet tool update -g "
                    "Microsoft.Agents.A365.DevTools.Cli --prerelease"
                ),
                "nuget": A365_CLI_NUGET_URL,
            },
        )
    return ProbeResult(
        "a365_cli",
        _WARN,
        (
            f"present at {binary}; {version_first}; "
            "newer than the latest live-verified affected build "
            f"({A365_CLI_SECRET_LATEST_AFFECTED_VERSION_TEXT}) but not "
            "yet verified fixed; keep `register --auto-recover-secret` "
            "enabled for live setup"
        ),
        {
            **data,
            "latest_known_affected_version": A365_CLI_SECRET_LATEST_AFFECTED_VERSION_TEXT,
        },
    )


def parse_az_cli_version(text: str) -> tuple[int, int, int] | None:
    """Extract Azure CLI ``major.minor.patch`` from ``az version -o json`` output."""

    trimmed = text.strip()
    if trimmed.startswith("{"):
        try:
            payload = json.loads(trimmed)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key in ("azure-cli", "azure_cli"):
                value = payload.get(key)
                if isinstance(value, str):
                    parsed = _semver_triple_from_text(value)
                    if parsed is not None:
                        return parsed
    return _semver_triple_from_text(text)


def _semver_triple_from_text(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_a365_cli_version(text: str) -> tuple[int, int, int] | None:
    """Extract the first semantic ``major.minor.patch`` from CLI output.

    Observed shapes include plain ``1.1.171+11c378141d`` and prose like
    ``Agent 365 Developer Tools CLI v1.1.171``. Build metadata and
    preview suffixes are irrelevant for the doctor floor check.
    """
    match = re.search(r"\bv?(\d+)\.(\d+)\.(\d+)(?:[.+-][0-9A-Za-z.-]+)?\b", text)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def probe_az_cli() -> ProbeResult:
    binary = shutil.which("az")
    min_label = ".".join(str(part) for part in AZURE_CLI_MIN)
    if not binary:
        return ProbeResult(
            "az_cli",
            _ERROR,
            f"az not found on PATH (install Azure CLI ≥ {min_label})",
        )
    version_text = safe_run([binary, "version", "-o", "json"], timeout=10.0) or ""
    parsed_version = parse_az_cli_version(version_text)
    data: dict[str, Any] = {"path": binary}
    if parsed_version is not None:
        data["version"] = ".".join(str(part) for part in parsed_version)

    signed_in = safe_run([binary, "account", "show", "--query", "user.name", "-o", "tsv"])
    if not signed_in:
        return ProbeResult(
            "az_cli",
            _WARN,
            f"present at {binary}; not signed in (run `az login`)",
            data,
        )
    user = signed_in.strip()
    data["user"] = user
    if parsed_version is None:
        return ProbeResult(
            "az_cli",
            _WARN,
            (
                f"present and signed in as {user} ({binary}); "
                "could not parse `az version` output"
            ),
            data,
        )
    if parsed_version < AZURE_CLI_MIN:
        version_label = data["version"]
        return ProbeResult(
            "az_cli",
            _WARN,
            (
                f"present and signed in as {user} ({binary}); "
                f"version {version_label} is below the required ≥ {min_label}"
            ),
            data,
        )
    return ProbeResult(
        "az_cli",
        _OK,
        f"present and signed in as {user} ({binary}); version {data['version']}",
        data,
    )


def probe_powershell() -> ProbeResult:
    """PowerShell 7+ as ``pwsh`` — the A365 CLI shells out to it."""
    binary = shutil.which("pwsh")
    if not binary:
        return ProbeResult(
            "powershell",
            _ERROR,
            "pwsh not found on PATH; install PowerShell 7+ "
            "(https://learn.microsoft.com/powershell/scripting/install/installing-powershell)",
        )
    version_raw = safe_run([binary, "-Command", "$PSVersionTable.PSVersion.ToString()"]) or ""
    version = version_raw.strip() or "unknown"
    major = version.split(".", 1)[0] if version != "unknown" else ""
    if version != "unknown" and (
        not major.isdigit() or int(major) < POWERSHELL_MIN_MAJOR
    ):
        return ProbeResult(
            "powershell",
            _WARN,
            f"present at {binary}; version {version} (CLI requires "
            f"{POWERSHELL_MIN_MAJOR}+)",
            {"path": binary, "version": version},
        )
    return ProbeResult(
        "powershell",
        _OK,
        f"present at {binary}; version {version}",
        {"path": binary, "version": version},
    )


def probe_custom_client_app(*, name: str | None = None) -> ProbeResult:
    """Best-effort: ask az whether the operator-managed client app exists.

    A miss isn't fatal — operators may have named the app differently —
    but it's a warn so the operator can confirm. The CLI itself will
    fail clearly later if the app doesn't exist or its permissions are
    wrong (see ``a365 setup requirements`` for the authoritative check).

    If ``name`` is not given, reads ``A365_CLIENT_APP_NAME`` from the
    environment, falling back to :data:`DEFAULT_CLIENT_APP_NAME`. The
    underlying ``a365`` CLI hard-codes the default; operators with a
    different name need to either rename the Entra app to match (the
    appId stays stable) or accept that the CLI itself won't find it
    until they do.
    """
    if name is None:
        name = os.environ.get(CLIENT_APP_NAME_ENV) or DEFAULT_CLIENT_APP_NAME
    if not shutil.which("az"):
        return ProbeResult(
            "custom_client_app",
            _WARN,
            "az CLI not on PATH; cannot look up custom client app",
        )
    out = safe_run(
        [
            "az",
            "ad",
            "app",
            "list",
            "--display-name",
            name,
            "--query",
            "[].appId",
            "-o",
            "tsv",
        ],
        timeout=15.0,
    )
    if out is None:
        # `az ad app list` may also fail if not signed in — surface that.
        return ProbeResult(
            "custom_client_app",
            _WARN,
            f"could not query Entra for app {name!r} (az not signed in?)",
            {"display_name": name},
        )
    out = out.strip()
    if not out:
        return ProbeResult(
            "custom_client_app",
            _WARN,
            f"no Entra app named {name!r} in tenant; register one per {CUSTOM_CLIENT_APP_DOCS}",
            {"display_name": name, "docs": CUSTOM_CLIENT_APP_DOCS},
        )
    app_id = out.splitlines()[0].strip()
    return ProbeResult(
        "custom_client_app",
        _OK,
        f"{name!r} exists (appId={app_id[:8]}…)",
        {"display_name": name, "app_id": app_id},
    )


def probe_network(hosts: tuple[str, ...] = _DEFAULT_NETWORK_HOSTS) -> ProbeResult:
    unreachable = [h for h in hosts if not tcp_reachable(h)]
    if unreachable:
        return ProbeResult(
            "network",
            _ERROR,
            f"unreachable: {unreachable}",
            {"checked": list(hosts), "unreachable": unreachable},
        )
    return ProbeResult(
        "network",
        _OK,
        f"reachable: {list(hosts)}",
        {"checked": list(hosts)},
    )


def probe_python_runtime() -> ProbeResult:
    ver = python_version_tuple()
    label = f"{ver[0]}.{ver[1]}.{ver[2]}"
    if not python_in_supported_range(ver):
        return ProbeResult(
            "python",
            _ERROR,
            f"Python {label} is below the v1 minimum (3.11); "
            f"supported range is {REQUIRES_PYTHON}",
            {"version": label},
        )
    if not python_in_ci_matrix(ver):
        return ProbeResult(
            "python",
            _WARN,
            f"Python {label} meets {REQUIRES_PYTHON} but is outside the CI matrix "
            f"({', '.join(PYTHON_VERSIONS_CI)}); not continuously tested",
            {"version": label},
        )
    detail = f"Python {label} is within {REQUIRES_PYTHON}"
    state: ProbeState = _OK
    if ver[:2] != (3, 14):
        detail += (
            f"; Hermes gateway plugin runtime targets Python {HERMES_GATEWAY_PYTHON} "
            f"(this interpreter is fine for the standalone CLI)"
        )
    return ProbeResult("python", state, detail, {"version": label})


def probe_host_platform() -> ProbeResult:
    if sys.platform == "win32":
        return ProbeResult(
            "host_platform",
            _ERROR,
            "Windows is unsupported for v1: Hermes-A365 has no OS secret-store "
            "backend (macOS Keychain / Linux libsecret only). The GA A365 CLI "
            "can persist blueprint secrets via DPAPI on Windows, but this package "
            "does not read them — use macOS or Linux for end-to-end operator flows",
            {"platform": sys.platform},
        )
    if not host_platform_supported():
        return ProbeResult(
            "host_platform",
            _ERROR,
            f"unsupported platform: {sys.platform} (v1 supports macOS + Linux)",
            {"platform": sys.platform},
        )
    name = "macOS" if sys.platform == "darwin" else "Linux"
    return ProbeResult(
        "host_platform",
        _OK,
        f"{name} ({sys.platform}) is a supported v1 host",
        {"platform": sys.platform},
    )


def probe_keychain() -> ProbeResult:
    if sys.platform == "darwin":
        binary = shutil.which("security")
        if not binary:
            return ProbeResult(
                "keychain",
                _ERROR,
                "macOS `security` not on PATH (Security framework wrapper)",
            )
        return ProbeResult(
            "keychain",
            _OK,
            f"macOS Security framework available for reads ({binary}); writes require "
            "HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI=1",
        )
    if sys.platform.startswith("linux"):
        binary = shutil.which("secret-tool")
        if not binary:
            return ProbeResult(
                "keychain",
                _ERROR,
                "Linux `secret-tool` not found (install libsecret-tools / libsecret-1-0)",
            )
        return ProbeResult("keychain", _OK, f"libsecret available ({binary})")
    return ProbeResult(
        "keychain",
        _ERROR,
        f"unsupported platform: {sys.platform} (v1 supports macOS + Linux)",
    )


def probe_local_config() -> ProbeResult:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    home = Path(os.path.expanduser(raw))
    env_file = home / ".env"
    config_file = home / "config.yaml"

    parts: list[str] = [str(home)]
    state: ProbeState = _OK

    if env_file.exists():
        try:
            parsed = parse_env(env_file.read_text())
        except OSError as e:
            return ProbeResult(
                "local_config",
                _ERROR,
                f".env unreadable: {e}",
                {"home": str(home)},
            )
        parts.append(f".env: {len(parsed)} keys")
        tenant_id = parsed.get("A365_TENANT_ID", "").strip()
        if not tenant_id:
            state = _ERROR
            parts.append(
                "no canonical tenant pin was resolved; set A365_TENANT_ID "
                "before Azure mutations"
            )
    else:
        parts.append(".env: absent or empty")
        state = _WARN

    parts.append("config.yaml: " + ("present" if config_file.exists() else "absent"))

    return ProbeResult(
        "local_config",
        state,
        " | ".join(parts),
        {
            "home": str(home),
            "env_present": env_file.exists(),
            "config_yaml_present": config_file.exists(),
            "tenant_pinned": bool(
                env_file.exists() and parsed.get("A365_TENANT_ID", "").strip()
            ),
        },
    )


def _hermes_agent365_plugin_in_use() -> bool:
    """True when the Hermes gateway is configured to load the agent365 plugin."""
    home = Path(os.path.expanduser(os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT))
    cfg = home / "config.yaml"
    if not cfg.is_file():
        return False
    try:
        import yaml

        data = yaml.safe_load(cfg.read_text()) or {}
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    plugins = data.get("plugins")
    if isinstance(plugins, dict):
        enabled = plugins.get("enabled")
        if isinstance(enabled, (list, tuple, set)) and "agent365" in enabled:
            return True
    gw = data.get("gateway")
    if isinstance(gw, dict):
        platforms = gw.get("platforms")
        if isinstance(platforms, dict):
            agent365 = platforms.get("agent365")
            if isinstance(agent365, dict) and agent365.get("enabled"):
                return True
    return False


def _agent365_file_hosts_from_config() -> tuple[str, list[str], bool]:
    """Read ``gateway.platforms.agent365.extra.file_host_allowlist`` from the
    default ``$HERMES_HOME/config.yaml``, mirroring the adapter's precedence.

    Returns ``(status, hosts, multiplex)`` where ``multiplex`` is
    ``gateway.multiplex_profiles`` (truthy) and ``status`` is:

    - ``"set"`` — the key is present with a non-null value (``hosts`` may be an
      empty list for an explicit ``[]`` / ``""`` / a non-str/non-list scalar,
      which the adapter also treats as no hosts). The profile is authoritative;
      the adapter ignores the env in this case.
    - ``"key_absent"`` — config readable but the key is missing or explicitly
      ``null`` (the adapter falls back to the env var).
    - ``"unreadable"`` — config.yaml absent, or the parser/pyyaml is unavailable,
      or a parse error — the profile can't be inspected. Only the DEFAULT profile
      is visible here; multiplex profiles under ``profiles/<name>/`` are never
      inspected (the caller downgrades to indeterminate when multiplex is on)."""
    home = Path(os.path.expanduser(os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT))
    cfg = home / "config.yaml"
    if not cfg.exists():
        return ("unreadable", [], False)
    try:
        import yaml

        data = yaml.safe_load(cfg.read_text()) or {}
    except Exception:
        return ("unreadable", [], False)
    gw = data.get("gateway") if isinstance(data, dict) else None
    multiplex = bool(gw.get("multiplex_profiles")) if isinstance(gw, dict) else False
    node: Any = data
    for key in ("gateway", "platforms", "agent365", "extra"):
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return ("key_absent", [], multiplex)
    raw = node.get("file_host_allowlist") if isinstance(node, dict) else None
    if raw is None:
        # key missing OR explicitly null → the adapter's ``extra.get(...) is None``
        # branch falls through to the env var, so mirror that here.
        return ("key_absent", [], multiplex)
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []  # non-str/non-list (int/bool/mapping) → no hosts (adapter parity)
    return ("set", [str(h).strip() for h in items if str(h).strip()], multiplex)


def probe_file_transfer() -> ProbeResult:
    """#76 file transfer is fail-closed on host allowlisting (R2/R3/R4/R5). Reports
    whether a tenant SharePoint/OneDrive host is pinned, **mirroring the adapter's
    precedence**: the profile ``extra.file_host_allowlist`` wins whenever the key
    is present (an explicit empty list disables it — env is NOT consulted); the
    ``A365_FILE_HOST_ALLOWLIST`` env var applies only when the profile key is
    absent. When the profile config can't be read (no config.yaml / no pyyaml /
    parse error) the state is *indeterminate*, never a false 'disabled'. Never an
    error — file transfer is optional."""
    status, cfg_hosts, multiplex = _agent365_file_hosts_from_config()
    if multiplex:
        # Only the DEFAULT profile's config is visible here; under
        # multiplex_profiles the active profile may pin a different (or no) host,
        # so a determinate verdict would mislead. Report indeterminate + what the
        # default profile shows for context.
        seen = (
            f"default profile pins {len(cfg_hosts)} host(s)"
            if status == "set" and cfg_hosts
            else "default profile sets an empty allowlist"
            if status == "set"
            else "default profile key absent"
        )
        return ProbeResult(
            "file_transfer",
            _OK,
            "#76 file transfer state indeterminate — gateway.multiplex_profiles is "
            f"on and only the default profile is inspected here ({seen}); check each "
            "profile's extra.file_host_allowlist",
            {"source": "indeterminate-multiplex", "host_count": len(cfg_hosts)},
        )
    if status == "set":
        if cfg_hosts:
            return ProbeResult(
                "file_transfer",
                _OK,
                "#76 file transfer enabled via profile extra.file_host_allowlist "
                f"— {len(cfg_hosts)} pinned tenant host(s)",
                {"source": "profile", "host_count": len(cfg_hosts)},
            )
        return ProbeResult(
            "file_transfer",
            _OK,
            "#76 file transfer disabled — the profile sets an empty "
            "extra.file_host_allowlist (fail-closed; env is not consulted when the "
            "profile pins the key)",
            {"source": "profile-empty", "host_count": 0},
        )
    if status == "key_absent":
        raw = os.environ.get("A365_FILE_HOST_ALLOWLIST", "")
        env_hosts = [h.strip() for h in raw.split(",") if h.strip()]
        if env_hosts:
            return ProbeResult(
                "file_transfer",
                _OK,
                "#76 file transfer enabled via A365_FILE_HOST_ALLOWLIST "
                f"(profile key absent) — {len(env_hosts)} pinned tenant host(s)",
                {"source": "env", "host_count": len(env_hosts)},
            )
        return ProbeResult(
            "file_transfer",
            _OK,
            "#76 file transfer disabled (fail-closed) — the default profile and "
            "A365_FILE_HOST_ALLOWLIST both unset; set extra.file_host_allowlist "
            "(per profile) or the env var to the tenant SharePoint/OneDrive host(s)",
            {"source": "none", "host_count": 0},
        )
    return ProbeResult(
        "file_transfer",
        _OK,
        "#76 file transfer state indeterminate — the profile config.yaml can't be "
        "read here (absent / unparseable); its extra.file_host_allowlist (which "
        "takes precedence over the env var) may still enable or disable it",
        {"source": "indeterminate", "host_count": 0},
    )


_GRAPH_AGENT_IDENTITY_COLLECTION = (
    "https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentity"
)
_MAX_AGENT_IDENTITY_BLUEPRINTS = 4
_MAX_AGENT_IDENTITY_PAGES = 3


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        return parse_env(path.read_text())
    except OSError:
        return {}


def _plugin_agent_identity_ids(home: Path) -> Any:
    """Return ``extra.agent_identity_ids`` from the default profile, or None."""
    cfg = home / "config.yaml"
    if not cfg.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(cfg.read_text()) or {}
    except Exception:
        return None
    node: Any = data
    for key in ("gateway", "platforms", "agent365", "extra"):
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return None
    if not isinstance(node, dict) or "agent_identity_ids" not in node:
        return None
    return node.get("agent_identity_ids")


def _generated_blueprint_id(home: Path) -> str:
    candidates = [
        os.environ.get("A365_GENERATED_CONFIG_PATH") or "",
        str(Path.cwd() / "a365.generated.config.json"),
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


def _collect_agent_identity_targets() -> list[tuple[str, frozenset[str]]]:
    """Return ``(blueprint app id, allowlist)`` pairs the local config names.

    Gateway precedence matches the adapter: process
    ``A365_AGENT_IDENTITY_IDS`` when set, else the operator ``.env``, else
    ``extra.agent_identity_ids``. Each per-agent ``.env`` contributes its
    own allowlist for its ``A365_APP_ID``. Ids are unioned per blueprint
    so an instance listed for the runtime that serves it does not warn.
    """
    home = Path(os.path.expanduser(os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT))
    home_env = _read_env_file(home / ".env")
    gateway_blueprint = (
        (os.environ.get("A365_APP_ID") or "").strip()
        or (home_env.get("A365_APP_ID") or "").strip()
        or _generated_blueprint_id(home)
    )
    if "A365_AGENT_IDENTITY_IDS" in os.environ:
        gateway_allow: Any = os.environ.get("A365_AGENT_IDENTITY_IDS", "")
    elif "A365_AGENT_IDENTITY_IDS" in home_env:
        gateway_allow = home_env.get("A365_AGENT_IDENTITY_IDS", "")
    else:
        gateway_allow = _plugin_agent_identity_ids(home)

    by_blueprint: dict[str, tuple[str, set[str]]] = {}

    def add(blueprint: str, raw_allow: Any) -> None:
        text = blueprint.strip()
        if not text:
            return
        key = text.lower()
        current = by_blueprint.get(key)
        if current is None:
            current = (text, set())
            by_blueprint[key] = current
        current[1].update(parse_agent_identity_ids(raw_allow))

    if gateway_blueprint:
        add(gateway_blueprint, gateway_allow)
    agents = home / "agents"
    if agents.is_dir():
        for env_path in sorted(agents.glob("*/.env")):
            parsed = _read_env_file(env_path)
            agent_blueprint = (parsed.get("A365_APP_ID") or "").strip() or gateway_blueprint
            add(agent_blueprint, parsed.get("A365_AGENT_IDENTITY_IDS"))
    return [(blueprint, frozenset(allow)) for blueprint, allow in by_blueprint.values()]


def _az_rest_json(url: str) -> dict[str, Any] | None:
    """GET one Graph URL via ``az rest``. Stdout only, so warnings stay out of the JSON."""
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
            text=True,
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _list_blueprint_agent_identities(blueprint_app_id: str) -> list[dict[str, Any]] | None:
    """List agentIdentity service principals for one blueprint app id.

    Returns ``None`` when the lookup cannot run. The filter value is a
    canonical UUID so it cannot change the OData query shape.
    """
    try:
        canonical = str(UUID(blueprint_app_id))
    except ValueError:
        return None
    query = urllib.parse.urlencode(
        {
            "$select": "appId,agentIdentityBlueprintId",
            "$filter": f"agentIdentityBlueprintId eq '{canonical}'",
            "$top": "100",
        }
    )
    url = f"{_GRAPH_AGENT_IDENTITY_COLLECTION}?{query}"
    rows: list[dict[str, Any]] = []
    for page in range(_MAX_AGENT_IDENTITY_PAGES):
        payload = _az_rest_json(url)
        if payload is None:
            return None if page == 0 else rows
        value = payload.get("value")
        if not isinstance(value, list):
            return None if page == 0 else rows
        rows.extend(item for item in value if isinstance(item, dict))
        next_link = payload.get("@odata.nextLink")
        if not isinstance(next_link, str) or not next_link:
            return rows
        url = next_link
    return rows


def _unlisted_agent_identity_ids(
    blueprint_app_id: str,
    allowlist: frozenset[str],
    identities: list[dict[str, Any]],
) -> list[str]:
    """App ids of this blueprint's agent identities that the guard would refuse."""
    blueprint = blueprint_app_id.strip().lower()
    allowed = {item.strip().lower() for item in allowlist if item.strip()}
    unlisted: list[str] = []
    seen: set[str] = set()
    for item in identities:
        app_id = str(item.get("appId") or "").strip()
        if not app_id:
            continue
        folded = app_id.lower()
        claimed = str(item.get("agentIdentityBlueprintId") or "").strip().lower()
        if claimed and claimed != blueprint:
            continue
        if folded == blueprint or folded in allowed or folded in seen:
            continue
        seen.add(folded)
        unlisted.append(app_id)
    return unlisted


def probe_agent_instance_allowlist(
    *,
    blueprint_app_id: str | None = None,
    allowlist: tuple[str, ...] | frozenset[str] | None = None,
    identities: list[dict[str, Any]] | None = None,
    skip_reason: str | None = None,
) -> ProbeResult:
    """Warn when an instance agentIdentity of the blueprint is not allowlisted.

    A missing blueprint, ``--no-network``, or a Graph lookup that cannot
    run stays ``ok``. The warning is only a positive finding: Graph
    returned an agentIdentity whose ``agentIdentityBlueprintId`` is the
    configured blueprint and whose ``appId`` is neither that blueprint
    nor ``A365_AGENT_IDENTITY_IDS`` / ``extra.agent_identity_ids``.
    """
    if skip_reason:
        return ProbeResult(
            "agent_instance_allowlist",
            _OK,
            f"skipped; {skip_reason}",
        )
    if blueprint_app_id is None or allowlist is None:
        targets = _collect_agent_identity_targets()
    else:
        targets = [(blueprint_app_id, frozenset(allowlist))]
    targets = [(bp, allow) for bp, allow in targets if bp.strip()]
    if not targets:
        return ProbeResult(
            "agent_instance_allowlist",
            _OK,
            "skipped; no blueprint app id configured "
            "(A365_APP_ID or generated config agentBlueprintId)",
        )
    unlisted: list[str] = []
    checked = 0
    lookup_failed = False
    for blueprint, allow in targets[:_MAX_AGENT_IDENTITY_BLUEPRINTS]:
        rows = (
            identities
            if identities is not None
            else _list_blueprint_agent_identities(blueprint)
        )
        if rows is None:
            lookup_failed = True
            continue
        checked += 1
        unlisted.extend(_unlisted_agent_identity_ids(blueprint, allow, rows))
    if checked == 0 and lookup_failed:
        return ProbeResult(
            "agent_instance_allowlist",
            _OK,
            "skipped; could not list Entra agent identities for the configured blueprint",
        )
    if not unlisted:
        return ProbeResult(
            "agent_instance_allowlist",
            _OK,
            "configured blueprint agent identities are allowlisted or absent",
            {"checked_blueprints": checked},
        )
    shown = ", ".join(unlisted[:5])
    extra = f" (+{len(unlisted) - 5} more)" if len(unlisted) > 5 else ""
    return ProbeResult(
        "agent_instance_allowlist",
        _WARN,
        "Entra agentIdentity app id(s) of this blueprint are not in "
        "A365_AGENT_IDENTITY_IDS or extra.agent_identity_ids: "
        f"{shown}{extra}. Teams Create-instance replies under those ids "
        "are refused until they are allowlisted.",
        {"unlisted_app_ids": unlisted[:20]},
    )


def probe_hermes_harness() -> ProbeResult:
    binary = shutil.which("hermes")
    if not binary:
        return ProbeResult(
            "hermes_harness",
            _WARN,
            "hermes not on PATH (standalone CLI works; gateway plugin needs the "
            f"Hermes harness on Python {HERMES_GATEWAY_PYTHON})",
        )
    out = safe_run([binary, "--version"], timeout=10.0) or ""
    first = out.splitlines()[0] if out else "version unknown"
    parsed = parse_hermes_version_text(out)
    data: dict[str, Any] = {"path": binary, "version_raw": out}
    if parsed is not None:
        data["parsed_kind"] = parsed[0]
        data["parsed_version"] = list(parsed[1])

    plugin_ok = hermes_meets_plugin_floor(parsed)
    multiplex_ok = hermes_meets_multiplex_floor(parsed)
    pin_ok = hermes_meets_session_pin_resolution_floor(parsed)
    _, _, multiplex_cfg = _agent365_file_hosts_from_config()
    plugin_in_use = _hermes_agent365_plugin_in_use()

    if plugin_ok is False:
        floor = (
            f"{format_calendar_tag(HERMES_PLUGIN_MIN_CALENDAR)} / "
            "Hermes Agent 0.12.0+"
        )
        if plugin_in_use or multiplex_cfg:
            return ProbeResult(
                "hermes_harness",
                _ERROR,
                f"{first} ({binary}) is below the v1 plugin floor ({floor})",
                data,
            )
        return ProbeResult(
            "hermes_harness",
            _WARN,
            (
                f"{first} ({binary}) is below the v1 plugin floor ({floor}); "
                "standalone CLI use is fine — upgrade Hermes before enabling "
                "the agent365 gateway plugin"
            ),
            data,
        )
    if multiplex_cfg and multiplex_ok is False:
        return ProbeResult(
            "hermes_harness",
            _ERROR,
            (
                f"{first} ({binary}) is below the multiplex floor "
                f"({format_calendar_tag(HERMES_MULTIPLEX_MIN_CALENDAR)}); "
                "set gateway.multiplex_profiles: false or upgrade Hermes"
            ),
            {**data, "multiplex_profiles": True},
        )

    state: ProbeState = _OK
    detail = f"{first} ({binary})"
    if parsed is None:
        state = _WARN
        detail += "; could not parse version against the v1 Hermes matrix"
    elif pin_ok is False:
        state = _WARN
        detail += (
            "; below the Copilot→Teams session-pin resolution floor "
            f"({format_calendar_tag(HERMES_SESSION_PIN_RESOLUTION_MIN_CALENDAR)}); "
            "handoff pins may be skipped"
        )
    elif plugin_ok is None or multiplex_ok is None:
        state = _WARN
        detail += "; Hermes version unverified against the v1 matrix"
    return ProbeResult("hermes_harness", state, detail, data)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class DoctorReport:
    probes: list[ProbeResult]

    @property
    def overall(self) -> ProbeState:
        if any(p.state == _ERROR for p in self.probes):
            return _ERROR
        if any(p.state == _WARN for p in self.probes):
            return _WARN
        return _OK


def overall_to_exit_code(o: ProbeState) -> int:
    return {"ok": 0, "warn": 1, "error": 2}[o]


def _map_bot_service_probe(result: bot_service_diagnostics.DiagnosticResult) -> ProbeResult:
    state: ProbeState = _WARN if result.state == "skipped" else result.state
    return ProbeResult(result.name, state, result.detail, result.data)


def run_all_probes(
    *,
    no_network: bool = False,
    bot_service_sidecar_path: Path | None = None,
    generated_config_path: Path | None = None,
    bot_service_runner: bot_service.CommandRunner | None = None,
    bot_service_operator_env: dict[str, str] | None = None,
    bot_service_runtime_auth_probe: bot_service_diagnostics.RuntimeAuthProbe | None = None,
) -> DoctorReport:
    probes: list[ProbeResult] = []
    probes.append(probe_python_runtime())
    probes.append(probe_host_platform())
    probes.append(probe_a365_cli())
    probes.append(probe_az_cli())
    probes.append(probe_powershell())
    probes.append(probe_custom_client_app())
    if not no_network:
        probes.append(probe_network())
        probes.append(probe_agent_instance_allowlist())
    else:
        probes.append(
            probe_agent_instance_allowlist(skip_reason="--no-network")
        )
    probes.append(probe_keychain())
    probes.append(probe_local_config())
    probes.append(probe_file_transfer())
    if bot_service_sidecar_path is None:
        bot_service_sidecar_path = Path.cwd() / bot_service.SIDECAR_FILENAME
    if generated_config_path is None:
        generated_config_path = Path.cwd() / "a365.generated.config.json"
    probes.extend(
        _map_bot_service_probe(result)
        for result in bot_service_diagnostics.collect_bot_service_diagnostics(
            sidecar_path=bot_service_sidecar_path,
            generated_config_path=generated_config_path,
            no_network=no_network,
            runner=bot_service_runner,
            operator_env=bot_service_operator_env,
            runtime_auth_probe=bot_service_runtime_auth_probe,
        )
    )
    probes.append(probe_hermes_harness())
    return DoctorReport(probes=probes)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_human(report: DoctorReport) -> str:
    name_w = max((len(p.name) for p in report.probes), default=4)
    name_w = max(name_w, len("probe"))
    lines = ["hermes a365 doctor", "-" * 30]
    for p in report.probes:
        marker = {_OK: "[ ok ]", _WARN: "[WARN]", _ERROR: "[FAIL]"}[p.state]
        lines.append(f"  {marker}  {p.name:<{name_w}}  {p.detail}")
    lines.append("")
    lines.append(f"overall: {report.overall}")
    lines.append("")
    lines.append(
        "Frontier Preview Program enrollment is not auto-verifiable; "
        f"verify your tenant at {FRONTIER_PROGRAM_URL}."
    )
    lines.append("Deeper auth-requiring checks: `a365 setup requirements`")
    return "\n".join(lines) + "\n"


def render_json(report: DoctorReport) -> str:
    return (
        json.dumps(
            {
                "overall": report.overall,
                "probes": [asdict(p) for p in report.probes],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description="hermes a365 doctor — read-only environment probe.",
        )
    parser.add_argument("--human", action="store_true", help="formatted output for terminals")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="skip network reachability probes (offline diagnostic)",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    report = run_all_probes(no_network=args.no_network)
    sys.stdout.write(render_human(report) if args.human else render_json(report))
    return overall_to_exit_code(report.overall)


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
